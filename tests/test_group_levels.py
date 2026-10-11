from __future__ import annotations

import json
import unittest

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.models  # noqa: F401
from app.core import group_levels
from app.core.database import Base
from app.models.conversation import Conversation, ConversationMember
from app.models.enums import ConversationType, MemberRole, MessageType
from app.models.message import Message
from app.models.user import User
from app.schemas.conversation import GroupSettingsUpdate, LevelTitle
from app.services import conversations as conversation_service
from app.services import group_activity as activity_service
from app.services.conversations import normalize_level_titles


class LevelCurveTests(unittest.TestCase):
    def test_curve_is_linear_and_capped(self) -> None:
        self.assertEqual(group_levels.exp_required_for_level(1), 40)
        self.assertEqual(group_levels.exp_required_for_level(2), 52)
        self.assertEqual(group_levels.total_exp_for_level(10), 792)
        self.assertEqual(group_levels.total_exp_for_level(20), 2812)
        self.assertEqual(group_levels.total_exp_for_level(100), 62172)
        self.assertEqual(group_levels.level_from_exp(10**9), 100)
        self.assertEqual(group_levels.next_level_remaining_exp(10**9), 0)

    def test_level_and_remaining_agree_at_boundaries(self) -> None:
        self.assertEqual(group_levels.level_from_exp(0), 1)
        self.assertEqual(group_levels.level_from_exp(39), 1)
        self.assertEqual(group_levels.level_from_exp(40), 2)
        self.assertEqual(group_levels.next_level_remaining_exp(0), 40)
        self.assertEqual(group_levels.next_level_remaining_exp(39), 1)
        self.assertEqual(group_levels.next_level_remaining_exp(40), 52)
        for level in (2, 10, 37, 99, 100):
            total = group_levels.total_exp_for_level(level)
            self.assertEqual(group_levels.level_from_exp(total), level)
            self.assertEqual(group_levels.level_from_exp(total - 1), level - 1)

    def test_an_active_member_reaches_level_ten_within_a_week(self) -> None:
        # 每天发满 100 经验的消息 + 一次签到
        daily = activity_service.MESSAGE_EXP_DAILY_LIMIT + activity_service.calculate_checkin_exp(1)
        self.assertGreaterEqual(group_levels.level_from_exp(daily * 7), 10)

    def test_title_lookup(self) -> None:
        titles = group_levels.default_level_titles()
        self.assertEqual(titles[0], {"min_level": 1, "title": "萌新"})
        self.assertEqual(group_levels.level_title_for(1, titles), "萌新")
        self.assertEqual(group_levels.level_title_for(10, titles), "萌新")
        self.assertEqual(group_levels.level_title_for(11, titles), "活跃")
        self.assertEqual(group_levels.level_title_for(100, titles), "殿堂")


class LevelTitleValidationTests(unittest.TestCase):
    def _error(self, items) -> str:
        with self.assertRaises(HTTPException) as caught:
            normalize_level_titles(items)
        self.assertEqual(caught.exception.detail["code"], "invalid_level_titles")
        return caught.exception.detail["message"]

    def test_sorts_and_trims(self) -> None:
        result = normalize_level_titles([{"min_level": 11, "title": " 老手 "}, {"min_level": 1, "title": "新人"}])
        self.assertEqual(result, [{"min_level": 1, "title": "新人"}, {"min_level": 11, "title": "老手"}])
        self.assertEqual(normalize_level_titles([]), [])

    def test_rejects_bad_tiers(self) -> None:
        self.assertIn("1 级开始", self._error([{"min_level": 5, "title": "新人"}]))
        self.assertIn("同一个等级", self._error([{"min_level": 1, "title": "甲"}, {"min_level": 1, "title": "乙"}]))
        self.assertIn("头衔长度", self._error([{"min_level": 1, "title": "   "}]))
        self.assertIn("头衔长度", self._error([{"min_level": 1, "title": "一二三四五六七八九"}]))
        self.assertIn("起始等级", self._error([{"min_level": 1, "title": "甲"}, {"min_level": 101, "title": "乙"}]))
        self.assertIn("最多", self._error([{"min_level": index + 1, "title": "档"} for index in range(13)]))


class LevelSettingsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with self.sessions() as db:
            owner = User(username="owner", hashed_password="x")
            member = User(username="member", hashed_password="x")
            db.add_all([owner, member])
            await db.flush()
            group = Conversation(type=ConversationType.GROUP, title="群")
            db.add(group)
            await db.flush()
            db.add_all(
                [
                    ConversationMember(conversation_id=group.id, user_id=owner.id, role=MemberRole.OWNER),
                    ConversationMember(conversation_id=group.id, user_id=member.id, role=MemberRole.MEMBER),
                ]
            )
            await db.commit()
            self.owner_id, self.member_id, self.group_id = owner.id, member.id, group.id

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def test_titles_are_off_by_default_with_default_tiers_ready(self) -> None:
        async with self.sessions() as db:
            group = await db.get(Conversation, self.group_id)
            read = await conversation_service.build_conversation_read(db, group, self.owner_id)
            self.assertFalse(read.level_titles_enabled)
            self.assertEqual([item.title for item in read.level_titles][:2], ["萌新", "活跃"])

    async def test_staff_can_enable_and_customize_titles(self) -> None:
        async with self.sessions() as db:
            payload = GroupSettingsUpdate(
                level_titles_enabled=True,
                level_titles=[LevelTitle(min_level=21, title="大佬"), LevelTitle(min_level=1, title="新人")],
            )
            group = await conversation_service.update_group_settings(db, self.group_id, self.owner_id, payload)
            self.assertTrue(group.level_titles_enabled)
            self.assertEqual(json.loads(group.level_titles), [{"min_level": 1, "title": "新人"}, {"min_level": 21, "title": "大佬"}])
            read = await conversation_service.build_conversation_read(db, group, self.member_id)
            self.assertEqual([(item.min_level, item.title) for item in read.level_titles], [(1, "新人"), (21, "大佬")])

            # 传空数组恢复默认分档，开关不受影响
            group = await conversation_service.update_group_settings(db, self.group_id, self.owner_id, GroupSettingsUpdate(level_titles=[]))
            self.assertIsNone(group.level_titles)
            self.assertTrue(group.level_titles_enabled)
            read = await conversation_service.build_conversation_read(db, group, self.member_id)
            self.assertEqual(read.level_titles[0].title, "萌新")

            with self.assertRaises(HTTPException) as denied:
                await conversation_service.update_group_settings(db, self.group_id, self.member_id, GroupSettingsUpdate(level_titles_enabled=False))
            self.assertEqual(denied.exception.status_code, 403)

    async def test_messages_grant_faster_exp_up_to_the_daily_cap(self) -> None:
        async with self.sessions() as db:
            member = (
                await db.execute(
                    ConversationMember.__table__.select().where(ConversationMember.user_id == self.member_id)
                )
            ).first()
            row = await db.get(ConversationMember, member.id)
            text = Message(conversation_id=self.group_id, sender_id=self.member_id, content="hi")
            image = Message(conversation_id=self.group_id, sender_id=self.member_id, content="/uploads/messages/a.png", type=MessageType.IMAGE)
            db.add_all([text, image])
            await db.flush()
            await activity_service.record_message_activity(db, self.group_id, row, text)
            self.assertEqual(row.level_exp, 2)
            await activity_service.record_message_activity(db, self.group_id, row, image)
            self.assertEqual(row.level_exp, 5)
            for _ in range(60):
                await activity_service.record_message_activity(db, self.group_id, row, text)
            self.assertEqual(row.level_exp, activity_service.MESSAGE_EXP_DAILY_LIMIT)
            self.assertEqual(row.level, group_levels.level_from_exp(100))
            self.assertEqual(row.next_level_exp, group_levels.next_level_remaining_exp(100))


if __name__ == "__main__":
    unittest.main()
