from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.models  # noqa: F401
from app.core.database import Base
from app.core.security import hash_password, verify_password
from app.models.conversation import Conversation, ConversationMember
from app.models.enums import ConversationType, MemberRole, PasswordResetStatus, PlatformRole, PostVisibility, ReportStatus, ReportTargetType, UserStatus
from app.models.post import Post
from app.models.report import AdminAuditLog, PasswordResetRequest
from app.models.user import AuthSession, User
from app.schemas.report import AdminPasswordResetReview, AdminUserPasswordReset, ReportCreate, ReportReview
from app.services import reports as report_service


class ReportFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def _seed_post(self) -> tuple[int, int, int]:
        async with self.sessions() as db:
            reporter = User(username="reporter", hashed_password="x", nickname="举报人")
            author = User(username="author", hashed_password="x", nickname="作者")
            admin = User(username="admin", hashed_password="x", nickname="管理员", platform_role=PlatformRole.ADMIN)
            db.add_all([reporter, author, admin])
            await db.flush()
            post = Post(
                author_id=author.id,
                content="举报时保存的动态正文",
                visibility=PostVisibility.PUBLIC,
                image_urls_json='["/uploads/posts/evidence.png"]',
                moderation_status="approved",
            )
            db.add(post)
            await db.commit()
            return reporter.id, author.id, admin.id

    async def test_post_report_preserves_snapshot_and_rejects_duplicate(self) -> None:
        reporter_id, author_id, _ = await self._seed_post()
        async with self.sessions() as db:
            post = await db.get(Post, 1)
            report = await report_service.create_report(
                db,
                reporter_id,
                ReportCreate(target_type=ReportTargetType.POST, target_id=post.id, reason="辱骂骚扰"),
            )
            read = await report_service.build_report_read(db, report, reporter_id, include_snapshot=True)
            snapshot = read.target.extra["snapshot"]
            self.assertEqual(read.reported_user_id, author_id)
            self.assertEqual(snapshot["post"]["content"], "举报时保存的动态正文")
            self.assertEqual(snapshot["post"]["image_urls"], ["/uploads/posts/evidence.png"])
            with self.assertRaisesRegex(Exception, "pending report already exists"):
                await report_service.create_report(
                    db,
                    reporter_id,
                    ReportCreate(target_type=ReportTargetType.POST, target_id=post.id, reason="其他"),
                )

    async def test_review_can_delete_post_and_disable_author_together(self) -> None:
        reporter_id, author_id, admin_id = await self._seed_post()
        async with self.sessions() as db:
            author_session = AuthSession(
                user_id=author_id,
                session_hash="author-session",
                expires_at=datetime.now(UTC) + timedelta(days=1),
            )
            db.add(author_session)
            await db.commit()
            report = await report_service.create_report(
                db,
                reporter_id,
                ReportCreate(target_type=ReportTargetType.POST, target_id=1, reason="违法违规"),
            )
            reviewed = await report_service.review_report(
                db,
                report.id,
                admin_id,
                ReportReview(
                    status=ReportStatus.REVIEWED,
                    review_note="多次发布违规内容",
                    delete_content=True,
                    disable_user=True,
                    ban_duration_hours=72,
                ),
            )
            post = await db.get(Post, 1)
            author = await db.get(User, author_id)
            self.assertIsNotNone(post.deleted_at)
            self.assertEqual(author.status, UserStatus.DISABLED)
            self.assertIsNotNone(author.banned_until)
            self.assertEqual(author.ban_reason, "多次发布违规内容")
            self.assertEqual(reviewed.action, "delete_post+disable_user")
            await db.refresh(author_session)
            self.assertIsNotNone(author_session.revoked_at)

    async def test_admin_cannot_be_partially_deleted_before_ban_validation(self) -> None:
        reporter_id, _, admin_id = await self._seed_post()
        async with self.sessions() as db:
            post = await db.get(Post, 1)
            post.author_id = admin_id
            await db.commit()
            report = await report_service.create_report(
                db,
                reporter_id,
                ReportCreate(target_type=ReportTargetType.POST, target_id=post.id, reason="其他"),
            )
            with self.assertRaisesRegex(Exception, "Cannot disable an admin"):
                await report_service.review_report(
                    db,
                    report.id,
                    admin_id + 100,
                    ReportReview(status=ReportStatus.REVIEWED, delete_content=True, disable_user=True),
                )
            refreshed = await db.get(Post, post.id)
            self.assertIsNone(refreshed.deleted_at)

    async def test_post_snapshot_keeps_deleted_image_referenced(self) -> None:
        reporter_id, _, admin_id = await self._seed_post()
        async with self.sessions() as db:
            report = await report_service.create_report(
                db,
                reporter_id,
                ReportCreate(target_type=ReportTargetType.POST, target_id=1, reason="违法违规"),
            )
            await report_service.review_report(
                db,
                report.id,
                admin_id,
                ReportReview(status=ReportStatus.REVIEWED, delete_content=True),
            )
            self.assertTrue(await report_service.post_service._upload_path_still_referenced(db, "posts/evidence.png"))

    async def test_manual_password_reset_revokes_sessions_and_audits_reason(self) -> None:
        async with self.sessions() as db:
            admin = User(username="reset-admin", hashed_password=hash_password("old-admin"), platform_role=PlatformRole.ADMIN)
            user = User(username="reset-target", hashed_password=hash_password("old-password"))
            db.add_all([admin, user])
            await db.flush()
            session = AuthSession(user_id=user.id, session_hash="reset-session", expires_at=datetime.now(UTC) + timedelta(days=1))
            db.add(session)
            await db.commit()

            await report_service.reset_user_password(
                db,
                user.id,
                admin.id,
                AdminUserPasswordReset(new_password="new-password", reason="  已完成身份核验  "),
            )

            await db.refresh(user)
            await db.refresh(session)
            self.assertTrue(verify_password("new-password", user.hashed_password))
            self.assertIsNotNone(session.revoked_at)
            audit = (await db.execute(select(AdminAuditLog).where(AdminAuditLog.action == "user.password.reset"))).scalar_one()
            self.assertIn("已完成身份核验", audit.details)
            self.assertNotIn("new-password", audit.details)

    async def test_approved_password_request_revokes_existing_sessions(self) -> None:
        async with self.sessions() as db:
            admin = User(username="request-admin", hashed_password="x", platform_role=PlatformRole.ADMIN)
            user = User(username="request-target", hashed_password=hash_password("old-password"))
            db.add_all([admin, user])
            await db.flush()
            request = PasswordResetRequest(
                username=user.username,
                account_user_id=user.id,
                ccw_profile_url="https://www.ccw.site/student/example",
                reason="无法登录",
                requested_password="requested-password",
                request_ip="127.0.0.2",
                status=PasswordResetStatus.PENDING,
            )
            session = AuthSession(user_id=user.id, session_hash="request-session", expires_at=datetime.now(UTC) + timedelta(days=1))
            db.add_all([request, session])
            await db.commit()

            await report_service.review_password_reset_request(
                db,
                request.id,
                admin.id,
                AdminPasswordResetReview(status=PasswordResetStatus.APPROVED, new_password="requested-password"),
            )

            await db.refresh(user)
            await db.refresh(session)
            self.assertTrue(verify_password("requested-password", user.hashed_password))
            self.assertIsNotNone(session.revoked_at)

    async def test_admin_conversation_summary_does_not_embed_full_member_list(self) -> None:
        async with self.sessions() as db:
            users = [User(username=f"member-{index}", hashed_password="x") for index in range(75)]
            db.add_all(users)
            await db.flush()
            conversation = Conversation(type=ConversationType.GROUP, title="大群")
            db.add(conversation)
            await db.flush()
            db.add_all([
                ConversationMember(
                    conversation_id=conversation.id,
                    user_id=user.id,
                    role=MemberRole.OWNER if index == 0 else MemberRole.MEMBER,
                )
                for index, user in enumerate(users)
            ])
            await db.commit()

            summary = await report_service.get_conversation_read(db, conversation.id)
            first_page, total = await report_service.list_admin_conversation_members(
                db, conversation.id, limit=30, offset=0
            )

            self.assertIsNotNone(summary)
            self.assertEqual(summary.member_count, 75)
            self.assertEqual(summary.participants, [])
            self.assertEqual(total, 75)
            self.assertEqual(len(first_page), 30)
            self.assertEqual(first_page[0].id, users[0].id)


if __name__ == "__main__":
    unittest.main()
