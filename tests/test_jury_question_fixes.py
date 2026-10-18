"""质控题出错之后的补救：改答案 / 移出计分时给答错的人改判，答错多的题自动移出计分，看不全的题不能当计分题。"""

from __future__ import annotations

import json
import unittest
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.models.case import JuryNotification, JuryPointLog, ReportCase
from app.models.report import AdminAuditLog
from app.services import jury_bank as bank_service

from test_jury_v2_phase2 import Phase2TestCase


class QuestionFixTests(Phase2TestCase):
    def _snapshot(self, *, recalled: bool = False, voice_target: bool = False) -> str:
        """被举报人在上下文里先说了一句（可以是已撤回的），然后是被举报的那条。"""
        now = datetime.now(UTC) - timedelta(days=10)
        earlier = {
            "sender_id": self.ids["offender"],
            "content": "你浮木没了",
            "type": "text",
            "is_target": False,
            "recalled_at": now.replace(tzinfo=None).isoformat() if recalled else None,
            "created_at": (now - timedelta(seconds=30)).replace(tzinfo=None).isoformat(),
        }
        target = {
            "sender_id": self.ids["offender"],
            "content": "https://example.com/a.webm" if voice_target else "笑死我了",
            "type": "voice" if voice_target else "text",
            "is_target": True,
            "created_at": now.replace(tzinfo=None).isoformat(),
        }
        return json.dumps({"messages": [earlier, target], "message": target})

    async def _update(self, db, case: ReportCase, **changes):
        return await bank_service.update_question(db, case.id, actor_id=self.ids["admin"], changes=changes)

    async def _fixed_logs(self, db, name: str, case_id: int) -> list[int]:
        rows = await db.execute(
            select(JuryPointLog.points).where(JuryPointLog.user_id == self.ids[name], JuryPointLog.kind == "quality_fixed", JuryPointLog.case_id == case_id)
        )
        return list(rows.scalars().all())

    async def test_changing_the_answer_refunds_the_jurors_who_got_it_right(self) -> None:
        async with self.sessions() as db:
            question = await self._question(db, upheld=False)
            # juror1 判违规（按原答案答错，扣 5），juror2 判不违规（按原答案答对，加 5）
            await self._cast(db, "juror1", question.id, "l2", quality=True)
            await self._cast(db, "juror2", question.id, "none", quality=True)
            first, second = await self._member(db, "juror1"), await self._member(db, "juror2")
            self.assertEqual((first.golden_correct, first.points, second.golden_correct), (0, 0, 1))
            before = second.points

            await self._update(db, question, outcome="violation")
            await db.refresh(first)
            await db.refresh(second)
            # 原来答错、现在对的：改判为答对，退回实际扣掉的分并补上答对的分。
            # 当时先拿了今日首票 +2，扣 5 被 0 封底只扣到 2，所以补 2 + 5
            self.assertEqual((first.golden_total, first.golden_correct, first.points), (1, 1, 7))
            self.assertEqual(await self._fixed_logs(db, "juror1", question.id), [7])
            notice = (await db.execute(select(JuryNotification).where(JuryNotification.user_id == self.ids["juror1"], JuryNotification.kind == "quality_fixed"))).scalar_one()
            self.assertIn("已改判为答对", notice.body)
            # 原来对、现在错的：不追溯扣分
            self.assertEqual((second.golden_correct, second.points), (1, before))
            # 再改一次答案不会重复补分
            await self._update(db, question, outcome="clean")
            await self._update(db, question, outcome="violation")
            self.assertEqual(await self._fixed_logs(db, "juror1", question.id), [7])

    async def test_pulling_a_question_from_scoring_voids_wrong_answers_and_lifts_suspensions(self) -> None:
        async with self.sessions() as db:
            question = await self._question(db)
            member = await self._member(db, "juror1")
            # 已经答过 4 道、对 2 道，有点积分可扣（投票当天先拿今日首票 +2）
            member.golden_total, member.golden_correct, member.points = 4, 2, 100
            await db.commit()
            await self._cast(db, "juror1", question.id, "none", quality=True)
            await db.refresh(member)
            self.assertEqual((member.status, member.golden_total, member.golden_correct, member.points), ("suspended", 5, 2, 97))

            # 管理员判断这道题有问题，改回未核对：答错的一律改判为答对，准确率回到 3/5 = 60%，资格恢复
            await self._update(db, question, verified=False)
            await db.refresh(member)
            self.assertEqual((member.status, member.golden_correct, member.points), ("active", 3, 107))
            self.assertEqual(await self._fixed_logs(db, "juror1", question.id), [10])
            kinds = (await db.execute(select(JuryNotification.kind).where(JuryNotification.user_id == self.ids["juror1"]))).scalars().all()
            self.assertIn("reinstated", kinds)
            restored = (await db.execute(select(AdminAuditLog).where(AdminAuditLog.action == "jury.member.auto_reinstate"))).scalar_one()
            self.assertEqual(restored.target_id, self.ids["juror1"])

    async def test_removing_a_scored_question_voids_wrong_answers(self) -> None:
        async with self.sessions() as db:
            question = await self._question(db)
            await self._cast(db, "juror1", question.id, "none", quality=True)
            await self._update(db, question, in_bank=False)
            member = await self._member(db, "juror1")
            self.assertEqual((member.golden_total, member.golden_correct), (1, 1))
            self.assertEqual(await self._fixed_logs(db, "juror1", question.id), [7])

    async def test_questions_many_jurors_get_wrong_are_pulled_from_scoring(self) -> None:
        async with self.sessions() as db:
            question = await self._question(db)
            await self._cast(db, "juror1", question.id, "l2", quality=True)
            await self._cast(db, "juror2", question.id, "none", quality=True)
            await db.refresh(question)
            # 1 错 / 2 答：错的人数还不够
            self.assertTrue(question.golden_verified)
            await self._cast(db, "juror3", question.id, "none", quality=True)
            await db.refresh(question)
            self.assertFalse(question.golden_verified)
            self.assertTrue(question.is_golden)
            log = (await db.execute(select(AdminAuditLog).where(AdminAuditLog.action == "jury.question.auto_pulled"))).scalar_one()
            self.assertEqual(json.loads(log.details), {"wrong": 2, "answered": 3})
            # 只是移出计分，不自动改判：等管理员复核
            self.assertEqual(await self._fixed_logs(db, "juror2", question.id), [])

            # 管理员复核后重新标成已核对，只看这之后的作答
            await self._update(db, question, verified=True)
            await self._cast(db, "juror4", question.id, "none", quality=True)
            await db.refresh(question)
            self.assertTrue(question.golden_verified)

    async def test_questions_the_jurors_cannot_fully_see_cannot_be_scored(self) -> None:
        async with self.sessions() as db:
            hidden = await self._question(db, upheld=False, verified=False, snapshot=self._snapshot(recalled=True))
            visible = await self._question(db, upheld=False, verified=False, snapshot=self._snapshot())
            voice = await self._question(db, verified=False, snapshot=self._snapshot(voice_target=True))

            # 判「不违规」、被举报人还有撤回的消息：委员看不全，不能当计分题
            self.assertEqual(await self._code(self._update(db, hidden, verified=True)), (422, "question_not_verifiable"))
            await db.refresh(hidden)
            self.assertFalse(hidden.golden_verified)
            detail = await bank_service.question_detail(db, hidden.id)
            self.assertEqual(detail["verify_block_reason"], "hidden_context")
            # 改成「违规」就可以：被举报的那条本身看得见
            self.assertTrue((await self._update(db, hidden, outcome="violation", verified=True))["verified"])
            # 已核对的题把答案改成「不违规」也会被拦下
            self.assertEqual(await self._code(self._update(db, hidden, outcome="clean")), (422, "question_not_verifiable"))

            self.assertTrue((await self._update(db, visible, verified=True))["verified"])
            self.assertEqual(await self._code(self._update(db, voice, verified=True)), (422, "question_not_verifiable"))


if __name__ == "__main__":
    unittest.main()
