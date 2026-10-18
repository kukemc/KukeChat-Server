"""二审（docs/moderation-v2.md 1.10）：一审标出来的文本消息和被举报的消息交给判定模型再看一遍上下文。

二审服务一律是假的：级联规则的测试直接替换 moderation_judge.judge，队列 / 超时 / 降级的测试替换真正发请求的 _post_judge。
"""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import patch
from urllib.error import URLError

from sqlalchemy import func, select

from app.core.config import settings
from app.models.case import ReportCase
from app.models.conversation import Conversation
from app.models.enums import ConversationType, MessageType, ModerationAction, ModerationSource
from app.models.message import Message
from app.models.penalty import UserViolation
from app.models.report import ContentModerationRecord
from app.services import cases as case_service
from app.services import jury_machine as machine_service
from app.services import moderation as moderation_service
from app.services import moderation_judge

from test_jury_v2_phase3 import UNCERTAIN, Phase3TestCase

BLOCK = {"decision": "block", "severity": 2, "categories": ["harassment"], "category": "harassment", "level": 2, "confidence": 0.97, "reason": "辱骂", "model": "Gandi-AIHub"}
ERROR = {**UNCERTAIN, "confidence": 0.0, "error_code": "upstream_failed", "reason": "上游模型出错"}
ALLOW = {"decision": "allow", "severity": 0, "categories": []}


class FakeJudge:
    """替换 moderation_judge.judge：按调用顺序返回给定的概率（None = 二审不可用），记下每次送出去的内容。"""

    def __init__(self, *results: float | None):
        self.results = list(results)
        self.calls: list[dict] = []

    async def __call__(self, text, context=None, after=None, *, priority, risk=None, mode=moderation_judge.MODE_REPORT):
        self.calls.append({"text": text, "context": list(context or []), "after": list(after or []), "priority": priority, "risk": risk, "mode": mode})
        p = self.results.pop(0) if self.results else None
        return None if p is None else moderation_judge.JudgeVerdict(p=p, latency_ms=4800, model="startlux-test")


