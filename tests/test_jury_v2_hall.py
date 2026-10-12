"""风纪系统 v2 三期：判例和议事厅（docs/jury-v2.md 3.4、3.5、3.8）。"""

from __future__ import annotations

import asyncio
import json
import unittest
from datetime import UTC, datetime, timedelta
from typing import Annotated
from unittest.mock import patch

import httpx
from fastapi import Depends, FastAPI, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

import app.models  # noqa: F401
from app.api.v1 import admin as admin_api
from app.api.v1 import admin_jury_hall as admin_hall_api
from app.api.v1 import jury as jury_api
from app.api.v1 import jury_hall as hall_api
from app.core.config import settings
from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.models.case import JuryVote, ReportCase
from app.models.jury_hall import JuryHallComment, JuryHallPost, JuryHallReport, JuryHallVote, JuryPrecedent
from app.models.penalty import UserViolation
from app.models.report import AdminAuditLog
from app.models.user import User
from app.schemas.case import JuryCaseDetail
from app.schemas.jury import AdminQuestionRead, JuryConfig, PracticeNext, PublicCase, PublicCaseDetail
from app.schemas.jury_hall import (
    AdminHallPostDetail,
    AdminHallPostRead,
    AdminHallReportRead,
    AdminPrecedentRead,
    HallComment,
    HallPost,
    Precedent,
    PrecedentDetail,
)
from app.services import cases as case_service
from app.services import jury as jury_service
from app.services import jury_bank as bank_service
from app.services import jury_exam as exam_service
from app.services import jury_hall as hall_service
from app.services import jury_overview as overview_service
from app.services import jury_precedents as precedent_service
from app.services import jury_settings
from app.services import moderation as moderation_service
from app.services import penalties as penalty_service

# 夹具和工具方法沿用一、二期的；基类自己没有测试方法，不会被重复收集。
from test_jury_v2 import JURORS
from test_jury_v2_phase2 import Phase2TestCase

THREE_FOR = {"juror1": "l2", "juror2": "l2", "juror3": "l2"}


class HallTestCase(Phase2TestCase):
    """juror1 是资深委员，juror2 是正式委员，其余七位在任委员都是见习。"""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        # IsolatedAsyncioTestCase 默认开 asyncio 调试模式，在这台机器上慢一个数量级。
        asyncio.get_running_loop().set_debug(False)
        # 质控题的位置是随机的；这里的测试会造已核对的题，把随机关掉，免得取件时碰巧发到质控题。
        roll = patch.object(jury_service, "_quality_roll", lambda: 1.0)
        roll.start()
        self.addCleanup(roll.stop)
        async with self.sessions() as db:
            await self._tier(db, "juror1", "senior")
            await self._tier(db, "juror2", "regular")

    # ------------------------------------------------------------ 工具

    async def _tier(self, db, name: str, tier: str) -> None:
        (await self._member(db, name)).tier = tier
        await db.commit()

    async def _mute(self, db, name: str, hours: float | None = 1) -> User:
        """让一个人处在受处罚状态（平台禁言）；hours 为 None 表示解除。"""
        user = await self._user(db, name)
        user.muted_until = datetime.now(UTC) + timedelta(hours=hours) if hours is not None else None
        await db.commit()
        return user

    async def _mark(self, db, juror: str, case_id: int, title: str = "反复辱骂群成员属于辱骂骚扰", summary: str = "连续几条都指向同一个人，不是玩笑。") -> dict:
        """资深委员把自己投过票的案件标为判例（用他自己的评审编号）。"""
        user = await self._user(db, juror)
        return await precedent_service.create_by_senior(db, user, await self._rid(db, user, case_id), title, summary)

    async def _audit_actions(self, db, prefix: str) -> list[str]:
        rows = await db.execute(select(AdminAuditLog.action).where(AdminAuditLog.action.like(f"{prefix}%")).order_by(AdminAuditLog.id))
        return list(rows.scalars().all())

    async def _hall_post(self, db, author: str, kind: str = "discussion", title: str = "关于刷屏的尺度", content: str = "想听听大家怎么判断刷屏。") -> dict:
        return await hall_service.create_post(db, await self._user(db, author), kind, title, content)

    async def _hall_row(self, db, post_id: int) -> JuryHallPost:
        post = await db.get(JuryHallPost, post_id)
        await db.refresh(post)
        return post

    async def _ballots(self, db, post_id: int, yes: int, no: int) -> None:
        """按 JURORS 的顺序投出 yes 张赞成、no 张反对。"""
        for index, name in enumerate(JURORS[: yes + no]):
            await hall_service.cast_vote(db, await self._user(db, name), post_id, "yes" if index < yes else "no")

    async def _expire(self, db, post_id: int) -> None:
        """把提案的表决截止时间挪到过去。"""
        post = await db.get(JuryHallPost, post_id)
        post.vote_ends_at = datetime.now(UTC) - timedelta(minutes=1)
        await db.commit()

    async def _comment(self, db, author: str, post_id: int, content: str, parent_id: int | None = None) -> dict:
        return await hall_service.create_comment(db, await self._user(db, author), post_id, content, parent_id)


# ---------------------------------------------------------------- 判例：谁能标、什么能标


class PrecedentRuleTests(HallTestCase):
    async def test_seniors_mark_cases_they_voted_on_by_review_id(self) -> None:
        async with self.sessions() as db:
            case_id = await self._jury_case(db, "insult")
            await self._settle(db, case_id)
            await self._tier(db, "juror4", "senior")
            juror1, juror2, juror4, bob = [await self._user(db, name) for name in ("juror1", "juror2", "juror4", "bob")]
            rid1, rid2 = await self._rid(db, juror1, case_id), await self._rid(db, juror2, case_id)
            title, summary = "反复辱骂群成员属于辱骂骚扰", "连续几条都指向同一个人，不是玩笑。"

            async def refused(user: User, review_id: int, new_title: str = title, new_summary: str = summary) -> tuple[int, str | None]:
                return await self._code(precedent_service.create_by_senior(db, user, review_id, new_title, new_summary))

            # 正式委员、非委员都不行，哪怕拿的是自己的评审编号
            self.assertEqual(await refused(juror2, rid2), (403, "not_senior"))
            self.assertEqual(await refused(bob, rid1), (403, "not_juror"))
            # 委员侧只认本人的评审编号：公开编号、别人的评审编号、不存在的都是 404
            for wrong in (case_id, rid2, 99999):
                self.assertEqual(await refused(juror1, wrong), (404, "case_not_found"), wrong)
            # 资深委员也只能标自己投过票的：没参与的拿别人的编号是 404，分到手还没投的是 403
            self.assertEqual(await refused(juror4, rid1), (404, "case_not_found"))
            pending = await self._open_case(db)
            db.add(JuryVote(case_id=pending.id, juror_id=juror4.id, assigned_at=datetime.now(UTC)))
            await db.commit()
            self.assertEqual(await refused(juror4, await self._rid(db, juror4, pending.id)), (403, "not_voted"))
            for bad_title, bad_summary in (("", summary), ("字" * 61, summary), (title, "  "), (title, "字" * 1001)):
                self.assertEqual(await refused(juror1, rid1, bad_title, bad_summary), (422, "invalid_precedent"))
            self.assertEqual((await db.execute(select(func.count()).select_from(JuryPrecedent))).scalar_one(), 0)

            created = Precedent(**await precedent_service.create_by_senior(db, juror1, rid1, f"  {title} ", "字" * 1000))
            # 判例里的 case_id 是案件的公开编号，不是提交上来的评审编号
            self.assertEqual((created.case_id, created.title, len(created.summary)), (case_id, title, 1000))
            self.assertNotEqual(created.case_id, rid1)
            self.assertEqual(
                (created.kind, created.outcome, created.category, created.category_label, created.level, created.by, created.mine),
                ("message", "violation", "harassment", "辱骂骚扰", 2, "senior", True),
            )
            self.assertEqual(await refused(juror1, rid1), (409, "precedent_exists"))
            # 另一位投过同一个案件的资深委员也不能再标一条
            await self._tier(db, "juror2", "senior")
            self.assertEqual(await refused(juror2, rid2), (409, "precedent_exists"))
            # 受处罚期间不能标
            await self._mute(db, "juror2")
            self.assertEqual(await refused(juror2, rid2), (403, "juror_restricted"))

    async def test_cases_that_cannot_become_precedents(self) -> None:
        async with self.sessions() as db:
            juror1, admin = await self._user(db, "juror1"), await self._user(db, "admin")

            async def refused(case_id: int) -> tuple[int, str | None]:
                return await self._code(precedent_service.create_by_senior(db, juror1, await self._rid(db, juror1, case_id), "标题", "要旨"))

            # 未定案
            open_case = await self._open_case(db)
            await self._cast(db, "juror1", open_case.id, "l2")
            # 被改判
            overturned = await self._settle_case(db, THREE_FOR)
            await case_service.overturn_case(db, overturned.id, actor_id=admin.id, upheld=False, note="结合上下文是玩笑")
            # 敏感类别：定案类别是隐私 / 色情类，分案时就标了敏感，或机器把内容判到了色情类
            privacy = await self._settle_case(db, THREE_FOR)
            privacy.verdict_category = "privacy"
            blocked = await self._settle_case(db, THREE_FOR)
            blocked.jury_block_reason = "sensitive"
            machine = await self._settle_case(db, THREE_FOR)
            machine.machine_category = "sexual"
            # 涉及私聊
            private = await self._settle_case(db, THREE_FOR)
            private.jury_eligible = False
            await db.commit()
            ineligible = (open_case, overturned, privacy, blocked, machine, private)
            for case in ineligible:
                self.assertEqual(await refused(case.id), (422, "case_not_eligible"), case.id)
                # 后台按同一套规则，只是用公开编号
                self.assertEqual(await self._code(precedent_service.admin_create(db, admin, case.id, "标题", "要旨")), (422, "case_not_eligible"), case.id)
            self.assertEqual(await self._code(precedent_service.admin_create(db, admin, 99999, "标题", "要旨")), (404, "case_not_found"))

            # 质控题那条分配记录：这个案件本身没问题，但他是把它当质控题答的
            question = await self._question(db)
            await self._cast(db, "juror1", question.id, "l2", quality=True)
            self.assertEqual(await refused(question.id), (422, "case_not_eligible"))
            self.assertEqual((await db.execute(select(func.count()).select_from(JuryPrecedent))).scalar_one(), 0)
            # 同一个案件由管理员标是可以的
            self.assertEqual((await precedent_service.admin_create(db, admin, question.id, "标题", "要旨"))["status"], "active")


# ---------------------------------------------------------------- 判例：读


