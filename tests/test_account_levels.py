"""账号等级（docs/account-level.md）。"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
import tempfile
import types
import unittest
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import Depends, FastAPI
from sqlalchemy import event, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.models  # noqa: F401
from app.core.dialect import upsert_insert
from app.api.v1.router import api_router
from app.core.config import settings
from app.core.database import Base, get_db
from app.core.dependencies import get_current_user
from app.models.account_level import AccountLevelDay, AccountLevelNotification
from app.models.case import JuryMember, JuryNotification, JuryReview, JuryReviewVote, JuryVote, ReportCase
from app.models.conversation import Conversation
from app.models.enums import ConversationType, MessageType, PlatformRole, UserStatus
from app.models.group_activity import GroupCheckin, GroupMemberDailyActivity
from app.models.message import Message
from app.models.report import AdminAuditLog
from app.models.system import SystemSetting
from app.models.user import User
from app.schemas.account_level import LevelCard, LevelHistory, LevelLeaderboard, LevelMe, LevelRules
from app.schemas.user import UserPublic
from app.services import account_levels as svc
from app.websocket.manager import manager

LOCAL = timezone(timedelta(hours=8))
D = date(2026, 10, 12)
NAMES = ("alice", "bob", "carol", "dave", "erin", "frank", "admin")
# 只建用得到的表：全量建表在这台机器上要慢得多
TABLES = [
    model.__table__
    for model in (
        User,
        AccountLevelDay,
        AccountLevelNotification,
        Conversation,
        Message,
        GroupMemberDailyActivity,
        GroupCheckin,
        JuryMember,
        ReportCase,
        JuryVote,
        JuryReview,
        JuryReviewVote,
        SystemSetting,
        AdminAuditLog,
    )
]
DAY = AccountLevelDay.__table__
USERS = User.__table__


def local(day: date, hour: int = 12, minute: int = 0, second: int = 0) -> datetime:
    """北京时间的某个时刻，返回 UTC。"""
    return datetime(day.year, day.month, day.day, hour, minute, second, tzinfo=LOCAL).astimezone(UTC)


class PureFunctionTests(unittest.TestCase):
    def test_days_for_level(self) -> None:
        self.assertEqual([svc.days_for_level(level) for level in (0, 1, 4, 16, 32, 64, 256)], [0, 5, 32, 320, 1152, 4352, 66560])

    def test_level_for_days_boundaries(self) -> None:
        cases = {
            0: 0, 4: 0, 4.9: 0, 5: 1, 11.9: 1, 12: 2, 31: 3, 31.9: 3, 32: 4, 319.9: 15, 320: 16,
            4351.9: 63, 4352: 64, 66559.9: 255, 66560: 256, 66561: 256, 1_000_000: 256,
        }  # fmt: skip
        self.assertEqual({days: svc.level_for_days(days) for days in cases}, cases)
        # 每一级的门槛上正好升级，差 0.1 天不升
        for level in range(1, 257):
            needed = svc.days_for_level(level)
            self.assertEqual((svc.level_for_days(needed), svc.level_for_days(needed - 0.1)), (level, level - 1), level)
        self.assertEqual(svc.level_for_days(100, max_level=5), 5)

    def test_icons(self) -> None:
        def icons(level: int) -> tuple[int, int, int, int]:
            value = svc.icons_for_level(level)
            return value["crown"], value["sun"], value["moon"], value["star"]

        expected = {
            0: (0, 0, 0, 0), 1: (0, 0, 0, 1), 3: (0, 0, 0, 3), 4: (0, 0, 1, 0), 5: (0, 0, 1, 1), 15: (0, 0, 3, 3), 16: (0, 1, 0, 0),
            23: (0, 1, 1, 3), 31: (0, 1, 3, 3), 32: (0, 2, 0, 0), 63: (0, 3, 3, 3), 64: (1, 0, 0, 0), 255: (3, 3, 3, 3), 256: (4, 0, 0, 0),
        }  # fmt: skip
        self.assertEqual({level: icons(level) for level in expected}, expected)
        self.assertEqual(list(svc.icons_for_level(0)), ["crown", "sun", "moon", "star"])
        # 拆出来的图标加回去就是等级
        for level in range(257):
            value = svc.icons_for_level(level)
            self.assertEqual(value["crown"] * 64 + value["sun"] * 16 + value["moon"] * 4 + value["star"], level)

    def test_online_tiers(self) -> None:
        r = svc.rules()
        self.assertEqual([r.base_for(minutes) for minutes in (0, 25, 29, 30, 119, 120, 1440)], [0, 0, 0, 5, 5, 10, 10])
        self.assertEqual((r.tick_minutes, r.min_online_minutes, r.cap), (5, 30, 20))

    def test_day_and_slot(self) -> None:
        # UTC 16:00 是北京时间的零点
        self.assertEqual(svc.local_day(datetime(2026, 10, 12, 15, 59, 59, tzinfo=UTC)), date(2026, 10, 12))
        self.assertEqual(svc.local_day(datetime(2026, 10, 12, 16, 0, 0, tzinfo=UTC)), date(2026, 10, 13))
        start, end = svc.day_bounds(D)
        self.assertEqual((start, end), (datetime(2026, 10, 11, 16, tzinfo=UTC), datetime(2026, 10, 12, 16, tzinfo=UTC)))
        base = datetime(2026, 10, 12, 4, 0, 0, tzinfo=UTC)
        self.assertEqual(svc.slot_of(base), int(base.timestamp()) // 300)
        self.assertEqual(svc.slot_of(base + timedelta(seconds=299)), svc.slot_of(base))
        self.assertEqual(svc.slot_of(base + timedelta(seconds=300)), svc.slot_of(base) + 1)
        # 时间片的边界和自然日的边界对齐
        self.assertEqual(int(start.timestamp()) % 300, 0)
        self.assertEqual(svc.seconds_until_next_tick(1000.0), 202.0)
        self.assertEqual(svc.seconds_until_next_tick(1200.0), 302.0)

    def test_streak_helpers(self) -> None:
        yesterday = D - timedelta(days=1)
        self.assertEqual(svc._next_streak(3, yesterday, D), (4, True))
        self.assertEqual(svc._next_streak(3, D - timedelta(days=2), D), (1, True))
        self.assertEqual(svc._next_streak(0, None, D), (1, True))
        self.assertEqual(svc._next_streak(3, D, D), (3, False))
        self.assertEqual(svc._next_streak(3, D + timedelta(days=1), D), (1, False))
        self.assertEqual([svc.current_streak(5, day, D) for day in (D, yesterday, D - timedelta(days=2), None)], [5, 5, 0, 0])

    def test_registered_days(self) -> None:
        self.assertEqual(svc.registered_days(local(D, 0, 0), D), 1)
        self.assertEqual(svc.registered_days(local(D - timedelta(days=1), 23, 59), D), 2)
        self.assertEqual(svc.registered_days(local(D - timedelta(days=150)), D), 151)
        # 库里读出来的时间不带时区，按 UTC 理解
        self.assertEqual(svc.registered_days(datetime(2026, 10, 11, 16, 0), D), 1)

    def test_rules_payload_matches_contract(self) -> None:
        self.assertEqual(
            LevelRules(**svc.rules_payload()).model_dump(mode="json"),
            {
                "max_level": 256,
                "formula": "N² + 4N",
                "icons": [
                    {"key": "crown", "label": "皇冠", "levels": 64},
                    {"key": "sun", "label": "太阳", "levels": 16},
                    {"key": "moon", "label": "月亮", "levels": 4},
                    {"key": "star", "label": "星星", "levels": 1},
                ],
                "daily_cap": 2.0,
                "online": [{"minutes": 120, "days": 1.0}, {"minutes": 30, "days": 0.5}],
                "bonuses": [
                    {"key": "messages", "label": "当天发言满 20 条", "days": 0.3, "target": 20},
                    {"key": "checkin", "label": "当天在任意群签到", "days": 0.1},
                    {"key": "ccw_bound", "label": "已绑定 CCW 账号", "days": 0.1},
                    {"key": "jury", "label": "风纪委员当天参与评审", "days": 0.2},
                    {"key": "streak_7", "label": "连续活跃 7 天", "days": 0.1, "target": 7},
                    {"key": "streak_30", "label": "连续活跃 30 天", "days": 0.3, "target": 30},
                ],
                "milestones": [{"level": 4, "days": 32}, {"level": 16, "days": 320}, {"level": 64, "days": 4352}],
            },
        )

    def test_rules_follow_settings(self) -> None:
        with (
            patch.object(settings, "account_level_bonus_messages_target", 50),
            patch.object(settings, "account_level_daily_cap", 1.5),
            patch.object(settings, "account_level_online_half_minutes", 20),
            patch.object(settings, "account_level_max", 32),
        ):
            payload = svc.rules_payload()
            self.assertEqual((payload["daily_cap"], payload["max_level"], payload["online"][1]), (1.5, 32, {"minutes": 20, "days": 0.5}))
            self.assertEqual((payload["bonuses"][0]["label"], payload["bonuses"][0]["target"]), ("当天发言满 50 条", 50))
            self.assertEqual(payload["milestones"], [{"level": 4, "days": 32}, {"level": 16, "days": 320}])
            self.assertEqual(svc.level_for_days(100000), 32)


class AccountLevelTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        # IsolatedAsyncioTestCase 默认开 asyncio 调试模式，每次调度都要抓一遍调用栈，慢一个数量级。
        asyncio.get_running_loop().set_debug(False)
        svc.reset_leaderboard_cache()
        # 商店在另一个分支里；合并之后这些测试也不该真的去调它的钩子（None = 导入时当作模块不存在）
        self._no_shop = patch.dict(sys.modules, {svc.SHOP_MODULE: None})
        self._no_shop.start()
        self._events = patch.object(manager, "send_to_user", new=AsyncMock())
        self.send_to_user = self._events.start()
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        await self._create_tables()
        async with self.sessions() as db:
            # 注册时间依次晚一天：排行榜并列时看它
            users = [
                User(username=name, hashed_password="x", created_at=local(D - timedelta(days=200 - index)))
                for index, name in enumerate(NAMES)
            ]
            users[-1].platform_role = PlatformRole.ADMIN
            users.append(User(username="robot", hashed_password="x", is_bot=True, created_at=local(D - timedelta(days=300))))
            db.add_all(users)
            group = Conversation(type=ConversationType.GROUP, title="创作群")
            other_group = Conversation(type=ConversationType.GROUP, title="水群")
            direct = Conversation(type=ConversationType.DIRECT)
            db.add_all([group, other_group, direct])
            await db.commit()
            self.ids = {user.username: user.id for user in users}
            self.group_id, self.other_group_id, self.direct_id = group.id, other_group.id, direct.id
            # 下面的测试里委员的票指向案件 1、2，复审票指向复审 1：先把它们建出来（PG 严格执行外键，SQLite 测试库不查）
            db.add_all([ReportCase(id=case_id, target_key=f"message:{case_id}", target_type="message", target_id=case_id) for case_id in (1, 2)])
            db.add(JuryReview(id=1, kind="appeal", requester_id=users[0].id, expires_at=local(D + timedelta(days=3))))
            await db.commit()
            if db.get_bind().dialect.name == "postgresql":
                # 显式给的 id 不会推进 PG 的自增序列
                for table, last in (("report_cases", 2), ("jury_reviews", 1)):
                    await db.execute(text(f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), {last})"))
                await db.commit()

    async def _create_tables(self) -> None:
        async with self.engine.begin() as connection:
            await connection.run_sync(lambda sync: Base.metadata.create_all(sync, tables=TABLES))

    async def asyncTearDown(self) -> None:
        self._events.stop()
        self._no_shop.stop()
        svc.reset_leaderboard_cache()
        await self.engine.dispose()

    # ------------------------------------------------------------ 工具

    async def _minutes(self, db: AsyncSession, name: str, day: date, minutes: int) -> None:
        """直接把某天的在线分钟设成这个数（不经过在线循环）。"""
        now = local(day, 0, 0)
        statement = upsert_insert(db, DAY).values(user_id=self.ids[name], day=day, online_minutes=minutes, last_slot=0, created_at=now, updated_at=now)
        await db.execute(statement.on_conflict_do_update(index_elements=[DAY.c.user_id, DAY.c.day], set_={"online_minutes": minutes}))
        await db.commit()

    async def _row(self, db: AsyncSession, name: str, day: date = D):
        return (await db.execute(select(DAY).where(DAY.c.user_id == self.ids[name], DAY.c.day == day))).first()

    async def _account(self, db: AsyncSession, name: str) -> tuple[int, int, int, date | None]:
        """(等级, 累计十分之一天, 连续天数, 连续到哪天)"""
        row = (await db.execute(select(USERS).where(USERS.c.id == self.ids[name]))).first()
        return row.account_level, row.account_active_tenths, row.account_streak_days, row.account_streak_day

    async def _set_account(self, db: AsyncSession, name: str, **values) -> None:
        await db.execute(USERS.update().where(USERS.c.id == self.ids[name]).values(**values))
        await db.commit()

    async def _settle(self, db: AsyncSession, *names: str, day: date = D, hour: int = 12) -> list[svc.LevelUp]:
        ups = await svc.settle_users(db, [self.ids[name] for name in names], day=day, now=local(day, hour))
        await svc.announce_level_ups(db, ups)
        return ups

    async def _bonuses(self, db: AsyncSession, name: str, day: date = D) -> dict[str, int]:
        row = await self._row(db, name, day)
        return {key: getattr(row, f"bonus_{key}") for key in svc.BONUS_KEYS if getattr(row, f"bonus_{key}")}

    def _message(self, name: str, when: datetime, *, conversation_id: int | None = None, type: MessageType = MessageType.TEXT) -> Message:
        return Message(conversation_id=conversation_id or self.direct_id, sender_id=self.ids[name], content="hi", type=type, created_at=when)

    def _group_count(self, name: str, day: date, messages: int = 0, checkins: int = 0, *, conversation_id: int | None = None) -> GroupMemberDailyActivity:
        return GroupMemberDailyActivity(
            conversation_id=conversation_id or self.group_id, user_id=self.ids[name], activity_date=day, message_count=messages, checkin_count=checkins
        )

    def _checkin(self, name: str, when: datetime, *, conversation_id: int | None = None) -> GroupCheckin:
        # 群签到自己的日期是 UTC 日期，每个群每个 UTC 日期只能签一次
        return GroupCheckin(conversation_id=conversation_id or self.group_id, user_id=self.ids[name], checkin_date=when.date(), created_at=when)


class OnlineTickTests(AccountLevelTestCase):
    async def test_online_minutes_to_base_three_tiers(self) -> None:
        start = local(D, 10, 0)
        alice = self.ids["alice"]
        async with self.sessions() as db:
            seen: dict[int, tuple[int, int]] = {}
            for tick in range(1, 25):
                stats = await svc.run_tick(db, now=start + timedelta(minutes=5 * (tick - 1)), online_ids={alice})
                self.assertEqual((stats["online"], stats["credited"]), (1, 1))
                row = await self._row(db, "alice")
                seen[tick] = (row.online_minutes, row.base_tenths)
                self.assertEqual((await self._account(db, "alice"))[1], row.earned_tenths)
            # 不到 30 分钟没有；满 30 分钟 0.5；满 2 小时 1.0
            self.assertEqual((seen[5], seen[6], seen[23], seen[24]), ((25, 0), (30, 5), (115, 5), (120, 10)))
            self.assertEqual(await self._account(db, "alice"), (0, 10, 1, D))
            # 之后再在线也不会更多
            for tick in range(25, 40):
                await svc.run_tick(db, now=start + timedelta(minutes=5 * (tick - 1)), online_ids={alice})
            self.assertEqual(((await self._row(db, "alice")).online_minutes, (await self._account(db, "alice"))[1]), (195, 10))

    async def test_same_slot_counts_once(self) -> None:
        now = local(D, 10, 0)
        ids = [self.ids["alice"], self.ids["bob"]]
        async with self.sessions() as db:
            # 多端在线：同一个人在名单里出现几次也只有一行
            self.assertEqual(await svc.add_online_minutes(db, [*ids, ids[0], ids[0]], now=now), (sorted(ids), 2))
            # 同一个时间片再来一次（另一个 worker）：什么都不加
            self.assertEqual(await svc.add_online_minutes(db, ids, now=now + timedelta(seconds=120)), (sorted(ids), 0))
            self.assertEqual(await svc.add_online_minutes(db, ids, now=now + timedelta(seconds=299)), (sorted(ids), 0))
            self.assertEqual((await self._row(db, "alice")).online_minutes, 5)
            # 第二个 worker 的名单里多一个人：只给多出来的那个加
            self.assertEqual((await svc.add_online_minutes(db, [*ids, self.ids["carol"]], now=now + timedelta(seconds=200)))[1], 1)
            # 下一个时间片
            self.assertEqual((await svc.add_online_minutes(db, ids, now=now + timedelta(seconds=300)))[1], 2)
            rows = {name: await self._row(db, name) for name in ("alice", "bob", "carol")}
            self.assertEqual([rows[name].online_minutes for name in rows], [10, 10, 5])
            self.assertEqual(rows["alice"].last_slot, svc.slot_of(now) + 1)
            # 时间倒着走（时钟回拨）也不会重复记
            self.assertEqual((await svc.add_online_minutes(db, ids, now=now))[1], 0)
            self.assertEqual(int((await db.execute(select(func.count()).select_from(DAY))).scalar_one()), 3)

    async def test_bots_and_unknown_users_are_skipped(self) -> None:
        async with self.sessions() as db:
            users, credited = await svc.add_online_minutes(db, [self.ids["alice"], self.ids["robot"], 987654], now=local(D))
            self.assertEqual((users, credited), ([self.ids["alice"]], 1))
            self.assertEqual(await svc.add_online_minutes(db, [self.ids["robot"], 987654], now=local(D, 13)), ([], 0))
            self.assertEqual(await svc.add_online_minutes(db, [], now=local(D, 13)), ([], 0))
            self.assertIsNone(await self._row(db, "robot"))

    async def test_run_tick_reads_online_set_and_skips_settle_when_nothing_credited(self) -> None:
        async with self.sessions() as db:
            await self._minutes(db, "alice", D, 25)
            with patch.object(manager, "online_user_ids_global", new=AsyncMock(return_value={self.ids["alice"]})) as online:
                self.assertEqual(await svc.run_tick(db, now=local(D, 10, 0)), {"online": 1, "credited": 1, "level_ups": 0})
                online.assert_awaited_once_with(fresh=True)
            self.assertEqual((await self._account(db, "alice"))[1], 5)
            # 同一个时间片里的第二个 worker：没记上任何一行，也就不再结算
            with patch.object(svc, "settle_users", new=AsyncMock()) as settle:
                self.assertEqual(await svc.run_tick(db, now=local(D, 10, 1), online_ids={self.ids["alice"]}), {"online": 1, "credited": 0, "level_ups": 0})
                settle.assert_not_awaited()


class TwoWorkerTests(unittest.IsolatedAsyncioTestCase):
    """两个进程连同一个库文件：各自的引擎、各自的连接。"""

    async def asyncSetUp(self) -> None:
        asyncio.get_running_loop().set_debug(False)
        svc.reset_leaderboard_cache()
        self._no_shop = patch.dict(sys.modules, {svc.SHOP_MODULE: None})
        self._no_shop.start()
        self._events = patch.object(manager, "send_to_user", new=AsyncMock())
        self.send_to_user = self._events.start()
        self.directory = tempfile.mkdtemp(prefix="kuke-level-")
        url = f"sqlite+aiosqlite:///{Path(self.directory, 'level.db').as_posix()}"
        self.engines = [create_async_engine(url, connect_args={"timeout": 15}) for _ in range(2)]
        for engine in self.engines:
            if engine.dialect.name != "sqlite":
                # 测试跑在 PostgreSQL 上时（tests/conftest.py）两个引擎连同一个 schema，不需要 PRAGMA
                continue

            @event.listens_for(engine.sync_engine, "connect")
            def _configure(dbapi_connection, _record) -> None:
                cursor = dbapi_connection.cursor()
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA synchronous=NORMAL")
                cursor.close()

        self.workers = [async_sessionmaker(engine, expire_on_commit=False) for engine in self.engines]
        async with self.engines[0].begin() as connection:
            await connection.run_sync(lambda sync: Base.metadata.create_all(sync, tables=TABLES))
        async with self.workers[0]() as db:
            users = [User(username=f"user{index}", hashed_password="x") for index in range(40)]
            db.add_all(users)
            await db.commit()
            self.user_ids = [user.id for user in users]

    async def asyncTearDown(self) -> None:
        self._events.stop()
        self._no_shop.stop()
        for engine in self.engines:
            await engine.dispose()
        shutil.rmtree(self.directory, ignore_errors=True)

    async def _totals(self) -> list[tuple[int, int, int]]:
        async with self.workers[0]() as db:
            rows = await db.execute(
                select(DAY.c.online_minutes, DAY.c.earned_tenths, USERS.c.account_active_tenths).join(USERS, USERS.c.id == DAY.c.user_id).order_by(DAY.c.user_id)
            )
            return [tuple(row) for row in rows.all()]

    async def test_two_workers_tick_concurrently(self) -> None:
        start = local(D, 10, 0)
        for tick in range(24):
            now = start + timedelta(minutes=5 * tick)
            async with self.workers[0]() as first, self.workers[1]() as second:
                stats = await asyncio.gather(
                    svc.run_tick(first, now=now, online_ids=self.user_ids),
                    svc.run_tick(second, now=now + timedelta(seconds=3), online_ids=self.user_ids),
                )
            # 每个人在这个时间片里恰好被记一次，不管是哪个 worker 记的
            self.assertEqual(sum(item["credited"] for item in stats), len(self.user_ids), tick)
        self.assertEqual(await self._totals(), [(120, 10, 10)] * len(self.user_ids))

    async def test_two_workers_settle_concurrently(self) -> None:
        now = local(D, 12, 0)
        async with self.workers[0]() as db:
            for user_id in self.user_ids:
                db.add(AccountLevelDay(user_id=user_id, day=D, online_minutes=120))
            await db.execute(USERS.update().values(ccw_student_oid=USERS.c.username))
            await db.commit()
        for _round in range(3):
            async with self.workers[0]() as first, self.workers[1]() as second:
                await asyncio.gather(svc.settle_users(first, self.user_ids, now=now), svc.settle_users(second, self.user_ids, now=now))
        # 基础 1.0 + 已绑定 CCW 0.1，只记了一次
        self.assertEqual(await self._totals(), [(120, 11, 11)] * len(self.user_ids))

    async def test_two_workers_backfill_concurrently(self) -> None:
        async with self.workers[0]() as db:
            db.add(Conversation(type=ConversationType.GROUP, title="群"))
            await db.flush()
            for user_id in self.user_ids:
                for offset in range(1, 8):
                    db.add(GroupMemberDailyActivity(conversation_id=1, user_id=user_id, activity_date=D - timedelta(days=offset), message_count=3))
            await db.commit()
        async with self.workers[0]() as first, self.workers[1]() as second:
            done = await asyncio.gather(svc.backfill_legacy(first, now=local(D)), svc.backfill_legacy(second, now=local(D)))
        self.assertEqual(done, [True, True])
        async with self.workers[1]() as db:
            totals = (await db.execute(select(USERS.c.account_active_tenths, USERS.c.account_level))).all()
            self.assertEqual([tuple(row) for row in totals], [(70, 1)] * len(self.user_ids))
            self.assertEqual(int((await db.execute(select(func.count()).select_from(SystemSetting))).scalar_one()), 1)


class BonusTests(AccountLevelTestCase):
    async def test_bonuses_need_base_first(self) -> None:
        """条件全满足，但在线不到 30 分钟：什么都没有。满 30 分钟的那一刻一起记上。"""
        alice = self.ids["alice"]
        async with self.sessions() as db:
            db.add_all(
                [
                    self._group_count("alice", D, messages=25),
                    self._checkin("alice", local(D, 9)),
                    JuryMember(user_id=alice, status="active", term_ends_at=local(D + timedelta(days=30))),
                    JuryVote(case_id=1, juror_id=alice, vote="l1", voted_at=local(D, 9, 30)),
                ]
            )
            await self._set_account(db, "alice", ccw_student_oid="oid-alice")
            await self._minutes(db, "alice", D, 25)
            self.assertEqual(await self._settle(db, "alice"), [])
            row = await self._row(db, "alice")
            self.assertEqual((row.base_tenths, row.earned_tenths, row.streak_days, await self._bonuses(db, "alice")), (0, 0, 0, {}))
            # 发言数先记下来了（群消息之后可能被裁掉）
            self.assertEqual(row.message_count, 25)
            self.assertEqual(await self._account(db, "alice"), (0, 0, 0, None))

            await self._minutes(db, "alice", D, 30)
            await self._settle(db, "alice")
            self.assertEqual(await self._bonuses(db, "alice"), {"messages": 3, "checkin": 1, "ccw_bound": 1, "jury": 2})
            row = await self._row(db, "alice")
            self.assertEqual((row.base_tenths, row.earned_tenths, row.streak_days), (5, 12, 1))
            self.assertEqual(await self._account(db, "alice"), (0, 12, 1, D))

            await self._minutes(db, "alice", D, 120)
            await self._settle(db, "alice")
            self.assertEqual(((await self._row(db, "alice")).earned_tenths, (await self._account(db, "alice"))[1]), (17, 17))

    async def test_settle_is_idempotent(self) -> None:
        async with self.sessions() as db:
            db.add_all([self._group_count("alice", D, messages=20), self._checkin("alice", local(D, 9))])
            await self._minutes(db, "alice", D, 120)
            for _ in range(4):
                await self._settle(db, "alice", "bob")
                self.assertEqual(await self._account(db, "alice"), (0, 14, 1, D))
            # 换一个会话、换个时刻再算也一样
        async with self.sessions() as db:
            await self._settle(db, "alice", hour=23)
            self.assertEqual(await self._account(db, "alice"), (0, 14, 1, D))
            self.assertEqual((await self._row(db, "alice")).earned_tenths, 14)
            # 没有日活行的人：什么都不做，也不会建行
            self.assertEqual(await self._account(db, "bob"), (0, 0, 0, None))
            self.assertIsNone(await self._row(db, "bob"))

    async def test_stale_read_does_not_double_credit(self) -> None:
        """两个 worker 都读到了「还没结算」，先写的那个记上，后写的发现行变了就跳过。"""
        async with self.sessions() as db, self.sessions() as other:
            await self._set_account(db, "alice", ccw_student_oid="oid-alice")
            await self._minutes(db, "alice", D, 120)
            original = svc._checkin_users
            raced = []

            async def checkin_then_race(session, user_ids, day):
                result = await original(session, user_ids, day)
                if not raced:
                    raced.append(True)
                    # 这时第一个结算已经读完日活行、还没写；另一个 worker 抢先完成整个结算
                    await svc.settle_users(other, [self.ids["alice"]], day=D, now=local(D))
                return result

            with patch.object(svc, "_checkin_users", new=checkin_then_race):
                self.assertEqual(await svc.settle_users(db, [self.ids["alice"]], day=D, now=local(D)), [])
            self.assertEqual(raced, [True])
            self.assertEqual(await self._account(db, "alice"), (0, 11, 1, D))
            self.assertEqual((await self._row(db, "alice")).earned_tenths, 11)

            # 一批里只有一个人被抢先：整批写对不上数，退回逐行写 —— 被抢先的跳过，其余的照常记上（bob 差 0.5 天升 1 级）
            tomorrow = D + timedelta(days=1)
            await self._set_account(db, "bob", account_active_tenths=45)
            for name in ("alice", "bob", "carol"):
                await self._minutes(db, name, tomorrow, 30)
            raced.clear()

            async def checkin_then_race_tomorrow(session, user_ids, day):
                result = await original(session, user_ids, day)
                if not raced:
                    raced.append(True)
                    await svc.settle_users(other, [self.ids["alice"]], day=tomorrow, now=local(tomorrow))
                return result

            with patch.object(svc, "_checkin_users", new=checkin_then_race_tomorrow), patch.object(svc, "_write_changes", wraps=svc._write_changes) as write:
                ups = await svc.settle_users(db, [self.ids[name] for name in ("alice", "bob", "carol")], day=tomorrow, now=local(tomorrow))
            self.assertEqual(len(write.await_args.args[1]), 3)
            self.assertEqual([(up.user_id, up.level) for up in ups], [(self.ids["bob"], 1)])
            # alice：昨天 1.1 + 今天 0.5 + 0.1；bob：4.5 + 0.5；carol：0.5
            self.assertEqual(await self._account(db, "alice"), (0, 17, 2, tomorrow))
            self.assertEqual(await self._account(db, "bob"), (1, 50, 1, tomorrow))
            self.assertEqual(await self._account(db, "carol"), (0, 5, 1, tomorrow))

    async def test_message_bonus_counts_group_counter_and_private_messages(self) -> None:
        async with self.sessions() as db:
            await self._minutes(db, "bob", D, 120)
            yesterday = D - timedelta(days=1)
            db.add_all(
                [
                    # 群：当天（UTC 日期）的计数，两个群加起来 10 条
                    self._group_count("bob", D, messages=6),
                    self._group_count("bob", D, messages=4, conversation_id=self.other_group_id),
                    # 前一个 UTC 日期的计数里混着昨天的发言，不能算进今天
                    self._group_count("bob", yesterday, messages=50),
                    # 白天的群消息在 messages 里也有，但已经在计数里了，不能再数一遍
                    *[self._message("bob", local(D, 10, index), conversation_id=self.group_id) for index in range(10)],
                    # 北京时间 00:00–08:00 的群消息落在前一个 UTC 日期，计数里分不出来，从 messages 数：4 条
                    *[self._message("bob", local(D, 1, index), conversation_id=self.group_id) for index in range(4)],
                    # 私聊全天从 messages 数：5 条
                    *[self._message("bob", local(D, 11, index)) for index in range(3)],
                    *[self._message("bob", local(D, 2, index)) for index in range(2)],
                    # 不算的：系统消息、昨天最后一秒、明天第一秒、别人发的
                    self._message("bob", local(D, 11, 30), type=MessageType.SYSTEM),
                    self._message("bob", local(yesterday, 23, 59, 59)),
                    self._message("bob", local(D + timedelta(days=1), 0, 0, 0)),
                    self._message("carol", local(D, 11)),
                ]
            )
            await db.commit()
            bob = self.ids["bob"]
            # (群计数, 凌晨那一段, 白天的私聊)
            counts = await svc._message_counts(db, [bob, self.ids["carol"], self.ids["dave"]], D, local(D, 20))
            self.assertEqual({user_id: tuple(value) for user_id, value in counts.items()}, {bob: (10, 6, 3), self.ids["carol"]: (0, 0, 1), self.ids["dave"]: (0, 0, 0)})
            # 只问够不够时，群计数已经够了的人不再去数 messages
            db.add(self._group_count("carol", D, messages=20))
            await db.commit()
            counts = await svc._message_counts(db, [bob, self.ids["carol"]], D, local(D, 20), enough=20)
            self.assertEqual({user_id: tuple(value) for user_id, value in counts.items()}, {bob: (10, 6, 3), self.ids["carol"]: (20, 0, 0)})
            await self._settle(db, "bob", hour=20)
            row = await self._row(db, "bob")
            self.assertEqual((row.message_count, row.night_messages, row.bonus_messages, row.earned_tenths), (19, 6, 0, 10))

            # 第 20 条
            db.add(self._message("bob", local(D, 20, 30)))
            await db.commit()
            await self._settle(db, "bob", hour=21)
            row = await self._row(db, "bob")
            self.assertEqual((row.message_count, row.bonus_messages, row.earned_tenths), (20, 3, 13))
            self.assertEqual((await self._account(db, "bob"))[1], 13)

    async def test_message_count_is_remembered_after_pruning(self) -> None:
        """凌晨发的群消息之后被裁掉了：之前结算时记下的数还在。"""
        async with self.sessions() as db:
            await self._minutes(db, "carol", D, 10)
            night = [self._message("carol", local(D, 0, 30, index), conversation_id=self.group_id) for index in range(20)]
            db.add_all(night)
            await db.commit()
            await self._settle(db, "carol", hour=1)
            self.assertEqual(((await self._row(db, "carol")).message_count, (await self._row(db, "carol")).earned_tenths), (20, 0))
            await db.execute(Message.__table__.delete().where(Message.__table__.c.sender_id == self.ids["carol"]))
            await db.commit()
            await self._minutes(db, "carol", D, 30)
            await self._settle(db, "carol", hour=19)
            row = await self._row(db, "carol")
            self.assertEqual((row.message_count, row.bonus_messages, row.earned_tenths), (20, 3, 8))

    async def test_night_messages_still_count_after_pruning(self) -> None:
        """凌晨在热闹的群里说了 10 句，白天这些消息被裁掉了，晚上又说了 12 句：一共 22 句，不能只算成 12。"""
        async with self.sessions() as db:
            await self._minutes(db, "dave", D, 120)
            db.add_all([self._message("dave", local(D, 0, 30, index), conversation_id=self.group_id) for index in range(10)])
            db.add_all([self._message("dave", local(D, 7, 59, index)) for index in range(2)])
            await db.commit()
            await self._settle(db, "dave", hour=8)
            row = await self._row(db, "dave")
            self.assertEqual((row.message_count, row.night_messages, row.bonus_messages), (12, 12, 0))
            # 白天：群里的旧消息被裁掉；又发了 5 条群消息（记在群计数里）和 2 条私聊
            await db.execute(Message.__table__.delete().where(Message.__table__.c.conversation_id == self.group_id))
            db.add_all([self._group_count("dave", D, messages=5), self._message("dave", local(D, 15)), self._message("dave", local(D, 15, 1))])
            await db.commit()
            me = await svc.my_level(db, await db.get(User, self.ids["dave"]), now=local(D, 16))
            self.assertEqual((me["today"]["bonuses"][0]["current"], me["today"]["bonuses"][0]["earned"]), (19, False))
            row = await self._row(db, "dave")
            self.assertEqual((row.message_count, row.night_messages, row.bonus_messages), (19, 12, 0))
            # 第 20 条
            await db.execute(GroupMemberDailyActivity.__table__.update().values(message_count=6))
            await db.commit()
            await self._settle(db, "dave", hour=20)
            row = await self._row(db, "dave")
            self.assertEqual((row.message_count, row.night_messages, row.bonus_messages, row.earned_tenths), (20, 12, 3, 13))

    async def test_checkin_bonus(self) -> None:
        async with self.sessions() as db:
            await self._minutes(db, "carol", D, 120)
            # 昨天签的不算
            db.add(self._checkin("carol", local(D - timedelta(days=1), 23, 0)))
            await db.commit()
            await self._settle(db, "carol")
            self.assertEqual((await self._bonuses(db, "carol"), (await self._account(db, "carol"))[1]), ({}, 10))
            # 北京时间早上 7 点半在另一个群签到：群签到把它记在前一个 UTC 日期，但它发生在今天
            db.add(self._checkin("carol", local(D, 7, 30), conversation_id=self.other_group_id))
            await db.commit()
            await self._settle(db, "carol")
            self.assertEqual((await self._bonuses(db, "carol"), (await self._account(db, "carol"))[1]), ({"checkin": 1}, 11))
            # 一次查的人多时走另一条查询（按时间取当天全部签到，再取交集），结果一样
            everyone = [self.ids[name] for name in NAMES]
            for threshold in (0, 100):
                with patch.object(svc, "CHECKIN_FILTER_BY_USER_MAX", threshold):
                    self.assertEqual(await svc._checkin_users(db, everyone, D), {self.ids["carol"]})
                    self.assertEqual(await svc._checkin_users(db, [self.ids["dave"]], D), set())
                    self.assertEqual(await svc._checkin_users(db, everyone, D - timedelta(days=1)), {self.ids["carol"]})
            self.assertEqual(await svc._checkin_users(db, [], D), set())

    async def test_ccw_bonus_and_bonuses_are_not_taken_back(self) -> None:
        async with self.sessions() as db:
            await self._minutes(db, "dave", D, 30)
            await self._settle(db, "dave")
            self.assertEqual((await self._bonuses(db, "dave"), (await self._account(db, "dave"))[1]), ({}, 5))
            await self._set_account(db, "dave", ccw_student_oid="oid-dave")
            await self._settle(db, "dave")
            self.assertEqual((await self._bonuses(db, "dave"), (await self._account(db, "dave"))[1]), ({"ccw_bound": 1}, 6))
            # 解绑之后今天已经拿到的不收回；明天就没有了
            await self._set_account(db, "dave", ccw_student_oid=None)
            await self._settle(db, "dave")
            self.assertEqual((await self._bonuses(db, "dave"), (await self._account(db, "dave"))[1]), ({"ccw_bound": 1}, 6))
            tomorrow = D + timedelta(days=1)
            await self._minutes(db, "dave", tomorrow, 30)
            await self._settle(db, "dave", day=tomorrow)
            self.assertEqual((await self._bonuses(db, "dave", tomorrow), (await self._account(db, "dave"))[1]), ({}, 11))

    async def test_jury_bonus(self) -> None:
        yesterday = D - timedelta(days=1)
        term = local(D + timedelta(days=30))
        async with self.sessions() as db:
            ids = self.ids
            db.add_all(
                [
                    # alice：在任，今天投了票
                    JuryMember(user_id=ids["alice"], status="active", term_ends_at=term),
                    JuryVote(case_id=1, juror_id=ids["alice"], vote="l2", voted_at=local(D, 9)),
                    # bob：在任，今天只是分到了案件 / 回避了，没有投出票
                    JuryMember(user_id=ids["bob"], status="active", term_ends_at=None),
                    JuryVote(case_id=1, juror_id=ids["bob"], vote=None, voted_at=None),
                    JuryVote(case_id=2, juror_id=ids["bob"], vote="recuse", voted_at=None, recused_at=local(D, 9)),
                    # carol：今天投了票，但已经卸任
                    JuryMember(user_id=ids["carol"], status="retired", term_ends_at=term),
                    JuryVote(case_id=1, juror_id=ids["carol"], vote="none", voted_at=local(D, 9)),
                    # dave：任期已经过了
                    JuryMember(user_id=ids["dave"], status="active", term_ends_at=local(D, 8)),
                    JuryVote(case_id=1, juror_id=ids["dave"], vote="none", voted_at=local(D, 7)),
                    # erin：在任，票是昨天投的
                    JuryMember(user_id=ids["erin"], status="active", term_ends_at=term),
                    JuryVote(case_id=1, juror_id=ids["erin"], vote="none", voted_at=local(yesterday, 23, 59)),
                    # frank：投了票但不是委员
                    JuryVote(case_id=1, juror_id=ids["frank"], vote="none", voted_at=local(D, 9)),
                ]
            )
            await db.commit()
            names = ("alice", "bob", "carol", "dave", "erin", "frank")
            for name in names:
                await self._minutes(db, name, D, 30)
            await self._settle(db, *names)
            self.assertEqual({name: (await self._account(db, name))[1] for name in names}, {"alice": 7, "bob": 5, "carol": 5, "dave": 5, "erin": 5, "frank": 5})
            self.assertEqual(await self._bonuses(db, "alice"), {"jury": 2})
            # 申诉复审 / 复议的票也算
            db.add(JuryReviewVote(review_id=1, juror_id=ids["erin"], vote="uphold", voted_at=local(D, 15)))
            await db.commit()
            await self._settle(db, *names, hour=16)
            self.assertEqual((await self._bonuses(db, "erin"), (await self._account(db, "erin"))[1]), ({"jury": 2}, 7))

    async def test_daily_cap(self) -> None:
        async with self.sessions() as db:
            db.add_all([self._group_count("alice", D, messages=30), self._checkin("alice", local(D, 9))])
            await self._set_account(db, "alice", ccw_student_oid="oid-alice")
            await self._minutes(db, "alice", D, 120)
            with patch.object(settings, "account_level_daily_cap", 1.2):
                await self._settle(db, "alice")
                await self._settle(db, "alice")
                # 1.0 + 0.3 + 0.1 + 0.1 = 1.5，按上限只记 1.2
                self.assertEqual(await self._bonuses(db, "alice"), {"messages": 3, "checkin": 1, "ccw_bound": 1})
                self.assertEqual(((await self._row(db, "alice")).earned_tenths, (await self._account(db, "alice"))[1]), (12, 12))
                history = await svc.history(db, self.ids["alice"], days=1, now=local(D))
                self.assertEqual(history["items"], [{"date": D, "online_minutes": 120, "base": 1.0, "bonus": 0.2, "earned": 1.2}])
                me = await svc.my_level(db, await db.get(User, self.ids["alice"]), now=local(D))
                self.assertEqual((me["today"]["earned"], me["today"]["cap"], me["active_days"]), (1.2, 1.2, 1.2))
        # 默认配置下所有加成加满正好是上限 2.0
        r = svc.rules()
        self.assertEqual(max(tenths for _minutes, tenths in r.online) + sum(r.bonus.values()) - r.bonus["streak_7"], r.cap)

    async def test_everything_at_once_is_exactly_the_cap(self) -> None:
        yesterday = D - timedelta(days=1)
        async with self.sessions() as db:
            db.add_all(
                [
                    self._group_count("alice", D, messages=30),
                    self._checkin("alice", local(D, 9)),
                    JuryMember(user_id=self.ids["alice"], status="active", term_ends_at=None),
                    JuryVote(case_id=1, juror_id=self.ids["alice"], vote="l1", voted_at=local(D, 9, 30)),
                ]
            )
            await self._set_account(db, "alice", ccw_student_oid="oid-alice", account_streak_days=40, account_streak_day=yesterday)
            await self._minutes(db, "alice", D, 120)
            await self._settle(db, "alice")
            self.assertEqual(await self._bonuses(db, "alice"), {"messages": 3, "checkin": 1, "ccw_bound": 1, "jury": 2, "streak_30": 3})
            self.assertEqual(((await self._row(db, "alice")).earned_tenths, await self._account(db, "alice")), (20, (0, 20, 41, D)))


class StreakTests(AccountLevelTestCase):
    async def test_streak_grows_resets_and_gives_bonus_on_day_seven(self) -> None:
        async with self.sessions() as db:
            alice = await db.get(User, self.ids["alice"])
            earned: list[int] = []
            for offset in range(8):
                day = D + timedelta(days=offset)
                await self._minutes(db, "alice", day, 30)
                await self._settle(db, "alice", day=day)
                row = await self._row(db, "alice", day)
                earned.append(row.earned_tenths)
                self.assertEqual((row.streak_days, (await self._account(db, "alice"))[2:]), (offset + 1, (offset + 1, day)))
            # 前 6 天只有基础 0.5；第 7 天起多 0.1
            self.assertEqual(earned, [5, 5, 5, 5, 5, 5, 6, 6])
            self.assertEqual(await self._bonuses(db, "alice", D + timedelta(days=5)), {})
            self.assertEqual(await self._bonuses(db, "alice", D + timedelta(days=6)), {"streak_7": 1})
            self.assertEqual((await self._account(db, "alice"))[1], 42)

            # 第 9 天没上线。那天看到的连续天数还是 8（昨天拿到了），不含今天
            day9, day10 = D + timedelta(days=8), D + timedelta(days=9)
            self.assertEqual((await svc.my_level(db, alice, now=local(day9)))["streak"], 8)
            self.assertEqual((await svc.user_level(db, alice.id, now=local(day9)))["streak"], 8)
            # 第 10 天：昨天没拿到，已经断了；今天拿到基础后从 1 重新算，也没有连续加成
            self.assertEqual((await svc.my_level(db, alice, now=local(day10, 9)))["streak"], 0)
            await self._minutes(db, "alice", day10, 120)
            me = await svc.my_level(db, alice, now=local(day10, 12))
            self.assertEqual((me["streak"], me["today"]["base"], me["today"]["earned"]), (1, 1.0, 1.0))
            self.assertEqual((await self._bonuses(db, "alice", day10), await self._account(db, "alice")), ({}, (1, 52, 1, day10)))

    async def test_online_but_under_half_an_hour_breaks_the_streak(self) -> None:
        async with self.sessions() as db:
            await self._set_account(db, "bob", account_streak_days=12, account_streak_day=D - timedelta(days=2))
            # 昨天在线 25 分钟：没拿到基础，不算活跃
            await self._minutes(db, "bob", D - timedelta(days=1), 25)
            await self._settle(db, "bob", day=D - timedelta(days=1))
            await self._minutes(db, "bob", D, 30)
            await self._settle(db, "bob")
            self.assertEqual(await self._account(db, "bob"), (0, 5, 1, D))

    async def test_streak_7_and_streak_30_replace_each_other(self) -> None:
        yesterday = D - timedelta(days=1)
        async with self.sessions() as db:
            for name, streak in (("alice", 5), ("bob", 6), ("carol", 28), ("dave", 29), ("erin", 99), ("frank", 6)):
                await self._set_account(db, name, account_streak_days=streak, account_streak_day=yesterday)
                await self._minutes(db, name, D, 120)
            # frank 的连续记录停在前天：昨天断了
            await self._set_account(db, "frank", account_streak_day=D - timedelta(days=2))
            names = ("alice", "bob", "carol", "dave", "erin", "frank")
            await self._settle(db, *names)
            self.assertEqual(
                {name: (await self._bonuses(db, name), (await self._account(db, name))[2]) for name in names},
                {
                    "alice": ({}, 6),
                    "bob": ({"streak_7": 1}, 7),
                    "carol": ({"streak_7": 1}, 29),
                    # 满 30 天只给 0.3，不再给 0.1
                    "dave": ({"streak_30": 3}, 30),
                    "erin": ({"streak_30": 3}, 100),
                    "frank": ({}, 1),
                },
            )
            self.assertEqual({name: (await self._account(db, name))[1] for name in names}, {"alice": 10, "bob": 11, "carol": 11, "dave": 13, "erin": 13, "frank": 10})
            # 同一天再结算，连续天数不会再涨
            await self._settle(db, *names, hour=23)
            self.assertEqual([(await self._account(db, name))[2] for name in names], [6, 7, 29, 30, 100, 1])
            me = await svc.my_level(db, await db.get(User, self.ids["dave"]), now=local(D, 23))
            states = {item["key"]: item for item in me["today"]["bonuses"]}
            self.assertEqual(states["streak_7"], {"key": "streak_7", "earned": False, "days": 0.1, "current": 30, "target": 7})
            self.assertEqual(states["streak_30"], {"key": "streak_30", "earned": True, "days": 0.3, "current": 30, "target": 30})


class DayBoundaryTests(AccountLevelTestCase):
    async def test_utc_16_is_the_day_boundary(self) -> None:
        alice = self.ids["alice"]
        next_day = D + timedelta(days=1)
        async with self.sessions() as db:
            # UTC 15:55 还是 12 日（北京时间 23:55），UTC 16:00 已经是 13 日
            await svc.run_tick(db, now=datetime(2026, 10, 12, 15, 55, tzinfo=UTC), online_ids=[alice])
            await svc.run_tick(db, now=datetime(2026, 10, 12, 16, 0, tzinfo=UTC), online_ids=[alice])
            await svc.run_tick(db, now=datetime(2026, 10, 12, 16, 5, tzinfo=UTC), online_ids=[alice])
            self.assertEqual(((await self._row(db, "alice", D)).online_minutes, (await self._row(db, "alice", next_day)).online_minutes), (5, 10))

            # 发言按时刻归到自然日：15:59:59 算 12 日，16:00:00 算 13 日
            db.add_all([self._message("alice", datetime(2026, 10, 12, 15, 59, 59, tzinfo=UTC)), self._message("alice", datetime(2026, 10, 12, 16, 0, 0, tzinfo=UTC))])
            db.add_all([self._message("alice", datetime(2026, 10, 12, 16, 0, 1, tzinfo=UTC)), self._checkin("alice", datetime(2026, 10, 12, 15, 59, tzinfo=UTC))])
            await db.commit()
            late = datetime(2026, 10, 12, 16, 10, tzinfo=UTC)
            self.assertEqual(sum((await svc._message_counts(db, [alice], D, late))[alice]), 1)
            self.assertEqual(sum((await svc._message_counts(db, [alice], next_day, late))[alice]), 2)
            self.assertEqual((await svc._checkin_users(db, [alice], D), await svc._checkin_users(db, [alice], next_day)), ({alice}, set()))

    async def test_streak_continues_across_midnight(self) -> None:
        alice = self.ids["alice"]
        async with self.sessions() as db:
            # 23:30–23:55 在线 30 分钟，零点之后接着在线
            for index in range(6):
                await svc.run_tick(db, now=local(D, 23, 30) + timedelta(minutes=5 * index), online_ids=[alice])
            self.assertEqual(await self._account(db, "alice"), (0, 5, 1, D))
            next_day = D + timedelta(days=1)
            for index in range(6):
                stats = await svc.run_tick(db, now=local(next_day, 0, 0) + timedelta(minutes=5 * index), online_ids=[alice])
                self.assertEqual(stats["credited"], 1)
            self.assertEqual(await self._account(db, "alice"), (0, 10, 2, next_day))
            self.assertEqual(((await self._row(db, "alice", D)).earned_tenths, (await self._row(db, "alice", next_day)).streak_days), (5, 2))

    async def test_finalize_day_picks_up_late_bonuses(self) -> None:
        next_day = D + timedelta(days=1)
        async with self.sessions() as db:
            await self._minutes(db, "alice", D, 120)
            await self._minutes(db, "bob", D, 25)
            await self._settle(db, "alice", "bob", hour=18)
            self.assertEqual((await self._account(db, "alice"))[1], 10)
            # 下线之后才满足的：晚上发的言、短暂上线签的到
            db.add_all([self._group_count("alice", D, messages=30), self._checkin("alice", local(D, 23, 50)), self._group_count("bob", D, messages=30)])
            await db.commit()
            self.assertEqual(await svc.finalize_day(db, D, now=local(next_day, 0, 2)), 0)
            self.assertEqual((await self._bonuses(db, "alice"), (await self._row(db, "alice")).earned_tenths), ({"messages": 3, "checkin": 1}, 14))
            self.assertEqual(await self._account(db, "alice"), (0, 14, 1, D))
            # 没拿到基础的人不处理
            self.assertEqual(((await self._row(db, "bob")).earned_tenths, (await self._account(db, "bob"))[1]), (0, 0))
            # 再收尾一次没有变化；也没有因此给第二天建行
            await svc.finalize_day(db, D, now=local(next_day, 0, 7))
            self.assertEqual(await self._account(db, "alice"), (0, 14, 1, D))
            self.assertIsNone(await self._row(db, "alice", next_day))

    async def test_late_finalize_ignores_group_counter(self) -> None:
        """隔了很久才收尾（进程第二天中午才起来）：群计数那一行这时已经混进了第二天凌晨的发言，不再读它。"""
        next_day = D + timedelta(days=1)
        async with self.sessions() as db:
            await self._minutes(db, "alice", D, 120)
            await self._minutes(db, "bob", D, 120)
            db.add_all([self._group_count("alice", D, messages=30), self._group_count("bob", D, messages=30)])
            await db.commit()
            # alice 当天结算过，发言数已经记下；bob 没有
            await self._settle(db, "alice", hour=20)
            await svc.finalize_day(db, D, now=local(next_day, 12))
            self.assertEqual(((await self._account(db, "alice"))[1], (await self._account(db, "bob"))[1]), (13, 10))
            # 宽限以内还读
            await self._minutes(db, "carol", D, 120)
            db.add(self._group_count("carol", D, messages=30))
            await db.commit()
            await svc.finalize_day(db, D, now=local(next_day, 0, 10))
            self.assertEqual((await self._account(db, "carol"))[1], 13)

    async def test_worker_jobs(self) -> None:
        """后台循环的三件事：折算只做一次、跨天给昨天收尾、记在线时长。"""
        now = datetime.now(UTC)
        today = svc.local_day(now)
        yesterday = today - timedelta(days=1)
        async with self.sessions() as db:
            await self._minutes(db, "alice", yesterday, 120)
            await self._set_account(db, "alice", ccw_student_oid="oid-alice")
            with patch.object(svc, "_backfill_done", False), patch.object(svc, "_finalized_day", None):
                await svc._backfill_job(db)
                self.assertTrue(svc._backfill_done)
                await svc._finalize_job(db)
                self.assertEqual(svc._finalized_day, yesterday)
                self.assertEqual((await self._row(db, "alice", yesterday)).earned_tenths, 11)
                with patch.object(svc, "finalize_day", new=AsyncMock()) as finalize, patch.object(svc, "backfill_legacy", new=AsyncMock()) as backfill:
                    await svc._finalize_job(db)
                    await svc._backfill_job(db)
                    finalize.assert_not_awaited()
                    backfill.assert_not_awaited()
                with patch.object(manager, "online_user_ids_global", new=AsyncMock(return_value={self.ids["bob"]})):
                    await svc._tick_job(db)
                self.assertEqual((await self._row(db, "bob", today)).online_minutes, 5)

    async def test_worker_start_and_stop(self) -> None:
        with patch.object(svc, "WORKER_STARTUP_DELAY_SECONDS", 3600):
            svc.start_account_level_worker()
            task = svc._worker_task
            svc.start_account_level_worker()
            self.assertIs(svc._worker_task, task)
            await svc.stop_account_level_worker()
            self.assertTrue(task.cancelled())
            self.assertIsNone(svc._worker_task)
            await svc.stop_account_level_worker()


class LevelUpTests(AccountLevelTestCase):
    async def _notifications(self, db: AsyncSession, name: str) -> list[tuple[int, str]]:
        rows = await db.execute(
            select(AccountLevelNotification.level, AccountLevelNotification.title)
            .where(AccountLevelNotification.user_id == self.ids[name])
            .order_by(AccountLevelNotification.id)
        )
        return [tuple(row) for row in rows.all()]

    async def test_level_up_sends_event_every_level_and_notifies_on_multiples_of_four(self) -> None:
        async with self.sessions() as db:
            # alice 差 0.1 天到 1 级；bob 差 0.5 天到 4 级；carol 今天不升级
            await self._set_account(db, "alice", account_active_tenths=49)
            await self._set_account(db, "bob", account_active_tenths=315, account_level=3)
            await self._set_account(db, "carol", account_active_tenths=100, account_level=1)
            for name in ("alice", "bob", "carol"):
                await self._minutes(db, name, D, 30)
            ups = await self._settle(db, "alice", "bob", "carol")
            self.assertEqual(sorted((up.user_id, up.old_level, up.level, up.active_tenths) for up in ups), sorted([(self.ids["alice"], 0, 1, 54), (self.ids["bob"], 3, 4, 320)]))
            self.assertEqual((await self._account(db, "alice"))[:2], (1, 54))
            self.assertEqual((await self._account(db, "bob"))[:2], (4, 320))
            self.assertEqual((await self._account(db, "carol"))[:2], (1, 105))
            sent = {call.args[0]: call.args[1:] for call in self.send_to_user.await_args_list}
            self.assertEqual(
                sent,
                {
                    self.ids["alice"]: ("account.level.up", {"level": 1, "icons": {"crown": 0, "sun": 0, "moon": 0, "star": 1}, "active_days": 5.4}),
                    self.ids["bob"]: ("account.level.up", {"level": 4, "icons": {"crown": 0, "sun": 0, "moon": 1, "star": 0}, "active_days": 32.0}),
                },
            )
            # 通知中心只在升到 4 的倍数时写
            self.assertEqual(await self._notifications(db, "alice"), [])
            self.assertEqual(await self._notifications(db, "bob"), [(4, "账号等级升到了 4 级")])
            items = await svc.notification_items(db, self.ids["bob"])
            self.assertEqual(
                [(item.id, item.category, item.type, item.title, item.body, item.action, item.payload) for item in items],
                [("account_level:1", "system", "account_level", "账号等级升到了 4 级", "累计活跃 32.0 天，等级图标是 1 个月亮。", "none", {"level": 4, "icons": {"crown": 0, "sun": 0, "moon": 1, "star": 0}})],
            )
            self.assertEqual(await svc.notification_items(db, self.ids["alice"]), [])
            # 再结算一次不会重复发
            self.send_to_user.reset_mock()
            self.assertEqual(await self._settle(db, "alice", "bob", "carol"), [])
            self.send_to_user.assert_not_awaited()
            self.assertEqual(len(await self._notifications(db, "bob")), 1)

    async def test_user_public_reads_the_column(self) -> None:
        async with self.sessions() as db:
            alice = await db.get(User, self.ids["alice"])
            self.assertEqual((alice.account_level, UserPublic.model_validate(alice).account_level), (0, 0))
            self.assertIn("account_level", UserPublic.model_validate(alice).model_dump())
            await self._set_account(db, "alice", account_active_tenths=4350, account_level=19)
            await db.refresh(alice)
            await self._minutes(db, "alice", D, 120)
            me = await svc.my_level(db, alice, now=local(D))
            self.assertEqual((me["level"], alice.account_level, UserPublic.model_validate(alice).account_level), (19, 19, 19))
            # /levels/me 里结算导致升级后，请求里的用户对象也跟着刷新
            await self._set_account(db, "alice", account_active_tenths=4795, account_level=19)
            await self._minutes(db, "alice", D + timedelta(days=1), 30)
            me = await svc.my_level(db, alice, now=local(D + timedelta(days=1)))
            self.assertEqual((me["level"], me["active_days"], alice.account_level, UserPublic.model_validate(alice).account_level), (20, 480.0, 20, 20))
            self.assertEqual(self.send_to_user.await_args.args[2]["level"], 20)
            self.assertEqual(await self._notifications(db, "alice"), [(20, "账号等级升到了 20 级")])

    async def test_level_is_capped(self) -> None:
        async with self.sessions() as db:
            await self._set_account(db, "alice", account_active_tenths=665595, account_level=255)
            await self._minutes(db, "alice", D, 120)
            await self._settle(db, "alice")
            self.assertEqual((await self._account(db, "alice"))[:2], (256, 665605))
            self.assertEqual(self.send_to_user.await_args.args[2], {"level": 256, "icons": {"crown": 4, "sun": 0, "moon": 0, "star": 0}, "active_days": 66560.5})
            # 满级之后累计还在涨，等级不动、没有「下一级」
            await self._set_account(db, "alice", account_active_tenths=900000)
            await self._minutes(db, "alice", D + timedelta(days=1), 120)
            self.send_to_user.reset_mock()
            me = await svc.my_level(db, await db.get(User, self.ids["alice"]), now=local(D + timedelta(days=1)))
            self.assertEqual((me["level"], me["level_days"], me["next_level"], me["next_level_days"], me["days_to_next"]), (256, 66560, None, None, None))
            self.assertEqual(me["active_days"], 90001.0)
            self.send_to_user.assert_not_awaited()
            card = await svc.user_level(db, self.ids["alice"], now=local(D + timedelta(days=1)))
            self.assertEqual((card["level"], card["next_level_days"], card["days_to_next"]), (256, None, None))

    async def test_event_failure_does_not_affect_settlement(self) -> None:
        async with self.sessions() as db:
            await self._set_account(db, "alice", account_active_tenths=315, account_level=3)
            await self._minutes(db, "alice", D, 30)
            self.send_to_user.side_effect = RuntimeError("redis down")
            with self.assertLogs(svc.logger, level="ERROR"):
                ups = await self._settle(db, "alice")
            self.assertEqual(len(ups), 1)
            self.assertEqual((await self._account(db, "alice"))[:2], (4, 320))
            self.assertEqual(len(await self._notifications(db, "alice")), 1)

    async def test_shop_hook_missing_is_fine(self) -> None:
        async with self.sessions() as db:
            await self._set_account(db, "alice", account_active_tenths=49)
            await self._minutes(db, "alice", D, 30)
            # 模块不存在（现在的情况）：安静地跳过，不记错误日志
            with self.assertNoLogs(svc.logger, level="WARNING"):
                self.assertEqual(len(await self._settle(db, "alice")), 1)
            self.assertEqual((await self._account(db, "alice"))[0], 1)
            # 模块在但没有这个函数：同样跳过
            with patch.dict(sys.modules, {svc.SHOP_MODULE: types.ModuleType(svc.SHOP_MODULE)}), self.assertNoLogs(svc.logger, level="WARNING"):
                await svc._shop_on_level_up(db, self.ids["alice"], 1)

    async def test_shop_hook_is_called_and_its_failure_is_contained(self) -> None:
        calls: list[tuple[AsyncSession, int, int]] = []
        shop = types.ModuleType(svc.SHOP_MODULE)

        async def on_level_up(db: AsyncSession, user_id: int, level: int) -> None:
            calls.append((db, user_id, level))
            # 钩子自己不提交也行：调用方会补一次提交
            db.add(SystemSetting(key=f"shop.level.{user_id}", value=str(level)))

        shop.on_level_up = on_level_up
        async with self.sessions() as db:
            await self._set_account(db, "alice", account_active_tenths=49)
            await self._minutes(db, "alice", D, 30)
            with patch.dict(sys.modules, {svc.SHOP_MODULE: shop}):
                await self._settle(db, "alice")
            self.assertEqual(calls, [(db, self.ids["alice"], 1)])
        async with self.sessions() as db:
            self.assertEqual((await db.get(SystemSetting, f"shop.level.{self.ids['alice']}")).value, "1")

            def broken(_db: AsyncSession, _user_id: int, _level: int) -> None:
                _db.add(SystemSetting(key="shop.half-written", value="x"))
                raise RuntimeError("shop exploded")

            shop.on_level_up = broken
            await self._set_account(db, "bob", account_active_tenths=49)
            await self._minutes(db, "bob", D, 30)
            with patch.dict(sys.modules, {svc.SHOP_MODULE: shop}), self.assertLogs(svc.logger, level="ERROR") as logs:
                ups = await self._settle(db, "bob")
            self.assertIn("Shop level-up hook failed", logs.output[0])
            # 结算已经落库；钩子写了一半的东西被回滚
            self.assertEqual((len(ups), (await self._account(db, "bob"))[:2]), (1, (1, 54)))
            self.assertIsNone(await db.get(SystemSetting, "shop.half-written"))
            self.assertEqual(self.send_to_user.await_count, 2)


class BackfillTests(AccountLevelTestCase):
    async def _seed(self, db: AsyncSession) -> None:
        day = lambda offset: D - timedelta(days=offset)  # noqa: E731
        db.add_all(
            [
                # alice：3 个不同的日期。同一天在两个群里发言只算一天；只签到没发言也算；发言和签到都是 0 的不算；今天的不算
                self._group_count("alice", day(1), messages=5),
                self._group_count("alice", day(1), messages=2, conversation_id=self.other_group_id),
                self._group_count("alice", day(2), checkins=1),
                self._group_count("alice", day(3), messages=0, checkins=0),
                self._group_count("alice", day(9), messages=1, checkins=1),
                self._group_count("alice", D, messages=40),
                # bob：40 天 -> 4 级；carol：正好 5 天 -> 1 级；dave：4 天 -> 0 级
                *[self._group_count("bob", day(offset), messages=1) for offset in range(1, 41)],
                *[self._group_count("carol", day(offset), checkins=1) for offset in range(1, 6)],
                *[self._group_count("dave", day(offset), messages=9) for offset in range(1, 5)],
                # erin：只有今天的；frank：什么都没有；机器人不折算
                self._group_count("erin", D, messages=3),
                *[self._group_count("robot", day(offset), messages=100) for offset in range(1, 30)],
            ]
        )
        await db.commit()

    async def test_backfill_counts_distinct_active_days_once(self) -> None:
        names = ("alice", "bob", "carol", "dave", "erin", "frank", "robot")
        async with self.sessions() as db:
            await self._seed(db)
            self.assertTrue(await svc.backfill_legacy(db, now=local(D)))
            expected = {"alice": (0, 30), "bob": (4, 400), "carol": (1, 50), "dave": (0, 40), "erin": (0, 0), "frank": (0, 0), "robot": (0, 0)}
            self.assertEqual({name: (await self._account(db, name))[:2] for name in names}, expected)
            marker = json.loads((await db.get(SystemSetting, svc.BACKFILL_KEY)).value)
            self.assertEqual((marker["users"], marker["at"]), (4, local(D).isoformat()))
            # 折算不建日活行、不动连续天数、不发事件、不写通知
            self.assertEqual(int((await db.execute(select(func.count()).select_from(DAY))).scalar_one()), 0)
            self.assertEqual((await self._account(db, "bob"))[2:], (0, None))
            self.send_to_user.assert_not_awaited()
            self.assertEqual(int((await db.execute(select(func.count()).select_from(AccountLevelNotification))).scalar_one()), 0)

            # 再跑多少次都不会重复加，哪怕这期间又多了记录
            db.add(self._group_count("frank", D - timedelta(days=1), messages=5))
            await db.commit()
            for _ in range(3):
                self.assertTrue(await svc.backfill_legacy(db, now=local(D + timedelta(days=3))))
            self.assertEqual({name: (await self._account(db, name))[:2] for name in names}, expected)
        async with self.sessions() as db:
            self.assertTrue(await svc.backfill_legacy(db))
            self.assertEqual((await self._account(db, "bob"))[:2], (4, 400))
            self.assertEqual((await svc.admin_settings(db))["legacy_backfill_at"], local(D).isoformat())

    async def test_backfill_adds_on_top_and_then_normal_rules_apply(self) -> None:
        async with self.sessions() as db:
            await self._seed(db)
            # 折算之前今天已经攒了一点（上线当天循环先跑了一轮）
            await self._minutes(db, "carol", D, 30)
            await self._settle(db, "carol")
            self.assertEqual((await self._account(db, "carol"))[:2], (0, 5))
            await svc.backfill_legacy(db, now=local(D))
            self.assertEqual((await self._account(db, "carol"))[:2], (1, 55))
            # 之后照常：alice 今天的 40 条发言按新规则给加成，而不是折算成一天
            await self._minutes(db, "alice", D, 120)
            await self._settle(db, "alice")
            self.assertEqual(await self._account(db, "alice"), (0, 43, 1, D))

    async def test_failed_backfill_leaves_no_marker(self) -> None:
        async with self.sessions() as db:
            await self._seed(db)
            with patch.object(svc, "_resync_all_levels", new=AsyncMock(side_effect=RuntimeError("boom"))), self.assertRaises(RuntimeError):
                await svc.backfill_legacy(db, now=local(D))
            self.assertIsNone(await db.get(SystemSetting, svc.BACKFILL_KEY))
            self.assertEqual((await self._account(db, "bob"))[:2], (0, 0))
            # 下一轮重试成功
            self.assertTrue(await svc.backfill_legacy(db, now=local(D)))
            self.assertEqual((await self._account(db, "bob"))[:2], (4, 400))


class LeaderboardTests(AccountLevelTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.now = local(D, 12)
        async with self.sessions() as db:
            # 累计：alice = bob（alice 注册早）> carol > dave；erin 封禁中；frank 的封禁已经到期；admin 没有活跃天
            totals = {"alice": 1000, "bob": 1000, "carol": 500, "dave": 320, "erin": 9000, "frank": 55, "robot": 7000}
            for name, tenths in totals.items():
                await self._set_account(db, name, account_active_tenths=tenths, account_level=svc.level_for_days(tenths / 10))
            await self._set_account(db, "erin", status=UserStatus.DISABLED, banned_until=None, banned_at=self.now)
            await self._set_account(db, "frank", status=UserStatus.DISABLED, banned_until=self.now - timedelta(hours=1))
            # 本月（10 月）新增：dave 3.0 > bob = carol 2.0（bob 注册早）> alice 0.5；9 月 30 日的不算
            gained = {
                "alice": [(date(2026, 10, 1), 5), (date(2026, 9, 30), 20)],
                "bob": [(date(2026, 10, 2), 12), (date(2026, 10, 12), 8)],
                "carol": [(date(2026, 10, 5), 20)],
                "dave": [(date(2026, 10, 1), 15), (date(2026, 10, 11), 15)],
                "erin": [(date(2026, 10, 3), 20)],
                "frank": [(date(2026, 9, 1), 20), (date(2026, 10, 9), 0)],
            }
            for name, days in gained.items():
                for day, tenths in days:
                    db.add(AccountLevelDay(user_id=self.ids[name], day=day, online_minutes=120, base_tenths=min(10, tenths), earned_tenths=tenths))
            await db.commit()

    async def _board(self, db: AsyncSession, name: str, **kwargs) -> dict:
        return await svc.leaderboard(db, await db.get(User, self.ids[name]), now=self.now, **kwargs)

    async def test_all_time(self) -> None:
        async with self.sessions() as db:
            board = await self._board(db, "carol", period="all")
            rows = [(item["rank"], item["user"].username, item["level"], item["active_days"], item["gained"]) for item in board["items"]]
            self.assertEqual(
                rows,
                [(1, "alice", 8, 100.0, 0.5), (2, "bob", 8, 100.0, 2.0), (3, "carol", 5, 50.0, 2.0), (4, "dave", 4, 32.0, 3.0), (5, "frank", 1, 5.5, 0.0)],
            )
            self.assertEqual((board["period"], board["total"], board["generated_at"]), ("all", 5, self.now))
            self.assertEqual((board["me"]["rank"], board["me"]["user"].username, board["me"]["active_days"], board["me"]["gained"]), (3, "carol", 50.0, 2.0))
            self.assertEqual(board["items"][0]["user"].account_level, 8)
            # 封禁中的、机器人、没有活跃天的都不上榜，自己看也是 null
            self.assertIsNone((await self._board(db, "erin"))["me"])
            self.assertIsNone((await self._board(db, "admin"))["me"])
            self.assertEqual(LevelLeaderboard(**board).model_dump(mode="json")["items"][0]["user"]["username"], "alice")

    async def test_month(self) -> None:
        async with self.sessions() as db:
            board = await self._board(db, "alice", period="month")
            rows = [(item["rank"], item["user"].username, item["active_days"], item["gained"]) for item in board["items"]]
            self.assertEqual(rows, [(1, "dave", 32.0, 3.0), (2, "bob", 100.0, 2.0), (3, "carol", 50.0, 2.0), (4, "alice", 100.0, 0.5)])
            self.assertEqual((board["period"], board["total"], board["me"]["rank"], board["me"]["gained"]), ("month", 4, 4, 0.5))
            # 这个月没有新增的人不在月榜上
            self.assertIsNone((await self._board(db, "frank", period="month"))["me"])
            self.assertEqual((await self._board(db, "frank", period="all"))["me"]["rank"], 5)
        # 到了下个月，月榜从头算
        svc.reset_leaderboard_cache()
        async with self.sessions() as db:
            board = await svc.leaderboard(db, await db.get(User, self.ids["alice"]), period="month", now=local(date(2026, 11, 1), 0, 5))
            self.assertEqual((board["items"], board["total"], board["me"]), ([], 0, None))
        # 月初那一刻按北京时间算：UTC 9 月 30 日 15:59 还是 9 月（alice 9 月 30 日那 2.0 天算数），16:00 就是 10 月 1 日
        for moment, expected in (
            (datetime(2026, 9, 30, 15, 59, tzinfo=UTC), [("alice", 2.0)]),
            (datetime(2026, 9, 30, 16, 0, tzinfo=UTC), [("dave", 1.5), ("alice", 0.5)]),
        ):
            svc.reset_leaderboard_cache()
            async with self.sessions() as db:
                board = await svc.leaderboard(db, await db.get(User, self.ids["alice"]), period="month", now=moment)
                self.assertEqual([(item["user"].username, item["gained"]) for item in board["items"]], expected)

    async def test_paging_and_me_outside_the_page(self) -> None:
        async with self.sessions() as db:
            board = await self._board(db, "dave", limit=2, offset=1)
            self.assertEqual([(item["rank"], item["user"].username) for item in board["items"]], [(2, "bob"), (3, "carol")])
            self.assertEqual((board["total"], board["me"]["rank"]), (5, 4))
            self.assertEqual((await self._board(db, "dave", limit=2, offset=10))["items"], [])

    async def test_cache_and_rank(self) -> None:
        async with self.sessions() as db:
            first = await self._board(db, "carol")
            await self._set_account(db, "carol", account_active_tenths=5000, account_level=21)
            # 60 秒内名次和数字来自缓存
            cached = await svc.leaderboard(db, await db.get(User, self.ids["carol"]), now=self.now + timedelta(seconds=30))
            self.assertEqual((cached["me"]["rank"], cached["me"]["active_days"], cached["generated_at"]), (3, 50.0, first["generated_at"]))
            self.assertEqual(await svc.rank_of(db, self.ids["carol"]), 3)
            with patch.object(svc, "_build_board", new=AsyncMock(side_effect=AssertionError("should use cache"))):
                await self._board(db, "alice", period="month")
                me = await svc.my_level(db, await db.get(User, self.ids["carol"]), now=self.now)
                self.assertEqual((me["rank"], me["active_days"]), (3, 500.0))
            # 过期之后重算
            with patch.object(svc.time, "monotonic", return_value=svc.time.monotonic() + 61):
                fresh = await svc.leaderboard(db, await db.get(User, self.ids["carol"]), now=self.now + timedelta(seconds=61))
            self.assertEqual((fresh["me"]["rank"], fresh["me"]["active_days"], fresh["generated_at"]), (1, 500.0, self.now + timedelta(seconds=61)))
            self.assertEqual([await svc.rank_of(db, self.ids[name]) for name in ("carol", "alice", "erin", "admin")], [1, 2, None, None])
            # 缓存时间配成 0 就是不缓存
            svc.reset_leaderboard_cache()
            with patch.object(settings, "account_level_leaderboard_cache_seconds", 0):
                self.assertEqual(await svc.rank_of(db, self.ids["dave"]), 4)
                await self._set_account(db, "dave", account_active_tenths=6000)
                self.assertEqual(await svc.rank_of(db, self.ids["dave"]), 1)

    async def test_level_up_and_admin_adjust_refresh_the_cache(self) -> None:
        async with self.sessions() as db:
            self.assertEqual(await svc.rank_of(db, self.ids["frank"], now=self.now), 5)
            await svc.admin_adjust(db, await db.get(User, self.ids["admin"]), self.ids["frank"], days=1000, note="活动奖励", now=self.now)
            self.assertEqual(await svc.rank_of(db, self.ids["frank"], now=self.now), 1)


class ViewTests(AccountLevelTestCase):
    async def test_my_level_shape(self) -> None:
        async with self.sessions() as db:
            alice = await db.get(User, self.ids["alice"])
            # 631.0 天 = 23 级（621 天），今天再拿 0.5 + 0.1
            await self._set_account(db, "alice", account_active_tenths=6310, account_level=23, account_streak_days=11, account_streak_day=D - timedelta(days=1))
            await self._minutes(db, "alice", D, 95)
            db.add_all([self._checkin("alice", local(D, 9)), self._group_count("alice", D, messages=10), self._message("alice", local(D, 10)), self._message("alice", local(D, 10, 1))])
            await db.commit()
            me = LevelMe(**await svc.my_level(db, alice, now=local(D, 12))).model_dump(mode="json")
            self.assertEqual(
                me,
                {
                    "level": 23,
                    "active_days": 631.7,
                    "level_days": 621,
                    "next_level": 24,
                    "next_level_days": 672,
                    "days_to_next": 40.3,
                    "icons": {"crown": 0, "sun": 1, "moon": 1, "star": 3},
                    "streak": 12,
                    "rank": 1,
                    "today": {
                        "date": "2026-10-12",
                        "online_minutes": 95,
                        "base": 0.5,
                        "bonuses": [
                            {"key": "messages", "earned": False, "days": 0.3, "current": 12, "target": 20},
                            {"key": "checkin", "earned": True, "days": 0.1},
                            {"key": "ccw_bound", "earned": False, "days": 0.1},
                            {"key": "jury", "earned": False, "days": 0.2},
                            {"key": "streak_7", "earned": True, "days": 0.1, "current": 12, "target": 7},
                            {"key": "streak_30", "earned": False, "days": 0.3, "current": 12, "target": 30},
                        ],
                        "earned": 0.7,
                        "cap": 2.0,
                    },
                    "registered_days": 201,
                },
            )
            # 读两次不会多加
            again = await svc.my_level(db, alice, now=local(D, 12, 1))
            self.assertEqual((again["active_days"], again["today"]["earned"]), (631.7, 0.7))

    async def test_my_level_for_a_brand_new_user(self) -> None:
        async with self.sessions() as db:
            newcomer = User(username="newcomer", hashed_password="x", created_at=local(D, 8))
            db.add(newcomer)
            await db.commit()
            me = LevelMe(**await svc.my_level(db, newcomer, now=local(D, 9))).model_dump(mode="json")
            self.assertEqual((me["level"], me["active_days"], me["level_days"], me["next_level"], me["next_level_days"], me["days_to_next"]), (0, 0.0, 0, 1, 5, 5.0))
            self.assertEqual((me["icons"], me["streak"], me["rank"], me["registered_days"]), ({"crown": 0, "sun": 0, "moon": 0, "star": 0}, 0, None, 1))
            self.assertEqual((me["today"]["date"], me["today"]["online_minutes"], me["today"]["base"], me["today"]["earned"]), ("2026-10-12", 0, 0.0, 0.0))
            self.assertEqual([item["earned"] for item in me["today"]["bonuses"]], [False] * 6)
            self.assertEqual(me["today"]["bonuses"][0], {"key": "messages", "earned": False, "days": 0.3, "current": 0, "target": 20})
            # 读取不会凭空建日活行
            self.assertEqual(int((await db.execute(select(func.count()).select_from(DAY))).scalar_one()), 0)

    async def test_my_level_survives_a_failed_settle(self) -> None:
        async with self.sessions() as db:
            await self._minutes(db, "alice", D, 120)
            with patch.object(svc, "settle_users", new=AsyncMock(side_effect=RuntimeError("database is locked"))), self.assertLogs(svc.logger, level="ERROR"):
                me = await svc.my_level(db, await db.get(User, self.ids["alice"]), now=local(D))
            self.assertEqual((me["today"]["online_minutes"], me["today"]["base"], me["active_days"]), (120, 0.0, 0.0))

    async def test_user_level_card(self) -> None:
        async with self.sessions() as db:
            await self._set_account(db, "bob", account_active_tenths=6315, account_level=23, account_streak_days=12, account_streak_day=D)
            # 别人的卡片不触发结算
            await self._minutes(db, "bob", D, 120)
            card = LevelCard(**await svc.user_level(db, self.ids["bob"], now=local(D))).model_dump(mode="json")
            self.assertEqual(
                card,
                {
                    "user_id": self.ids["bob"],
                    "level": 23,
                    "active_days": 631.5,
                    "icons": {"crown": 0, "sun": 1, "moon": 1, "star": 3},
                    "next_level_days": 672,
                    "days_to_next": 40.5,
                    "streak": 12,
                    "rank": 1,
                    "registered_days": 200,
                },
            )
            self.assertEqual((await self._row(db, "bob")).earned_tenths, 0)
            with self.assertRaises(Exception) as caught:
                await svc.user_level(db, 987654)
            self.assertEqual((caught.exception.status_code, caught.exception.detail["code"]), (404, "user_not_found"))

    async def test_history(self) -> None:
        async with self.sessions() as db:
            db.add_all(
                [
                    AccountLevelDay(user_id=self.ids["alice"], day=D - timedelta(days=1), online_minutes=184, base_tenths=10, earned_tenths=14),
                    AccountLevelDay(user_id=self.ids["alice"], day=D - timedelta(days=3), online_minutes=45, base_tenths=5, earned_tenths=5),
                    AccountLevelDay(user_id=self.ids["alice"], day=D - timedelta(days=4), online_minutes=600, base_tenths=10, earned_tenths=20),
                    AccountLevelDay(user_id=self.ids["bob"], day=D, online_minutes=999, base_tenths=10, earned_tenths=20),
                ]
            )
            await db.commit()
            history = LevelHistory(**await svc.history(db, self.ids["alice"], days=4, now=local(D))).model_dump(mode="json")
            self.assertEqual(
                history,
                {
                    "items": [
                        {"date": "2026-10-09", "online_minutes": 45, "base": 0.5, "bonus": 0.0, "earned": 0.5},
                        {"date": "2026-10-10", "online_minutes": 0, "base": 0.0, "bonus": 0.0, "earned": 0.0},
                        {"date": "2026-10-11", "online_minutes": 184, "base": 1.0, "bonus": 0.4, "earned": 1.4},
                        {"date": "2026-10-12", "online_minutes": 0, "base": 0.0, "bonus": 0.0, "earned": 0.0},
                    ]
                },
            )
            self.assertEqual(len((await svc.history(db, self.ids["alice"], days=90, now=local(D)))["items"]), 90)
            self.assertEqual((await svc.history(db, self.ids["alice"], days=1, now=local(D)))["items"][0]["date"], D)


class AdminTests(AccountLevelTestCase):
    async def test_list_users(self) -> None:
        async with self.sessions() as db:
            await self._set_account(db, "alice", account_active_tenths=320, account_level=4, account_streak_days=3, account_streak_day=D, nickname="爱丽丝")
            await self._set_account(db, "bob", account_active_tenths=900, account_level=7)
            db.add(AccountLevelDay(user_id=self.ids["alice"], day=D - timedelta(days=2), online_minutes=130, base_tenths=10, earned_tenths=13))
            db.add(AccountLevelDay(user_id=self.ids["alice"], day=D - timedelta(days=7), online_minutes=130, base_tenths=10, earned_tenths=13))
            await db.commit()
            items, total = await svc.admin_list_users(db, now=local(D))
            self.assertEqual((total, [item["user"].username for item in items][:3]), (8, ["bob", "alice", "carol"]))
            items, total = await svc.admin_list_users(db, limit=1, offset=1, now=local(D))
            self.assertEqual((total, [item["user"].username for item in items]), (8, ["alice"]))
            alice = items[0]
            self.assertEqual((alice["level"], alice["active_days"], alice["streak"], len(alice["recent"])), (4, 32.0, 3, 7))
            self.assertEqual((alice["recent"][0]["date"], alice["recent"][-1]["date"]), (D - timedelta(days=6), D))
            self.assertEqual(alice["recent"][4], {"date": D - timedelta(days=2), "online_minutes": 130, "base": 1.0, "bonus": 0.3, "earned": 1.3})
            self.assertEqual([day["earned"] for day in alice["recent"]], [0.0, 0.0, 0.0, 0.0, 1.3, 0.0, 0.0])
            # 按用户名 / 昵称 / id 查
            for query, expected in (("ALI", ["alice"]), ("丽", ["alice"]), (str(self.ids["bob"]), ["bob"]), ("  bob ", ["bob"]), ("nobody", []), ("%", []), ("a_i", [])):
                items, total = await svc.admin_list_users(db, q=query, now=local(D))
                self.assertEqual((total, [item["user"].username for item in items]), (len(expected), expected), query)

    async def test_adjust(self) -> None:
        async with self.sessions() as db:
            admin = await db.get(User, self.ids["admin"])
            alice_id = self.ids["alice"]
            await self._set_account(db, "alice", account_active_tenths=300, account_level=3)
            # 加：重算等级，发升级事件；升到 4 的倍数写通知
            item = await svc.admin_adjust(db, admin, alice_id, days=2.5, note="  补偿  ", now=local(D))
            self.assertEqual((item["user"].username, item["user"].account_level, item["level"], item["active_days"]), ("alice", 4, 4, 32.5))
            self.assertEqual(self.send_to_user.await_args.args, (alice_id, "account.level.up", {"level": 4, "icons": {"crown": 0, "sun": 0, "moon": 1, "star": 0}, "active_days": 32.5}))
            self.assertEqual(len(await svc.notification_items(db, alice_id)), 1)
            # 减：等级往下掉，不发事件
            self.send_to_user.reset_mock()
            item = await svc.admin_adjust(db, admin, alice_id, days=-20.6, now=local(D))
            self.assertEqual((item["level"], item["active_days"], (await self._account(db, "alice"))[:2]), (1, 11.9, (1, 119)))
            self.send_to_user.assert_not_awaited()
            # 减过头：累计不低于 0
            item = await svc.admin_adjust(db, admin, alice_id, days=-500, note="清零", now=local(D))
            self.assertEqual((item["level"], item["active_days"], (await self._account(db, "alice"))[:2]), (0, 0.0, (0, 0)))
            # 审计日志：记实际变动的数
            logs = (await db.execute(select(AdminAuditLog).order_by(AdminAuditLog.id))).scalars().all()
            self.assertEqual([(log.actor_id, log.action, log.target_type, log.target_id) for log in logs], [(admin.id, "account_level.adjust", "user", alice_id)] * 3)
            self.assertEqual(
                [json.loads(log.details) for log in logs],
                [
                    {"days": 2.5, "applied": 2.5, "before": 30.0, "after": 32.5, "level_before": 3, "level_after": 4, "note": "补偿"},
                    {"days": -20.6, "applied": -20.6, "before": 32.5, "after": 11.9, "level_before": 4, "level_after": 1, "note": None},
                    {"days": -500.0, "applied": -11.9, "before": 11.9, "after": 0.0, "level_before": 1, "level_after": 0, "note": "清零"},
                ],
            )
            # 不合法的请求
            for call, expected in (
                (svc.admin_adjust(db, admin, alice_id, days=0), (422, "invalid_days")),
                (svc.admin_adjust(db, admin, alice_id, days=0.04), (422, "invalid_days")),
                (svc.admin_adjust(db, admin, 987654, days=1), (404, "user_not_found")),
            ):
                with self.assertRaises(Exception) as caught:
                    await call
                self.assertEqual((caught.exception.status_code, caught.exception.detail["code"]), expected)
            self.assertEqual(int((await db.execute(select(func.count()).select_from(AdminAuditLog))).scalar_one()), 3)

    async def test_adjust_does_not_lose_a_concurrent_credit(self) -> None:
        """后台调整和在线循环的结算撞在一起：两边的数都要在。"""
        async with self.sessions() as db, self.sessions() as other:
            admin = await db.get(User, self.ids["admin"])
            await self._set_account(db, "alice", account_active_tenths=100, account_level=1)
            await self._minutes(db, "alice", D, 120)
            original = svc._user_row
            raced = []

            async def read_then_race(session, user_id):
                row = await original(session, user_id)
                if not raced:
                    raced.append(True)
                    await svc.settle_users(other, [user_id], day=D, now=local(D))
                return row

            with patch.object(svc, "_user_row", new=read_then_race):
                item = await svc.admin_adjust(db, admin, self.ids["alice"], days=3, now=local(D))
            self.assertEqual((item["active_days"], (await self._account(db, "alice"))[:2]), (14.0, (2, 140)))
            details = json.loads((await db.execute(select(AdminAuditLog.details))).scalar_one())
            self.assertEqual((details["before"], details["after"]), (11.0, 14.0))

    async def test_settings(self) -> None:
        async with self.sessions() as db:
            data = await svc.admin_settings(db)
            self.assertEqual({key: data[key] for key in svc.rules_payload()}, svc.rules_payload())
            self.assertEqual(
                (data["tick_seconds"], data["tick_minutes"], data["leaderboard_cache_seconds"], data["notify_every"], data["legacy_backfill_at"]),
                (300, 5, 60, 4, None),
            )


class HttpSmokeTests(AccountLevelTestCase):
    """走一遍真实的路由表（/api/v1 下的全部路由）和响应模型：路径、查询参数、错误结构。"""

    async def _create_tables(self) -> None:
        # 这里要调通知中心和用户资料，那两条路要查的表很多，全建
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.acting = "alice"
        api = FastAPI()
        api.include_router(api_router)

        async def isolated_db():
            async with self.sessions() as session:
                yield session

        async def acting_user(db: Annotated[AsyncSession, Depends(get_db)]) -> User:
            return await db.get(User, self.ids[self.acting])

        api.dependency_overrides[get_db] = isolated_db
        api.dependency_overrides[get_current_user] = acting_user
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://testserver/api/v1")

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        await super().asyncTearDown()

    async def test_routes(self) -> None:
        client = self.client
        today = svc.local_day(datetime.now(UTC))
        async with self.sessions() as db:
            await self._set_account(db, "alice", account_active_tenths=315, account_level=3, ccw_student_oid="oid-alice")
            await self._set_account(db, "bob", account_active_tenths=6315, account_level=23, account_streak_days=12, account_streak_day=today)
            await self._minutes(db, "alice", today, 120)

        rules = await client.get("/levels/rules")
        self.assertEqual(rules.status_code, 200)
        self.assertEqual(rules.json(), LevelRules(**svc.rules_payload()).model_dump(mode="json"))
        self.assertEqual(rules.json()["bonuses"][1], {"key": "checkin", "label": "当天在任意群签到", "days": 0.1})

        # 读 /levels/me 时结算：31.5 + 1.0 + 0.1 = 32.6 天，升到 4 级
        me = await client.get("/levels/me")
        self.assertEqual(me.status_code, 200)
        body = me.json()
        self.assertEqual((body["level"], body["active_days"], body["level_days"], body["next_level"], body["next_level_days"], body["days_to_next"]), (4, 32.6, 32, 5, 45, 12.4))
        self.assertEqual((body["icons"], body["streak"], body["registered_days"] >= 200), ({"crown": 0, "sun": 0, "moon": 1, "star": 0}, 1, True))
        self.assertEqual((body["today"]["date"], body["today"]["online_minutes"], body["today"]["base"], body["today"]["earned"], body["today"]["cap"]), (today.isoformat(), 120, 1.0, 1.1, 2.0))
        self.assertEqual([item["key"] for item in body["today"]["bonuses"]], list(svc.BONUS_KEYS))
        self.assertEqual(body["today"]["bonuses"][2], {"key": "ccw_bound", "earned": True, "days": 0.1})
        self.assertEqual(body["today"]["bonuses"][0], {"key": "messages", "earned": False, "days": 0.3, "current": 0, "target": 20})
        self.assertEqual(self.send_to_user.await_args.args[:2], (self.ids["alice"], "account.level.up"))
        self.assertEqual((await client.get("/levels/me")).json()["active_days"], 32.6)

        # /users/{user_id}/level 挂在 /users 下，没有被 users.py 的 /{user_id} 吃掉，也没有挡住它
        card = await client.get(f"/users/{self.ids['bob']}/level")
        self.assertEqual(card.status_code, 200)
        self.assertEqual(
            {key: card.json()[key] for key in ("user_id", "level", "active_days", "icons", "next_level_days", "days_to_next", "streak", "rank")},
            {"user_id": self.ids["bob"], "level": 23, "active_days": 631.5, "icons": {"crown": 0, "sun": 1, "moon": 1, "star": 3}, "next_level_days": 672, "days_to_next": 40.5, "streak": 12, "rank": 1},
        )
        self.assertEqual(sorted(card.json()), sorted(["user_id", "level", "active_days", "icons", "next_level_days", "days_to_next", "streak", "rank", "registered_days"]))
        missing = await client.get("/users/987654/level")
        self.assertEqual((missing.status_code, missing.json()["detail"]), (404, {"code": "user_not_found", "message": "用户不存在"}))
        profile = await client.get(f"/users/{self.ids['alice']}")
        self.assertEqual((profile.status_code, profile.json()["username"], profile.json()["account_level"]), (200, "alice", 4))
        self.assertEqual((await client.get(f"/users/{self.ids['bob']}")).json()["account_level"], 23)

        # 排行榜
        board = await client.get("/levels/leaderboard")
        self.assertEqual(board.status_code, 200)
        data = board.json()
        self.assertEqual((data["period"], data["total"], [(item["rank"], item["user"]["username"], item["level"], item["active_days"]) for item in data["items"]]), ("all", 2, [(1, "bob", 23, 631.5), (2, "alice", 4, 32.6)]))
        self.assertEqual((data["items"][0]["user"]["account_level"], data["me"]["rank"], data["me"]["gained"], data["me"]["user"]["account_level"]), (23, 2, 1.1, 4))
        self.assertIn("generated_at", data)
        month = (await client.get("/levels/leaderboard", params={"period": "month", "limit": 1, "offset": 0})).json()
        self.assertEqual((month["period"], month["total"], [item["user"]["username"] for item in month["items"]], month["items"][0]["gained"]), ("month", 1, ["alice"], 1.1))
        self.acting = "bob"
        self.assertIsNone((await client.get("/levels/leaderboard", params={"period": "month"})).json()["me"])
        self.assertEqual((await client.get("/levels/me")).json()["rank"], 1)
        self.acting = "alice"
        for params in ({"period": "week"}, {"limit": 0}, {"limit": 101}, {"offset": -1}):
            self.assertEqual((await client.get("/levels/leaderboard", params=params)).status_code, 422, params)

        # 历史
        history = (await client.get("/levels/history", params={"days": 3})).json()
        self.assertEqual([item["date"] for item in history["items"]], [(today - timedelta(days=offset)).isoformat() for offset in (2, 1, 0)])
        self.assertEqual(history["items"][-1], {"date": today.isoformat(), "online_minutes": 120, "base": 1.0, "bonus": 0.1, "earned": 1.1})
        self.assertEqual(len((await client.get("/levels/history")).json()["items"]), 30)
        for days in (0, 91):
            self.assertEqual((await client.get("/levels/history", params={"days": days})).status_code, 422)

        # 通知中心里有升级通知，和别的来源的通知放在一起按时间排
        async with self.sessions() as db:
            db.add(JuryNotification(user_id=self.ids["alice"], kind="promoted", title="你已晋升为正式委员", body=None, created_at=datetime.now(UTC) - timedelta(days=1)))
            await db.commit()
        notifications = (await client.get("/notifications")).json()
        self.assertEqual([item["type"] for item in notifications["items"]], ["account_level", "jury"])
        level_items = [item for item in notifications["items"] if item["type"] == "account_level"]
        self.assertEqual([(item["category"], item["title"], item["payload"]["level"], item["unread"]) for item in level_items], [("system", "账号等级升到了 4 级", 4, False)])
        self.assertEqual((level_items[0]["id"].split(":")[0], level_items[0]["action"], level_items[0]["payload"]["icons"]["moon"]), ("account_level", "none", 1))
        self.assertEqual(notifications["counts"]["system"], 2)

        # 后台：普通用户进不去
        for method, path in (("get", "/admin/levels/users"), ("get", "/admin/levels/settings"), ("post", f"/admin/levels/users/{self.ids['bob']}/adjust")):
            denied = await getattr(client, method)(path, **({"json": {"days": 1}} if method == "post" else {}))
            self.assertEqual(denied.status_code, 403, path)
        self.acting = "admin"
        listing = (await client.get("/admin/levels/users", params={"q": "bob"})).json()
        self.assertEqual((listing["total"], listing["items"][0]["user"]["username"], listing["items"][0]["level"], listing["items"][0]["active_days"], listing["items"][0]["streak"]), (1, "bob", 23, 631.5, 12))
        self.assertEqual((len(listing["items"][0]["recent"]), listing["items"][0]["recent"][-1]["date"]), (7, today.isoformat()))
        self.assertEqual((await client.get("/admin/levels/users", params={"limit": 2, "offset": 0})).json()["total"], 8)
        adjusted = await client.post(f"/admin/levels/users/{self.ids['bob']}/adjust", json={"days": 40.5, "note": "活动奖励"})
        self.assertEqual((adjusted.status_code, adjusted.json()["level"], adjusted.json()["active_days"], adjusted.json()["user"]["account_level"]), (200, 24, 672.0, 24))
        lowered = await client.post(f"/admin/levels/users/{self.ids['bob']}/adjust", json={"days": -0.1})
        self.assertEqual((lowered.json()["level"], lowered.json()["active_days"]), (23, 671.9))
        zero = await client.post(f"/admin/levels/users/{self.ids['bob']}/adjust", json={"days": 0})
        self.assertEqual((zero.status_code, zero.json()["detail"]["code"]), (422, "invalid_days"))
        self.assertEqual((await client.post("/admin/levels/users/987654/adjust", json={"days": 1})).json()["detail"]["code"], "user_not_found")
        self.assertEqual((await client.post(f"/admin/levels/users/{self.ids['bob']}/adjust", json={"note": "没写天数"})).status_code, 422)
        admin_settings = (await client.get("/admin/levels/settings")).json()
        self.assertEqual((admin_settings["max_level"], admin_settings["daily_cap"], admin_settings["tick_seconds"], admin_settings["notify_every"], admin_settings["legacy_backfill_at"]), (256, 2.0, 300, 4, None))
        async with self.sessions() as db:
            logs = (await db.execute(select(AdminAuditLog.action, AdminAuditLog.target_id).order_by(AdminAuditLog.id))).all()
            self.assertEqual([tuple(log) for log in logs], [("account_level.adjust", self.ids["bob"])] * 2)


if __name__ == "__main__":
    unittest.main()
