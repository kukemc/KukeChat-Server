from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.models  # noqa: F401
from app.core.database import Base
from app.models.conversation import Conversation, ConversationMember
from app.models.enums import ConversationType, MemberRole, ModerationAction, ModerationSource, PlatformRole, PostVisibility, ReportStatus, ReportTargetType, UserStatus
from app.models.message import Message
from app.models.penalty import UserViolation
from app.models.post import Post
from app.models.report import ContentModerationRecord
from app.models.user import User
from app.schemas.report import ReportCreate, ReportReview
from app.services import messages as message_service
from app.services import moderation as moderation_service
from app.services import notifications as notification_service
from app.services import penalties as penalty_service
from app.services import reports as report_service


class PenaltyIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with self.sessions() as db:
            reporter = User(username="reporter", hashed_password="x")
            author = User(username="author", hashed_password="x")
            admin = User(username="admin", hashed_password="x", platform_role=PlatformRole.ADMIN)
            db.add_all([reporter, author, admin])
            await db.flush()
            conversation = Conversation(type=ConversationType.GROUP, title="群")
            db.add(conversation)
            await db.flush()
            db.add_all(
                [
                    ConversationMember(conversation_id=conversation.id, user_id=reporter.id, role=MemberRole.OWNER),
                    ConversationMember(conversation_id=conversation.id, user_id=author.id, role=MemberRole.MEMBER),
                    Post(author_id=author.id, content="被举报的动态", visibility=PostVisibility.PUBLIC, moderation_status="approved"),
                    Message(conversation_id=conversation.id, sender_id=author.id, content="违规消息"),
                ]
            )
            await db.commit()
            self.reporter_id, self.author_id, self.admin_id = reporter.id, author.id, admin.id
            self.conversation_id = conversation.id

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def _violations(self, db) -> list[UserViolation]:
        return list((await db.execute(select(UserViolation).order_by(UserViolation.id))).scalars().all())

    async def test_report_review_with_category_goes_through_the_ladder(self) -> None:
        async with self.sessions() as db:
            report = await report_service.create_report(
                db, self.reporter_id, ReportCreate(target_type=ReportTargetType.POST, target_id=1, reason="辱骂骚扰")
            )
            reviewed = await report_service.review_report(
                db,
                report.id,
                self.admin_id,
                ReportReview(status=ReportStatus.REVIEWED, delete_content=True, violation_category="harassment"),
            )
            self.assertEqual(reviewed.action, "delete_post+penalty_warning")
            rows = await self._violations(db)
            self.assertEqual(len(rows), 1)
            self.assertEqual((rows[0].source, rows[0].level, rows[0].points), ("report", 2, 3))
            self.assertEqual((rows[0].target_type, rows[0].target_id, rows[0].report_id), ("post", 1, report.id))
            self.assertEqual(rows[0].summary, "被举报的动态")
            self.assertEqual((await db.get(User, self.author_id)).status, UserStatus.ACTIVE)

    async def test_report_review_explicit_ban_is_recorded_in_the_ledger(self) -> None:
        async with self.sessions() as db:
            report = await report_service.create_report(
                db, self.reporter_id, ReportCreate(target_type=ReportTargetType.POST, target_id=1, reason="违法违规")
            )
            reviewed = await report_service.review_report(
                db,
                report.id,
                self.admin_id,
                ReportReview(status=ReportStatus.REVIEWED, disable_user=True, ban_duration_hours=48, review_note="发布违规内容"),
            )
            self.assertEqual(reviewed.action, "disable_user")
            author = await db.get(User, self.author_id)
            self.assertEqual(author.status, UserStatus.DISABLED)
            remaining = penalty_service.aware(author.banned_until) - datetime.now(UTC)
            self.assertTrue(timedelta(hours=47) < remaining <= timedelta(hours=48))
            rows = await self._violations(db)
            self.assertEqual((rows[0].category, rows[0].penalty_kind, rows[0].source), ("illegal", "ban", "report"))

    async def test_manual_ban_and_unban_from_user_page(self) -> None:
        async with self.sessions() as db:
            user = await report_service.update_user_status(db, self.author_id, "disabled", self.admin_id, "恶意刷屏", None, None)
            self.assertEqual(user.status, UserStatus.DISABLED)
            self.assertIsNone(user.banned_until)
            self.assertEqual(user.ban_reason, "恶意刷屏")
            rows = await self._violations(db)
            self.assertEqual((rows[0].source, rows[0].points, rows[0].penalty_permanent), ("admin", 0, True))
            user = await report_service.update_user_status(db, self.author_id, "active", self.admin_id)
            self.assertEqual(user.status, UserStatus.ACTIVE)
            self.assertIsNone(user.banned_at)
            await db.refresh(rows[0])
            self.assertFalse(rows[0].penalty_permanent)

    async def test_auto_moderation_recalls_and_records_violation(self) -> None:
        async with self.sessions() as db:
            message = (await db.execute(select(Message))).scalar_one()
            record = ContentModerationRecord(
                source=ModerationSource.MESSAGE_AUTO,
                message_id=message.id,
                conversation_id=message.conversation_id,
                user_id=message.sender_id,
                content_type="text",
                content_text="加我领红包返利",
                decision="block",
                severity=2,
                categories='["fraud","illegal"]',
                raw_response='{"model":"rules-first"}',
            )
            db.add(record)
            action = await moderation_service._apply_moderation_action(db, message, record)
            await db.commit()
            penalty_service.discard_notifications(db)
            # 规则命中的诈骗不再直接封 7 天：L3 记 6 分，首次是禁言 1 天
            self.assertEqual(action, ModerationAction.MUTED)
            self.assertIsNotNone(message.recalled_at)
            rows = await self._violations(db)
            self.assertEqual((rows[0].category, rows[0].level, rows[0].source), ("fraud", 3, "auto_rule"))
            self.assertEqual((rows[0].target_type, rows[0].target_id, rows[0].moderation_record_id), ("message", message.id, record.id))
            author = await db.get(User, self.author_id)
            self.assertEqual(author.status, UserStatus.ACTIVE)

            with self.assertRaises(HTTPException) as caught:
                await message_service.send_message(db, self.conversation_id, author, "还想继续发")
            self.assertEqual(caught.exception.status_code, 403)
            self.assertEqual(caught.exception.detail["code"], "account_muted")

            notifications = await notification_service.list_unified_notifications(db, self.author_id)
            penalties = [item for item in notifications.items if item.type == "moderation_penalty"]
            self.assertEqual(len(penalties), 1)
            self.assertIn("诈骗", penalties[0].title)
            self.assertIn("禁言 1 天", penalties[0].body)

    async def test_uncertain_verdict_neither_recalls_nor_punishes(self) -> None:
        async with self.sessions() as db:
            message = (await db.execute(select(Message))).scalar_one()
            record = ContentModerationRecord(
                source=ModerationSource.MESSAGE_AUTO,
                message_id=message.id,
                user_id=message.sender_id,
                content_type="text",
                decision="review",
                severity=1,
                categories='["fraud"]',
            )
            db.add(record)
            action = await moderation_service._apply_moderation_action(db, message, record)
            await db.commit()
            self.assertEqual(action, ModerationAction.NONE)
            self.assertIsNone(message.recalled_at)
            self.assertEqual(await self._violations(db), [])


if __name__ == "__main__":
    unittest.main()
