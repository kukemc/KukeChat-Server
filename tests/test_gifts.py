"""限时商品（docs/shop.md 2.5）、礼物（docs/gifts.md）、生日（docs/chat-features.md 第 8 节）的测试。余额来源用假账本，不发任何外部请求。

并发和红包的测试一样分两层：同一个进程里多个任务同时来（各用各的会话和连接），以及真的起几个进程对着同一个库文件同时送 / 同时续期 / 同时清扫
（线上是两个 worker 进程，进程之间只有 SQLite 自己的锁）。后者把本文件当脚本跑：python tests/test_gifts.py worker '<json>'。
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any
from unittest.mock import AsyncMock, Mock, patch

import httpx
from fastapi import Depends, FastAPI, HTTPException
from pydantic import ValidationError
from sqlalchemy import event, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.models  # noqa: F401
from app import integrations
from app.core import database as database_module
from app.core import localtime
from app.core.config import settings
from app.core.database import Base, configure_sqlite_connection, get_db
from app.core.dependencies import get_current_user
from app.models.conversation import Conversation, ConversationMember, TemporaryConversationBlock
from app.models.enums import ConversationType, MemberRole, MessageType, PlatformRole
from app.models.gift import Gift, GiftRecord
from app.models.message import Message
from app.models.report import AdminAuditLog
from app.models.shop import ShopItem, ShopPurchase
from app.models.system import SystemSetting
from app.models.user import User
from app.schemas.message import MessageCreate
from app.schemas.report import AdminUserRead
from app.schemas.user import UserPublic
from app.services import birthdays as birthday_service
from app.services import gifts as gift_service
from app.services import messages as message_service
from app.services import moderation as moderation_service
from app.services import red_packets as rp
from app.services import reports as report_service
from app.services import shop as shop_service
from app.websocket.manager import manager
from integration_fakes import FakeCoinLedger

REPO_ROOT = Path(__file__).resolve().parents[1]
OID_A, OID_B, OID_D = "a" * 24, "b" * 24, "d" * 24
# 2026-10-12 16:00（UTC+8）
START = datetime(2026, 10, 12, 8, 0, tzinfo=UTC)
STYLE = {"version": 1, "background": "#112233", "text": "#ffffff"}
DAY = timedelta(days=1)


def make_engine(path: Path):
    """文件库，每个会话一条连接，WAL、抢不到写锁就等 —— 和线上一样。"""
    engine = create_async_engine(f"sqlite+aiosqlite:///{path.as_posix()}")
    event.listen(engine.sync_engine, "connect", configure_sqlite_connection)
    return engine


class GiftTestCase(unittest.IsolatedAsyncioTestCase):
    """alice / bob / dave 绑定了 CCW，carol 没绑定；eve 不在任何会话里；robot 是机器人。群主是 dave；alice 和 bob 之间有一个私聊。

    商品：bubble.timed 50 币 / 30 天，bubble.perm 60 币永久，bubble.free 不卖，bubble.off 已下架。礼物目录是首批那 6 件。
    """

    async def asyncSetUp(self) -> None:
        asyncio.get_running_loop().set_debug(False)
        shop_service.reset_runtime_state()
        rp.reset_runtime_state()
        gift_service.reset_runtime_state()
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "gifts.db"
        self.engine = make_engine(self.db_path)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with self.sessions() as db:
            users = [User(username=name, hashed_password="x") for name in ("alice", "bob", "carol", "dave", "eve", "admin", "robot")]
            users[0].ccw_student_oid, users[1].ccw_student_oid, users[3].ccw_student_oid = OID_A, OID_B, OID_D
            users[1].nickname = "阿夸"
            users[5].platform_role = PlatformRole.ADMIN
            users[6].is_bot = True
            db.add_all(users)
            await db.flush()
            self.ids = {user.username: user.id for user in users}
            group = Conversation(type=ConversationType.GROUP, title="群")
            direct = Conversation(type=ConversationType.DIRECT)
            db.add_all([group, direct])
            await db.flush()
            self.group_id, self.direct_id = group.id, direct.id
            db.add(ConversationMember(conversation_id=group.id, user_id=self.ids["dave"], role=MemberRole.OWNER))
            for name in ("alice", "bob", "carol", "robot"):
                db.add(ConversationMember(conversation_id=group.id, user_id=self.ids[name], role=MemberRole.MEMBER))
            for name in ("alice", "bob"):
                db.add(ConversationMember(conversation_id=direct.id, user_id=self.ids[name], role=MemberRole.MEMBER))
            db.add(SystemSetting(key="gifts.enabled", value="1"))
            db.add(SystemSetting(key="red_packet.enabled", value="1"))
            items = [
                ShopItem(sku="bubble.timed", category="bubble", name="限时", price=50, style=json.dumps(STYLE), duration_days=30),
                ShopItem(sku="bubble.perm", category="bubble", name="永久", price=60, style=json.dumps(STYLE)),
                ShopItem(sku="bubble.free", category="bubble", name="不卖", price=0, style=json.dumps(STYLE)),
                ShopItem(sku="bubble.off", category="bubble", name="下架", price=10, style=json.dumps(STYLE), is_active=False),
            ]
            db.add_all(items)
            await db.commit()
            self.items = {item.sku.split(".")[1]: item.id for item in items}
            self.assertEqual(await gift_service.ensure_default_gifts(db), 6)

        self.now = START
        self.ledger = FakeCoinLedger()
        integrations.set_coin_ledger(self.ledger)
        self.addCleanup(integrations.set_coin_ledger, None)
        self.send_to_user = AsyncMock()
        self.send_to_users = AsyncMock()
        self.broadcast = AsyncMock()

        async def everyone_online(user_ids: list[int]) -> set[int]:
            return set(user_ids)

        for patcher in (
            patch.object(shop_service, "_now", lambda: self.now),
            patch.object(rp, "_now", lambda: self.now),
            patch.object(gift_service, "_now", lambda: self.now),
            patch.object(localtime, "_now", lambda: self.now),
            patch.object(shop_service, "AsyncSessionLocal", self.sessions),
            patch.object(rp, "AsyncSessionLocal", self.sessions),
            patch.object(manager, "send_to_user", self.send_to_user),
            patch.object(manager, "send_to_users", self.send_to_users),
            patch.object(manager, "broadcast", self.broadcast),
            patch.object(manager, "online_user_ids_among", everyone_online),
            patch.object(settings, "shop_refresh_interval_seconds", 20),
            patch.object(settings, "shop_cosmetics_event_interval_seconds", 0),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    async def asyncTearDown(self) -> None:
        await shop_service.drain()
        await self.engine.dispose()

    # ------------------------------------------------------------ 工具

    def tick(self, seconds: float = 0, *, hours: float = 0, days: float = 0) -> None:
        self.now = self.now + timedelta(seconds=seconds, hours=hours, days=days)

    async def _as(self, name: str, function, *args: Any, **kwargs: Any):
        """用一个新会话、以某个用户的身份调服务层函数（和一次请求一样）。"""
        async with self.sessions() as db:
            return await function(db, await db.get(User, self.ids[name]), *args, **kwargs)

    async def _fails(self, awaitable) -> tuple[int, str | None, dict[str, Any]]:
        with self.assertRaises(HTTPException) as caught:
            await awaitable
        detail = caught.exception.detail
        self.assertIsInstance(detail, dict)
        self.assertTrue(detail.get("message"))
        return caught.exception.status_code, detail.get("code"), detail

    async def _code(self, awaitable) -> tuple[int, str | None]:
        return (await self._fails(awaitable))[:2]

    async def _configure(self, **values: Any) -> None:
        async with self.sessions() as db:
            for name, value in values.items():
                raw = ("1" if value else "0") if isinstance(value, bool) else str(value)
                item = await db.get(SystemSetting, f"gifts.{name}")
                if item is None:
                    db.add(SystemSetting(key=f"gifts.{name}", value=raw))
                else:
                    item.value = raw
            await db.commit()
        gift_service.reset_cache()

    def _payload(self, to: str = "bob", **overrides: Any) -> dict[str, Any]:
        return {"to_user_id": self.ids[to], "conversation_id": self.group_id, "kind": "gift", "gift_key": "cake", "count": 1, "message": None, **overrides}

    async def _send(self, name: str = "alice", to: str = "bob", **overrides: Any) -> dict[str, Any]:
        result, message = await self._as(name, gift_service.send_gift, self._payload(to, **overrides))
        return {**result, "message": message}

    async def _send_item(self, sku: str, name: str = "alice", to: str = "bob", **overrides: Any) -> dict[str, Any]:
        return await self._send(name, to, **{"kind": "item", "item_sku": sku, "gift_key": None, "count": 1, **overrides})

    async def _user(self, name: str) -> User:
        async with self.sessions() as db:
            return await db.get(User, self.ids[name])

    async def _public(self, name: str) -> dict[str, Any]:
        return UserPublic.model_validate(await self._user(name)).model_dump(mode="json")

    async def _charm(self, name: str) -> int:
        return (await self._user(name)).charm

    async def _balance(self, name: str) -> dict[str, Any]:
        async with self.sessions() as db:
            return await shop_service.balance_of(db, self.ids[name])

    async def _coins(self, name: str) -> int:
        return (await self._balance(name))["balance"]

    async def _count(self, model: Any) -> int:
        async with self.sessions() as db:
            return int(await db.scalar(select(func.count()).select_from(model)) or 0)

    async def _records(self) -> list[GiftRecord]:
        async with self.sessions() as db:
            return list((await db.execute(select(GiftRecord).order_by(GiftRecord.id))).scalars().all())

    async def _rows(self, name: str, item: str) -> list[ShopPurchase]:
        async with self.sessions() as db:
            rows = await db.execute(
                select(ShopPurchase).where(ShopPurchase.user_id == self.ids[name], ShopPurchase.item_id == self.items[item]).order_by(ShopPurchase.id)
            )
            return list(rows.scalars().all())

    async def _live(self, name: str, item: str) -> ShopPurchase | None:
        live = [row for row in await self._rows(name, item) if row.revoked_at is None and row.superseded_at is None]
        self.assertLessEqual(len(live), 1)
        return live[0] if live else None

    async def _expiry(self, name: str, item: str) -> datetime | None:
        live = await self._live(name, item)
        return shop_service._aware(live.expires_at) if live is not None else None

    async def _buy(self, name: str, item: str) -> dict[str, Any]:
        return await self._as(name, shop_service.purchase, self.items[item])

    async def _equip(self, name: str, sku: str | None) -> dict[str, Any]:
        return await self._as(name, shop_service.set_equipped, "bubble", sku)

    async def _overview_item(self, name: str, sku: str) -> dict[str, Any] | None:
        overview = await self._as(name, shop_service.overview)
        return next((item for item in overview["items"] if item["sku"] == sku), None)

    async def _sweep(self) -> dict[str, int]:
        async with self.sessions() as db:
            return await shop_service.sweep_expiry(db)

    async def _events(self, event_type: str) -> list[tuple[Any, Any]]:
        """到现在为止发出去的某种事件：[(收件人, data)]。"""
        await shop_service.drain()
        calls = [*self.send_to_user.await_args_list, *self.send_to_users.await_args_list]
        return [(call.args[0], call.args[2]) for call in calls if call.args[1] == event_type]

    async def _broadcasts(self, event_type: str) -> list[Any]:
        await shop_service.drain()
        return [call.args[1] for call in self.broadcast.await_args_list if call.args[0] == event_type]

    async def _audit(self) -> list[tuple[str, dict[str, Any]]]:
        async with self.sessions() as db:
            rows = (await db.execute(select(AdminAuditLog).order_by(AdminAuditLog.id))).scalars().all()
            return [(row.action, json.loads(row.details or "{}")) for row in rows]

    async def _ledger(self) -> dict[str, Any]:
        async with self.sessions() as db:
            return await gift_service.ledger_check(db)

    async def _assert_conserved(self) -> dict[str, Any]:
        """每个人的心意值 = 他收到的记录的 amount 之和；每个人送礼花掉的 = 他送出的记录的 amount 之和。"""
        ledger = await self._ledger()
        self.assertTrue(ledger["balanced"], ledger)
        self.assertEqual(ledger["total_charm"], ledger["total_amount"])
        records = await self._records()
        for name in self.ids:
            self.assertEqual(await self._charm(name), sum(record.amount for record in records if record.to_user_id == self.ids[name]), name)
            self.assertEqual((await self._balance(name))["gift_spent"], sum(record.amount for record in records if record.from_user_id == self.ids[name]), name)
        return ledger


# ---------------------------------------------------------------- 限时商品


class TimedItemTests(GiftTestCase):
    async def test_buy_then_renew_adds_to_the_original_expiry(self) -> None:
        self.ledger.donate(OID_A, 200)
        bought = await self._buy("alice", "timed")
        self.assertEqual((bought["item"]["owned"], bought["item"]["obtainable"], bought["item"]["duration_days"]), (True, "renew", 30))
        self.assertEqual(bought["item"]["expires_at"], START + 30 * DAY)
        self.assertEqual(bought["balance"]["balance"], 150)
        self.tick(days=5)
        renewed = await self._buy("alice", "timed")
        # 续期：在原到期时间上加 30 天，不是从现在起算
        self.assertEqual(renewed["item"]["expires_at"], START + 60 * DAY)
        self.assertEqual((renewed["balance"]["balance"], renewed["balance"]["spent"]), (100, 100))
        rows = await self._rows("alice", "timed")
        self.assertEqual([(row.price, row.duration_days, row.source, row.superseded_at is not None) for row in rows], [(50, 30, "purchase", True), (50, 30, "purchase", False)])
        shown = await self._overview_item("alice", "bubble.timed")
        self.assertEqual((shown["owned"], shown["obtainable"], shown["expires_at"]), (True, "renew", START + 60 * DAY))
        # 购买记录：每次各一条；被接替的那一条到续期的那一刻为止，不会看起来像同时有两份没到期的
        async with self.sessions() as db:
            mine = await shop_service.list_purchases(db, self.ids["alice"], 50, 0)
            admin = await shop_service.admin_list_purchases(db, user_id=self.ids["alice"], item_id=None, limit=50, offset=0)
            listing = await shop_service.admin_list_items(db)
        self.assertEqual([(row["price"], row["duration_days"], row["expires_at"], row["from_user"]) for row in mine["items"]], [(50, 30, START + 60 * DAY, None), (50, 30, START + 5 * DAY, None)])
        self.assertEqual([(row["expires_at"], row["superseded_at"]) for row in admin["items"]], [(START + 60 * DAY, None), (START + 5 * DAY, START + 5 * DAY)])
        counts = {item["sku"]: (item["sold"], item["owners"], item["duration_days"]) for item in listing["items"]}
        self.assertEqual((counts["bubble.timed"], counts["bubble.perm"]), ((2, 1, 30), (0, 0, None)))

    async def test_permanent_items_still_cannot_be_bought_twice(self) -> None:
        self.ledger.donate(OID_A, 200)
        bought = await self._buy("alice", "perm")
        self.assertEqual((bought["item"]["obtainable"], bought["item"]["expires_at"], bought["item"]["duration_days"]), ("owned", None, None))
        self.assertEqual(await self._code(self._buy("alice", "perm")), (409, "already_owned"))
        self.assertEqual((await self._coins("alice"), len(await self._rows("alice", "perm"))), (140, 1))

    async def test_expiry_is_judged_on_every_read_without_the_sweeper(self) -> None:
        self.ledger.donate(OID_A, 200)
        await self._buy("alice", "timed")
        self.assertEqual((await self._equip("alice", "bubble.timed"))["equipped"], {"bubble": "bubble.timed"})
        self.assertEqual((await self._public("alice"))["cosmetics"], {"bubble": {"sku": "bubble.timed", "style": STYLE}})
        self.tick(days=30, seconds=-1)
        self.assertEqual(set((await self._public("alice"))["cosmetics"]), {"bubble"})
        self.assertTrue((await self._overview_item("alice", "bubble.timed"))["equipped"])
        self.tick(seconds=1)
        # 到期的那一刻起：公开资料上不再装备着、商店里不再拥有、装备接口不认 —— 都没跑过清扫
        self.assertEqual((await self._public("alice"))["cosmetics"], {})
        overview = await self._as("alice", shop_service.overview)
        shown = next(item for item in overview["items"] if item["sku"] == "bubble.timed")
        self.assertEqual((overview["equipped"], shown["owned"], shown["equipped"], shown["obtainable"], shown["expires_at"]), ({}, False, False, "buy", None))
        self.assertEqual(await self._code(self._equip("alice", "bubble.timed")), (403, "not_owned"))
        async with self.sessions() as db:
            listing = await shop_service.admin_list_items(db)
        self.assertEqual(next(item["owners"] for item in listing["items"] if item["sku"] == "bubble.timed"), 0)
        # 到期之后再买：从现在起算 30 天
        self.tick(days=10)
        again = await self._buy("alice", "timed")
        self.assertEqual((again["item"]["expires_at"], again["item"]["obtainable"]), (self.now + 30 * DAY, "renew"))
        self.assertEqual(len(await self._rows("alice", "timed")), 2)

    async def test_sweep_unequips_and_tells_the_owner_exactly_once(self) -> None:
        self.ledger.donate(OID_A, 200)
        await self._buy("alice", "timed")
        await self._equip("alice", "bubble.timed")
        await shop_service.drain()
        self.broadcast.reset_mock()
        self.assertEqual(await self._sweep(), {"expired": 0, "expiring": 0})
        # 到期前 3 天提醒一次
        self.tick(days=27)
        self.assertEqual(await self._sweep(), {"expired": 0, "expiring": 1})
        self.assertEqual(await self._sweep(), {"expired": 0, "expiring": 0})
        brief = {"sku": "bubble.timed", "name": "限时", "category": "bubble"}
        self.assertEqual(await self._events("shop.item.expiring"), [(self.ids["alice"], {"item": brief, "expires_at": START + 30 * DAY})])
        self.tick(days=3)
        self.assertEqual(await self._sweep(), {"expired": 1, "expiring": 0})
        self.assertEqual(await self._sweep(), {"expired": 0, "expiring": 0})
        self.assertEqual(await self._events("shop.item.expired"), [(self.ids["alice"], {"item": brief})])
        self.assertEqual(len(await self._events("shop.item.expiring")), 1)
        # 卸下：users.cosmetics 里真的删掉了，并且照常广播 user.cosmetics.updated
        self.assertIsNone((await self._user("alice")).cosmetics)
        self.assertEqual(await self._broadcasts("user.cosmetics.updated"), [{"user_id": self.ids["alice"], "cosmetics": {}}])

    async def test_renewing_restarts_the_reminder_and_keeps_the_item_equipped(self) -> None:
        self.ledger.donate(OID_A, 200)
        await self._buy("alice", "timed")
        await self._equip("alice", "bubble.timed")
        self.tick(days=28)
        self.assertEqual((await self._sweep())["expiring"], 1)
        await self._buy("alice", "timed")
        # 续期之后装备着的那一件跟着延长：原来的到期时间过了也还在
        self.tick(days=5)
        self.assertEqual(await self._sweep(), {"expired": 0, "expiring": 0})
        self.assertEqual(set((await self._public("alice"))["cosmetics"]), {"bubble"})
        self.tick(days=25)
        self.assertEqual(await self._sweep(), {"expired": 0, "expiring": 1})
        self.assertEqual([data["expires_at"] for _user, data in await self._events("shop.item.expiring")], [START + 30 * DAY, START + 60 * DAY])
        self.tick(days=3)
        self.assertEqual(await self._sweep(), {"expired": 1, "expiring": 0})
        self.assertEqual((await self._public("alice"))["cosmetics"], {})

    async def test_short_lived_items_skip_the_reminder(self) -> None:
        await self._as("admin", shop_service.admin_grant, self.ids["carol"], self.items["perm"], None, 2)
        self.assertEqual(await self._expiry("carol", "perm"), START + 2 * DAY)
        self.assertEqual(await self._sweep(), {"expired": 0, "expiring": 0})
        self.tick(days=2)
        self.assertEqual(await self._sweep(), {"expired": 1, "expiring": 0})

    async def test_concurrent_renewals_do_not_lose_days(self) -> None:
        self.ledger.donate(OID_A, 1000)
        await self._buy("alice", "timed")
        results = await asyncio.gather(*(self._buy("alice", "timed") for _ in range(6)))
        self.assertEqual(sorted(result["item"]["expires_at"] for result in results), [START + 30 * count * DAY for count in range(2, 8)])
        self.assertEqual((await self._expiry("alice", "timed"), await self._coins("alice"), len(await self._rows("alice", "timed"))), (START + 210 * DAY, 650, 7))
        # 不花币的路径（后台赠送）没有扣款那条语句开路，靠接替那条带条件的 UPDATE 和重试：一样不丢天数
        grants = [self._as("admin", shop_service.admin_grant, self.ids["bob"], self.items["timed"], None, 10) for _ in range(3)]
        await asyncio.gather(*grants)
        self.assertEqual((await self._expiry("bob", "timed"), len(await self._rows("bob", "timed"))), (START + 30 * DAY, 3))

    async def test_two_first_purchases_at_once_keep_both_periods(self) -> None:
        self.ledger.donate(OID_A, 1000)
        await asyncio.gather(*(self._buy("alice", "timed") for _ in range(3)))
        self.assertEqual((await self._expiry("alice", "timed"), await self._coins("alice")), (START + 90 * DAY, 850))
        # 永久商品同时买两次：只成一次，只扣一次
        results = await asyncio.gather(*(self._buy("alice", "perm") for _ in range(3)), return_exceptions=True)
        self.assertEqual(sorted("ok" if isinstance(result, dict) else result.detail["code"] for result in results), ["already_owned", "already_owned", "ok"])
        self.assertEqual((await self._coins("alice"), len(await self._rows("alice", "perm"))), (790, 1))

    async def test_changing_the_duration_only_affects_later_purchases(self) -> None:
        self.ledger.donate(OID_A, 500)
        await self._buy("alice", "timed")
        await self._equip("alice", "bubble.timed")
        edited = await self._as("admin", shop_service.admin_update_item, self.items["timed"], {"duration_days": 7})
        self.assertEqual((edited["duration_days"], await self._expiry("alice", "timed")), (7, START + 30 * DAY))
        await self._buy("alice", "timed")
        self.assertEqual(await self._expiry("alice", "timed"), START + 37 * DAY)
        # 改成永久：已有的记录不动，还是限时的；再买一次就换成永久的（装备着的那一件也不再到期）
        edited = await self._as("admin", shop_service.admin_update_item, self.items["timed"], {"duration_days": None})
        self.assertEqual((edited["duration_days"], await self._expiry("alice", "timed")), (None, START + 37 * DAY))
        self.assertEqual((await self._overview_item("alice", "bubble.timed"))["obtainable"], "renew")
        bought = await self._buy("alice", "timed")
        self.assertEqual((bought["item"]["expires_at"], bought["item"]["obtainable"]), (None, "owned"))
        self.tick(days=400)
        self.assertEqual(set((await self._public("alice"))["cosmetics"]), {"bubble"})
        self.assertEqual(await self._code(self._buy("alice", "timed")), (409, "already_owned"))
        for value in (0, -1, 3651, "30", True):
            status_code, code, detail = await self._fails(self._as("admin", shop_service.admin_update_item, self.items["perm"], {"duration_days": value}))
            self.assertEqual((status_code, code, detail["field"]), (422, "invalid_item", "duration_days"), value)

    async def test_admin_grant_durations(self) -> None:
        grant = shop_service.admin_grant
        # 不传：按商品的设置
        row = await self._as("admin", grant, self.ids["carol"], self.items["timed"], "活动")
        self.assertEqual((row["source"], row["duration_days"], row["expires_at"]), ("grant", 30, START + 30 * DAY))
        # 给已经有这件限时商品的人再送：续期
        row = await self._as("admin", grant, self.ids["carol"], self.items["timed"], None, 7)
        self.assertEqual((row["duration_days"], row["expires_at"]), (7, START + 37 * DAY))
        # 0：送成永久
        row = await self._as("admin", grant, self.ids["carol"], self.items["timed"], None, 0)
        self.assertEqual((row["duration_days"], row["expires_at"]), (None, None))
        self.assertEqual(await self._code(self._as("admin", grant, self.ids["carol"], self.items["timed"], None)), (409, "already_owned"))
        # 永久商品另外指定天数
        row = await self._as("admin", grant, self.ids["bob"], self.items["perm"], None, 3)
        self.assertEqual(row["expires_at"], START + 3 * DAY)
        for value in (-1, 3651):
            status_code, code, detail = await self._fails(self._as("admin", grant, self.ids["bob"], self.items["perm"], None, value))
            self.assertEqual((status_code, code, detail["field"]), (422, "invalid_duration", "duration_days"))
        self.assertEqual([details["duration_days"] for action, details in await self._audit() if action == "shop.grant"], [30, 7, None, 3])
        self.assertEqual(len(await self._events("shop.item.granted")), 4)

    async def test_revoking_a_timed_item_ends_ownership(self) -> None:
        self.ledger.donate(OID_A, 200)
        await self._buy("alice", "timed")
        await self._buy("alice", "timed")
        await self._equip("alice", "bubble.timed")
        row = await self._as("admin", shop_service.admin_revoke, self.ids["alice"], self.items["timed"], "收回", True)
        # 收回的是当前的那一行：只退最后一次的钱
        self.assertEqual((row["refunded"], await self._coins("alice"), await self._live("alice", "timed")), (True, 150, None))
        self.assertEqual(((await self._public("alice"))["cosmetics"], (await self._overview_item("alice", "bubble.timed"))["owned"]), ({}, False))
        self.assertEqual((await self._buy("alice", "timed"))["item"]["expires_at"], START + 30 * DAY)

    async def test_level_unlocks_follow_the_item_duration(self) -> None:
        async with self.sessions() as db:
            item = await db.get(ShopItem, self.items["timed"])
            item.unlock_level, item.price = 2, 0
            await db.commit()
            granted = await shop_service.on_level_up(db, self.ids["carol"], 2)
        self.assertEqual([item["sku"] for item in granted], ["bubble.timed"])
        live = await self._live("carol", "timed")
        self.assertEqual((live.source, live.duration_days, shop_service._aware(live.expires_at)), ("level", 30, START + 30 * DAY))
        self.tick(days=31)
        # 到期之后不会再自动发，但等级够的人可以自己去商店再领
        async with self.sessions() as db:
            self.assertEqual(await shop_service.on_level_up(db, self.ids["carol"], 3), [])
            (await db.get(User, self.ids["carol"])).account_level = 3
            await db.commit()
        self.assertEqual((await self._overview_item("carol", "bubble.timed"))["obtainable"], "level")
        self.assertEqual((await self._buy("carol", "timed"))["item"]["expires_at"], self.now + 30 * DAY)

    async def test_default_frames_include_the_two_timed_ones(self) -> None:
        async with self.sessions() as db:
            await shop_service.ensure_default_items(db)
            rows = await db.execute(select(ShopItem.sku, ShopItem.price, ShopItem.duration_days).where(ShopItem.sku.like("frame.%")))
            frames = {sku: (price, days) for sku, price, days in rows.all()}
            # 种子只在不存在时插入：改过的不动
            (await db.execute(select(ShopItem).where(ShopItem.sku == "frame.angel"))).scalar_one().price = 999
            await db.commit()
            self.assertEqual(await shop_service.ensure_default_items(db), 0)
            self.assertEqual(await db.scalar(select(ShopItem.price).where(ShopItem.sku == "frame.angel")), 999)
        self.assertEqual((frames["frame.angel"], frames["frame.devil"]), ((120, 30), (50, 30)))
        self.assertEqual({days for sku, (_price, days) in frames.items() if sku not in ("frame.angel", "frame.devil")}, {None})

    async def test_style_edits_keep_the_expiry_of_equipped_items(self) -> None:
        self.ledger.donate(OID_A, 200)
        await self._buy("alice", "timed")
        await self._equip("alice", "bubble.timed")
        new_style = {**STYLE, "text": "#000000"}
        await self._as("admin", shop_service.admin_update_item, self.items["timed"], {"style": new_style})
        self.assertEqual((await self._public("alice"))["cosmetics"]["bubble"]["style"]["text"], "#000000")
        self.tick(days=31)
        self.assertEqual((await self._public("alice"))["cosmetics"], {})


# ---------------------------------------------------------------- 送礼


class SendTests(GiftTestCase):
    async def test_send_in_a_group_charges_and_posts_a_message(self) -> None:
        self.ledger.donate(OID_A, 300)
        result = await self._send(count=3, message="  生日\n快乐！​ ")
        record, message, balance = result["record"], result["message"], result["balance"]
        self.assertEqual((record["kind"], record["count"], record["amount"], record["message"], record["on_birthday"]), ("gift", 3, 60, "生日 快乐！", False))
        self.assertEqual(record["gift"], {"key": "cake", "name": "蛋糕", "icon": "cake", "price": 20, "tier": 2})
        self.assertEqual((record["item"], record["conversation_id"], record["message_id"], record["created_at"]), (None, self.group_id, message.id, START))
        # 记录里收礼人的心意值是送完之后的新值
        self.assertEqual((record["from_user"].username, record["to_user"].username, record["to_user"].charm), ("alice", "bob", 60))
        self.assertEqual((balance["balance"], balance["gift_spent"], balance["spent"], balance["donated"]), (240, 60, 0, 300))
        self.assertEqual((await self._charm("bob"), await self._charm("alice")), (60, 0))
        # 会话里落一条 gift 消息；正文是给旧客户端的兜底文字
        self.assertEqual((message.type, message.content, message.sender_id), (MessageType.GIFT, "[礼物] 送给 阿夸 蛋糕 ×3", self.ids["alice"]))
        self.assertEqual(
            json.loads(message.metadata_json),
            {
                "gift": {
                    "id": record["id"],
                    "kind": "gift",
                    "gift": {"key": "cake", "name": "蛋糕", "icon": "cake", "price": 20, "tier": 2},
                    "item": None,
                    "count": 3,
                    "to_user": {"id": self.ids["bob"], "display_name": "阿夸", "avatar_url": None},
                    "message": "生日 快乐！",
                }
            },
        )
        # 事件：收礼人收到 gift.received（带留言和新的心意值），送礼人收到 shop.balance.changed
        received = await self._events("gift.received")
        self.assertEqual([user for user, _data in received], [self.ids["bob"]])
        self.assertEqual((received[0][1]["record"]["id"], received[0][1]["record"]["message"], received[0][1]["record"]["to_user"].charm), (record["id"], "生日 快乐！", 60))
        changed = await self._events("shop.balance.changed")
        self.assertEqual([(user, data["reason"], data["change"], data["gift_id"], data["balance"]["balance"]) for user, data in changed], [(self.ids["alice"], "gift", "gift.sent", record["id"], 240)])
        await self._assert_conserved()

    async def test_send_from_a_profile_leaves_no_message(self) -> None:
        self.ledger.donate(OID_A, 300)
        before = await self._count(Message)
        # eve 和 alice 没有任何共同的会话：从个人空间 / 资料卡送不要求
        result = await self._send(to="eve", conversation_id=None, gift_key="flower", count=99, message="路过")
        self.assertEqual((result["message"], result["record"]["conversation_id"], result["record"]["message_id"], result["record"]["amount"]), (None, None, None, 99))
        self.assertEqual((await self._count(Message), await self._charm("eve"), await self._coins("alice")), (before, 99, 201))
        self.assertEqual([user for user, _data in await self._events("gift.received")], [self.ids["eve"]])
        # 私聊里送
        direct = await self._send(conversation_id=self.direct_id, gift_key="rocket")
        self.assertEqual((direct["message"].conversation_id, direct["message"].content, direct["record"]["message"]), (self.direct_id, "[礼物] 送给 阿夸 火箭", None))
        self.assertEqual(json.loads(direct["message"].metadata_json)["gift"]["message"], "")
        await self._assert_conserved()

    async def test_target_and_membership_errors(self) -> None:
        self.ledger.donate(OID_A, 300)
        await self._configure(enabled=False)
        self.assertEqual(await self._code(self._send()), (403, "disabled"))
        await self._configure(enabled=True)
        for target in (self.ids["alice"], 987654, self.ids["robot"], None, "2", True, -1):
            self.assertEqual(await self._code(self._send(to_user_id=target)), (422, "invalid_target"), target)
        # 双方都得在会话里：eve 不在群里；私聊里的对方只能是收礼人；会话不存在
        self.assertEqual(await self._code(self._send(to="eve")), (403, "not_member"))
        self.assertEqual(await self._code(self._send("dave", "bob", conversation_id=self.direct_id)), (403, "not_member"))
        self.assertEqual(await self._code(self._send(to="carol", conversation_id=self.direct_id)), (403, "not_member"))
        for conversation in (987654, "x", 0):
            self.assertEqual(await self._code(self._send(conversation_id=conversation)), (403, "not_member"), conversation)
        # 没绑定 CCW
        self.assertEqual(await self._code(self._send("carol", "bob")), (403, "ccw_not_bound"))
        self.assertEqual((await self._count(GiftRecord), await self._coins("alice"), await self._charm("bob")), (0, 0, 0))

    async def test_mutes_and_blocks(self) -> None:
        self.ledger.donate(OID_A, 300)
        self.ledger.donate(OID_D, 300)
        # 禁言到期是和真实的现在比的（和发消息那条路径一致），所以这里用真实时间
        until = datetime.now(UTC) + timedelta(hours=1)
        async with self.sessions() as db:
            (await db.get(User, self.ids["alice"])).muted_until = until
            await db.commit()
        # 平台禁言：在哪儿都不能送，从个人空间送也不行
        for conversation in (self.group_id, None):
            status_code, code, detail = await self._fails(self._send(conversation_id=conversation))
            self.assertEqual((status_code, code, detail["muted_until"]), (403, "muted", until.isoformat()))
        async with self.sessions() as db:
            (await db.get(User, self.ids["alice"])).muted_until = None
            member = (await db.execute(select(ConversationMember).where(ConversationMember.conversation_id == self.group_id, ConversationMember.user_id == self.ids["alice"]))).scalar_one()
            member.muted = True
            await db.commit()
        # 群内禁言只管在这个群里送
        self.assertEqual(await self._code(self._send()), (403, "muted"))
        await self._send(conversation_id=None)
        async with self.sessions() as db:
            member = (await db.execute(select(ConversationMember).where(ConversationMember.conversation_id == self.group_id, ConversationMember.user_id == self.ids["alice"]))).scalar_one()
            member.muted = False
            (await db.get(Conversation, self.group_id)).all_muted = True
            await db.commit()
        self.assertEqual(await self._code(self._send()), (403, "muted"))
        # 群主、管理员不受群内禁言限制
        await self._send("dave", "bob")
        # 被对方拉黑：不能送给 TA；反过来不受影响
        async with self.sessions() as db:
            db.add(TemporaryConversationBlock(blocker_id=self.ids["bob"], blocked_user_id=self.ids["alice"]))
            await db.commit()
        for conversation in (None, self.direct_id):
            self.assertEqual(await self._code(self._send(conversation_id=conversation)), (403, "blocked"))
        self.ledger.donate(OID_B, 50)
        await self._send("bob", "alice", conversation_id=None)
        self.assertEqual(await self._count(GiftRecord), 3)
        await self._assert_conserved()

    async def test_gift_count_and_item_validation(self) -> None:
        self.ledger.donate(OID_A, 300)
        async with self.sessions() as db:
            db.add(Gift(key="retired", name="下架的", price=9, icon="star", tier=1, is_active=False))
            db.add(ShopItem(sku="bubble.gone", category="bubble", name="过季", price=10, style=json.dumps(STYLE), available_until=START - DAY))
            await db.commit()
        for overrides in ({"kind": "coins"}, {"kind": None}, {"gift_key": "nope"}, {"gift_key": None}, {"gift_key": 5}, {"gift_key": "retired"}):
            self.assertEqual(await self._code(self._send(**overrides)), (422, "invalid_gift"), overrides)
        for count in (0, 100, -1, "3", 1.5, None, True):
            status_code, code, detail = await self._fails(self._send(count=count))
            self.assertEqual((status_code, code, detail["count_max"]), (422, "invalid_count", 99), count)
        self.assertEqual(await self._code(self._send_item("bubble.perm", count=2)), (422, "invalid_count"))
        # 不存在的、不卖的（价格 0）、已下架的、过了限时销售期的商品都不能送
        for sku in ("bubble.nope", None, "bubble.free", "bubble.off", "bubble.gone"):
            self.assertEqual(await self._code(self._send_item(sku)), (422, "not_giftable"), sku)
        self.assertEqual((await self._count(GiftRecord), await self._coins("alice")), (0, 0))

    async def test_message_length_and_moderation(self) -> None:
        self.ledger.donate(OID_A, 300)
        status_code, code, detail = await self._fails(self._send(message="字" * 61))
        self.assertEqual((status_code, code, detail["message_max"]), (422, "message_rejected", 60))
        self.assertEqual(await self._code(self._send(message=123)), (422, "message_rejected"))
        await self._send(message="字" * 60)
        await self._configure(message_max=5)
        self.assertEqual(await self._code(self._send(message="六个字的留言")), (422, "message_rejected"))
        await self._configure(message_max=60)
        calls: list[tuple[str, int, str]] = []

        async def moderate(content: str, user_id: int, *, context_id: str) -> dict[str, Any]:
            calls.append((content, user_id, context_id))
            if "炸" in content:
                raise OSError("审核服务不通")
            return {"decision": "block" if "坏" in content else "allow"}

        with patch.object(settings, "content_moderation_enabled", True), patch.object(moderation_service, "_call_text_moderation_for_user", moderate):
            self.assertEqual(await self._code(self._send(message="坏话")), (422, "message_rejected"))
            await self._send(message="好话")
            # 审核服务不通时放行；没写留言的不送审
            await self._send(message="炸了")
            await self._send(message=None)
            await self._send(message="   ")
        self.assertEqual(calls, [("坏话", self.ids["alice"], f"gift:{self.ids['bob']}"), ("好话", self.ids["alice"], f"gift:{self.ids['bob']}"), ("炸了", self.ids["alice"], f"gift:{self.ids['bob']}")])
        self.assertEqual([record.message for record in await self._records()], ["字" * 60, "好话", "炸了", None, None])
        await self._assert_conserved()

    async def test_gift_messages_are_moderated_by_the_note_only(self) -> None:
        self.ledger.donate(OID_A, 300)
        with_note = (await self._send(message="留言内容"))["message"]
        without = (await self._send())["message"]
        self.assertEqual(moderation_service._text_to_moderate(with_note, json.loads(with_note.metadata_json)), "留言内容")
        self.assertIsNone(moderation_service._text_to_moderate(without, json.loads(without.metadata_json)))

    async def test_daily_limit_counts_sends_per_local_day(self) -> None:
        self.ledger.donate(OID_A, 5000)
        await self._configure(daily_send_limit=3)
        # 北京时间 10 月 12 日 23:59
        self.now = datetime(2026, 10, 12, 15, 59, tzinfo=UTC)
        await self._send(count=99, gift_key="flower")
        await self._send_item("bubble.perm")
        await self._send(conversation_id=None)
        catalog = await self._as("alice", gift_service.catalog_view)
        self.assertEqual((catalog["sent_today"], catalog["limits"]["daily_send_limit"]), (3, 3))
        status_code, code, detail = await self._fails(self._send())
        self.assertEqual((status_code, code, detail["daily_send_limit"]), (429, "daily_limit", 3))
        # 礼物和商品合在一起、按次数算；别人不受影响；过了零点重新算
        self.ledger.donate(OID_D, 100)
        await self._send("dave", "bob")
        self.tick(seconds=61)
        self.assertEqual((await self._as("alice", gift_service.catalog_view))["sent_today"], 0)
        await self._send()
        self.assertEqual(await self._count(GiftRecord), 5)

    async def test_daily_limit_holds_under_concurrency(self) -> None:
        self.ledger.donate(OID_A, 5000)
        await self._configure(daily_send_limit=3)
        results = await asyncio.gather(*(self._send(conversation_id=None) for _ in range(8)), return_exceptions=True)
        codes = sorted("ok" if isinstance(result, dict) else result.detail["code"] for result in results)
        self.assertEqual(codes, ["daily_limit"] * 5 + ["ok"] * 3)
        self.assertEqual((await self._count(GiftRecord), await self._coins("alice"), await self._charm("bob")), (3, 4940, 60))

    async def test_balance_errors_charge_nothing(self) -> None:
        self.ledger.donate(OID_A, 50)
        status_code, code, detail = await self._fails(self._send(count=3))
        self.assertEqual((status_code, code, detail["balance"], detail["shortfall"], detail["total"]), (402, "insufficient_balance", 50, 10, 60))
        self.assertEqual(await self._code(self._send_item("bubble.perm")), (402, "insufficient_balance"))
        # 核实不了余额：503，不扣币
        self.tick(seconds=60)
        self.ledger.error = OSError("down")
        self.assertEqual(await self._code(self._send()), (503, "verification_unavailable"))
        self.ledger.error = None
        integrations.set_coin_ledger(FakeCoinLedger(source_id=None))
        self.assertEqual(await self._code(self._send()), (503, "verification_unavailable"))
        integrations.set_coin_ledger(self.ledger)
        self.assertEqual((await self._count(GiftRecord), await self._count(Message), await self._charm("bob"), len(await self._events("gift.received"))), (0, 0, 0, 0))
        self.tick(seconds=60)
        await self._send(count=2)
        self.assertEqual(await self._coins("alice"), 10)

    async def test_a_rejected_message_rolls_everything_back(self) -> None:
        """发消息那一步被拒（这里是群的发言限频）：扣款、记录、心意值、对方的拥有记录一起回滚。"""
        self.ledger.donate(OID_A, 300)
        async with self.sessions() as db:
            (await db.get(Conversation, self.group_id)).message_rate_limit_per_minute = 1
            await db.commit()
        await self._send()
        self.assertEqual(await self._code(self._send()), (429, "rate_limited"))
        self.assertEqual(await self._code(self._send_item("bubble.timed")), (429, "rate_limited"))
        self.assertEqual((await self._count(GiftRecord), await self._count(Message), await self._coins("alice"), await self._charm("bob")), (1, 1, 280, 20))
        self.assertEqual((await self._count(ShopPurchase), len(await self._events("gift.received"))), (0, 1))
        # 从个人空间送不落消息，不受发言限频约束
        await self._send(conversation_id=None)
        await self._assert_conserved()

    async def test_gift_messages_cannot_be_sent_forwarded_or_copied(self) -> None:
        self.ledger.donate(OID_A, 300)
        with self.assertRaises(ValidationError):
            MessageCreate(type="gift", content="[礼物]")
        sent = await self._send(message="心意")
        async with self.sessions() as db:
            alice = await db.get(User, self.ids["alice"])
            with self.assertRaises(HTTPException) as caught:
                await message_service.send_message(db, self.group_id, alice, "[礼物]", MessageType.GIFT, {"gift": {"id": 1}})
            self.assertEqual(caught.exception.status_code, 422)
            plain = await message_service.send_message(db, self.group_id, alice, "普通消息")
            plain_id = plain.id
            before = await self._count(Message)
            for message_ids in ([sent["message"].id], [plain_id, sent["message"].id]):
                with self.assertRaises(HTTPException) as caught:
                    await message_service.create_forward_bundle(db, self.direct_id, alice, self.group_id, message_ids)
                self.assertEqual((caught.exception.status_code, caught.exception.detail["code"]), (422, "gift_not_forwardable"))
            with self.assertRaises(HTTPException) as caught:
                await message_service.send_message(db, self.direct_id, alice, "[礼物] 送给 阿夸 蛋糕", MessageType.TEXT, json.loads(sent["message"].metadata_json))
            self.assertEqual((caught.exception.status_code, caught.exception.detail["code"]), (422, "gift_not_forwardable"))
            # 不带礼物的照常能转；引用礼物消息也照常
            await message_service.create_forward_bundle(db, self.direct_id, alice, self.group_id, [plain_id])
            await message_service.send_message(db, self.group_id, alice, "谢谢", MessageType.TEXT, {"reply_to": {"message_id": sent["message"].id}})
        self.assertEqual(await self._count(Message), before + 2)

    async def test_recalling_the_message_refunds_nothing(self) -> None:
        self.ledger.donate(OID_A, 300)
        sent = await self._send_item("bubble.timed", message="拿去")
        async with self.sessions() as db:
            recalled = await message_service.recall_message(db, self.group_id, self.ids["alice"], sent["message"].id)
            self.assertIsNotNone(recalled.recalled_at)
        self.assertEqual((await self._coins("alice"), await self._charm("bob"), await self._count(GiftRecord), await self._expiry("bob", "timed")), (250, 50, 1, START + 30 * DAY))
        await self._assert_conserved()

    async def test_catalog_defaults(self) -> None:
        async with self.sessions() as db:
            await db.execute(text("DELETE FROM system_settings WHERE key LIKE 'gifts.%'"))
            db.add(Gift(key="retired", name="下架的", price=9, icon="star", tier=1, is_active=False, sort=5))
            await db.commit()
            # 种子是幂等的：已经有的不动
            (await db.execute(select(Gift).where(Gift.key == "cake"))).scalar_one().price = 25
            await db.commit()
            self.assertEqual(await gift_service.ensure_default_gifts(db), 0)
        gift_service.reset_cache()
        catalog = await self._as("carol", gift_service.catalog_view)
        # 线上默认关；关着也能看目录
        self.assertEqual((catalog["enabled"], catalog["charm_label"], catalog["sent_today"]), (False, "心意值", 0))
        self.assertEqual(catalog["limits"], {"message_max": 60, "count_max": 99, "daily_send_limit": 200})
        self.assertEqual(
            [(gift["key"], gift["name"], gift["price"], gift["icon"], gift["tier"], gift["is_active"], gift["sort"]) for gift in catalog["gifts"]],
            [
                ("flower", "小花", 1, "flower", 1, True, 10),
                ("lollipop", "棒棒糖", 5, "lollipop", 1, True, 20),
                ("cake", "蛋糕", 25, "cake", 2, True, 30),
                ("trophy", "奖杯", 50, "trophy", 2, True, 40),
                ("rocket", "火箭", 100, "rocket", 3, True, 50),
                ("castle", "城堡", 300, "castle", 3, True, 60),
            ],
        )
        self.ledger.donate(OID_A, 300)
        self.assertEqual(await self._code(self._send()), (403, "disabled"))


class ItemGiftTests(GiftTestCase):
    async def test_the_three_cases_of_giving_an_item(self) -> None:
        self.ledger.donate(OID_A, 1000)
        # 一、对方没有：按现价扣币，对方直接拥有（不自动装备），心意值 = 价格
        first = await self._send_item("bubble.timed", message="戴上试试")
        record = first["record"]
        self.assertEqual((record["kind"], record["count"], record["amount"], record["gift"]), ("item", 1, 50, None))
        self.assertEqual(record["item"], {"sku": "bubble.timed", "name": "限时", "category": "bubble", "style": STYLE, "duration_days": 30})
        self.assertEqual(first["message"].content, "[礼物] 送给 阿夸 「限时」")
        self.assertEqual((await self._coins("alice"), await self._charm("bob"), await self._expiry("bob", "timed")), (950, 50, START + 30 * DAY))
        self.assertEqual(((await self._public("bob"))["cosmetics"], (await self._overview_item("bob", "bubble.timed"))["owned"]), ({}, True))
        # 送礼人自己并不因此拥有
        self.assertIsNone(await self._live("alice", "timed"))
        # 二、对方已有这件限时商品：续期
        self.tick(days=10)
        await self._send_item("bubble.timed", conversation_id=None)
        self.assertEqual((await self._expiry("bob", "timed"), await self._charm("bob")), (START + 60 * DAY, 100))
        # 三、对方已经永久拥有：409，不扣币
        await self._send_item("bubble.perm")
        self.assertEqual(await self._expiry("bob", "perm"), None)
        self.assertEqual(await self._code(self._send_item("bubble.perm")), (409, "recipient_owns"))
        self.assertEqual((await self._coins("alice"), await self._charm("bob"), await self._count(GiftRecord)), (840, 160, 3))
        # 收礼人的购买记录里 source = gift，price 0，带送礼人
        async with self.sessions() as db:
            mine = await shop_service.list_purchases(db, self.ids["bob"], 50, 0)
            admin = await shop_service.admin_list_purchases(db, user_id=self.ids["bob"], item_id=None, limit=50, offset=0)
        self.assertEqual({(row["source"], row["price"], row["from_user"].username) for row in mine["items"]}, {("gift", 0, "alice")})
        self.assertEqual([row["item"]["sku"] for row in mine["items"]], ["bubble.perm", "bubble.timed", "bubble.timed"])
        self.assertEqual({row["from_user"]["username"] for row in admin["items"]}, {"alice"})
        # 别人送的商品不计入收礼人的已花，也不算「卖出」
        self.ledger.donate(OID_B, 0)
        self.assertEqual(((await self._balance("bob"))["spent"], (await self._balance("alice"))["spent"], (await self._balance("alice"))["gift_spent"]), (0, 0, 160))
        # 只发 gift.received，不另发 shop.item.granted
        self.assertEqual((len(await self._events("gift.received")), await self._events("shop.item.granted")), (3, []))
        await self._assert_conserved()

    async def test_gifting_extends_what_the_recipient_bought_and_wears(self) -> None:
        self.ledger.donate(OID_A, 500)
        self.ledger.donate(OID_B, 500)
        await self._buy("bob", "timed")
        await self._equip("bob", "bubble.timed")
        await self._send_item("bubble.timed")
        self.assertEqual(await self._expiry("bob", "timed"), START + 60 * DAY)
        self.tick(days=45)
        self.assertEqual(set((await self._public("bob"))["cosmetics"]), {"bubble"})
        # 限时的拥有者收到同一件的永久版（商品后来改成了永久）：换成永久
        await self._as("admin", shop_service.admin_update_item, self.items["timed"], {"duration_days": None})
        await self._send_item("bubble.timed")
        self.assertEqual((await self._expiry("bob", "timed"), (await self._live("bob", "timed")).source), (None, "gift"))
        self.assertEqual(await self._code(self._send_item("bubble.timed")), (409, "recipient_owns"))
        await self._assert_conserved()

    async def test_the_price_charged_is_the_current_one(self) -> None:
        self.ledger.donate(OID_A, 500)
        await self._as("admin", shop_service.admin_update_item, self.items["perm"], {"price": 75})
        self.assertEqual((await self._send_item("bubble.perm"))["record"]["amount"], 75)
        # 礼物改价、改名不影响已经送出的记录
        await self._send(count=2)
        await self._as("admin", gift_service.admin_update_gift, "cake", {"price": 30, "name": "大蛋糕"})
        await self._send(count=2)
        self.assertEqual([(record.amount, record.gift_name) for record in (await self._records())[1:]], [(40, "蛋糕"), (60, "大蛋糕")])
        async with self.sessions() as db:
            page = await gift_service.wall_records(db, await db.get(User, self.ids["carol"]), self.ids["bob"], gift_key="cake", item_sku=None, limit=10, offset=0)
        self.assertEqual([(row["gift"]["name"], row["gift"]["price"]) for row in page["items"]], [("大蛋糕", 30), ("蛋糕", 20)])
        await self._assert_conserved()


# ---------------------------------------------------------------- 余额与守恒


class BalanceTests(GiftTestCase):
    async def test_gifts_are_one_more_term_of_the_formula(self) -> None:
        self.ledger.donate(OID_A, 500)
        self.ledger.donate(OID_B, 100)
        await self._as("bob", shop_service.refresh_balance)
        await self._buy("alice", "perm")
        await self._send(count=2)
        await self._send_item("bubble.timed", conversation_id=None)
        packet = await self._as("alice", rp.send_red_packet, {"conversation_id": self.group_id, "kind": "normal", "amount": 10, "count": 3, "greeting": None})
        await self._as("bob", rp.claim, packet[0]["red_packet"]["id"])
        alice, bob = await self._balance("alice"), await self._balance("bob")
        self.assertEqual((alice["donated"], alice["spent"], alice["gift_spent"], alice["red_packet_sent"], alice["balance"]), (500, 60, 90, 30, 320))
        # 送礼是真消耗：收礼人不得币，只涨心意值
        self.assertEqual((bob["balance"], bob["gift_spent"], bob["red_packet_received"], await self._charm("bob")), (110, 0, 10, 90))
        async with self.sessions() as db:
            accounts = await shop_service.admin_list_accounts(db, q=None, mismatch=None, limit=50, offset=0)
        rows = {row["user"]["username"]: (row["gift_spent"], row["balance"], row["mismatch"]) for row in accounts["items"]}
        self.assertEqual((rows["alice"], rows["bob"]), ((90, 320, False), (0, 110, False)))
        await self._assert_conserved()

    async def test_gifted_coins_are_gone_for_every_way_of_spending(self) -> None:
        self.ledger.donate(OID_A, 100)
        await self._send(count=3)
        self.assertEqual(await self._code(self._buy("alice", "perm")), (402, "insufficient_coins"))
        packet = self._as("alice", rp.send_red_packet, {"conversation_id": self.group_id, "kind": "normal", "amount": 41, "count": 1, "greeting": None})
        self.assertEqual(await self._code(packet), (402, "insufficient_balance"))
        self.assertEqual(await self._code(self._send(count=3)), (402, "insufficient_balance"))
        await self._send(count=2)
        self.assertEqual((await self._coins("alice"), await self._charm("bob")), (0, 100))

    async def test_a_ccw_account_cannot_be_gifted_twice_by_moving_it(self) -> None:
        """换绑防刷：记录上记着送的时候绑定的 CCW 账号；同一个 CCW 账号在别的 KukeChat 账号上送礼花掉的，算进这个账号的已花。"""
        self.ledger.donate(OID_A, 100)
        await self._send(count=3)
        self.assertEqual([record.ccw_oid for record in await self._records()], [OID_A])
        # alice 解绑，eve 绑上同一个 CCW 账号：那 60 已经花掉了
        async with self.sessions() as db:
            (await db.get(User, self.ids["alice"])).ccw_student_oid = None
            await db.flush()
            (await db.get(User, self.ids["eve"])).ccw_student_oid = OID_A
            await db.commit()
        self.tick(seconds=60)
        await self._as("eve", shop_service.refresh_balance)
        eve = await self._balance("eve")
        self.assertEqual((eve["donated"], eve["spent"], eve["gift_spent"], eve["balance"]), (100, 60, 0, 40))
        self.assertEqual(await self._code(self._send("eve", "bob", conversation_id=None, count=3)), (402, "insufficient_balance"))
        self.assertEqual(await self._code(self._buy("eve", "perm")), (402, "insufficient_coins"))
        await self._send("eve", "bob", conversation_id=None, count=2)
        self.assertEqual((await self._coins("eve"), await self._charm("bob")), (0, 100))
        # 解绑的那个账号：自己送出去的仍然记在自己名下，余额是负的，什么也花不了
        alice = await self._balance("alice")
        self.assertEqual((alice["bound"], alice["donated"], alice["gift_spent"], alice["balance"]), (False, 0, 60, -60))
        # 再换回来：两个账号花掉的都算
        async with self.sessions() as db:
            (await db.get(User, self.ids["eve"])).ccw_student_oid = None
            await db.flush()
            (await db.get(User, self.ids["alice"])).ccw_student_oid = OID_A
            await db.commit()
        self.tick(seconds=60)
        await self._as("alice", shop_service.refresh_balance)
        alice = await self._balance("alice")
        self.assertEqual((alice["donated"], alice["spent"], alice["gift_spent"], alice["balance"]), (100, 40, 60, 0))
        self.assertEqual(await self._code(self._send(gift_key="flower")), (402, "insufficient_balance"))
        async with self.sessions() as db:
            accounts = await shop_service.admin_list_accounts(db, q="alice", mismatch=None, limit=50, offset=0)
        self.assertEqual([(row["shared_spent"], row["gift_spent"], row["balance"]) for row in accounts["items"]], [(40, 60, 0)])
        await self._assert_conserved()

    async def test_reconcile_checks_the_charm_ledger(self) -> None:
        self.ledger.donate(OID_A, 300)
        await self._send(count=3)
        await self._send_item("bubble.perm", to="carol", conversation_id=None)
        result = await self._as("admin", shop_service.admin_reconcile)
        self.assertEqual(result["gifts"], {"total_amount": 120, "total_count": 2, "total_charm": 120, "total_received": 120, "balanced": True, "problems": []})
        self.assertEqual(result["mismatched"], [])
        # 心意值被改坏了：对账能指出是谁
        async with self.sessions() as db:
            (await db.get(User, self.ids["bob"])).charm = 61
            (await db.get(User, self.ids["dave"])).charm = 5
            await db.commit()
        ledger = await self._ledger()
        self.assertEqual((ledger["balanced"], ledger["total_charm"], ledger["total_received"]), (False, 126, 120))
        self.assertEqual(ledger["problems"], [{"user_id": self.ids["bob"], "charm": 61, "received": 60}, {"user_id": self.ids["dave"], "charm": 5, "received": 0}])


class ConcurrencyTests(GiftTestCase):
    """同一个进程里同时来的请求：各用各的会话和连接。"""

    async def test_two_gifts_cannot_overdraw(self) -> None:
        self.ledger.donate(OID_A, 100)
        await self._as("alice", shop_service.refresh_balance)
        results = await asyncio.gather(*(self._send(count=3, conversation_id=None if index % 2 else self.group_id) for index in range(6)), return_exceptions=True)
        codes = sorted("ok" if isinstance(result, dict) else result.detail["code"] for result in results)
        self.assertEqual(codes, ["insufficient_balance"] * 5 + ["ok"])
        self.assertEqual((await self._coins("alice"), await self._charm("bob"), await self._count(GiftRecord)), (40, 60, 1))
        await self._assert_conserved()

    async def test_gift_purchase_and_red_packet_cannot_overdraw_together(self) -> None:
        self.ledger.donate(OID_A, 100)
        await self._as("alice", shop_service.refresh_balance)
        packet = {"conversation_id": self.group_id, "kind": "normal", "amount": 60, "count": 1, "greeting": None}
        results = await asyncio.gather(
            self._send(count=3),
            self._send_item("bubble.perm", conversation_id=None),
            self._buy("alice", "perm"),
            self._as("alice", rp.send_red_packet, packet),
            self._send(count=3, conversation_id=None),
            return_exceptions=True,
        )
        failures = [result.detail["code"] for result in results if isinstance(result, HTTPException)]
        self.assertEqual((len(failures), set(failures) <= {"insufficient_balance", "insufficient_coins"}), (4, True), results)
        balance = await self._balance("alice")
        self.assertEqual((balance["balance"], balance["spent"] + balance["gift_spent"] + balance["red_packet_sent"]), (40, 60))
        await self._assert_conserved()

    async def test_many_people_gift_the_same_person_at_once(self) -> None:
        for oid in (OID_A, OID_B, OID_D):
            self.ledger.donate(oid, 1000)
        senders = ["alice", "bob", "dave"] * 5
        await asyncio.gather(*(self._send(name, "carol", conversation_id=None, gift_key="trophy") for name in senders))
        self.assertEqual((await self._charm("carol"), await self._count(GiftRecord)), (750, 15))
        self.assertEqual([await self._coins(name) for name in ("alice", "bob", "dave")], [750, 750, 750])
        await self._assert_conserved()

    async def test_gifting_and_buying_the_same_timed_item_at_once(self) -> None:
        self.ledger.donate(OID_A, 1000)
        self.ledger.donate(OID_B, 1000)
        await asyncio.gather(*(self._send_item("bubble.timed", conversation_id=None) for _ in range(3)), *(self._buy("bob", "timed") for _ in range(3)))
        # 三份别人送的、三份自己买的：六个 30 天一天不少
        self.assertEqual((await self._expiry("bob", "timed"), await self._charm("bob"), await self._coins("alice"), await self._coins("bob")), (START + 180 * DAY, 150, 850, 850))
        await self._assert_conserved()


# ---------------------------------------------------------------- 并发（多个进程）


def _spawn_workers(jobs: list[dict[str, Any]]) -> list[Any]:
    """每个 job 起一个进程，等它们都准备好再一起放行；返回各自打印的结果。"""
    environment = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONPATH": str(REPO_ROOT)}
    workers = [
        subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "worker", json.dumps(job)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(REPO_ROOT),
            env=environment,
            text=True,
            encoding="utf-8",
        )
        for job in jobs
    ]
    try:
        for worker in workers:
            line = worker.stdout.readline()
            if line.strip() != "ready":
                raise AssertionError(f"worker did not start: {line!r} {worker.stderr.read()}")
        for worker in workers:
            worker.stdin.write("go\n")
            worker.stdin.flush()
        outputs = []
        for worker in workers:
            out, err = worker.communicate(timeout=120)
            if worker.returncode != 0:
                raise AssertionError(f"worker failed: {err}")
            outputs.append(json.loads(out.strip().splitlines()[-1]))
        return outputs
    finally:
        for worker in workers:
            if worker.poll() is None:
                worker.kill()


class MultiProcessTests(GiftTestCase):
    """线上是两个 worker 进程。这里起三个进程对着同一个库文件，进程之间没有任何共享的锁，只有 SQLite 自己的。"""

    PROCESSES = 3

    def _job(self, mode: str, **extra: Any) -> dict[str, Any]:
        return {"db": str(self.db_path), "mode": mode, "now": self.now.isoformat(), "donations": {OID_A: 100, OID_B: 1000}, "ids": self.ids, "items": self.items, "group": self.group_id, **extra}

    async def _run(self, jobs: list[dict[str, Any]]) -> list[Any]:
        # 子进程要读库文件：先把这边连接池里的连接都放掉，免得挡着
        await self.engine.dispose()
        return await asyncio.to_thread(_spawn_workers, jobs)

    async def test_processes_cannot_double_spend(self) -> None:
        self.ledger.donate(OID_A, 100)
        await self._as("alice", shop_service.refresh_balance)
        # 余额 100：三个进程各送两次 60 的礼物（一次落群消息、一次不落），外加一次 60 的购买、一个 60 的红包，同时来，只成一个
        outputs = await self._run([self._job("spend", extras=index == 0) for index in range(self.PROCESSES)])
        results = [code for output in outputs for code in output]
        self.assertEqual(len(results), 8)
        self.assertEqual(results.count("ok"), 1, results)
        self.assertTrue(set(results) <= {"ok", "insufficient_balance", "insufficient_coins"}, results)
        balance = await self._balance("alice")
        self.assertEqual((balance["balance"], balance["spent"] + balance["gift_spent"] + balance["red_packet_sent"]), (40, 60))
        records = await self._records()
        self.assertEqual(await self._charm("bob"), sum(record.amount for record in records))
        self.assertEqual(len(records) + await self._count(ShopPurchase) + await self._count(rp.RedPacket), 1)
        await self._assert_conserved()

    async def test_processes_gifting_keep_charm_in_step(self) -> None:
        self.ledger.donate(OID_B, 1000)
        await self._as("bob", shop_service.refresh_balance)
        outputs = await self._run([self._job("charm", sends=6) for _ in range(self.PROCESSES)])
        self.assertEqual([code for output in outputs for code in output], ["ok"] * 18)
        # 18 次 20 币的礼物送给同一个人：心意值一分不差，余额一分不差
        self.assertEqual((await self._charm("carol"), await self._coins("bob"), await self._count(GiftRecord)), (360, 640, 18))
        await self._assert_conserved()

    async def test_processes_renewing_do_not_lose_days(self) -> None:
        self.ledger.donate(OID_B, 1000)
        await self._as("bob", shop_service.refresh_balance)
        await self._buy("bob", "timed")
        # 每个进程：自己续两次（花币）、后台给他送两次 10 天的（不花币）
        outputs = await self._run([self._job("renew") for _ in range(self.PROCESSES)])
        self.assertEqual([code for output in outputs for code in output], ["ok"] * 12)
        self.assertEqual(await self._expiry("bob", "timed"), START + (30 + 6 * 30 + 6 * 10) * DAY)
        self.assertEqual((await self._coins("bob"), len(await self._rows("bob", "timed"))), (650, 13))

    async def test_processes_sweep_the_same_expiry_once(self) -> None:
        self.ledger.donate(OID_B, 1000)
        await self._as("bob", shop_service.refresh_balance)
        await self._buy("bob", "timed")
        await self._equip("bob", "bubble.timed")
        for name in ("alice", "carol", "dave", "eve"):
            await self._as("admin", shop_service.admin_grant, self.ids[name], self.items["timed"], None)
        self.tick(days=28)
        outputs = await self._run([self._job("sweep") for _ in range(self.PROCESSES)])
        # 五个人各提醒一次，不管是哪个进程发的；每个进程实际发出去的事件数和它占到的行数一致
        self.assertEqual((sum(output["expiring"] for output in outputs), sum(output["expired"] for output in outputs)), (5, 0))
        self.assertEqual(sum(output["events"] for output in outputs), 5)
        self.tick(days=2)
        outputs = await self._run([self._job("sweep") for _ in range(self.PROCESSES)])
        self.assertEqual((sum(output["expiring"] for output in outputs), sum(output["expired"] for output in outputs)), (0, 5))
        self.assertEqual((sum(output["events"] for output in outputs), sum(output["cosmetics"] for output in outputs)), (5, 1))
        self.assertIsNone((await self._user("bob")).cosmetics)


# ---------------------------------------------------------------- 留言的可见性、礼物墙、心意榜


class VisibilityTests(GiftTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.ledger.donate(OID_A, 1000)
        self.where = {
            "group": (await self._send(message="群里的悄悄话"))["record"]["id"],
            "direct": (await self._send(conversation_id=self.direct_id, message="私聊里的悄悄话"))["record"]["id"],
            "profile": (await self._send(conversation_id=None, message="空间里的悄悄话"))["record"]["id"],
        }
        self.notes = {"group": "群里的悄悄话", "direct": "私聊里的悄悄话", "profile": "空间里的悄悄话"}

    async def _wall_notes(self, viewer: str) -> dict[int, str | None]:
        async with self.sessions() as db:
            page = await gift_service.wall_records(db, await db.get(User, self.ids[viewer]), self.ids["bob"], gift_key=None, item_sku=None, limit=50, offset=0)
        self.assertEqual(page["total"], 3)
        return {row["id"]: row["message"] for row in page["items"]}

    async def test_only_the_two_parties_see_the_note(self) -> None:
        expected = {self.where[place]: self.notes[place] for place in self.where}
        hidden = {record_id: None for record_id in expected}
        # 礼物墙的记录：送的人、收的人看得到留言；第三方（哪怕是同一个群的成员、群主、平台管理员以用户身份看）一律 null
        self.assertEqual(await self._wall_notes("alice"), expected)
        self.assertEqual(await self._wall_notes("bob"), expected)
        for viewer in ("carol", "dave", "eve", "admin"):
            self.assertEqual(await self._wall_notes(viewer), hidden, viewer)
        # 我的记录：两个方向都是当事人
        sent = await self._as("alice", gift_service.list_mine, "sent", 50, 0)
        received = await self._as("bob", gift_service.list_mine, "received", 50, 0)
        self.assertEqual(({row["id"]: row["message"] for row in sent["items"]}, sent["total"], sent["sum"]), (expected, 3, 60))
        self.assertEqual(({row["id"]: row["message"] for row in received["items"]}, received["total"], received["sum"]), (expected, 3, 60))
        self.assertEqual(((await self._as("bob", gift_service.list_mine, "sent", 50, 0))["items"], (await self._as("carol", gift_service.list_mine, "received", 50, 0))["sum"]), ([], 0))
        # 礼物墙本身（聚合）不带留言
        async with self.sessions() as db:
            wall = await gift_service.wall(db, self.ids["bob"])
        self.assertNotIn("悄悄话", json.dumps(wall, ensure_ascii=False, default=str))
        # gift.received 只发给收礼人，带留言
        received_events = await self._events("gift.received")
        self.assertEqual({user for user, _data in received_events}, {self.ids["bob"]})
        self.assertEqual({data["record"]["id"]: data["record"]["message"] for _user, data in received_events}, expected)
        # 余额事件里没有留言
        self.assertNotIn("悄悄话", json.dumps([data for _user, data in await self._events("shop.balance.changed")], ensure_ascii=False, default=str))

    async def test_message_cards_carry_the_note_only_where_the_chat_already_shows_it(self) -> None:
        async with self.sessions() as db:
            rows = (await db.execute(select(Message).where(Message.type == MessageType.GIFT).order_by(Message.id))).scalars().all()
            cards = {message.conversation_id: json.loads(message.metadata_json)["gift"]["message"] for message in rows}
        # 群聊的卡片群成员本来就看得到；私聊的卡片只有两个人；从个人空间送的没有卡片
        self.assertEqual(cards, {self.group_id: "群里的悄悄话", self.direct_id: "私聊里的悄悄话"})
        self.assertTrue(all("悄悄话" not in message.content for message in rows))

    async def test_admin_never_sees_notes_from_private_places(self) -> None:
        async with self.sessions() as db:
            page = await gift_service.admin_records(db, kind=None, from_user_id=None, to_user_id=None, created_from=None, created_to=None, limit=50, offset=0)
        rows = {row["id"]: row for row in page["items"]}
        # 隐私边界：只有群聊里送的给留言；私聊里送的、从个人空间送的一律 null
        self.assertEqual({place: rows[self.where[place]]["message"] for place in self.where}, {"group": "群里的悄悄话", "direct": None, "profile": None})
        self.assertEqual({place: (rows[self.where[place]]["conversation_type"], rows[self.where[place]]["conversation_title"]) for place in self.where}, {"group": ("group", "群"), "direct": ("direct", None), "profile": (None, None)})
        dumped = json.dumps(page, ensure_ascii=False, default=str)
        self.assertNotIn("私聊里的悄悄话", dumped)
        self.assertNotIn("空间里的悄悄话", dumped)
        # 后台接口不返回任何消息内容
        self.assertTrue(all({"content", "metadata"}.isdisjoint(row) for row in page["items"]))
        self.assertNotIn("[礼物]", dumped)
        # 会话后来被删掉了（群解散）：记录上还记着它是群聊还是私聊，私聊的照样不给
        async with self.sessions() as db:
            await db.delete(await db.get(Conversation, self.direct_id))
            await db.delete(await db.get(Conversation, self.group_id))
            await db.commit()
            page = await gift_service.admin_records(db, kind=None, from_user_id=None, to_user_id=None, created_from=None, created_to=None, limit=50, offset=0)
        rows = {row["id"]: row for row in page["items"]}
        self.assertEqual({place: rows[self.where[place]]["message"] for place in self.where}, {"group": "群里的悄悄话", "direct": None, "profile": None})
        self.assertEqual(rows[self.where["group"]]["conversation_title"], None)


class WallTests(GiftTestCase):
    async def test_wall_groups_by_kind_and_is_public(self) -> None:
        self.ledger.donate(OID_A, 2000)
        self.ledger.donate(OID_D, 2000)
        await self._send(count=3)
        self.tick(hours=1)
        await self._send("dave", "bob", gift_key="flower", count=10)
        self.tick(hours=1)
        await self._send(count=2, conversation_id=None)
        await self._send_item("bubble.timed")
        self.tick(hours=1)
        await self._send_item("bubble.timed", "dave")
        await self._send(gift_key="castle", to="carol")
        async with self.sessions() as db:
            wall = await gift_service.wall(db, self.ids["bob"])
            empty = await gift_service.wall(db, self.ids["eve"])
            self.assertEqual(await self._code(gift_service.wall(db, 987654)), (404, "user_not_found"))
        self.assertEqual((wall["charm"], wall["total_count"]), (210, 17))
        # 按单价从高到低：限时气泡 50、蛋糕 20、小花 1
        self.assertEqual(
            [(entry["kind"], (entry["gift"] or entry["item"]).get("key") or entry["item"]["sku"], entry["count"], entry["last_at"]) for entry in wall["summary"]],
            [("item", "bubble.timed", 2, START + timedelta(hours=3)), ("gift", "cake", 5, START + timedelta(hours=2)), ("gift", "flower", 10, START + timedelta(hours=1))],
        )
        self.assertEqual(empty, {"charm": 0, "total_count": 0, "summary": []})

        async def records(**filters: Any) -> dict[str, Any]:
            async with self.sessions() as db:
                viewer = await db.get(User, self.ids["eve"])
                return await gift_service.wall_records(db, viewer, self.ids["bob"], gift_key=filters.get("gift_key"), item_sku=filters.get("item_sku"), limit=filters.get("limit", 20), offset=filters.get("offset", 0))

        everything = await records()
        self.assertEqual((everything["total"], [row["from_user"].username for row in everything["items"]]), (5, ["dave", "alice", "alice", "dave", "alice"]))
        cakes = await records(gift_key="cake")
        self.assertEqual((cakes["total"], [row["count"] for row in cakes["items"]]), (2, [2, 3]))
        items = await records(item_sku="bubble.timed", limit=1, offset=1)
        self.assertEqual((items["total"], [(row["kind"], row["from_user"].username) for row in items["items"]]), (2, [("item", "alice")]))
        self.assertEqual((await records(gift_key="castle"))["total"], 0)


class LeaderboardTests(GiftTestCase):
    async def _board(self, name: str, period: str, limit: int = 50) -> dict[str, Any]:
        return await self._as(name, gift_service.leaderboard, period, limit)

    async def _seed(self, to: str, amount: int, at: datetime) -> None:
        """直接写一条过去的记录（连同心意值），不走扣款。"""
        async with self.sessions() as db:
            db.add(GiftRecord(kind="gift", from_user_id=self.ids["dave"], to_user_id=self.ids[to], gift_key="cake", gift_name="蛋糕", gift_icon="cake", gift_price=amount, gift_tier=2, count=1, amount=amount, created_at=at))
            (await db.get(User, self.ids[to])).charm += amount
            await db.commit()

    async def test_all_time_and_monthly_boards(self) -> None:
        # 北京时间 9 月 30 日 23:59:59 和 10 月 1 日 00:00:00：本月榜按 UTC+8 的自然月切
        await self._seed("bob", 500, datetime(2026, 9, 30, 15, 59, 59, tzinfo=UTC))
        await self._seed("carol", 80, datetime(2026, 9, 30, 16, 0, 0, tzinfo=UTC))
        await self._seed("alice", 80, START - DAY)
        await self._seed("bob", 30, START - DAY)
        everything = await self._board("carol", "all")
        self.assertEqual([(row["rank"], row["user"].username, row["charm"]) for row in everything["items"]], [(1, "bob", 530), (2, "alice", 80), (3, "carol", 80)])
        self.assertEqual(everything["me"], {"rank": 3, "charm": 80})
        month = await self._board("bob", "month")
        self.assertEqual([(row["rank"], row["user"].username, row["charm"]) for row in month["items"]], [(1, "alice", 80), (2, "carol", 80), (3, "bob", 30)])
        self.assertEqual(month["me"], {"rank": 3, "charm": 30})
        # 没上榜的人
        self.assertEqual(((await self._board("eve", "all"))["me"], (await self._board("eve", "month"))["me"]), ({"rank": None, "charm": 0}, {"rank": None, "charm": 0}))
        # limit 只截榜单，不影响我的名次
        short = await self._board("carol", "month", 1)
        self.assertEqual(([row["user"].username for row in short["items"]], short["me"]), (["alice"], {"rank": 2, "charm": 80}))
        with patch.object(gift_service, "BOARD_MAX", 1):
            gift_service.reset_runtime_state()
            self.assertEqual((await self._board("carol", "all"))["me"], {"rank": 3, "charm": 80})
        # 月份翻过去：本月榜清零，总榜不变
        gift_service.reset_runtime_state()
        self.now = datetime(2026, 10, 31, 16, 0, 0, tzinfo=UTC)
        self.assertEqual(((await self._board("bob", "month"))["items"], len((await self._board("bob", "all"))["items"])), ([], 3))

    async def test_boards_are_cached_and_not_recomputed_per_request(self) -> None:
        await self._seed("bob", 30, START - DAY)
        statements: list[str] = []
        event.listen(self.engine.sync_engine, "before_cursor_execute", lambda _c, _cur, statement, *_rest: statements.append(statement))
        for period in ("all", "month"):
            await self._board("carol", period)
            statements.clear()
            for _ in range(5):
                await self._board("carol", period)
            # 缓存命中时不再聚合：只剩取当前用户、取上榜用户这两条按主键的查询
            self.assertFalse([statement for statement in statements if "gift_records" in statement or "ORDER BY" in statement], period)
        # 别的进程里送的礼最多晚一个缓存周期才上榜；本进程里送的立刻作废缓存
        await self._seed("carol", 99, START)
        self.assertEqual([row["user"].username for row in (await self._board("carol", "month"))["items"]], ["bob"])
        with patch.object(gift_service.time, "monotonic", lambda: 10**9):
            self.assertEqual([row["user"].username for row in (await self._board("carol", "month"))["items"]], ["carol", "bob"])
        self.ledger.donate(OID_A, 500)
        await self._send(to="dave", gift_key="castle")
        self.assertEqual([row["user"].username for row in (await self._board("carol", "all"))["items"]], ["dave", "carol", "bob"])


# ---------------------------------------------------------------- 生日


class BirthdayTests(GiftTestCase):
    async def _set(self, name: str = "alice", **overrides: Any) -> dict[str, Any]:
        return await self._as(name, birthday_service.set_birthday, {"year": 2008, "month": 10, "day": 12, "show_year": False, **overrides})

    async def _view(self, name: str = "alice") -> dict[str, Any]:
        return birthday_service.view(await self._user(name))

    async def test_set_and_public_fields(self) -> None:
        self.assertEqual(await self._view(), {"birthday": None, "show_year": False, "next_change_at": None, "celebrate_today": False})
        self.assertEqual(((await self._public("alice"))["birthday"], (await self._public("alice"))["is_birthday_today"], (await self._public("alice"))["charm"]), (None, False, 0))
        result = await self._set()
        # PUT 返回和 GET 一样的形状；本人看自己总是带年份
        self.assertEqual(result, {"birthday": {"year": 2008, "month": 10, "day": 12}, "show_year": False, "next_change_at": None, "celebrate_today": True})
        self.assertEqual(await self._view(), result)
        # 公开资料：年份要本人选择公开才给
        public = await self._public("alice")
        self.assertEqual((public["birthday"], public["is_birthday_today"]), ({"month": 10, "day": 12, "year": None}, True))
        await self._set(show_year=True)
        self.assertEqual((await self._public("alice"))["birthday"], {"month": 10, "day": 12, "year": 2008})
        # 后台的用户详情总是带完整生日
        await self._set(show_year=False)
        async with self.sessions() as db:
            detail = await report_service.get_admin_user(db, self.ids["alice"])
            nobody = await report_service.get_admin_user(db, self.ids["bob"])
        self.assertIsInstance(detail, AdminUserRead)
        self.assertEqual((detail.birthday.model_dump(), detail.is_birthday_today, detail.charm), ({"month": 10, "day": 12, "year": 2008}, True, 0))
        self.assertEqual((nobody.birthday, nobody.is_birthday_today), (None, False))

    async def test_today_follows_utc_plus_eight(self) -> None:
        await self._set(month=10, day=13)
        # 北京时间 10 月 12 日 23:59:59 → 还不是；10 月 13 日 00:00:00 → 是；10 月 14 日 00:00:00 → 不是了
        for moment, expected in ((datetime(2026, 10, 12, 15, 59, 59, tzinfo=UTC), False), (datetime(2026, 10, 12, 16, 0, 0, tzinfo=UTC), True), (datetime(2026, 10, 13, 15, 59, 59, tzinfo=UTC), True), (datetime(2026, 10, 13, 16, 0, 0, tzinfo=UTC), False)):
            self.now = moment
            self.assertEqual(((await self._public("alice"))["is_birthday_today"], (await self._view())["celebrate_today"]), (expected, expected), moment)

    async def test_february_29_is_celebrated_on_the_28th_in_common_years(self) -> None:
        await self._set(year=2008, month=2, day=29)
        self.assertEqual((await self._public("alice"))["birthday"], {"month": 2, "day": 29, "year": None})
        cases = (
            (datetime(2027, 2, 28, 4, 0, tzinfo=UTC), True),  # 平年：按 2 月 28 日过
            (datetime(2027, 3, 1, 4, 0, tzinfo=UTC), False),
            (datetime(2027, 2, 27, 4, 0, tzinfo=UTC), False),
            (datetime(2028, 2, 28, 4, 0, tzinfo=UTC), False),  # 闰年：28 日不算，29 日才是
            (datetime(2028, 2, 29, 4, 0, tzinfo=UTC), True),
            (datetime(2100, 2, 28, 4, 0, tzinfo=UTC), True),  # 2100 年不是闰年
        )
        for moment, expected in cases:
            self.now = moment
            self.assertEqual((await self._public("alice"))["is_birthday_today"], expected, moment)
        # 别人 2 月 28 日的生日不受这条规则影响
        self.now = START
        await self._set("bob", year=2007, month=2, day=28)
        self.now = datetime(2028, 2, 29, 4, 0, tzinfo=UTC)
        self.assertFalse((await self._public("bob"))["is_birthday_today"])

    async def test_invalid_dates(self) -> None:
        bad = (
            {"year": 2007, "month": 2, "day": 29},  # 2007 不是闰年
            {"year": 1900, "month": 2, "day": 29},
            {"year": 2008, "month": 4, "day": 31},
            {"year": 2008, "month": 13, "day": 1},
            {"year": 2008, "month": 0, "day": 1},
            {"year": 1899, "month": 12, "day": 31},
            {"year": 2026, "month": 10, "day": 13},  # 明天（北京时间）
            {"year": 2027, "month": 1, "day": 1},
            {"year": "2008"},
            {"year": 2008.0},
            {"day": None},
            {"month": True},
            {"show_year": "yes"},
        )
        for overrides in bad:
            self.assertEqual(await self._code(self._set(**overrides)), (422, "invalid_birthday"), overrides)
        self.assertEqual((await self._view())["birthday"], None)
        # 边界上合法的：1900 年 1 月 1 日、今天（北京时间已经是 10 月 12 日）
        await self._set("bob", year=1900, month=1, day=1)
        await self._set("carol", year=2026, month=10, day=12)
        self.now = datetime(2026, 10, 12, 16, 0, tzinfo=UTC)
        await self._set("dave", year=2026, month=10, day=13)

    async def test_the_date_can_change_any_number_of_times(self) -> None:
        # 连续改多次都成功，next_change_at 恒为 null
        for day in (1, 2, 3, 4, 5):
            changed = await self._set(day=day, show_year=day % 2 == 0)
            self.assertEqual((changed["birthday"], changed["show_year"], changed["next_change_at"]), ({"year": 2008, "month": 10, "day": day}, day % 2 == 0, None))
            self.assertEqual((await self._public("alice"))["birthday"]["day"], day)
        # 清空、再填、再清空、再填，随时都行
        for _ in range(3):
            cleared = await self._as("alice", birthday_service.clear_birthday)
            self.assertEqual((cleared["birthday"], cleared["next_change_at"], (await self._public("alice"))["birthday"]), (None, None, None))
            refilled = await self._set(year=2007, month=3, day=9)
            self.assertEqual((refilled["birthday"], refilled["next_change_at"]), ({"year": 2007, "month": 3, "day": 9}, None))
        self.assertEqual((await self._view())["next_change_at"], None)
        # 两列已经停用：不再写
        user = await self._user("alice")
        self.assertEqual((user.birthday_set_at, user.birthday_changed_at), (None, None))

    async def test_changing_the_date_does_not_celebrate_twice_a_year(self) -> None:
        # 今天（北京时间 10 月 12 日）不是生日；改成今天就是，今年还没播过
        await self._set(day=1)
        self.assertFalse((await self._view())["celebrate_today"])
        self.assertTrue((await self._set(day=12))["celebrate_today"])
        self.assertFalse((await self._as("alice", birthday_service.mark_celebrated))["celebrate_today"])
        # 播过之后反复改走再改回今天、清空再填回今天，今年都不再播
        for day in (11, 12, 1, 12):
            self.assertFalse((await self._set(day=day))["celebrate_today"], day)
        await self._as("alice", birthday_service.clear_birthday)
        self.assertFalse((await self._set(day=12))["celebrate_today"])
        # 公开资料里「今天是生日」照常跟着日期走
        self.assertTrue((await self._public("alice"))["is_birthday_today"])
        # 明年同一天又可以播
        self.now = datetime(2027, 10, 12, 1, 0, tzinfo=UTC)
        self.assertTrue((await self._view())["celebrate_today"])
        await self._as("alice", birthday_service.mark_celebrated)
        # 今年按旧日期播过了，再把生日改成今天也不重播
        self.now = datetime(2027, 10, 20, 1, 0, tzinfo=UTC)
        self.assertFalse((await self._set(month=10, day=20))["celebrate_today"])
        # 另一个人：今年没播过，生日改成今天就会播（只一次）
        await self._set("bob", day=1)
        self.assertTrue((await self._set("bob", month=10, day=20))["celebrate_today"])
        await self._as("bob", birthday_service.mark_celebrated)
        self.assertFalse((await self._set("bob", month=10, day=20))["celebrate_today"])

    async def test_celebration_is_marked_once_a_year(self) -> None:
        await self._set()
        self.assertTrue((await self._view())["celebrate_today"])
        self.assertFalse((await self._as("alice", birthday_service.mark_celebrated))["celebrate_today"])
        self.assertEqual(((await self._view())["celebrate_today"], (await self._public("alice"))["is_birthday_today"]), (False, True))
        # 明年生日又是没播过的
        self.now = datetime(2027, 10, 12, 1, 0, tzinfo=UTC)
        self.assertTrue((await self._view())["celebrate_today"])
        # 不是生日那天标记，什么都不改
        self.now = datetime(2027, 10, 20, 1, 0, tzinfo=UTC)
        self.assertFalse((await self._as("alice", birthday_service.mark_celebrated))["celebrate_today"])
        self.now = datetime(2028, 10, 12, 1, 0, tzinfo=UTC)
        self.assertTrue((await self._view())["celebrate_today"])

    async def test_gifts_sent_on_the_birthday_are_marked(self) -> None:
        self.ledger.donate(OID_A, 300)
        await self._set("bob", month=10, day=12)
        today = await self._send(message="生日快乐")
        self.assertEqual((today["record"]["on_birthday"], today["record"]["to_user"].is_birthday_today), (True, True))
        # 北京时间过了零点就不是了；记录上的标记是送的那一刻定的，不会变
        self.now = datetime(2026, 10, 12, 16, 0, 1, tzinfo=UTC)
        tomorrow = await self._send(conversation_id=None)
        self.assertEqual((tomorrow["record"]["on_birthday"], [record.on_birthday for record in await self._records()]), (False, [True, False]))
        # 送礼人过生日不算
        await self._set("alice", month=10, day=13)
        self.assertFalse((await self._send(conversation_id=None))["record"]["on_birthday"])

    async def test_public_fields_cost_no_queries(self) -> None:
        self.ledger.donate(OID_A, 300)
        await self._set("bob", show_year=True)
        await self._send()
        # 送礼之后推余额的后台任务（shop.balance.changed）还会查一次库，等它跑完再开始数，不然这条查询会被算进来
        await shop_service.drain()
        async with self.sessions() as db:
            users = list((await db.execute(select(User).order_by(User.id))).scalars().all())
            statements: list[str] = []
            event.listen(self.engine.sync_engine, "before_cursor_execute", lambda _c, _cur, statement, *_rest: statements.append(statement))
            dumped = {user.username: UserPublic.model_validate(user).model_dump() for user in users}
        # 心意值、生日、今天是不是生日都在用户这一行上：序列化多少个用户都不发一条查询
        self.assertEqual(statements, [])
        self.assertEqual((dumped["bob"]["charm"], dumped["bob"]["birthday"], dumped["bob"]["is_birthday_today"]), (20, {"month": 10, "day": 12, "year": 2008}, True))
        self.assertEqual((dumped["alice"]["charm"], dumped["alice"]["birthday"], dumped["alice"]["is_birthday_today"]), (0, None, False))
        # 新字段都有默认值：旧快照里没有它们也能读回来
        legacy = UserPublic(id=1, username="old", created_at=START)
        self.assertEqual((legacy.charm, legacy.birthday, legacy.is_birthday_today), (0, None, False))


# ---------------------------------------------------------------- 后台


class AdminTests(GiftTestCase):
    async def test_settings(self) -> None:
        self.assertEqual(await self._as("admin", gift_service.admin_update_settings, {}), {"enabled": True, "daily_send_limit": 200, "message_max": 60})
        updated = await self._as("admin", gift_service.admin_update_settings, {"enabled": False, "daily_send_limit": 10000, "message_max": 1})
        self.assertEqual(updated, {"enabled": False, "daily_send_limit": 10000, "message_max": 1})
        # 没变化的提交不写审计
        await self._as("admin", gift_service.admin_update_settings, {"enabled": False, "daily_send_limit": 10000})
        self.assertEqual(await self._audit(), [("gift.settings.update", {"enabled": {"old": True, "new": False}, "daily_send_limit": {"old": 200, "new": 10000}, "message_max": {"old": 60, "new": 1}})])
        for changes, field in (({"daily_send_limit": 0}, "daily_send_limit"), ({"daily_send_limit": 10001}, "daily_send_limit"), ({"message_max": 201}, "message_max"), ({"message_max": 0}, "message_max"), ({"message_max": "60"}, "message_max"), ({"enabled": 1}, "enabled")):
            status_code, code, detail = await self._fails(self._as("admin", gift_service.admin_update_settings, changes))
            self.assertEqual((status_code, code, detail["field"]), (422, "invalid_settings", field), changes)
        async with self.sessions() as db:
            self.assertEqual(await gift_service.admin_settings(db), updated)

    async def test_settings_reach_the_other_worker_within_the_cache_window(self) -> None:
        async with self.sessions() as db:
            self.assertTrue((await gift_service.get_settings(db))["enabled"])
            # 另一个进程直接改了库：这个进程 5 秒内看到的还是缓存
            (await db.get(SystemSetting, "gifts.enabled")).value = "0"
            await db.commit()
            self.assertTrue((await gift_service.get_settings(db))["enabled"])
            with patch.object(gift_service.time, "monotonic", lambda: 10**9):
                self.assertFalse((await gift_service.get_settings(db))["enabled"])

    async def test_catalog_editing(self) -> None:
        created = await self._as("admin", gift_service.admin_create_gift, {"key": "star.v2", "name": " 星星 ", "price": 2, "icon": "star", "tier": 1})
        self.assertEqual(created, {"key": "star.v2", "name": "星星", "price": 2, "icon": "star", "tier": 1, "is_active": True, "sort": 0})
        status_code, code, detail = await self._fails(self._as("admin", gift_service.admin_create_gift, {"key": "star.v2", "name": "星星", "price": 2, "icon": "star", "tier": 1}))
        self.assertEqual((status_code, code, detail["field"]), (409, "key_exists", "key"))
        base = {"key": "new-one", "name": "新的", "price": 5, "icon": "heart", "tier": 2}
        invalid = (
            ({"key": "Star"}, "key"),
            ({"key": "a" * 33}, "key"),
            ({"key": "有中文"}, "key"),
            ({"key": None}, "key"),
            ({"name": ""}, "name"),
            ({"name": "字" * 21}, "name"),
            ({"price": 0}, "price"),
            ({"price": shop_service.PRICE_MAX + 1}, "price"),
            ({"price": "5"}, "price"),
            ({"icon": ""}, "icon"),
            ({"icon": "http://x/y.png"}, "icon"),
            ({"tier": 4}, "tier"),
            ({"tier": 0}, "tier"),
            ({"sort": 10**7}, "sort"),
            ({"is_active": "no"}, "is_active"),
        )
        for overrides, field in invalid:
            status_code, code, detail = await self._fails(self._as("admin", gift_service.admin_create_gift, {**base, **overrides}))
            self.assertEqual((status_code, code, detail["field"]), (422, "invalid_gift", field), overrides)
        # 取值范围的上限是合法的
        await self._as("admin", gift_service.admin_create_gift, {**base, "key": "a" * 32, "name": "字" * 20, "price": shop_service.PRICE_MAX, "tier": 3})
        updated = await self._as("admin", gift_service.admin_update_gift, "star.v2", {"price": 3, "is_active": False, "sort": 5})
        self.assertEqual((updated["price"], updated["is_active"], updated["sort"], updated["name"]), (3, False, 5, "星星"))
        self.assertEqual(await self._code(self._as("admin", gift_service.admin_update_gift, "nope", {"price": 3})), (404, "gift_not_found"))
        status_code, code, detail = await self._fails(self._as("admin", gift_service.admin_update_gift, "star.v2", {"key": "other"}))
        self.assertEqual((status_code, code, detail["field"]), (422, "key_immutable", "key"))
        self.assertEqual(await self._code(self._as("admin", gift_service.admin_update_gift, "star.v2", {"price": 0})), (422, "invalid_gift"))
        # 后台的目录含已下架的；用户侧的不含，下架的也送不出去
        async with self.sessions() as db:
            listing = await gift_service.admin_catalog(db)
        self.assertEqual((len(listing["gifts"]), [gift["is_active"] for gift in listing["gifts"] if gift["key"] == "star.v2"]), (8, [False]))
        self.assertNotIn("star.v2", [gift["key"] for gift in (await self._as("alice", gift_service.catalog_view))["gifts"]])
        self.ledger.donate(OID_A, 100)
        self.assertEqual(await self._code(self._send(gift_key="star.v2")), (422, "invalid_gift"))
        actions = [action for action, _details in await self._audit()]
        self.assertEqual(actions, ["gift.catalog.create", "gift.catalog.create", "gift.catalog.update"])
        self.assertEqual((await self._audit())[-1][1], {"key": "star.v2", "changed": {"price": {"old": 2, "new": 3}, "is_active": {"old": True, "new": False}, "sort": {"old": 0, "new": 5}}})

    async def test_records_filters_and_sitewide_summary(self) -> None:
        self.ledger.donate(OID_A, 1000)
        self.ledger.donate(OID_D, 1000)
        await self._send(count=3)
        self.tick(hours=1)
        await self._send_item("bubble.perm", "dave", "carol")
        self.tick(hours=1)
        await self._send("dave", "bob", conversation_id=None, gift_key="flower", count=5)

        async def listing(**filters: Any) -> dict[str, Any]:
            options = {"kind": None, "from_user_id": None, "to_user_id": None, "created_from": None, "created_to": None, "limit": 50, "offset": 0, **filters}
            async with self.sessions() as db:
                return await gift_service.admin_records(db, **options)

        everything = await listing()
        self.assertEqual((everything["total"], [row["amount"] for row in everything["items"]]), (3, [5, 60, 60]))
        # summary 是全站累计（次数 = 记录条数），不随筛选变
        self.assertEqual(everything["summary"], {"total_amount": 125, "total_count": 3})
        for filters, amounts in (
            ({"kind": "item"}, [60]),
            ({"kind": "gift"}, [5, 60]),
            ({"from_user_id": self.ids["dave"]}, [5, 60]),
            ({"to_user_id": self.ids["bob"]}, [5, 60]),
            ({"from_user_id": self.ids["dave"], "to_user_id": self.ids["bob"]}, [5]),
            ({"created_from": START + timedelta(hours=1)}, [5, 60]),
            ({"created_to": START + timedelta(hours=1)}, [60]),
            ({"created_from": START + timedelta(hours=1), "created_to": START + timedelta(hours=2)}, [60]),
            ({"limit": 1, "offset": 1}, [60]),
        ):
            page = await listing(**filters)
            self.assertEqual(([row["amount"] for row in page["items"]], page["summary"]), (amounts, {"total_amount": 125, "total_count": 3}), filters)
        row = everything["items"][1]
        self.assertEqual((row["kind"], row["item"]["sku"], row["from_user"].username, row["to_user"].username, row["conversation_type"], row["conversation_title"], row["on_birthday"]), ("item", "bubble.perm", "dave", "carol", "group", "群", False))


# ---------------------------------------------------------------- 走真实的路由


class HttpTests(GiftTestCase):
    """路径都挂上了、返回的整体形状、错误体的形状、后台接口的权限。"""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        from app.api.v1.router import api_router

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
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://testserver")
        self.moderation = Mock()
        patcher = patch.object(moderation_service, "schedule_message_moderation", self.moderation)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        await super().asyncTearDown()

    async def _json(self, method: str, path: str, expected: int = 200, **kwargs: Any) -> Any:
        response = await self.client.request(method, f"/api/v1{path}", **kwargs)
        self.assertEqual(response.status_code, expected, response.text)
        return response.json()

    async def test_gift_flow(self) -> None:
        self.ledger.donate(OID_A, 300)
        catalog = await self._json("GET", "/gifts/catalog")
        self.assertEqual(set(catalog), {"enabled", "charm_label", "gifts", "limits", "sent_today"})
        self.assertEqual((catalog["enabled"], len(catalog["gifts"]), set(catalog["gifts"][0])), (True, 6, {"key", "name", "price", "icon", "tier", "is_active", "sort"}))
        body = {"to_user_id": self.ids["bob"], "conversation_id": self.group_id, "kind": "gift", "gift_key": "cake", "count": 3, "message": "生日快乐！"}
        sent = await self._json("POST", "/gifts", json=body)
        self.assertEqual(set(sent), {"record", "message", "balance"})
        record_keys = {"id", "kind", "gift", "item", "count", "amount", "from_user", "to_user", "conversation_id", "message_id", "message", "on_birthday", "created_at"}
        self.assertEqual(set(sent["record"]), record_keys)
        self.assertEqual((sent["record"]["amount"], sent["record"]["to_user"]["charm"], sent["record"]["message"], sent["balance"]["balance"], sent["balance"]["gift_spent"]), (60, 60, "生日快乐！", 240, 60))
        self.assertEqual((sent["message"]["type"], sent["message"]["content"], sent["message"]["metadata"]["gift"]["id"]), ("gift", "[礼物] 送给 阿夸 蛋糕 ×3", sent["record"]["id"]))
        self.assertEqual(set(sent["message"]["metadata"]["gift"]), {"id", "kind", "gift", "item", "count", "to_user", "message"})
        # 消息照常广播给会话成员，带留言的走一次异步审核
        created = [(users, data) for users, data in await self._events("message.created")]
        self.assertEqual((len(created), created[0][1].type), (1, MessageType.GIFT))
        self.moderation.assert_called_once_with(sent["message"]["id"])
        # 带结尾斜杠也接；不落消息的 message 是 null，也不送审
        profile = await self._json("POST", "/gifts/", json={**body, "conversation_id": None, "kind": "item", "item_sku": "bubble.timed", "gift_key": None, "count": 1, "message": None})
        self.assertEqual((profile["message"], profile["record"]["kind"], profile["record"]["item"]["duration_days"], profile["balance"]["balance"]), (None, "item", 30, 190))
        self.assertEqual(self.moderation.call_count, 1)
        error = await self._json("POST", "/gifts", 402, json={**body, "gift_key": "castle"})
        self.assertEqual((error["detail"]["code"], error["detail"]["balance"], error["detail"]["shortfall"]), ("insufficient_balance", 190, 710))
        self.assertEqual((await self._json("POST", "/gifts", 422, json={**body, "to_user_id": self.ids["alice"]}))["detail"]["code"], "invalid_target")
        self.assertEqual((await self._json("POST", "/gifts", 422, json={}))["detail"]["code"], "invalid_target")
        # 普通的发消息接口发不了 gift
        await self._json("POST", f"/conversations/{self.group_id}/messages", 422, json={"type": "gift", "content": "[礼物]"})

        mine = await self._json("GET", "/gifts/mine?direction=sent&limit=1")
        self.assertEqual((set(mine), mine["total"], mine["sum"], len(mine["items"])), ({"items", "total", "sum"}, 2, 110, 1))
        await self._json("GET", "/gifts/mine?direction=sideways", 422)
        board = await self._json("GET", "/gifts/leaderboard?period=month&limit=10")
        self.assertEqual(([(row["rank"], row["user"]["username"], row["charm"]) for row in board["items"]], board["me"]), ([(1, "bob", 110)], {"rank": None, "charm": 0}))
        await self._json("GET", "/gifts/leaderboard?period=week", 422)
        await self._json("GET", "/gifts/leaderboard?limit=101", 422)
        wall = await self._json("GET", f"/users/{self.ids['bob']}/gifts")
        self.assertEqual((set(wall), wall["charm"], wall["total_count"], set(wall["summary"][0])), ({"charm", "total_count", "summary"}, 110, 4, {"kind", "gift", "item", "count", "last_at"}))
        self.assertEqual((await self._json("GET", "/users/987654/gifts", 404))["detail"]["code"], "user_not_found")
        # 第三方看礼物墙的记录：留言是 null；当事人看得到
        self.acting = "carol"
        records = await self._json("GET", f"/users/{self.ids['bob']}/gifts/records?gift_key=cake")
        self.assertEqual((records["total"], records["items"][0]["message"], set(records["items"][0])), (1, None, record_keys))
        self.assertEqual((await self._json("GET", f"/users/{self.ids['bob']}/profile-does-not-exist", 404))["detail"], "Not Found")
        self.assertEqual((await self._json("GET", f"/users/{self.ids['bob']}"))["charm"], 110)
        self.acting = "bob"
        records = await self._json("GET", f"/users/{self.ids['bob']}/gifts/records?item_sku=bubble.timed")
        self.assertEqual((records["total"], records["items"][0]["from_user"]["username"]), (1, "alice"))
        purchases = await self._json("GET", "/shop/purchases")
        self.assertEqual([(row["source"], row["price"], row["from_user"]["username"], row["duration_days"], row["expires_at"] is not None) for row in purchases["items"]], [("gift", 0, "alice", 30, True)])
        shop = await self._json("GET", "/shop")
        timed = next(item for item in shop["items"] if item["sku"] == "bubble.timed")
        self.assertEqual((timed["owned"], timed["obtainable"], timed["duration_days"], timed["expires_at"] is not None, shop["balance"]["gift_spent"]), (True, "renew", 30, True, 0))
        # 开关关着：只读接口照常返回数据，只有送礼是 403
        await self._configure(enabled=False)
        self.assertEqual(((await self._json("GET", "/gifts/catalog"))["enabled"], (await self._json("GET", f"/users/{self.ids['bob']}/gifts"))["charm"]), (False, 110))
        self.assertEqual((len((await self._json("GET", "/gifts/leaderboard"))["items"]), (await self._json("GET", "/gifts/mine"))["total"]), (1, 2))
        self.acting = "alice"
        self.assertEqual((await self._json("POST", "/gifts", 403, json=body))["detail"]["code"], "disabled")

    async def test_birthday_flow(self) -> None:
        empty = await self._json("GET", "/users/me/birthday")
        self.assertEqual(empty, {"birthday": None, "show_year": False, "next_change_at": None, "celebrate_today": False})
        saved = await self._json("PUT", "/users/me/birthday", json={"year": 2008, "month": 10, "day": 12, "show_year": False})
        self.assertEqual(saved, {"birthday": {"year": 2008, "month": 10, "day": 12}, "show_year": False, "next_change_at": None, "celebrate_today": True})
        self.assertEqual(await self._json("GET", "/users/me/birthday"), saved)
        profile = await self._json("GET", f"/users/{self.ids['alice']}")
        self.assertEqual((profile["birthday"], profile["is_birthday_today"]), ({"month": 10, "day": 12, "year": None}, True))
        self.assertEqual((await self._json("POST", "/users/me/birthday/celebrated"))["celebrate_today"], False)
        # 连续改都成功，不再 409；next_change_at 恒为 null
        for day in (10, 9, 11):
            changed = await self._json("PUT", "/users/me/birthday", json={"year": 2008, "month": 10, "day": day, "show_year": True})
            self.assertEqual((changed["birthday"]["day"], changed["next_change_at"], changed["celebrate_today"]), (day, None, False))
        self.assertEqual((await self._json("PUT", "/users/me/birthday", 422, json={"year": 2008, "month": 2, "day": 30}))["detail"]["code"], "invalid_birthday")
        self.assertEqual((await self._json("PUT", "/users/me/birthday", 422, json={}))["detail"]["code"], "invalid_birthday")
        # 在线名单、搜索这些列表里的用户对象都带着
        found = await self._json("GET", "/users/search?q=alice")
        self.assertEqual((found[0]["birthday"], found[0]["is_birthday_today"], found[0]["charm"]), ({"month": 10, "day": 11, "year": 2008}, False, 0))
        cleared = await self._json("DELETE", "/users/me/birthday")
        self.assertEqual((cleared["birthday"], cleared["next_change_at"]), (None, None))
        self.assertEqual((await self._json("GET", f"/users/{self.ids['alice']}"))["birthday"], None)
        # 清空之后立刻再填也行；今年已经播过，改回今天不再播
        refilled = await self._json("PUT", "/users/me/birthday", json={"year": 2008, "month": 10, "day": 12})
        self.assertEqual((refilled["birthday"]["day"], refilled["next_change_at"], refilled["celebrate_today"]), (12, None, False))

    async def test_admin_flow(self) -> None:
        self.ledger.donate(OID_A, 300)
        await self._send(conversation_id=self.direct_id, message="私聊留言")
        for method, path in (("GET", "/admin/gifts/settings"), ("PUT", "/admin/gifts/settings"), ("GET", "/admin/gifts/catalog"), ("POST", "/admin/gifts/catalog"), ("PUT", "/admin/gifts/catalog/cake"), ("GET", "/admin/gifts/records")):
            response = await self.client.request(method, f"/api/v1{path}", json={})
            self.assertEqual(response.status_code, 403, path)
        self.acting = "admin"
        self.assertEqual(await self._json("GET", "/admin/gifts/settings"), {"enabled": True, "daily_send_limit": 200, "message_max": 60})
        self.assertEqual(await self._json("PUT", "/admin/gifts/settings", json={"enabled": True, "daily_send_limit": 50, "message_max": 80}), {"enabled": True, "daily_send_limit": 50, "message_max": 80})
        error = await self._json("PUT", "/admin/gifts/settings", 422, json={"message_max": 500})
        self.assertEqual((error["detail"]["code"], error["detail"]["field"]), ("invalid_settings", "message_max"))
        catalog = await self._json("GET", "/admin/gifts/catalog")
        self.assertEqual((set(catalog), len(catalog["gifts"])), ({"gifts"}, 6))
        created = await self._json("POST", "/admin/gifts/catalog", json={"key": "heart", "name": "爱心", "price": 3, "icon": "heart", "tier": 1, "is_active": True, "sort": 70})
        self.assertEqual(created, {"key": "heart", "name": "爱心", "price": 3, "icon": "heart", "tier": 1, "is_active": True, "sort": 70})
        error = await self._json("POST", "/admin/gifts/catalog", 409, json={"key": "heart", "name": "爱心", "price": 3, "icon": "heart", "tier": 1})
        self.assertEqual((error["detail"]["code"], error["detail"]["field"]), ("key_exists", "key"))
        error = await self._json("POST", "/admin/gifts/catalog", 422, json={"key": "x", "name": "爱心", "price": 0, "icon": "heart", "tier": 1})
        self.assertEqual((error["detail"]["code"], error["detail"]["field"]), ("invalid_gift", "price"))
        self.assertEqual((await self._json("PUT", "/admin/gifts/catalog/heart", json={"is_active": False}))["is_active"], False)
        self.assertEqual((await self._json("PUT", "/admin/gifts/catalog/nope", 404, json={"price": 1}))["detail"]["code"], "gift_not_found")
        records = await self._json("GET", f"/admin/gifts/records?kind=gift&from_user_id={self.ids['alice']}&created_from=2026-10-12T00:00:00%2B08:00&created_to=2026-10-13T00:00:00%2B08:00")
        self.assertEqual((set(records), records["total"], records["summary"]), ({"items", "total", "summary"}, 1, {"total_amount": 20, "total_count": 1}))
        row = records["items"][0]
        self.assertEqual((row["message"], row["conversation_type"], row["conversation_title"], row["from_user"]["username"], row["to_user"]["charm"]), (None, "direct", None, "alice", 20))
        self.assertNotIn("私聊留言", json.dumps(records, ensure_ascii=False))
        await self._json("GET", "/admin/gifts/records?kind=coins", 422)
        # 商店后台：商品的有效期、账户里的送礼支出、对账里的心意值守恒、带天数的赠送、用户详情里的完整生日
        item = await self._json("POST", "/admin/shop/items", json={"sku": "bubble.trial", "category": "bubble", "name": "体验", "price": 5, "style": STYLE, "duration_days": 7})
        self.assertEqual(item["duration_days"], 7)
        self.assertEqual((await self._json("PUT", f"/admin/shop/items/{item['id']}", json={"duration_days": None}))["duration_days"], None)
        error = await self._json("PUT", f"/admin/shop/items/{item['id']}", 422, json={"duration_days": 0})
        self.assertEqual((error["detail"]["code"], error["detail"]["field"]), ("invalid_item", "duration_days"))
        granted = await self._json("POST", f"/admin/shop/users/{self.ids['carol']}/grant", json={"item_id": item["id"], "duration_days": 3})
        self.assertEqual((granted["duration_days"], granted["expires_at"] is not None, granted["source"]), (3, True, "grant"))
        self.assertEqual((await self._json("POST", f"/admin/shop/users/{self.ids['carol']}/grant", json={"item_id": item["id"], "duration_days": 0}))["expires_at"], None)
        accounts = await self._json("GET", "/admin/shop/accounts?q=alice")
        self.assertEqual([(row["gift_spent"], row["balance"]) for row in accounts["items"]], [(20, 280)])
        self.assertEqual((await self._json("POST", "/admin/shop/reconcile"))["gifts"]["balanced"], True)
        self.assertEqual((await self._json("GET", "/admin/shop/settings"))["duration_max"], 3650)
        async with self.sessions() as db:
            await birthday_service.set_birthday(db, await db.get(User, self.ids["bob"]), {"year": 2009, "month": 10, "day": 12, "show_year": False})
        detail = await self._json("GET", f"/admin/users/{self.ids['bob']}")
        self.assertEqual((detail["birthday"], detail["is_birthday_today"], detail["charm"]), ({"month": 10, "day": 12, "year": 2009}, True, 20))
        # 公开资料上的字段后台也要有：CCW 绑定、账号等级、装备着的装扮（以前逐字段手写，漏了这些，后台把已绑定的人显示成「未绑定」）
        async with self.sessions() as db:
            (await db.get(User, self.ids["bob"])).account_level = 7
            await db.commit()
        detail = await self._json("GET", f"/admin/users/{self.ids['bob']}")
        self.assertEqual((detail["ccw_student_oid"], detail["account_level"], detail["cosmetics"]), (OID_B, 7, (await self._public("bob"))["cosmetics"]))
        # 叠上去的后台字段没被公开资料盖掉：生日仍带年份，后台专有的字段还在
        self.assertEqual(detail["birthday"]["year"], 2009)
        self.assertIn("email", detail)


# ---------------------------------------------------------------- 老库升级


class InitDbTests(unittest.IsolatedAsyncioTestCase):
    async def test_init_db_upgrades_an_existing_database(self) -> None:
        """老库：没有礼物的表，users / shop_items / shop_purchases 上没有新列，shop_purchases 的唯一索引还是旧条件。init_db 补上，重复执行无害。"""
        asyncio.get_running_loop().set_debug(False)
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        new_columns = {
            "users": ("charm", "birthday_year", "birthday_month", "birthday_day", "birthday_show_year", "birthday_set_at", "birthday_changed_at", "birthday_celebrated_year"),
            "shop_items": ("duration_days",),
            "shop_purchases": ("expires_at", "duration_days", "superseded_at", "from_user_id", "gift_record_id", "expiring_notified_at", "expired_notified_at"),
        }
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
                for statement in (
                    "DROP TABLE gifts",
                    "DROP TABLE gift_records",
                    "DROP INDEX ix_users_charm",
                    "DROP INDEX ix_shop_purchases_expires",
                    "DROP INDEX uq_shop_purchases_live",
                    "CREATE UNIQUE INDEX uq_shop_purchases_owner ON shop_purchases (user_id, item_id) WHERE revoked_at IS NULL",
                ):
                    await connection.execute(text(statement))
                for table, columns in new_columns.items():
                    for column in columns:
                        await connection.execute(text(f"ALTER TABLE {table} DROP COLUMN {column}"))
                await connection.execute(text("INSERT INTO users (username, hashed_password, created_at, updated_at) VALUES ('old', 'x', '2026-01-01 00:00:00', '2026-01-01 00:00:00')"))
                await connection.execute(text("INSERT INTO shop_items (sku, category, name, style, created_at, updated_at) VALUES ('bubble.old', 'bubble', '旧', '{}', '2026-01-01 00:00:00', '2026-01-01 00:00:00')"))
                await connection.execute(text("INSERT INTO shop_purchases (user_id, item_id, price, source, created_at) VALUES (1, 1, 10, 'purchase', '2026-01-01 00:00:00')"))

            with patch.object(database_module, "engine", engine):
                await database_module.init_db()
                await database_module.init_db()

            async with sessions() as db:
                for table, columns in new_columns.items():
                    names = {row[1] for row in (await db.execute(text(f"PRAGMA table_info({table})"))).all()}
                    self.assertTrue(set(columns) <= names, table)
                tables = {row[0] for row in (await db.execute(text("SELECT name FROM sqlite_master WHERE type = 'table'"))).all()}
                self.assertTrue({"gifts", "gift_records"} <= tables)
                indexes = {row[0]: row[1] for row in (await db.execute(text("SELECT name, sql FROM sqlite_master WHERE type = 'index'"))).all()}
                self.assertTrue({"ix_users_charm", "ix_shop_purchases_expires", "uq_shop_purchases_live", "ix_gift_records_to_created"} <= set(indexes))
                self.assertNotIn("uq_shop_purchases_owner", indexes)
                self.assertIn("superseded_at IS NULL", indexes["uq_shop_purchases_live"])
                # 老数据原样：老用户心意值 0、没有生日；老的拥有记录是永久的，仍然算拥有
                old = (await db.execute(select(User).where(User.username == "old"))).scalar_one()
                self.assertEqual((old.charm, old.birthday, old.is_birthday_today, old.birthday_show_year), (0, None, False, False))
                self.assertTrue(await shop_service.owns_permanently(db, old.id, 1))
                self.assertEqual((await db.get(ShopItem, 1)).duration_days, None)
                # 补出来的库能直接用：续期要靠新的唯一索引
                item = ShopItem(sku="bubble.timed", category="bubble", name="限时", price=0, style=json.dumps(STYLE), duration_days=30)
                db.add(item)
                await db.commit()
                now = datetime.now(UTC)
                for _ in range(2):
                    await shop_service._grant_ownership(db, user_id=old.id, item_id=item.id, sku=item.sku, days=30, now=now, source="grant")
                    await db.commit()
                live = (await db.execute(select(ShopPurchase).where(ShopPurchase.item_id == item.id, ShopPurchase.superseded_at.is_(None)))).scalars().all()
                self.assertEqual([shop_service._aware(row.expires_at) for row in live], [now + 60 * DAY])
                self.assertEqual(await gift_service.ensure_default_gifts(db), 6)
        finally:
            await engine.dispose()


# ---------------------------------------------------------------- 子进程入口（MultiProcessTests 用）


async def _worker(job: dict[str, Any]) -> Any:
    engine = make_engine(Path(job["db"]))
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.fromisoformat(job["now"])
    ledger = FakeCoinLedger()
    for oid, amount in job.get("donations", {}).items():
        ledger.donate(oid, amount)
    integrations.set_coin_ledger(ledger)
    for module in (shop_service, rp, gift_service, localtime):
        module._now = lambda: now
    shop_service.AsyncSessionLocal = sessions
    rp.AsyncSessionLocal = sessions
    settings.shop_cosmetics_event_interval_seconds = 0
    sent: list[tuple[str, Any]] = []

    async def record_event(*args: Any) -> None:
        sent.append((args[-2], args[0]))

    manager.send_to_user = record_event
    manager.send_to_users = record_event
    manager.broadcast = record_event
    ids, items, group = job["ids"], job["items"], job["group"]

    async def attempt(name: str, function, *args: Any) -> str:
        async with sessions() as db:
            try:
                await function(db, await db.get(User, ids[name]), *args)
                return "ok"
            except HTTPException as exc:
                return exc.detail["code"]

    def gift(to: str, conversation_id: int | None, **extra: Any) -> dict[str, Any]:
        return {"to_user_id": ids[to], "conversation_id": conversation_id, "kind": "gift", "gift_key": "cake", "count": 1, "message": None, **extra}

    # 先把连接建起来，再报告准备好了，等主进程放行：几个进程尽量同时开始
    async with sessions() as db:
        await db.scalar(select(func.count()).select_from(GiftRecord))
    print("ready", flush=True)
    await asyncio.to_thread(sys.stdin.readline)
    try:
        if job["mode"] == "spend":
            tasks = [attempt("alice", gift_service.send_gift, gift("bob", group, count=3)), attempt("alice", gift_service.send_gift, gift("bob", None, count=3))]
            if job["extras"]:
                tasks.append(attempt("alice", shop_service.purchase, items["perm"]))
                tasks.append(attempt("alice", rp.send_red_packet, {"conversation_id": group, "kind": "normal", "amount": 60, "count": 1, "greeting": None}))
            return await asyncio.gather(*tasks)
        if job["mode"] == "charm":
            return await asyncio.gather(*(attempt("bob", gift_service.send_gift, gift("carol", None)) for _ in range(job["sends"])))
        if job["mode"] == "renew":
            buys = [attempt("bob", shop_service.purchase, items["timed"]) for _ in range(2)]
            grants = [attempt("admin", shop_service.admin_grant, ids["bob"], items["timed"], None, 10) for _ in range(2)]
            return await asyncio.gather(*buys, *grants)
        async with sessions() as db:
            done = await shop_service.sweep_expiry(db, now=now)
        await shop_service.drain()
        return {**done, "events": sum(1 for name, _to in sent if name.startswith("shop.item.")), "cosmetics": sum(1 for name, _to in sent if name == "user.cosmetics.updated")}
    finally:
        await shop_service.drain()
        await engine.dispose()


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "worker":
        print(json.dumps(asyncio.run(_worker(json.loads(sys.argv[2])))), flush=True)
    else:
        unittest.main()
