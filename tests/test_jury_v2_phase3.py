"""风纪系统 v2 三期的前半部分（docs/jury-v2.md 3.1、3.2、3.3、3.7、3.8）：
资深合议、申诉复审、机器拿不准的内容作为案源、重复举报堵漏、举报人复议。"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Annotated
from unittest.mock import patch

import httpx
from fastapi import Depends, FastAPI, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

import app.models  # noqa: F401
from app.api.v1 import admin as admin_api
from app.api.v1 import jury as jury_api
from app.api.v1 import jury_senior as senior_api
from app.api.v1 import reports as reports_api
from app.core.config import settings
from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.models.case import JuryPointLog, JuryReview, JuryVote, ReportCase
from app.models.conversation import Conversation, ConversationMember
from app.models.enums import ConversationType, MemberRole, ModerationSource, PostVisibility, ReportStatus, ReportTargetType
from app.models.friend import Friendship
from app.models.message import Message
from app.models.penalty import UserViolation
from app.models.post import Post
from app.models.report import AdminAuditLog, ContentModerationRecord, UserReport
from app.models.user import User
from app.schemas.case import CaseRead, JuryCaseDetail, JuryCaseView, JuryStatus, JuryVoteResult
from app.schemas.jury import JuryConfig, JuryVoteItem, PublicCase, PublicCaseDetail
from app.schemas.jury_senior import AdminReviewRead, AppealMineItem, AppealReviewView, AppealVoteResult, PanelCaseView
from app.schemas.penalty import ViolationRead
from app.schemas.report import AdminMessageRecall, ReportCreate, ReportReview
from app.services import cases as case_service
from app.services import jury as jury_service
from app.services import jury_machine as machine_service
from app.services import jury_opinions as opinion_service
from app.services import jury_overview as overview_service
from app.services import jury_panel as panel_service
from app.services import jury_reviews as review_service
from app.services import jury_settings
from app.services import messages as message_service
from app.services import moderation as moderation_service
from app.services import notifications as notification_service
from app.services import penalties as penalty_service
from app.services import reports as report_service
from app.websocket.manager import manager

from test_jury_v2_phase2 import Phase2TestCase

# 原审里凑不出共识的一组票：5 张有效票，3 比 2
SPLIT = (("juror1", "l2"), ("juror2", "none"), ("juror3", "l1"), ("speaker", "none"), ("lurker", "l2"))
SPLIT_TALLY = {"none": 2, "l1": 1, "l2": 2, "l3": 0, "abstain": 0, "need_context": 0}
SENIORS = ("juror4", "juror5", "juror6")
# 机器「拿不准」的一条文本结论（新版审核服务直接带 category）
UNCERTAIN = {"decision": "review", "severity": 1, "categories": ["harassment"], "category": "harassment", "confidence": 0.62, "reason": "疑似辱骂", "model": "Gandi-AIHub"}
APPEAL_TEXT = "我只是在开玩笑，没有恶意"


class Phase3TestCase(Phase2TestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        asyncio.get_running_loop().set_debug(False)

    # ------------------------------------------------------------ 工具

    async def _seniors(self, db, *names: str) -> None:
        for name in names or SENIORS:
            (await self._member(db, name)).tier = "senior"
        await db.commit()

    async def _switch(self, db, **switches: bool) -> None:
        await jury_settings.update_switches(db, self.ids["admin"], switches)

    async def _split(self, db, case_id: int, note: str | None = None) -> ReportCase:
        """让原审凑不出共识：案件由 escalate_from_jury 决定去哪。"""
        for index, (juror, vote) in enumerate(SPLIT):
            result = await self._cast(db, juror, case_id, vote, note if index == 0 else None)
        self.assertFalse(result["settled"])
        case = await db.get(ReportCase, case_id)
        await db.refresh(case)
        return case

    async def _panel_cast(self, db, juror: str, case_id: int, vote: str, note: str | None = None, category: str | None = None) -> dict:
        """对指定的案件投合议票：还没分到手的先直接分给他。"""
        user = await self._user(db, juror)
        existing = (await db.execute(select(JuryVote).where(JuryVote.case_id == case_id, JuryVote.juror_id == user.id))).scalar_one_or_none()
        if existing is None:
            db.add(JuryVote(case_id=case_id, juror_id=user.id, assigned_at=datetime.now(UTC), stage="panel"))
            await db.commit()
        return await panel_service.cast_vote(db, user, await self._rid(db, user, case_id), vote, note, category)

    async def _review_vote(self, db, juror: str, review_id: int, vote: str, **details) -> dict:
        """取到这件复审再投票（走正常的分案）。"""
        user = await self._user(db, juror)
        view = await review_service.next_review(db, user)
        self.assertIsNotNone(view, f"{juror} got no review")
        self.assertEqual(view["review_id"], review_id)
        return await review_service.cast_vote(db, user, review_id, vote, **details)

    async def _prior_points(self, db, points: int) -> None:
        """给 offender 垫一些还在生效的记分，让下一次违规跨过阶梯。"""
        now = datetime.now(UTC)
        db.add(
            UserViolation(
                user_id=self.ids["offender"], category="other", level=1, points=points, source="admin",
                created_at=now - timedelta(days=1), expires_at=now + timedelta(days=89), points_total_after=points,
            )
        )
        await db.commit()

    async def _jury_violation(self, db, message: str = "insult") -> tuple[ReportCase, UserViolation]:
        """委员定案的一条处罚：L2 记 3 分，加上已有的 4 分跨过 6 分线，禁言 24 小时。"""
        await self._prior_points(db, 4)
        case = await self._settle(db, await self._jury_case(db, message))
        violation = await db.get(UserViolation, case.violation_id)
        self.assertEqual((violation.source, violation.penalty_kind, violation.case_id), ("jury", "mute", case.id))
        return case, violation

    async def _machine_violation(self, db, **fields) -> UserViolation:
        """机器自动处罚的一条记录（大厅里的那条消息）：没有案件，只有审查记录。"""
        now = datetime.now(UTC)
        record = ContentModerationRecord(
            source=ModerationSource.MESSAGE_AUTO, message_id=self.messages["plaza_insult"], conversation_id=self.plaza_id,
            user_id=self.ids["offender"], content_type="text", content_text="大厅里的辱骂", decision="block", severity=2, categories='["harassment"]',
        )
        db.add(record)
        await db.flush()
        values = dict(
            user_id=self.ids["offender"], category="harassment", level=2, points=3, source="auto_ai", target_type="message",
            target_id=self.messages["plaza_insult"], conversation_id=self.plaza_id, moderation_record_id=record.id, summary="大厅里的辱骂",
            created_at=now, expires_at=now + timedelta(days=90), penalty_kind="mute", penalty_ends_at=now + timedelta(hours=24), points_total_after=3,
        )
        values.update(fields)
        violation = UserViolation(**values)
        db.add(violation)
        await db.commit()
        return violation

    async def _appeal(self, db, violation: UserViolation) -> JuryReview | None:
        await penalty_service.submit_appeal(db, violation.user_id, violation.id, APPEAL_TEXT)
        return (
            await db.execute(select(JuryReview).where(JuryReview.kind == "appeal", JuryReview.violation_id == violation.id))
        ).scalar_one_or_none()

    async def _moderate(self, message_id: int, result: dict = UNCERTAIN, source: ModerationSource = ModerationSource.MESSAGE_AUTO) -> None:
        """跑一遍消息的机器审核任务，审核服务的结论用给定的。"""

        async def verdict(*_args, **_kwargs):
            return dict(result)

        with patch.object(moderation_service, "AsyncSessionLocal", self.sessions), patch.object(moderation_service, "_call_text_moderation", verdict):
            await moderation_service._moderate_message_task(message_id, source, None)

    async def _machine_cases(self, db) -> list[ReportCase]:
        rows = await db.execute(select(ReportCase).where(ReportCase.case_origin == "machine").order_by(ReportCase.id.asc()))
        return list(rows.scalars().all())

    async def _points(self, db, name: str) -> dict[str, int]:
        """这个人各种加分项各得了多少分。"""
        rows = await db.execute(select(JuryPointLog.kind, func.sum(JuryPointLog.points)).where(JuryPointLog.user_id == self.ids[name]).group_by(JuryPointLog.kind))
        return {kind: int(total) for kind, total in rows.all()}


# ---------------------------------------------------------------- 资深合议


class PanelRoutingTests(Phase3TestCase):
    async def test_three_ways_out_of_the_jury_queue_enter_the_panel(self) -> None:
        async with self.sessions() as db:
            await self._seniors(db)
            now = datetime.now(UTC)
            # 无共识
            split = await self._open_case(db)
            with self._events() as events:
                split = await self._split(db, split.id)
            self.assertEqual((split.status, split.queue, split.jury_outcome, split.panel_outcome), ("open", "panel", "no_consensus", None))
            self.assertIsNotNone(split.panel_at)
            self.assertIsNotNone(split.escalated_at)
            self.assertIn("jury.queue.changed", [event for event, _users, _data in events])
            # 原审委员的详情里案件现在处于合议阶段
            detail = await jury_service.case_detail(db, self.ids["juror1"], await self._rid(db, "juror1", split.id))
            self.assertEqual((detail["stage"], detail["tally"], detail["verdict"]), ("panel", None, None))
            JuryCaseDetail(**detail)

            # 上下文不足累计 2 票
            context = await self._open_case(db)
            await self._cast(db, "juror1", context.id, "need_context")
            await self._cast(db, "juror2", context.id, "need_context")
            await db.refresh(context)
            self.assertEqual((context.queue, context.jury_outcome), ("panel", "need_context"))

            # 72 小时超时：定时任务里处理
            stale = await self._open_case(db, queued_at=now - timedelta(hours=73))
            self.assertEqual((await case_service.sweep_jury_queue(db))["timeout"], 1)
            await db.refresh(stale)
            self.assertEqual((stale.queue, stale.jury_outcome, stale.panel_outcome), ("panel", "timeout", None))

            # 总览、角标、后台列表
            overview_service.reset_overview_cache()
            juror4 = await self._user(db, "juror4")
            self.assertEqual((await overview_service.overview(db, juror4))["backlog"], {"jury": 0, "panel": 3})
            status = await jury_service.my_status(db, juror4)
            JuryStatus(**status)
            self.assertEqual(status["senior"], {"panel": 3, "appeals": 0})
            # 不是资深委员的没有这一项
            self.assertIsNone((await jury_service.my_status(db, await self._user(db, "juror1")))["senior"])
            self.assertIsNone((await jury_service.my_status(db, await self._user(db, "alice")))["senior"])
            items, total, counts = await case_service.list_cases(db, queue="panel")
            self.assertEqual((total, counts.get("panel"), counts.get("admin")), (3, 3, None))
            reads = [CaseRead(**item) for item in await case_service.build_case_reads(db, items)]
            self.assertEqual({(item.queue, item.case_origin, item.panel_outcome, item.reconsider_status) for item in reads}, {("panel", "report", None, "none")})
            self.assertTrue(all(item.panel_at is not None for item in reads))

    async def test_without_three_available_seniors_the_case_goes_to_the_admin(self) -> None:
        async with self.sessions() as db:
            offender = self.ids["offender"]

            async def route(**fields) -> ReportCase:
                case = await self._open_case(db, **fields)
                return await self._split(db, case.id)

            # 一个资深委员都没有：和三期之前一样转管理员
            none = await route()
            self.assertEqual((none.queue, none.jury_outcome, none.panel_outcome, none.panel_at), ("admin", "no_consensus", "unavailable", None))
            # 只有两位
            await self._seniors(db, "juror4", "juror5")
            self.assertEqual((await route()).queue, "admin")
            # 三位，但其中一位在原审里投过票
            await self._seniors(db, "juror3")
            voted = await route()
            self.assertEqual((voted.queue, voted.panel_outcome), ("admin", "unavailable"))
            (await self._member(db, "juror3")).tier = "regular"
            await self._seniors(db, "juror6")
            # 三位，但其中一位在原审里回避过
            recused = await self._open_case(db)
            await self._cast(db, "juror6", recused.id, "recuse")
            self.assertEqual((await self._split(db, recused.id)).queue, "admin")
            # 其中一位正在受处罚
            juror6 = await self._user(db, "juror6")
            juror6.muted_until = datetime.now(UTC) + timedelta(hours=2)
            await db.commit()
            self.assertEqual((await route()).queue, "admin")
            juror6.muted_until = None
            # 其中一位要回避（被举报人的好友）
            friendship = Friendship(user_id=juror6.id, friend_id=offender)
            db.add(friendship)
            await db.commit()
            self.assertEqual((await route(reported_user_id=offender)).queue, "admin")
            await db.delete(friendship)
            # 其中一位任期已经届满
            member = await self._member(db, "juror6")
            member.term_ends_at = datetime.now(UTC) - timedelta(hours=1)
            await db.commit()
            self.assertEqual((await route()).queue, "admin")
            member.term_ends_at = datetime.now(UTC) + timedelta(days=30)
            await db.commit()
            # 都没问题：进合议
            self.assertEqual((await route(reported_user_id=offender)).queue, "panel")
            # 转管理员的案件照常能被管理员处理
            closed = await case_service.resolve_case(db, none.id, source="admin", actor_id=self.ids["admin"], upheld=False)
            self.assertEqual((closed.status, closed.panel_outcome), ("dismissed", "unavailable"))


class PanelVotingTests(Phase3TestCase):
    async def test_two_agreeing_votes_settle_the_case(self) -> None:
        async with self.sessions() as db:
            await self._seniors(db)
            # offender 已有 15 分：严重违规再记 6 分会跨过 18 分的 30 天封禁线
            await self._prior_points(db, 15)
            case_id = await self._jury_case(db, "insult")
            case = await self._split(db, case_id, "原审备注")
            self.assertEqual(case.queue, "panel")
            juror4, juror5 = await self._user(db, "juror4"), await self._user(db, "juror5")

            view = await panel_service.next_case(db, juror4)
            PanelCaseView(**view)
            rid4 = view["case_id"]
            self.assertEqual(await self._real(db, rid4), case_id)
            self.assertNotEqual(rid4, case_id)
            self.assertEqual((view["kind"], view["report_count"], view["case_origin"]), ("message", 1, "report"))
            self.assertEqual((view["origin"]["reason"], view["origin"]["tally"]), ("no_consensus", SPLIT_TALLY))
            # 原审的观点只读，每条都带作者投的票（案件还没定案）
            self.assertEqual([(item["alias"], item["vote"], item["content"]) for item in view["origin"]["opinions"]], [("委员 A", "l2", "原审备注")])
            self.assertEqual((view["panel"]["seats"], view["panel"]["votes_cast"]), (3, 0))
            self.assertEqual(view["panel"]["expires_at"], penalty_service.aware(case.panel_at) + timedelta(hours=48))
            self.assertNotIn("sender_id", str(view))
            # 重复调用返回同一件
            self.assertEqual((await panel_service.next_case(db, juror4))["case_id"], rid4)
            # 评审台不出合议的分配；拿合议的评审编号去投普通票、拿原审的评审编号去投合议票都是 404
            self.assertIsNone(await jury_service.next_case(db, juror4))
            self.assertEqual(await self._code(jury_service.cast_vote(db, juror4, rid4, "l2", None)), (404, None))
            juror1 = await self._user(db, "juror1")
            (await self._member(db, "juror1")).tier = "senior"
            await db.commit()
            self.assertEqual(await self._code(panel_service.cast_vote(db, juror1, await self._rid(db, juror1, case_id), "l2", None)), (404, "case_not_found"))
            # 原审投过票的资深委员分不到这个案件
            self.assertIsNone(await panel_service.next_case(db, juror1))
            (await self._member(db, "juror1")).tier = "trainee"
            await db.commit()
            # 要回避的资深委员（案发群的成员）也分不到
            (await self._member(db, "insider")).tier = "senior"
            await db.commit()
            self.assertIsNone(await panel_service.next_case(db, await self._user(db, "insider")))
            self.assertEqual((await jury_service.my_status(db, await self._user(db, "insider")))["senior"], {"panel": 0, "appeals": 0})
            # 不是资深委员
            self.assertEqual(await self._code(panel_service.next_case(db, juror1)), (403, "not_senior"))
            self.assertEqual(await self._code(panel_service.next_case(db, await self._user(db, "alice"))), (403, "not_senior"))
            # 合议不能弃权
            for invalid in ("abstain", "need_context"):
                self.assertEqual(await self._code(panel_service.cast_vote(db, juror4, rid4, invalid, None)), (422, "invalid_vote"))
            self.assertEqual(await self._code(panel_service.cast_vote(db, juror4, rid4, "l3", None, "sexual")), (422, "invalid_category"))

            with self._events() as events:
                first = await panel_service.cast_vote(db, juror4, rid4, "l3", "性质恶劣", "threat")
                JuryVoteResult(**first)
                self.assertEqual((first["case_id"], first["settled"], first["case"]["stage"], first["case"]["tally"]), (rid4, False, "panel", None))
                # 代号接着原审往下排
                self.assertEqual(first["case"]["my_vote"]["alias"], "委员 F")
                self.assertEqual(await self._code(panel_service.cast_vote(db, juror4, rid4, "l3", None)), (409, "already_voted"))
                # 下一位看到的进度；先投的合议委员的备注不在「原审的观点」里
                view5 = await panel_service.next_case(db, juror5)
                self.assertEqual((view5["panel"]["votes_cast"], len(view5["origin"]["opinions"])), (1, 1))
                second = await panel_service.cast_vote(db, juror5, view5["case_id"], "l3", None, "threat")
            self.assertTrue(second["settled"])
            await db.refresh(case)
            self.assertEqual(
                (case.status, case.queue, case.verdict_source, case.verdict_level, case.verdict_category, case.jury_outcome, case.panel_outcome),
                ("resolved", "panel", "panel", 3, "threat", "no_consensus", "consensus"),
            )
            self.assertTrue(case.publishable)
            detail = second["case"]
            JuryCaseDetail(**detail)
            self.assertEqual((detail["stage"], detail["verdict"]["source"], detail["verdict"]["outcome"], detail["verdict"]["level"]), ("closed", "panel", "violation", 3))
            # 原审的票数分布保持原样，合议的另给
            self.assertEqual(detail["tally"], {**SPLIT_TALLY, "panel": {"none": 0, "l1": 0, "l2": 0, "l3": 2}})
            self.assertEqual((detail["my_vote"]["agreed"], detail["my_vote"]["points"]), (True, 15))

            # 处罚按委员一档：阶梯算出的 30 天封禁被压到 7 天，等管理员确认
            violation = await db.get(UserViolation, case.violation_id)
            self.assertEqual((violation.source, violation.level, violation.penalty_kind, violation.review_status, violation.recommended_penalty), ("jury", 3, "ban", "pending", "ban:720"))
            self.assertEqual(round((penalty_service.aware(violation.penalty_ends_at) - penalty_service.aware(violation.created_at)).total_seconds() / 3600), 168)
            self.assertIsNotNone((await db.get(Message, self.messages["insult"])).recalled_at)

            # 原审的票按合议的结论回填一致率和风纪值；合议票是 panel_agreed
            agreed = dict((await db.execute(select(JuryVote.juror_id, JuryVote.agreed).where(JuryVote.case_id == case_id))).all())
            self.assertEqual({name: agreed[self.ids[name]] for name, _vote in SPLIT}, {"juror1": True, "juror2": False, "juror3": True, "speaker": False, "lurker": True})
            for name, settled, right in (("juror1", 1, 1), ("juror2", 1, 0), ("juror4", 1, 1)):
                member = await self._member(db, name)
                self.assertEqual((member.votes_settled, member.votes_agreed, member.votes_total), (settled, right, 1))
            self.assertEqual(await self._points(db, "juror1"), {"daily_first": 2, "vote_agreed": 10})
            self.assertEqual(await self._points(db, "juror2"), {"daily_first": 2, "vote_disagreed": 3})
            self.assertEqual(await self._points(db, "juror4"), {"daily_first": 2, "panel_agreed": 15})
            self.assertEqual((await self._member(db, "juror4")).points, 17)

            # 定案通知发给原审和合议的每一位，各自是自己的评审编号
            settled = self._per_user(events, "jury.case.settled")
            self.assertEqual(set(settled), {self.ids[name] for name in ("juror1", "juror2", "juror3", "speaker", "lurker", "juror4", "juror5")})
            self.assertEqual((settled[juror4.id]["case_id"], settled[juror4.id]["outcome"]), (rid4, "violation"))

            # 我的评审：这一票是合议投的
            items, _total = await jury_service.list_votes(db, juror4.id)
            JuryVoteItem(**items[0])
            self.assertEqual((items[0]["case_id"], items[0]["vote_stage"], items[0]["stage"], items[0]["verdict_source"], items[0]["agreed"]), (rid4, "panel", "closed", "panel", True))
            items, _total = await jury_service.list_votes(db, juror1.id)
            self.assertEqual((items[0]["vote_stage"], items[0]["verdict_source"], items[0]["agreed"]), ("jury", "panel", True))
            # 合议委员是这个案件的参与者：能进观点区、能发言
            page = await opinion_service.list_opinions(db, juror4, rid4)
            self.assertEqual([(item["alias"], item["vote"], item["content"]) for item in page["items"]], [("委员 A", "l2", "原审备注"), ("委员 F", "l3", "性质恶劣")])
            reply = await opinion_service.create_opinion(db, juror5, view5["case_id"], "同意", page["items"][1]["id"])
            self.assertEqual((reply["alias"], reply["parent_id"]), ("委员 G", page["items"][1]["id"]))
            # 没轮到的第三位：案件已经定案，没有可分的了
            self.assertIsNone(await panel_service.next_case(db, await self._user(db, "juror6")))
            # 后台案件详情的票带类别和轮次
            admin_detail = await admin_api._case_detail(db, case, self.ids["admin"])
            self.assertEqual(
                [(vote.juror.username, vote.vote, vote.category, vote.stage) for vote in admin_detail.votes if vote.vote == "l3"],
                [("juror4", "l3", "threat", "panel"), ("juror5", "l3", "threat", "panel")],
            )
            self.assertEqual({vote.stage for vote in admin_detail.votes if vote.vote != "l3"}, {"jury"})

    async def test_level_is_the_lower_median_and_a_split_waits_for_the_third_vote(self) -> None:
        self.assertEqual(panel_service.panel_tally(["l1", "l3"]), ("violation", 1))
        self.assertEqual(panel_service.panel_tally(["l3", "none", "l2"]), ("violation", 2))
        self.assertEqual(panel_service.panel_tally(["l3", "l3"]), ("violation", 3))
        self.assertEqual(panel_service.panel_tally(["none", "none"]), ("clean", None))
        self.assertEqual(panel_service.panel_tally(["none", "l1", "none"]), ("clean", None))
        self.assertIsNone(panel_service.panel_tally(["none", "l1"]))
        self.assertIsNone(panel_service.panel_tally(["l2"]))
        async with self.sessions() as db:
            await self._seniors(db)
            case = await self._split(db, (await self._open_case(db)).id)
            self.assertFalse((await self._panel_cast(db, "juror4", case.id, "l2", None, "harassment"))["settled"])
            self.assertFalse((await self._panel_cast(db, "juror5", case.id, "none"))["settled"])
            third = await self._panel_cast(db, "juror6", case.id, "none")
            self.assertTrue(third["settled"])
            await db.refresh(case)
            self.assertEqual((case.status, case.verdict_source, case.verdict_level, case.panel_outcome), ("dismissed", "panel", None, "consensus"))
            self.assertEqual(third["case"]["tally"]["panel"], {"none": 2, "l1": 0, "l2": 1, "l3": 0})
            # 合议票与结论不一致 +5
            self.assertEqual(await self._points(db, "juror4"), {"daily_first": 2, "panel_disagreed": 5})
            self.assertEqual(await self._points(db, "juror6"), {"daily_first": 2, "panel_agreed": 15})
            # 原审里投「不违规」的两位算对了
            self.assertEqual((await self._points(db, "juror2"))["vote_agreed"], 10)
            self.assertEqual((await self._points(db, "juror1"))["vote_disagreed"], 3)

            # 等级取较低中位数：l1 + l3 定为轻微
            other = await self._split(db, (await self._open_case(db)).id)
            await self._panel_cast(db, "juror4", other.id, "l1", None, "flooding")
            self.assertTrue((await self._panel_cast(db, "juror5", other.id, "l3", None, "flooding"))["settled"])
            await db.refresh(other)
            self.assertEqual((other.status, other.verdict_level, other.verdict_category), ("resolved", 1, "flooding"))

    async def test_timeout_and_admin_takeover(self) -> None:
        async with self.sessions() as db:
            await self._seniors(db)
            admin_id = self.ids["admin"]
            stale = await self._split(db, (await self._open_case(db)).id)
            fresh = await self._split(db, (await self._open_case(db)).id)
            await self._panel_cast(db, "juror4", stale.id, "l2")
            stale.panel_at = datetime.now(UTC) - timedelta(hours=49)
            fresh.panel_at = datetime.now(UTC) - timedelta(hours=47)
            await db.commit()
            # jury.queue.changed 同一进程 30 秒内只发一次：前面进合议时已经发过了，这里重新计时
            case_service._last_queue_event_at = None
            with self._events() as events:
                self.assertEqual(await case_service.sweep_senior_queues(db), {"panel": 1, "reviews": 0})
            for case in (stale, fresh):
                await db.refresh(case)
            self.assertEqual((stale.status, stale.queue, stale.panel_outcome, stale.jury_outcome), ("open", "admin", "timeout", "no_consensus"))
            self.assertEqual((fresh.queue, fresh.panel_outcome), ("panel", None))
            self.assertIn("jury.queue.changed", [event for event, _users, _data in events])
            # 已经投出的那张合议票，之后按管理员的结论结算
            await case_service.resolve_case(db, stale.id, source="admin", actor_id=admin_id, upheld=True, category="harassment")
            self.assertEqual((await self._points(db, "juror4"))["panel_agreed"], 15)
            await db.refresh(stale)
            self.assertEqual((stale.verdict_source, stale.panel_outcome), ("admin", "timeout"))

            # 管理员可以直接给合议中的案件下结论
            taken = await case_service.resolve_case(db, fresh.id, source="admin", actor_id=admin_id, upheld=False)
            self.assertEqual((taken.status, taken.queue, taken.verdict_source, taken.panel_outcome), ("dismissed", "panel", "admin", "manual"))
            # 也可以把它挪到自己的队列里
            moved = await self._split(db, (await self._open_case(db)).id)
            moved = await case_service.move_case(db, moved.id, "admin", actor_id=admin_id)
            self.assertEqual((moved.queue, moved.panel_outcome), ("admin", "manual"))
            # 合议中途人不够了（有人被降级）：不用等到超时
            short = await self._split(db, (await self._open_case(db)).id)
            self.assertEqual(short.queue, "panel")
            (await self._member(db, "juror6")).tier = "regular"
            await db.commit()
            self.assertEqual((await case_service.sweep_senior_queues(db))["panel"], 1)
            await db.refresh(short)
            self.assertEqual((short.queue, short.panel_outcome), ("admin", "unavailable"))

    async def test_recusal_frees_the_seat_or_falls_back_to_the_admin(self) -> None:
        async with self.sessions() as db:
            # 四位资深委员：insider 在这类没有会话的案件上不用回避
            await self._seniors(db, *SENIORS, "insider")
            case = await self._split(db, (await self._open_case(db)).id)
            juror4 = await self._user(db, "juror4")
            result = await self._panel_cast(db, "juror4", case.id, "recuse", "认识当事人")
            self.assertEqual((result["settled"], result["case"]), (False, None))
            await db.refresh(case)
            # 还有三位能参加：留在合议里
            self.assertEqual((case.queue, case.panel_outcome), ("panel", None))
            rid4 = await self._rid(db, juror4, case.id)
            self.assertEqual(await self._code(jury_service.case_detail(db, juror4.id, rid4)), (403, "not_voted"))
            self.assertEqual(await self._code(panel_service.cast_vote(db, juror4, rid4, "l2", None)), (409, "already_recused"))
            items, _total = await jury_service.list_votes(db, juror4.id)
            self.assertEqual((items[0]["vote"], items[0]["vote_stage"], items[0]["can_view"]), ("recuse", "panel", False))
            self.assertIsNone(await panel_service.next_case(db, juror4))
            # 再有一位回避就凑不齐了：转管理员
            case_service._last_queue_event_at = None
            with self._events() as events:
                await self._panel_cast(db, "juror5", case.id, "recuse")
            await db.refresh(case)
            self.assertEqual((case.status, case.queue, case.panel_outcome), ("open", "admin", "unavailable"))
            self.assertIn("jury.queue.changed", [event for event, _users, _data in events])
            self.assertIsNone(await panel_service.next_case(db, await self._user(db, "juror6")))


# ---------------------------------------------------------------- 申诉复审


class AppealRoutingTests(Phase3TestCase):
    async def test_every_condition_must_hold_for_the_senior_review(self) -> None:
        async with self.sessions() as db:
            await self._seniors(db)
            now = datetime.now(UTC)
            reason = review_service.appeal_block_reason
            eligible = await self._machine_violation(db)
            # 开关关着：全部到管理员
            self.assertEqual(await reason(db, eligible), "switch_off")
            await self._switch(db, appeals=True)
            self.assertIsNone(await reason(db, eligible))

            # 处罚来源：管理员手动、管理员结案的举报都不进；标成委员来源但找不到案件的也不进
            for source in ("admin", "report", "jury"):
                self.assertEqual(await reason(db, await self._machine_violation(db, source=source)), "source")
            # 内容：私聊、临时会话、评论、仅好友可见的动态都不算
            direct = Conversation(type=ConversationType.DIRECT)
            temporary = Conversation(type=ConversationType.GROUP, title="临时会话", is_temporary=True)
            friends_post = Post(author_id=self.ids["offender"], content="仅好友可见", visibility=PostVisibility.FRIENDS, moderation_status="approved")
            db.add_all([direct, temporary, friends_post])
            await db.flush()
            self.assertEqual(await reason(db, await self._machine_violation(db, conversation_id=direct.id)), "content")
            self.assertEqual(await reason(db, await self._machine_violation(db, conversation_id=temporary.id)), "content")
            self.assertEqual(await reason(db, await self._machine_violation(db, target_type="post_comment", conversation_id=None)), "content")
            self.assertEqual(await reason(db, await self._machine_violation(db, target_type="profile_cover", conversation_id=None)), "content")
            self.assertEqual(await reason(db, await self._machine_violation(db, target_type="post", target_id=friends_post.id, conversation_id=None)), "content")
            self.assertIsNone(await reason(db, await self._machine_violation(db, target_type="post", target_id=self.post_id, conversation_id=None)))
            # 类别：色情 / 涉未成年
            for category in ("sexual", "minor_sexual"):
                self.assertEqual(await reason(db, await self._machine_violation(db, category=category)), "category")
            # 处罚：永久封禁、超过 7 天的封禁、被上限压住还在等管理员确认的
            self.assertEqual(await reason(db, await self._machine_violation(db, penalty_kind="ban", penalty_ends_at=None, penalty_permanent=True)), "penalty")
            self.assertEqual(await reason(db, await self._machine_violation(db, penalty_kind="ban", penalty_ends_at=now + timedelta(days=8))), "penalty")
            self.assertEqual(await reason(db, await self._machine_violation(db, penalty_kind="ban", review_status="pending", penalty_ends_at=now + timedelta(days=7))), "penalty")
            self.assertIsNone(await reason(db, await self._machine_violation(db, penalty_kind="ban", penalty_ends_at=now + timedelta(days=7))))
            self.assertIsNone(await reason(db, await self._machine_violation(db, penalty_kind="warning", penalty_ends_at=None)))
            # 资深委员：要有三位能参加的。群成员要回避（大厅没到「大型公开群」的阈值）
            await self._seniors(db, "lurker")
            (await self._member(db, "juror6")).tier = "regular"
            await db.commit()
            self.assertEqual(await reason(db, eligible), "seniors")
            with patch.object(jury_service, "LARGE_GROUP_MEMBERS", 5):
                # 大型公开群只回避当事人：lurker 可以参加
                self.assertIsNone(await reason(db, eligible))
            await self._seniors(db, "juror6")
            self.assertIsNone(await reason(db, eligible))

            # 落到申诉上：不满足的和现在一样直接到管理员，满足的进资深复审
            blocked = await self._machine_violation(db, source="admin")
            self.assertIsNone(await self._appeal(db, blocked))
            payload = penalty_service.violation_payload(blocked)
            ViolationRead(**payload)
            self.assertEqual((payload["appeal_status"], payload["appeal_stage"], payload["appeal_result"], payload["appeal_decided_by"]), ("pending", "admin", None, None))
            with self._events() as events:
                review = await self._appeal(db, eligible)
            self.assertEqual((review.kind, review.violation_id, review.case_id, review.requester_id, review.stage, review.status), ("appeal", eligible.id, None, self.ids["offender"], "senior", "open"))
            self.assertEqual(penalty_service.aware(review.expires_at) - penalty_service.aware(review.created_at), timedelta(hours=72))
            payload = penalty_service.violation_payload(eligible)
            ViolationRead(**payload)
            self.assertEqual((payload["appeal_status"], payload["appeal_stage"], payload["can_appeal"]), ("pending", "senior", False))
            self.assertIn("jury.queue.changed", [event for event, _users, _data in events])
            # 每条处罚仍然只能申诉一次
            with self.assertRaises(HTTPException) as caught:
                await penalty_service.submit_appeal(db, eligible.user_id, eligible.id, APPEAL_TEXT)
            self.assertEqual(caught.exception.status_code, 409)
            # 群成员是资深委员也分不到这件复审；别的资深委员可以
            self.assertIsNone(await review_service.next_review(db, await self._user(db, "lurker")))
            self.assertEqual((await jury_service.my_status(db, await self._user(db, "juror4")))["senior"], {"panel": 0, "appeals": 1})
            self.assertEqual((await jury_service.my_status(db, await self._user(db, "lurker")))["senior"], {"panel": 0, "appeals": 0})

    async def test_switch_off_keeps_the_appeal_flow_as_it_was(self) -> None:
        async with self.sessions() as db:
            await self._seniors(db)
            _case, from_jury = await self._jury_violation(db)
            from_machine = await self._machine_violation(db)
            for violation in (from_jury, from_machine):
                self.assertIsNone(await self._appeal(db, violation))
                payload = penalty_service.violation_payload(violation)
                self.assertEqual((payload["appeal_status"], payload["appeal_stage"]), ("pending", "admin"))
            self.assertEqual((await db.execute(select(func.count(JuryReview.id)))).scalar_one(), 0)
            self.assertIsNone(await review_service.next_review(db, await self._user(db, "juror4")))
            # 管理员照旧处理：通过即撤销，驳回保持原样
            accepted = await penalty_service.review_appeal(db, from_jury.id, reviewer_id=self.ids["admin"], accept=True, note="误判")
            payload = penalty_service.violation_payload(accepted)
            ViolationRead(**payload)
            self.assertEqual((payload["status"], payload["appeal_status"], payload["appeal_stage"], payload["appeal_result"], payload["appeal_decided_by"]), ("revoked", "accepted", None, "revoked", "admin"))
            rejected = await penalty_service.review_appeal(db, from_machine.id, reviewer_id=self.ids["admin"], accept=False, note=None)
            payload = penalty_service.violation_payload(rejected)
            self.assertEqual((payload["status"], payload["appeal_status"], payload["appeal_result"], payload["appeal_decided_by"]), ("active", "rejected", "upheld", "admin"))
            # 管理员处理过的申诉不能再处理一次（和原来一样是 400）
            with self.assertRaises(HTTPException) as caught:
                await penalty_service.review_appeal(db, from_machine.id, reviewer_id=self.ids["admin"], accept=True, note=None)
            self.assertEqual(caught.exception.status_code, 400)
            details = json.loads((await db.execute(select(AdminAuditLog.details).where(AdminAuditLog.action == "violation.appeal.reviewed").order_by(AdminAuditLog.id.desc()).limit(1))).scalar_one())
            self.assertEqual(set(details), {"violation_id", "accepted", "note"})


class AppealOutcomeTests(Phase3TestCase):
    async def test_uphold_rejects_the_appeal(self) -> None:
        async with self.sessions() as db:
            await self._seniors(db)
            await self._switch(db, appeals=True)
            case, violation = await self._jury_violation(db)
            review = await self._appeal(db, violation)
            self.assertEqual((review.case_id, review.stage), (case.id, "senior"))
            juror4, juror5 = await self._user(db, "juror4"), await self._user(db, "juror5")
            # 不是资深委员（原审委员）不能审
            self.assertEqual(await self._code(review_service.next_review(db, await self._user(db, "juror1"))), (403, "not_senior"))
            # 原审投过票的人即使是资深委员也分不到
            (await self._member(db, "juror1")).tier = "senior"
            await db.commit()
            self.assertIsNone(await review_service.next_review(db, await self._user(db, "juror1")))

            view = await review_service.next_review(db, juror4)
            AppealReviewView(**view)
            self.assertEqual(
                (view["review_id"], view["type"], view["kind"], view["source"], view["category"], view["category_label"], view["level"]),
                (review.id, "appeal", "message", "jury", "harassment", "辱骂骚扰", 2),
            )
            self.assertEqual((view["penalty"], view["appeal_text"], view["content"], view["seats"], view["votes_cast"]), ({"kind": "mute", "hours": 24}, APPEAL_TEXT, None, 3, 0))
            self.assertEqual(view["expires_at"], penalty_service.aware(review.expires_at))
            # 案件快照的匿名视图 + 原审票数分布
            self.assertEqual([item["speaker"] for item in view["messages"] if item["is_target"]], ["被举报人"])
            self.assertEqual(view["origin"]["tally"], {"none": 0, "l1": 0, "l2": 3, "l3": 0, "abstain": 0, "need_context": 0})
            self.assertNotIn("sender_id", str(view))
            self.assertNotIn("offender", str(view))
            self.assertEqual((await review_service.next_review(db, juror4))["review_id"], review.id)

            # 没分给我的 404；这种复审没有的选项 422
            self.assertEqual(await self._code(review_service.cast_vote(db, juror5, review.id, "uphold")), (404, "review_not_found"))
            self.assertEqual(await self._code(review_service.cast_vote(db, juror4, 99999, "uphold")), (404, "review_not_found"))
            self.assertEqual(await self._code(review_service.cast_vote(db, juror4, review.id, "overturn")), (422, "invalid_vote"))
            first = await review_service.cast_vote(db, juror4, review.id, "uphold", "证据清楚")
            AppealVoteResult(**first)
            self.assertEqual((first["review_id"], first["settled"], first["outcome"]), (review.id, False, None))
            # 每张复审票 +8，当场给
            self.assertEqual((first["reward"]["points"], first["reward"]["items"][0]["kind"], first["reward"]["items"][0]["label"]), (8, "review_vote", "参与复审"))
            self.assertEqual(await self._code(review_service.cast_vote(db, juror4, review.id, "uphold")), (409, "already_voted"))
            with self._events() as events:
                second = await self._review_vote(db, "juror5", review.id, "uphold")
            self.assertEqual((second["settled"], second["outcome"]), (True, "uphold"))

            await db.refresh(violation)
            payload = penalty_service.violation_payload(violation)
            ViolationRead(**payload)
            self.assertEqual(
                (payload["status"], payload["appeal_status"], payload["appeal_stage"], payload["appeal_result"], payload["appeal_decided_by"], payload["penalty_kind"]),
                ("active", "rejected", None, "upheld", "senior", "mute"),
            )
            self.assertIsNone(violation.appeal_reviewer_id)
            self.assertIsNotNone(penalty_service.mute_state(await self._user(db, "offender")))
            await db.refresh(review)
            self.assertEqual((review.status, review.outcome, review.decided_by), ("closed", "uphold", "senior"))
            # 审计日志：actor 为空，注明来自资深复审
            log = (await db.execute(select(AdminAuditLog).where(AdminAuditLog.action == "violation.appeal.reviewed"))).scalar_one()
            details = json.loads(log.details)
            self.assertEqual((log.actor_id, log.target_id, details["by"], details["result"], details["accepted"], details["votes"]), (None, self.ids["offender"], "senior_review", "upheld", False, {"uphold": 2}))
            # 通知申诉人：已有的事件 + 通知中心
            updated = self._per_user(events, "moderation.violation.updated")
            self.assertEqual(set(updated), {self.ids["offender"]})
            self.assertEqual((updated[self.ids["offender"]]["action"], updated[self.ids["offender"]]["violation"]["appeal_result"]), ("appeal_rejected", "upheld"))
            notifications = await notification_service.list_unified_notifications(db, self.ids["offender"])
            self.assertEqual([item.title for item in notifications.items if item.type == "jury"], ["你的申诉有结果了"])

            # 我参与过的复审
            items, total = await review_service.my_reviews(db, juror4.id)
            AppealMineItem(**items[0])
            self.assertEqual(total, 1)
            self.assertEqual(
                (items[0]["review_id"], items[0]["type"], items[0]["source"], items[0]["category_label"], items[0]["penalty"], items[0]["my_vote"], items[0]["outcome"]),
                (review.id, "appeal", "jury", "辱骂骚扰", {"kind": "mute", "hours": 24}, "uphold", "uphold"),
            )
            self.assertIsNotNone(items[0]["settled_at"])
            # 没轮到的第三位：已经结束
            self.assertIsNone(await review_service.next_review(db, await self._user(db, "juror6")))
            self.assertEqual((await jury_service.my_status(db, juror4))["senior"], {"panel": 0, "appeals": 0})
            # 后台监控：每位复审委员的票是实名的
            items, total = await review_service.admin_list(db, kind="appeal", status_filter="closed")
            read = AdminReviewRead(**items[0])
            self.assertEqual((total, read.outcome, read.decided_by, read.stage, read.violation_id, read.case_id, read.text), (1, "uphold", "senior", "senior", violation.id, case.id, APPEAL_TEXT))
            self.assertEqual([(vote.juror.username, vote.vote, vote.note) for vote in read.votes], [("juror4", "uphold", "证据清楚"), ("juror5", "uphold", None)])
            self.assertEqual((await review_service.admin_list(db, status_filter="open"))[1], 0)

            # 管理员随时可以改掉复审的结果：这里把「维持」改成撤销，案件跟着改判
            overridden = await penalty_service.review_appeal(db, violation.id, reviewer_id=self.ids["admin"], accept=True, note="复核后撤销")
            payload = penalty_service.violation_payload(overridden)
            self.assertEqual((payload["status"], payload["appeal_status"], payload["appeal_result"], payload["appeal_decided_by"]), ("revoked", "accepted", "revoked", "admin"))
            await db.refresh(case)
            await db.refresh(review)
            self.assertEqual((case.status, review.outcome, review.decided_by), ("dismissed", "revoke", "admin"))
            self.assertIsNotNone(case.overturned_at)

    async def test_three_different_votes_reduce_and_the_admin_can_restore(self) -> None:
        async with self.sessions() as db:
            await self._seniors(db)
            await self._switch(db, appeals=True)
            case, violation = await self._jury_violation(db)
            original_end = penalty_service.aware(violation.penalty_ends_at)
            review = await self._appeal(db, violation)
            self.assertFalse((await self._review_vote(db, "juror4", review.id, "reduce"))["settled"])
            self.assertFalse((await self._review_vote(db, "juror5", review.id, "revoke"))["settled"])
            # 三票各不相同：取「减轻」
            third = await self._review_vote(db, "juror6", review.id, "uphold")
            self.assertEqual((third["settled"], third["outcome"]), (True, "reduce"))
            await db.refresh(violation)
            offender = await self._user(db, "offender")
            await db.refresh(offender)
            # 记录和记分保留，这条处罚带来的禁言解除
            self.assertEqual((violation.status, violation.points, violation.penalty_kind, violation.penalty_ends_at), ("active", 3, "warning", None))
            self.assertEqual((violation.appeal_status, violation.appeal_result, violation.appeal_decided_by, violation.appeal_stage), ("accepted", "reduced", "senior", None))
            self.assertIsNone(offender.muted_until)
            self.assertEqual(await penalty_service.active_points(db, offender.id), 7)
            await db.refresh(case)
            self.assertEqual((case.status, case.overturned_at), ("resolved", None))
            self.assertIn("申诉后处罚已减轻", penalty_service.describe_violation(violation)[1])
            # 列表里给的仍是被申诉的那个处罚
            items, _total = await review_service.my_reviews(db, self.ids["juror6"])
            self.assertEqual((items[0]["penalty"], items[0]["my_vote"], items[0]["outcome"]), ({"kind": "mute", "hours": 24}, "uphold", "reduce"))

            # 管理员不同意减轻：驳回申诉，原来的禁言恢复
            overridden = await penalty_service.review_appeal(db, violation.id, reviewer_id=self.ids["admin"], accept=False, note="不同意减轻")
            self.assertEqual((overridden.status, overridden.appeal_status, overridden.appeal_result, overridden.appeal_decided_by, overridden.penalty_kind), ("active", "rejected", "upheld", "admin", "mute"))
            self.assertEqual(penalty_service.aware(overridden.penalty_ends_at), original_end)
            await db.refresh(offender)
            self.assertEqual(penalty_service.aware(offender.muted_until), original_end)
            await db.refresh(review)
            self.assertEqual((review.status, review.outcome, review.decided_by), ("closed", "uphold", "admin"))
            details = json.loads((await db.execute(select(AdminAuditLog.details).where(AdminAuditLog.action == "violation.appeal.reviewed").order_by(AdminAuditLog.id.desc()).limit(1))).scalar_one())
            self.assertEqual(details["senior_review"], {"review_id": review.id, "result": "reduced"})

    async def test_revoke_overturns_the_case_and_restores_the_content(self) -> None:
        async with self.sessions() as db:
            await self._seniors(db)
            await self._switch(db, appeals=True)
            case, violation = await self._jury_violation(db)
            message = await db.get(Message, self.messages["insult"])
            self.assertIsNotNone(message.recalled_at)
            review = await self._appeal(db, violation)
            await self._review_vote(db, "juror4", review.id, "revoke")
            with self._events() as events:
                second = await self._review_vote(db, "juror5", review.id, "revoke")
            self.assertEqual((second["settled"], second["outcome"]), (True, "revoke"))
            for item in (violation, case, message):
                await db.refresh(item)
            offender = await self._user(db, "offender")
            await db.refresh(offender)
            self.assertEqual((violation.status, violation.appeal_status, violation.appeal_result, violation.appeal_decided_by), ("revoked", "accepted", "revoked", "senior"))
            self.assertIsNone(violation.revoked_by_id)
            self.assertIsNone(offender.muted_until)
            # 来自案件的：案件按改判处理，内容恢复，原审的一致率跟着变
            self.assertEqual((case.status, case.overturn_note), ("dismissed", "申诉通过，处罚已撤销"))
            self.assertIsNotNone(case.overturned_at)
            self.assertIsNone(message.recalled_at)
            agreed = (await db.execute(select(JuryVote.agreed).where(JuryVote.case_id == case.id, JuryVote.juror_id == self.ids["juror1"]))).scalar_one()
            self.assertFalse(agreed)
            self.assertEqual(self._per_user(events, "moderation.violation.updated")[self.ids["offender"]]["action"], "appeal_accepted")
            self.assertEqual({data["outcome"] for data in self._per_user(events, "jury.case.settled").values()}, {"clean"})
            self.assertEqual(set(self._per_user(events, "report.resolved")), {self.ids["alice"]})
            # 已经撤销的处罚，管理员不能再「驳回申诉」把它变回来
            self.assertEqual(await self._code(penalty_service.review_appeal(db, violation.id, reviewer_id=self.ids["admin"], accept=False, note=None)), (409, "appeal_already_revoked"))

    async def test_machine_penalty_review_shows_only_the_punished_content(self) -> None:
        async with self.sessions() as db:
            await self._seniors(db)
            await self._switch(db, appeals=True)
            violation = await self._machine_violation(db)
            offender = await self._user(db, "offender")
            offender.muted_until = violation.penalty_ends_at
            await db.commit()
            review = await self._appeal(db, violation)
            view = await review_service.next_review(db, await self._user(db, "juror4"))
            AppealReviewView(**view)
            self.assertEqual((view["source"], view["kind"], view["messages"], view["post"], view["origin"]), ("machine", "message", [], None, None))
            self.assertEqual((view["content"], view["penalty"]), ({"text": "大厅里的辱骂", "image_count": 0}, {"kind": "mute", "hours": 24}))
            await review_service.cast_vote(db, await self._user(db, "juror4"), review.id, "reduce")
            result = await self._review_vote(db, "juror5", review.id, "reduce")
            self.assertEqual(result["outcome"], "reduce")
            await db.refresh(violation)
            await db.refresh(offender)
            self.assertEqual((violation.status, violation.penalty_kind, violation.appeal_result, offender.muted_until), ("active", "warning", "reduced", None))
            # 图片只给数量
            image = Message(conversation_id=self.plaza_id, sender_id=self.ids["offender"], content="看图", metadata_json=json.dumps({"images": ["/uploads/a.png", "/uploads/b.png"]}))
            db.add(image)
            await db.flush()
            pictured = await self._machine_violation(db, target_id=image.id, moderation_record_id=None, summary="看图")
            self.assertEqual(await review_service._machine_content(db, pictured), {"text": "看图", "image_count": 2})

    async def test_timeout_admin_override_and_direct_revocation(self) -> None:
        async with self.sessions() as db:
            await self._seniors(db)
            await self._switch(db, appeals=True)
            admin_id = self.ids["admin"]
            juror4, juror5 = await self._user(db, "juror4"), await self._user(db, "juror5")

            # 复审进行中，管理员直接处理：复审就此关闭，没投的人不能再投
            ongoing = await self._machine_violation(db)
            review = await self._appeal(db, ongoing)
            await self._review_vote(db, "juror4", review.id, "revoke")
            await review_service.next_review(db, juror5)
            decided = await penalty_service.review_appeal(db, ongoing.id, reviewer_id=admin_id, accept=False, note="管理员直接处理")
            self.assertEqual((decided.status, decided.appeal_status, decided.appeal_result, decided.appeal_decided_by, decided.appeal_stage), ("active", "rejected", "upheld", "admin", None))
            await db.refresh(review)
            self.assertEqual((review.status, review.outcome, review.decided_by), ("closed", "uphold", "admin"))
            self.assertEqual(await self._code(review_service.cast_vote(db, juror5, review.id, "revoke")), (409, "review_closed"))
            self.assertIsNone(await review_service.next_review(db, juror5))

            # 72 小时没出结果：转管理员
            slow = await self._machine_violation(db)
            review = await self._appeal(db, slow)
            await self._review_vote(db, "juror4", review.id, "uphold")
            review.expires_at = datetime.now(UTC) - timedelta(minutes=1)
            await db.commit()
            with self._events() as events:
                self.assertEqual(await case_service.sweep_senior_queues(db), {"panel": 0, "reviews": 1})
            await db.refresh(review)
            await db.refresh(slow)
            self.assertEqual((review.status, review.stage, review.escalation, review.outcome), ("open", "admin", "timeout", None))
            self.assertEqual((slow.appeal_status, penalty_service.violation_payload(slow)["appeal_stage"]), ("pending", "admin"))
            self.assertEqual(self._per_user(events, "moderation.violation.updated")[slow.user_id]["action"], "appeal_escalated")
            self.assertIsNone(await review_service.next_review(db, juror5))
            self.assertEqual(await self._code(review_service.cast_vote(db, juror5, review.id, "uphold")), (404, "review_not_found"))
            items, total = await review_service.admin_list(db, status_filter="open")
            self.assertEqual((total, items[0]["stage"], items[0]["escalation"], [vote["vote"] for vote in items[0]["votes"]]), (1, "admin", "timeout", ["uphold"]))
            # 之后由管理员处理
            accepted = await penalty_service.review_appeal(db, slow.id, reviewer_id=admin_id, accept=True, note="超时后由管理员处理")
            await db.refresh(review)
            self.assertEqual((accepted.status, accepted.appeal_result, review.status, review.outcome, review.decided_by), ("revoked", "revoked", "closed", "revoke", "admin"))

            # 复审进行中处罚被直接撤销：复审没有可审的了
            gone = await self._machine_violation(db)
            review = await self._appeal(db, gone)
            await penalty_service.revoke_violation(db, gone.id, actor_id=admin_id, reason="误伤")
            await db.commit()
            await db.refresh(review)
            self.assertEqual((review.status, review.outcome, gone.appeal_stage), ("closed", None, "admin"))
            self.assertIsNone(await review_service.next_review(db, juror4))

            # 资深委员中途不够了：不用等到超时
            short = await self._machine_violation(db)
            review = await self._appeal(db, short)
            (await self._member(db, "juror6")).tier = "regular"
            await db.commit()
            self.assertEqual((await case_service.sweep_senior_queues(db))["reviews"], 1)
            await db.refresh(review)
            await db.refresh(short)
            self.assertEqual((review.stage, review.escalation, short.appeal_stage), ("admin", "unavailable", "admin"))


# ---------------------------------------------------------------- 机器拿不准的内容作为案源


class MachineCaseTests(Phase3TestCase):
    async def test_switch_off_creates_nothing(self) -> None:
        await self._moderate(self.messages["insult"])
        async with self.sessions() as db:
            self.assertEqual(await self._machine_cases(db), [])
            self.assertEqual((await db.execute(select(func.count(ReportCase.id)))).scalar_one(), 0)
            # 审核本身照常：留了一条「拿不准」的记录，没有动消息
            record = (await db.execute(select(ContentModerationRecord))).scalar_one()
            self.assertEqual((record.decision, record.action_taken.value), ("review", "none"))
            self.assertIsNone((await db.get(Message, self.messages["insult"])).recalled_at)

    async def test_uncertain_group_text_becomes_a_case_without_reporters(self) -> None:
        async with self.sessions() as db:
            await self._switch(db, machine_review=True)
        with self._events() as events:
            await self._moderate(self.messages["insult"])
        async with self.sessions() as db:
            (case,) = await self._machine_cases(db)
            self.assertEqual(
                (case.target_key, case.target_type, case.message_id, case.conversation_id, case.reported_user_id, case.first_report_id),
                (f"message:{self.messages['insult']}", "message", self.messages["insult"], self.small_id, self.ids["offender"], None),
            )
            self.assertEqual((case.status, case.queue, case.jury_eligible, case.report_count, case.machine_decision, case.machine_category), ("open", "jury", True, 0, "review", "harassment"))
            self.assertIsNotNone(case.queued_at)
            self.assertLess(case.priority, 0)
            self.assertIn("jury.queue.changed", [event for event, _users, _data in events])
            # 案件自己带快照：被判的那条和前后的上下文
            snapshot = await case_service.case_snapshot(db, case)
            self.assertEqual([(item["content"], item["is_target"]) for item in snapshot["messages"]][:3], [("大家好", False), ("你们都是废物", True), ("别这样说话", False)])
            self.assertEqual(len(snapshot["messages"]), 6)
            self.assertEqual((await case_service.case_snapshots(db, [case]))[case.id]["message"]["content"], "你们都是废物")

            juror = await self._user(db, "juror1")
            view = await jury_service.next_case(db, juror)
            JuryCaseView(**view)
            self.assertEqual(await self._real(db, view["case_id"]), case.id)
            self.assertEqual((view["case_origin"], view["report_count"], view["reasons"], view["suggested_category"]), ("machine", 0, [{"reason": "机器筛查 · 辱骂骚扰", "count": 1}], "harassment"))
            self.assertEqual([item["offset"] for item in view["messages"]], [-1, 0, 1, 2, 3, 4])
            self.assertNotIn("sender_id", str(view))
            # 案发群的成员照常回避
            self.assertIsNone(await jury_service.next_case(db, await self._user(db, "insider")))
            read = CaseRead(**(await case_service.build_case_reads(db, [case]))[0])
            self.assertEqual((read.case_origin, read.report_count, read.summary, read.primary_reason), ("machine", 0, "你们都是废物", "机器筛查 · 辱骂骚扰"))

            # 认定违规后和举报案件一样：撤回内容、按阶梯处罚、进公示栏（群聊消息不带内容）
            with self._events() as events:
                case = await self._settle(db, case.id)
            self.assertEqual((case.status, case.verdict_source, case.verdict_category, case.verdict_level, case.jury_outcome, case.publishable), ("resolved", "jury", "harassment", 2, "consensus", True))
            self.assertIsNotNone((await db.get(Message, self.messages["insult"])).recalled_at)
            violation = await db.get(UserViolation, case.violation_id)
            self.assertEqual((violation.source, violation.category, violation.level, violation.case_id, violation.report_id, violation.summary), ("jury", "harassment", 2, case.id, None, "你们都是废物"))
            self.assertEqual(self._per_user(events, "report.resolved"), {})
            await self._age(db, case.id, case_service.PUBLISH_DELAY_HOURS + 1)
            items, total = await overview_service.public_cases(db)
            PublicCase(**items[0])
            self.assertEqual((total, items[0]["case_id"], items[0]["case_origin"], items[0]["report_count"], items[0]["reasons"]), (1, case.id, "machine", 0, [{"reason": "机器筛查 · 辱骂骚扰", "count": 1}]))
            public = PublicCaseDetail(**await overview_service.public_case(db, case.id))
            self.assertEqual((public.content, public.content_hidden_reason, public.case_origin), (None, "group_message", "machine"))
            # 同一条内容不会再建第二个案件
            await self._moderate(self.messages["insult"])
            self.assertEqual(len(await self._machine_cases(db)), 1)

    async def test_exclusions(self) -> None:
        async with self.sessions() as db:
            await self._switch(db, machine_review=True)
            offender = self.ids["offender"]
            direct = Conversation(type=ConversationType.DIRECT)
            temporary = Conversation(type=ConversationType.GROUP, title="临时会话", is_temporary=True)
            db.add_all([direct, temporary])
            await db.flush()
            extra = {
                "direct": Message(conversation_id=direct.id, sender_id=offender, content="私聊里的话"),
                "temporary": Message(conversation_id=temporary.id, sender_id=offender, content="临时会话里的话"),
                "pictured": Message(conversation_id=self.small_id, sender_id=offender, content="带图的话", metadata_json=json.dumps({"images": ["/uploads/messages/a.png"]})),
            }
            db.add_all(extra.values())
            await db.commit()
            ids = {name: message.id for name, message in extra.items()}

        async def allow_image(*_args, **_kwargs):
            return {"decision": "allow", "severity": 0, "categories": []}

        # 私聊、临时会话、带图的消息
        await self._moderate(ids["direct"])
        await self._moderate(ids["temporary"])
        with patch.object(moderation_service, "_call_image_moderation", allow_image):
            await self._moderate(ids["pictured"])
        # 色情类（新旧两种类别写法）
        await self._moderate(self.messages["insult"], {**UNCERTAIN, "categories": ["sexual"], "category": "vulgar"})
        await self._moderate(self.messages["insult2"], {**UNCERTAIN, "categories": ["sexual"], "category": None})
        # 机器放行的、机器明确判违规的（已经采取了动作）
        await self._moderate(self.messages["spam"], {"decision": "allow", "severity": 0, "categories": []})
        await self._moderate(self.messages["insult3"], {**UNCERTAIN, "decision": "block", "severity": 2, "level": 2})
        # 配置成「拿不准的文本先撤回」时内容已经被处理了
        with patch.object(settings, "content_moderation_text_review_recall", True):
            await self._moderate(self.messages["plaza_hello"])
        # 举报触发的机器复审本来就有案件
        await self._moderate(self.messages["club_insult"], source=ModerationSource.REPORT_REVIEW)
        async with self.sessions() as db:
            self.assertEqual(await self._machine_cases(db), [])
            self.assertEqual((await db.execute(select(func.count(ReportCase.id)))).scalar_one(), 0)
            self.assertIsNotNone((await db.get(Message, self.messages["insult3"])).recalled_at)
            # 这条内容已经有举报案件：不重复建
            reported = await self._report(db, "alice", "greeting")
        await self._moderate(self.messages["greeting"])
        async with self.sessions() as db:
            self.assertEqual(await self._machine_cases(db), [])
            self.assertEqual((await db.get(ReportCase, reported.case_id)).case_origin, "report")
        # 都不是上面这些情况的：建
        await self._moderate(self.messages["reply"])
        async with self.sessions() as db:
            self.assertEqual([case.message_id for case in await self._machine_cases(db)], [self.messages["reply"]])

    async def test_daily_cap_scales_with_the_number_of_jurors(self) -> None:
        async with self.sessions() as db:
            # 空闲复查关着：名额全给拿不准的（复查开着时的分法见 test_moderation_idle）
            await self._switch(db, machine_review=True, machine_idle_scan=False)
            now = datetime.now(UTC)
            # 9 位在任委员 × 6 = 54；至少 15
            self.assertEqual(await machine_service.daily_cap(db, now), 54)
            with patch.object(machine_service, "DAILY_PER_JUROR", 0.2):
                self.assertEqual(await machine_service.daily_cap(db, now), 15)
        with patch.object(machine_service, "DAILY_MIN", 2), patch.object(machine_service, "DAILY_PER_JUROR", 0):
            for name in ("insult", "insult2", "insult3"):
                await self._moderate(self.messages[name])
            async with self.sessions() as db:
                cases = await self._machine_cases(db)
                # 到上限后不再收
                self.assertEqual([case.message_id for case in cases], [self.messages["insult"], self.messages["insult2"]])
                # 按最近 24 小时滚动：早先的不占今天的名额
                cases[0].created_at = datetime.now(UTC) - timedelta(hours=25)
                await db.commit()
            await self._moderate(self.messages["insult3"])
            async with self.sessions() as db:
                self.assertEqual(len(await self._machine_cases(db)), 3)

    async def test_ranked_after_real_reports_and_merges_later_reports(self) -> None:
        async with self.sessions() as db:
            await self._switch(db, machine_review=True)
        await self._moderate(self.messages["insult"])
        async with self.sessions() as db:
            (machine,) = await self._machine_cases(db)
            # 后建的真实举报排在它前面，哪怕举报的优先级已经是最低的那一档
            reported = await db.get(ReportCase, await self._jury_case(db, "insult2"))
            reported.priority = 0.01
            await db.commit()
            juror = await self._user(db, "juror1")
            self.assertEqual(await self._real(db, (await jury_service.next_case(db, juror))["case_id"]), reported.id)
            self.assertEqual(await jury_service.available_cases(db, juror), 2)
            await jury_service.cast_vote(db, juror, await self._rid(db, juror, reported.id), "l2", None)
            self.assertEqual(await self._real(db, (await jury_service.next_case(db, juror))["case_id"]), machine.id)
            await jury_service.cast_vote(db, juror, await self._rid(db, juror, machine.id), "l2", None)

            # 之后有人举报同一条内容：并进这个案件，照常计数和排队
            report = await self._report(db, "alice", "insult")
            self.assertEqual(report.case_id, machine.id)
            self.assertIsNone(getattr(report, "_new_case_id", None))
            await db.refresh(machine)
            self.assertEqual((machine.case_origin, machine.report_count, machine.primary_reason, machine.queue, machine.first_report_id), ("machine", 1, "辱骂骚扰", "jury", None))
            self.assertGreater(machine.priority, 0)
            view = await jury_service.next_case(db, await self._user(db, "juror2"))
            self.assertEqual((await self._real(db, view["case_id"]), view["case_origin"], view["report_count"], view["reasons"]), (machine.id, "machine", 1, [{"reason": "辱骂骚扰", "count": 1}]))
            # 快照仍是案件自己的那份
            self.assertEqual(len(view["messages"]), 6)
            # 有了举报人之后和普通案件一样：结论会通知举报人
            with self._events() as events:
                await jury_service.cast_vote(db, await self._user(db, "juror2"), view["case_id"], "l2", None)
                await self._cast(db, "juror3", machine.id, "l2")
            await db.refresh(machine)
            self.assertEqual(machine.status, "resolved")
            self.assertEqual(set(self._per_user(events, "report.resolved")), {self.ids["alice"]})

    async def test_unsettled_machine_cases_never_reach_the_admin(self) -> None:
        async with self.sessions() as db:
            await self._switch(db, machine_review=True)
        for name in ("insult", "insult2", "insult3", "spam"):
            await self._moderate(self.messages[name])
        async with self.sessions() as db:
            timed_out, split, paneled, reported = await self._machine_cases(db)
            # 超时、没有资深委员：以「未作处理」结案，不转管理员
            timed_out.queued_at = datetime.now(UTC) - timedelta(hours=73)
            await db.commit()
            await self._cast(db, "juror1", timed_out.id, "l2")
            # juror2 分到手了还没投
            db.add(JuryVote(case_id=timed_out.id, juror_id=self.ids["juror2"], assigned_at=datetime.now(UTC)))
            await db.commit()
            with self._events() as events:
                self.assertEqual((await case_service.sweep_jury_queue(db))["timeout"], 1)
            await db.refresh(timed_out)
            self.assertEqual(
                (timed_out.status, timed_out.queue, timed_out.verdict_source, timed_out.jury_outcome, timed_out.panel_outcome, timed_out.publishable, timed_out.violation_id),
                ("dismissed", "admin", "auto", "timeout", "unavailable", False, None),
            )
            self.assertIsNotNone(timed_out.closed_at)
            # 不记处罚、不动内容、不通知当事人；投过票的委员不计一致率
            self.assertEqual((await db.execute(select(func.count(UserViolation.id)))).scalar_one(), 0)
            self.assertIsNone((await db.get(Message, self.messages["insult"])).recalled_at)
            self.assertEqual([event for event, _users, _data in events if event in {"report.resolved", "moderation.penalty", "moderation.violation.updated"}], [])
            vote = (await db.execute(select(JuryVote).where(JuryVote.case_id == timed_out.id, JuryVote.juror_id == self.ids["juror1"]))).scalar_one()
            self.assertIsNone(vote.agreed)
            self.assertEqual((await self._member(db, "juror1")).votes_settled, 0)
            # 结案之后才把手里那件投出去的也一样：票照常记，但没有结论可比
            late = await jury_service.cast_vote(db, await self._user(db, "juror2"), await self._rid(db, "juror2", timed_out.id), "none", None)
            self.assertEqual((late["settled"], late["case"]["stage"], late["case"]["my_vote"]["agreed"]), (False, "closed", None))
            self.assertEqual((await self._member(db, "juror2")).votes_settled, 0)
            detail = await jury_service.case_detail(db, self.ids["juror1"], vote.id)
            self.assertEqual((detail["stage"], detail["verdict"]["source"], detail["verdict"]["outcome"], detail["my_vote"]["agreed"]), ("closed", "auto", "clean", None))
            # 不进公示栏
            await self._age(db, timed_out.id, case_service.PUBLISH_DELAY_HOURS + 1)
            self.assertEqual((await overview_service.public_cases(db))[1], 0)
            # 「未作处理」不是「认定不违规」：之后有人举报照常受理
            report = await self._report(db, "alice", "insult")
            self.assertNotEqual(report.case_id, timed_out.id)

            # 无共识同理
            split = await self._split(db, split.id)
            self.assertEqual((split.status, split.verdict_source, split.jury_outcome, split.queue), ("dismissed", "auto", "no_consensus", "admin"))
            self.assertEqual((await case_service.list_cases(db, queue="admin"))[1], 0)

            # 凑得齐合议庭就进合议；合议超时也不转管理员
            await self._seniors(db)
            paneled = await self._split(db, paneled.id)
            self.assertEqual((paneled.status, paneled.queue), ("open", "panel"))
            view = await panel_service.next_case(db, await self._user(db, "juror4"))
            self.assertEqual((view["case_origin"], view["reasons"]), ("machine", [{"reason": "机器筛查 · 辱骂骚扰", "count": 1}]))
            paneled.panel_at = datetime.now(UTC) - timedelta(hours=49)
            await db.commit()
            self.assertEqual((await case_service.sweep_senior_queues(db))["panel"], 1)
            await db.refresh(paneled)
            self.assertEqual((paneled.status, paneled.verdict_source, paneled.panel_outcome), ("dismissed", "auto", "timeout"))

            # 已经有人举报的机器案件有举报人在等结果：和普通案件一样转管理员
            (await self._member(db, "juror6")).tier = "regular"
            await db.commit()
            await self._report(db, "alice", "spam", "垃圾广告")
            reported = await self._split(db, reported.id)
            self.assertEqual((reported.status, reported.queue, reported.panel_outcome), ("open", "admin", "unavailable"))

    async def test_failures_never_touch_the_moderation_flow(self) -> None:
        async with self.sessions() as db:
            await self._switch(db, machine_review=True)

        async def broken(*_args, **_kwargs):
            raise RuntimeError("boom")

        with patch.object(machine_service, "_open_case", broken), self.assertLogs(moderation_service.logger, level="ERROR"):
            await self._moderate(self.messages["insult"])
        async with self.sessions() as db:
            # 审核记录照常落库，没有被当成一次审核失败
            records = (await db.execute(select(ContentModerationRecord))).scalars().all()
            self.assertEqual([(record.decision, record.action_taken.value) for record in records], [("review", "none")])
            self.assertEqual(await self._machine_cases(db), [])
        # 之后恢复正常
        await self._moderate(self.messages["insult2"])
        async with self.sessions() as db:
            self.assertEqual(len(await self._machine_cases(db)), 1)

    async def test_uncertain_public_post_text_becomes_a_case(self) -> None:
        async with self.sessions() as db:
            await self._switch(db, machine_review=True)
            private = Post(author_id=self.ids["offender"], content="仅好友可见的动态", visibility=PostVisibility.FRIENDS, moderation_status="pending")
            db.add(private)
            await db.commit()
            private_id = private.id

        async def text_verdict(*_args, **_kwargs):
            return dict(UNCERTAIN)

        async def image_verdict(*_args, **_kwargs):
            return {"decision": "allow", "severity": 0, "categories": []}

        async def image_uncertain(*_args, **_kwargs):
            return {"decision": "review", "severity": 1, "categories": []}

        with patch.object(moderation_service, "AsyncSessionLocal", self.sessions), patch.object(moderation_service, "_call_text_moderation_for_user", text_verdict):
            # 图片也拿不准的不进（图片不给委员判）；仅好友可见的不进
            with patch.object(moderation_service, "_call_image_moderation_for_user", image_uncertain):
                await moderation_service._moderate_post_task(self.post_id)
            with patch.object(moderation_service, "_call_image_moderation_for_user", image_verdict):
                await moderation_service._moderate_post_task(private_id)
                async with self.sessions() as db:
                    self.assertEqual(await self._machine_cases(db), [])
                await moderation_service._moderate_post_task(self.post_id)
        async with self.sessions() as db:
            (case,) = await self._machine_cases(db)
            self.assertEqual((case.target_key, case.target_type, case.reported_user_id, case.queue, case.content_publishable, case.conversation_id), (f"post:{self.post_id}", "post", self.ids["offender"], "jury", True, None))
            view = await jury_service.next_case(db, await self._user(db, "juror1"))
            self.assertEqual((view["kind"], view["case_origin"], view["post"]["content"], len(view["post"]["image_urls"])), ("post", "machine", "低价卖号，加微信", 2))
            self.assertEqual((await db.get(Post, self.post_id)).moderation_status, "approved")


# ---------------------------------------------------------------- 重复举报与举报人复议


class RepeatReportTests(Phase3TestCase):
    async def test_content_cleared_within_30_days_cannot_be_reported_again(self) -> None:
        async with self.sessions() as db:
            cleared = await self._settle(db, await self._jury_case(db, "insult"), "none")
            self.assertEqual(cleared.status, "dismissed")
            for reporter in ("alice", "bob"):
                with self.assertRaises(HTTPException) as caught:
                    await self._report(db, reporter, "insult")
                self.assertEqual((caught.exception.status_code, caught.exception.detail["code"], caught.exception.detail["case_id"]), (409, "already_reviewed", cleared.id))
            self.assertEqual((await db.execute(select(func.count(ReportCase.id)))).scalar_one(), 1)
            # 别的消息不受影响；动态同理
            self.assertNotEqual((await self._report(db, "bob", "insult2")).case_id, cleared.id)
            post_case = await self._settle(db, await self._post_case(db), "none")
            with self.assertRaises(HTTPException) as caught:
                await report_service.create_report(db, self.ids["bob"], ReportCreate(target_type=ReportTargetType.POST, target_id=self.post_id, reason="垃圾广告"))
            self.assertEqual((caught.exception.status_code, caught.exception.detail["code"], caught.exception.detail["case_id"]), (409, "already_reviewed", post_case.id))
            # 管理员认定不违规的同样算
            admin_case_id = (await self._report(db, "alice", "spam", "垃圾广告")).case_id
            await case_service.resolve_case(db, admin_case_id, source="admin", actor_id=self.ids["admin"], upheld=False)
            self.assertEqual(await self._code(self._report(db, "bob", "spam", "垃圾广告")), (409, "already_reviewed"))

            # 过了 30 天可以再举报，开一个新案件
            cleared.closed_at = datetime.now(UTC) - timedelta(days=31)
            await db.commit()
            again = await self._report(db, "bob", "insult")
            self.assertNotEqual(again.case_id, cleared.id)

    async def test_overturned_dismissals_do_not_block(self) -> None:
        async with self.sessions() as db:
            # 先认定违规，再被改判成不违规：不是「评审认定不违规」，内容恢复后可以再举报
            case = await self._settle(db, await self._jury_case(db, "insult"))
            await case_service.overturn_case(db, case.id, actor_id=self.ids["admin"], upheld=False, note="误判")
            await db.refresh(case)
            self.assertEqual(case.status, "dismissed")
            self.assertIsNone(await case_service.recently_cleared_case(db, case.target_key))
            self.assertNotEqual((await self._report(db, "bob", "insult")).case_id, case.id)
            # 举报用户 / 会话不在这条规则里
            self.assertIsNone(await case_service.recently_cleared_case(db, f"user:{self.ids['offender']}"))


class ReconsiderTests(Phase3TestCase):
    TEXT = "对方连续三条都在骂人，不是玩笑"

    async def _dismissed(self, db, message: str = "insult") -> tuple[ReportCase, UserReport]:
        report = await self._report(db, "alice", message)
        await self._queue(db, report.case_id)
        return await self._settle(db, report.case_id, "none"), report

    async def _fields(self, db, report: UserReport, viewer: str) -> tuple[bool, str]:
        read = await report_service.build_report_read(db, await report_service.get_report(db, report.id), self.ids[viewer])
        return read.can_reconsider, read.reconsider_status

    async def test_overturn_by_the_senior_review(self) -> None:
        async with self.sessions() as db:
            await self._seniors(db)
            alice, bob = self.ids["alice"], self.ids["bob"]
            alice_report = await self._report(db, "alice", "insult")
            bob_report = await self._report(db, "bob", "insult")
            await self._queue(db, alice_report.case_id)
            case = await self._settle(db, alice_report.case_id, "none")
            self.assertEqual(await self._fields(db, alice_report, "alice"), (True, "none"))
            # 入口只给举报人自己
            self.assertEqual(await self._fields(db, alice_report, "admin"), (False, "none"))
            self.assertEqual(await self._code(review_service.request_reconsider(db, bob, alice_report.id, self.TEXT)), (404, "report_not_found"))
            self.assertEqual(await self._code(review_service.request_reconsider(db, alice, alice_report.id, "不服")), (422, "text_required"))
            case_service._last_queue_event_at = None
            with self._events() as events:
                await review_service.request_reconsider(db, alice, alice_report.id, self.TEXT)
            self.assertIn("jury.queue.changed", [event for event, _users, _data in events])
            self.assertEqual(await self._fields(db, alice_report, "alice"), (False, "pending"))
            # 一个案件只能复议一次，不管有几个举报人
            self.assertEqual(await self._fields(db, bob_report, "bob"), (False, "pending"))
            self.assertEqual(await self._code(review_service.request_reconsider(db, bob, bob_report.id, self.TEXT)), (409, "reconsider_unavailable"))
            self.assertEqual(CaseRead(**(await case_service.build_case_reads(db, [case]))[0]).reconsider_status, "pending")
            review = (await db.execute(select(JuryReview))).scalar_one()
            self.assertEqual((review.kind, review.case_id, review.report_id, review.requester_id, review.stage, review.text), ("reconsider", case.id, alice_report.id, alice, "senior", self.TEXT))

            juror4 = await self._user(db, "juror4")
            self.assertEqual((await jury_service.my_status(db, juror4))["senior"], {"panel": 0, "appeals": 1})
            view = await review_service.next_review(db, juror4)
            AppealReviewView(**view)
            self.assertEqual((view["type"], view["kind"], view["source"], view["reconsider_text"]), ("reconsider", "message", "jury", self.TEXT))
            self.assertEqual((view["verdict"]["outcome"], view["verdict"]["source"], view["verdict"]["closed_at"]), ("clean", "jury", penalty_service.aware(case.closed_at)))
            self.assertEqual((view["category"], view["category_label"], view["level"], view["penalty"], view["content"], view["appeal_text"], view["appealed_at"]), (None,) * 7)
            self.assertIsNotNone(view["requested_at"])
            self.assertEqual((view["origin"]["tally"]["none"], len(view["messages"])), (3, 6))
            self.assertNotIn("alice", str(view))
            # 复议只有两个选项；类别要在可选范围里
            self.assertEqual(await self._code(review_service.cast_vote(db, juror4, review.id, "revoke")), (422, "invalid_vote"))
            self.assertEqual(await self._code(review_service.cast_vote(db, juror4, review.id, "overturn", None, 2, "sexual")), (422, "invalid_category"))
            first = await review_service.cast_vote(db, juror4, review.id, "overturn", "明显是辱骂", 3, "harassment")
            self.assertEqual((first["settled"], first["reward"]["points"]), (False, 8))
            with self._events() as events:
                second = await self._review_vote(db, "juror5", review.id, "overturn", level=1, category="harassment")
            self.assertEqual((second["settled"], second["outcome"]), (True, "overturn"))

            await db.refresh(case)
            # 按改判处理：等级取 overturn 票的较低中位数；原结论的来源不变
            self.assertEqual((case.status, case.verdict_source, case.verdict_category, case.verdict_level, case.overturn_note), ("resolved", "jury", "harassment", 1, "举报人申请复议，资深委员复审后改判为违规"))
            self.assertIsNotNone(case.overturned_at)
            self.assertIsNone(case.overturned_by_id)
            violation = await db.get(UserViolation, case.violation_id)
            self.assertEqual((violation.source, violation.level, violation.actor_id), ("jury", 1, None))
            self.assertIsNotNone((await db.get(Message, self.messages["insult"])).recalled_at)
            for report in (alice_report, bob_report):
                await db.refresh(report)
                self.assertEqual(report.status, ReportStatus.REVIEWED)
            self.assertEqual(set(self._per_user(events, "report.resolved")), {alice, bob})
            self.assertEqual(await self._fields(db, alice_report, "alice"), (False, "overturned"))
            # 原审里投「不违规」的票现在是不一致
            agreed = (await db.execute(select(JuryVote.agreed).where(JuryVote.case_id == case.id, JuryVote.juror_id == self.ids["juror1"]))).scalar_one()
            self.assertFalse(agreed)
            notifications = await notification_service.list_unified_notifications(db, alice)
            self.assertIn("你申请的复议有结果了", [item.title for item in notifications.items if item.type == "jury"])
            items, _total = await review_service.my_reviews(db, juror4.id)
            self.assertEqual((items[0]["type"], items[0]["category_label"], items[0]["level"], items[0]["penalty"], items[0]["my_vote"], items[0]["outcome"]), ("reconsider", None, None, None, "overturn", "overturn"))
            items, _total = await review_service.admin_list(db, kind="reconsider")
            read = AdminReviewRead(**items[0])
            self.assertEqual((read.outcome, read.case_id, read.report_id, read.requester.username, read.text), ("overturn", case.id, alice_report.id, "alice", self.TEXT))
            self.assertEqual([(vote.juror.username, vote.vote, vote.level, vote.category) for vote in read.votes], [("juror4", "overturn", 3, "harassment"), ("juror5", "overturn", 1, "harassment")])

            # 被改判处罚的人再申诉：改判他的那几位不能再审
            await self._switch(db, appeals=True)
            self.assertEqual(await review_service.appeal_block_reason(db, violation), "seniors")

    async def test_uphold_keeps_the_verdict(self) -> None:
        async with self.sessions() as db:
            await self._seniors(db)
            case, report = await self._dismissed(db)
            await review_service.request_reconsider(db, self.ids["alice"], report.id, self.TEXT)
            review = (await db.execute(select(JuryReview))).scalar_one()
            await self._review_vote(db, "juror4", review.id, "overturn")
            await self._review_vote(db, "juror5", review.id, "uphold")
            with self._events() as events:
                third = await self._review_vote(db, "juror6", review.id, "uphold")
            self.assertEqual((third["settled"], third["outcome"]), (True, "uphold"))
            await db.refresh(case)
            self.assertEqual((case.status, case.overturned_at), ("dismissed", None))
            self.assertEqual(await self._fields(db, report, "alice"), (False, "upheld"))
            resolved = self._per_user(events, "report.resolved")
            self.assertEqual((set(resolved), resolved[self.ids["alice"]]["title"], resolved[self.ids["alice"]]["status"]), ({self.ids["alice"]}, "你申请的复议有结果了", "rejected"))
            self.assertEqual(CaseRead(**(await case_service.build_case_reads(db, [case]))[0]).reconsider_status, "upheld")
            # 复议过的案件不能再复议
            self.assertEqual(await self._code(review_service.request_reconsider(db, self.ids["alice"], report.id, self.TEXT)), (409, "reconsider_unavailable"))

    async def test_only_jury_dismissals_within_the_window_can_be_reconsidered(self) -> None:
        async with self.sessions() as db:
            alice = self.ids["alice"]
            # 管理员认定不违规的
            by_admin = await self._report(db, "alice", "insult")
            await case_service.resolve_case(db, by_admin.case_id, source="admin", actor_id=self.ids["admin"], upheld=False)
            # 认定违规的
            upheld = await self._report(db, "alice", "insult2")
            await self._queue(db, upheld.case_id)
            await self._settle(db, upheld.case_id)
            # 还没出结论的
            pending = await self._report(db, "alice", "insult3")
            for report in (by_admin, upheld, pending):
                self.assertEqual(await self._fields(db, report, "alice"), (False, "none"))
                self.assertEqual(await self._code(review_service.request_reconsider(db, alice, report.id, self.TEXT)), (409, "reconsider_unavailable"))
            # 超过 7 天
            case, late = await self._dismissed(db, "spam")
            self.assertEqual(await self._fields(db, late, "alice"), (True, "none"))
            case.closed_at = datetime.now(UTC) - timedelta(days=8)
            await db.commit()
            self.assertEqual(await self._fields(db, late, "alice"), (False, "none"))
            self.assertEqual(await self._code(review_service.request_reconsider(db, alice, late.id, self.TEXT)), (409, "reconsider_unavailable"))

    async def test_without_seniors_or_after_the_deadline_the_admin_decides(self) -> None:
        async with self.sessions() as db:
            alice = self.ids["alice"]
            # 凑不齐三位资深委员：直接转给管理员
            case, report = await self._dismissed(db)
            await review_service.request_reconsider(db, alice, report.id, self.TEXT)
            review = (await db.execute(select(JuryReview))).scalar_one()
            self.assertEqual((review.stage, review.status, review.escalation), ("admin", "open", "unavailable"))
            self.assertEqual(await self._fields(db, report, "alice"), (False, "pending"))
            # 管理员用改判接口处理，复议随之有了结果
            await case_service.overturn_case(db, case.id, actor_id=self.ids["admin"], upheld=True, category="harassment", level=2, note="复核后认定违规")
            await db.refresh(review)
            self.assertEqual((review.status, review.outcome, review.decided_by), ("closed", "overturn", "admin"))
            self.assertEqual(await self._fields(db, report, "alice"), (False, "overturned"))
            violation = await db.get(UserViolation, (await db.get(ReportCase, case.id)).violation_id)
            self.assertEqual(violation.source, "report")

            # 72 小时没结果：转管理员
            await self._seniors(db)
            other, late = await self._dismissed(db, "insult2")
            await review_service.request_reconsider(db, alice, late.id, self.TEXT)
            review = (await db.execute(select(JuryReview).where(JuryReview.case_id == other.id))).scalar_one()
            self.assertEqual(review.stage, "senior")
            await self._review_vote(db, "juror4", review.id, "overturn")
            review.expires_at = datetime.now(UTC) - timedelta(minutes=1)
            await db.commit()
            self.assertEqual((await case_service.sweep_senior_queues(db))["reviews"], 1)
            await db.refresh(review)
            self.assertEqual((review.stage, review.status, review.escalation), ("admin", "open", "timeout"))
            self.assertIsNone(await review_service.next_review(db, await self._user(db, "juror5")))
            self.assertEqual(await self._fields(db, late, "alice"), (False, "pending"))


# ---------------------------------------------------------------- 配置与后台


class ConfigTests(Phase3TestCase):
    async def test_config_and_settings_expose_the_senior_blocks(self) -> None:
        async with self.sessions() as db:
            raw = await overview_service.config(db)
            config = JuryConfig(**raw)
            # 三期里这一半追加的能力；判例和议事厅不在这里
            self.assertEqual(config.capabilities[13:], ["panel", "appeal_review", "machine_cases", "reconsider", "precedents", "hall"])
            self.assertEqual(raw["panel"], {"seats": 3, "consensus_votes": 2, "timeout_hours": 48})
            self.assertEqual(raw["appeals"], {"seats": 3, "consensus_votes": 2, "timeout_hours": 72})
            self.assertEqual(raw["reconsider"], {"window_days": 7, "text_min": 10})
            self.assertEqual((raw["points"]["panel_agreed"], raw["points"]["panel_disagreed"], raw["points"]["review_vote"]), (15, 5, 8))
            self.assertEqual((config.panel.timeout_hours, config.appeals.seats, config.reconsider.window_days), (48, 3, 7))
            self.assertEqual(raw["switches"], {"auto_appoint": False, "appeals": False, "machine_review": False})
            # 复议没做完时两处都不下发
            with patch.object(review_service, "RECONSIDER_ENABLED", False), patch.object(overview_service, "SENIOR_CAPABILITIES", ["panel", "appeal_review", "machine_cases"]):
                trimmed = await overview_service.config(db)
                self.assertNotIn("reconsider", trimmed["capabilities"])
                self.assertIsNone(JuryConfig(**trimmed).reconsider)
            thresholds = jury_settings.thresholds()
            self.assertEqual(
                {key: thresholds[key] for key in ("panel_timeout_hours", "appeal_timeout_hours", "points_panel_agreed", "points_panel_disagreed", "points_review_vote")},
                {"panel_timeout_hours": 48, "appeal_timeout_hours": 72, "points_panel_agreed": 15, "points_panel_disagreed": 5, "points_review_vote": 8},
            )
            self.assertEqual(
                (thresholds["machine_daily_per_juror"], thresholds["machine_daily_min"], thresholds["cleared_block_days"], thresholds["reconsider_window_days"], thresholds["reconsider_text_min"]),
                (6, 15, 30, 7, 10),
            )


class EndpointTests(Phase3TestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.acting = "juror4"
        api = FastAPI()
        api.include_router(jury_api.router, prefix="/jury")
        api.include_router(senior_api.router, prefix="/jury")
        api.include_router(admin_api.router, prefix="/admin")
        api.include_router(reports_api.router, prefix="/reports")

        async def isolated_db():
            async with self.sessions() as session:
                yield session

        async def acting_user(db: Annotated[AsyncSession, Depends(get_db)]) -> User:
            return await db.get(User, self.ids[self.acting])

        api.dependency_overrides[get_db] = isolated_db
        api.dependency_overrides[get_current_user] = acting_user
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://testserver")

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        await super().asyncTearDown()

    async def test_senior_routes(self) -> None:
        async with self.sessions() as db:
            await self._seniors(db)
            await self._switch(db, appeals=True)
            case = await self._split(db, await self._jury_case(db, "insult"))
            violation = await self._machine_violation(db)
            await self._appeal(db, violation)
            dismissed = await self._settle(db, await self._jury_case(db, "club_insult"), "none")
            report_id = (await db.execute(select(UserReport.id).where(UserReport.case_id == dismissed.id))).scalar_one()
        client = self.client
        config = (await client.get("/jury/config")).json()
        self.assertEqual((config["panel"]["seats"], config["appeals"]["timeout_hours"], config["reconsider"]["text_min"]), (3, 72, 10))
        self.assertEqual((await client.get("/jury/me")).json()["senior"], {"panel": 1, "appeals": 1})

        # 资深合议
        panel = (await client.get("/jury/panel/next")).json()["case"]
        self.assertEqual((panel["origin"]["reason"], panel["panel"]["seats"], panel["case_origin"]), ("no_consensus", 3, "report"))
        response = await client.post(f"/jury/panel/{panel['case_id']}/vote", json={"vote": "abstain"})
        self.assertEqual((response.status_code, response.json()["detail"]["code"]), (422, "invalid_vote"))
        voted = (await client.post(f"/jury/panel/{panel['case_id']}/vote", json={"vote": "l2", "category": "harassment", "note": "同意违规"})).json()
        self.assertEqual((voted["settled"], voted["case"]["stage"], voted["case"]["my_vote"]["vote"]), (False, "panel", "l2"))
        self.assertEqual((await client.get(f"/jury/cases/{panel['case_id']}")).json()["stage"], "panel")
        self.assertEqual((await client.get("/jury/votes")).json()["items"][0]["vote_stage"], "panel")
        self.assertIsNone((await client.get("/jury/panel/next")).json()["case"])

        # 申诉复审
        review = (await client.get("/jury/appeals/next")).json()["review"]
        self.assertEqual((review["type"], review["source"], review["content"]["text"], review["appeal_text"]), ("appeal", "machine", "大厅里的辱骂", APPEAL_TEXT))
        response = await client.post(f"/jury/appeals/{review['review_id']}/vote", json={"vote": "keep"})
        self.assertEqual(response.status_code, 422)
        voted = (await client.post(f"/jury/appeals/{review['review_id']}/vote", json={"vote": "uphold", "note": "没有问题"})).json()
        self.assertEqual((voted["review_id"], voted["settled"], voted["outcome"], voted["reward"]["points"]), (review["review_id"], False, None, 8))
        response = await client.post(f"/jury/appeals/{review['review_id']}/vote", json={"vote": "uphold"})
        self.assertEqual((response.status_code, response.json()["detail"]["code"]), (409, "already_voted"))
        mine = (await client.get("/jury/appeals/mine")).json()
        self.assertEqual((mine["total"], mine["items"][0]["my_vote"], mine["items"][0]["outcome"]), (1, "uphold", None))
        self.assertIsNone((await client.get("/jury/appeals/next")).json()["review"])

        # 不是资深委员
        self.acting = "juror1"
        for path in ("/jury/panel/next", "/jury/appeals/next"):
            response = await client.get(path)
            self.assertEqual((response.status_code, response.json()["detail"]["code"]), (403, "not_senior"))
        self.assertEqual((await client.get("/jury/appeals/mine")).json(), {"items": [], "total": 0})
        self.assertIsNone((await client.get("/jury/me")).json()["senior"])

        # 举报人复议
        self.acting = "alice"
        mine = [item for item in (await client.get("/reports/me")).json() if item["id"] == report_id][0]
        self.assertEqual((mine["can_reconsider"], mine["reconsider_status"]), (True, "none"))
        response = await client.post(f"/reports/{report_id}/reconsider", json={"text": "太短"})
        self.assertEqual((response.status_code, response.json()["detail"]["code"]), (422, "text_required"))
        reconsidered = (await client.post(f"/reports/{report_id}/reconsider", json={"text": "社团里那句话是在针对我，请再看一下"})).json()
        self.assertEqual((reconsidered["id"], reconsidered["can_reconsider"], reconsidered["reconsider_status"]), (report_id, False, "pending"))
        response = await client.post(f"/reports/{report_id}/reconsider", json={"text": "社团里那句话是在针对我，请再看一下"})
        self.assertEqual((response.status_code, response.json()["detail"]["code"]), (409, "reconsider_unavailable"))
        # 重复举报
        response = await client.post("/reports", json={"target_type": "message", "target_id": self.messages["club_insult"], "reason": "辱骂骚扰"})
        self.assertEqual((response.status_code, response.json()["detail"]["code"], response.json()["detail"]["case_id"]), (409, "already_reviewed", dismissed.id))

        # 后台
        self.acting = "admin"
        page = (await client.get("/admin/cases", params={"queue": "panel"})).json()
        self.assertEqual((page["total"], page["items"][0]["id"], page["items"][0]["queue"], page["open_counts"]["panel"]), (1, case.id, "panel", 1))
        detail = (await client.get(f"/admin/cases/{case.id}")).json()
        self.assertEqual([(vote["vote"], vote["category"], vote["stage"]) for vote in detail["votes"] if vote["stage"] == "panel"], [("l2", "harassment", "panel")])
        self.assertEqual((await client.get(f"/admin/cases/{dismissed.id}")).json()["reconsider_status"], "pending")
        reviews = (await client.get("/admin/jury/reviews", params={"status": "open"})).json()
        self.assertEqual(reviews["total"], 2)
        appeals = (await client.get("/admin/jury/reviews", params={"kind": "appeal", "status": ""})).json()
        self.assertEqual((appeals["total"], appeals["items"][0]["violation_id"], appeals["items"][0]["votes"][0]["juror"]["username"], appeals["items"][0]["votes"][0]["vote"]), (1, violation.id, "juror4", "uphold"))
        self.assertEqual((await client.get("/admin/jury/reviews", params={"kind": "reconsider"})).json()["items"][0]["case_id"], dismissed.id)
        thresholds = (await client.get("/admin/jury/settings")).json()["thresholds"]
        self.assertEqual((thresholds["panel_timeout_hours"], thresholds["machine_daily_min"]), (48, 15))
        # 管理员直接给合议中的案件下结论
        resolved = (await client.post(f"/admin/cases/{case.id}/resolve", json={"outcome": "dismiss"})).json()
        self.assertEqual((resolved["status"], resolved["queue"], resolved["panel_outcome"]), ("dismissed", "panel", "manual"))
        # 管理员处理还在资深复审里的申诉
        decided = (await client.post(f"/admin/violations/{violation.id}/appeal", json={"accept": True, "note": "直接撤销"})).json()
        self.assertEqual((decided["status"], decided["appeal_result"], decided["appeal_decided_by"], decided["appeal_stage"]), ("revoked", "revoked", "admin", None))


# ---------------------------------------------------------------- 撤回广播


class RecallBroadcastTests(Phase3TestCase):
    """机器撤回、后台撤回、旧的逐条审核接口：给群里推「消息被撤回」都只群发一次，不逐人组装完整消息。"""

    async def _crowd(self, db, title: str, size: int) -> tuple[int, int, list[int]]:
        group = Conversation(type=ConversationType.GROUP, title=title)
        db.add(group)
        members = [User(username=f"{title}{index}", hashed_password="x") for index in range(size)]
        db.add_all(members)
        await db.flush()
        for user in members:
            db.add(ConversationMember(conversation_id=group.id, user_id=user.id, role=MemberRole.MEMBER))
        for name in ("alice", "offender"):
            db.add(ConversationMember(conversation_id=group.id, user_id=self.ids[name], role=MemberRole.MEMBER))
        message = Message(conversation_id=group.id, sender_id=self.ids["offender"], content=f"{title}里的辱骂")
        db.add(message)
        await db.commit()
        return group.id, message.id, [user.id for user in members]

    async def _measure(self, db, online: set[int], action) -> tuple[list[tuple[set[int], dict]], int, list[str]]:
        """跑一段会撤回消息的操作：返回 (message.recalled 事件, 组装了几次完整消息, 逐人发送的事件)。"""
        built = 0
        original = message_service.build_message_read

        async def counting(*args, **kwargs):
            nonlocal built
            built += 1
            return await original(*args, **kwargs)

        with self._events() as events, patch.object(manager, "online_user_ids", return_value=online), patch.object(message_service, "build_message_read", counting):
            await action()
        recalled = [(users, data) for event_type, users, data in events if event_type == "message.recalled"]
        return recalled, built, [event_type for event_type, users, _data in events if event_type == "message.recalled" and len(users) == 1]

    def _assert_single_broadcast(self, recalled, online_members: set[int], message_id: int, group_id: int) -> None:
        # 一条群发，只发给在线的群成员：不在线的不发，在线但不在群里的也不发
        self.assertEqual(len(recalled), 1)
        users, data = recalled[0]
        self.assertEqual(users, online_members)
        # 负载和用户自己撤回消息时一样，只有撤回信息，对所有人都相同
        self.assertEqual(set(data), {"id", "conversation_id", "message_id", "recalled_at", "recalled_by_id"})
        self.assertEqual((data["id"], data["conversation_id"]), (message_id, group_id))
        self.assertIsNotNone(data["recalled_at"])

    async def test_machine_recall_broadcasts_once(self) -> None:
        async with self.sessions() as db:
            group_id, message_id, members = await self._crowd(db, "大群", 200)
            online = set(members[:20]) | {self.ids["juror6"]}

        async def block(*_args, **_kwargs):
            return {"decision": "block", "severity": 2, "categories": ["harassment"], "category": "harassment", "level": 2, "model": "Gandi-AIHub"}

        async def moderate() -> None:
            with patch.object(moderation_service, "AsyncSessionLocal", self.sessions), patch.object(moderation_service, "_call_text_moderation", block):
                await moderation_service._moderate_message_task(message_id, ModerationSource.MESSAGE_AUTO, None)

        async with self.sessions() as db:
            recalled, built, per_user = await self._measure(db, online, moderate)
            self._assert_single_broadcast(recalled, set(members[:20]), message_id, group_id)
            self.assertEqual((built, per_user), (0, []))
            self.assertIsNotNone((await db.get(Message, message_id)).recalled_at)
            self.assertEqual((await db.execute(select(func.count(UserViolation.id)))).scalar_one(), 1)

    async def test_admin_recall_broadcasts_once(self) -> None:
        async with self.sessions() as db:
            group_id, message_id, members = await self._crowd(db, "大群", 200)
            online = set(members[:20]) | {self.ids["juror6"]}
            admin = await self._user(db, "admin")
            result: dict = {}

            async def recall() -> None:
                result["read"] = await admin_api.admin_recall_message(message_id, AdminMessageRecall(reason="违规"), admin, db)

            recalled, built, per_user = await self._measure(db, online, recall)
            self._assert_single_broadcast(recalled, set(members[:20]), message_id, group_id)
            self.assertEqual(recalled[0][1]["recalled_by_id"], admin.id)
            # 只为接口自己的返回值组装了一次，和群人数无关
            self.assertEqual((built, per_user), (1, []))
            self.assertEqual(result["read"].id, message_id)

    async def test_legacy_report_review_broadcasts_once(self) -> None:
        async with self.sessions() as db:
            group_id, message_id, members = await self._crowd(db, "大群", 200)
            online = set(members[:20]) | {self.ids["juror6"]}
            admin = await self._user(db, "admin")
            reporter = await self._user(db, "alice")
            report = await report_service.create_report(db, reporter.id, ReportCreate(target_type=ReportTargetType.MESSAGE, target_id=message_id, reason="辱骂骚扰"))

            async def review() -> None:
                await admin_api.admin_review_report(report.id, ReportReview(status=ReportStatus.REVIEWED, action="recall_message"), admin, db)

            recalled, built, per_user = await self._measure(db, online, review)
            self._assert_single_broadcast(recalled, set(members[:20]), message_id, group_id)
            # 只为接口返回的举报详情组装了一次
            self.assertEqual((built, per_user), (1, []))
            self.assertIsNotNone((await db.get(Message, message_id)).recalled_at)


# ---------------------------------------------------------------- 第二轮修复：回避的记录不漏结论、复议转管理员后的出口、后台设置


class RecusedRecordTests(Phase3TestCase):
    """回避的人不该知道案件后来怎么样了（docs/jury-v2.md 2.5）。"""

    # 回避的记录上，一切能反映案件进展的字段都是空的
    HIDDEN = {
        "stage": None, "outcome": None, "verdict_source": None, "verdict_level": None, "closed_at": None, "agreed": None,
        "overturned": False, "is_quality_check": False, "opinion_count": 0, "can_view": False, "public_case_id": None, "alias": None,
    }  # fmt: skip

    async def _recused(self, db, name: str, **filters) -> list[dict]:
        items, total = await jury_service.list_votes(db, self.ids[name], votes=["recuse"], **filters)
        self.assertEqual(total, len(items))
        return items

    async def test_recused_records_reveal_nothing_about_the_case(self) -> None:
        async with self.sessions() as db:
            # 合议里要有人回避之后还凑得齐三位，所以多一位资深委员
            await self._seniors(db, "juror4", "juror5", "juror6", "insider")
            # 原审里回避，案件之后定案为违规，有观点，进了公示栏，后来又被改判
            settled = await self._open_case(db)
            await self._cast(db, "juror1", settled.id, "recuse", "我认识这个人")
            for name in ("juror2", "juror3", "speaker"):
                result = await self._cast(db, name, settled.id, "l2", "明显违规")
            self.assertTrue(result["settled"])
            await self._age(db, settled.id, case_service.PUBLISH_DELAY_HOURS + 1)
            await case_service.overturn_case(db, settled.id, actor_id=self.ids["admin"], upheld=False, note="改判")
            # 原审里回避，案件还没定案
            pending = await self._open_case(db)
            await self._cast(db, "juror1", pending.id, "recuse")
            # 在质控题上回避：题目是早就结案的旧案件
            question = await self._question(db)
            await self._cast(db, "juror1", question.id, "recuse", quality=True)
            # 合议里回避，案件之后由合议定案
            split = await self._split(db, (await self._open_case(db)).id)
            self.assertEqual(split.queue, "panel")
            self.assertIsNone((await self._panel_cast(db, "juror4", split.id, "recuse"))["case"])
            await self._panel_cast(db, "juror5", split.id, "none", "不算违规")
            self.assertTrue((await self._panel_cast(db, "juror6", split.id, "none"))["settled"])

            # juror1：三条回避的记录长得一模一样，看不出哪个结了、结论是什么、哪个是质控题
            items = await self._recused(db, "juror1")
            self.assertEqual([await self._real(db, item["case_id"]) for item in items], [question.id, pending.id, settled.id])
            for item in items:
                read = JuryVoteItem(**item)
                self.assertEqual({key: item[key] for key in self.HIDDEN}, self.HIDDEN)
                self.assertEqual((read.vote, read.stage, read.vote_stage), ("recuse", None, "jury"))
                self.assertIsNotNone(read.voted_at)
            # 同一个案件，投过票的人照常看得到
            voter_rid = await self._rid(db, "juror2", settled.id)
            voter = next(item for item in (await jury_service.list_votes(db, self.ids["juror2"]))[0] if item["case_id"] == voter_rid)
            self.assertEqual((voter["stage"], voter["outcome"], voter["overturned"], voter["opinion_count"], voter["can_view"]), ("closed", "clean", True, 3, True))

            # 带了 state / outcome / agreed 任何一个筛选，就不返回回避的记录
            for filters in ({"state": "closed"}, {"state": "open"}, {"outcome": "violation"}, {"outcome": "clean"}, {"agreed": True}, {"agreed": False}):
                self.assertEqual(await self._recused(db, "juror1", **filters), [], filters)
                listed, total = await jury_service.list_votes(db, self.ids["juror1"], **filters)
                self.assertEqual(total, len(listed))
                self.assertNotIn("recuse", [item["vote"] for item in listed], filters)
            # juror1 在 split 那个案件上投过票：不带 vote 筛选时，state=closed 只剩这一条
            closed, total = await jury_service.list_votes(db, self.ids["juror1"], state="closed")
            self.assertEqual((total, [await self._real(db, item["case_id"]) for item in closed]), (1, [split.id]))
            self.assertEqual((await jury_service.list_votes(db, self.ids["juror1"]))[1], 4)

            # 合议里的回避同理
            panel = await self._recused(db, "juror4")
            self.assertEqual([(await self._real(db, item["case_id"]), item["vote_stage"]) for item in panel], [(split.id, "panel")])
            self.assertEqual({key: panel[0][key] for key in self.HIDDEN}, self.HIDDEN)
            JuryVoteItem(**panel[0])
            for filters in ({"state": "closed"}, {"outcome": "clean"}, {"agreed": False}):
                self.assertEqual(await self._recused(db, "juror4", **filters), [], filters)
            # 合议里投了票的人看得到结论
            seat = (await jury_service.list_votes(db, self.ids["juror5"]))[0][0]
            self.assertEqual((seat["stage"], seat["vote_stage"], seat["outcome"], seat["verdict_source"], seat["agreed"]), ("closed", "panel", "clean", "panel", True))
            # 回避的人也收不到定案 / 改判的实时事件
            with self._events() as events:
                await case_service.notify_jurors_settled(db, split)
            self.assertNotIn(self.ids["juror4"], self._per_user(events, "jury.case.settled"))


class ReconsiderAdminTests(Phase3TestCase):
    """复议转给管理员之后：维持原结论也要有出口（POST /admin/jury/reviews/{id}/decide）。"""

    TEXT = "对方连续三条都在骂人，不是玩笑"
    TITLE = "你申请的复议有结果了"

    async def _reconsider(self, db, message: str) -> tuple[ReportCase, UserReport, JuryReview]:
        report = await self._report(db, "alice", message)
        await self._queue(db, report.case_id)
        case = await self._settle(db, report.case_id, "none")
        await review_service.request_reconsider(db, self.ids["alice"], report.id, self.TEXT)
        review = (await db.execute(select(JuryReview).where(JuryReview.kind == "reconsider", JuryReview.case_id == case.id))).scalar_one()
        return case, report, review

    async def _status(self, db, report: UserReport) -> tuple[bool, str]:
        read = await report_service.build_report_read(db, await report_service.get_report(db, report.id), self.ids["alice"])
        return read.can_reconsider, read.reconsider_status

    async def _decisions(self, db) -> list[dict]:
        rows = await db.execute(select(AdminAuditLog).where(AdminAuditLog.action == "jury.review.decide").order_by(AdminAuditLog.id))
        return [{"actor": log.actor_id, "target": (log.target_type, log.target_id), **json.loads(log.details)} for log in rows.scalars().all()]

    async def _result_notices(self) -> list[str]:
        # 用一个新会话读，和真实请求一样：通知中心按时间排序，同一个会话里刚写的对象带时区、读出来的不带，混在一起排不了
        async with self.sessions() as db:
            notifications = await notification_service.list_unified_notifications(db, self.ids["alice"])
        return [item.title for item in notifications.items if item.type == "jury" and item.title == self.TITLE]

    async def test_admin_upholds_a_reconsideration_that_was_handed_over(self) -> None:
        async with self.sessions() as db:
            admin, alice = await self._user(db, "admin"), self.ids["alice"]
            # 凑不齐三位资深委员：复议直接到了管理员手里
            case, report, review = await self._reconsider(db, "insult")
            self.assertEqual((review.stage, review.status), ("admin", "open"))
            self.assertEqual(await self._code(review_service.admin_decide(db, admin, 99999, "uphold")), (404, "review_not_found"))
            self.assertEqual(await self._code(review_service.admin_decide(db, admin, review.id, "revoke")), (422, "invalid_action"))
            self.assertEqual(await self._code(review_service.admin_decide(db, admin, review.id, "overturn", category="nonsense")), (422, "invalid_category"))
            self.assertEqual(await self._code(review_service.admin_decide(db, admin, review.id, "overturn", level=4)), (422, "invalid_level"))
            self.assertEqual(await self._status(db, report), (False, "pending"))

            with self._events() as events:
                read = AdminReviewRead(**await review_service.admin_decide(db, admin, review.id, "uphold", note=" 复核过了，确实是玩笑 "))
            self.assertEqual((read.id, read.kind, read.status, read.outcome, read.decided_by, read.stage, read.case_id), (review.id, "reconsider", "closed", "uphold", "admin", "admin", case.id))
            self.assertIsNotNone(read.closed_at)
            # 案件的结论没动，举报人这边不再是 pending
            await db.refresh(case)
            self.assertEqual((case.status, case.overturned_at), ("dismissed", None))
            self.assertEqual(await self._status(db, report), (False, "upheld"))
            self.assertEqual(CaseRead(**(await case_service.build_case_reads(db, [case]))[0]).reconsider_status, "upheld")
            resolved = self._per_user(events, "report.resolved")
            self.assertEqual(set(resolved), {alice})
            self.assertEqual((resolved[alice]["report_id"], resolved[alice]["title"], resolved[alice]["body"], resolved[alice]["status"]), (report.id, self.TITLE, review_service.RECONSIDER_UPHELD_BY_ADMIN, "rejected"))
            self.assertEqual(await self._result_notices(), [self.TITLE])
            self.assertEqual(
                await self._decisions(db),
                [{"actor": admin.id, "target": ("jury_review", review.id), "kind": "reconsider", "case_id": case.id, "action": "uphold", "stage": "admin", "level": None, "category": None, "note": "复核过了，确实是玩笑"}],
            )
            # 只能处理一次；一个案件也只能复议一次
            self.assertEqual(await self._code(review_service.admin_decide(db, admin, review.id, "overturn")), (409, "review_closed"))
            self.assertEqual(await self._code(review_service.request_reconsider(db, alice, report.id, self.TEXT)), (409, "reconsider_unavailable"))

    async def test_admin_overturns_or_takes_over_from_the_seniors(self) -> None:
        async with self.sessions() as db:
            await self._seniors(db)
            admin, alice = await self._user(db, "admin"), self.ids["alice"]
            juror5 = await self._user(db, "juror5")

            # 还在资深阶段：一位已经投了、一位分到手没投，管理员直接接手并改判
            case, report, review = await self._reconsider(db, "insult")
            self.assertEqual(review.stage, "senior")
            await self._review_vote(db, "juror4", review.id, "uphold")
            self.assertEqual((await review_service.next_review(db, juror5))["review_id"], review.id)
            case_service._last_queue_event_at = None
            with self._events() as events:
                read = AdminReviewRead(**await review_service.admin_decide(db, admin, review.id, "overturn", level=3, category="harassment", note="三条都在骂人"))
            self.assertEqual((read.status, read.outcome, read.decided_by, read.stage), ("closed", "overturn", "admin", "senior"))
            self.assertEqual([(vote.juror.username, vote.vote) for vote in read.votes], [("juror4", "uphold"), ("juror5", None)])
            await db.refresh(case)
            self.assertEqual((case.status, case.verdict_category, case.verdict_level, case.overturn_note, case.overturned_by_id), ("resolved", "harassment", 3, "三条都在骂人", admin.id))
            violation = await db.get(UserViolation, case.violation_id)
            self.assertEqual((violation.source, violation.level, violation.actor_id), ("report", 3, admin.id))
            self.assertIsNotNone((await db.get(Message, self.messages["insult"])).recalled_at)
            self.assertEqual(await self._status(db, report), (False, "overturned"))
            self.assertEqual(set(self._per_user(events, "report.resolved")), {alice})
            self.assertIn("jury.queue.changed", [event for event, _users, _data in events])
            # 资深委员那边：这件没了，分到手没投的再投是 409
            self.assertIsNone(await review_service.next_review(db, juror5))
            self.assertEqual((await jury_service.my_status(db, juror5))["senior"]["appeals"], 0)
            self.assertEqual(await self._code(review_service.cast_vote(db, juror5, review.id, "overturn")), (409, "review_closed"))
            # 已经投的那张票照样留着，结果是管理员给的
            mine, _total = await review_service.my_reviews(db, self.ids["juror4"])
            self.assertEqual((mine[0]["my_vote"], mine[0]["outcome"]), ("uphold", "overturn"))

            # 不带等级、类别和说明：类别按举报理由映射，等级取类别的默认值，说明用默认的
            other, other_report, other_review = await self._reconsider(db, "insult2")
            read = AdminReviewRead(**await review_service.admin_decide(db, admin, other_review.id, "overturn"))
            await db.refresh(other)
            self.assertEqual((read.outcome, other.status, other.verdict_category, other.verdict_level, other.overturn_note),
                             ("overturn", "resolved", "harassment", penalty_service.default_level("harassment"), "举报人申请复议，管理员复核后改判为违规"))
            self.assertEqual([(item["action"], item["stage"], item["level"], item["category"]) for item in await self._decisions(db)], [("overturn", "senior", 3, "harassment"), ("overturn", "senior", None, None)])

            # 管理员不走这个接口、直接用改判接口：进行中的复议一起了结，举报人收到通知
            third, third_report, third_review = await self._reconsider(db, "insult3")
            self.assertEqual((third_review.stage, third_review.status), ("senior", "open"))
            await self._review_vote(db, "juror4", third_review.id, "uphold")
            with self._events() as events:
                await case_service.overturn_case(db, third.id, actor_id=admin.id, upheld=True, category="harassment", level=1, note="复核后认定违规")
            await db.refresh(third_review)
            self.assertEqual((third_review.status, third_review.outcome, third_review.decided_by), ("closed", "overturn", "admin"))
            self.assertIsNotNone(third_review.closed_at)
            self.assertEqual(await self._status(db, third_report), (False, "overturned"))
            self.assertEqual(set(self._per_user(events, "report.resolved")), {alice})
            self.assertEqual(len(await self._result_notices()), 3)
            # 已经有结果的复议不能再处理
            self.assertEqual(await self._code(review_service.admin_decide(db, admin, third_review.id, "uphold")), (409, "review_closed"))
            self.assertEqual(len(await self._decisions(db)), 2)

    async def test_appeal_reviews_are_pointed_at_the_appeal_endpoint(self) -> None:
        async with self.sessions() as db:
            await self._seniors(db)
            await self._switch(db, appeals=True)
            admin = await self._user(db, "admin")
            violation = await self._machine_violation(db)
            review = await self._appeal(db, violation)
            self.assertIsNotNone(review)
            for action in ("uphold", "overturn"):
                with self.assertRaises(HTTPException) as caught:
                    await review_service.admin_decide(db, admin, review.id, action)
                detail = caught.exception.detail
                self.assertEqual((caught.exception.status_code, detail["code"], detail["violation_id"]), (409, "appeal_review", violation.id))
                self.assertIn(f"POST /admin/violations/{violation.id}/appeal", detail["message"])
            await db.refresh(review)
            await db.refresh(violation)
            self.assertEqual((review.status, review.outcome, violation.appeal_status, violation.appeal_stage), ("open", None, "pending", "senior"))
            self.assertEqual(await self._decisions(db), [])


class RoundTwoSettingsTests(Phase3TestCase):
    async def test_settings_list_every_hall_precedent_and_quality_threshold(self) -> None:
        thresholds = jury_settings.thresholds()
        names = [name for name in type(settings).model_fields if name.startswith(("jury_hall_", "jury_precedent_", "jury_quality_"))]
        self.assertGreaterEqual(len(names), 20)
        for name in names:
            self.assertEqual(thresholds[name.removeprefix("jury_")], getattr(settings, name), name)
        self.assertEqual(
            {key: thresholds[key] for key in ("hall_reports_per_day", "hall_auto_hide_reports", "quality_recuse_retry_days", "quality_shift_min_hours", "quality_shift_max_hours", "precedent_summary_max")},
            {"hall_reports_per_day": 20, "hall_auto_hide_reports": 3, "quality_recuse_retry_days": 7, "quality_shift_min_hours": 2, "quality_shift_max_hours": 40, "precedent_summary_max": 1000},
        )


class RoundTwoEndpointTests(Phase3TestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.acting = "juror1"
        api = FastAPI()
        api.include_router(jury_api.router, prefix="/jury")
        api.include_router(senior_api.router, prefix="/jury")
        api.include_router(admin_api.router, prefix="/admin")
        api.include_router(reports_api.router, prefix="/reports")

        async def isolated_db():
            async with self.sessions() as session:
                yield session

        async def acting_user(db: Annotated[AsyncSession, Depends(get_db)]) -> User:
            return await db.get(User, self.ids[self.acting])

        api.dependency_overrides[get_db] = isolated_db
        api.dependency_overrides[get_current_user] = acting_user
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://testserver")

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        await super().asyncTearDown()

    async def test_recused_votes_and_review_decisions_over_http(self) -> None:
        async with self.sessions() as db:
            settled = await self._open_case(db)
            await self._cast(db, "juror1", settled.id, "recuse")
            for name in ("juror2", "juror3", "speaker"):
                await self._cast(db, name, settled.id, "l2")
            # 没有资深委员：复议直接到管理员手里
            report = await self._report(db, "alice", "insult")
            await self._queue(db, report.case_id)
            dismissed = await self._settle(db, report.case_id, "none")
            await review_service.request_reconsider(db, self.ids["alice"], report.id, "对方连续三条都在骂人，不是玩笑")
            review_id = (await db.execute(select(JuryReview.id))).scalar_one()
            second = await self._report(db, "alice", "club_insult")
            await self._queue(db, second.case_id)
            await self._settle(db, second.case_id, "none", jurors=("juror4", "juror5", "juror6"))
            await review_service.request_reconsider(db, self.ids["alice"], second.id, "社团里那句话是在针对我，请再看一下")
            second_review = (await db.execute(select(JuryReview.id).where(JuryReview.case_id == second.case_id))).scalar_one()
        client = self.client

        # 我的评审：回避的记录 stage 为 null，带进展类筛选时不返回
        votes = (await client.get("/jury/votes", params={"vote": "recuse"})).json()
        self.assertEqual(votes["total"], 1)
        item = votes["items"][0]
        self.assertEqual((item["vote"], item["stage"], item["outcome"], item["closed_at"], item["agreed"], item["can_view"]), ("recuse", None, None, None, None, False))
        for params in ({"vote": "recuse", "state": "closed"}, {"vote": "recuse", "outcome": "violation"}, {"vote": "recuse", "agreed": "false"}):
            self.assertEqual((await client.get("/jury/votes", params=params)).json(), {"items": [], "total": 0}, params)
        # 不限 vote 时，state=closed 只剩真正投过票的那一条
        closed = (await client.get("/jury/votes", params={"state": "closed"})).json()
        self.assertEqual((closed["total"], [entry["vote"] for entry in closed["items"]]), (1, ["none"]))
        # 留空等同于不筛选：回避的记录照常列出
        self.assertEqual((await client.get("/jury/votes?state=&outcome=&agreed=&vote=recuse")).json()["total"], 1)

        # 后台：给转过来的复议出结果
        self.acting = "admin"
        pending = (await client.get("/admin/jury/reviews", params={"kind": "reconsider", "status": "open"})).json()
        self.assertEqual((pending["total"], {entry["stage"] for entry in pending["items"]}), (2, {"admin"}))
        self.assertEqual((await client.post(f"/admin/jury/reviews/{review_id}/decide", json={"action": "keep"})).status_code, 422)
        self.assertEqual((await client.post(f"/admin/jury/reviews/{review_id}/decide", json={"action": "overturn", "level": 4})).status_code, 422)
        missing = await client.post("/admin/jury/reviews/99999/decide", json={"action": "uphold"})
        self.assertEqual((missing.status_code, missing.json()["detail"]["code"]), (404, "review_not_found"))
        upheld = await client.post(f"/admin/jury/reviews/{review_id}/decide", json={"action": "uphold", "note": "复核过了"})
        self.assertEqual((upheld.status_code, upheld.json()["id"], upheld.json()["status"], upheld.json()["outcome"], upheld.json()["decided_by"]), (200, review_id, "closed", "uphold", "admin"))
        again = await client.post(f"/admin/jury/reviews/{review_id}/decide", json={"action": "uphold"})
        self.assertEqual((again.status_code, again.json()["detail"]["code"]), (409, "review_closed"))
        self.assertEqual((await client.get(f"/admin/cases/{dismissed.id}")).json()["reconsider_status"], "upheld")
        overturned = await client.post(f"/admin/jury/reviews/{second_review}/decide", json={"action": "overturn", "level": 2, "category": "harassment", "note": "确实是在针对人"})
        self.assertEqual((overturned.status_code, overturned.json()["outcome"], overturned.json()["case_id"]), (200, "overturn", second.case_id))
        detail = (await client.get(f"/admin/cases/{second.case_id}")).json()
        self.assertEqual((detail["status"], detail["verdict_level"], detail["overturn_note"], detail["reconsider_status"]), ("resolved", 2, "确实是在针对人", "overturned"))
        self.assertEqual((await client.get("/admin/jury/reviews", params={"status": "open"})).json()["total"], 0)

        # 举报人这边
        self.acting = "alice"
        mine = {entry["id"]: entry["reconsider_status"] for entry in (await client.get("/reports/me")).json()}
        self.assertEqual((mine[report.id], mine[second.id]), ("upheld", "overturned"))
