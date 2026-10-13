from __future__ import annotations

import asyncio
import hashlib
import tempfile
import time
import unittest
import uuid
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Annotated
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
from fastapi import Depends, FastAPI, HTTPException
from sqlalchemy import event, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.models  # noqa: F401
from app.api.v1 import users as users_api
from app.core import database as database_module
from app.core.config import settings
from app.core.database import Base, configure_sqlite_connection, get_db
from app.core.dependencies import get_current_user
from app.models.profile_like import ProfileLike
from app.models.user import User
from app.schemas.user import UserPublic, UserRead
from app.services import profile_likes as like_service

# 只建用得到的表：点赞本身只碰前两张，后两张是冒烟测试里 /users/{id}/level 要读的
TABLES = [Base.metadata.tables[name] for name in ("users", "profile_likes", "account_level_days", "system_settings")]
NAMES = ("alice", "bob", "carol", "dave", "robot")
STATE_FIELDS = {"user_id", "like_count", "today_count", "liked_today", "can_like", "reason", "left_today"}

# 2026-03-01 是 UTC+8 的哪一天，取决于在 UTC 16:00 的哪一边
NOON = datetime(2026, 3, 1, 4, 0, tzinfo=UTC)  # 北京时间 3 月 1 日 12:00
BEFORE_MIDNIGHT = datetime(2026, 3, 1, 15, 59, 59, tzinfo=UTC)  # 北京时间 3 月 1 日 23:59:59
MIDNIGHT = datetime(2026, 3, 1, 16, 0, 0, tzinfo=UTC)  # 北京时间 3 月 2 日 00:00:00
DAY_ONE, DAY_TWO = date(2026, 3, 1), date(2026, 3, 2)


class LikeTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        asyncio.get_running_loop().set_debug(False)
        # 合并推送有一份进程内的记录，别让上一个用例的残留影响这一个
        users_api._pending_profile_likes.clear()
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(lambda sync: Base.metadata.create_all(sync, tables=TABLES))
        self.ids = await self._seed(self.sessions)

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    @staticmethod
    async def _seed(sessions, names: tuple[str, ...] = NAMES) -> dict[str, int]:
        async with sessions() as db:
            users = [User(username=name, hashed_password="x", is_bot=name == "robot") for name in names]
            db.add_all(users)
            await db.commit()
            return {user.username: user.id for user in users}

    async def _user(self, db, name: str) -> User:
        return await db.get(User, self.ids[name])

    async def _like(self, db, liker: str, target: str | int) -> dict:
        target_id = self.ids[target] if isinstance(target, str) else target
        return await like_service.like(db, await self._user(db, liker), target_id)

    async def _state(self, db, viewer: str, target: str | int) -> dict:
        target_id = self.ids[target] if isinstance(target, str) else target
        return await like_service.get_state(db, await self._user(db, viewer), target_id)

    async def _detail(self, awaitable) -> tuple[int, dict]:
        with self.assertRaises(HTTPException) as caught:
            await awaitable
        self.assertIsInstance(caught.exception.detail, dict)
        return caught.exception.status_code, caught.exception.detail

    async def _code(self, awaitable) -> tuple[int, str]:
        status_code, detail = await self._detail(awaitable)
        self.assertIsInstance(detail["message"], str)
        return status_code, detail["code"]

    async def _count(self, db, name: str) -> int:
        """库里那一列的值（不是会话里缓存的对象）。"""
        return int(await db.scalar(text("SELECT profile_like_count FROM users WHERE id = :id"), {"id": self.ids[name]}))

    async def _rows(self, db) -> int:
        return int(await db.scalar(select(func.count(ProfileLike.id))) or 0)

    @staticmethod
    @contextmanager
    def _at(moment: datetime):
        with patch.object(like_service, "_now", return_value=moment):
            yield

    @contextmanager
    def _statements(self):
        seen: list[str] = []

        def record(_conn, _cursor, statement, *_rest) -> None:
            seen.append(statement)

        event.listen(self.engine.sync_engine, "before_cursor_execute", record)
        try:
            yield seen
        finally:
            event.remove(self.engine.sync_engine, "before_cursor_execute", record)


