"""好友亲密度与好友成就（docs/intimacy.md）。"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import Depends, FastAPI
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.models  # noqa: F401
from app.api.v1.router import api_router
from app.core import localtime
from app.core.database import Base, configure_sqlite_connection, get_db
from app.core.dependencies import get_current_admin, get_current_user
from app.models.conversation import Conversation, ConversationMember
from app.models.enums import ConversationType, MemberRole, MessageType, PlatformRole
from app.models.friend import Friendship
from app.models.gift import GiftRecord
from app.models.intimacy import FriendBadge, FriendBond, FriendBondDay
from app.models.message import Message
from app.models.report import AdminAuditLog
from app.models.system import SystemSetting
from app.models.user import User
from app.services import intimacy as svc
from app.services import intimacy_catalog as catalog
from app.websocket.manager import manager

LOCAL = timezone(timedelta(hours=8))
# 2026-10-14 是周三
D = date(2026, 10, 14)
CFG = dict(svc.DEFAULTS)


def local(day: date, hour: int = 12, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=LOCAL).astimezone(UTC)


class PureTests(unittest.TestCase):
    def test_chat_target_needs_both_sides(self) -> None:
        self.assertEqual(svc.chat_target(50, 0, CFG), 0)
        self.assertEqual(svc.chat_target(0, 50, CFG), 0)
        self.assertEqual(svc.chat_target(1, 1, CFG), 6)
        self.assertEqual(svc.chat_target(10, 3, CFG), 8)
        self.assertEqual(svc.chat_target(500, 500, CFG), CFG["chat_cap"])

    def test_gift_points(self) -> None:
        self.assertEqual(svc.gift_points(1, CFG), 3)
        self.assertEqual(svc.gift_points(20, CFG), 5)
        self.assertEqual(svc.gift_points(300, CFG), 33)

    def test_streak(self) -> None:
        self.assertEqual(svc.next_streak(0, None, D), 1)
        self.assertEqual(svc.next_streak(4, D - timedelta(days=1), D), 5)
        self.assertEqual(svc.next_streak(4, D, D), 4)
        self.assertEqual(svc.next_streak(4, D - timedelta(days=2), D), 1)
        self.assertEqual([svc.current_streak(5, day, D) for day in (D, D - timedelta(days=1), D - timedelta(days=2), None)], [5, 5, 0, 0])

    def test_levels(self) -> None:
        self.assertEqual(catalog.level_for(0).level, 0)
        self.assertEqual(catalog.level_for(19).level, 0)
        self.assertEqual(catalog.level_for(20).level, 1)
        self.assertEqual(catalog.level_for(10**9).level, catalog.MAX_LEVEL)
        thresholds = [lv.threshold for lv in catalog.LEVELS]
        self.assertEqual(thresholds, sorted(thresholds))
        self.assertIsNone(catalog.next_level(catalog.MAX_LEVEL))

    def test_catalog_is_well_formed(self) -> None:
        keys = [series.key for series in catalog.SERIES]
        self.assertEqual(len(keys), len(set(keys)))
        stats = set(svc.bond_stats(svc._new_bond(1, 2, datetime.now(UTC)), 0))
        for series in catalog.SERIES:
            self.assertIn(series.stat, stats, series.key)
            self.assertEqual([lv.level for lv in series.levels], list(range(1, len(series.levels) + 1)), series.key)
            thresholds = [lv.threshold for lv in series.levels]
            self.assertEqual(thresholds, sorted(set(thresholds)), series.key)
            self.assertTrue(all(1 <= lv.tier <= 4 for lv in series.levels), series.key)
            self.assertIn("{n}", series.condition, series.key)
        self.assertGreaterEqual(catalog.TOTAL_BADGES, 40)
        self.assertEqual(catalog.reached(catalog.SERIES_BY_KEY["clover"], 29), 0)
        self.assertEqual(catalog.reached(catalog.SERIES_BY_KEY["clover"], 30), 1)
        self.assertEqual(catalog.SERIES_BY_KEY["clover"].levels[0].name, "友谊幸运草")


class IntimacyBase(unittest.IsolatedAsyncioTestCase):
    """alice、bob 是好友（私聊 direct），alice、carol 是好友（私聊 direct2），dave 和 alice 不是好友但有私聊（direct3）。"""

    async def asyncSetUp(self) -> None:
        asyncio.get_running_loop().set_debug(False)
        svc.reset_runtime_state()
        self._events = patch.object(manager, "send_to_user", new=AsyncMock())
        self.send_to_user = self._events.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{Path(self._tmp.name, 'db.sqlite').as_posix()}")
        event.listen(self.engine.sync_engine, "connect", configure_sqlite_connection)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        svc.session_factory = self.sessions
        tables = [m.__table__ for m in (User, Friendship, Conversation, ConversationMember, Message, GiftRecord, FriendBond, FriendBondDay, FriendBadge, SystemSetting, AdminAuditLog)]
        async with self.engine.begin() as conn:
            await conn.run_sync(lambda sync: Base.metadata.create_all(sync, tables=tables))
        self.now = local(D, 12)
        self._clock = patch.object(localtime, "_now", side_effect=lambda: self.now)
        self._clock.start()
        async with self.sessions() as db:
            users = [User(username=name, hashed_password="x") for name in ("alice", "bob", "carol", "dave", "admin")]
            users[-1].platform_role = PlatformRole.ADMIN
            db.add_all(users)
            await db.flush()
            self.ids = {user.username: user.id for user in users}
            a, b, c, d = (self.ids[n] for n in ("alice", "bob", "carol", "dave"))
            since = local(D - timedelta(days=10))
            for x, y in ((a, b), (a, c)):
                db.add_all([Friendship(user_id=x, friend_id=y, created_at=since), Friendship(user_id=y, friend_id=x, created_at=since)])
            self.conv = {}
            for name, (x, y) in {"bob": (a, b), "carol": (a, c), "dave": (a, d)}.items():
                low, high = sorted((x, y))
                conv = Conversation(type=ConversationType.DIRECT, direct_key=f"{low}:{high}")
                db.add(conv)
                await db.flush()
                db.add_all([ConversationMember(conversation_id=conv.id, user_id=x, role=MemberRole.MEMBER), ConversationMember(conversation_id=conv.id, user_id=y, role=MemberRole.MEMBER)])
                self.conv[name] = conv.id
            group = Conversation(type=ConversationType.GROUP, title="群")
            db.add(group)
            await db.flush()
            self.group = group.id
            await db.commit()

    async def asyncTearDown(self) -> None:
        await svc.drain()
        svc.session_factory = None
        self._clock.stop()
        self._events.stop()
        await self.engine.dispose()
        self._tmp.cleanup()

    async def say(self, who: str, friend: str, count: int = 1, at: datetime | None = None, kind: MessageType = MessageType.TEXT) -> svc.Outcome | None:
        outcome = None
        for _ in range(count):
            async with self.sessions() as db:
                outcome = await svc.record_message(db, self.conv[friend], self.ids[who], kind, at=at or self.now)
        return outcome

    async def bond(self, x: str = "alice", y: str = "bob") -> FriendBond | None:
        low, high = sorted((self.ids[x], self.ids[y]))
        async with self.sessions() as db:
            return (await db.execute(select(FriendBond).where(FriendBond.user_low_id == low, FriendBond.user_high_id == high))).scalar_one_or_none()

    async def badges(self, x: str = "alice", y: str = "bob") -> set[tuple[str, int]]:
        bond = await self.bond(x, y)
        if bond is None:
            return set()
        async with self.sessions() as db:
            return {tuple(row) for row in (await db.execute(select(FriendBadge.series, FriendBadge.level).where(FriendBadge.bond_id == bond.id))).all()}

    async def gift(self, sender: str, to: str, amount: int, *, on_birthday: bool = False, at: datetime | None = None) -> svc.Outcome | None:
        async with self.sessions() as db:
            record = GiftRecord(kind="gift", from_user_id=self.ids[sender], to_user_id=self.ids[to], amount=amount, count=1, on_birthday=on_birthday, created_at=at or self.now)
            db.add(record)
            await db.commit()
            record_id = record.id
        async with self.sessions() as db:
            return await svc.record_gift(db, record_id)



class ServiceTests(IntimacyBase):
    # ------------------------------------------------------------ 聊天

    async def test_one_sided_chat_gives_nothing(self) -> None:
        outcome = await self.say("alice", "bob", 40)
        bond = await self.bond()
        self.assertEqual((bond.intimacy, bond.messages_total, bond.messages_low + bond.messages_high, bond.mutual_days), (0, 40, 40, 0))
        self.assertEqual(outcome.gained, 0)
        # 一方发了 40 条也解锁「友谊幸运草」：那一系列只看互发总数
        self.assertIn(("clover", 1), await self.badges())

    async def test_mutual_chat_points_and_cap(self) -> None:
        await self.say("alice", "bob", 3)
        outcome = await self.say("bob", "bob", 1)
        bond = await self.bond()
        # bob 一开口：+5（双方都开口）+1（一来一回）
        self.assertEqual(bond.intimacy, 6)
        self.assertEqual(outcome.gained, 6)
        self.assertEqual(bond.mutual_days, 1)
        await self.say("bob", "bob", 2)
        self.assertEqual((await self.bond()).intimacy, 8)
        # 再多也只到聊天上限
        await self.say("alice", "bob", 100)
        await self.say("bob", "bob", 100)
        bond = await self.bond()
        self.assertEqual(bond.intimacy, CFG["chat_cap"])
        self.assertEqual(bond.best_day_messages, 206)
        self.assertIn(("chatter", 1), await self.badges())
        self.assertIn(("clover", 1), await self.badges())

    async def test_non_friend_and_group_and_system_do_not_count(self) -> None:
        async with self.sessions() as db:
            self.assertIsNone(await svc.record_message(db, self.conv["dave"], self.ids["alice"], MessageType.TEXT))
            self.assertIsNone(await svc.record_message(db, self.group, self.ids["alice"], MessageType.TEXT))
            self.assertIsNone(await svc.record_message(db, self.conv["bob"], self.ids["alice"], MessageType.SYSTEM))
            self.assertIsNone(await svc.record_message(db, self.conv["bob"], self.ids["alice"], MessageType.GIFT))
            # 不是会话成员的人（direct_key 里没有他）
            self.assertIsNone(await svc.record_message(db, self.conv["bob"], self.ids["carol"], MessageType.TEXT))
        async with self.sessions() as db:
            self.assertEqual(await db.scalar(select(func.count()).select_from(FriendBond)), 0)

    async def test_disabled_stops_accrual(self) -> None:
        async with self.sessions() as db:
            await svc.admin_update_settings(db, await db.get(User, self.ids["admin"]), {"enabled": False})
        await self.say("alice", "bob", 3)
        await self.say("bob", "bob", 3)
        self.assertIsNone(await self.bond())

    async def test_streak_night_dawn_weekend_newyear(self) -> None:
        # 连续 3 天互相聊，第 3 天起有连续互动加成
        for offset in range(3):
            day = D + timedelta(days=offset)
            self.now = local(day, 12)
            await self.say("alice", "bob", 1)
            outcome = await self.say("bob", "bob", 1)
        bond = await self.bond()
        self.assertEqual((bond.streak_days, bond.best_streak, bond.mutual_days), (3, 3, 3))
        self.assertEqual(outcome.gained, 6 + CFG["streak_bonus"])
        self.assertIn(("streak", 1), await self.badges())
        # 断一天：重新从 1 开始，最好纪录留着
        self.now = local(D + timedelta(days=4), 12)
        await self.say("alice", "bob", 1)
        await self.say("bob", "bob", 1)
        bond = await self.bond()
        self.assertEqual((bond.streak_days, bond.best_streak), (1, 3))
        # 深夜：双方都在 0–5 点说过话
        night = local(D + timedelta(days=5), 2)
        await self.say("alice", "bob", 1, at=night)
        self.assertEqual((await self.bond()).night_days, 0)
        await self.say("bob", "bob", 1, at=night)
        self.assertEqual((await self.bond()).night_days, 1)
        self.assertIn(("night", 1), await self.badges())
        # 清晨
        dawn = local(D + timedelta(days=6), 6)
        await self.say("alice", "bob", 1, at=dawn)
        await self.say("bob", "bob", 1, at=dawn)
        self.assertEqual((await self.bond()).dawn_days, 1)
        # 10-17、10-18 是周末；元旦
        await self.say("alice", "bob", 1, at=local(date(2026, 10, 18), 10))
        await self.say("bob", "bob", 1, at=local(date(2026, 10, 18), 11))
        await self.say("alice", "bob", 1, at=local(date(2027, 1, 1), 0, 5))
        await self.say("bob", "bob", 1, at=local(date(2027, 1, 1), 0, 6))
        bond = await self.bond()
        self.assertEqual((bond.weekend_days, bond.newyear_days), (1, 1))
        self.assertIn(("newyear", 1), await self.badges())

    async def test_utc_plus_8_day_boundary(self) -> None:
        # 北京时间 23:59 和第二天 00:01 是两天
        await self.say("alice", "bob", 1, at=local(D, 23, 59))
        await self.say("bob", "bob", 1, at=local(D + timedelta(days=1), 0, 1))
        bond = await self.bond()
        self.assertEqual(bond.mutual_days, 0)
        async with self.sessions() as db:
            self.assertEqual(await db.scalar(select(func.count()).select_from(FriendBondDay)), 2)

    # ------------------------------------------------------------ 礼物

    async def test_gift_points_cap_and_birthday(self) -> None:
        outcome = await self.gift("alice", "bob", 20)
        self.assertEqual(outcome.gained, 5)
        self.assertIn(("gifts", 1), await self.badges())
        await self.gift("alice", "bob", 300)
        bond = await self.bond()
        self.assertEqual(bond.intimacy, CFG["gift_cap"])
        self.assertEqual(bond.gift_count, 2)
        # 生日：加成不占每日上限，每人每年一次
        outcome = await self.gift("bob", "alice", 1, on_birthday=True)
        self.assertEqual(outcome.gained, CFG["birthday_bonus"])
        outcome = await self.gift("bob", "alice", 1, on_birthday=True)
        self.assertEqual(outcome.gained, 0)
        # 另一方的生日单独算
        outcome = await self.gift("alice", "bob", 1, on_birthday=True)
        self.assertEqual(outcome.gained, CFG["birthday_bonus"])
        bond = await self.bond()
        self.assertEqual(bond.birthday_gifts, 2)
        self.assertIn(("birthday", 1), await self.badges())
        # 明年同一个人的生日又能拿一次
        self.now = local(D + timedelta(days=365))
        outcome = await self.gift("bob", "alice", 1, on_birthday=True, at=self.now)
        self.assertEqual(outcome.gained, 3 + CFG["birthday_bonus"])

    async def test_daily_cap_covers_all_counted_sources(self) -> None:
        await self.say("alice", "bob", 100)
        await self.say("bob", "bob", 100)
        await self.gift("alice", "bob", 1000)
        bond = await self.bond()
        self.assertEqual(bond.intimacy, CFG["daily_cap"])
        async with self.sessions() as db:
            day = (await db.execute(select(FriendBondDay))).scalar_one()
        self.assertEqual((day.chat_points, day.gift_points), (25, 15))

    async def test_gift_to_non_friend(self) -> None:
        self.assertIsNone(await self.gift("alice", "dave", 50))

    # ------------------------------------------------------------ 等级、徽章、事件

    async def test_level_up_and_events(self) -> None:
        await self.say("alice", "bob", 30)
        self.send_to_user.reset_mock()
        async with self.sessions() as db:
            outcome = await svc.record_message(db, self.conv["bob"], self.ids["bob"], MessageType.TEXT, at=self.now)
            await svc.announce(db, outcome)
        # 5 + 1 = 6，还没到 1 级；再聊几轮
        self.assertEqual(outcome.level_after, 0)
        for _ in range(14):
            await self.say("bob", "bob")
        bond = await self.bond()
        self.assertEqual((bond.intimacy, bond.level), (20, 1))
        events = [call.args[1] for call in self.send_to_user.await_args_list]
        self.assertIn(svc.EVENT_UPDATED, events)

    async def test_announce_sends_per_viewer(self) -> None:
        await self.say("alice", "bob", 29)
        self.send_to_user.reset_mock()
        async with self.sessions() as db:
            outcome = await svc.record_message(db, self.conv["bob"], self.ids["bob"], MessageType.TEXT, at=self.now)
            await svc.announce(db, outcome)
        self.assertIn(("clover", 1), outcome.new_badges)
        calls = {(call.args[0], call.args[1]): call.args[2] for call in self.send_to_user.await_args_list}
        a, b = self.ids["alice"], self.ids["bob"]
        self.assertEqual(calls[(a, svc.EVENT_BADGES)]["friend_id"], b)
        self.assertEqual(calls[(b, svc.EVENT_BADGES)]["friend_id"], a)
        self.assertEqual(calls[(a, svc.EVENT_BADGES)]["badges"][0]["name"], "友谊幸运草")
        self.assertEqual(calls[(a, svc.EVENT_UPDATED)]["badge"]["series"], "clover")

    async def test_display_choice_and_seen(self) -> None:
        await self.say("alice", "bob", 30)
        await self.gift("alice", "bob", 1, at=self.now + timedelta(minutes=1))
        async with self.sessions() as db:
            alice = await db.get(User, self.ids["alice"])
            bob = await db.get(User, self.ids["bob"])
            listing = await svc.list_friends(db, alice)
            row = next(item for item in listing["items"] if item["friend"].id == self.ids["bob"])
            self.assertEqual(row["badge_count"], 2)
            self.assertEqual(row["new_badges"], 2)
            # 自动：一样是铜的，挑最近解锁的（礼物那枚）
            self.assertEqual(row["badge"]["series"], "gifts")
            result = await svc.set_display(db, alice, self.ids["bob"], "clover")
            self.assertEqual(result["badge"]["series"], "clover")
            with self.assertRaises(Exception) as caught:
                await svc.set_display(db, alice, self.ids["bob"], "night")
            self.assertEqual(caught.exception.detail["code"], "badge_locked")
            with self.assertRaises(Exception) as caught:
                await svc.set_display(db, alice, self.ids["bob"], "nope")
            self.assertEqual(caught.exception.detail["code"], "invalid_badge")
            # bob 那边不受影响
            listing = await svc.list_friends(db, bob)
            self.assertEqual(listing["items"][0]["badge"]["series"], "gifts")
            await svc.set_display(db, alice, self.ids["bob"], "none")
            listing = await svc.list_friends(db, alice)
            self.assertIsNone(next(item for item in listing["items"] if item["friend"].id == self.ids["bob"])["badge"])
            self.assertEqual((await svc.mark_seen(db, alice, self.ids["bob"]))["cleared"], 2)
            listing = await svc.list_friends(db, alice)
            self.assertEqual(next(item for item in listing["items"] if item["friend"].id == self.ids["bob"])["new_badges"], 0)
            listing = await svc.list_friends(db, bob)
            self.assertEqual(listing["items"][0]["new_badges"], 2)

    async def test_ranking_and_detail(self) -> None:
        await self.say("alice", "carol", 5)
        await self.say("carol", "carol", 5)
        await self.say("alice", "bob", 1)
        await self.say("bob", "bob", 1)
        async with self.sessions() as db:
            alice = await db.get(User, self.ids["alice"])
            listing = await svc.list_friends(db, alice)
            self.assertEqual([item["friend"].username for item in listing["items"]], ["carol", "bob"])
            self.assertEqual([item["rank"] for item in listing["items"]], [1, 2])
            self.assertEqual(listing["items"][0]["intimacy"], 10)
            detail = await svc.friend_detail(db, alice, self.ids["carol"])
            self.assertEqual((detail["rank"], detail["friend_count"], detail["intimacy"]), (1, 2, 10))
            self.assertEqual((detail["today"]["chat"], detail["today"]["messages_mine"], detail["today"]["messages_theirs"]), (10, 5, 5))
            self.assertEqual(detail["friend_days"], 10)
            hidden = next(series for series in detail["series"] if series["key"] == "newyear")
            self.assertFalse(hidden["revealed"])
            self.assertIsNone(hidden["name"])
            self.assertIsNone(hidden["levels"][0]["threshold"])
            clover = next(series for series in detail["series"] if series["key"] == "clover")
            self.assertEqual((clover["current"], clover["level"], clover["next_threshold"]), (10, 0, 30))
            history = await svc.history(db, alice, self.ids["carol"], 7)
            self.assertEqual(len(history["items"]), 7)
            self.assertEqual(history["items"][-1]["gained"], 10)
            # 不是好友
            with self.assertRaises(Exception) as caught:
                await svc.friend_detail(db, alice, self.ids["dave"])
            self.assertEqual(caught.exception.detail["code"], "not_friend")

    async def test_unfriend_keeps_data_but_stops(self) -> None:
        await self.say("alice", "bob", 2)
        await self.say("bob", "bob", 2)
        async with self.sessions() as db:
            await db.execute(Friendship.__table__.delete().where(Friendship.user_id.in_([self.ids["alice"], self.ids["bob"]]), Friendship.friend_id.in_([self.ids["alice"], self.ids["bob"]])))
            await db.commit()
        before = (await self.bond()).intimacy
        await self.say("alice", "bob", 5)
        await self.say("bob", "bob", 5)
        self.assertEqual((await self.bond()).intimacy, before)

    async def test_friend_days_sweep(self) -> None:
        # 成为好友第 29 天：还不够 30 天
        self.now = local(D + timedelta(days=19))
        async with self.sessions() as db:
            self.assertEqual(await svc.sweep_friend_days(db, now=self.now), 0)
        self.now = local(D + timedelta(days=20))
        async with self.sessions() as db:
            self.assertEqual(await svc.sweep_friend_days(db, now=self.now), 2)
            # 再扫一遍没有新的
            self.assertEqual(await svc.sweep_friend_days(db, now=self.now), 0)
        self.assertEqual(await self.badges(), {("oldfriend", 1)})
        self.assertEqual(await self.badges("alice", "carol"), {("oldfriend", 1)})

    async def test_concurrent_messages_count_once_each(self) -> None:
        async def one(who: str) -> None:
            async with self.sessions() as db:
                await svc.record_message(db, self.conv["bob"], self.ids[who], MessageType.TEXT, at=self.now)

        await asyncio.gather(*(one("alice" if index % 2 else "bob") for index in range(40)))
        bond = await self.bond()
        self.assertEqual((bond.messages_total, bond.mutual_days, bond.intimacy), (40, 1, 25))

    async def test_scheduled_tasks(self) -> None:
        svc.schedule_message(self.conv["bob"], self.ids["alice"], MessageType.TEXT, self.now)
        svc.schedule_message(self.conv["bob"], self.ids["bob"], MessageType.TEXT, self.now)
        svc.schedule_message(self.conv["bob"], self.ids["bob"], MessageType.SYSTEM, self.now)
        await svc.drain()
        self.assertEqual((await self.bond()).messages_total, 2)

    # ------------------------------------------------------------ 彩蛋（隐藏成就）

    async def test_easter_eggs(self) -> None:
        # 同一天深夜、清晨都互相聊过：千年之后
        await self.say("alice", "bob", 1, at=local(D, 2))
        await self.say("bob", "bob", 1, at=local(D, 3))
        self.assertNotIn(("sunmoon", 1), await self.badges())
        await self.say("alice", "bob", 1, at=local(D, 6))
        await self.say("bob", "bob", 1, at=local(D, 7))
        self.assertEqual((await self.bond()).sunmoon_days, 1)
        self.assertIn(("sunmoon", 1), await self.badges())
        # 集齐 6 个台面上的系列：和谐之元（隐藏的彩蛋自己不算数）
        owned = {key for key, _level in await self.badges()}
        self.assertEqual(owned & set(catalog.VISIBLE_SERIES), {"night", "dawn"})
        self.assertNotIn(("harmony", 1), await self.badges())
        await self.say("alice", "bob", 30)
        await self.gift("alice", "bob", 1)
        async with self.sessions() as db:
            await svc.admin_adjust(db, await db.get(User, self.ids["admin"]), self.ids["alice"], self.ids["bob"], 1000, "测试")
        await self.say("alice", "bob", 1, at=local(D + timedelta(days=4), 12))
        await self.say("bob", "bob", 1, at=local(D + timedelta(days=4), 12))
        owned = {key for key, _level in await self.badges()}
        # night / dawn / clover / gifts / bond = 5 个，还差一个
        self.assertEqual(len(owned & set(catalog.VISIBLE_SERIES)), 5)
        self.assertNotIn("harmony", owned)
        # 周六日（10-17、10-18）各聊一次，第 4 个周末日解锁「周末见」
        for day in (date(2026, 10, 17), date(2026, 10, 18), date(2026, 10, 24), date(2026, 10, 25)):
            await self.say("alice", "bob", 1, at=local(day, 12))
            await self.say("bob", "bob", 1, at=local(day, 12))
        self.assertIn(("harmony", 1), await self.badges())

    async def test_hidden_series_are_masked(self) -> None:
        async with self.sessions() as db:
            alice = await db.get(User, self.ids["alice"])
            rules = await svc.rules_view(db)
            detail = await svc.friend_detail(db, alice, self.ids["bob"])
        hidden_keys = {series.key for series in catalog.SERIES if series.hidden}
        self.assertGreaterEqual(len(hidden_keys), 7)
        for item in rules["series"]:
            if item["key"] in hidden_keys:
                self.assertEqual((item["name"], item["condition"], item["icon"]), (None, None, svc.HIDDEN_ICON))
                self.assertTrue(all(level["name"] is None and level["threshold"] is None for level in item["levels"]))
        for item in detail["series"]:
            if item["key"] in hidden_keys:
                self.assertEqual((item["revealed"], item["icon"], item["current"]), (False, svc.HIDDEN_ICON, None))
        # 台面上的名字里不出现小马宝莉的角色名
        # 成就名里不出现小马宝莉的角色名和名场面原词（彩蛋也只用冷门的说法）
        all_text = "".join(series.name + "".join(level.name for level in series.levels) for series in catalog.SERIES)
        for word in ("露娜", "塞拉斯蒂娅", "碧琪", "萍琪", "珍奇", "云宝", "小马", "公主", "彩虹音爆", "无序", "友谊就是魔法"):
            self.assertNotIn(word, all_text)

    # ------------------------------------------------------------ 回填

    async def test_backfill(self) -> None:
        a, b = self.ids["alice"], self.ids["bob"]
        async with self.sessions() as db:
            for offset in range(3):
                day = D - timedelta(days=5 - offset)
                db.add(Message(conversation_id=self.conv["bob"], sender_id=a, type=MessageType.TEXT, content="hi", created_at=local(day, 1)))
                db.add(Message(conversation_id=self.conv["bob"], sender_id=b, type=MessageType.TEXT, content="hi", created_at=local(day, 2)))
            # 成为好友之前的、系统消息、撤回的不算
            db.add(Message(conversation_id=self.conv["bob"], sender_id=a, type=MessageType.TEXT, content="old", created_at=local(D - timedelta(days=30))))
            db.add(Message(conversation_id=self.conv["bob"], sender_id=a, type=MessageType.SYSTEM, content="sys", created_at=local(D - timedelta(days=4))))
            db.add(Message(conversation_id=self.conv["bob"], sender_id=a, type=MessageType.TEXT, content="x", created_at=local(D - timedelta(days=4)), recalled_at=local(D)))
            db.add(GiftRecord(kind="gift", from_user_id=a, to_user_id=b, amount=20, count=1, on_birthday=True, created_at=local(D - timedelta(days=3))))
            await db.commit()
        async with self.sessions() as db:
            self.assertTrue(await svc.backfill_once(db, now=self.now))
        async with self.sessions() as db:
            self.assertFalse(await svc.backfill_once(db, now=self.now))
        bond = await self.bond()
        self.assertEqual((bond.messages_total, bond.mutual_days, bond.night_days, bond.best_streak, bond.gift_count, bond.birthday_gifts), (6, 3, 3, 3, 1, 1))
        # 3 天各 6 + 第 3 天连续加成 3 + 礼物 5 + 生日 30
        self.assertEqual(bond.intimacy, 18 + 3 + 5 + 30)
        self.assertTrue({("night", 1), ("streak", 1), ("gifts", 1), ("birthday", 1)} <= await self.badges())
        # 再跑一次不会重复加
        async with self.sessions() as db:
            stats = await svc.backfill(db, now=self.now)
        self.assertEqual(stats["pairs"], 0)
        self.assertEqual((await self.bond()).intimacy, bond.intimacy)


class ApiTests(IntimacyBase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.app = FastAPI()
        self.app.include_router(api_router)
        self.as_user = "alice"

        async def override_db():
            async with self.sessions() as db:
                yield db

        async def override_user(db: Annotated[AsyncSession, Depends(get_db)]) -> User:
            return await db.get(User, self.ids[self.as_user])

        self.app.dependency_overrides[get_db] = override_db
        self.app.dependency_overrides[get_current_user] = override_user
        self.app.dependency_overrides[get_current_admin] = override_user
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://test")

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        await super().asyncTearDown()

    async def test_user_endpoints(self) -> None:
        await self.say("alice", "bob", 30)
        await self.say("bob", "bob", 1)
        rules = (await self.client.get("/api/v1/intimacy/rules")).json()
        self.assertTrue(rules["enabled"])
        self.assertEqual(rules["total_badges"], catalog.TOTAL_BADGES)
        self.assertIsNone(next(s for s in rules["series"] if s["key"] == "newyear")["name"])
        listing = (await self.client.get("/api/v1/intimacy/friends")).json()
        self.assertEqual(listing["items"][0]["friend"]["username"], "bob")
        self.assertEqual(listing["items"][0]["badge"]["series"], "clover")
        bob = self.ids["bob"]
        detail = await self.client.get(f"/api/v1/intimacy/friends/{bob}")
        self.assertEqual(detail.status_code, 200, detail.text)
        self.assertEqual(detail.json()["level"]["name"], "点头之交")
        response = await self.client.put(f"/api/v1/intimacy/friends/{bob}/badge", json={"badge": "none"})
        self.assertEqual(response.json()["display"], "none")
        response = await self.client.post(f"/api/v1/intimacy/friends/{bob}/seen")
        self.assertEqual(response.json()["cleared"], 1)
        response = await self.client.get(f"/api/v1/intimacy/friends/{bob}/history?days=3")
        self.assertEqual(len(response.json()["items"]), 3)
        response = await self.client.get(f"/api/v1/intimacy/friends/{self.ids['dave']}")
        self.assertEqual((response.status_code, response.json()["detail"]["code"]), (404, "not_friend"))

    async def test_admin_endpoints(self) -> None:
        self.as_user = "admin"
        await self.say("alice", "bob", 3)
        await self.say("bob", "bob", 3)
        overview = (await self.client.get("/api/v1/admin/intimacy/overview")).json()
        self.assertEqual((overview["friend_pairs"], overview["bonds"]), (2, 1))
        self.assertEqual(sum(level["count"] for level in overview["levels"]), 2)
        response = await self.client.put("/api/v1/admin/intimacy/settings", json={"daily_cap": 0})
        self.assertEqual((response.status_code, response.json()["detail"]["field"]), (422, "daily_cap"))
        response = await self.client.put("/api/v1/admin/intimacy/settings", json={"daily_cap": 60})
        self.assertEqual(response.json()["daily_cap"], 60)
        bonds = (await self.client.get(f"/api/v1/admin/intimacy/users/{self.ids['alice']}")).json()
        self.assertEqual(bonds["total"], 2)
        self.assertNotIn("messages_total", bonds["items"][0])
        response = await self.client.post(
            "/api/v1/admin/intimacy/adjust", json={"user_id": self.ids["alice"], "friend_id": self.ids["bob"], "delta": 1000, "note": "补偿"}
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["intimacy"], 1008)
        # 只给数量，不给是哪几枚
        self.assertNotIn("new_badges", response.json())
        self.assertGreaterEqual(response.json()["new_badge_count"], 1)
        response = await self.client.post("/api/v1/admin/intimacy/adjust", json={"user_id": self.ids["alice"], "friend_id": self.ids["dave"], "delta": 5, "note": "x"})
        self.assertEqual(response.status_code, 404)
        async with self.sessions() as db:
            actions = (await db.execute(select(AdminAuditLog.action))).scalars().all()
        self.assertIn("intimacy.adjust", actions)
        self.assertIn("intimacy.settings.update", actions)


if __name__ == "__main__":
    unittest.main()
