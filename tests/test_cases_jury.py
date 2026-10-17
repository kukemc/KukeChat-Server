from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.models  # noqa: F401
from app.core.config import settings
from app.core.database import Base
from app.models.case import JuryMember, JuryVote, ReportCase
from app.models.conversation import Conversation, ConversationMember
from app.models.enums import ConversationType, MemberRole, PlatformRole, ReportStatus, ReportTargetType
from app.models.friend import Friendship
from app.models.message import Message
from app.models.penalty import UserViolation
from app.models.report import UserReport
from app.models.user import User
from app.schemas.report import ReportCreate, ReportReview
from app.services import cases as case_service
from app.services import jury as jury_service
from app.services import moderation as moderation_service
from app.services import notifications as notification_service
from app.services import reports as report_service
from app.services.jury import tally


class TallyTests(unittest.TestCase):
    def test_three_unanimous_votes_settle(self) -> None:
        self.assertEqual(tally(["l2", "l2", "l1"]), ("violation", 2))
        self.assertEqual(tally(["none", "none", "none"]), ("clean", None))
        self.assertIsNone(tally(["l2", "l2"]))

    def test_level_is_lower_median_of_violation_votes(self) -> None:
        # 二期：按现有票会定成严重违规（l3）但有效票不足 5 张时先不定案，到 5 张再按原规则表决
        self.assertIsNone(tally(["l1", "l3", "l3"]))
        self.assertIsNone(tally(["l1", "l3", "l3", "l3"]))
        self.assertEqual(tally(["l1", "l3", "l3", "l3", "l3"]), ("violation", 3))
        self.assertEqual(tally(["l1", "l3", "l3", "l3", "none"]), ("violation", 3))
        self.assertEqual(tally(["l1", "l1", "l3"]), ("violation", 1))
        self.assertEqual(tally(["l1", "l2", "l2", "l3", "none"]), ("violation", 2))

    def test_split_waits_for_five_then_needs_four(self) -> None:
        self.assertIsNone(tally(["l2", "l2", "none"]))
        self.assertIsNone(tally(["l2", "l2", "none", "l1"]))
        self.assertEqual(tally(["l2", "l2", "none", "l1", "l1"]), ("violation", 1))
        self.assertEqual(tally(["l2", "l2", "none", "none", "l1"]), ("escalate", None))

    def test_abstentions_do_not_count_but_cannot_stall_forever(self) -> None:
        self.assertIsNone(tally(["abstain", "abstain", "l2", "l2"]))
        self.assertEqual(tally(["abstain", "l2", "l2", "l2"]), ("violation", 2))
        self.assertEqual(tally(["abstain"] * 5 + ["l2", "none"]), ("escalate", None))


class ReportValidationTests(unittest.TestCase):
    def _check(self, target, reason, details=None, attachments=(), evidence=()):
        case_service.validate_report_payload(target, reason, details, list(attachments), list(evidence))

    def test_other_reason_needs_real_details(self) -> None:
        with self.assertRaises(HTTPException) as caught:
            self._check(ReportTargetType.MESSAGE, "其他", "不好")
        self.assertEqual(caught.exception.detail["code"], "details_required")
        self._check(ReportTargetType.MESSAGE, "其他", "他在群里反复发同一条引战的话")
        self._check(ReportTargetType.MESSAGE, "辱骂骚扰")

    def test_unknown_reason_is_rejected(self) -> None:
        with self.assertRaises(HTTPException) as caught:
            self._check(ReportTargetType.MESSAGE, "看他不爽")
        self.assertEqual(caught.exception.detail["code"], "invalid_reason")

    def test_reporting_a_user_needs_evidence(self) -> None:
        with self.assertRaises(HTTPException) as caught:
            self._check(ReportTargetType.USER, "辱骂骚扰")
        self.assertEqual(caught.exception.detail["code"], "evidence_required")
        self._check(ReportTargetType.USER, "辱骂骚扰", evidence=[3])
        self._check(ReportTargetType.USER, "辱骂骚扰", attachments=["/uploads/messages/a.png"])


class CaseAndJuryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with self.sessions() as db:
            names = ["alice", "bob", "offender", "admin", "juror1", "juror2", "juror3", "juror4", "juror5", "insider"]
            users = [User(username=name, hashed_password="x") for name in names]
            users[3].platform_role = PlatformRole.ADMIN
            db.add_all(users)
            await db.flush()
            self.ids = {user.username: user.id for user in users}
            group = Conversation(type=ConversationType.GROUP, title="创作群")
            direct = Conversation(type=ConversationType.DIRECT)
            db.add_all([group, direct])
            await db.flush()
            self.group_id, self.direct_id = group.id, direct.id
            for name in ("alice", "bob", "offender", "insider"):
                db.add(ConversationMember(conversation_id=group.id, user_id=self.ids[name], role=MemberRole.MEMBER))
            for name in ("alice", "offender"):
                db.add(ConversationMember(conversation_id=direct.id, user_id=self.ids[name], role=MemberRole.MEMBER))
            db.add_all(
                [
                    Message(conversation_id=group.id, sender_id=self.ids["bob"], content="大家好"),
                    Message(conversation_id=group.id, sender_id=self.ids["offender"], content="你们都是废物"),
                    Message(conversation_id=group.id, sender_id=self.ids["alice"], content="别这样说话"),
                    Message(conversation_id=direct.id, sender_id=self.ids["offender"], content="私聊里的辱骂"),
                ]
            )
            for name in ("juror1", "juror2", "juror3", "juror4", "juror5", "insider"):
                db.add(JuryMember(user_id=self.ids[name], status="active", term_ends_at=datetime.now(UTC) + timedelta(days=30)))
            await db.commit()
        self.group_message_id, self.direct_message_id = 2, 4

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def _report(self, db, reporter: str, message_id: int, reason: str = "辱骂骚扰") -> UserReport:
        return await report_service.create_report(
            db, self.ids[reporter], ReportCreate(target_type=ReportTargetType.MESSAGE, target_id=message_id, reason=reason)
        )

    async def _queue_for_jury(self, db, case_id: int) -> ReportCase:
        case = await db.get(ReportCase, case_id)
        case.queue = "jury"
        await db.commit()
        return case

    async def _rid(self, db, user: User, case_id: int) -> int:
        """这位委员在这个案件上的评审编号：委员侧接口用的是它，不是案件编号。"""
        return (await db.execute(select(JuryVote.id).where(JuryVote.case_id == case_id, JuryVote.juror_id == user.id))).scalar_one()

    async def _real(self, db, review_id: int) -> int:
        return (await db.execute(select(JuryVote.case_id).where(JuryVote.id == review_id))).scalar_one()

    async def _vote(self, db, juror: str, vote: str) -> dict:
        user = await db.get(User, self.ids[juror])
        view = await jury_service.next_case(db, user)
        self.assertIsNotNone(view, f"{juror} got no case")
        return await jury_service.cast_vote(db, user, view["case_id"], vote, None)

    async def test_reports_on_the_same_target_merge_into_one_case(self) -> None:
        async with self.sessions() as db:
            first = await self._report(db, "alice", self.group_message_id)
            second = await self._report(db, "bob", self.group_message_id, "其他" if False else "垃圾广告")
            self.assertEqual(first.case_id, second.case_id)
            self.assertIsNotNone(getattr(first, "_new_case_id"))
            self.assertIsNone(getattr(second, "_new_case_id"))
            case = await db.get(ReportCase, first.case_id)
            self.assertEqual((case.report_count, case.status, case.queue, case.jury_eligible), (2, "open", "triage", True))
            self.assertGreater(case.priority, 0)

            with self.assertRaises(HTTPException) as duplicate:
                await self._report(db, "alice", self.group_message_id)
            self.assertEqual(duplicate.exception.status_code, 409)
            self.assertEqual(duplicate.exception.detail["code"], "already_reported")
            with self.assertRaises(HTTPException) as own:
                await self._report(db, "offender", self.group_message_id)
            self.assertEqual(own.exception.status_code, 400)

            private = await self._report(db, "alice", self.direct_message_id)
            self.assertFalse((await db.get(ReportCase, private.case_id)).jury_eligible)

    async def test_admin_resolution_fans_out_to_every_report(self) -> None:
        async with self.sessions() as db:
            first = await self._report(db, "alice", self.group_message_id)
            await self._report(db, "bob", self.group_message_id)
            case = await case_service.resolve_case(
                db, first.case_id, source="admin", actor_id=self.ids["admin"], upheld=True, category="harassment", note="人身攻击"
            )
            self.assertEqual((case.status, case.verdict_source, case.verdict_level), ("resolved", "admin", 2))
            message = await db.get(Message, self.group_message_id)
            self.assertIsNotNone(message.recalled_at)
            violation = (await db.execute(select(UserViolation))).scalar_one()
            self.assertEqual((violation.user_id, violation.source, violation.case_id, violation.points), (self.ids["offender"], "report", case.id, 3))
            self.assertEqual(case.violation_id, violation.id)
            reports = await case_service.case_reports(db, case.id)
            self.assertEqual({report.status for report in reports}, {ReportStatus.REVIEWED})
            self.assertEqual({report.action for report in reports}, {"recall_message+penalty_warning"})

            credit = await case_service.reporter_credit(db, self.ids["alice"])
            self.assertEqual((credit["upheld"], credit["dismissed"]), (1, 0))
            notifications = await notification_service.list_unified_notifications(db, self.ids["bob"])
            results = [item for item in notifications.items if item.type == "report_result"]
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0].title, "你的举报已处理")
            # 已处理的内容不能再被举报
            with self.assertRaises(HTTPException) as handled:
                await self._report(db, "insider", self.group_message_id)
            self.assertEqual(handled.exception.detail["code"], "already_handled")

    async def test_malicious_reports_cost_reporting_quota(self) -> None:
        async with self.sessions() as db:
            before = await case_service.reporter_credit(db, self.ids["alice"])
            self.assertEqual(before["daily_quota"], 10)
            for message_id in (1, 3):
                reporter = "alice" if message_id == 1 else "bob"
                report = await self._report(db, reporter, message_id)
                await case_service.resolve_case(
                    db, report.case_id, source="admin", actor_id=self.ids["admin"], upheld=False, malicious_reporter_ids={self.ids["alice"]}
                )
            alice_report = (await db.execute(select(UserReport).where(UserReport.reporter_id == self.ids["alice"]))).scalar_one()
            self.assertTrue(alice_report.malicious)
            self.assertEqual(alice_report.status, ReportStatus.REJECTED)
            after = await case_service.reporter_credit(db, self.ids["alice"])
            self.assertEqual(after["malicious"], 1)
            self.assertLess(after["score"], before["score"])
            # bob 的举报只是不成立，不算恶意
            self.assertEqual((await case_service.reporter_credit(db, self.ids["bob"]))["malicious"], 0)
            message = await db.get(Message, 1)
            self.assertIsNone(message.recalled_at)

    async def test_jury_consensus_resolves_case_with_capped_source(self) -> None:
        async with self.sessions() as db:
            report = await self._report(db, "alice", self.group_message_id)
            await self._queue_for_jury(db, report.case_id)

            juror = await db.get(User, self.ids["juror1"])
            view = await jury_service.next_case(db, juror)
            self.assertEqual(view["kind"], "message")
            speakers = [item["speaker"] for item in view["messages"]]
            self.assertEqual(speakers, ["成员A", "被举报人", "成员B"])
            self.assertTrue(view["messages"][1]["is_target"])
            self.assertNotIn("offender", str(view))
            self.assertNotIn("sender_id", str(view))
            # 没投票前再取，还是同一个案件
            self.assertEqual((await jury_service.next_case(db, juror))["case_id"], view["case_id"])

            self.assertFalse((await self._vote(db, "juror1", "l2"))["settled"])
            self.assertFalse((await self._vote(db, "juror2", "l2"))["settled"])
            self.assertTrue((await self._vote(db, "juror3", "l3"))["settled"])

            case = await db.get(ReportCase, report.case_id)
            await db.refresh(case)
            self.assertEqual((case.status, case.verdict_source, case.verdict_level), ("resolved", "jury", 2))
            self.assertIsNotNone((await db.get(Message, self.group_message_id)).recalled_at)
            violation = (await db.execute(select(UserViolation))).scalar_one()
            self.assertEqual((violation.source, violation.level, violation.case_id), ("jury", 2, case.id))
            member = await jury_service.get_member(db, self.ids["juror1"])
            self.assertEqual((member.votes_total, member.votes_settled, member.votes_agreed), (1, 1, 1))
            with self.assertRaises(HTTPException):
                await jury_service.cast_vote(db, juror, await self._rid(db, juror, case.id), "none", None)
            self.assertEqual((await jury_service.history(db, self.ids["juror1"]))[0]["outcome"], "violation")

    async def test_jurors_with_conflicts_never_see_the_case(self) -> None:
        async with self.sessions() as db:
            report = await self._report(db, "alice", self.group_message_id)
            await self._queue_for_jury(db, report.case_id)
            # insider 是案发群的成员；juror5 是被举报人的好友
            db.add(Friendship(user_id=self.ids["juror5"], friend_id=self.ids["offender"]))
            await db.commit()
            for name in ("insider", "juror5"):
                self.assertIsNone(await jury_service.next_case(db, await db.get(User, self.ids[name])))
            self.assertIsNotNone(await jury_service.next_case(db, await db.get(User, self.ids["juror1"])))

            # 私聊举报不进风纪委员队列，管理员也不能硬塞进去
            private = await self._report(db, "alice", self.direct_message_id)
            with self.assertRaises(HTTPException):
                await case_service.move_case(db, private.case_id, "jury", actor_id=self.ids["admin"])
            outsider = User(username="outsider", hashed_password="x")
            db.add(outsider)
            await db.commit()
            with self.assertRaises(HTTPException) as denied:
                await jury_service.next_case(db, outsider)
            self.assertEqual(denied.exception.detail["code"], "not_juror")

    async def test_split_jury_escalates_and_admin_verdict_scores_the_votes(self) -> None:
        async with self.sessions() as db:
            report = await self._report(db, "alice", self.group_message_id)
            await self._queue_for_jury(db, report.case_id)
            for name, vote in (("juror1", "l2"), ("juror2", "none"), ("juror3", "l1"), ("juror4", "none"), ("juror5", "l2")):
                self.assertFalse((await self._vote(db, name, vote))["settled"])
            case = await db.get(ReportCase, report.case_id)
            await db.refresh(case)
            self.assertEqual((case.status, case.queue), ("open", "admin"))
            self.assertIsNone((await db.get(Message, self.group_message_id)).recalled_at)

            case = await case_service.resolve_case(db, case.id, source="admin", actor_id=self.ids["admin"], upheld=False, note="玩笑话")
            await jury_service.settle_after_admin(db, case)
            await db.commit()
            agreed = {self.ids["juror2"]: True, self.ids["juror1"]: False}
            for vote in (await db.execute(select(JuryVote))).scalars().all():
                if vote.juror_id in agreed:
                    self.assertEqual(vote.agreed, agreed[vote.juror_id])
            self.assertEqual((await jury_service.get_member(db, self.ids["juror4"])).votes_agreed, 1)

    async def test_golden_cases_measure_jurors_and_suspend_the_unreliable(self) -> None:
        async with self.sessions() as db:
            report = await self._report(db, "alice", self.group_message_id)
            case = await case_service.resolve_case(db, report.case_id, source="admin", actor_id=self.ids["admin"], upheld=True, category="harassment")
            # 二期：评审中的质控题只出已核对的；位置随机，隔了太多张没出才强制出
            case.is_golden, case.golden_verified = True, True
            member = await jury_service.get_member(db, self.ids["juror1"])
            member.votes_since_quality = jury_service.QUALITY_MAX_GAP + 1
            member.golden_total, member.golden_correct = 4, 2
            await db.commit()

            juror = await db.get(User, self.ids["juror1"])
            view = await jury_service.next_case(db, juror)
            self.assertEqual(await self._real(db, view["case_id"]), case.id)
            # 质控题里被举报的消息已经撤回，但委员仍然要能看到原文
            self.assertEqual(view["messages"][1]["content"], "你们都是废物")
            result = await jury_service.cast_vote(db, juror, await self._rid(db, juror, case.id), "none", None)
            self.assertFalse(result["settled"])
            await db.refresh(member)
            self.assertEqual((member.golden_total, member.golden_correct), (5, 2))
            self.assertEqual(member.status, "suspended")
            with self.assertRaises(HTTPException):
                await jury_service.next_case(db, juror)

    async def test_triage_auto_closes_clear_violations_and_queues_the_rest(self) -> None:
        async def blocked(*_args, **_kwargs):
            return {"decision": "block", "severity": 2, "categories": ["fraud"], "confidence": 0.99, "reason": "命中诈骗规则", "model": "rules-first"}

        async def allowed(*_args, **_kwargs):
            return {"decision": "allow", "severity": 0, "categories": ["safe"], "confidence": 0.9, "model": "Gandi-AIHub"}

        async with self.sessions() as db:
            clear = await self._report(db, "alice", self.group_message_id, "诈骗或钓鱼")
            unclear = await self._report(db, "alice", 1)
            private = await self._report(db, "alice", self.direct_message_id)
        with patch.object(case_service, "AsyncSessionLocal", self.sessions), patch.object(settings, "content_moderation_enabled", True):
            with patch.object(moderation_service, "_call_text_moderation", blocked):
                await case_service.triage_case(clear.case_id)
            with patch.object(moderation_service, "_call_text_moderation", allowed):
                await case_service.triage_case(unclear.case_id)
                await case_service.triage_case(private.case_id)
        async with self.sessions() as db:
            closed = await db.get(ReportCase, clear.case_id)
            self.assertEqual((closed.status, closed.verdict_source, closed.machine_decision, closed.verdict_category), ("resolved", "auto", "block", "fraud"))
            self.assertIsNotNone((await db.get(Message, self.group_message_id)).recalled_at)
            self.assertEqual((await db.get(UserReport, clear.id)).status, ReportStatus.REVIEWED)
            queued = await db.get(ReportCase, unclear.case_id)
            self.assertEqual((queued.status, queued.queue, queued.machine_decision), ("open", "jury", "allow"))
            self.assertEqual((await db.get(ReportCase, private.case_id)).queue, "admin")

    async def test_open_cases_sort_by_creation_time(self) -> None:
        async with self.sessions() as db:
            reports = [await self._report(db, "alice", self.group_message_id), await self._report(db, "bob", 3), await self._report(db, "alice", 1)]
            ids = [report.case_id for report in reports]
            now = datetime.now(UTC)
            # 建案时间：第二个最早，第一个最新；优先级让第三个排最前
            for case_id, days, priority in zip(ids, (1, 9, 5), (1.0, 2.0, 9.0)):
                case = await db.get(ReportCase, case_id)
                case.created_at, case.priority = now - timedelta(days=days), priority
            await db.commit()
            newest, _total, _counts = await case_service.list_cases(db)
            self.assertEqual([case.id for case in newest], [ids[0], ids[2], ids[1]])
            oldest, _total, _counts = await case_service.list_cases(db, sort="oldest")
            self.assertEqual([case.id for case in oldest], [ids[1], ids[2], ids[0]])
            by_priority, _total, _counts = await case_service.list_cases(db, sort="priority")
            self.assertEqual([case.id for case in by_priority], [ids[2], ids[1], ids[0]])

    async def test_archiving_old_cases_closes_them_without_a_verdict(self) -> None:
        async with self.sessions() as db:
            old_group = await self._report(db, "alice", self.group_message_id)
            old_private = await self._report(db, "alice", self.direct_message_id)
            fresh = await self._report(db, "bob", 3)
            now = datetime.now(UTC)
            for case_id, days, queue in ((old_group.case_id, 30, "jury"), (old_private.case_id, 10, "admin"), (fresh.case_id, 1, "admin")):
                case = await db.get(ReportCase, case_id)
                case.created_at, case.queue = now - timedelta(days=days), queue
            await db.commit()
            credit_before = await case_service.reporter_credit(db, self.ids["alice"])
            # 委员先分到了这个旧案件，还没投
            juror = await db.get(User, self.ids["juror1"])
            assigned = await jury_service.next_case(db, juror)
            self.assertEqual(await self._real(db, assigned["case_id"]), old_group.case_id)

            before = now - timedelta(days=7)
            preview = await case_service.archive_cases(db, before=before, dry_run=True, actor_id=self.ids["admin"])
            self.assertEqual(preview, {"count": 2, "by_queue": {"jury": 1, "admin": 1}, "dry_run": True})
            self.assertEqual((await case_service.archive_cases(db, before=before, queue="jury", dry_run=True, actor_id=self.ids["admin"]))["count"], 1)
            self.assertEqual((await db.get(ReportCase, old_group.case_id)).status, "open")

            result = await case_service.archive_cases(db, before=before, actor_id=self.ids["admin"])
            self.assertEqual((result["count"], result["dry_run"]), (2, False))
            for case_id in (old_group.case_id, old_private.case_id):
                case = await db.get(ReportCase, case_id)
                await db.refresh(case)
                self.assertEqual((case.status, case.verdict_source, case.verdict_note), ("archived", "archive", case_service.ARCHIVE_NOTE))
                self.assertIsNotNone(case.closed_at)
                self.assertFalse(case.is_golden)
            for report_id in (old_group.id, old_private.id):
                report = await db.get(UserReport, report_id)
                await db.refresh(report)
                self.assertEqual((report.status, report.action, report.review_note), (ReportStatus.REJECTED, "archived", case_service.ARCHIVE_NOTE))
                read = await report_service.build_report_read(db, await report_service.get_report(db, report_id), self.ids["alice"])
                self.assertTrue(read.archived)
                self.assertEqual(read.result_title, "你的举报已归档")
            self.assertFalse((await report_service.build_report_read(db, await report_service.get_report(db, fresh.id), self.ids["bob"])).archived)
            # 不处罚、不撤内容、不通知举报人
            self.assertEqual((await db.execute(select(UserViolation))).scalars().all(), [])
            self.assertIsNone((await db.get(Message, self.group_message_id)).recalled_at)
            notifications = await notification_service.list_unified_notifications(db, self.ids["alice"])
            self.assertEqual([item for item in notifications.items if item.type == "report_result"], [])
            # 举报人信誉不变
            credit_after = await case_service.reporter_credit(db, self.ids["alice"])
            self.assertEqual((credit_after["score"], credit_after["dismissed"], credit_after["malicious"]), (credit_before["score"], 0, 0))
            # 待处理列表里只剩新的；已归档能单独查；open_counts 不含已归档
            open_items, open_total, counts = await case_service.list_cases(db)
            self.assertEqual(([case.id for case in open_items], open_total, counts), ([fresh.case_id], 1, {"admin": 1}))
            archived, archived_total, _counts = await case_service.list_cases(db, status_filter="archived")
            self.assertEqual(({case.id for case in archived}, archived_total), ({old_group.case_id, old_private.case_id}, 2))
            # 没有结论：不能改判
            with self.assertRaises(HTTPException) as overturn:
                await case_service.overturn_case(db, old_group.case_id, actor_id=self.ids["admin"], upheld=True, note="补判")
            self.assertEqual(overturn.exception.status_code, 409)
            # 委员再也取不到它（包括已经分到手的那一份）
            self.assertIsNone(await jury_service.next_case(db, juror))
            self.assertIsNone(await jury_service.next_case(db, await db.get(User, self.ids["juror2"])))
            # 再归档一次什么也不做
            self.assertEqual((await case_service.archive_cases(db, before=before, actor_id=self.ids["admin"]))["count"], 0)

    async def test_late_vote_on_archived_case_is_not_scored(self) -> None:
        async with self.sessions() as db:
            report = await self._report(db, "alice", self.group_message_id)
            await self._queue_for_jury(db, report.case_id)
            juror = await db.get(User, self.ids["juror1"])
            view = await jury_service.next_case(db, juror)
            await case_service.archive_cases(db, before=datetime.now(UTC) + timedelta(minutes=1), actor_id=self.ids["admin"])
            result = await jury_service.cast_vote(db, juror, view["case_id"], "l2", None)
            self.assertFalse(result["settled"])
            vote = (await db.execute(select(JuryVote).where(JuryVote.juror_id == juror.id))).scalar_one()
            self.assertIsNone(vote.agreed)
            self.assertIsNone((await jury_service.history(db, juror.id))[0]["outcome"])
            items, _total = await jury_service.list_votes(db, juror.id)
            self.assertEqual((items[0]["stage"], items[0]["outcome"]), ("closed", None))

    async def test_legacy_review_and_backfill(self) -> None:
        async with self.sessions() as db:
            first = await self._report(db, "alice", self.group_message_id)
            second = await self._report(db, "bob", self.group_message_id)
            await report_service.review_report(
                db, first.id, self.ids["admin"], ReportReview(status=ReportStatus.REVIEWED, delete_content=True, violation_category="harassment")
            )
            case = await db.get(ReportCase, first.case_id)
            await db.refresh(case)
            self.assertEqual((case.status, case.verdict_source), ("resolved", "admin"))
            sibling = await db.get(UserReport, second.id)
            await db.refresh(sibling)
            self.assertEqual(sibling.status, ReportStatus.REVIEWED)
            self.assertIsNotNone(case.violation_id)

            # 案件上线前的存量举报：两条待处理的并成一案，已撤回的直接结案
            db.add_all(
                [
                    UserReport(reporter_id=self.ids["alice"], target_type=ReportTargetType.MESSAGE, target_id=3, message_id=3, conversation_id=self.group_id, reported_user_id=self.ids["alice"], reason="其他"),
                    UserReport(reporter_id=self.ids["bob"], target_type=ReportTargetType.MESSAGE, target_id=3, message_id=3, conversation_id=self.group_id, reported_user_id=self.ids["alice"], reason="其他"),
                    UserReport(reporter_id=self.ids["bob"], target_type=ReportTargetType.MESSAGE, target_id=self.group_message_id, message_id=self.group_message_id, conversation_id=self.group_id, reported_user_id=self.ids["offender"], reason="辱骂骚扰"),
                ]
            )
            await db.commit()
            stats = await case_service.backfill_cases(db)
            self.assertEqual(stats, {"reports": 3, "cases_open": 1, "cases_closed": 0, "auto_closed": 1})
            self.assertEqual((await case_service.backfill_cases(db))["reports"], 0)
            merged = (await db.execute(select(ReportCase).where(ReportCase.target_key == "message:3"))).scalar_one()
            self.assertEqual((merged.report_count, merged.queue, merged.status), (2, "jury", "open"))


if __name__ == "__main__":
    unittest.main()
