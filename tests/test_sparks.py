"""好友火花（docs/sparks.md）。余额来源用假账本，不发任何外部请求。"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated
from unittest.mock import AsyncMock, Mock, patch

import httpx
from fastapi import Depends, FastAPI
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.models  # noqa: F401
from app import integrations
from app.core import localtime
from app.core.config import settings
from app.core.database import Base, configure_sqlite_connection, get_db
from app.core.dependencies import get_current_user
from app.models.conversation import Conversation, ConversationMember
from app.models.enums import ConversationType, MemberRole, MessageType
from app.models.friend import Friendship
from app.models.message import Message
from app.models.spark import FriendSpark, SparkRelight
from app.models.user import User
from app.services import intimacy as intimacy_service
from app.services import moderation as moderation_service
from app.services import shop as shop_service
from app.services import sparks as svc
from app.websocket.manager import manager
from integration_fakes import FakeCoinLedger

LOCAL = timezone(timedelta(hours=8))
D = date(2026, 10, 14)
OID_A = "a" * 24


def local(day: date, hour: int = 12, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=LOCAL).astimezone(UTC)


def spark_row(**values) -> FriendSpark:
    row = FriendSpark(user_low_id=1, user_high_id=2)
    defaults = dict(streak_days=0, best_streak=0, level=0, lost_days=0, lit_on=None, low_spoke_on=None, high_spoke_on=None, extinguished_at=None, relit_at=None, undo_json=None)
    defaults.update(values)
    for key, value in defaults.items():
        setattr(row, key, value)
    return row


class PureTests(unittest.TestCase):
    def test_levels(self) -> None:
        self.assertIsNone(svc.level_for(0))
        self.assertEqual([svc.level_for(n).name for n in (1, 2, 3, 6, 7, 29, 30, 99, 100, 364, 365, 9999)],
                         ["火苗", "火苗", "小火花", "小火花", "火花", "火花", "大火花", "大火花", "烈焰", "烈焰", "永恒之火", "永恒之火"])
        self.assertEqual(svc.next_level(5).min_days, 7)
        self.assertIsNone(svc.next_level(365))

    def test_status(self) -> None:
        self.assertEqual(svc.status_of(None, D), "none")
        self.assertEqual(svc.status_of(spark_row(lit_on=D, streak_days=3), D), "lit")
        self.assertEqual(svc.status_of(spark_row(lit_on=D - timedelta(days=1), streak_days=3), D), "pending")
        self.assertEqual(svc.status_of(spark_row(lit_on=D - timedelta(days=2), streak_days=3), D), "dying")
        out = spark_row(lit_on=D - timedelta(days=3), streak_days=3)
        self.assertEqual(svc.status_of(out, D), "out")
        self.assertEqual(svc.current_days(out, D), 0)
        # 熄灭的那天零点算起 3 天内可以重燃
        self.assertEqual(svc.restorable(out, D), (3, D))
        self.assertTrue(svc.relight_state(out, D, local(D + timedelta(days=2), 23))["can"])
        self.assertEqual(svc.relight_state(out, D, local(D + timedelta(days=3), 0))["block"], "expired")

    def test_continue_with_grace_and_restart(self) -> None:
        row = spark_row(lit_on=D - timedelta(days=2), streak_days=6, best_streak=6, level=2)
        # 漏了一天（快熄灭）今天续上：接着数，升到「火花」
        self.assertEqual(svc.continue_on(row, D), 3)
        self.assertEqual((row.streak_days, row.lit_on, row.best_streak), (7, D, 7))
        # 漏了两天：从 1 开始，原来的天数记下来
        row2 = spark_row(lit_on=D - timedelta(days=3), streak_days=9, best_streak=9, level=3)
        self.assertIsNone(svc.continue_on(row2, D))
        self.assertEqual((row2.streak_days, row2.lost_days, row2.level), (1, 9, 1))
        self.assertEqual(svc.restorable(row2, D), (9, D))
        # 撤回今天的续火：回到续之前
        self.assertTrue(svc.undo_today(row2, D))
        self.assertEqual((row2.streak_days, row2.lit_on, row2.lost_days, row2.level), (9, D - timedelta(days=3), 0, 3))


class SparkBase(unittest.IsolatedAsyncioTestCase):
    """alice、bob 是好友（私聊 direct），alice 和 dave 不是好友但有私聊；alice 绑定了 CCW。"""

    async def asyncSetUp(self) -> None:
        asyncio.get_running_loop().set_debug(False)
        shop_service.reset_runtime_state()
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{Path(self._tmp.name, 'db.sqlite').as_posix()}")
        event.listen(self.engine.sync_engine, "connect", configure_sqlite_connection)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        svc.session_factory = self.sessions
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.now = local(D, 12)
        self.ledger = FakeCoinLedger()
        integrations.set_coin_ledger(self.ledger)
        self.addCleanup(integrations.set_coin_ledger, None)
        self.send_to_user = AsyncMock()
        self.send_to_users = AsyncMock()

        async def everyone_online(user_ids) -> set[int]:
            return set(user_ids)

        for patcher in (
            patch.object(localtime, "_now", side_effect=lambda: self.now),
            patch.object(shop_service, "_now", side_effect=lambda: self.now),
            patch.object(shop_service, "AsyncSessionLocal", self.sessions),
            patch.object(manager, "send_to_user", self.send_to_user),
            patch.object(manager, "send_to_users", self.send_to_users),
            patch.object(manager, "online_user_ids_among", everyone_online),
            patch.object(settings, "shop_refresh_interval_seconds", 20),
            patch.object(moderation_service, "schedule_message_moderation", Mock()),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        async with self.sessions() as db:
            users = [User(username=name, hashed_password="x") for name in ("alice", "bob", "dave")]
            users[0].ccw_student_oid = OID_A
            users[1].nickname = "小鲍"
            db.add_all(users)
            await db.flush()
            self.ids = {user.username: user.id for user in users}
            a, b, d = (self.ids[n] for n in ("alice", "bob", "dave"))
            db.add_all([Friendship(user_id=a, friend_id=b), Friendship(user_id=b, friend_id=a)])
            self.conv = {}
            for name, (x, y) in {"bob": (a, b), "dave": (a, d)}.items():
                low, high = sorted((x, y))
                conv = Conversation(type=ConversationType.DIRECT, direct_key=f"{low}:{high}")
                db.add(conv)
                await db.flush()
                db.add_all([ConversationMember(conversation_id=conv.id, user_id=x, role=MemberRole.MEMBER), ConversationMember(conversation_id=conv.id, user_id=y, role=MemberRole.MEMBER)])
                self.conv[name] = conv.id
            await db.commit()

        self.acting = "alice"
        from app.api.v1.router import api_router

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

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        await svc.drain()
        await intimacy_service.drain()
        await shop_service.drain()
        svc.session_factory = None
        await self.engine.dispose()

    async def say(self, who: str, friend: str = "bob", at: datetime | None = None, kind: MessageType = MessageType.TEXT) -> svc.Change | None:
        """落一条消息（时间是 at / 现在），再记火花、发事件。"""
        when = at or self.now
        async with self.sessions() as db:
            message = Message(conversation_id=self.conv[friend], sender_id=self.ids[who], type=kind, content="hi", created_at=when)
            db.add(message)
            await db.commit()
            self.last_message_id = message.id
        async with self.sessions() as db:
            change = await svc.record_message(db, self.conv[friend], self.ids[who], kind, at=when)
            await svc.announce(db, change)
            return change

    async def both(self, day: date) -> None:
        self.now = local(day, 12)
        await self.say("alice")
        await self.say("bob")

    async def spark(self) -> FriendSpark | None:
        low, high = sorted((self.ids["alice"], self.ids["bob"]))
        async with self.sessions() as db:
            return (await db.execute(select(FriendSpark).where(FriendSpark.user_low_id == low, FriendSpark.user_high_id == high))).scalar_one_or_none()

    async def system_messages(self) -> list[Message]:
        async with self.sessions() as db:
            return list((await db.execute(select(Message).where(Message.conversation_id == self.conv["bob"], Message.type == MessageType.SYSTEM).order_by(Message.id))).scalars())

    def events(self, user: str) -> list[dict]:
        return [call.args[2] for call in self.send_to_user.call_args_list if call.args[0] == self.ids[user] and call.args[1] == svc.EVENT_UPDATED]


class ServiceTests(SparkBase):
    async def test_both_sides_needed_once_per_day(self) -> None:
        change = await self.say("alice")
        self.assertEqual(change.reason, "spoke")
        row = await self.spark()
        self.assertEqual((row.streak_days, row.lit_on), (0, None))
        # 同一个人当天再发：不写库、不发事件
        before = len(self.send_to_user.call_args_list)
        self.assertIsNone(await self.say("alice"))
        self.assertEqual(len(self.send_to_user.call_args_list), before)
        change = await self.say("bob")
        self.assertEqual(change.reason, "lit")
        row = await self.spark()
        self.assertEqual((row.streak_days, row.lit_on, row.best_streak, row.level), (1, D, 1, 1))
        await self.say("bob")
        await self.say("alice")
        self.assertEqual((await self.spark()).streak_days, 1)
        # 双方各收一份，视角各自的
        alice_view = self.events("alice")[-1]
        self.assertEqual((alice_view["friend_id"], alice_view["spark"]["days"], alice_view["spark"]["status"]), (self.ids["bob"], 1, "lit"))
        self.assertEqual(self.events("bob")[-1]["friend_id"], self.ids["alice"])
        # 火苗（1 级）不发系统提示
        self.assertEqual(await self.system_messages(), [])

    async def test_non_friend_system_and_group_do_not_count(self) -> None:
        async with self.sessions() as db:
            self.assertIsNone(await svc.record_message(db, self.conv["dave"], self.ids["alice"], MessageType.TEXT, at=self.now))
            self.assertIsNone(await svc.record_message(db, self.conv["bob"], self.ids["alice"], MessageType.SYSTEM, at=self.now))
            self.assertIsNone(await svc.record_message(db, self.conv["bob"], self.ids["dave"], MessageType.TEXT, at=self.now))
        async with self.sessions() as db:
            self.assertEqual(await db.scalar(select(func.count()).select_from(FriendSpark)), 0)
        # 送礼、发红包这类消息也算发言
        self.assertEqual((await self.say("alice", kind=MessageType.GIFT)).reason, "spoke")

    async def test_utc_plus_8_day_boundary(self) -> None:
        await self.say("alice", at=local(D, 23, 59))
        await self.say("bob", at=local(D + timedelta(days=1), 0, 1))
        row = await self.spark()
        self.assertIsNone(row.lit_on)

    async def test_streak_dying_level_up_and_milestone(self) -> None:
        await self.both(D)
        await self.both(D + timedelta(days=1))
        # 漏一天：快熄灭
        self.now = local(D + timedelta(days=3), 9)
        async with self.sessions() as db:
            item = await svc.view_for(db, self.ids["alice"], self.ids["bob"])
        self.assertEqual((item["status"], item["days"], item["dying"], item["today_lit"]), ("dying", 2, True, False))
        # 今天续上：接着数，第 3 天升级「小火花」，私聊里一条系统提示，双方实时收到
        await self.both(D + timedelta(days=3))
        row = await self.spark()
        self.assertEqual((row.streak_days, row.level, row.best_streak), (3, 2, 3))
        messages = await self.system_messages()
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].content, "你们的火花升级为「小火花」，已经连续 3 天")
        meta = json.loads(messages[0].metadata_json)
        self.assertEqual((meta["system_event"], meta["spark"]["level_key"], meta["spark"]["days"]), ("spark.level_up", "spark", 3))
        created = [call for call in self.send_to_users.call_args_list if call.args[1] == "message.created"]
        self.assertEqual(set(created[-1].args[0]), {self.ids["alice"], self.ids["bob"]})
        # 系统消息本身不算发言
        self.assertEqual((await self.spark()).streak_days, 3)

    async def test_recall_only_message_undoes_today(self) -> None:
        await self.both(D - timedelta(days=1))
        self.now = local(D, 12)
        await self.say("alice")
        await self.say("bob")
        self.assertEqual((await self.spark()).streak_days, 2)
        bob_message = self.last_message_id
        async with self.sessions() as db:
            message = await db.get(Message, bob_message)
            message.recalled_at = self.now
            await db.commit()
        async with self.sessions() as db:
            change = await svc.record_recall(db, bob_message)
        self.assertEqual(change.reason, "recalled")
        row = await self.spark()
        self.assertEqual((row.streak_days, row.lit_on, row.high_spoke_on if self.ids["bob"] > self.ids["alice"] else row.low_spoke_on), (1, D - timedelta(days=1), D - timedelta(days=1)))
        # 再发一条：今天又续上
        self.assertEqual((await self.say("bob")).reason, "lit")
        self.assertEqual((await self.spark()).streak_days, 2)

    async def test_recall_with_other_messages_left_changes_nothing(self) -> None:
        await self.say("alice")
        first = self.last_message_id
        await self.say("alice")
        await self.say("bob")
        async with self.sessions() as db:
            message = await db.get(Message, first)
            message.recalled_at = self.now
            await db.commit()
        async with self.sessions() as db:
            self.assertIsNone(await svc.record_recall(db, first))
        self.assertEqual((await self.spark()).streak_days, 1)

    async def test_extinguish_sweep_and_restart(self) -> None:
        for offset in range(4):
            await self.both(D + timedelta(days=offset))
        # 最后续上是 D+3；D+4、D+5 都没续 → D+6 零点熄灭
        self.now = local(D + timedelta(days=6), 0, 5)
        async with self.sessions() as db:
            self.assertEqual(await svc.sweep(db), 1)
            self.assertEqual(await svc.sweep(db), 0)
        row = await self.spark()
        self.assertEqual((row.streak_days, row.lost_days, row.level, row.best_streak), (0, 4, 0, 4))
        self.assertEqual(svc._aware(row.extinguished_at), local(D + timedelta(days=6), 0))
        event_data = self.events("alice")[-1]
        self.assertEqual((event_data["reason"], event_data["spark"]["status"], event_data["spark"]["can_relight"]), ("extinguished", "out", True))
        # 熄灭后又聊起来：从 1 开始，原来的 4 天仍然可以重燃
        await self.both(D + timedelta(days=7))
        async with self.sessions() as db:
            item = await svc.view_for(db, self.ids["alice"], self.ids["bob"])
        self.assertEqual((item["days"], item["lost_days"], item["can_relight"]), (1, 4, True))


class ApiTests(SparkBase):
    async def burn_out(self) -> None:
        for offset in range(7):
            await self.both(D + timedelta(days=offset))
        # D+6 是第 7 天（火花）。D+7、D+8 没续，D+9 熄灭
        self.now = local(D + timedelta(days=9), 10)

    async def test_detail_and_conversation_list(self) -> None:
        await self.both(D)
        response = await self.client.get(f"/api/v1/friends/{self.ids['bob']}/spark")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual((body["conversation_id"], body["spark"]["days"], body["spark"]["level_key"], body["spark"]["days_to_next"]), (self.conv["bob"], 1, "ember", 2))
        self.assertEqual([lv["min_days"] for lv in body["rules"]["levels"]], [1, 3, 7, 30, 100, 365])
        self.assertEqual((await self.client.get(f"/api/v1/friends/{self.ids['dave']}/spark")).json()["detail"]["code"], "not_friend")
        conversations = (await self.client.get("/api/v1/conversations")).json()
        by_id = {item["id"]: item for item in conversations}
        self.assertEqual(by_id[self.conv["bob"]]["spark"]["days"], 1)
        self.assertIsNone(by_id[self.conv["dave"]]["spark"])
        mine = (await self.client.get("/api/v1/sparks")).json()["items"]
        self.assertEqual([(item["friend_id"], item["spark"]["days"]) for item in mine], [(self.ids["bob"], 1)])
        intimacy = (await self.client.get(f"/api/v1/intimacy/friends/{self.ids['bob']}")).json()
        self.assertEqual(intimacy["spark"]["days"], 1)

    async def test_relight_charges_and_restores(self) -> None:
        await self.burn_out()
        self.ledger.donate(OID_A, 250)
        # 还没熄灭的时候不能重燃
        self.now = local(D + timedelta(days=8), 10)
        response = await self.client.post(f"/api/v1/friends/{self.ids['bob']}/spark/relight")
        self.assertEqual((response.status_code, response.json()["detail"]["reason"]), (409, "not_out"))
        self.now = local(D + timedelta(days=9), 10)
        response = await self.client.post(f"/api/v1/friends/{self.ids['bob']}/spark/relight")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual((body["restored_days"], body["spark"]["days"], body["spark"]["status"], body["spark"]["level_key"]), (7, 7, "pending", "flame"))
        self.assertEqual((body["balance"]["balance"], body["balance"]["spark_spent"]), (150, 100))
        async with self.sessions() as db:
            self.assertEqual(await db.scalar(select(func.sum(SparkRelight.amount))), 100)
        messages = await self.system_messages()
        self.assertTrue(messages[-1].content.endswith("重燃了你们的火花，连续 7 天"))
        # 今天续上：第 8 天
        await self.both(D + timedelta(days=9))
        self.assertEqual((await self.spark()).streak_days, 8)
        # 30 天内只能重燃一次
        # 又熄灭了（最后续上 D+9，D+12 熄灭），还在 3 天期限里，但 30 天内重燃过
        self.now = local(D + timedelta(days=13), 10)
        response = await self.client.post(f"/api/v1/friends/{self.ids['bob']}/spark/relight")
        self.assertEqual((response.status_code, response.json()["detail"]["reason"]), (409, "cooldown"))

    async def test_relight_needs_coins_and_window(self) -> None:
        await self.burn_out()
        self.ledger.donate(OID_A, 99)
        response = await self.client.post(f"/api/v1/friends/{self.ids['bob']}/spark/relight")
        self.assertEqual((response.status_code, response.json()["detail"]["code"], response.json()["detail"]["shortfall"]), (402, "insufficient_balance", 1))
        async with self.sessions() as db:
            self.assertEqual(await db.scalar(select(func.count()).select_from(SparkRelight)), 0)
        # bob 没绑定 CCW
        self.acting = "bob"
        response = await self.client.post(f"/api/v1/friends/{self.ids['alice']}/spark/relight")
        self.assertEqual((response.status_code, response.json()["detail"]["code"]), (403, "ccw_not_bound"))
        # 熄灭超过 3 天
        self.acting = "alice"
        self.ledger.donate(OID_A, 500)
        self.now = local(D + timedelta(days=12), 0, 1)
        response = await self.client.post(f"/api/v1/friends/{self.ids['bob']}/spark/relight")
        self.assertEqual((response.status_code, response.json()["detail"]["reason"]), (409, "expired"))

    async def test_send_endpoint_schedules_spark(self) -> None:
        with patch.object(svc, "schedule_message") as scheduled:
            response = await self.client.post(f"/api/v1/conversations/{self.conv['bob']}/messages", json={"content": "在吗", "type": "text"})
        self.assertEqual(response.status_code, 201, response.text)
        scheduled.assert_called_once()
        self.assertEqual(scheduled.call_args.args[:2], (self.conv["bob"], self.ids["alice"]))


if __name__ == "__main__":
    unittest.main()
