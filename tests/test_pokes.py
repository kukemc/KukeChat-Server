from __future__ import annotations

import json
import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.models  # noqa: F401
from app.core.database import Base
from app.models.conversation import Conversation, ConversationMember
from app.models.enums import ConversationType, MemberRole, MessageType, ReportTargetType
from app.models.user import User
from app.schemas.report import ReportCreate
from app.schemas.user import UserPublic, UserUpdate
from app.services import messages as message_service
from app.services import moderation as moderation_service
from app.services import pokes as poke_service
from app.services import reports as report_service
from app.services import users as user_service


class PokeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with self.sessions() as db:
            users = [User(username=name, hashed_password="x") for name in ("owner", "alice", "bob", "carol", "outsider")]
            db.add_all(users)
            await db.flush()
            self.ids = {user.username: user.id for user in users}
            group = Conversation(type=ConversationType.GROUP, title="群")
            direct = Conversation(type=ConversationType.DIRECT)
            db.add_all([group, direct])
            await db.flush()
            self.group_id, self.direct_id = group.id, direct.id
            db.add(ConversationMember(conversation_id=group.id, user_id=self.ids["owner"], role=MemberRole.OWNER))
            for name in ("alice", "bob", "carol"):
                db.add(ConversationMember(conversation_id=group.id, user_id=self.ids[name], role=MemberRole.MEMBER))
            for name in ("alice", "bob"):
                db.add(ConversationMember(conversation_id=direct.id, user_id=self.ids[name], role=MemberRole.MEMBER))
            await db.commit()

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def _user(self, db, name: str) -> User:
        return await db.get(User, self.ids[name])

    async def _poke(self, db, actor: str, target: str, action: str | None = None, conversation_id: int | None = None):
        return await poke_service.create_poke(
            db, conversation_id or self.group_id, await self._user(db, actor), self.ids[target], action
        )

    async def test_poke_is_a_system_message_built_from_the_targets_settings(self) -> None:
        async with self.sessions() as db:
            bob = await self._user(db, "bob")
            self.assertEqual((bob.poke_action, bob.poke_suffix), ("戳", ""))
            message, poke = await self._poke(db, "alice", "bob")
            self.assertEqual(message.type, MessageType.SYSTEM)
            self.assertEqual(message.content, "alice 戳了戳 bob")
            self.assertEqual(message.sender_id, self.ids["alice"])

            await user_service.update_profile(db, bob, UserUpdate(poke_action="拍", poke_suffix="  的\n小脑袋  "))
            self.assertEqual((bob.poke_action, bob.poke_suffix), ("拍", "的 小脑袋"))
            public = UserPublic.model_validate(bob)
            self.assertEqual((public.poke_action, public.poke_suffix), ("拍", "的 小脑袋"))

            # 私聊也能戳；动作和后缀跟着被戳的人走
            message, poke = await self._poke(db, "alice", "bob", conversation_id=self.direct_id)
            self.assertEqual(message.content, "alice 拍了拍 bob 的 小脑袋")
            self.assertEqual(
                json.loads(message.metadata_json),
                {
                    "system_event": "conversation.member.poked",
                    "actor_id": self.ids["alice"],
                    "user_id": self.ids["bob"],
                    "action": "拍",
                    "suffix": "的 小脑袋",
                    "actor_name": "alice",
                    "user_name": "bob",
                },
            )
            self.assertEqual(poke["action"], "拍")
            read = await message_service.build_message_read(db, message, self.ids["bob"])
            self.assertEqual(read.metadata["system_event"], "conversation.member.poked")

            # 发起人临时换动作，后缀仍然是对方的；名字优先用群昵称
            alice_member = await poke_service.require_membership(db, self.group_id, self.ids["alice"])
            alice_member.nickname = "阿夸"
            await db.commit()
            with patch.object(poke_service, "PAIR_COOLDOWN_SECONDS", 0):
                message, poke = await self._poke(db, "alice", "bob", action="摸", conversation_id=self.group_id)
            self.assertEqual(message.content, "阿夸 摸了摸 bob 的 小脑袋")

            # null / 空串恢复默认
            await user_service.update_profile(db, bob, UserUpdate(poke_action=None, poke_suffix=""))
            self.assertEqual((bob.poke_action, bob.poke_suffix), ("戳", ""))

    async def test_validation_and_membership(self) -> None:
        async with self.sessions() as db:
            bob = await self._user(db, "bob")
            with self.assertRaises(HTTPException) as caught:
                await user_service.update_profile(db, bob, UserUpdate(poke_action="操"))
            self.assertEqual((caught.exception.status_code, caught.exception.detail["code"]), (422, "invalid_poke_action"))
            with self.assertRaises(HTTPException) as caught:
                await user_service.update_profile(db, bob, UserUpdate(poke_suffix="字" * 17))
            self.assertEqual(caught.exception.detail["code"], "invalid_poke_suffix")
            await user_service.update_profile(db, bob, UserUpdate(poke_suffix="字" * 16))

            with patch.object(poke_service.settings, "content_moderation_enabled", True), patch.object(
                moderation_service, "_call_text_moderation_for_user", new=AsyncMock(return_value={"decision": "block"})
            ) as moderation:
                with self.assertRaises(HTTPException) as caught:
                    await user_service.update_profile(db, bob, UserUpdate(poke_suffix="不行的话"))
                self.assertEqual(caught.exception.detail["code"], "poke_suffix_rejected")
                # 没改动就不再送审
                moderation.reset_mock()
                await user_service.update_profile(db, bob, UserUpdate(poke_suffix="字" * 16))
                moderation.assert_not_awaited()
            await db.refresh(bob)
            self.assertEqual(bob.poke_suffix, "字" * 16)

            with self.assertRaises(HTTPException) as caught:
                await self._poke(db, "alice", "bob", action="操")
            self.assertEqual(caught.exception.detail["code"], "invalid_poke_action")
            with self.assertRaises(HTTPException) as caught:
                await self._poke(db, "alice", "alice")
            self.assertEqual(caught.exception.status_code, 400)
            with self.assertRaises(HTTPException) as caught:
                await self._poke(db, "outsider", "bob")
            self.assertEqual(caught.exception.status_code, 404)
            with self.assertRaises(HTTPException) as caught:
                await self._poke(db, "alice", "outsider")
            self.assertEqual(caught.exception.status_code, 404)

    async def test_rate_limits(self) -> None:
        async with self.sessions() as db:
            await self._poke(db, "alice", "bob")
            with self.assertRaises(HTTPException) as caught:
                await self._poke(db, "alice", "bob")
            self.assertEqual((caught.exception.status_code, caught.exception.detail["code"]), (429, "poke_rate_limited"))
            # 换个人不受「同一对」冷却影响，反过来戳也不受影响
            await self._poke(db, "alice", "carol")
            await self._poke(db, "bob", "alice")

            with patch.object(poke_service, "PAIR_COOLDOWN_SECONDS", 0):
                for _ in range(poke_service.ACTOR_LIMIT_PER_MINUTE - 2):
                    await self._poke(db, "alice", "owner")
                with self.assertRaises(HTTPException) as caught:
                    await self._poke(db, "alice", "owner")
                self.assertEqual(caught.exception.status_code, 429)
                # 别的会话单独计数
                await self._poke(db, "alice", "bob", conversation_id=self.direct_id)

                with patch.object(poke_service, "CONVERSATION_LIMIT_PER_MINUTE", 8):
                    await self._poke(db, "carol", "owner")
                    with self.assertRaises(HTTPException) as caught:
                        await self._poke(db, "owner", "carol")
                    self.assertEqual(caught.exception.status_code, 429)

    async def test_muted_accounts_and_muted_groups_cannot_poke(self) -> None:
        async with self.sessions() as db:
            alice = await self._user(db, "alice")
            alice.muted_until = datetime.now(UTC) + timedelta(hours=1)
            await db.commit()
            with self.assertRaises(HTTPException) as caught:
                await self._poke(db, "alice", "bob")
            self.assertEqual((caught.exception.status_code, caught.exception.detail["code"]), (403, "account_muted"))

            group = await db.get(Conversation, self.group_id)
            group.all_muted = True
            await db.commit()
            with self.assertRaises(HTTPException) as caught:
                await self._poke(db, "bob", "carol")
            self.assertEqual((caught.exception.status_code, caught.exception.detail["code"]), (403, "poke_muted"))
            await self._poke(db, "owner", "carol")

    async def test_poke_message_cannot_be_reported_as_a_message(self) -> None:
        async with self.sessions() as db:
            message, _ = await self._poke(db, "alice", "bob")
            with self.assertRaises(HTTPException) as caught:
                await report_service.create_report(
                    db,
                    self.ids["bob"],
                    ReportCreate(target_type=ReportTargetType.MESSAGE, target_id=message.id, reason="辱骂骚扰"),
                )
            self.assertEqual((caught.exception.status_code, caught.exception.detail["code"]), (422, "not_reportable"))
            # 也不能在举报发起人时拿它当证据：后缀是被戳的人自己写的
            with self.assertRaises(HTTPException) as caught:
                await report_service.create_report(
                    db,
                    self.ids["bob"],
                    ReportCreate(
                        target_type=ReportTargetType.USER,
                        target_id=self.ids["alice"],
                        reason="辱骂骚扰",
                        conversation_id=self.group_id,
                        evidence_message_ids=[message.id],
                    ),
                )
            self.assertEqual((caught.exception.status_code, caught.exception.detail["code"]), (422, "not_reportable"))


if __name__ == "__main__":
    unittest.main()