class PrecedentReadTests(HallTestCase):
    async def test_list_and_detail_follow_the_public_board_rules(self) -> None:
        async with self.sessions() as db:
            post_case = await self._post_case(db)
            await self._settle(db, post_case)
            message_case = await self._jury_case(db, "insult")
            await self._settle(db, message_case)
            juror1, juror5, bob = [await self._user(db, name) for name in ("juror1", "juror5", "bob")]
            first = await self._mark(db, "juror1", post_case, "公开动态里卖号属于垃圾广告")
            second = await self._mark(db, "juror1", message_case)

            # 列表：全站可读，新的在前；mine 只对创建人为真
            items, total = await precedent_service.list_precedents(db, bob)
            self.assertEqual((total, [item["id"] for item in items], [item["mine"] for item in items]), (2, [second["id"], first["id"]], [False, False]))
            self.assertEqual([item["mine"] for item in (await precedent_service.list_precedents(db, juror1))[0]], [True, True])
            self.assertEqual([Precedent(**item).case_id for item in items], [message_case, post_case])
            for category, expected in (("advertising", [first["id"]]), ("harassment", [second["id"]]), ("fraud", [])):
                found, count = await precedent_service.list_precedents(db, bob, category=category)
                self.assertEqual((count, [item["id"] for item in found]), (len(expected), expected), category)
            page, total = await precedent_service.list_precedents(db, bob, limit=1, offset=1)
            self.assertEqual((total, [item["id"] for item in page]), (2, [first["id"]]))

            # 案件还没进公示栏（定案不满 72 小时）：公示栏不给内容，判例也不给
            early = PrecedentDetail(**await precedent_service.precedent_detail(db, bob, first["id"]))
            self.assertEqual((early.content, early.content_hidden_reason, early.case), (None, "unpublished", None))
            self.assertEqual((await self._code(overview_service.public_case(db, post_case)))[0], 404)
            await self._age(db, post_case, 100)
            await self._age(db, message_case, 99)

            async def same_as_board(precedent_id: int, case_id: int) -> dict:
                detail = await precedent_service.precedent_detail(db, bob, precedent_id)
                board = await overview_service.public_case(db, case_id)
                self.assertEqual((detail["content"], detail["content_hidden_reason"]), (board["content"], board["content_hidden_reason"]))
                return detail

            # 全站可见的内容和公示栏详情一模一样：公开动态给文字和图片数量，群聊消息默认不给
            detail = PrecedentDetail(**await same_as_board(first["id"], post_case))
            self.assertEqual((detail.content.text, detail.content.image_count, detail.content_hidden_reason, detail.case), ("低价卖号，加微信", 2, None, None))
            self.assertNotIn("/uploads/", detail.model_dump_json())
            self.assertEqual((await same_as_board(second["id"], message_case))["content_hidden_reason"], "group_message")
            await jury_settings.update_switches(db, self.ids["admin"], {"public_group_content": True})
            self.assertEqual((await same_as_board(second["id"], message_case))["content"]["text"], "你们都是废物")
            await jury_settings.update_switches(db, self.ids["admin"], {"public_group_content": False})
            # 相关处罚有待处理的申诉时公示栏会撤下案件，判例的内容跟着撤
            violation = (await db.execute(select(UserViolation).where(UserViolation.case_id == post_case))).scalar_one()
            await penalty_service.submit_appeal(db, self.ids["offender"], violation.id, "我没有卖号，是朋友借的")
            pending = await precedent_service.precedent_detail(db, bob, first["id"])
            self.assertEqual((pending["content"], pending["content_hidden_reason"]), (None, "unpublished"))
            await penalty_service.review_appeal(db, violation.id, reviewer_id=self.ids["admin"], accept=False, note="证据充分")
            await same_as_board(first["id"], post_case)

            # case 只给在任委员（不要求投过这个案件），用的是评审台那个匿名视图
            raw = await precedent_service.precedent_detail(db, juror5, second["id"])
            view = await jury_service.juror_case_view(db, await db.get(ReportCase, message_case))
            self.assertEqual(raw["case"], {"messages": view["messages"], "post": None})
            seen = PrecedentDetail(**raw)
            target = next(message for message in seen.case.messages if message.is_target)
            self.assertEqual((target.speaker, target.content, target.offset, len(seen.case.messages)), ("被举报人", "你们都是废物", 0, len(view["messages"])))
            self.assertGreater(len(seen.case.messages), 1)
            dumped = json.dumps(raw, default=str, ensure_ascii=False)
            for name in ("offender", "alice", "juror1"):
                self.assertNotIn(name, dumped)
            post_view = PrecedentDetail(**await precedent_service.precedent_detail(db, juror5, first["id"]))
            self.assertEqual((post_view.case.post.content, len(post_view.case.post.image_urls), post_view.case.messages), ("低价卖号，加微信", 2, []))
            # 被停职的前委员不算在任
            await jury_service.review_member(db, juror5.id, action="suspend", actor_id=self.ids["admin"], note="测试")
            self.assertIsNone((await precedent_service.precedent_detail(db, juror5, second["id"]))["case"])
            self.assertEqual(await self._code(precedent_service.precedent_detail(db, bob, 99999)), (404, "precedent_not_found"))

    async def test_only_the_author_edits_and_withdraws(self) -> None:
        async with self.sessions() as db:
            case = await self._settle_case(db, THREE_FOR)
            await self._tier(db, "juror2", "senior")
            juror1, juror2, bob = [await self._user(db, name) for name in ("juror1", "juror2", "bob")]
            created = await self._mark(db, "juror1", case.id, "原标题", "原要旨")
            precedent_id = created["id"]
            # 别人（包括同样投过这个案件的资深委员）不能改、不能撤
            for other in (juror2, bob):
                self.assertEqual(await self._code(precedent_service.update_precedent(db, other, precedent_id, "抢改", None)), (403, "not_precedent_author"))
                self.assertEqual(await self._code(precedent_service.withdraw_precedent(db, other, precedent_id)), (403, "not_precedent_author"))
            # 只改传进来的那一项
            edited = await precedent_service.update_precedent(db, juror1, precedent_id, "新标题", None)
            self.assertEqual((edited["title"], edited["summary"], edited["mine"]), ("新标题", "原要旨", True))
            edited = await precedent_service.update_precedent(db, juror1, precedent_id, None, "新要旨")
            self.assertEqual((edited["title"], edited["summary"]), ("新标题", "新要旨"))
            self.assertEqual(await self._code(precedent_service.update_precedent(db, juror1, precedent_id, "字" * 61, None)), (422, "invalid_precedent"))
            self.assertEqual(await self._code(precedent_service.update_precedent(db, juror1, 99999, "标题", None)), (404, "precedent_not_found"))
            # mine 说的是「我创建的」：之后被降级也还能改自己标的
            await self._tier(db, "juror1", "regular")
            self.assertEqual((await precedent_service.update_precedent(db, juror1, precedent_id, "降级后改的", None))["title"], "降级后改的")

            self.assertEqual(await precedent_service.withdraw_precedent(db, juror1, precedent_id), {"id": precedent_id, "status": "withdrawn"})
            self.assertEqual(await precedent_service.list_precedents(db, bob), ([], 0))
            self.assertEqual(await self._code(precedent_service.precedent_detail(db, bob, precedent_id)), (404, "precedent_not_found"))
            self.assertEqual(await self._code(precedent_service.update_precedent(db, juror1, precedent_id, "撤了再改", None)), (404, "precedent_not_found"))
            # 撤下之后可以重新标（一个案件只有一行，复用它）；这次是另一位资深委员
            again = await self._mark(db, "juror2", case.id, "重新标的", "另一位的要旨")
            self.assertEqual((again["id"], again["title"], again["mine"]), (precedent_id, "重新标的", True))
            self.assertEqual([(item["title"], item["mine"]) for item in (await precedent_service.list_precedents(db, juror1))[0]], [("重新标的", False)])


# ---------------------------------------------------------------- 判例：题库


class PrecedentBankTests(HallTestCase):
    async def _practice_total(self, db, user: User) -> int:
        return (await exam_service.practice_next(db, user))["round"]["total"]

    async def test_precedents_join_the_practice_pool_and_leave_when_withdrawn(self) -> None:
        async with self.sessions() as db:
            carol = await self._eligible_user(db, "carol")
            juror1 = await self._user(db, "juror1")
            case = await self._settle_case(db, THREE_FOR)
            self.assertEqual((case.is_golden, case.golden_source, await self._practice_total(db, carol)), (False, None, 0))
            created = await self._mark(db, "juror1", case.id)
            await db.refresh(case)
            # 进题库，但只在练习那一份里：未核对的题不用于考试和质控
            self.assertEqual((case.is_golden, case.golden_verified, case.golden_source, bank_service.question_pool(case)), (True, False, "precedent", "practice"))
            self.assertEqual(await bank_service.verified_count(db), 0)
            practice = PracticeNext(**await exam_service.practice_next(db, carol))
            self.assertEqual((practice.round.total, practice.question.question_id, practice.question.verified), (1, case.id, False))
            self.assertTrue((await exam_service.practice_answer(db, carol, case.id, "l2"))["correct"])
            # 后台题库里能看出它是因为判例进来的，不会被算成历史回填的旧题
            read = AdminQuestionRead(**(await bank_service.question_reads(db, [case]))[0])
            self.assertEqual((read.source, read.pool, read.verified), ("precedent", "practice", False))
            self.assertEqual((await bank_service.list_questions(db, source="legacy"))[1], 0)
            self.assertEqual((await bank_service.list_questions(db, source="precedent"))[1], 1)
            row = await db.get(JuryPrecedent, created["id"])
            self.assertTrue(row.bank_added)

            # 撤下判例：因为它才进题库的题移出去，以后也不会被自动入库捡回来
            await precedent_service.withdraw_precedent(db, juror1, created["id"])
            await db.refresh(case)
            self.assertEqual((case.is_golden, case.golden_source, row.bank_added), (False, "excluded", False))
            self.assertEqual(await self._practice_total(db, carol), 0)
            self.assertEqual(await self._code(exam_service.practice_answer(db, carol, case.id, "l2")), (404, "question_not_found"))
            # 重新标又回到练习那一份
            await self._mark(db, "juror1", case.id)
            await db.refresh(case)
            self.assertEqual((case.is_golden, case.golden_source, await self._practice_total(db, carol)), (True, "precedent", 1))

    async def test_questions_already_in_the_bank_keep_out_of_the_scored_pool(self) -> None:
        async with self.sessions() as db:
            admin = await self._user(db, "admin")
            # 本来在计分那一份里的题：判例把内容和结论都公开了，改成只用于练习；撤下判例也不会把它移出题库
            scored = await self._question(db)
            self.assertEqual(bank_service.question_pool(scored), "scored")
            read = AdminPrecedentRead(**await precedent_service.admin_create(db, admin, scored.id, "标题", "要旨"))
            await db.refresh(scored)
            self.assertEqual((scored.is_golden, scored.golden_verified, scored.golden_source, bank_service.question_pool(scored)), (True, False, "admin", "practice"))
            self.assertEqual((read.in_bank, read.bank_added, await bank_service.verified_count(db)), (True, False, 0))
            demoted = (await db.execute(select(AdminAuditLog).where(AdminAuditLog.action == "jury.question.update", AdminAuditLog.target_id == scored.id))).scalar_one()
            self.assertEqual((demoted.actor_id, json.loads(demoted.details)["verified"]), (None, {"old": True, "new": False}))
            await precedent_service.admin_withdraw(db, admin, read.id)
            await db.refresh(scored)
            self.assertEqual((scored.is_golden, scored.golden_verified, scored.golden_source), (True, False, "admin"))

            # 管理员移出过题库的：尊重那次决定，不放回去
            removed = await self._question(db, is_golden=False, verified=False, source="removed")
            read = AdminPrecedentRead(**await precedent_service.admin_create(db, admin, removed.id, "标题", "要旨"))
            await db.refresh(removed)
            self.assertEqual((read.status, read.bank_added, removed.is_golden, removed.golden_source), ("active", False, False, "removed"))

            # 内容会在公示栏展示的案件：可以是判例，但题库不收（题库自己的规则）
            public = await self._question(db, is_golden=False, verified=False, source=None, target_type="post", content_publishable=True)
            read = AdminPrecedentRead(**await precedent_service.admin_create(db, admin, public.id, "标题", "要旨"))
            await db.refresh(public)
            self.assertEqual((read.status, read.bank_added, public.is_golden, public.golden_source), ("active", False, False, None))

            # 因判例进题库、之后被管理员核对过的题：算管理员接手了，撤下判例时留着
            kept = await self._question(db, is_golden=False, verified=False, source=None)
            read = AdminPrecedentRead(**await precedent_service.admin_create(db, admin, kept.id, "标题", "要旨"))
            await db.refresh(kept)
            self.assertEqual((read.bank_added, kept.is_golden, kept.golden_source), (True, True, "precedent"))
            await bank_service.update_question(db, kept.id, actor_id=admin.id, changes={"verified": True})
            await precedent_service.admin_withdraw(db, admin, read.id)
            await db.refresh(kept)
            self.assertEqual((kept.is_golden, kept.golden_verified), (True, True))

    async def test_overturned_cases_lose_their_precedent(self) -> None:
        async with self.sessions() as db:
            case_id = await self._jury_case(db, "insult")
            await self._settle(db, case_id)
            juror1, bob = await self._user(db, "juror1"), await self._user(db, "bob")
            created = await self._mark(db, "juror1", case_id)
            case = await db.get(ReportCase, case_id)
            self.assertEqual((case.is_golden, await precedent_service.case_precedent_ids(db, [case_id])), (True, {case_id: created["id"]}))
            await case_service.overturn_case(db, case_id, actor_id=self.ids["admin"], upheld=False, note="结合上下文是玩笑")
            row = await db.get(JuryPrecedent, created["id"])
            # 不管改判那边有没有接钩子（on_case_overturned），读取时都按案件状态过滤掉了
            self.assertEqual(await precedent_service.case_precedent_ids(db, [case_id]), {})
            # 没接钩子的话，读列表 / 详情时顺手撤下，连同因它进题库的那道题
            self.assertEqual(await precedent_service.list_precedents(db, bob), ([], 0))
            await db.refresh(row)
            await db.refresh(case)
            self.assertEqual((row.status, row.withdrawn_by_id, row.withdraw_note, row.bank_added), ("withdrawn", None, "案件被改判，判例自动撤下", False))
            self.assertEqual((case.is_golden, case.golden_source), (False, "excluded"))
            self.assertEqual(await self._code(precedent_service.precedent_detail(db, bob, created["id"])), (404, "precedent_not_found"))
            self.assertEqual(await self._code(precedent_service.update_precedent(db, juror1, created["id"], "改", None)), (404, "precedent_not_found"))
            self.assertEqual(await self._code(self._mark(db, "juror1", case_id)), (422, "case_not_eligible"))
            self.assertEqual((await precedent_service.admin_list(db))[0][0]["overturned"], True)

            # 钩子本身：改判的事务里调用，不自己提交
            other = await self._settle_case(db, THREE_FOR)
            made = await self._mark(db, "juror1", other.id)
            other.overturned_at = datetime.now(UTC)
            self.assertTrue(await precedent_service.on_case_overturned(db, other))
            await db.commit()
            hooked = await db.get(JuryPrecedent, made["id"])
            await db.refresh(hooked)
            await db.refresh(other)
            self.assertEqual((hooked.status, hooked.withdrawn_by_id, other.is_golden), ("withdrawn", None, False))
            self.assertFalse(await precedent_service.on_case_overturned(db, other))
            self.assertEqual(await precedent_service.sync_overturned(db), 0)


