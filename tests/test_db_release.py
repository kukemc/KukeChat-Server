"""等网络 / 慢 IO 之前把数据库连接还回去：release_connection 本身，以及几条改过顺序的路径。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.models  # noqa: F401
from app import integrations
from app.core.config import settings
from app.core.database import Base, has_uncommitted_write, release_connection
from app.models.conversation import Conversation, ConversationMember
from app.models.enums import ConversationType, MemberRole, MessageType
from app.models.message import Message
from app.models.user import User
from app.services import group_emojis as emoji_service
from app.services import messages as message_service
from app.services import moderation as moderation_service
from app.services import response_cache
from app.services.storage import StoredObject
from integration_fakes import FakeCcwGateway

CREATION = "5f0000000000000000000001"


class _Base(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{Path(self._temp.name) / 'release.db'}")
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with self.sessions() as db:
            owner = User(username="owner", hashed_password="x")
            db.add(owner)
            await db.flush()
            group = Conversation(type=ConversationType.GROUP, title="群")
            db.add(group)
            await db.flush()
            db.add(ConversationMember(conversation_id=group.id, user_id=owner.id, role=MemberRole.OWNER))
            await db.commit()
            self.owner_id, self.group_id = owner.id, group.id

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        self._temp.cleanup()


class ReleaseConnectionTests(_Base):
    async def test_read_only_session_is_committed_and_connection_returned(self) -> None:
        async with self.sessions() as db:
            user = await db.get(User, self.owner_id)
            self.assertTrue(db.in_transaction())
            self.assertFalse(await has_uncommitted_write(db))
            self.assertTrue(await release_connection(db))
            self.assertFalse(db.in_transaction())
            # 对象没有过期，之后接着用
            self.assertEqual(user.username, "owner")

    async def test_pending_or_flushed_write_is_kept(self) -> None:
        async with self.sessions() as db:
            user = await db.get(User, self.owner_id)
            user.nickname = "改了一半"
            self.assertTrue(await has_uncommitted_write(db))
            self.assertFalse(await release_connection(db))
            # flush 之后 db.dirty 清空了，但写还没提交：同样不能提交
            await db.flush()
            self.assertFalse(db.dirty)
            self.assertTrue(await has_uncommitted_write(db))
            self.assertFalse(await release_connection(db))
            await db.rollback()
        async with self.sessions() as db:
            self.assertIsNone((await db.get(User, self.owner_id)).nickname)

    async def test_core_update_counts_as_write(self) -> None:
        from sqlalchemy import update

        async with self.sessions() as db:
            await db.execute(update(User).where(User.id == self.owner_id).values(nickname="直接执行"))
            self.assertTrue(await has_uncommitted_write(db))
            self.assertFalse(await release_connection(db))
            await db.rollback()

    async def test_untouched_session_is_noop(self) -> None:
        async with self.sessions() as db:
            self.assertTrue(await release_connection(db))
            self.assertFalse(db.in_transaction())


class GroupEmojiOrderTests(_Base):
    async def test_image_moderation_runs_without_transaction(self) -> None:
        """以前先插入 StickerAsset 并 flush 再去取图、过审核，整段拿着写锁。现在网络都在写之前，而且不占连接。"""
        seen: list[tuple[bool, bool]] = []
        db_holder: dict = {}

        async def moderate(image_url, user_id, context_id=None):
            db = db_holder["db"]
            seen.append((db.in_transaction(), await has_uncommitted_write(db)))
            return {"decision": "allow", "severity": 0}

        with (
            patch.object(emoji_service.sticker_service, "_resolve_source", lambda source: ("stickers/a.jpg", "image/jpeg")),
            patch.object(emoji_service, "get_object", lambda key, max_bytes=None: StoredObject(data=b"x", content_type="image/jpeg")),
            patch.object(moderation_service, "_call_image_moderation_for_user", moderate),
            patch.object(settings, "content_moderation_enabled", True),
        ):
            async with self.sessions() as db:
                db_holder["db"] = db
                owner = await db.get(User, self.owner_id)
                emoji, asset = await emoji_service.create_emoji(db, self.group_id, owner, "狗头", "/uploads/messages/a.png")
        self.assertEqual(seen, [(False, False)])
        self.assertEqual((emoji.name, asset.storage_path), ("狗头", "stickers/a.jpg"))

    async def test_rejected_image_leaves_nothing_behind(self) -> None:
        with (
            patch.object(emoji_service.sticker_service, "_resolve_source", lambda source: ("stickers/b.jpg", "image/jpeg")),
            patch.object(emoji_service, "get_object", lambda key, max_bytes=None: StoredObject(data=b"x", content_type="image/jpeg")),
            patch.object(moderation_service, "_call_image_moderation_for_user", AsyncMock(return_value={"decision": "block", "severity": 1})),
            patch.object(settings, "content_moderation_enabled", True),
        ):
            async with self.sessions() as db:
                owner = await db.get(User, self.owner_id)
                with self.assertRaises(Exception):
                    await emoji_service.create_emoji(db, self.group_id, owner, "狗头", "/uploads/messages/b.png")
        from app.models.sticker import StickerAsset

        async with self.sessions() as db:
            self.assertEqual((await db.execute(select(StickerAsset))).scalars().all(), [])


class MessagePreviewInTransactionTests(_Base):
    async def test_preview_skipped_when_caller_has_uncommitted_writes(self) -> None:
        """调用方在自己的写事务里落消息（commit=False）时不去 CCW 取卡片：不能带着写锁等网络。"""
        calls: list[str] = []

        def fetch(oid: str, access_key: str) -> dict:
            calls.append(oid)
            return {"body": {"oid": oid, "title": "t", "creationRelease": {}}}

        content = f"看看 https://www.ccw.site/detail/{CREATION}"
        with (
            integrations.use_ccw_gateway(FakeCcwGateway(creation_fetch=fetch)),
            patch.object(response_cache, "get_ccw_creation", return_value=None),
            patch.object(response_cache, "set_ccw_creation"),
        ):
            async with self.sessions() as db:
                owner = await db.get(User, self.owner_id)
                owner.nickname = "同一个事务里的写"
                await db.flush()
                message = await message_service.send_message(db, self.group_id, owner, content, MessageType.TEXT, None, commit=False)
                await db.commit()
                self.assertNotIn("ccw_creations", message.metadata_json or "")
            self.assertEqual(calls, [])
            # 普通发消息（会话里没有写）照常取，取的时候已经不占连接
            async with self.sessions() as db:
                owner = await db.get(User, self.owner_id)
                message = await message_service.send_message(db, self.group_id, owner, content, MessageType.TEXT, None)
            self.assertEqual(calls, [CREATION])
        async with self.sessions() as db:
            stored = (await db.execute(select(Message).where(Message.id == message.id))).scalar_one()
            self.assertIn("ccw_creations", stored.metadata_json or "")


if __name__ == "__main__":
    unittest.main()
