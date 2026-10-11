from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.models  # noqa: F401
from app.core.database import Base
from app.models.conversation import Conversation, ConversationMember
from app.models.enums import ConversationType, MemberRole, MessageType
from app.models.message import Message
from app.models.poll import GroupEmoji, Poll
from app.models.user import User
from app.schemas.message import MessageCreate
from app.schemas.poll import PollCreate
from app.schemas.user import UserUpdate
from app.services import group_emojis as emoji_service
from app.services import messages as message_service
from app.services import moderation as moderation_service
from app.services import polls as poll_service
from app.services import typing as typing_service
from app.services import users as user_service
from app.services.storage import StoredObject


class ChatExtrasBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with self.sessions() as db:
            users = [User(username=name, hashed_password="x") for name in ("owner", "alice", "bob", "outsider")]
            db.add_all(users)
            await db.flush()
            self.ids = {user.username: user.id for user in users}
            group = Conversation(type=ConversationType.GROUP, title="群")
            direct = Conversation(type=ConversationType.DIRECT)
            db.add_all([group, direct])
            await db.flush()
            self.group_id, self.direct_id = group.id, direct.id
            db.add(ConversationMember(conversation_id=group.id, user_id=self.ids["owner"], role=MemberRole.OWNER))
            for name in ("alice", "bob"):
                db.add(ConversationMember(conversation_id=group.id, user_id=self.ids[name], role=MemberRole.MEMBER))
                db.add(ConversationMember(conversation_id=direct.id, user_id=self.ids[name], role=MemberRole.MEMBER))
            await db.commit()

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def _user(self, db, name: str) -> User:
        return await db.get(User, self.ids[name])