# ---------------------------------------------------------------- 判例：和案件互相关联的字段


class PrecedentLinkTests(HallTestCase):
    async def test_case_detail_and_public_board_point_at_the_precedent(self) -> None:
        async with self.sessions() as db:
            case_id = await self._jury_case(db, "insult")
            await self._settle(db, case_id)
            other_id = await self._jury_case(db, "insult2")
            await self._settle(db, other_id)
            await self._age(db, case_id, 100)
            await self._age(db, other_id, 99)
            await self._tier(db, "juror6", "senior")
            juror1, juror2, juror6 = [await self._user(db, name) for name in ("juror1", "juror2", "juror6")]

            async def fields(user: User, target: int) -> tuple[dict | None, bool]:
                detail = JuryCaseDetail(**await jury_service.case_detail(db, user.id, await self._rid(db, user, target)))
                return (detail.precedent.model_dump() if detail.precedent else None, detail.can_mark_precedent)

            async def board() -> dict[int, int | None]:
                return {PublicCase(**item).case_id: item["precedent_id"] for item in (await overview_service.public_cases(db))[0]}

            # 还不是判例：资深委员可以标，正式委员不行
            self.assertEqual(await fields(juror1, case_id), (None, True))
            self.assertEqual(await fields(juror2, case_id), (None, False))
            self.assertEqual(await board(), {case_id: None, other_id: None})
            created = await self._mark(db, "juror1", case_id, "判例标题")
            self.assertEqual(await fields(juror1, case_id), ({"id": created["id"], "title": "判例标题", "mine": True}, False))
            self.assertEqual(await fields(juror2, case_id), ({"id": created["id"], "title": "判例标题", "mine": False}, False))
            self.assertEqual(await board(), {case_id: created["id"], other_id: None})
            self.assertEqual(PublicCaseDetail(**await overview_service.public_case(db, case_id)).precedent_id, created["id"])
            self.assertIsNone(PublicCaseDetail(**await overview_service.public_case(db, other_id)).precedent_id)

            # 把这两个案件当质控题答过的人：两项恒为空，否则等于把公开编号还了回去
            now = datetime.now(UTC)
            for target in (case_id, other_id):
                db.add(JuryVote(case_id=target, juror_id=juror6.id, vote="l2", assigned_at=now, voted_at=now, agreed=True, is_quality_check=True))
            await db.commit()
            self.assertEqual(await fields(juror6, case_id), (None, False))
            self.assertEqual(await fields(juror6, other_id), (None, False))

            # can_mark_precedent 的其余条件：受处罚、未定案、类别不允许
            self.assertEqual(await fields(juror1, other_id), (None, True))
            await self._mute(db, "juror1")
            self.assertEqual(await fields(juror1, other_id), (None, False))
            await self._mute(db, "juror1", None)
            open_case = await self._open_case(db)
            await self._cast(db, "juror1", open_case.id, "l2")
            self.assertEqual(await fields(juror1, open_case.id), (None, False))
            other = await db.get(ReportCase, other_id)
            other.verdict_category = "privacy"
            await db.commit()
            self.assertEqual(await fields(juror1, other_id), (None, False))
            other.verdict_category = "harassment"
            await db.commit()

            # 撤下之后又可以标；被改判的两项都空
            await precedent_service.withdraw_precedent(db, juror1, created["id"])
            self.assertEqual(await fields(juror1, case_id), (None, True))
            self.assertEqual(await board(), {case_id: None, other_id: None})
            again = await self._mark(db, "juror1", case_id)
            self.assertEqual((await board())[case_id], again["id"])
            await case_service.overturn_case(db, case_id, actor_id=self.ids["admin"], upheld=False, note="结合上下文是玩笑")
            self.assertEqual(await fields(juror1, case_id), (None, False))
            self.assertEqual((await board())[case_id], None)


# ---------------------------------------------------------------- 判例：后台


class PrecedentAdminTests(HallTestCase):
    async def test_admin_manages_any_precedent(self) -> None:
        async with self.sessions() as db:
            admin, juror1, bob = [await self._user(db, name) for name in ("admin", "juror1", "bob")]
            # 管理员结案、没有委员投过票的案件也可以标，用的是公开编号
            plain = await self._question(db, is_golden=False, verified=False, source=None)
            own = AdminPrecedentRead(**await precedent_service.admin_create(db, admin, plain.id, "管理员标的", "要旨"))
            self.assertEqual(
                (own.by, own.mine, own.status, own.case_id, own.created_by.username, own.bank_added, own.overturned),
                ("admin", True, "active", plain.id, "admin", True, False),
            )
            self.assertEqual(await self._code(precedent_service.admin_create(db, admin, plain.id, "再标一次", "要旨")), (409, "precedent_exists"))
            self.assertEqual(await self._code(precedent_service.admin_create(db, admin, plain.id + 1000, "标题", "要旨")), (404, "case_not_found"))
            self.assertEqual(await self._code(precedent_service.admin_create(db, admin, plain.id, "", "要旨")), (422, "invalid_precedent"))
            seen = Precedent(**(await precedent_service.list_precedents(db, bob))[0][0])
            self.assertEqual((seen.by, seen.mine, seen.title), ("admin", False, "管理员标的"))

            # 资深委员标的：管理员可以改，创建人不变
            case = await self._settle_case(db, THREE_FOR)
            theirs = await self._mark(db, "juror1", case.id, "原标题", "原要旨")
            edited = AdminPrecedentRead(**await precedent_service.admin_update(db, admin, theirs["id"], "改过的标题", None))
            self.assertEqual((edited.title, edited.summary, edited.by, edited.created_by.username, edited.mine), ("改过的标题", "原要旨", "senior", "juror1", False))
            self.assertEqual(await self._code(precedent_service.admin_update(db, admin, theirs["id"], None, "字" * 1001)), (422, "invalid_precedent"))
            self.assertTrue((await precedent_service.list_precedents(db, juror1))[0][0]["mine"])
            # 也可以撤；重复撤是幂等的
            withdrawn = AdminPrecedentRead(**await precedent_service.admin_withdraw(db, admin, theirs["id"], " 表述不准确 "))
            self.assertEqual((withdrawn.status, withdrawn.withdrawn_by_id, withdrawn.withdraw_note, withdrawn.in_bank), ("withdrawn", admin.id, "表述不准确", False))
            self.assertEqual((await precedent_service.admin_withdraw(db, admin, theirs["id"]))["withdraw_note"], "表述不准确")
            self.assertEqual(await self._code(precedent_service.admin_update(db, admin, theirs["id"], "撤了再改", None)), (404, "precedent_not_found"))
            self.assertEqual(await self._code(precedent_service.admin_withdraw(db, admin, 99999)), (404, "precedent_not_found"))
            self.assertEqual(await self._code(precedent_service.withdraw_precedent(db, juror1, theirs["id"])), (404, "precedent_not_found"))

            # 后台列表含已撤下的
            items, total = await precedent_service.admin_list(db, viewer_id=admin.id)
            self.assertEqual((total, [(item["id"], item["status"]) for item in items]), (2, [(theirs["id"], "withdrawn"), (own.id, "active")]))
            self.assertEqual([item["id"] for item in (await precedent_service.admin_list(db, status_filter="active"))[0]], [own.id])
            self.assertEqual((await precedent_service.admin_list(db, status_filter="withdrawn", limit=1, offset=1)), ([], 1))
            self.assertEqual((await precedent_service.list_precedents(db, juror1))[1], 1)
            # 管理员的操作都有审计日志；资深委员自己标的不算管理操作
            self.assertEqual(await self._audit_actions(db, "jury.precedent."), ["jury.precedent.create", "jury.precedent.update", "jury.precedent.withdraw"])


# ---------------------------------------------------------------- 议事厅：发帖