class LikeServiceTests(LikeTestCase):
    async def test_like_adds_one_and_reports_the_new_state(self) -> None:
        async with self.sessions() as db:
            self.assertEqual(like_service.options(), {"targets_per_day": 50})
            before = await db.scalar(text("SELECT updated_at FROM users WHERE id = :id"), {"id": self.ids["bob"]})
            state = await self._like(db, "alice", "bob")
            self.assertEqual(
                state,
                {"user_id": self.ids["bob"], "like_count": 1, "today_count": 1, "liked_today": True, "can_like": False, "reason": "liked_today", "left_today": 49},
            )
            self.assertEqual(set(state), STATE_FIELDS)
            # GET 看到的和 POST 返回的是同一个对象
            self.assertEqual(await self._state(db, "alice", "bob"), state)
            self.assertEqual((await self._count(db, "bob"), await self._count(db, "alice"), await self._rows(db)), (1, 0, 1))

            row = (await db.execute(select(ProfileLike))).scalar_one()
            self.assertEqual((row.liker_id, row.target_id), (self.ids["alice"], self.ids["bob"]))
            self.assertEqual(row.day, like_service.local_day(datetime.now(UTC)))
            # 被赞不是资料修改，updated_at 不动
            after = await db.scalar(text("SELECT updated_at FROM users WHERE id = :id"), {"id": self.ids["bob"]})
            self.assertEqual(after, before)

            # 别人再点：累计往上加，各人的「今天点过没有」互不影响
            state = await self._like(db, "carol", "bob")
            self.assertEqual((state["like_count"], state["today_count"], state["liked_today"], state["left_today"]), (2, 2, True, 49))
            self.assertEqual(
                await self._state(db, "dave", "bob"),
                {"user_id": self.ids["bob"], "like_count": 2, "today_count": 2, "liked_today": False, "can_like": True, "reason": None, "left_today": 50},
            )
            # 互赞是两回事
            self.assertEqual((await self._like(db, "bob", "alice"))["like_count"], 1)

    async def test_second_like_on_the_same_day_is_409_and_does_not_count(self) -> None:
        async with self.sessions() as db:
            await self._like(db, "alice", "bob")
            for _ in range(3):
                status_code, detail = await self._detail(self._like(db, "alice", "bob"))
                self.assertEqual((status_code, detail["code"], set(detail)), (409, "already_liked_today", {"code", "message"}))
            self.assertEqual((await self._count(db, "bob"), await self._rows(db)), (1, 1))
            state = await self._state(db, "alice", "bob")
            self.assertEqual((state["like_count"], state["today_count"], state["liked_today"], state["can_like"], state["reason"], state["left_today"]), (1, 1, True, False, "liked_today", 49))
            # 同一天换个人照样能点
            self.assertEqual((await self._like(db, "alice", "carol"))["left_today"], 48)

    async def test_day_rolls_over_at_utc8_midnight(self) -> None:
        async with self.sessions() as db:
            with self._at(BEFORE_MIDNIGHT - timedelta(seconds=30)):
                self.assertEqual((await self._like(db, "alice", "bob"))["like_count"], 1)
            with self._at(BEFORE_MIDNIGHT):
                # UTC 15:59:59 还是北京时间的同一天
                self.assertEqual(await self._code(self._like(db, "alice", "bob")), (409, "already_liked_today"))
                state = await self._state(db, "alice", "bob")
                self.assertEqual((state["liked_today"], state["can_like"], state["reason"], state["today_count"], state["left_today"]), (True, False, "liked_today", 1, 49))
            with self._at(MIDNIGHT):
                # UTC 16:00 整就是第二天了：昨天点的不算今天，额度也回来了
                state = await self._state(db, "alice", "bob")
                self.assertEqual((state["like_count"], state["today_count"], state["liked_today"], state["can_like"], state["reason"], state["left_today"]), (1, 0, False, True, None, 50))
                state = await self._like(db, "alice", "bob")
                self.assertEqual((state["like_count"], state["today_count"], state["liked_today"], state["left_today"]), (2, 1, True, 49))
                self.assertEqual(await self._code(self._like(db, "alice", "bob")), (409, "already_liked_today"))
            self.assertEqual((await self._count(db, "bob"), await self._rows(db)), (2, 2))
            days = (await db.execute(select(ProfileLike.day).order_by(ProfileLike.id))).scalars().all()
            self.assertEqual(days, [DAY_ONE, DAY_TWO])
            # UTC 的同一个日历日（3 月 1 日）里点了两次：按 UTC 算天的话第二次会被拒
            stamps = (await db.execute(select(ProfileLike.created_at).order_by(ProfileLike.id))).scalars().all()
            self.assertEqual([stamp.date() for stamp in stamps], [DAY_ONE, DAY_ONE])

    async def test_self_bot_and_missing_user(self) -> None:
        async with self.sessions() as db:
            for target, expected in (("alice", (400, "cannot_like_self")), ("robot", (400, "cannot_like_bot")), (987654, (404, "user_not_found"))):
                status_code, detail = await self._detail(self._like(db, "alice", target))
                self.assertEqual((status_code, detail["code"], set(detail)), (*expected, {"code", "message"}), target)
            self.assertEqual(await self._code(self._state(db, "alice", 987654)), (404, "user_not_found"))
            self.assertEqual((await self._rows(db), await self._count(db, "alice"), await self._count(db, "robot")), (0, 0, 0))
            # 机器人和自己都能看状态，只是不能点
            self.assertEqual(
                await self._state(db, "alice", "robot"),
                {"user_id": self.ids["robot"], "like_count": 0, "today_count": 0, "liked_today": False, "can_like": False, "reason": "bot", "left_today": 50},
            )
            self.assertEqual(
                await self._state(db, "alice", "alice"),
                {"user_id": self.ids["alice"], "like_count": 0, "today_count": 0, "liked_today": False, "can_like": False, "reason": "self", "left_today": 50},
            )

    async def test_muted_user_cannot_like(self) -> None:
        async with self.sessions() as db:
            await self._like(db, "alice", "bob")
            alice = await self._user(db, "alice")
            alice.muted_until = datetime.now(UTC) + timedelta(hours=1)
            alice.mute_reason = "刷屏"
            await db.commit()

            status_code, detail = await self._detail(self._like(db, "alice", "carol"))
            self.assertEqual((status_code, detail["code"], detail["reason"]), (403, "account_muted", "刷屏"))
            self.assertIn("muted_until", detail)
            # 顺序：不存在 / 自己 / 机器人排在禁言前面，禁言排在「今天点过」前面
            self.assertEqual(await self._code(self._like(db, "alice", 987654)), (404, "user_not_found"))
            self.assertEqual(await self._code(self._like(db, "alice", "alice")), (400, "cannot_like_self"))
            self.assertEqual(await self._code(self._like(db, "alice", "robot")), (400, "cannot_like_bot"))
            self.assertEqual(await self._code(self._like(db, "alice", "bob")), (403, "account_muted"))
            self.assertEqual((await self._rows(db), await self._count(db, "carol")), (1, 0))
            # 状态照常能看，can_like 是 false；别人给被禁言的人点赞不受影响
            state = await self._state(db, "alice", "carol")
            self.assertEqual((state["can_like"], state["reason"], state["liked_today"], state["left_today"]), (False, "muted", False, 49))
            self.assertEqual((await self._like(db, "bob", "alice"))["like_count"], 1)

            # 禁言到期后恢复
            alice.muted_until = datetime.now(UTC) - timedelta(seconds=1)
            await db.commit()
            self.assertTrue((await self._state(db, "alice", "carol"))["can_like"])
            self.assertEqual((await self._like(db, "alice", "carol"))["like_count"], 1)

    async def test_daily_limit_of_fifty_targets_and_resets_at(self) -> None:
        self.assertEqual(settings.profile_like_targets_per_day, 50)
        extra = await self._seed(self.sessions, tuple(f"user{index}" for index in range(51)))
        self.ids.update(extra)
        targets = [extra[f"user{index}"] for index in range(51)]
        async with self.sessions() as db:
            with self._at(NOON):
                for index, target_id in enumerate(targets[:50]):
                    self.assertEqual((await self._like(db, "alice", target_id))["left_today"], 49 - index)
                # 第 51 个人：429，带明天 0 点（UTC+8）
                status_code, detail = await self._detail(self._like(db, "alice", targets[50]))
                self.assertEqual((status_code, detail["code"], set(detail)), (429, "like_limit", {"code", "message", "resets_at"}))
                self.assertEqual(detail["resets_at"], "2026-03-01T16:00:00Z")
                self.assertEqual(datetime.fromisoformat(detail["resets_at"]), MIDNIGHT)
                self.assertEqual(await self._code(self._like(db, "alice", "bob")), (429, "like_limit"))
                # 今天点过的人排在上限前面：还是 409
                self.assertEqual(await self._code(self._like(db, "alice", targets[0])), (409, "already_liked_today"))
                state = await self._state(db, "alice", targets[50])
                self.assertEqual((state["can_like"], state["reason"], state["liked_today"], state["left_today"]), (False, "limit", False, 0))
                self.assertEqual((await self._rows(db), await self._count(db, "user50"), await self._count(db, "bob")), (50, 0, 0))
                # 上限是各人各算的
                self.assertEqual((await self._like(db, "bob", targets[50]))["left_today"], 49)
            with self._at(BEFORE_MIDNIGHT):
                status_code, detail = await self._detail(self._like(db, "alice", targets[50]))
                self.assertEqual((status_code, detail["resets_at"]), (429, "2026-03-01T16:00:00Z"))
            with self._at(MIDNIGHT):
                # 过了零点额度回来；这时候的「明天 0 点」是再往后一天
                self.assertEqual((await self._state(db, "alice", targets[50]))["left_today"], 50)
                self.assertEqual((await self._like(db, "alice", targets[50]))["left_today"], 49)
                with patch.object(settings, "profile_like_targets_per_day", 1):
                    self.assertEqual(like_service.options(), {"targets_per_day": 1})
                    status_code, detail = await self._detail(self._like(db, "alice", "bob"))
                    self.assertEqual((status_code, detail["code"], detail["resets_at"]), (429, "like_limit", "2026-03-02T16:00:00Z"))

    async def test_duplicate_submit_race_hits_the_unique_constraint(self) -> None:
        """两个请求同时点同一个赞：都过了「今天点过没有」的检查，后到的撞唯一约束，409，计数不多加。"""
        async with self.sessions() as db:
            await self._like(db, "alice", "bob")
            with patch.object(like_service, "_given_today", new=AsyncMock(return_value=(0, False))):
                self.assertEqual(await self._code(self._like(db, "alice", "bob")), (409, "already_liked_today"))
            self.assertEqual((await self._count(db, "bob"), await self._rows(db)), (1, 1))
        async with self.sessions() as db:
            # 回滚没有留下半截事务，后面照常能用
            self.assertEqual((await self._like(db, "alice", "carol"))["like_count"], 1)
            self.assertEqual((await self._like(db, "carol", "bob"))["like_count"], 2)

    async def test_count_is_incremented_in_sql_not_read_and_written_back(self) -> None:
        async with self.sessions() as db:
            # 会话里缓存着 bob（计数 0），库里的数被别的进程加到了 5
            bob = await self._user(db, "bob")
            self.assertEqual(bob.profile_like_count, 0)
            async with self.sessions() as other:
                await other.execute(text("UPDATE users SET profile_like_count = 5 WHERE id = :id"), {"id": self.ids["bob"]})
                await other.commit()
            with self._statements() as seen:
                state = await self._like(db, "alice", "bob")
            self.assertEqual((state["like_count"], await self._count(db, "bob")), (6, 6))
            self.assertEqual((await self._state(db, "carol", "bob"))["like_count"], 6)
            updates = [statement for statement in seen if statement.startswith("UPDATE users")]
            self.assertEqual(len(updates), 1)
            self.assertIn("profile_like_count=(users.profile_like_count + ?)", updates[0])
            # 明细在前、计数在后，同一个事务
            kinds = [statement.split()[0] for statement in seen]
            self.assertLess(kinds.index("INSERT"), kinds.index("UPDATE"))

    async def test_state_fields_for_others_and_self(self) -> None:
        async with self.sessions() as db:
            with self._at(NOON):
                await self._like(db, "bob", "alice")
                await self._like(db, "carol", "alice")
                await self._like(db, "alice", "carol")
            with self._at(MIDNIGHT + timedelta(hours=1)):
                await self._like(db, "dave", "alice")
                await self._like(db, "alice", "dave")
                # 看自己：累计 3，今天收到 1；liked_today / can_like 恒为 false；left_today 是自己今天还能点几个人
                self.assertEqual(
                    await self._state(db, "alice", "alice"),
                    {"user_id": self.ids["alice"], "like_count": 3, "today_count": 1, "liked_today": False, "can_like": False, "reason": "self", "left_today": 49},
                )
                # 看别人：昨天给 carol 点过，今天还没点
                self.assertEqual(
                    await self._state(db, "alice", "carol"),
                    {"user_id": self.ids["carol"], "like_count": 1, "today_count": 0, "liked_today": False, "can_like": True, "reason": None, "left_today": 49},
                )
                self.assertEqual(
                    await self._state(db, "alice", "dave"),
                    {"user_id": self.ids["dave"], "like_count": 1, "today_count": 1, "liked_today": True, "can_like": False, "reason": "liked_today", "left_today": 49},
                )
                # 别人看 alice：today_count 是 alice 今天收到的，不是看的人点出去的
                self.assertEqual(
                    await self._state(db, "bob", "alice"),
                    {"user_id": self.ids["alice"], "like_count": 3, "today_count": 1, "liked_today": False, "can_like": True, "reason": None, "left_today": 50},
                )
                # 一次读状态是三条查询：对方那一行、我今天点出去的、对方今天收到的
                bob = await self._user(db, "bob")
                with self._statements() as seen:
                    await like_service.get_state(db, bob, self.ids["alice"])
                self.assertEqual(len(seen), 3, seen)

    async def test_reason_says_why_and_picks_the_first_that_applies(self) -> None:
        """reason：can_like 为 false 时说明原因，同时成立时按 self → bot → muted → liked_today → limit 取第一个。"""

        async def reason(viewer: str, target: str) -> tuple[bool, str | None, int]:
            state = await self._state(db, viewer, target)
            # can_like 和 reason 永远对得上
            self.assertEqual(state["can_like"], state["reason"] is None)
            return state["can_like"], state["reason"], state["left_today"]

        async with self.sessions() as db:
            self.assertEqual(await reason("alice", "bob"), (True, None, 50))
            self.assertEqual(await reason("alice", "alice"), (False, "self", 50))
            self.assertEqual(await reason("alice", "robot"), (False, "bot", 50))
            # 点成功后返回的对象里就是 liked_today
            self.assertEqual((await self._like(db, "alice", "bob"))["reason"], "liked_today")
            self.assertEqual(await reason("alice", "bob"), (False, "liked_today", 49))
            self.assertEqual(await reason("alice", "carol"), (True, None, 49))

            with patch.object(settings, "profile_like_targets_per_day", 1):
                # 到上限了：今天点过的人还是 liked_today（排在 limit 前面），别人是 limit
                self.assertEqual(await reason("alice", "bob"), (False, "liked_today", 0))
                self.assertEqual(await reason("alice", "carol"), (False, "limit", 0))
                # 自己 / 机器人排在最前面；看自己时 left_today 照常返回
                self.assertEqual(await reason("alice", "alice"), (False, "self", 0))
                self.assertEqual(await reason("alice", "robot"), (False, "bot", 0))

                alice = await self._user(db, "alice")
                alice.muted_until = datetime.now(UTC) + timedelta(hours=1)
                await db.commit()
                # 禁言排在 liked_today 和 limit 前面，排在 self / bot 后面
                self.assertEqual(await reason("alice", "bob"), (False, "muted", 0))
                self.assertEqual(await reason("alice", "carol"), (False, "muted", 0))
                self.assertEqual(await reason("alice", "alice"), (False, "self", 0))
                self.assertEqual(await reason("alice", "robot"), (False, "bot", 0))
                # reason 和真去点时报的错是同一个顺序
                self.assertEqual(await self._code(self._like(db, "alice", "alice")), (400, "cannot_like_self"))
                self.assertEqual(await self._code(self._like(db, "alice", "robot")), (400, "cannot_like_bot"))
                self.assertEqual(await self._code(self._like(db, "alice", "bob")), (403, "account_muted"))
                alice.muted_until = None
                await db.commit()
                self.assertEqual(await self._code(self._like(db, "alice", "bob")), (409, "already_liked_today"))
                self.assertEqual(await self._code(self._like(db, "alice", "carol")), (429, "like_limit"))
            # 只禁言、别的都没问题
            alice.muted_until = datetime.now(UTC) + timedelta(hours=1)
            await db.commit()
            self.assertEqual(await reason("alice", "carol"), (False, "muted", 49))
            # 别人看被禁言的人不受影响
            self.assertEqual(await reason("carol", "alice"), (True, None, 50))

    async def test_received_is_newest_first_paginated_and_marks_today(self) -> None:
        moments = {
            ("carol", 1): datetime(2026, 3, 1, 2, 0, tzinfo=UTC),
            ("bob", 1): datetime(2026, 3, 1, 3, 0, tzinfo=UTC),
            # 下面三条都在北京时间的 3 月 2 日；bob 和 dave 是同一个时刻，后写入的排前面
            ("bob", 2): datetime(2026, 3, 1, 16, 30, tzinfo=UTC),
            ("dave", 2): datetime(2026, 3, 1, 16, 30, tzinfo=UTC),
            ("carol", 2): datetime(2026, 3, 2, 1, 0, tzinfo=UTC),
        }
        async with self.sessions() as db:
            for (name, _day), moment in moments.items():
                with self._at(moment):
                    await self._like(db, name, "alice")
            # 不相干的赞不会混进来
            with self._at(moments[("carol", 2)]):
                await self._like(db, "alice", "bob")
            alice = await self._user(db, "alice")

            def flat(page) -> list[tuple[str, datetime, bool]]:
                return [(item["user"].username, item["created_at"], item["today"]) for item in page["items"]]

            with self._at(datetime(2026, 3, 2, 2, 0, tzinfo=UTC)):
                page = await like_service.received(db, alice, 20, 0)
                self.assertEqual(set(page), {"items", "total", "today_count"})
                self.assertEqual(
                    flat(page),
                    [
                        ("carol", moments[("carol", 2)], True),
                        ("dave", moments[("dave", 2)], True),
                        ("bob", moments[("bob", 2)], True),
                        ("bob", moments[("bob", 1)], False),
                        ("carol", moments[("carol", 1)], False),
                    ],
                )
                # 时间带时区（SQLite 读出来是不带的）
                self.assertTrue(all(item["created_at"].tzinfo is not None for item in page["items"]))
                # 同一个人两天各一条；total 是累计，today_count 只算今天
                self.assertEqual((page["total"], page["today_count"], alice.id), (5, 3, self.ids["alice"]))
                self.assertEqual(await self._count(db, "alice"), 5)

                pages = [await like_service.received(db, alice, 2, offset) for offset in (0, 2, 4, 6)]
                self.assertEqual([[name for name, _at, _today in flat(page)] for page in pages], [["carol", "dave"], ["bob", "bob"], ["carol"], []])
                self.assertEqual([(page["total"], page["today_count"]) for page in pages], [(5, 3)] * 4)
                with self._statements() as seen:
                    await like_service.received(db, alice, 50, 0)
                self.assertEqual(len(seen), 3)

                # 没人赞过的人
                self.assertEqual(await like_service.received(db, await self._user(db, "dave"), 20, 0), {"items": [], "total": 0, "today_count": 0})
            with self._at(datetime(2026, 3, 2, 16, 0, tzinfo=UTC)):
                # 又过了一天：一条「今天」的都没有了
                page = await like_service.received(db, alice, 20, 0)
                self.assertEqual(([today for _name, _at, today in flat(page)], page["total"], page["today_count"]), ([False] * 5, 5, 0))

    async def test_user_public_reads_the_column_without_touching_other_tables(self) -> None:
        self.assertIn("profile_like_count", UserPublic.model_fields)
        self.assertIn("profile_like_count", UserRead.model_fields)
        async with self.sessions() as db:
            await self._like(db, "alice", "bob")
            await self._like(db, "carol", "bob")
        async with self.sessions() as db:
            bob, dave = await self._user(db, "bob"), await self._user(db, "dave")
            with self._statements() as seen:
                public = UserPublic.model_validate(bob)
                self.assertEqual(UserRead.model_validate(bob).profile_like_count, 2)
                self.assertEqual(UserPublic.model_validate(dave).profile_like_count, 0)
            self.assertEqual(seen, [])
            self.assertEqual((public.profile_like_count, public.model_dump(mode="json")["profile_like_count"]), (2, 2))

    async def test_event_payload(self) -> None:
        async with self.sessions() as db:
            with self._at(NOON):
                await self._like(db, "carol", "bob")
                await self._like(db, "bob", "alice")
            with self._at(MIDNIGHT):
                await self._like(db, "alice", "bob")
                payload = await like_service.liked_event_payload(db, self.ids["bob"], self.ids["alice"])
                self.assertEqual(set(payload), {"from_user", "like_count", "today_count"})
                self.assertEqual((payload["like_count"], payload["today_count"]), (2, 1))
                # from_user 是完整的 UserPublic，已经是可以直接发出去的 JSON 数据
                self.assertEqual(set(payload["from_user"]), set(UserPublic.model_fields))
                self.assertEqual(
                    (payload["from_user"]["id"], payload["from_user"]["username"], payload["from_user"]["profile_like_count"]),
                    (self.ids["alice"], "alice", 1),
                )
                self.assertIsInstance(payload["from_user"]["created_at"], str)
                self.assertNotIn("email", payload["from_user"])
                # 任何一方已经不在了就不推
                self.assertIsNone(await like_service.liked_event_payload(db, self.ids["bob"], 987654))
                self.assertIsNone(await like_service.liked_event_payload(db, 987654, self.ids["alice"]))


