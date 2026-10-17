"""风纪系统 v2 一期（docs/jury-v2.md 第 1 节）。"""

from __future__ import annotations

import asyncio
import json
import unittest
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Annotated
from unittest.mock import patch

import httpx
from fastapi import Depends, FastAPI, HTTPException
from sqlalchemy import event, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.models  # noqa: F401
from app.api.v1 import admin as admin_api
from app.api.v1 import jury as jury_api
from app.core.config import settings
from app.core.database import Base, get_db
from app.core.dependencies import get_current_user
from app.models.case import JuryMember, JuryOpinion, JuryPointLog, JuryVote, ReportCase
from app.models.conversation import Conversation, ConversationMember
from app.models.enums import ConversationType, MemberRole, PlatformRole, PostVisibility, ReportStatus, ReportTargetType, UserStatus
from app.models.message import Message
from app.models.penalty import UserViolation
from app.models.post import Post
from app.models.report import AdminAuditLog, UserReport
from app.models.user import User
from app.schemas.case import JuryCaseDetail, JuryCaseView, JuryHistoryItem, JuryMemberRead, JuryMemberReview, JuryStatus, JuryVoteResult
from app.schemas.jury import JuryConfig, JuryMyStats, JuryOpinion as JuryOpinionSchema, JuryOpinionPage, JuryOverview, JuryVoteItem, PublicCase, PublicCaseDetail
from app.schemas.report import ReportCreate
from app.services import cases as case_service
from app.services import jury as jury_service
from app.services import jury_bank as bank_service
from app.services import jury_opinions as opinion_service
from app.services import jury_overview as overview_service
from app.services import jury_points as points_service
from app.services import jury_settings
from app.services import messages as message_service
from app.services import moderation as moderation_service
from app.services import penalties as penalty_service
from app.services import reports as report_service
from app.websocket.manager import manager

JURORS = ("juror1", "juror2", "juror3", "juror4", "juror5", "juror6", "insider", "speaker", "lurker")
# 委员侧接口里的 case_id 是评审编号（jury_votes.id），不是案件编号。夹具里先占掉前面一段评审编号，
# 这样两种编号在测试里不会碰巧相等：把案件编号误传给委员侧接口一定是 404，而不是恰好撞上谁的评审编号。
REVIEW_ID_OFFSET = 5000


class JuryV2TestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        # IsolatedAsyncioTestCase 默认开 asyncio 调试模式，每次调度都要抓一遍调用栈，慢一个数量级。
        asyncio.get_running_loop().set_debug(False)
        jury_settings.reset_cache()
        overview_service.reset_overview_cache()
        points_service.reset_leaderboard_cache()
        case_service._last_queue_event_at = None
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with self.sessions() as db:
            users = [User(username=name, hashed_password="x") for name in ("alice", "bob", "offender", "admin", *JURORS)]
            users[3].platform_role = PlatformRole.ADMIN
            db.add_all(users)
            await db.flush()
            self.ids = {user.username: user.id for user in users}
            # 小群（要审批）/ 公开大厅（任何人可进）/ 同样大小但要审批的社团
            small = Conversation(type=ConversationType.GROUP, title="创作群")
            plaza = Conversation(type=ConversationType.GROUP, title="大厅", join_mode="open")
            club = Conversation(type=ConversationType.GROUP, title="社团")
            db.add_all([small, plaza, club])
            await db.flush()
            self.small_id, self.plaza_id, self.club_id = small.id, plaza.id, club.id
            for name in ("alice", "bob", "offender", "insider"):
                db.add(ConversationMember(conversation_id=small.id, user_id=self.ids[name], role=MemberRole.MEMBER))
            for conversation in (plaza, club):
                for name in ("alice", "bob", "offender", "speaker", "lurker"):
                    db.add(ConversationMember(conversation_id=conversation.id, user_id=self.ids[name], role=MemberRole.MEMBER))
            messages = {
                "greeting": Message(conversation_id=small.id, sender_id=self.ids["bob"], content="大家好"),
                "insult": Message(conversation_id=small.id, sender_id=self.ids["offender"], content="你们都是废物"),
                "reply": Message(conversation_id=small.id, sender_id=self.ids["alice"], content="别这样说话"),
                "insult2": Message(conversation_id=small.id, sender_id=self.ids["offender"], content="再说一遍，废物"),
                "spam": Message(conversation_id=small.id, sender_id=self.ids["offender"], content="加我领福利"),
                "insult3": Message(conversation_id=small.id, sender_id=self.ids["offender"], content="第三次骂人"),
                "plaza_hello": Message(conversation_id=plaza.id, sender_id=self.ids["speaker"], content="有人吗"),
                "plaza_insult": Message(conversation_id=plaza.id, sender_id=self.ids["offender"], content="大厅里的辱骂"),
                "club_insult": Message(conversation_id=club.id, sender_id=self.ids["offender"], content="社团里的辱骂"),
            }
            for message in messages.values():
                db.add(message)
                await db.flush()
            self.messages = {name: message.id for name, message in messages.items()}
            post = Post(
                author_id=self.ids["offender"],
                content="低价卖号，加微信",
                visibility=PostVisibility.PUBLIC,
                image_urls_json=json.dumps(["/uploads/posts/a.png", "/uploads/posts/b.png"]),
                moderation_status="approved",
            )
            db.add(post)
            for name in JURORS:
                db.add(JuryMember(user_id=self.ids[name], status="active", term_ends_at=datetime.now(UTC) + timedelta(days=30)))
            if db.get_bind().dialect.name == "postgresql":
                # PG 严格执行外键，也不会因为插了显式 id 就推进自增序列：直接把序列拨到偏移量上，效果和下面的占位记录一样
                await db.execute(text("SELECT setval(pg_get_serial_sequence('jury_votes', 'id'), :n)"), {"n": REVIEW_ID_OFFSET})
            else:
                # 占位用的分配记录：不属于任何委员，指向一个不存在的案件
                db.add(JuryVote(id=REVIEW_ID_OFFSET, case_id=0, juror_id=self.ids["admin"], vote="recuse", recused_at=datetime.now(UTC) - timedelta(days=365)))
            await db.commit()
            self.post_id = post.id

    async def asyncTearDown(self) -> None:
        jury_settings.reset_cache()
        overview_service.reset_overview_cache()
        await self.engine.dispose()

    # ------------------------------------------------------------ 工具

    async def _user(self, db, name: str) -> User:
        return await db.get(User, self.ids[name])

    async def _report(self, db, reporter: str, message: str, reason: str = "辱骂骚扰") -> UserReport:
        # 接口里举报人已经由鉴权依赖加载进会话、并且一直被请求引用着；生成快照时会用到。
        current_user = await self._user(db, reporter)
        return await report_service.create_report(
            db, current_user.id, ReportCreate(target_type=ReportTargetType.MESSAGE, target_id=self.messages[message], reason=reason)
        )

    async def _queue(self, db, case_id: int) -> ReportCase:
        """模拟机器预审放行后进入委员队列。"""
        case = await db.get(ReportCase, case_id)
        case_service._enter_jury_queue(case, datetime.now(UTC))
        await db.commit()
        return case

    async def _jury_case(self, db, message: str, reporter: str = "alice", reason: str = "辱骂骚扰") -> int:
        report = await self._report(db, reporter, message, reason)
        await self._queue(db, report.case_id)
        return report.case_id

    async def _post_case(self, db, reporter: str = "alice") -> int:
        report = await report_service.create_report(
            db, self.ids[reporter], ReportCreate(target_type=ReportTargetType.POST, target_id=self.post_id, reason="垃圾广告")
        )
        await self._queue(db, report.case_id)
        return report.case_id

    def _user_id(self, who) -> int:
        return self.ids[who] if isinstance(who, str) else getattr(who, "id", who)

    async def _rid(self, db, who, case_id: int) -> int:
        """这个人在这个案件上的评审编号（who 可以是用户名、用户对象或用户 id）。委员侧接口只认它。"""
        row = await db.execute(select(JuryVote.id).where(JuryVote.case_id == case_id, JuryVote.juror_id == self._user_id(who)))
        return row.scalar_one()

    async def _real(self, db, review_id: int) -> int:
        """评审编号背后的真实案件编号。只有测试需要反查。"""
        return (await db.execute(select(JuryVote.case_id).where(JuryVote.id == review_id))).scalar_one()

    async def _vote(self, db, juror: str, vote: str, note: str | None = None, case_id: int | None = None) -> dict:
        user = await self._user(db, juror)
        view = await jury_service.next_case(db, user)
        self.assertIsNotNone(view, f"{juror} got no case")
        self.assertGreater(view["case_id"], REVIEW_ID_OFFSET)
        if case_id is not None:
            self.assertEqual(await self._real(db, view["case_id"]), case_id)
        return await jury_service.cast_vote(db, user, view["case_id"], vote, note)

    async def _settle(self, db, case_id: int, vote: str = "l2", jurors: tuple[str, ...] = ("juror1", "juror2", "juror3")) -> ReportCase:
        for juror in jurors:
            result = await self._vote(db, juror, vote, case_id=case_id)
        self.assertTrue(result["settled"])
        case = await db.get(ReportCase, case_id)
        await db.refresh(case)
        return case

    async def _age(self, db, case_id: int, hours: float) -> None:
        """把定案时间往前挪，模拟过了公示延迟 / 内容保留期。"""
        case = await db.get(ReportCase, case_id)
        case.closed_at = datetime.now(UTC) - timedelta(hours=hours)
        await db.commit()

    @contextmanager
    def _events(self):
        events: list[tuple[str, set[int], dict]] = []

        async def to_users(user_ids, event_type, data):
            events.append((event_type, set(user_ids), data))

        async def to_user(user_id, event_type, data):
            events.append((event_type, {user_id}, data))

        with patch.object(manager, "send_to_users", to_users), patch.object(manager, "send_to_user", to_user):
            yield events

    @contextmanager
    def _queries(self):
        """数一段代码发了多少条 SQL。"""
        statements: list[str] = []

        def record(_connection, _cursor, statement, *_rest) -> None:
            statements.append(statement)

        event.listen(self.engine.sync_engine, "before_cursor_execute", record)
        try:
            yield statements
        finally:
            event.remove(self.engine.sync_engine, "before_cursor_execute", record)

    @staticmethod
    def _per_user(events, name: str, **match) -> dict[int, dict]:
        """某一种事件按接收者整理：user_id -> data。发给委员的事件是逐个发的，每个人的 case_id 是自己的评审编号。"""
        found: dict[int, dict] = {}
        for event, users, data in events:
            if event == name and all(data.get(key) == value for key, value in match.items()):
                for user_id in users:
                    found[user_id] = data
        return found

    async def _code(self, awaitable) -> tuple[int, str | None]:
        with self.assertRaises(HTTPException) as caught:
            await awaitable
        detail = caught.exception.detail
        return caught.exception.status_code, detail.get("code") if isinstance(detail, dict) else None


class AssignmentTests(JuryV2TestCase):
    async def test_large_open_group_only_recuses_speakers_in_the_snapshot(self) -> None:
        async with self.sessions() as db:
            plaza_case = await db.get(ReportCase, await self._jury_case(db, "plaza_insult"))
            club_case = await db.get(ReportCase, await self._jury_case(db, "club_insult"))
            small_case = await db.get(ReportCase, await self._jury_case(db, "insult"))
            speaker, lurker, insider = self.ids["speaker"], self.ids["lurker"], self.ids["insider"]
            with patch.object(jury_service, "LARGE_GROUP_MEMBERS", 5):
                # 大厅是 open 且够大：群成员可以审，但在快照里说过话的不行
                self.assertFalse(await jury_service._has_conflict(db, lurker, plaza_case))
                self.assertTrue(await jury_service._has_conflict(db, speaker, plaza_case))
                # 同样大小但要审批的群、以及小群，保持「成员一律回避」
                self.assertTrue(await jury_service._has_conflict(db, lurker, club_case))
                self.assertTrue(await jury_service._has_conflict(db, insider, small_case))
                # 当事人和举报人在大厅里也始终回避
                self.assertTrue(await jury_service._has_conflict(db, self.ids["offender"], plaza_case))
                self.assertTrue(await jury_service._has_conflict(db, self.ids["alice"], plaza_case))
                # 分案走的是同一套判断：lurker 拿到的是大厅的案件；speaker 在大厅和社团都要回避，只能拿到小群的
                self.assertEqual(await self._real(db, (await jury_service.next_case(db, await self._user(db, "lurker")))["case_id"]), plaza_case.id)
                self.assertEqual(await self._real(db, (await jury_service.next_case(db, await self._user(db, "speaker")))["case_id"]), small_case.id)
            # 没到阈值的公开群不放宽
            self.assertTrue(await jury_service._has_conflict(db, lurker, plaza_case))
            # 委员能看到快照里的全部上下文，每条带相对位置
            view = await jury_service.next_case(db, await self._user(db, "juror1"))
            self.assertEqual(await self._real(db, view["case_id"]), plaza_case.id)
            self.assertEqual([(item["offset"], item["is_target"]) for item in view["messages"]], [(-1, False), (0, True)])

    async def test_context_is_the_whole_snapshot_with_offsets(self) -> None:
        async with self.sessions() as db:
            case_id = await self._jury_case(db, "insult")
            view = await jury_service.next_case(db, await self._user(db, "juror1"))
            self.assertEqual(await self._real(db, view["case_id"]), case_id)
            # 目标后面有 4 条：旧实现只给后 3 条
            self.assertEqual([item["offset"] for item in view["messages"]], [-1, 0, 1, 2, 3, 4])
            self.assertEqual(view["messages"][1]["content"], "你们都是废物")
            self.assertNotIn("sender_id", str(view))
            self.assertEqual(view["expires_at"] - view["assigned_at"], timedelta(hours=jury_service.ASSIGNMENT_TTL_HOURS))
            JuryCaseView(**view)

    async def test_assignment_keeps_scanning_past_cases_that_need_recusal(self) -> None:
        async with self.sessions() as db:
            now = datetime.now(UTC)
            # 排在前面的 35 个案件都出自 insider 所在的小群
            for index in range(35):
                db.add(
                    ReportCase(
                        target_key=f"message:{9000 + index}",
                        target_type="message",
                        target_id=9000 + index,
                        conversation_id=self.small_id,
                        reported_user_id=self.ids["offender"],
                        jury_eligible=True,
                        status="open",
                        queue="jury",
                        queued_at=now,
                        priority=5.0,
                    )
                )
            reviewable = ReportCase(
                target_key="post:9999", target_type="post", target_id=9999, reported_user_id=self.ids["offender"],
                jury_eligible=True, status="open", queue="jury", queued_at=now, priority=1.0,
            )
            db.add(reviewable)
            await db.commit()
            insider = await self._user(db, "insider")
            status = await jury_service.my_status(db, insider)
            self.assertEqual((status["tier"], status["available_cases"]), ("trainee", 1))
            self.assertEqual(await self._real(db, (await jury_service.next_case(db, insider))["case_id"]), reviewable.id)
            # 已经分到手还没投的仍然算「能评审的」
            self.assertEqual((await jury_service.my_status(db, insider))["available_cases"], 1)
            # 没有回避关系的委员能数到全部，角标上限 50
            juror = await self._user(db, "juror1")
            self.assertEqual(await jury_service.available_cases(db, juror), 36)
            with patch.object(jury_service, "AVAILABLE_CASES_CAP", 10):
                self.assertEqual(await jury_service.available_cases(db, juror), 10)
            # 不是在任委员：两个字段都是 null
            outsider = await jury_service.my_status(db, await self._user(db, "alice"))
            self.assertEqual((outsider["status"], outsider["tier"], outsider["available_cases"]), ("none", None, None))