class HallPostingTests(HallTestCase):
    async def test_who_can_post_which_kind(self) -> None:
        async with self.sessions() as db:
            juror1, juror2, juror3, bob, admin = [await self._user(db, name) for name in ("juror1", "juror2", "juror3", "bob", "admin")]

            async def refused(user: User, kind: str = "discussion", title: str = "标题", content: str = "正文") -> tuple[int, str | None]:
                return await self._code(hall_service.create_post(db, user, kind, title, content))

            # 见习不能发帖；非委员、不是委员的管理员都不能走委员的发帖接口
            self.assertEqual(await refused(juror3), (403, "tier_required"))
            self.assertEqual(await refused(juror3, "proposal"), (403, "tier_required"))
            self.assertEqual(await refused(bob), (403, "not_juror"))
            self.assertEqual(await refused(admin), (403, "not_juror"))
            # 公告只能在后台发
            self.assertEqual(await refused(juror1, "announcement"), (422, "invalid_post"))
            self.assertEqual(await refused(juror1, "bogus"), (422, "invalid_post"))
            for title, content in (("", "正文"), ("字" * 61, "正文"), ("标题", "   "), ("标题", "字" * 5001)):
                self.assertEqual(await refused(juror2, "discussion", title, content), (422, "invalid_post"))
            # 受处罚的委员不能发
            await self._mute(db, "juror2")
            self.assertEqual(await refused(juror2), (403, "juror_restricted"))
            await self._mute(db, "juror2", None)
            self.assertEqual((await db.execute(select(func.count()).select_from(JuryHallPost))).scalar_one(), 0)

            with self._events() as events:
                discussion = HallPost(**await hall_service.create_post(db, juror2, "discussion", f"  {'字' * 60} ", "字" * 5000))
                proposal = HallPost(**await self._hall_post(db, "juror1", "proposal", "建议把刷屏的默认等级调到一般", "理由如下。"))
            self.assertEqual(
                (discussion.kind, len(discussion.title), len(discussion.content), discussion.status, discussion.state, discussion.vote, discussion.pinned),
                ("discussion", 60, 5000, "open", "visible", None, False),
            )
            self.assertEqual((discussion.author.username, discussion.author_tier, discussion.mine, discussion.comment_count, discussion.admin_note), ("juror2", "regular", True, 0, None))
            self.assertEqual((proposal.kind, proposal.author.username, proposal.author_tier, proposal.status), ("proposal", "juror1", "senior", "open"))
            # 提案带表决块：9 位在任委员 × 30% 向上取整 = 3 人，通过线 2/3，表决 7 天
            vote = proposal.vote
            self.assertEqual((vote.yes, vote.no, vote.my_vote, vote.can_vote, vote.quorum, vote.pass_num, vote.pass_den), (0, 0, None, True, 3, 2, 3))
            self.assertAlmostEqual((vote.ends_at - proposal.created_at).total_seconds(), 7 * 86400, delta=5)
            # 实时事件发给全部在任委员
            jurors = {self.ids[name] for name in JURORS}
            self.assertEqual(
                [(name, users, data) for name, users, data in events if name == "jury.hall.changed"],
                [
                    ("jury.hall.changed", jurors, {"post_id": discussion.id, "kind": "discussion", "action": "created"}),
                    ("jury.hall.changed", jurors, {"post_id": proposal.id, "kind": "proposal", "action": "created"}),
                ],
            )

            # 管理员不是委员也能在后台发公告；公告的 author_tier 为 null
            read = AdminHallPostRead(**await hall_service.admin_create_announcement(db, admin, "议事厅开放了", "欢迎大家来讨论规则。", pinned=True))
            self.assertEqual((read.kind, read.author.username, read.author_tier, read.pinned, read.vote, read.status), ("announcement", "admin", None, True, None, "open"))
            self.assertEqual(await self._code(hall_service.admin_create_announcement(db, admin, "", "正文")), (422, "invalid_post"))
            announcement = HallPost(**await hall_service.get_post(db, juror3, read.id))
            self.assertEqual((announcement.author.username, announcement.author_tier, announcement.vote, announcement.can_comment, announcement.mine), ("admin", None, None, True, False))
            # 全站可读：非委员能看，但不能评论、不能表决
            for viewer in (bob, admin):
                seen = HallPost(**await hall_service.get_post(db, viewer, proposal.id))
                self.assertEqual((seen.title, seen.can_comment, seen.vote.can_vote, seen.mine, seen.can_edit, seen.can_delete), (proposal.title, False, False, False, False, False))
            self.assertEqual(await self._code(hall_service.get_post(db, bob, 99999)), (404, "post_not_found"))
            self.assertEqual(await self._audit_actions(db, "jury.hall."), ["jury.hall.post.announce"])

    async def test_authors_edit_and_delete_their_own_posts(self) -> None:
        async with self.sessions() as db:
            juror1, juror2, juror3, admin = [await self._user(db, name) for name in ("juror1", "juror2", "juror3", "admin")]
            discussion = await self._hall_post(db, "juror2")
            post_id = discussion["id"]

            async def flags(user: User, target: int = post_id) -> tuple[bool, bool, bool]:
                view = await hall_service.get_post(db, user, target)
                return view["mine"], view["can_edit"], view["can_delete"]

            self.assertEqual(await flags(juror2), (True, True, True))
            self.assertEqual(await flags(juror1), (False, False, False))
            self.assertEqual(await self._code(hall_service.update_post(db, juror1, post_id, "抢改", None)), (403, "not_post_author"))
            self.assertEqual(await self._code(hall_service.delete_post(db, juror1, post_id)), (403, "not_post_author"))
            # 只改传进来的那一项
            with self._events() as events:
                edited = await hall_service.update_post(db, juror2, post_id, "改过的标题", None)
            self.assertEqual((edited["title"], edited["content"]), ("改过的标题", discussion["content"]))
            self.assertGreater(edited["updated_at"], discussion["updated_at"])
            self.assertEqual([data["action"] for name, _users, data in events if name == "jury.hall.changed"], ["updated"])
            self.assertEqual(await self._code(hall_service.update_post(db, juror2, post_id, None, "字" * 5001)), (422, "invalid_post"))
            self.assertEqual(await self._code(hall_service.update_post(db, juror2, 99999, "标题", None)), (404, "post_not_found"))
            # 受处罚期间不能改（改等于重新发言），但可以删自己的
            await self._mute(db, "juror2")
            self.assertEqual(await flags(juror2), (True, False, True))
            self.assertEqual(await self._code(hall_service.update_post(db, juror2, post_id, "禁言期间改", None)), (403, "juror_restricted"))
            with self._events() as events:
                self.assertEqual(await hall_service.delete_post(db, juror2, post_id), {"id": post_id, "state": "deleted"})
            self.assertEqual([data["action"] for name, _users, data in events if name == "jury.hall.changed"], ["closed"])
            self.assertEqual(await self._code(hall_service.get_post(db, juror2, post_id)), (404, "post_not_found"))
            self.assertEqual(await self._code(hall_service.delete_post(db, juror2, post_id)), (404, "post_not_found"))
            self.assertEqual((await hall_service.list_posts(db, juror1))[1], 0)

            # 提案：有人表决之前可以改，之后不行；还开着的时候可以撤回（删除）
            proposal_id = (await self._hall_post(db, "juror1", "proposal"))["id"]
            self.assertEqual((await hall_service.update_post(db, juror1, proposal_id, None, "补充了理由"))["content"], "补充了理由")
            await hall_service.cast_vote(db, juror3, proposal_id, "no")
            self.assertEqual(await flags(juror1, proposal_id), (True, False, True))
            self.assertEqual(await self._code(hall_service.update_post(db, juror1, proposal_id, "表决后改", None)), (409, "post_locked"))
            self.assertEqual((await hall_service.delete_post(db, juror1, proposal_id))["state"], "deleted")
            # 出了结果的提案是表决的记录，作者不能删也不能改
            settled_id = (await self._hall_post(db, "juror1", "proposal"))["id"]
            await self._expire(db, settled_id)
            self.assertEqual(await flags(juror1, settled_id), (True, False, False))
            self.assertEqual((await hall_service.get_post(db, juror1, settled_id))["status"], "rejected")
            self.assertEqual(await self._code(hall_service.delete_post(db, juror1, settled_id)), (409, "post_locked"))
            self.assertEqual(await self._code(hall_service.update_post(db, juror1, settled_id, "结束后改", None)), (409, "post_locked"))

            # 公告：发它的管理员可以改和删，别的人不行
            notice_id = (await hall_service.admin_create_announcement(db, admin, "公告", "内容"))["id"]
            self.assertEqual(await flags(admin, notice_id), (True, True, True))
            self.assertEqual(await flags(juror1, notice_id), (False, False, False))
            self.assertEqual((await hall_service.update_post(db, admin, notice_id, "改过的公告", None))["title"], "改过的公告")
            self.assertEqual(await self._code(hall_service.update_post(db, juror1, notice_id, "抢改", None)), (403, "not_post_author"))
            self.assertEqual((await hall_service.delete_post(db, admin, notice_id))["state"], "deleted")


# ---------------------------------------------------------------- 议事厅：表决


class HallVotingTests(HallTestCase):
    def test_pass_line_is_an_integer_comparison(self) -> None:
        # 恰好 2/3 通过；差一票不通过；比例够但人数不足不通过
        self.assertTrue(hall_service.passes(2, 1, 3, 2, 3))
        self.assertTrue(hall_service.passes(4, 2, 3, 2, 3))
        self.assertTrue(hall_service.passes(20, 10, 3, 2, 3))
        self.assertFalse(hall_service.passes(3, 2, 3, 2, 3))
        self.assertFalse(hall_service.passes(19, 10, 3, 2, 3))
        self.assertFalse(hall_service.passes(2, 0, 3, 2, 3))
        self.assertFalse(hall_service.passes(0, 3, 3, 2, 3))
        # 最少表决人数：在任委员数 × 30% 向上取整，至少 3 人。10 × 0.3 按浮点算是 3.0000000000000004，不能被取成 4
        self.assertEqual([hall_service.quorum_for(count) for count in (0, 1, 9, 10, 11, 20, 21, 100)], [3, 3, 3, 3, 4, 6, 7, 30])

    async def test_serving_jurors_vote_and_may_change_until_the_deadline(self) -> None:
        async with self.sessions() as db:
            juror1, juror2, juror3, juror4, bob = [await self._user(db, name) for name in ("juror1", "juror2", "juror3", "juror4", "bob")]
            post_id = (await self._hall_post(db, "juror1", "proposal"))["id"]
            discussion_id = (await self._hall_post(db, "juror2"))["id"]
            # 所有在任委员都能表决，包括见习和提案人自己
            self.assertEqual((await hall_service.cast_vote(db, juror1, post_id, "yes"))["vote"]["my_vote"], "yes")
            await hall_service.cast_vote(db, juror2, post_id, "yes")
            with self._events() as events:
                voted = HallPost(**await hall_service.cast_vote(db, juror3, post_id, "yes"))
            self.assertEqual((voted.vote.yes, voted.vote.no, voted.vote.my_vote, voted.vote.can_vote, voted.status), (3, 0, "yes", True, "open"))
            self.assertEqual([data for name, _users, data in events if name == "jury.hall.changed"], [{"post_id": post_id, "kind": "proposal", "action": "updated"}])
            # 结束前可以改票：还是一人一票
            changed = await hall_service.cast_vote(db, juror3, post_id, "no")
            self.assertEqual((changed["vote"]["yes"], changed["vote"]["no"], changed["vote"]["my_vote"]), (2, 1, "no"))
            self.assertEqual((await hall_service.cast_vote(db, juror3, post_id, "no"))["vote"]["no"], 1)
            self.assertEqual((await db.execute(select(func.count()).select_from(JuryHallVote))).scalar_one(), 3)
            self.assertEqual((await hall_service.get_post(db, bob, post_id))["vote"]["my_vote"], None)

            self.assertEqual(await self._code(hall_service.cast_vote(db, bob, post_id, "yes")), (403, "not_juror"))
            await self._mute(db, "juror4")
            self.assertEqual(await self._code(hall_service.cast_vote(db, juror4, post_id, "yes")), (403, "juror_restricted"))
            self.assertFalse((await hall_service.get_post(db, juror4, post_id))["vote"]["can_vote"])
            await self._mute(db, "juror4", None)
            self.assertEqual(await self._code(hall_service.cast_vote(db, juror4, post_id, "maybe")), (422, "invalid_vote"))
            self.assertEqual(await self._code(hall_service.cast_vote(db, juror4, discussion_id, "yes")), (409, "vote_closed"))
            self.assertEqual(await self._code(hall_service.cast_vote(db, juror4, 99999, "yes")), (404, "post_not_found"))

            # 截止之后：409，并且这个提案当场按已有的票出结果（2 赞成 1 反对，恰好 2/3，人数刚好够）
            await self._expire(db, post_id)
            with self._events() as events:
                self.assertEqual(await self._code(hall_service.cast_vote(db, juror4, post_id, "no")), (409, "vote_closed"))
            self.assertEqual([data["action"] for name, _users, data in events if name == "jury.hall.changed"], ["closed"])
            closed = HallPost(**await hall_service.get_post(db, juror3, post_id))
            self.assertEqual((closed.status, closed.vote.yes, closed.vote.no, closed.vote.my_vote, closed.vote.can_vote), ("passed", 2, 1, "no", False))
            self.assertEqual(await self._code(hall_service.cast_vote(db, juror3, post_id, "yes")), (409, "vote_closed"))

    async def test_due_proposals_are_settled_by_the_sweep_or_lazily_on_read(self) -> None:
        async with self.sessions() as db:
            juror1, bob = await self._user(db, "juror1"), await self._user(db, "bob")
            # 赞成、反对 -> 结果
            expected = {(4, 2): "passed", (3, 2): "rejected", (5, 3): "rejected", (2, 0): "rejected", (3, 0): "passed", (0, 0): "rejected"}
            posts: dict[tuple[int, int], int] = {}
            with patch.object(hall_service, "POSTS_PER_DAY", 100):
                for (yes, no) in expected:
                    post_id = (await self._hall_post(db, "juror1", "proposal", f"提案 {yes}:{no}"))["id"]
                    await self._ballots(db, post_id, yes, no)
                    posts[(yes, no)] = post_id
                still_open = (await self._hall_post(db, "juror1", "proposal", "还没到期"))["id"]
                discussion = (await self._hall_post(db, "juror1", "discussion"))["id"]
                lazy_list = (await self._hall_post(db, "juror1", "proposal", "读列表时结算"))["id"]
                lazy_detail = (await self._hall_post(db, "juror1", "proposal", "读详情时结算"))["id"]
            await self._ballots(db, still_open, 1, 0)
            await self._ballots(db, lazy_list, 3, 1)
            await self._ballots(db, lazy_detail, 1, 3)
            self.assertEqual(await hall_service.settle_due_proposals(db), 0)
            for post_id in posts.values():
                await self._expire(db, post_id)
            # 定时循环调的就是这个函数
            with self._events() as events:
                self.assertEqual(await hall_service.settle_due_proposals(db), len(expected))
            self.assertEqual(sorted(data["post_id"] for name, _users, data in events if name == "jury.hall.changed" and data["action"] == "closed"), sorted(posts.values()))
            for tally, status in expected.items():
                row = await self._hall_row(db, posts[tally])
                self.assertEqual((row.status, row.settled_at is not None), (status, True), tally)
            self.assertEqual(await hall_service.settle_due_proposals(db), 0)
            self.assertEqual([(await self._hall_row(db, post_id)).status for post_id in (still_open, discussion)], ["open", "open"])

            # 没接定时循环也没关系：读列表 / 读详情时到期的提案当场结算
            await self._expire(db, lazy_list)
            listed = {item["id"]: item["status"] for item in (await hall_service.list_posts(db, bob, kind="proposal", limit=50))[0]}
            self.assertEqual((listed[lazy_list], listed[still_open], listed[posts[(4, 2)]]), ("passed", "open", "passed"))
            await self._expire(db, lazy_detail)
            self.assertEqual((await hall_service.get_post(db, bob, lazy_detail))["status"], "rejected")
            self.assertEqual((await hall_service.list_posts(db, bob, status_filter="passed", limit=50))[1], 3)
            # 已删除的提案不结算
            await hall_service.delete_post(db, juror1, still_open)
            await self._expire(db, still_open)
            self.assertEqual(await hall_service.settle_due_proposals(db), 0)

    async def test_quorum_and_pass_line_are_fixed_when_the_proposal_is_posted(self) -> None:
        async with self.sessions() as db:
            first = (await self._hall_post(db, "juror1", "proposal", "先发的"))["id"]
            # 之后在任委员少了、配置也改了：先发的提案不受影响，后发的按新的算
            for name in JURORS[3:]:
                await jury_service.review_member(db, self.ids[name], action="retire", actor_id=self.ids["admin"], note=None)
            with patch.object(hall_service, "QUORUM_MIN", 1), patch.object(hall_service, "QUORUM_RATIO", 0.5), patch.object(hall_service, "PASS_NUM", 1), patch.object(hall_service, "PASS_DEN", 2):
                second = (await self._hall_post(db, "juror1", "proposal", "后发的"))["id"]
                rows = [await self._hall_row(db, post_id) for post_id in (first, second)]
                self.assertEqual([(row.quorum, row.pass_num, row.pass_den) for row in rows], [(3, 2, 3), (2, 1, 2)])
                # 各投 1 赞成 1 反对：后发的（过半、2 人）通过，先发的（2/3、3 人）不通过
                for post_id in (first, second):
                    await self._ballots(db, post_id, 1, 1)
                    await self._expire(db, post_id)
                self.assertEqual(await hall_service.settle_due_proposals(db), 2)
            views = [await hall_service.get_post(db, await self._user(db, "bob"), post_id) for post_id in (first, second)]
            self.assertEqual([(view["status"], view["vote"]["quorum"], view["vote"]["pass_num"], view["vote"]["pass_den"]) for view in views], [("rejected", 3, 2, 3), ("passed", 2, 1, 2)])


