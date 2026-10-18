"""商店（docs/shop.md）的测试。余额来源用假账本（integration_fakes.FakeCoinLedger），不发任何外部请求。"""

from __future__ import annotations

import asyncio
import json
import re
import tempfile
import time
import unittest
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import Depends, FastAPI, HTTPException
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.models  # noqa: F401
from app import integrations
from app.core.config import settings
from app.core.database import Base, configure_sqlite_connection, get_db
from app.core.dependencies import get_current_user
from app.models.enums import PlatformRole
from app.models.report import AdminAuditLog
from app.models.shop import ShopAccount, ShopItem, ShopPurchase
from app.models.user import User
from app.schemas.user import UserPublic, parse_cosmetics
from app.services import shop as shop_service
from app.services.shop_style import (
    ANIMATIONS,
    FRAME_ANIMATIONS,
    FRAME_ORNAMENTS,
    FRAME_POSITIONS,
    ORNAMENTS,
    PATTERNS,
    StyleError,
    validate_bubble_style,
    validate_frame_style,
)
from app.websocket.manager import manager
from integration_fakes import TOPUP_URL, FakeCoinLedger

TABLES = [
    Base.metadata.tables[name]
    for name in ("users", "shop_items", "shop_purchases", "shop_accounts", "admin_audit_logs", "red_packets", "red_packet_shares", "gifts", "gift_records", "group_plus_records", "coin_grants", "conversations", "spark_relights")
]
CREATION = "c0ffee" * 4
OID_A, OID_B, OID_D, OID_X = "a" * 24, "b" * 24, "d" * 24, "e" * 24
# 契约 2.2 里的例子
STYLE: dict[str, Any] = {
    "version": 1,
    "background": "linear-gradient(135deg, #7F7FD5 0%, #86A8E7 50%, #91EAE4 100%)",
    "text": "#ffffff",
    "link": "#e8f4ff",
    "border": {"color": "#ffffff33", "width": 1},
    "radius": 14,
    "shadow": "0 2px 8px #7F7FD566",
    "animation": "flow",
    "pattern": {"key": "stars", "color": "#ffffff", "opacity": 0.18, "scale": 1},
    "ornaments": [
        {"key": "sparkle", "corner": "tr", "color": "#fff6c4", "size": 18, "animated": True},
        {"key": "moon", "corner": "bl", "color": "#ffe9a8", "size": 14},
    ],
    "dark": {
        "background": "#223",
        "text": "#eee",
        "link": "#9cf",
        "border": {"color": "#445566", "width": 0},
        "shadow": "0 0 4px #000000",
        "pattern": {"key": "stars", "color": "#aab", "opacity": 0.1},
    },
}
PLAIN = {"version": 1, "background": "#112233", "text": "#ffffff"}
PATTERN = {"key": "dots", "color": "#ffffff", "opacity": 0.2}
ORNAMENT = {"key": "star", "corner": "tr", "color": "#ffcc00", "size": 16}
# 契约 2.4 里的例子
FRAME: dict[str, Any] = {
    "version": 1,
    "ring": {"background": "conic-gradient(from 0deg, #7F7FD5, #91EAE4, #7F7FD5)", "width": 3, "gap": 1},
    "glow": {"color": "#7F7FD588", "blur": 8},
    "animation": "spin",
    "ornaments": [{"key": "crown", "position": "top", "color": "#ffd76a", "size": 18, "animated": False}],
    "dark": {"ring": {"background": "#9a9af0"}, "glow": {"color": "#9a9af066", "blur": 10}},
}
RING = {"background": "#112233", "width": 3}
PLAIN_FRAME: dict[str, Any] = {"version": 1, "ring": RING}
FRAME_ORNAMENT = {"key": "halo", "position": "top", "color": "#ffcc00", "size": 16}
# 没发过也没抢过红包的人，balance 对象里红包那三项都是 0；全站一个红包都没有时对账结果里的红包账
# 没有红包、也没送过礼时 balance 对象里的那几项
NO_PACKETS = {"red_packet_sent": 0, "red_packet_received": 0, "red_packet_refunded": 0, "gift_spent": 0, "group_plus_spent": 0, "spark_spent": 0, "granted": 0}
NO_PACKET_LEDGER = {"total_sent": 0, "total_claimed": 0, "total_refunded": 0, "outstanding": 0, "balanced": True, "problems": []}
NO_PLUS_LEDGER = {"total_amount": 0, "total_count": 0, "granted_count": 0, "revoked_count": 0, "balanced": True, "problems": []}
NO_GIFT_LEDGER = {"total_amount": 0, "total_count": 0, "total_charm": 0, "total_received": 0, "balanced": True, "problems": []}
CATEGORIES_VIEW = [{"key": "bubble", "label": "气泡", "slot": "bubble"}, {"key": "frame", "label": "头像框", "slot": "frame"}]


class ShopTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        asyncio.get_running_loop().set_debug(False)
        # 进程内的缓存和限频状态，别让上一个用例的残留影响这一个
        shop_service.reset_runtime_state()
        self.engine = self._make_engine()
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(lambda sync: Base.metadata.create_all(sync, tables=TABLES))
        async with self.sessions() as db:
            users = [User(username=name, hashed_password="x") for name in ("alice", "bob", "carol", "dave", "admin")]
            users[0].ccw_student_oid, users[1].ccw_student_oid, users[3].ccw_student_oid = OID_A, OID_B, OID_D
            users[-1].platform_role = PlatformRole.ADMIN
            db.add_all(users)
            await db.commit()
            self.ids = {user.username: user.id for user in users}

        self.now = datetime(2026, 10, 12, 8, 0, tzinfo=UTC)
        # 余额来源：假账本。CREATION 是它的标识（后台设置里的 creation_oid）
        self.ledger = FakeCoinLedger(source_id=CREATION, source_setting="ccw_shop_creation_oid")
        integrations.set_coin_ledger(self.ledger)
        self.addCleanup(integrations.set_coin_ledger, None)
        self.send_to_user = AsyncMock()
        self.send_to_users = AsyncMock()
        self.broadcast = AsyncMock()
        for patcher in (
            patch.object(shop_service, "_now", lambda: self.now),
            patch.object(shop_service, "AsyncSessionLocal", self.sessions),
            patch.object(manager, "send_to_user", self.send_to_user),
            patch.object(manager, "send_to_users", self.send_to_users),
            patch.object(manager, "broadcast", self.broadcast),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self._settings(
            shop_refresh_interval_seconds=20,
            shop_ranking_recent_limit=200,
            shop_ranking_cache_seconds=60,
            # 默认不限 shop.changed 和 user.cosmetics.updated 的频率，测限频的用例自己调
            shop_changed_interval_seconds=0,
            shop_cosmetics_event_interval_seconds=0,
        )

    async def asyncTearDown(self) -> None:
        await shop_service.drain()
        await self.engine.dispose()

    def _make_engine(self):
        """每个用例一个临时文件库，而不是内存库。

        内存库所有会话共用一条连接：商店里有用自己的会话在后台读写的任务（核实余额、发装扮事件），
        共用连接时它一关会话就把请求那边还没提交的改动回滚掉，并发的事务也测不出来。文件库每个会话一条连接，和线上一样。
        """
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        engine = create_async_engine(f"sqlite+aiosqlite:///{(Path(self.tmp.name) / 'shop.db').as_posix()}")
        # 和线上一样：WAL、抢不到写锁就等
        event.listen(engine.sync_engine, "connect", configure_sqlite_connection)
        return engine

    def _settings(self, **values: Any) -> None:
        for name, value in values.items():
            patcher = patch.object(settings, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _events(self, event_type: str) -> list[Any]:
        """到现在为止广播给所有在线用户的某种事件，按先后顺序。"""
        return [call.args[1] for call in self.broadcast.await_args_list if call.args[0] == event_type]

    async def _cosmetics_events(self) -> list[Any]:
        await shop_service.drain()
        return self._events("user.cosmetics.updated")

    def tick(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)

    async def _item(self, sku: str, price: int = 0, unlock_level: int | None = None, style: dict[str, Any] | None = None, **extra: Any) -> int:
        async with self.sessions() as db:
            category = extra.pop("category", "bubble")
            item = ShopItem(
                sku=sku,
                category=category,
                name=extra.pop("name", sku),
                price=price,
                unlock_level=unlock_level,
                style=json.dumps(shop_service.CATEGORIES[category].validate(style or (FRAME if category == "frame" else STYLE))),
                **extra,
            )
            db.add(item)
            await db.commit()
            return item.id

    async def _as(self, name: str, function, *args: Any, level: int | None = None, **kwargs: Any):
        """用一个新会话、以某个用户的身份调服务层函数（和一次请求一样）。level：这个人的账号等级。"""
        async with self.sessions() as db:
            user = await db.get(User, self.ids[name])
            if level is not None:
                user.account_level = level
            return await function(db, user, *args, **kwargs)

    async def _fails(self, awaitable) -> tuple[int, str | None, dict[str, Any]]:
        with self.assertRaises(HTTPException) as caught:
            await awaitable
        detail = caught.exception.detail
        self.assertIsInstance(detail, dict)
        self.assertTrue(detail.get("message"))
        return caught.exception.status_code, detail.get("code"), detail

    async def _refresh(self, name: str = "alice") -> dict[str, Any]:
        return await self._as(name, shop_service.refresh_balance)

    async def _buy(self, name: str, item_id: int, level: int | None = None) -> dict[str, Any]:
        return await self._as(name, shop_service.purchase, item_id, level=level)

    async def _overview(self, name: str = "alice", level: int | None = None) -> dict[str, Any]:
        return await self._as(name, shop_service.overview, level=level)

    async def _equip(self, name: str, sku: str | None, slot: str = "bubble") -> dict[str, Any]:
        return await self._as(name, shop_service.set_equipped, slot, sku)

    async def _user(self, name: str) -> User:
        async with self.sessions() as db:
            return await db.get(User, self.ids[name])

    async def _account(self, name: str) -> ShopAccount | None:
        async with self.sessions() as db:
            return await db.get(ShopAccount, self.ids[name])

    async def _purchases(self, name: str | None = None) -> list[ShopPurchase]:
        async with self.sessions() as db:
            query = select(ShopPurchase).order_by(ShopPurchase.id)
            if name is not None:
                query = query.where(ShopPurchase.user_id == self.ids[name])
            return list((await db.execute(query)).scalars().all())

    async def _audit(self) -> list[tuple[str, dict[str, Any]]]:
        async with self.sessions() as db:
            rows = (await db.execute(select(AdminAuditLog).order_by(AdminAuditLog.id))).scalars().all()
            return [(row.action, json.loads(row.details or "{}")) for row in rows]

    async def _rebind(self, name: str, oid: str | None) -> None:
        async with self.sessions() as db:
            (await db.get(User, self.ids[name])).ccw_student_oid = oid
            await db.commit()

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


# ---------------------------------------------------------------- 样式校验


class StyleValidationTests(unittest.TestCase):
    def _field(self, style: Any) -> str:
        with self.assertRaises(StyleError) as caught:
            validate_bubble_style(style)
        return caught.exception.field

    def test_contract_example_is_valid_and_canonical(self) -> None:
        cleaned = validate_bubble_style(STYLE)
        self.assertEqual(cleaned["background"], "linear-gradient(135deg, #7F7FD5 0%, #86A8E7 50%, #91EAE4 100%)")
        self.assertEqual(cleaned["dark"], STYLE["dark"])
        self.assertEqual((cleaned["pattern"], cleaned["ornaments"]), (STYLE["pattern"], STYLE["ornaments"]))
        self.assertEqual(
            list(cleaned), ["version", "background", "text", "link", "border", "radius", "shadow", "animation", "pattern", "ornaments", "dark"]
        )
        self.assertEqual(cleaned, STYLE)
        # 规范形式再校验一遍还是它自己
        self.assertEqual(validate_bubble_style(cleaned), cleaned)

    def test_accepted_variants(self) -> None:
        for background in (
            "#abc",
            "#A1b2C3",
            "#a1b2c3d4",
            "linear-gradient(#fff, #000)",
            "linear-gradient(-45deg, #fff 0%, #000 100%)",
            "linear-gradient(90.5deg, #fff 12.5%, #000)",
            "radial-gradient(#ffffff 0%, #00000080 100%)",
            "linear-gradient(0deg, #111, #222, #333, #444, #555, #666, #777, #888)",
        ):
            self.assertEqual(validate_bubble_style({**PLAIN, "background": background})["background"], background)
        for shadow in ("0 2px 8px #7F7FD566", "0 0 0 #000", "-2px 4px 12px 1px #00000033", "0 1px 2px #00000033, 0 8px 24px -4px #00000040"):
            self.assertEqual(validate_bubble_style({**PLAIN, "shadow": shadow})["shadow"], shadow)
        for animation in ("none", "flow", "shimmer", "breathe"):
            self.assertEqual(validate_bubble_style({**PLAIN, "animation": animation})["animation"], animation)
        self.assertEqual(ANIMATIONS, ("none", "flow", "shimmer", "breathe"))
        self.assertEqual(validate_bubble_style({**PLAIN, "radius": 4, "border": {"color": "#fff", "width": 3}})["border"]["width"], 3)
        self.assertEqual(validate_bubble_style({**PLAIN, "radius": 22})["radius"], 22)
        self.assertEqual(validate_bubble_style({**PLAIN, "radius": 12.5, "border": {"color": "#fff", "width": 1.5}})["border"]["width"], 1.5)

    def test_nullable_fields_and_whitespace_are_normalized(self) -> None:
        cleaned = validate_bubble_style(
            {
                **PLAIN,
                "background": "linear-gradient(  135deg ,#fff   0% ,  #000 100%  )",
                "link": None,
                "border": None,
                "radius": None,
                "shadow": "  0   2px  8px   #00000033 ",
                "animation": None,
                "dark": {"background": None},
            }
        )
        self.assertEqual(
            cleaned, {"version": 1, "background": "linear-gradient(135deg, #fff 0%, #000 100%)", "text": "#ffffff", "shadow": "0 2px 8px #00000033"}
        )
        self.assertEqual(validate_bubble_style({**PLAIN, "radius": 14.0, "shadow": "0px 0 0px #000"}), {**PLAIN, "radius": 14, "shadow": "0 0 0 #000"})
        self.assertNotIn("dark", validate_bubble_style({**PLAIN, "dark": {}}))
        # 深色覆盖：background / text / link / border / shadow / pattern 都可以单独写，顺序规范化
        dark = {"pattern": PATTERN, "shadow": "0 0 4px #000", "border": {"color": "#fff", "width": 1}, "link": "#9cf", "text": "#eee", "background": "#000"}
        cleaned = validate_bubble_style({**PLAIN, "dark": dark})["dark"]
        self.assertEqual((cleaned, list(cleaned)), (dark, ["background", "text", "link", "border", "shadow", "pattern"]))
        for key, value in dark.items():
            self.assertEqual(validate_bubble_style({**PLAIN, "dark": {key: value}})["dark"], {key: value})

    def test_injection_attempts_are_rejected(self) -> None:
        for background in (
            "url(https://evil.example/x.png)",
            "url('javascript:alert(1)')",
            "expression(alert(1))",
            "#fff; position: fixed",
            "#fff;background:url(x)",
            "linear-gradient(135deg, #fff, #000); color: red",
            "linear-gradient(135deg, #fff, url(x))",
            "linear-gradient(135deg, #fff, #000) , url(x)",
            "linear-gradient(135deg, #fff, #000), linear-gradient(#fff, #000)",
            "linear-gradient(135deg, #fff, var(--x))",
            "linear-gradient(135deg, #fff, rgba(0,0,0,.5))",
            "linear-gradient(to right, #fff, #000)",
            "linear-gradient(135deg, red, blue)",
            "linear-gradient(135deg, #fff)",
            "linear-gradient(135deg, #fff 0%, #000 101%)",
            "linear-gradient(361deg, #fff, #000)",
            "linear-gradient(#fff, 135deg, #000)",
            "linear-gradient(135deg, #fff 10px, #000)",
            "linear-gradient(135deg, #fff, #000",
            "linear-gradient(135deg, #fff, #000))",
            "radial-gradient(135deg, #fff, #000)",
            "radial-gradient(circle at center, #fff, #000)",
            "conic-gradient(#fff, #000)",
            "repeating-linear-gradient(#fff, #000)",
            "LINEAR-GRADIENT(#fff, #000)",
            "linear-gradient(135deg, #fff, #000) /* x */",
            "linear-gradient(135deg, #fff,\n#000)",
            "linear-gradient(１３５deg, #fff, #000)",
            "linear-gradient(135deg, #fff, #000, #111, #222, #333, #444, #555, #666, #777)",
            "#ffff",
            "#12345",
            "#gggggg",
            "fff",
            "red",
            "rgb(0,0,0)",
            "transparent",
            "#fff\n",
            " #fff",
            "",
            "   ",
            "#fff" + " " * 300,
            None,
            123,
            ["#fff"],
            {"color": "#fff"},
        ):
            with self.subTest(background=background):
                self.assertEqual(self._field({**PLAIN, "background": background}), "background")
        for shadow in (
            "0 2px 8px red",
            "0 2px 8px",
            "2px 8px #000",
            "0 2px 8px 1px 1px #000",
            "inset 0 2px 8px #000",
            "0 2px 8px #000, 0 2px 8px #000, 0 2px 8px #000",
            "0 2px 8px #000; display:none",
            "0 2px 8px url(x)",
            "0 2 8px #000",
            "0 2em 8px #000",
            "0 2px -1px #000",
            "0 99px 8px #000",
            "0 2px 49px #000",
            "0 2px 8px 25px #000",
            "0 2px 8px #000,",
            "",
            5,
        ):
            with self.subTest(shadow=shadow):
                self.assertEqual(self._field({**PLAIN, "shadow": shadow}), "shadow")

    def test_bad_fields_are_named(self) -> None:
        cases: list[tuple[Any, str]] = [
            ("not a dict", "style"),
            (None, "style"),
            ([PLAIN], "style"),
            ({**PLAIN, "version": 2}, "version"),
            ({**PLAIN, "version": "1"}, "version"),
            ({**PLAIN, "version": True}, "version"),
            ({**PLAIN, "version": 1.0}, "version"),
            ({"background": "#fff", "text": "#000"}, "version"),
            ({"version": 1, "text": "#000"}, "background"),
            ({"version": 1, "background": "#fff"}, "text"),
            ({**PLAIN, "text": "white"}, "text"),
            ({**PLAIN, "text": "#fff;"}, "text"),
            ({**PLAIN, "text": None}, "text"),
            ({**PLAIN, "link": "javascript:alert(1)"}, "link"),
            ({**PLAIN, "link": 0}, "link"),
            # 多余的属性
            ({**PLAIN, "position": "fixed"}, "position"),
            ({**PLAIN, "css": "body{display:none}"}, "css"),
            ({**PLAIN, "font": "Comic Sans"}, "font"),
            ({**PLAIN, "border": {"color": "#fff", "width": 1, "style": "dashed"}}, "border.style"),
            ({**PLAIN, "border": "1px solid #fff"}, "border"),
            ({**PLAIN, "border": {"color": "#fff"}}, "border.width"),
            ({**PLAIN, "border": {"width": 1}}, "border.color"),
            ({**PLAIN, "border": {"color": "red", "width": 1}}, "border.color"),
            # 超范围的数值
            ({**PLAIN, "border": {"color": "#fff", "width": 4}}, "border.width"),
            ({**PLAIN, "border": {"color": "#fff", "width": -1}}, "border.width"),
            ({**PLAIN, "border": {"color": "#fff", "width": "1"}}, "border.width"),
            ({**PLAIN, "border": {"color": "#fff", "width": True}}, "border.width"),
            ({**PLAIN, "radius": 3}, "radius"),
            ({**PLAIN, "radius": 23}, "radius"),
            ({**PLAIN, "radius": "14px"}, "radius"),
            ({**PLAIN, "radius": float("nan")}, "radius"),
            ({**PLAIN, "radius": float("inf")}, "radius"),
            ({**PLAIN, "radius": True}, "radius"),
            ({**PLAIN, "animation": "spin"}, "animation"),
            ({**PLAIN, "animation": "flow 1s infinite"}, "animation"),
            ({**PLAIN, "animation": 1}, "animation"),
            ({**PLAIN, "dark": "#000"}, "dark"),
            ({**PLAIN, "dark": {"background": "url(x)"}}, "dark.background"),
            ({**PLAIN, "dark": {"text": "black"}}, "dark.text"),
            ({**PLAIN, "dark": {"shadow": "inset 0 0 0 #000"}}, "dark.shadow"),
            ({**PLAIN, "dark": {"border": {"color": "#000", "width": 9}}}, "dark.border.width"),
            ({**PLAIN, "dark": {"link": "blue"}}, "dark.link"),
            # 深色覆盖里只有配色相关的那几项；圆角、动效、角饰两种配色共用，不能在这里写
            ({**PLAIN, "dark": {"radius": 10}}, "dark.radius"),
            ({**PLAIN, "dark": {"animation": "flow"}}, "dark.animation"),
            ({**PLAIN, "dark": {"version": 1}}, "dark.version"),
            ({**PLAIN, "dark": {"dark": {}}}, "dark.dark"),
        ]
        for style, field in cases:
            with self.subTest(style=style):
                self.assertEqual(self._field(style), field)

    def test_patterns_and_ornaments_accept_only_builtin_names(self) -> None:
        self.assertEqual(PATTERNS, ("dots", "grid", "stripes", "waves", "stars", "hearts", "pixels", "clouds", "petals", "noise"))
        self.assertEqual(
            ORNAMENTS, ("sparkle", "star", "moon", "sun", "heart", "petal", "cloud", "paw", "leaf", "crown", "pixel_heart", "bolt", "snow")
        )
        for key in PATTERNS:
            pattern = {"key": key, "color": "#abc", "opacity": 0.6, "scale": 2}
            self.assertEqual(validate_bubble_style({**PLAIN, "pattern": pattern, "dark": {"pattern": pattern}})["dark"]["pattern"], pattern)
        for key in ORNAMENTS:
            ornament = {**ORNAMENT, "key": key}
            self.assertEqual(validate_bubble_style({**PLAIN, "ornaments": [ornament]})["ornaments"], [ornament])
        # 边界值、可空的字段、规范化
        self.assertEqual(validate_bubble_style({**PLAIN, "pattern": {**PATTERN, "opacity": 0, "scale": 0.5}})["pattern"], {**PATTERN, "opacity": 0, "scale": 0.5})
        self.assertEqual(validate_bubble_style({**PLAIN, "pattern": {**PATTERN, "opacity": 0.184, "scale": None}})["pattern"], {**PATTERN, "opacity": 0.18})
        self.assertEqual(validate_bubble_style({**PLAIN, "pattern": {**PATTERN, "scale": 1.0}})["pattern"], {**PATTERN, "scale": 1})
        four_corners = [{**ORNAMENT, "corner": corner} for corner in ("tl", "tr", "bl", "br")]
        for first, second in zip(four_corners, reversed(four_corners)):
            self.assertEqual(validate_bubble_style({**PLAIN, "ornaments": [first, second]})["ornaments"], [first, second])
        sized = [{**ORNAMENT, "size": 10, "animated": False}, {**ORNAMENT, "corner": "bl", "size": 28.0, "animated": None}]
        self.assertEqual(
            validate_bubble_style({**PLAIN, "ornaments": sized})["ornaments"],
            [{**ORNAMENT, "size": 10, "animated": False}, {**ORNAMENT, "corner": "bl", "size": 28}],
        )
        for empty in ({"pattern": None}, {"ornaments": None}, {"ornaments": []}, {"dark": {"pattern": None}}):
            self.assertEqual(validate_bubble_style({**PLAIN, **empty}), PLAIN)

        bad_patterns: list[tuple[Any, str]] = [
            ("stars", "pattern"),
            (["stars"], "pattern"),
            ({"color": "#fff", "opacity": 0.2}, "pattern.key"),
            ({**PATTERN, "key": None}, "pattern.key"),
            ({**PATTERN, "key": "unicorns"}, "pattern.key"),
            ({**PATTERN, "key": "Stars"}, "pattern.key"),
            ({**PATTERN, "key": "stars "}, "pattern.key"),
            ({**PATTERN, "key": "sparkle"}, "pattern.key"),
            ({**PATTERN, "key": 3}, "pattern.key"),
            ({**PATTERN, "key": ["stars"]}, "pattern.key"),
            # 图案只能是内置的名字，任何形式的地址都进不来
            ({**PATTERN, "key": "https://evil.example/p.png"}, "pattern.key"),
            ({**PATTERN, "key": "url(https://evil.example/p.png)"}, "pattern.key"),
            ({**PATTERN, "key": "data:image/svg+xml;base64,AAAA"}, "pattern.key"),
            ({**PATTERN, "key": "../../uploads/x.svg"}, "pattern.key"),
            ({**PATTERN, "url": "https://evil.example/p.png"}, "pattern.url"),
            ({**PATTERN, "image": "x.png"}, "pattern.image"),
            ({**PATTERN, "src": "x.png"}, "pattern.src"),
            ({"key": "dots", "opacity": 0.2}, "pattern.color"),
            ({**PATTERN, "color": "white"}, "pattern.color"),
            ({**PATTERN, "color": "url(x)"}, "pattern.color"),
            ({**PATTERN, "color": "#fff;background:url(x)"}, "pattern.color"),
            ({"key": "dots", "color": "#fff"}, "pattern.opacity"),
            ({**PATTERN, "opacity": 0.61}, "pattern.opacity"),
            ({**PATTERN, "opacity": 1}, "pattern.opacity"),
            ({**PATTERN, "opacity": -0.1}, "pattern.opacity"),
            ({**PATTERN, "opacity": "0.2"}, "pattern.opacity"),
            ({**PATTERN, "opacity": True}, "pattern.opacity"),
            ({**PATTERN, "opacity": float("nan")}, "pattern.opacity"),
            ({**PATTERN, "scale": 0.49}, "pattern.scale"),
            ({**PATTERN, "scale": 2.01}, "pattern.scale"),
            ({**PATTERN, "scale": 0}, "pattern.scale"),
            ({**PATTERN, "scale": "1"}, "pattern.scale"),
            ({**PATTERN, "scale": False}, "pattern.scale"),
        ]
        for pattern, field in bad_patterns:
            with self.subTest(pattern=pattern):
                self.assertEqual(self._field({**PLAIN, "pattern": pattern}), field)
                # 深色覆盖里的纹样是同一套规则
                self.assertEqual(self._field({**PLAIN, "dark": {"pattern": pattern}}), f"dark.{field}")

        bad_ornaments: list[tuple[Any, str]] = [
            ({"key": "star"}, "ornaments"),
            ("star", "ornaments"),
            ([ORNAMENT, {**ORNAMENT, "corner": "bl"}, {**ORNAMENT, "corner": "br"}], "ornaments"),
            (["star"], "ornaments[0]"),
            ([None], "ornaments[0]"),
            ([{k: v for k, v in ORNAMENT.items() if k != "key"}], "ornaments[0].key"),
            ([{**ORNAMENT, "key": "dragon"}], "ornaments[0].key"),
            ([{**ORNAMENT, "key": "stars"}], "ornaments[0].key"),
            ([{**ORNAMENT, "key": "pixel-heart"}], "ornaments[0].key"),
            ([{**ORNAMENT, "key": "https://evil.example/o.svg"}], "ornaments[0].key"),
            ([{**ORNAMENT, "key": "<svg onload=alert(1)>"}], "ornaments[0].key"),
            ([{**ORNAMENT, "key": 1}], "ornaments[0].key"),
            ([{**ORNAMENT, "url": "https://evil.example/o.svg"}], "ornaments[0].url"),
            ([{**ORNAMENT, "svg": "<svg/>"}], "ornaments[0].svg"),
            ([{**ORNAMENT, "href": "javascript:alert(1)"}], "ornaments[0].href"),
            ([{**ORNAMENT, "opacity": 0.5}], "ornaments[0].opacity"),
            ([{k: v for k, v in ORNAMENT.items() if k != "corner"}], "ornaments[0].corner"),
            ([{**ORNAMENT, "corner": "top-right"}], "ornaments[0].corner"),
            ([{**ORNAMENT, "corner": "TR"}], "ornaments[0].corner"),
            ([{**ORNAMENT, "corner": "center"}], "ornaments[0].corner"),
            ([{k: v for k, v in ORNAMENT.items() if k != "color"}], "ornaments[0].color"),
            ([{**ORNAMENT, "color": "gold"}], "ornaments[0].color"),
            ([{k: v for k, v in ORNAMENT.items() if k != "size"}], "ornaments[0].size"),
            ([{**ORNAMENT, "size": 9}], "ornaments[0].size"),
            ([{**ORNAMENT, "size": 29}], "ornaments[0].size"),
            ([{**ORNAMENT, "size": "18px"}], "ornaments[0].size"),
            ([{**ORNAMENT, "size": True}], "ornaments[0].size"),
            ([{**ORNAMENT, "animated": "yes"}], "ornaments[0].animated"),
            ([{**ORNAMENT, "animated": 1}], "ornaments[0].animated"),
            # 同一个角不能放两个；出问题的是第二个
            ([ORNAMENT, {**ORNAMENT, "key": "moon"}], "ornaments[1].corner"),
            ([ORNAMENT, {**ORNAMENT, "corner": "bl", "key": "ufo"}], "ornaments[1].key"),
        ]
        for ornaments, field in bad_ornaments:
            with self.subTest(ornaments=ornaments):
                self.assertEqual(self._field({**PLAIN, "ornaments": ornaments}), field)
        # 深色覆盖里没有角饰
        self.assertEqual(self._field({**PLAIN, "dark": {"ornaments": [ORNAMENT]}}), "dark.ornaments")

    def test_input_is_not_mutated(self) -> None:
        source = json.loads(json.dumps(STYLE))
        validate_bubble_style(source)
        self.assertEqual(source, STYLE)


class FrameStyleValidationTests(unittest.TestCase):
    def _field(self, style: Any) -> str:
        with self.assertRaises(StyleError) as caught:
            validate_frame_style(style)
        return caught.exception.field

    def test_contract_example_is_valid_and_canonical(self) -> None:
        cleaned = validate_frame_style(FRAME)
        self.assertEqual(cleaned, FRAME)
        self.assertEqual(list(cleaned), ["version", "ring", "glow", "animation", "ornaments", "dark"])
        self.assertEqual((list(cleaned["ring"]), list(cleaned["ornaments"][0])), (["background", "width", "gap"], ["key", "position", "color", "size", "animated"]))
        # 规范形式再校验一遍还是它自己；入参不被改动
        self.assertEqual(validate_frame_style(cleaned), cleaned)
        source = json.loads(json.dumps(FRAME))
        validate_frame_style(source)
        self.assertEqual(source, FRAME)
        # 两个类别的样式互不通用
        with self.assertRaises(StyleError):
            validate_bubble_style(FRAME)
        self.assertEqual(self._field(STYLE), "background")

    def test_accepted_variants(self) -> None:
        for background in (
            "#abc",
            "#A1b2C3",
            "#a1b2c3d4",
            "linear-gradient(135deg, #fff 0%, #000 100%)",
            "radial-gradient(#ffffff 0%, #00000080 100%)",
            "conic-gradient(#fff, #000)",
            "conic-gradient(from 0deg, #7F7FD5, #91EAE4, #7F7FD5)",
            "conic-gradient(from -90deg, #fff 0%, #000 50%, #fff 100%)",
            "conic-gradient(from 22.5deg, #fff 12.5%, #000)",
            "conic-gradient(from 360deg, #111, #222, #333, #444, #555, #666, #777, #888)",
        ):
            with self.subTest(background=background):
                cleaned = validate_frame_style({**PLAIN_FRAME, "ring": {**RING, "background": background}, "dark": {"ring": {"background": background}}})
                self.assertEqual((cleaned["ring"]["background"], cleaned["dark"]["ring"]["background"]), (background, background))
        for width, gap in ((2, 0), (6, 3), (2.5, 1.5), (4.0, 1.0)):
            self.assertEqual(validate_frame_style({**PLAIN_FRAME, "ring": {**RING, "width": width, "gap": gap}})["ring"], {**RING, "width": width, "gap": gap})
        self.assertIs(type(validate_frame_style({**PLAIN_FRAME, "ring": {**RING, "width": 4.0}})["ring"]["width"]), int)
        for blur in (0, 16, 7.5):
            self.assertEqual(validate_frame_style({**PLAIN_FRAME, "glow": {"color": "#abc", "blur": blur}})["glow"], {"color": "#abc", "blur": blur})
        self.assertEqual(FRAME_ANIMATIONS, ("none", "spin", "pulse", "shimmer"))
        for animation in FRAME_ANIMATIONS:
            self.assertEqual(validate_frame_style({**PLAIN_FRAME, "animation": animation})["animation"], animation)

    def test_nullable_fields_and_whitespace_are_normalized(self) -> None:
        cleaned = validate_frame_style(
            {
                "version": 1,
                "ring": {"background": "conic-gradient(  from   0deg ,#fff   0% ,  #000 100%  )", "width": 3.0, "gap": None},
                "glow": None,
                "animation": None,
                "ornaments": None,
                "dark": {"ring": {"background": None}, "glow": None},
            }
        )
        self.assertEqual(cleaned, {"version": 1, "ring": {"background": "conic-gradient(from 0deg, #fff 0%, #000 100%)", "width": 3}})
        for empty in ({"ornaments": []}, {"dark": {}}, {"dark": {"ring": None}}, {"dark": {"ring": {}}}):
            self.assertEqual(validate_frame_style({**PLAIN_FRAME, **empty}), PLAIN_FRAME)
        # 深色覆盖：环的底色和外发光可以各写各的，顺序规范化
        dark = {"glow": {"blur": 4, "color": "#fff"}, "ring": {"background": "#000"}}
        cleaned = validate_frame_style({**PLAIN_FRAME, "dark": dark})["dark"]
        self.assertEqual((cleaned, list(cleaned), list(cleaned["glow"])), (dark, ["ring", "glow"], ["color", "blur"]))
        for key, value in dark.items():
            self.assertEqual(validate_frame_style({**PLAIN_FRAME, "dark": {key: value}})["dark"], {key: value})

    def test_conic_gradient_is_only_for_the_ring(self) -> None:
        conic = "conic-gradient(from 0deg, #fff, #000)"
        # 气泡的底色（包括深色覆盖）不放开
        with self.assertRaises(StyleError) as caught:
            validate_bubble_style({**PLAIN, "background": conic})
        self.assertEqual((caught.exception.field, "conic" in caught.exception.message), ("background", False))
        with self.assertRaises(StyleError) as caught:
            validate_bubble_style({**PLAIN, "dark": {"background": conic}})
        self.assertEqual(caught.exception.field, "dark.background")
        with self.assertRaises(StyleError) as caught:
            validate_frame_style({**PLAIN_FRAME, "ring": {**RING, "background": "url(x)"}})
        self.assertIn("conic-gradient", caught.exception.message)

    def test_injection_attempts_are_rejected(self) -> None:
        for background in (
            "url(https://evil.example/x.png)",
            "url('javascript:alert(1)')",
            "expression(alert(1))",
            "#fff; position: fixed",
            "conic-gradient(from 0deg, #fff, #000); color: red",
            "conic-gradient(from 0deg, #fff, url(x))",
            "conic-gradient(from 0deg, #fff, #000) , url(x)",
            "conic-gradient(from 0deg, #fff, #000), conic-gradient(#fff, #000)",
            "conic-gradient(from 0deg, #fff, var(--x))",
            "conic-gradient(from 0deg, #fff, rgba(0,0,0,.5))",
            "conic-gradient(from 0deg, red, blue)",
            "conic-gradient(from 0deg, #fff)",
            "conic-gradient(from 0deg, #fff 0%, #000 101%)",
            "conic-gradient(from 361deg, #fff, #000)",
            # from 只能在最前面、只能带 deg 角度；at 位置、按角度写的色标、别的单位都不行
            "conic-gradient(#fff, from 0deg, #000)",
            "conic-gradient(0deg, #fff, #000)",
            "conic-gradient(from 0deg at 50% 50%, #fff, #000)",
            "conic-gradient(at 50% 50%, #fff, #000)",
            "conic-gradient(from 0.5turn, #fff, #000)",
            "conic-gradient(from 1rad, #fff, #000)",
            "conic-gradient(from 0, #fff, #000)",
            "conic-gradient(from deg, #fff, #000)",
            "conic-gradient(from 0deg from 90deg, #fff, #000)",
            "conic-gradient(from 0deg, #fff 0deg, #000 180deg)",
            "conic-gradient(from 0deg, #fff 10px, #000)",
            "conic-gradient(from 0deg, #fff, #000",
            "conic-gradient(from 0deg, #fff, #000))",
            "conic-gradient(from 0deg, #fff,\n#000)",
            "conic-gradient(from ０deg, #fff, #000)",
            "conic-gradient(from 0deg, #fff, #000, #111, #222, #333, #444, #555, #666, #777)",
            "conic-gradient(from 0deg, #fff, #000) /* x */",
            "repeating-conic-gradient(#fff, #000)",
            "CONIC-GRADIENT(#fff, #000)",
            "linear-gradient(from 0deg, #fff, #000)",
            "radial-gradient(from 0deg, #fff, #000)",
            "linear-gradient(to right, #fff, #000)",
            "red",
            "transparent",
            "#ffff",
            " #fff",
            "",
            "#fff" + " " * 300,
            123,
            ["#fff"],
        ):
            with self.subTest(background=background):
                self.assertEqual(self._field({**PLAIN_FRAME, "ring": {**RING, "background": background}}), "ring.background")
                self.assertEqual(self._field({**PLAIN_FRAME, "dark": {"ring": {"background": background}}}), "dark.ring.background")

    def test_bad_fields_are_named(self) -> None:
        glow = {"color": "#fff", "blur": 4}
        cases: list[tuple[Any, str]] = [
            ("not a dict", "style"),
            (None, "style"),
            ([PLAIN_FRAME], "style"),
            ({**PLAIN_FRAME, "version": 2}, "version"),
            ({**PLAIN_FRAME, "version": "1"}, "version"),
            ({**PLAIN_FRAME, "version": True}, "version"),
            ({"ring": RING}, "version"),
            ({"version": 1}, "ring"),
            ({"version": 1, "ring": None}, "ring"),
            ({"version": 1, "ring": "#fff"}, "ring"),
            ({"version": 1, "ring": {"width": 3}}, "ring.background"),
            ({"version": 1, "ring": {"background": None, "width": 3}}, "ring.background"),
            ({"version": 1, "ring": {"background": "#fff"}}, "ring.width"),
            # 超范围的数值
            ({"version": 1, "ring": {**RING, "width": 1}}, "ring.width"),
            ({"version": 1, "ring": {**RING, "width": 7}}, "ring.width"),
            ({"version": 1, "ring": {**RING, "width": "3px"}}, "ring.width"),
            ({"version": 1, "ring": {**RING, "width": True}}, "ring.width"),
            ({"version": 1, "ring": {**RING, "width": float("nan")}}, "ring.width"),
            ({"version": 1, "ring": {**RING, "gap": -1}}, "ring.gap"),
            ({"version": 1, "ring": {**RING, "gap": 3.5}}, "ring.gap"),
            ({"version": 1, "ring": {**RING, "gap": "1"}}, "ring.gap"),
            ({**PLAIN_FRAME, "glow": "#fff"}, "glow"),
            ({**PLAIN_FRAME, "glow": {"blur": 4}}, "glow.color"),
            ({**PLAIN_FRAME, "glow": {"color": "gold", "blur": 4}}, "glow.color"),
            ({**PLAIN_FRAME, "glow": {"color": "#fff;", "blur": 4}}, "glow.color"),
            ({**PLAIN_FRAME, "glow": {"color": "#fff"}}, "glow.blur"),
            ({**PLAIN_FRAME, "glow": {**glow, "blur": 17}}, "glow.blur"),
            ({**PLAIN_FRAME, "glow": {**glow, "blur": -1}}, "glow.blur"),
            ({**PLAIN_FRAME, "glow": {**glow, "blur": "8px"}}, "glow.blur"),
            ({**PLAIN_FRAME, "animation": "flow"}, "animation"),
            ({**PLAIN_FRAME, "animation": "breathe"}, "animation"),
            ({**PLAIN_FRAME, "animation": "spin 1s infinite"}, "animation"),
            ({**PLAIN_FRAME, "animation": 1}, "animation"),
            # 多余的属性：气泡的字段在这里也不认
            ({**PLAIN_FRAME, "background": "#fff"}, "background"),
            ({**PLAIN_FRAME, "text": "#fff"}, "text"),
            ({**PLAIN_FRAME, "pattern": PATTERN}, "pattern"),
            ({**PLAIN_FRAME, "css": "body{display:none}"}, "css"),
            ({**PLAIN_FRAME, "image": "https://evil.example/f.png"}, "image"),
            ({"version": 1, "ring": {**RING, "style": "dashed"}}, "ring.style"),
            ({"version": 1, "ring": {**RING, "image": "x.png"}}, "ring.image"),
            ({**PLAIN_FRAME, "glow": {**glow, "spread": 2}}, "glow.spread"),
            # 深色覆盖里只有 ring.background 和 glow
            ({**PLAIN_FRAME, "dark": "#000"}, "dark"),
            ({**PLAIN_FRAME, "dark": {"ring": "#000"}}, "dark.ring"),
            ({**PLAIN_FRAME, "dark": {"ring": {"background": "#000", "width": 3}}}, "dark.ring.width"),
            ({**PLAIN_FRAME, "dark": {"ring": {"gap": 1}}}, "dark.ring.gap"),
            ({**PLAIN_FRAME, "dark": {"glow": {"color": "#000"}}}, "dark.glow.blur"),
            ({**PLAIN_FRAME, "dark": {"glow": {**glow, "blur": 99}}}, "dark.glow.blur"),
            ({**PLAIN_FRAME, "dark": {"animation": "spin"}}, "dark.animation"),
            ({**PLAIN_FRAME, "dark": {"ornaments": [FRAME_ORNAMENT]}}, "dark.ornaments"),
            ({**PLAIN_FRAME, "dark": {"background": "#000"}}, "dark.background"),
            ({**PLAIN_FRAME, "dark": {"dark": {}}}, "dark.dark"),
        ]
        for style, field in cases:
            with self.subTest(style=style):
                self.assertEqual(self._field(style), field)

    def test_ornaments_accept_only_builtin_names(self) -> None:
        self.assertEqual(FRAME_ORNAMENTS, (*ORNAMENTS, "cat_ears", "halo", "horns", "laurel", "wings", "bow"))
        self.assertEqual(FRAME_POSITIONS, ("top", "tr", "right", "br", "bottom", "bl", "left", "tl"))
        for key in FRAME_ORNAMENTS:
            ornament = {**FRAME_ORNAMENT, "key": key}
            self.assertEqual(validate_frame_style({**PLAIN_FRAME, "ornaments": [ornament]})["ornaments"], [ornament])
        for position in FRAME_POSITIONS:
            ornament = {**FRAME_ORNAMENT, "position": position}
            self.assertEqual(validate_frame_style({**PLAIN_FRAME, "ornaments": [ornament]})["ornaments"], [ornament])
        # 头像框专用的那几个，气泡不能用
        for key in ("cat_ears", "halo", "horns", "laurel", "wings", "bow"):
            with self.assertRaises(StyleError) as caught:
                validate_bubble_style({**PLAIN, "ornaments": [{**ORNAMENT, "key": key}]})
            self.assertEqual(caught.exception.field, "ornaments[0].key")
        # 最多 3 个；边界值、可空的 animated
        three = [
            {**FRAME_ORNAMENT, "size": 10, "animated": False},
            {**FRAME_ORNAMENT, "key": "star", "position": "bl", "size": 28.0, "animated": None},
            {**FRAME_ORNAMENT, "key": "wings", "position": "left", "animated": True},
        ]
        self.assertEqual(
            validate_frame_style({**PLAIN_FRAME, "ornaments": three})["ornaments"],
            [
                {**FRAME_ORNAMENT, "size": 10, "animated": False},
                {**FRAME_ORNAMENT, "key": "star", "position": "bl", "size": 28},
                {**FRAME_ORNAMENT, "key": "wings", "position": "left", "animated": True},
            ],
        )
        bad: list[tuple[Any, str]] = [
            ({"key": "halo"}, "ornaments"),
            ("halo", "ornaments"),
            ([*three, {**FRAME_ORNAMENT, "position": "right"}], "ornaments"),
            (["halo"], "ornaments[0]"),
            ([None], "ornaments[0]"),
            ([{k: v for k, v in FRAME_ORNAMENT.items() if k != "key"}], "ornaments[0].key"),
            ([{**FRAME_ORNAMENT, "key": "dragon"}], "ornaments[0].key"),
            ([{**FRAME_ORNAMENT, "key": "cat-ears"}], "ornaments[0].key"),
            ([{**FRAME_ORNAMENT, "key": "stars"}], "ornaments[0].key"),
            ([{**FRAME_ORNAMENT, "key": "https://evil.example/o.svg"}], "ornaments[0].key"),
            ([{**FRAME_ORNAMENT, "key": "<svg onload=alert(1)>"}], "ornaments[0].key"),
            ([{**FRAME_ORNAMENT, "key": 1}], "ornaments[0].key"),
            ([{**FRAME_ORNAMENT, "url": "https://evil.example/o.svg"}], "ornaments[0].url"),
            ([{**FRAME_ORNAMENT, "svg": "<svg/>"}], "ornaments[0].svg"),
            # 位置字段叫 position，不是气泡的 corner
            ([{**FRAME_ORNAMENT, "corner": "tr"}], "ornaments[0].corner"),
            ([{k: v for k, v in FRAME_ORNAMENT.items() if k != "position"}], "ornaments[0].position"),
            ([{**FRAME_ORNAMENT, "position": "center"}], "ornaments[0].position"),
            ([{**FRAME_ORNAMENT, "position": "TOP"}], "ornaments[0].position"),
            ([{**FRAME_ORNAMENT, "position": "top-right"}], "ornaments[0].position"),
            ([{**FRAME_ORNAMENT, "position": 0}], "ornaments[0].position"),
            ([{k: v for k, v in FRAME_ORNAMENT.items() if k != "color"}], "ornaments[0].color"),
            ([{**FRAME_ORNAMENT, "color": "gold"}], "ornaments[0].color"),
            ([{k: v for k, v in FRAME_ORNAMENT.items() if k != "size"}], "ornaments[0].size"),
            ([{**FRAME_ORNAMENT, "size": 9}], "ornaments[0].size"),
            ([{**FRAME_ORNAMENT, "size": 29}], "ornaments[0].size"),
            ([{**FRAME_ORNAMENT, "size": "18px"}], "ornaments[0].size"),
            ([{**FRAME_ORNAMENT, "animated": "yes"}], "ornaments[0].animated"),
            ([{**FRAME_ORNAMENT, "animated": 1}], "ornaments[0].animated"),
            # 同一个位置不能放两个；出问题的是第二个
            ([FRAME_ORNAMENT, {**FRAME_ORNAMENT, "key": "crown"}], "ornaments[1].position"),
            ([FRAME_ORNAMENT, {**FRAME_ORNAMENT, "position": "bl", "key": "ufo"}], "ornaments[1].key"),
        ]
        for ornaments, field in bad:
            with self.subTest(ornaments=ornaments):
                self.assertEqual(self._field({**PLAIN_FRAME, "ornaments": ornaments}), field)


# ---------------------------------------------------------------- 余额


class BalanceTests(ShopTestCase):
    async def test_never_verified_overview_does_not_query_the_ledger(self) -> None:
        self.ledger.donate(OID_A, 100)
        view = await self._overview("alice")
        self.assertEqual(view["balance"], {"bound": True, "donated": 0, "spent": 0, "balance": 0, "verified_at": None, "stale": False, **NO_PACKETS})
        self.assertEqual((await self._overview("carol"))["balance"], {"bound": False, "donated": 0, "spent": 0, "balance": 0, "verified_at": None, "stale": False, **NO_PACKETS})
        self.assertEqual(self.ledger.calls, [])
        self.assertIsNone(await self._account("alice"))

    async def test_refresh_asks_the_ledger_once(self) -> None:
        self.ledger.donate(OID_A, 320.9)
        balance = await self._refresh()
        self.assertEqual(balance, {"bound": True, "donated": 320, "spent": 0, "balance": 320, "verified_at": self.now, "stale": False, **NO_PACKETS})
        # 问的是用户绑定的那个外部账号
        self.assertEqual(self.ledger.calls, [("credited_to", OID_A)])
        self.assertEqual((await self._overview())["balance"], balance)
        account = await self._account("alice")
        self.assertEqual((account.ccw_oid, account.donated, account.spent, account.verify_ok, account.last_error), (OID_A, 320, 0, True, None))

    async def test_refresh_follows_the_ledger(self) -> None:
        self.ledger.donate(OID_A, 80)
        self.ledger.donate(OID_B, 7)
        self.ledger.fill(300)
        self.assertEqual((await self._refresh("alice"))["donated"], 80)
        self.assertEqual((await self._refresh("bob"))["donated"], 7)
        self.tick(21)
        self.assertEqual((await self._refresh("alice"))["donated"], 80)
        # 每次核实都只问账本一次；要不要缓存、怎么翻页是账本实现自己的事
        self.assertEqual(self.ledger.calls, [("credited_to", OID_A), ("credited_to", OID_B), ("credited_to", OID_A)])
        # 投完币回来：立刻能看到
        self.ledger.donate(OID_A, 130)
        self.tick(21)
        self.assertEqual((await self._refresh("alice"))["donated"], 130)

    async def test_refresh_for_someone_who_never_donated(self) -> None:
        self.ledger.fill(300)
        balance = await self._refresh("dave")
        self.assertEqual(balance, {"bound": True, "donated": 0, "spent": 0, "balance": 0, "verified_at": self.now, "stale": False, **NO_PACKETS})

    async def test_ledger_errors_keep_the_last_number_and_mark_stale(self) -> None:
        self.ledger.donate(OID_A, 100)
        verified_at = self.now
        await self._refresh()
        for problem in (OSError("boom"), integrations.CoinsUnavailable("返回格式不对")):
            self.tick(21)
            self.ledger.error = problem
            with self.subTest(problem=problem):
                balance = await self._refresh()
                self.assertEqual(balance, {"bound": True, "donated": 100, "spent": 0, "balance": 100, "verified_at": verified_at, "stale": True, **NO_PACKETS})
                account = await self._account("alice")
                self.assertEqual((account.donated, account.verify_ok, bool(account.last_error)), (100, False, True))
                self.assertTrue((await self._overview())["balance"]["stale"])
        # 恢复之后 stale 消失
        self.tick(21)
        self.ledger.error = None
        self.ledger.donate(OID_A, 150)
        self.assertEqual(await self._refresh(), {"bound": True, "donated": 150, "spent": 0, "balance": 150, "verified_at": self.now, "stale": False, **NO_PACKETS})

    async def test_first_ever_refresh_failing(self) -> None:
        self.ledger.error = TimeoutError()
        self.assertEqual(await self._refresh(), {"bound": True, "donated": 0, "spent": 0, "balance": 0, "verified_at": None, "stale": True, **NO_PACKETS})

    async def test_refresh_requires_binding(self) -> None:
        self.assertEqual((await self._fails(self._refresh("carol")))[:2], (409, "ccw_not_bound"))
        self.assertEqual(self.ledger.calls, [])

    async def test_unconfigured_ledger_means_stale_and_no_queries(self) -> None:
        self.ledger.source_id = None
        await self._check_shop_without_a_balance_source()
        self.assertEqual(self.ledger.calls, [])

    async def test_without_any_ledger_implementation(self) -> None:
        """没有接入余额来源（公开仓库单独跑时的占位实现）：和「没配置」一样，能逛、能领，不能花币买。"""
        integrations.set_coin_ledger(integrations.NullCoinLedger())
        await self._check_shop_without_a_balance_source()
        view = shop_service.admin_settings()
        self.assertEqual((view["configured"], view["creation_oid"], view["creation_oid_source"], view["donate_url"]), (False, None, None, None))
        status_code, code, _detail = await self._fails(self._as("admin", shop_service.admin_reconcile))
        self.assertEqual((status_code, code, await self._audit()), (503, "verification_unavailable", []))

    async def _check_shop_without_a_balance_source(self) -> None:
        paid = await self._item("bubble.paid", price=10)
        free = await self._item("bubble.free", unlock_level=2)
        for _ in range(3):
            # 没配置时刷新不计入限频，也不建账户
            self.assertEqual(await self._refresh(), {"bound": True, "donated": 0, "spent": 0, "balance": 0, "verified_at": None, "stale": True, **NO_PACKETS})
        view = await self._overview()
        self.assertEqual((view["balance"]["stale"], view["donate_url"]), (True, None))
        # 没绑定外部账号的人：不算 stale（本来就没有余额可言）
        self.assertEqual((await self._overview("carol"))["balance"]["stale"], False)
        self.assertEqual((await self._fails(self._buy("alice", paid)))[:2], (503, "verification_unavailable"))
        # 不花钱的照常能领
        self.assertEqual((await self._buy("alice", free, level=2))["item"]["owned"], True)
        self.assertIsNone(await self._account("alice"))

    async def test_refresh_is_limited_to_once_per_interval(self) -> None:
        self.ledger.donate(OID_A, 100)
        await self._refresh()
        status_code, code, detail = await self._fails(self._refresh())
        self.assertEqual((status_code, code, detail["retry_after"]), (429, "refresh_too_fast", 20))
        self.tick(12.5)
        self.assertEqual((await self._fails(self._refresh()))[2]["retry_after"], 8)
        # 别人不受影响
        await self._refresh("bob")
        self.tick(7.4)
        self.assertEqual((await self._fails(self._refresh()))[2]["retry_after"], 1)
        self.assertEqual(len(self.ledger.calls), 2)
        self.tick(0.2)
        await self._refresh()
        self.assertEqual(len(self.ledger.calls), 3)
        # 失败的那次也算一次
        self.ledger.error = OSError("down")
        self.tick(20)
        self.assertTrue((await self._refresh())["stale"])
        self.assertEqual((await self._fails(self._refresh()))[:2], (429, "refresh_too_fast"))

    async def test_rebinding_revalidates_donated_and_keeps_spent(self) -> None:
        item = await self._item("bubble.sixty", price=60)
        cheap = await self._item("bubble.cheap", price=20)
        self.ledger.donate(OID_A, 100)
        self.ledger.donate(OID_X, 30)
        self.assertEqual((await self._buy("alice", item))["balance"]["balance"], 40)
        await self._rebind("alice", OID_X)
        # 换绑之后、重新核实之前：原来那个账号的累计投币不算数了
        self.assertEqual((await self._overview())["balance"], {"bound": True, "donated": 0, "spent": 60, "balance": -60, "verified_at": None, "stale": True, **NO_PACKETS})
        self.tick(21)
        balance = await self._refresh()
        self.assertEqual(balance, {"bound": True, "donated": 30, "spent": 60, "balance": -30, "verified_at": self.now, "stale": False, **NO_PACKETS})
        status_code, code, detail = await self._fails(self._buy("alice", cheap))
        self.assertEqual((status_code, code, detail["balance"], detail["price"], detail["shortfall"]), (402, "insufficient_coins", -30, 20, 50))
        # 后台能看到这个人花的比投的多
        accounts = await self._as("admin", lambda db, _admin: shop_service.admin_list_accounts(db, q=None, mismatch=True, limit=50, offset=0))
        self.assertEqual([(row["user"]["username"], row["donated"], row["spent"], row["balance"], row["mismatch"]) for row in accounts["items"]], [("alice", 30, 60, -30, True)])
        # 解绑：能逛不能买
        await self._rebind("alice", None)
        self.assertEqual((await self._overview())["balance"], {"bound": False, "donated": 0, "spent": 60, "balance": -60, "verified_at": None, "stale": False, **NO_PACKETS})
        self.assertEqual((await self._fails(self._buy("alice", cheap)))[:2], (409, "ccw_not_bound"))

    async def test_a_ccw_account_cannot_be_spent_twice_by_moving_it(self) -> None:
        """同一个 CCW 账号解绑后绑到另一个 KukeChat 账号上：它已经被花掉的币不会再冒出来。"""
        sixty = await self._item("bubble.sixty", price=60)
        forty = await self._item("bubble.forty", price=40)
        self.ledger.donate(OID_A, 100)
        await self._buy("alice", sixty)
        await self._rebind("alice", None)
        await self._rebind("carol", OID_A)
        balance = await self._refresh("carol")
        self.assertEqual(balance, {"bound": True, "donated": 100, "spent": 60, "balance": 40, "verified_at": self.now, "stale": False, **NO_PACKETS})
        status_code, code, detail = await self._fails(self._buy("carol", sixty))
        self.assertEqual((status_code, code, detail["balance"], detail["shortfall"]), (402, "insufficient_coins", 40, 20))
        self.assertEqual((await self._buy("carol", forty))["balance"]["balance"], 0)
        accounts = await self._as("admin", lambda db, _admin: shop_service.admin_list_accounts(db, q="carol", mismatch=None, limit=50, offset=0))
        self.assertEqual([(row["spent"], row["shared_spent"], row["balance"], row["mismatch"]) for row in accounts["items"]], [(40, 60, 0, False)])


# ---------------------------------------------------------------- 购买


class PurchaseTests(ShopTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.item = await self._item("bubble.sixty", price=60, name="六十")
        self.ledger.donate(OID_A, 100)

    async def test_purchase_charges_and_delivers(self) -> None:
        result = await self._buy("alice", self.item)
        self.assertEqual(set(result), {"item", "balance", "equipped"})
        self.assertEqual((result["item"]["sku"], result["item"]["owned"], result["item"]["equipped"], result["item"]["obtainable"]), ("bubble.sixty", True, False, "owned"))
        self.assertEqual(result["balance"], {"bound": True, "donated": 100, "spent": 60, "balance": 40, "verified_at": self.now, "stale": False, **NO_PACKETS})
        self.assertEqual(result["equipped"], {})
        rows = await self._purchases("alice")
        self.assertEqual([(row.item_id, row.price, row.source, row.ccw_oid, row.revoked_at) for row in rows], [(self.item, 60, "purchase", OID_A, None)])
        self.assertEqual((await self._account("alice")).spent, 60)
        history = await self._as("alice", lambda db, user: shop_service.list_purchases(db, user.id, 50, 0))
        self.assertEqual(history["total"], 1)
        self.assertEqual(
            {key: history["items"][0][key] for key in ("item", "price", "source", "created_at")},
            {"item": {"sku": "bubble.sixty", "name": "六十", "category": "bubble"}, "price": 60, "source": "purchase", "created_at": self.now},
        )

    async def test_item_not_found(self) -> None:
        self.assertEqual((await self._fails(self._buy("alice", 99999)))[:2], (404, "item_not_found"))

    async def test_item_unavailable_when_inactive_or_expired(self) -> None:
        inactive = await self._item("bubble.off", price=10, is_active=False)
        expired = await self._item("bubble.expired", price=10, available_until=self.now - timedelta(seconds=1))
        limited = await self._item("bubble.limited", price=10, available_until=self.now + timedelta(hours=1))
        for item in (inactive, expired):
            self.assertEqual((await self._fails(self._buy("alice", item)))[:2], (409, "item_unavailable"))
            # 等级够了也领不了下架的
            self.assertEqual((await self._fails(self._buy("alice", item, level=200)))[:2], (409, "item_unavailable"))
        self.assertEqual(self.ledger.calls, [])
        self.assertEqual((await self._buy("alice", limited))["balance"]["balance"], 90)
        self.assertEqual(await self._purchases("carol"), [])

    async def test_already_owned(self) -> None:
        await self._buy("alice", self.item)
        self.assertEqual((await self._fails(self._buy("alice", self.item)))[:2], (409, "already_owned"))
        self.assertEqual(((await self._account("alice")).spent, len(await self._purchases("alice"))), (60, 1))

    async def test_ccw_not_bound(self) -> None:
        self.assertEqual((await self._fails(self._buy("carol", self.item)))[:2], (409, "ccw_not_bound"))
        self.assertEqual((self.ledger.calls, await self._purchases()), ([], []))

    async def test_item_locked(self) -> None:
        locked = await self._item("bubble.level", unlock_level=4)
        grant_only = await self._item("bubble.gift")
        for name in ("alice", "carol"):
            self.assertEqual((await self._fails(self._buy(name, locked, level=3)))[:2], (409, "item_locked"))
            self.assertEqual((await self._fails(self._buy(name, grant_only, level=256)))[:2], (409, "item_locked"))
        self.assertEqual(self.ledger.calls, [])

    async def test_insufficient_coins_carries_the_numbers(self) -> None:
        pricey = await self._item("bubble.pricey", price=250)
        status_code, code, detail = await self._fails(self._buy("alice", pricey))
        self.assertEqual((status_code, code), (402, "insufficient_coins"))
        self.assertEqual(
            {key: detail[key] for key in ("balance", "price", "shortfall", "donate_url")},
            {"balance": 100, "price": 250, "shortfall": 150, "donate_url": TOPUP_URL},
        )
        self.assertEqual(((await self._account("alice")).spent, await self._purchases()), (0, []))
        # 差一个币也不行，刚好够可以
        await self._buy("alice", self.item)
        exact = await self._item("bubble.exact", price=40)
        over = await self._item("bubble.over", price=41)
        self.assertEqual((await self._fails(self._buy("alice", over)))[2]["shortfall"], 1)
        self.assertEqual((await self._buy("alice", exact))["balance"]["balance"], 0)

    async def test_verification_failure_charges_nothing(self) -> None:
        self.ledger.error = OSError("bad gateway")
        self.assertEqual((await self._fails(self._buy("alice", self.item)))[:2], (503, "verification_unavailable"))
        account = await self._account("alice")
        self.assertEqual((account.spent, account.verify_ok, await self._purchases()), (0, False, []))
        # 上次核实成功过也一样：不拿旧数字扣币
        self.ledger.error = None
        self.tick(21)
        await self._refresh()
        self.tick(21)
        self.ledger.error = integrations.CoinsUnavailable("返回格式不对")
        self.assertEqual((await self._fails(self._buy("alice", self.item)))[:2], (503, "verification_unavailable"))
        account = await self._account("alice")
        self.assertEqual((account.donated, account.spent, account.verify_ok, await self._purchases()), (100, 0, False, []))
        self.assertTrue((await self._overview())["balance"]["stale"])

    async def test_purchase_reuses_a_recent_verification_only(self) -> None:
        other = await self._item("bubble.other", price=10)
        third = await self._item("bubble.third", price=10)
        await self._refresh()
        self.assertEqual(self.ledger.calls, [("credited_to", OID_A)])
        self.tick(19)
        await self._buy("alice", other)
        self.assertEqual(self.ledger.calls, [("credited_to", OID_A)])
        # 超过 20 秒就重新核实；这期间 CCW 上的数变了，以新的为准
        self.tick(2)
        self.ledger.donate(OID_A, 15)
        status_code, code, detail = await self._fails(self._buy("alice", self.item))
        self.assertEqual((status_code, code, detail["balance"], len(self.ledger.calls)), (402, "insufficient_coins", 5, 2))
        # 刚核实失败过：20 秒内不再追着 CCW 问，直接 503
        self.tick(21)
        self.ledger.error = OSError("down")
        self.assertEqual((await self._fails(self._buy("alice", third)))[:2], (503, "verification_unavailable"))
        self.ledger.error = None
        self.assertEqual((await self._fails(self._buy("alice", third)))[:2], (503, "verification_unavailable"))
        self.assertEqual(len(self.ledger.calls), 3)
        self.tick(21)
        self.ledger.donate(OID_A, 100)
        self.assertEqual((await self._buy("alice", third))["balance"]["balance"], 80)

    async def test_item_changed_while_verifying(self) -> None:
        """核实余额要等 CCW 几秒，这期间商品被改价或下架：按最新的来。"""
        real_verify = shop_service._verify_once

        async def reprice(user_id, oid):
            async with self.sessions() as other:
                (await other.get(ShopItem, self.item)).price = 80
                await other.commit()
            return await real_verify(user_id, oid)

        with patch.object(shop_service, "_verify_once", reprice):
            result = await self._buy("alice", self.item)
        self.assertEqual((result["item"]["price"], result["balance"]["spent"], (await self._purchases())[0].price), (80, 80, 80))

        gone = await self._item("bubble.gone", price=5)

        async def delist(user_id, oid):
            async with self.sessions() as other:
                (await other.get(ShopItem, gone)).is_active = False
                await other.commit()
            return await real_verify(user_id, oid)

        self.tick(21)
        with patch.object(shop_service, "_verify_once", delist):
            self.assertEqual((await self._fails(self._buy("alice", gone)))[:2], (409, "item_unavailable"))
        self.assertEqual((await self._account("alice")).spent, 80)

    async def test_level_unlock_is_claimed_for_free(self) -> None:
        moon = await self._item("bubble.moon", unlock_level=4)
        both = await self._item("bubble.both", price=500, unlock_level=10)
        items = {item["sku"]: item["obtainable"] for item in (await self._overview("carol", level=3))["items"]}
        self.assertEqual(items, {"bubble.sixty": "buy", "bubble.moon": "locked", "bubble.both": "buy"})
        view = await self._overview("carol", level=10)
        self.assertEqual({item["sku"]: item["obtainable"] for item in view["items"]}, {"bubble.sixty": "buy", "bubble.moon": "level", "bubble.both": "level"})
        self.assertEqual(view["level"], 10)
        # 没绑定 CCW 也能领；不扣币，也不去问 CCW
        result = await self._buy("carol", moon, level=4)
        self.assertEqual((result["item"]["obtainable"], result["balance"]["spent"], result["balance"]["bound"]), ("owned", 0, False))
        result = await self._buy("alice", both, level=10)
        self.assertEqual((result["item"]["owned"], result["balance"]["spent"]), (True, 0))
        self.assertEqual([(row.price, row.source, row.ccw_oid) for row in await self._purchases()], [(0, "level", None), (0, "level", None)])
        self.assertEqual((await self._fails(self._buy("carol", moon, level=4)))[:2], (409, "already_owned"))
        self.assertEqual((self.ledger.calls, self.send_to_user.await_args_list), ([], []))
        # 等级不够、但标了价：照常花金币买
        self.assertEqual((await self._buy("alice", self.item, level=0))["balance"]["spent"], 60)
        # 还没合并等级功能时用户对象上没有 account_level：按 0 级
        self.assertEqual((shop_service.user_level(await self._user("dave")), (await self._overview("dave"))["level"]), (0, 0))

    async def test_on_level_up_grants_newly_unlocked_items(self) -> None:
        moon = await self._item("bubble.moon", unlock_level=4, name="月光", sort=2)
        await self._item("bubble.sun", unlock_level=16, name="日冕", sort=1)
        await self._item("bubble.off", unlock_level=2, is_active=False)
        await self._item("bubble.expired", unlock_level=2, available_until=self.now - timedelta(days=1))
        async with self.sessions() as db:
            self.assertEqual(await shop_service.on_level_up(db, self.ids["carol"], 3), [])
            granted = await shop_service.on_level_up(db, self.ids["carol"], 4)
            self.assertEqual(granted, [{"sku": "bubble.moon", "name": "月光", "category": "bubble"}])
            # 重复调用不会重复发
            self.assertEqual(await shop_service.on_level_up(db, self.ids["carol"], 4), [])
            self.assertEqual(await shop_service.on_level_up(db, self.ids["carol"], 5), [])
        self.assertEqual([(row.item_id, row.price, row.source) for row in await self._purchases("carol")], [(moon, 0, "level")])
        self.send_to_user.assert_awaited_once_with(
            self.ids["carol"], "shop.item.granted", {"item": {"sku": "bubble.moon", "name": "月光", "category": "bubble"}, "source": "level"}
        )
        self.assertEqual((await self._overview("carol", level=4))["items"][0]["owned"], False)
        # 一次跳了好几级：中间解锁的都补上，按解锁等级排
        self.send_to_user.reset_mock()
        async with self.sessions() as db:
            granted = await shop_service.on_level_up(db, self.ids["dave"], 20)
        self.assertEqual([item["sku"] for item in granted], ["bubble.moon", "bubble.sun"])
        self.assertEqual([call.args[2]["item"]["sku"] for call in self.send_to_user.await_args_list], ["bubble.moon", "bubble.sun"])

    async def test_on_level_up_skips_owned_and_revoked_and_survives_errors(self) -> None:
        moon = await self._item("bubble.moon", unlock_level=4)
        sun = await self._item("bubble.sun", unlock_level=16)
        await self._buy("carol", moon, level=4)
        await self._as("admin", shop_service.admin_grant, self.ids["carol"], sun, None)
        await self._as("admin", shop_service.admin_revoke, self.ids["carol"], sun, "收回")
        self.send_to_user.reset_mock()
        async with self.sessions() as db:
            # 已经有的不重复发；被后台收回过的不自动发回去
            self.assertEqual(await shop_service.on_level_up(db, self.ids["carol"], 16), [])
        self.assertEqual(self.send_to_user.await_args_list, [])
        # 但用户还是能在商店里手动领
        self.assertEqual((await self._buy("carol", sun, level=16))["item"]["owned"], True)
        # 出错不往外抛
        async with self.sessions() as db:
            with patch.object(shop_service, "_on_sale_clause", side_effect=RuntimeError("boom")):
                self.assertEqual(await shop_service.on_level_up(db, self.ids["dave"], 16), [])
        self.assertEqual(await self._purchases("dave"), [])


class ConcurrencyTests(ShopTestCase):
    """并发的请求各用各的会话和连接（见 ShopTestCase._make_engine），事务之间真的会抢写锁。"""

    async def test_two_purchases_cannot_overdraw(self) -> None:
        self.ledger.donate(OID_A, 100)
        for round_index in range(6):
            first = await self._item(f"bubble.first{round_index}", price=60)
            second = await self._item(f"bubble.second{round_index}", price=60)
            # 让余额每一轮都回到 100：把花掉的补到累计投币里
            self.ledger.donate(OID_A, 100 + 60 * round_index)
            self.tick(21)
            if round_index % 2:
                # 奇数轮先核实好，两个请求直接抢扣款；偶数轮连核实也一起并发
                await self._refresh()
            calls_before = len(self.ledger.calls)
            results = await asyncio.gather(self._buy("alice", first), self._buy("alice", second), return_exceptions=True)
            bought = [result for result in results if isinstance(result, dict)]
            failed = [result for result in results if not isinstance(result, dict)]
            with self.subTest(round=round_index):
                self.assertEqual((len(bought), len(failed)), (1, 1))
                self.assertEqual((failed[0].status_code, failed[0].detail["code"], failed[0].detail["balance"]), (402, "insufficient_coins", 40))
                account = await self._account("alice")
                self.assertEqual((account.spent, account.donated - account.spent), (60 * (round_index + 1), 40))
                self.assertEqual(len(await self._purchases("alice")), round_index + 1)
                if not round_index % 2:
                    # 两个请求同时要核实：只问了账本一次
                    self.assertEqual(len(self.ledger.calls) - calls_before, 1)

    async def test_simultaneous_refreshes_share_one_verification(self) -> None:
        self.ledger.donate(OID_A, 100)
        self.ledger.donate(OID_B, 7)
        self.ledger.delay = 0.3
        results = await asyncio.gather(self._refresh(), self._refresh(), self._refresh())
        # 同一个人同时点了几下：落在同一次核实上，谁也不会被限频挡回去，账本只被问了一次
        self.assertEqual([result["donated"] for result in results], [100, 100, 100])
        self.assertEqual(self.ledger.calls, [("credited_to", OID_A)])
        self.assertEqual((await self._fails(self._refresh()))[:2], (429, "refresh_too_fast"))
        # 不同的人同时刷新：各核实各的，互不限频
        results = await asyncio.gather(self._refresh("bob"), self._refresh("dave"))
        self.assertEqual([result["donated"] for result in results], [7, 0])
        self.assertEqual(sorted(self.ledger.calls[1:]), [("credited_to", OID_B), ("credited_to", OID_D)])

    async def test_same_item_twice_is_delivered_and_charged_once(self) -> None:
        self.ledger.donate(OID_A, 1000)
        for round_index in range(6):
            item = await self._item(f"bubble.dup{round_index}", price=30)
            results = await asyncio.gather(*(self._buy("alice", item) for _ in range(3)), return_exceptions=True)
            bought = [result for result in results if isinstance(result, dict)]
            failed = [result for result in results if not isinstance(result, dict)]
            with self.subTest(round=round_index):
                self.assertEqual(len(bought), 1)
                self.assertEqual([(error.status_code, error.detail["code"]) for error in failed], [(409, "already_owned")] * 2)
                async with self.sessions() as db:
                    rows = await db.scalar(select(func.count()).select_from(ShopPurchase).where(ShopPurchase.item_id == item))
                    spent = await db.scalar(select(func.sum(ShopPurchase.price)).where(ShopPurchase.user_id == self.ids["alice"]))
                self.assertEqual((rows, spent, (await self._account("alice")).spent), (1, 30 * (round_index + 1), 30 * (round_index + 1)))

    async def test_free_claim_races_with_on_level_up(self) -> None:
        moon = await self._item("bubble.moon", unlock_level=4)

        async def auto() -> Any:
            async with self.sessions() as db:
                return await shop_service.on_level_up(db, self.ids["carol"], 4)

        results = await asyncio.gather(self._buy("carol", moon, level=4), auto(), return_exceptions=True)
        self.assertEqual(len(await self._purchases("carol")), 1)
        for result in results:
            if isinstance(result, HTTPException):
                self.assertEqual(result.detail["code"], "already_owned")


# ---------------------------------------------------------------- 装备与 UserPublic.cosmetics


class EquipTests(ShopTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.item = await self._item("bubble.aurora", price=10)
        self.other = await self._item("bubble.plain", price=10, style=PLAIN)
        self.ledger.donate(OID_A, 100)
        await self._buy("alice", self.item)
        await self._buy("alice", self.other)
        self.entry = {"sku": "bubble.aurora", "style": validate_bubble_style(STYLE)}

    async def _public(self, name: str) -> dict[str, Any]:
        return UserPublic.model_validate(await self._user(name)).model_dump(mode="json")

    async def test_equip_switch_and_unequip(self) -> None:
        self.assertEqual((await self._public("alice"))["cosmetics"], {})
        self.assertEqual(await self._equip("alice", "bubble.aurora"), {"equipped": {"bubble": "bubble.aurora"}})
        self.assertEqual((await self._public("alice"))["cosmetics"], {"bubble": self.entry})
        view = await self._overview()
        self.assertEqual(view["equipped"], {"bubble": "bubble.aurora"})
        self.assertEqual({item["sku"]: item["equipped"] for item in view["items"]}, {"bubble.aurora": True, "bubble.plain": False})
        # 一个槽同时只能装一件：换一件就顶掉原来的
        self.assertEqual(await self._equip("alice", "bubble.plain"), {"equipped": {"bubble": "bubble.plain"}})
        self.assertEqual((await self._public("alice"))["cosmetics"], {"bubble": {"sku": "bubble.plain", "style": PLAIN}})
        self.assertEqual(await self._equip("alice", None), {"equipped": {}})
        self.assertEqual(((await self._public("alice"))["cosmetics"], (await self._user("alice")).cosmetics), ({}, None))
        self.assertEqual((await self._overview())["equipped"], {})
        # 本来就没装备：照常返回
        self.assertEqual(await self._equip("alice", None), {"equipped": {}})

    async def test_equip_errors(self) -> None:
        self.assertEqual((await self._fails(self._equip("bob", "bubble.aurora")))[:2], (403, "not_owned"))
        self.assertEqual((await self._fails(self._equip("alice", "bubble.nope")))[:2], (404, "item_not_found"))
        self.assertEqual((await self._fails(self._equip("alice", "bubble.aurora", slot="nameplate")))[:2], (422, "invalid_slot"))
        self.assertEqual((await self._fails(self._equip("alice", None, slot="nameplate")))[:2], (422, "invalid_slot"))
        # 槽是有的，但这件商品不是这个槽的
        self.assertEqual((await self._fails(self._equip("alice", "bubble.aurora", slot="frame")))[:2], (422, "invalid_slot"))
        self.assertEqual(((await self._user("bob")).cosmetics, (await self._user("alice")).cosmetics), (None, None))
        self.assertEqual(await self._cosmetics_events(), [])

    async def test_owned_items_stay_equippable_after_delisting(self) -> None:
        async with self.sessions() as db:
            (await db.get(ShopItem, self.item)).is_active = False
            await db.commit()
        self.assertEqual(await self._equip("alice", "bubble.aurora"), {"equipped": {"bubble": "bubble.aurora"}})
        # 我拥有的，下架了也还在列表里（is_active 为 false）；别人看不到它，所以别人看到的 is_active 全是 true
        mine = [(item["sku"], item["owned"], item["equipped"], item["is_active"]) for item in (await self._overview("alice"))["items"]]
        self.assertEqual(mine, [("bubble.aurora", True, True, False), ("bubble.plain", True, False, True)])
        self.assertEqual([(item["sku"], item["is_active"]) for item in (await self._overview("bob"))["items"]], [("bubble.plain", True)])
        # 过了限时的商品也一样留在拥有者的列表里；is_active 只表示上下架，过没过期看 available_until
        async with self.sessions() as db:
            (await db.get(ShopItem, self.other)).available_until = self.now - timedelta(days=1)
            await db.commit()
        self.assertEqual([(item["sku"], item["is_active"]) for item in (await self._overview("alice"))["items"]], [("bubble.aurora", False), ("bubble.plain", True)])
        self.assertEqual((await self._overview("bob"))["items"], [])

    async def test_equip_change_broadcasts_cosmetics(self) -> None:
        alice = self.ids["alice"]
        await self._equip("alice", "bubble.aurora")
        # 发给所有在线用户；cosmetics 和 UserPublic.cosmetics 同形
        self.assertEqual(await self._cosmetics_events(), [{"user_id": alice, "cosmetics": {"bubble": self.entry}}])
        self.assertEqual((await self._public("alice"))["cosmetics"], {"bubble": self.entry})
        self.assertEqual((self.send_to_users.await_args_list, self.send_to_user.await_args_list), ([], []))
        # 没变化就不发
        await self._equip("alice", "bubble.aurora")
        self.assertEqual(len(await self._cosmetics_events()), 1)
        await self._equip("alice", "bubble.plain")
        await self._equip("alice", None)
        events = await self._cosmetics_events()
        # 卸下后是 {}
        self.assertEqual(events[-1], {"user_id": alice, "cosmetics": {}})
        self.assertEqual(len(events), 3)
        # 推送失败不影响装备本身，只记日志
        self.broadcast.side_effect = RuntimeError("redis down")
        with self.assertLogs(shop_service.logger, level="ERROR"):
            self.assertEqual(await self._equip("alice", "bubble.plain"), {"equipped": {"bubble": "bubble.plain"}})
            await shop_service.drain()
        self.assertEqual((await self._public("alice"))["cosmetics"]["bubble"]["sku"], "bubble.plain")

    async def test_cosmetics_event_is_merged_per_user(self) -> None:
        """同一个人间隔内连着换：第一下立刻发，其余的并成一次，间隔到了发最后的状态。别人不受影响。"""
        self._settings(shop_cosmetics_event_interval_seconds=2)
        alice, bob = self.ids["alice"], self.ids["bob"]
        async with self.sessions() as db:
            db.add(ShopPurchase(user_id=bob, item_id=self.other, price=0, source="grant"))
            await db.commit()
        stamps: list[float] = []
        self.broadcast.side_effect = lambda *_args: stamps.append(time.monotonic())
        await self._equip("alice", "bubble.aurora")
        await asyncio.sleep(0.1)
        self.assertEqual(self._events("user.cosmetics.updated"), [{"user_id": alice, "cosmetics": {"bubble": self.entry}}])
        await self._equip("alice", "bubble.plain")
        await self._equip("alice", None)
        await self._equip("alice", "bubble.aurora")
        await self._equip("alice", "bubble.plain")
        # bob 是另一个人：他的第一下也是立刻发
        await self._equip("bob", "bubble.plain")
        await asyncio.sleep(0.1)
        plain = {"bubble": {"sku": "bubble.plain", "style": PLAIN}}
        self.assertEqual(self._events("user.cosmetics.updated")[1:], [{"user_id": bob, "cosmetics": plain}])
        events = await self._cosmetics_events()
        # alice 中间那三次没有单独发：间隔到了只发一次，是最后的状态
        self.assertEqual(events[2:], [{"user_id": alice, "cosmetics": plain}])
        self.assertGreaterEqual(stamps[2] - stamps[0], 1.9)
        # 间隔过去之后又是立刻发
        await asyncio.sleep(2.1)
        before = time.monotonic()
        await self._equip("alice", None)
        self.assertEqual((await self._cosmetics_events())[3:], [{"user_id": alice, "cosmetics": {}}])
        self.assertLess(stamps[3] - before, 1.0)

    async def test_user_public_parses_the_column_without_queries(self) -> None:
        await self._equip("alice", "bubble.aurora")
        async with self.sessions() as db:
            alice = await db.get(User, self.ids["alice"])
            bob = await db.get(User, self.ids["bob"])
            with self._statements() as seen:
                self.assertEqual(UserPublic.model_validate(alice).cosmetics, {"bubble": self.entry})
                self.assertEqual(UserPublic.model_validate(bob).cosmetics, {})
            self.assertEqual(seen, [])
        # 旧字段都还在
        public = await self._public("alice")
        self.assertTrue({"id", "username", "nickname", "avatar_url", "poke_action", "jury_badge", "created_at", "cosmetics"} <= set(public))
        for raw in (None, "", "not json", "[]", "null", '"text"', "123", '{"bubble": "aurora"}', '{"bubble": {"sku": 1, "style": {}}}', '{"bubble": {"sku": "x"}}'):
            with self.subTest(raw=raw):
                self.assertEqual(parse_cosmetics(raw), {})
                self.assertEqual(UserPublic(id=1, username="u", created_at=self.now, cosmetics=raw).cosmetics, {})
        mixed = '{"bubble": {"sku": "a", "style": {"version": 1}, "extra": 1}, "frame": "bad"}'
        self.assertEqual(parse_cosmetics(mixed), {"bubble": {"sku": "a", "style": {"version": 1}}})
        self.assertEqual(UserPublic(id=1, username="u", created_at=self.now).cosmetics, {})

    async def test_style_edit_follows_equipped_users(self) -> None:
        self.ledger.donate(OID_B, 50)
        await self._buy("bob", self.item)
        await self._equip("alice", "bubble.aurora")
        sent = len(await self._cosmetics_events())
        new_style = {**PLAIN, "background": "linear-gradient(90deg, #000 0%, #fff 100%)", "radius": 8, "pattern": {**PATTERN, "scale": 1.5}, "ornaments": [ORNAMENT]}
        updated = await self._as("admin", shop_service.admin_update_item, self.item, {"style": new_style})
        self.assertEqual(updated["style"], new_style)
        await shop_service.drain()
        # 装备着的人跟着变；只是拥有、没装备的人不受影响
        self.assertEqual((await self._public("alice"))["cosmetics"], {"bubble": {"sku": "bubble.aurora", "style": new_style}})
        self.assertIsNone((await self._user("bob")).cosmetics)
        # 只给装扮真的变了的人发事件
        events = (await self._cosmetics_events())[sent:]
        self.assertEqual(events, [{"user_id": self.ids["alice"], "cosmetics": {"bubble": {"sku": "bubble.aurora", "style": new_style}}}])
        self.assertEqual((await self._audit())[-1], ("shop.item.update", {"sku": "bubble.aurora", "changed": ["style"], "equipped_users": 1}))
        # 改别的字段、或者提交一模一样的样式：不动用户
        await self._as("admin", shop_service.admin_update_item, self.item, {"price": 99, "style": new_style})
        self.assertEqual(len(await self._cosmetics_events()), sent + 1)
        # 下架不影响已经装备的人
        await self._as("admin", shop_service.admin_update_item, self.item, {"is_active": False})
        self.assertEqual((await self._public("alice"))["cosmetics"]["bubble"]["sku"], "bubble.aurora")
        self.assertEqual(len(await self._cosmetics_events()), sent + 1)

    async def test_revoke_unequips(self) -> None:
        await self._equip("alice", "bubble.aurora")
        await self._as("admin", shop_service.admin_revoke, self.ids["alice"], self.item, "测试收回")
        self.assertEqual(((await self._public("alice"))["cosmetics"], (await self._overview())["equipped"]), ({}, {}))
        self.assertEqual((await self._cosmetics_events())[1:], [{"user_id": self.ids["alice"], "cosmetics": {}}])
        self.assertEqual((await self._fails(self._equip("alice", "bubble.aurora")))[:2], (403, "not_owned"))
        # 收回没装备着的那件：装扮没变，不发事件
        await self._equip("alice", "bubble.plain")
        sent = len(await self._cosmetics_events())
        async with self.sessions() as db:
            db.add(ShopPurchase(user_id=self.ids["alice"], item_id=self.item, price=0, source="grant"))
            await db.commit()
        await self._as("admin", shop_service.admin_revoke, self.ids["alice"], self.item, None)
        self.assertEqual(((await self._public("alice"))["cosmetics"]["bubble"]["sku"], len(await self._cosmetics_events())), ("bubble.plain", sent))


class FrameEquipTests(ShopTestCase):
    """头像框是第二个槽：买、装备、卸下、收回、改样式都和气泡走同一套代码，两个槽互不影响。"""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.bubble = await self._item("bubble.aurora", price=10)
        self.frame = await self._item("frame.ring", price=30, category="frame")
        self.other = await self._item("frame.plain", price=10, category="frame", style=PLAIN_FRAME)
        self.reward = await self._item("frame.reward", unlock_level=4, category="frame", style=PLAIN_FRAME)
        self.ledger.donate(OID_A, 100)
        self.bubble_entry = {"sku": "bubble.aurora", "style": validate_bubble_style(STYLE)}
        self.frame_entry = {"sku": "frame.ring", "style": FRAME}
        self.plain_entry = {"sku": "frame.plain", "style": PLAIN_FRAME}

    async def _public(self, name: str) -> dict[str, Any]:
        return UserPublic.model_validate(await self._user(name)).model_dump(mode="json")

    async def _equip(self, name: str, sku: str | None, slot: str = "bubble") -> dict[str, Any]:
        result = await super()._equip(name, sku, slot)
        # 等这一次的事件发出去再往下走，免得和下一次变化并成一条
        await shop_service.drain()
        return result

    async def test_buy_equip_and_unequip(self) -> None:
        alice = self.ids["alice"]
        view = await self._overview()
        self.assertEqual(view["categories"], CATEGORIES_VIEW)
        self.assertEqual({item["sku"]: (item["category"], item["obtainable"]) for item in view["items"] if item["category"] == "frame"}, {"frame.ring": ("frame", "buy"), "frame.plain": ("frame", "buy"), "frame.reward": ("frame", "locked")})
        # 没买不能装备
        self.assertEqual((await self._fails(self._equip("alice", "frame.ring", slot="frame")))[:2], (403, "not_owned"))
        bought = await self._buy("alice", self.frame)
        self.assertEqual((bought["item"]["sku"], bought["item"]["category"], bought["item"]["owned"], bought["item"]["style"]), ("frame.ring", "frame", True, FRAME))
        self.assertEqual((bought["balance"]["spent"], bought["balance"]["balance"], bought["equipped"]), (30, 70, {}))
        await self._buy("alice", self.other)
        await self._buy("alice", self.bubble)
        # 等级够了的头像框免费领，不扣币
        claimed = await self._buy("alice", self.reward, level=4)
        self.assertEqual((claimed["item"]["owned"], claimed["balance"]["spent"], [row.source for row in await self._purchases("alice")][-1]), (True, 50, "level"))
        # 头像框只能装在 frame 槽里，气泡只能装在 bubble 槽里
        self.assertEqual((await self._fails(self._equip("alice", "frame.ring", slot="bubble")))[:2], (422, "invalid_slot"))
        self.assertEqual((await self._fails(self._equip("alice", "bubble.aurora", slot="frame")))[:2], (422, "invalid_slot"))
        self.assertEqual(await self._cosmetics_events(), [])

        self.assertEqual(await self._equip("alice", "frame.ring", slot="frame"), {"equipped": {"frame": "frame.ring"}})
        self.assertEqual((await self._public("alice"))["cosmetics"], {"frame": self.frame_entry})
        # 两个槽各装各的
        self.assertEqual(await self._equip("alice", "bubble.aurora"), {"equipped": {"frame": "frame.ring", "bubble": "bubble.aurora"}})
        both = {"bubble": self.bubble_entry, "frame": self.frame_entry}
        self.assertEqual((await self._public("alice"))["cosmetics"], both)
        view = await self._overview()
        self.assertEqual(view["equipped"], {"bubble": "bubble.aurora", "frame": "frame.ring"})
        self.assertEqual(sorted(item["sku"] for item in view["items"] if item["equipped"]), ["bubble.aurora", "frame.ring"])
        # 同一个槽换一件：只顶掉这个槽里原来的那件
        self.assertEqual((await self._equip("alice", "frame.plain", slot="frame"))["equipped"], {"bubble": "bubble.aurora", "frame": "frame.plain"})
        self.assertEqual((await self._public("alice"))["cosmetics"], {"bubble": self.bubble_entry, "frame": self.plain_entry})
        # 卸下一个槽不动另一个
        self.assertEqual(await self._equip("alice", None, slot="frame"), {"equipped": {"bubble": "bubble.aurora"}})
        self.assertEqual((await self._public("alice"))["cosmetics"], {"bubble": self.bubble_entry})
        self.assertEqual(await self._equip("alice", None, slot="frame"), {"equipped": {"bubble": "bubble.aurora"}})
        await self._equip("alice", "frame.ring", slot="frame")
        self.assertEqual(await self._equip("alice", None), {"equipped": {"frame": "frame.ring"}})
        self.assertEqual(await self._equip("alice", None, slot="frame"), {"equipped": {}})
        self.assertEqual(((await self._public("alice"))["cosmetics"], (await self._user("alice")).cosmetics), ({}, None))
        # 每次真的变了都发一次 user.cosmetics.updated，带的是两个槽合在一起的最新状态
        self.assertEqual(
            [event["cosmetics"] for event in await self._cosmetics_events()],
            [
                {"frame": self.frame_entry},
                both,
                {"bubble": self.bubble_entry, "frame": self.plain_entry},
                {"bubble": self.bubble_entry},
                both,
                {"frame": self.frame_entry},
                {},
            ],
        )
        self.assertEqual({event["user_id"] for event in await self._cosmetics_events()}, {alice})

    async def test_user_public_carries_both_slots_without_queries(self) -> None:
        await self._buy("alice", self.frame)
        await self._buy("alice", self.bubble)
        await self._equip("alice", "bubble.aurora")
        await self._equip("alice", "frame.ring", slot="frame")
        async with self.sessions() as db:
            alice = await db.get(User, self.ids["alice"])
            with self._statements() as seen:
                self.assertEqual(UserPublic.model_validate(alice).cosmetics, {"bubble": self.bubble_entry, "frame": self.frame_entry})
            self.assertEqual(seen, [])
        self.assertEqual(parse_cosmetics(json.dumps({"frame": self.frame_entry})), {"frame": self.frame_entry})
        # 一个槽的内容坏了不连累另一个
        self.assertEqual(parse_cosmetics(json.dumps({"bubble": "bad", "frame": {**self.frame_entry, "extra": 1}})), {"frame": self.frame_entry})

    async def test_revoke_unequips_only_that_slot(self) -> None:
        alice = self.ids["alice"]
        await self._buy("alice", self.frame)
        await self._buy("alice", self.bubble)
        await self._equip("alice", "bubble.aurora")
        await self._equip("alice", "frame.ring", slot="frame")
        sent = len(await self._cosmetics_events())
        revoked = await self._as("admin", shop_service.admin_revoke, alice, self.frame, "测试收回", refund=True)
        self.assertEqual((revoked["item"], revoked["refunded"]), ({"sku": "frame.ring", "name": "frame.ring", "category": "frame"}, True))
        # 头像框卸下了，气泡还在；买头像框花的币退回来
        self.assertEqual((await self._public("alice"))["cosmetics"], {"bubble": self.bubble_entry})
        self.assertEqual(((await self._overview())["equipped"], (await self._account("alice")).spent), ({"bubble": "bubble.aurora"}, 10))
        self.assertEqual((await self._cosmetics_events())[sent:], [{"user_id": alice, "cosmetics": {"bubble": self.bubble_entry}}])
        self.assertEqual((await self._fails(self._equip("alice", "frame.ring", slot="frame")))[:2], (403, "not_owned"))
        # 收回之后可以再买回来、再装上
        await self._buy("alice", self.frame)
        self.assertEqual((await self._equip("alice", "frame.ring", slot="frame"))["equipped"], {"bubble": "bubble.aurora", "frame": "frame.ring"})
        # 收回拥有但没装备着的那件：装扮没变，不发事件
        await self._buy("alice", self.other)
        sent = len(await self._cosmetics_events())
        await self._as("admin", shop_service.admin_revoke, alice, self.other, None)
        self.assertEqual(((await self._overview())["equipped"], len(await self._cosmetics_events())), ({"bubble": "bubble.aurora", "frame": "frame.ring"}, sent))

    async def test_style_edit_follows_equipped_users(self) -> None:
        alice = self.ids["alice"]
        await self._buy("alice", self.frame)
        await self._buy("alice", self.bubble)
        await self._equip("alice", "bubble.aurora")
        await self._equip("alice", "frame.ring", slot="frame")
        sent = len(await self._cosmetics_events())
        new_style = {"version": 1, "ring": {"background": "conic-gradient(from 90deg, #000, #fff, #000)", "width": 4}, "ornaments": [FRAME_ORNAMENT]}
        updated = await self._as("admin", shop_service.admin_update_item, self.frame, {"style": new_style})
        self.assertEqual(updated["style"], new_style)
        expected = {"bubble": self.bubble_entry, "frame": {"sku": "frame.ring", "style": new_style}}
        self.assertEqual((await self._public("alice"))["cosmetics"], expected)
        self.assertEqual((await self._cosmetics_events())[sent:], [{"user_id": alice, "cosmetics": expected}])
        # 后台提交的样式按商品的类别校验：气泡的样式存不进头像框，反过来也一样
        status_code, code, detail = await self._fails(self._as("admin", shop_service.admin_update_item, self.frame, {"style": STYLE}))
        self.assertEqual((status_code, code, detail["field"]), (422, "invalid_style", "background"))
        status_code, code, detail = await self._fails(self._as("admin", shop_service.admin_update_item, self.bubble, {"style": FRAME}))
        self.assertEqual((status_code, code, detail["field"]), (422, "invalid_style", "ring"))
        status_code, code, detail = await self._fails(self._as("admin", shop_service.admin_update_item, self.frame, {"style": {**FRAME, "ring": {**RING, "background": "url(x)"}}}))
        self.assertEqual((status_code, code, detail["field"]), (422, "invalid_style", "ring.background"))
        # 只改类别不改样式：旧样式过不了新类别的校验，改不成
        self.assertEqual((await self._fails(self._as("admin", shop_service.admin_update_item, self.frame, {"category": "bubble"})))[:2], (422, "invalid_style"))
        # 类别和样式一起换成气泡：原来装在 frame 槽里的那份被拿掉
        await self._as("admin", shop_service.admin_update_item, self.frame, {"category": "bubble", "style": PLAIN})
        self.assertEqual((await self._public("alice"))["cosmetics"], {"bubble": self.bubble_entry})
        self.assertEqual((await self._cosmetics_events())[-1], {"user_id": alice, "cosmetics": {"bubble": self.bubble_entry}})

    async def test_admin_creates_and_grants_frames(self) -> None:
        carol = self.ids["carol"]
        created = await self._as("admin", shop_service.admin_create_item, {"sku": "frame.event", "category": "frame", "name": "活动限定", "style": FRAME, "tag": "limited"})
        self.assertEqual((created["category"], created["style"], created["price"]), ("frame", FRAME, 0))
        status_code, code, detail = await self._fails(self._as("admin", shop_service.admin_create_item, {"sku": "frame.bad", "category": "frame", "name": "坏的", "style": PLAIN}))
        self.assertEqual((status_code, code, detail["field"]), (422, "invalid_style", "background"))
        # 送出去的头像框本人会收到 shop.item.granted，没绑定 CCW 的人也能装
        await self._as("admin", shop_service.admin_grant, carol, created["id"], "送你")
        self.assertEqual(self.send_to_user.await_args_list[-1].args, (carol, "shop.item.granted", {"item": {"sku": "frame.event", "name": "活动限定", "category": "frame"}, "source": "grant"}))
        self.assertEqual(await self._equip("carol", "frame.event", slot="frame"), {"equipped": {"frame": "frame.event"}})
        self.assertEqual((await self._public("carol"))["cosmetics"], {"frame": {"sku": "frame.event", "style": FRAME}})
        # 升级时等级解锁的头像框和气泡一起发
        granted = await self._as("dave", lambda db, user: shop_service.on_level_up(db, user.id, 4))
        self.assertEqual(granted, [{"sku": "frame.reward", "name": "frame.reward", "category": "frame"}])


# ---------------------------------------------------------------- 初始商品


def _rgb(color: str) -> list[int]:
    digits = color.lstrip("#")
    if len(digits) == 3:
        digits = "".join(char * 2 for char in digits)
    return [int(digits[index : index + 2], 16) for index in (0, 2, 4)]


def _luminance(color: str) -> float:
    red, green, blue = (value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4 for value in (channel / 255 for channel in _rgb(color)))
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def _contrast(first: str, second: str) -> float:
    """WCAG 的对比度：正文要到 4.5 才算正常可读。"""
    high, low = sorted((_luminance(first), _luminance(second)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def _blend(base: str, overlay: str, opacity: float) -> str:
    """把 overlay 以 opacity 盖在 base 上之后的颜色（纹样压在底色上最深 / 最浅的地方）。"""
    return "#" + "".join(f"{round(under * (1 - opacity) + over * opacity):02X}" for under, over in zip(_rgb(base), _rgb(overlay)))


class SeedTests(ShopTestCase):
    def test_catalog_is_what_was_asked_for(self) -> None:
        self.assertEqual([item.get("category", "bubble") for item in shop_service.DEFAULT_ITEMS], ["bubble"] * 8 + ["frame"] * 11)
        self.assertEqual(len({item["sku"] for item in shop_service.DEFAULT_ITEMS}), 19)
        items = [item for item in shop_service.DEFAULT_ITEMS if item.get("category", "bubble") == "bubble"]
        by_sku = {item["sku"]: item for item in items}
        self.assertEqual((len(items), len(by_sku)), (8, 8))
        self.assertEqual(len({item["name"] for item in items}), 8)
        self.assertEqual(len({item["style"]["background"] for item in items}), 8)
        paid = [item for item in items if item["price"] > 0]
        self.assertEqual([item["price"] for item in paid], [10, 30, 60, 60, 100, 200])
        self.assertTrue(all(item.get("unlock_level") is None for item in paid))
        rewards = [item for item in items if item["price"] == 0]
        self.assertEqual([item["unlock_level"] for item in rewards], [4, 16])

        def extras(item: dict[str, Any]) -> tuple[bool, int, str | None]:
            style = item["style"]
            return "pattern" in style, len(style.get("ornaments", [])), style.get("animation")

        # 便宜的两件是干净的配色；中档的带纹样或角饰、没有动效；最贵的两件是渐变 + 纹样 + 两个角饰 + 动效
        self.assertEqual([extras(item) for item in paid[:2]], [(False, 0, None)] * 2)
        for item in paid[2:4]:
            has_pattern, ornaments, animation = extras(item)
            self.assertTrue((has_pattern or ornaments) and ornaments <= 1 and animation is None, item["sku"])
        self.assertEqual([extras(item)[:2] for item in paid[4:]], [(True, 2)] * 2)
        self.assertEqual({extras(item)[2] for item in paid[4:]}, {"flow", "shimmer"})
        self.assertTrue(all("gradient(" in item["style"]["background"] for item in paid[4:]))
        self.assertTrue(all(any(ornament.get("animated") for ornament in item["style"]["ornaments"]) for item in paid[4:]))
        # 等级奖励和等级图标对得上：4 级是月亮，16 级是太阳
        self.assertEqual([[ornament["key"] for ornament in item["style"]["ornaments"]][0] for item in rewards], ["moon", "sun"])
        self.assertTrue(all("pattern" in item["style"] and "等级奖励" in item["description"] for item in rewards))
        self.assertEqual(rewards[1]["style"]["animation"], "breathe")
        # 彼此要有区别：用到的纹样没有重样的（两件等级奖励之外），圆角、动效也不是一个模子
        patterns = [item["style"]["pattern"]["key"] for item in paid if "pattern" in item["style"]]
        self.assertEqual(len(patterns), len(set(patterns)))
        self.assertGreaterEqual(len({item["style"].get("radius") for item in items}), 4)
        self.assertEqual({item["style"].get("animation") for item in items}, {None, "flow", "shimmer", "breathe"})

        for item in items:
            with self.subTest(sku=item["sku"]):
                style = item["style"]
                # 存进去的就是规范形式
                self.assertEqual(validate_bubble_style(style), style)
                self.assertTrue(shop_service.SKU_RE.fullmatch(item["sku"]) and item["sku"].startswith("bubble."))
                self.assertTrue(0 < len(item["name"]) <= shop_service.NAME_MAX and 0 < len(item["description"]) <= shop_service.DESCRIPTION_MAX)
                self.assertTrue(0 < len(item["preview_text"]) <= shop_service.PREVIEW_TEXT_MAX)
                self.assertIn(item.get("tag"), (None, *shop_service.TAGS))
                # 每件都有深色覆盖；浅色有纹样的，深色也单独配了一份
                dark = style["dark"]
                self.assertTrue({"background", "text"} <= set(dark))
                self.assertEqual("pattern" in style, "pattern" in dark)
                for mode, surface in (("light", style), ("dark", dark)):
                    text, pattern = surface["text"], surface.get("pattern")
                    for stop in re.findall(r"#[0-9A-Fa-f]{3,8}", surface["background"]):
                        # 正文对底色每一段的对比度；有纹样的话，压在纹样上的那一点也要够
                        self.assertGreaterEqual(_contrast(text, stop), 4.5, (item["sku"], mode, text, stop))
                        if pattern is not None:
                            covered = _blend(stop, pattern["color"], pattern["opacity"])
                            self.assertGreaterEqual(_contrast(text, covered), 4.5, (item["sku"], mode, "pattern", text, covered))
                        # 链接色：深色覆盖里没单独写的沿用浅色的那个
                        link = surface.get("link", style["link"])
                        self.assertGreaterEqual(_contrast(link, stop), 4.3, (item["sku"], mode, "link", link, stop))
                light_is_bright = _luminance(re.findall(r"#[0-9A-Fa-f]{3,8}", style["background"])[0]) > 0.4
                dark_is_bright = _luminance(re.findall(r"#[0-9A-Fa-f]{3,8}", dark["background"])[0]) > 0.4
                self.assertFalse(dark_is_bright, "深色模式下不能是浅底")
                if light_is_bright:
                    # 浅底配深字，深底配浅字：不出现浅底白字。两种模式明暗相反，链接色也得各配各的
                    self.assertLess(_luminance(style["text"]), 0.2)
                    self.assertIn("link", dark)
                self.assertGreater(_luminance(dark["text"]), 0.6)
                for ornament in style.get("ornaments", []):
                    # 角饰没有深色覆盖，还会探出气泡：颜色要在浅色和深色聊天背景上都看得见
                    self.assertTrue(0.12 <= _luminance(ornament["color"]) <= 0.75, (item["sku"], ornament))

    def test_frame_catalog_is_what_was_asked_for(self) -> None:
        frames = [item for item in shop_service.DEFAULT_ITEMS if item.get("category") == "frame"]
        by_sku = {item["sku"]: item for item in frames}
        self.assertEqual(
            [(item["sku"], item["name"], item["price"], item.get("unlock_level")) for item in frames],
            [
                ("frame.mint", "薄荷", 10, None),
                ("frame.sakura", "樱花", 30, None),
                ("frame.ocean", "海盐", 60, None),
                ("frame.neko", "猫耳", 60, None),
                ("frame.galaxy", "星环", 100, None),
                ("frame.aurora", "极光", 200, None),
                ("frame.halo", "光环", 0, 4),
                ("frame.crown", "加冕", 0, 16),
                ("frame.bow", "蝴蝶结", 30, None),
                ("frame.devil", "小恶魔", 50, None),
                ("frame.angel", "天使", 120, None),
            ],
        )
        # 前 8 件和气泡成套：价位、等级奖励的档位一一对应
        bubbles = [item for item in shop_service.DEFAULT_ITEMS if item.get("category", "bubble") == "bubble"]
        self.assertEqual([(item["price"], item.get("unlock_level")) for item in frames[:8]], [(item["price"], item.get("unlock_level")) for item in bubbles])

        def keys(sku: str) -> list[tuple[str, str]]:
            return [(ornament["key"], ornament["position"]) for ornament in by_sku[sku]["style"].get("ornaments", [])]

        self.assertEqual(keys("frame.mint"), [("leaf", "br")])
        self.assertEqual(keys("frame.sakura"), [("petal", "tr"), ("petal", "bl")])
        self.assertEqual(keys("frame.ocean"), [("sparkle", "tr")])
        self.assertEqual(keys("frame.neko"), [("cat_ears", "top"), ("paw", "br")])
        self.assertEqual(keys("frame.galaxy"), [("star", "tr"), ("sparkle", "bl")])
        self.assertEqual(keys("frame.aurora"), [])
        self.assertEqual(keys("frame.halo"), [("halo", "top")])
        self.assertEqual(keys("frame.crown"), [("crown", "top"), ("laurel", "bottom")])
        self.assertEqual(keys("frame.bow"), [("bow", "tr"), ("heart", "bl")])
        self.assertEqual(keys("frame.devil"), [("horns", "top"), ("bolt", "br")])
        self.assertEqual(keys("frame.angel"), [("wings", "right"), ("halo", "top")])
        # 头像框专用的六种装饰每一种都有默认商品用到
        used = {key for sku in by_sku for key, _position in keys(sku)}
        self.assertTrue(set(FRAME_ORNAMENTS) - set(ORNAMENTS) <= used)
        # 星环和极光是会转的锥形渐变；凡是锥形渐变都首尾同色，转起来（或者静止时）没有接缝
        self.assertEqual({sku for sku in by_sku if by_sku[sku]["style"].get("animation") == "spin"}, {"frame.galaxy", "frame.aurora"})
        self.assertEqual({sku for sku in by_sku if "conic" in by_sku[sku]["style"]["ring"]["background"]}, {"frame.galaxy", "frame.aurora", "frame.crown"})
        # 彼此要有区别：环的配色没有重样的，四种动效都用上了，等级奖励写明了怎么得到
        self.assertEqual(len({item["style"]["ring"]["background"] for item in frames}), 11)
        self.assertEqual({item["style"].get("animation") for item in frames}, {None, "spin", "pulse", "shimmer"})
        self.assertEqual([item["sku"] for item in frames if "等级奖励" in item["description"]], ["frame.halo", "frame.crown"])

        # 客户端两种聊天背景的大致颜色。样式是在客户端截图看过的，这里只挡「整圈环都融进背景里」这种明显的失误
        light_page, dark_page = "#F4F5FA", "#1E1F27"
        for item in frames:
            with self.subTest(sku=item["sku"]):
                style = item["style"]
                # 存进去的就是规范形式
                self.assertEqual(validate_frame_style(style), style)
                self.assertTrue(shop_service.SKU_RE.fullmatch(item["sku"]) and item["sku"].startswith("frame."))
                self.assertTrue(0 < len(item["name"]) <= shop_service.NAME_MAX and 0 < len(item["description"]) <= shop_service.DESCRIPTION_MAX)
                self.assertIn(item.get("tag"), (None, *shop_service.TAGS))
                dark = style.get("dark", {})
                for ring in (style["ring"], dark.get("ring", style["ring"])):
                    stops = re.findall(r"#[0-9A-Fa-f]{6}", ring["background"])
                    if ring["background"].startswith("conic-gradient("):
                        self.assertEqual(stops[0], stops[-1], ring)
                # 浅色背景：环上最深的那一段要和背景分得开；整圈都很浅的（光环、天使）靠外发光把轮廓托出来
                light_stops = re.findall(r"#[0-9A-Fa-f]{6}", style["ring"]["background"])
                self.assertTrue(max(_contrast(stop, light_page) for stop in light_stops) >= 1.5 or "glow" in style, (item["sku"], light_stops))
                # 深色背景（有覆盖用覆盖）：环上最亮的那一段要够亮
                dark_stops = re.findall(r"#[0-9A-Fa-f]{6}", dark.get("ring", style["ring"])["background"])
                self.assertGreaterEqual(max(_contrast(stop, dark_page) for stop in dark_stops), 3.0, (item["sku"], dark_stops))

    async def test_seeding_is_idempotent_and_leaves_existing_items_alone(self) -> None:
        async with self.sessions() as db:
            self.assertEqual(await shop_service.ensure_default_items(db), 19)
            self.assertEqual(await shop_service.ensure_default_items(db), 0)
        listing = await self._as("admin", lambda db, _admin: shop_service.admin_list_items(db))
        self.assertEqual([item["sku"] for item in listing["items"]], [item["sku"] for item in shop_service.DEFAULT_ITEMS])
        self.assertEqual([item["sort"] for item in listing["items"]], [index * 10 for index in range(1, 20)])
        self.assertTrue(all(item["is_active"] for item in listing["items"]))
        self.assertEqual([item["category"] for item in listing["items"]], ["bubble"] * 8 + ["frame"] * 11)
        self.assertEqual([item["style"] for item in listing["items"]], [item["style"] for item in shop_service.DEFAULT_ITEMS])
        # 后台改过价、下架过、删掉一件之后重新启动：只补缺的那一件，其余不动
        async with self.sessions() as db:
            rows = {item.sku: item for item in (await db.execute(select(ShopItem))).scalars()}
            rows["bubble.mint"].price = 999
            rows["bubble.mint"].name = "改过的名字"
            rows["bubble.peach"].is_active = False
            await db.delete(rows["bubble.aurora"])
            await db.commit()
            self.assertEqual(await shop_service.ensure_default_items(db), 1)
            rows = {item.sku: item for item in (await db.execute(select(ShopItem))).scalars()}
        self.assertEqual((len(rows), rows["bubble.mint"].price, rows["bubble.mint"].name, rows["bubble.peach"].is_active), (19, 999, "改过的名字", False))
        self.assertEqual(rows["bubble.aurora"].price, 200)
        # 和线上的商店一样用：等级解锁的几件对 0 级的人是 locked
        view = await self._overview("carol")
        self.assertEqual(
            {item["sku"]: item["obtainable"] for item in view["items"] if item["price"] == 0},
            {"bubble.moonlight": "locked", "bubble.corona": "locked", "frame.halo": "locked", "frame.crown": "locked"},
        )

    async def test_frames_are_added_to_a_shop_that_already_has_the_bubbles(self) -> None:
        """线上的情况：气泡早就播种过、后台也改过，这次启动只补头像框，气泡和已有的头像框一概不动。"""
        frames = tuple(item for item in shop_service.DEFAULT_ITEMS if item.get("category") == "frame")
        bubbles = tuple(item for item in shop_service.DEFAULT_ITEMS if item.get("category", "bubble") == "bubble")
        edited = {"version": 1, "ring": {"background": "#123456", "width": 5}}
        async with self.sessions() as db:
            with patch.object(shop_service, "DEFAULT_ITEMS", bubbles):
                self.assertEqual(await shop_service.ensure_default_items(db), 8)
            mint = (await db.execute(select(ShopItem).where(ShopItem.sku == "bubble.mint"))).scalar_one()
            mint.price, mint.style, mint.sort = 7, json.dumps(PLAIN), 5
            # 后台自己先建了一件同名 sku 的头像框，样式、价格都和默认的不一样，还下架了
            db.add(ShopItem(sku="frame.mint", category="frame", name="后台建的", price=77, style=json.dumps(edited), is_active=False, sort=1))
            await db.commit()
            self.assertEqual(await shop_service.ensure_default_items(db), 10)
            self.assertEqual(await shop_service.ensure_default_items(db), 0)
            rows = {item.sku: item for item in (await db.execute(select(ShopItem))).scalars()}
        self.assertEqual(len(rows), 19)
        self.assertEqual((rows["bubble.mint"].price, json.loads(rows["bubble.mint"].style), rows["bubble.mint"].sort), (7, PLAIN, 5))
        kept = rows["frame.mint"]
        self.assertEqual((kept.name, kept.price, json.loads(kept.style), kept.is_active, kept.sort), ("后台建的", 77, edited, False, 1))
        for spec in frames[1:]:
            row = rows[spec["sku"]]
            self.assertEqual((row.category, row.name, row.price, row.unlock_level, json.loads(row.style), row.is_active), ("frame", spec["name"], spec["price"], spec.get("unlock_level"), spec["style"], True))
        self.assertEqual([rows[spec["sku"]].sort for spec in frames[1:]], [100, 110, 120, 130, 140, 150, 160, 170, 180, 190])
        # 默认的头像框买得了、装得上
        self.ledger.donate(OID_A, 100)
        await self._buy("alice", rows["frame.galaxy"].id)
        self.assertEqual(await self._equip("alice", "frame.galaxy", slot="frame"), {"equipped": {"frame": "frame.galaxy"}})
        self.assertEqual(parse_cosmetics((await self._user("alice")).cosmetics), {"frame": {"sku": "frame.galaxy", "style": frames[4]["style"]}})

    async def test_seeding_never_raises(self) -> None:
        async with self.sessions() as db:
            with patch.object(shop_service, "DEFAULT_ITEMS", ({"sku": "bubble.bad", "name": "坏的", "style": {"version": 9}},)):
                self.assertEqual(await shop_service.ensure_default_items(db), 0)
            self.assertEqual(await db.scalar(select(func.count()).select_from(ShopItem)), 0)


# ---------------------------------------------------------------- 后台


class AdminTests(ShopTestCase):
    async def _admin(self, function, *args: Any, **kwargs: Any):
        return await self._as("admin", function, *args, **kwargs)

    async def _create(self, **fields: Any) -> dict[str, Any]:
        return await self._admin(shop_service.admin_create_item, {"sku": "bubble.new", "category": "bubble", "name": "新的", "style": STYLE, **fields})

    async def test_create_item(self) -> None:
        until = self.now + timedelta(days=7)
        created = await self._create(description=" 一段描述 ", price=120, preview_text=" 你好 ", tag="limited", sort=5, available_until=until)
        self.assertEqual(
            {key: created[key] for key in ("sku", "category", "name", "description", "price", "unlock_level", "preview_text", "tag", "is_active", "sort", "sold", "owners")},
            {
                "sku": "bubble.new",
                "category": "bubble",
                "name": "新的",
                "description": "一段描述",
                "price": 120,
                "unlock_level": None,
                "preview_text": "你好",
                "tag": "limited",
                "is_active": True,
                "sort": 5,
                "sold": 0,
                "owners": 0,
            },
        )
        self.assertEqual((created["style"], created["available_until"], created["created_at"]), (validate_bubble_style(STYLE), until, self.now))
        self.assertEqual(await self._audit(), [("shop.item.create", {"sku": "bubble.new", "name": "新的", "price": 120})])
        # 新建的立刻能在商店里看到
        self.assertEqual([item["sku"] for item in (await self._overview("alice"))["items"]], ["bubble.new"])
        # 只填必填项
        minimal = await self._admin(shop_service.admin_create_item, {"sku": "bubble.min-1_a", "category": "bubble", "name": "最简", "style": PLAIN})
        self.assertEqual((minimal["price"], minimal["description"], minimal["tag"], minimal["is_active"], minimal["sort"]), (0, "", None, True, 0))

    async def test_create_rejects_bad_input(self) -> None:
        status_code, code, detail = await self._fails(self._create(style={**STYLE, "background": "url(javascript:alert(1))"}))
        self.assertEqual((status_code, code, detail["field"]), (422, "invalid_style", "background"))
        self.assertIn("background", detail["message"])
        self.assertEqual((await self._fails(self._create(style={**STYLE, "dark": {"border": {"color": "#fff", "width": 9}}})))[2]["field"], "dark.border.width")
        # 不认识的纹样 / 角饰名字：拒绝，并说明是哪个字段
        status_code, code, detail = await self._fails(self._create(style={**STYLE, "pattern": {**PATTERN, "key": "https://evil.example/p.png"}}))
        self.assertEqual((status_code, code, detail["field"]), (422, "invalid_style", "pattern.key"))
        self.assertIn("pattern.key", detail["message"])
        status_code, code, detail = await self._fails(self._create(style={**STYLE, "ornaments": [ORNAMENT, {**ORNAMENT, "corner": "bl", "key": "dragon"}]}))
        self.assertEqual((status_code, code, detail["field"]), (422, "invalid_style", "ornaments[1].key"))
        self.assertEqual((await self._fails(self._create(style={**STYLE, "dark": {"pattern": {**PATTERN, "opacity": 0.9}}})))[2]["field"], "dark.pattern.opacity")
        self.assertEqual((await self._fails(self._create(style=None)))[1:2], ("invalid_style",))
        cases = [
            ({"sku": "Bubble.New"}, "sku"),
            ({"sku": "bubble..x"}, "sku"),
            ({"sku": ".bubble"}, "sku"),
            ({"sku": "bubble x"}, "sku"),
            ({"sku": "x" * 65}, "sku"),
            ({"sku": None}, "sku"),
            ({"category": "nameplate"}, "category"),
            ({"category": None}, "category"),
            ({"name": "   "}, "name"),
            ({"name": "名" * 41}, "name"),
            ({"name": None}, "name"),
            ({"description": "字" * 201}, "description"),
            ({"price": -1}, "price"),
            ({"price": shop_service.PRICE_MAX + 1}, "price"),
            ({"unlock_level": 0}, "unlock_level"),
            ({"unlock_level": 257}, "unlock_level"),
            ({"preview_text": "字" * 81}, "preview_text"),
            ({"tag": "sale"}, "tag"),
            ({"sort": 10**9}, "sort"),
        ]
        for fields, field in cases:
            with self.subTest(fields=fields):
                status_code, code, detail = await self._fails(self._create(**fields))
                self.assertEqual((status_code, code, detail["field"]), (422, "invalid_item", field))
        await self._create()
        self.assertEqual((await self._fails(self._create()))[:2], (409, "sku_exists"))
        async with self.sessions() as db:
            self.assertEqual(await db.scalar(select(func.count()).select_from(ShopItem)), 1)

    async def test_update_item(self) -> None:
        created = await self._create(price=120, tag="new", unlock_level=8, preview_text="你好", description="描述")
        item_id = created["id"]
        self.tick(60)
        updated = await self._admin(shop_service.admin_update_item, item_id, {"price": 80, "name": " 改名 ", "sku": "bubble.new"})
        self.assertEqual((updated["price"], updated["name"], updated["tag"], updated["unlock_level"], updated["updated_at"]), (80, "改名", "new", 8, self.now))
        self.assertEqual((await self._audit())[-1], ("shop.item.update", {"sku": "bubble.new", "changed": ["name", "price"], "equipped_users": 0}))
        # 可空的字段传 null 是清空；不可空的字段传 null 等于没传
        cleared = await self._admin(
            shop_service.admin_update_item, item_id, {"tag": None, "unlock_level": None, "preview_text": None, "description": None, "available_until": None, "name": None, "price": None, "style": None}
        )
        self.assertEqual(
            (cleared["tag"], cleared["unlock_level"], cleared["preview_text"], cleared["description"], cleared["name"], cleared["price"], cleared["style"]),
            (None, None, None, "", "改名", 80, validate_bubble_style(STYLE)),
        )
        # 没有实际变化：不写审计
        audits = len(await self._audit())
        await self._admin(shop_service.admin_update_item, item_id, {"price": 80})
        self.assertEqual(len(await self._audit()), audits)
        # 下架后商店里就没有了
        await self._admin(shop_service.admin_update_item, item_id, {"is_active": False, "sort": -3})
        self.assertEqual((await self._overview("alice"))["items"], [])
        self.assertEqual((await self._fails(self._admin(shop_service.admin_update_item, item_id, {"sku": "bubble.other"})))[:2], (422, "sku_immutable"))
        self.assertEqual((await self._fails(self._admin(shop_service.admin_update_item, item_id, {"style": {"version": 1}})))[:2], (422, "invalid_style"))
        self.assertEqual((await self._fails(self._admin(shop_service.admin_update_item, item_id, {"price": -5})))[:2], (422, "invalid_item"))
        self.assertEqual((await self._fails(self._admin(shop_service.admin_update_item, item_id, {"category": "nameplate"})))[:2], (422, "invalid_item"))
        self.assertEqual((await self._fails(self._admin(shop_service.admin_update_item, 4040, {"price": 1})))[:2], (404, "item_not_found"))
        listing = await self._admin(lambda db, _admin: shop_service.admin_list_items(db))
        self.assertEqual([(item["sku"], item["price"], item["is_active"], item["sort"]) for item in listing["items"]], [("bubble.new", 80, False, -3)])

    async def test_list_items_counts_sales_and_owners(self) -> None:
        first = await self._item("bubble.first", price=10, sort=2)
        second = await self._item("bubble.second", price=10, sort=1, is_active=False)
        self.ledger.donate(OID_A, 100)
        self.ledger.donate(OID_B, 100)
        await self._buy("alice", first)
        await self._buy("bob", first)
        await self._admin(shop_service.admin_grant, self.ids["carol"], first, None)
        await self._admin(shop_service.admin_grant, self.ids["carol"], second, None)
        await self._admin(shop_service.admin_revoke, self.ids["bob"], first, None, True)
        listing = await self._admin(lambda db, _admin: shop_service.admin_list_items(db))
        self.assertEqual(listing["total"], 2)
        # 含下架的；卖出只算花钱买且没退款的，拥有人数含赠送
        self.assertEqual([(item["sku"], item["is_active"], item["sold"], item["owners"]) for item in listing["items"]], [("bubble.second", False, 0, 1), ("bubble.first", True, 1, 2)])

    async def test_grant(self) -> None:
        gift = await self._item("bubble.gift", name="礼物", is_active=False)
        result = await self._admin(shop_service.admin_grant, self.ids["carol"], gift, "  活动奖励  ")
        self.assertEqual(
            {key: result[key] for key in ("item", "item_id", "price", "source", "note", "actor_id", "revoked_at", "refunded")},
            {
                "item": {"sku": "bubble.gift", "name": "礼物", "category": "bubble"},
                "item_id": gift,
                "price": 0,
                "source": "grant",
                "note": "活动奖励",
                "actor_id": self.ids["admin"],
                "revoked_at": None,
                "refunded": False,
            },
        )
        self.assertEqual(result["user"]["username"], "carol")
        self.send_to_user.assert_awaited_once_with(
            self.ids["carol"], "shop.item.granted", {"item": {"sku": "bubble.gift", "name": "礼物", "category": "bubble"}, "source": "grant"}
        )
        self.assertEqual(await self._audit(), [("shop.grant", {"item_id": gift, "sku": "bubble.gift", "note": "活动奖励", "duration_days": None})])
        # 送的东西（哪怕已下架、不卖）出现在对方的商店里，能装备
        view = await self._overview("carol")
        self.assertEqual([(item["sku"], item["owned"], item["obtainable"]) for item in view["items"]], [("bubble.gift", True, "owned")])
        self.assertEqual(await self._equip("carol", "bubble.gift"), {"equipped": {"bubble": "bubble.gift"}})
        history = await self._as("carol", lambda db, user: shop_service.list_purchases(db, user.id, 50, 0))
        self.assertEqual([(row["source"], row["price"]) for row in history["items"]], [("grant", 0)])
        self.assertEqual((await self._fails(self._admin(shop_service.admin_grant, self.ids["carol"], gift, None)))[:2], (409, "already_owned"))
        self.assertEqual((await self._fails(self._admin(shop_service.admin_grant, 4040, gift, None)))[:2], (404, "user_not_found"))
        self.assertEqual((await self._fails(self._admin(shop_service.admin_grant, self.ids["carol"], 4040, None)))[:2], (404, "item_not_found"))
        self.assertEqual(self.send_to_user.await_count, 1)

    async def test_revoke_with_and_without_refund(self) -> None:
        kept = await self._item("bubble.kept", price=30)
        refunded = await self._item("bubble.refunded", price=50)
        self.ledger.donate(OID_A, 100)
        await self._buy("alice", kept)
        await self._buy("alice", refunded)
        # 默认不退币
        result = await self._admin(shop_service.admin_revoke, self.ids["alice"], kept, "违规使用")
        self.assertEqual((result["revoked_at"], result["revoked_by_id"], result["revoke_note"], result["refunded"]), (self.now, self.ids["admin"], "违规使用", False))
        self.assertEqual((await self._overview())["balance"]["spent"], 80)
        result = await self._admin(shop_service.admin_revoke, self.ids["alice"], refunded, None, True)
        self.assertEqual(result["refunded"], True)
        view = await self._overview()
        self.assertEqual((view["balance"]["spent"], view["balance"]["balance"], [item["owned"] for item in view["items"]]), (30, 70, [False, False]))
        self.assertEqual([action for action, _details in await self._audit()], ["shop.revoke", "shop.revoke"])
        self.assertEqual((await self._audit())[-1][1], {"item_id": refunded, "sku": "bubble.refunded", "note": None, "refunded": True, "price": 50})
        # 记录还在；收回之后可以再买
        history = await self._as("alice", lambda db, user: shop_service.list_purchases(db, user.id, 50, 0))
        self.assertEqual([(row["item"]["sku"], row["revoked_at"] is not None, row["refunded"]) for row in history["items"]], [("bubble.refunded", True, True), ("bubble.kept", True, False)])
        self.assertEqual((await self._buy("alice", refunded))["balance"]["balance"], 20)
        self.assertEqual(len(await self._purchases("alice")), 3)
        # 没有这件
        self.assertEqual((await self._fails(self._admin(shop_service.admin_revoke, self.ids["alice"], kept, None)))[:2], (404, "ownership_not_found"))
        self.assertEqual((await self._fails(self._admin(shop_service.admin_revoke, 4040, kept, None)))[:2], (404, "user_not_found"))
        self.assertEqual((await self._fails(self._admin(shop_service.admin_revoke, self.ids["alice"], 4040, None)))[:2], (404, "item_not_found"))
        # 赠送的东西 refund 也不会多退
        gift = await self._item("bubble.gift")
        await self._admin(shop_service.admin_grant, self.ids["alice"], gift, None)
        self.assertEqual((await self._admin(shop_service.admin_revoke, self.ids["alice"], gift, None, True))["refunded"], False)
        self.assertEqual((await self._account("alice")).spent, 80)

    async def test_list_purchases_filters(self) -> None:
        first = await self._item("bubble.first", price=10)
        second = await self._item("bubble.second", price=20)
        self.ledger.donate(OID_A, 100)
        self.ledger.donate(OID_B, 100)
        await self._buy("alice", first)
        self.tick(1)
        await self._buy("bob", first)
        self.tick(1)
        await self._buy("alice", second)
        self.tick(1)
        await self._admin(shop_service.admin_revoke, self.ids["bob"], first, "收回")

        async def listing(**filters: Any) -> dict[str, Any]:
            options = {"user_id": None, "item_id": None, "limit": 50, "offset": 0, **filters}
            return await self._admin(lambda db, _admin: shop_service.admin_list_purchases(db, **options))

        everything = await listing()
        self.assertEqual(everything["total"], 3)
        self.assertEqual([(row["user"]["username"], row["item"]["sku"], row["price"], row["revoke_note"]) for row in everything["items"]], [("alice", "bubble.second", 20, None), ("bob", "bubble.first", 10, "收回"), ("alice", "bubble.first", 10, None)])
        self.assertEqual([row["item"]["sku"] for row in (await listing(user_id=self.ids["alice"]))["items"]], ["bubble.second", "bubble.first"])
        self.assertEqual([row["user"]["username"] for row in (await listing(item_id=first))["items"]], ["bob", "alice"])
        self.assertEqual((await listing(user_id=self.ids["bob"], item_id=second))["items"], [])
        page = await listing(limit=1, offset=1)
        self.assertEqual((page["total"], [row["user"]["username"] for row in page["items"]]), (3, ["bob"]))

    async def test_accounts_and_reconcile(self) -> None:
        sixty = await self._item("bubble.sixty", price=60)
        self.ledger.entries = [(OID_A, 100.5), (OID_B, 55.9), (OID_X, 10.0)]
        result = await self._admin(shop_service.admin_reconcile)
        # 名单上 3 个投币人，对得上 alice 和 bob；dave 绑定了但没投过，不用建账户
        self.assertEqual(result, {"donors": 3, "matched": 2, "updated": 2, "mismatched": [], "red_packets": NO_PACKET_LEDGER, "gifts": NO_GIFT_LEDGER, "group_plus": NO_PLUS_LEDGER})
        # 对账要的是不走缓存的完整名单
        self.assertEqual(self.ledger.calls, [("all_credits", True)])
        self.assertEqual((await self._audit())[-1], ("shop.reconcile", {"donors": 3, "matched": 2, "updated": 2}))
        self.assertIsNone(await self._account("dave"))
        # 对账之后不用自己刷新就有余额
        self.assertEqual((await self._overview("bob"))["balance"], {"bound": True, "donated": 55, "spent": 0, "balance": 55, "verified_at": self.now, "stale": False, **NO_PACKETS})

        async def accounts(**filters: Any) -> dict[str, Any]:
            options = {"q": None, "mismatch": None, "limit": 50, "offset": 0, **filters}
            return await self._admin(lambda db, _admin: shop_service.admin_list_accounts(db, **options))

        listing = await accounts()
        self.assertEqual(listing["total"], 2)
        self.assertEqual([(row["user"]["username"], row["ccw_oid"], row["donated"], row["spent"], row["balance"], row["stale"], row["mismatch"]) for row in listing["items"]], [("bob", OID_B, 55, 0, 55, False, False), ("alice", OID_A, 100, 0, 100, False, False)])
        self.assertEqual(listing["items"][0]["verified_at"], self.now)
        self.assertEqual([row["user"]["username"] for row in (await accounts(q="ali"))["items"]], ["alice"])
        self.assertEqual([row["user"]["username"] for row in (await accounts(q=str(self.ids["bob"])))["items"]], ["bob"])
        self.assertEqual([row["user"]["username"] for row in (await accounts(q=OID_A[:10]))["items"]], ["alice"])
        self.assertEqual((await accounts(q="nobody"))["items"], [])
        self.assertEqual((await accounts(mismatch=True))["items"], [])
        self.assertEqual((await accounts(mismatch=False))["total"], 2)
        page = await accounts(limit=1, offset=1)
        self.assertEqual((page["total"], [row["user"]["username"] for row in page["items"]]), (2, ["alice"]))

        # 再对一次：没变化
        self.tick(30)
        await self._buy("alice", sixty)
        self.assertEqual((await self._admin(shop_service.admin_reconcile))["updated"], 0)
        # alice 换绑到一个只投了 10 个币的账号，bob 又投了一些，dave 第一次投币
        await self._rebind("alice", OID_X)
        self.ledger.donate(OID_B, 70)
        self.ledger.donate(OID_D, 8)
        self.tick(30)
        result = await self._admin(shop_service.admin_reconcile)
        self.assertEqual((result["donors"], result["matched"], result["updated"]), (4, 3, 3))
        self.assertEqual([(row["user"]["username"], row["donated"], row["spent"], row["balance"], row["mismatch"]) for row in result["mismatched"]], [("alice", 10, 60, -50, True)])
        self.assertEqual([row["user"]["username"] for row in (await accounts(mismatch=True))["items"]], ["alice"])
        self.assertEqual([row["user"]["username"] for row in (await accounts())["items"]], ["alice", "dave", "bob"])
        account = await self._account("alice")
        self.assertEqual((account.ccw_oid, account.donated, account.spent, account.verify_ok), (OID_X, 10, 60, True))
        self.assertEqual((await self._account("dave")).donated, 8)
        # 换绑后还没重新核实的：按 0 算，同样算「花的比投的多」
        await self._rebind("alice", OID_A)
        row = (await accounts(mismatch=True))["items"][0]
        self.assertEqual((row["ccw_oid"], row["verified_oid"], row["donated"], row["balance"], row["stale"], row["mismatch"]), (OID_A, OID_X, 0, -60, True, True))

    async def test_reconcile_failure_changes_nothing(self) -> None:
        self.ledger.donate(OID_A, 100)
        await self._refresh()
        before = await self._account("alice")
        self.ledger.donate(OID_A, 500)
        for problem in (OSError("down"), integrations.CoinsUnavailable("名单没取全")):
            self.ledger.error = problem
            status_code, code, _detail = await self._fails(self._admin(shop_service.admin_reconcile))
            self.assertEqual((status_code, code), (503, "verification_unavailable"))
        after = await self._account("alice")
        self.assertEqual((after.donated, after.verified_at, await self._audit()), (before.donated, before.verified_at, []))

    async def test_settings(self) -> None:
        self.assertEqual(
            shop_service.admin_settings(),
            {
                "configured": True,
                "creation_oid": CREATION,
                "creation_oid_source": "ccw_shop_creation_oid",
                "donate_url": TOPUP_URL,
                "refresh_interval_seconds": 20,
                "ranking_recent_limit": 200,
                "ranking_cache_seconds": 60,
                "changed_interval_seconds": 0,
                "cosmetics_event_interval_seconds": 0,
                "categories": CATEGORIES_VIEW,
                "tags": ["new", "hot", "limited"],
                "animations": ["none", "flow", "shimmer", "breathe"],
                "patterns": list(PATTERNS),
                "ornaments": list(ORNAMENTS),
                "frame_animations": ["none", "spin", "pulse", "shimmer"],
                "frame_ornaments": list(FRAME_ORNAMENTS),
                "frame_positions": list(FRAME_POSITIONS),
                "price_max": shop_service.PRICE_MAX,
                "unlock_level_max": 256,
                "duration_max": 3650,
            },
        )
        # creation_oid / creation_oid_source / donate_url 原样取自余额来源的说明
        self.ledger.source_setting = "ccw_bind_subject_oid"
        self.assertEqual(shop_service.admin_settings()["creation_oid_source"], "ccw_bind_subject_oid")
        self.ledger.source_id = None
        view = shop_service.admin_settings()
        self.assertEqual((view["configured"], view["creation_oid"], view["creation_oid_source"], view["donate_url"]), (False, None, None, None))


# ---------------------------------------------------------------- 实时事件


class EventTests(ShopTestCase):
    async def test_catalog_changes_broadcast_shop_changed(self) -> None:
        created = await self._as("admin", shop_service.admin_create_item, {"sku": "bubble.new", "category": "bubble", "name": "新的", "style": PLAIN})
        await shop_service.drain()
        self.broadcast.assert_awaited_once_with("shop.changed", {})
        await self._as("admin", shop_service.admin_update_item, created["id"], {"price": 5})
        await shop_service.drain()
        self.assertEqual(self.broadcast.await_count, 2)
        # 没有实际变化、赠送、收回都不算目录变化
        await self._as("admin", shop_service.admin_update_item, created["id"], {"price": 5})
        await self._as("admin", shop_service.admin_grant, self.ids["carol"], created["id"], None)
        await self._as("admin", shop_service.admin_revoke, self.ids["carol"], created["id"], None)
        await shop_service.drain()
        self.assertEqual(self.broadcast.await_count, 2)
        # 推送失败不影响后台操作
        self.broadcast.side_effect = RuntimeError("redis down")
        self.assertEqual((await self._as("admin", shop_service.admin_update_item, created["id"], {"price": 6}))["price"], 6)
        await shop_service.drain()

    async def test_shop_changed_is_throttled_per_process(self) -> None:
        # 间隔留得宽一点：这台机器上几次写库加起来可能要零点几秒，定时器的粒度也有十几毫秒
        self._settings(shop_changed_interval_seconds=2)
        item = await self._item("bubble.one", price=1)
        stamps: list[float] = []
        self.broadcast.side_effect = lambda *_args: stamps.append(time.monotonic())
        for price in range(2, 7):
            await self._as("admin", shop_service.admin_update_item, item, {"price": price})
            await asyncio.sleep(0.01)
        # 第一次立刻发；间隔内的其余四次改动并成一次，等间隔过了再补发
        self.assertEqual(self.broadcast.await_count, 1)
        await shop_service.drain()
        self.assertEqual(self.broadcast.await_args_list, [(("shop.changed", {}),)] * 2)
        self.assertGreaterEqual(stamps[1] - stamps[0], 1.9)
        # 间隔过去之后的下一次改动又是立刻发
        await asyncio.sleep(2.1)
        before = time.monotonic()
        await self._as("admin", shop_service.admin_update_item, item, {"price": 99})
        await shop_service.drain()
        self.assertEqual(self.broadcast.await_count, 3)
        self.assertLess(stamps[2] - before, 1.0)

    async def test_grant_events_do_not_break_on_push_failure(self) -> None:
        gift = await self._item("bubble.gift", unlock_level=4)
        self.send_to_user.side_effect = RuntimeError("redis down")
        await self._as("admin", shop_service.admin_grant, self.ids["carol"], gift, None)
        async with self.sessions() as db:
            self.assertEqual(len(await shop_service.on_level_up(db, self.ids["dave"], 4)), 1)
        self.assertEqual(self.send_to_user.await_count, 2)
        self.assertEqual(len(await self._purchases()), 2)


# ---------------------------------------------------------------- 走真实路由


class HttpShopTests(ShopTestCase):
    """路径都挂上了、GET /shop 的整体形状、错误体的形状、后台接口的权限。"""

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
        async with self.sessions() as db:
            await shop_service.ensure_default_items(db)

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        await super().asyncTearDown()

    async def _json(self, method: str, path: str, expected: int = 200, **kwargs: Any) -> Any:
        response = await self.client.request(method, f"/api/v1{path}", **kwargs)
        self.assertEqual(response.status_code, expected, response.text)
        return response.json()

    async def test_user_flow(self) -> None:
        self.ledger.donate(OID_A, 320)
        shop = await self._json("GET", "/shop")
        self.assertEqual(set(shop), {"categories", "items", "balance", "equipped", "donate_url", "level"})
        self.assertEqual(shop["categories"], CATEGORIES_VIEW)
        self.assertEqual(shop["balance"], {"bound": True, "donated": 0, "spent": 0, "balance": 0, "verified_at": None, "stale": False, **NO_PACKETS})
        self.assertEqual((shop["equipped"], shop["donate_url"], shop["level"]), ({}, TOPUP_URL, 0))
        self.assertEqual(len(shop["items"]), 19)
        item_keys = {"id", "sku", "category", "name", "description", "price", "unlock_level", "style", "preview_text", "tag", "available_until", "is_active", "owned", "equipped", "obtainable", "duration_days", "expires_at"}
        self.assertTrue(all(set(item) == item_keys and item["is_active"] is True for item in shop["items"]))
        aurora = next(item for item in shop["items"] if item["sku"] == "bubble.aurora")
        self.assertEqual((aurora["price"], aurora["owned"], aurora["equipped"], aurora["obtainable"], aurora["style"]["animation"]), (200, False, False, "buy", "flow"))
        self.assertEqual({item["obtainable"] for item in shop["items"] if item["unlock_level"]}, {"locked"})

        balance = await self._json("POST", "/shop/balance/refresh")
        self.assertEqual((balance["donated"], balance["balance"], balance["stale"]), (320, 320, False))
        self.assertTrue(balance["verified_at"].startswith("2026-10-12T08:00:00"))
        self.assertTrue(balance["verified_at"].endswith(("Z", "+00:00")))
        error = await self._json("POST", "/shop/balance/refresh", 429)
        self.assertEqual((error["detail"]["code"], error["detail"]["retry_after"]), ("refresh_too_fast", 20))

        bought = await self._json("POST", f"/shop/items/{aurora['id']}/purchase")
        self.assertEqual(set(bought), {"item", "balance", "equipped"})
        self.assertEqual((bought["item"]["owned"], bought["item"]["obtainable"], bought["balance"]["balance"], bought["equipped"]), (True, "owned", 120, {}))
        error = await self._json("POST", f"/shop/items/{aurora['id']}/purchase", 409)
        self.assertEqual(error["detail"]["code"], "already_owned")
        self.assertEqual((await self._json("POST", "/shop/items/987654/purchase", 404))["detail"]["code"], "item_not_found")
        galaxy = next(item for item in shop["items"] if item["sku"] == "bubble.galaxy")
        ocean = next(item for item in shop["items"] if item["sku"] == "bubble.ocean")
        await self._json("POST", f"/shop/items/{galaxy['id']}/purchase")
        error = await self._json("POST", f"/shop/items/{ocean['id']}/purchase", 402)
        self.assertEqual(
            {key: error["detail"][key] for key in ("code", "balance", "price", "shortfall", "donate_url")},
            {"code": "insufficient_coins", "balance": 20, "price": 60, "shortfall": 40, "donate_url": TOPUP_URL},
        )

        self.assertEqual(await self._json("PUT", "/shop/equipped", json={"slot": "bubble", "sku": "bubble.aurora"}), {"equipped": {"bubble": "bubble.aurora"}})
        self.assertEqual(await self._cosmetics_events(), [{"user_id": self.ids["alice"], "cosmetics": {"bubble": {"sku": "bubble.aurora", "style": aurora["style"]}}}])
        # 402 里的 balance 是数字（当前余额），不是整个余额对象
        self.assertIs(type(error["detail"]["balance"]), int)
        self.assertEqual((await self._json("PUT", "/shop/equipped", 403, json={"slot": "bubble", "sku": "bubble.ocean"}))["detail"]["code"], "not_owned")
        shop = await self._json("GET", "/shop")
        self.assertEqual((shop["equipped"], shop["balance"]["spent"]), ({"bubble": "bubble.aurora"}, 300))
        self.assertEqual([item["sku"] for item in shop["items"] if item["equipped"]], ["bubble.aurora"])
        # 别人通过用户资料看到我的皮肤
        self.acting = "bob"
        profile = await self._json("GET", f"/users/{self.ids['alice']}")
        self.assertEqual((profile["cosmetics"]["bubble"]["sku"], profile["cosmetics"]["bubble"]["style"]), ("bubble.aurora", aurora["style"]))
        self.assertEqual((await self._json("GET", f"/users/{self.ids['carol']}"))["cosmetics"], {})
        self.acting = "alice"
        self.assertEqual(await self._json("PUT", "/shop/equipped", json={"slot": "bubble", "sku": None}), {"equipped": {}})
        self.assertEqual(await self._json("PUT", "/shop/equipped", json={"slot": "bubble"}), {"equipped": {}})
        # 头像框走同一组接口，槽是 frame
        mint = next(item for item in shop["items"] if item["sku"] == "frame.mint")
        self.assertEqual((mint["category"], mint["price"], mint["preview_text"], set(mint["style"])), ("frame", 10, None, {"version", "ring", "ornaments", "dark"}))
        self.assertEqual((await self._json("PUT", "/shop/equipped", 403, json={"slot": "frame", "sku": "frame.mint"}))["detail"]["code"], "not_owned")
        bought = await self._json("POST", f"/shop/items/{mint['id']}/purchase")
        self.assertEqual((bought["item"]["owned"], bought["balance"]["balance"]), (True, 10))
        self.assertEqual((await self._json("PUT", "/shop/equipped", 422, json={"slot": "bubble", "sku": "frame.mint"}))["detail"]["code"], "invalid_slot")
        self.assertEqual(await self._json("PUT", "/shop/equipped", json={"slot": "frame", "sku": "frame.mint"}), {"equipped": {"frame": "frame.mint"}})
        self.assertEqual(await self._json("PUT", "/shop/equipped", json={"slot": "bubble", "sku": "bubble.aurora"}), {"equipped": {"frame": "frame.mint", "bubble": "bubble.aurora"}})
        self.acting = "bob"
        profile = await self._json("GET", f"/users/{self.ids['alice']}")
        self.assertEqual(profile["cosmetics"], {"bubble": {"sku": "bubble.aurora", "style": aurora["style"]}, "frame": {"sku": "frame.mint", "style": mint["style"]}})
        self.acting = "alice"
        self.assertEqual(await self._json("PUT", "/shop/equipped", json={"slot": "frame", "sku": None}), {"equipped": {"bubble": "bubble.aurora"}})
        self.assertEqual(await self._json("PUT", "/shop/equipped", json={"slot": "bubble", "sku": None}), {"equipped": {}})

        history = await self._json("GET", "/shop/purchases?limit=1&offset=0")
        self.assertEqual((history["total"], len(history["items"])), (3, 1))
        self.assertEqual(set(history["items"][0]), {"id", "item", "price", "source", "created_at", "revoked_at", "refunded", "expires_at", "duration_days", "from_user"})
        self.assertEqual((history["items"][0]["item"], history["items"][0]["price"], history["items"][0]["source"]), ({"sku": "frame.mint", "name": "薄荷", "category": "frame"}, 10, "purchase"))
        await self._json("GET", "/shop/purchases?limit=0", 422)

        self.acting = "carol"
        self.assertEqual((await self._json("POST", "/shop/balance/refresh", 409))["detail"]["code"], "ccw_not_bound")
        self.assertEqual((await self._json("POST", f"/shop/items/{ocean['id']}/purchase", 409))["detail"]["code"], "ccw_not_bound")
        moon = next(item for item in shop["items"] if item["sku"] == "bubble.moonlight")
        self.assertEqual((await self._json("POST", f"/shop/items/{moon['id']}/purchase", 409))["detail"]["code"], "item_locked")
        self.ledger.error = OSError("down")
        self.acting = "bob"
        self.assertEqual((await self._json("POST", f"/shop/items/{ocean['id']}/purchase", 503))["detail"]["code"], "verification_unavailable")
        self.assertTrue((await self._json("GET", "/shop"))["balance"]["stale"])

    async def test_admin_flow(self) -> None:
        admin_calls = [
            ("GET", "/admin/shop/items", None),
            ("POST", "/admin/shop/items", {"sku": "bubble.x", "category": "bubble", "name": "x", "style": PLAIN}),
            ("PUT", "/admin/shop/items/1", {"price": 1}),
            ("GET", "/admin/shop/purchases", None),
            ("GET", "/admin/shop/accounts", None),
            ("POST", "/admin/shop/reconcile", None),
            ("GET", "/admin/shop/settings", None),
            ("POST", f"/admin/shop/users/{self.ids['carol']}/grant", {"item_id": 1}),
            ("POST", f"/admin/shop/users/{self.ids['carol']}/revoke", {"item_id": 1}),
        ]
        # 普通用户一个都调不了
        for method, path, body in admin_calls:
            with self.subTest(path=path):
                await self._json(method, path, 403, json=body)
        async with self.sessions() as db:
            self.assertEqual(await db.scalar(select(func.count()).select_from(ShopItem)), 19)
            self.assertEqual(await db.scalar(select(func.count()).select_from(ShopPurchase)), 0)

        self.acting = "admin"
        listing = await self._json("GET", "/admin/shop/items")
        self.assertEqual((listing["total"], len(listing["items"])), (19, 19))
        self.assertTrue({"is_active", "sort", "sold", "owners", "created_at", "updated_at", "style", "sku"} <= set(listing["items"][0]))
        created = await self._json("POST", "/admin/shop/items", json={"sku": "bubble.event", "category": "bubble", "name": "活动限定", "style": STYLE, "price": 5, "tag": "limited"})
        self.assertEqual((created["sku"], created["price"], created["tag"], created["sold"]), ("bubble.event", 5, "limited", 0))
        error = await self._json("POST", "/admin/shop/items", 422, json={"sku": "bubble.bad", "category": "bubble", "name": "坏的", "style": {**PLAIN, "background": "url(x)"}})
        self.assertEqual((error["detail"]["code"], error["detail"]["field"]), ("invalid_style", "background"))
        updated = await self._json("PUT", f"/admin/shop/items/{created['id']}", json={"price": 8, "tag": None, "is_active": False})
        self.assertEqual((updated["price"], updated["tag"], updated["is_active"], updated["name"]), (8, None, False, "活动限定"))
        self.assertEqual((await self._json("PUT", f"/admin/shop/items/{created['id']}", 422, json={"style": {"version": 3}}))["detail"]["code"], "invalid_style")

        granted = await self._json("POST", f"/admin/shop/users/{self.ids['carol']}/grant", json={"item_id": created["id"], "note": "送你"})
        self.assertEqual((granted["source"], granted["note"], granted["user"]["username"], granted["item"]["sku"]), ("grant", "送你", "carol", "bubble.event"))
        self.assertEqual((await self._json("POST", f"/admin/shop/users/{self.ids['carol']}/grant", 409, json={"item_id": created["id"]}))["detail"]["code"], "already_owned")
        purchases = await self._json("GET", f"/admin/shop/purchases?user_id={self.ids['carol']}&item_id={created['id']}")
        self.assertEqual((purchases["total"], purchases["items"][0]["source"]), (1, "grant"))
        revoked = await self._json("POST", f"/admin/shop/users/{self.ids['carol']}/revoke", json={"item_id": created["id"], "note": "收回"})
        self.assertEqual((revoked["revoke_note"], revoked["refunded"], revoked["revoked_at"] is not None), ("收回", False, True))
        self.assertEqual((await self._json("POST", f"/admin/shop/users/{self.ids['carol']}/revoke", 404, json={"item_id": created["id"]}))["detail"]["code"], "ownership_not_found")

        self.ledger.donate(OID_A, 40)
        reconciled = await self._json("POST", "/admin/shop/reconcile")
        self.assertEqual(reconciled, {"donors": 1, "matched": 1, "updated": 1, "mismatched": [], "red_packets": NO_PACKET_LEDGER, "gifts": NO_GIFT_LEDGER, "group_plus": NO_PLUS_LEDGER})
        accounts = await self._json("GET", "/admin/shop/accounts?q=alice&mismatch=false&limit=10")
        self.assertEqual((accounts["total"], accounts["items"][0]["donated"], accounts["items"][0]["user"]["username"]), (1, 40, "alice"))
        self.assertEqual((await self._json("GET", "/admin/shop/accounts?mismatch=true"))["items"], [])
        config = await self._json("GET", "/admin/shop/settings")
        self.assertEqual((config["configured"], config["creation_oid"], config["refresh_interval_seconds"]), (True, CREATION, 20))
        self.assertEqual((config["categories"], config["frame_animations"], config["frame_ornaments"], config["frame_positions"]), (CATEGORIES_VIEW, list(FRAME_ANIMATIONS), list(FRAME_ORNAMENTS), list(FRAME_POSITIONS)))
        frame = await self._json("POST", "/admin/shop/items", json={"sku": "frame.event", "category": "frame", "name": "活动头像框", "style": FRAME, "price": 5})
        self.assertEqual((frame["category"], frame["style"]), ("frame", FRAME))
        error = await self._json("POST", "/admin/shop/items", 422, json={"sku": "frame.bad", "category": "frame", "name": "坏的", "style": {**FRAME, "ornaments": [{**FRAME_ORNAMENT, "key": "dragon"}]}})
        self.assertEqual((error["detail"]["code"], error["detail"]["field"]), ("invalid_style", "ornaments[0].key"))
        self.ledger.error = OSError("down")
        self.assertEqual((await self._json("POST", "/admin/shop/reconcile", 503))["detail"]["code"], "verification_unavailable")


if __name__ == "__main__":
    unittest.main()