class QueueTests(JuryV2TestCase):
    async def test_sensitive_cases_never_enter_the_jury_queue(self) -> None:
        async def flagged(*_args, **_kwargs):
            return {"decision": "review", "severity": 1, "categories": ["sexual"], "confidence": 0.6, "reason": "疑似低俗", "model": "Gandi-AIHub"}

        async with self.sessions() as db:
            by_reason = await self._report(db, "alice", "insult", "色情低俗")
            by_machine = await self._report(db, "alice", "insult2")
            normal = await self._report(db, "alice", "insult3")
        with patch.object(case_service, "AsyncSessionLocal", self.sessions):
            await case_service.triage_case(by_reason.case_id)
            await case_service.triage_case(normal.case_id)
            with patch.object(settings, "content_moderation_enabled", True), patch.object(moderation_service, "_call_text_moderation", flagged):
                await case_service.triage_case(by_machine.case_id)
        async with self.sessions() as db:
            for report in (by_reason, by_machine):
                case = await db.get(ReportCase, report.case_id)
                self.assertEqual((case.queue, case.jury_block_reason, case.queued_at), ("admin", "sensitive", None))
                self.assertEqual(await self._code(case_service.move_case(db, case.id, "jury", actor_id=self.ids["admin"])), (400, "case_sensitive"))
            queued = await db.get(ReportCase, normal.case_id)
            self.assertEqual((queued.queue, queued.jury_block_reason), ("jury", None))
            self.assertIsNotNone(queued.queued_at)

            # 已经在委员队列里的案件，后来的举报带「色情低俗」时撤出来
            case_service._last_queue_event_at = None
            with self._events() as events:
                await self._report(db, "bob", "insult3", "色情低俗")
            await db.refresh(queued)
            self.assertEqual((queued.queue, queued.jury_block_reason, queued.jury_outcome), ("admin", "sensitive", None))
            self.assertIsNotNone(queued.escalated_at)
            self.assertIn("jury.queue.changed", [event for event, _users, _data in events])
            self.assertIsNone(await jury_service.next_case(db, await self._user(db, "juror1")))

            # 兜底：仍然留在委员队列里的敏感案件由定时任务转走
            queued.queue, queued.jury_block_reason = "jury", None
            await db.commit()
            stats = await case_service.sweep_jury_queue(db)
            await db.refresh(queued)
            self.assertEqual((stats["sensitive"], queued.queue, queued.jury_block_reason), (1, "admin", "sensitive"))

    async def test_timeout_counts_from_queue_entry_and_is_not_reset_by_new_reports(self) -> None:
        async with self.sessions() as db:
            now = datetime.now(UTC)
            stale = await db.get(ReportCase, await self._jury_case(db, "insult"))
            fresh = await db.get(ReportCase, await self._jury_case(db, "insult2"))
            legacy = await db.get(ReportCase, await self._jury_case(db, "insult3"))
            unstamped = await db.get(ReportCase, await self._jury_case(db, "spam", reason="垃圾广告"))
            stale.queued_at = now - timedelta(hours=73)
            # 进队列才 10 小时，只是很久没人动过
            fresh.queued_at, fresh.updated_at = now - timedelta(hours=10), now - timedelta(hours=100)
            # 上线前就在队列里的案件没有 queued_at：退回按 updated_at 算
            legacy.queued_at, legacy.updated_at = None, now - timedelta(hours=80)
            unstamped.queued_at, unstamped.updated_at = None, now - timedelta(hours=5)
            await db.commit()
            # 新举报会刷新 updated_at，但不重置超时
            await self._report(db, "bob", "insult")
            await db.refresh(stale)
            self.assertGreater(penalty_service.aware(stale.updated_at), now - timedelta(minutes=1))

            stats = await case_service.sweep_jury_queue(db)
            self.assertEqual(stats, {"backfilled": 2, "sensitive": 0, "timeout": 2})
            for case in (stale, fresh, legacy, unstamped):
                await db.refresh(case)
            self.assertEqual((stale.queue, stale.jury_outcome), ("admin", "timeout"))
            self.assertEqual((legacy.queue, legacy.jury_outcome), ("admin", "timeout"))
            self.assertIsNotNone(stale.escalated_at)
            self.assertEqual((fresh.queue, fresh.jury_outcome, fresh.escalated_at), ("jury", None, None))
            # 没有 queued_at 的补成 updated_at
            self.assertEqual((unstamped.queue, unstamped.queued_at), ("jury", unstamped.updated_at))

    async def test_leaving_the_jury_queue_records_why(self) -> None:
        async with self.sessions() as db:
            consensus = await self._settle(db, await self._jury_case(db, "insult"))
            self.assertEqual((consensus.jury_outcome, consensus.escalated_at, consensus.publishable), ("consensus", None, True))

            split_id = await self._jury_case(db, "insult2")
            for juror, vote in (("juror1", "l2"), ("juror2", "none"), ("juror3", "l1"), ("juror4", "none"), ("juror5", "l2")):
                self.assertFalse((await self._vote(db, juror, vote, case_id=split_id))["settled"])
            split = await db.get(ReportCase, split_id)
            await db.refresh(split)
            self.assertEqual((split.status, split.queue, split.jury_outcome), ("open", "admin", "no_consensus"))
            self.assertIsNotNone(split.escalated_at)

            manual = await case_service.move_case(db, await self._jury_case(db, "insult3"), "admin", actor_id=self.ids["admin"])
            self.assertEqual((manual.queue, manual.jury_outcome), ("admin", "manual"))
            # 再派回去：重新计时，之前的转出记录清掉
            requeued = await case_service.move_case(db, manual.id, "jury", actor_id=self.ids["admin"])
            self.assertEqual((requeued.queue, requeued.jury_outcome, requeued.escalated_at), ("jury", None, None))
            # 管理员不改派、直接把还在委员队列里的案件结了，也算他接手
            direct = await case_service.resolve_case(db, manual.id, source="admin", actor_id=self.ids["admin"], upheld=False)
            self.assertEqual((direct.jury_outcome, direct.publishable), ("manual", False))

    async def test_queue_changed_event_is_throttled_per_process(self) -> None:
        async with self.sessions() as db:
            with self._events() as events:
                await self._jury_case(db, "insult")
                await case_service.notify_jury_queue_changed(db)
                await case_service.notify_jury_queue_changed(db)
                queue_events = [(users, data) for event, users, data in events if event == "jury.queue.changed"]
                self.assertEqual(queue_events, [({self.ids[name] for name in JURORS}, {})])
                # 过了 30 秒才会再发
                case_service._last_queue_event_at -= case_service.QUEUE_EVENT_INTERVAL_SECONDS + 1
                await case_service.notify_jury_queue_changed(db)
                self.assertEqual(len([event for event, _users, _data in events if event == "jury.queue.changed"]), 2)

            async def broken(*_args, **_kwargs):
                raise RuntimeError("redis down")

            # 事件发不出去不能影响主流程
            case_service._last_queue_event_at = None
            with patch.object(manager, "send_to_users", broken):
                await case_service.notify_jury_queue_changed(db)
                self.assertFalse((await self._vote(db, "juror1", "l2", "有理有据"))["settled"])