class PollTests(ChatExtrasBase):
    async def _create(self, db, creator="alice", conversation_id=None, **overrides) -> tuple[Message, Poll]:
        payload = {"title": "周末做什么", "options": ["写游戏", " 画画 ", "睡觉"], **overrides}
        message = await poll_service.create_poll(db, conversation_id or self.group_id, await self._user(db, creator), PollCreate(**payload))
        poll = (await db.execute(select(Poll).where(Poll.message_id == message.id))).scalar_one()
        return message, poll

    async def test_poll_is_a_message_with_definition_and_live_state(self) -> None:
        async with self.sessions() as db:
            message, poll = await self._create(db)
            self.assertEqual(message.type, MessageType.POLL)
            self.assertEqual(message.content, "[投票] 周末做什么\n1. 写游戏\n2. 画画\n3. 睡觉")
            new_read = message_service.build_new_message_read(message, self.ids["alice"])
            self.assertEqual(new_read.metadata["poll"]["id"], poll.id)
            self.assertEqual([o["votes"] for o in new_read.metadata["poll"]["options"]], [0, 0, 0])
            self.assertFalse(new_read.metadata["poll"]["closed"])

            await poll_service.cast_vote(db, self.group_id, poll.id, await self._user(db, "bob"), [2])
            await poll_service.cast_vote(db, self.group_id, poll.id, await self._user(db, "owner"), [2])
            state = await poll_service.cast_vote(db, self.group_id, poll.id, await self._user(db, "alice"), [1])
            self.assertEqual([(o["id"], o["votes"]) for o in state["options"]], [(1, 1), (2, 2), (3, 0)])
            self.assertEqual((state["total_voters"], state["my_option_ids"]), (3, [1]))

            # 改票：提交的是完整集合，旧票被替换
            state = await poll_service.cast_vote(db, self.group_id, poll.id, await self._user(db, "alice"), [3])
            self.assertEqual([o["votes"] for o in state["options"]], [0, 2, 1])
            # 撤票
            state = await poll_service.cast_vote(db, self.group_id, poll.id, await self._user(db, "alice"), [])
            self.assertEqual((state["total_voters"], state["my_option_ids"]), (2, []))

            reads = await message_service.build_message_reads(db, [message], self.ids["bob"])
            listed = reads[0].metadata["poll"]
            self.assertEqual([(o["text"], o["votes"]) for o in listed["options"]], [("写游戏", 0), ("画画", 2), ("睡觉", 0)])
            self.assertEqual((listed["total_voters"], listed["my_option_ids"], listed["closed"]), (2, [2], False))
            self.assertNotIn("my_option_ids", poll_service.public_state(state))

    async def test_single_choice_multi_choice_and_bad_options(self) -> None:
        async with self.sessions() as db:
            _, single = await self._create(db)
            bob = await self._user(db, "bob")
            with self.assertRaises(HTTPException) as caught:
                await poll_service.cast_vote(db, self.group_id, single.id, bob, [1, 2])
            self.assertEqual(caught.exception.detail["code"], "poll_single_choice")
            with self.assertRaises(HTTPException) as caught:
                await poll_service.cast_vote(db, self.group_id, single.id, bob, [9])
            self.assertEqual(caught.exception.detail["code"], "invalid_poll_option")
            with self.assertRaises(HTTPException) as caught:
                await poll_service.cast_vote(db, self.group_id, single.id, await self._user(db, "outsider"), [1])
            self.assertEqual(caught.exception.status_code, 404)

            _, multi = await self._create(db, multiple=True)
            state = await poll_service.cast_vote(db, self.group_id, multi.id, bob, [1, 3, 3])
            self.assertEqual((state["my_option_ids"], state["total_voters"]), ([1, 3], 1))

    async def test_closing_permissions_deadline_and_recall(self) -> None:
        async with self.sessions() as db:
            message, poll = await self._create(db)
            with self.assertRaises(HTTPException) as caught:
                await poll_service.close_poll(db, self.group_id, poll.id, await self._user(db, "bob"))
            self.assertEqual(caught.exception.detail["code"], "poll_forbidden")
            # 群主不是发起人，但可以结束
            state = await poll_service.close_poll(db, self.group_id, poll.id, await self._user(db, "owner"))
            self.assertTrue(state["closed"])
            with self.assertRaises(HTTPException) as caught:
                await poll_service.cast_vote(db, self.group_id, poll.id, await self._user(db, "bob"), [1])
            self.assertEqual(caught.exception.detail["code"], "poll_closed")

            _, timed = await self._create(db, closes_at=datetime.now(UTC) + timedelta(hours=1))
            timed.closes_at = datetime.now(UTC) - timedelta(seconds=1)
            await db.commit()
            self.assertTrue((await poll_service.get_state(db, self.group_id, timed.id, await self._user(db, "bob")))["closed"])

            recalled_message, recalled = await self._create(db)
            recalled_message.recalled_at = datetime.now(UTC)
            await db.commit()
            with self.assertRaises(HTTPException) as caught:
                await poll_service.cast_vote(db, self.group_id, recalled.id, await self._user(db, "bob"), [1])
            self.assertEqual(caught.exception.detail["code"], "poll_void")

    async def test_voter_list_respects_anonymity_and_works_in_direct_chats(self) -> None:
        async with self.sessions() as db:
            _, public = await self._create(db, conversation_id=self.direct_id)
            await poll_service.cast_vote(db, self.direct_id, public.id, await self._user(db, "bob"), [1])
            voters, total = await poll_service.list_voters(db, self.direct_id, public.id, 1, await self._user(db, "alice"), 50, 0)
            self.assertEqual(([user.username for user in voters], total), (["bob"], 1))
            # 私聊里没有管理员：只有发起人能结束
            with self.assertRaises(HTTPException):
                await poll_service.close_poll(db, self.direct_id, public.id, await self._user(db, "bob"))

            _, secret = await self._create(db, anonymous=True)
            await poll_service.cast_vote(db, self.group_id, secret.id, await self._user(db, "bob"), [1])
            with self.assertRaises(HTTPException) as caught:
                await poll_service.list_voters(db, self.group_id, secret.id, 1, await self._user(db, "owner"), 50, 0)
            self.assertEqual(caught.exception.detail["code"], "poll_anonymous")

    async def test_validation_mute_and_entry_points(self) -> None:
        async with self.sessions() as db:
            alice = await self._user(db, "alice")
            for bad in (
                {"title": " "},
                {"options": ["只有一个"]},
                {"options": ["重复", "重复"]},
                {"options": ["a" * 41, "b"]},
                {"closes_at": datetime.now(UTC) - timedelta(minutes=1)},
            ):
                with self.assertRaises(HTTPException) as caught:
                    await self._create(db, **bad)
                self.assertEqual(caught.exception.detail["code"], "invalid_poll")
            # 不能绕过投票接口直接发 poll 类型的消息
            with self.assertRaises(ValueError):
                MessageCreate(type=MessageType.POLL, content="x")
            with self.assertRaises(HTTPException):
                await message_service.send_message(db, self.group_id, alice, "x", MessageType.POLL, {"poll": {}})

            _, poll = await self._create(db, creator="bob")
            alice.muted_until = datetime.now(UTC) + timedelta(hours=1)
            await db.commit()
            with self.assertRaises(HTTPException) as caught:
                await self._create(db)
            self.assertEqual(caught.exception.detail["code"], "account_muted")
            # 被禁言的人不能发起投票，但可以参与
            state = await poll_service.cast_vote(db, self.group_id, poll.id, alice, [1])
            self.assertEqual(state["my_option_ids"], [1])

            message = (await db.execute(select(Message).where(Message.id == poll.message_id))).scalar_one()
            text = moderation_service._text_to_moderate(message, moderation_service._json_object(message.metadata_json))
            self.assertEqual(text, "周末做什么\n写游戏\n画画\n睡觉")


