"""风纪系统 v2 二期（docs/jury-v2.md 第 2 节）。"""

from __future__ import annotations

import json
import unittest
from datetime import UTC, date, datetime, timedelta
from typing import Annotated
from unittest.mock import patch

import httpx
from fastapi import Depends, FastAPI, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

import app.models  # noqa: F401
from app.api.v1 import admin as admin_api
from app.api.v1 import jury as jury_api
from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.models.case import (
    JuryAchievement,
    JuryExamAttempt,
    JuryMember,
    JuryNotification,
    JuryPointLog,
    JuryPracticeAnswer,
    JuryVote,
    ReportCase,
)
from app.models.enums import UserStatus
from app.models.group_activity import GroupMemberDailyActivity
from app.models.penalty import UserViolation
from app.models.report import AdminAuditLog
from app.models.user import User
from app.schemas.case import JuryCaseView, JuryMemberRead, JuryMemberReview, JuryStatus, JuryVoteCreate, JuryVoteResult
from app.schemas.jury import (
    AdminExamRead,
    AdminQuestionDetail,
    AdminQuestionRead,
    ExamResult,
    ExamStart,
    ExamState,
    JuryAchievementPage,
    JuryConfig,
    JuryPointPage,
    JuryVoteItem,
    Leaderboard,
    PracticeAnswerResult,
    PracticeNext,
)
from app.schemas.user import UserPublic
from app.services import cases as case_service
from app.services import jury as jury_service
from app.services import jury_bank as bank_service
from app.services import jury_exam as exam_service
from app.services import jury_opinions as opinion_service
from app.services import jury_overview as overview_service
from app.services import jury_points as points_service
from app.services import jury_settings
from app.services import notifications as notification_service
from app.services import penalties as penalty_service

# 夹具和工具方法沿用一期的；基类自己没有测试方法，不会被重复收集。
from test_jury_v2 import JURORS, JuryV2TestCase