class LikeStorageTests(unittest.IsolatedAsyncioTestCase):
    """用文件库 + 线上那套连接设置（WAL、busy_timeout、外键）：每个会话是自己的连接，才有真正的并发。"""

    async def asyncSetUp(self) -> None:
        asyncio.get_running_loop().set_debug(False)
        self.directory = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.directory.cleanup)
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{Path(self.directory.name, 'likes.db').as_posix()}")
        event.listen(self.engine.sync_engine, "connect", configure_sqlite_connection)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(lambda sync: Base.metadata.create_all(sync, tables=TABLES))
        self.ids = await LikeTestCase._seed(self.sessions, (*NAMES, *(f"user{index}" for index in range(8))))

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def _attempt(self, liker: str, target: str):
        async with self.sessions() as db:
            return await like_service.like(db, await db.get(User, self.ids[liker]), self.ids[target])

    @contextmanager
    def _everyone_passes_the_precheck_first(self, parties: int):
        """让并发的几个请求都先读完「今天点过没有」，再一起往下写。"""
        original = like_service._given_today
        barrier = asyncio.Barrier(parties)

        async def gated(*args):
            result = await original(*args)
            await asyncio.wait_for(barrier.wait(), timeout=10)
            return result

        with patch.object(like_service, "_given_today", gated):
            yield

    async def _totals(self, target: str) -> tuple[int, int]:
        async with self.sessions() as db:
            count = await db.scalar(select(User.profile_like_count).where(User.id == self.ids[target]))
            rows = await db.scalar(select(func.count(ProfileLike.id)).where(ProfileLike.target_id == self.ids[target]))
            return int(count), int(rows)

    async def test_two_concurrent_identical_likes_only_one_succeeds(self) -> None:
        for _round, target in enumerate(("bob", "carol", "dave")):
            with self._everyone_passes_the_precheck_first(2):
                results = await asyncio.gather(self._attempt("alice", target), self._attempt("alice", target), return_exceptions=True)
            succeeded = [item for item in results if isinstance(item, dict)]
            failed = [item for item in results if not isinstance(item, dict)]
            self.assertEqual((len(succeeded), len(failed)), (1, 1), results)
            self.assertIsInstance(failed[0], HTTPException)
            self.assertEqual((failed[0].status_code, failed[0].detail["code"]), (409, "already_liked_today"))
            self.assertEqual((succeeded[0]["like_count"], succeeded[0]["liked_today"]), (1, True))
            self.assertEqual(await self._totals(target), (1, 1))

    async def test_concurrent_likes_from_different_people_all_count(self) -> None:
        """加一是在库里做的：八个人同时给同一个人点赞，一个都不会丢。"""
        likers = [f"user{index}" for index in range(8)]
        with self._everyone_passes_the_precheck_first(len(likers)):
            results = await asyncio.gather(*(self._attempt(name, "bob") for name in likers), return_exceptions=True)
        self.assertTrue(all(isinstance(item, dict) for item in results), results)
        self.assertEqual(await self._totals("bob"), (8, 8))
        self.assertEqual(sorted(item["like_count"] for item in results)[-1], 8)

    async def test_deleting_a_user_cascades_but_the_total_stays(self) -> None:
        await self._attempt("alice", "bob")
        await self._attempt("carol", "bob")
        await self._attempt("bob", "dave")
        async with self.sessions() as db:
            self.assertEqual((await db.execute(text("PRAGMA foreign_keys"))).scalar(), 1)
            # 点赞人注销：他点的那一行跟着没，对方的累计数不减（只增不减）
            await db.execute(text("DELETE FROM users WHERE id = :id"), {"id": self.ids["alice"]})
            await db.commit()
        self.assertEqual(await self._totals("bob"), (2, 1))
        async with self.sessions() as db:
            page = await like_service.received(db, await db.get(User, self.ids["bob"]), 20, 0)
            self.assertEqual(([item["user"].username for item in page["items"]], page["total"]), (["carol"], 1))
            # 被赞的人注销：他收到的、他点出去的都跟着没
            await db.execute(text("DELETE FROM users WHERE id = :id"), {"id": self.ids["bob"]})
            await db.commit()
            self.assertEqual(int(await db.scalar(select(func.count(ProfileLike.id)))), 0)

    async def test_schema_has_the_unique_constraint_and_both_indexes(self) -> None:
        async with self.sessions() as db:
            indexes = {row[1]: bool(row[2]) for row in (await db.execute(text("PRAGMA index_list(profile_likes)"))).all()}
            self.assertEqual(
                indexes,
                {"ix_profile_likes_target_created": False, "ix_profile_likes_liker_day": False, "sqlite_autoindex_profile_likes_1": True},
            )

            async def columns(name: str) -> list[str]:
                return [row[2] for row in (await db.execute(text(f"PRAGMA index_info({name})"))).all()]

            self.assertEqual(await columns("sqlite_autoindex_profile_likes_1"), ["liker_id", "target_id", "day"])
            self.assertEqual(await columns("ix_profile_likes_target_created"), ["target_id", "created_at"])
            self.assertEqual(await columns("ix_profile_likes_liker_day"), ["liker_id", "day"])
            foreign_keys = {(row[3], row[2], row[6]) for row in (await db.execute(text("PRAGMA foreign_key_list(profile_likes)"))).all()}
            self.assertEqual(foreign_keys, {("liker_id", "users", "CASCADE"), ("target_id", "users", "CASCADE")})

            # 两条热查询各走各的索引
            await self._attempt("alice", "bob")
            plans = []
            for sql in (
                "SELECT count(id) FROM profile_likes WHERE liker_id = 1 AND day = '2026-03-01'",
                "SELECT count(id) FROM profile_likes WHERE target_id = 1 AND created_at >= '2026-03-01' AND created_at < '2026-03-02'",
                "SELECT id FROM profile_likes WHERE target_id = 1 ORDER BY created_at DESC, id DESC LIMIT 20",
            ):
                plans.append(" | ".join(row[3] for row in (await db.execute(text("EXPLAIN QUERY PLAN " + sql))).all()))
            self.assertIn("ix_profile_likes_liker_day", plans[0])
            self.assertIn("ix_profile_likes_target_created", plans[1])
            self.assertIn("ix_profile_likes_target_created", plans[2])
            self.assertNotIn("TEMP B-TREE", plans[2])