class VotingTests(JuryV2TestCase):
    async def test_vote_assigns_alias_and_reveals_tally_only_after_settlement(self) -> None:
        async with self.sessions() as db:
            case_id = await self._jury_case(db, "insult")
            with self._events() as events:
                first = await self._vote(db, "juror1", "l2", "连续骂人，不是玩笑", case_id=case_id)
                second = await self._vote(db, "juror2", "abstain", case_id=case_id)
                third = await self._vote(db, "juror3", "l2", "同意", case_id=case_id)
            self.assertEqual([item["case"]["my_vote"]["alias"] for item in (first, second, third)], ["委员 A", "委员 B", "委员 C"])
            JuryVoteResult(**third)
            detail = third["case"]
            # 定案前：只有有效票数（不含弃权），没有票数分布和结论
            self.assertEqual((detail["stage"], detail["votes_cast"], detail["tally"], detail["verdict"]), ("jury", 2, None, None))
            self.assertEqual((detail["my_vote"]["vote"], detail["my_vote"]["agreed"]), ("l2", None))
            self.assertEqual((detail["is_quality_check"], detail["opinions_enabled"], detail["opinion_count"], detail["content_available"]), (False, True, 2, True))
            self.assertEqual(len(detail["messages"]), 6)
            # 备注成了观点；定案前不透露谁投了什么
            juror2 = await self._user(db, "juror2")
            page = await opinion_service.list_opinions(db, juror2, await self._rid(db, juror2, case_id))
            self.assertEqual([(item["alias"], item["content"], item["is_note"], item["vote"]) for item in page["items"]], [
                ("委员 A", "连续骂人，不是玩笑", True, None),
                ("委员 C", "同意", True, None),
            ])
            # 第一条备注时还没有别人投过票，不用通知；第二条通知到之前投过票的两个人（含弃权的）
            # 每个人收到的 case_id 是他自己的评审编号
            created = self._per_user(events, "jury.opinion.created")
            self.assertEqual(
                {user_id: (data["parent_id"], data["case_id"]) for user_id, data in created.items()},
                {self.ids[name]: (None, await self._rid(db, name, case_id)) for name in ("juror1", "juror2")},
            )

            # 没分到这个案件的人：拿案件的真实编号、拿别人的评审编号来读都是 404，和编号不存在没有区别
            self.assertEqual(await self._code(jury_service.case_detail(db, self.ids["juror4"], case_id)), (404, "case_not_found"))
            self.assertEqual(await self._code(jury_service.case_detail(db, self.ids["juror4"], await self._rid(db, "juror1", case_id))), (404, "case_not_found"))
            self.assertEqual(await self._code(jury_service.case_detail(db, self.ids["juror1"], 99999)), (404, "case_not_found"))
            # 投过票的人自己拿真实编号来也不行
            self.assertEqual(await self._code(jury_service.case_detail(db, self.ids["juror1"], case_id)), (404, "case_not_found"))
            # 分到手还没投：有评审编号了，但读不了详情
            assigned = await jury_service.next_case(db, await self._user(db, "juror4"))
            self.assertEqual(await self._real(db, assigned["case_id"]), case_id)
            self.assertEqual(await self._code(jury_service.case_detail(db, self.ids["juror4"], assigned["case_id"])), (403, "not_voted"))

            with self._events() as events:
                final = await self._vote(db, "juror4", "l1", case_id=case_id)
            self.assertTrue(final["settled"])
            self.assertEqual(
                self._per_user(events, "jury.case.settled"),
                {self.ids[name]: {"outcome": "violation", "case_id": await self._rid(db, name, case_id)} for name in ("juror1", "juror2", "juror3", "juror4")},
            )
            detail = JuryCaseDetail(**await jury_service.case_detail(db, self.ids["juror2"], await self._rid(db, self.ids["juror2"], case_id)))
            self.assertEqual((detail.stage, detail.votes_cast), ("closed", 3))
            self.assertEqual(detail.tally, {"none": 0, "l1": 1, "l2": 2, "l3": 0, "abstain": 1, "need_context": 0})
            self.assertEqual((detail.verdict.outcome, detail.verdict.source, detail.verdict.category, detail.verdict.category_label, detail.verdict.level), ("violation", "jury", "harassment", "辱骂骚扰", 2))
            self.assertFalse(detail.verdict.overturned)
            self.assertIsNotNone(detail.verdict.hours)
            # 题库讲解二期才有：字段在，一期恒为 null
            self.assertIn("explanation", (await jury_service.case_detail(db, self.ids["juror2"], await self._rid(db, self.ids["juror2"], case_id)))["verdict"])
            self.assertIsNone(detail.verdict.explanation)
            # 弃权的人没有「是否一致」；投了违规的与结论一致
            self.assertIsNone(detail.my_vote.agreed)
            self.assertTrue((await jury_service.case_detail(db, self.ids["juror1"], await self._rid(db, self.ids["juror1"], case_id)))["my_vote"]["agreed"])
            page = await opinion_service.list_opinions(db, juror2, await self._rid(db, juror2, case_id))
            self.assertEqual([item["vote"] for item in page["items"]], ["l2", "l2"])

            # 过了内容保留期：结论还在，内容不给了
            await self._age(db, case_id, 24 * jury_service.CONTENT_RETENTION_DAYS + 1)
            votes = (await db.execute(select(JuryVote).where(JuryVote.case_id == case_id))).scalars().all()
            for vote in votes:
                vote.voted_at = datetime.now(UTC) - timedelta(days=jury_service.CONTENT_RETENTION_DAYS + 2)
            await db.commit()
            expired = await jury_service.case_detail(db, self.ids["juror1"], await self._rid(db, self.ids["juror1"], case_id))
            self.assertEqual((expired["content_available"], expired["messages"], expired["post"]), (False, [], None))
            self.assertEqual(expired["verdict"]["outcome"], "violation")

    async def test_quality_check_is_revealed_on_the_spot_and_has_no_opinions(self) -> None:
        async with self.sessions() as db:
            report = await self._report(db, "alice", "insult")
            golden = await case_service.resolve_case(db, report.case_id, source="admin", actor_id=self.ids["admin"], upheld=True, category="harassment")
            # 二期：评审中的质控题只出已核对的；位置随机，隔了太多张没出才强制出
            golden.is_golden, golden.golden_verified = True, True
            member = await jury_service.get_member(db, self.ids["juror1"])
            member.votes_since_quality = jury_service.QUALITY_MAX_GAP + 1
            await db.commit()
            juror = await self._user(db, "juror1")
            view = await jury_service.next_case(db, juror)
            self.assertEqual(await self._real(db, view["case_id"]), golden.id)
            result = await jury_service.cast_vote(db, juror, await self._rid(db, juror, golden.id), "none", "看着像玩笑")
            detail = JuryCaseDetail(**result["case"])
            self.assertFalse(result["settled"])
            self.assertEqual((detail.is_quality_check, detail.stage, detail.tally, detail.votes_cast, detail.opinions_enabled, detail.opinion_count), (True, "closed", None, 0, False, 0))
            self.assertEqual((detail.verdict.outcome, detail.verdict.level, detail.my_vote.agreed, detail.my_vote.alias), ("violation", 2, False, None))
            self.assertTrue(detail.content_available)
            # 备注只留给管理员，不进观点区
            vote = (await db.execute(select(JuryVote).where(JuryVote.case_id == golden.id))).scalar_one()
            self.assertEqual((vote.is_quality_check, vote.note, vote.alias), (True, "看着像玩笑", None))
            self.assertEqual((await db.execute(select(JuryOpinion))).scalars().all(), [])
            self.assertEqual(await self._code(opinion_service.list_opinions(db, juror, await self._rid(db, juror, golden.id))), (403, "opinions_closed"))
            self.assertEqual(await self._code(opinion_service.create_opinion(db, juror, await self._rid(db, juror, golden.id), "讨论一下", None)), (403, "opinions_closed"))
            await db.refresh(member)
            self.assertEqual((member.golden_total, member.golden_correct, member.votes_settled), (1, 0, 0))
            # 质控题不让案件进公示，也不出现在我的评审的「未定案」里
            await db.refresh(golden)
            self.assertFalse(golden.publishable)
            items, _total = await jury_service.list_votes(db, juror.id)
            self.assertEqual((items[0]["is_quality_check"], items[0]["stage"], items[0]["agreed"]), (True, "closed", False))

    async def test_real_case_closed_by_admin_is_not_mistaken_for_a_quality_check(self) -> None:
        async with self.sessions() as db:
            case_id = await self._jury_case(db, "insult")
            juror = await self._user(db, "juror1")
            self.assertEqual(await self._real(db, (await jury_service.next_case(db, juror))["case_id"]), case_id)
            # 委员还没投，管理员先结了案；后台会把它放进质控题库
            case = await case_service.resolve_case(db, case_id, source="admin", actor_id=self.ids["admin"], upheld=True, category="harassment")
            case.is_golden = True
            await db.commit()
            result = await jury_service.cast_vote(db, juror, await self._rid(db, juror, case_id), "l2", "确实在骂人")
            self.assertFalse(result["case"]["is_quality_check"])
            self.assertEqual((result["case"]["my_vote"]["alias"], result["case"]["my_vote"]["agreed"], result["case"]["opinion_count"]), ("委员 A", True, 1))
            member = await jury_service.get_member(db, juror.id)
            self.assertEqual((member.golden_total, member.votes_settled, member.votes_agreed), (0, 1, 1))


class ReviewIdTests(JuryV2TestCase):
    """委员侧的 case_id 是评审编号（docs/jury-v2.md 1.0）。"""

    async def test_quality_checks_cannot_be_told_apart_by_their_number(self) -> None:
        async with self.sessions() as db:
            # 质控题是早就结案的旧案件：真实编号比现在队列里的小，而且在公示栏里查得到
            report = await self._report(db, "alice", "insult")
            golden = await case_service.resolve_case(db, report.case_id, source="admin", actor_id=self.ids["admin"], upheld=True, category="harassment")
            golden.is_golden, golden.golden_verified, golden.publishable = True, True, True
            await db.commit()
            await self._age(db, golden.id, 500)
            self.assertEqual(bank_service.question_pool(golden), "scored")
            self.assertEqual((await overview_service.public_case(db, golden.id))["outcome"], "violation")
            current = await self._jury_case(db, "plaza_insult")
            self.assertLess(golden.id, current)

            juror, other = await self._user(db, "juror1"), await self._user(db, "juror2")
            member = await jury_service.get_member(db, juror.id)
            with patch.object(jury_service, "_quality_roll", return_value=0.99):
                normal_view = await jury_service.next_case(db, juror)
            self.assertEqual(await self._real(db, normal_view["case_id"]), current)
            await jury_service.cast_vote(db, juror, normal_view["case_id"], "l2", None)
            member.votes_since_quality = jury_service.QUALITY_MAX_GAP + 1
            await db.commit()
            quality_view = await jury_service.next_case(db, juror)
            self.assertEqual(await self._real(db, quality_view["case_id"]), golden.id)

            # 编号按分到手的先后往上长，和案件新旧无关；两种案件的视图长得一样
            self.assertEqual(quality_view["case_id"], normal_view["case_id"] + 1)
            self.assertEqual(set(quality_view), set(normal_view))
            self.assertNotIn(golden.id, (quality_view["case_id"], normal_view["case_id"]))
            JuryCaseView(**quality_view)
            # 拿案件的真实编号（比如从公示栏抄来的）在委员侧接口里什么也查不到
            for real_id in (golden.id, current):
                self.assertEqual(await self._code(jury_service.case_detail(db, juror.id, real_id)), (404, "case_not_found"))
                self.assertEqual(await self._code(opinion_service.list_opinions(db, juror, real_id)), (404, "case_not_found"))
                self.assertEqual(await self._code(opinion_service.create_opinion(db, juror, real_id, "试试", None)), (404, "case_not_found"))
                self.assertEqual((await self._code(jury_service.cast_vote(db, juror, real_id, "l2", None)))[0], 404)
            vote = await db.get(JuryVote, quality_view["case_id"])
            self.assertEqual((vote.voted_at, vote.is_quality_check), (None, True))

            result = await jury_service.cast_vote(db, juror, quality_view["case_id"], "l2", None)
            detail = JuryCaseDetail(**result["case"])
            self.assertEqual((result["case_id"], detail.case_id, detail.is_quality_check), (quality_view["case_id"], quality_view["case_id"], True))
            # 质控题即使案件本身在公示栏里也不给公开编号，否则等于把真实编号还了回去
            self.assertIsNone(detail.public_case_id)
            items, _total = await jury_service.list_votes(db, juror.id)
            self.assertEqual([(item["case_id"], item["is_quality_check"], item["public_case_id"]) for item in items], [
                (quality_view["case_id"], True, None), (normal_view["case_id"], False, None),
            ])
            self.assertEqual([item["case_id"] for item in await jury_service.history(db, juror.id)], [quality_view["case_id"], normal_view["case_id"]])
            # 风纪值明细里的也是评审编号；库里存的仍是真实编号
            points = await points_service.list_points(db, juror.id)
            self.assertEqual([(item["kind"], item["case_id"]) for item in points["items"]], [("quality_correct", quality_view["case_id"]), ("daily_first", None)])
            log = (await db.execute(select(JuryPointLog).where(JuryPointLog.kind == "quality_correct"))).scalar_one()
            self.assertEqual(log.case_id, golden.id)

            # 评审编号只对本人有效：同一个案件在别人那里是另一个编号，互相拿来用都是 404
            with patch.object(jury_service, "_quality_roll", return_value=0.99):
                other_view = await jury_service.next_case(db, other)
            self.assertEqual(await self._real(db, other_view["case_id"]), current)
            self.assertNotEqual(other_view["case_id"], normal_view["case_id"])
            self.assertEqual(await self._code(jury_service.case_detail(db, other.id, normal_view["case_id"])), (404, "case_not_found"))
            self.assertEqual(await self._code(opinion_service.list_opinions(db, other, normal_view["case_id"])), (404, "case_not_found"))
            self.assertEqual((await self._code(jury_service.cast_vote(db, juror, other_view["case_id"], "l2", None)))[0], 404)
            self.assertEqual((await self._code(jury_service.cast_vote(db, other, quality_view["case_id"], "l2", None)))[0], 404)
            # 那几次乱试没有改动任何东西
            self.assertIsNone((await db.get(JuryVote, other_view["case_id"])).voted_at)

    async def test_snapshot_times_carry_a_timezone(self) -> None:
        self.assertEqual(jury_service.snapshot_time("2026-10-12T03:04:05.678901"), "2026-10-12T03:04:05.678901Z")
        self.assertEqual(jury_service.snapshot_time("2026-10-12T03:04:05"), "2026-10-12T03:04:05Z")
        # 已经带时区的不重复处理，只是统一成 UTC
        self.assertEqual(jury_service.snapshot_time("2026-10-12T03:04:05Z"), "2026-10-12T03:04:05Z")
        self.assertEqual(jury_service.snapshot_time("2026-10-12T03:04:05+00:00"), "2026-10-12T03:04:05Z")
        self.assertEqual(jury_service.snapshot_time("2026-10-12T11:04:05+08:00"), "2026-10-12T03:04:05Z")
        self.assertEqual((jury_service.snapshot_time(None), jury_service.snapshot_time(""), jury_service.snapshot_time("昨天")), (None, None, "昨天"))
        async with self.sessions() as db:
            message_case = await self._jury_case(db, "insult")
            view = await jury_service.next_case(db, await self._user(db, "juror1"))
            self.assertEqual(await self._real(db, view["case_id"]), message_case)
            times = [item["created_at"] for item in view["messages"]]
            self.assertEqual(len(times), 6)
            for text in times:
                self.assertTrue(text.endswith("Z"), text)
                self.assertLess(abs((datetime.fromisoformat(text.replace("Z", "+00:00")) - datetime.now(UTC)).total_seconds()), 3600)
            await self._settle(db, message_case)
            detail = await jury_service.case_detail(db, self.ids["juror1"], view["case_id"])
            self.assertTrue(all(item["created_at"].endswith("Z") for item in detail["messages"]))

            post_case = await self._post_case(db)
            post_view = await jury_service.next_case(db, await self._user(db, "juror1"))
            self.assertTrue(post_view["post"]["created_at"].endswith("Z"))
            await self._settle(db, post_case)
            await self._age(db, post_case, case_service.PUBLISH_DELAY_HOURS + 1)
            public = PublicCaseDetail(**await overview_service.public_case(db, post_case))
            self.assertEqual(public.content.created_at, post_view["post"]["created_at"])


