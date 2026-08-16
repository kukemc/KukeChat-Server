"""游戏模式的边界测试。

游戏模式会让一个群的近期消息对所有游玩者（含匿名）可见，所以这里锁定
三条约束：只有群主能开、只有公开群能开、只有绑定了当前作品的群能接入。
"""

from __future__ import annotations

import unittest

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.models  # noqa: F401
from app.core.database import Base
from app.models.conversation import Conversation, ConversationMember
from app.models.enums import ConversationType, MemberRole
from app.models.message import Message
from app.models.user import User
from app.schemas.game import GameModeUpdate
from app.services import game_mode as game_service

CREATION_OID = "66d52d2366bfcb0e0b42e7c8"


class GameModeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    _seed_counter = 0

    async def _seed(self, join_mode: str = "open") -> tuple[int, int, int]:
        """建一个群。返回 (群主 id, 普通成员 id, 会话 id)。

        用户名带序号，允许同一个用例里多次调用（例如遍历各种 join_mode）。
        """
        GameModeTests._seed_counter += 1
        n = GameModeTests._seed_counter
        async with self.sessions() as db:
            owner = User(username=f"owner{n}", hashed_password="x", nickname="群主")
            member = User(username=f"member{n}", hashed_password="x", nickname="成员")
            outsider = User(username=f"outsider{n}", hashed_password="x", nickname="路人")
            db.add_all([owner, member, outsider])
            await db.flush()

            conversation = Conversation(type=ConversationType.GROUP, title="游戏群", join_mode=join_mode)
            db.add(conversation)
            await db.flush()
            db.add_all(
                [
                    ConversationMember(conversation_id=conversation.id, user_id=owner.id, role=MemberRole.OWNER),
                    ConversationMember(conversation_id=conversation.id, user_id=member.id, role=MemberRole.MEMBER),
                ]
            )
            db.add(Message(conversation_id=conversation.id, sender_id=member.id, content="群里的历史消息"))
            await db.commit()
            self.outsider_id = outsider.id
            return owner.id, member.id, conversation.id

    async def _user(self, db, user_id: int) -> User:
        return await db.get(User, user_id)

    async def test_owner_can_enable_on_public_group(self) -> None:
        owner_id, _, conversation_id = await self._seed(join_mode="open")
        async with self.sessions() as db:
            result = await game_service.set_game_mode(
                db,
                conversation_id,
                await self._user(db, owner_id),
                GameModeUpdate(enabled=True, creation_oid=CREATION_OID),
            )
            self.assertTrue(result.enabled)
            self.assertEqual(result.creation_oid, CREATION_OID)

    async def test_non_owner_cannot_enable(self) -> None:
        _, member_id, conversation_id = await self._seed()
        async with self.sessions() as db:
            with self.assertRaises(HTTPException) as caught:
                await game_service.set_game_mode(
                    db,
                    conversation_id,
                    await self._user(db, member_id),
                    GameModeUpdate(enabled=True, creation_oid=CREATION_OID),
                )
            self.assertEqual(caught.exception.status_code, 403)

    async def test_private_group_cannot_enable(self) -> None:
        """审批制 / 邀请制的群不能开启 —— 否则等于把私密聊天记录公开。"""
        for join_mode in ("approval", "invite_only", "question"):
            with self.subTest(join_mode=join_mode):
                owner_id, _, conversation_id = await self._seed(join_mode=join_mode)
                async with self.sessions() as db:
                    with self.assertRaises(HTTPException) as caught:
                        await game_service.set_game_mode(
                            db,
                            conversation_id,
                            await self._user(db, owner_id),
                            GameModeUpdate(enabled=True, creation_oid=CREATION_OID),
                        )
                    self.assertEqual(caught.exception.status_code, 400)

    async def test_enable_requires_creation_oid(self) -> None:
        owner_id, _, conversation_id = await self._seed()
        async with self.sessions() as db:
            with self.assertRaises(HTTPException) as caught:
                await game_service.set_game_mode(
                    db,
                    conversation_id,
                    await self._user(db, owner_id),
                    GameModeUpdate(enabled=True, creation_oid=None),
                )
            self.assertEqual(caught.exception.status_code, 422)

    async def test_resolve_rejects_unbound_group(self) -> None:
        """没开游戏模式的群不能被接入。"""
        _, _, conversation_id = await self._seed()
        async with self.sessions() as db:
            with self.assertRaises(HTTPException) as caught:
                await game_service.resolve_game_conversation(db, conversation_id, CREATION_OID)
            self.assertEqual(caught.exception.status_code, 403)

    async def test_resolve_rejects_wrong_creation(self) -> None:
        """群绑定了作品 A，就不能从作品 B 接入。"""
        owner_id, _, conversation_id = await self._seed()
        async with self.sessions() as db:
            await game_service.set_game_mode(
                db,
                conversation_id,
                await self._user(db, owner_id),
                GameModeUpdate(enabled=True, creation_oid=CREATION_OID),
            )
            with self.assertRaises(HTTPException) as caught:
                await game_service.resolve_game_conversation(db, conversation_id, "0123456789abcdef01234567")
            self.assertEqual(caught.exception.status_code, 403)

    async def test_anonymous_session_is_read_only(self) -> None:
        owner_id, _, conversation_id = await self._seed()
        async with self.sessions() as db:
            await game_service.set_game_mode(
                db,
                conversation_id,
                await self._user(db, owner_id),
                GameModeUpdate(enabled=True, creation_oid=CREATION_OID),
            )
            conversation = await game_service.resolve_game_conversation(db, conversation_id, CREATION_OID)
            session = await game_service.build_game_session(db, conversation, None)

            self.assertFalse(session.can_send)
            self.assertIsNone(session.viewer)
            self.assertEqual(len(session.messages), 1)

    async def test_logged_in_outsider_can_send_and_will_auto_join(self) -> None:
        owner_id, _, conversation_id = await self._seed()
        async with self.sessions() as db:
            await game_service.set_game_mode(
                db,
                conversation_id,
                await self._user(db, owner_id),
                GameModeUpdate(enabled=True, creation_oid=CREATION_OID),
            )
            conversation = await game_service.resolve_game_conversation(db, conversation_id, CREATION_OID)
            outsider = await self._user(db, self.outsider_id)
            session = await game_service.build_game_session(db, conversation, outsider)

            self.assertTrue(session.can_send)
            # 还不是成员，发第一条消息时会自动入群
            self.assertTrue(session.will_auto_join)

    async def test_disable_clears_binding(self) -> None:
        owner_id, _, conversation_id = await self._seed()
        async with self.sessions() as db:
            owner = await self._user(db, owner_id)
            await game_service.set_game_mode(db, conversation_id, owner, GameModeUpdate(enabled=True, creation_oid=CREATION_OID))
            result = await game_service.set_game_mode(db, conversation_id, owner, GameModeUpdate(enabled=False))
            self.assertFalse(result.enabled)
            self.assertIsNone(result.creation_oid)

            with self.assertRaises(HTTPException):
                await game_service.resolve_game_conversation(db, conversation_id, CREATION_OID)


if __name__ == "__main__":
    unittest.main()