class Phase2TestCase(JuryV2TestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self._serial = 0
        # 已核对的题按编号固定切出一份只给练习场。这里的测试大多直接按顺序造题，先把这一份关掉
        # （已核对的全部用于考试和质控，练习场只出未核对的）；切分本身在 PoolTests 里单独测。
        splitter = patch.object(bank_service, "PRACTICE_EVERY", 10**9)
        splitter.start()
        self.addCleanup(splitter.stop)

    # ------------------------------------------------------------ 工具

    async def _member(self, db, name: str) -> JuryMember:
        return await jury_service.get_member(db, self.ids[name])

    async def _question(self, db, *, upheld: bool = True, verified: bool = True, source: str | None = "admin", **fields) -> ReportCase:
        """直接造一道题：一个已结的群聊消息案件，和所有委员都没有回避关系。"""
        self._serial += 1
        values = dict(
            target_key=f"message:{80000 + self._serial}",
            target_type="message",
            target_id=80000 + self._serial,
            reported_user_id=self.ids["offender"],
            jury_eligible=True,
            status="resolved" if upheld else "dismissed",
            queue="admin",
            verdict_source="admin",
            verdict_category="harassment" if upheld else None,
            verdict_level=2 if upheld else None,
            primary_reason="辱骂骚扰",
            closed_at=datetime.now(UTC) - timedelta(days=10),
            is_golden=True,
            golden_verified=verified,
            golden_source=source,
        )
        values.update(fields)
        case = ReportCase(**values)
        db.add(case)
        await db.commit()
        return case

    async def _questions(self, db, count: int, **fields) -> list[ReportCase]:
        return [await self._question(db, upheld=index % 2 == 0, **fields) for index in range(count)]

    async def _open_case(self, db, **fields) -> ReportCase:
        """直接造一个在委员队列里的案件：没有被举报人，结案时不会连带出处罚。"""
        self._serial += 1
        values = dict(
            target_key=f"post:{90000 + self._serial}",
            target_type="post",
            target_id=90000 + self._serial,
            jury_eligible=True,
            status="open",
            queue="jury",
            queued_at=datetime.now(UTC),
            priority=1.0,
            primary_reason="辱骂骚扰",
        )
        values.update(fields)
        case = ReportCase(**values)
        db.add(case)
        await db.commit()
        return case

    async def _cast(self, db, juror: str, case_id: int, vote: str, note: str | None = None, category: str | None = None, *, quality: bool = False) -> dict:
        """对指定的案件投票：还没分到手的先直接分给他，免得受分案顺序影响。"""
        user = await self._user(db, juror)
        existing = (await db.execute(select(JuryVote).where(JuryVote.case_id == case_id, JuryVote.juror_id == user.id))).scalar_one_or_none()
        if existing is None:
            db.add(JuryVote(case_id=case_id, juror_id=user.id, assigned_at=datetime.now(UTC), is_quality_check=quality))
            await db.commit()
        return await jury_service.cast_vote(db, user, await self._rid(db, user, case_id), vote, note, category)

    async def _settle_case(self, db, votes: dict[str, str]) -> ReportCase:
        case = await self._open_case(db)
        for juror, vote in votes.items():
            result = await self._cast(db, juror, case.id, vote)
        self.assertTrue(result["settled"])
        await db.refresh(case)
        return case

    async def _eligible_user(self, db, name: str) -> User:
        """满足四项申请条件的普通用户。发言数来自群活跃日报（消息表里一条都没有）。"""
        user = User(
            username=name, hashed_password="x", ccw_student_oid=f"oid-{name}", created_at=datetime.now(UTC) - timedelta(days=60)
        )
        db.add(user)
        await db.flush()
        db.add(
            GroupMemberDailyActivity(
                conversation_id=self.plaza_id, user_id=user.id, activity_date=datetime.now(UTC).date() - timedelta(days=3), message_count=45
            )
        )
        await db.commit()
        self.ids[name] = user.id
        return user

    async def _history_votes(self, db, juror: str, agreed: list[bool]) -> None:
        """给一位委员补一批已经出了结论的历史票（每张票一个案件）。"""
        now = datetime.now(UTC)
        for index, value in enumerate(agreed):
            case = await self._question(db, is_golden=False, golden_verified=False, golden_source=None)
            db.add(
                JuryVote(
                    case_id=case.id,
                    juror_id=self.ids[juror],
                    vote="l2" if value else "none",
                    assigned_at=now - timedelta(days=2),
                    voted_at=now - timedelta(days=2) + timedelta(minutes=index),
                    agreed=value,
                )
            )
        member = await self._member(db, juror)
        member.votes_total += len(agreed)
        member.votes_settled += len(agreed)
        member.votes_agreed += sum(agreed)
        await db.commit()

    async def _exam_answers(self, db, attempt_id: int, correct: int, answered: int | None = None) -> list[dict]:
        """前 correct 道答对、其余答错；只答前 answered 道。"""
        attempt = await db.get(JuryExamAttempt, attempt_id)
        answers: list[dict] = []
        for index, case_id in enumerate(json.loads(attempt.question_ids), start=1):
            if answered is not None and index > answered:
                break
            upheld = bank_service.question_upheld(await db.get(ReportCase, case_id))
            right, wrong = ("l2", "none") if upheld else ("none", "l1")
            answers.append({"question": index, "vote": right if index <= correct else wrong})
        return answers

    async def _kinds(self, db, name: str) -> list[str]:
        rows = await db.execute(select(JuryPointLog.kind).where(JuryPointLog.user_id == self.ids[name]).order_by(JuryPointLog.id))
        return list(rows.scalars().all())


# ---------------------------------------------------------------- 题库


class BankTests(Phase2TestCase):
    async def _consensus(self, db, votes: list[str], *, hours: float = 200, **fields) -> ReportCase:
        """委员定案的案件，带上他们的票。"""
        now = datetime.now(UTC)
        case = await self._question(
            db,
            is_golden=False,
            verified=False,
            source=None,
            verdict_source="jury",
            jury_outcome="consensus",
            publishable=True,
            closed_at=now - timedelta(hours=hours),
            **fields,
        )
        for index, vote in enumerate(votes):
            db.add(JuryVote(case_id=case.id, juror_id=self.ids[JURORS[index]], vote=vote, assigned_at=now - timedelta(hours=hours), voted_at=now - timedelta(hours=hours)))
        await db.commit()
        return case

    async def test_unanimous_cases_enter_the_bank_automatically_and_verified(self) -> None:
        async with self.sessions() as db:
            unanimous = await self._consensus(db, ["l2", "l1", "l2", "l3"])
            # 弃权不是有效票，不影响「全票一致」
            with_abstain = await self._consensus(db, ["none", "none", "abstain", "none", "none"], upheld=False)
            too_fresh = await self._consensus(db, ["l2"] * 4, hours=10)
            three_votes = await self._consensus(db, ["l2"] * 3)
            split = await self._consensus(db, ["l2", "l2", "l2", "l2", "none"])
            overturned = await self._consensus(db, ["l2"] * 4, overturned_at=datetime.now(UTC))
            private = await self._consensus(db, ["l2"] * 4, jury_eligible=False)
            privacy = await self._consensus(db, ["l2"] * 4, verdict_category="privacy")
            blocked = await self._consensus(db, ["l2"] * 4, jury_block_reason="sensitive")
            # 公开动态且认定违规：公示栏会展示内容，考试可以去查答案
            public_post = await self._consensus(db, ["l2"] * 4, target_type="post", content_publishable=True)
            clean_post = await self._consensus(db, ["none"] * 4, upheld=False, target_type="post", content_publishable=True)
            appealed = await self._consensus(db, ["l2"] * 4)
            violation = UserViolation(
                user_id=self.ids["offender"], category="harassment", level=2, points=3, source="jury", case_id=appealed.id,
                expires_at=datetime.now(UTC) + timedelta(days=90), appeal_status="pending",
            )
            db.add(violation)
            await db.commit()

            self.assertEqual(await bank_service.auto_include(db), 4)
            cases = [unanimous, with_abstain, too_fresh, three_votes, split, overturned, private, privacy, blocked, public_post, clean_post, appealed]
            for case in cases:
                await db.refresh(case)
            # 三票一致就能定案，也是最常见的定案方式，同样入库
            for case in (unanimous, with_abstain, three_votes, clean_post):
                self.assertEqual((case.is_golden, case.golden_verified, case.golden_source), (True, True, "consensus"))
            # 还没到 72 小时、申诉还没有结果：下次再看
            for case in (too_fresh, appealed):
                self.assertEqual((case.is_golden, case.golden_source), (False, None))
            # 永远不会满足条件的记成 excluded，不再反复检查
            for case in (split, overturned, private, privacy, blocked, public_post):
                self.assertEqual((case.is_golden, case.golden_verified, case.golden_source), (False, False, "excluded"))
            self.assertEqual(await bank_service.auto_include(db), 0)

            # 申诉驳回后入库；定时循环里走的是同一个函数
            violation.appeal_status = "rejected"
            await db.commit()
            with patch.object(jury_service, "renew_expired_terms", wraps=jury_service.renew_expired_terms):
                self.assertEqual(await case_service.sweep_jury_housekeeping(db), {"terms": 0, "questions": 1})
            await db.refresh(appealed)
            self.assertEqual((appealed.is_golden, appealed.golden_verified, appealed.golden_source), (True, True, "consensus"))
            # 群聊内容公示的开关打开后，委员审过的群聊违规案件也会展示内容：不再入库，已经在库里的也不再出题
            await jury_settings.update_switches(db, self.ids["admin"], {"public_group_content": True})
            late = await self._consensus(db, ["l2"] * 4)
            self.assertEqual(await bank_service.auto_include(db), 0)
            await db.refresh(late)
            self.assertEqual(late.golden_source, "excluded")
            self.assertEqual(await bank_service.block_reason(db, unanimous), "public_content")
            self.assertIsNone(await bank_service.block_reason(db, with_abstain))

    async def test_admin_verdicts_enter_the_bank_unverified_and_respect_the_exclusions(self) -> None:
        async with self.sessions() as db:
            message = await self._report(db, "alice", "insult")
            sensitive = await self._report(db, "alice", "insult2", "色情低俗")
            post_case_id = await self._post_case(db)
            dismissed_post = await self._open_case(db, content_publishable=True)
            cases: dict[str, ReportCase] = {}
            for name, case_id, upheld in (("message", message.case_id, True), ("sensitive", sensitive.case_id, True), ("post", post_case_id, True), ("clean_post", dismissed_post.id, False)):
                case = await case_service.resolve_case(db, case_id, source="admin", actor_id=self.ids["admin"], upheld=upheld, category="harassment" if upheld else None)
                await bank_service.include_after_admin(db, case)
                await db.commit()
                cases[name] = case
            # 管理员随手结的案进题库，但只是未核对的练习题
            self.assertEqual((cases["message"].is_golden, cases["message"].golden_verified, cases["message"].golden_source), (True, False, "admin"))
            self.assertEqual((cases["clean_post"].is_golden, cases["clean_post"].golden_verified), (True, False))
            # 敏感类别、公示栏会展示内容的（公开动态且认定违规）不入库
            self.assertFalse(cases["sensitive"].is_golden)
            self.assertFalse(cases["post"].is_golden)
            self.assertEqual(await bank_service.block_reason(db, cases["sensitive"]), "sensitive")
            self.assertEqual(await bank_service.block_reason(db, cases["post"]), "public_content")
            # 私聊的不入库
            private = await self._question(db, is_golden=False, verified=False, source=None, jury_eligible=False)
            self.assertFalse(await bank_service.include_after_admin(db, private))

            # 未核对的题不用于评审中的质控：强制出质控题也不会发它
            juror = await self._user(db, "juror1")
            member = await self._member(db, "juror1")
            member.force_quality = True
            await db.commit()
            self.assertIsNone(await jury_service.next_case(db, juror))
            self.assertEqual(await bank_service.verified_count(db), 0)
            # 核对之后才发
            detail = await bank_service.update_question(db, cases["message"].id, actor_id=self.ids["admin"], changes={"verified": True, "explanation": " 连续三条都在骂人 "})
            AdminQuestionDetail(**detail)
            self.assertEqual((detail["verified"], detail["source"], detail["explanation"], detail["block_reason"]), (True, "admin", "连续三条都在骂人", None))
            view = await jury_service.next_case(db, juror)
            self.assertEqual(await self._real(db, view["case_id"]), cases["message"].id)
            result = await jury_service.cast_vote(db, juror, await self._rid(db, juror, cases["message"].id), "l2", None)
            # 质控题揭晓时带上题库里的讲解；普通案件的详情没有
            self.assertEqual((result["case"]["is_quality_check"], result["case"]["verdict"]["explanation"], result["case"]["my_vote"]["agreed"]), (True, "连续三条都在骂人", True))

    async def test_admin_can_curate_the_bank(self) -> None:
        async with self.sessions() as db:
            question = await self._question(db, verified=False, source=None)
            private = await self._question(db, is_golden=False, verified=False, source=None, jury_eligible=False)
            sensitive = await self._question(db, is_golden=False, verified=False, source=None, verdict_category="privacy")
            open_case = await self._open_case(db)
            admin_id = self.ids["admin"]

            async def update(case: ReportCase, **changes):
                return await bank_service.update_question(db, case.id, actor_id=admin_id, changes=changes)

            # 上线前回填的旧题：没记来源，算 legacy、未核对
            listed, total = await bank_service.list_questions(db)
            self.assertEqual((total, listed[0]["case_id"], listed[0]["source"], listed[0]["verified"], listed[0]["outcome"], listed[0]["level"]), (1, question.id, "legacy", False, "violation", 2))
            AdminQuestionRead(**listed[0])
            self.assertEqual((await bank_service.list_questions(db, source="legacy"))[1], 1)
            self.assertEqual((await bank_service.list_questions(db, source="consensus"))[1], 0)
            self.assertEqual((await bank_service.list_questions(db, verified=True))[1], 0)
            self.assertEqual((await bank_service.list_questions(db, query=str(question.id)))[1], 1)
            self.assertEqual((await bank_service.list_questions(db, query="不存在的理由"))[1], 0)

            # 不允许入库的案件：加入和核对都被拒绝，什么都不改
            for case in (private, sensitive, open_case):
                self.assertEqual(await self._code(update(case, in_bank=True)), (422, "case_not_eligible"))
                await db.refresh(case)
                self.assertFalse(case.is_golden)
            self.assertEqual(await self._code(update(private, verified=True)), (409, "not_in_bank"))
            self.assertEqual((await self._code(update(question, category="sexual")))[1], "invalid_category")
            self.assertEqual((await self._code(bank_service.update_question(db, 999999, actor_id=admin_id, changes={"verified": True})))[0], 404)
            # 还没入库的案件也能预览，并告诉管理员为什么不能当题目
            preview = await bank_service.question_detail(db, private.id)
            self.assertEqual((preview["in_bank"], preview["source"], preview["block_reason"]), (False, None, "private"))

            # 一位委员先在评审中答过这道质控题：按原答案（违规）他答错了
            await update(question, verified=True)
            juror = await self._user(db, "juror1")
            await self._cast(db, "juror1", question.id, "none", quality=True)
            member = await self._member(db, "juror1")
            self.assertEqual((member.golden_total, member.golden_correct), (1, 0))
            # 改标准答案：之前的作答重新对一遍；「不违规」的答案不留等级和类别
            detail = await update(question, outcome="clean", level=3, category="fraud", explanation="结合上下文是玩笑")
            self.assertEqual((detail["outcome"], detail["level"], detail["category"], detail["answer_overridden"], detail["answered"], detail["correct_rate"]), ("clean", None, None, True, 1, 1.0))
            await db.refresh(member)
            self.assertEqual((member.golden_total, member.golden_correct), (1, 1))
            self.assertTrue((await jury_service.case_detail(db, juror.id, await self._rid(db, juror.id, question.id)))["my_vote"]["agreed"])
            self.assertEqual((await jury_service.case_detail(db, juror.id, await self._rid(db, juror.id, question.id)))["verdict"]["outcome"], "clean")
            detail = await update(question, outcome="violation", level=3, category="fraud")
            self.assertEqual((detail["outcome"], detail["level"], detail["category"], detail["category_label"]), ("violation", 3, "fraud", "诈骗"))
            # 传 null 清空改动，退回案件自身的结论
            detail = await update(question, outcome=None, level=None, category=None)
            self.assertEqual((detail["outcome"], detail["level"], detail["category"], detail["answer_overridden"]), ("violation", 2, "harassment", False))

            # 移出题库之后，管理员结案和自动入库都不会再把它放回来
            removed = await update(question, in_bank=False)
            self.assertEqual((removed["in_bank"], removed["verified"], removed["source"]), (False, False, None))
            await db.refresh(question)
            self.assertEqual(question.golden_source, "removed")
            self.assertFalse(await bank_service.include_after_admin(db, question))
            readded = await update(question, in_bank=True, verified=True)
            self.assertEqual((readded["in_bank"], readded["verified"], readded["source"]), (True, True, "admin"))
            actions = (await db.execute(select(AdminAuditLog.action).where(AdminAuditLog.action == "jury.question.update"))).scalars().all()
            self.assertGreaterEqual(len(actions), 5)


# ---------------------------------------------------------------- 入门考试


class ExamTests(Phase2TestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        async with self.sessions() as db:
            # 12 道已核对的题，外加：未核对的、考生本人是当事人的 —— 这两种都不能出现在卷子里
            self.questions = [case.id for case in await self._questions(db, 12)]
            self.unverified = (await self._question(db, verified=False)).id
            carol = await self._eligible_user(db, "carol")
            self.own_case = (await self._question(db, reported_user_id=carol.id)).id

    async def test_exam_draws_verified_questions_without_case_ids(self) -> None:
        async with self.sessions() as db:
            carol = await self._user(db, "carol")
            state = ExamState(**await exam_service.exam_state(db, carol))
            self.assertEqual((state.available, state.eligible, state.passed, state.active, state.last_result, state.next_attempt_at), (True, True, False, None, None, None))
            self.assertEqual((state.total, state.pass_score, state.time_limit_minutes, state.attempts_used, state.attempts_max, state.window_days), (10, 8, 60, 0, 3, 30))
            # 发言数取的是群活跃日报（消息表里一条都没有）
            activity = next(item for item in await jury_service.eligibility(db, carol) if item["key"] == "activity")
            self.assertEqual((activity["ok"], activity["detail"]), (True, "已发言 45 条"))

            started = await exam_service.start_exam(db, carol)
            ExamStart(**started)
            self.assertEqual([item["question"] for item in started["questions"]], list(range(1, 11)))
            for item in started["questions"]:
                self.assertEqual(set(item), {"question", "kind", "reasons", "report_count", "messages", "post"})
            self.assertEqual(started["expires_at"] - started["server_time"], timedelta(minutes=60))
            attempt = await db.get(JuryExamAttempt, started["attempt_id"])
            drawn = json.loads(attempt.question_ids)
            self.assertEqual(len(set(drawn)), 10)
            self.assertLessEqual(set(drawn), set(self.questions))
            self.assertNotIn(self.unverified, drawn)
            self.assertNotIn(self.own_case, drawn)
            # 已有进行中的一场：返回同一场，题目和顺序不变
            again = await exam_service.start_exam(db, carol)
            self.assertEqual((again["attempt_id"], again["expires_at"]), (started["attempt_id"], started["expires_at"]))
            state = await exam_service.exam_state(db, carol)
            self.assertEqual((state["active"]["attempt_id"], len(state["active"]["questions"]), state["attempts_used"]), (started["attempt_id"], 10, 1))

            # 不满足资格的不能考；题库里已核对的题不够时也不能
            self.assertEqual(await self._code(exam_service.start_exam(db, await self._user(db, "offender"))), (403, "jury_not_eligible"))
            dave = await self._eligible_user(db, "dave")
            for case_id in self.questions[:4]:
                (await db.get(ReportCase, case_id)).golden_verified = False
            await db.commit()
            self.assertEqual(await self._code(exam_service.start_exam(db, dave)), (409, "exam_unavailable"))
            self.assertFalse((await exam_service.exam_state(db, dave))["available"])
            self.assertFalse((await jury_service.my_status(db, dave))["exam"]["available"])

    async def test_submit_rules_retry_interval_and_attempt_limit(self) -> None:
        async with self.sessions() as db:
            carol = await self._user(db, "carol")
            first = await exam_service.start_exam(db, carol)
            attempt_id = first["attempt_id"]
            for bad in (
                [{"question": 0, "vote": "l2"}],
                [{"question": 11, "vote": "l2"}],
                [{"question": 1, "vote": "l2"}, {"question": 1, "vote": "none"}],
                [{"question": 1, "vote": "abstain"}],
                [{"question": "1", "vote": "l2"}],
            ):
                self.assertEqual(await self._code(exam_service.submit_exam(db, carol, attempt_id, bad)), (422, "invalid_answers"))
            self.assertEqual((await self._code(exam_service.submit_exam(db, await self._user(db, "juror1"), attempt_id, [])))[0], 404)

            # 可以不答完就交：只答了 6 道（对 5 道），没答的按答错算
            result = await exam_service.submit_exam(db, carol, attempt_id, await self._exam_answers(db, attempt_id, correct=5, answered=6))
            ExamResult(**result)
            self.assertEqual((result["score"], result["total"], result["pass_score"], result["passed"], result["attempts_left"]), (5, 10, 8, False, 2))
            self.assertEqual([item["correct"] for item in result["results"]], [True] * 5 + [False] * 5)
            self.assertEqual([item["your_vote"] is None for item in result["results"]], [False] * 6 + [True] * 4)
            answer = result["results"][0]["answer"]
            self.assertEqual(set(answer), {"outcome", "level", "category_label", "explanation"})
            self.assertEqual(result["status"]["status"], "none")
            self.assertEqual(await self._code(exam_service.submit_exam(db, carol, attempt_id, [])), (409, "exam_closed"))

            # 没通过：24 小时后才能重考
            state = await exam_service.exam_state(db, carol)
            self.assertEqual((state["attempts_used"], state["last_result"]["score"], state["last_result"]["passed"], state["active"]), (1, 5, False, None))
            self.assertAlmostEqual((state["next_attempt_at"] - datetime.now(UTC)).total_seconds(), 24 * 3600, delta=60)
            self.assertEqual(result["next_attempt_at"], state["next_attempt_at"])
            with self.assertRaises(Exception) as cooldown:
                await exam_service.start_exam(db, carol)
            self.assertEqual((cooldown.exception.status_code, cooldown.exception.detail["code"]), (429, "exam_cooldown"))
            self.assertIsInstance(cooldown.exception.detail["next_attempt_at"], str)
            summary = (await jury_service.my_status(db, carol))["exam"]
            self.assertEqual((summary["passed"], summary["attempts_used"], summary["attempts_max"], summary["last_score"], summary["pass_score"], summary["total"]), (False, 1, 3, 5, 8, 10))

            async def age_last_attempt(hours: float) -> None:
                attempt = (await db.execute(select(JuryExamAttempt).order_by(JuryExamAttempt.id.desc()).limit(1))).scalar_one()
                attempt.submitted_at = datetime.now(UTC) - timedelta(hours=hours)
                await db.commit()

            # 第二场：截止后 30 秒的宽限之内交卷仍然算数
            await age_last_attempt(25)
            second = await exam_service.start_exam(db, carol)
            self.assertNotEqual(second["attempt_id"], attempt_id)
            attempt = await db.get(JuryExamAttempt, second["attempt_id"])
            attempt.expires_at = datetime.now(UTC) - timedelta(seconds=10)
            await db.commit()
            result = await exam_service.submit_exam(db, carol, second["attempt_id"], await self._exam_answers(db, second["attempt_id"], correct=7))
            self.assertEqual((result["score"], result["passed"], result["attempts_left"]), (7, False, 1))

            # 第三场：过了宽限才交 —— 409，这一场按 0 分记一次
            await age_last_attempt(25)
            third = await exam_service.start_exam(db, carol)
            attempt = await db.get(JuryExamAttempt, third["attempt_id"])
            attempt.expires_at = datetime.now(UTC) - timedelta(seconds=exam_service.EXAM_GRACE_SECONDS + 5)
            await db.commit()
            answers = await self._exam_answers(db, third["attempt_id"], correct=10)
            self.assertEqual(await self._code(exam_service.submit_exam(db, carol, third["attempt_id"], answers)), (409, "exam_closed"))
            await db.refresh(attempt)
            self.assertEqual((attempt.score, attempt.passed, attempt.timed_out), (0, False, True))
            self.assertIsNotNone(attempt.submitted_at)

            # 30 天内最多 3 次
            await age_last_attempt(25)
            state = await exam_service.exam_state(db, carol)
            self.assertEqual((state["attempts_used"], state["last_result"]["score"]), (3, 0))
            self.assertIsNotNone(state["next_attempt_at"])
            self.assertEqual(await self._code(exam_service.start_exam(db, carol)), (429, "exam_attempts_exhausted"))
            # 最早的一场滑出 30 天的窗口之后又能考了
            oldest = await db.get(JuryExamAttempt, attempt_id)
            oldest.started_at = datetime.now(UTC) - timedelta(days=31)
            await db.commit()
            self.assertIsNone((await exam_service.exam_state(db, carol))["next_attempt_at"])
            fourth = await exam_service.start_exam(db, carol)
            self.assertNotIn(fourth["attempt_id"], {attempt_id, second["attempt_id"], third["attempt_id"]})
            # 考试记录（后台）：只列有结果的场次
            items, total = await bank_service.list_exams(db)
            self.assertEqual((total, [(item["score"], item["passed"], item["timed_out"]) for item in items]), (3, [(0, False, True), (7, False, False), (5, False, False)]))
            AdminExamRead(**items[0])
            self.assertEqual((await bank_service.list_exams(db, passed=True))[1], 0)

    async def test_unsubmitted_exam_is_settled_lazily_as_a_zero(self) -> None:
        async with self.sessions() as db:
            carol = await self._user(db, "carol")
            started = await exam_service.start_exam(db, carol)
            attempt = await db.get(JuryExamAttempt, started["attempt_id"])
            attempt.expires_at = datetime.now(UTC) - timedelta(minutes=5)
            await db.commit()
            # 没人交卷：下次读状态时顺手结算
            state = await exam_service.exam_state(db, carol)
            self.assertEqual((state["active"], state["attempts_used"], state["last_result"]["score"], state["last_result"]["passed"]), (None, 1, 0, False))
            await db.refresh(attempt)
            self.assertTrue(attempt.timed_out)

    async def test_passing_without_auto_appoint_waits_for_an_application(self) -> None:
        async with self.sessions() as db:
            carol = await self._user(db, "carol")
            started = await exam_service.start_exam(db, carol)
            with self._events() as events:
                result = await exam_service.submit_exam(db, carol, started["attempt_id"], await self._exam_answers(db, started["attempt_id"], correct=8))
            self.assertEqual((result["score"], result["passed"], result["next_attempt_at"]), (8, True, None))
            # 开关关着：什么都不自动做，没有成员记录，也没有地方记风纪值
            status = JuryStatus(**result["status"])
            self.assertEqual((status.status, status.points, status.exam.passed, status.exam_required), ("none", 0, True, False))
            self.assertIsNotNone(status.exam.passed_at)
            self.assertIsNone(await self._member(db, "carol"))
            # 「考试一次通过」的成就不依赖委员身份
            self.assertIn(("jury.achievement.earned", {carol.id}, {"key": "exam_first_try", "label": "一次过关"}), events)
            self.assertEqual(await self._code(exam_service.start_exam(db, carol)), (409, "exam_passed"))

            # 接着申请：进审批队列，后台能看到考试成绩
            member = await jury_service.apply(db, carol, "想帮忙")
            self.assertEqual(member.status, "applied")
            items, _total = await jury_service.list_members(db, "applied", 10, 0)
            read = JuryMemberRead(**items[0])
            self.assertEqual((read.user_id, read.exam_score, read.points), (carol.id, 8, 0))
            self.assertIsNotNone(read.exam_passed_at)
            # 管理员批准时补上「通过考试」的风纪值，只给一次
            await jury_service.review_member(db, carol.id, action="approve", actor_id=self.ids["admin"], note=None)
            await jury_service.review_member(db, carol.id, action="approve", actor_id=self.ids["admin"], note=None)
            self.assertEqual(((await self._member(db, "carol")).points, await self._kinds(db, "carol")), (20, ["exam_passed"]))

            # 通过 90 天后失效；但已经被任命的不用重考
            attempt = await db.get(JuryExamAttempt, started["attempt_id"])
            attempt.submitted_at = datetime.now(UTC) - timedelta(days=91)
            await db.commit()
            self.assertTrue((await jury_service.my_status(db, carol))["exam"]["passed"])
            await jury_service.review_member(db, carol.id, action="retire", actor_id=self.ids["admin"], note=None)
            self.assertFalse((await jury_service.my_status(db, carol))["exam"]["passed"])

    async def test_passing_with_auto_appoint_makes_a_trainee_at_once(self) -> None:
        async with self.sessions() as db:
            await jury_settings.update_switches(db, self.ids["admin"], {"auto_appoint": True})
            carol = await self._user(db, "carol")
            started = await exam_service.start_exam(db, carol)
            with self._events() as events:
                result = await exam_service.submit_exam(db, carol, started["attempt_id"], await self._exam_answers(db, started["attempt_id"], correct=10))
            status = JuryStatus(**result["status"])
            self.assertEqual((result["passed"], status.status, status.tier, status.points), (True, "active", "trainee", 20))
            self.assertEqual(status.tier_progress.next_tier, "regular")
            member = await self._member(db, "carol")
            self.assertEqual((member.status, member.tier, member.reviewed_by_id), ("active", "trainee", None))
            self.assertIsNotNone(member.serving_since)
            self.assertGreater(penalty_service.aware(member.term_ends_at), datetime.now(UTC) + timedelta(days=29))
            # 和管理员批准走同一条路：写审计日志，actor 为空
            log = (await db.execute(select(AdminAuditLog).where(AdminAuditLog.action == "jury.member.auto_appoint"))).scalar_one()
            self.assertEqual((log.actor_id, log.target_id), (None, carol.id))
            self.assertIn("jury.status.changed", [event for event, users, _data in events if users == {carol.id}])
            self.assertIn({"delta": 20, "total": 20, "kind": "exam_passed", "label": "通过入门考试", "case_id": None}, [data for event, _users, data in events if event == "jury.points.changed"])
            # 之后重复调用不会重复给分；已经是委员了，再申请是 409
            await jury_service.grant_exam_points(db, member)
            await db.commit()
            self.assertEqual(member.points, 20)
            self.assertEqual((await self._code(jury_service.apply(db, carol, None)))[0], 409)

            # 被管理员拒绝过的人通过考试也不会被自动任命，仍然进审批队列
            dave = await self._eligible_user(db, "dave")
            db.add(JuryMember(user_id=dave.id, status="rejected"))
            await db.commit()
            started = await exam_service.start_exam(db, dave)
            result = await exam_service.submit_exam(db, dave, started["attempt_id"], await self._exam_answers(db, started["attempt_id"], correct=9))
            self.assertEqual((result["passed"], result["status"]["status"]), (True, "rejected"))
            self.assertEqual((await jury_service.apply(db, dave, None)).status, "applied")

    async def test_apply_follows_the_exam_switches(self) -> None:
        async with self.sessions() as db:
            carol = await self._user(db, "carol")
            dave = await self._eligible_user(db, "dave")
            erin = await self._eligible_user(db, "erin")
            admin_id = self.ids["admin"]
            # 没考试、exam_required 关：和现在一样进审批队列，后台显示未考试
            self.assertEqual((await jury_service.apply(db, carol, "旧客户端的申请")).status, "applied")
            items, _total = await jury_service.list_members(db, "applied", 10, 0)
            self.assertEqual((items[0]["user_id"], items[0]["exam_score"], items[0]["exam_passed_at"]), (carol.id, None, None))
            # 没考试、exam_required 开：403，也不留下申请记录
            await jury_settings.update_switches(db, admin_id, {"exam_required": True})
            self.assertTrue((await jury_service.my_status(db, dave))["exam_required"])
            self.assertEqual(await self._code(jury_service.apply(db, dave, None)), (403, "exam_required"))
            await db.commit()
            self.assertIsNone(await self._member(db, "dave"))
            # 已通过考试、auto_appoint 关：applied
            for user in (dave, erin):
                started = await exam_service.start_exam(db, user)
                await exam_service.submit_exam(db, user, started["attempt_id"], await self._exam_answers(db, started["attempt_id"], correct=9))
            self.assertEqual((await jury_service.apply(db, dave, None)).status, "applied")
            # 已通过考试、auto_appoint 开：直接成为见习委员
            await jury_settings.update_switches(db, admin_id, {"auto_appoint": True})
            member = await jury_service.apply(db, erin, "考过了")
            self.assertEqual((member.status, member.tier, member.statement, member.points), ("active", "trainee", "考过了", 20))
            status = await jury_service.my_status(db, erin)
            self.assertEqual((status["status"], status["tier"]), ("active", "trainee"))
            # 已经在审批队列里的人（考试之前就申请了）通过考试时直接任命
            started = await exam_service.start_exam(db, carol)
            result = await exam_service.submit_exam(db, carol, started["attempt_id"], await self._exam_answers(db, started["attempt_id"], correct=8))
            self.assertEqual(result["status"]["status"], "active")


# ---------------------------------------------------------------- 练习场


class PracticeTests(Phase2TestCase):
    async def test_practice_serves_unseen_questions_first_then_the_oldest(self) -> None:
        # 这里看的是出题顺序和进度：让已核对的题全部落在练习场那一份里。两份题怎么切见 PoolTests。
        splitter = patch.object(bank_service, "PRACTICE_EVERY", 1)
        splitter.start()
        self.addCleanup(splitter.stop)
        async with self.sessions() as db:
            carol = await self._eligible_user(db, "carol")
            first = await self._question(db)
            second = await self._question(db, upheld=False, golden_explanation="只是口头禅")
            # 未核对的题也出
            third = await self._question(db, verified=False, source="legacy")
            own = await self._question(db, reported_user_id=carol.id)
            outside = await self._question(db, is_golden=False, verified=False, source=None)

            async def next_question() -> dict:
                page = await exam_service.practice_next(db, carol)
                PracticeNext(**page)
                return page

            page = await next_question()
            self.assertEqual((page["question"]["question_id"], page["question"]["verified"], page["question"]["answered_before"]), (first.id, True, False))
            self.assertEqual((page["left_today"], page["daily_limit"], page["stats"], page["round"]), (20, 20, {"answered": 0, "correct": 0}, {"total": 3, "done": 0}))
            self.assertEqual(set(page["question"]), {"question_id", "verified", "answered_before", "kind", "reasons", "report_count", "messages", "post"})
            # 没作答之前再取，还是同一道
            self.assertEqual((await next_question())["question"]["question_id"], first.id)

            result = await exam_service.practice_answer(db, carol, first.id, "l3")
            PracticeAnswerResult(**result)
            self.assertEqual((result["correct"], result["answer"], result["left_today"], result["stats"]), (
                True, {"outcome": "violation", "level": 2, "category_label": "辱骂骚扰", "explanation": None}, 19, {"answered": 1, "correct": 1},
            ))
            self.assertEqual((await next_question())["question"]["question_id"], second.id)
            result = await exam_service.practice_answer(db, carol, second.id, "l1")
            self.assertEqual((result["correct"], result["answer"]["outcome"], result["answer"]["level"], result["answer"]["explanation"]), (False, "clean", None, "只是口头禅"))
            page = await next_question()
            self.assertEqual((page["question"]["question_id"], page["question"]["verified"], page["round"]), (third.id, False, {"total": 3, "done": 2}))
            await exam_service.practice_answer(db, carol, third.id, "l2")
            # 都做过了：按「最久没做」的顺序重出
            page = await next_question()
            self.assertEqual((page["question"]["question_id"], page["question"]["answered_before"], page["round"]), (first.id, True, {"total": 3, "done": 3}))
            await exam_service.practice_answer(db, carol, first.id, "none")
            self.assertEqual((await next_question())["question"]["question_id"], second.id)

            # 自己是当事人的题、不在题库里的案件都答不了
            for case in (own, outside):
                self.assertEqual(await self._code(exam_service.practice_answer(db, carol, case.id, "l2")), (404, "question_not_found"))
            # 每日上限
            with patch.object(exam_service, "PRACTICE_DAILY_LIMIT", 4):
                page = await next_question()
                self.assertEqual((page["question"], page["left_today"], page["daily_limit"], page["stats"]), (None, 0, 4, {"answered": 4, "correct": 2}))
                self.assertEqual(await self._code(exam_service.practice_answer(db, carol, second.id, "none")), (429, "practice_daily_limit"))
            # 练习不计入一致率，不给风纪值，也不产生成员记录
            self.assertIsNone(await self._member(db, "carol"))
            self.assertEqual((await db.execute(select(func.count()).select_from(JuryPointLog))).scalar_one(), 0)
            self.assertEqual((await db.execute(select(func.count()).select_from(JuryPracticeAnswer))).scalar_one(), 4)
            # 后台的题目统计把练习场的作答算进去
            listed, _total = await bank_service.list_questions(db, query=str(first.id))
            self.assertEqual((listed[0]["answered"], listed[0]["correct_rate"]), (2, 0.5))

    async def test_practice_is_open_to_eligible_users_and_past_jurors(self) -> None:
        async with self.sessions() as db:
            await self._question(db, verified=False)
            # 不满足四项资格、也没当过委员
            self.assertEqual(await self._code(exam_service.practice_next(db, await self._user(db, "offender"))), (403, "jury_not_eligible"))
            applied = await self._user(db, "alice")
            db.add(JuryMember(user_id=applied.id, status="applied"))
            await db.commit()
            self.assertEqual(await self._code(exam_service.practice_next(db, applied)), (403, "jury_not_eligible"))
            # 当过委员的（哪怕现在被停职）可以；在任委员受处罚期间不能评审，但练习不拦
            juror = await self._user(db, "juror1")
            await jury_service.review_member(db, juror.id, action="suspend", actor_id=self.ids["admin"], note="测试")
            self.assertIsNotNone((await exam_service.practice_next(db, juror))["question"])
            muted = await self._user(db, "juror2")
            muted.muted_until = datetime.now(UTC) + timedelta(hours=2)
            await db.commit()
            self.assertEqual((await jury_service.my_status(db, muted))["restriction"]["reason"], "muted")
            page = await exam_service.practice_next(db, muted)
            self.assertIsNotNone(page["question"])
            self.assertTrue((await exam_service.practice_answer(db, muted, page["question"]["question_id"], "l2"))["correct"])
            # 题库为空
            for case in (await db.execute(select(ReportCase))).scalars().all():
                case.is_golden = False
            await db.commit()
            page = await exam_service.practice_next(db, juror)
            self.assertEqual((page["question"], page["round"]), (None, {"total": 0, "done": 0}))


# ---------------------------------------------------------------- 练习题和计分题分开


class PoolTests(Phase2TestCase):
    async def _forced_quality_check(self, db, name: str) -> int | None:
        """强制让这位委员的下一件出质控题并答掉，返回那道题的案件编号；出不了质控题返回 None。"""
        member = await self._member(db, name)
        member.votes_since_quality = jury_service.QUALITY_MAX_GAP + 1
        await db.commit()
        user = await self._user(db, name)
        view = await jury_service.next_case(db, user)
        if view is None:
            return None
        case_id = await self._real(db, view["case_id"])
        # 答对：连着答错会被系统暂停资格
        upheld = bank_service.question_upheld(await db.get(ReportCase, case_id))
        result = await jury_service.cast_vote(db, user, view["case_id"], "l2" if upheld else "none", None)
        self.assertTrue(result["case"]["is_quality_check"])
        return case_id

    async def test_practice_questions_never_show_up_in_exams_or_quality_checks(self) -> None:
        with patch.object(bank_service, "PRACTICE_EVERY", 3):
            async with self.sessions() as db:
                verified = await self._questions(db, 12)
                unverified = [await self._question(db, verified=False, source="legacy") for _ in range(3)]
                # 已核对的题按编号固定切出一份只给练习场；未核对的全部只给练习场
                practice = {case.id for case in verified if case.id % 3 == 0} | {case.id for case in unverified}
                scored = {case.id for case in verified} - practice
                self.assertEqual((len(practice), len(scored)), (7, 8))
                self.assertEqual({case.id for case in [*verified, *unverified] if bank_service.question_pool(case) == "practice"}, practice)
                self.assertEqual({case.id for case in verified if bank_service.question_pool(case) == "scored"}, scored)
                outside = await self._question(db, is_golden=False, verified=False, source=None)
                self.assertIsNone(bank_service.question_pool(outside))
                self.assertEqual(await bank_service.verified_count(db), len(scored))

                # 练习场：一轮下来出的正好是练习那一份，进度也按这一份算
                carol = await self._eligible_user(db, "carol")
                served: list[int] = []
                for index in range(len(practice)):
                    page = await exam_service.practice_next(db, carol)
                    self.assertEqual(page["round"], {"total": len(practice), "done": index})
                    served.append(page["question"]["question_id"])
                    await exam_service.practice_answer(db, carol, served[-1], "l2")
                self.assertEqual(set(served), practice)
                self.assertEqual((await exam_service.practice_next(db, carol))["round"], {"total": len(practice), "done": len(practice)})
                # 计分用的题拿题号来「练习」问不出答案
                for case_id in sorted(scored)[:3]:
                    self.assertEqual(await self._code(exam_service.practice_answer(db, carol, case_id, "l2")), (404, "question_not_found"))

                # 入门考试：只从计分那一份里抽；这一份不够一场时不能考
                self.assertFalse((await exam_service.exam_state(db, carol))["available"])
                self.assertEqual(await self._code(exam_service.start_exam(db, carol)), (409, "exam_unavailable"))
                with patch.object(exam_service, "EXAM_TOTAL", len(scored)), patch.object(exam_service, "EXAM_PASS_SCORE", 1):
                    self.assertTrue((await exam_service.exam_state(db, carol))["available"])
                    started = await exam_service.start_exam(db, carol)
                attempt = await db.get(JuryExamAttempt, started["attempt_id"])
                self.assertEqual(set(json.loads(attempt.question_ids)), scored)

                # 评审中的质控题：同样只出计分那一份
                asked = set()
                for _ in range(len(scored)):
                    asked.add(await self._forced_quality_check(db, "juror1"))
                self.assertEqual(asked, scored)
                self.assertIsNone(await self._forced_quality_check(db, "juror1"))

                # 切分的份数是配置项：换一个数，同一道题落在哪一份跟着变
                sample = next(case for case in verified if case.id % 3 == 0 and case.id % 4 != 0)
                self.assertEqual(bank_service.question_pool(sample), "practice")
                with patch.object(bank_service, "PRACTICE_EVERY", 4):
                    self.assertEqual(bank_service.question_pool(sample), "scored")

    async def test_quality_checks_skip_questions_already_seen_in_the_exam_or_in_practice(self) -> None:
        async with self.sessions() as db:
            examined, practiced, fresh = await self._questions(db, 3)
            juror = await self._user(db, "juror1")
            now = datetime.now(UTC)
            # juror1 入门考试里考到过第一道（考完会公布答案）；第二道还没核对的时候他在练习场做过
            db.add(
                JuryExamAttempt(
                    user_id=juror.id, started_at=now - timedelta(days=5), expires_at=now - timedelta(days=5), submitted_at=now - timedelta(days=5),
                    question_ids=json.dumps([examined.id]), total=1, score=1, passed=True,
                )
            )
            db.add(JuryPracticeAnswer(user_id=juror.id, case_id=practiced.id, vote="l2", correct=True, created_at=now - timedelta(days=3)))
            await db.commit()
            self.assertEqual(await jury_service._exam_question_ids(db, juror.id), {examined.id})

            self.assertEqual(await self._forced_quality_check(db, "juror1"), fresh.id)
            # 剩下两道他都见过答案：该出质控题也出不了
            self.assertIsNone(await self._forced_quality_check(db, "juror1"))
            # 别的委员不受影响，三道都会出
            self.assertEqual({await self._forced_quality_check(db, "juror2") for _ in range(3)}, {examined.id, practiced.id, fresh.id})

            # 考试抽题也把练习场做过的排到最后：题够的时候不会抽到
            carol = await self._eligible_user(db, "carol")
            db.add(JuryPracticeAnswer(user_id=carol.id, case_id=practiced.id, vote="l2", correct=True, created_at=now))
            await db.commit()
            with patch.object(exam_service, "EXAM_TOTAL", 2), patch.object(exam_service, "EXAM_PASS_SCORE", 1):
                for _ in range(5):
                    drawn = await exam_service._draw_questions(db, carol.id, [])
                    self.assertEqual(set(drawn), {examined.id, fresh.id})

    async def test_practice_next_runs_a_fixed_number_of_queries(self) -> None:
        async with self.sessions() as db:
            carol = await self._eligible_user(db, "carol")

            async def add_questions(count: int) -> None:
                for index in range(count):
                    # 一部分题出自大型公开群：这种案件判断回避要看快照里的发言人
                    await self._question(db, verified=False, source="legacy", conversation_id=self.plaza_id if index % 2 else None)

            async def measure() -> tuple[int, dict]:
                jury_settings.reset_cache()
                with self._queries() as statements:
                    page = await exam_service.practice_next(db, carol)
                return len(statements), page

            with patch.object(jury_service, "LARGE_GROUP_MEMBERS", 5):
                await add_questions(4)
                few, page = await measure()
                self.assertEqual(page["round"], {"total": 4, "done": 0})
                await add_questions(60)
                many, page = await measure()
                self.assertEqual(page["round"], {"total": 64, "done": 0})
                self.assertIsNotNone(page["question"])
            # 原来每道题要查三四次（回避关系、敏感类别），64 道题是两百多条
            self.assertEqual(many, few)
            self.assertLessEqual(many, 25)

    async def test_sensitive_and_conflicting_questions_are_still_filtered_in_batch(self) -> None:
        async with self.sessions() as db:
            carol = await self._eligible_user(db, "carol")
            plain = await self._question(db, verified=False)
            own = await self._question(db, verified=False, reported_user_id=carol.id)
            machine = await self._question(db, verified=False, machine_category="sexual")
            # 举报理由是色情低俗的：靠批量查出来的举报理由判断
            report = await self._report(db, "alice", "spam", "色情低俗")
            reported = await db.get(ReportCase, report.case_id)
            reported.status, reported.is_golden, reported.jury_block_reason, reported.closed_at = "resolved", True, None, datetime.now(UTC)
            await db.commit()
            self.assertEqual(await case_service.sensitive_case_ids(db, [plain, own, machine, reported]), {machine.id, reported.id})
            self.assertTrue(await case_service.is_sensitive(db, reported))
            self.assertFalse(await case_service.is_sensitive(db, plain))
            page = await exam_service.practice_next(db, carol)
            self.assertEqual((page["question"]["question_id"], page["round"]), (plain.id, {"total": 1, "done": 0}))


# ---------------------------------------------------------------- 滚动上限的恢复时间


class RollingLimitTests(Phase2TestCase):
    async def _detail(self, awaitable) -> dict:
        with self.assertRaises(HTTPException) as caught:
            await awaitable
        self.assertEqual(caught.exception.status_code, 429)
        return caught.exception.detail

    async def test_review_and_recusal_limits_say_when_the_next_slot_frees_up(self) -> None:
        async with self.sessions() as db:
            juror = await self._user(db, "juror1")
            status = await jury_service.my_status(db, juror)
            # 最近 24 小时没投过票、没回避过：没有恢复时间
            self.assertEqual((status["voted_today"], status["daily_resets_at"], status["recuse_left_today"], status["recuse_resets_at"]), (0, None, 5, None))
            # 不是委员的人两个都是 null
            outsider = await jury_service.my_status(db, await self._user(db, "alice"))
            self.assertEqual((outsider["daily_resets_at"], outsider["recuse_resets_at"]), (None, None))

            now = datetime.now(UTC)
            ages = {"stale": timedelta(hours=25), "oldest": timedelta(hours=23), "newest": timedelta(hours=1)}
            for age in ages.values():
                case = await self._question(db, is_golden=False, verified=False, source=None)
                db.add(JuryVote(case_id=case.id, juror_id=juror.id, vote="l2", assigned_at=now - age, voted_at=now - age))
            recused_case = await self._question(db, is_golden=False, verified=False, source=None)
            db.add(JuryVote(case_id=recused_case.id, juror_id=juror.id, vote="recuse", assigned_at=now - timedelta(hours=20), recused_at=now - timedelta(hours=20)))
            await db.commit()

            status = await jury_service.my_status(db, juror)
            # 下一个名额 = 窗口里最早那一票的时间 + 24 小时；25 小时前的那张已经不占名额
            self.assertEqual((status["voted_today"], status["recuse_left_today"]), (2, 4))
            self.assertEqual(status["daily_resets_at"], now - ages["oldest"] + timedelta(hours=24))
            self.assertEqual(status["recuse_resets_at"], now + timedelta(hours=4))
            read = JuryStatus(**status)
            self.assertEqual((read.daily_resets_at, read.recuse_resets_at), (status["daily_resets_at"], status["recuse_resets_at"]))

            await self._open_case(db)
            with patch.object(jury_service, "DAILY_VOTE_LIMIT", 2):
                detail = await self._detail(jury_service.next_case(db, juror))
            self.assertEqual((detail["code"], detail["resets_at"]), ("jury_daily_limit", jury_service._iso(status["daily_resets_at"])))
            self.assertTrue(detail["resets_at"].endswith("Z"))

            view = await jury_service.next_case(db, juror)
            with patch.object(jury_service, "RECUSE_DAILY_LIMIT", 1):
                detail = await self._detail(jury_service.cast_vote(db, juror, view["case_id"], "recuse", None))
            self.assertEqual((detail["code"], detail["resets_at"]), ("recuse_limit", jury_service._iso(status["recuse_resets_at"])))
            # 没超限时照常回避；恢复时间仍然看窗口里最早的那一次
            self.assertIsNone((await jury_service.cast_vote(db, juror, view["case_id"], "recuse", None))["case"])
            self.assertEqual((await jury_service.my_status(db, juror))["recuse_resets_at"], status["recuse_resets_at"])

    async def test_practice_limit_says_when_the_next_slot_frees_up(self) -> None:
        async with self.sessions() as db:
            carol = await self._eligible_user(db, "carol")
            first, second = [await self._question(db, verified=False) for _ in range(2)]
            page = await exam_service.practice_next(db, carol)
            self.assertEqual((page["left_today"], page["resets_at"]), (20, None))
            PracticeNext(**page)

            now = datetime.now(UTC)
            db.add(JuryPracticeAnswer(user_id=carol.id, case_id=first.id, vote="l2", correct=True, created_at=now - timedelta(hours=30)))
            db.add(JuryPracticeAnswer(user_id=carol.id, case_id=first.id, vote="l2", correct=True, created_at=now - timedelta(hours=10)))
            await db.commit()
            page = await exam_service.practice_next(db, carol)
            resets_at = now + timedelta(hours=14)
            self.assertEqual((page["left_today"], page["resets_at"]), (19, resets_at))

            result = await exam_service.practice_answer(db, carol, second.id, "l2")
            self.assertEqual((result["left_today"], result["resets_at"]), (18, resets_at))
            self.assertEqual(PracticeAnswerResult(**result).resets_at, resets_at)
            with patch.object(exam_service, "PRACTICE_DAILY_LIMIT", 2):
                page = await exam_service.practice_next(db, carol)
                self.assertEqual((page["question"], page["left_today"], page["resets_at"]), (None, 0, resets_at))
                detail = await self._detail(exam_service.practice_answer(db, carol, second.id, "l2"))
            self.assertEqual((detail["code"], detail["resets_at"]), ("practice_daily_limit", jury_service._iso(resets_at)))


# ---------------------------------------------------------------- 投票的变化


class VotingV2Tests(Phase2TestCase):
    async def test_recusal_frees_the_seat_and_counts_for_nothing(self) -> None:
        async with self.sessions() as db:
            case = await self._open_case(db)
            jurors = [await self._user(db, f"juror{index}") for index in range(1, 7)]
            for juror in jurors[:5]:
                self.assertEqual(await self._real(db, (await jury_service.next_case(db, juror))["case_id"]), case.id)
            # 5 个席位都被占着
            self.assertIsNone(await jury_service.next_case(db, jurors[5]))
            result = await jury_service.cast_vote(db, jurors[0], await self._rid(db, jurors[0], case.id), "recuse", "我认识这个人")
            JuryVoteResult(**result)
            self.assertEqual(
                (result["case_id"], result["settled"], result["case"], result["reward"]),
                (await self._rid(db, jurors[0], case.id), False, None, {"points": 0, "total": 0, "items": []}),
            )
            # 席位让出来了，案件改派给别人；回避的人不会再分到它
            self.assertEqual(await self._real(db, (await jury_service.next_case(db, jurors[5]))["case_id"]), case.id)
            self.assertIsNone(await jury_service.next_case(db, jurors[0]))
            # 不计入总票数和当日评审数
            member = await self._member(db, "juror1")
            status = await jury_service.my_status(db, jurors[0])
            self.assertEqual((member.votes_total, status["voted_today"], status["recuse_left_today"], status["stats"]["votes_total"]), (0, 0, 4, 0))
            self.assertEqual((await jury_service.my_stats(db, jurors[0].id))["trend"][-1]["votes"], 0)
            # 回避之后不能看这个案件的详情和观点区，也不能反悔再投
            self.assertEqual(await self._code(jury_service.case_detail(db, jurors[0].id, await self._rid(db, jurors[0].id, case.id))), (403, "not_voted"))
            self.assertEqual(await self._code(opinion_service.list_opinions(db, jurors[0], await self._rid(db, jurors[0], case.id))), (403, "not_voted"))
            self.assertEqual((await self._code(jury_service.cast_vote(db, jurors[0], await self._rid(db, jurors[0], case.id), "l2", None)))[0], 409)
            self.assertEqual((await self._code(jury_service.cast_vote(db, jurors[0], await self._rid(db, jurors[0], case.id), "recuse", None)))[0], 409)

            # 不计入表决、「到 7 票转出」和一致率：其余的人照常定案
            for juror in jurors[1:4]:
                result = await jury_service.cast_vote(db, juror, await self._rid(db, juror, case.id), "l2", None)
            self.assertTrue(result["settled"])
            detail = await jury_service.case_detail(db, jurors[1].id, await self._rid(db, jurors[1].id, case.id))
            self.assertEqual((detail["tally"], detail["votes_cast"]), ({"none": 0, "l1": 0, "l2": 3, "l3": 0, "abstain": 0, "need_context": 0}, 3))
            await db.refresh(member)
            self.assertEqual((member.votes_settled, member.points), (0, 0))
            row = (await db.execute(select(JuryVote).where(JuryVote.case_id == case.id, JuryVote.juror_id == jurors[0].id))).scalar_one()
            self.assertEqual((row.vote, row.voted_at, row.agreed, row.alias, row.note), ("recuse", None, None, None, "我认识这个人"))
            self.assertIsNotNone(row.recused_at)

            # 即使打开了「公示栏里的案件的观点区对在任委员只读」，回避过的人也进不去；没回避关系的别人可以
            await jury_settings.update_switches(db, self.ids["admin"], {"opinions_open_to_jurors": True})
            await self._age(db, case.id, case_service.PUBLISH_DELAY_HOURS + 1)
            published = await overview_service.published_case(db, case.id)
            self.assertEqual(await self._code(opinion_service.list_opinions(db, jurors[0], await self._rid(db, jurors[0], case.id))), (403, "not_voted"))
            self.assertEqual(await self._code(opinion_service.list_public_opinions(db, jurors[0], published)), (403, "opinions_closed"))
            self.assertEqual((await opinion_service.list_public_opinions(db, await self._user(db, "lurker"), published))["total"], 0)
            # 我的评审里列出回避的记录，但点不进去、不透露结论
            items, total = await jury_service.list_votes(db, jurors[0].id)
            item = JuryVoteItem(**items[0])
            self.assertEqual((total, item.vote, item.can_view, item.agreed, item.outcome, item.alias), (1, "recuse", False, None, None, None))
            # 回避的记录不给公开编号（案件此刻在公示栏里），投了票的人有
            self.assertEqual((item.public_case_id, (await jury_service.list_votes(db, jurors[1].id))[0][0]["public_case_id"]), (None, case.id))
            self.assertIsNotNone(item.voted_at)
            self.assertEqual((await jury_service.list_votes(db, jurors[0].id, votes=["recuse"]))[1], 1)
            self.assertEqual((await jury_service.list_votes(db, jurors[0].id, votes=["l2"]))[1], 0)
            self.assertTrue((await jury_service.list_votes(db, jurors[1].id))[0][0]["can_view"])
            # 旧的 /jury/history 不受影响
            self.assertEqual(await jury_service.history(db, jurors[0].id), [])

            # 每天上限
            with patch.object(jury_service, "RECUSE_DAILY_LIMIT", 2):
                second = await self._open_case(db)
                third = await self._open_case(db)
                self.assertIsNone((await self._cast(db, "juror1", second.id, "recuse"))["case"])
                self.assertEqual((await jury_service.my_status(db, jurors[0]))["recuse_left_today"], 0)
                self.assertEqual(await self._code(self._cast(db, "juror1", third.id, "recuse")), (429, "recuse_limit"))
                # 超限的那次没有被记成回避，还可以正常投
                self.assertFalse((await self._cast(db, "juror1", third.id, "l2"))["settled"])

    async def test_recusing_a_quality_check_brings_another_one(self) -> None:
        async with self.sessions() as db:
            questions = {(await self._question(db)).id, (await self._question(db, upheld=False)).id}
            normal = await self._open_case(db)
            juror = await self._user(db, "juror1")
            member = await self._member(db, "juror1")
            member.votes_since_quality = jury_service.QUALITY_MAX_GAP + 1
            await db.commit()
            first = await self._real(db, (await jury_service.next_case(db, juror))["case_id"])
            self.assertIn(first, questions)
            self.assertIsNone((await jury_service.cast_vote(db, juror, await self._rid(db, juror, first), "recuse", None))["case"])
            await db.refresh(member)
            self.assertEqual((member.force_quality, member.golden_total, member.votes_total), (True, 0, 0))
            # 即使按随机位置这次不该出质控题，也要补一道
            member.votes_since_quality = 0
            await db.commit()
            with patch.object(jury_service, "_quality_roll", return_value=0.99):
                second = await self._real(db, (await jury_service.next_case(db, juror))["case_id"])
            self.assertEqual({first, second}, questions)
            result = await jury_service.cast_vote(db, juror, await self._rid(db, juror, second), "l2", None)
            self.assertTrue(result["case"]["is_quality_check"])
            await db.refresh(member)
            self.assertEqual((member.force_quality, member.golden_total, member.votes_since_quality), (False, 1, 0))
            with patch.object(jury_service, "_quality_roll", return_value=0.99):
                self.assertEqual(await self._real(db, (await jury_service.next_case(db, juror))["case_id"]), normal.id)

    async def test_need_context_votes_escalate_the_case(self) -> None:
        async with self.sessions() as db:
            case = await self._open_case(db)
            self.assertFalse((await self._cast(db, "juror1", case.id, "l2"))["settled"])
            with self._events() as events:
                first = await self._cast(db, "juror2", case.id, "need_context", "前面肯定还有别的话")
                await db.refresh(case)
                self.assertEqual((case.queue, case.jury_outcome), ("jury", None))
                second = await self._cast(db, "juror3", case.id, "need_context")
            self.assertEqual((first["settled"], second["settled"], second["case"]["stage"]), (False, False, "admin"))
            # 累计 2 张：转出（现在转管理员），记下原因
            await db.refresh(case)
            self.assertEqual((case.status, case.queue, case.jury_outcome), ("open", "admin", "need_context"))
            self.assertIsNotNone(case.escalated_at)
            self.assertIn("jury.queue.changed", [event for event, _users, _data in events])
            # 计入总票数和当日评审数，有代号、可以进观点区；不占席位、不计入表决
            member = await self._member(db, "juror2")
            juror2 = await self._user(db, "juror2")
            self.assertEqual((member.votes_total, (await jury_service.my_status(db, juror2))["voted_today"]), (1, 1))
            self.assertEqual((first["case"]["my_vote"]["alias"], first["case"]["opinion_count"], first["reward"]), ("委员 B", 1, {"points": 0, "total": 0, "items": []}))
            self.assertEqual((await jury_service.vote_tallies(db, [case.id]))[case.id], {"none": 0, "l1": 0, "l2": 1, "l3": 0, "abstain": 0, "need_context": 2})
            self.assertEqual(second["case"]["votes_cast"], 1)
            items, _total = await jury_service.list_votes(db, juror2.id, votes=["need_context"])
            self.assertEqual((items[0]["vote"], items[0]["can_view"]), ("need_context", True))

            # 管理员定案后：投了违规的计入一致率，「上下文不足」不计
            await case_service.resolve_case(db, case.id, source="admin", actor_id=self.ids["admin"], upheld=True, category="harassment")
            await db.refresh(member)
            self.assertEqual((member.votes_settled, member.points), (0, 0))
            detail = await jury_service.case_detail(db, juror2.id, await self._rid(db, juror2.id, case.id))
            self.assertEqual((detail["my_vote"]["agreed"], detail["tally"]["need_context"], detail["votes_cast"]), (None, 2, 1))
            self.assertEqual((await self._member(db, "juror1")).votes_settled, 1)
            # 一张「上下文不足」不够转出
            other = await self._open_case(db)
            await self._cast(db, "juror1", other.id, "need_context")
            await self._cast(db, "juror2", other.id, "abstain")
            await db.refresh(other)
            self.assertEqual((other.queue, other.jury_outcome), ("jury", None))

    async def test_violation_votes_can_carry_a_category(self) -> None:
        async with self.sessions() as db:
            majority = await self._open_case(db)
            view = await jury_service.next_case(db, await self._user(db, "juror1"))
            self.assertEqual((await self._real(db, view["case_id"]), view["suggested_category"]), (majority.id, "harassment"))
            JuryCaseView(**view)
            for bad in ("sexual", "vulgar", "admin_action", "nonsense"):
                self.assertEqual(await self._code(self._cast(db, "juror1", majority.id, "l2", category=bad)), (422, "invalid_category"))
            first = await self._cast(db, "juror1", majority.id, "l2", category="advertising")
            self.assertEqual(first["case"]["my_vote"]["category"], "advertising")
            await self._cast(db, "juror2", majority.id, "l1", category="harassment")
            self.assertTrue((await self._cast(db, "juror3", majority.id, "l2", category="advertising"))["settled"])
            await db.refresh(majority)
            # 类别取带类别的违规票里的多数；等级照旧是较低中位数
            self.assertEqual((majority.status, majority.verdict_category, majority.verdict_level), ("resolved", "advertising", 2))

            # 平票：退回按举报理由映射的默认值
            tied = await self._open_case(db)
            await self._cast(db, "juror1", tied.id, "l2", category="advertising")
            await self._cast(db, "juror2", tied.id, "l2", category="fraud")
            await self._cast(db, "juror3", tied.id, "l2")
            await db.refresh(tied)
            self.assertEqual((tied.status, tied.verdict_category), ("resolved", "harassment"))
            # 都没带（旧客户端）：和以前一样用默认值；不违规的票带了类别也不记
            plain = await self._open_case(db, primary_reason="垃圾广告")
            for juror in ("juror1", "juror2", "juror3"):
                await self._cast(db, juror, plain.id, "l2")
            await db.refresh(plain)
            self.assertEqual(plain.verdict_category, "advertising")
            clean = await self._open_case(db)
            await self._cast(db, "juror1", clean.id, "none", category="fraud")
            row = (await db.execute(select(JuryVote).where(JuryVote.case_id == clean.id))).scalar_one()
            self.assertIsNone(row.category)

    async def test_severe_verdicts_need_five_votes(self) -> None:
        async with self.sessions() as db:
            case = await self._open_case(db)
            for juror in ("juror1", "juror2", "juror3", "juror4"):
                result = await self._cast(db, juror, case.id, "l3")
                # 3 票、4 票一致都还不定案
                self.assertEqual((result["settled"], result["case"]["stage"], result["case"]["verdict"]), (False, "jury", None))
            # 席位规则没变（5 个）：第 5 个人还能分到
            self.assertEqual(await self._real(db, (await jury_service.next_case(db, await self._user(db, "juror5")))["case_id"]), case.id)
            self.assertTrue((await self._cast(db, "juror5", case.id, "l3"))["settled"])
            await db.refresh(case)
            self.assertEqual((case.status, case.verdict_level, case.jury_outcome), ("resolved", 3, "consensus"))

            # 等级不到 l3 的不受影响：3 票一致照旧定案
            mild = await self._settle_case(db, {"juror1": "l3", "juror2": "l2", "juror3": "l2"})
            self.assertEqual(mild.verdict_level, 2)
            # 等不到 5 张有效票也不会卡死：弃权把票数顶到 7 张就转出
            stuck = await self._open_case(db)
            for juror, vote in (("juror1", "l3"), ("juror2", "l3"), ("juror3", "l3"), ("juror4", "abstain"), ("juror5", "abstain"), ("juror6", "abstain"), ("lurker", "l3")):
                result = await self._cast(db, juror, stuck.id, vote)
            await db.refresh(stuck)
            self.assertEqual((result["settled"], stuck.queue, stuck.jury_outcome), (False, "admin", "no_consensus"))
            self.assertEqual(jury_service.tally(["l3", "l3", "l3", "recuse", "need_context"]), None)

    async def test_quality_checks_come_at_random_positions(self) -> None:
        async with self.sessions() as db:
            questions = {(await self._question(db)).id for _ in range(3)}
            for _ in range(8):
                await self._open_case(db)
            juror = await self._user(db, "juror1")
            member = await self._member(db, "juror1")

            async def take(roll: float) -> bool:
                """取一件并投票，返回这一件是不是质控题。"""
                with patch.object(jury_service, "_quality_roll", return_value=roll):
                    view = await jury_service.next_case(db, juror)
                await jury_service.cast_vote(db, juror, view["case_id"], "l2", None)
                return await self._real(db, view["case_id"]) in questions

            # 距上一道不到 2 张票：随机数再小也不出
            self.assertEqual([await take(0.0), await take(0.0)], [False, False])
            # 之后每次取件按 1/5 的概率出
            self.assertFalse(await take(0.2))
            self.assertTrue(await take(0.19))
            await db.refresh(member)
            self.assertEqual((member.votes_since_quality, member.golden_total), (0, 1))
            self.assertEqual([await take(0.0), await take(0.0)], [False, False])
            self.assertTrue(await take(0.0))
            # 隔了超过 9 张还没出：强制出
            member.votes_since_quality = jury_service.QUALITY_MAX_GAP
            await db.commit()
            self.assertFalse(await take(0.99))
            await db.refresh(member)
            self.assertEqual(member.votes_since_quality, jury_service.QUALITY_MAX_GAP + 1)
            self.assertTrue(await take(0.99))
            # 题库里的题他都答过了：该出也出不了，照常发普通案件
            member.votes_since_quality = jury_service.QUALITY_MAX_GAP + 1
            await db.commit()
            self.assertFalse(await take(0.0))

    async def test_too_many_abstentions_warn_then_pause_assignments(self) -> None:
        async with self.sessions() as db:
            cases = [await self._open_case(db) for _ in range(8)]
            juror = await self._user(db, "juror1")
            with patch.object(jury_service, "ABSTAIN_MIN_VOTES", 4), patch.object(jury_service, "ABSTAIN_WINDOW", 5):
                for case, vote in zip(cases[:3], ("l2", "abstain", "need_context")):
                    await self._cast(db, "juror1", case.id, vote)
                # 不足 min_votes 张：不判断
                status = await jury_service.my_status(db, juror)
                self.assertEqual((status["warnings"], status["cooldown_until"]), ([], None))
                await self._cast(db, "juror1", cases[3].id, "l2")
                # 4 张里 2 张：超过 30% 给提示，没超过 50% 不暂停
                status = JuryStatus(**await jury_service.my_status(db, juror))
                self.assertEqual([(item.code, item.current, item.threshold, item.sample) for item in status.warnings], [("high_abstain", 0.5, 0.3, 4)])
                self.assertIsNone(status.cooldown_until)
                self.assertIsNotNone(await jury_service.next_case(db, juror))
                # 第 5 张又是弃权：60%，暂停发案 24 小时
                await self._cast(db, "juror1", cases[4].id, "abstain")
                status = await jury_service.my_status(db, juror)
                self.assertAlmostEqual((status["cooldown_until"] - datetime.now(UTC)).total_seconds(), 24 * 3600, delta=60)
                self.assertEqual((status["available_cases"], status["warnings"][0]["current"]), (0, 0.6))
                with self.assertRaises(Exception) as paused:
                    await jury_service.next_case(db, juror)
                self.assertEqual((paused.exception.status_code, paused.exception.detail["code"]), (429, "jury_cooldown"))
                self.assertEqual(paused.exception.detail["until"], jury_service._iso(status["cooldown_until"]))
                json.dumps(paused.exception.detail)
                # 暂停结束后可以继续；只看最近 5 张，正常投几张比例就下来了
                member = await self._member(db, "juror1")
                member.cooldown_until = datetime.now(UTC) - timedelta(minutes=1)
                await db.commit()
                self.assertIsNone((await jury_service.my_status(db, juror))["cooldown_until"])
                for case in cases[5:8]:
                    await self._cast(db, "juror1", case.id, "l2")
                await db.refresh(member)
                self.assertLess(penalty_service.aware(member.cooldown_until), datetime.now(UTC))
                self.assertEqual((await jury_service.my_status(db, juror))["warnings"], [])

    async def test_old_clients_five_votes_behave_as_before(self) -> None:
        async with self.sessions() as db:
            # 旧客户端的请求体：只有 vote 和 note
            for vote in ("none", "l1", "l2", "l3", "abstain"):
                payload = JuryVoteCreate(vote=vote)
                self.assertIsNone(payload.category)
            case_id = await self._jury_case(db, "insult")
            first = await self._vote(db, "juror1", "l2", "连续骂人", case_id=case_id)
            self.assertEqual({"case_id", "settled", "case"} - set(first), set())
            await self._vote(db, "juror2", "abstain", case_id=case_id)
            await self._vote(db, "juror3", "l1", case_id=case_id)
            self.assertTrue((await self._vote(db, "juror4", "l2", case_id=case_id))["settled"])
            case = await db.get(ReportCase, case_id)
            await db.refresh(case)
            self.assertEqual((case.status, case.verdict_source, case.verdict_category, case.verdict_level, case.jury_outcome), ("resolved", "jury", "harassment", 2, "consensus"))
            agreed = dict((await db.execute(select(JuryVote.juror_id, JuryVote.agreed).where(JuryVote.case_id == case_id))).all())
            self.assertEqual([agreed[self.ids[name]] for name in ("juror1", "juror2", "juror3", "juror4")], [True, None, True, True])
            clean_id = await self._jury_case(db, "insult2")
            for juror in ("juror1", "juror2", "juror3"):
                result = await self._vote(db, juror, "none", case_id=clean_id)
            self.assertTrue(result["settled"])
            self.assertEqual((await db.get(ReportCase, clean_id)).status, "dismissed")


# ---------------------------------------------------------------- 风纪值


class PointsTests(Phase2TestCase):
    async def test_votes_are_scored_when_the_case_settles(self) -> None:
        async with self.sessions() as db:
            case = await self._open_case(db)
            with self._events() as events:
                first = await self._cast(db, "juror1", case.id, "l2")
                await self._cast(db, "juror2", case.id, "l2")
                last = await self._cast(db, "juror3", case.id, "l2")
            # 当场结算的只有「今日首票」；定案时的加分走事件
            self.assertEqual(first["reward"], {"points": 2, "total": 2, "items": [{"kind": "daily_first", "points": 2, "label": "今日首票"}]})
            self.assertEqual((last["settled"], last["reward"]["points"], last["reward"]["total"]), (True, 2, 12))
            changed = [(users, data) for event, users, data in events if event == "jury.points.changed"]
            self.assertEqual(sorted(users.copy().pop() for users, _data in changed), sorted(self.ids[name] for name in ("juror1", "juror2", "juror3")))
            # 事件里的 case_id 是接收者自己的评审编号
            self.assertEqual(
                self._per_user(events, "jury.points.changed"),
                {
                    self.ids[name]: {"delta": 10, "total": 12, "kind": "vote_agreed", "label": "与结论一致", "case_id": await self._rid(db, name, case.id)}
                    for name in ("juror1", "juror2", "juror3")
                },
            )
            juror = await self._user(db, "juror1")
            member = await self._member(db, "juror1")
            self.assertEqual((member.points, (await jury_service.my_status(db, juror))["points"]), (12, 12))
            self.assertEqual((await jury_service.case_detail(db, juror.id, await self._rid(db, juror.id, case.id)))["my_vote"]["points"], 10)
            page = JuryPointPage(**await points_service.list_points(db, juror.id))
            self.assertEqual((page.total, page.count, [(item.kind, item.label, item.points, item.case_id) for item in page.items]), (
                12, 2, [("vote_agreed", "与结论一致", 10, await self._rid(db, juror, case.id)), ("daily_first", "今日首票", 2, None)],
            ))
            self.assertEqual(len((await points_service.list_points(db, juror.id, limit=1, offset=1))["items"]), 1)
            # 同一 (用户, kind, 案件) 不重复给：再结算一次没有变化
            await jury_service._settle_votes(db, case, True)
            await jury_service.resettle_votes(db, case, True)
            self.assertIsNone(await points_service.award(db, member, "vote_agreed", case_id=case.id))
            await db.commit()
            self.assertEqual((member.points, await self._kinds(db, "juror1")), (12, ["daily_first", "vote_agreed"]))

            # 同一天的第二件没有「今日首票」；与结论不一致的给 3 分；弃权什么都不给
            split = await self._open_case(db)
            second = await self._cast(db, "juror1", split.id, "none")
            self.assertEqual(second["reward"], {"points": 0, "total": 12, "items": []})
            abstained = await self._cast(db, "juror6", split.id, "abstain")
            self.assertEqual(abstained["reward"], {"points": 0, "total": 0, "items": []})
            for name in ("juror2", "juror3", "juror4", "juror5"):
                result = await self._cast(db, name, split.id, "l2")
            self.assertTrue(result["settled"])
            await db.refresh(member)
            self.assertEqual((member.points, (await self._member(db, "juror6")).points, (await self._member(db, "juror4")).points), (15, 0, 12))
            self.assertEqual(await self._kinds(db, "juror1"), ["daily_first", "vote_agreed", "vote_disagreed"])

            # 改判：按新结论补差，一致变不一致 -7，反之 +7
            with self._events() as events:
                await case_service.overturn_case(db, split.id, actor_id=self.ids["admin"], upheld=False, note="结合上下文是玩笑")
            await db.refresh(member)
            self.assertEqual((member.points, (await self._member(db, "juror4")).points, (await self._member(db, "juror6")).points), (22, 5, 0))
            adjusts = {users.copy().pop(): data for event, users, data in events if event == "jury.points.changed"}
            self.assertEqual(
                adjusts[juror.id], {"delta": 7, "total": 22, "kind": "overturn_adjust", "label": "改判补差", "case_id": await self._rid(db, juror, split.id)}
            )
            self.assertEqual(adjusts[self.ids["juror4"]]["delta"], -7)
            self.assertEqual((await jury_service.case_detail(db, juror.id, await self._rid(db, juror.id, split.id)))["my_vote"]["points"], 10)
            # 同一个案件可以被改判不止一次，每次都补
            await case_service.overturn_case(db, split.id, actor_id=self.ids["admin"], upheld=True, category="harassment", level=2, note="复核后维持原判")
            await db.refresh(member)
            self.assertEqual((member.points, (await self._member(db, "juror4")).points), (15, 12))
            self.assertEqual((await self._kinds(db, "juror1"))[-2:], ["overturn_adjust", "overturn_adjust"])

    async def test_points_never_go_below_zero(self) -> None:
        async with self.sessions() as db:
            question = await self._question(db)
            right = await self._question(db)
            member = await self._member(db, "juror1")
            member.points = 1
            await db.commit()
            with self._events() as events:
                wrong = await self._cast(db, "juror1", question.id, "none", quality=True)
            # 质控题当场结算：先有今日首票的 2 分（共 3 分），答错扣 5 分 —— 但总分扣到 0 为止，流水里记实际扣的
            self.assertEqual(wrong["reward"], {
                "points": -1, "total": 0,
                "items": [{"kind": "daily_first", "points": 2, "label": "今日首票"}, {"kind": "quality_wrong", "points": -3, "label": "质控题答错"}],
            })
            self.assertEqual(member.points, 0)
            # 随响应返回的部分不再重复发事件
            self.assertEqual([event for event, _users, _data in events if event == "jury.points.changed"], [])
            self.assertEqual((await jury_service.case_detail(db, self.ids["juror1"], await self._rid(db, self.ids["juror1"], question.id)))["my_vote"]["points"], -3)
            again = await self._cast(db, "juror1", right.id, "l2", quality=True)
            self.assertEqual(again["reward"], {"points": 5, "total": 5, "items": [{"kind": "quality_correct", "points": 5, "label": "质控题答对"}]})
            member.points = 2
            log = await points_service.award(db, member, "quality_wrong", case_id=987654)
            self.assertEqual((log.points, member.points), (-2, 0))
            # 管理员调整：可加可减，同样不低于 0，写审计日志
            await db.commit()
            with self.assertRaises(ValueError):
                JuryMemberReview(action="adjust_points")
            with self.assertRaises(ValueError):
                JuryMemberReview(action="approve", points=5)
            await jury_service.review_member(db, self.ids["juror1"], action="adjust_points", actor_id=self.ids["admin"], note="活动奖励", points=50)
            self.assertEqual((member.points, member.status, member.review_note), (50, "active", None))
            await jury_service.review_member(db, self.ids["juror1"], action="adjust_points", actor_id=self.ids["admin"], note=None, points=-80)
            self.assertEqual(member.points, 0)
            logs = (await db.execute(select(JuryPointLog).where(JuryPointLog.kind == "admin_adjust").order_by(JuryPointLog.id))).scalars().all()
            self.assertEqual([(log.points, log.note) for log in logs], [(50, "活动奖励"), (-50, None)])
            audits = (await db.execute(select(AdminAuditLog.details).where(AdminAuditLog.action == "jury.member.adjust_points").order_by(AdminAuditLog.id))).scalars().all()
            self.assertEqual(audits, ["points=50；applied=50；活动奖励", "points=-80；applied=-50"])
            self.assertEqual((await self._code(jury_service.review_member(db, self.ids["alice"], action="adjust_points", actor_id=self.ids["admin"], note=None, points=5)))[0], 404)

    async def test_streaks_and_daily_first_follow_local_days(self) -> None:
        async with self.sessions() as db:
            today = points_service.local_day(datetime.now(UTC))
            member = await self._member(db, "juror1")
            member.streak_days, member.streak_last_day = 6, today - timedelta(days=1)
            broken = await self._member(db, "juror2")
            broken.streak_days, broken.streak_last_day = 6, today - timedelta(days=2)
            await db.commit()
            first, second = await self._open_case(db), await self._open_case(db)
            with self._events() as events:
                result = await self._cast(db, "juror1", first.id, "l2")
            # 连满 7 天：当天第一张计入表决的票上一并结算
            self.assertEqual([(item["kind"], item["points"]) for item in result["reward"]["items"]], [("daily_first", 2), ("streak_7", 10)])
            self.assertEqual((member.streak_days, member.streak_last_day, member.points), (7, today, 12))
            self.assertIn({"key": "streak_7", "label": "七日之约"}, [data for event, _users, data in events if event == "jury.achievement.earned"])
            self.assertEqual((await self._cast(db, "juror1", second.id, "l2"))["reward"]["items"], [])
            self.assertEqual(member.streak_days, 7)
            # 断了一天：从 1 重新数
            await self._cast(db, "juror2", first.id, "none")
            self.assertEqual((broken.streak_days, broken.points), (1, 2))
            # 弃权不算参与：不给首票分，也不续连续天数
            idle = await self._member(db, "juror3")
            await self._cast(db, "juror3", second.id, "abstain")
            self.assertEqual((idle.streak_days, idle.streak_last_day, idle.points), (0, None, 0))
            self.assertFalse(points_service.touch_streak(member, today))
            self.assertEqual((points_service.current_streak(member, today), points_service.current_streak(member, today + timedelta(days=1)), points_service.current_streak(member, today + timedelta(days=2))), (7, 7, 0))
            # 第二天的第一张票又有首票分；连到 14 天再给一次
            member.streak_days, member.streak_last_day = 13, today - timedelta(days=1)
            log = (await db.execute(select(JuryPointLog).where(JuryPointLog.user_id == member.user_id, JuryPointLog.kind == "daily_first"))).scalar_one()
            log.ref_id = (today - timedelta(days=1)).toordinal()
            streak_log = (await db.execute(select(JuryPointLog).where(JuryPointLog.user_id == member.user_id, JuryPointLog.kind == "streak_7"))).scalar_one()
            streak_log.ref_id = (today - timedelta(days=7)).toordinal()
            await db.commit()
            third = await self._open_case(db)
            result = await self._cast(db, "juror1", third.id, "l2")
            self.assertEqual([item["kind"] for item in result["reward"]["items"]], ["daily_first", "streak_7"])
            self.assertEqual(member.streak_days, 14)

    async def test_liked_opinions_reward_the_author_once(self) -> None:
        async with self.sessions() as db:
            case = await self._open_case(db)
            await self._cast(db, "juror1", case.id, "l2", "这是在骂人")
            await self._cast(db, "juror2", case.id, "none")
            for name in ("juror3", "juror4", "juror5"):
                await self._cast(db, name, case.id, "abstain")
            author = await self._member(db, "juror1")
            before = author.points
            note_id = (await opinion_service.list_opinions(db, await self._user(db, "juror2"), await self._rid(db, await self._user(db, "juror2"), case.id)))["items"][0]["id"]
            likers = [await self._user(db, name) for name in ("juror2", "juror3", "juror4", "juror5")]
            with self._events() as events:
                await opinion_service.set_like(db, likers[0], note_id, True)
                await opinion_service.set_like(db, likers[1], note_id, True)
                await db.refresh(author)
                self.assertEqual(author.points, before)
                # 赞数第一次达到 3：给作者 5 分
                self.assertEqual(await opinion_service.set_like(db, likers[2], note_id, True), {"id": note_id, "like_count": 3, "liked": True})
            await db.refresh(author)
            self.assertEqual(author.points, before + 5)
            self.assertEqual([data for event, users, data in events if event == "jury.points.changed" and users == {author.user_id}], [
                {"delta": 5, "total": before + 5, "kind": "opinion_liked", "label": "观点获赞", "case_id": await self._rid(db, "juror1", case.id)}
            ])
            # 之后取消赞不扣，再点回来、更多人点也不再给
            await opinion_service.set_like(db, likers[2], note_id, False)
            await opinion_service.set_like(db, likers[2], note_id, True)
            await opinion_service.set_like(db, likers[3], note_id, True)
            await db.refresh(author)
            self.assertEqual((author.points, (await self._kinds(db, "juror1")).count("opinion_liked")), (before + 5, 1))


# ---------------------------------------------------------------- 层级


class TierTests(Phase2TestCase):
    async def test_promotion_checks_every_requirement_after_a_vote_settles(self) -> None:
        async with self.sessions() as db:
            now = datetime.now(UTC)
            ready, fresh, shaky = [await self._member(db, name) for name in ("juror1", "juror2", "juror3")]
            for member in (ready, fresh, shaky):
                member.votes_total, member.votes_settled, member.votes_agreed = 14, 14, 13
                member.serving_since = now - timedelta(days=20)
            ready.badge_visible = True
            # 在任还不满 14 天
            fresh.serving_since = now - timedelta(days=3)
            # 答过 3 道质控题、只对了 1 道
            shaky.golden_total, shaky.golden_correct = 3, 1
            await db.commit()
            juror = await self._user(db, "juror1")
            progress = (await jury_service.my_status(db, juror))["tier_progress"]
            self.assertEqual(progress["next_tier"], "regular")
            self.assertEqual([(item["key"], item["label"], item["current"], item["target"], item["ok"]) for item in progress["requirements"]], [
                ("votes_settled", "已出结论的票", 14, 15, False),
                ("agreement_rate", "一致率", 0.929, 0.7, True),
                ("quality_rate", "质控正确率", None, 0.7, True),
                ("days_served", "在任天数", 20, 14, True),
            ])
            self.assertEqual(progress["requirements"][2]["note"], "答满 3 道后开始计算")
            self.assertNotIn("note", progress["requirements"][0])

            with self._events() as events:
                await self._settle_case(db, {"juror1": "l2", "juror2": "l2", "juror3": "l2"})
            for member in (ready, fresh, shaky):
                await db.refresh(member)
            self.assertEqual((ready.tier, fresh.tier, shaky.tier, ready.votes_settled), ("regular", "trainee", "trainee", 15))
            # 晋升：通知中心一条通知、状态事件、成就，公开身份的徽章跟着变
            self.assertEqual((await db.get(User, juror.id)).jury_badge, "regular")
            self.assertEqual([users for event, users, _data in events if event == "jury.status.changed"], [{juror.id}])
            self.assertIn(("jury.achievement.earned", {juror.id}, {"key": "tier_regular", "label": "正式委员"}), events)
            notifications = [item for item in (await notification_service.list_unified_notifications(db, juror.id)).items if item.type == "jury"]
            self.assertEqual([(item.category, item.title, item.payload) for item in notifications], [("system", "你已晋升为正式委员", {"kind": "promoted"})])
            status = JuryStatus(**await jury_service.my_status(db, juror))
            self.assertEqual((status.tier, status.tier_progress.next_tier, [item.target for item in status.tier_progress.requirements]), ("regular", "senior", [50, 0.8, 0.8, 60]))
            self.assertEqual((await jury_service.my_status(db, await self._user(db, "juror3")))["tier_progress"]["requirements"][2]["ok"], False)
            # 已是资深：没有下一层
            ready.tier = "senior"
            await db.commit()
            self.assertEqual((await jury_service.my_status(db, juror))["tier_progress"], {"next_tier": None, "requirements": []})
            self.assertIsNone((await jury_service.my_status(db, await self._user(db, "alice")))["tier_progress"])

    async def test_demotion_looks_at_recent_votes_only(self) -> None:
        async with self.sessions() as db:
            # 正式委员，最近 15 张已结票里只对了 7 张
            await self._history_votes(db, "juror3", [True] * 7 + [False] * 8)
            slipping = await self._member(db, "juror3")
            slipping.tier, slipping.badge_visible = "regular", True
            await points_service.sync_badge(db, slipping)
            # 管理员直接任命的资深委员，一张票都还没有
            founder = (await jury_service.review_member(db, self.ids["juror4"], action="set_tier", actor_id=self.ids["admin"], note=None, tier="senior"))
            # 累计一致率很高、但最近一直在错的正式委员；以及累计够格、最近一直在错的见习委员
            await self._history_votes(db, "juror5", [True] * 40 + [False] * 10)
            await self._history_votes(db, "juror6", [True] * 20 + [False] * 12)
            fading = await self._member(db, "juror5")
            fading.tier = "regular"
            stuck = await self._member(db, "juror6")
            stuck.serving_since = datetime.now(UTC) - timedelta(days=30)
            await db.commit()
            juror3 = await self._user(db, "juror3")

            # 快要被降级 / 暂停时先在 /jury/me 里提醒
            warnings = {item["code"]: item for item in (await jury_service.my_status(db, juror3))["warnings"]}
            self.assertEqual(set(warnings), {"demote_risk", "suspend_risk"})
            self.assertEqual((warnings["demote_risk"]["current"], warnings["demote_risk"]["threshold"], warnings["demote_risk"]["sample"]), (0.467, 0.6, 15))
            self.assertEqual((warnings["suspend_risk"]["current"], warnings["suspend_risk"]["threshold"], warnings["suspend_risk"]["sample"]), (0.467, 0.6, 15))
            JuryStatus(**await jury_service.my_status(db, juror3))
            # 样本不够的不提醒；见习没有降级线，只会有暂停线的提醒（32 张里对了 20 张，62.5%）
            self.assertEqual((await jury_service.my_status(db, await self._user(db, "juror4")))["warnings"], [])
            self.assertEqual([item["code"] for item in (await jury_service.my_status(db, await self._user(db, "juror6")))["warnings"]], ["suspend_risk"])
            # 正式委员最近 30 张里对了 20 张（66.7%）：离 60% 的降级线不到 0.1，提醒；累计 80% 离暂停线还远
            fading_warnings = (await jury_service.my_status(db, await self._user(db, "juror5")))["warnings"]
            self.assertEqual([(item["code"], item["current"], item["threshold"], item["sample"]) for item in fading_warnings], [("demote_risk", 0.667, 0.6, 30)])

            with self._events() as events:
                await self._settle_case(db, {"juror3": "l2", "juror5": "l2", "juror6": "l2"})
            for member in (slipping, fading, stuck):
                await db.refresh(member)
            # 最近 16 张里对了 8 张（50%）：正式降为见习
            self.assertEqual((slipping.tier, slipping.status), ("trainee", "active"))
            self.assertEqual((await db.get(User, juror3.id)).jury_badge, "trainee")
            self.assertIn(("jury.status.changed", {juror3.id}, {}), events)
            notification = (await db.execute(select(JuryNotification).where(JuryNotification.user_id == juror3.id))).scalar_one()
            self.assertEqual((notification.kind, notification.title), ("demoted", "你的委员层级调整为见习委员"))
            # 最近 30 张里对了 21 张（70%）：没到 60% 的降级线，保持正式；但也不会升资深
            self.assertEqual((fading.tier, fading.votes_settled), ("regular", 51))
            # 累计 33 张、一致率 64%：不到 70%，不晋升
            self.assertEqual(stuck.tier, "trainee")

            # 管理员设定的层级同样适用降级规则，但不会因为票数不够被降
            await self._settle_case(db, {"juror4": "none", "juror5": "l2", "juror6": "l2", "juror1": "l2", "juror2": "l2"})
            await db.refresh(founder)
            self.assertEqual((founder.tier, founder.votes_settled, founder.votes_agreed), ("senior", 1, 0))
            await self._history_votes(db, "juror4", [True] * 5 + [False] * 9)
            await self._settle_case(db, {"juror4": "l2", "juror1": "l2", "juror2": "l2"})
            await db.refresh(founder)
            # 16 张里对了 6 张：资深降为正式（一次降一层）
            self.assertEqual((founder.tier, founder.status), ("regular", "active"))

            # 累计数字够了、但近期一致率在新层级的降级线以下：不升，免得升上去马上又降回来
            await self._history_votes(db, "juror1", [True] * 32 + [False] * 14)
            climber = await self._member(db, "juror1")
            climber.serving_since = datetime.now(UTC) - timedelta(days=30)
            await db.commit()
            self.assertTrue(all(item["ok"] for item in points_service.tier_progress(climber)["requirements"]))
            await self._settle_case(db, {"juror1": "none", "juror2": "none", "lurker": "none"})
            await db.refresh(climber)
            self.assertEqual(climber.tier, "trainee")

    async def test_system_suspension_notifies_and_hides_the_badge(self) -> None:
        async with self.sessions() as db:
            question = await self._question(db)
            juror = await self._user(db, "juror1")
            member = await self._member(db, "juror1")
            member.golden_total, member.golden_correct, member.badge_visible = 4, 3, True
            await points_service.sync_badge(db, member)
            await db.commit()
            self.assertEqual(juror.jury_badge, "trainee")
            # 质控题 4 道对 3 道：离暂停线还远，不提醒；再错两道就危险了
            self.assertEqual((await jury_service.my_status(db, juror))["warnings"], [])
            member.golden_correct = 2
            await db.commit()
            warning = (await jury_service.my_status(db, juror))["warnings"][0]
            self.assertEqual((warning["code"], warning["current"], warning["threshold"], warning["sample"]), ("suspend_risk", 0.5, 0.6, 4))
            with self._events() as events:
                await self._cast(db, "juror1", question.id, "none", quality=True)
            await db.refresh(member)
            await db.refresh(juror)
            self.assertEqual((member.status, member.golden_total, juror.jury_badge), ("suspended", 5, None))
            self.assertIn(("jury.status.changed", {juror.id}, {}), events)
            notification = (await db.execute(select(JuryNotification).where(JuryNotification.user_id == juror.id))).scalar_one()
            self.assertEqual((notification.kind, notification.title), ("suspended", "你的风纪委员资格已被暂停"))
            status = await jury_service.my_status(db, juror)
            self.assertEqual((status["status"], status["tier"], status["tier_progress"], status["warnings"], status["recuse_left_today"]), ("suspended", None, None, [], None))

    async def test_suspend_warning_picks_the_measure_closest_to_the_line(self) -> None:
        async with self.sessions() as db:
            juror = await self._user(db, "juror1")
            member = await self._member(db, "juror1")

            async def warning() -> tuple | None:
                await db.commit()
                items = [item for item in (await jury_service.my_status(db, juror))["warnings"] if item["code"] == "suspend_risk"]
                return (items[0]["current"], items[0]["threshold"], items[0]["sample"]) if items else None

            # 已结票不到暂停线要求的一半（10 张）：不看
            member.votes_settled, member.votes_agreed = 9, 3
            self.assertIsNone(await warning())
            member.votes_settled, member.votes_agreed = 10, 6
            self.assertEqual(await warning(), (0.6, 0.6, 10))
            # 一致率比暂停线高出 0.1 以上：不提醒
            member.votes_agreed = 7
            self.assertIsNone(await warning())
            # 两项都触发：给更低的那一项
            member.votes_agreed = 6
            member.golden_total, member.golden_correct = 3, 1
            self.assertEqual(await warning(), (0.333, 0.6, 3))
            member.golden_total, member.golden_correct = 2, 0
            self.assertEqual(await warning(), (0.6, 0.6, 10))
            # 余量是配置项
            with patch.object(jury_service, "WARNING_MARGIN", 0.0), patch.object(jury_service, "WARNING_SAMPLE_RATIO", 1.0):
                self.assertIsNone(await warning())


# ---------------------------------------------------------------- 任期


class TermTests(Phase2TestCase):
    async def test_expired_terms_renew_automatically_when_the_record_is_clean(self) -> None:
        async with self.sessions() as db:
            now = datetime.now(UTC)
            clean, punished, lazy = [await self._member(db, name) for name in ("juror1", "juror2", "juror3")]
            for member in (clean, punished, lazy):
                member.term_ends_at = now - timedelta(hours=1)
                member.badge_visible = True
            for name in ("juror1", "juror2", "juror3"):
                (await self._user(db, name)).jury_badge = "trainee"
            banned = await self._user(db, "juror2")
            banned.status = UserStatus.DISABLED
            await db.commit()
            self.assertEqual(jury_service.member_state(clean), "expired")

            # 定时循环：没有违规记录的直接续任，不需要再点申请
            with self._events() as events:
                with patch.object(jury_service, "_is_serving", wraps=jury_service._is_serving):
                    stats = await case_service.sweep_jury_housekeeping(db)
            self.assertEqual(stats, {"terms": 3, "questions": 0})
            for member in (clean, punished, lazy):
                await db.refresh(member)
            self.assertEqual((jury_service.member_state(clean), jury_service.member_state(lazy), jury_service.member_state(punished)), ("active", "active", "expired"))
            self.assertGreater(penalty_service.aware(clean.term_ends_at), now + timedelta(days=29))
            self.assertEqual((await self._user(db, "juror1")).jury_badge, "trainee")
            # 条件不满足：状态显示 expired，发一条通知，徽章撤下
            await db.refresh(banned)
            self.assertIsNone(banned.jury_badge)
            self.assertEqual(sorted(users.copy().pop() for event, users, _data in events if event == "jury.status.changed"), sorted(self.ids[name] for name in ("juror1", "juror2", "juror3")))
            notifications = (await db.execute(select(JuryNotification))).scalars().all()
            self.assertEqual([(item.user_id, item.kind, item.title) for item in notifications], [(banned.id, "term_expired", "你的风纪委员任期已届满")])
            listed = [item for item in (await notification_service.list_unified_notifications(db, banned.id)).items if item.type == "jury"]
            self.assertEqual([(item.category, item.payload) for item in listed], [("system", {"kind": "term_expired"})])
            # 判过「不续任」之后不再反复通知，也不会等条件恢复后悄悄续上
            with self._events() as events:
                self.assertEqual(await jury_service.renew_expired_terms(db), 0)
                status = await jury_service.my_status(db, banned)
                banned.status = UserStatus.ACTIVE
                await db.commit()
                self.assertEqual((await jury_service.my_status(db, banned))["status"], "expired")
            self.assertEqual((status["status"], status["tier"], events), ("expired", None, []))
            self.assertEqual((await db.execute(select(func.count()).select_from(JuryNotification))).scalar_one(), 1)
            self.assertEqual(await self._code(jury_service.next_case(db, banned)), (403, "not_juror"))

    async def test_expired_terms_renew_lazily_on_status_and_assignment(self) -> None:
        async with self.sessions() as db:
            now = datetime.now(UTC)
            case = await self._open_case(db)
            for name in ("juror1", "juror2"):
                (await self._member(db, name)).term_ends_at = now - timedelta(minutes=5)
            await db.commit()
            # 读 /jury/me 时顺手续任
            with self._events() as events:
                status = await jury_service.my_status(db, await self._user(db, "juror1"))
            self.assertEqual((status["status"], status["tier"], status["available_cases"]), ("active", "trainee", 1))
            self.assertGreater(status["term_ends_at"], now + timedelta(days=29))
            self.assertEqual([(event, users) for event, users, _data in events], [("jury.status.changed", {self.ids["juror1"]})])
            # 取案件时也一样
            self.assertEqual(await self._real(db, (await jury_service.next_case(db, await self._user(db, "juror2")))["case_id"]), case.id)
            self.assertEqual(jury_service.member_state(await self._member(db, "juror2")), "active")
            self.assertEqual((await db.execute(select(func.count()).select_from(JuryNotification))).scalar_one(), 0)


# ---------------------------------------------------------------- 成就与排行榜


class AchievementTests(Phase2TestCase):
    async def test_achievements_are_earned_once_and_report_progress(self) -> None:
        async with self.sessions() as db:
            outsider = JuryAchievementPage(items=await points_service.list_achievements(db, self.ids["alice"]))
            self.assertEqual([item.key for item in outsider.items], [
                "first_vote", "votes_10", "votes_50", "votes_100", "sharp_eye", "streak_7", "streak_30", "quality_10", "liked_10", "exam_first_try", "tier_regular", "tier_senior",
            ])
            self.assertEqual({(item.earned_at, item.current) for item in outsider.items}, {(None, 0)})
            self.assertEqual({item.key: item.target for item in outsider.items}["sharp_eye"], 30)

            member = await self._member(db, "juror1")
            member.votes_total, member.votes_settled, member.votes_agreed = 9, 9, 9
            await db.commit()
            juror = await self._user(db, "juror1")
            with self._events() as events:
                await self._settle_case(db, {"juror1": "l2", "juror2": "l2", "juror3": "l2"})
            earned = [(users.copy().pop(), data["key"]) for event, users, data in events if event == "jury.achievement.earned"]
            # juror1：这是他第 10 张出结论的票；另外两位是第一案
            self.assertEqual(sorted(key for user_id, key in earned if user_id == juror.id), ["first_vote", "votes_10"])
            self.assertEqual(sorted(key for user_id, key in earned if user_id == self.ids["juror2"]), ["first_vote"])
            items = {item["key"]: item for item in await points_service.list_achievements(db, juror.id)}
            self.assertIsNotNone(items["votes_10"]["earned_at"])
            self.assertEqual((items["votes_10"]["current"], items["votes_50"]["current"], items["votes_50"]["earned_at"], items["sharp_eye"]["current"]), (10, 10, None, 10))
            self.assertEqual((items["streak_7"]["current"], items["streak_7"]["target"], items["first_vote"]["current"]), (1, 7, 1))
            # 不会重复获得
            with self._events() as events:
                await self._settle_case(db, {"juror1": "l2", "juror2": "l2", "juror3": "l2"})
            self.assertEqual([data for event, _users, data in events if event == "jury.achievement.earned"], [])
            self.assertEqual(await points_service.check_achievements(db, juror.id), [])

            # 火眼金睛要票数和一致率都达标；质控题连续答对从最近一道往回数
            member.votes_settled, member.votes_agreed = 30, 26
            self.assertEqual(await points_service.check_achievements(db, juror.id, member), [])
            member.votes_agreed = 27
            self.assertEqual(await points_service.check_achievements(db, juror.id, member), ["sharp_eye"])
            for index, agreed in enumerate([False] + [True] * 9):
                question = await self._question(db)
                db.add(JuryVote(case_id=question.id, juror_id=juror.id, vote="l2", is_quality_check=True, agreed=agreed, voted_at=datetime.now(UTC) + timedelta(seconds=index)))
            member.golden_total, member.golden_correct = 10, 9
            await db.commit()
            self.assertEqual({item["key"]: item["current"] for item in await points_service.list_achievements(db, juror.id)}["quality_10"], 9)
            last = await self._question(db)
            await self._cast(db, "juror1", last.id, "l2", quality=True)
            earned_keys = set((await db.execute(select(JuryAchievement.key).where(JuryAchievement.user_id == juror.id))).scalars().all())
            self.assertEqual(earned_keys, {"first_vote", "votes_10", "sharp_eye", "quality_10"})
            # 得到之后进度条保持满格，即使数字后来掉回去
            wrong = await self._question(db)
            await self._cast(db, "juror1", wrong.id, "none", quality=True)
            item = {item["key"]: item for item in await points_service.list_achievements(db, juror.id)}["quality_10"]
            self.assertEqual((item["current"], item["target"], item["earned_at"] is not None), (10, 10, True))
            # 管理员直接任命为资深：两个层级成就一起拿到
            await jury_service.review_member(db, self.ids["juror4"], action="set_tier", actor_id=self.ids["admin"], note=None, tier="senior")
            keys = set((await db.execute(select(JuryAchievement.key).where(JuryAchievement.user_id == self.ids["juror4"]))).scalars().all())
            self.assertEqual(keys, {"tier_regular", "tier_senior"})


class LeaderboardTests(Phase2TestCase):
    async def test_leaderboard_periods_anonymity_and_me(self) -> None:
        async with self.sessions() as db:
            members = {name: await self._member(db, name) for name in ("juror1", "juror2", "juror3", "juror4")}
            for name, points in (("juror1", 30), ("juror2", 50), ("juror3", 10)):
                await points_service.award(db, members[name], "admin_adjust", points=points, unique=False)
            members["juror2"].badge_visible = True
            members["juror2"].tier = "regular"
            await points_service.sync_badge(db, members["juror2"])
            # juror3 的分是 40 天前得的
            old = (await db.execute(select(JuryPointLog).where(JuryPointLog.user_id == self.ids["juror3"]))).scalar_one()
            old.created_at = datetime.now(UTC) - timedelta(days=40)
            await db.commit()
            case = await self._open_case(db)
            await self._cast(db, "juror1", case.id, "abstain")
            points_service.reset_leaderboard_cache()
            juror1, juror2, alice = self.ids["juror1"], self.ids["juror2"], self.ids["alice"]

            week = Leaderboard(**await points_service.leaderboard(db, juror1, period="week"))
            self.assertEqual([(item.rank, item.points, item.tier, item.votes, item.me) for item in week.items], [(1, 50, "regular", 0, False), (2, 30, "trainee", 1, True)])
            # 公开身份的带用户信息，没公开的是匿名委员
            self.assertEqual((week.items[0].user.username, week.items[0].user.jury_badge, week.items[1].user), ("juror2", "regular", None))
            self.assertNotIn("juror1", week.model_dump_json())
            # me 和榜单行同形
            self.assertEqual(week.me.model_dump(), {"rank": 2, "points": 30, "tier": "trainee", "votes": 1, "me": True, "user": None})
            mine = (await points_service.leaderboard(db, juror2, period="week"))["me"]
            self.assertEqual((mine["rank"], mine["me"], mine["user"].username), (1, True, "juror2"))
            month = await points_service.leaderboard(db, juror1, period="month")
            self.assertEqual([(item["rank"], item["points"]) for item in month["items"]], [(1, 50), (2, 30)])
            # 累计榜：40 天前的分也算
            everything = await points_service.leaderboard(db, juror1, period="all")
            self.assertEqual([(item["points"], item["votes"]) for item in everything["items"]], [(50, 0), (30, 1), (10, 0)])
            # 不是委员、本期没有得分的：me 为 null
            self.assertIsNone((await points_service.leaderboard(db, alice, period="week"))["me"])
            self.assertIsNone((await points_service.leaderboard(db, self.ids["juror3"], period="week"))["me"])
            self.assertIsNone((await points_service.leaderboard(db, self.ids["juror4"], period="all"))["me"])
            # limit 之外的人仍然知道自己的名次
            short = await points_service.leaderboard(db, juror1, period="week", limit=1)
            self.assertEqual((len(short["items"]), short["me"]["rank"]), (1, 2))

            # 名次缓存 60 秒
            await points_service.award(db, members["juror4"], "admin_adjust", points=99, unique=False)
            await db.commit()
            cached = await points_service.leaderboard(db, self.ids["juror4"], period="week")
            self.assertEqual((cached["generated_at"], cached["me"], len(cached["items"])), (week.generated_at, None, 2))
            tick, generated_at, rows = points_service._leaderboard_cache["week"]
            points_service._leaderboard_cache["week"] = (tick - points_service.LEADERBOARD_CACHE_SECONDS - 1, generated_at, rows)
            fresh = await points_service.leaderboard(db, self.ids["juror4"], period="week")
            self.assertEqual((fresh["me"]["rank"], fresh["me"]["points"], [item["points"] for item in fresh["items"]]), (1, 99, [99, 50, 30]))
            # 只列在任委员
            await jury_service.review_member(db, juror2, action="suspend", actor_id=self.ids["admin"], note="测试")
            self.assertEqual([item["points"] for item in (await points_service.leaderboard(db, juror1, period="week"))["items"]], [99, 30])

    async def test_period_boundaries_follow_local_calendar(self) -> None:
        # 2026-10-12 是周一；UTC 的 10 月 11 日 16:00 就是北京时间周一零点
        monday = datetime(2026, 10, 12, 3, 0, tzinfo=UTC)
        self.assertEqual(points_service.period_start("week", monday), datetime(2026, 10, 11, 16, 0, tzinfo=UTC))
        self.assertEqual(points_service.period_start("week", datetime(2026, 10, 18, 15, 59, tzinfo=UTC)), datetime(2026, 10, 11, 16, 0, tzinfo=UTC))
        self.assertEqual(points_service.period_start("week", datetime(2026, 10, 18, 16, 0, tzinfo=UTC)), datetime(2026, 10, 18, 16, 0, tzinfo=UTC))
        self.assertEqual(points_service.period_start("month", monday), datetime(2026, 9, 30, 16, 0, tzinfo=UTC))
        self.assertIsNone(points_service.period_start("all", monday))
        self.assertEqual(points_service.local_day(datetime(2026, 10, 11, 16, 30, tzinfo=UTC)), date(2026, 10, 12))


# ---------------------------------------------------------------- 公开展示身份


class BadgeTests(Phase2TestCase):
    async def test_badge_follows_tier_status_and_the_members_choice(self) -> None:
        async with self.sessions() as db:
            juror = await self._user(db, "juror1")
            admin_id = self.ids["admin"]

            async def badge() -> str | None:
                await db.refresh(juror)
                # UserPublic 直接读 users 表上的这一列
                self.assertEqual(UserPublic.model_validate(juror).jury_badge, juror.jury_badge)
                return juror.jury_badge

            async def review(action: str, **kwargs) -> None:
                await jury_service.review_member(db, juror.id, action=action, actor_id=admin_id, note=None, **kwargs)

            # 默认不展示
            self.assertEqual((await badge(), (await jury_service.my_status(db, juror))["badge_visible"]), (None, False))
            await review("set_tier", tier="regular")
            self.assertIsNone(await badge())
            await jury_service.update_settings(db, juror, badge_visible=True)
            self.assertEqual((await badge(), (await jury_service.my_status(db, juror))["badge_visible"]), ("regular", True))
            await review("set_tier", tier="senior")
            self.assertEqual(await badge(), "senior")
            await review("suspend")
            self.assertIsNone(await badge())
            await review("reinstate")
            self.assertEqual(await badge(), "senior")
            await review("reinstate", tier="trainee")
            self.assertEqual(await badge(), "trainee")
            await review("retire")
            self.assertIsNone(await badge())
            await review("approve")
            self.assertEqual(await badge(), "trainee")
            await review("reject")
            self.assertIsNone(await badge())
            await review("approve", tier="regular")
            await jury_service.update_settings(db, juror, badge_visible=False)
            self.assertIsNone(await badge())
            # 本人的选择一直记着：不在任时打开，复职后生效
            await review("suspend")
            await jury_service.update_settings(db, juror, badge_visible=True)
            self.assertIsNone(await badge())
            await review("reinstate")
            self.assertEqual(await badge(), "regular")
            # 任期届满后重新申请续任，徽章跟着回来（任期届满时的处理见 TermTests）
            member = await self._member(db, "juror1")
            self.assertTrue(member.badge_visible)
            # 还没有委员记录的人没有东西可设；普通用户的 jury_badge 是 null
            alice = await self._user(db, "alice")
            self.assertEqual(await self._code(jury_service.update_settings(db, alice, badge_visible=True)), (403, "not_juror"))
            self.assertIsNone(UserPublic.model_validate(alice).jury_badge)
            self.assertIn("jury_badge", UserPublic.model_validate(alice).model_dump())
            # 后台成员列表的新字段
            items, _total = await jury_service.list_members(db, None, 1, 0, user_id=juror.id)
            read = JuryMemberRead(**items[0])
            self.assertEqual((read.badge_visible, read.points, read.exam_score, read.abstain_rate, read.recuse_rate, read.days_served), (True, 0, None, 0, 0, 0))

    async def test_member_rates_in_the_admin_list(self) -> None:
        async with self.sessions() as db:
            cases = [await self._open_case(db) for _ in range(5)]
            for case, vote in zip(cases, ("l2", "abstain", "need_context", "recuse", "recuse")):
                await self._cast(db, "juror1", case.id, vote)
            member = await self._member(db, "juror1")
            member.serving_since = datetime.now(UTC) - timedelta(days=9, hours=1)
            await db.commit()
            items, _total = await jury_service.list_members(db, "active", 50, 0)
            read = {item["user_id"]: item for item in items}[self.ids["juror1"]]
            # 3 张票里 2 张不计入表决；5 次分配里回避了 2 次
            self.assertEqual((read["votes_total"], read["abstain_rate"], read["recuse_rate"], read["days_served"], read["points"]), (3, 0.667, 0.4, 9, 2))


# ---------------------------------------------------------------- 配置


class ConfigTests(Phase2TestCase):
    async def test_config_exposes_the_phase_two_blocks(self) -> None:
        async with self.sessions() as db:
            raw = await overview_service.config(db)
            config = JuryConfig(**raw)
            # 一期、二期的能力排在最前面、顺序不变；三期追加的在 test_jury_v2_phase3.py 里查
            self.assertEqual(config.capabilities[:13], [
                "overview", "case_detail", "opinions", "votes", "public_cases",
                "exam", "practice", "votes_v2", "points", "tiers", "achievements", "leaderboard", "badge",
            ])
            self.assertEqual([(vote.value, vote.kind) for vote in config.votes[5:]], [("recuse", "recuse"), ("need_context", "abstain")])
            categories = {item.key: item for item in config.categories}
            self.assertEqual((categories["harassment"].label, categories["harassment"].default_level, categories["fraud"].default_level), ("辱骂骚扰", 2, 3))
            self.assertEqual(set(categories) & {"sexual", "minor_sexual", "vulgar", "suggestive", "unverified_media", "admin_action"}, set())
            self.assertIn("privacy", categories)
            # 三期多了三个键（panel_agreed / panel_disagreed / review_vote），原有的不变
            self.assertLessEqual({
                "vote_agreed": 10, "vote_disagreed": 3, "quality_correct": 5, "quality_wrong": -5,
                "daily_first": 2, "streak_7": 10, "opinion_liked": 5, "exam_passed": 20, "overturn_adjust": 7,
            }.items(), raw["points"].items())
            self.assertEqual(raw["tiers"], [
                {"key": "trainee", "label": "见习委员", "requirements": None, "demote_below": None},
                {"key": "regular", "label": "正式委员",
                 "requirements": {"votes_settled": 15, "agreement_rate": 0.7, "quality_rate": 0.7, "quality_min": 3, "days_served": 14},
                 "demote_below": {"agreement_rate": 0.6, "window": 30, "min_votes": 15, "to": "trainee"}},
                {"key": "senior", "label": "资深委员",
                 "requirements": {"votes_settled": 50, "agreement_rate": 0.8, "quality_rate": 0.8, "quality_min": 8, "days_served": 60},
                 "demote_below": {"agreement_rate": 0.7, "window": 30, "min_votes": 15, "to": "regular"}},
            ])
            self.assertEqual(config.model_dump()["tiers"], raw["tiers"])
            self.assertEqual(raw["suspend"], {"min_settled": 20, "agreement_rate": 0.6, "quality_min": 5, "quality_rate": 0.6})
            self.assertEqual(raw["exam"], {
                "total": 10, "pass_score": 8, "time_limit_minutes": 60, "grace_seconds": 90,
                "attempts_max": 3, "window_days": 30, "retry_hours": 24, "valid_days": 90,
            })
            self.assertEqual((raw["practice"], raw["recuse"]), ({"daily_limit": 20}, {"daily_limit": 5}))
            self.assertEqual(raw["abstain"], {"window": 50, "min_votes": 20, "warn_rate": 0.3, "cooldown_rate": 0.5, "cooldown_hours": 24})
            self.assertEqual((raw["rules"]["severe_min_votes"], raw["rules"]["need_context_votes"], raw["opinions"]["like_reward_threshold"]), (5, 2, 3))
            # exam_required 是后台开关，不在公开的三个开关里；余量之类的阈值后台能看到
            self.assertEqual(set(raw["switches"]), {"auto_appoint", "appeals", "machine_review"})
            thresholds = jury_settings.thresholds()
            self.assertEqual((thresholds["warning_margin"], thresholds["warning_sample_ratio"], thresholds["exam_total"]), (0.1, 0.5, 10))


# ---------------------------------------------------------------- 走一遍路由


class HttpPhase2Tests(Phase2TestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.acting = "juror1"
        api = FastAPI()
        api.include_router(jury_api.router, prefix="/jury")
        api.include_router(admin_api.router, prefix="/admin")

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

    async def test_phase_two_routes(self) -> None:
        # 这里按线上默认的方式切题：已核对的题里编号能被 3 整除的只给练习场
        splitter = patch.object(bank_service, "PRACTICE_EVERY", 3)
        splitter.start()
        self.addCleanup(splitter.stop)
        async with self.sessions() as db:
            questions = await self._questions(db, 16)
            practice_ids = [case.id for case in questions if case.id % 3 == 0]
            self.assertEqual(len(practice_ids), 5)
            private = await self._question(db, is_golden=False, verified=False, source=None, jury_eligible=False)
            first, second = await self._open_case(db), await self._open_case(db)
            await self._eligible_user(db, "carol")
        client = self.client

        me = (await client.get("/jury/me")).json()
        self.assertEqual((me["points"], me["exam_required"], me["badge_visible"], me["warnings"], me["cooldown_until"], me["recuse_left_today"]), (0, False, False, [], None, 5))
        self.assertEqual((me["exam"]["available"], me["exam"]["passed"], me["exam"]["total"], me["tier_progress"]["next_tier"]), (True, False, 10, "regular"))
        config = (await client.get("/jury/config")).json()
        self.assertEqual((config["exam"]["total"], config["recuse"]["daily_limit"], config["votes"][-1]["value"]), (10, 5, "need_context"))

        # 投票：回避、带类别、上下文不足
        with patch.object(jury_service, "_quality_roll", return_value=0.99):
            assigned = (await client.get("/jury/cases/next")).json()["case"]
            # 委员侧的编号是评审编号：先后两件的编号是连着的，和案件编号无关
            first_id = assigned["case_id"]
            self.assertEqual(assigned["suggested_category"], "harassment")
            async with self.sessions() as db:
                self.assertEqual(await self._real(db, first_id), first.id)
            self.assertEqual((await client.post(f"/jury/cases/{first.id}/vote", json={"vote": "recuse"})).status_code, 404)
            recused = (await client.post(f"/jury/cases/{first_id}/vote", json={"vote": "recuse"})).json()
            self.assertEqual((recused["case_id"], recused["case"], recused["settled"], recused["reward"]["points"]), (first_id, None, False, 0))
            self.assertEqual((await client.get(f"/jury/cases/{first_id}")).json()["detail"]["code"], "not_voted")
            second_id = (await client.get("/jury/cases/next")).json()["case"]["case_id"]
            self.assertEqual(second_id, first_id + 1)
            bad = await client.post(f"/jury/cases/{second_id}/vote", json={"vote": "l2", "category": "sexual"})
            self.assertEqual((bad.status_code, bad.json()["detail"]["code"]), (422, "invalid_category"))
            self.assertEqual((await client.post(f"/jury/cases/{second_id}/vote", json={"vote": "maybe"})).status_code, 422)
            voted = (await client.post(f"/jury/cases/{second_id}/vote", json={"vote": "l2", "category": "advertising", "note": "是广告"})).json()
            self.assertEqual((voted["case"]["my_vote"]["category"], voted["case"]["my_vote"]["points"], voted["reward"]["items"][0]["kind"]), ("advertising", 0, "daily_first"))
        votes = (await client.get("/jury/votes?vote=recuse,need_context")).json()
        self.assertEqual((votes["total"], votes["items"][0]["vote"], votes["items"][0]["can_view"], votes["items"][0]["case_id"]), (1, "recuse", False, first_id))
        # 按最近 24 小时滚动的上限：/jury/me 给出下一个名额什么时候释放
        me = (await client.get("/jury/me")).json()
        self.assertEqual((me["voted_today"], me["recuse_left_today"]), (1, 4))
        self.assertTrue(me["daily_resets_at"].endswith("Z") and me["recuse_resets_at"].endswith("Z"))
        self.assertEqual((await client.get("/jury/votes")).json()["total"], 2)
        self.assertEqual((await client.get("/jury/votes?vote=bogus")).json()["detail"]["code"], "invalid_vote_filter")

        # 风纪值、成就、排行榜、公开身份
        points = (await client.get("/jury/me/points?limit=20&offset=0")).json()
        self.assertEqual((points["total"], points["count"], points["items"][0]["kind"], points["items"][0]["label"]), (2, 1, "daily_first", "今日首票"))
        achievements = (await client.get("/jury/me/achievements")).json()["items"]
        self.assertEqual((len(achievements), achievements[0]["key"], achievements[0]["earned_at"] is not None), (12, "first_vote", True))
        settings_response = await client.put("/jury/me/settings", json={"badge_visible": True})
        self.assertEqual((settings_response.status_code, settings_response.json()["badge_visible"], settings_response.json()["status"]), (200, True, "active"))
        board = (await client.get("/jury/leaderboard?period=week&limit=50")).json()
        self.assertEqual((board["period"], board["items"][0]["points"], board["items"][0]["me"], board["items"][0]["user"]["jury_badge"]), ("week", 2, True, "trainee"))
        self.assertEqual(board["me"], board["items"][0])
        self.assertEqual((await client.get("/jury/leaderboard")).json()["period"], "week")
        self.assertEqual((await client.get("/jury/leaderboard?period=year")).status_code, 422)

        # 弃权过多被暂停发案：错误详情要能序列化
        async with self.sessions() as db:
            (await self._member(db, "juror1")).cooldown_until = datetime.now(UTC) + timedelta(hours=3)
            await db.commit()
        paused = await client.get("/jury/cases/next")
        self.assertEqual((paused.status_code, paused.json()["detail"]["code"]), (429, "jury_cooldown"))
        self.assertTrue(paused.json()["detail"]["until"].endswith("Z"))
        self.assertIsNotNone((await client.get("/jury/me")).json()["cooldown_until"])

        # 练习场：在任委员可以练
        practice = (await client.get("/jury/practice/next")).json()
        self.assertEqual(
            (practice["question"]["question_id"], practice["round"], practice["left_today"], practice["resets_at"]),
            (practice_ids[0], {"total": 5, "done": 0}, 20, None),
        )
        self.assertTrue(practice["question"]["messages"] == [] or practice["question"]["messages"][0]["created_at"].endswith("Z"))
        practiced = next(case for case in questions if case.id == practice_ids[0])
        answered = await client.post(f"/jury/practice/{practiced.id}/answer", json={"vote": "l2" if practiced.status == "resolved" else "none"})
        self.assertEqual((answered.status_code, answered.json()["correct"], answered.json()["left_today"]), (200, True, 19))
        self.assertTrue(answered.json()["resets_at"].endswith("Z"))
        self.assertEqual((await client.get("/jury/practice/next")).json()["resets_at"], answered.json()["resets_at"])
        self.assertEqual((await client.post(f"/jury/practice/{practiced.id}/answer", json={"vote": "abstain"})).status_code, 422)
        self.assertEqual((await client.post("/jury/practice/999999/answer", json={"vote": "l2"})).status_code, 404)
        # 用于考试和质控的题不在练习场里：拿它的题号来「练习」问不出答案
        self.assertEqual((await client.post(f"/jury/practice/{questions[0].id}/answer", json={"vote": "l2"})).status_code, 404)

        # 入门考试：juror1 不满足四项资格；换一个满足的普通用户
        exam = (await client.get("/jury/exam")).json()
        self.assertEqual((exam["available"], exam["eligible"], exam["active"]), (True, False, None))
        self.assertEqual((await client.post("/jury/exam/start")).json()["detail"]["code"], "jury_not_eligible")
        self.acting = "carol"
        started = await client.post("/jury/exam/start")
        self.assertEqual((started.status_code, len(started.json()["questions"])), (200, 10))
        self.assertNotIn("case_id", started.text)
        attempt_id = started.json()["attempt_id"]
        async with self.sessions() as db:
            drawn = set(json.loads((await db.get(JuryExamAttempt, attempt_id)).question_ids))
        self.assertEqual(drawn & set(practice_ids), set())
        self.assertEqual((await client.get("/jury/exam")).json()["active"]["attempt_id"], attempt_id)
        invalid = await client.post(f"/jury/exam/{attempt_id}/submit", json={"answers": [{"question": 99, "vote": "l2"}]})
        self.assertEqual((invalid.status_code, invalid.json()["detail"]["code"]), (422, "invalid_answers"))
        invalid = await client.post(f"/jury/exam/{attempt_id}/submit", json={"answers": [{"question": "一", "vote": "l2"}]})
        self.assertEqual((invalid.status_code, invalid.json()["detail"]["code"]), (422, "invalid_answers"))
        async with self.sessions() as db:
            answers = await self._exam_answers(db, attempt_id, correct=9)
        submitted = await client.post(f"/jury/exam/{attempt_id}/submit", json={"answers": answers})
        self.assertEqual((submitted.status_code, submitted.json()["score"], submitted.json()["passed"], submitted.json()["status"]["status"]), (200, 9, True, "none"))
        self.assertEqual((await client.post("/jury/exam/start")).json()["detail"]["code"], "exam_passed")
        self.assertEqual((await client.post("/jury/exam/999/submit", json={"answers": []})).status_code, 404)
        self.assertEqual((await client.put("/jury/me/settings", json={"badge_visible": True})).json()["detail"]["code"], "not_juror")
        self.assertEqual((await client.get("/admin/jury/questions")).status_code, 403)

        # 后台
        self.acting = "admin"
        listed = (await client.get("/admin/jury/questions?verified=true&source=&q=&limit=50&offset=0")).json()
        self.assertEqual((listed["total"], listed["items"][-1]["case_id"]), (16, questions[0].id))
        by_id = {item["case_id"]: item for item in listed["items"]}
        self.assertEqual((by_id[practiced.id]["answered"], by_id[practiced.id]["correct_rate"], by_id[practiced.id]["pool"]), (1, 1.0, "practice"))
        self.assertEqual({item["case_id"] for item in listed["items"] if item["pool"] == "practice"}, set(practice_ids))
        self.assertEqual(by_id[questions[0].id]["pool"], "scored")
        self.assertEqual((await client.get("/admin/jury/questions?source=consensus")).json()["total"], 0)
        detail = (await client.get(f"/admin/jury/questions/{questions[0].id}")).json()
        self.assertEqual((detail["case_id"], detail["verified"], detail["source"], detail["outcome"], detail["block_reason"]), (questions[0].id, True, "admin", "violation", None))
        updated = await client.put(f"/admin/jury/questions/{questions[0].id}", json={"explanation": "连续辱骂，不是玩笑", "verified": False})
        self.assertEqual((updated.status_code, updated.json()["explanation"], updated.json()["verified"], updated.json()["in_bank"]), (200, "连续辱骂，不是玩笑", False, True))
        # 未核对的题只给练习场
        self.assertEqual(updated.json()["pool"], "practice")
        rejected = await client.put(f"/admin/jury/questions/{private.id}", json={"in_bank": True})
        self.assertEqual((rejected.status_code, rejected.json()["detail"]["code"]), (422, "case_not_eligible"))
        self.assertEqual((await client.put(f"/admin/jury/questions/{questions[0].id}", json={"level": 9})).status_code, 422)
        self.assertEqual((await client.get("/admin/jury/questions/999999")).status_code, 404)
        exams = (await client.get("/admin/jury/exams?passed=true&limit=50&offset=0")).json()
        self.assertEqual((exams["total"], exams["items"][0]["user"]["username"], exams["items"][0]["score"], exams["items"][0]["passed"]), (1, "carol", 9, True))
        self.assertEqual((await client.get("/admin/jury/exams?passed=false")).json()["total"], 0)
        members = (await client.get("/admin/jury/members?status=active&limit=200")).json()["items"]
        member = next(item for item in members if item["user_id"] == self.ids["juror1"])
        self.assertEqual((member["points"], member["badge_visible"], member["exam_score"], member["recuse_rate"], member["abstain_rate"]), (2, True, None, 0.5, 0))
        adjusted = await client.post(f"/admin/jury/members/{self.ids['juror1']}/review", json={"action": "adjust_points", "points": 40, "note": "补偿"})
        self.assertEqual((adjusted.status_code, adjusted.json()["points"], adjusted.json()["status"]), (200, 42, "active"))
        self.assertEqual((await client.post(f"/admin/jury/members/{self.ids['juror1']}/review", json={"action": "adjust_points"})).status_code, 422)
        switches = await client.put("/admin/jury/settings", json={"exam_required": True})
        self.assertEqual((switches.status_code, switches.json()["switches"]["exam_required"], switches.json()["thresholds"]["severe_min_votes"]), (200, True, 5))
        # 管理员结案：案件进题库，但只是未核对
        async with self.sessions() as db:
            report = await self._report(db, "alice", "insult")
        resolved = await client.post(f"/admin/cases/{report.case_id}/resolve", json={"outcome": "violation", "category": "harassment"})
        self.assertEqual(resolved.status_code, 200)
        bank = (await client.get(f"/admin/jury/questions/{report.case_id}")).json()
        self.assertEqual((bank["in_bank"], bank["verified"], bank["source"], len(bank["messages"]) > 0), (True, False, "admin", True))


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------- 第二轮修复：质控题的内容时间、在质控题上回避


def _moment(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


class QualityDisguiseTests(Phase2TestCase):
    """质控题 / 考试题是早就结案的旧案件：投票之前展示的内容时间整体平移到近期。"""

    GAPS = (-900, -40, 0, 25, 600)
    MIN, MAX = timedelta(hours=2), timedelta(hours=40)

    def _old_messages(self, days: float = 30, gaps: tuple[int, ...] = GAPS) -> str:
        """一份几周前的群聊快照：被举报的那条在 days 天前，其余各条和它差 gaps 秒。时间的格式和真实快照一样（不带时区的 UTC）。"""
        target = datetime.now(UTC) - timedelta(days=days)
        messages = [
            {
                "sender_id": self.ids["offender"] if gap == 0 else self.ids["bob"],
                "content": f"第 {index} 条",
                "type": "text",
                "is_target": gap == 0,
                "created_at": (target + timedelta(seconds=gap)).replace(tzinfo=None).isoformat(),
            }
            for index, gap in enumerate(gaps)
        ]
        return json.dumps({"messages": messages, "message": next(item for item in messages if item["is_target"])})

    def _old_post(self, days: float = 30) -> str:
        return json.dumps({"post": {"content": "低价卖号", "image_urls": [], "created_at": (datetime.now(UTC) - timedelta(days=days)).replace(tzinfo=None).isoformat()}})

    async def _force_quality(self, db, name: str) -> dict:
        member = await self._member(db, name)
        member.votes_since_quality = jury_service.QUALITY_MAX_GAP + 1
        await db.commit()
        return await jury_service.next_case(db, await self._user(db, name))

    def test_the_disguised_age_is_stable_secret_and_within_range(self) -> None:
        ages = [jury_service.disguised_age("quality", seed, 1760000000) for seed in range(300)]
        self.assertTrue(all(self.MIN <= age <= self.MAX for age in ages))
        # 精确到秒，不是整点，也不是所有人都一样
        self.assertGreater(len(set(ages)), 250)
        self.assertEqual(ages, [jury_service.disguised_age("quality", seed, 1760000000) for seed in range(300)])
        self.assertNotEqual(jury_service.disguised_age("quality", 7, 1), jury_service.disguised_age("exam", 7, 1))
        # 掺了服务端密钥：只知道评审编号算不出来
        with patch.object(jury_service.settings, "jwt_secret_key", "another-secret-key-that-is-long-enough-000"):
            self.assertNotEqual([jury_service.disguised_age("quality", seed, 1760000000) for seed in range(300)], ages)
        # 范围是配置项
        with patch.object(jury_service, "QUALITY_SHIFT_MIN_HOURS", 5), patch.object(jury_service, "QUALITY_SHIFT_MAX_HOURS", 5):
            self.assertEqual(jury_service.disguised_age("x"), timedelta(hours=5))

    def test_time_shift_keeps_gaps_and_never_shows_the_future(self) -> None:
        base = datetime(2026, 10, 12, 8, 0, tzinfo=UTC)
        target, later = "2026-09-01T00:00:00.250000", "2026-09-03T03:00:00"
        # 被举报那条之后的上下文隔了两天多：整体再往前挪，让最晚的一条也落在基准时间之前 2 小时
        shift = jury_service._time_shift(target, ["2026-08-31T23:50:00", target, later], base, ("seed",))
        self.assertEqual(_moment(jury_service.snapshot_time(later, shift)), base - self.MIN)
        self.assertEqual(_moment(jury_service.snapshot_time(later, shift)) - _moment(jury_service.snapshot_time(target, shift)), _moment(later + "Z") - _moment(target + "Z"))
        # 没有能用的时间：不平移；被举报那条没有时间时以最晚的一条为准
        self.assertIsNone(jury_service._time_shift(None, [None, "昨天", ""], base, ("seed",)))
        fallback = jury_service._time_shift(None, [target, later], base, ("seed",))
        self.assertTrue(self.MIN <= base - _moment(jury_service.snapshot_time(later, fallback)) <= self.MAX)
        # 认不出来的时间原样返回，空的还是 None
        self.assertEqual((jury_service.snapshot_time("昨天", shift), jury_service.snapshot_time(None, shift)), ("昨天", None))

    async def test_quality_check_times_look_recent_until_the_vote(self) -> None:
        async with self.sessions() as db:
            question = await self._question(db, snapshot=self._old_messages())
            juror = await self._user(db, "juror1")
            view = await self._force_quality(db, "juror1")
            self.assertEqual(await self._real(db, view["case_id"]), question.id)
            JuryCaseView(**view)
            times = [_moment(item["created_at"]) for item in view["messages"]]
            target = times[self.GAPS.index(0)]
            # 被举报的那条看起来发于分到手之前 2–40 小时，各条之间的间隔一秒不差
            self.assertTrue(self.MIN <= view["assigned_at"] - target <= self.MAX, view["assigned_at"] - target)
            self.assertEqual([(moment - target).total_seconds() for moment in times], list(self.GAPS))
            self.assertTrue(all(item["created_at"].endswith("Z") for item in view["messages"]))
            # 重复取同一个案件：时间不变
            self.assertEqual((await jury_service.next_case(db, juror))["messages"], view["messages"])
        # 换一个会话（分到手的时间从库里读回来是不带时区的）也不变
        async with self.sessions() as db:
            juror = await self._user(db, "juror1")
            self.assertEqual((await jury_service.next_case(db, juror))["messages"], view["messages"])

            # 投票之后（已经揭晓是质控题）恢复真实时间
            result = await jury_service.cast_vote(db, juror, view["case_id"], "l2", None)
            self.assertTrue(result["case"]["is_quality_check"])
            for detail in (result["case"], await jury_service.case_detail(db, juror.id, view["case_id"])):
                real = [_moment(item["created_at"]) for item in detail["messages"]]
                self.assertLess(abs((datetime.now(UTC) - real[self.GAPS.index(0)]) - timedelta(days=30)), timedelta(minutes=5))
                self.assertEqual([(moment - real[self.GAPS.index(0)]).total_seconds() for moment in real], list(self.GAPS))

            # 别的委员拿到同一道题：各取各的值，也都在范围里
            other = await self._force_quality(db, "juror2")
            self.assertEqual(await self._real(db, other["case_id"]), question.id)
            other_target = _moment(other["messages"][self.GAPS.index(0)]["created_at"])
            self.assertTrue(self.MIN <= other["assigned_at"] - other_target <= self.MAX)

    async def test_posts_are_shifted_too_and_real_cases_are_left_alone(self) -> None:
        async with self.sessions() as db:
            # 真实的待评审案件不动：内容是什么时间就显示什么时间
            real_case = await self._open_case(db, snapshot=self._old_post(days=3))
            juror = await self._user(db, "juror1")
            with patch.object(jury_service, "_quality_roll", return_value=0.99):
                view = await jury_service.next_case(db, juror)
            self.assertEqual(await self._real(db, view["case_id"]), real_case.id)
            self.assertLess(abs((datetime.now(UTC) - _moment(view["post"]["created_at"])) - timedelta(days=3)), timedelta(minutes=5))
            await jury_service.cast_vote(db, juror, view["case_id"], "l2", None)

            self._serial += 1
            question = await self._question(db, target_type="post", target_key=f"post:{70000 + self._serial}", snapshot=self._old_post())
            quality = await self._force_quality(db, "juror1")
            self.assertEqual(await self._real(db, quality["case_id"]), question.id)
            age = quality["assigned_at"] - _moment(quality["post"]["created_at"])
            self.assertTrue(self.MIN <= age <= self.MAX, age)
            self.assertEqual(set(quality), set(view))
            result = await jury_service.cast_vote(db, juror, quality["case_id"], "l2", None)
            self.assertLess(abs((datetime.now(UTC) - _moment(result["case"]["post"]["created_at"])) - timedelta(days=30)), timedelta(minutes=5))

    async def test_exam_questions_are_shifted_and_practice_is_not(self) -> None:
        async with self.sessions() as db:
            for index in range(3):
                await self._question(db, upheld=index % 2 == 0, snapshot=self._old_messages(days=20 + index))
            practice = await self._question(db, verified=False, source="legacy", snapshot=self._old_messages(days=45))
            carol = await self._eligible_user(db, "carol")
            with patch.object(exam_service, "EXAM_TOTAL", 3), patch.object(exam_service, "EXAM_PASS_SCORE", 1):
                started = ExamStart(**await exam_service.start_exam(db, carol))
                attempt = await db.get(JuryExamAttempt, started.attempt_id)
                began = penalty_service.aware(attempt.started_at)
                self.assertEqual(len(started.questions), 3)
                ages = []
                for question in started.questions:
                    times = [_moment(item.created_at) for item in question.messages]
                    target = times[self.GAPS.index(0)]
                    ages.append(began - target)
                    self.assertTrue(self.MIN <= began - target <= self.MAX, began - target)
                    self.assertEqual([(moment - target).total_seconds() for moment in times], list(self.GAPS))
                # 每道题各取各的值
                self.assertEqual(len(set(ages)), 3)
                # 同一场里重复读：时间不变
                state = ExamState(**await exam_service.exam_state(db, carol))
                self.assertEqual([question.model_dump() for question in state.active.questions], [question.model_dump() for question in started.questions])
            # 练习场不动
            page = PracticeNext(**await exam_service.practice_next(db, carol))
            self.assertEqual(page.question.question_id, practice.id)
            shown = _moment(page.question.messages[self.GAPS.index(0)].created_at)
            self.assertLess(abs((datetime.now(UTC) - shown) - timedelta(days=45)), timedelta(minutes=5))


class QualityRecusalTests(Phase2TestCase):
    """在质控题上回避不算「见过」：过几天同一道题可以再出，题库不会被回避耗光。"""

    async def _take(self, db, name: str = "juror1", *, force: bool = False) -> dict | None:
        """取下一件；随机位置关掉，所以只有强制（force，或者欠着一道）时才会出质控题。"""
        if force:
            (await self._member(db, name)).votes_since_quality = jury_service.QUALITY_MAX_GAP + 1
            await db.commit()
        with patch.object(jury_service, "_quality_roll", return_value=0.99):
            return await jury_service.next_case(db, await self._user(db, name))

    async def _row(self, db, review_id: int) -> JuryVote:
        row = await db.get(JuryVote, review_id)
        await db.refresh(row)
        return row

    async def _backdate_recusal(self, db, review_id: int, days: float) -> None:
        (await self._row(db, review_id)).recused_at = datetime.now(UTC) - timedelta(days=days)
        await db.commit()

    async def _answer(self, db, view: dict, name: str = "juror1") -> dict:
        case = await db.get(ReportCase, await self._real(db, view["case_id"]))
        return await jury_service.cast_vote(db, await self._user(db, name), view["case_id"], "l2" if bank_service.question_upheld(case) else "none", None)

    async def test_recused_quality_checks_return_later_and_unseen_ones_go_first(self) -> None:
        async with self.sessions() as db:
            questions = {(await self._question(db)).id, (await self._question(db, upheld=False)).id}
            normal = await self._open_case(db)
            juror = await self._user(db, "juror1")
            member = await self._member(db, "juror1")
            self.assertEqual(jury_service.QUALITY_RECUSE_RETRY_DAYS, 7)

            first = await self._take(db, force=True)
            first_case = await self._real(db, first["case_id"])
            self.assertIn(first_case, questions)
            await jury_service.cast_vote(db, juror, first["case_id"], "recuse", "不想答")
            # 回避后下一件仍补一道质控题：从没见过的里选
            second = await self._take(db)
            second_case = await self._real(db, second["case_id"])
            self.assertEqual({first_case, second_case}, questions)
            await jury_service.cast_vote(db, juror, second["case_id"], "recuse", None)
            await db.refresh(member)
            self.assertTrue(member.force_quality)

            # 两道都刚回避过：欠着的那道宁可不出，也不再出刚回避的 —— 发的是普通案件
            third = await self._take(db)
            self.assertEqual(await self._real(db, third["case_id"]), normal.id)
            self.assertFalse((await self._row(db, third["case_id"])).is_quality_check)
            await db.refresh(member)
            self.assertTrue(member.force_quality)
            await jury_service.cast_vote(db, juror, third["case_id"], "l2", None)
            self.assertIsNone(await self._take(db))
            # 回避的记录里看不出那是质控题，也看不出案件的进展
            items, total = await jury_service.list_votes(db, juror.id, votes=["recuse"])
            self.assertEqual((total, {(item["is_quality_check"], item["stage"], item["can_view"]) for item in items}), (2, {(False, None, False)}))

            # 过了 7 天：第一道可以再出（复用原来那一行，评审编号不变）；第二道才回避 3 天，还不行
            await self._backdate_recusal(db, first["case_id"], 8)
            await self._backdate_recusal(db, second["case_id"], 3)
            again = await self._take(db)
            self.assertEqual((again["case_id"], await self._real(db, again["case_id"])), (first["case_id"], first_case))
            row = await self._row(db, again["case_id"])
            self.assertEqual((row.vote, row.note, row.recused_at, row.voted_at, row.past_recusals, row.is_quality_check), (None, None, None, None, 1, True))
            self.assertLess(datetime.now(UTC) - penalty_service.aware(row.assigned_at), timedelta(minutes=1))
            await db.refresh(member)
            self.assertFalse(member.force_quality)
            # 重复取还是这一件；回避记录里只剩第二道
            self.assertEqual((await self._take(db))["case_id"], first["case_id"])
            self.assertEqual((await jury_service.list_votes(db, juror.id, votes=["recuse"]))[1], 1)
            result = await self._answer(db, again)
            self.assertTrue(result["case"]["is_quality_check"])
            await db.refresh(member)
            self.assertEqual((member.golden_total, member.golden_correct), (1, 1))

            # 答过的才算「见过」：不会再出。第二道也到期了，但题库里新来了一道没见过的 —— 没见过的优先
            fresh = await self._question(db)
            await self._backdate_recusal(db, second["case_id"], 9)
            unseen = await self._take(db, force=True)
            self.assertEqual(await self._real(db, unseen["case_id"]), fresh.id)
            await self._answer(db, unseen)
            late = await self._take(db, force=True)
            self.assertEqual((late["case_id"], await self._real(db, late["case_id"])), (second["case_id"], second_case))
            # 再回避一次：又欠一道，但已经没有别的可出
            await jury_service.cast_vote(db, juror, late["case_id"], "recuse", None)
            self.assertIsNone(await self._take(db))
            row = await self._row(db, second["case_id"])
            self.assertEqual((row.vote, row.past_recusals), ("recuse", 1))
            self.assertIsNotNone(row.recused_at)

            # 后台：在质控题上回避了几次（再出之后又回避的重复计），回避率照样算上被清掉的那几次
            items, _total = await jury_service.list_members(db, "active", 50, 0)
            reads = {item["user_id"]: JuryMemberRead(**item) for item in items}
            mine = reads[juror.id]
            self.assertEqual((mine.votes_total, mine.quality_recused, mine.recuse_rate, mine.golden_total), (3, 3, 0.5, 2))
            self.assertEqual((reads[self.ids["juror2"]].quality_recused, reads[self.ids["juror2"]].recuse_rate), (0, 0))

    async def test_recusing_real_cases_is_not_counted_as_dodging_quality_checks(self) -> None:
        async with self.sessions() as db:
            question = await self._question(db)
            real = await self._open_case(db)
            # 真实案件上的回避是永久的：过多久都不会再分给他，哪怕这个案件后来进了计分题库
            await self._cast(db, "juror1", real.id, "recuse")
            for name in ("juror2", "juror3", "juror4"):
                await self._cast(db, name, real.id, "l2")
            await db.refresh(real)
            real.is_golden, real.golden_verified, real.golden_source = True, True, "consensus"
            await db.commit()
            await self._backdate_recusal(db, await self._rid(db, "juror1", real.id), 30)
            view = await self._take(db, force=True)
            self.assertEqual(await self._real(db, view["case_id"]), question.id)
            await self._answer(db, view)
            self.assertIsNone(await self._take(db, force=True))
            items, _total = await jury_service.list_members(db, "active", 50, 0)
            mine = {item["user_id"]: item for item in items}[self.ids["juror1"]]
            self.assertEqual((mine["quality_recused"], mine["recuse_rate"]), (0, 0.5))