class RemovalBroadcastTests(JuryV2TestCase):
    async def test_recall_broadcast_does_not_grow_with_group_size(self) -> None:
        """认定违规后给群里推「消息被撤回」：一次序列化、只发给在线的成员，查询条数和群人数无关。"""

        async def crowd(db, title: str, size: int) -> tuple[int, int, list[int]]:
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

        async def resolve(db, group_id: int, message_id: int, online: set[int]) -> tuple[list, int, int]:
            # 和 _report 一样：举报人要先加载进会话并一直被引用着，生成快照时会用到
            reporter = await self._user(db, "alice")
            report = await report_service.create_report(
                db, reporter.id, ReportCreate(target_type=ReportTargetType.MESSAGE, target_id=message_id, reason="辱骂骚扰")
            )
            built = 0
            original = message_service.build_message_read

            async def counting(*args, **kwargs):
                nonlocal built
                built += 1
                return await original(*args, **kwargs)

            with self._events() as events, patch.object(manager, "online_user_ids", return_value=online), patch.object(
                message_service, "build_message_read", counting
            ):
                case = await db.get(ReportCase, report.case_id)
                info = {"kind": "message", "message_id": message_id, "conversation_id": group_id}
                await case_service.resolve_case(db, case.id, source="admin", actor_id=self.ids["admin"], upheld=True, category="harassment")
                # 再单独量一次广播这一步
                with self._queries() as statements:
                    await case_service._broadcast_removed(db, info)
            recalled = [(users, data) for event_type, users, data in events if event_type == "message.recalled"]
            return recalled, built, len(statements)

        async with self.sessions() as db:
            small_group, small_message, small_members = await crowd(db, "小群", 12)
            big_group, big_message, big_members = await crowd(db, "大群", 400)
            outsider = self.ids["juror6"]
            small_online = set(small_members[:5]) | {outsider}
            big_online = set(big_members[:30]) | {outsider}
            small_recalled, small_built, small_queries = await resolve(db, small_group, small_message, small_online)
            big_recalled, big_built, big_queries = await resolve(db, big_group, big_message, big_online)

            # 结案时一次、单独量的那一次又一次；每次都是一条群发，不是每个成员一条
            self.assertEqual((len(small_recalled), len(big_recalled)), (2, 2))
            users, data = big_recalled[0]
            # 只发给在线的群成员：不在线的 370 人不发，在线但不在群里的人也不发
            self.assertEqual(users, set(big_members[:30]))
            self.assertEqual(small_recalled[0][0], set(small_members[:5]))
            # 负载和用户自己撤回消息时一样，只有撤回信息，对所有人都相同
            self.assertEqual(set(data), {"id", "conversation_id", "message_id", "recalled_at", "recalled_by_id"})
            self.assertEqual((data["id"], data["conversation_id"], data["recalled_by_id"]), (big_message, big_group, self.ids["admin"]))
            self.assertIsNotNone(data["recalled_at"])
            # 不再给每个成员各组装一遍完整消息；查询条数不随群人数增加
            self.assertEqual((small_built, big_built), (0, 0))
            self.assertLessEqual(big_queries, 8)
            self.assertLessEqual(abs(big_queries - small_queries), 2)
            self.assertIsNotNone((await db.get(Message, big_message)).recalled_at)


class AdminMemberListTests(JuryV2TestCase):
    async def test_expired_members_are_listed_as_expired(self) -> None:
        async with self.sessions() as db:
            past = datetime.now(UTC) - timedelta(hours=2)
            for name in ("juror2", "juror3"):
                (await jury_service.get_member(db, self.ids[name])).term_ends_at = past
            # juror2 正在被封禁，届满时续不了任；juror3 没有违规记录，会自动续任
            banned = await self._user(db, "juror2")
            banned.status = UserStatus.DISABLED
            await db.commit()

            items, total = await jury_service.list_members(db, None, 50, 0)
            states = {item["user"].username: item["status"] for item in items}
            self.assertEqual((total, states["juror2"], states["juror3"], states["juror1"]), (len(JURORS), "expired", "active", "active"))
            # 和本人在 /jury/me 看到的一致
            self.assertEqual((await jury_service.my_status(db, banned))["status"], "expired")
            self.assertEqual((await jury_service.my_status(db, await self._user(db, "juror3")))["status"], "active")
            JuryMemberRead(**next(item for item in items if item["user"].username == "juror2"))

            expired, expired_total = await jury_service.list_members(db, "expired", 50, 0)
            self.assertEqual((expired_total, [item["user"].username for item in expired]), (1, ["juror2"]))
            active, active_total = await jury_service.list_members(db, "active", 50, 0)
            self.assertEqual(active_total, len(JURORS) - 1)
            self.assertNotIn("juror2", {item["user"].username for item in active})
            # 单人查询（审批接口的返回）同样的口径
            single, _total = await jury_service.list_members(db, None, 1, 0, user_id=banned.id)
            self.assertEqual(single[0]["status"], "expired")