# ---------------------------------------------------------------- 议事厅：管理员


class HallAdminTests(HallTestCase):
    async def test_admin_adopts_declines_closes_pins_and_deletes(self) -> None:
        async with self.sessions() as db:
            admin, juror1, juror3, bob = [await self._user(db, name) for name in ("admin", "juror1", "juror3", "bob")]
            passed = (await self._hall_post(db, "juror1", "proposal", "会通过的提案"))["id"]
            await self._ballots(db, passed, 3, 0)
            pending = (await self._hall_post(db, "juror1", "proposal", "还在表决的提案"))["id"]
            discussion = (await self._hall_post(db, "juror2", "discussion", "一个讨论"))["id"]

            async def decide(post_id: int, action: str, note: str | None = None) -> AdminHallPostRead:
                return AdminHallPostRead(**await hall_service.admin_decide(db, admin, post_id, action, note))

            async def refused(post_id: int, action: str) -> tuple[int, str | None]:
                return await self._code(hall_service.admin_decide(db, admin, post_id, action, None))

            # 只有表决通过的提案能标采纳 / 不采纳
            for post_id in (pending, discussion):
                self.assertEqual(await refused(post_id, "adopt"), (409, "invalid_post_status"))
                self.assertEqual(await refused(post_id, "decline"), (409, "invalid_post_status"))
            # 到期没结算的提案：管理员操作之前先结算，所以可以直接采纳
            await self._expire(db, passed)
            with self._events() as events:
                adopted = await decide(passed, "adopt", " 下个版本调整 ")
            self.assertEqual((adopted.status, adopted.admin_note, adopted.decided_by_id, adopted.vote["yes"], adopted.settled_at is not None), ("adopted", "下个版本调整", admin.id, 3, True))
            self.assertEqual([data["action"] for name, _users, data in events if name == "jury.hall.changed"], ["closed", "updated"])
            view = HallPost(**await hall_service.get_post(db, bob, passed))
            self.assertEqual((view.status, view.admin_note, view.vote.can_vote), ("adopted", "下个版本调整", False))
            # 结论可以改
            declined = await decide(passed, "decline", "和现有规则冲突")
            self.assertEqual((declined.status, declined.admin_note), ("declined", "和现有规则冲突"))
            # 没通过的提案不能采纳
            rejected = (await self._hall_post(db, "juror1", "proposal", "没人投的提案"))["id"]
            await self._expire(db, rejected)
            self.assertEqual(await refused(rejected, "adopt"), (409, "invalid_post_status"))
            self.assertEqual((await self._hall_row(db, rejected)).status, "rejected")

            # 关闭：之后不能评论、不能表决，帖子还在
            await self._comment(db, "juror3", discussion, "关之前的评论")
            closed = await decide(discussion, "close", "跑题了")
            self.assertEqual((closed.status, closed.admin_note, closed.state, closed.comment_count), ("closed", "跑题了", "visible", 1))
            self.assertEqual(await self._code(self._comment(db, "juror3", discussion, "关之后的评论")), (409, "post_closed"))
            view = await hall_service.get_post(db, juror3, discussion)
            self.assertEqual((view["status"], view["can_comment"], view["admin_note"]), ("closed", False, "跑题了"))
            self.assertEqual(await refused(discussion, "close"), (409, "invalid_post_status"))
            closed_proposal = await decide(pending, "close")
            self.assertEqual(closed_proposal.status, "closed")
            self.assertEqual(await self._code(hall_service.cast_vote(db, juror3, pending, "yes")), (409, "vote_closed"))
            self.assertEqual(await refused(pending, "adopt"), (409, "invalid_post_status"))

            # 置顶：列表里始终在最前
            self.assertEqual([item["id"] for item in (await hall_service.list_posts(db, bob, sort="new"))[0]], [rejected, discussion, pending, passed])
            pinned = await decide(passed, "pin")
            self.assertTrue(pinned.pinned)
            for sort in ("new", "active"):
                self.assertEqual((await hall_service.list_posts(db, bob, sort=sort))[0][0]["id"], passed, sort)
            self.assertFalse((await decide(passed, "unpin")).pinned)
            self.assertEqual((await hall_service.list_posts(db, bob, sort="new"))[0][0]["id"], rejected)

            # 删除：用户侧看不到，后台还在；重复删是幂等的
            with self._events() as events:
                deleted = await decide(discussion, "delete", "违反守则")
            self.assertEqual((deleted.state, deleted.deleted_by_id, deleted.moderation_note, deleted.admin_note), ("deleted", admin.id, "违反守则", "跑题了"))
            self.assertEqual([data["action"] for name, _users, data in events if name == "jury.hall.changed"], ["closed"])
            self.assertEqual((await decide(discussion, "delete")).state, "deleted")
            self.assertEqual(await self._code(hall_service.get_post(db, juror1, discussion)), (404, "post_not_found"))
            self.assertEqual(await refused(discussion, "pin"), (404, "post_not_found"))
            self.assertEqual(await refused(99999, "pin"), (404, "post_not_found"))
            self.assertEqual(await self._code(hall_service.admin_decide(db, admin, passed, "bogus", None)), (422, "invalid_action"))

            # 后台的列表和详情：含已删除的，带评论原文
            items, total = await hall_service.admin_list_posts(db)
            self.assertEqual((total, [item["id"] for item in items]), (4, [rejected, discussion, pending, passed]))
            self.assertEqual([item["id"] for item in (await hall_service.admin_list_posts(db, state="deleted"))[0]], [discussion])
            self.assertEqual((await hall_service.admin_list_posts(db, kind="proposal", status_filter="declined"))[1], 1)
            detail = AdminHallPostDetail(**await hall_service.admin_post_detail(db, discussion))
            self.assertEqual((detail.state, detail.content, [(comment.author.username, comment.content) for comment in detail.comments]), ("deleted", "想听听大家怎么判断刷屏。", [("juror3", "关之前的评论")]))
            self.assertEqual(await self._code(hall_service.admin_post_detail(db, 99999)), (404, "post_not_found"))
            self.assertEqual(
                await self._audit_actions(db, "jury.hall.post."),
                [f"jury.hall.post.{action}" for action in ("adopt", "decline", "close", "close", "pin", "unpin", "delete")],
            )


# ---------------------------------------------------------------- 议事厅：评论