class InitDbTests(unittest.IsolatedAsyncioTestCase):
    async def test_init_db_adds_the_column_and_table_to_an_existing_database(self) -> None:
        """老库：users 上没有 profile_like_count，也没有 profile_likes 表。init_db 补上，重复执行无害。"""
        asyncio.get_running_loop().set_debug(False)
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with sessions() as db:
                db.add(User(username="old", hashed_password="x"))
                await db.commit()
            async with engine.begin() as connection:
                await connection.execute(text("DROP TABLE profile_likes"))
                await connection.execute(text("ALTER TABLE users DROP COLUMN profile_like_count"))
                names = {row[1] for row in await connection.execute(text("PRAGMA table_info(users)"))}
                self.assertNotIn("profile_like_count", names)

            with patch.object(database_module, "engine", engine):
                await database_module.init_db()
                await database_module.init_db()

            async with sessions() as db:
                column = {row[1]: row for row in (await db.execute(text("PRAGMA table_info(users)"))).all()}["profile_like_count"]
                # (cid, name, type, notnull, dflt_value, pk)
                self.assertEqual((column[2], column[3], column[4]), ("INTEGER", 1, "0"))
                self.assertEqual(await db.scalar(text("SELECT profile_like_count FROM users WHERE username = 'old'")), 0)
                tables = {row[0] for row in (await db.execute(text("SELECT name FROM sqlite_master WHERE type = 'table'"))).all()}
                self.assertIn("profile_likes", tables)

                # 补出来的库能直接用
                new = User(username="new", hashed_password="x")
                db.add(new)
                await db.commit()
                old = (await db.execute(select(User).where(User.username == "old"))).scalar_one()
                self.assertEqual((await like_service.like(db, new, old.id))["like_count"], 1)
                self.assertEqual(await db.scalar(text("SELECT profile_like_count FROM users WHERE username = 'old'")), 1)
        finally:
            await engine.dispose()