class TypingTests(ChatExtrasBase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        typing_service._last_sent.clear()
        typing_service._recipients.clear()

    async def _typing(self, user: str, conversation_id: int, online: set[int]) -> AsyncMock:
        from app.websocket.manager import manager

        send = AsyncMock()
        with patch.object(typing_service, "AsyncSessionLocal", self.sessions), patch.object(
            manager, "online_user_ids_among", AsyncMock(side_effect=lambda ids: online & set(ids))
        ), patch.object(manager, "send_to_users", send):
            await typing_service.handle_typing(self.ids[user], conversation_id)
        return send

    async def test_forwards_to_other_online_members_with_rate_limit(self) -> None:
        online = set(self.ids.values())
        send = await self._typing("alice", self.group_id, online)
        recipients, event, data = send.call_args.args
        self.assertEqual(set(recipients), {self.ids["owner"], self.ids["bob"]})
        self.assertEqual(event, "conversation.typing")
        self.assertEqual(data, {"conversation_id": self.group_id, "user_id": self.ids["alice"], "name": "alice"})
        # 两秒内的重复上报被丢弃
        self.assertFalse((await self._typing("alice", self.group_id, online)).called)
        # 换一个会话不受影响
        self.assertTrue((await self._typing("alice", self.direct_id, online)).called)

    async def test_respects_membership_privacy_and_ignore_setting(self) -> None:
        online = set(self.ids.values())
        self.assertFalse((await self._typing("outsider", self.group_id, online)).called)
        async with self.sessions() as db:
            bob = await self._user(db, "bob")
            await user_service.update_profile(db, bob, UserUpdate(share_typing=False))
            self.assertFalse(bob.share_typing)
            member = (
                await db.execute(select(ConversationMember).where(ConversationMember.conversation_id == self.group_id, ConversationMember.user_id == self.ids["owner"]))
            ).scalar_one()
            member.message_setting = "ignore"
            await db.commit()
        self.assertFalse((await self._typing("bob", self.group_id, online)).called)
        send = await self._typing("alice", self.group_id, online)
        self.assertEqual(set(send.call_args.args[0]), {self.ids["bob"]})

    async def test_large_groups_do_not_broadcast_typing(self) -> None:
        with patch.object(typing_service, "MAX_GROUP_MEMBERS", 2):
            self.assertFalse((await self._typing("alice", self.group_id, set(self.ids.values()))).called)


class GroupEmojiTests(ChatExtrasBase):
    def _storage(self, size: int = 1000):
        return (
            patch.object(emoji_service.sticker_service, "_resolve_source", lambda source: (f"stickers/{abs(hash(source))}.jpg", "image/jpeg")),
            patch.object(emoji_service, "get_object", lambda key, max_bytes=None: StoredObject(data=b"x" * size, content_type="image/jpeg")),
        )

    async def test_staff_manage_members_read(self) -> None:
        resolve, fetch = self._storage()
        with resolve, fetch:
            async with self.sessions() as db:
                owner, alice = await self._user(db, "owner"), await self._user(db, "alice")
                with self.assertRaises(HTTPException) as caught:
                    await emoji_service.create_emoji(db, self.group_id, alice, "狗头", "/uploads/messages/a.png")
                self.assertEqual(caught.exception.detail["code"], "emoji_forbidden")
                emoji, asset = await emoji_service.create_emoji(db, self.group_id, owner, " 狗 头 ", "/uploads/messages/a.png")
                self.assertEqual(emoji.name, "狗头")
                self.assertTrue(emoji_service.build_read(emoji, asset, None).url.endswith(asset.storage_path))
                with self.assertRaises(HTTPException) as caught:
                    await emoji_service.create_emoji(db, self.group_id, owner, "狗头", "/uploads/messages/b.png")
                self.assertEqual(caught.exception.detail["code"], "emoji_name_taken")
                with self.assertRaises(HTTPException) as caught:
                    await emoji_service.create_emoji(db, self.group_id, owner, "一二三四五六七八九十一二三", "/uploads/messages/b.png")
                self.assertEqual(caught.exception.detail["code"], "invalid_emoji_name")
                with self.assertRaises(HTTPException) as caught:
                    await emoji_service.create_emoji(db, self.group_id, owner, "外链", "https://example.com/a.png")
                self.assertEqual(caught.exception.detail["code"], "invalid_emoji_image")
                with self.assertRaises(HTTPException):
                    await emoji_service.create_emoji(db, self.direct_id, alice, "私聊", "/uploads/messages/a.png")

                listed = await emoji_service.list_emojis(db, self.group_id, alice.id)
                self.assertEqual([item.name for item, _ in listed], ["狗头"])
                with self.assertRaises(HTTPException):
                    await emoji_service.list_emojis(db, self.group_id, self.ids["outsider"])

                second, _ = await emoji_service.create_emoji(db, self.group_id, owner, "滑稽", "/uploads/messages/b.png")
                with self.assertRaises(HTTPException) as caught:
                    await emoji_service.rename_emoji(db, self.group_id, second.id, owner, "狗头")
                self.assertEqual(caught.exception.detail["code"], "emoji_name_taken")
                renamed, _ = await emoji_service.rename_emoji(db, self.group_id, second.id, owner, "斜眼笑")
                self.assertEqual(renamed.name, "斜眼笑")
                with self.assertRaises(HTTPException):
                    await emoji_service.delete_emoji(db, self.group_id, second.id, alice)
                await emoji_service.delete_emoji(db, self.group_id, second.id, owner)
                self.assertEqual(len(await emoji_service.list_emojis(db, self.group_id, alice.id)), 1)

    async def test_limits(self) -> None:
        resolve, _ = self._storage()
        big = patch.object(emoji_service, "get_object", lambda key, max_bytes=None: StoredObject(data=b"x" * (600 * 1024), content_type="image/gif"))
        with resolve, big:
            async with self.sessions() as db:
                owner = await self._user(db, "owner")
                with self.assertRaises(HTTPException) as caught:
                    await emoji_service.create_emoji(db, self.group_id, owner, "太大", "/uploads/messages/big.gif")
                self.assertEqual(caught.exception.detail["code"], "emoji_too_large")
        resolve, fetch = self._storage()
        with resolve, fetch, patch.object(emoji_service, "MAX_EMOJIS_PER_GROUP", 1):
            async with self.sessions() as db:
                owner = await self._user(db, "owner")
                await emoji_service.create_emoji(db, self.group_id, owner, "第一个", "/uploads/messages/a.png")
                with self.assertRaises(HTTPException) as caught:
                    await emoji_service.create_emoji(db, self.group_id, owner, "第二个", "/uploads/messages/b.png")
                self.assertEqual(caught.exception.detail["code"], "emoji_limit_reached")
                self.assertEqual(len((await db.execute(select(GroupEmoji))).scalars().all()), 1)


if __name__ == "__main__":
    unittest.main()
