from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.models  # noqa: F401
from app.core.database import Base
from app.core.dependencies import get_current_user_from_token
from app.core.security import create_access_token, decode_access_token
from app.models.enums import PlatformRole, UserStatus
from app.models.penalty import UserViolation
from app.models.user import AuthSession, User
from app.services import penalties as penalty_service
from app.services.auth import APPEAL_TOKEN_SCOPE, ensure_user_not_disabled
from app.services.penalties import Penalty, compute_penalty


def _create_tables(connection) -> None:
    # 只建引擎用到的表：全量建表在这台机器上一次要二十多秒。
    names = ("users", "auth_sessions", "user_violations", "admin_audit_logs")
    Base.metadata.create_all(connection, tables=[Base.metadata.tables[name] for name in names])


class LadderTests(unittest.TestCase):
    def test_single_violation_base_penalty(self) -> None:
        self.assertEqual(compute_penalty(1, 0, 1), Penalty("notice"))
        self.assertEqual(compute_penalty(2, 0, 3), Penalty("warning"))
        self.assertEqual(compute_penalty(3, 0, 6), Penalty("mute", 24))
        self.assertEqual(compute_penalty(4, 0, 12), Penalty("ban", 24 * 7))

    def test_ladder_only_fires_when_threshold_is_crossed(self) -> None:
        # 5 -> 6：一句脏话跨过 6 分门槛，禁言 1 天
        self.assertEqual(compute_penalty(1, 5, 6), Penalty("mute", 24))
        # 6 -> 7：没有跨过新门槛，只是提醒，不会再禁言一次
        self.assertEqual(compute_penalty(1, 6, 7), Penalty("notice"))
        self.assertEqual(compute_penalty(2, 12, 15), Penalty("warning"))

    def test_highest_crossed_tier_wins(self) -> None:
        self.assertEqual(compute_penalty(3, 7, 13), Penalty("ban", 24 * 7))
        self.assertEqual(compute_penalty(3, 12, 18), Penalty("ban", 24 * 30))
        self.assertEqual(compute_penalty(3, 18, 24), Penalty("ban", None))

    def test_machine_and_jury_cannot_exceed_seven_day_ban(self) -> None:
        for source in ("auto_rule", "auto_ai", "auto_image", "jury"):
            applied, recommended = penalty_service.cap_penalty(Penalty("ban", None), source, 3)
            self.assertEqual(applied, Penalty("ban", 24 * 7))
            self.assertEqual(recommended, Penalty("ban", None))
        applied, recommended = penalty_service.cap_penalty(Penalty("ban", None), "admin", 3)
        self.assertEqual((applied, recommended), (Penalty("ban", None), None))

    def test_red_line_content_always_goes_to_admin_review(self) -> None:
        applied, recommended = penalty_service.cap_penalty(Penalty("ban", 24 * 7), "auto_image", 4)
        self.assertEqual(applied, Penalty("ban", 24 * 7))
        self.assertEqual(recommended, Penalty("ban", None))

    def test_penalty_round_trips_through_encoding(self) -> None:
        for penalty in (Penalty("notice"), Penalty("mute", 72), Penalty("ban", 168), Penalty("ban", None)):
            self.assertEqual(Penalty.decode(penalty.encode()), penalty)


class PenaltyEngineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(_create_tables)
        async with self.sessions() as db:
            db.add_all(
                [
                    User(username="member", hashed_password="x"),
                    User(username="admin", hashed_password="x", platform_role=PlatformRole.ADMIN),
                ]
            )
            await db.commit()
        self.user_id, self.admin_id = 1, 2

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def _violate(self, db, **overrides):
        params = {"user_id": self.user_id, "category": "harassment", "source": "auto_ai"}
        params.update(overrides)
        outcome = await penalty_service.record_violation(db, **params)
        await db.commit()
        penalty_service.discard_notifications(db)
        return outcome

    async def test_points_accumulate_into_mute_then_ban(self) -> None:
        async with self.sessions() as db:
            first = await self._violate(db)  # L2 = 3 分
            self.assertEqual(first.penalty, Penalty("warning"))
            second = await self._violate(db)  # 6 分 -> 禁言 1 天
            self.assertEqual(second.penalty, Penalty("mute", 24))
            user = await db.get(User, self.user_id)
            self.assertIsNotNone(penalty_service.mute_state(user))
            self.assertEqual(user.status, UserStatus.ACTIVE)
            with self.assertRaises(HTTPException) as caught:
                penalty_service.ensure_can_post(user)
            self.assertEqual(caught.exception.detail["code"], "account_muted")

            third = await self._violate(db)  # 9 分 -> 禁言 3 天
            self.assertEqual(third.penalty, Penalty("mute", 72))
            fourth = await self._violate(db)  # 12 分 -> 封 7 天
            self.assertEqual(fourth.penalty, Penalty("ban", 24 * 7))
            self.assertEqual(user.status, UserStatus.DISABLED)
            self.assertIsNotNone(user.banned_until)
            self.assertEqual(await penalty_service.active_points(db, self.user_id), 12)

    async def test_ban_revokes_sessions(self) -> None:
        async with self.sessions() as db:
            db.add(AuthSession(user_id=self.user_id, session_hash="s", expires_at=datetime.now(UTC) + timedelta(days=1)))
            await db.commit()
            await self._violate(db, category="minor_sexual", source="auto_image")
            session = (await db.execute(AuthSession.__table__.select())).first()
            self.assertIsNotNone(session.revoked_at)

    async def test_machine_never_bans_permanently_and_flags_for_review(self) -> None:
        async with self.sessions() as db:
            outcome = None
            for _ in range(4):  # 4 次 L3 = 24 分，阶梯上是永封
                outcome = await self._violate(db, category="fraud", source="auto_rule")
            self.assertEqual(outcome.penalty, Penalty("ban", 24 * 7))
            self.assertEqual(outcome.violation.review_status, "pending")
            self.assertEqual(outcome.violation.recommended_penalty, "ban:permanent")
            user = await db.get(User, self.user_id)
            self.assertIsNotNone(user.banned_until)

            await penalty_service.adjust_penalty(db, outcome.violation.id, actor_id=self.admin_id, penalty=Penalty("ban", None))
            await db.commit()
            self.assertIsNone(user.banned_until)
            self.assertEqual(user.status, UserStatus.DISABLED)
            self.assertEqual(outcome.violation.review_status, "adjusted")
            self.assertTrue(outcome.violation.penalty_permanent)

    async def test_same_content_is_only_punished_once(self) -> None:
        async with self.sessions() as db:
            first = await self._violate(db, target_type="message", target_id=7)
            self.assertIsNotNone(first)
            self.assertIsNone(await self._violate(db, target_type="message", target_id=7))
            self.assertEqual(await penalty_service.active_points(db, self.user_id), 3)
            # 人工改判为更高等级时，旧记录作废、按新等级重算
            upgraded = await self._violate(db, target_type="message", target_id=7, category="threat", source="report", actor_id=self.admin_id)
            self.assertIsNotNone(upgraded)
            self.assertEqual(await penalty_service.active_points(db, self.user_id), 6)

    async def test_expired_points_do_not_count(self) -> None:
        async with self.sessions() as db:
            outcome = await self._violate(db)
            outcome.violation.expires_at = datetime.now(UTC) - timedelta(days=1)
            await db.commit()
            self.assertEqual(await penalty_service.active_points(db, self.user_id), 0)
            self.assertEqual((await self._violate(db)).penalty, Penalty("warning"))

    async def test_admin_is_warned_but_never_muted_or_banned(self) -> None:
        async with self.sessions() as db:
            outcome = await self._violate(db, user_id=self.admin_id, category="minor_sexual", source="auto_image")
            self.assertEqual(outcome.penalty, Penalty("warning"))
            admin = await db.get(User, self.admin_id)
            self.assertEqual(admin.status, UserStatus.ACTIVE)
            self.assertIsNone(admin.muted_until)

    async def test_accepted_appeal_revokes_points_and_lifts_ban(self) -> None:
        async with self.sessions() as db:
            outcome = await self._violate(db, category="minor_sexual", source="auto_image")
            user = await db.get(User, self.user_id)
            self.assertEqual(user.status, UserStatus.DISABLED)
            await penalty_service.submit_appeal(db, self.user_id, outcome.violation.id, "这是一张普通的游戏截图")
            with self.assertRaises(HTTPException):
                await penalty_service.submit_appeal(db, self.user_id, outcome.violation.id, "再申诉一次试试")
            await penalty_service.review_appeal(db, outcome.violation.id, reviewer_id=self.admin_id, accept=True, note="误判")
            self.assertEqual(user.status, UserStatus.ACTIVE)
            self.assertIsNone(user.banned_at)
            self.assertEqual(outcome.violation.status, "revoked")
            self.assertEqual(await penalty_service.active_points(db, self.user_id), 0)

    async def test_rejected_appeal_keeps_penalty(self) -> None:
        async with self.sessions() as db:
            outcome = await self._violate(db, category="fraud", source="auto_rule")
            await penalty_service.submit_appeal(db, self.user_id, outcome.violation.id, "我没有诈骗别人")
            await penalty_service.review_appeal(db, outcome.violation.id, reviewer_id=self.admin_id, accept=False, note=None)
            user = await db.get(User, self.user_id)
            self.assertIsNotNone(penalty_service.mute_state(user))
            self.assertEqual(outcome.violation.status, "active")
            self.assertEqual(outcome.violation.appeal_status, "rejected")

    async def test_revoking_one_mute_keeps_the_other(self) -> None:
        async with self.sessions() as db:
            first = await self._violate(db, category="fraud", source="auto_rule", target_type="message", target_id=1)
            second = await self._violate(db, category="fraud", source="auto_rule", target_type="message", target_id=2)
            self.assertEqual(second.penalty, Penalty("ban", 24 * 7))
            await penalty_service.revoke_violation(db, second.violation.id, actor_id=self.admin_id, reason="误判")
            await db.commit()
            user = await db.get(User, self.user_id)
            self.assertEqual(user.status, UserStatus.ACTIVE)
            self.assertIsNotNone(penalty_service.mute_state(user))
            await penalty_service.revoke_violation(db, first.violation.id, actor_id=self.admin_id, reason="误判")
            await db.commit()
            self.assertIsNone(user.muted_until)

    async def test_revoke_does_not_release_a_ban_the_ledger_does_not_own(self) -> None:
        async with self.sessions() as db:
            outcome = await self._violate(db)  # 只是警告
            user = await db.get(User, self.user_id)
            # 台账上线前就存在的旧封禁：比这条记录更早，且台账里没有对应的封禁行
            user.status = UserStatus.DISABLED
            user.banned_at = datetime.now(UTC) - timedelta(days=30)
            user.ban_reason = "旧封禁"
            await db.commit()
            await penalty_service.revoke_violation(db, outcome.violation.id, actor_id=self.admin_id, reason="误判")
            await db.commit()
            self.assertEqual(user.status, UserStatus.DISABLED)
            self.assertEqual(user.ban_reason, "旧封禁")

    async def test_manual_override_and_lift(self) -> None:
        async with self.sessions() as db:
            outcome = await self._violate(db, category="admin_action", level=0, source="admin", actor_id=self.admin_id, override=Penalty("ban", None))
            user = await db.get(User, self.user_id)
            self.assertEqual(user.status, UserStatus.DISABLED)
            self.assertIsNone(user.banned_until)
            self.assertEqual(outcome.violation.points, 0)
            await penalty_service.lift_ban(db, user, actor_id=self.admin_id)
            await db.commit()
            self.assertEqual(user.status, UserStatus.ACTIVE)
            self.assertFalse(outcome.violation.penalty_permanent)

    async def test_standing_and_payload(self) -> None:
        async with self.sessions() as db:
            outcome = await self._violate(db, target_type="message", target_id=3, conversation_id=9, summary="违规内容摘要")
            user = await db.get(User, self.user_id)
            standing = await penalty_service.user_standing(db, user)
            self.assertEqual(standing["points"], 3)
            self.assertEqual(standing["next_threshold"], 6)
            self.assertEqual(standing["next_penalty"], "mute:24")
            payload = penalty_service.violation_payload(outcome.violation)
            self.assertEqual(payload["source"], "auto")
            self.assertEqual(payload["category_label"], "辱骂骚扰")
            self.assertTrue(payload["can_appeal"])
            self.assertNotIn("reason", payload)
            title, body = penalty_service.describe_violation(outcome.violation)
            self.assertIn("辱骂骚扰", title)
            self.assertIn("累计 3 分", body)


class AppealTokenTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(_create_tables)

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def test_banned_login_gets_scoped_token_that_normal_auth_rejects(self) -> None:
        async with self.sessions() as db:
            user = User(username="banned", hashed_password="x", status=UserStatus.DISABLED, banned_at=datetime.now(UTC))
            db.add(user)
            await db.commit()
            with self.assertRaises(HTTPException) as caught:
                await ensure_user_not_disabled(db, user, issue_appeal_token=True)
            token = caught.exception.detail["appeal_token"]
            self.assertEqual(decode_access_token(token)["scope"], APPEAL_TOKEN_SCOPE)
            # 即使之后解封，这张令牌也不能当正常登录态用
            user.status = UserStatus.ACTIVE
            await db.commit()
            with self.assertRaises(HTTPException) as rejected:
                await get_current_user_from_token(db, token)
            self.assertEqual(rejected.exception.status_code, 401)
            self.assertEqual((await get_current_user_from_token(db, create_access_token(user.id))).id, user.id)

    async def test_session_resume_does_not_leak_appeal_token(self) -> None:
        async with self.sessions() as db:
            user = User(username="banned", hashed_password="x", status=UserStatus.DISABLED, banned_at=datetime.now(UTC))
            db.add(user)
            await db.commit()
            with self.assertRaises(HTTPException) as caught:
                await ensure_user_not_disabled(db, user)
            self.assertNotIn("appeal_token", caught.exception.detail)


class ViolationModelTests(unittest.TestCase):
    def test_table_registered(self) -> None:
        self.assertIn("user_violations", Base.metadata.tables)
        self.assertIn("muted_until", Base.metadata.tables["users"].columns)
        self.assertEqual(UserViolation.__tablename__, "user_violations")


if __name__ == "__main__":
    unittest.main()