class OpinionTests(JuryV2TestCase):
    async def _discussion(self, db) -> int:
        """juror1、juror2、juror3 各投一票（未定案），juror1 带备注。"""
        case_id = await self._jury_case(db, "insult")
        await self._vote(db, "juror1", "l2", "这是在骂人", case_id=case_id)
        await self._vote(db, "juror2", "none", case_id=case_id)
        await self._vote(db, "juror3", "abstain", case_id=case_id)
        return case_id

    async def test_only_voters_can_read_and_only_serving_jurors_can_write(self) -> None:
        async with self.sessions() as db:
            case_id = await self._discussion(db)
            juror4, juror3 = await self._user(db, "juror4"), await self._user(db, "juror3")
            # 没分到这个案件的人没有评审编号：拿真实编号、拿别人的评审编号都是 404
            for wrong in (case_id, await self._rid(db, juror3, case_id), 99999):
                self.assertEqual(await self._code(opinion_service.list_opinions(db, juror4, wrong)), (404, "case_not_found"))
                self.assertEqual(await self._code(opinion_service.create_opinion(db, juror4, wrong, "路过", None)), (404, "case_not_found"))
            # 分到手还没投：进不了观点区
            pending = (await jury_service.next_case(db, juror4))["case_id"]
            self.assertEqual(await self._real(db, pending), case_id)
            self.assertEqual(await self._code(opinion_service.list_opinions(db, juror4, pending)), (403, "not_voted"))
            self.assertEqual(await self._code(opinion_service.create_opinion(db, juror4, pending, "路过", None)), (403, "not_voted"))
            note_id = (await opinion_service.list_opinions(db, juror3, await self._rid(db, juror3, case_id)))["items"][0]["id"]
            # 看不到的案件下面的观点，对外就当不存在
            self.assertEqual((await self._code(opinion_service.set_like(db, juror4, note_id, True)))[0], 404)
            self.assertEqual((await self._code(opinion_service.report_opinion(db, juror4, note_id, "人身攻击", None)))[0], 404)
            self.assertEqual((await self._code(opinion_service.delete_opinion(db, juror4, note_id)))[0], 404)

            # 弃权的人也投过票，可以参与讨论
            created = await opinion_service.create_opinion(db, juror3, await self._rid(db, juror3, case_id), "  我没看懂这句话  ", None)
            JuryOpinionSchema(**created)
            self.assertEqual((created["content"], created["alias"], created["tier"], created["mine"], created["is_note"]), ("我没看懂这句话", "委员 C", "trainee", True, False))
            for content in ("", "   ", "字" * (opinion_service.MAX_LENGTH + 1)):
                self.assertEqual(await self._code(opinion_service.create_opinion(db, juror3, await self._rid(db, juror3, case_id), content, None)), (422, "invalid_opinion"))
            self.assertEqual((await self._code(opinion_service.create_opinion(db, juror3, await self._rid(db, juror3, case_id), "回复", 99999)))[0], 404)

            # 被停职的前委员：能回看、能删自己的，不能再发、赞、举报
            await jury_service.review_member(db, juror3.id, action="suspend", actor_id=self.ids["admin"], note="测试")
            self.assertEqual((await opinion_service.list_opinions(db, juror3, await self._rid(db, juror3, case_id)))["total"], 2)
            self.assertEqual((await jury_service.case_detail(db, juror3.id, await self._rid(db, juror3.id, case_id)))["my_vote"]["vote"], "abstain")
            self.assertEqual(await self._code(opinion_service.create_opinion(db, juror3, await self._rid(db, juror3, case_id), "再说一句", None)), (403, "not_juror"))
            self.assertEqual(await self._code(opinion_service.set_like(db, juror3, note_id, True)), (403, "not_juror"))
            self.assertEqual(await self._code(opinion_service.report_opinion(db, juror3, note_id, "人身攻击", None)), (403, "not_juror"))
            self.assertEqual(await opinion_service.delete_opinion(db, juror3, created["id"]), {"id": created["id"], "state": "deleted"})
            # 受处罚期间同样只读
            juror2 = await self._user(db, "juror2")
            juror2.muted_until = datetime.now(UTC) + timedelta(hours=1)
            await db.commit()
            self.assertEqual(await self._code(opinion_service.create_opinion(db, juror2, await self._rid(db, juror2, case_id), "我被禁言了", None)), (403, "juror_restricted"))
            self.assertEqual((await opinion_service.list_opinions(db, juror2, await self._rid(db, juror2, case_id)))["total"], 1)

            # /jury/me 直接告诉客户端受限状态，不用等接口报错
            status = await jury_service.my_status(db, juror2)
            self.assertEqual(
                status["restriction"], {"reason": "muted", "until": penalty_service.aware(juror2.muted_until), "message": "受处罚期间不能参与评审"}
            )
            self.assertEqual((status["status"], status["available_cases"]), ("active", 0))
            self.assertEqual(JuryStatus(**status).restriction.reason, "muted")
            juror2.status, juror2.banned_until = UserStatus.DISABLED, None
            await db.commit()
            self.assertEqual((await jury_service.my_status(db, juror2))["restriction"], {"reason": "banned", "until": None, "message": "受处罚期间不能参与评审"})
            # 没受处罚的在任委员是 null；已经不是在任委员的人即使被禁言也是 null
            self.assertIsNone((await jury_service.my_status(db, juror4))["restriction"])
            juror3.muted_until = datetime.now(UTC) + timedelta(hours=1)
            await db.commit()
            suspended = await jury_service.my_status(db, juror3)
            self.assertEqual((suspended["status"], suspended["restriction"], suspended["tier"]), ("suspended", None, None))
            alice = await self._user(db, "alice")
            alice.muted_until = datetime.now(UTC) + timedelta(hours=1)
            await db.commit()
            self.assertIsNone((await jury_service.my_status(db, alice))["restriction"])

    async def test_replies_are_flattened_to_one_level(self) -> None:
        async with self.sessions() as db:
            case_id = await self._discussion(db)
            juror1, juror2, juror3 = [await self._user(db, name) for name in ("juror1", "juror2", "juror3")]
            root_id = (await opinion_service.list_opinions(db, juror1, await self._rid(db, juror1, case_id)))["items"][0]["id"]
            with self._events() as events:
                reply = await opinion_service.create_opinion(db, juror2, await self._rid(db, juror2, case_id), "我觉得只是口头禅", root_id)
                nested = await opinion_service.create_opinion(db, juror3, await self._rid(db, juror3, case_id), "口头禅也不能对着人说", reply["id"])
                again = await opinion_service.create_opinion(db, juror1, await self._rid(db, juror1, case_id), "同意楼上", nested["id"])
            self.assertEqual((reply["parent_id"], reply["reply_to_alias"]), (root_id, None))
            # 回复一条回复：挂到同一条根观点下面，并记下回复的是谁
            self.assertEqual((nested["parent_id"], nested["reply_to_alias"]), (root_id, "委员 B"))
            self.assertEqual((again["parent_id"], again["reply_to_alias"]), (root_id, "委员 C"))
            self.assertEqual(
                self._per_user(events, "jury.opinion.created", opinion_id=reply["id"]),
                {juror.id: {"opinion_id": reply["id"], "parent_id": root_id, "case_id": await self._rid(db, juror, case_id)} for juror in (juror1, juror3)},
            )

            page = JuryOpinionPage(**await opinion_service.list_opinions(db, juror1, await self._rid(db, juror1, case_id)))
            self.assertEqual((page.total, len(page.items)), (4, 1))
            root = page.items[0]
            self.assertEqual([(item.id, item.alias, item.reply_to_alias, item.replies) for item in root.replies], [
                (reply["id"], "委员 B", None, []),
                (nested["id"], "委员 C", "委员 B", []),
                (again["id"], "委员 A", "委员 C", []),
            ])
            self.assertEqual((await jury_service.case_detail(db, juror1.id, await self._rid(db, juror1.id, case_id)))["opinion_count"], 4)

            # 本人删除：有回复的根观点留占位，没有回复的直接不出现
            with self._events() as events:
                self.assertEqual(await opinion_service.delete_opinion(db, juror1, root_id), {"id": root_id, "state": "deleted"})
                await opinion_service.delete_opinion(db, juror1, again["id"])
            self.assertEqual(
                self._per_user(events, "jury.opinion.changed", opinion_id=root_id),
                {juror.id: {"opinion_id": root_id, "state": "deleted", "case_id": await self._rid(db, juror, case_id)} for juror in (juror1, juror2, juror3)},
            )
            self.assertEqual(await self._code(opinion_service.delete_opinion(db, juror2, nested["id"])), (403, "not_opinion_author"))
            page = await opinion_service.list_opinions(db, juror2, await self._rid(db, juror2, case_id))
            self.assertEqual((page["total"], page["items"][0]["state"], page["items"][0]["content"]), (2, "deleted", None))
            self.assertEqual([item["id"] for item in page["items"][0]["replies"]], [reply["id"], nested["id"]])
            # 已删除的不能再回复；全删光之后占位也没了
            self.assertEqual((await self._code(opinion_service.create_opinion(db, juror2, await self._rid(db, juror2, case_id), "补一句", again["id"])))[0], 404)
            await opinion_service.delete_opinion(db, juror2, reply["id"])
            await opinion_service.delete_opinion(db, juror3, nested["id"])
            self.assertEqual(await opinion_service.list_opinions(db, juror2, await self._rid(db, juror2, case_id)), {"items": [], "total": 0})

    async def test_likes_are_idempotent_and_reports_fold_the_opinion(self) -> None:
        async with self.sessions() as db:
            case_id = await self._discussion(db)
            juror1, juror2, juror3 = [await self._user(db, name) for name in ("juror1", "juror2", "juror3")]
            note_id = (await opinion_service.list_opinions(db, juror1, await self._rid(db, juror1, case_id)))["items"][0]["id"]
            self.assertEqual(await self._code(opinion_service.set_like(db, juror1, note_id, True)), (400, "cannot_like_own"))
            self.assertEqual(await opinion_service.set_like(db, juror2, note_id, True), {"id": note_id, "like_count": 1, "liked": True})
            self.assertEqual(await opinion_service.set_like(db, juror2, note_id, True), {"id": note_id, "like_count": 1, "liked": True})
            self.assertEqual((await opinion_service.set_like(db, juror3, note_id, True))["like_count"], 2)
            view = (await opinion_service.list_opinions(db, juror2, await self._rid(db, juror2, case_id)))["items"][0]
            self.assertEqual((view["like_count"], view["liked"], view["mine"]), (2, True, False))
            self.assertEqual(await opinion_service.set_like(db, juror2, note_id, False), {"id": note_id, "like_count": 1, "liked": False})
            self.assertEqual(await opinion_service.set_like(db, juror2, note_id, False), {"id": note_id, "like_count": 1, "liked": False})
            self.assertEqual((await jury_service.my_stats(db, juror1.id))["likes_received"], 1)
            self.assertEqual((await self._code(opinion_service.set_like(db, juror2, 99999, True)))[0], 404)

            self.assertEqual(await self._code(opinion_service.report_opinion(db, juror1, note_id, "人身攻击", None)), (400, "cannot_report_own"))
            self.assertEqual(await self._code(opinion_service.report_opinion(db, juror2, note_id, "看他不爽", None)), (422, "invalid_reason"))
            self.assertEqual(await self._code(opinion_service.report_opinion(db, juror2, note_id, "其他", "不好")), (422, "invalid_reason"))
            with self._events() as events:
                self.assertEqual(await opinion_service.report_opinion(db, juror2, note_id, "其他", "照搬了被举报人的原话"), {"id": note_id, "state": "visible"})
                self.assertEqual(await self._code(opinion_service.report_opinion(db, juror2, note_id, "人身攻击", None)), (409, "already_reported"))
                # 第二名委员举报后自动折叠
                self.assertEqual(await opinion_service.report_opinion(db, juror3, note_id, "照抄违规原文", None), {"id": note_id, "state": "hidden"})
            hidden = self._per_user(events, "jury.opinion.changed")
            self.assertEqual({user_id: data["state"] for user_id, data in hidden.items()}, {juror.id: "hidden" for juror in (juror1, juror2, juror3)})
            view = (await opinion_service.list_opinions(db, juror2, await self._rid(db, juror2, case_id)))["items"][0]
            self.assertEqual((view["state"], view["content"], view["reported"]), ("hidden", None, True))
            self.assertFalse((await opinion_service.list_opinions(db, juror1, await self._rid(db, juror1, case_id)))["items"][0]["reported"])
            self.assertEqual((await self._code(opinion_service.set_like(db, juror2, note_id, True)))[0], 404)

            # 后台看得到真实作者、原文和举报记录
            items, total = await opinion_service.admin_list(db, reported=True)
            self.assertEqual((total, items[0]["author"].username, items[0]["content"], items[0]["pending_reports"]), (1, "juror1", "这是在骂人", 2))
            self.assertEqual([(report["reporter"].username, report["reason"]) for report in items[0]["reports"]], [("juror2", "其他"), ("juror3", "照抄违规原文")])
            self.assertEqual((await opinion_service.admin_list(db, state="visible"))[1], 0)
            # 驳回举报：重新显示；之后再有一个人举报不会立刻又折叠
            restored = await opinion_service.admin_moderate(db, note_id, action="dismiss_reports", actor_id=self.ids["admin"], note="正常讨论")
            self.assertEqual((restored["state"], restored["pending_reports"], restored["moderation_note"]), ("visible", 0, "正常讨论"))
            self.assertEqual((await opinion_service.list_opinions(db, juror2, await self._rid(db, juror2, case_id)))["items"][0]["content"], "这是在骂人")
            deleted = await opinion_service.admin_moderate(db, note_id, action="delete", actor_id=self.ids["admin"], note="照抄原文")
            self.assertEqual((deleted["state"], deleted["deleted_by_id"]), ("deleted", self.ids["admin"]))
            self.assertEqual((await opinion_service.list_opinions(db, juror2, await self._rid(db, juror2, case_id)))["total"], 0)
            self.assertEqual((await opinion_service.admin_moderate(db, note_id, action="restore", actor_id=self.ids["admin"], note=None))["state"], "visible")
            # 作者自己删掉的，管理员不能替他恢复
            await opinion_service.delete_opinion(db, juror1, note_id)
            self.assertEqual(await self._code(opinion_service.admin_moderate(db, note_id, action="restore", actor_id=self.ids["admin"], note=None)), (409, "deleted_by_author"))
            actions = (await db.execute(select(AdminAuditLog.action).where(AdminAuditLog.target_type == "jury_opinion").order_by(AdminAuditLog.id))).scalars().all()
            self.assertEqual(actions, ["jury.opinion.dismiss_reports", "jury.opinion.delete", "jury.opinion.restore"])

    async def test_posting_is_rate_limited(self) -> None:
        async with self.sessions() as db:
            case_id = await self._discussion(db)
            juror1, juror2 = await self._user(db, "juror1"), await self._user(db, "juror2")
            with patch.object(opinion_service, "PER_MINUTE", 2):
                await opinion_service.create_opinion(db, juror2, await self._rid(db, juror2, case_id), "第一条", None)
                await opinion_service.create_opinion(db, juror2, await self._rid(db, juror2, case_id), "第二条", None)
                self.assertEqual(await self._code(opinion_service.create_opinion(db, juror2, await self._rid(db, juror2, case_id), "第三条", None)), (429, "opinion_rate_limited"))
            with patch.object(opinion_service, "PER_DAY", 2):
                self.assertEqual(await self._code(opinion_service.create_opinion(db, juror2, await self._rid(db, juror2, case_id), "第三条", None)), (429, "opinion_rate_limited"))
            # 每案上限把投票时的备注也算进去，删掉的也算
            with patch.object(opinion_service, "PER_CASE", 2):
                reply = await opinion_service.create_opinion(db, juror1, await self._rid(db, juror1, case_id), "补充一点", None)
                await opinion_service.delete_opinion(db, juror1, reply["id"])
                self.assertEqual(await self._code(opinion_service.create_opinion(db, juror1, await self._rid(db, juror1, case_id), "再补充一点", None)), (429, "opinion_rate_limited"))

    async def test_switch_opens_published_discussions_read_only_by_public_id(self) -> None:
        async with self.sessions() as db:
            case_id = await self._jury_case(db, "insult")
            await self._vote(db, "juror1", "l2", "这是在骂人", case_id=case_id)
            await self._vote(db, "juror2", "l2", case_id=case_id)
            juror1, juror5, insider = [await self._user(db, name) for name in ("juror1", "juror5", "insider")]
            await jury_settings.update_switches(db, self.ids["admin"], {"opinions_open_to_jurors": True})
            # 委员侧的入口只认评审编号：没参与的人拿真实编号进不来，开关开着也一样
            self.assertEqual(await self._code(opinion_service.list_opinions(db, juror5, case_id)), (404, "case_not_found"))
            # 开关只管公示栏里的案件：没定案、定案没满公示期的，公开编号的入口都是 404
            self.assertEqual(await self._code(overview_service.published_case(db, case_id)), (404, "case_not_found"))
            await self._vote(db, "juror3", "l2", case_id=case_id)
            self.assertEqual(await self._code(overview_service.published_case(db, case_id)), (404, "case_not_found"))
            await self._age(db, case_id, case_service.PUBLISH_DELAY_HOURS + 1)
            case = await overview_service.published_case(db, case_id)

            page = await opinion_service.list_public_opinions(db, juror5, case)
            JuryOpinionPage(**page)
            self.assertEqual([(item["alias"], item["vote"], item["mine"]) for item in page["items"]], [("委员 A", "l2", False)])
            note_id = page["items"][0]["id"]
            # 只读：不能赞、不能举报；看得到所以删别人的是 403 而不是 404
            self.assertEqual((await self._code(opinion_service.set_like(db, juror5, note_id, True)))[0], 404)
            self.assertEqual((await self._code(opinion_service.report_opinion(db, juror5, note_id, "人身攻击", None)))[0], 404)
            self.assertEqual(await self._code(opinion_service.delete_opinion(db, juror5, note_id)), (403, "not_opinion_author"))
            # 案件详情仍然只给投过票的人
            self.assertEqual(await self._code(jury_service.case_detail(db, juror5.id, case_id)), (404, "case_not_found"))
            # 有回避关系的（案发群的成员）进不来，不是委员的也进不来
            self.assertEqual(await self._code(opinion_service.list_public_opinions(db, insider, case)), (403, "opinions_closed"))
            self.assertEqual(await self._code(opinion_service.list_public_opinions(db, await self._user(db, "offender"), case)), (403, "not_juror"))
            # 参与者自己也可以从公开编号进来，看到的是同一个观点区
            mine = await opinion_service.list_public_opinions(db, juror1, case)
            self.assertEqual([(item["id"], item["mine"]) for item in mine["items"]], [(note_id, True)])

            # 把这个案件当质控题答过的人：按自己的评审编号仍然没有观点区，但和没碰过它的人一样能从公开编号读 —— 不再倒挂
            case.is_golden, case.golden_verified = True, True
            member = await jury_service.get_member(db, self.ids["juror6"])
            member.votes_since_quality = jury_service.QUALITY_MAX_GAP + 1
            await db.commit()
            juror6 = await self._user(db, "juror6")
            with patch.object(bank_service, "PRACTICE_EVERY", 10**9):
                view = await jury_service.next_case(db, juror6)
            self.assertEqual(await self._real(db, view["case_id"]), case_id)
            result = await jury_service.cast_vote(db, juror6, view["case_id"], "l2", None)
            self.assertTrue(result["case"]["is_quality_check"])
            self.assertEqual(await self._code(opinion_service.list_opinions(db, juror6, view["case_id"])), (403, "opinions_closed"))
            self.assertEqual((await opinion_service.list_public_opinions(db, juror6, case))["total"], 1)
            # 质控题的记录永远不带公开编号；真正投过它的人在它进了公示栏之后才有
            self.assertIsNone(result["case"]["public_case_id"])
            self.assertIsNone((await jury_service.list_votes(db, juror6.id))[0][0]["public_case_id"])
            self.assertEqual((await jury_service.case_detail(db, juror1.id, await self._rid(db, juror1, case_id)))["public_case_id"], case_id)
            self.assertEqual((await jury_service.list_votes(db, juror1.id))[0][0]["public_case_id"], case_id)

            await jury_settings.update_switches(db, self.ids["admin"], {"opinions_open_to_jurors": False})
            for visitor in (juror5, juror6):
                self.assertEqual(await self._code(opinion_service.list_public_opinions(db, visitor, case)), (403, "opinions_closed"))
            self.assertEqual((await self._code(opinion_service.delete_opinion(db, juror5, note_id)))[0], 404)
            # 参与者的入口不受开关影响
            self.assertEqual((await opinion_service.list_opinions(db, juror1, await self._rid(db, juror1, case_id)))["total"], 1)


