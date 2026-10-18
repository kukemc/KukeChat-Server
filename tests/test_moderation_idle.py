"""一审风险分、空闲复查（docs/moderation-v2.md 1.11）、申诉预审（1.3）、题库体检（jury-v2.md 2.1.1）、机器案件名额。

二审一律是假的（替换 moderation_judge.judge），只有排队顺序的测试用真正的队列、替换发请求的 _post_judge。
"""

from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Annotated
from unittest.mock import patch

import httpx
from fastapi import Depends, FastAPI, HTTPException
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1 import admin as admin_api
from app.core.database import get_db
from app.core.dependencies import get_current_user

from app.core.config import settings
from app.models.case import ReportCase
from app.models.conversation import Conversation
from app.models.device import DeviceBan, DeviceSignal
from app.models.enums import ConversationType, ModerationSource, ReportTargetType, UserStatus
from app.models.message import Message
from app.models.penalty import UserViolation
from app.models.report import ContentModerationRecord, UserReport
from app.models.system import SystemSetting
from app.models.user import User
from app.services import jury_bank as bank_service
from app.services import jury_bank_audit as audit_service
from app.services import jury_machine as machine_service
from app.services import moderation as moderation_service
from app.services import moderation_idle as idle_service
from app.services import moderation_judge
from app.services import penalties as penalty_service

from test_jury_v2_phase3 import APPEAL_TEXT, UNCERTAIN
from test_moderation_judge import FakeJudge, JudgeTestCase


async def _no_pace() -> None:
    return None