class HallCommentTests(HallTestCase):
    async def test_comments_are_flat_with_one_level_of_replies(self) -> None:
        async with self.sessions() as db:
            juror1, juror3, juror4, bob = [await self._user(db, name) for name in ("juror1", "juror3", "juror4", "bob")]
            post_id = (await self._hall_post(db, "juror2"))["id"]
            other_post = (await self._hall_post(db, "juror1", "discussion", "另一个帖子"))["id"]

            async def listing(user: User = bob, **page) -> tuple[list[tuple], int]:
                items, total = await hall_service.list_comments(db, user, post_id, **page)
                rows = [HallComment(**item) for item in items]
                return [(row.id, row.parent_id, row.state, row.content, row.reply_to.username if row.reply_to else None) for row in rows], total

            # 所有在任委员都能评论，包括见习
            with self._events() as events:
                root = HallComment(**await self._comment(db, "juror3", post_id, "  我觉得要看频率  "))
            self.assertEqual((root.parent_id, root.content, root.state, root.mine, root.reported, root.reply_to), (None, "我觉得要看频率", "visible", True, False, None))
            self.assertEqual((root.author.username, root.author_tier), ("juror3", "trainee"))
            self.assertEqual([data for name, _users, data in events if name == "jury.hall.changed"], [{"post_id": post_id, "kind": "discussion", "action": "updated"}])
            # 回复根评论：挂在它下面，reply_to 为空
            reply = HallComment(**await self._comment(db, "juror1", post_id, "同意", root.id))
            self.assertEqual((reply.parent_id, reply.reply_to, reply.author_tier), (root.id, None, "senior"))
            # 回复一条回复：挂到同一条根评论下，并记下回复的是谁
            nested = HallComment(**await self._comment(db, "juror4", post_id, "补充一个例子", reply.id))
            self.assertEqual((nested.parent_id, nested.reply_to.username), (root.id, "juror1"))
            second_root = HallComment(**await self._comment(db, "juror4", post_id, "另起一条"))
            deeper = HallComment(**await self._comment(db, "juror3", post_id, "再回一层", nested.id))
            self.assertEqual((deeper.parent_id, deeper.reply_to.username), (root.id, "juror4"))

            # 平铺、时间正序，根评论一定在它的回复前面；任何登录用户都能读
            rows, total = await listing()
            self.assertEqual(total, 5)
            self.assertEqual(
                rows,
                [
                    (root.id, None, "visible", "我觉得要看频率", None),
                    (reply.id, root.id, "visible", "同意", None),
                    (nested.id, root.id, "visible", "补充一个例子", "juror1"),
                    (second_root.id, None, "visible", "另起一条", None),
                    (deeper.id, root.id, "visible", "再回一层", "juror4"),
                ],
            )
            self.assertEqual((await listing(limit=2, offset=1))[0], rows[1:3])
            mine = {item["id"]: item["mine"] for item in (await hall_service.list_comments(db, juror4, post_id))[0]}
            self.assertEqual(mine, {root.id: False, reply.id: False, nested.id: True, second_root.id: True, deeper.id: False})
            self.assertEqual((await hall_service.get_post(db, bob, post_id))["comment_count"], 5)

            # 谁不能评论
            self.assertEqual(await self._code(self._comment(db, "bob", post_id, "路过")), (403, "not_juror"))
            self.assertEqual(await self._code(self._comment(db, "admin", post_id, "管理员路过")), (403, "not_juror"))
            await self._mute(db, "juror4")
            self.assertEqual(await self._code(self._comment(db, "juror4", post_id, "禁言期间")), (403, "juror_restricted"))
            self.assertFalse((await hall_service.get_post(db, juror4, post_id))["can_comment"])
            for bad in ("", "   ", "字" * 1001):
                self.assertEqual(await self._code(self._comment(db, "juror3", post_id, bad)), (422, "invalid_comment"))
            self.assertEqual(len((await self._comment(db, "juror3", other_post, "字" * 1000))["content"]), 1000)
            # 回复的目标必须是同一个帖子下还在的评论
            self.assertEqual(await self._code(self._comment(db, "juror3", other_post, "串帖了", root.id)), (404, "comment_not_found"))
            self.assertEqual(await self._code(self._comment(db, "juror3", post_id, "回复不存在的", 99999)), (404, "comment_not_found"))
            self.assertEqual(await self._code(self._comment(db, "juror3", 99999, "帖子不存在")), (404, "post_not_found"))

            # 删除：只能删自己的（受处罚期间也可以）；删掉的回复不再出现
            self.assertEqual(await self._code(hall_service.delete_comment(db, juror1, nested.id)), (403, "not_comment_author"))
            self.assertEqual(await hall_service.delete_comment(db, juror4, nested.id), {"id": nested.id, "state": "deleted"})
            self.assertEqual(await hall_service.delete_comment(db, juror4, nested.id), {"id": nested.id, "state": "deleted"})
            self.assertEqual(await self._code(self._comment(db, "juror3", post_id, "回复已删除的", nested.id)), (404, "comment_not_found"))
            self.assertEqual(await self._code(hall_service.delete_comment(db, juror4, 99999)), (404, "comment_not_found"))
            # 删掉的根评论：下面还有回复就留一个占位（content 为 null），没有回复的直接消失
            await hall_service.delete_comment(db, juror3, root.id)
            await hall_service.delete_comment(db, juror4, second_root.id)
            rows, total = await listing()
            self.assertEqual((total, rows), (3, [(root.id, None, "deleted", None, None), (reply.id, root.id, "visible", "同意", None), (deeper.id, root.id, "visible", "再回一层", "juror4")]))
            self.assertEqual((await hall_service.get_post(db, bob, post_id))["comment_count"], 2)
            # 回复都删光之后占位也没了
            await hall_service.delete_comment(db, juror1, reply.id)
            await hall_service.delete_comment(db, juror3, deeper.id)
            self.assertEqual(await listing(), ([], 0))
            self.assertEqual(await self._code(hall_service.list_comments(db, bob, 99999)), (404, "post_not_found"))

    async def test_posts_and_comments_pass_text_moderation(self) -> None:
        calls: list[tuple[str, int, str | None]] = []

        async def moderate(text: str, user_id: int, context_id: str | None = None) -> dict:
            calls.append((text, user_id, context_id))
            if "炸了" in text:
                raise RuntimeError("moderation service is down")
            return {"decision": "block" if "违禁词" in text else "review" if "擦边" in text else "allow"}

        async with self.sessions() as db:
            juror2, juror3, admin = [await self._user(db, name) for name in ("juror2", "juror3", "admin")]
            # 审核没启用：不调用，直接放行
            with patch.object(moderation_service, "_call_text_moderation_for_user", moderate):
                post_id = (await self._hall_post(db, "juror2", "discussion", "含违禁词的标题"))["id"]
                self.assertEqual(calls, [])
            with patch.object(settings, "content_moderation_enabled", True), patch.object(moderation_service, "_call_text_moderation_for_user", moderate), patch.object(hall_service, "POSTS_PER_DAY", 100):
                # 标题和正文都审；明确违规才拒绝
                self.assertEqual(await self._code(self._hall_post(db, "juror2", "discussion", "含违禁词的标题", "正常的正文")), (422, "content_rejected"))
                self.assertEqual(await self._code(self._hall_post(db, "juror2", "proposal", "正常的标题", "正文里有违禁词")), (422, "content_rejected"))
                self.assertEqual((calls[-1][0], calls[-1][1]), ("正常的标题\n正文里有违禁词", juror2.id))
                self.assertEqual(await self._code(self._comment(db, "juror3", post_id, "评论里有违禁词")), (422, "content_rejected"))
                self.assertEqual(await self._code(hall_service.update_post(db, juror2, post_id, None, "改成了违禁词")), (422, "content_rejected"))
                self.assertEqual((await db.execute(select(func.count()).select_from(JuryHallPost))).scalar_one(), 1)
                self.assertEqual((await db.execute(select(func.count()).select_from(JuryHallComment))).scalar_one(), 0)
                # 机器拿不准的（review）不拦；审核服务挂了也放行
                self.assertEqual((await self._hall_post(db, "juror2", "discussion", "有点擦边的标题"))["state"], "visible")
                self.assertEqual((await self._comment(db, "juror3", post_id, "服务炸了也能发"))["state"], "visible")
                self.assertEqual((await hall_service.update_post(db, juror2, post_id, "正常的新标题", None))["title"], "正常的新标题")
                # 白名单里的人、管理员的公告不审
                before = len(calls)
                juror3.content_moderation_whitelisted = True
                await db.commit()
                self.assertEqual((await self._comment(db, "juror3", post_id, "白名单里说违禁词"))["content"], "白名单里说违禁词")
                self.assertEqual((await hall_service.admin_create_announcement(db, admin, "公告里引用违禁词", "说明哪些词不能用"))["state"], "visible")
                self.assertEqual(len(calls), before)

    async def test_posting_and_commenting_are_rate_limited(self) -> None:
        async with self.sessions() as db:
            juror2, juror3 = await self._user(db, "juror2"), await self._user(db, "juror3")
            posts = [(await self._hall_post(db, "juror2", "discussion", f"第 {index} 帖"))["id"] for index in range(3)]
            with self.assertRaises(HTTPException) as caught:
                await self._hall_post(db, "juror2", "proposal", "第 4 帖")
            detail = caught.exception.detail
            self.assertEqual((caught.exception.status_code, detail["code"]), (429, "hall_rate_limited"))
            # 按最近 24 小时滚动：告诉客户端下一个名额什么时候释放
            first = await self._hall_row(db, posts[0])
            self.assertEqual(detail["resets_at"], jury_service._iso(first.created_at.replace(tzinfo=UTC) + timedelta(hours=24)))
            # 删了重发绕不过去；别人不受影响
            await hall_service.delete_post(db, juror2, posts[1])
            self.assertEqual(await self._code(self._hall_post(db, "juror2")), (429, "hall_rate_limited"))
            self.assertEqual((await self._hall_post(db, "juror1"))["mine"], True)
            # 最早那一帖过了 24 小时，名额就回来了
            first.created_at = datetime.now(UTC) - timedelta(hours=25)
            await db.commit()
            self.assertEqual((await self._hall_post(db, "juror2", "discussion", "第二天"))["title"], "第二天")
            # 管理员本人如果也是正式委员，发的公告不占他的发帖额度
            admin = await self._user(db, "admin")
            await jury_service.review_member(db, admin.id, action="approve", actor_id=admin.id, note=None, tier="regular")
            for index in range(4):
                await hall_service.admin_create_announcement(db, admin, f"公告 {index}", "内容")
            self.assertEqual((await self._hall_post(db, "admin"))["author"].username, "admin")

            with patch.object(hall_service, "COMMENTS_PER_DAY", 3):
                comments = [(await self._comment(db, "juror3", posts[0], f"第 {index} 条"))["id"] for index in range(3)]
                await hall_service.delete_comment(db, juror3, comments[0])
                self.assertEqual(await self._code(self._comment(db, "juror3", posts[2], "第 4 条")), (429, "hall_rate_limited"))
                self.assertEqual((await self._comment(db, "juror2", posts[0], "别人的"))["mine"], True)
            self.assertEqual((await self._comment(db, "juror3", posts[2], "上限是配置项"))["state"], "visible")


# ---------------------------------------------------------------- 议事厅：举报


class HallReportTests(HallTestCase):
    async def test_reports_fold_content_and_admin_handles_them(self) -> None:
        async with self.sessions() as db:
            admin, juror2, juror3, juror5, alice, bob = [await self._user(db, name) for name in ("admin", "juror2", "juror3", "juror5", "alice", "bob")]
            post_id = (await self._hall_post(db, "juror2", "discussion", "一个会被举报的帖子", "正文原文"))["id"]
            comment_id = (await self._comment(db, "juror3", post_id, "一条会被举报的评论"))["id"]
            reply_id = (await self._comment(db, "juror5", post_id, "它下面的回复", comment_id))["id"]
            notice_id = (await hall_service.admin_create_announcement(db, admin, "公告", "公告正文"))["id"]

            async def report(user: User, target_type: str, target_id: int, reason: str = "人身攻击", details: str | None = None) -> dict:
                return await hall_service.create_report(db, user, target_type, target_id, reason, details)

            # 任何登录用户都能举报；自己的不能举报；理由要在列表里，选「其他」要写说明
            self.assertEqual(await self._code(report(juror2, "post", post_id)), (400, "cannot_report_own"))
            self.assertEqual(await self._code(report(juror3, "comment", comment_id)), (400, "cannot_report_own"))
            self.assertEqual(await self._code(report(bob, "post", post_id, "看不顺眼")), (422, "invalid_reason"))
            self.assertEqual(await self._code(report(bob, "post", post_id, "其他", "太短")), (422, "invalid_reason"))
            self.assertEqual(await self._code(report(bob, "post", post_id, "人身攻击", "字" * 501)), (422, "invalid_reason"))
            self.assertEqual(await self._code(report(bob, "post", 99999)), (404, "post_not_found"))
            self.assertEqual(await self._code(report(bob, "comment", 99999)), (404, "comment_not_found"))
            self.assertEqual(await self._code(report(bob, "user", post_id)), (422, "invalid_target"))
            self.assertEqual((await db.execute(select(func.count()).select_from(JuryHallReport))).scalar_one(), 0)

            self.assertEqual(await report(bob, "post", post_id, "其他", "这是一段足够长的说明"), {"target_type": "post", "target_id": post_id, "state": "visible"})
            self.assertEqual(await self._code(report(bob, "post", post_id, "讨论未结案件")), (409, "already_reported"))
            # 帖子和评论各算各的：同一个编号举报过帖子，不妨碍举报评论
            self.assertEqual((await report(bob, "comment", comment_id))["state"], "visible")
            self.assertEqual((await report(alice, "post", post_id, "讨论未结案件"))["state"], "visible")
            seen = {viewer.username: (await hall_service.get_post(db, viewer, post_id))["reported"] for viewer in (bob, alice, juror5)}
            self.assertEqual(seen, {"bob": True, "alice": True, "juror5": False})

            # 第三个不同的人举报之后自动折叠：留一个占位，标题和正文都不给
            with self._events() as events:
                self.assertEqual((await report(juror5, "post", post_id, "泄露身份或隐私"))["state"], "hidden")
            self.assertEqual([data for name, _users, data in events if name == "jury.hall.changed"], [{"post_id": post_id, "kind": "discussion", "action": "updated"}])
            folded = HallPost(**await hall_service.get_post(db, juror2, post_id))
            self.assertEqual((folded.state, folded.title, folded.content, folded.can_comment, folded.can_edit, folded.can_delete, folded.mine), ("hidden", hall_service.HIDDEN_TITLE, None, False, False, True, True))
            listed = (await hall_service.list_posts(db, bob))[0]
            self.assertEqual([(item["id"], item["state"], item["content"], item["reported"]) for item in listed if item["id"] == post_id], [(post_id, "hidden", None, True)])
            self.assertNotIn("正文原文", json.dumps(listed, default=str, ensure_ascii=False))
            self.assertEqual(await self._code(self._comment(db, "juror3", post_id, "折叠之后评论")), (409, "post_closed"))
            self.assertEqual(await self._code(hall_service.update_post(db, juror2, post_id, "折叠之后改", None)), (409, "post_locked"))

            # 评论同理：折叠后留占位，content 为 null；回复还在
            await report(alice, "comment", comment_id, "与风纪无关")
            self.assertEqual((await report(juror2, "comment", comment_id))["state"], "hidden")
            comments = {item["id"]: item for item in (await hall_service.list_comments(db, bob, post_id))[0]}
            self.assertEqual((comments[comment_id]["state"], comments[comment_id]["content"], comments[comment_id]["reported"]), ("hidden", None, True))
            self.assertEqual((comments[reply_id]["state"], comments[reply_id]["content"], comments[reply_id]["reported"]), ("visible", "它下面的回复", False))
            self.assertFalse({item["id"]: item for item in (await hall_service.list_comments(db, juror5, post_id))[0]}[comment_id]["reported"])
            # 阈值是配置项
            with patch.object(hall_service, "AUTO_HIDE_REPORTS", 1):
                self.assertEqual((await report(bob, "comment", reply_id))["state"], "hidden")
            # 公告不自动折叠，举报照样记下来
            for viewer in (bob, alice, juror5, juror2):
                self.assertEqual((await report(viewer, "post", notice_id, "与风纪无关"))["state"], "visible")

            # 后台：逐条举报，带目标的原文和真实作者
            items, total = await hall_service.admin_list_reports(db, status_filter="pending", limit=100)
            self.assertEqual(total, 11)
            reads = [AdminHallReportRead(**item) for item in items]
            on_post = [read for read in reads if (read.target_type, read.target_id) == ("post", post_id)]
            self.assertEqual(sorted(read.reporter.username for read in on_post), ["alice", "bob", "juror5"])
            self.assertEqual({(read.target.content, read.target.state, read.target.author.username, read.target.pending_reports, read.target.post_title) for read in on_post}, {("正文原文", "hidden", "juror2", 3, "一个会被举报的帖子")})
            self.assertEqual({read.details for read in on_post}, {"这是一段足够长的说明", None})
            listed_admin = {item["id"]: item["pending_reports"] for item in (await hall_service.admin_list_posts(db))[0]}
            self.assertEqual(listed_admin, {post_id: 7, notice_id: 4})

            # 驳回：帖子恢复显示，这个帖子上其余待处理的举报一起了结；评论的举报不受影响
            with self._events() as events:
                handled = AdminHallReportRead(**await hall_service.admin_handle_report(db, admin, on_post[0].id, "dismiss", "正常讨论"))
            self.assertEqual((handled.status, handled.handled_by_id, handled.handle_note, handled.target.state, handled.target.pending_reports), ("dismissed", admin.id, "正常讨论", "visible", 0))
            self.assertEqual([data["action"] for name, _users, data in events if name == "jury.hall.changed"], ["updated"])
            restored = await hall_service.get_post(db, bob, post_id)
            self.assertEqual((restored["state"], restored["title"], restored["content"], restored["reported"]), ("visible", "一个会被举报的帖子", "正文原文", True))
            self.assertEqual(await self._code(hall_service.admin_handle_report(db, admin, on_post[1].id, "dismiss", None)), (409, "report_handled"))
            self.assertEqual(await self._code(hall_service.admin_handle_report(db, admin, 99999, "dismiss", None)), (404, "report_not_found"))
            self.assertEqual((await hall_service.admin_list_reports(db, status_filter="dismissed"))[1], 3)
            # 驳回过的人不能再举报一次；新的举报重新从零数
            self.assertEqual(await self._code(report(bob, "post", post_id)), (409, "already_reported"))

            # 删除：评论变成已删除，它的举报记成已处理
            on_comment = next(read for read in reads if (read.target_type, read.target_id) == ("comment", comment_id))
            removed = AdminHallReportRead(**await hall_service.admin_handle_report(db, admin, on_comment.id, "delete", "人身攻击"))
            self.assertEqual((removed.status, removed.target.state, removed.target.content), ("handled", "deleted", "一条会被举报的评论"))
            comments = {item["id"]: item for item in (await hall_service.list_comments(db, bob, post_id))[0]}
            # 下面还有（被折叠的）回复，所以留一个已删除的占位
            self.assertEqual((comments[comment_id]["state"], comments[comment_id]["content"], comments[reply_id]["state"]), ("deleted", None, "hidden"))
            row = await db.get(JuryHallComment, comment_id)
            self.assertEqual((row.deleted_by_id, row.moderation_note), (admin.id, "人身攻击"))
            self.assertEqual(await self._code(report(alice, "comment", comment_id)), (404, "comment_not_found"))
            self.assertEqual((await hall_service.admin_list_reports(db, status_filter="handled"))[1], 3)
            # 删帖子：帖子和它下面评论的举报一起了结
            on_reply = next(read for read in reads if (read.target_type, read.target_id) == ("comment", reply_id))
            await report(juror3, "post", post_id)
            await hall_service.admin_decide(db, admin, post_id, "delete", "整帖删除")
            self.assertEqual(await self._code(hall_service.admin_handle_report(db, admin, on_reply.id, "dismiss", None)), (409, "report_handled"))
            self.assertEqual((await hall_service.admin_list_reports(db, status_filter="pending"))[1], 4)
            self.assertEqual(await self._audit_actions(db, "jury.hall.report."), ["jury.hall.report.dismiss", "jury.hall.report.delete"])