class MyVotesTests(JuryV2TestCase):
    async def test_vote_list_filters_and_personal_stats(self) -> None:
        async with self.sessions() as db:
            violation = await self._jury_case(db, "insult")
            await self._settle(db, violation)
            clean = await self._jury_case(db, "insult2")
            for juror, vote in (("juror1", "l1"), ("juror2", "none"), ("juror3", "none"), ("juror4", "none"), ("juror5", "none")):
                result = await self._vote(db, juror, vote, "备注" if juror == "juror1" else None, case_id=clean)
            self.assertTrue(result["settled"])
            pending = await self._jury_case(db, "insult3")
            await self._vote(db, "juror1", "abstain", case_id=pending)
            juror_id = self.ids["juror1"]

            items, total = await jury_service.list_votes(db, juror_id)
            # case_id 是评审编号；背后的案件按投票时间倒序
            self.assertEqual((total, [await self._real(db, item["case_id"]) for item in items]), (3, [pending, clean, violation]))
            self.assertEqual([item["case_id"] for item in items], [await self._rid(db, "juror1", case) for case in (pending, clean, violation)])
            for item in items:
                JuryVoteItem(**item)
            # 还没进公示栏：没有公开编号
            self.assertEqual({item["public_case_id"] for item in items}, {None})
            self.assertEqual(
                [(item["vote"], item["stage"], item["outcome"], item["agreed"], item["verdict_source"], item["verdict_level"]) for item in items],
                [("abstain", "jury", None, None, None, None), ("l1", "closed", "clean", False, "jury", None), ("l2", "closed", "violation", True, "jury", 2)],
            )
            self.assertEqual((items[1]["alias"], items[1]["primary_reason"], items[1]["kind"], items[1]["opinion_count"], items[1]["overturned"]), ("委员 A", "辱骂骚扰", "message", 1, False))
            self.assertIsNone(items[0]["closed_at"])

            async def ids(**filters) -> list[int]:
                return [await self._real(db, item["case_id"]) for item in (await jury_service.list_votes(db, juror_id, **filters))[0]]

            self.assertEqual(await ids(state="open"), [pending])
            self.assertEqual(await ids(state="closed"), [clean, violation])
            self.assertEqual(await ids(outcome="violation"), [violation])
            self.assertEqual(await ids(outcome="clean"), [clean])
            self.assertEqual(await ids(agreed=True), [violation])
            self.assertEqual(await ids(agreed=False), [clean])
            self.assertEqual(await ids(votes=["abstain"]), [pending])
            self.assertEqual(await ids(votes=["l1", "l2"]), [clean, violation])
            self.assertEqual(await ids(state="closed", votes=["l2"], agreed=True), [violation])
            page, total = await jury_service.list_votes(db, juror_id, limit=1, offset=1)
            self.assertEqual((total, [await self._real(db, item["case_id"]) for item in page]), (3, [clean]))
            # 过了公示延迟：定案的两件有公开编号，未定案的没有
            for closed in (clean, violation):
                await self._age(db, closed, case_service.PUBLISH_DELAY_HOURS + 1)
            items, _total = await jury_service.list_votes(db, juror_id)
            self.assertEqual([item["public_case_id"] for item in items], [None, clean, violation])

            stats = JuryMyStats(**await jury_service.my_stats(db, juror_id))
            self.assertEqual((stats.votes_total, stats.votes_settled, stats.votes_agreed, stats.agreement_rate), (3, 2, 1, 0.5))
            self.assertEqual((stats.abstain_total, stats.abstain_rate, stats.golden_total, stats.golden_rate, stats.opinions_total), (1, 0.333, 0, 0, 1))
            # 近 30 天每天一项，日期按 UTC+8，今天在最后
            today = datetime.now(UTC).astimezone(jury_service.LOCAL_TZ).date()
            self.assertEqual(len(stats.trend), 30)
            self.assertEqual([day.date for day in stats.trend], [(today - timedelta(days=29 - index)).isoformat() for index in range(30)])
            self.assertEqual((stats.trend[-1].votes, stats.trend[-1].settled, stats.trend[-1].agreed), (3, 2, 1))
            self.assertEqual(sum(day.votes for day in stats.trend[:-1]), 0)

            # 从没当过委员的人：空列表、全 0
            self.assertEqual(await jury_service.list_votes(db, self.ids["alice"]), ([], 0))
            empty = await jury_service.my_stats(db, self.ids["alice"])
            self.assertEqual(len(empty.pop("trend")), 30)
            self.assertEqual(set(empty.values()), {0})

    async def test_existing_endpoints_keep_their_fields(self) -> None:
        async with self.sessions() as db:
            case_id = await self._jury_case(db, "insult")
            juror = await self._user(db, "juror1")
            status = await jury_service.my_status(db, juror)
            self.assertLessEqual({"status", "eligible", "requirements", "term_ends_at", "review_note", "stats", "daily_limit", "voted_today"}, set(status))
            self.assertEqual((status["status"], status["daily_limit"], status["voted_today"]), ("active", jury_service.DAILY_VOTE_LIMIT, 0))
            self.assertEqual(set(status["stats"]), {"votes_total", "votes_settled", "votes_agreed", "agreement_rate", "golden_total", "golden_correct"})
            JuryStatus(**status)
            view = await jury_service.next_case(db, juror)
            self.assertLessEqual({"case_id", "kind", "reasons", "report_count", "messages", "post"}, set(view))
            self.assertLessEqual({"speaker", "is_target", "type", "content", "images", "created_at"}, set(view["messages"][0]))
            self.assertEqual((view["kind"], view["reasons"], view["report_count"], view["post"]), ("message", [{"reason": "辱骂骚扰", "count": 1}], 1, None))
            # 委员看到的编号是评审编号，不是案件编号；投票响应、历史里的是同一个
            self.assertNotEqual(view["case_id"], case_id)
            self.assertEqual(await self._code(jury_service.cast_vote(db, juror, case_id, "l2", None)), (404, None))
            result = await jury_service.cast_vote(db, juror, view["case_id"], "l2", None)
            self.assertEqual((result["case_id"], result["settled"]), (view["case_id"], False))
            history = await jury_service.history(db, juror.id)
            self.assertEqual(set(history[0]), {"case_id", "kind", "vote", "voted_at", "agreed", "outcome"})
            self.assertEqual((history[0]["case_id"], history[0]["vote"], history[0]["agreed"], history[0]["outcome"]), (view["case_id"], "l2", None, None))
            JuryHistoryItem(**history[0])
            # 路由顺序：固定路径必须排在带参数的路径前面
            paths = [route.path for route in jury_api.router.routes]
            self.assertLess(paths.index("/cases/next"), paths.index("/cases/{case_id}"))
            self.assertLess(paths.index("/me/stats"), paths.index("/cases/{case_id}"))
            for path in ("/me", "/apply", "/cases/next", "/cases/{case_id}/vote", "/history", "/config", "/overview", "/votes", "/public/cases", "/public/cases/{case_id}", "/public/cases/{case_id}/opinions"):
                self.assertIn(path, paths)


class PublicBoardTests(JuryV2TestCase):
    async def _public_ids(self, db, **filters) -> list[int]:
        items, total = await overview_service.public_cases(db, **filters)
        self.assertEqual(total, len(items))
        return [item["case_id"] for item in items]

    async def test_cases_enter_the_board_after_the_delay_and_without_pending_appeals(self) -> None:
        async with self.sessions() as db:
            post_case = await self._post_case(db)
            case = await self._settle(db, post_case)
            self.assertEqual((case.publishable, case.content_publishable, case.verdict_category), (True, True, "advertising"))
            # 定案不满 72 小时：不公示，详情 404
            self.assertEqual(await self._public_ids(db), [])
            self.assertEqual(await self._code(overview_service.public_case(db, post_case)), (404, "case_not_found"))
            await self._age(db, post_case, case_service.PUBLISH_DELAY_HOURS + 1)
            self.assertEqual(await self._public_ids(db), [post_case])
            item = PublicCase(**(await overview_service.public_cases(db))[0][0])
            self.assertEqual((item.kind, item.outcome, item.category, item.category_label, item.level, item.source), ("post", "violation", "advertising", "垃圾广告", 2, "jury"))
            self.assertEqual((item.tally, item.votes, item.report_count, item.overturned, item.has_content), ({"none": 0, "l1": 0, "l2": 3, "l3": 0, "abstain": 0, "need_context": 0}, 3, 1, False, True))
            self.assertEqual([(reason.reason, reason.count) for reason in item.reasons], [("垃圾广告", 1)])
            detail = PublicCaseDetail(**await overview_service.public_case(db, post_case))
            # 只给文字，图片只给数量
            self.assertEqual((detail.content.text, detail.content.image_count, detail.content_hidden_reason), ("低价卖号，加微信", 2, None))
            self.assertNotIn("/uploads/", detail.model_dump_json())
            self.assertNotIn("offender", detail.model_dump_json())
            read = (await case_service.build_case_reads(db, [case]))[0]
            self.assertEqual(read["published_at"], penalty_service.aware(case.closed_at) + timedelta(hours=case_service.PUBLISH_DELAY_HOURS))

            # 相关处罚有待处理的申诉：先撤下，申诉驳回后再回来
            violation = (await db.execute(select(UserViolation).where(UserViolation.case_id == post_case))).scalar_one()
            await penalty_service.submit_appeal(db, self.ids["offender"], violation.id, "我没有卖号，是朋友借的")
            self.assertEqual(await self._public_ids(db), [])
            self.assertEqual((await self._code(overview_service.public_case(db, post_case)))[0], 404)
            self.assertIsNone((await case_service.build_case_reads(db, [case]))[0]["published_at"])
            await penalty_service.review_appeal(db, violation.id, reviewer_id=self.ids["admin"], accept=False, note="证据充分")
            self.assertEqual(await self._public_ids(db), [post_case])

            # 质控题、没有委员投过票的、机器自动结案的都不进公示栏
            report = await self._report(db, "alice", "insult")
            golden = await case_service.resolve_case(db, report.case_id, source="admin", actor_id=self.ids["admin"], upheld=True, category="harassment")
            golden.is_golden, golden.golden_verified = True, True
            member = await jury_service.get_member(db, self.ids["juror4"])
            member.votes_since_quality = jury_service.QUALITY_MAX_GAP + 1
            auto_report = await self._report(db, "alice", "insult2")
            auto = await db.get(ReportCase, auto_report.case_id)
            await case_service._close(
                db, auto, upheld=True, source="auto", actor_id=None, category="harassment", level=2, note="自动审核确认违规", violation_id=None, action="recall_message"
            )
            await db.commit()
            juror4 = await self._user(db, "juror4")
            self.assertEqual(await self._real(db, (await jury_service.next_case(db, juror4))["case_id"]), golden.id)
            await jury_service.cast_vote(db, juror4, await self._rid(db, juror4, golden.id), "l2", None)
            for case_id in (golden.id, auto.id):
                await self._age(db, case_id, 200)
            self.assertEqual(await self._public_ids(db), [post_case])
            self.assertEqual((await self._code(overview_service.public_case(db, golden.id)))[0], 404)

    async def test_content_is_only_published_for_upheld_public_posts(self) -> None:
        async with self.sessions() as db:
            post_case = await self._post_case(db)
            await self._settle(db, post_case)
            message_case = await self._jury_case(db, "insult")
            await self._settle(db, message_case)
            clean_case = await self._jury_case(db, "insult2")
            await self._settle(db, clean_case, vote="none")
            for index, case_id in enumerate((post_case, message_case, clean_case)):
                await self._age(db, case_id, 100 - index)
            self.assertEqual(await self._public_ids(db), [clean_case, message_case, post_case])
            self.assertEqual(await self._public_ids(db, kind="post"), [post_case])
            self.assertEqual(await self._public_ids(db, kind="message"), [clean_case, message_case])
            self.assertEqual(await self._public_ids(db, outcome="clean"), [clean_case])
            self.assertEqual(await self._public_ids(db, outcome="violation"), [message_case, post_case])
            self.assertEqual(await self._public_ids(db, category="harassment"), [message_case])
            page, total = await overview_service.public_cases(db, limit=1, offset=1)
            self.assertEqual((total, [item["case_id"] for item in page]), (3, [message_case]))

            async def content(case_id: int) -> tuple[str | None, str | None, bool]:
                detail = PublicCaseDetail(**await overview_service.public_case(db, case_id))
                return (detail.content.text if detail.content else None, detail.content_hidden_reason, detail.has_content)

            # 群聊消息默认不带内容；认定不违规的只公示结论和票数
            self.assertEqual(await content(message_case), (None, "group_message", False))
            self.assertEqual(await content(clean_case), (None, "clean", False))
            clean = await overview_service.public_case(db, clean_case)
            self.assertEqual((clean["outcome"], clean["category"], clean["level"], clean["votes"]), ("clean", None, None, 3))

            # 开关打开后，群聊消息案件给被举报那一条的文字；不违规的仍然不给
            await jury_settings.update_switches(db, self.ids["admin"], {"public_group_content": True})
            self.assertEqual(await content(message_case), ("你们都是废物", None, True))
            self.assertEqual(await content(clean_case), (None, "clean", False))
            self.assertNotIn("大家好", json.dumps(await overview_service.public_case(db, message_case), default=str, ensure_ascii=False))
            # 隐私 / 色情类不带内容
            case = await db.get(ReportCase, message_case)
            case.verdict_category = "privacy"
            await db.commit()
            self.assertEqual(await content(message_case), (None, "sensitive", False))

            # 被改判的标「已改判」并撤下内容
            self.assertEqual(await content(post_case), ("低价卖号，加微信", None, True))
            await case_service.overturn_case(db, post_case, actor_id=self.ids["admin"], upheld=False, note="是正常的交易讨论")
            detail = await overview_service.public_case(db, post_case)
            self.assertEqual((detail["content"], detail["content_hidden_reason"], detail["overturned"], detail["overturn_note"], detail["outcome"]), (None, "overturned", True, "是正常的交易讨论", "clean"))
            self.assertEqual(await self._public_ids(db, outcome="clean"), [clean_case, post_case])