class IdleTestCase(JudgeTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        idle_service.reset_state()
        self.addCleanup(idle_service.reset_state)
        # 夹具里的账号都是刚注册的：先变成老账号，「新账号」这条信号单独测
        async with self.sessions() as db:
            await db.execute(update(User).values(created_at=datetime.now(UTC) - timedelta(days=60)))
            await db.commit()

    async def _allowed(self, db, name: str, *, risk: float | None = None, minutes: int = 10, **fields) -> ContentModerationRecord:
        """这条消息的一审：放行（发出 minutes 分钟前审的）。"""
        message = await db.get(Message, self.messages[name] if isinstance(name, str) else name)
        values = dict(
            source=ModerationSource.MESSAGE_AUTO, message_id=message.id, conversation_id=message.conversation_id, user_id=message.sender_id,
            content_type="text", content_text=message.content, decision="allow", severity=0, risk_score=risk,
            created_at=datetime.now(UTC) - timedelta(minutes=minutes),
        )
        values.update(fields)
        record = ContentModerationRecord(**values)
        db.add(record)
        await db.commit()
        return record

    async def _candidates(self) -> dict[int, list[str]]:
        idle_service.reset_state()
        async with self.sessions() as db:
            found = await idle_service.select_candidates(db, datetime.now(UTC))
        return {item.message_id: item.reasons for item in found}

    @contextmanager
    def _scan(self, *results: float | None):
        fake = FakeJudge(*results)
        with patch.object(moderation_judge, "is_active", lambda: True), patch.object(moderation_judge, "judge", fake), patch.object(
            idle_service, "_pace", _no_pace
        ):
            yield fake


# ---------------------------------------------------------------- 一审风险分


class RiskScoreTests(IdleTestCase):
    async def test_risk_score_is_stored_for_text_only(self) -> None:
        await self._run("insult", {**UNCERTAIN, "risk_score": 6.5}, 0.5)
        async with self.sessions() as db:
            self.assertEqual((await self._record(db, "insult")).risk_score, 6.5)
        # 统一入口 /moderate：文本结果顶层带；图片没有；不是数、超出 0–10 的当没有
        unified = {"results": [{"type": "image", "decision": "allow", "risk_score": 9}, {"type": "text", "decision": "allow", "risk_score": 3.25}]}
        self.assertEqual(moderation_service._normalize_result(unified, "text")["risk_score"], 3.25)
        self.assertIsNone(moderation_service._normalize_result(unified, "image")["risk_score"])
        for value in (11, -1, "7", True, None):
            self.assertIsNone(moderation_service._normalize_result({"decision": "allow", "risk_score": value}, "text")["risk_score"])
        # 老版本审核服务没有这个字段
        await self._moderate(self.messages["insult2"], dict(UNCERTAIN))
        async with self.sessions() as db:
            self.assertIsNone((await self._record(db, "insult2")).risk_score)

    async def test_live_judgements_carry_the_risk_score(self) -> None:
        fake = await self._run("insult", {**UNCERTAIN, "risk_score": 8.0}, 0.5)
        self.assertEqual(fake.calls[0]["risk"], 8.0)


class QueueOrderTests(IdleTestCase):
    @contextmanager
    def _service(self, post):
        values = {"content_moderation_enabled": True, "content_moderation_url": "http://moderation.test", "moderation_judge_enabled": True}
        with patch.multiple(settings, **values), patch.object(moderation_judge, "_post_judge", post):
            yield

    async def test_higher_risk_first_within_a_tier_and_background_waits_for_live_work(self) -> None:
        order: list[str] = []
        release = asyncio.Event()
        loop = asyncio.get_running_loop()

        def post(payload, timeout):
            order.append(payload["text"])
            if payload["text"] == "first":
                asyncio.run_coroutine_threadsafe(release.wait(), loop).result(timeout=5)
            return moderation_judge.JudgeVerdict(p=0.5, latency_ms=1, model="m")

        self.assertTrue(moderation_judge.background_idle())
        with self._service(post):
            first = asyncio.create_task(moderation_judge.judge("first", priority=moderation_judge.PRIORITY_FLAGGED))
            while not order:
                await asyncio.sleep(0.01)
            # 实时的活在路上：后台的活不该放进来
            self.assertFalse(moderation_judge.background_idle())
            jobs = [
                asyncio.create_task(moderation_judge.judge(text, priority=priority, risk=risk))
                for text, priority, risk in (
                    ("idle", moderation_judge.PRIORITY_IDLE, 9.0),
                    ("review-low", moderation_judge.PRIORITY_FLAGGED, 1.0),
                    ("review-none", moderation_judge.PRIORITY_FLAGGED, None),
                    ("review-high", moderation_judge.PRIORITY_FLAGGED, 8.0),
                    ("block", moderation_judge.PRIORITY_BLOCK, 0.0),
                    ("report-high", moderation_judge.PRIORITY_REPORT, 9.5),
                )
            ]
            await asyncio.sleep(0.05)
            release.set()
            await asyncio.gather(first, *jobs)
        self.assertEqual(order, ["first", "block", "review-high", "review-low", "review-none", "report-high", "idle"])
        self.assertTrue(moderation_judge.background_idle())


# ---------------------------------------------------------------- 空闲复查：选消息


class SelectionTests(IdleTestCase):
    async def test_no_signal_no_candidate(self) -> None:
        async with self.sessions() as db:
            await self._allowed(db, "insult")
        self.assertEqual(await self._candidates(), {})

    async def test_new_account(self) -> None:
        async with self.sessions() as db:
            await self._allowed(db, "insult")
            (await db.get(User, self.ids["offender"])).created_at = datetime.now(UTC) - timedelta(days=2)
            await db.commit()
        self.assertEqual(await self._candidates(), {self.messages["insult"]: [idle_service.REASON_NEW_ACCOUNT]})

    async def test_linked_to_a_banned_device(self) -> None:
        async with self.sessions() as db:
            await self._allowed(db, "insult")
            db.add(DeviceSignal(user_id=self.ids["offender"], kind="device", value_hash="dev-1"))
            db.add(DeviceBan(kind="device", value_hash="dev-1"))
            # 只靠 IP 的弱关联不算
            db.add(DeviceSignal(user_id=self.ids["bob"], kind="ip", value_hash="10.0.0.1"))
            db.add(DeviceBan(kind="ip", value_hash="10.0.0.1"))
            await self._allowed(db, "greeting")
        self.assertEqual(await self._candidates(), {self.messages["insult"]: [idle_service.REASON_LINKED]})
        # 已经解除的设备封禁不算
        async with self.sessions() as db:
            await db.execute(update(DeviceBan).values(lifted_at=datetime.now(UTC)))
            await db.commit()
        self.assertEqual(await self._candidates(), {})

    async def test_linked_to_a_banned_account(self) -> None:
        async with self.sessions() as db:
            await self._allowed(db, "insult")
            banned = User(username="banned-main", hashed_password="x", status=UserStatus.DISABLED, banned_at=datetime.now(UTC) - timedelta(days=3))
            db.add(banned)
            await db.flush()
            db.add(DeviceSignal(user_id=banned.id, kind="fp", value_hash="fp-1"))
            db.add(DeviceSignal(user_id=self.ids["offender"], kind="fp", value_hash="fp-1"))
            await db.commit()
        self.assertEqual(await self._candidates(), {self.messages["insult"]: [idle_service.REASON_LINKED]})

    async def test_recent_penalty(self) -> None:
        async with self.sessions() as db:
            await self._allowed(db, "insult")
            now = datetime.now(UTC)
            db.add(UserViolation(user_id=self.ids["offender"], category="other", level=1, source="admin", created_at=now - timedelta(days=5), expires_at=now + timedelta(days=85)))
            # 撤销了的、太久以前的不算
            db.add(UserViolation(user_id=self.ids["bob"], category="other", level=1, source="admin", status="revoked", created_at=now - timedelta(days=5), expires_at=now))
            db.add(UserViolation(user_id=self.ids["alice"], category="other", level=1, source="admin", created_at=now - timedelta(days=40), expires_at=now))
            await db.commit()
            await self._allowed(db, "greeting")
            await self._allowed(db, "reply")
        self.assertEqual(await self._candidates(), {self.messages["insult"]: [idle_service.REASON_PENALIZED]})

    async def test_recently_reported(self) -> None:
        async with self.sessions() as db:
            await self._allowed(db, "insult")
            db.add(UserReport(reporter_id=self.ids["alice"], target_type=ReportTargetType.USER, target_id=self.ids["offender"], reported_user_id=self.ids["offender"], reason="辱骂骚扰"))
            await db.commit()
        self.assertEqual(await self._candidates(), {self.messages["insult"]: [idle_service.REASON_REPORTED]})

    async def test_conversation_with_many_reports(self) -> None:
        async with self.sessions() as db:
            await self._allowed(db, "greeting")
            await self._allowed(db, "plaza_hello")
            for reporter in ("alice", "bob", "juror1"):
                db.add(UserReport(reporter_id=self.ids[reporter], target_type=ReportTargetType.CONVERSATION, target_id=self.small_id, conversation_id=self.small_id, reason="其他"))
            # 大厅只有两次
            for reporter in ("alice", "bob"):
                db.add(UserReport(reporter_id=self.ids[reporter], target_type=ReportTargetType.CONVERSATION, target_id=self.plaza_id, conversation_id=self.plaza_id, reason="其他"))
            await db.commit()
        self.assertEqual(await self._candidates(), {self.messages["greeting"]: [idle_service.REASON_HOT_CONVERSATION]})

    async def test_high_first_stage_risk_score_and_ordering(self) -> None:
        async with self.sessions() as db:
            await self._allowed(db, "greeting", risk=4.0)
            await self._allowed(db, "reply", risk=2.9)
            await self._allowed(db, "plaza_hello", risk=7.5)
            # 作者有风险但风险分低：排在风险分高的后面；同样没有风险分的按作者身上的信号多少排
            await self._allowed(db, "insult")
            await self._allowed(db, "spam")
            now = datetime.now(UTC)
            db.add(UserViolation(user_id=self.ids["offender"], category="other", level=1, source="admin", created_at=now, expires_at=now + timedelta(days=90)))
            (await db.get(User, self.ids["alice"])).created_at = now
            await db.commit()
        idle_service.reset_state()
        async with self.sessions() as db:
            found = await idle_service.select_candidates(db, datetime.now(UTC))
        self.assertEqual(
            [item.message_id for item in found],
            [self.messages["plaza_hello"], self.messages["greeting"], self.messages["reply"], self.messages["insult"], self.messages["spam"]],
        )
        self.assertEqual(found[0].reasons, ["一审风险分 7.5"])
        # alice 是新账号，2.9 不够线但作者有信号
        self.assertEqual(found[2].reasons, [idle_service.REASON_NEW_ACCOUNT])

    async def test_skips_dms_recalled_judged_flagged_too_new_too_old_and_cased(self) -> None:
        async with self.sessions() as db:
            (await db.get(User, self.ids["offender"])).created_at = datetime.now(UTC)
            direct = Conversation(type=ConversationType.DIRECT)
            db.add(direct)
            await db.flush()
            dm = Message(conversation_id=direct.id, sender_id=self.ids["offender"], content="私聊里的话")
            db.add(dm)
            await db.commit()
            await self._allowed(db, dm.id, risk=9)
            (await db.get(Message, self.messages["insult"])).recalled_at = datetime.now(UTC)
            await db.commit()
            await self._allowed(db, "insult", risk=9)
            await self._allowed(db, "insult2", risk=9, judge_outcome="idle_scan", judge_p=0.2)
            await self._allowed(db, "insult3", risk=9, decision="review")
            await self._allowed(db, "spam", risk=9, minutes=1)
            await self._allowed(db, "plaza_insult", risk=9, minutes=60 * 7)
            await self._allowed(db, "club_insult", risk=9)
            db.add(ReportCase(target_key=f"message:{self.messages['club_insult']}", target_type="message", target_id=self.messages["club_insult"], message_id=self.messages["club_insult"]))
            await db.commit()
        self.assertEqual(await self._candidates(), {})


# ---------------------------------------------------------------- 空闲复查：一轮


class RoundTests(IdleTestCase):
    async def test_suspicious_message_becomes_an_idle_scan_case_without_punishment(self) -> None:
        async with self.sessions() as db:
            await self._allowed(db, "insult", risk=5)
            await self._allowed(db, "insult2", risk=4)
        with self._scan(0.83, 0.3) as fake:
            result = await idle_service.run_round(self.sessions)
        self.assertEqual((result.status, result.judged, result.cases), ("done", 2, 1))
        self.assertEqual(fake.calls[0]["priority"], moderation_judge.PRIORITY_IDLE)
        self.assertEqual(fake.calls[0]["risk"], 5)
        self.assertEqual(fake.calls[0]["context"], ["成员A：大家好"])
        self.assertEqual(fake.calls[0]["after"], ["成员B：别这样说话", "被举报人：再说一遍，废物", "被举报人：加我领福利"])
        async with self.sessions() as db:
            record = await self._record(db, "insult")
            self.assertEqual((record.decision, record.judge_outcome, record.judge_p), ("allow", "idle_scan", 0.83))
            self.assertEqual(((await self._record(db, "insult2")).judge_outcome, (await self._record(db, "insult2")).judge_p), ("idle_scan", 0.3))
            (case,) = await self._machine_cases(db)
            self.assertEqual((case.message_id, case.machine_source, case.machine_decision, case.queue, case.judge_p), (self.messages["insult"], "idle_scan", "allow", "jury", 0.83))
            self.assertEqual(case.primary_reason, "机器筛查 · 空闲复查")
            self.assertEqual(case.machine_note, "空闲复查：一审风险分 5.0；二审 0.83")
            self.assertLess(case.priority, 0)
            self.assertFalse(await self._recalled(db, "insult"))
            self.assertEqual(await self._violations(db, "insult"), [])
        # 不会再问第二遍
        with self._scan(0.99) as fake:
            result = await idle_service.run_round(self.sessions)
        self.assertEqual((result.status, fake.calls), ("empty", []))

    async def test_waits_while_live_work_is_queued_here_or_elsewhere(self) -> None:
        async with self.sessions() as db:
            await self._allowed(db, "insult", risk=5)
        with self._scan(0.9) as fake, patch.object(idle_service, "IDLE_WAIT_SECONDS", 0), patch.object(moderation_judge, "background_idle", lambda: False):
            result = await idle_service.run_round(self.sessions)
        self.assertEqual((result.status, fake.calls), ("busy", []))
        # 别的进程有一条在等二审
        async with self.sessions() as db:
            await self._allowed(db, "greeting", judge_outcome="pending", decision="review", minutes=0)
        with self._scan(0.9) as fake, patch.object(idle_service, "IDLE_WAIT_SECONDS", 0):
            result = await idle_service.run_round(self.sessions)
        self.assertEqual((result.status, fake.calls), ("busy", []))
        async with self.sessions() as db:
            self.assertIsNone((await self._record(db, "insult")).judge_outcome)

    async def test_unavailable_judge_releases_the_claim_and_backs_off(self) -> None:
        async with self.sessions() as db:
            await self._allowed(db, "insult", risk=5)
        with self._scan(None) as fake:
            result = await idle_service.run_round(self.sessions)
            self.assertEqual(result.status, "unavailable")
            self.assertEqual((await idle_service.run_round(self.sessions)).status, "backoff")
        self.assertEqual(len(fake.calls), 1)
        async with self.sessions() as db:
            record = await self._record(db, "insult")
            self.assertEqual((record.judge_outcome, record.judge_p), (None, None))

    async def test_only_the_lease_holder_scans(self) -> None:
        async with self.sessions() as db:
            self.assertTrue(await idle_service.acquire_lease(db, "worker-a"))
            self.assertTrue(await idle_service.acquire_lease(db, "worker-a"))
            self.assertFalse(await idle_service.acquire_lease(db, "worker-b"))
            await self._allowed(db, "insult", risk=5)
        with self._scan(0.9) as fake:
            self.assertEqual((await idle_service.run_round(self.sessions)).status, "not_leader")
        self.assertEqual(fake.calls, [])
        # 持有者不续约，过期后别人接手
        async with self.sessions() as db:
            await db.execute(
                update(SystemSetting).where(SystemSetting.key == idle_service.LEASE_KEY).values(updated_at=datetime.now(UTC) - timedelta(seconds=idle_service.LEASE_SECONDS + 5))
            )
            await db.commit()
            self.assertTrue(await idle_service.acquire_lease(db, "worker-b"))
            await idle_service.release_lease(db, "worker-b")
            self.assertTrue(await idle_service.acquire_lease(db, "worker-c"))

    async def test_switches(self) -> None:
        async with self.sessions() as db:
            await self._allowed(db, "insult", risk=5)
        for switch in ("machine_idle_scan", "machine_review", "machine_judge"):
            async with self.sessions() as db:
                await self._switch(db, **{switch: False})
            with self._scan(0.9) as fake:
                self.assertEqual((await idle_service.run_round(self.sessions)).status, "disabled")
            self.assertEqual(fake.calls, [])
            async with self.sessions() as db:
                await self._switch(db, **{switch: True})
        with self._scan(0.9), patch.object(settings, "moderation_idle_scan_enabled", False):
            self.assertEqual((await idle_service.run_round(self.sessions)).status, "disabled")

    async def test_pacing_spreads_calls_over_the_hour(self) -> None:
        sleeps: list[float] = []

        async def fake_sleep(seconds):
            sleeps.append(seconds)

        with patch.object(settings, "moderation_idle_scan_hourly_cap", 600), patch.object(idle_service.asyncio, "sleep", fake_sleep):
            await idle_service._pace()
            await idle_service._pace()
        self.assertEqual(len(sleeps), 1)
        self.assertGreater(sleeps[0], 5.5)
        self.assertLessEqual(sleeps[0], 6.0)


# ---------------------------------------------------------------- 名额


class CapTests(IdleTestCase):
    async def test_idle_scan_has_its_own_share_of_the_daily_cap(self) -> None:
        async with self.sessions() as db:
            with patch.object(machine_service, "DAILY_MIN", 7), patch.object(machine_service, "DAILY_PER_JUROR", 0):
                # 7 × 0.3 向下取整 = 2
                self.assertEqual(await machine_service.caps(db, datetime.now(UTC)), {"total": 7, "idle_scan": 2, "regular": 5})
                await self._switch(db, machine_idle_scan=False)
                self.assertEqual(await machine_service.caps(db, datetime.now(UTC)), {"total": 7, "idle_scan": 0, "regular": 7})
                await self._switch(db, machine_idle_scan=True)
            for name in ("insult", "insult2", "insult3"):
                await self._allowed(db, name, risk=9)
        with patch.object(machine_service, "DAILY_MIN", 7), patch.object(machine_service, "DAILY_PER_JUROR", 0):
            with self._scan(0.9, 0.9, 0.9) as fake:
                result = await idle_service.run_round(self.sessions)
            # 第三条问完发现复查那一份满了：不建案，这一轮收工
            self.assertEqual((result.status, result.cases, len(fake.calls)), ("cap_full", 2, 3))
            # 拿不准的照样有自己的名额
            for name in ("spam", "plaza_insult"):
                await self._run(name, UNCERTAIN, 0.5)
            await self._run("club_insult", UNCERTAIN, 0.5)
            async with self.sessions() as db:
                cases = await self._machine_cases(db)
                self.assertEqual([case.machine_source for case in cases], ["idle_scan", "idle_scan", "review", "review", "review"])
                # 复查那一份满了，下一轮问都不问
                with self._scan(0.9) as fake:
                    self.assertEqual((await idle_service.run_round(self.sessions)).status, "cap_full")
                self.assertEqual(fake.calls, [])

    async def test_flagged_and_downgraded_review_tier_items_are_eligible(self) -> None:
        # 二审概率在放行线和处罚线之间的：拿不准的（flagged）、一审 block 被降级的（downgraded）都进，降级的不占名额。
        # 名额只有 1 件时 1 × 0.3 向下取整 = 0，全给拿不准的
        from test_moderation_judge import BLOCK

        with patch.object(machine_service, "DAILY_MIN", 1), patch.object(machine_service, "DAILY_PER_JUROR", 0):
            await self._run("insult", UNCERTAIN, 0.5)
            await self._run("insult2", BLOCK, 0.5)
            await self._run("insult3", UNCERTAIN, 0.89)
        async with self.sessions() as db:
            cases = await self._machine_cases(db)
            self.assertEqual([(case.message_id, case.machine_source) for case in cases], [(self.messages["insult"], "review"), (self.messages["insult2"], "downgraded")])


# ---------------------------------------------------------------- 申诉预审


class AppealJudgeTests(IdleTestCase):
    async def test_machine_punishment_appeal_is_judged_and_low_p_goes_first(self) -> None:
        async with self.sessions() as db:
            low = await self._machine_violation(db)
            high = await self._machine_violation(db)
            unjudged = await self._machine_violation(db)
            jury = await self._machine_violation(db, source="jury")
            for violation in (low, high, unjudged, jury):
                await penalty_service.submit_appeal(db, violation.user_id, violation.id, APPEAL_TEXT)
        with self._judge(0.08, 0.95) as fake:
            self.assertEqual(await moderation_service.judge_appeal(low.id, self.sessions), 0.08)
            self.assertEqual(await moderation_service.judge_appeal(high.id, self.sessions), 0.95)
            # 不是机器处罚的不问
            self.assertIsNone(await moderation_service.judge_appeal(jury.id, self.sessions))
        self.assertEqual(len(fake.calls), 2)
        self.assertEqual(fake.calls[0]["text"], "大厅里的辱骂")
        self.assertEqual(fake.calls[0]["context"], ["成员A：有人吗"])
        self.assertEqual(fake.calls[0]["priority"], moderation_judge.PRIORITY_REPORT)
        async with self.sessions() as db:
            items, _total = await penalty_service.list_violations(db, appeal_status="pending")
            self.assertEqual([item.id for item in items][:3], [low.id, high.id, max(unjudged.id, jury.id)])
            reads = {item["id"]: item for item in await penalty_service.build_admin_reads(db, items)}
            self.assertEqual((reads[low.id]["appeal_judge_p"], reads[low.id]["appeal_possible_error"]), (0.08, True))
            self.assertEqual((reads[high.id]["appeal_judge_p"], reads[high.id]["appeal_possible_error"]), (0.95, False))
            self.assertIsNone(reads[unjudged.id]["appeal_judge_p"])
            # 只排先后，从不自动改判
            for violation_id in (low.id, high.id):
                violation = await db.get(UserViolation, violation_id)
                self.assertEqual((violation.appeal_status, violation.status), ("pending", "active"))

    async def test_switch_off_or_unavailable_leaves_no_probability(self) -> None:
        async with self.sessions() as db:
            violation = await self._machine_violation(db)
            await penalty_service.submit_appeal(db, violation.user_id, violation.id, APPEAL_TEXT)
        with self._judge(None):
            self.assertIsNone(await moderation_service.judge_appeal(violation.id, self.sessions))
        async with self.sessions() as db:
            await self._switch(db, machine_judge=False)
        with self._judge(0.1) as fake:
            self.assertIsNone(await moderation_service.judge_appeal(violation.id, self.sessions))
        self.assertEqual(fake.calls, [])
        async with self.sessions() as db:
            self.assertIsNone((await db.get(UserViolation, violation.id)).appeal_judge_p)


# ---------------------------------------------------------------- 题库体检


def _snapshot(*lines: tuple[str, str, str, bool]) -> str:
    """(发言人 id, 类型, 内容, 是否被举报的那条)"""
    return json.dumps(
        {"messages": [{"sender_id": sender, "type": kind, "content": content, "is_target": target} for sender, kind, content, target in lines]},
        ensure_ascii=False,
    )


class BankAuditTests(IdleTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self._serial = 0

    async def _question(self, db, *, upheld: bool, snapshot: str) -> ReportCase:
        self._serial += 1
        case = ReportCase(
            target_key=f"message:{70000 + self._serial}", target_type="message", target_id=70000 + self._serial, reported_user_id=self.ids["offender"],
            jury_eligible=True, status="resolved" if upheld else "dismissed", queue="admin", verdict_source="admin",
            verdict_category="harassment" if upheld else None, verdict_level=2 if upheld else None, primary_reason="辱骂骚扰",
            closed_at=datetime.now(UTC) - timedelta(days=10), is_golden=True, golden_verified=True, golden_source="admin", case_origin="machine",
            snapshot=snapshot, judge_p=0.5,
        )
        db.add(case)
        await db.commit()
        return case

    async def test_flags_questions_whose_answer_disagrees_with_the_judge(self) -> None:
        async with self.sessions() as db:
            missed = await self._question(db, upheld=True, snapshot=_snapshot(("1", "text", "你好", False), ("2", "text", "笑死我了", True), ("1", "text", "？", False)))
            harsh = await self._question(db, upheld=False, snapshot=_snapshot(("2", "text", "滚，废物", True)))
            fine = await self._question(db, upheld=False, snapshot=_snapshot(("2", "text", "打一把吗", True)))
            image = await self._question(db, upheld=True, snapshot=_snapshot(("2", "image", None, True)))
        with patch.object(moderation_judge, "is_active", lambda: True), patch.object(moderation_judge, "judge", FakeJudge(0.05, 0.92, 0.3)) as fake:
            async with self.sessions() as db:
                state = await audit_service.start_audit(db, self.ids["admin"], self.sessions)
                self.assertEqual((state["status"], state["total"]), ("running", 4))
                with self.assertRaises(HTTPException) as caught:
                    await audit_service.start_audit(db, self.ids["admin"], self.sessions)
                self.assertEqual(caught.exception.detail["code"], "audit_running")
            await asyncio.gather(*list(audit_service._tasks))
        self.assertEqual(fake.calls[0]["text"], "笑死我了")
        self.assertEqual((fake.calls[0]["context"], fake.calls[0]["after"]), (["成员A：你好"], ["成员A：？"]))
        self.assertEqual(fake.calls[0]["priority"], moderation_judge.PRIORITY_AUDIT)
        async with self.sessions() as db:
            state = await audit_service.audit_status(db)
            self.assertEqual({key: state[key] for key in ("status", "total", "done", "flagged", "skipped", "suspect_total")}, {"status": "done", "total": 4, "done": 4, "flagged": 2, "skipped": 1, "suspect_total": 2})
            for case, flag, p in ((missed, "suspect", 0.05), (harsh, "suspect", 0.92), (fine, "ok", 0.3), (image, "skipped", None)):
                row = await db.get(ReportCase, case.id)
                await db.refresh(row)
                self.assertEqual(row.bank_audit_flag, flag)
                if p is not None:
                    self.assertEqual(row.judge_p, p)
                # 答案不动
                self.assertEqual((row.golden_outcome, row.status), (None, case.status))
            items, total = await bank_service.list_questions(db, audit="suspect")
            self.assertEqual((total, sorted(item["case_id"] for item in items)), (2, sorted([missed.id, harsh.id])))
            self.assertEqual({item["case_id"]: item["audit_flag"] for item in items}, {missed.id: "suspect", harsh.id: "suspect"})

    async def test_stale_run_is_interrupted_and_judge_off_refuses(self) -> None:
        async with self.sessions() as db:
            self.assertEqual((await audit_service.audit_status(db))["status"], "idle")
            with self.assertRaises(HTTPException) as caught:
                await audit_service.start_audit(db, self.ids["admin"], self.sessions)
            self.assertEqual(caught.exception.detail["code"], "judge_unavailable")
            await audit_service._save(db, {"status": "running", "run_id": "old", "total": 3, "done": 1})
            await db.commit()
            await db.execute(update(SystemSetting).where(SystemSetting.key == audit_service.STATE_KEY).values(updated_at=datetime.now(UTC) - timedelta(hours=1)))
            await db.commit()
            self.assertEqual((await audit_service.audit_status(db))["status"], "interrupted")

    def test_flag_thresholds(self) -> None:
        self.assertEqual(audit_service.audit_flag(True, 0.1), "suspect")
        self.assertEqual(audit_service.audit_flag(True, 0.11), "ok")
        self.assertEqual(audit_service.audit_flag(False, 0.8), "suspect")
        self.assertEqual(audit_service.audit_flag(False, 0.79), "ok")


# ---------------------------------------------------------------- 后台接口


class AdminRouteTests(IdleTestCase):
    async def test_routes_expose_the_new_fields(self) -> None:
        api = FastAPI()
        api.include_router(admin_api.router, prefix="/admin")

        async def isolated_db():
            async with self.sessions() as session:
                yield session

        async def acting_user(db: Annotated[AsyncSession, Depends(get_db)]) -> User:
            return await db.get(User, self.ids["admin"])

        api.dependency_overrides[get_db] = isolated_db
        api.dependency_overrides[get_current_user] = acting_user
        async with self.sessions() as db:
            violation = await self._machine_violation(db)
            await penalty_service.submit_appeal(db, violation.user_id, violation.id, APPEAL_TEXT)
            (await db.get(UserViolation, violation.id)).appeal_judge_p = 0.05
            await db.commit()
        await self._run("insult", {**UNCERTAIN, "risk_score": 6.5}, 0.5)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://testserver") as client:
            # /jury/questions/audit 没有被 /jury/questions/{case_id} 吃掉
            response = await client.get("/admin/jury/questions/audit")
            self.assertEqual((response.status_code, response.json()["status"]), (200, "idle"))
            self.assertEqual((await client.get("/admin/jury/questions", params={"audit": "suspect"})).json()["total"], 0)
            appeals = (await client.get("/admin/violations", params={"appeal_status": "pending"})).json()["items"]
            self.assertEqual((appeals[0]["appeal_judge_p"], appeals[0]["appeal_possible_error"]), (0.05, True))
            records = (await client.get("/admin/moderation-records")).json()
            self.assertIn(6.5, [item["risk_score"] for item in records])
            cases = (await client.get("/admin/cases")).json()["items"]
            self.assertEqual(cases[0]["machine_source"], "review")
            settings_read = (await client.get("/admin/jury/settings")).json()
            self.assertTrue(settings_read["switches"]["machine_idle_scan"])
            self.assertEqual(settings_read["thresholds"]["idle_scan_threshold"], 0.6)