class JudgeTestCase(Phase3TestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        moderation_judge._unavailable_until = 0.0
        async with self.sessions() as db:
            await self._switch(db, machine_review=True)

    async def asyncTearDown(self) -> None:
        await moderation_judge.stop_worker()
        await super().asyncTearDown()

    @contextmanager
    def _judge(self, *results: float | None):
        fake = FakeJudge(*results)
        with patch.object(moderation_judge, "is_active", lambda: True), patch.object(moderation_judge, "judge", fake):
            yield fake

    async def _run(self, name: str, result: dict, *results: float | None, source: ModerationSource = ModerationSource.MESSAGE_AUTO) -> FakeJudge:
        with self._judge(*results) as fake:
            await self._moderate(self.messages[name], result, source)
        return fake

    async def _record(self, db, name: str) -> ContentModerationRecord:
        rows = await db.execute(
            select(ContentModerationRecord).where(ContentModerationRecord.message_id == self.messages[name]).order_by(ContentModerationRecord.id.desc())
        )
        return rows.scalars().first()

    async def _violations(self, db, name: str) -> list[UserViolation]:
        rows = await db.execute(select(UserViolation).where(UserViolation.target_type == "message", UserViolation.target_id == self.messages[name]))
        return list(rows.scalars().all())

    async def _recalled(self, db, name: str) -> bool:
        message = await db.get(Message, self.messages[name])
        await db.refresh(message)
        return message.recalled_at is not None


# ---------------------------------------------------------------- 级联规则


class CascadeTests(JudgeTestCase):
    async def test_block_confirmed_by_the_judge_is_punished_as_before(self) -> None:
        fake = await self._run("insult", BLOCK, 0.95)
        async with self.sessions() as db:
            record = await self._record(db, "insult")
            self.assertEqual((record.decision, record.judge_outcome, record.judge_p, record.judge_ms), ("block", "confirmed", 0.95, 4800))
            self.assertNotEqual(record.action_taken, ModerationAction.NONE)
            self.assertTrue(await self._recalled(db, "insult"))
            (violation,) = await self._violations(db, "insult")
            self.assertEqual((violation.category, violation.level, violation.moderation_record_id), ("harassment", 2, record.id))
            self.assertEqual(await self._machine_cases(db), [])
        self.assertEqual(fake.calls[0]["priority"], moderation_judge.PRIORITY_BLOCK)

    async def test_block_without_enough_confidence_is_not_punished_and_goes_to_the_jury_first(self) -> None:
        # 名额已经用完也照收：它们原本是要直接处罚的
        with patch.object(machine_service, "DAILY_MIN", 0), patch.object(machine_service, "DAILY_PER_JUROR", 0):
            await self._run("insult", BLOCK, 0.6)
            await self._run("insult2", UNCERTAIN, 0.8)
        async with self.sessions() as db:
            record = await self._record(db, "insult")
            self.assertEqual((record.decision, record.judge_outcome, record.judge_p, record.action_taken), ("block", "downgraded", 0.6, ModerationAction.NONE))
            self.assertFalse(await self._recalled(db, "insult"))
            self.assertEqual(await self._violations(db, "insult"), [])
            (case,) = await self._machine_cases(db)
            self.assertEqual((case.message_id, case.machine_decision, case.judge_p, case.queue), (self.messages["insult"], "review", 0.6, "jury"))
            self.assertLess(case.priority, 0)
            # 拿不准的那条被每日名额挡住了
            self.assertEqual((await self._record(db, "insult2")).judge_outcome, "flagged")
        # 名额正常时，降级的排在其余机器案件前面（哪怕那条的概率更高），全都排在真实举报后面
        await self._run("insult3", UNCERTAIN, 0.89)
        async with self.sessions() as db:
            downgraded, flagged = await self._machine_cases(db)
            self.assertEqual(flagged.message_id, self.messages["insult3"])
            self.assertGreater(downgraded.priority, flagged.priority)
            self.assertLess(downgraded.priority, 0)
            self.assertGreater(flagged.priority, machine_service.MACHINE_PRIORITY)

    async def test_uncertain_text_cleared_by_the_judge_is_simply_allowed(self) -> None:
        # 配置成「拿不准先撤回」也不撤：二审认为没问题
        with patch.object(settings, "content_moderation_text_review_recall", True):
            fake = await self._run("insult", UNCERTAIN, 0.05)
        async with self.sessions() as db:
            record = await self._record(db, "insult")
            self.assertEqual((record.decision, record.judge_outcome, record.action_taken), ("review", "cleared", ModerationAction.NONE))
            self.assertFalse(await self._recalled(db, "insult"))
            self.assertEqual(await self._machine_cases(db), [])
        self.assertEqual(fake.calls[0]["priority"], moderation_judge.PRIORITY_FLAGGED)

    async def test_uncertain_text_flagged_by_the_judge_becomes_a_case_ordered_by_probability(self) -> None:
        await self._run("insult", UNCERTAIN, 0.3)
        await self._run("insult2", UNCERTAIN, 0.7)
        async with self.sessions() as db:
            low, high = await self._machine_cases(db)
            self.assertEqual(((low.judge_p, high.judge_p)), (0.3, 0.7))
            self.assertGreater(high.priority, low.priority)
            self.assertEqual((await self._record(db, "insult")).judge_outcome, "flagged")
            self.assertFalse(await self._recalled(db, "insult"))

    async def test_moderation_errors_are_judged_too(self) -> None:
        # 一审出错原来一律不进委员队列；二审认为值得看就进，认为没问题就放行
        await self._run("insult", ERROR, 0.4)
        await self._run("insult2", ERROR, 0.02)
        async with self.sessions() as db:
            (case,) = await self._machine_cases(db)
            self.assertEqual(case.message_id, self.messages["insult"])
            self.assertEqual((await self._record(db, "insult2")).judge_outcome, "cleared")

    async def test_allowed_messages_and_images_never_reach_the_judge(self) -> None:
        fake = await self._run("greeting", ALLOW, 0.99)
        self.assertEqual(fake.calls, [])
        async with self.sessions() as db:
            pictured = Message(conversation_id=self.small_id, sender_id=self.ids["offender"], content="看图", metadata_json=json.dumps({"images": ["/uploads/messages/a.png"]}))
            db.add(pictured)
            await db.commit()

        async def image_block(*_args, **_kwargs):
            return {"decision": "block", "severity": 2, "categories": ["sexual"]}

        # 图片没被放行时图片说了算，文字也不送二审
        with self._judge(0.01) as fake, patch.object(moderation_service, "AsyncSessionLocal", self.sessions), patch.object(
            moderation_service, "_call_text_moderation", lambda *_a, **_k: asyncio.sleep(0, result=dict(BLOCK))
        ), patch.object(moderation_service, "_call_image_moderation", image_block):
            await moderation_service._moderate_message_task(pictured.id, ModerationSource.MESSAGE_AUTO, None)
        self.assertEqual(fake.calls, [])
        async with self.sessions() as db:
            self.assertIsNotNone((await db.get(Message, pictured.id)).recalled_at)

    async def test_switch_off_keeps_the_old_behaviour(self) -> None:
        async with self.sessions() as db:
            await self._switch(db, machine_judge=False)
        fake = await self._run("insult", BLOCK, 0.01)
        self.assertEqual(fake.calls, [])
        async with self.sessions() as db:
            record = await self._record(db, "insult")
            self.assertIsNone(record.judge_outcome)
            self.assertTrue(await self._recalled(db, "insult"))
            self.assertEqual(len(await self._violations(db, "insult")), 1)


# ---------------------------------------------------------------- 二审不可用


class FallbackTests(JudgeTestCase):
    async def test_unavailable_judge_falls_back_to_the_first_stage(self) -> None:
        await self._run("insult", BLOCK, None)
        await self._run("insult2", UNCERTAIN, None)
        await self._run("insult3", ERROR, None)
        async with self.sessions() as db:
            block = await self._record(db, "insult")
            self.assertEqual((block.judge_outcome, block.judge_p), ("unavailable", None))
            self.assertTrue(await self._recalled(db, "insult"))
            self.assertEqual(len(await self._violations(db, "insult")), 1)
            # 拿不准的照旧进委员队列；出错的照旧不进
            self.assertEqual([case.message_id for case in await self._machine_cases(db)], [self.messages["insult2"]])

    async def test_a_crash_inside_the_judge_step_keeps_the_first_stage_verdict(self) -> None:
        async def broken(*_args, **_kwargs):
            raise RuntimeError("context query exploded")

        with self._judge(0.01), patch.object(moderation_judge, "message_context", broken):
            await self._moderate(self.messages["insult"], BLOCK)
        async with self.sessions() as db:
            self.assertEqual((await self._record(db, "insult")).judge_outcome, "unavailable")
            self.assertTrue(await self._recalled(db, "insult"))

    async def test_records_left_pending_by_a_restart_are_finished_with_the_first_stage_verdict(self) -> None:
        old = datetime.now(UTC) - timedelta(minutes=10)
        async with self.sessions() as db:
            stale = ContentModerationRecord(
                source=ModerationSource.MESSAGE_AUTO, message_id=self.messages["insult"], conversation_id=self.small_id, user_id=self.ids["offender"],
                content_type="text", content_text="你们都是废物", decision="block", severity=2, categories='["harassment"]',
                raw_response=json.dumps(BLOCK), judge_outcome="pending", created_at=old,
            )
            # 刚开始等的那条不能动：别的进程可能正在等它的二审
            fresh = ContentModerationRecord(
                source=ModerationSource.MESSAGE_AUTO, message_id=self.messages["insult2"], conversation_id=self.small_id, user_id=self.ids["offender"],
                content_type="text", content_text="再说一遍，废物", decision="block", severity=2, categories='["harassment"]',
                raw_response=json.dumps(BLOCK), judge_outcome="pending",
            )
            db.add_all([stale, fresh])
            await db.commit()
        with patch.object(moderation_service, "AsyncSessionLocal", self.sessions):
            self.assertEqual(await moderation_service.recover_stale_judgements(), 1)
            # 认领过的不会被第二次处理
            self.assertEqual(await moderation_service.recover_stale_judgements(), 0)
        async with self.sessions() as db:
            self.assertEqual((await db.get(ContentModerationRecord, stale.id)).judge_outcome, "unavailable")
            self.assertEqual((await db.get(ContentModerationRecord, fresh.id)).judge_outcome, "pending")
            self.assertTrue(await self._recalled(db, "insult"))
            self.assertEqual(len(await self._violations(db, "insult")), 1)
            self.assertFalse(await self._recalled(db, "insult2"))


class QueueTests(JudgeTestCase):
    """真正的队列和 worker，只把发请求的那一步换掉。"""

    @contextmanager
    def _service(self, post=None, **overrides):
        values = {"content_moderation_enabled": True, "content_moderation_url": "http://moderation.test", "moderation_judge_enabled": True, **overrides}
        with patch.multiple(settings, **values):
            if post is None:
                yield
            else:
                with patch.object(moderation_judge, "_post_judge", post):
                    yield

    async def test_returns_the_probability(self) -> None:
        seen = []

        def post(payload, timeout):
            seen.append((payload, timeout))
            return moderation_judge.JudgeVerdict(p=0.42, latency_ms=5000, model="m")

        with self._service(post):
            verdict = await moderation_judge.judge("你真菜", ["成员A：打一把"], priority=moderation_judge.PRIORITY_FLAGGED)
        self.assertEqual(verdict.p, 0.42)
        self.assertEqual(seen[0][0], {"text": "你真菜", "context": ["成员A：打一把"], "after": [], "mode": "report"})
        self.assertLessEqual(seen[0][1], settings.moderation_judge_timeout_seconds)

    async def test_inactive_errors_and_timeouts_return_none(self) -> None:
        calls = []

        def boom(payload, timeout):
            calls.append(payload)
            raise RuntimeError("judge HTTP 503")

        with self._service(boom, moderation_judge_enabled=False):
            self.assertIsNone(await moderation_judge.judge("x", priority=0))
        self.assertEqual(calls, [])
        with self._service(boom):
            self.assertIsNone(await moderation_judge.judge("x", priority=0))
        self.assertEqual(len(calls), 1)

        def slow(payload, timeout):
            time.sleep(3)
            return moderation_judge.JudgeVerdict(p=0.99, latency_ms=3000, model="m")

        with self._service(slow, moderation_judge_timeout_seconds=2):
            started = time.monotonic()
            self.assertIsNone(await moderation_judge.judge("x", priority=0))
            self.assertLess(time.monotonic() - started, 2.8)

    async def test_post_judge_parses_and_validates(self) -> None:
        class Response:
            def __init__(self, body):
                self.body = body

            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

            def read(self):
                return json.dumps(self.body).encode("utf-8")

        requests = []

        def opener(body):
            def fake(request, timeout):
                requests.append((request, timeout))
                return Response(body)

            return fake

        with self._service(content_moderation_token="token-x"), patch.object(moderation_judge, "urlopen", opener({"p_violation": 0.93, "latency_ms": 5120, "model": "m"})):
            verdict = moderation_judge._post_judge({"text": "x", "context": [], "after": []}, 30)
        self.assertEqual(verdict, moderation_judge.JudgeVerdict(p=0.93, latency_ms=5120, model="m"))
        request, timeout = requests[0]
        self.assertEqual((request.full_url, request.get_header("X-moderation-token"), timeout), ("http://moderation.test/judge/text", "token-x", 30))
        for body in ({"p_violation": 1.5}, {"p_violation": True}, {"detail": "x"}):
            with self._service(), patch.object(moderation_judge, "urlopen", opener(body)), self.assertRaises(RuntimeError):
                moderation_judge._post_judge({"text": "x"}, 30)

    async def test_unreachable_service_is_skipped_for_a_while(self) -> None:
        with self._service(), patch.object(moderation_judge, "urlopen", side_effect=URLError(ConnectionRefusedError())) as opener:
            self.assertIsNone(await moderation_judge.judge("x", priority=0))
            self.assertIsNone(await moderation_judge.judge("y", priority=0))
        self.assertEqual(opener.call_count, 1)

    async def test_one_at_a_time_most_urgent_first_and_a_full_queue_falls_back(self) -> None:
        order: list[str] = []
        active = []
        release = asyncio.Event()
        loop = asyncio.get_running_loop()

        def post(payload, timeout):
            active.append(1)
            self.assertEqual(len(active), 1)
            order.append(payload["text"])
            if payload["text"] == "first":
                # 第一个请求卡住 worker，让后面的在队列里排好
                asyncio.run_coroutine_threadsafe(release.wait(), loop).result(timeout=5)
            active.pop()
            return moderation_judge.JudgeVerdict(p=0.5, latency_ms=1, model="m")

        with self._service(post, moderation_judge_queue_size=2):
            first = asyncio.create_task(moderation_judge.judge("first", priority=moderation_judge.PRIORITY_REPORT))
            while not order:
                await asyncio.sleep(0.01)
            report = asyncio.create_task(moderation_judge.judge("report", priority=moderation_judge.PRIORITY_REPORT))
            block = asyncio.create_task(moderation_judge.judge("block", priority=moderation_judge.PRIORITY_BLOCK))
            await asyncio.sleep(0.05)
            # 队列已满（两个在排队）：直接退回一审
            self.assertIsNone(await moderation_judge.judge("overflow", priority=moderation_judge.PRIORITY_BLOCK))
            release.set()
            results = await asyncio.gather(first, report, block)
        self.assertTrue(all(result is not None for result in results))
        self.assertEqual(order, ["first", "block", "report"])


# ---------------------------------------------------------------- 举报


class ReportJudgeTests(JudgeTestCase):
    async def _triage(self, db, reporter: str, name: str, result: dict, *results: float | None) -> tuple[ReportCase, FakeJudge]:
        report = await self._report(db, reporter, name)

        async def verdict(*_args, **_kwargs):
            return dict(result)

        with self._judge(*results) as fake, patch.object(settings, "content_moderation_enabled", True), patch.object(
            moderation_service, "_call_text_moderation", verdict
        ), patch.object(case_service, "AsyncSessionLocal", self.sessions), patch.object(moderation_service, "AsyncSessionLocal", self.sessions):
            await case_service.triage_case(report.case_id)
        case = await db.get(ReportCase, report.case_id)
        await db.refresh(case)
        return case, fake

    async def test_judge_probability_orders_reports_but_never_dismisses_them(self) -> None:
        async with self.sessions() as db:
            likely, fake = await self._triage(db, "alice", "insult", ALLOW, 0.9)
            unlikely, _ = await self._triage(db, "alice", "insult2", ALLOW, 0.02)
            self.assertEqual((likely.judge_p, unlikely.judge_p), (0.9, 0.02))
            self.assertEqual((likely.status, unlikely.status), ("open", "open"))
            self.assertEqual((likely.queue, unlikely.queue), ("jury", "jury"))
            self.assertGreater(likely.priority, unlikely.priority)
            self.assertGreater(unlikely.priority, 0)
            # 举报排在实时审核后面；带下文
            self.assertEqual(fake.calls[0]["priority"], moderation_judge.PRIORITY_REPORT)
            self.assertEqual(fake.calls[0]["context"], ["成员A：大家好"])
            self.assertEqual(fake.calls[0]["after"], ["成员B：别这样说话", "被举报人：再说一遍，废物", "被举报人：加我领福利"])
            self.assertEqual((await self._record(db, "insult")).judge_outcome, "scored")

    async def test_block_on_a_report_needs_the_judge_to_close_the_case(self) -> None:
        async with self.sessions() as db:
            doubtful, _ = await self._triage(db, "alice", "insult", BLOCK, 0.5)
            self.assertEqual((doubtful.status, doubtful.machine_decision, doubtful.judge_p), ("open", "review", 0.5))
            self.assertFalse(await self._recalled(db, "insult"))
            self.assertEqual(await self._violations(db, "insult"), [])
            sure, _ = await self._triage(db, "alice", "insult2", BLOCK, 0.97)
            self.assertEqual((sure.status, sure.verdict_source), ("resolved", "auto"))
            self.assertTrue(await self._recalled(db, "insult2"))

    async def test_reuses_the_live_judgement(self) -> None:
        await self._run("insult", UNCERTAIN, 0.66)
        async with self.sessions() as db:
            # 机器案件已经带着概率；另一条消息的举报复用不到别人的
            (machine,) = await self._machine_cases(db)
            self.assertEqual(machine.judge_p, 0.66)
        await self._run("insult2", UNCERTAIN, 0.07)
        async with self.sessions() as db:
            case, fake = await self._triage(db, "alice", "insult2", UNCERTAIN, 0.99)
            self.assertEqual((case.judge_p, fake.calls), (0.07, []))

    async def test_a_report_joining_a_queued_case_gets_judged(self) -> None:
        async with self.sessions() as db:
            case_id = await self._jury_case(db, "insult")
            before = (await db.get(ReportCase, case_id)).priority
            report = await self._report(db, "bob", "insult")
            self.assertEqual(report.case_id, case_id)
            self.assertEqual(getattr(report, "_judge_case_id", None), case_id)
        with self._judge(0.95) as fake, patch.object(case_service, "AsyncSessionLocal", self.sessions):
            await case_service.judge_case(case_id)
            # 已经有概率了：不再问
            await case_service.judge_case(case_id)
        self.assertEqual(len(fake.calls), 1)
        async with self.sessions() as db:
            case = await db.get(ReportCase, case_id)
            self.assertEqual((case.judge_p, case.status), (0.95, "open"))
            self.assertGreater(case.priority, before)


# ---------------------------------------------------------------- 上下文


class ContextTests(JudgeTestCase):
    async def test_context_is_labelled_without_names_or_ids(self) -> None:
        async with self.sessions() as db:
            extra = [
                Message(conversation_id=self.small_id, sender_id=self.ids["bob"], type=MessageType.IMAGE, content="/uploads/messages/x.png"),
                Message(conversation_id=self.small_id, sender_id=self.ids["alice"], content="已经撤回的话", recalled_at=datetime.now(UTC)),
                Message(conversation_id=self.small_id, sender_id=self.ids["admin"], type=MessageType.SYSTEM, content="alice 加入了群聊"),
                Message(conversation_id=self.small_id, sender_id=self.ids["alice"], content="@offender 你说谁呢"),
                Message(conversation_id=self.small_id, sender_id=self.ids["offender"], content="说的就是你 @alice"),
            ]
            for message in extra:
                db.add(message)
                await db.flush()
            await db.commit()
            target = extra[-1].id
        with self._judge(0.5) as fake, patch.object(moderation_service, "AsyncSessionLocal", self.sessions), patch.object(
            moderation_service, "_call_text_moderation", lambda *_a, **_k: asyncio.sleep(0, result=dict(UNCERTAIN))
        ):
            await moderation_service._moderate_message_task(target, ModerationSource.MESSAGE_AUTO, None)
        (call,) = fake.calls
        # 前 5 条（撤回的、系统消息不算）：发言人按出现先后编成 成员A / B / C，待审消息的作者是「被举报人」
        self.assertEqual(call["context"], ["被举报人：再说一遍，废物", "被举报人：加我领福利", "被举报人：第三次骂人", "成员A：[图片]", "成员B：@某人 你说谁呢"])
        self.assertEqual(call["after"], [])
        self.assertEqual(call["text"], "说的就是你 @某人")
        payload = json.dumps(call, ensure_ascii=False)
        for name in ("alice", "bob", "offender", "admin"):
            self.assertNotIn(name, payload)

    async def test_direct_messages_are_judged_with_their_own_context_only(self) -> None:
        async with self.sessions() as db:
            direct = Conversation(type=ConversationType.DIRECT)
            db.add(direct)
            await db.flush()
            first = Message(conversation_id=direct.id, sender_id=self.ids["alice"], content="你个憨憨")
            db.add(first)
            await db.flush()
            second = Message(conversation_id=direct.id, sender_id=self.ids["offender"], content="你才是傻子哈哈")
            db.add(second)
            await db.commit()
        with self._judge(0.95) as fake, patch.object(moderation_service, "AsyncSessionLocal", self.sessions), patch.object(
            moderation_service, "_call_text_moderation", lambda *_a, **_k: asyncio.sleep(0, result=dict(BLOCK))
        ):
            await moderation_service._moderate_message_task(second.id, ModerationSource.MESSAGE_AUTO, None)
        self.assertEqual(fake.calls[0]["context"], ["成员A：你个憨憨"])
        async with self.sessions() as db:
            # 私聊不进委员队列，二审确认了照旧处罚
            self.assertEqual(await self._machine_cases(db), [])
            self.assertIsNotNone((await db.get(Message, second.id)).recalled_at)
            count = (await db.execute(select(func.count(UserViolation.id)).where(UserViolation.target_id == second.id))).scalar_one()
            self.assertEqual(count, 1)