class OverturnTests(JuryV2TestCase):
    async def test_overturning_a_violation_revokes_the_penalty_and_restores_content(self) -> None:
        async with self.sessions() as db:
            case_id = await self._jury_case(db, "insult")
            await self._report(db, "bob", "insult")
            await self._vote(db, "juror1", "l2", case_id=case_id)
            await self._vote(db, "juror2", "none", case_id=case_id)
            await self._vote(db, "juror3", "l2", case_id=case_id)
            await self._vote(db, "juror4", "l3", case_id=case_id)
            self.assertTrue((await self._vote(db, "juror5", "l2", case_id=case_id))["settled"])
            message = await db.get(Message, self.messages["insult"])
            self.assertIsNotNone(message.recalled_at)
            violation = (await db.execute(select(UserViolation))).scalar_one()
            self.assertEqual((violation.status, violation.case_id), ("active", case_id))
            agreeing, dissenting = await jury_service.get_member(db, self.ids["juror1"]), await jury_service.get_member(db, self.ids["juror2"])
            self.assertEqual((agreeing.votes_settled, agreeing.votes_agreed, dissenting.votes_settled, dissenting.votes_agreed), (1, 1, 1, 0))
            self.assertEqual(await self._code(case_service.overturn_case(db, case_id, actor_id=self.ids["admin"], upheld=True, note="重复")), (409, "verdict_unchanged"))

            with self._events() as events:
                result = await case_service.overturn_case(db, case_id, actor_id=self.ids["admin"], upheld=False, note="结合上下文是朋友间的玩笑")
            case = result["case"]
            self.assertEqual((result["content_restored"], result["revoked_violation_ids"]), (True, [violation.id]))
            self.assertEqual((case.status, case.verdict_category, case.verdict_level, case.overturned_by_id, case.overturn_note), ("dismissed", None, None, self.ids["admin"], "结合上下文是朋友间的玩笑"))
            self.assertIsNotNone(case.overturned_at)
            await db.refresh(violation)
            await db.refresh(message)
            self.assertEqual((violation.status, message.recalled_at), ("revoked", None))
            self.assertEqual(await penalty_service.active_points(db, self.ids["offender"]), 0)
            # 举报改为不成立，但不算恶意
            reports = await case_service.case_reports(db, case_id)
            self.assertEqual({(report.status, report.malicious, report.action) for report in reports}, {(ReportStatus.REJECTED, False, "none")})
            # 委员的一致率跟着变：已出结论的票数不变，一致的票数对调
            for member in (agreeing, dissenting):
                await db.refresh(member)
            self.assertEqual((agreeing.votes_settled, agreeing.votes_agreed, dissenting.votes_settled, dissenting.votes_agreed), (1, 0, 1, 1))
            agreed = dict((await db.execute(select(JuryVote.juror_id, JuryVote.agreed).where(JuryVote.case_id == case_id))).all())
            self.assertEqual((agreed[self.ids["juror1"]], agreed[self.ids["juror2"]], agreed[self.ids["juror4"]]), (False, True, False))
            detail = await jury_service.case_detail(db, self.ids["juror1"], await self._rid(db, self.ids["juror1"], case_id))
            self.assertEqual((detail["my_vote"]["agreed"], detail["verdict"]["outcome"], detail["verdict"]["overturned"], detail["verdict"]["overturn_note"]), (False, "clean", True, "结合上下文是朋友间的玩笑"))
            # 通知：投过票的委员、举报人、被处罚的人
            by_event = {event: (users, data) for event, users, data in events}
            self.assertEqual(
                self._per_user(events, "jury.case.settled"),
                {self.ids[name]: {"outcome": "clean", "case_id": await self._rid(db, name, case_id)} for name in ("juror1", "juror2", "juror3", "juror4", "juror5")},
            )
            self.assertEqual(by_event["moderation.violation.updated"][0], {self.ids["offender"]})
            self.assertEqual({users.pop() for event, users, _data in events if event == "report.resolved"}, {self.ids["alice"], self.ids["bob"]})
            log = (await db.execute(select(AdminAuditLog).where(AdminAuditLog.action == "case.overturned"))).scalar_one()
            self.assertEqual((log.actor_id, log.target_id, json.loads(log.details)["from"]["status"], json.loads(log.details)["to"]["status"]), (self.ids["admin"], case_id, "resolved", "dismissed"))

            # 后台的案件详情带上新字段
            read = await admin_api._case_detail(db, case, self.ids["admin"], content_restored=result["content_restored"])
            self.assertEqual((read.jury_outcome, read.overturn_note, read.content_restored, read.jury_block_reason), ("consensus", "结合上下文是朋友间的玩笑", True, None))
            self.assertIsNotNone(read.queued_at)
            self.assertIsNotNone(read.overturned_at)
            self.assertEqual([(vote.alias, vote.tier, vote.vote) for vote in read.votes][:2], [("委员 A", "trainee", "l2"), ("委员 B", "trainee", "none")])
            # 后台能把委员说的评审编号对上
            self.assertEqual([vote.review_id for vote in read.votes][:2], [await self._rid(db, "juror1", case_id), await self._rid(db, "juror2", case_id)])
            self.assertEqual(read.opinions, [])

    async def test_overturning_a_dismissal_records_the_penalty(self) -> None:
        async with self.sessions() as db:
            case_id = await self._jury_case(db, "insult")
            await self._vote(db, "juror1", "none", "只是口头禅", case_id=case_id)
            await self._vote(db, "juror2", "none", case_id=case_id)
            await self._vote(db, "juror3", "l2", case_id=case_id)
            await self._vote(db, "juror4", "none", case_id=case_id)
            self.assertTrue((await self._vote(db, "juror5", "none", case_id=case_id))["settled"])
            self.assertEqual(await self._code(case_service.overturn_case(db, case_id, actor_id=self.ids["admin"], upheld=False, note="重复")), (409, "verdict_unchanged"))
            open_case = await self._jury_case(db, "insult2")
            self.assertEqual(await self._code(case_service.overturn_case(db, open_case, actor_id=self.ids["admin"], upheld=True, note="还没结")), (409, "case_not_closed"))

            result = await case_service.overturn_case(db, case_id, actor_id=self.ids["admin"], upheld=True, category="harassment", level=3, note="对象是未成年人，性质恶劣")
            case = result["case"]
            violation = (await db.execute(select(UserViolation).where(UserViolation.case_id == case_id))).scalar_one()
            self.assertEqual((case.status, case.verdict_category, case.verdict_level, case.violation_id, case.verdict_source), ("resolved", "harassment", 3, violation.id, "jury"))
            self.assertEqual((violation.status, violation.level, violation.source, violation.actor_id), ("active", 3, "report", self.ids["admin"]))
            self.assertIsNotNone((await db.get(Message, self.messages["insult"])).recalled_at)
            report = (await case_service.case_reports(db, case_id))[0]
            self.assertEqual((report.status, report.malicious), (ReportStatus.REVIEWED, False))
            self.assertIn("recall_message", report.action)
            lone, majority = await jury_service.get_member(db, self.ids["juror3"]), await jury_service.get_member(db, self.ids["juror1"])
            self.assertEqual((lone.votes_settled, lone.votes_agreed, majority.votes_settled, majority.votes_agreed), (1, 1, 1, 0))
            detail = await jury_service.case_detail(db, self.ids["juror1"], await self._rid(db, self.ids["juror1"], case_id))
            self.assertEqual((detail["my_vote"]["agreed"], detail["verdict"]["outcome"], detail["verdict"]["level"], detail["verdict"]["overturned"]), (False, "violation", 3, True))

    async def test_accepted_appeal_and_direct_revocation_overturn_the_case(self) -> None:
        async with self.sessions() as db:
            appealed = await self._jury_case(db, "insult")
            await self._settle(db, appealed)
            revoked = await self._jury_case(db, "insult2")
            await self._settle(db, revoked)
            first, second = (await db.execute(select(UserViolation).order_by(UserViolation.id))).scalars().all()
            self.assertEqual((first.case_id, second.case_id), (appealed, revoked))
            member = await jury_service.get_member(db, self.ids["juror1"])
            self.assertEqual((member.votes_settled, member.votes_agreed), (2, 2))

            await penalty_service.submit_appeal(db, self.ids["offender"], first.id, "这是我们之间的玩笑话")
            with self._events() as events:
                violation = await penalty_service.review_appeal(db, first.id, reviewer_id=self.ids["admin"], accept=True, note="核实是熟人之间的玩笑")
                await penalty_service.notify_violation_updated(db, violation, "appeal_accepted")
            case = await db.get(ReportCase, appealed)
            await db.refresh(case)
            self.assertEqual((violation.status, case.status, case.overturned_by_id), ("revoked", "dismissed", self.ids["admin"]))
            # 管理员写的申诉处理意见不外传，公示栏里只有一句通用说明
            self.assertEqual(case.overturn_note, "申诉通过，处罚已撤销")
            self.assertIsNone((await db.get(Message, self.messages["insult"])).recalled_at)
            self.assertEqual({report.status for report in await case_service.case_reports(db, appealed)}, {ReportStatus.REJECTED})
            self.assertEqual(
                self._per_user(events, "jury.case.settled"),
                {self.ids[name]: {"outcome": "clean", "case_id": await self._rid(db, name, appealed)} for name in ("juror1", "juror2", "juror3")},
            )
            self.assertEqual(len((await db.execute(select(AdminAuditLog).where(AdminAuditLog.action == "case.overturned"))).scalars().all()), 1)

            # 管理员直接撤销处罚也一样
            await penalty_service.revoke_violation(db, second.id, actor_id=self.ids["admin"], reason="误判")
            await db.commit()
            case = await db.get(ReportCase, revoked)
            await db.refresh(case)
            self.assertEqual((case.status, case.overturn_note), ("dismissed", "处罚已撤销"))
            self.assertIsNotNone(case.overturned_at)
            await db.refresh(member)
            self.assertEqual((member.votes_settled, member.votes_agreed), (2, 0))
            # 已经不是「违规」结论的案件，不会被再次改判
            self.assertEqual(await self._code(case_service.overturn_case(db, revoked, actor_id=self.ids["admin"], upheld=False, note="重复")), (409, "verdict_unchanged"))


class OverviewAndSettingsTests(JuryV2TestCase):
    async def test_overview_withholds_rates_until_there_is_enough_sample(self) -> None:
        async with self.sessions() as db:
            violation = await self._jury_case(db, "insult")
            await self._settle(db, violation)
            clean = await self._jury_case(db, "insult2")
            await self._settle(db, clean, vote="none")
            split = await self._jury_case(db, "insult3")
            for juror, vote in (("juror1", "l2"), ("juror2", "none"), ("juror3", "l1"), ("juror4", "none"), ("juror5", "l2")):
                await self._vote(db, juror, vote, case_id=split)
            # 没进过委员队列的案件（历史回填、机器结案）不计入
            legacy = await self._report(db, "alice", "spam", "垃圾广告")
            await case_service.resolve_case(db, legacy.case_id, source="admin", actor_id=self.ids["admin"], upheld=True)
            waiting = await self._jury_case(db, "plaza_insult")
            # 其中一个案件是前天进的队列、昨天定的案
            earlier = await db.get(ReportCase, clean)
            earlier.queued_at, earlier.closed_at = datetime.now(UTC) - timedelta(days=2), datetime.now(UTC) - timedelta(days=1)
            await db.commit()
            juror, alice = await self._user(db, "juror1"), await self._user(db, "alice")

            overview = JuryOverview(**await overview_service.overview(db, juror))
            self.assertEqual(overview.jurors, {"total": 9, "trainee": 9, "regular": 0, "senior": 0})
            self.assertEqual((overview.backlog, overview.settled), ({"jury": 1, "panel": 0}, {"d7": 2, "d30": 2, "total": 2}))
            self.assertEqual((overview.window_days, overview.sample, overview.sample_enough), (30, 3, False))
            # 样本不够：三个比率都不给，只给件数
            self.assertEqual((overview.consensus_rate, overview.violation_rate, overview.overturn_rate), (None, None, None))
            self.assertEqual(overview.counts, {"consensus": 2, "escalated": 1, "violation": 1, "clean": 1, "overturned": 0})
            self.assertEqual(overview.median_hours, 0.0)
            # 近 30 天每天一项，UTC+8 自然日、今天在最后；样本不足也照样给。没进过委员队列的那个案件不算
            today = datetime.now(UTC).astimezone(jury_service.LOCAL_TZ).date()
            self.assertEqual([day.date for day in overview.daily], [(today - timedelta(days=29 - index)).isoformat() for index in range(30)])
            self.assertEqual([(day.queued, day.settled) for day in overview.daily[-3:]], [(1, 0), (0, 1), (3, 1)])
            self.assertEqual(sum(day.queued + day.settled for day in overview.daily[:-3]), 0)
            self.assertEqual((overview.me.status, overview.me.tier, overview.me.available_cases, overview.me.voted_today, overview.me.daily_limit), ("active", "trainee", 1, 3, jury_service.DAILY_VOTE_LIMIT))

            # 全站数字缓存 60 秒，me 不缓存
            await case_service.overturn_case(db, violation, actor_id=self.ids["admin"], upheld=False, note="改判")
            cached = await overview_service.overview(db, alice)
            self.assertEqual((cached["generated_at"], cached["counts"]["overturned"]), (overview.generated_at, 0))
            self.assertEqual(cached["me"], {"status": "none", "tier": None, "available_cases": None, "voted_today": 0, "daily_limit": jury_service.DAILY_VOTE_LIMIT})

            overview_service.reset_overview_cache()
            with patch.object(overview_service, "MIN_SAMPLE", 3):
                enough = await overview_service.overview(db, juror)
            self.assertTrue(enough["sample_enough"])
            # 2 个委员定案 / 3 个离开队列；定案的 2 个里现在 0 个违规、1 个被改判
            self.assertEqual((enough["consensus_rate"], enough["violation_rate"], enough["overturn_rate"]), (0.667, 0.0, 0.5))
            self.assertEqual(enough["counts"], {"consensus": 2, "escalated": 1, "violation": 0, "clean": 2, "overturned": 1})
            self.assertIsNotNone(waiting)

    async def test_switches_config_and_tiers(self) -> None:
        async with self.sessions() as db:
            self.assertEqual(await jury_settings.get_switches(db), {name: False for name in jury_settings.SWITCHES})
            config = JuryConfig(**await overview_service.config(db))
            self.assertEqual((config.version, config.switches), (2, {"auto_appoint": False, "appeals": False, "machine_review": False}))
            # 一期的能力和投票选项原样在前面，二期的追加在后面
            self.assertEqual(config.capabilities[:5], ["overview", "case_detail", "opinions", "votes", "public_cases"])
            self.assertEqual([tier.key for tier in config.tiers], ["trainee", "regular", "senior"])
            self.assertEqual([(vote.value, vote.kind, vote.level) for vote in config.votes][:5], [("none", "clean", None), ("l1", "violation", 1), ("l2", "violation", 2), ("l3", "violation", 3), ("abstain", "abstain", None)])
            self.assertEqual(config.rules.model_dump(), {
                "seats_per_case": 5, "quick_consensus_votes": 3, "full_consensus_votes": 4, "timeout_hours": 72, "assignment_ttl_hours": 24,
                "daily_votes": 30, "term_days": 30, "context_near": 3, "content_retention_days": 30, "publish_delay_hours": 72, "min_sample": 5,
                "max_ban_hours": penalty_service.CAPPED_BAN_HOURS,
                # 二期新增
                "severe_min_votes": 5, "need_context_votes": 2,
            })
            self.assertEqual(config.rules.max_ban_hours, 168)
            self.assertEqual(config.opinions.model_dump(), {
                "max_length": 500, "per_case": 20, "per_minute": 5, "per_day": 100, "auto_hide_reports": 2,
                "report_reasons": ["人身攻击", "泄露身份或隐私", "照抄违规原文", "与案件无关", "其他"],
                # 二期新增
                "like_reward_threshold": 3,
            })
            self.assertEqual(config.eligibility.model_dump(), {"min_account_age_days": 30, "min_recent_messages": 30, "recent_activity_days": 30})
            self.assertEqual(settings.jury_large_group_members, 200)

            # 改开关：立即生效，写审计日志，没变化的不记
            changed = await jury_settings.update_switches(db, self.ids["admin"], {"appeals": True, "auto_appoint": False, "public_group_content": True})
            self.assertEqual(changed, {"auto_appoint": False, "appeals": True, "machine_review": False, "public_group_content": True, "opinions_open_to_jurors": False, "exam_required": False})
            self.assertTrue(await jury_settings.is_enabled(db, "public_group_content"))
            self.assertEqual((await overview_service.config(db))["switches"], {"auto_appoint": False, "appeals": True, "machine_review": False})
            log = (await db.execute(select(AdminAuditLog).where(AdminAuditLog.action == "jury.settings.update"))).scalar_one()
            self.assertEqual((log.actor_id, json.loads(log.details)), (self.ids["admin"], {"appeals": {"old": False, "new": True}, "public_group_content": {"old": False, "new": True}}))
            read = await admin_api._jury_settings_read(db)
            self.assertEqual((read.switches["appeals"], read.thresholds["large_group_members"], read.thresholds["timeout_hours"]), (True, 200, 72))
            # 另一个进程改的值，过了缓存时间就能读到
            jury_settings.reset_cache()
            self.assertTrue((await jury_settings.get_switches(db))["appeals"])

            # 创始任命：直接任命为任意层级；之后可以单独调层级
            with self._events() as events:
                founder = await jury_service.review_member(db, self.ids["alice"], action="approve", actor_id=self.ids["admin"], note=None, tier="senior")
                self.assertEqual((founder.status, founder.tier), ("active", "senior"))
                juror = await jury_service.review_member(db, self.ids["juror1"], action="set_tier", actor_id=self.ids["admin"], note="表现稳定", tier="regular")
            self.assertEqual((juror.status, juror.tier, juror.review_note), ("active", "regular", None))
            self.assertEqual([users for event, users, _data in events if event == "jury.status.changed"], [{self.ids["alice"]}, {self.ids["juror1"]}])
            self.assertEqual((await jury_service.my_status(db, await self._user(db, "alice")))["tier"], "senior")
            items, _total = await jury_service.list_members(db, None, 1, 0, user_id=self.ids["juror1"])
            self.assertEqual(JuryMemberRead(**items[0]).tier, "regular")
            plain = await jury_service.review_member(db, self.ids["juror2"], action="reinstate", actor_id=self.ids["admin"], note=None)
            self.assertEqual(plain.tier, "trainee")
            self.assertEqual(await self._code(jury_service.review_member(db, self.ids["juror2"], action="set_tier", actor_id=self.ids["admin"], note=None, tier="boss")), (422, "invalid_tier"))
            with self.assertRaises(ValueError):
                JuryMemberReview(action="set_tier")
            with self.assertRaises(ValueError):
                JuryMemberReview(action="suspend", tier="senior")
            overview_service.reset_overview_cache()
            self.assertEqual((await overview_service.overview(db, await self._user(db, "bob")))["jurors"], {"total": 10, "trainee": 8, "regular": 1, "senior": 1})


