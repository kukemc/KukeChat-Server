"""管理员读取私聊内容的边界测试。

后台不再能主动翻阅任意会话，只能查看「被举报的消息」及其上下文。
这些用例锁定该边界，防止将来有人重新放开。

用例直接构造 UserReport 行，字段取值与 services/reports.py::create_report
在 target_type=MESSAGE 分支写入的一致（同时设置 message_id 与 target_id），
避免把守卫的测试与举报快照的构造逻辑耦合在一起。
"""

from __future__ import annotations

import unittest

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.models  # noqa: F401
from app.core.database import Base
from app.models.conversation import Conversation, ConversationMember
from app.models.enums import ConversationType, MemberRole, ReportTargetType
from app.models.message import Message
from app.models.report import UserReport
from app.models.user import User
from app.services import reports as report_service


class AdminMessageScopeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def _seed(self) -> tuple[int, int, int, int]:
        """两人私聊 + 两条消息。返回 (举报人, 会话, 被举报消息, 无关消息)。"""
        async with self.sessions() as db:
            reporter = User(username="reporter", hashed_password="x", nickname="举报人")
            author = User(username="author", hashed_password="x", nickname="发言人")
            db.add_all([reporter, author])
            await db.flush()

            conversation = Conversation(type=ConversationType.DIRECT)
            db.add(conversation)
            await db.flush()
            db.add_all(
                [
                    ConversationMember(conversation_id=conversation.id, user_id=reporter.id, role=MemberRole.MEMBER),
                    ConversationMember(conversation_id=conversation.id, user_id=author.id, role=MemberRole.MEMBER),
                ]
            )

            reported = Message(conversation_id=conversation.id, sender_id=author.id, content="被举报的那条私聊")
            unrelated = Message(conversation_id=conversation.id, sender_id=author.id, content="没人举报过的私聊")
            db.add_all([reported, unrelated])
            await db.commit()
            return reporter.id, conversation.id, reported.id, unrelated.id

    async def _add_message_report(self, db, reporter_id: int, conversation_id: int, message_id: int) -> None:
        db.add(
            UserReport(
                reporter_id=reporter_id,
                target_type=ReportTargetType.MESSAGE,
                target_id=message_id,
                conversation_id=conversation_id,
                message_id=message_id,
                reason="辱骂骚扰",
            )
        )
        await db.commit()

    async def _add_conversation_report(self, db, reporter_id: int, conversation_id: int) -> None:
        """举报整个会话：create_report 在该分支不写 message_id。"""
        db.add(
            UserReport(
                reporter_id=reporter_id,
                target_type=ReportTargetType.CONVERSATION,
                target_id=conversation_id,
                conversation_id=conversation_id,
                reason="辱骂骚扰",
            )
        )
        await db.commit()

    async def test_unreported_message_is_not_readable(self) -> None:
        """没有举报记录的私聊消息，管理员读取应被拒绝。"""
        _, _, _, unrelated_id = await self._seed()
        async with self.sessions() as db:
            self.assertFalse(await report_service.message_is_under_report(db, unrelated_id))
            with self.assertRaises(HTTPException) as caught:
                await report_service.ensure_message_is_under_report(db, unrelated_id)
            self.assertEqual(caught.exception.status_code, 403)

    async def test_reported_message_is_readable(self) -> None:
        """被举报的消息，管理员可以读取其上下文。"""
        reporter_id, conversation_id, reported_id, _ = await self._seed()
        async with self.sessions() as db:
            await self._add_message_report(db, reporter_id, conversation_id, reported_id)
            self.assertTrue(await report_service.message_is_under_report(db, reported_id))
            await report_service.ensure_message_is_under_report(db, reported_id)

    async def test_report_does_not_unlock_sibling_messages(self) -> None:
        """举报一条消息，不应连带解锁同一会话里的其他消息。"""
        reporter_id, conversation_id, reported_id, unrelated_id = await self._seed()
        async with self.sessions() as db:
            await self._add_message_report(db, reporter_id, conversation_id, reported_id)
            self.assertTrue(await report_service.message_is_under_report(db, reported_id))
            self.assertFalse(await report_service.message_is_under_report(db, unrelated_id))

    async def test_conversation_report_does_not_unlock_messages(self) -> None:
        """举报整个会话，也不能因此逐条翻阅该会话的消息。

        会话级举报的证据在举报时已固化进 target_snapshot，管理员看快照即可，
        无需（也不应）获得对该会话消息的实时读取权。
        """
        reporter_id, conversation_id, reported_id, unrelated_id = await self._seed()
        async with self.sessions() as db:
            await self._add_conversation_report(db, reporter_id, conversation_id)
            self.assertFalse(await report_service.message_is_under_report(db, reported_id))
            self.assertFalse(await report_service.message_is_under_report(db, unrelated_id))

    async def test_browse_endpoints_are_gone(self) -> None:
        """全站 / 按用户 / 按会话的消息浏览端点必须不存在。"""
        from app.main import app

        paths = app.openapi()["paths"]
        for removed in (
            "/api/v1/admin/messages",
            "/api/v1/admin/users/{user_id}/messages",
            "/api/v1/admin/conversations/{conversation_id}/messages",
        ):
            self.assertNotIn(removed, paths, f"{removed} 不应再存在")

        # 举报驱动的上下文查看仍然保留
        self.assertIn("/api/v1/admin/messages/{message_id}/context", paths)
        self.assertIn("/api/v1/admin/conversations/{conversation_id}/messages/{message_id}/context", paths)


if __name__ == "__main__":
    unittest.main()