class HttpLikeTests(LikeTestCase):
    """走真实的路由：错误的形状、实时事件的负载和合并。"""

    DELAY = 0.4

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        from app.api.v1.router import api_router

        self.acting = "alice"
        self.api = FastAPI()
        self.api.include_router(api_router)

        async def isolated_db():
            async with self.sessions() as session:
                yield session

        async def acting_user(db: Annotated[AsyncSession, Depends(get_db)]) -> User:
            return await db.get(User, self.ids[self.acting])

        self.api.dependency_overrides[get_db] = isolated_db
        self.api.dependency_overrides[get_current_user] = acting_user
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.api), base_url="http://testserver")

        # 事件：合并窗口调短，推送换成记录，后台任务读库走测试库
        self.sent = AsyncMock()
        self.sent_many = AsyncMock()
        self.broadcast = AsyncMock()
        for patcher in (
            patch.object(settings, "profile_like_event_merge_seconds", self.DELAY),
            patch.object(users_api, "AsyncSessionLocal", self.sessions),
            patch.object(users_api.manager, "send_to_user", self.sent),
            patch.object(users_api.manager, "send_to_users", self.sent_many),
            patch.object(users_api.manager, "broadcast", self.broadcast),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    async def asyncTearDown(self) -> None:
        await self._drain()
        await self.client.aclose()
        await super().asyncTearDown()

    async def _drain(self) -> None:
        tasks = list(users_api._profile_like_tasks)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _post(self, liker: str, target: str | int) -> httpx.Response:
        self.acting = liker
        return await self.client.post(f"/api/v1/users/{self.ids[target] if isinstance(target, str) else target}/likes")

    async def _get(self, viewer: str, path: str) -> httpx.Response:
        self.acting = viewer
        return await self.client.get("/api/v1" + path)

    async def test_default_thresholds(self) -> None:
        defaults = type(settings).model_fields
        self.assertEqual(defaults["profile_like_targets_per_day"].default, 50)
        self.assertEqual(defaults["profile_like_event_merge_seconds"].default, 3.0)

    async def test_error_shapes_over_http(self) -> None:
        for target, expected in (("alice", (400, "cannot_like_self")), ("robot", (400, "cannot_like_bot")), (987654, (404, "user_not_found"))):
            response = await self._post("alice", target)
            detail = response.json()["detail"]
            self.assertEqual((response.status_code, detail["code"], set(detail)), (*expected, {"code", "message"}), target)
        self.assertEqual((await self._get("alice", "/users/987654/likes")).json()["detail"]["code"], "user_not_found")

        self.assertEqual((await self._post("alice", "bob")).status_code, 200)
        response = await self._post("alice", "bob")
        self.assertEqual((response.status_code, response.json()["detail"]["code"]), (409, "already_liked_today"))

        with patch.object(settings, "profile_like_targets_per_day", 1):
            self.assertEqual((await self._get("alice", "/users/likes/options")).json(), {"targets_per_day": 1})
            response = await self._post("alice", "carol")
            detail = response.json()["detail"]
            self.assertEqual((response.status_code, detail["code"], set(detail)), (429, "like_limit", {"code", "message", "resets_at"}))
            # 明天 0 点（UTC+8）：带时区的时间，换成北京时间正好是零点，而且就在接下来的 24 小时里
            self.assertTrue(detail["resets_at"].endswith("Z"))
            resets_at = datetime.fromisoformat(detail["resets_at"])
            beijing = resets_at + timedelta(hours=8)
            self.assertEqual((beijing.hour, beijing.minute, beijing.second, resets_at.utcoffset()), (0, 0, 0, timedelta(0)))
            self.assertTrue(timedelta(0) < resets_at - datetime.now(UTC) <= timedelta(days=1))
            response = await self._get("alice", f"/users/{self.ids['carol']}/likes")
            self.assertEqual((response.json()["can_like"], response.json()["reason"], response.json()["left_today"]), (False, "limit", 0))

        async with self.sessions() as db:
            (await self._user(db, "dave")).muted_until = datetime.now(UTC) + timedelta(hours=1)
            await db.commit()
        response = await self._post("dave", "bob")
        self.assertEqual((response.status_code, response.json()["detail"]["code"]), (403, "account_muted"))
        self.assertIn("muted_until", response.json()["detail"])
        body = (await self._get("dave", f"/users/{self.ids['bob']}/likes")).json()
        self.assertEqual((body["can_like"], body["reason"]), (False, "muted"))

        # 被拒绝的请求都不触发事件
        await self._drain()
        self.sent.assert_awaited_once()
        async with self.sessions() as db:
            self.assertEqual((await self._count(db, "bob"), await self._rows(db)), (1, 1))

    async def test_received_list_over_http(self) -> None:
        for name in ("bob", "carol", "dave"):
            self.assertEqual((await self._post(name, "alice")).status_code, 200)
        response = await self._get("alice", "/users/me/likes/received")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual((set(body), body["total"], body["today_count"]), ({"items", "total", "today_count"}, 3, 3))
        self.assertEqual([item["user"]["username"] for item in body["items"]], ["dave", "carol", "bob"])
        for item in body["items"]:
            self.assertEqual(set(item), {"user", "created_at", "today"})
            self.assertEqual(set(item["user"]), set(UserPublic.model_fields))
            self.assertIs(item["today"], True)
            self.assertIsNotNone(datetime.fromisoformat(item["created_at"]).tzinfo)
        # 列表是「我」的，换个人看到的是他自己的
        self.assertEqual((await self._get("bob", "/users/me/likes/received")).json(), {"items": [], "total": 0, "today_count": 0})

        body = (await self._get("alice", "/users/me/likes/received?limit=1&offset=1")).json()
        self.assertEqual(([item["user"]["username"] for item in body["items"]], body["total"]), (["carol"], 3))
        self.assertEqual((await self._get("alice", "/users/me/likes/received?limit=50")).status_code, 200)
        for query in ("limit=0", "limit=51", "offset=-1", "limit=abc"):
            self.assertEqual((await self._get("alice", "/users/me/likes/received?" + query)).status_code, 422, query)

    async def test_realtime_event_goes_only_to_the_liked_user_and_is_coalesced(self) -> None:
        # 窗口（这里调短了）里的三个赞：到点只推一次，点赞人是最后一个，数字是最新的
        for name in ("alice", "carol", "dave"):
            self.assertEqual((await self._post(name, "bob")).status_code, 200)
        self.sent.assert_not_awaited()
        self.assertEqual(users_api._pending_profile_likes, {self.ids["bob"]: self.ids["dave"]})
        await self._drain()
        self.sent.assert_awaited_once()
        user_id, event_name, payload = self.sent.await_args.args
        self.assertEqual((user_id, event_name), (self.ids["bob"], "user.profile.liked"))
        self.assertEqual(set(payload), {"from_user", "like_count", "today_count"})
        self.assertEqual((payload["like_count"], payload["today_count"]), (3, 3))
        self.assertEqual(set(payload["from_user"]), set(UserPublic.model_fields))
        self.assertEqual((payload["from_user"]["id"], payload["from_user"]["username"]), (self.ids["dave"], "dave"))
        self.assertEqual(users_api._pending_profile_likes, {})
        # 只发给被赞的人本人：不广播，也不发给别人
        self.broadcast.assert_not_awaited()
        self.sent_many.assert_not_awaited()

        # 被拒绝的请求不触发；之后的赞另算一次
        self.sent.reset_mock()
        self.assertEqual((await self._post("alice", "bob")).status_code, 409)
        self.assertEqual((await self._post("alice", "alice")).status_code, 400)
        self.assertEqual((await self._get("alice", f"/users/{self.ids['bob']}/likes")).status_code, 200)
        self.assertEqual(users_api._pending_profile_likes, {})
        await self._drain()
        self.sent.assert_not_awaited()

        # 不同的被赞者各自合并
        self.assertEqual((await self._post("alice", "carol")).status_code, 200)
        self.assertEqual((await self._post("bob", "carol")).status_code, 200)
        self.assertEqual((await self._post("carol", "alice")).status_code, 200)
        await self._drain()
        self.assertEqual(
            sorted((call.args[0], call.args[1], call.args[2]["from_user"]["username"], call.args[2]["like_count"]) for call in self.sent.await_args_list),
            sorted([(self.ids["carol"], "user.profile.liked", "bob", 2), (self.ids["alice"], "user.profile.liked", "carol", 1)]),
        )

    async def test_event_waits_for_the_window_then_a_later_like_starts_a_new_one(self) -> None:
        started = asyncio.get_running_loop().time()
        await self._post("alice", "bob")
        await asyncio.sleep(self.DELAY / 3)
        self.sent.assert_not_awaited()
        await self._drain()
        self.sent.assert_awaited_once()
        self.assertGreaterEqual(asyncio.get_running_loop().time() - started, self.DELAY * 0.9)
        self.assertEqual(self.sent.await_args.args[2]["from_user"]["username"], "alice")

        await self._post("carol", "bob")
        await self._drain()
        self.assertEqual(self.sent.await_count, 2)
        payload = self.sent.await_args.args[2]
        self.assertEqual((payload["from_user"]["username"], payload["like_count"], payload["today_count"]), ("carol", 2, 2))

    async def test_event_failure_is_only_logged(self) -> None:
        self.sent.side_effect = RuntimeError("redis down")
        with self.assertLogs(users_api.logger, level="ERROR") as logs:
            response = await self._post("alice", "bob")
            self.assertEqual((response.status_code, response.json()["like_count"]), (200, 1))
            await self._drain()
        self.assertIn("Profile liked event", logs.output[0])
        self.sent.assert_awaited_once()
        # 读库失败也一样只记日志
        self.sent.side_effect = None
        self.sent.reset_mock()
        with patch.object(like_service, "liked_event_payload", new=AsyncMock(side_effect=RuntimeError("db down"))):
            with self.assertLogs(users_api.logger, level="ERROR"):
                self.assertEqual((await self._post("carol", "bob")).status_code, 200)
                await self._drain()
        self.sent.assert_not_awaited()
        # 失败之后标记已经摘掉，下一个赞照常推
        self.assertEqual(users_api._pending_profile_likes, {})
        self.assertEqual((await self._post("dave", "bob")).status_code, 200)
        await self._drain()
        self.sent.assert_awaited_once()
        self.assertEqual(self.sent.await_args.args[2]["like_count"], 3)


class RealAppSmokeTests(LikeTestCase):
    """用真实的应用（app.main.app，带客户端证明等全部中间件）走一遍：四个路径都挂上了，也没有互相截走。"""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        from app.main import app as real_app

        self.acting = "alice"

        async def isolated_db():
            async with self.sessions() as session:
                yield session

        async def acting_user(db: Annotated[AsyncSession, Depends(get_db)]) -> User:
            return await db.get(User, self.ids[self.acting])

        real_app.dependency_overrides[get_db] = isolated_db
        real_app.dependency_overrides[get_current_user] = acting_user
        self.addCleanup(real_app.dependency_overrides.clear)
        # 这一组只看请求能不能走通，实时事件在 HttpLikeTests 里测
        patcher = patch.object(users_api, "_schedule_profile_liked", MagicMock())
        self.scheduled = patcher.start()
        self.addCleanup(patcher.stop)
        self.real_app = real_app
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=real_app), base_url="http://testserver")

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        await super().asyncTearDown()

    @staticmethod
    def _proof(method: str, path: str) -> dict[str, str]:
        """和客户端同一个算法：METHOD|去掉 /api/v1 的路径|时间戳|nonce|盐。"""
        timestamp, nonce = str(int(time.time() * 1000)), uuid.uuid4().hex
        digest = hashlib.sha256(f"{method}|{path}|{timestamp}|{nonce}|KukeChat".encode("utf-8")).hexdigest()
        return {"X-Kuke-Client": "kukechat-web/test", "X-Kuke-Timestamp": timestamp, "X-Kuke-Nonce": nonce, "X-Kuke-Proof": digest}

    async def _call(self, method: str, path: str, query: str = "") -> httpx.Response:
        return await self.client.request(method, "/api/v1" + path + query, headers=self._proof(method, path))

    async def test_all_four_routes_are_mounted_and_nothing_is_shadowed(self) -> None:
        paths = self.real_app.openapi()["paths"]
        for path, methods in (
            ("/api/v1/users/likes/options", {"get"}),
            ("/api/v1/users/me/likes/received", {"get"}),
            ("/api/v1/users/{user_id}/likes", {"get", "post"}),
            ("/api/v1/users/{user_id}/level", {"get"}),
            ("/api/v1/users/{user_id}", {"get"}),
        ):
            self.assertEqual(set(paths[path]), methods, path)

        bob = self.ids["bob"]
        # 没带客户端证明头的请求到不了路由
        response = await self.client.post(f"/api/v1/users/{bob}/likes")
        self.assertEqual((response.status_code, response.json()), (403, {"detail": "Invalid client"}))

        # 固定路径：没有被 /{user_id}、/{user_id}/likes 之类带参数的路径截走（截走的话会是 422）
        response = await self._call("GET", "/users/likes/options")
        self.assertEqual((response.status_code, response.json()), (200, {"targets_per_day": 50}))
        response = await self._call("GET", "/users/me/likes/received")
        self.assertEqual((response.status_code, response.json()), (200, {"items": [], "total": 0, "today_count": 0}))

        response = await self._call("GET", f"/users/{bob}/likes")
        self.assertEqual(
            (response.status_code, response.json()),
            (200, {"user_id": bob, "like_count": 0, "today_count": 0, "liked_today": False, "can_like": True, "reason": None, "left_today": 50}),
        )
        response = await self._call("POST", f"/users/{bob}/likes")
        self.assertEqual(
            (response.status_code, response.json()),
            (200, {"user_id": bob, "like_count": 1, "today_count": 1, "liked_today": True, "can_like": False, "reason": "liked_today", "left_today": 49}),
        )
        self.scheduled.assert_called_once_with(bob, self.ids["alice"])
        response = await self._call("POST", f"/users/{bob}/likes")
        self.assertEqual((response.status_code, response.json()["detail"]["code"]), (409, "already_liked_today"))
        self.scheduled.assert_called_once()
        response = await self._call("GET", f"/users/{bob}/likes")
        self.assertEqual((response.json()["like_count"], response.json()["liked_today"]), (1, True))

        self.acting = "bob"
        response = await self._call("GET", "/users/me/likes/received", "?limit=20&offset=0")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual((body["total"], body["today_count"], len(body["items"])), (1, 1, 1))
        self.assertEqual((body["items"][0]["user"]["username"], body["items"][0]["today"]), ("alice", True))
        self.acting = "alice"

        # 原有的路由照旧：公开资料带上了新字段，/users/{id}/level 仍然可用
        response = await self._call("GET", f"/users/{bob}")
        self.assertEqual((response.status_code, response.json()["username"], response.json()["profile_like_count"]), (200, "bob", 1))
        response = await self._call("GET", f"/users/{self.ids['carol']}")
        self.assertEqual(response.json()["profile_like_count"], 0)
        response = await self._call("GET", f"/users/{bob}/level")
        self.assertEqual((response.status_code, response.json()["user_id"], response.json()["level"]), (200, bob, 0))
        response = await self._call("GET", f"/users/{bob}/online")
        self.assertEqual((response.status_code, response.json()["user_id"]), (200, bob))
        response = await self._call("GET", "/users/poke-options")
        self.assertEqual((response.status_code, response.json()["default_action"]), (200, "戳"))
        response = await self._call("GET", "/users/online-count")
        self.assertEqual(response.status_code, 200)
        response = await self._call("GET", "/users/search", "?q=bob")
        self.assertEqual((response.status_code, [item["profile_like_count"] for item in response.json()]), (200, [1]))

        # 业务上的 404 带 code；路径不存在 / 方法不对是框架的 404 / 405，detail 是字符串
        response = await self._call("POST", "/users/987654/likes")
        self.assertEqual((response.status_code, response.json()["detail"]["code"]), (404, "user_not_found"))
        response = await self._call("DELETE", f"/users/{bob}/likes")
        self.assertEqual((response.status_code, response.json()), (405, {"detail": "Method Not Allowed"}))
        response = await self._call("POST", "/users/likes/options")
        self.assertEqual(response.status_code, 405)
        response = await self._call("GET", f"/users/{bob}/like")
        self.assertEqual((response.status_code, response.json()), (404, {"detail": "Not Found"}))
        # /users/me/likes 不是契约里的路径：me 不是数字
        response = await self._call("GET", "/users/me/likes")
        self.assertEqual(response.status_code, 422)


if __name__ == "__main__":
    unittest.main()