# ---------------------------------------------------------------- 议事厅：列表与配置


class HallListTests(HallTestCase):
    async def test_list_order_filters_and_excerpts(self) -> None:
        async with self.sessions() as db:
            juror1, juror2, juror3, bob, admin = [await self._user(db, name) for name in ("juror1", "juror2", "juror3", "bob", "admin")]
            long_text = "".join(str(index % 10) for index in range(500))
            oldest = (await self._hall_post(db, "juror2", "discussion", "最早的讨论", long_text))["id"]
            notice = (await hall_service.admin_create_announcement(db, admin, "公告", "公告内容", pinned=True))["id"]
            proposal = (await self._hall_post(db, "juror1", "proposal", "一个提案", "短正文"))["id"]
            newest = (await self._hall_post(db, "juror2", "discussion", "最新的讨论", "字" * 200))["id"]

            async def order(viewer: User = bob, **filters) -> list[int]:
                items, total = await hall_service.list_posts(db, viewer, **filters)
                self.assertGreaterEqual(total, len(items))
                return [item["id"] for item in items]

            # 置顶始终在前；默认按最后活动时间，new 按发帖时间
            self.assertEqual(await order(), [notice, newest, proposal, oldest])
            self.assertEqual(await order(sort="new"), [notice, newest, proposal, oldest])
            await self._comment(db, "juror3", oldest, "顶一下")
            self.assertEqual(await order(), [notice, oldest, newest, proposal])
            self.assertEqual(await order(sort="active"), [notice, oldest, newest, proposal])
            self.assertEqual(await order(sort="new"), [notice, newest, proposal, oldest])
            # 表决和编辑不算「活动」，不会把帖子顶上去
            await hall_service.cast_vote(db, juror3, proposal, "yes")
            await hall_service.update_post(db, juror2, newest, "改了标题的最新讨论", None)
            self.assertEqual(await order(), [notice, oldest, newest, proposal])
            self.assertEqual(await order(kind="discussion"), [oldest, newest])
            self.assertEqual(await order(kind="announcement"), [notice])
            self.assertEqual(await order(status_filter="open", limit=2, offset=1), [oldest, newest])
            self.assertEqual((await hall_service.list_posts(db, bob, limit=2, offset=1))[1], 4)
            self.assertEqual(await order(status_filter="passed"), [])

            # 列表里的正文是前 200 字，详情里是全文
            items = {item["id"]: HallPost(**item) for item in (await hall_service.list_posts(db, bob))[0]}
            self.assertEqual((items[oldest].content, items[oldest].content_truncated), (long_text[:200], True))
            self.assertEqual((items[newest].content, items[newest].content_truncated), ("字" * 200, False))
            self.assertEqual((items[proposal].content, items[proposal].content_truncated, items[oldest].comment_count), ("短正文", False, 1))
            full = HallPost(**await hall_service.get_post(db, bob, oldest))
            self.assertEqual((full.content, full.content_truncated), (long_text, False))

            # 每个人看到的标记
            await hall_service.create_report(db, bob, "post", proposal, "与风纪无关", None)

            async def flags(viewer: User, post_id: int) -> dict:
                item = next(item for item in (await hall_service.list_posts(db, viewer))[0] if item["id"] == post_id)
                return {key: item[key] for key in ("mine", "can_edit", "can_delete", "can_comment", "reported")} | {"can_vote": item["vote"]["can_vote"] if item["vote"] else None}

            self.assertEqual(await flags(juror1, proposal), {"mine": True, "can_edit": False, "can_delete": True, "can_comment": True, "reported": False, "can_vote": True})
            self.assertEqual(await flags(juror3, proposal), {"mine": False, "can_edit": False, "can_delete": False, "can_comment": True, "reported": False, "can_vote": True})
            self.assertEqual(await flags(bob, proposal), {"mine": False, "can_edit": False, "can_delete": False, "can_comment": False, "reported": True, "can_vote": False})
            self.assertEqual(await flags(juror2, newest), {"mine": True, "can_edit": True, "can_delete": True, "can_comment": True, "reported": False, "can_vote": None})
            self.assertEqual(await flags(admin, notice), {"mine": True, "can_edit": True, "can_delete": True, "can_comment": False, "reported": False, "can_vote": None})
            # 作者现在不是在任委员了：帖子还在，author_tier 为 null，他自己也不能再改
            await jury_service.review_member(db, juror2.id, action="retire", actor_id=admin.id, note=None)
            item = next(item for item in (await hall_service.list_posts(db, juror2))[0] if item["id"] == newest)
            self.assertEqual((item["author"].username, item["author_tier"], item["mine"], item["can_edit"], item["can_delete"], item["can_comment"]), ("juror2", None, True, False, True, False))

    async def test_config_exposes_the_hall_block_and_capabilities(self) -> None:
        async with self.sessions() as db:
            raw = await overview_service.config(db)
            config = JuryConfig(**raw)
            self.assertEqual(config.capabilities[-2:], ["precedents", "hall"])
            self.assertEqual(len(config.capabilities), len(set(config.capabilities)))
            hall = dict(raw["hall"])
            guidelines = hall.pop("guidelines")
            self.assertEqual(
                hall,
                {
                    "title_max": 60, "content_max": 5000, "comment_max": 1000,
                    "posts_per_day": 3, "comments_per_day": 50,
                    "vote_days": 7, "quorum_ratio": 0.3, "quorum_min": 3,
                    "pass_num": 2, "pass_den": 3, "pass_ratio": 0.6667,
                    "report_reasons": ["人身攻击", "讨论未结案件", "泄露身份或隐私", "与风纪无关", "其他"],
                    "auto_hide_reports": 3,
                },
            )  # fmt: skip
            for phrase in ("不要讨论具体的未结案件", "对事不对人", "规则由管理员修改，提案通过后由管理员决定是否采纳"):
                self.assertIn(phrase, guidelines)
            self.assertIn("\n", guidelines)
            self.assertEqual((config.hall.guidelines, config.hall.pass_ratio, config.hall.auto_hide_reports), (guidelines, 0.6667, 3))
            # 数字都来自配置项
            self.assertEqual(
                (settings.jury_hall_title_max, settings.jury_hall_content_max, settings.jury_hall_comment_max, settings.jury_hall_posts_per_day, settings.jury_hall_comments_per_day),
                (hall["title_max"], hall["content_max"], hall["comment_max"], hall["posts_per_day"], hall["comments_per_day"]),
            )
            self.assertEqual(
                (settings.jury_hall_vote_days, settings.jury_hall_quorum_ratio, settings.jury_hall_quorum_min, settings.jury_hall_pass_num, settings.jury_hall_pass_den, settings.jury_hall_auto_hide_reports),
                (hall["vote_days"], hall["quorum_ratio"], hall["quorum_min"], hall["pass_num"], hall["pass_den"], hall["auto_hide_reports"]),
            )


# ---------------------------------------------------------------- 走一遍路由