class HttpSmokeTests(JuryV2TestCase):
    """走一遍真实的路由和响应模型：路径顺序、查询参数、错误结构。"""

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

    async def test_jury_and_admin_routes(self) -> None:
        async with self.sessions() as db:
            case_id = await self._jury_case(db, "insult")
        client = self.client
        config = (await client.get("/jury/config")).json()
        self.assertEqual((config["version"], config["rules"]["context_near"], config["switches"]["appeals"]), (2, 3, False))
        overview = (await client.get("/jury/overview")).json()
        self.assertEqual((overview["backlog"]["jury"], overview["sample_enough"], overview["me"]["available_cases"]), (1, False, 1))
        self.assertEqual((len(overview["daily"]), overview["daily"][-1]["queued"], overview["daily"][-1]["settled"]), (30, 1, 0))
        self.assertEqual(config["rules"]["max_ban_hours"], 168)
        me = (await client.get("/jury/me")).json()
        self.assertEqual((me["status"], me["tier"], me["available_cases"], me["daily_limit"]), ("active", "trainee", 1, 30))
        self.assertIsNone(me["restriction"])

        # /cases/next 没有被 /cases/{case_id} 吃掉
        assigned = (await client.get("/jury/cases/next")).json()["case"]
        self.assertEqual(assigned["messages"][1]["offset"], 0)
        self.assertIsNotNone(assigned["expires_at"])
        # 路径里的 {case_id} 是评审编号；案件的真实编号在委员侧接口里一律 404
        real_id, case_id = case_id, assigned["case_id"]
        self.assertNotEqual(case_id, real_id)
        for method, path in (("get", ""), ("get", "/opinions"), ("post", "/vote"), ("post", "/opinions")):
            wrong = await getattr(client, method)(f"/jury/cases/{real_id}{path}", **({"json": {"vote": "l2", "content": "x"}} if method == "post" else {}))
            self.assertEqual(wrong.status_code, 404, path)
        self.assertEqual((await client.get(f"/jury/cases/{case_id}")).json()["detail"]["code"], "not_voted")
        voted = await client.post(f"/jury/cases/{case_id}/vote", json={"vote": "l2", "note": "这是在骂人"})
        self.assertEqual(voted.status_code, 200)
        self.assertEqual((voted.json()["case_id"], voted.json()["case"]["case_id"], voted.json()["case"]["public_case_id"]), (case_id, case_id, None))
        self.assertEqual((voted.json()["settled"], voted.json()["case"]["my_vote"]["alias"], voted.json()["case"]["tally"]), (False, "委员 A", None))
        detail = await client.get(f"/jury/cases/{case_id}")
        self.assertEqual((detail.status_code, detail.json()["stage"], detail.json()["votes_cast"], detail.json()["verdict"]), (200, "jury", 1, None))

        opinions = (await client.get(f"/jury/cases/{case_id}/opinions")).json()
        self.assertEqual((opinions["total"], opinions["items"][0]["is_note"], opinions["items"][0]["replies"]), (1, True, []))
        note_id = opinions["items"][0]["id"]
        reply = await client.post(f"/jury/cases/{case_id}/opinions", json={"content": "补充：前后几条都是", "parent_id": note_id})
        self.assertEqual((reply.status_code, reply.json()["parent_id"], reply.json()["mine"]), (200, note_id, True))
        too_long = await client.post(f"/jury/cases/{case_id}/opinions", json={"content": "字" * 501})
        self.assertEqual((too_long.status_code, too_long.json()["detail"]["code"]), (422, "invalid_opinion"))
        self.assertEqual((await client.post(f"/jury/opinions/{note_id}/like")).json()["detail"]["code"], "cannot_like_own")
        self.assertEqual((await client.delete(f"/jury/opinions/{reply.json()['id']}")).json(), {"id": reply.json()["id"], "state": "deleted"})

        # 契约里的查询串带着空值：留空表示不筛选
        votes = await client.get("/jury/votes?limit=20&offset=0&state=&outcome=&agreed=&vote=")
        self.assertEqual((votes.status_code, votes.json()["total"], votes.json()["items"][0]["case_id"]), (200, 1, case_id))
        self.assertIsNone(votes.json()["items"][0]["public_case_id"])
        self.assertEqual((await client.get("/jury/votes?state=open&agreed=false&vote=l1,l2")).json()["total"], 0)
        self.assertEqual((await client.get("/jury/votes?vote=bogus")).json()["detail"]["code"], "invalid_vote_filter")
        self.assertEqual((await client.get("/jury/votes?limit=51")).status_code, 422)
        stats = (await client.get("/jury/me/stats")).json()
        self.assertEqual((stats["votes_total"], stats["opinions_total"], len(stats["trend"])), (1, 1, 30))
        self.assertEqual([item["case_id"] for item in (await client.get("/jury/history")).json()], [case_id])
        board = await client.get("/jury/public/cases?limit=20&offset=0&kind=&outcome=&category=")
        self.assertEqual((board.status_code, board.json()), (200, {"items": [], "total": 0}))
        # 公示栏用的是公开编号：还没公示，详情和只读观点区都是 404
        self.assertEqual((await client.get(f"/jury/public/cases/{real_id}")).status_code, 404)
        self.assertEqual((await client.get(f"/jury/public/cases/{real_id}/opinions")).status_code, 404)

        # 别的委员：别人的评审编号对他无效，也看不到那条观点
        self.acting = "juror2"
        self.assertEqual((await client.get(f"/jury/cases/{case_id}")).status_code, 404)
        self.assertEqual((await client.get(f"/jury/cases/{case_id}/opinions")).json()["detail"]["code"], "case_not_found")
        self.assertEqual((await client.post(f"/jury/cases/{case_id}/vote", json={"vote": "l2"})).status_code, 404)
        self.assertEqual((await client.post(f"/jury/opinions/{note_id}/like")).status_code, 404)
        self.assertEqual((await client.post(f"/jury/opinions/{note_id}/report", json={"reason": "人身攻击"})).status_code, 404)
        self.assertEqual((await client.get("/admin/jury/settings")).status_code, 403)

        self.acting = "admin"
        settings_read = (await client.get("/admin/jury/settings")).json()
        self.assertEqual((settings_read["switches"]["public_group_content"], settings_read["thresholds"]["publish_delay_hours"]), (False, 72))
        updated = await client.put("/admin/jury/settings", json={"public_group_content": True})
        self.assertEqual((updated.status_code, updated.json()["switches"]["public_group_content"], updated.json()["switches"]["appeals"]), (200, True, False))
        expired = await client.get("/admin/jury/members?status=expired")
        self.assertEqual((expired.status_code, expired.json()), (200, {"items": [], "total": 0}))
        self.assertEqual((await client.get("/admin/jury/members?status=bogus")).status_code, 422)
        appointed = await client.post(f"/admin/jury/members/{self.ids['alice']}/review", json={"action": "approve", "tier": "senior"})
        self.assertEqual((appointed.status_code, appointed.json()["status"], appointed.json()["tier"]), (200, "active", "senior"))
        self.assertEqual((await client.post(f"/admin/jury/members/{self.ids['alice']}/review", json={"action": "set_tier"})).status_code, 422)
        listed = (await client.get("/admin/jury/opinions?reported=false&state=&case_id=")).json()
        self.assertEqual((listed["total"], listed["items"][-1]["author"]["username"], listed["items"][-1]["content"]), (2, "juror1", "这是在骂人"))
        # 后台一律用案件的真实编号
        review_id, case_id = case_id, real_id
        self.assertEqual((await client.get(f"/admin/jury/opinions?state=deleted&case_id={case_id}")).json()["total"], 1)
        self.assertEqual((await client.get("/admin/jury/opinions?case_id=99999")).json(), {"items": [], "total": 0})
        moderated = await client.post(f"/admin/jury/opinions/{note_id}/moderate", json={"action": "delete", "note": "测试"})
        self.assertEqual((moderated.status_code, moderated.json()["state"]), (200, "deleted"))
        cases = (await client.get("/admin/cases?status=open&queue=jury")).json()
        self.assertEqual((cases["total"], cases["items"][0]["jury_outcome"], cases["items"][0]["published_at"]), (1, None, None))
        self.assertIsNotNone(cases["items"][0]["queued_at"])
        admin_detail = (await client.get(f"/admin/cases/{case_id}")).json()
        self.assertEqual([(vote["alias"], vote["tier"], vote["juror"]["username"]) for vote in admin_detail["votes"]], [("委员 A", "trainee", "juror1")])
        self.assertEqual(admin_detail["votes"][0]["review_id"], review_id)
        self.assertEqual([opinion["state"] for opinion in admin_detail["opinions"]], ["deleted", "deleted"])
        early = await client.post(f"/admin/cases/{case_id}/overturn", json={"outcome": "dismiss", "note": "还没结案"})
        self.assertEqual((early.status_code, early.json()["detail"]["code"]), (409, "case_not_closed"))
        self.assertEqual((await client.post(f"/admin/cases/{case_id}/overturn", json={"outcome": "dismiss"})).status_code, 422)
        resolved = await client.post(f"/admin/cases/{case_id}/resolve", json={"outcome": "violation", "category": "harassment"})
        self.assertEqual((resolved.status_code, resolved.json()["jury_outcome"], resolved.json()["status"]), (200, "manual", "resolved"))
        overturned = await client.post(f"/admin/cases/{case_id}/overturn", json={"outcome": "dismiss", "note": "结合上下文是玩笑"})
        self.assertEqual(
            (overturned.status_code, overturned.json()["status"], overturned.json()["content_restored"], overturned.json()["overturn_note"]),
            (200, "dismissed", True, "结合上下文是玩笑"),
        )


if __name__ == "__main__":
    unittest.main()
