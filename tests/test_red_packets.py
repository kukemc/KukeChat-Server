"""红包（docs/red-packets.md）的测试。余额来源用假账本，不发任何外部请求。

并发分两层测：同一个进程里多个任务同时来（各用各的会话和连接），以及真的起几个进程对着同一个库文件同时抢 / 同时发 / 同时清扫
（线上是两个 worker 进程，进程之间只有 SQLite 自己的锁）。后者把本文件当脚本跑：python tests/test_red_packets.py worker '<json>'。
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
from sqlalchemy import event, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.models  # noqa: F401
from app import integrations
from app.core.config import settings
from app.core.database import Base, configure_sqlite_connection, get_db
from app.core.dependencies import get_current_user
from app.models.conversation import Conversation, ConversationMember
from app.models.enums import ConversationType, MemberRole, MessageType, PlatformRole
from app.models.message import Message
from app.models.red_packet import RedPacket, RedPacketShare
from app.models.report import AdminAuditLog
from app.models.shop import ShopItem, ShopPurchase
from app.models.system import SystemSetting
from app.models.user import User
from app.schemas.message import MessageCreate
from app.services import messages as message_service
from app.services import moderation as moderation_service
from app.services import red_packets as rp
from app.services import shop as shop_service
from app.websocket.manager import manager
from integration_fakes import FakeCoinLedger

REPO_ROOT = Path(__file__).resolve().parents[1]
OID_A, OID_B, OID_D = "a" * 24, "b" * 24, "d" * 24
START = datetime(2026, 10, 12, 8, 0, tzinfo=UTC)
EXTRAS = [f"u{index:02d}" for index in range(20)]
STYLE = {"version": 1, "background": "#112233", "text": "#ffffff"}


def make_engine(path: Path):
    """文件库，每个会话一条连接，WAL、抢不到写锁就等 —— 和线上一样。"""
    engine = create_async_engine(f"sqlite+aiosqlite:///{path.as_posix()}")
    event.listen(engine.sync_engine, "connect", configure_sqlite_connection)
    return engine


class RedPacketTestCase(unittest.IsolatedAsyncioTestCase):
    """alice / bob / dave 绑定了 CCW，carol 和 u00–u19 没绑定；eve 不在任何会话里。群主是 dave；alice 和 bob 之间有一个私聊。"""

    async def asyncSetUp(self) -> None:
        asyncio.get_running_loop().set_debug(False)
        shop_service.reset_runtime_state()
        rp.reset_runtime_state()
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "red-packets.db"
        self.engine = make_engine(self.db_path)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with self.sessions() as db:
            users = [User(username=name, hashed_password="x") for name in ("alice", "bob", "carol", "dave", "eve", "admin", *EXTRAS)]
            users[0].ccw_student_oid, users[1].ccw_student_oid, users[3].ccw_student_oid = OID_A, OID_B, OID_D
            users[5].platform_role = PlatformRole.ADMIN
            db.add_all(users)
            await db.flush()
            self.ids = {user.username: user.id for user in users}
            group = Conversation(type=ConversationType.GROUP, title="群")
            direct = Conversation(type=ConversationType.DIRECT)
            db.add_all([group, direct])
            await db.flush()
            self.group_id, self.direct_id = group.id, direct.id
            db.add(ConversationMember(conversation_id=group.id, user_id=self.ids["dave"], role=MemberRole.OWNER))
            for name in ("alice", "bob", "carol", *EXTRAS):
                db.add(ConversationMember(conversation_id=group.id, user_id=self.ids[name], role=MemberRole.MEMBER))
            for name in ("alice", "bob"):
                db.add(ConversationMember(conversation_id=direct.id, user_id=self.ids[name], role=MemberRole.MEMBER))
            db.add(SystemSetting(key="red_packet.enabled", value="1"))
            await db.commit()

        self.now = START
        self.ledger = FakeCoinLedger()
        integrations.set_coin_ledger(self.ledger)
        self.addCleanup(integrations.set_coin_ledger, None)
        self.send_to_user = AsyncMock()
        self.send_to_users = AsyncMock()

        async def everyone_online(user_ids: list[int]) -> set[int]:
            return set(user_ids)

        for patcher in (
            patch.object(shop_service, "_now", lambda: self.now),
            patch.object(rp, "_now", lambda: self.now),
            patch.object(shop_service, "AsyncSessionLocal", self.sessions),
            patch.object(rp, "AsyncSessionLocal", self.sessions),
            patch.object(rp, "UPDATE_EVENT_DELAY_SECONDS", 0.0),
            patch.object(manager, "send_to_user", self.send_to_user),
            patch.object(manager, "send_to_users", self.send_to_users),
            patch.object(manager, "broadcast", AsyncMock()),
            patch.object(manager, "online_user_ids_among", everyone_online),
            patch.object(settings, "shop_refresh_interval_seconds", 20),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    async def asyncTearDown(self) -> None:
        await shop_service.drain()
        await self.engine.dispose()

    # ------------------------------------------------------------ 工具

    def tick(self, seconds: float = 0, *, hours: float = 0) -> None:
        self.now = self.now + timedelta(seconds=seconds, hours=hours)

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
                text = ("1" if value else "0") if isinstance(value, bool) else str(value)
                item = await db.get(SystemSetting, f"red_packet.{name}")
                if item is None:
                    db.add(SystemSetting(key=f"red_packet.{name}", value=text))
                else:
                    item.value = text
            await db.commit()
        rp.reset_cache()

    def _payload(self, **overrides: Any) -> dict[str, Any]:
        return {"conversation_id": self.group_id, "kind": "lucky", "amount": 100, "count": 5, "greeting": None, **overrides}

    async def _send(self, name: str = "alice", **overrides: Any) -> dict[str, Any]:
        result, message = await self._as(name, rp.send_red_packet, self._payload(**overrides))
        return {**result, "message": message}

    async def _send_id(self, name: str = "alice", **overrides: Any) -> int:
        return (await self._send(name, **overrides))["red_packet"]["id"]

    async def _claim(self, name: str, packet_id: int) -> dict[str, Any]:
        return (await self._as(name, rp.claim, packet_id))[0]

    async def _claim_with_message(self, name: str, packet_id: int) -> tuple[dict[str, Any], Message | None]:
        return await self._as(name, rp.claim, packet_id)

    async def _get(self, name: str, packet_id: int) -> dict[str, Any]:
        return await self._as(name, rp.get_red_packet, packet_id)

    async def _balance(self, name: str) -> dict[str, Any]:
        async with self.sessions() as db:
            return await shop_service.balance_of(db, self.ids[name])

    async def _coins(self, name: str) -> int:
        return (await self._balance(name))["balance"]

    async def _packet(self, packet_id: int) -> RedPacket:
        async with self.sessions() as db:
            return await db.get(RedPacket, packet_id)

    async def _shares(self, packet_id: int) -> list[RedPacketShare]:
        async with self.sessions() as db:
            return list((await db.execute(select(RedPacketShare).where(RedPacketShare.packet_id == packet_id).order_by(RedPacketShare.seq))).scalars().all())

    async def _count(self, model: Any) -> int:
        async with self.sessions() as db:
            return int(await db.scalar(select(func.count()).select_from(model)) or 0)

    async def _ledger(self) -> dict[str, Any]:
        async with self.sessions() as db:
            return await rp.ledger_check(db)

    async def _sweep(self) -> dict[str, int]:
        async with self.sessions() as db:
            return await rp.sweep(db)

    async def _audit(self) -> list[tuple[str, dict[str, Any]]]:
        async with self.sessions() as db:
            rows = (await db.execute(select(AdminAuditLog).order_by(AdminAuditLog.id))).scalars().all()
            return [(row.action, json.loads(row.details or "{}")) for row in rows]

    async def _item(self, sku: str, price: int) -> int:
        async with self.sessions() as db:
            item = ShopItem(sku=sku, category="bubble", name=sku, price=price, style=json.dumps(STYLE))
            db.add(item)
            await db.commit()
            return item.id

    async def _events(self, event_type: str) -> list[tuple[Any, Any]]:
        """到现在为止发出去的某种事件：[(收件人, data)]。"""
        await shop_service.drain()
        calls = [*self.send_to_user.await_args_list, *self.send_to_users.await_args_list]
        return [(call.args[0], call.args[2]) for call in calls if call.args[1] == event_type]

    async def _assert_conserved(self) -> dict[str, Any]:
        ledger = await self._ledger()
        self.assertTrue(ledger["balanced"], ledger)
        self.assertEqual(ledger["total_sent"], ledger["total_claimed"] + ledger["total_refunded"] + ledger["outstanding"])
        return ledger


# ---------------------------------------------------------------- 金额分配


class SplitTests(unittest.TestCase):
    def test_lucky_split_properties(self) -> None:
        """性质：份数对、每份不少于下限、加起来严格等于总额。"""
        cases = [(total, count, low) for low in (1, 2, 7) for count in (1, 2, 3, 10, 100) for total in (low * count, low * count + 1, low * count * 2, low * count + 499)]
        for total, count, low in cases:
            for _ in range(40):
                amounts = rp.split_lucky(total, count, low)
                with self.subTest(total=total, count=count, low=low):
                    self.assertEqual(len(amounts), count)
                    self.assertEqual(sum(amounts), total)
                    self.assertGreaterEqual(min(amounts), low)
                    self.assertTrue(all(isinstance(value, int) for value in amounts))

    def test_lucky_split_is_random_and_fair(self) -> None:
        seen: set[tuple[int, ...]] = set()
        totals = [0] * 5
        rounds = 3000
        for _ in range(rounds):
            amounts = rp.split_lucky(100, 5, 1)
            seen.add(tuple(amounts))
            for index, value in enumerate(amounts):
                totals[index] += value
        self.assertGreater(len(seen), 500)
        # 打乱之后每个位置的期望都是人均 20
        for total in totals:
            self.assertAlmostEqual(total / rounds, 20, delta=2.0)

    def test_lucky_split_edges(self) -> None:
        self.assertEqual(rp.split_lucky(5, 5, 1), [1] * 5)
        self.assertEqual(rp.split_lucky(9, 1, 1), [9])
        self.assertEqual(rp.split_lucky(14, 2, 7), [7, 7])
        for total, count, low in ((4, 5, 1), (0, 1, 1), (5, 0, 1), (13, 2, 7), (5, 5, 0)):
            with self.assertRaises(ValueError):
                rp.split_lucky(total, count, low)

    def test_split_uses_the_system_random_source(self) -> None:
        with patch.object(rp.secrets, "randbelow", return_value=0) as randbelow:
            amounts = rp.split_lucky(100, 4, 1)
        self.assertEqual(randbelow.call_count, 3)
        self.assertEqual(sorted(amounts), [1, 1, 1, 97])

    def test_normal_split(self) -> None:
        self.assertEqual(rp.split("normal", 7, 4, 1), [7, 7, 7, 7])
        self.assertEqual(sum(rp.split("lucky", 30, 4, 2)), 30)


# ---------------------------------------------------------------- 发


class SendTests(RedPacketTestCase):
    async def test_send_charges_the_whole_amount_and_posts_a_message(self) -> None:
        self.ledger.donate(OID_A, 300)
        sent = await self._send(amount=100, count=5, greeting="  新年\n快乐  ")
        packet, message = sent["red_packet"], sent["message"]
        self.assertEqual(
            (packet["kind"], packet["greeting"], packet["total"], packet["count"], packet["claimed_count"], packet["claimed_amount"], packet["status"]),
            ("lucky", "新年 快乐", 100, 5, 0, 0, "active"),
        )
        self.assertEqual((packet["sender"].username, packet["conversation_id"], packet["message_id"], packet["my_claim"], packet["claims"]), ("alice", self.group_id, message.id, None, []))
        self.assertEqual((packet["created_at"], packet["expires_at"], packet["finished_at"]), (self.now, self.now + timedelta(hours=24), None))
        # 立刻扣整包
        self.assertEqual(sent["balance"]["balance"], 200)
        self.assertEqual((sent["balance"]["red_packet_sent"], sent["balance"]["red_packet_received"], sent["balance"]["red_packet_refunded"]), (100, 0, 0))
        # 会话里的那条消息：metadata 只有不会变的字段，正文是旧客户端看的兜底文字
        self.assertEqual(message.type, MessageType.RED_PACKET)
        self.assertEqual(message.content, "[红包] 新年 快乐（更新到新版才能领取）")
        self.assertEqual(json.loads(message.metadata_json), {"red_packet": {"id": packet["id"], "kind": "lucky", "greeting": "新年 快乐", "count": 5}})
        read = message_service.build_new_message_read(message, self.ids["alice"])
        self.assertEqual((read.type, read.metadata["red_packet"]["id"]), (MessageType.RED_PACKET, packet["id"]))
        # 每一份在创建时就分好了
        shares = await self._shares(packet["id"])
        self.assertEqual(([share.seq for share in shares], sum(share.amount for share in shares)), ([1, 2, 3, 4, 5], 100))
        self.assertTrue(all(share.user_id is None and share.amount >= 1 for share in shares))
        stored = await self._packet(packet["id"])
        self.assertEqual((stored.ccw_oid, stored.conversation_type, stored.refunded_amount), (OID_A, "group", 0))
        self.assertEqual([(to, data["reason"], data["change"], data["balance"]["balance"], data["red_packet_id"]) for to, data in await self._events("shop.balance.changed")], [(self.ids["alice"], "red_packet", "red_packet.sent", 200, packet["id"])])
        await self._assert_conserved()

    async def test_normal_amount_is_per_share_and_greeting_defaults(self) -> None:
        self.ledger.donate(OID_A, 300)
        sent = await self._send(kind="normal", amount=7, count=4, greeting="   ")
        self.assertEqual((sent["red_packet"]["total"], sent["red_packet"]["greeting"], sent["balance"]["balance"]), (28, "恭喜发财", 272))
        self.assertEqual([share.amount for share in await self._shares(sent["red_packet"]["id"])], [7, 7, 7, 7])
        self.assertEqual(sent["message"].content, "[红包] 恭喜发财（更新到新版才能领取）")

    async def test_validation_errors(self) -> None:
        self.ledger.donate(OID_A, 5000)
        await self._configure(min_each=2)
        cases = [
            ({"kind": "gold"}, "invalid_kind"),
            ({"kind": None}, "invalid_kind"),
            ({"count": 0}, "invalid_count"),
            ({"count": 101}, "invalid_count"),
            ({"count": "5"}, "invalid_count"),
            ({"count": True}, "invalid_count"),
            ({"count": 2.0}, "invalid_count"),
            ({"amount": 0}, "invalid_amount"),
            ({"amount": -5}, "invalid_amount"),
            ({"amount": 10.5}, "invalid_amount"),
            ({"amount": None}, "invalid_amount"),
            ({"amount": 501}, "invalid_amount"),
            # 拼手气：总额分不出每份至少 2
            ({"amount": 9, "count": 5}, "invalid_amount"),
            # 普通：单个金额低于下限；总额 = 单个 × 个数 超过上限
            ({"kind": "normal", "amount": 1, "count": 3}, "invalid_amount"),
            ({"kind": "normal", "amount": 101, "count": 5}, "invalid_amount"),
            ({"greeting": "字" * 31}, "greeting_rejected"),
            ({"greeting": 123}, "greeting_rejected"),
        ]
        for overrides, code in cases:
            with self.subTest(**overrides):
                self.assertEqual(await self._code(self._send(**overrides)), (422, code))
        self.assertEqual((await self._count(RedPacket), await self._count(Message), await self._coins("alice")), (0, 0, 0))
        # 边界上的都能发：正好 30 个字、正好到上限、正好每份 2
        await self._send(greeting="字" * 30, amount=10, count=5)
        await self._send(kind="normal", amount=100, count=5)
        await self._send(kind="normal", amount=2, count=1)
        self.assertEqual(await self._count(RedPacket), 3)

    async def test_direct_only_allows_one_normal_packet(self) -> None:
        self.ledger.donate(OID_A, 300)
        self.assertEqual(await self._code(self._send(conversation_id=self.direct_id, kind="lucky", amount=10, count=1)), (422, "invalid_kind"))
        self.assertEqual(await self._code(self._send(conversation_id=self.direct_id, kind="normal", amount=10, count=2)), (422, "invalid_count"))
        sent = await self._send(conversation_id=self.direct_id, kind="normal", amount=10, count=1)
        self.assertEqual((sent["red_packet"]["total"], (await self._packet(sent["red_packet"]["id"])).conversation_type), (10, "direct"))

    async def test_membership_mute_and_binding(self) -> None:
        self.ledger.donate(OID_A, 300)
        self.assertEqual(await self._code(self._as("eve", rp.send_red_packet, self._payload())), (403, "not_member"))
        self.assertEqual(await self._code(self._send(conversation_id=987654)), (403, "not_member"))
        self.assertEqual(await self._code(self._send("carol")), (403, "ccw_not_bound"))
        # 平台禁言
        async with self.sessions() as db:
            (await db.get(User, self.ids["alice"])).muted_until = datetime.now(UTC) + timedelta(hours=1)
            await db.commit()
        status_code, code, detail = await self._fails(self._send())
        self.assertEqual((status_code, code, bool(detail["muted_until"])), (403, "muted", True))
        async with self.sessions() as db:
            (await db.get(User, self.ids["alice"])).muted_until = None
            member = (await db.execute(select(ConversationMember).where(ConversationMember.conversation_id == self.group_id, ConversationMember.user_id == self.ids["alice"]))).scalar_one()
            member.muted = True
            await db.commit()
        # 群里被禁言：群里发不了，私聊照常
        self.assertEqual(await self._code(self._send()), (403, "muted"))
        await self._send(conversation_id=self.direct_id, kind="normal", amount=5, count=1)
        async with self.sessions() as db:
            await db.execute(update(ConversationMember).values(muted=False))
            (await db.get(Conversation, self.group_id)).all_muted = True
            await db.commit()
        self.assertEqual(await self._code(self._send()), (403, "muted"))
        # 全员禁言管不到群主
        self.ledger.donate(OID_D, 50)
        await self._send("dave", amount=10, count=2)
        self.assertEqual((await self._count(RedPacket), await self._coins("alice")), (2, 295))

    async def test_insufficient_balance_carries_the_numbers(self) -> None:
        self.ledger.donate(OID_A, 60)
        status_code, code, detail = await self._fails(self._send(amount=100))
        self.assertEqual((status_code, code), (402, "insufficient_balance"))
        self.assertEqual((detail["balance"], detail["total"], detail["shortfall"]), (60, 100, 40))
        self.assertEqual((await self._count(RedPacket), await self._count(RedPacketShare), await self._count(Message), await self._coins("alice")), (0, 0, 0, 60))
        # 从没投过币的人
        self.assertEqual(await self._code(self._send("bob", amount=5, count=1)), (402, "insufficient_balance"))

    async def test_verification_failure_charges_nothing(self) -> None:
        self.ledger.donate(OID_A, 300)
        self.ledger.error = RuntimeError("boom")
        self.assertEqual(await self._code(self._send()), (503, "verification_unavailable"))
        self.ledger.error = None
        integrations.set_coin_ledger(None)
        self.assertEqual(await self._code(self._send()), (503, "verification_unavailable"))
        self.assertEqual((await self._count(RedPacket), await self._count(Message)), (0, 0))

    async def test_daily_limit_follows_the_local_day(self) -> None:
        self.ledger.donate(OID_A, 300)
        await self._configure(daily_send_limit=2)
        first = await self._send_id(amount=5, count=1)
        self.assertEqual((await self._as("alice", rp.config_view))["sent_today"], 1)
        # 被撤销的也算一次
        await self._as("admin", rp.admin_revoke, first, "测试")
        await self._send(amount=5, count=1)
        status_code, code, detail = await self._fails(self._send(amount=5, count=1))
        self.assertEqual((status_code, code, detail["daily_send_limit"]), (429, "daily_limit", 2))
        # 别人不受影响
        self.ledger.donate(OID_B, 50)
        await self._send("bob", amount=5, count=1)
        # START 是北京时间 16:00：过 7 小时 59 分还是同一天，再过两分钟就是第二天了
        self.tick(hours=7, seconds=59 * 60)
        self.assertEqual(await self._code(self._send(amount=5, count=1)), (429, "daily_limit"))
        self.tick(120)
        await self._send(amount=5, count=1)
        self.assertEqual((await self._as("alice", rp.config_view))["sent_today"], 1)

    async def test_greeting_goes_through_text_moderation(self) -> None:
        self.ledger.donate(OID_A, 300)
        seen: list[str] = []

        async def moderate(text: str, _user_id: int, context_id: str | None = None) -> dict[str, Any]:
            seen.append(text)
            return {"decision": "block" if "坏" in text else "allow"}

        with patch.object(settings, "content_moderation_enabled", True), patch.object(moderation_service, "_call_text_moderation_for_user", moderate):
            self.assertEqual(await self._code(self._send(greeting="坏话")), (422, "greeting_rejected"))
            await self._send(greeting="好话", amount=5, count=1)
            # 默认祝福语不送审
            await self._send(amount=5, count=1)
            with patch.object(moderation_service, "_call_text_moderation_for_user", AsyncMock(side_effect=RuntimeError("down"))):
                # 审核服务不可用时放行（消息发出去之后还有异步审核）
                await self._send(greeting="坏话", amount=5, count=1)
        self.assertEqual((seen, await self._count(RedPacket)), (["坏话", "好话"], 3))
        # 异步审核看的是祝福语
        async with self.sessions() as db:
            message = (await db.execute(select(Message).order_by(Message.id).limit(1))).scalar_one()
            self.assertEqual(moderation_service._text_to_moderate(message, json.loads(message.metadata_json)), "好话")

    async def test_red_packet_messages_cannot_be_sent_through_the_message_endpoint(self) -> None:
        with self.assertRaises(ValueError):
            MessageCreate(type="red_packet", content="[红包] 假的")
        async with self.sessions() as db:
            with self.assertRaises(HTTPException) as caught:
                await message_service.send_message(db, self.group_id, await db.get(User, self.ids["alice"]), "[红包] 假的", MessageType.RED_PACKET, {"red_packet": {"id": 1}})
        self.assertEqual(caught.exception.status_code, 422)
        self.assertEqual(await self._count(Message), 0)

    async def test_red_packets_cannot_be_forwarded(self) -> None:
        """红包消息不能转发：单独转、夹在合并转发里、把 metadata 抄进别的消息里，都不行。"""
        self.ledger.donate(OID_A, 300)
        packet = (await self._send(amount=30, count=3))["red_packet"]
        async with self.sessions() as db:
            alice = await db.get(User, self.ids["alice"])
            plain = await message_service.send_message(db, self.group_id, alice, "普通消息")
            plain_id = plain.id
            before = await self._count(Message)
            for message_ids in ([packet["message_id"]], [plain_id, packet["message_id"]]):
                with self.assertRaises(HTTPException) as caught:
                    await message_service.create_forward_bundle(db, self.direct_id, alice, self.group_id, message_ids)
                self.assertEqual((caught.exception.status_code, caught.exception.detail["code"]), (422, "red_packet_not_forwardable"))
            with self.assertRaises(HTTPException) as caught:
                await message_service.send_message(db, self.direct_id, alice, "[红包] 恭喜发财", MessageType.TEXT, {"red_packet": {"id": packet["id"], "kind": "lucky", "greeting": "恭喜发财", "count": 3}})
            self.assertEqual((caught.exception.status_code, caught.exception.detail["code"]), (422, "red_packet_not_forwardable"))
            # 不带红包的照常能转；引用红包消息（metadata 里只是一个 reply 引用）也照常
            await message_service.create_forward_bundle(db, self.direct_id, alice, self.group_id, [plain_id])
            await message_service.send_message(db, self.group_id, alice, "谢谢老板", MessageType.TEXT, {"reply_to": {"message_id": packet["message_id"]}})
        self.assertEqual(await self._count(Message), before + 2)

    async def test_a_rejected_message_rolls_the_charge_back(self) -> None:
        """发消息那一步被拒（这里是群的发言限频）：红包、每一份、扣款一起回滚。"""
        self.ledger.donate(OID_A, 300)
        async with self.sessions() as db:
            (await db.get(Conversation, self.group_id)).message_rate_limit_per_minute = 1
            await db.commit()
        await self._send(amount=10, count=2)
        self.assertEqual(await self._code(self._send(amount=10, count=2)), (429, "rate_limited"))
        self.assertEqual((await self._count(RedPacket), await self._count(RedPacketShare), await self._count(Message), await self._coins("alice")), (1, 2, 1, 290))
        await self._assert_conserved()


# ---------------------------------------------------------------- 抢


class ClaimTests(RedPacketTestCase):
    async def test_claim_until_finished(self) -> None:
        self.ledger.donate(OID_A, 300)
        packet_id = await self._send_id(amount=30, count=3)
        amounts = [share.amount for share in await self._shares(packet_id)]
        first = await self._claim("bob", packet_id)
        self.assertEqual((first["amount"], first["balance"]["balance"], first["balance"]["red_packet_received"]), (amounts[0], amounts[0], amounts[0]))
        self.assertEqual((first["red_packet"]["status"], first["red_packet"]["claimed_count"], first["red_packet"]["my_claim"]), ("active", 1, {"amount": amounts[0], "created_at": self.now}))
        self.assertEqual(await self._code(self._claim("bob", packet_id)), (409, "already_claimed"))
        # 没绑定 CCW 的人也能领
        second = await self._claim("carol", packet_id)
        self.assertEqual((second["amount"], second["balance"]["bound"], second["balance"]["balance"]), (amounts[1], False, amounts[1]))
        # 群里发的人自己也能抢
        self.tick(5)
        third = await self._claim("alice", packet_id)
        self.assertEqual((third["amount"], third["balance"]["balance"]), (amounts[2], 270 + amounts[2]))
        self.assertEqual((third["red_packet"]["status"], third["red_packet"]["claimed_count"], third["red_packet"]["claimed_amount"], third["red_packet"]["finished_at"]), ("finished", 3, 30, self.now))
        self.assertEqual(await self._code(self._claim("dave", packet_id)), (410, "finished"))
        # 抢完之后已经领过的人再点：仍然是「领过了」
        self.assertEqual(await self._code(self._claim("bob", packet_id)), (409, "already_claimed"))
        stored = await self._packet(packet_id)
        self.assertEqual((stored.status, stored.claimed_count, stored.claimed_amount, stored.refunded_amount), ("finished", 3, 30, 0))
        ledger = await self._assert_conserved()
        self.assertEqual((ledger["total_sent"], ledger["total_claimed"], ledger["total_refunded"], ledger["outstanding"]), (30, 30, 0, 0))

    async def test_who_may_claim(self) -> None:
        self.ledger.donate(OID_A, 300)
        group_packet = await self._send_id(amount=30, count=3)
        direct_packet = await self._send_id(conversation_id=self.direct_id, kind="normal", amount=10, count=1)
        self.assertEqual(await self._code(self._claim("eve", group_packet)), (403, "not_member"))
        # carol 在群里，但不在那个私聊里
        self.assertEqual(await self._code(self._claim("carol", direct_packet)), (403, "not_member"))
        self.assertEqual(await self._code(self._claim("alice", direct_packet)), (403, "own_packet"))
        self.assertEqual(await self._code(self._claim("bob", 424242)), (404, "not_found"))
        async with self.sessions() as db:
            (await db.get(User, self.ids["bob"])).muted_until = datetime.now(UTC) + timedelta(hours=1)
            await db.commit()
        self.assertEqual(await self._code(self._claim("bob", direct_packet)), (403, "muted"))
        async with self.sessions() as db:
            (await db.get(User, self.ids["bob"])).muted_until = None
            # 群内禁言不影响领
            await db.execute(update(ConversationMember).where(ConversationMember.user_id == self.ids["carol"]).values(muted=True))
            await db.commit()
        self.assertEqual((await self._claim("bob", direct_packet))["amount"], 10)
        self.assertEqual((await self._packet(direct_packet)).status, "finished")
        self.assertGreaterEqual((await self._claim("carol", group_packet))["amount"], 1)

    async def test_pending_in_conversation_lists_what_i_can_still_claim(self) -> None:
        self.ledger.donate(OID_A, 300)
        first = await self._send_id(amount=10, count=1)
        self.tick(1)
        second = await self._send_id(amount=20, count=2)
        direct = await self._send_id(conversation_id=self.direct_id, kind="normal", amount=10, count=1)

        async def pending(name: str, conversation_id: int) -> list[int]:
            return [packet.id for packet in await self._as(name, lambda db, user: rp.pending_in_conversation(db, user.id, conversation_id))]

        # 新的在前；发的人在群里也算（自己还没抢），私聊里自己发的不算
        self.assertEqual(await pending("bob", self.group_id), [second, first])
        self.assertEqual(await pending("alice", self.group_id), [second, first])
        self.assertEqual(await pending("alice", self.direct_id), [])
        self.assertEqual(await pending("bob", self.direct_id), [direct])
        await self._claim("bob", second)
        self.assertEqual(await pending("bob", self.group_id), [first])
        # 被别人抢完的不再出现
        await self._claim("carol", first)
        self.assertEqual(await pending("bob", self.group_id), [])
        self.assertEqual(await self._code(self._as("eve", lambda db, user: rp.pending_in_conversation(db, user.id, self.group_id))), (403, "not_member"))

    async def test_turning_the_switch_off_only_blocks_new_packets(self) -> None:
        self.ledger.donate(OID_A, 300)
        packet_id = await self._send_id(amount=30, count=3)
        await self._configure(enabled=False)
        self.assertEqual(await self._code(self._send()), (403, "disabled"))
        self.assertEqual(await self._code(self._as("eve", rp.send_red_packet, self._payload())), (403, "disabled"))
        config = await self._as("alice", rp.config_view)
        self.assertEqual((config["enabled"], config["sent_today"]), (False, 1))
        # 已经发出去的照常能看、能领、会过期退回
        self.assertGreaterEqual((await self._claim("bob", packet_id))["amount"], 1)
        self.assertEqual((await self._get("carol", packet_id))["claimed_count"], 1)
        self.tick(hours=25)
        self.assertEqual(await self._sweep(), {"expired": 1, "revoked": 0})
        self.assertEqual((await self._packet(packet_id)).status, "expired")
        self.assertEqual(await self._coins("alice") + await self._coins("bob"), 300)

    async def test_default_is_off_with_the_contract_limits(self) -> None:
        async with self.sessions() as db:
            await db.execute(SystemSetting.__table__.delete())
            await db.commit()
        rp.reset_cache()
        config = await self._as("alice", rp.config_view)
        self.assertEqual(config, {"enabled": False, "limits": {"max_total": 500, "max_count": 100, "min_each": 1, "daily_send_limit": 20, "expire_hours": 24}, "sent_today": 0, "features": ["chain", "exclusive"]})
        self.assertEqual(await self._code(self._send()), (403, "disabled"))


# ---------------------------------------------------------------- 余额公式


class BalanceTests(RedPacketTestCase):
    async def test_every_term_of_the_formula(self) -> None:
        """余额 = 累计投币 − 商店花掉的 − 发出的 + 抢到的 + 退回的。"""
        self.ledger.donate(OID_A, 300)
        self.ledger.donate(OID_B, 40)
        item = await self._item("bubble.sixty", 60)
        await self._as("alice", shop_service.purchase, item)
        self.assertEqual(await self._coins("alice"), 240)
        await self._as("bob", shop_service.refresh_balance)
        # 发出：立刻扣整包
        packet_id = await self._send_id(kind="normal", amount=25, count=4)
        self.assertEqual(await self._coins("alice"), 140)
        # 抢到：bob 和没绑定的 carol 各 25
        await self._claim("bob", packet_id)
        await self._claim("carol", packet_id)
        self.assertEqual((await self._coins("bob"), await self._coins("carol")), (65, 25))
        # bob 自己再发一个，alice 抢到
        bob_packet = await self._send_id("bob", kind="normal", amount=30, count=2)
        await self._claim("alice", bob_packet)
        self.assertEqual((await self._coins("alice"), await self._coins("bob")), (170, 5))
        # 退回：到期，没领的两份回到 alice，bob 没领的一份回到 bob
        self.tick(hours=24, seconds=1)
        self.assertEqual(await self._sweep(), {"expired": 2, "revoked": 0})
        alice, bob, carol = await self._balance("alice"), await self._balance("bob"), await self._balance("carol")
        self.assertEqual((alice["donated"], alice["spent"], alice["red_packet_sent"], alice["red_packet_received"], alice["red_packet_refunded"], alice["balance"]), (300, 60, 100, 30, 50, 220))
        self.assertEqual((bob["donated"], bob["spent"], bob["red_packet_sent"], bob["red_packet_received"], bob["red_packet_refunded"], bob["balance"]), (40, 0, 60, 25, 30, 35))
        self.assertEqual((carol["bound"], carol["donated"], carol["balance"]), (False, 0, 25))
        # 币只在用户之间流动：三个人的余额加起来 = 投进来的 − 商店花掉的
        self.assertEqual(alice["balance"] + bob["balance"] + carol["balance"], 300 + 40 - 60)
        ledger = await self._assert_conserved()
        self.assertEqual((ledger["total_sent"], ledger["total_claimed"], ledger["total_refunded"], ledger["outstanding"]), (160, 80, 80, 0))

    async def test_received_coins_can_be_spent_but_only_once_bound(self) -> None:
        self.ledger.donate(OID_A, 300)
        packet_id = await self._send_id(kind="normal", amount=50, count=2)
        await self._claim("bob", packet_id)
        await self._claim("carol", packet_id)
        item = await self._item("bubble.forty", 40)
        # carol 没绑定：币在账上，花不了也发不了（沿用商店的规则）
        self.assertEqual(await self._code(self._as("carol", shop_service.purchase, item)), (409, "ccw_not_bound"))
        self.assertEqual(await self._code(self._send("carol", amount=10, count=1)), (403, "ccw_not_bound"))
        # bob 绑定了、自己一个币没投过：抢到的 50 能买 40 的东西，也能接着发
        bought = await self._as("bob", shop_service.purchase, item)
        self.assertEqual((bought["balance"]["donated"], bought["balance"]["spent"], bought["balance"]["balance"]), (0, 40, 10))
        self.assertEqual(await self._code(self._send("bob", amount=11, count=1)), (402, "insufficient_balance"))
        await self._send("bob", amount=10, count=1)
        self.assertEqual(await self._coins("bob"), 0)
        await self._assert_conserved()

    async def test_sent_coins_cannot_be_spent_in_the_shop(self) -> None:
        self.ledger.donate(OID_A, 100)
        await self._send(amount=70, count=2)
        item = await self._item("bubble.forty", 40)
        status_code, code, detail = await self._fails(self._as("alice", shop_service.purchase, item))
        self.assertEqual((status_code, code, detail["balance"], detail["shortfall"]), (402, "insufficient_coins", 30, 10))

    async def test_a_ccw_account_cannot_be_sent_twice_by_moving_it(self) -> None:
        """绑定 → 把币发给小号 → 解绑 → 换个 KukeChat 账号绑同一个 CCW 账号：同一笔投币不能再发一遍。"""
        self.ledger.donate(OID_A, 100)
        packet_id = await self._send_id(kind="normal", amount=100, count=1)
        await self._claim("bob", packet_id)
        async with self.sessions() as db:
            (await db.get(User, self.ids["alice"])).ccw_student_oid = None
            (await db.get(User, self.ids["carol"])).ccw_student_oid = OID_A
            await db.commit()
        self.tick(30)
        refreshed = await self._as("carol", shop_service.refresh_balance)
        self.assertEqual((refreshed["donated"], refreshed["spent"], refreshed["balance"]), (100, 100, 0))
        self.assertEqual(await self._code(self._send("carol", amount=1, count=1)), (402, "insufficient_balance"))
        item = await self._item("bubble.one", 1)
        self.assertEqual(await self._code(self._as("carol", shop_service.purchase, item)), (402, "insufficient_coins"))
        # 币在 bob 手里，哪儿也没多出来
        self.assertEqual(await self._coins("bob"), 100)

    async def test_refund_restores_the_shared_account(self) -> None:
        """换绑之后，原来那个账号发出去又退回来的部分不再算在这个 CCW 账号头上。"""
        self.ledger.donate(OID_A, 100)
        await self._send(kind="normal", amount=40, count=2)
        async with self.sessions() as db:
            (await db.get(User, self.ids["alice"])).ccw_student_oid = None
            (await db.get(User, self.ids["carol"])).ccw_student_oid = OID_A
            await db.commit()
        self.tick(30)
        self.assertEqual((await self._as("carol", shop_service.refresh_balance))["balance"], 20)
        self.tick(hours=25)
        await self._sweep()
        self.assertEqual(await self._coins("carol"), 100)


# ---------------------------------------------------------------- 并发（同一个进程里）


class ConcurrencyTests(RedPacketTestCase):
    """并发的请求各用各的会话和连接，事务之间真的会抢写锁。"""

    async def test_many_people_grab_at_once(self) -> None:
        self.ledger.donate(OID_A, 5000)
        people = ["bob", "carol", "dave", *EXTRAS]
        for round_index, (count, total) in enumerate(((5, 100), (1, 7), (12, 12), (23, 400))):
            await self._configure(max_count=100)
            packet_id = await self._send_id(amount=total, count=count)
            # 每个人同时点两下
            results = await asyncio.gather(*(self._claim(name, packet_id) for name in people for _ in range(2)), return_exceptions=True)
            won = [result for result in results if isinstance(result, dict)]
            codes = sorted({(result.status_code, result.detail["code"]) for result in results if not isinstance(result, dict)})
            shares = await self._shares(packet_id)
            taken = [share for share in shares if share.user_id is not None]
            stored = await self._packet(packet_id)
            with self.subTest(round=round_index):
                # 不超发：成功的次数 = 被领走的份数 <= 总份数
                self.assertEqual(len(won), min(count, len(people)))
                self.assertEqual(len(taken), len(won))
                # 不重复：每个人最多一份
                self.assertEqual(len({share.user_id for share in taken}), len(taken))
                # 每个成功的请求拿到的就是记在他名下的那一份
                self.assertEqual(sorted(result["amount"] for result in won), sorted(share.amount for share in taken))
                self.assertEqual((stored.claimed_count, stored.claimed_amount), (len(taken), sum(share.amount for share in taken)))
                self.assertEqual(sum(share.amount for share in shares), total)
                self.assertTrue(set(codes) <= {(409, "already_claimed"), (410, "finished")}, codes)
                if count <= len(people):
                    self.assertEqual((stored.status, stored.claimed_amount), ("finished", total))
            await self._assert_conserved()
        # 所有人的余额加起来 = 投进来的
        total_coins = sum([await self._coins(name) for name in ("alice", *people)])
        self.assertEqual(total_coins, 5000)

    async def test_two_sends_cannot_overdraw(self) -> None:
        self.ledger.donate(OID_A, 100)
        for round_index in range(4):
            # 让余额每一轮都回到 100
            self.ledger.donate(OID_A, 100 + 60 * round_index)
            self.tick(21)
            if round_index % 2:
                # 奇数轮先核实好，两个请求直接抢扣款；偶数轮连核实也一起并发
                await self._as("alice", shop_service.refresh_balance)
            results = await asyncio.gather(self._send(amount=60, count=3), self._send(amount=60, count=2), return_exceptions=True)
            sent = [result for result in results if isinstance(result, dict)]
            failed = [result for result in results if not isinstance(result, dict)]
            with self.subTest(round=round_index):
                self.assertEqual((len(sent), len(failed)), (1, 1))
                self.assertEqual((failed[0].status_code, failed[0].detail["code"], failed[0].detail["balance"]), (402, "insufficient_balance", 40))
                self.assertEqual((await self._coins("alice"), await self._count(RedPacket), await self._count(Message)), (40, round_index + 1, round_index + 1))
        await self._assert_conserved()

    async def test_send_and_purchase_cannot_overdraw_together(self) -> None:
        self.ledger.donate(OID_A, 100)
        for round_index in range(4):
            item = await self._item(f"bubble.sixty{round_index}", 60)
            self.ledger.donate(OID_A, 100 + 60 * round_index)
            self.tick(21)
            await self._as("alice", shop_service.refresh_balance)
            results = await asyncio.gather(self._send(amount=60, count=3), self._as("alice", shop_service.purchase, item), return_exceptions=True)
            failed = [result for result in results if isinstance(result, Exception)]
            with self.subTest(round=round_index):
                self.assertEqual(len(failed), 1, results)
                self.assertEqual(failed[0].status_code, 402)
                self.assertEqual(await self._coins("alice"), 40)
        balance = await self._balance("alice")
        self.assertEqual(balance["spent"] + balance["red_packet_sent"], 240)

    async def test_daily_limit_holds_under_concurrency(self) -> None:
        self.ledger.donate(OID_A, 300)
        await self._configure(daily_send_limit=2)
        results = await asyncio.gather(*(self._send(amount=5, count=1) for _ in range(5)), return_exceptions=True)
        failed = [result for result in results if isinstance(result, Exception)]
        self.assertEqual(len(failed), 3, results)
        self.assertEqual({(result.status_code, result.detail["code"]) for result in failed}, {(429, "daily_limit")})
        self.assertEqual((await self._count(RedPacket), await self._coins("alice")), (2, 290))

    async def test_claims_race_with_expiry_and_revocation(self) -> None:
        """一边有人在抢，一边到期清扫 / 后台撤销：每一份要么被领走、要么被退回，不会两头都算。"""
        self.ledger.donate(OID_A, 5000)
        people = ["bob", "carol", "dave", *EXTRAS[:9]]
        for round_index in range(4):
            packet_id = await self._send_id(amount=200, count=8)
            if round_index % 2:
                enders = [self._as("admin", rp.admin_revoke, packet_id, "并发") for _ in range(2)]
            else:
                # 清扫用的是「已经过期」的时刻，抢的人用的是没过期的时刻：模拟卡在到期那一瞬间
                async def sweep_later() -> dict[str, int]:
                    async with self.sessions() as db:
                        return await rp.sweep(db, now=self.now + timedelta(hours=25))

                enders = [sweep_later(), sweep_later()]
            results = await asyncio.gather(*(self._claim(name, packet_id) for name in people), *enders, return_exceptions=True)
            won = [result for result in results[: len(people)] if isinstance(result, dict)]
            stored = await self._packet(packet_id)
            taken = [share for share in await self._shares(packet_id) if share.user_id is not None]
            with self.subTest(round=round_index):
                self.assertEqual(len(won), len(taken))
                self.assertEqual(stored.claimed_amount, sum(share.amount for share in taken))
                self.assertIn(stored.status, ("finished", "revoked", "expired"))
                self.assertEqual(stored.claimed_amount + stored.refunded_amount, 200)
                # 结束它的只有一个
                ended = [result for result in results[len(people) :] if not isinstance(result, Exception)]
                if round_index % 2:
                    self.assertLessEqual(len(ended), 1)
                else:
                    self.assertLessEqual(sum(result["expired"] for result in ended), 1)
            await self._assert_conserved()
        self.assertEqual(sum([await self._coins(name) for name in ("alice", *people)]), 5000)


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


class MultiProcessTests(RedPacketTestCase):
    """线上是两个 worker 进程。这里起三个进程对着同一个库文件，进程之间没有任何共享的锁，只有 SQLite 自己的。"""

    PROCESSES = 3

    def _job(self, mode: str, **extra: Any) -> dict[str, Any]:
        return {"db": str(self.db_path), "mode": mode, "now": self.now.isoformat(), "donations": {OID_A: 100}, **extra}

    async def _run(self, jobs: list[dict[str, Any]]) -> list[Any]:
        # 子进程要读库文件：先把这边连接池里的连接都放掉，免得挡着
        await self.engine.dispose()
        return await asyncio.to_thread(_spawn_workers, jobs)

    async def test_processes_grab_the_same_packet(self) -> None:
        self.ledger.donate(OID_A, 5000)
        people = [self.ids[name] for name in ("bob", "carol", "dave", *EXTRAS)]
        packets = [await self._send_id(amount=total, count=count) for count, total in ((8, 300), (1, 9), (20, 500))]
        for packet_id, count in zip(packets, (8, 1, 20)):
            # 每个进程都替全部 23 个人各点一下：同一个人会从三个进程同时来
            outputs = await self._run([self._job("claim", packet_id=packet_id, user_ids=people) for _ in range(self.PROCESSES)])
            results = [tuple(item) for output in outputs for item in output]
            won = [item for item in results if item[1] == "ok"]
            shares = await self._shares(packet_id)
            taken = [share for share in shares if share.user_id is not None]
            stored = await self._packet(packet_id)
            with self.subTest(packet=packet_id):
                self.assertEqual(len(results), len(people) * self.PROCESSES)
                self.assertEqual(len(won), count)
                self.assertEqual(len({user_id for user_id, _code, _amount in won}), count)
                self.assertEqual(sorted((user_id, amount) for user_id, _code, amount in won), sorted((share.user_id, share.amount) for share in taken))
                self.assertEqual((stored.status, stored.claimed_count, stored.claimed_amount), ("finished", count, stored.total))
                self.assertTrue({code for _user, code, _amount in results} <= {"ok", "already_claimed", "finished"})
            await self._assert_conserved()
        self.assertEqual(sum([await self._coins(name) for name in ("alice", "bob", "carol", "dave", *EXTRAS)]), 5000)

    async def test_processes_cannot_double_spend(self) -> None:
        self.ledger.donate(OID_A, 100)
        await self._as("alice", shop_service.refresh_balance)
        item = await self._item("bubble.sixty", 60)
        jobs = [self._job("send", user_id=self.ids["alice"], conversation_id=self.group_id, total=60, sends=2) for _ in range(self.PROCESSES - 1)]
        jobs.append(self._job("spend", user_id=self.ids["alice"], conversation_id=self.group_id, total=60, sends=1, item_id=item))
        outputs = await self._run(jobs)
        results = [code for output in outputs for code in output]
        # 余额 100：五个发 60 的红包加一次 60 的购买同时来，只成一个
        self.assertEqual(len(results), 6)
        self.assertEqual(results.count("ok"), 1, results)
        self.assertTrue(set(results) <= {"ok", "insufficient_balance", "insufficient_coins"}, results)
        balance = await self._balance("alice")
        self.assertEqual((balance["balance"], balance["spent"] + balance["red_packet_sent"]), (40, 60))
        async with self.sessions() as db:
            purchases = int(await db.scalar(select(func.count()).select_from(ShopPurchase)) or 0)
        self.assertEqual(await self._count(RedPacket) + purchases, 1)
        await self._assert_conserved()

    async def test_processes_sweep_the_same_packets_once(self) -> None:
        self.ledger.donate(OID_A, 5000)
        packets = [await self._send_id(amount=50, count=5) for _ in range(6)]
        for packet_id in packets[:3]:
            await self._claim("bob", packet_id)
        self.tick(hours=25)
        outputs = await self._run([self._job("sweep") for _ in range(self.PROCESSES)])
        # 六个红包各只被结束一次，不管是哪个进程做的
        self.assertEqual(sum(output["expired"] for output in outputs), 6)
        for packet_id in packets:
            stored = await self._packet(packet_id)
            self.assertEqual((stored.status, stored.claimed_amount + stored.refunded_amount), ("expired", 50))
        self.assertEqual(await self._coins("alice") + await self._coins("bob"), 5000)
        ledger = await self._assert_conserved()
        self.assertEqual(ledger["outstanding"], 0)


# ---------------------------------------------------------------- 过期与撤回


class ExpiryTests(RedPacketTestCase):
    async def test_expiry_is_judged_at_claim_time_without_the_sweeper(self) -> None:
        self.ledger.donate(OID_A, 300)
        packet_id = await self._send_id(amount=100, count=4)
        taken = (await self._claim("bob", packet_id))["amount"]
        self.tick(hours=24)
        # 正好到点：领不了了，而且当场退回，不等清扫
        self.assertEqual(await self._code(self._claim("carol", packet_id)), (410, "expired"))
        stored = await self._packet(packet_id)
        self.assertEqual((stored.status, stored.claimed_count, stored.refunded_amount, stored.finished_at.replace(tzinfo=UTC)), ("expired", 1, 100 - taken, self.now))
        self.assertEqual((await self._coins("alice"), await self._coins("bob"), await self._coins("carol")), (300 - taken, taken, 0))
        # 已经领过的人：仍然是「领过了」
        self.assertEqual(await self._code(self._claim("bob", packet_id)), (409, "already_claimed"))
        # 清扫再来一遍什么都不做
        self.assertEqual(await self._sweep(), {"expired": 0, "revoked": 0})
        await self._assert_conserved()

    async def test_the_claim_statement_itself_refuses_expired_and_recalled_packets(self) -> None:
        """就算没有任何人先把红包结束掉，抢占那条 UPDATE 自己也拿不到行。"""
        self.ledger.donate(OID_A, 300)
        expired = await self._send_id(amount=50, count=2)
        recalled = await self._send_id(amount=50, count=2)
        async with self.sessions() as db:
            message = await db.get(Message, (await db.get(RedPacket, recalled)).message_id)
            message.recalled_at = datetime.now(UTC)
            await db.commit()
        async with self.sessions() as db:
            self.assertEqual(await rp._take_share(db, expired, self.ids["bob"], self.now + timedelta(hours=24)), ("none", None))
            self.assertEqual(await rp._take_share(db, recalled, self.ids["bob"], self.now), ("none", None))
            self.assertEqual(await rp._take_share(db, expired, self.ids["bob"], self.now + timedelta(hours=23)), ("ok", None))
            self.assertEqual(await rp._take_share(db, expired, self.ids["bob"], self.now + timedelta(hours=23)), ("already", None))
        self.assertEqual([(await self._packet(packet_id)).claimed_count for packet_id in (expired, recalled)], [1, 0])

    async def test_sweep_refunds_and_is_idempotent(self) -> None:
        self.ledger.donate(OID_A, 300)
        untouched = await self._send_id(amount=40, count=4)
        partial = await self._send_id(amount=60, count=3)
        finished = await self._send_id(kind="normal", amount=10, count=1)
        taken = (await self._claim("bob", partial))["amount"]
        await self._claim("bob", finished)
        self.tick(hours=23)
        later = await self._send_id(amount=20, count=2)
        self.assertEqual(await self._sweep(), {"expired": 0, "revoked": 0})
        self.tick(hours=1)
        # 两轮清扫同时跑（两个 worker），再补一轮：每个红包只退一次
        results = await asyncio.gather(self._sweep(), self._sweep())
        self.assertEqual(sum(result["expired"] for result in results), 2)
        self.assertEqual(await self._sweep(), {"expired": 0, "revoked": 0})
        states = [(packet.status, packet.claimed_amount, packet.refunded_amount) for packet in [await self._packet(packet_id) for packet_id in (untouched, partial, finished, later)]]
        self.assertEqual(states, [("expired", 0, 40), ("expired", taken, 60 - taken), ("finished", 10, 0), ("active", 0, 0)])
        self.assertEqual((await self._coins("alice"), await self._coins("bob")), (300 - taken - 10 - 20, taken + 10))
        # 事件：每个过期的红包一条 red_packet.updated（不带金额），发送者一条余额变更
        updates = [data for _to, data in await self._events("red_packet.updated") if data["status"] == "expired"]
        self.assertEqual(sorted(data["id"] for data in updates), [untouched, partial])
        self.assertTrue(all(set(data) == {"id", "conversation_id", "message_id", "claimed_count", "count", "status"} for data in updates))
        refunds = [(to, data["red_packet_id"]) for to, data in await self._events("shop.balance.changed") if data["change"] == "red_packet.refunded"]
        self.assertEqual(sorted(refunds), [(self.ids["alice"], untouched), (self.ids["alice"], partial)])
        ledger = await self._assert_conserved()
        self.assertEqual((ledger["total_sent"], ledger["total_claimed"], ledger["total_refunded"], ledger["outstanding"]), (130, taken + 10, 100 - taken, 20))

    async def test_expire_hours_only_applies_to_new_packets(self) -> None:
        self.ledger.donate(OID_A, 300)
        old = await self._send_id(amount=10, count=1)
        await self._configure(expire_hours=1)
        new = await self._send_id(amount=10, count=1)
        self.tick(hours=2)
        self.assertEqual(await self._sweep(), {"expired": 1, "revoked": 0})
        self.assertEqual([(await self._packet(packet_id)).status for packet_id in (old, new)], ["active", "expired"])

    async def test_reads_show_expired_before_the_sweeper_gets_there(self) -> None:
        self.ledger.donate(OID_A, 300)
        packet_id = await self._send_id(amount=40, count=4)
        self.tick(hours=30)
        listed = (await self._as("alice", rp.list_mine, "sent", 20, 0))["items"][0]
        self.assertEqual((listed["status"], listed["finished_at"], listed["refunded_amount"]), ("expired", START + timedelta(hours=24), 0))
        self.assertEqual((await self._packet(packet_id)).status, "active")
        # 读单个红包会顺手把它结束掉
        self.assertEqual((await self._get("bob", packet_id))["status"], "expired")
        self.assertEqual(((await self._packet(packet_id)).status, await self._coins("alice")), ("expired", 300))


class RecallTests(RedPacketTestCase):
    async def _message_id(self, packet_id: int) -> int:
        return (await self._packet(packet_id)).message_id

    async def test_sender_recalling_the_message_ends_the_packet(self) -> None:
        self.ledger.donate(OID_A, 300)
        packet_id = await self._send_id(amount=90, count=3)
        taken = (await self._claim("bob", packet_id))["amount"]
        async with self.sessions() as db:
            message = await message_service.recall_message(db, self.group_id, self.ids["alice"], await self._message_id(packet_id))
            self.assertIsNotNone(message.recalled_at)
        stored = await self._packet(packet_id)
        self.assertEqual((stored.status, stored.refunded_amount, stored.revoked_by_id), ("revoked", 90 - taken, None))
        self.assertEqual((await self._coins("alice"), await self._coins("bob")), (300 - taken, taken))
        self.assertEqual(await self._code(self._claim("carol", packet_id)), (410, "revoked"))
        # 已经领到的不收回
        self.assertEqual((await self._get("bob", packet_id))["my_claim"]["amount"], taken)
        updates = [data for _to, data in await self._events("red_packet.updated")]
        self.assertEqual((updates[-1]["id"], updates[-1]["status"], updates[-1]["claimed_count"]), (packet_id, "revoked", 1))
        # 再撤回一次（幂等）不会再退一遍
        async with self.sessions() as db:
            await message_service.recall_message(db, self.group_id, self.ids["alice"], await self._message_id(packet_id))
        self.assertEqual(await self._coins("alice"), 300 - taken)
        await self._assert_conserved()

    async def test_group_staff_and_platform_admin_recalls(self) -> None:
        self.ledger.donate(OID_A, 300)
        by_owner = await self._send_id(amount=30, count=3)
        by_admin = await self._send_id(amount=30, count=3)
        async with self.sessions() as db:
            await message_service.recall_message(db, self.group_id, self.ids["dave"], await self._message_id(by_owner))
        async with self.sessions() as db:
            await message_service.admin_recall_message(db, self.ids["admin"], await self._message_id(by_admin))
        self.assertEqual([(await self._packet(packet_id)).status for packet_id in (by_owner, by_admin)], ["revoked", "revoked"])
        self.assertEqual(await self._coins("alice"), 300)

    async def test_recalls_that_bypass_the_hook_are_caught(self) -> None:
        """机器审核、案件结案直接改 recalled_at，不经过撤回接口：领取时现场判断，清扫负责退回。"""
        self.ledger.donate(OID_A, 300)
        claimed_first = await self._send_id(amount=30, count=3)
        swept = await self._send_id(amount=30, count=3)
        async with self.sessions() as db:
            for packet_id in (claimed_first, swept):
                (await db.get(Message, (await db.get(RedPacket, packet_id)).message_id)).recalled_at = datetime.now(UTC)
            await db.commit()
        self.assertEqual(await self._coins("alice"), 240)
        self.assertEqual(await self._code(self._claim("bob", claimed_first)), (410, "revoked"))
        self.assertEqual(((await self._packet(claimed_first)).status, await self._coins("alice")), ("revoked", 270))
        self.assertEqual(await self._sweep(), {"expired": 0, "revoked": 1})
        self.assertEqual(((await self._packet(swept)).status, await self._coins("alice"), await self._coins("bob")), ("revoked", 300, 0))
        self.assertEqual(await self._sweep(), {"expired": 0, "revoked": 0})

    async def test_dissolved_conversation_is_refunded_by_the_sweeper(self) -> None:
        self.ledger.donate(OID_A, 300)
        packet_id = await self._send_id(amount=30, count=3)
        async with self.sessions() as db:
            await db.delete(await db.get(Conversation, self.group_id))
            await db.commit()
        # 会话和消息都没了，红包和账还在
        self.assertEqual(await self._sweep(), {"expired": 0, "revoked": 1})
        self.assertEqual(((await self._packet(packet_id)).status, await self._coins("alice")), ("revoked", 300))
        await self._assert_conserved()


# ---------------------------------------------------------------- 可见性


class VisibilityTests(RedPacketTestCase):
    async def test_amounts_are_hidden_from_those_who_have_not_claimed(self) -> None:
        self.ledger.donate(OID_A, 300)
        packet_id = await self._send_id(amount=100, count=3, greeting="手快有")
        first = (await self._claim("bob", packet_id))["amount"]
        # 没抢的人：看得到祝福语、份数和进度，看不到任何金额和明细
        outsider_view = await self._get("carol", packet_id)
        self.assertEqual((outsider_view["total"], outsider_view["claimed_amount"], outsider_view["claims"], outsider_view["my_claim"]), (None, None, None, None))
        self.assertEqual((outsider_view["greeting"], outsider_view["count"], outsider_view["claimed_count"], outsider_view["status"], outsider_view["sender"].username), ("手快有", 3, 1, "active", "alice"))
        self.assertNotIn(str(first), json.dumps({key: value for key, value in outsider_view.items() if key in ("total", "claimed_amount", "claims", "my_claim")}))
        # 抢了的人、发的人：都看得到
        for name in ("bob", "alice"):
            view = await self._get(name, packet_id)
            with self.subTest(viewer=name):
                self.assertEqual((view["total"], view["claimed_amount"]), (100, first))
                self.assertEqual([(claim["user"].username, claim["amount"], claim["is_luckiest"]) for claim in view["claims"]], [("bob", first, False)])
        self.assertEqual((await self._get("bob", packet_id))["my_claim"], {"amount": first, "created_at": self.now})
        self.assertIsNone((await self._get("alice", packet_id))["my_claim"])
        # 不是会话成员：看都不能看
        self.assertEqual(await self._code(self._get("eve", packet_id)), (403, "not_member"))
        self.assertEqual(await self._code(self._get("alice", 999999)), (404, "not_found"))
        # 抢完之后所有成员都能看，手气最佳只标一个
        await self._claim("dave", packet_id)
        await self._claim("alice", packet_id)
        final = await self._get("carol", packet_id)
        self.assertEqual((final["status"], final["total"], final["claimed_amount"], final["my_claim"]), ("finished", 100, 100, None))
        self.assertEqual([claim["user"].username for claim in final["claims"]], ["bob", "dave", "alice"])
        self.assertEqual(sum(claim["amount"] for claim in final["claims"]), 100)
        lucky = [claim for claim in final["claims"] if claim["is_luckiest"]]
        self.assertEqual(len(lucky), 1)
        self.assertEqual(lucky[0]["amount"], max(claim["amount"] for claim in final["claims"]))

    async def test_luckiest_rules(self) -> None:
        self.ledger.donate(OID_A, 300)
        # 金额一样大时算先抢到的
        with patch.object(rp, "split_lucky", return_value=[10, 30, 30]):
            tied = await self._send_id(amount=70, count=3)
        normal = await self._send_id(kind="normal", amount=5, count=2)
        unfinished = await self._send_id(amount=50, count=3)
        for name in ("bob", "carol", "dave"):
            await self._claim(name, tied)
        for name in ("bob", "carol"):
            await self._claim(name, normal)
            await self._claim(name, unfinished)
        self.assertEqual([(claim["user"].username, claim["is_luckiest"]) for claim in (await self._get("alice", tied))["claims"]], [("bob", False), ("carol", True), ("dave", False)])
        # 普通红包不标；没抢完的拼手气也不标（到期了也不标）
        self.assertFalse(any(claim["is_luckiest"] for claim in (await self._get("alice", normal))["claims"]))
        self.assertFalse(any(claim["is_luckiest"] for claim in (await self._get("alice", unfinished))["claims"]))
        self.tick(hours=25)
        expired = await self._get("dave", unfinished)
        self.assertEqual((expired["status"], expired["total"], len(expired["claims"])), ("expired", 50, 2))
        self.assertFalse(any(claim["is_luckiest"] for claim in expired["claims"]))

    async def test_my_records(self) -> None:
        self.ledger.donate(OID_A, 300)
        self.ledger.donate(OID_B, 100)
        first = await self._send_id(kind="normal", amount=10, count=2, greeting="一")
        self.tick(60)
        second = await self._send_id(kind="normal", amount=20, count=1, greeting="二")
        self.tick(60)
        from_bob = await self._send_id("bob", kind="normal", amount=7, count=3, greeting="三")
        await self._claim("bob", first)
        self.tick(60)
        await self._claim("alice", from_bob)
        self.tick(60)
        await self._claim("alice", first)
        sent = await self._as("alice", rp.list_mine, "sent", 20, 0)
        self.assertEqual((sent["total"], sent["sum"], [item["id"] for item in sent["items"]]), (2, 40, [second, first]))
        self.assertEqual([(item["total"], item["claimed_count"], item["claimed_amount"], item["refunded_amount"], item["status"], item["claims"]) for item in sent["items"]], [(20, 0, 0, 0, "active", None), (20, 2, 20, 0, "finished", None)])
        received = await self._as("alice", rp.list_mine, "received", 20, 0)
        self.assertEqual([set(item) for item in received["items"]], [{"amount", "created_at", "red_packet"}] * 2)
        self.assertEqual((received["total"], received["sum"], [item["red_packet"]["id"] for item in received["items"]]), (2, 17, [first, from_bob]))
        self.assertEqual([(item["amount"], item["created_at"], item["red_packet"]["sender"].username, item["red_packet"]["greeting"]) for item in received["items"]], [(10, self.now, "alice", "一"), (7, self.now - timedelta(seconds=60), "bob", "三")])
        self.assertEqual([item["red_packet"]["my_claim"]["amount"] for item in received["items"]], [10, 7])
        page = await self._as("alice", rp.list_mine, "received", 1, 1)
        self.assertEqual((page["total"], page["sum"], [item["red_packet"]["id"] for item in page["items"]]), (2, 17, [from_bob]))
        # 退回的数：到期之后在「我发出的」里看得到
        self.tick(hours=25)
        await self._sweep()
        sent = await self._as("alice", rp.list_mine, "sent", 20, 0)
        self.assertEqual([(item["status"], item["refunded_amount"]) for item in sent["items"]], [("expired", 20), ("finished", 0)])
        empty = await self._as("carol", rp.list_mine, "received", 20, 0)
        self.assertEqual(empty, {"items": [], "total": 0, "sum": 0})


# ---------------------------------------------------------------- 会话列表上的「[有红包]」


class PendingFlagTests(RedPacketTestCase):
    async def _pending(self, name: str) -> set[int]:
        async with self.sessions() as db:
            return await rp.pending_conversation_ids(db, self.ids[name])

    async def _flags(self, name: str) -> dict[int, bool]:
        """走会话列表真正用的那条路径。"""
        from app.services import conversations as conversation_service

        async with self.sessions() as db:
            items = await conversation_service.list_user_conversations(db, self.ids[name])
            reads = await conversation_service.build_conversation_reads(db, items, self.ids[name])
            return {read.id: read.red_packet_pending for read in reads}

    def _red_packet_statements(self):
        seen: list[str] = []

        def record(_conn, _cursor, statement, *_rest) -> None:
            if "red_packet" in statement:
                seen.append(statement)

        event.listen(self.engine.sync_engine, "before_cursor_execute", record)
        self.addCleanup(event.remove, self.engine.sync_engine, "before_cursor_execute", record)
        return seen

    async def test_flag_follows_what_i_can_still_claim(self) -> None:
        self.ledger.donate(OID_A, 500)
        self.assertEqual(await self._flags("alice"), {self.group_id: False, self.direct_id: False})
        group_packet = await self._send_id(amount=30, count=2)
        # 群里自己发的、自己还没抢的也算；不在会话里的人没有
        self.assertEqual(await self._flags("alice"), {self.group_id: True, self.direct_id: False})
        self.assertEqual(await self._flags("bob"), {self.group_id: True, self.direct_id: False})
        self.assertEqual((await self._pending("carol"), await self._pending("eve")), ({self.group_id}, set()))
        # 私聊：发的人自己没有，对方有
        direct_packet = await self._send_id(conversation_id=self.direct_id, kind="normal", amount=10, count=1)
        self.assertEqual(await self._flags("alice"), {self.group_id: True, self.direct_id: False})
        self.assertEqual(await self._flags("bob"), {self.group_id: True, self.direct_id: True})
        # 已领：我这边没有了，别人还有
        await self._claim("bob", group_packet)
        self.assertEqual(await self._flags("bob"), {self.group_id: False, self.direct_id: True})
        self.assertEqual(await self._pending("carol"), {self.group_id})
        await self._claim("bob", direct_packet)
        self.assertEqual(await self._flags("bob"), {self.group_id: False, self.direct_id: False})
        # 已抢完：谁都没有了
        await self._claim("alice", group_packet)
        self.assertEqual((await self._packet(group_packet)).status, "finished")
        self.assertEqual((await self._pending("carol"), await self._pending("alice"), await self._pending("dave")), (set(), set(), set()))
        # 同一个会话里一个领过、一个没领过：还是有
        await self._send_id(amount=30, count=2)
        self.assertEqual(await self._pending("bob"), {self.group_id})

    async def test_expired_and_ended_packets_do_not_count(self) -> None:
        self.ledger.donate(OID_A, 500)
        expiring = await self._send_id(amount=30, count=2)
        self.tick(hours=12)
        revoked = await self._send_id(amount=30, count=2)
        recalled = await self._send_id(conversation_id=self.direct_id, kind="normal", amount=10, count=1)
        self.assertEqual(await self._pending("bob"), {self.group_id, self.direct_id})
        async with self.sessions() as db:
            await message_service.recall_message(db, self.direct_id, self.ids["alice"], (await self._packet(recalled)).message_id)
        self.assertEqual(await self._pending("bob"), {self.group_id})
        await self._as("admin", rp.admin_revoke, revoked, "测试")
        self.assertEqual(await self._pending("bob"), {self.group_id})
        # 过期但还没清扫：库里仍是 active，标记已经没有了
        self.tick(hours=12)
        self.assertEqual(((await self._packet(expiring)).status, await self._pending("bob"), await self._pending("carol")), ("active", set(), set()))
        self.assertEqual(await self._flags("bob"), {self.group_id: False, self.direct_id: False})

    async def test_one_query_for_the_whole_list_and_none_when_idle(self) -> None:
        self.ledger.donate(OID_A, 500)
        async with self.sessions() as db:
            # bob 再多几个会话：查询次数不随会话数变
            for index in range(6):
                extra = Conversation(type=ConversationType.GROUP, title=f"群{index}")
                db.add(extra)
                await db.flush()
                db.add(ConversationMember(conversation_id=extra.id, user_id=self.ids["bob"], role=MemberRole.OWNER))
            await db.commit()
        seen = self._red_packet_statements()
        # 全站没有进行中的红包：第一次是一次 LIMIT 1 的探测，之后几秒内完全不碰红包的表
        self.assertFalse(any((await self._flags("bob")).values()))
        self.assertEqual(len(seen), 1)
        self.assertIn("LIMIT", seen[0])
        for name in ("bob", "alice", "carol"):
            await self._flags(name)
        self.assertEqual(len(seen), 1)
        # 本进程里发了新红包：马上看得到，不用等那几秒
        await self._send_id(amount=30, count=2)
        await shop_service.drain()
        seen.clear()
        flags = await self._flags("bob")
        self.assertEqual((sum(flags.values()), flags[self.group_id], len(flags)), (1, True, 8))
        # 一次探测 + 一条算出全部会话的查询
        self.assertEqual(len(seen), 2)
        seen.clear()
        await self._flags("bob")
        await self._flags("carol")
        self.assertEqual(len(seen), 2)
        self.assertTrue(all("conversation_members" in statement for statement in seen))
        # 探测的答案过期之后，发现已经没有进行中的红包了，又回到不查库
        self.tick(hours=25)
        with patch.object(rp, "LIVE_CACHE_SECONDS", 0.0):
            self.assertFalse(any((await self._flags("bob")).values()))
        seen.clear()
        await self._flags("bob")
        self.assertEqual(seen, [])

    async def test_single_conversation_carries_the_flag(self) -> None:
        from app.services import conversations as conversation_service

        self.ledger.donate(OID_A, 500)
        await self._send_id(conversation_id=self.direct_id, kind="normal", amount=10, count=1)
        async with self.sessions() as db:
            conversation = await db.get(Conversation, self.direct_id)
            for_bob = await conversation_service.build_conversation_read(db, conversation, self.ids["bob"])
            for_alice = await conversation_service.build_conversation_read(db, conversation, self.ids["alice"])
        self.assertEqual((for_bob.red_packet_pending, for_alice.red_packet_pending), (True, False))


# ---------------------------------------------------------------- 实时事件


class EventTests(RedPacketTestCase):
    async def test_updated_event_goes_to_members_without_amounts(self) -> None:
        self.ledger.donate(OID_A, 300)
        packet_id = await self._send_id(kind="normal", amount=10, count=2)
        message_id = (await self._packet(packet_id)).message_id
        await self._claim("bob", packet_id)
        events = await self._events("red_packet.updated")
        members = {self.ids[name] for name in ("alice", "bob", "carol", "dave", *EXTRAS)}
        self.assertEqual([(set(to), data) for to, data in events], [(members, {"id": packet_id, "conversation_id": self.group_id, "message_id": message_id, "claimed_count": 1, "count": 2, "status": "active"})])
        await self._claim("carol", packet_id)
        last = (await self._events("red_packet.updated"))[-1][1]
        self.assertEqual((last["claimed_count"], last["status"]), (2, "finished"))
        # 领的人收到余额变更，带完整的 balance 对象
        self.assertEqual({data["reason"] for _to, data in await self._events("shop.balance.changed")}, {"red_packet"})
        changes = [(to, data["change"], data["balance"]["balance"]) for to, data in await self._events("shop.balance.changed")]
        self.assertEqual(changes, [(self.ids["alice"], "red_packet.sent", 280), (self.ids["bob"], "red_packet.claimed", 10), (self.ids["carol"], "red_packet.claimed", 10)])

    async def test_bursts_of_claims_are_merged(self) -> None:
        self.ledger.donate(OID_A, 300)
        packet_id = await self._send_id(kind="normal", amount=5, count=6)
        with patch.object(rp, "UPDATE_EVENT_DELAY_SECONDS", 1.5):
            for name in ("bob", "carol", "dave", "u00"):
                await self._claim(name, packet_id)
            events = await self._events("red_packet.updated")
        self.assertEqual([data["claimed_count"] for _to, data in events], [4])

    async def test_push_failures_do_not_break_claims(self) -> None:
        self.ledger.donate(OID_A, 300)
        packet_id = await self._send_id(kind="normal", amount=5, count=2)
        self.send_to_users.side_effect = RuntimeError("redis down")
        self.send_to_user.side_effect = RuntimeError("redis down")
        with self.assertLogs("app.services", level="ERROR"):
            self.assertEqual((await self._claim("bob", packet_id))["amount"], 5)
            await shop_service.drain()


# ---------------------------------------------------------------- 后台


class AdminTests(RedPacketTestCase):
    SECRET = "只给你看的悄悄话"

    async def _admin(self, function, *args: Any, **kwargs: Any):
        async with self.sessions() as db:
            return await function(db, await db.get(User, self.ids["admin"]), *args, **kwargs)

    async def _list(self, **filters: Any) -> dict[str, Any]:
        options = {"status_filter": None, "kind": None, "sender_id": None, "conversation_id": None, "created_from": None, "created_to": None, "limit": 50, "offset": 0, **filters}
        async with self.sessions() as db:
            return await rp.admin_list(db, **options)

    async def test_settings(self) -> None:
        async with self.sessions() as db:
            await db.execute(SystemSetting.__table__.delete())
            await db.commit()
        rp.reset_cache()
        async with self.sessions() as db:
            current = await rp.admin_settings(db)
        self.assertEqual({key: current[key] for key in rp.DEFAULTS}, {"enabled": False, "max_total": 500, "max_count": 100, "min_each": 1, "daily_send_limit": 20, "expire_hours": 24})
        self.assertEqual(current["bounds"]["max_count"], {"min": 1, "max": 500})
        # 只传部分字段
        changed = await self._admin(rp.admin_update_settings, {"enabled": True, "max_total": 200})
        self.assertEqual((changed["enabled"], changed["max_total"], changed["max_count"]), (True, 200, 100))
        # 整份提交，没变的不记
        whole = {"enabled": True, "max_total": 200, "max_count": 10, "min_each": 2, "daily_send_limit": 5, "expire_hours": 48}
        self.assertEqual({key: value for key, value in (await self._admin(rp.admin_update_settings, whole)).items() if key != "bounds"}, whole)
        await self._admin(rp.admin_update_settings, whole)
        self.assertEqual(
            await self._audit(),
            [
                ("red_packet.settings.update", {"enabled": {"old": False, "new": True}, "max_total": {"old": 500, "new": 200}}),
                ("red_packet.settings.update", {"max_count": {"old": 100, "new": 10}, "min_each": {"old": 1, "new": 2}, "daily_send_limit": {"old": 20, "new": 5}, "expire_hours": {"old": 24, "new": 48}}),
            ],
        )
        for bad, field in (({"max_total": 0}, "max_total"), ({"max_count": 501}, "max_count"), ({"expire_hours": "24"}, "expire_hours"), ({"enabled": 1}, "enabled"), ({"min_each": 201}, "min_each"), ({"daily_send_limit": True}, "daily_send_limit")):
            status_code, code, detail = await self._fails(self._admin(rp.admin_update_settings, bad))
            with self.subTest(**bad):
                self.assertEqual((status_code, code, detail["field"]), (422, "invalid_settings", field))
        # 用户侧看到的就是这一份，新的限制马上生效
        self.assertEqual((await self._as("alice", rp.config_view))["limits"], {key: whole[key] for key in rp.LIMITS})
        self.ledger.donate(OID_A, 300)
        self.assertEqual(await self._code(self._send(amount=100, count=11)), (422, "invalid_count"))
        self.assertEqual(await self._code(self._send(amount=201, count=5)), (422, "invalid_amount"))

    async def test_list_filters_and_sitewide_summary(self) -> None:
        self.ledger.donate(OID_A, 400)
        self.ledger.donate(OID_B, 100)
        lucky = await self._send_id(amount=100, count=4, greeting="群里的")
        self.tick(hours=1)
        direct = await self._send_id(conversation_id=self.direct_id, kind="normal", amount=30, count=1, greeting=self.SECRET)
        self.tick(hours=1)
        from_bob = await self._send_id("bob", kind="normal", amount=10, count=2)
        self.tick(hours=1)
        revoked = await self._send_id(amount=50, count=5)
        taken = (await self._claim("carol", lucky))["amount"]
        await self._claim("bob", direct)
        await self._admin(rp.admin_revoke, revoked, "违规刷屏")
        summary = {"total_sent": 200, "total_claimed": taken + 30, "total_refunded": 50, "outstanding": 100 - taken + 20}
        everything = await self._list()
        self.assertEqual((everything["total"], [item["id"] for item in everything["items"]], everything["summary"]), (4, [revoked, from_bob, direct, lucky], summary))
        first = everything["items"][-1]
        self.assertEqual(
            (first["sender"]["username"], first["sender_id"], first["conversation_id"], first["conversation_type"], first["kind"], first["greeting"], first["total"], first["count"]),
            ("alice", self.ids["alice"], self.group_id, "group", "lucky", "群里的", 100, 4),
        )
        self.assertEqual((first["claimed_count"], first["claimed_amount"], first["refunded_amount"], first["status"], first["created_at"], first["expires_at"]), (1, taken, 0, "active", START, START + timedelta(hours=24)))
        cases = [
            ({"status_filter": "active"}, [from_bob, lucky]),
            ({"status_filter": "finished"}, [direct]),
            ({"status_filter": "revoked"}, [revoked]),
            ({"status_filter": "expired"}, []),
            ({"kind": "normal"}, [from_bob, direct]),
            ({"sender_id": self.ids["bob"]}, [from_bob]),
            ({"conversation_id": self.direct_id}, [direct]),
            ({"created_from": START + timedelta(hours=1)}, [revoked, from_bob, direct]),
            ({"created_to": START + timedelta(hours=1)}, [lucky]),
            ({"created_from": START + timedelta(hours=1), "created_to": START + timedelta(hours=3), "kind": "normal", "sender_id": self.ids["alice"]}, [direct]),
            ({"limit": 2, "offset": 1}, [from_bob, direct]),
        ]
        for filters, expected in cases:
            listing = await self._list(**filters)
            with self.subTest(**{key: str(value) for key, value in filters.items()}):
                self.assertEqual([item["id"] for item in listing["items"]], expected)
                # summary 是全站累计，不随筛选变
                self.assertEqual(listing["summary"], summary)
        self.assertEqual((await self._list(limit=2, offset=1))["total"], 4)
        self.assertEqual((await self._list(kind="normal"))["total"], 2)

    async def test_privacy_boundary(self) -> None:
        """后台不能借红包看私聊内容：私聊红包的祝福语一律是 null，后台接口不返回任何消息内容。"""
        self.ledger.donate(OID_A, 300)
        direct = await self._send_id(conversation_id=self.direct_id, kind="normal", amount=30, count=1, greeting=self.SECRET)
        group = await self._send_id(amount=30, count=3, greeting="大家好")
        await self._claim("bob", direct)
        await self._claim("bob", group)
        async with self.sessions() as db:
            detail = await rp.admin_detail(db, direct)
            group_detail = await rp.admin_detail(db, group)
        listing = await self._list()
        revoked = await self._admin(rp.admin_revoke, group, "测试")
        self.assertEqual((detail["greeting"], detail["conversation_type"], group_detail["greeting"]), (None, "direct", "大家好"))
        self.assertEqual([item["greeting"] for item in listing["items"]], ["大家好", None])
        payloads = json.dumps([detail, group_detail, listing, revoked], ensure_ascii=False, default=str)
        self.assertNotIn(self.SECRET, payloads)
        self.assertNotIn("更新到新版才能领取", payloads)
        for payload in (detail, group_detail, revoked, *listing["items"]):
            self.assertFalse({"content", "message", "metadata", "fallback"} & set(payload))
        # 审计日志里也没有
        self.assertNotIn(self.SECRET, json.dumps(await self._audit(), ensure_ascii=False))
        # 接口的返回结构里同样没有能放消息内容的字段
        from app.schemas.red_packet import AdminRedPacketDetail

        self.assertFalse({"content", "message", "metadata"} & set(AdminRedPacketDetail.model_fields))
        self.assertIsNone(AdminRedPacketDetail(**detail).greeting)

    async def test_detail_and_revoke(self) -> None:
        self.ledger.donate(OID_A, 300)
        packet_id = await self._send_id(amount=90, count=3)
        taken = (await self._claim("bob", packet_id))["amount"]
        async with self.sessions() as db:
            detail = await rp.admin_detail(db, packet_id)
        self.assertEqual([(claim["user"]["username"], claim["user_id"], claim["amount"], claim["created_at"], claim["is_luckiest"]) for claim in detail["claims"]], [("bob", self.ids["bob"], taken, self.now, False)])
        for bad in ("", "   ", "字" * 501):
            self.assertEqual(await self._code(self._admin(rp.admin_revoke, packet_id, bad)), (422, "invalid_reason"))
        self.tick(600)
        revoked = await self._admin(rp.admin_revoke, packet_id, "  刷屏  ")
        self.assertEqual((revoked["status"], revoked["refunded_amount"], revoked["revoked_by_id"], revoked["revoke_reason"], revoked["finished_at"]), ("revoked", 90 - taken, self.ids["admin"], "刷屏", self.now))
        self.assertEqual(len(revoked["claims"]), 1)
        self.assertEqual((await self._audit())[-1], ("red_packet.revoke", {"reason": "刷屏", "refunded": 90 - taken}))
        self.assertEqual((await self._coins("alice"), await self._coins("bob")), (300 - taken, taken))
        self.assertEqual(await self._code(self._claim("carol", packet_id)), (410, "revoked"))
        # 已经结束的不能再撤销，也不会多一条审计
        self.assertEqual(await self._code(self._admin(rp.admin_revoke, packet_id, "再来")), (409, "not_active"))
        self.assertEqual(await self._code(self._admin(rp.admin_revoke, 999999, "没有")), (404, "not_found"))
        self.assertEqual(len(await self._audit()), 1)
        # 会话里的那条消息没被动过
        async with self.sessions() as db:
            self.assertIsNone((await db.get(Message, revoked["message_id"])).recalled_at)

    async def test_reconcile_checks_balances_and_conservation(self) -> None:
        self.ledger.donate(OID_A, 300)
        packet_id = await self._send_id(kind="normal", amount=50, count=3)
        await self._claim("bob", packet_id)
        await self._claim("carol", packet_id)
        # bob 一个币没投过，花了 40：以前这叫「花的比投的多」，现在不是异常
        item = await self._item("bubble.forty", 40)
        await self._as("bob", shop_service.purchase, item)
        self.tick(30)
        result = await self._admin(shop_service.admin_reconcile)
        self.assertEqual(result["mismatched"], [])
        self.assertEqual(result["red_packets"], {"total_sent": 150, "total_claimed": 100, "total_refunded": 0, "outstanding": 50, "balanced": True, "problems": []})
        async with self.sessions() as db:
            accounts = await shop_service.admin_list_accounts(db, q=None, mismatch=None, limit=50, offset=0)
        rows = {row["user"]["username"]: row for row in accounts["items"]}
        # 抢到过红包的人也有账户（carol 没绑定、从没核实过）
        self.assertEqual(set(rows), {"alice", "bob", "carol"})
        self.assertEqual([(rows[name]["donated"], rows[name]["spent"], rows[name]["red_packet_sent"], rows[name]["red_packet_received"], rows[name]["red_packet_refunded"], rows[name]["balance"], rows[name]["mismatch"]) for name in ("alice", "bob", "carol")], [(300, 0, 150, 0, 0, 150, False), (0, 40, 0, 50, 0, 10, False), (0, 0, 0, 50, 0, 50, False)])
        # 到期退回之后
        self.tick(hours=25)
        await self._sweep()
        async with self.sessions() as db:
            accounts = await shop_service.admin_list_accounts(db, q="alice", mismatch=None, limit=50, offset=0)
        self.assertEqual((accounts["items"][0]["red_packet_refunded"], accounts["items"][0]["balance"]), (50, 200))
        # 余额真的为负的才算异常：alice 换绑到一个没投过币的账号
        async with self.sessions() as db:
            (await db.get(User, self.ids["alice"])).ccw_student_oid = "f" * 24
            await db.commit()
        self.tick(30)
        result = await self._admin(shop_service.admin_reconcile)
        self.assertEqual([(row["user"]["username"], row["balance"], row["mismatch"]) for row in result["mismatched"]], [("alice", -100, True)])
        self.assertTrue(result["red_packets"]["balanced"])

    async def test_reconcile_reports_broken_packets(self) -> None:
        self.ledger.donate(OID_A, 300)
        healthy = await self._send_id(kind="normal", amount=10, count=2)
        tampered_share = await self._send_id(kind="normal", amount=10, count=2)
        tampered_counter = await self._send_id(kind="normal", amount=10, count=2)
        tampered_refund = await self._send_id(kind="normal", amount=10, count=2)
        await self._claim("bob", healthy)
        async with self.sessions() as db:
            await db.execute(update(RedPacketShare).where(RedPacketShare.packet_id == tampered_share, RedPacketShare.seq == 1).values(amount=11))
            await db.execute(update(RedPacket).where(RedPacket.id == tampered_counter).values(claimed_count=1, claimed_amount=10))
            await db.execute(update(RedPacket).where(RedPacket.id == tampered_refund).values(status="expired", refunded_amount=5))
            await db.commit()
        ledger = await self._ledger()
        self.assertFalse(ledger["balanced"])
        self.assertEqual([problem["id"] for problem in ledger["problems"]], [tampered_share, tampered_counter, tampered_refund])
        self.assertEqual((ledger["problems"][0]["share_sum"], ledger["problems"][0]["total"]), (21, 20))
        self.assertEqual((ledger["problems"][1]["claimed_count"], ledger["problems"][1]["taken_count"]), (1, 0))
        # 第三个：总数上也对不上（发出 80，已领 10 + 已退 5 + 未结 20+21+20 = 76）
        self.assertNotEqual(ledger["total_sent"], ledger["total_claimed"] + ledger["total_refunded"] + ledger["outstanding"])


# ---------------------------------------------------------------- HTTP


# ---------------------------------------------------------------- 接龙红包、专属红包


class ChainTests(RedPacketTestCase):
    async def _messages(self) -> list[Message]:
        async with self.sessions() as db:
            return list((await db.execute(select(Message).order_by(Message.id))).scalars().all())

    async def test_claiming_posts_the_chain_text_as_the_claimer(self) -> None:
        self.ledger.donate(OID_A, 300)
        sent = await self._send(amount=30, count=3, greeting="开工", chain_text="  老板大气  ")
        packet, message = sent["red_packet"], sent["message"]
        self.assertEqual((packet["chain_text"], packet["target_user_id"], packet["target"]), ("老板大气", None, None))
        self.assertEqual(json.loads(message.metadata_json), {"red_packet": {"id": packet["id"], "kind": "lucky", "greeting": "开工", "count": 3, "chain_text": "老板大气"}})
        self.assertEqual(message.content, "[接龙红包] 开工（更新到新版才能领取）")
        # 发红包时的异步审核把接龙口令也算进去（算在发的人头上）
        self.assertEqual(moderation_service._text_to_moderate(message, json.loads(message.metadata_json)), "开工\n老板大气")
        # 群开了发言限频，bob 刚发过一条：接龙消息不受限
        async with self.sessions() as db:
            (await db.get(Conversation, self.group_id)).message_rate_limit_per_minute = 1
            await db.commit()
        async with self.sessions() as db:
            await message_service.send_message(db, self.group_id, await db.get(User, self.ids["bob"]), "先说一句")
        before = len(await self._messages())
        result, chain = await self._claim_with_message("bob", packet["id"])
        self.assertIsNotNone(chain)
        messages = await self._messages()
        self.assertEqual(len(messages), before + 1)
        last = messages[-1]
        self.assertEqual((last.id, last.sender_id, last.type, last.content), (chain.id, self.ids["bob"], MessageType.TEXT, "老板大气"))
        self.assertEqual(json.loads(last.metadata_json), {"red_packet_chain": {"red_packet_id": packet["id"]}})
        self.assertEqual(result["red_packet"]["my_claim"]["amount"], result["amount"])
        # 会话最后一条、领取人自己的已读位置都跟着走（未读数由它算）
        async with self.sessions() as db:
            member = (await db.execute(select(ConversationMember).where(ConversationMember.conversation_id == self.group_id, ConversationMember.user_id == self.ids["bob"]))).scalar_one()
            self.assertEqual(member.last_read_message_id, chain.id)
        # 发的人自己在群里抢，也一样要说这句话
        _result, own = await self._claim_with_message("alice", packet["id"])
        self.assertEqual((own.sender_id, own.content), (self.ids["alice"], "老板大气"))
        # 普通红包领了不发言
        async with self.sessions() as db:
            (await db.get(Conversation, self.group_id)).message_rate_limit_per_minute = 0
            await db.commit()
        plain = await self._send_id(amount=10, count=1)
        self.assertIsNone((await self._claim_with_message("carol", plain))[1])
        await self._assert_conserved()

    async def test_chain_text_format(self) -> None:
        self.ledger.donate(OID_A, 300)
        self.assertEqual(await self._code(self._send(chain_text="第一行\n第二行")), (422, "chain_text_rejected"))
        self.assertEqual(await self._code(self._send(chain_text="a b")), (422, "chain_text_rejected"))
        self.assertEqual(await self._code(self._send(chain_text="长" * 31)), (422, "chain_text_rejected"))
        self.assertEqual(await self._code(self._send(chain_text=123)), (422, "chain_text_rejected"))
        self.assertEqual((await self._count(RedPacket), (await self._balance("alice"))["red_packet_sent"]), (0, 0))
        # 30 个字可以；只有空白 = 不是接龙红包
        self.assertEqual((await self._send(chain_text="长" * 30, amount=5, count=1))["red_packet"]["chain_text"], "长" * 30)
        self.assertIsNone((await self._send(chain_text="   ", amount=5, count=1))["red_packet"]["chain_text"])

    async def test_rejected_chain_text_charges_nothing(self) -> None:
        self.ledger.donate(OID_A, 300)
        seen: list[str] = []

        async def moderate(text: str, _user_id: int, context_id: str | None = None) -> dict[str, Any]:
            seen.append(text)
            return {"decision": "block" if "坏" in text else "allow"}

        with patch.object(settings, "content_moderation_enabled", True), patch.object(moderation_service, "_call_text_moderation_for_user", moderate):
            self.assertEqual(await self._code(self._send(chain_text="坏话接龙")), (422, "chain_text_rejected"))
            self.assertEqual((await self._count(RedPacket), await self._count(Message), (await self._balance("alice"))["red_packet_sent"]), (0, 0, 0))
            await self._send(chain_text="好话接龙", amount=5, count=1)
        self.assertEqual(seen, ["坏话接龙", "好话接龙"])
        await self._assert_conserved()

    async def test_muted_members_cannot_claim_a_chain_packet(self) -> None:
        self.ledger.donate(OID_A, 300)
        chain = await self._send_id(amount=40, count=4, chain_text="谢谢老板")
        plain = await self._send_id(amount=10, count=1)
        async with self.sessions() as db:
            member = (await db.execute(select(ConversationMember).where(ConversationMember.conversation_id == self.group_id, ConversationMember.user_id == self.ids["bob"]))).scalar_one()
            member.muted = True
            await db.commit()
        before = await self._count(Message)
        self.assertEqual(await self._code(self._claim("bob", chain)), (403, "muted_in_group"))
        # 群内禁言不影响领普通红包（领了不用发言）
        await self._claim("bob", plain)
        # 全员禁言：普通成员领不了，群主不受限
        async with self.sessions() as db:
            (await db.get(Conversation, self.group_id)).all_muted = True
            await db.commit()
        self.assertEqual(await self._code(self._claim("carol", chain)), (403, "muted_in_group"))
        _result, message = await self._claim_with_message("dave", chain)
        self.assertEqual(message.sender_id, self.ids["dave"])
        # 平台禁言
        async with self.sessions() as db:
            (await db.get(Conversation, self.group_id)).all_muted = False
            (await db.get(User, self.ids["u00"])).muted_until = datetime.now(UTC) + timedelta(hours=1)
            await db.commit()
        self.assertEqual(await self._code(self._claim("u00", chain)), (403, "muted"))
        stored = await self._packet(chain)
        self.assertEqual(stored.claimed_count, 1)
        self.assertEqual(await self._count(Message), before + 1)
        await self._assert_conserved()

    async def test_a_failed_chain_message_rolls_the_claim_back(self) -> None:
        """预检之后、事务里才发现发不了言（这一刻刚被禁言）：这一份没领走，消息也没有，接口报错。"""
        self.ledger.donate(OID_A, 300)
        chain = await self._send_id(amount=30, count=3, chain_text="谢谢老板")
        async with self.sessions() as db:
            await db.execute(update(ConversationMember).where(ConversationMember.user_id == self.ids["bob"]).values(muted=True))
            await db.commit()
        before = await self._count(Message)
        with patch.object(rp, "_ensure_can_speak_in_group", AsyncMock()):
            self.assertEqual(await self._code(self._claim("bob", chain)), (403, "muted_in_group"))
        stored = await self._packet(chain)
        self.assertEqual((stored.claimed_count, stored.claimed_amount, await self._count(Message), await self._coins("bob")), (0, 0, before, 0))
        self.assertTrue(all(share.user_id is None for share in await self._shares(chain)))
        await self._assert_conserved()

    async def test_chain_marker_cannot_be_forged(self) -> None:
        """普通发消息带上 red_packet_chain：这一项被去掉，消息照常发。"""
        async with self.sessions() as db:
            message = await message_service.send_message(db, self.group_id, await db.get(User, self.ids["bob"]), "假的", MessageType.TEXT, {"red_packet_chain": {"red_packet_id": 1}})
            self.assertIsNone(message.metadata_json)


class ExclusiveTests(RedPacketTestCase):
    async def test_only_the_target_may_claim(self) -> None:
        self.ledger.donate(OID_A, 300)
        async with self.sessions() as db:
            await db.execute(update(ConversationMember).where(ConversationMember.user_id == self.ids["bob"]).values(nickname="小波"))
            await db.commit()
        sent = await self._send(kind="normal", amount=20, count=1, greeting="生日快乐", target_user_id=self.ids["bob"])
        packet, message = sent["red_packet"], sent["message"]
        self.assertEqual((packet["target_user_id"], packet["target"].username, packet["total"], packet["chain_text"]), (self.ids["bob"], "bob", 20, None))
        self.assertEqual(message.content, "[专属红包] 给 小波：生日快乐（更新到新版才能领取）")
        info = json.loads(message.metadata_json)["red_packet"]
        self.assertEqual((info["target_user_id"], info["target"]["username"], info["kind"], info["count"]), (self.ids["bob"], "bob", "normal", 1))
        self.assertNotIn("chain_text", info)
        # 别人（包括发的人）领：403 not_target；别人看详情知道是给谁的
        for name in ("carol", "alice", "dave"):
            self.assertEqual(await self._code(self._claim(name, packet["id"])), (403, "not_target"))
        seen = await self._get("carol", packet["id"])
        self.assertEqual((seen["target_user_id"], seen["target"].username, seen["total"]), (self.ids["bob"], "bob", None))
        claimed = await self._claim("bob", packet["id"])
        self.assertEqual((claimed["amount"], claimed["red_packet"]["status"]), (20, "finished"))
        mine = await self._as("alice", rp.list_mine, "sent", 20, 0)
        self.assertEqual(mine["items"][0]["target"].username, "bob")
        await self._assert_conserved()

    async def test_invalid_targets(self) -> None:
        self.ledger.donate(OID_A, 300)
        async with self.sessions() as db:
            bot = User(username="robot", hashed_password="x", is_bot=True)
            db.add(bot)
            await db.flush()
            db.add(ConversationMember(conversation_id=self.group_id, user_id=bot.id, role=MemberRole.MEMBER))
            await db.commit()
            bot_id = bot.id

        async def reason(**overrides: Any) -> tuple[int, str | None, str | None]:
            status_code, code, detail = await self._fails(self._send(**{"kind": "normal", "amount": 10, "count": 1, **overrides}))
            return status_code, code, detail.get("reason")

        self.assertEqual(await reason(conversation_id=self.direct_id, target_user_id=self.ids["bob"]), (422, "invalid_target", "direct"))
        self.assertEqual(await reason(target_user_id=self.ids["alice"]), (422, "invalid_target", "self"))
        self.assertEqual(await reason(target_user_id=self.ids["eve"]), (422, "invalid_target", "not_member"))
        self.assertEqual(await reason(target_user_id=999_999), (422, "invalid_target", "not_member"))
        self.assertEqual(await reason(target_user_id=bot_id), (422, "invalid_target", "bot"))
        self.assertEqual(await reason(target_user_id="bob"), (422, "invalid_target", "invalid"))
        self.assertEqual(await reason(target_user_id=self.ids["bob"], kind="lucky", amount=10, count=1), (422, "invalid_kind", None))
        self.assertEqual(await reason(target_user_id=self.ids["bob"], count=2), (422, "invalid_count", None))
        self.assertEqual((await self._count(RedPacket), await self._count(Message), (await self._balance("alice"))["red_packet_sent"]), (0, 0, 0))

    async def test_pending_counts_only_for_the_target(self) -> None:
        self.ledger.donate(OID_A, 300)
        exclusive = await self._send_id(kind="normal", amount=10, count=1, target_user_id=self.ids["bob"])

        async def pending(name: str) -> list[int]:
            return [packet.id for packet in await self._as(name, lambda db, user: rp.pending_in_conversation(db, user.id, self.group_id))]

        async def flagged(name: str) -> set[int]:
            async with self.sessions() as db:
                return await rp.pending_conversation_ids(db, self.ids[name])

        self.assertEqual((await pending("bob"), await pending("alice"), await pending("carol")), ([exclusive], [], []))
        self.assertEqual((await flagged("bob"), await flagged("alice"), await flagged("carol")), ({self.group_id}, set(), set()))
        async with self.sessions() as db:
            packets = await rp.pending_in_conversation(db, self.ids["bob"], self.group_id)
            items = await rp.pending_items(db, packets)
        self.assertEqual((items[0]["target_user_id"], items[0]["target"].username, items[0]["chain_text"]), (self.ids["bob"], "bob", None))
        await self._claim("bob", exclusive)
        self.assertEqual((await pending("bob"), await flagged("bob")), ([], set()))

    async def test_exclusive_chain_packet(self) -> None:
        self.ledger.donate(OID_A, 300)
        sent = await self._send(kind="normal", amount=10, count=1, target_user_id=self.ids["bob"], chain_text="谢谢姐")
        self.assertEqual(sent["message"].content, "[专属接龙红包] 给 bob：恭喜发财（更新到新版才能领取）")
        self.assertEqual(await self._code(self._claim("carol", sent["red_packet"]["id"])), (403, "not_target"))
        _result, message = await self._claim_with_message("bob", sent["red_packet"]["id"])
        self.assertEqual((message.sender_id, message.content), (self.ids["bob"], "谢谢姐"))
        await self._assert_conserved()

    async def test_admin_view_shows_the_new_fields(self) -> None:
        self.ledger.donate(OID_A, 300)
        group_packet = await self._send_id(kind="normal", amount=10, count=1, target_user_id=self.ids["bob"], chain_text="谢谢")
        direct_packet = await self._send_id(conversation_id=self.direct_id, kind="normal", amount=10, count=1, chain_text="私聊口令")
        async with self.sessions() as db:
            listed = {item["id"]: item for item in (await rp.admin_list(db, status_filter=None, kind=None, sender_id=None, conversation_id=None, created_from=None, created_to=None, limit=50, offset=0))["items"]}
            detail = await rp.admin_detail(db, group_packet)
        self.assertEqual((listed[group_packet]["chain"], listed[group_packet]["chain_text"], listed[group_packet]["target_user_id"], listed[group_packet]["target"]["username"]), (True, "谢谢", self.ids["bob"], "bob"))
        # 私聊的接龙口令和祝福语一样不给后台看
        self.assertEqual((listed[direct_packet]["chain"], listed[direct_packet]["chain_text"], listed[direct_packet]["target"]), (True, None, None))
        self.assertEqual(detail["target_user_id"], self.ids["bob"])


class HttpTests(RedPacketTestCase):
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

    async def test_chain_and_exclusive_over_http(self) -> None:
        self.ledger.donate(OID_A, 300)
        body = {"conversation_id": self.group_id, "kind": "normal", "amount": 10, "count": 1, "chain_text": "谢谢老板", "target_user_id": self.ids["bob"]}
        sent = await self._json("POST", "/red-packets", 201, json=body)
        packet = sent["red_packet"]
        self.assertEqual((packet["chain_text"], packet["target_user_id"], packet["target"]["username"]), ("谢谢老板", self.ids["bob"], "bob"))
        self.assertEqual(sent["message"]["metadata"]["red_packet"]["target"]["username"], "bob")
        self.acting = "carol"
        error = await self._json("POST", f"/red-packets/{packet['id']}/claim", 403)
        self.assertEqual(error["detail"]["code"], "not_target")
        self.assertEqual((await self._json("GET", f"/red-packets/{packet['id']}"))["target"]["username"], "bob")
        self.assertEqual((await self._json("GET", "/red-packets/pending", params={"conversation_id": self.group_id}))["items"], [])
        self.acting = "bob"
        pending = (await self._json("GET", "/red-packets/pending", params={"conversation_id": self.group_id}))["items"]
        self.assertEqual([(item["id"], item["chain_text"], item["target"]["username"]) for item in pending], [(packet["id"], "谢谢老板", "bob")])
        self.moderation.reset_mock()
        claimed = await self._json("POST", f"/red-packets/{packet['id']}/claim")
        message = claimed["message"]
        self.assertEqual((message["type"], message["content"], message["sender_id"], message["metadata"]), ("text", "谢谢老板", self.ids["bob"], {"red_packet_chain": {"red_packet_id": packet["id"]}}))
        # 照普通消息广播；不单独送异步审核（口令是发红包的人写的，已经审过）
        created = [data for _to, data in await self._events("message.created")]
        self.assertEqual(created[-1].id, message["id"])
        self.moderation.assert_not_called()
        listed = await self._json("GET", f"/conversations/{self.group_id}/messages")
        self.assertEqual(listed[-1]["id"], message["id"])

    async def test_user_flow(self) -> None:
        self.ledger.donate(OID_A, 300)
        config = await self._json("GET", "/red-packets/config")
        self.assertEqual(
            config,
            {"enabled": True, "limits": {"max_total": 500, "max_count": 100, "min_each": 1, "daily_send_limit": 20, "expire_hours": 24}, "sent_today": 0, "features": ["chain", "exclusive"]},
        )
        body = {"conversation_id": self.group_id, "kind": "lucky", "amount": 100, "count": 3, "greeting": "开工大吉"}
        # 不带结尾斜杠（契约的写法）；带了也不重定向
        sent = await self._json("POST", "/red-packets", 201, json=body)
        again = await self.client.post("/api/v1/red-packets/", json={**body, "amount": 10, "count": 1})
        self.assertEqual(again.status_code, 201, again.text)
        self.assertEqual(set(sent), {"red_packet", "message", "balance"})
        packet = sent["red_packet"]
        self.assertEqual(
            set(packet),
            {"id", "kind", "greeting", "sender", "chain_text", "voice", "voice_text", "target_user_id", "target", "conversation_id", "message_id", "total", "count", "claimed_count", "claimed_amount", "refunded_amount", "status", "my_claim", "expires_at", "created_at", "finished_at", "claims"},
        )
        self.assertEqual((packet["kind"], packet["greeting"], packet["sender"]["username"], packet["total"], packet["count"], packet["status"], packet["claims"]), ("lucky", "开工大吉", "alice", 100, 3, "active", []))
        message = sent["message"]
        self.assertEqual((message["id"], message["type"], message["content"], message["metadata"]), (packet["message_id"], "red_packet", "[红包] 开工大吉（更新到新版才能领取）", {"red_packet": {"id": packet["id"], "kind": "lucky", "greeting": "开工大吉", "count": 3}}))
        self.assertEqual((sent["balance"]["balance"], sent["balance"]["red_packet_sent"]), (200, 100))
        # 消息走正常的发消息广播，并且送去异步审核
        created = [data for _to, data in await self._events("message.created")]
        self.assertEqual([data.metadata["red_packet"]["id"] for data in created], [packet["id"], packet["id"] + 1])
        self.assertEqual(self.moderation.call_args_list[0].args, (message["id"],))
        # 广播出去的新消息一眼看得出是红包（客户端据此在会话列表上置「[有红包]」）
        self.assertEqual({data.type for data in created}, {MessageType.RED_PACKET})
        conversations = {item["id"]: item["red_packet_pending"] for item in await self._json("GET", "/conversations")}
        self.assertEqual(conversations, {self.group_id: True, self.direct_id: False})
        # 消息列表里就是一条普通的 red_packet 消息
        listed = await self._json("GET", f"/conversations/{self.group_id}/messages")
        self.assertEqual([(item["type"], item["metadata"]["red_packet"]["id"]) for item in listed], [("red_packet", packet["id"]), ("red_packet", packet["id"] + 1)])

        self.acting = "bob"
        hidden = await self._json("GET", f"/red-packets/{packet['id']}")
        self.assertEqual((hidden["total"], hidden["claimed_amount"], hidden["claims"], hidden["my_claim"], hidden["count"]), (None, None, None, None, 3))
        claimed = await self._json("POST", f"/red-packets/{packet['id']}/claim")
        self.assertEqual(set(claimed), {"amount", "red_packet", "balance", "message"})
        self.assertIsNone(claimed["message"])
        self.assertEqual((claimed["red_packet"]["my_claim"]["amount"], claimed["balance"]["balance"], claimed["red_packet"]["total"]), (claimed["amount"], claimed["amount"], 100))
        self.assertEqual([(claim["user"]["username"], claim["amount"], claim["is_luckiest"]) for claim in claimed["red_packet"]["claims"]], [("bob", claimed["amount"], False)])
        error = await self._json("POST", f"/red-packets/{packet['id']}/claim", 409)
        self.assertEqual((error["detail"]["code"], bool(error["detail"]["message"])), ("already_claimed", True))
        mine = await self._json("GET", "/red-packets/mine", params={"direction": "received"})
        self.assertEqual((mine["total"], mine["sum"], [(item["amount"], item["red_packet"]["id"], set(item)) for item in mine["items"]]), (1, claimed["amount"], [(claimed["amount"], packet["id"], {"amount", "created_at", "red_packet"})]))
        self.assertEqual((await self._json("GET", "/red-packets/mine", params={"direction": "sent"}))["total"], 0)
        self.acting = "alice"
        mine = await self._json("GET", "/red-packets/mine", params={"direction": "sent"})
        self.assertEqual((mine["total"], mine["sum"], [(item["id"], item["total"], item["refunded_amount"]) for item in mine["items"]]), (2, 110, [(packet["id"] + 1, 10, 0), (packet["id"], 100, 0)]))
        # 红包消息不能转发
        forwarded = await self._json("POST", f"/conversations/{self.direct_id}/forward-bundle", 422, json={"source_conversation_id": self.group_id, "message_ids": [packet["message_id"]]})
        self.assertEqual(forwarded["detail"]["code"], "red_packet_not_forwardable")
        self.acting = "bob"
        await self._json("GET", "/red-packets/mine", 422, params={"direction": "sideways"})
        # 错误体的形状
        self.assertEqual((await self._json("POST", "/red-packets", 402, json=body))["detail"]["code"], "insufficient_balance")
        self.assertEqual((await self._json("POST", "/red-packets", 422, json={**body, "kind": "gold"}))["detail"]["code"], "invalid_kind")
        self.assertEqual((await self._json("GET", "/red-packets/424242", 404))["detail"]["code"], "not_found")
        self.acting = "eve"
        self.assertEqual((await self._json("GET", f"/red-packets/{packet['id']}", 403))["detail"]["code"], "not_member")
        self.assertEqual((await self._json("POST", f"/red-packets/{packet['id']}/claim", 403))["detail"]["code"], "not_member")
        # 发送者撤回消息 = 提前结束
        self.acting = "alice"
        await self._json("POST", f"/conversations/{self.group_id}/messages/{packet['message_id']}/recall")
        ended = await self._json("GET", f"/red-packets/{packet['id']}")
        self.assertEqual((ended["status"], ended["claimed_count"]), ("revoked", 1))
        self.acting = "carol"
        self.assertEqual((await self._json("POST", f"/red-packets/{packet['id']}/claim", 410))["detail"]["code"], "revoked")
        # 普通的发消息接口发不了红包消息
        await self._json("POST", f"/conversations/{self.group_id}/messages", 422, json={"type": "red_packet", "content": "[红包] 假的"})

    async def test_admin_flow(self) -> None:
        self.ledger.donate(OID_A, 300)
        packet_id = await self._send_id(conversation_id=self.direct_id, kind="normal", amount=30, count=1, greeting="悄悄话")
        group_packet = await self._send_id(amount=60, count=3)
        for method, path in (
            ("GET", "/admin/red-packets/settings"),
            ("PUT", "/admin/red-packets/settings"),
            ("GET", "/admin/red-packets"),
            ("GET", f"/admin/red-packets/{packet_id}"),
            ("POST", f"/admin/red-packets/{packet_id}/revoke"),
        ):
            with self.subTest(path=path):
                await self._json(method, path, 403, json={"reason": "x"} if method != "GET" else None)
        self.acting = "admin"
        current = await self._json("GET", "/admin/red-packets/settings")
        self.assertEqual(set(current), {"enabled", "max_total", "max_count", "min_each", "daily_send_limit", "expire_hours", "bounds"})
        updated = await self._json("PUT", "/admin/red-packets/settings", json={"max_total": 300})
        self.assertEqual((updated["enabled"], updated["max_total"], updated["expire_hours"]), (True, 300, 24))
        self.assertEqual((await self._json("PUT", "/admin/red-packets/settings", 422, json={"max_count": 0}))["detail"]["code"], "invalid_settings")
        listing = await self._json("GET", "/admin/red-packets", params={"status": "active", "created_from": START.isoformat(), "created_to": (START + timedelta(hours=1)).isoformat()})
        self.assertEqual(set(listing), {"items", "total", "summary"})
        self.assertEqual((listing["total"], listing["summary"]), (2, {"total_sent": 90, "total_claimed": 0, "total_refunded": 0, "outstanding": 90}))
        self.assertEqual([(item["id"], item["conversation_type"], item["greeting"]) for item in listing["items"]], [(group_packet, "group", "恭喜发财"), (packet_id, "direct", None)])
        # 带结尾斜杠也不重定向
        self.assertEqual((await self.client.get("/api/v1/admin/red-packets/")).status_code, 200)
        await self._json("GET", "/admin/red-packets", 422, params={"status": "weird"})
        detail = await self._json("GET", f"/admin/red-packets/{packet_id}")
        self.assertEqual((detail["greeting"], detail["claims"], detail["total"]), (None, [], 30))
        self.assertNotIn("悄悄话", json.dumps([listing, detail], ensure_ascii=False))
        # reason 必填
        await self._json("POST", f"/admin/red-packets/{packet_id}/revoke", 422, json={})
        await self._json("POST", f"/admin/red-packets/{packet_id}/revoke", 422, json={"reason": ""})
        revoked = await self._json("POST", f"/admin/red-packets/{packet_id}/revoke", json={"reason": "误发"})
        self.assertEqual((revoked["id"], revoked["status"], revoked["refunded_amount"], revoked["revoke_reason"], revoked["greeting"], revoked["claims"]), (packet_id, "revoked", 30, "误发", None, []))
        self.assertEqual((await self._json("POST", f"/admin/red-packets/{packet_id}/revoke", 409, json={"reason": "误发"}))["detail"]["code"], "not_active")
        reconcile = await self._json("POST", "/admin/shop/reconcile")
        self.assertEqual(reconcile["red_packets"], {"total_sent": 90, "total_claimed": 0, "total_refunded": 30, "outstanding": 60, "balanced": True, "problems": []})
        accounts = await self._json("GET", "/admin/shop/accounts")
        self.assertEqual([(row["user"]["username"], row["red_packet_sent"], row["red_packet_received"], row["red_packet_refunded"], row["balance"]) for row in accounts["items"]], [("alice", 90, 0, 30, 240)])


# ---------------------------------------------------------------- 子进程入口（MultiProcessTests 用）


async def _worker(job: dict[str, Any]) -> Any:
    engine = make_engine(Path(job["db"]))
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.fromisoformat(job["now"])
    ledger = FakeCoinLedger()
    for oid, amount in job.get("donations", {}).items():
        ledger.donate(oid, amount)
    integrations.set_coin_ledger(ledger)
    shop_service._now = lambda: now
    rp._now = lambda: now
    shop_service.AsyncSessionLocal = sessions
    rp.AsyncSessionLocal = sessions

    async def claim(user_id: int) -> list[Any]:
        async with sessions() as db:
            try:
                result, _message = await rp.claim(db, await db.get(User, user_id), job["packet_id"])
                return [user_id, "ok", result["amount"]]
            except HTTPException as exc:
                return [user_id, exc.detail["code"], 0]

    async def send() -> str:
        payload = {"conversation_id": job["conversation_id"], "kind": "lucky", "amount": job["total"], "count": 3, "greeting": None}
        async with sessions() as db:
            try:
                await rp.send_red_packet(db, await db.get(User, job["user_id"]), payload)
                return "ok"
            except HTTPException as exc:
                return exc.detail["code"]

    async def purchase() -> str:
        async with sessions() as db:
            try:
                await shop_service.purchase(db, await db.get(User, job["user_id"]), job["item_id"])
                return "ok"
            except HTTPException as exc:
                return exc.detail["code"]

    # 先把连接建起来，再报告准备好了，等主进程放行：几个进程尽量同时开始
    async with sessions() as db:
        await db.scalar(select(func.count()).select_from(RedPacket))
    print("ready", flush=True)
    await asyncio.to_thread(sys.stdin.readline)
    try:
        if job["mode"] == "claim":
            return await asyncio.gather(*(claim(user_id) for user_id in job["user_ids"]))
        if job["mode"] == "send":
            return await asyncio.gather(*(send() for _ in range(job["sends"])))
        if job["mode"] == "spend":
            return await asyncio.gather(*(send() for _ in range(job["sends"])), purchase())
        async with sessions() as db:
            return await rp.sweep(db, now=now)
    finally:
        await shop_service.drain()
        await engine.dispose()


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "worker":
        # 测试跑在 PostgreSQL 上时（TEST_DATABASE_URL），子进程连父进程分到的同一个 schema；否则原样用 SQLite 文件
        from pg_redirect import for_worker

        create_async_engine = for_worker(create_async_engine)
        print(json.dumps(asyncio.run(_worker(json.loads(sys.argv[2])))), flush=True)
    else:
        unittest.main()