class HttpHallTests(HallTestCase):
    """走一遍真实的路由和响应模型：路径顺序、查询参数、错误结构。"""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.acting = "juror1"
        api = FastAPI()
        # 和 app/api/v1/router.py 里的挂法一样：两个新路由各用已有的前缀再挂一次
        api.include_router(jury_api.router, prefix="/jury")
        api.include_router(admin_api.router, prefix="/admin")
        api.include_router(hall_api.router, prefix="/jury")
        api.include_router(admin_hall_api.router, prefix="/admin")

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

    async def test_the_real_router_mounts_both_modules(self) -> None:
        from app.api.v1.router import api_router

        api = FastAPI()
        api.include_router(api_router)
        paths = api.openapi()["paths"]
        for path, methods in (
            ("/api/v1/jury/precedents", {"get", "post"}),
            ("/api/v1/jury/precedents/{precedent_id}", {"get", "put", "delete"}),
            ("/api/v1/jury/hall/posts", {"get", "post"}),
            ("/api/v1/jury/hall/posts/{post_id}", {"get", "put", "delete"}),
            ("/api/v1/jury/hall/posts/{post_id}/vote", {"post"}),
            ("/api/v1/jury/hall/posts/{post_id}/comments", {"get", "post"}),
            ("/api/v1/jury/hall/comments/{comment_id}", {"delete"}),
            ("/api/v1/jury/hall/reports", {"post"}),
            ("/api/v1/admin/jury/precedents", {"get", "post"}),
            ("/api/v1/admin/jury/precedents/{precedent_id}", {"put", "delete"}),
            ("/api/v1/admin/jury/hall/posts", {"get", "post"}),
            ("/api/v1/admin/jury/hall/posts/{post_id}", {"get"}),
            ("/api/v1/admin/jury/hall/posts/{post_id}/decision", {"post"}),
            ("/api/v1/admin/jury/hall/reports", {"get"}),
            ("/api/v1/admin/jury/hall/reports/{report_id}/handle", {"post"}),
        ):
            self.assertEqual(set(paths[path]), methods, path)

    async def test_precedent_and_hall_routes(self) -> None:
        async with self.sessions() as db:
            case_id = await self._jury_case(db, "insult")
            await self._settle(db, case_id)
            await self._age(db, case_id, 100)
            review_id = await self._rid(db, "juror1", case_id)
        client = self.client
        config = (await client.get("/jury/config")).json()
        self.assertEqual(config["capabilities"][-2:], ["precedents", "hall"])
        self.assertEqual((config["hall"]["title_max"], config["hall"]["pass_num"], config["hall"]["pass_den"], config["hall"]["vote_days"]), (60, 2, 3, 7))

        # —— 判例：委员侧提交的是评审编号，公开编号一律 404 ——
        body = {"title": "反复辱骂群成员属于辱骂骚扰", "summary": "连续几条都指向同一个人。"}
        wrong = await client.post("/jury/precedents", json={"case_id": case_id, **body})
        self.assertEqual((wrong.status_code, wrong.json()["detail"]["code"]), (404, "case_not_found"))
        detail = (await client.get(f"/jury/cases/{review_id}")).json()
        self.assertEqual((detail["precedent"], detail["can_mark_precedent"]), (None, True))
        made = await client.post("/jury/precedents", json={"case_id": review_id, **body})
        self.assertEqual((made.status_code, made.json()["case_id"], made.json()["by"], made.json()["mine"]), (200, case_id, "senior", True))
        precedent_id = made.json()["id"]
        again = await client.post("/jury/precedents", json={"case_id": review_id, **body})
        self.assertEqual((again.status_code, again.json()["detail"]["code"]), (409, "precedent_exists"))
        self.assertEqual((await client.post("/jury/precedents", json={"case_id": review_id})).status_code, 422)
        # 契约里的查询串带着空值：留空表示不筛选
        listed = await client.get("/jury/precedents?category=&limit=20&offset=0")
        self.assertEqual((listed.status_code, listed.json()["total"], listed.json()["items"][0]["id"]), (200, 1, precedent_id))
        self.assertEqual((await client.get("/jury/precedents?category=fraud")).json(), {"items": [], "total": 0})
        self.assertEqual((await client.get("/jury/precedents?limit=51")).status_code, 422)
        shown = (await client.get(f"/jury/precedents/{precedent_id}")).json()
        self.assertEqual((shown["content"], shown["content_hidden_reason"], shown["case"]["post"]), (None, "group_message", None))
        self.assertEqual([message["offset"] for message in shown["case"]["messages"] if message["is_target"]], [0])
        self.assertEqual((await client.get("/jury/precedents/99999")).json()["detail"]["code"], "precedent_not_found")
        detail = (await client.get(f"/jury/cases/{review_id}")).json()
        self.assertEqual((detail["precedent"], detail["can_mark_precedent"]), ({"id": precedent_id, "title": body["title"], "mine": True}, False))
        self.assertEqual((await client.get(f"/jury/public/cases/{case_id}")).json()["precedent_id"], precedent_id)
        self.assertEqual((await client.get("/jury/public/cases")).json()["items"][0]["precedent_id"], precedent_id)
        edited = await client.put(f"/jury/precedents/{precedent_id}", json={"title": "新标题"})
        self.assertEqual((edited.status_code, edited.json()["title"], edited.json()["summary"]), (200, "新标题", body["summary"]))
        too_long = await client.put(f"/jury/precedents/{precedent_id}", json={"title": "字" * 61})
        self.assertEqual((too_long.status_code, too_long.json()["detail"]["code"]), (422, "invalid_precedent"))

        # —— 议事厅：发帖、列表、详情、修改、表决 ——
        posted = await client.post("/jury/hall/posts", json={"kind": "proposal", "title": "建议把刷屏的默认等级调到一般", "content": "理由如下。"})
        self.assertEqual((posted.status_code, posted.json()["vote"]["quorum"], posted.json()["author"]["username"], posted.json()["author_tier"]), (200, 3, "juror1", "senior"))
        post_id = posted.json()["id"]
        bad_kind = await client.post("/jury/hall/posts", json={"kind": "announcement", "title": "标题", "content": "正文"})
        self.assertEqual((bad_kind.status_code, bad_kind.json()["detail"]["code"]), (422, "invalid_post"))
        listed = await client.get("/jury/hall/posts?kind=&status=&sort=&limit=20&offset=0")
        self.assertEqual((listed.status_code, listed.json()["total"], listed.json()["items"][0]["id"]), (200, 1, post_id))
        self.assertEqual((await client.get("/jury/hall/posts?kind=discussion&status=open&sort=new")).json(), {"items": [], "total": 0})
        self.assertEqual((await client.get("/jury/hall/posts?sort=bogus")).status_code, 422)
        self.assertEqual((await client.get(f"/jury/hall/posts/{post_id}")).json()["content"], "理由如下。")
        self.assertEqual((await client.get("/jury/hall/posts/99999")).json()["detail"]["code"], "post_not_found")
        self.assertEqual((await client.put(f"/jury/hall/posts/{post_id}", json={"content": "补充了理由。"})).json()["content"], "补充了理由。")
        voted = await client.post(f"/jury/hall/posts/{post_id}/vote", json={"vote": "yes"})
        self.assertEqual((voted.status_code, voted.json()["vote"]["yes"], voted.json()["vote"]["my_vote"], voted.json()["can_edit"]), (200, 1, "yes", False))
        bad_vote = await client.post(f"/jury/hall/posts/{post_id}/vote", json={"vote": "maybe"})
        self.assertEqual((bad_vote.status_code, bad_vote.json()["detail"]["code"]), (422, "invalid_vote"))
        locked = await client.put(f"/jury/hall/posts/{post_id}", json={"title": "表决后改"})
        self.assertEqual((locked.status_code, locked.json()["detail"]["code"]), (409, "post_locked"))

        # 见习委员：不能发帖，可以评论和回复
        self.acting = "juror3"
        self.assertEqual((await client.post("/jury/hall/posts", json={"kind": "discussion", "title": "标题", "content": "正文"})).json()["detail"]["code"], "tier_required")
        comment = await client.post(f"/jury/hall/posts/{post_id}/comments", json={"content": "支持"})
        self.assertEqual((comment.status_code, comment.json()["parent_id"], comment.json()["author_tier"]), (200, None, "trainee"))
        comment_id = comment.json()["id"]
        reply = await client.post(f"/jury/hall/posts/{post_id}/comments", json={"content": "补充一点", "parent_id": comment_id})
        self.assertEqual((reply.status_code, reply.json()["parent_id"], reply.json()["reply_to"]), (200, comment_id, None))
        reply_id = reply.json()["id"]
        empty = await client.post(f"/jury/hall/posts/{post_id}/comments", json={"content": "  "})
        self.assertEqual((empty.status_code, empty.json()["detail"]["code"]), (422, "invalid_comment"))
        comments = await client.get(f"/jury/hall/posts/{post_id}/comments?limit=50&offset=0")
        self.assertEqual((comments.status_code, comments.json()["total"], [item["id"] for item in comments.json()["items"]]), (200, 2, [comment_id, reply_id]))

        # 普通用户：全站可读，可以举报，不能发言；判例只读、没有 case
        self.acting = "bob"
        self.assertEqual((await client.get("/jury/hall/posts")).json()["total"], 1)
        self.assertEqual((await client.get(f"/jury/hall/posts/{post_id}/comments")).json()["total"], 2)
        self.assertEqual((await client.post(f"/jury/hall/posts/{post_id}/comments", json={"content": "路过"})).json()["detail"]["code"], "not_juror")
        self.assertEqual((await client.post(f"/jury/hall/posts/{post_id}/vote", json={"vote": "no"})).json()["detail"]["code"], "not_juror")
        # /hall/reports 是固定路径，没有被别的路由吃掉
        reported = await client.post("/jury/hall/reports", json={"target_type": "comment", "target_id": reply_id, "reason": "人身攻击"})
        self.assertEqual((reported.status_code, reported.json()), (200, {"target_type": "comment", "target_id": reply_id, "state": "visible"}))
        duplicate = await client.post("/jury/hall/reports", json={"target_type": "comment", "target_id": reply_id, "reason": "人身攻击"})
        self.assertEqual((duplicate.status_code, duplicate.json()["detail"]["code"]), (409, "already_reported"))
        self.assertEqual((await client.post("/jury/hall/reports", json={"target_type": "user", "target_id": 1, "reason": "人身攻击"})).status_code, 422)
        self.assertTrue((await client.get(f"/jury/hall/posts/{post_id}/comments")).json()["items"][1]["reported"])
        self.assertEqual((await client.delete(f"/jury/hall/comments/{comment_id}")).json()["detail"]["code"], "not_comment_author")
        mine = (await client.get("/jury/precedents")).json()["items"][0]
        self.assertEqual((mine["mine"], (await client.get(f"/jury/precedents/{precedent_id}")).json()["case"]), (False, None))
        self.assertEqual((await client.delete(f"/jury/precedents/{precedent_id}")).json()["detail"]["code"], "not_precedent_author")
        self.assertEqual((await client.post("/jury/precedents", json={"case_id": review_id, **body})).json()["detail"]["code"], "not_juror")
        for path in ("/admin/jury/precedents", "/admin/jury/hall/posts", "/admin/jury/hall/reports"):
            self.assertEqual((await client.get(path)).status_code, 403, path)

        self.acting = "juror3"
        self.assertEqual((await client.delete(f"/jury/hall/comments/{comment_id}")).json(), {"id": comment_id, "state": "deleted"})
        self.acting = "juror1"
        self.assertEqual((await client.post("/admin/jury/hall/posts", json={"title": "冒充公告", "content": "正文"})).status_code, 403)

        # —— 后台 ——
        self.acting = "admin"
        precedents = await client.get("/admin/jury/precedents?status=&limit=50&offset=0")
        self.assertEqual((precedents.status_code, precedents.json()["total"], precedents.json()["items"][0]["created_by"]["username"]), (200, 1, "juror1"))
        self.assertEqual((await client.get("/admin/jury/precedents?status=bogus")).status_code, 422)
        changed = await client.put(f"/admin/jury/precedents/{precedent_id}", json={"summary": "管理员改过的要旨"})
        self.assertEqual((changed.status_code, changed.json()["summary"], changed.json()["by"]), (200, "管理员改过的要旨", "senior"))
        withdrawn = await client.delete(f"/admin/jury/precedents/{precedent_id}")
        self.assertEqual((withdrawn.status_code, withdrawn.json()["status"]), (200, "withdrawn"))
        self.assertEqual((await client.get("/jury/precedents")).json()["total"], 0)
        # 后台用的是案件的公开编号
        remade = await client.post("/admin/jury/precedents", json={"case_id": case_id, **body})
        self.assertEqual((remade.status_code, remade.json()["id"], remade.json()["by"], remade.json()["status"], remade.json()["case_id"]), (200, precedent_id, "admin", "active", case_id))
        noted = await client.request("DELETE", f"/admin/jury/precedents/{precedent_id}", json={"note": "重复了"})
        self.assertEqual((noted.status_code, noted.json()["withdraw_note"]), (200, "重复了"))

        notice = await client.post("/admin/jury/hall/posts", json={"title": "议事厅开放了", "content": "欢迎讨论。", "pinned": True})
        self.assertEqual((notice.status_code, notice.json()["kind"], notice.json()["pinned"], notice.json()["author_tier"]), (200, "announcement", True, None))
        notice_id = notice.json()["id"]
        posts = await client.get("/admin/jury/hall/posts?kind=&status=&state=")
        self.assertEqual((posts.status_code, posts.json()["total"], [item["id"] for item in posts.json()["items"]]), (200, 2, [notice_id, post_id]))
        admin_detail = await client.get(f"/admin/jury/hall/posts/{post_id}")
        self.assertEqual((admin_detail.status_code, [(item["id"], item["state"], item["content"]) for item in admin_detail.json()["comments"]]), (200, [(comment_id, "deleted", "支持"), (reply_id, "visible", "补充一点")]))
        self.assertEqual((await client.post(f"/admin/jury/hall/posts/{notice_id}/decision", json={"action": "unpin"})).json()["pinned"], False)
        early = await client.post(f"/admin/jury/hall/posts/{post_id}/decision", json={"action": "adopt", "note": "还没表决完"})
        self.assertEqual((early.status_code, early.json()["detail"]["code"]), (409, "invalid_post_status"))
        self.assertEqual((await client.post(f"/admin/jury/hall/posts/{post_id}/decision", json={"action": "bogus"})).status_code, 422)
        reports = await client.get("/admin/jury/hall/reports?status=pending")
        self.assertEqual((reports.status_code, reports.json()["total"], reports.json()["items"][0]["target"]["content"], reports.json()["items"][0]["reporter"]["username"]), (200, 1, "补充一点", "bob"))
        report_id = reports.json()["items"][0]["id"]
        handled = await client.post(f"/admin/jury/hall/reports/{report_id}/handle", json={"action": "delete", "note": "人身攻击"})
        self.assertEqual((handled.status_code, handled.json()["status"], handled.json()["target"]["state"]), (200, "handled", "deleted"))
        twice = await client.post(f"/admin/jury/hall/reports/{report_id}/handle", json={"action": "dismiss"})
        self.assertEqual((twice.status_code, twice.json()["detail"]["code"]), (409, "report_handled"))
        self.assertEqual((await client.post(f"/admin/jury/hall/reports/{report_id}/handle", json={"action": "bogus"})).status_code, 422)
        closed = await client.post(f"/admin/jury/hall/posts/{post_id}/decision", json={"action": "close", "note": "提案人撤回"})
        self.assertEqual((closed.status_code, closed.json()["status"], closed.json()["admin_note"]), (200, "closed", "提案人撤回"))
        deleted = await client.post(f"/admin/jury/hall/posts/{post_id}/decision", json={"action": "delete"})
        self.assertEqual((deleted.status_code, deleted.json()["state"]), (200, "deleted"))
        self.assertEqual((await client.get(f"/jury/hall/posts/{post_id}")).status_code, 404)
        self.assertEqual((await client.get("/jury/hall/posts")).json()["total"], 1)
        self.assertEqual((await client.get("/admin/jury/hall/posts?state=deleted")).json()["total"], 1)


if __name__ == "__main__":
    unittest.main()
