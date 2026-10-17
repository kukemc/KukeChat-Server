"""群升级、群文件、永久精华、群待办、聊天记录搜索的测试（docs/group-plus.md）。余额来源用假账本，不发任何外部请求。"""

from __future__ import annotations

import asyncio
import json
import tempfile
import time
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any
from unittest.mock import AsyncMock, Mock, patch

import httpx
from fastapi import Depends, FastAPI
from sqlalchemy import event, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.models  # noqa: F401
from app import integrations
from app.core import group_plus_tiers
from app.core import localtime
from app.core.config import settings
from app.core.database import Base, configure_sqlite_connection, get_db
from app.core.dependencies import get_current_user
from app.models.conversation import Conversation, ConversationMember
from app.models.enums import ConversationType, MemberRole, MessageType, PlatformRole
from app.models.group_plus import GroupFile, GroupFileFolder, GroupFileUpload, GroupPlusRecord, GroupTodo
from app.models.message import FeaturedMessage, MentionAllUsage, Message, MessageVisibility
from app.models.report import AdminAuditLog
from app.models.user import User
from app.services import group_files as file_service
from app.services import group_plus as plus_service
from app.services import messages as message_service
from app.services import moderation as moderation_service
from app.services import shop as shop_service
from app.services.schema_extras import install_sqlite_extras
from app.websocket.manager import manager
from pg_redirect import requires_sqlite
from integration_fakes import FakeCoinLedger

OID_A, OID_B, OID_D = "a" * 24, "b" * 24, "d" * 24
START = datetime(2026, 10, 12, 8, 0, tzinfo=UTC)
DAY = timedelta(days=1)


def make_engine(path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{path.as_posix()}")
    event.listen(engine.sync_engine, "connect", configure_sqlite_connection)
    return engine


class PlusCase(unittest.IsolatedAsyncioTestCase):
    """alice 群主、bob 管理员（都绑定 CCW）、carol / dave 成员（dave 绑定）、frank 管理员但没绑定、eve 不在群里、root 平台管理员。
    alice 和 carol 之间有一个私聊；另有 eve 和 carol 的私聊（alice 不在里面）。"""

    async def asyncSetUp(self) -> None:
        shop_service.reset_runtime_state()
        plus_service.reset_runtime_state()
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.engine = make_engine(Path(self.tmp.name) / "plus.db")
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with self.sessions() as db:
            users = [User(username=name, hashed_password="x") for name in ("alice", "bob", "carol", "dave", "eve", "frank", "root")]
            users[0].ccw_student_oid, users[1].ccw_student_oid, users[3].ccw_student_oid = OID_A, OID_B, OID_D
            users[2].nickname = "卡罗"
            users[6].platform_role = PlatformRole.ADMIN
            db.add_all(users)
            await db.flush()
            self.ids = {user.username: user.id for user in users}
            # id 1 是官方群（不限人数），先占掉
            db.add(Conversation(type=ConversationType.GROUP, title="官方群"))
            await db.flush()
            group = Conversation(type=ConversationType.GROUP, title="测试群")
            direct = Conversation(type=ConversationType.DIRECT)
            other = Conversation(type=ConversationType.DIRECT)
            db.add_all([group, direct, other])
            await db.flush()
            self.group_id, self.direct_id, self.other_id = group.id, direct.id, other.id
            roles = {"alice": MemberRole.OWNER, "bob": MemberRole.ADMIN, "frank": MemberRole.ADMIN, "carol": MemberRole.MEMBER, "dave": MemberRole.MEMBER}
            for name, role in roles.items():
                db.add(ConversationMember(conversation_id=group.id, user_id=self.ids[name], role=role))
            for name in ("alice", "carol"):
                db.add(ConversationMember(conversation_id=direct.id, user_id=self.ids[name], role=MemberRole.MEMBER))
            for name in ("eve", "carol"):
                db.add(ConversationMember(conversation_id=other.id, user_id=self.ids[name], role=MemberRole.MEMBER))
            await db.commit()

        self.now = START
        self.ledger = FakeCoinLedger()
        integrations.set_coin_ledger(self.ledger)
        self.addCleanup(integrations.set_coin_ledger, None)
        self.send_to_users = AsyncMock()

        async def everyone_online(user_ids: list[int]) -> set[int]:
            return set(user_ids)

        upload_dir = str(Path(self.tmp.name) / "uploads")
        for patcher in (
            patch.object(group_plus_tiers, "_now", lambda: self.now),
            patch.object(shop_service, "_now", lambda: self.now),
            patch.object(localtime, "_now", lambda: self.now),
            patch.object(shop_service, "AsyncSessionLocal", self.sessions),
            patch.object(file_service, "AsyncSessionLocal", self.sessions),
            patch.object(manager, "send_to_user", AsyncMock()),
            patch.object(manager, "send_to_users", self.send_to_users),
            patch.object(manager, "online_user_ids_among", everyone_online),
            patch.object(settings, "shop_refresh_interval_seconds", 20),
            patch.object(settings, "upload_dir", upload_dir),
            patch.object(settings, "storage_backend", "local"),
            patch.object(moderation_service, "schedule_message_moderation", Mock()),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

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

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        await shop_service.drain()
        await self.engine.dispose()

    # ------------------------------------------------------------ 工具

    async def call(self, method: str, path: str, *, as_: str | None = None, expected: int | None = None, **kwargs: Any) -> httpx.Response:
        if as_ is not None:
            self.acting = as_
        response = await self.client.request(method, f"/api/v1{path}", **kwargs)
        if expected is not None:
            self.assertEqual(response.status_code, expected, response.text)
        return response

    async def json(self, method: str, path: str, *, as_: str = "alice", expected: int = 200, **kwargs: Any) -> Any:
        return (await self.call(method, path, as_=as_, expected=expected, **kwargs)).json()

    async def code(self, method: str, path: str, status_code: int, *, as_: str = "alice", **kwargs: Any) -> dict[str, Any]:
        body = await self.json(method, path, as_=as_, expected=status_code, **kwargs)
        return body["detail"] if isinstance(body.get("detail"), dict) else {"code": None, "message": body.get("detail")}

    def tick(self, *, days: float = 0, hours: float = 0, seconds: float = 0) -> None:
        self.now = self.now + timedelta(days=days, hours=hours, seconds=seconds)

    async def buy(self, tier: str, period: str = "month", *, as_: str = "alice", gid: int | None = None, **extra: Any) -> dict[str, Any]:
        return await self.json(
            "POST", f"/conversations/{gid or self.group_id}/plus/purchase", as_=as_, json={"tier": tier, "period": period, **extra}
        )

    async def grant(self, tier: str, days: int | None = None, gid: int | None = None) -> dict[str, Any]:
        body = {"conversation_id": gid or self.group_id, "tier": tier}
        if days is not None:
            body["days"] = days
        return await self.json("POST", "/admin/group-plus/grant", as_="root", json=body)

    async def conversation(self, gid: int | None = None) -> Conversation:
        async with self.sessions() as db:
            return await db.get(Conversation, gid or self.group_id, populate_existing=True)

    async def coins(self, name: str) -> int:
        async with self.sessions() as db:
            return (await shop_service.balance_of(db, self.ids[name]))["balance"]

    async def add_messages(self, count: int, *, sender: str = "alice", gid: int | None = None, prefix: str = "消息", **fields: Any) -> list[int]:
        async with self.sessions() as db:
            rows = [
                Message(conversation_id=gid or self.group_id, sender_id=self.ids[sender], content=f"{prefix}{index}", **fields) for index in range(count)
            ]
            db.add_all(rows)
            await db.commit()
            return [row.id for row in rows]

    async def count(self, model: Any) -> int:
        async with self.sessions() as db:
            return int(await db.scalar(select(func.count()).select_from(model)) or 0)

    async def audit(self) -> list[str]:
        async with self.sessions() as db:
            return [row.action for row in (await db.execute(select(AdminAuditLog).order_by(AdminAuditLog.id))).scalars().all()]


# ---------------------------------------------------------------- 档位、购买、余额


class PurchaseTests(PlusCase):
    async def test_plans_and_status(self) -> None:
        plans = await self.json("GET", "/group-plus/plans")
        by_key = {tier["key"]: tier for tier in plans["tiers"]}
        self.assertEqual([tier["key"] for tier in plans["tiers"]], ["free", "pro", "pro_plus", "max"])
        self.assertEqual(
            [(by_key[k]["member_limit"], by_key[k]["message_limit"], by_key[k]["file_quota_bytes"], by_key[k]["featured_limit"]) for k in by_key],
            [(200, 1000, None, 100), (500, 5000, 0, 300), (1000, 20000, 200 * 1024 * 1024, 800), (3000, 50000, 1024**3, 3000)],
        )
        # 群文件：Pro 起解锁；Pro 没有空间只能传临时文件
        self.assertEqual([by_key[k]["file_enabled"] for k in by_key], [False, True, True, True])
        self.assertEqual([by_key[k]["file_temp_max_bytes"] for k in by_key], [None, 10 * 1024 * 1024, 30 * 1024 * 1024, 100 * 1024 * 1024])
        self.assertEqual(plans["file_temp_ttl_hours"], 24)
        self.assertEqual([(by_key[k]["price_month"], by_key[k]["price_year"]) for k in ("pro", "pro_plus", "max")], [(80, 800), (200, 2000), (500, 5000)])
        self.assertEqual((plans["file_max_bytes"], plans["file_ttl_options_hours"], plans["year_months_paid"]), (100 * 1024 * 1024, [24, 168, 720], 10))
        status = await self.json("GET", f"/conversations/{self.group_id}/plus")
        self.assertEqual((status["tier"], status["expires_at"], status["files"]["state"], status["can_purchase"]), ("free", None, "locked", True))
        self.assertEqual(status["usage"]["member_count"], 5)
        carol = await self.json("GET", f"/conversations/{self.group_id}/plus", as_="carol")
        self.assertFalse(carol["can_purchase"])
        await self.json("GET", f"/conversations/{self.group_id}/plus", as_="eve", expected=404)

    async def test_new_renew_upgrade_and_downgrade(self) -> None:
        self.ledger.donate(OID_A, 10000)
        quote = await self.json("POST", f"/conversations/{self.group_id}/plus/quote", json={"tier": "pro", "period": "month"})
        self.assertEqual((quote["action"], quote["price"], quote["days"], quote["forfeited_days"], quote["forfeit_notice"]), ("new", 80, 30, 0, None))
        self.assertEqual(quote["new_expires_at"], (START + 30 * DAY).isoformat().replace("+00:00", "Z"))
        bought = await self.buy("pro")
        self.assertEqual((bought["conversation"]["plus_tier"], bought["balance"]["balance"], bought["balance"]["group_plus_spent"]), ("pro", 9920, 80))
        self.assertEqual((bought["record"]["action"], bought["record"]["amount"], bought["record"]["user"]["username"]), ("new", 80, "alice"))
        # 会话对象上的角标字段
        listing = await self.json("GET", "/conversations")
        mine = next(item for item in listing if item["id"] == self.group_id)
        self.assertEqual((mine["plus_tier"], mine["plus_expires_at"] is not None), ("pro", True))
        self.assertEqual(next(item for item in listing if item["id"] == self.direct_id)["plus_tier"], "free")

        self.tick(days=5)
        renewed = await self.buy("pro", "year")
        # 续期：年费 = 10 个月价格，在原到期时间上加 365 天
        self.assertEqual((renewed["record"]["action"], renewed["record"]["amount"], renewed["balance"]["balance"]), ("renew", 800, 9120))
        conversation = await self.conversation()
        self.assertEqual(group_plus_tiers.aware(conversation.plus_expires_at), START + 30 * DAY + 365 * DAY)

        # 升级：剩余时间作废，要确认
        quote = await self.json("POST", f"/conversations/{self.group_id}/plus/quote", json={"tier": "max", "period": "month"})
        self.assertEqual((quote["action"], quote["forfeited_days"] > 300, quote["forfeit_notice"] is not None, quote["enough"]), ("upgrade", True, True, True))
        refused = await self.code("POST", f"/conversations/{self.group_id}/plus/purchase", 409, json={"tier": "max", "period": "month"})
        self.assertEqual(refused["code"], "forfeit_not_confirmed")
        self.assertEqual(await self.coins("alice"), 9120)
        changed = await self.code("POST", f"/conversations/{self.group_id}/plus/purchase", 409, json={"tier": "max", "period": "month", "confirm_forfeit": True, "expected_price": 1})
        self.assertEqual((changed["code"], changed["price"]), ("price_changed", 500))
        upgraded = await self.buy("max", confirm_forfeit=True, expected_price=500)
        self.assertEqual((upgraded["record"]["action"], upgraded["balance"]["balance"], upgraded["plus"]["tier"]), ("upgrade", 8620, "max"))
        conversation = await self.conversation()
        self.assertEqual(group_plus_tiers.aware(conversation.plus_expires_at), self.now + 30 * DAY)

        # 降级：当前档位到期前不允许，到期之后随便买
        denied = await self.code("POST", f"/conversations/{self.group_id}/plus/purchase", 422, json={"tier": "pro", "period": "month"})
        self.assertEqual((denied["code"], denied["current_tier"]), ("downgrade_not_allowed", "max"))
        self.assertEqual(await self.coins("alice"), 8620)
        self.tick(days=31)
        again = await self.buy("pro")
        self.assertEqual((again["record"]["action"], again["record"]["prev_tier"], again["conversation"]["plus_tier"]), ("new", "free", "pro"))

    async def test_rejections_never_charge(self) -> None:
        self.ledger.donate(OID_A, 50)
        self.ledger.donate(OID_B, 5000)
        path = f"/conversations/{self.group_id}/plus/purchase"
        self.assertEqual((await self.code("POST", path, 403, as_="carol", json={"tier": "pro", "period": "month"}))["code"], "not_group_staff")
        await self.json("POST", path, as_="eve", expected=404, json={"tier": "pro", "period": "month"})
        self.assertEqual((await self.code("POST", f"/conversations/{self.direct_id}/plus/purchase", 400, json={"tier": "pro", "period": "month"}))["code"], "not_group")
        self.assertEqual((await self.code("POST", path, 422, json={"tier": "free", "period": "month"}))["code"], "invalid_tier")
        self.assertEqual((await self.code("POST", path, 422, json={"tier": "pro", "period": "week"}))["code"], "invalid_period")
        self.assertEqual((await self.code("POST", path, 403, as_="frank", json={"tier": "pro", "period": "month"}))["code"], "ccw_not_bound")
        short = await self.code("POST", path, 402, as_="alice", json={"tier": "pro", "period": "month"})
        self.assertEqual((short["code"], short["balance"], short["total"], short["shortfall"]), ("insufficient_balance", 50, 80, 30))
        self.ledger.error = RuntimeError("down")
        self.assertEqual((await self.code("POST", path, 503, as_="bob", json={"tier": "pro", "period": "month"}))["code"], "verification_unavailable")
        self.ledger.error = None
        self.assertEqual((await self.conversation()).plus_tier, "free")
        self.assertEqual(await self.count(GroupPlusRecord), 0)
        # 关掉购买
        await self.json("PUT", "/admin/group-plus/settings", as_="root", json={"enabled": False})
        self.assertEqual((await self.code("POST", path, 403, as_="bob", json={"tier": "pro", "period": "month"}))["code"], "disabled")

    async def test_no_double_spend_and_rebinding(self) -> None:
        self.ledger.donate(OID_A, 80)
        path = f"/conversations/{self.group_id}/plus/purchase"
        # 币只够买一次：同时来两个，只有一个成功
        results = await asyncio.gather(
            self.client.post(f"/api/v1{path}", json={"tier": "pro", "period": "month"}),
            self.client.post(f"/api/v1{path}", json={"tier": "pro", "period": "month"}),
        )
        self.assertEqual(sorted(response.status_code for response in results), [200, 402])
        self.assertEqual(await self.count(GroupPlusRecord), 1)
        self.assertEqual(await self.coins("alice"), 0)
        # 同一个 CCW 账号换绑到别的 KukeChat 账号（dave 当管理员）：已经花掉的不能再花一遍
        async with self.sessions() as db:
            alice = await db.get(User, self.ids["alice"])
            dave = await db.get(User, self.ids["dave"])
            alice.ccw_student_oid = None
            dave.ccw_student_oid = OID_A
            member = (
                await db.execute(select(ConversationMember).where(ConversationMember.conversation_id == self.group_id, ConversationMember.user_id == dave.id))
            ).scalar_one()
            member.role = MemberRole.ADMIN
            await db.commit()
        short = await self.code("POST", path, 402, as_="dave", json={"tier": "pro", "period": "month"})
        self.assertEqual(short["balance"], 0)
        self.ledger.donate(OID_A, 250)
        self.tick(seconds=60)
        # 250 − 已经在别的账号上花掉的 80 = 170，买不起 200 的 Pro+
        self.assertEqual((await self.code("POST", path, 402, as_="dave", json={"tier": "pro_plus", "period": "month", "confirm_forfeit": True}))["balance"], 170)
        bought = await self.buy("pro", "month", as_="dave", gid=self.group_id, confirm_forfeit=True)
        self.assertEqual((bought["balance"]["balance"], bought["balance"]["spent"], bought["balance"]["group_plus_spent"]), (90, 80, 80))

    async def test_ledger_check_and_reconcile_shape(self) -> None:
        self.ledger.donate(OID_A, 5000)
        await self.buy("pro")
        await self.grant("max", 10, gid=self.group_id)
        async with self.sessions() as db:
            check = await plus_service.ledger_check(db)
        self.assertEqual((check["total_amount"], check["total_count"], check["granted_count"], check["balanced"]), (80, 1, 1, True))
        # 群的状态和最后一条记录对不上 → 不平
        async with self.sessions() as db:
            conversation = await db.get(Conversation, self.group_id)
            conversation.plus_tier = "pro"
            await db.commit()
            check = await plus_service.ledger_check(db)
        self.assertFalse(check["balanced"])
        self.assertEqual(check["problems"][0]["conversation_id"], self.group_id)
        async with self.sessions() as db:
            result = await shop_service.admin_reconcile(db, await db.get(User, self.ids["root"]))
        self.assertIn("group_plus", result)


# ---------------------------------------------------------------- 档位的效果：人数、修剪、精华


class LimitTests(PlusCase):
    async def test_member_capacity_follows_the_tier(self) -> None:
        async with self.sessions() as db:
            users = [User(username=f"u{i}", hashed_password="x") for i in range(196)]
            db.add_all(users)
            await db.flush()
            db.add_all([ConversationMember(conversation_id=self.group_id, user_id=user.id) for user in users])
            extra = [User(username=f"x{i}", hashed_password="x") for i in range(3)]
            db.add_all(extra)
            await db.flush()
            await db.commit()
            extra_ids = [user.id for user in extra]
        self.assertEqual((await self.json("GET", f"/conversations/{self.group_id}/plus"))["usage"]["member_count"], 201)
        full = await self.code("POST", f"/conversations/{self.group_id}/members", 409, json={"user_ids": [extra_ids[0]]})
        self.assertEqual((full["code"], full["limit"], full["tier"]), ("group_full", 200, "free"))
        await self.grant("pro", 30)
        await self.json("POST", f"/conversations/{self.group_id}/members", json={"user_ids": [extra_ids[0]]}, expected=200)
        # 到期降回免费：已有成员不踢，但不能再加
        self.tick(days=31)
        full = await self.code("POST", f"/conversations/{self.group_id}/members", 409, json={"user_ids": [extra_ids[1]]})
        self.assertEqual(full["code"], "group_full")
        self.assertEqual((await self.json("GET", f"/conversations/{self.group_id}/plus"))["usage"]["member_count"], 202)

    async def test_message_retention_follows_the_tier(self) -> None:
        with patch.object(message_service, "MAX_MESSAGES_PER_CONVERSATION", 10), patch.object(message_service, "PRUNE_BATCH_SIZE", 100), patch.dict(
            group_plus_tiers.TIERS["pro"], {"message_limit": 30}
        ):
            await self.add_messages(25)
            await self.grant("pro", 30)
            async with self.sessions() as db:
                await message_service._prune_conversation_messages(db, self.group_id)
                await db.commit()
            self.assertEqual(await self.count(Message), 25)
            self.tick(days=31)
            async with self.sessions() as db:
                await message_service._prune_conversation_messages(db, self.group_id)
                await db.commit()
            self.assertEqual(await self.count(Message), 10)
            # 私聊始终是默认条数
            await self.add_messages(15, gid=self.direct_id)
            async with self.sessions() as db:
                await message_service._prune_conversation_messages(db, self.direct_id)
                await db.commit()
                total = await db.scalar(select(func.count()).select_from(Message).where(Message.conversation_id == self.direct_id))
            self.assertEqual(total, 10)


class FeaturedTests(PlusCase):
    async def feature(self, message_id: int, *, as_: str = "alice", expected: int = 200) -> Any:
        return await self.json("POST", f"/conversations/{self.group_id}/messages/{message_id}/feature", as_=as_, expected=expected)

    async def test_featured_survives_pruning_and_recall(self) -> None:
        ids = await self.add_messages(3, sender="carol")
        self.assertEqual(await self.feature(ids[0]), {"featured": True})
        await self.feature(ids[1])
        # 修剪掉原消息：精华还在，带完整内容，原消息标记为不存在
        async with self.sessions() as db:
            await db.execute(text("DELETE FROM messages WHERE id IN (:a, :b)"), {"a": ids[0], "b": ids[1]})
            await db.commit()
        self.assertEqual(await self.count(FeaturedMessage), 2)
        response = await self.call("GET", f"/conversations/{self.group_id}/featured-messages?limit=1", as_="dave", expected=200)
        self.assertEqual((response.headers["X-Total-Count"], response.headers["X-Has-More"], len(response.json())), ("2", "true", 1))
        items = (await self.json("GET", f"/conversations/{self.group_id}/featured-messages", as_="dave"))
        first = next(item for item in items if item["message_id"] == ids[0])
        self.assertEqual((first["message"]["content"], first["message"]["sender"]["username"], first["message"]["sender_display_name"], first["message_exists"]), ("消息0", "carol", "卡罗", False))
        # 发送人改名不影响快照
        removed = await self.json("DELETE", f"/conversations/{self.group_id}/featured-messages/{first['id']}")
        self.assertEqual(removed, {"featured": False})
        # 撤回：精华保留，内容隐藏
        message_id = ids[2]
        await self.feature(message_id)
        await self.json("POST", f"/conversations/{self.group_id}/messages/{message_id}/recall", as_="carol")
        items = await self.json("GET", f"/conversations/{self.group_id}/featured-messages")
        recalled = next(item for item in items if item["message_id"] == message_id)
        self.assertEqual((recalled["recalled"], recalled["message"]["content"], recalled["message"]["metadata"]), (True, "", None))
        async with self.sessions() as db:
            row = (await db.execute(select(FeaturedMessage).where(FeaturedMessage.message_id == message_id))).scalar_one()
            self.assertEqual((row.snap_content, row.snap_recalled_at is not None), ("", True))

    async def test_featured_limit_by_tier_and_kept_after_expiry(self) -> None:
        ids = await self.add_messages(5)
        with patch.dict(group_plus_tiers.TIERS["free"], {"featured_limit": 2}), patch.dict(group_plus_tiers.TIERS["pro"], {"featured_limit": 4}):
            await self.feature(ids[0])
            await self.feature(ids[1])
            full = await self.code("POST", f"/conversations/{self.group_id}/messages/{ids[2]}/feature", 409)
            self.assertEqual((full["code"], full["limit"], full["used"], full["tier"]), ("featured_limit", 2, 2, "free"))
            await self.grant("pro", 30)
            await self.feature(ids[2])
            await self.feature(ids[3])
            self.assertEqual((await self.code("POST", f"/conversations/{self.group_id}/messages/{ids[4]}/feature", 409))["limit"], 4)
            # 过期后已有的保留（4 > 2），不能再加；取消不受限
            self.tick(days=31)
            self.assertEqual(len(await self.json("GET", f"/conversations/{self.group_id}/featured-messages")), 4)
            self.assertEqual((await self.code("POST", f"/conversations/{self.group_id}/messages/{ids[4]}/feature", 409))["tier"], "free")
            self.assertEqual(await self.feature(ids[3]), {"featured": False})

    @requires_sqlite
    async def test_backfill_from_legacy_table(self) -> None:
        ids = await self.add_messages(1, sender="carol")
        async with self.sessions() as db:
            member = (
                await db.execute(select(ConversationMember).where(ConversationMember.conversation_id == self.group_id, ConversationMember.user_id == self.ids["carol"]))
            ).scalar_one()
            member.title = "活跃分子"
            await db.commit()
        async with self.engine.begin() as connection:
            await connection.exec_driver_sql("DROP TABLE featured_messages")
            await connection.exec_driver_sql(
                "CREATE TABLE featured_messages (id INTEGER NOT NULL, message_id INTEGER NOT NULL, conversation_id INTEGER NOT NULL, "
                "set_by_id INTEGER, created_at DATETIME NOT NULL, PRIMARY KEY (id), CONSTRAINT uq_featured_message UNIQUE (message_id))"
            )
            await connection.exec_driver_sql("CREATE INDEX ix_featured_messages_conversation_id ON featured_messages (conversation_id)")
            await connection.execute(
                text("INSERT INTO featured_messages (id, message_id, conversation_id, set_by_id, created_at) VALUES (1, :m, :c, NULL, '2026-01-01'), (2, 99999, :c, NULL, '2026-01-02')"),
                {"m": ids[0], "c": self.group_id},
            )
            await connection.run_sync(lambda sync_connection: install_sqlite_extras(sync_connection))
        async with self.sessions() as db:
            rows = {row.id: row for row in (await db.execute(select(FeaturedMessage))).scalars().all()}
        self.assertEqual((rows[1].snap_content, rows[1].snap_sender_name, rows[1].snap_sender_title, rows[1].snapshot_missing), ("消息0", "卡罗", "活跃分子", False))
        self.assertEqual((rows[2].snapshot_missing, rows[2].snap_content), (True, ""))
        items = await self.json("GET", f"/conversations/{self.group_id}/featured-messages")
        self.assertEqual({item["id"]: (item["snapshot_missing"], item["message"]["content"]) for item in items}, {1: (False, "消息0"), 2: (True, "")})


# ---------------------------------------------------------------- 群待办


class TodoTests(PlusCase):
    async def test_single_todo_reminder_and_permissions(self) -> None:
        ids = await self.add_messages(2, sender="carol", prefix="周六晚上八点开会")
        path = f"/conversations/{self.group_id}/todo"
        self.assertEqual(await self.json("GET", path, as_="dave"), {"todo": None})
        self.assertEqual((await self.code("PUT", path, 403, as_="carol", json={"message_id": ids[0]}))["code"], "not_group_staff")
        await self.json("GET", path, as_="eve", expected=404)
        self.assertEqual((await self.code("PUT", path, 404, as_="alice", json={"message_id": 999999}))["message"], "Message not found")
        first = await self.json("PUT", path, as_="alice", json={"message_id": ids[0]})
        self.assertEqual((first["todo"]["message_id"], first["todo"]["message_exists"], first["todo"]["snapshot"]["content"]), (ids[0], True, "周六晚上八点开会0"))
        self.assertEqual(first["message"]["content"], "[群待办] 周六晚上八点开会0")
        self.assertEqual(first["message"]["metadata"]["group_todo"], {"message_id": ids[0]})
        # 只有一条：再设一次是替换
        second = await self.json("PUT", path, as_="bob", json={"message_id": ids[1]})
        self.assertEqual(await self.count(GroupTodo), 1)
        self.assertEqual((second["todo"]["message_id"], second["todo"]["set_by"]["id"]), (ids[1], self.ids["bob"]))
        # 提醒：@全体 的链路，且不占 @全体 的每日次数
        conversation = await self.conversation()
        self.assertEqual(conversation.last_mention_all_message_id, second["message"]["id"])
        self.assertEqual(await self.count(MentionAllUsage), 0)
        listing = await self.json("GET", "/conversations", as_="dave")
        mine = next(item for item in listing if item["id"] == self.group_id)
        self.assertEqual((mine["mention_pending"], mine["group_todo"]["message_id"]), ("all", ids[1]))
        events = [call.args[1] for call in self.send_to_users.await_args_list]
        self.assertIn("group.todo.updated", events)
        # 普通发消息伪造不了
        forged = await self.code("POST", f"/conversations/{self.group_id}/messages", 422, as_="alice", json={"content": "x", "metadata": {"group_todo": {"message_id": 1}}})
        self.assertEqual(forged["code"], "group_todo_not_sendable")
        # 取消
        await self.call("DELETE", path, as_="alice", expected=204)
        self.assertEqual(await self.json("GET", path, as_="dave"), {"todo": None})
        self.assertEqual((await self.code("DELETE", path, 404, as_="alice"))["code"], "todo_not_found")

    async def test_todo_survives_pruning_and_hides_recalled(self) -> None:
        ids = await self.add_messages(2, sender="carol", prefix="要办的事")
        path = f"/conversations/{self.group_id}/todo"
        await self.json("PUT", path, json={"message_id": ids[0]})
        async with self.sessions() as db:
            await db.execute(text("DELETE FROM messages WHERE id = :m"), {"m": ids[0]})
            await db.commit()
        todo = (await self.json("GET", path, as_="dave"))["todo"]
        self.assertEqual((todo["message_exists"], todo["snapshot"]["content"], todo["recalled"]), (False, "要办的事0", False))
        await self.json("PUT", path, json={"message_id": ids[1]})
        await self.json("POST", f"/conversations/{self.group_id}/messages/{ids[1]}/recall", as_="carol")
        todo = (await self.json("GET", path, as_="dave"))["todo"]
        self.assertEqual((todo["recalled"], todo["snapshot"]["content"], todo["summary"]), (True, "", "[消息已撤回]"))
        # 撤回的消息不能设为待办
        self.assertEqual((await self.code("PUT", path, 422, json={"message_id": ids[1]}))["code"], "not_todoable")


# ---------------------------------------------------------------- 群文件


class FileTests(PlusCase):
    def upload(self, name: str = "设计稿.txt", data: bytes = b"hello", **form: Any) -> dict[str, Any]:
        return {"files": {"file": (name, data, "text/plain")}, "data": {k: str(v).lower() for k, v in form.items()}}

    async def files_path(self, suffix: str = "") -> str:
        return f"/conversations/{self.group_id}/files{suffix}"

    async def test_locked_then_unlocked_upload_download_and_message(self) -> None:
        locked = await self.code("POST", await self.files_path(), 403, as_="carol", **self.upload())
        self.assertEqual((locked["code"], locked["state"]), ("files_locked", "locked"))
        await self.grant("pro_plus", 30)
        uploaded = await self.json("POST", await self.files_path(), as_="carol", expected=201, **self.upload("../../a b<>.txt"))
        file = uploaded["file"]
        self.assertEqual((file["name"], file["size"], file["permanent"], file["temporary"], file["uploader"]["username"]), ("a b.txt", 5, False, False, "carol"))
        expires = datetime.fromisoformat(file["expires_at"].replace("Z", "+00:00"))
        self.assertEqual(expires, START + timedelta(hours=24))
        self.assertEqual((uploaded["message"]["type"], uploaded["message"]["content"].startswith("[文件] a b.txt"), uploaded["message"]["metadata"]["file"]["id"]), ("text", True, file["id"]))
        # 下载：成员可以，外人不行；附件头
        download = await self.call("GET", file["download_path"].removeprefix("/api/v1"), as_="dave", expected=200)
        self.assertEqual(download.content, b"hello")
        self.assertTrue(download.headers["content-disposition"].startswith("attachment"))
        self.assertEqual((download.headers["x-content-type-options"], download.headers["content-type"]), ("nosniff", "application/octet-stream"))
        await self.call("GET", file["download_path"].removeprefix("/api/v1"), as_="eve", expected=404)
        # 列表、筛选、配额
        listing = await self.json("GET", await self.files_path("?q=a%20b"), as_="dave")
        self.assertEqual((listing["total"], listing["quota"]["used_bytes"], listing["quota"]["state"]), (1, 5, "active"))
        self.assertEqual((await self.json("GET", await self.files_path(f"?uploader_id={self.ids['bob']}"), as_="dave"))["total"], 0)
        # 文件消息不能伪造、不能转发
        forged = await self.code("POST", f"/conversations/{self.group_id}/messages", 422, as_="carol", json={"content": "x", "metadata": {"file": {"id": 1}}})
        self.assertEqual(forged["code"], "file_not_sendable")
        forward = await self.code(
            "POST",
            f"/conversations/{self.direct_id}/forward-bundle",
            422,
            as_="alice",
            json={"source_conversation_id": self.group_id, "message_ids": [uploaded["message"]["id"]]},
        )
        self.assertEqual(forward["code"], "file_not_forwardable")

    async def test_permissions_mute_ttl_and_permanent(self) -> None:
        await self.grant("pro_plus", 30)
        mine = (await self.json("POST", await self.files_path(), as_="carol", expected=201, **self.upload()))["file"]
        # 删除：上传人本人、群管；别的成员不行
        self.assertEqual((await self.code("DELETE", await self.files_path(f"/{mine['id']}"), 403, as_="dave"))["code"], "not_group_staff")
        # 转永久：只有群管
        self.assertEqual((await self.code("PATCH", await self.files_path(f"/{mine['id']}"), 403, as_="carol", json={"permanent": True}))["code"], "not_group_staff")
        kept = await self.json("PATCH", await self.files_path(f"/{mine['id']}"), as_="bob", json={"permanent": True})
        self.assertEqual((kept["permanent"], kept["expires_at"]), (True, None))
        # 默认保留时长：只能 1 / 7 / 30 天，仅群管
        bad = await self.code("PATCH", f"/conversations/{self.group_id}/settings", 422, as_="alice", json={"file_default_ttl_hours": 5})
        self.assertEqual(bad["code"], "invalid_file_ttl")
        await self.json("PATCH", f"/conversations/{self.group_id}/settings", as_="carol", expected=403, json={"file_default_ttl_hours": 168})
        await self.json("PATCH", f"/conversations/{self.group_id}/settings", as_="alice", json={"file_default_ttl_hours": 168})
        back = await self.json("PATCH", await self.files_path(f"/{mine['id']}"), as_="alice", json={"permanent": False})
        self.assertEqual(datetime.fromisoformat(back["expires_at"].replace("Z", "+00:00")), START + timedelta(days=7))
        # 被禁言的成员不能传
        async with self.sessions() as db:
            member = (
                await db.execute(select(ConversationMember).where(ConversationMember.conversation_id == self.group_id, ConversationMember.user_id == self.ids["dave"]))
            ).scalar_one()
            member.muted = True
            await db.commit()
        self.assertEqual((await self.code("POST", await self.files_path(), 403, as_="dave", **self.upload()))["code"], "muted")
        await self.call("DELETE", await self.files_path(f"/{mine['id']}"), as_="carol", expected=204)
        self.assertEqual(await self.count(GroupFile), 0)

    async def test_pro_uploads_are_temporary(self) -> None:
        await self.grant("pro", 30)
        await self.json("PATCH", f"/conversations/{self.group_id}/settings", as_="alice", json={"file_default_ttl_hours": 168})
        with patch.dict(group_plus_tiers.TIERS["pro"], {"file_temp_max_bytes": 10}):
            usage = await self.json("GET", await self.files_path("/usage"), as_="dave")
            self.assertEqual(
                {k: usage[k] for k in ("state", "tier", "quota_bytes", "used_bytes", "free_bytes", "temp_max_bytes", "temp_ttl_hours", "max_file_bytes")},
                {"state": "active", "tier": "pro", "quota_bytes": 0, "used_bytes": 0, "free_bytes": 0, "temp_max_bytes": 10, "temp_ttl_hours": 24, "max_file_bytes": 10},
            )
            # 10 字节以内：临时文件，固定 24 小时（不看群的默认时长），群管带 permanent 也不管用，不占空间
            uploaded = await self.json("POST", await self.files_path(), as_="alice", expected=201, **self.upload(data=b"0123456789", permanent=True))
            file = uploaded["file"]
            self.assertEqual((file["temporary"], file["permanent"]), (True, False))
            self.assertEqual(datetime.fromisoformat(file["expires_at"].replace("Z", "+00:00")), START + timedelta(hours=24))
            self.assertEqual((uploaded["message"]["metadata"]["file"]["temporary"], uploaded["message"]["metadata"]["file"]["permanent"]), (True, False))
            usage = await self.json("GET", await self.files_path("/usage"), as_="dave")
            self.assertEqual((usage["used_bytes"], usage["file_count"], usage["free_bytes"], usage["max_file_bytes"]), (0, 1, 0, 10))
            status = await self.json("GET", f"/conversations/{self.group_id}/plus", as_="dave")
            self.assertEqual((status["usage"]["file_bytes_used"], status["files"]["temp_max_bytes"], status["limits"]["file_enabled"]), (0, 10, True))
            # 超过临时文件上限
            big = await self.code("POST", await self.files_path(), 413, as_="carol", **self.upload("big.txt", b"x" * 11))
            self.assertEqual(
                (big["code"], big["max_file_bytes"], big["temp_max_bytes"], big["free_bytes"]), ("file_too_large", 10, 10, 0)
            )
            # 没有空间：临时文件不能转永久；转回限时什么都不做
            over = await self.code("PATCH", await self.files_path(f"/{file['id']}"), 413, as_="alice", json={"permanent": True})
            self.assertEqual((over["code"], over["quota_bytes"], over["used_bytes"], over["size"]), ("quota_exceeded", 0, 0, 10))
            same = await self.json("PATCH", await self.files_path(f"/{file['id']}"), as_="alice", json={"permanent": False})
            self.assertEqual((same["temporary"], same["expires_at"]), (True, file["expires_at"]))
        self.assertEqual(await self.count(GroupFile), 1)

    async def test_space_files_then_temporary_when_full(self) -> None:
        await self.grant("pro_plus", 30)
        with patch.dict(group_plus_tiers.TIERS["pro_plus"], {"file_quota_bytes": 50, "file_temp_max_bytes": 8}):
            # 放得进剩余空间的就是空间文件，没有单独的单文件上限（比临时文件上限大也行）
            large = (await self.json("POST", await self.files_path(), as_="carol", expected=201, **self.upload("a.txt", b"x" * 40)))["file"]
            self.assertEqual((large["temporary"], large["permanent"]), (False, False))
            usage = await self.json("GET", await self.files_path("/usage"), as_="dave")
            self.assertEqual((usage["used_bytes"], usage["free_bytes"], usage["temp_max_bytes"], usage["max_file_bytes"]), (40, 10, 8, 10))
            too_big = await self.code("POST", await self.files_path(), 413, as_="carol", **self.upload("b.txt", b"x" * 11))
            self.assertEqual((too_big["code"], too_big["max_file_bytes"], too_big["free_bytes"]), ("file_too_large", 10, 10))
            await self.json("POST", await self.files_path(), as_="carol", expected=201, **self.upload("c.txt", b"x" * 9))
            # 空间剩 1 字节：放不下的小文件传成临时文件，超过临时文件上限的 413
            temp = (await self.json("POST", await self.files_path(), as_="carol", expected=201, **self.upload("d.txt", b"x" * 5)))["file"]
            self.assertEqual(temp["temporary"], True)
            self.assertEqual(datetime.fromisoformat(temp["expires_at"].replace("Z", "+00:00")), START + timedelta(hours=24))
            big = await self.code("POST", await self.files_path(), 413, as_="carol", **self.upload("e.txt", b"x" * 9))
            self.assertEqual((big["code"], big["max_file_bytes"], big["temp_max_bytes"], big["free_bytes"]), ("file_too_large", 8, 8, 1))
            listing = await self.json("GET", await self.files_path(), as_="dave")
            self.assertEqual((listing["total"], listing["quota"]["used_bytes"], listing["quota"]["file_count"]), (3, 49, 3))
            # 临时文件转永久：剩余空间放得下才行
            over = await self.code("PATCH", await self.files_path(f"/{temp['id']}"), 413, as_="bob", json={"permanent": True})
            self.assertEqual((over["code"], over["quota_bytes"], over["used_bytes"], over["size"]), ("quota_exceeded", 50, 49, 5))
            await self.call("DELETE", await self.files_path(f"/{large['id']}"), as_="carol", expected=204)
            kept = await self.json("PATCH", await self.files_path(f"/{temp['id']}"), as_="bob", json={"permanent": True})
            self.assertEqual((kept["temporary"], kept["permanent"], kept["expires_at"]), (False, True, None))
            self.assertEqual((await self.json("GET", await self.files_path("/usage"), as_="dave"))["used_bytes"], 14)
            empty = await self.code("POST", await self.files_path(), 422, as_="carol", **self.upload("f.txt", b""))
            self.assertEqual(empty["code"], "empty_file")
        self.assertEqual(await self.count(GroupFile), 2)

    async def test_expiry_cleanup_and_grace_period(self) -> None:
        await self.grant("pro_plus", 30)
        short = (await self.json("POST", await self.files_path(), as_="carol", expected=201, **self.upload("short.txt", b"aaa", announce=False)))["file"]
        keep = (await self.json("POST", await self.files_path(), as_="alice", expected=201, **self.upload("keep.txt", b"bbb", permanent=True, announce=False)))["file"]
        self.assertIsNone(short["message_id"])
        # 过期（24 小时）：不再算配额、不可下载，worker 删库行和文件
        self.tick(hours=25)
        self.assertEqual((await self.json("GET", await self.files_path(), as_="dave"))["total"], 1)
        self.assertEqual((await self.code("GET", await self.files_path(f"/{short['id']}/download"), 410, as_="dave"))["code"], "file_expired")
        async with self.sessions() as db:
            row = await db.get(GroupFile, short["id"])
            stored = Path(settings.upload_dir) / row.storage_key
            self.assertTrue(stored.is_file())
            done = await file_service.sweep(db)
        self.assertEqual(done["expired"], 1)
        self.assertFalse(stored.exists())
        # 群升级到期：宽限 30 天内可读不可传
        self.tick(days=30)
        status = await self.json("GET", f"/conversations/{self.group_id}/plus", as_="dave")
        self.assertEqual((status["tier"], status["files"]["state"]), ("free", "grace"))
        locked = await self.code("POST", await self.files_path(), 403, as_="carol", **self.upload())
        self.assertEqual((locked["code"], locked["state"]), ("files_locked", "grace"))
        await self.call("GET", await self.files_path(f"/{keep['id']}/download"), as_="dave", expected=200)
        async with self.sessions() as db:
            self.assertEqual(await file_service.sweep(db), {"expired": 0, "orphaned": 0})
        # 续费恢复
        await self.grant("pro_plus", 30)
        await self.json("POST", await self.files_path(), as_="carol", expected=201, **self.upload("again.txt", b"ccc", announce=False))
        # 过了宽限：全部删掉（含永久）
        self.tick(days=31 + 31)
        async with self.sessions() as db:
            done = await file_service.sweep(db)
        self.assertGreaterEqual(done["orphaned"], 1)
        self.assertEqual(await self.count(GroupFile), 0)

    async def test_uploads_mount_does_not_serve_group_files(self) -> None:
        from starlette.applications import Starlette
        from starlette.routing import Mount

        from app.main import CachedUploadFiles

        root = Path(self.tmp.name) / "static"
        (root / "group-files" / "1").mkdir(parents=True)
        (root / "group-files" / "1" / "secret").write_bytes(b"secret")
        (root / "messages").mkdir()
        (root / "messages" / "ok.png").write_bytes(b"png")
        app = Starlette(routes=[Mount("/uploads", CachedUploadFiles(directory=str(root)))])
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
            self.assertEqual((await client.get("/uploads/group-files/1/secret")).status_code, 404)
            self.assertEqual((await client.get("/uploads/messages/ok.png")).status_code, 200)

    def test_clean_filename(self) -> None:
        clean = file_service.clean_filename
        self.assertEqual(clean("a/b\\c.txt"), "c.txt")
        self.assertEqual(clean("  ..hidden.. "), "hidden")
        self.assertEqual(clean("​x\u0000y.zip"), "xy.zip")
        self.assertEqual(clean(""), "file")
        long = clean("名" * 300 + ".pdf")
        self.assertTrue(long.endswith(".pdf") and len(long) <= 120)


class DirectFileTests(PlusCase):
    """直传（预签名 URL）：storage 的开关和预签名打桩，对象用本地存储模拟「客户端已经传到桶里」。"""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        from app.services import storage

        self.direct = True
        self.put_calls: list[tuple[str, int]] = []
        self.get_calls: list[tuple[str, str]] = []

        def fake_put(key: str, size: int, expires_in: int = 900) -> tuple[str, dict[str, str]]:
            self.put_calls.append((key, size))
            return f"https://bucket.test/{key}?sig=put", {"Content-Type": "application/octet-stream"}

        def fake_get(key: str, expires_in: int = 300, content_disposition: str | None = None) -> str:
            self.get_calls.append((key, content_disposition or ""))
            return f"https://bucket.test/{key}?sig=get"

        for patcher in (
            patch.object(storage, "direct_transfer_enabled", lambda: self.direct),
            patch.object(storage, "presign_put", fake_put),
            patch.object(storage, "presign_get", fake_get),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def path(self, suffix: str = "") -> str:
        return f"/conversations/{self.group_id}/files{suffix}"

    def put_object(self, key: str, data: bytes) -> Path:
        target = Path(settings.upload_dir) / key
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return target

    async def ticket(self, size: int, *, as_: str = "carol", expected: int = 201, **body: Any) -> dict[str, Any]:
        return await self.json("POST", self.path("/uploads"), as_=as_, expected=expected, json={"name": "设计稿.zip", "size": size, **body})

    async def complete(self, upload_id: str, *, as_: str = "carol", expected: int = 201, **body: Any) -> dict[str, Any]:
        return await self.json("POST", self.path(f"/uploads/{upload_id}/complete"), as_=as_, expected=expected, json=body)

    async def test_ticket_complete_space_then_temporary(self) -> None:
        await self.grant("pro_plus", 30)
        with patch.dict(group_plus_tiers.TIERS["pro_plus"], {"file_quota_bytes": 50, "file_temp_max_bytes": 8}):
            self.assertTrue((await self.json("GET", self.path("/usage"), as_="dave"))["direct"])
            self.assertTrue((await self.json("GET", self.path(), as_="dave"))["quota"]["direct"])
            self.assertTrue((await self.json("GET", f"/conversations/{self.group_id}/plus", as_="dave"))["files"]["direct"])
            ticket = await self.ticket(40, content_type="application/zip")
            self.assertEqual((ticket["method"], ticket["headers"], ticket["temporary"]), ("PUT", {"Content-Type": "application/octet-stream"}, False))
            key, size = self.put_calls[-1]
            self.assertTrue(key.startswith(f"group-files/{self.group_id}/") and size == 40)
            self.assertTrue(ticket["url"].startswith("https://bucket.test/") and ticket["expires_at"])
            # 还没传：409，上传单还在，传完再 complete
            self.assertEqual((await self.code("POST", self.path(f"/uploads/{ticket['upload_id']}/complete"), 409, as_="carol"))["code"], "upload_incomplete")
            self.put_object(key, b"x" * 40)
            # 别人不能 complete
            self.assertEqual((await self.code("POST", self.path(f"/uploads/{ticket['upload_id']}/complete"), 404, as_="dave"))["code"], "upload_not_found")
            done = await self.complete(ticket["upload_id"], announce=True)
            file = done["file"]
            self.assertEqual(
                (file["name"], file["size"], file["content_type"], file["temporary"], file["uploader_id"]),
                ("设计稿.zip", 40, "application/zip", False, self.ids["carol"]),
            )
            self.assertEqual(datetime.fromisoformat(file["expires_at"].replace("Z", "+00:00")), START + timedelta(hours=24))
            self.assertEqual((done["message"]["metadata"]["file"]["id"], done["message"]["metadata"]["file"]["temporary"]), (file["id"], False))
            async with self.sessions() as db:
                self.assertEqual((await db.get(GroupFile, file["id"])).storage_key, key)
            self.assertEqual(await self.count(GroupFileUpload), 0)
            # 第二次 complete：404
            self.assertEqual((await self.code("POST", self.path(f"/uploads/{ticket['upload_id']}/complete"), 404, as_="carol"))["code"], "upload_not_found")
            # 剩 10 字节：开单时放得进空间，complete 前空间被别的上传占了 → 变成临时文件
            pending = await self.ticket(6)
            self.assertFalse(pending["temporary"])
            self.put_object(self.put_calls[-1][0], b"y" * 6)
            await self.json("POST", self.path(), as_="dave", expected=201, files={"file": ("other.txt", b"z" * 9, "text/plain")}, data={"announce": "false"})
            late = await self.complete(pending["upload_id"], announce=False)
            self.assertEqual((late["file"]["temporary"], late["message"]), (True, None))
            self.assertEqual(datetime.fromisoformat(late["file"]["expires_at"].replace("Z", "+00:00")), START + timedelta(hours=24))
            # 剩 1 字节：开单就是临时文件；超过临时上限 413，和 multipart 一样的错误
            self.assertTrue((await self.ticket(5))["temporary"])
            big = await self.code("POST", self.path("/uploads"), 413, as_="carol", json={"name": "a", "size": 9})
            self.assertEqual((big["code"], big["max_file_bytes"], big["temp_max_bytes"], big["free_bytes"]), ("file_too_large", 8, 8, 1))
            self.assertEqual((await self.code("POST", self.path("/uploads"), 422, as_="carol", json={"name": "a", "size": 0}))["code"], "empty_file")
            # complete 时连临时文件都放不下（档位上限变了）：413，对象和上传单都删掉
            doomed = await self.ticket(5)
            doomed_path = self.put_object(self.put_calls[-1][0], b"q" * 5)
            with patch.dict(group_plus_tiers.TIERS["pro_plus"], {"file_temp_max_bytes": 4}):
                self.assertEqual((await self.code("POST", self.path(f"/uploads/{doomed['upload_id']}/complete"), 413, as_="carol"))["code"], "file_too_large")
            self.assertFalse(doomed_path.exists())
            self.assertEqual((await self.code("POST", self.path(f"/uploads/{doomed['upload_id']}/complete"), 404, as_="carol"))["code"], "upload_not_found")

    async def test_ticket_checks_match_multipart(self) -> None:
        locked = await self.code("POST", self.path("/uploads"), 403, as_="carol", json={"name": "a", "size": 1})
        self.assertEqual((locked["code"], locked["state"]), ("files_locked", "locked"))
        await self.grant("pro_plus", 30)
        # 群管 permanent=true 直接永久；成员带了也不管用
        staff = await self.ticket(3, as_="bob", permanent=True)
        self.put_object(self.put_calls[-1][0], b"abc")
        kept = await self.complete(staff["upload_id"], as_="bob")
        self.assertEqual((kept["file"]["permanent"], kept["file"]["expires_at"]), (True, None))
        member = await self.ticket(3, permanent=True)
        self.put_object(self.put_calls[-1][0], b"abc")
        self.assertFalse((await self.complete(member["upload_id"]))["file"]["permanent"])
        # 开单之后被禁言：complete 403，对象删掉
        pending = await self.ticket(3, as_="dave")
        stored = self.put_object(self.put_calls[-1][0], b"abc")
        async with self.sessions() as db:
            row = (
                await db.execute(select(ConversationMember).where(ConversationMember.conversation_id == self.group_id, ConversationMember.user_id == self.ids["dave"]))
            ).scalar_one()
            row.muted = True
            await db.commit()
        self.assertEqual((await self.code("POST", self.path(f"/uploads/{pending['upload_id']}/complete"), 403, as_="dave"))["code"], "muted")
        self.assertFalse(stored.exists())
        self.assertEqual((await self.code("POST", self.path("/uploads"), 403, as_="dave", json={"name": "a", "size": 1}))["code"], "muted")
        # 外人 404；直传关着 409（客户端回落到 multipart）
        await self.call("POST", self.path("/uploads"), as_="eve", expected=404, json={"name": "a", "size": 1})
        self.direct = False
        self.assertEqual((await self.code("POST", self.path("/uploads"), 409, as_="carol", json={"name": "a", "size": 1}))["code"], "direct_upload_unavailable")
        self.assertFalse((await self.json("GET", self.path("/usage"), as_="carol"))["direct"])

    async def test_size_mismatch_deletes_object_and_record(self) -> None:
        await self.grant("pro_plus", 30)
        ticket = await self.ticket(10)
        stored = self.put_object(self.put_calls[-1][0], b"x" * 11)
        mismatch = await self.code("POST", self.path(f"/uploads/{ticket['upload_id']}/complete"), 422, as_="carol")
        self.assertEqual((mismatch["code"], mismatch["size"], mismatch["uploaded_size"]), ("upload_size_mismatch", 10, 11))
        self.assertFalse(stored.exists())
        self.assertEqual((await self.count(GroupFileUpload), await self.count(GroupFile)), (0, 0))
        # 过期的上传单：404
        other = await self.ticket(3)
        self.put_object(self.put_calls[-1][0], b"abc")
        self.tick(hours=2)
        self.assertEqual((await self.code("POST", self.path(f"/uploads/{other['upload_id']}/complete"), 404, as_="carol"))["code"], "upload_not_found")

    async def test_other_group_id_is_not_found(self) -> None:
        await self.grant("pro_plus", 30)
        ticket = await self.ticket(3)
        async with self.sessions() as db:
            second = Conversation(type=ConversationType.GROUP, title="另一个群")
            db.add(second)
            await db.flush()
            db.add(ConversationMember(conversation_id=second.id, user_id=self.ids["carol"], role=MemberRole.OWNER))
            await db.commit()
            second_id = second.id
        await self.grant("pro_plus", 30, gid=second_id)
        missing = await self.code("POST", f"/conversations/{second_id}/files/uploads/{ticket['upload_id']}/complete", 404, as_="carol")
        self.assertEqual(missing["code"], "upload_not_found")
        self.assertEqual((await self.code("POST", self.path("/uploads/nope/complete"), 404, as_="carol"))["code"], "upload_not_found")

    async def test_rate_limited_complete_rolls_back(self) -> None:
        from fastapi import HTTPException

        await self.grant("pro_plus", 30)
        ticket = await self.ticket(3)
        stored = self.put_object(self.put_calls[-1][0], b"abc")
        with patch.object(message_service, "send_message", AsyncMock(side_effect=HTTPException(status_code=429, detail="slow down"))):
            limited = await self.code("POST", self.path(f"/uploads/{ticket['upload_id']}/complete"), 429, as_="carol")
        self.assertEqual(limited["code"], "rate_limited")
        self.assertFalse(stored.exists())
        self.assertEqual((await self.count(GroupFileUpload), await self.count(GroupFile)), (0, 0))

    async def test_download_link(self) -> None:
        await self.grant("pro_plus", 30)
        ticket = await self.ticket(5)
        self.put_object(self.put_calls[-1][0], b"hello")
        file = (await self.complete(ticket["upload_id"]))["file"]
        link = await self.json("GET", self.path(f"/{file['id']}/link"), as_="dave")
        self.assertTrue(link["url"].startswith("https://bucket.test/group-files/") and link["expires_at"])
        _key, disposition = self.get_calls[-1]
        self.assertEqual(disposition, "attachment; filename=\"___.zip\"; filename*=UTF-8''%E8%AE%BE%E8%AE%A1%E7%A8%BF.zip")
        await self.call("GET", self.path(f"/{file['id']}/link"), as_="eve", expected=404)
        # 老的下载接口照样能用
        self.assertEqual((await self.call("GET", self.path(f"/{file['id']}/download"), as_="dave", expected=200)).content, b"hello")
        self.direct = False
        self.assertEqual((await self.code("GET", self.path(f"/{file['id']}/link"), 409, as_="dave"))["code"], "direct_download_unavailable")
        self.direct = True
        self.tick(hours=25)
        self.assertEqual((await self.code("GET", self.path(f"/{file['id']}/link"), 410, as_="dave"))["code"], "file_expired")

    async def test_worker_sweeps_stale_uploads(self) -> None:
        await self.grant("pro_plus", 30)
        await self.ticket(3)
        uploaded_path = self.put_object(self.put_calls[-1][0], b"abc")
        await self.ticket(4)  # 拿了单没传
        self.tick(seconds=1800)
        fresh = await self.ticket(5)
        async with self.sessions() as db:
            self.assertEqual(await file_service.sweep_uploads(db), 0)
        self.tick(seconds=1801)
        async with self.sessions() as db:
            self.assertEqual(await file_service.sweep_uploads(db), 2)
        self.assertFalse(uploaded_path.exists())
        async with self.sessions() as db:
            left = (await db.execute(select(GroupFileUpload))).scalars().all()
        self.assertEqual([row.id for row in left], [fresh["upload_id"]])


# ---------------------------------------------------------------- 搜索


class SearchTests(PlusCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        # eve 和 carol 的私聊里有一条 alice 不该搜到的
        await self.add_messages(1, sender="eve", gid=self.other_id, prefix="绝密的项目计划")
        self.texts: dict[str, int] = {}
        async with self.sessions() as db:
            for key, content, sender, kwargs in (
                ("meeting", "今天下午三点开会，带上设计稿", "carol", {}),
                ("short", "好的收到", "bob", {}),
                ("english", "Please check the Report FINAL version", "carol", {}),
                ("special", "折扣 50% off_sale \"引号\" 'single'", "dave", {}),
                ("link", "看看这个 https://example.com/page 很不错", "carol", {}),
                ("direct", "私聊里的开会安排", "carol", {"conversation": self.direct_id}),
                ("file", "[文件] 报告.pdf（1.0 MB）", "carol", {"metadata": json.dumps({"file": {"id": 1, "name": "报告.pdf"}})}),
            ):
                row = Message(
                    conversation_id=kwargs.get("conversation", self.group_id),
                    sender_id=self.ids[sender],
                    content=content,
                    metadata_json=kwargs.get("metadata"),
                )
                db.add(row)
                await db.flush()
                self.texts[key] = row.id
            image = Message(conversation_id=self.group_id, sender_id=self.ids["carol"], type=MessageType.IMAGE, content="/uploads/messages/a.jpg")
            sticker = Message(
                conversation_id=self.group_id, sender_id=self.ids["carol"], type=MessageType.IMAGE, content="/uploads/messages/b.jpg", metadata_json='{"sticker": true}'
            )
            db.add_all([image, sticker])
            await db.flush()
            self.texts["image"], self.texts["sticker"] = image.id, sticker.id
            await db.commit()

    async def ids_of(self, params: str, *, as_: str = "alice") -> list[int]:
        body = await self.json("GET", f"/search/messages?{params}", as_=as_)
        return [item["message"]["id"] for item in body["items"]]

    async def test_accuracy(self) -> None:
        t = self.texts
        self.assertEqual(await self.ids_of("q=下午三点"), [t["meeting"]])
        self.assertEqual(await self.ids_of("q=开会"), [t["direct"], t["meeting"]])
        # 少于 3 个字也能搜（LIKE）；多个词要全部命中
        self.assertEqual(await self.ids_of("q=收到"), [t["short"]])
        self.assertEqual(await self.ids_of("q=开会 设计稿"), [t["meeting"]])
        self.assertEqual(await self.ids_of("q=开会 不存在的词"), [])
        self.assertEqual(await self.ids_of("q=report final"), [t["english"]])
        self.assertEqual(await self.ids_of("q=50%25"), [t["special"]])
        self.assertEqual(await self.ids_of("q=off_sale"), [t["special"]])
        self.assertEqual(await self.ids_of("q=%22引号%22"), [t["special"]])
        self.assertEqual(await self.ids_of("q=%25%25"), [])
        self.assertEqual(await self.ids_of("q=开会&conversation_id=" + str(self.group_id)), [t["meeting"]])
        self.assertEqual(await self.ids_of(f"q=开会&sender_id={self.ids['bob']}"), [])
        self.assertEqual(await self.ids_of("category=media"), [t["image"]])
        self.assertEqual(await self.ids_of("category=sticker"), [t["sticker"]])
        self.assertEqual(await self.ids_of("category=file"), [t["file"]])
        self.assertEqual(await self.ids_of("category=link"), [t["link"]])
        self.assertEqual((await self.code("GET", "/search/messages", 422))["code"], "query_required")

    async def test_scope_is_only_my_visible_messages(self) -> None:
        t = self.texts
        # 别人的会话（eve 和 carol 的私聊）搜不到，指定它是 404
        self.assertEqual(await self.ids_of("q=绝密"), [])
        await self.json("GET", f"/search/messages?q=绝密&conversation_id={self.other_id}", expected=404)
        self.assertEqual(len(await self.ids_of("q=绝密", as_="eve")), 1)
        # eve 不在群里，搜不到群里的消息
        self.assertEqual(await self.ids_of("q=开会", as_="eve"), [])
        # 仅自己删除
        async with self.sessions() as db:
            db.add(MessageVisibility(message_id=t["meeting"], user_id=self.ids["alice"]))
            await db.commit()
        self.assertEqual(await self.ids_of("q=开会"), [t["direct"]])
        self.assertEqual(await self.ids_of("q=开会", as_="bob"), [t["meeting"]])
        # 清空聊天记录之前的
        async with self.sessions() as db:
            member = (
                await db.execute(select(ConversationMember).where(ConversationMember.conversation_id == self.group_id, ConversationMember.user_id == self.ids["bob"]))
            ).scalar_one()
            member.cleared_before_message_id = t["meeting"]
            await db.commit()
        self.assertEqual(await self.ids_of("q=开会", as_="bob"), [])
        # 撤回、修剪（删除）：索引同步
        await self.json("POST", f"/conversations/{self.direct_id}/messages/{t['direct']}/recall", as_="carol")
        self.assertEqual(await self.ids_of("q=私聊里"), [])
        async with self.sessions() as db:
            await db.execute(text("DELETE FROM messages WHERE id = :m"), {"m": t["short"]})
            await db.commit()
            self.assertEqual(await db.scalar(text("SELECT count(*) FROM message_search_index WHERE message_id = :m"), {"m": t["short"]}), 0)
        self.assertEqual(await self.ids_of("q=收到"), [])

    async def test_date_filter_highlights_summary_and_paging(self) -> None:
        t = self.texts
        async with self.sessions() as db:
            await db.execute(text("UPDATE messages SET created_at = '2026-03-01 10:00:00.000000' WHERE id = :m"), {"m": t["meeting"]})
            await db.execute(text("UPDATE messages SET created_at = '2026-03-05 10:00:00.000000' WHERE id = :m"), {"m": t["direct"]})
            await db.commit()
        # 日期按 UTC+8 的整天，date_to 含当天
        self.assertEqual(await self.ids_of("q=开会&date_from=2026-03-01&date_to=2026-03-01"), [t["meeting"]])
        self.assertEqual(await self.ids_of("q=开会&date_from=2026-03-02"), [t["direct"]])
        self.assertEqual(await self.ids_of("q=开会&date_to=2026-03-04"), [t["meeting"]])
        self.assertEqual(await self.ids_of("q=开会&date_from=2026-03-01T02:30:00Z&date_to=2026-03-01T03:00:00Z"), [])
        self.assertEqual((await self.code("GET", "/search/messages?q=a&date_from=nope", 422))["code"], "invalid_date")
        body = await self.json("GET", "/search/messages?q=设计稿")
        hit = body["items"][0]
        start, end = hit["highlights"][0]
        self.assertEqual(hit["message"]["content"][start:end], "设计稿")
        snippet_start, snippet_end = hit["snippet_highlights"][0]
        self.assertEqual(hit["snippet"][snippet_start:snippet_end], "设计稿")
        self.assertEqual((hit["category"], hit["conversation"]["id"], hit["conversation"]["type"], hit["conversation"]["plus_tier"]), ("text", self.group_id, "group", "free"))
        direct = (await self.json("GET", "/search/messages?q=私聊里"))["items"][0]["conversation"]
        self.assertEqual((direct["type"], direct["direct_user"]["username"]), ("direct", "carol"))
        # 会话聚合
        summary = await self.json("GET", "/search/messages/summary?q=开会")
        self.assertEqual((summary["total"], [(item["conversation"]["id"], item["count"]) for item in summary["items"]]), (2, [(self.direct_id, 1), (self.group_id, 1)]))
        # 翻页
        page = await self.json("GET", "/search/messages?category=all&conversation_id=" + str(self.group_id) + "&limit=3")
        self.assertEqual((len(page["items"]), page["has_more"]), (3, True))
        rest = await self.json("GET", f"/search/messages?conversation_id={self.group_id}&before_id={page['next_before_id']}&limit=50")
        self.assertTrue(all(item["message"]["id"] < page["next_before_id"] for item in rest["items"]))
        by_offset = await self.json("GET", f"/search/messages?conversation_id={self.group_id}&limit=3&offset=3")
        self.assertEqual([i["message"]["id"] for i in by_offset["items"]], [i["message"]["id"] for i in rest["items"]][:3])

    async def test_recalled_message_is_never_returned_even_if_present_in_index_race(self) -> None:
        t = self.texts
        async with self.sessions() as db:
            await db.execute(text("UPDATE messages SET recalled_at = '2026-01-01' WHERE id = :m"), {"m": t["link"]})
            await db.commit()
        self.assertEqual(await self.ids_of("category=link"), [])

    async def test_performance_baseline_50k(self) -> None:
        # 50k 条的单会话搜索，SQL 本身是毫秒级；这里量的是整条 HTTP 路径，上限放宽到 1 秒以免慢机器上偶发失败
        async with self.engine.begin() as connection:
            rows = [
                {"c": self.group_id, "s": self.ids["carol"] if i % 3 else self.ids["alice"], "t": f"这是第{i}条普通的聊天消息，今天天气不错 {i % 97}"}
                for i in range(50000)
            ]
            rows[31337]["t"] = "唯一的暗号：星河滚烫"
            await connection.execute(
                text("INSERT INTO messages (conversation_id, sender_id, type, content, created_at) VALUES (:c, :s, 'TEXT', :t, '2026-02-01 00:00:00.000000')"), rows
            )
        # 先热身一次（第一次请求要付 SQLAlchemy 语句编译、连接建立的钱），再计时
        await self.json("GET", f"/search/messages?q=不存在的东西&conversation_id={self.group_id}")
        started = time.perf_counter()
        body = await self.json("GET", f"/search/messages?q=星河滚烫&conversation_id={self.group_id}")
        elapsed = time.perf_counter() - started
        self.assertEqual(len(body["items"]), 1)
        self.assertLess(elapsed, 1.0, f"搜索耗时 {elapsed:.3f}s")
        started = time.perf_counter()
        common = await self.json("GET", f"/search/messages?q=天气不错&conversation_id={self.group_id}&limit=20")
        self.assertEqual(len(common["items"]), 20)
        self.assertLess(time.perf_counter() - started, 1.0)
        started = time.perf_counter()
        everywhere = await self.json("GET", "/search/messages?q=星河滚烫")
        self.assertEqual(len(everywhere["items"]), 1)
        self.assertLess(time.perf_counter() - started, 1.0)


# ---------------------------------------------------------------- 后台


class AdminTests(PlusCase):
    async def test_settings_grant_groups_records_and_audit(self) -> None:
        for method, path in (("GET", "/admin/group-plus/settings"), ("GET", "/admin/group-plus/groups"), ("GET", "/admin/group-plus/records")):
            await self.json(method, path, as_="carol", expected=403)
        settings_view = await self.json("GET", "/admin/group-plus/settings", as_="root")
        self.assertEqual((settings_view["prices"], settings_view["enabled"]), ({"pro": 80, "pro_plus": 200, "max": 500}, True))
        bad = await self.json("PUT", "/admin/group-plus/settings", as_="root", expected=422, json={"price_max": 0})
        self.assertEqual((bad["detail"]["code"], bad["detail"]["field"]), ("invalid_settings", "price_max"))
        changed = await self.json("PUT", "/admin/group-plus/settings", as_="root", json={"price_pro": 100})
        self.assertEqual(changed["prices"]["pro"], 100)
        self.ledger.donate(OID_A, 1000)
        quote = await self.json("POST", f"/conversations/{self.group_id}/plus/quote", json={"tier": "pro", "period": "year"})
        self.assertEqual(quote["price"], 1000)

        granted = await self.grant("max", 20)
        self.assertEqual((granted["record"]["source"], granted["record"]["amount"], granted["conversation"]["plus_tier"], granted["reason"]), ("grant", 0, "max", "grant"))
        self.assertEqual(await self.coins("alice"), 0)
        again = await self.grant("max", 10)
        self.assertEqual(datetime.fromisoformat(again["conversation"]["plus_expires_at"].replace("Z", "+00:00")), START + 30 * DAY)
        groups = await self.json("GET", "/admin/group-plus/groups?tier=max&expiring_within_days=60", as_="root")
        self.assertEqual((groups["total"], groups["items"][0]["conversation_id"], groups["items"][0]["owner"]["id"], groups["items"][0]["days_left"]), (1, self.group_id, self.ids["alice"], 30))
        self.assertEqual((await self.json("GET", "/admin/group-plus/groups?tier=pro", as_="root"))["total"], 0)
        revoked = await self.grant("free")
        self.assertEqual((revoked["record"]["action"], revoked["conversation"]["plus_tier"]), ("revoke", "free"))
        records = await self.json("GET", f"/admin/group-plus/records?conversation_id={self.group_id}", as_="root")
        self.assertEqual((records["total"], [r["source"] for r in records["items"]]), (3, ["revoke", "grant", "grant"]))
        invalid = await self.json("POST", "/admin/group-plus/grant", as_="root", expected=422, json={"conversation_id": self.group_id, "tier": "pro", "days": 0})
        self.assertEqual(invalid["detail"]["code"], "invalid_days")
        await self.json("POST", "/admin/group-plus/grant", as_="root", expected=404, json={"conversation_id": self.direct_id, "tier": "pro", "days": 3})
        self.assertEqual(
            [a for a in await self.audit() if a.startswith("group_plus")],
            ["group_plus.settings.update", "group_plus.grant", "group_plus.grant", "group_plus.revoke"],
        )
        # 后台接口不返回任何消息内容
        self.assertNotIn("content", json.dumps(groups))


# ---------------------------------------------------------------- 群文件文件夹（5.5）


class FolderTests(PlusCase):
    def upload(self, name: str = "设计稿.txt", data: bytes = b"hello", **form: Any) -> dict[str, Any]:
        return {"files": {"file": (name, data, "text/plain")}, "data": {k: str(v).lower() for k, v in form.items()}}

    def path(self, suffix: str = "") -> str:
        return f"/conversations/{self.group_id}/files{suffix}"

    async def folder(self, name: str, *, as_: str = "alice", expected: int = 201) -> dict[str, Any]:
        return await self.json("POST", self.path("/folders"), as_=as_, expected=expected, json={"name": name})

    async def put(self, name: str = "a.txt", data: bytes = b"hello", *, as_: str = "carol", **form: Any) -> dict[str, Any]:
        form.setdefault("announce", False)
        return (await self.json("POST", self.path(), as_=as_, expected=201, **self.upload(name, data, **form)))["file"]

    def events(self, name: str) -> list[dict[str, Any]]:
        return [call.args[2] for call in self.send_to_users.call_args_list if call.args[1] == name]

    async def test_tier_gate(self) -> None:
        # 免费 / Pro 不能用；Pro+、MAX、永久 MAX 可以
        self.assertEqual((await self.code("POST", self.path("/folders"), 403, json={"name": "资料"}))["code"], "folders_require_plus")
        await self.grant("pro", 30)
        listing = await self.json("GET", self.path(), as_="alice")
        self.assertEqual((listing["can_manage_folders"], listing["can_tidy_folders"], listing["quota"]["folders_enabled"]), (False, True, False))
        denied = await self.code("POST", self.path("/folders"), 403, json={"name": "资料"})
        self.assertEqual((denied["code"], denied["tier"]), ("folders_require_plus", "pro"))
        await self.grant("pro_plus", 30)
        created = await self.folder("资料")
        self.assertEqual((created["name"], created["file_count"], created["total_size"], created["created_by"]["username"]), ("资料", 0, 0, "alice"))
        self.assertTrue(created["created_at"] and created["updated_at"])
        self.assertEqual(self.events("group.file.folders.changed")[-1], {"conversation_id": self.group_id, "action": "created", "folder_id": created["id"]})
        await self.json("POST", "/admin/group-plus/grant", as_="root", json={"conversation_id": self.group_id, "tier": "max", "permanent": True})
        self.assertTrue((await self.json("GET", f"/conversations/{self.group_id}/plus", as_="dave"))["files"]["folders_enabled"])
        await self.folder("永久档位也能建")
        listing = await self.json("GET", self.path(), as_="alice")
        self.assertEqual((listing["can_manage_folders"], listing["folder_limit"]), (True, 50))

    async def test_permissions_names_and_limit(self) -> None:
        await self.grant("max", 30)
        # 普通成员不能建、改、删、移动，但能看
        self.assertEqual((await self.code("POST", self.path("/folders"), 403, as_="carol", json={"name": "x"}))["code"], "not_group_staff")
        made = await self.folder("Docs", as_="bob")
        member_view = await self.json("GET", self.path("/folders"), as_="carol")
        self.assertEqual(([f["name"] for f in member_view["items"]], member_view["can_manage_folders"], member_view["can_tidy_folders"]), (["Docs"], False, False))
        self.assertEqual((await self.code("PATCH", self.path(f"/folders/{made['id']}"), 403, as_="carol", json={"name": "y"}))["code"], "not_group_staff")
        self.assertEqual((await self.code("DELETE", self.path(f"/folders/{made['id']}"), 403, as_="dave"))["code"], "not_group_staff")
        file = await self.put()
        moved = await self.code("POST", self.path("/move"), 403, as_="carol", json={"file_ids": [file["id"]], "folder_id": made["id"]})
        self.assertEqual(moved["code"], "not_group_staff")
        # 外人看不到
        await self.call("GET", self.path("/folders"), as_="eve", expected=404)
        # 名字：去首尾空白；不区分大小写不重名；长度、换行、/
        self.assertEqual((await self.code("POST", self.path("/folders"), 409, json={"name": "  docs "}))["code"], "folder_name_taken")
        self.assertEqual((await self.folder("  报告  "))["name"], "报告")
        for bad in ("", "   ", "a" * 41, "a\nb", "a/b", "a\\b", "a​b", 3):
            self.assertEqual((await self.code("POST", self.path("/folders"), 422, json={"name": bad}))["code"], "invalid_folder_name", bad)
        self.assertEqual((await self.folder("名" * 40))["name"], "名" * 40)
        # 重命名：可以只改大小写；不能和别的重名
        renamed = await self.json("PATCH", self.path(f"/folders/{made['id']}"), as_="alice", json={"name": "DOCS"})
        self.assertEqual(renamed["name"], "DOCS")
        self.assertEqual(self.events("group.file.folders.changed")[-1]["action"], "renamed")
        self.assertEqual((await self.code("PATCH", self.path(f"/folders/{made['id']}"), 409, json={"name": "报告"}))["code"], "folder_name_taken")
        self.assertEqual((await self.code("PATCH", self.path("/folders/9999"), 404, json={"name": "z"}))["code"], "folder_not_found")
        # 50 个上限
        for index in range(47):
            await self.folder(f"f{index}")
        self.assertEqual(len((await self.json("GET", self.path("/folders")))["items"]), 50)
        over = await self.code("POST", self.path("/folders"), 409, json={"name": "第 51 个"})
        self.assertEqual((over["code"], over["limit"]), ("folder_limit", 50))
        # 别的群的文件夹当不存在
        self.assertEqual((await self.code("GET", f"/conversations/{self.group_id}/files?folder_id=9999", 404))["code"], "folder_not_found")

    async def test_move_list_search_and_delete(self) -> None:
        await self.grant("pro_plus", 30)
        with patch.dict(group_plus_tiers.TIERS["pro_plus"], {"file_quota_bytes": 20, "file_temp_max_bytes": 10}):
            docs = await self.folder("文档")
            pics = await self.folder("图片")
            a = await self.put("a.txt", b"x" * 5)
            b = await self.put("b.txt", b"x" * 7, as_="dave")
            c = await self.put("c.txt", b"x" * 3)
            temp = await self.put("temp.txt", b"x" * 9)
            self.assertTrue(temp["temporary"])
            self.assertEqual((a["folder_id"], a["folder_name"]), (None, None))
            # 移进文件夹
            result = await self.json("POST", self.path("/move"), as_="bob", json={"file_ids": [b["id"], a["id"], a["id"]], "folder_id": docs["id"]})
            self.assertEqual((result["moved"], result["folder_id"], [f["id"] for f in result["items"]]), (2, docs["id"], [b["id"], a["id"]]))
            self.assertEqual({(f["folder_id"], f["folder_name"]) for f in result["items"]}, {(docs["id"], "文档")})
            event = self.events("group.file.updated")[-1]
            self.assertEqual((event["action"], event["file_ids"], event["folder_id"]), ("moved", [b["id"], a["id"]], docs["id"]))
            # 临时文件不能移进去（整个请求不生效）
            rejected = await self.code("POST", self.path("/move"), 422, json={"file_ids": [c["id"], temp["id"]], "folder_id": pics["id"]})
            self.assertEqual((rejected["code"], rejected["temporary_ids"], rejected["expired_ids"]), ("file_not_movable", [temp["id"]], []))
            missing = await self.code("POST", self.path("/move"), 404, json={"file_ids": [c["id"], 9999], "folder_id": pics["id"]})
            self.assertEqual((missing["code"], missing["file_ids"]), ("file_not_found", [9999]))
            self.assertEqual((await self.code("POST", self.path("/move"), 404, json={"file_ids": [c["id"]], "folder_id": 9999}))["code"], "folder_not_found")
            await self.json("POST", self.path("/move"), expected=422, json={"file_ids": [], "folder_id": None})
            await self.json("POST", self.path("/move"), expected=422, json={"file_ids": list(range(1, 102)), "folder_id": None})
            # 根目录：根目录的文件 + 全部文件夹（带数量、大小）
            root = await self.json("GET", self.path(), as_="carol")
            self.assertEqual(([f["id"] for f in root["items"]], root["total"], root["folder_id"], root["folder"], root["search"]), ([temp["id"], c["id"]], 2, None, None, False))
            self.assertEqual([(f["name"], f["file_count"], f["total_size"]) for f in root["folders"]], [("文档", 2, 12), ("图片", 0, 0)])
            self.assertEqual((root["can_manage_folders"], root["can_tidy_folders"], root["quota"]["file_count"]), (False, False, 4))
            # 文件夹里
            inside = await self.json("GET", self.path(f"?folder_id={docs['id']}"), as_="carol")
            self.assertEqual(([f["id"] for f in inside["items"]], inside["folder_id"], inside["folder"]["file_count"]), ([b["id"], a["id"]], docs["id"], 2))
            # 搜索：跨文件夹，带 folder_name；带了 folder_id 也忽略
            found = await self.json("GET", self.path(f"?q=.txt&folder_id={pics['id']}"), as_="carol")
            self.assertEqual((found["search"], found["total"]), (True, 4))
            self.assertEqual({f["name"]: f["folder_name"] for f in found["items"]}, {"a.txt": "文档", "b.txt": "文档", "c.txt": None, "temp.txt": None})
            by_dave = await self.json("GET", self.path(f"?uploader_id={self.ids['dave']}"), as_="carol")
            self.assertEqual(([f["id"] for f in by_dave["items"]], by_dave["items"][0]["folder_id"]), ([b["id"]], docs["id"]))
            # 移回根目录
            back = await self.json("POST", self.path("/move"), json={"file_ids": [b["id"]], "folder_id": None})
            self.assertEqual((back["items"][0]["folder_id"], back["folder_id"]), (None, None))
            # 删文件夹：文件回根目录，不删文件
            await self.json("POST", self.path("/move"), json={"file_ids": [c["id"]], "folder_id": docs["id"]})
            deleted = await self.json("DELETE", self.path(f"/folders/{docs['id']}"), as_="bob")
            self.assertEqual(deleted, {"folder_id": docs["id"], "moved_to_root": 2})
            self.assertEqual(self.events("group.file.folders.changed")[-1], {"conversation_id": self.group_id, "action": "deleted", "folder_id": docs["id"], "moved_to_root": 2})
            root = await self.json("GET", self.path(), as_="carol")
            self.assertEqual((root["total"], [f["name"] for f in root["folders"]]), (4, ["图片"]))
            self.assertEqual(await self.count(GroupFile), 4)
            self.assertEqual((await self.code("DELETE", self.path(f"/folders/{docs['id']}"), 404))["code"], "folder_not_found")
            await self.call("GET", self.path(f"/{a['id']}/download"), as_="dave", expected=200)

    async def test_expired_file_cannot_move_in(self) -> None:
        await self.grant("pro_plus", 30)
        folder = await self.folder("资料")
        short = await self.put("short.txt")
        keep = await self.put("keep.txt", as_="alice", permanent=True)
        await self.json("POST", self.path("/move"), json={"file_ids": [short["id"]], "folder_id": folder["id"]})
        self.tick(hours=25)
        # 过期的不算进 file_count；不能再移进去，但能移回根目录
        self.assertEqual((await self.json("GET", self.path("/folders")))["items"][0]["file_count"], 0)
        await self.json("POST", self.path("/move"), json={"file_ids": [short["id"]], "folder_id": None})
        rejected = await self.code("POST", self.path("/move"), 422, json={"file_ids": [short["id"], keep["id"]], "folder_id": folder["id"]})
        self.assertEqual((rejected["code"], rejected["expired_ids"], rejected["temporary_ids"]), ("file_not_movable", [short["id"]], []))

    async def test_upload_into_folder(self) -> None:
        await self.grant("pro_plus", 30)
        folder = await self.folder("作业")
        # 普通成员也能传进文件夹；事件带 folder_id
        file = await self.put("hw.txt", folder_id=folder["id"])
        self.assertEqual((file["folder_id"], file["folder_name"]), (folder["id"], "作业"))
        self.assertEqual(self.events("group.file.updated")[-1]["folder_id"], folder["id"])
        self.assertEqual((await self.json("GET", self.path(f"?folder_id={folder['id']}"), as_="dave"))["total"], 1)
        self.assertEqual((await self.json("GET", self.path(), as_="dave"))["total"], 0)
        missing = await self.code("POST", self.path(), 404, as_="carol", **self.upload(folder_id=9999))
        self.assertEqual(missing["code"], "folder_not_found")
        # 会变成临时文件的：忽略 folder_id，放根目录
        with patch.dict(group_plus_tiers.TIERS["pro_plus"], {"file_quota_bytes": 5, "file_temp_max_bytes": 100}):
            temp = await self.put("big.txt", b"x" * 50, folder_id=folder["id"])
        self.assertEqual((temp["temporary"], temp["folder_id"]), (True, None))
        # 被禁言的照样不能传
        await self.json("PATCH", f"/conversations/{self.group_id}/settings", as_="alice", json={"all_muted": True})
        self.assertEqual((await self.code("POST", self.path(), 403, as_="carol", **self.upload(folder_id=folder["id"])))["code"], "muted")

    async def test_direct_upload_into_folder(self) -> None:
        from app.services import storage

        await self.grant("pro_plus", 30)
        folder = await self.folder("直传")
        keys: list[str] = []

        def fake_put(key: str, size: int, expires_in: int = 900) -> tuple[str, dict[str, str]]:
            keys.append(key)
            return f"https://bucket.test/{key}?sig=put", {"Content-Type": "application/octet-stream"}

        def stored(key: str, data: bytes) -> None:
            target = Path(settings.upload_dir) / key
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)

        with patch.object(storage, "direct_transfer_enabled", lambda: True), patch.object(storage, "presign_put", fake_put):
            ticket = await self.json("POST", self.path("/uploads"), as_="carol", expected=201, json={"name": "a.zip", "size": 4, "folder_id": folder["id"]})
            self.assertEqual((ticket["temporary"], ticket["folder_id"]), (False, folder["id"]))
            stored(keys[-1], b"abcd")
            done = await self.json("POST", self.path(f"/uploads/{ticket['upload_id']}/complete"), as_="carol", expected=201, json={"announce": False})
            self.assertEqual((done["file"]["folder_id"], done["file"]["folder_name"]), (folder["id"], "直传"))
            self.assertEqual((await self.code("POST", self.path("/uploads"), 404, as_="carol", json={"name": "a", "size": 4, "folder_id": 9999}))["code"], "folder_not_found")
            # 开单之后文件夹被删了：complete 放根目录
            ticket = await self.json("POST", self.path("/uploads"), as_="carol", expected=201, json={"name": "b.zip", "size": 4, "folder_id": folder["id"]})
            stored(keys[-1], b"abcd")
            await self.json("DELETE", self.path(f"/folders/{folder['id']}"))
            done = await self.json("POST", self.path(f"/uploads/{ticket['upload_id']}/complete"), as_="carol", expected=201, json={"announce": False})
            self.assertEqual(done["file"]["folder_id"], None)
        # Pro 不能指定文件夹
        async with self.sessions() as db:
            conversation = await db.get(Conversation, self.group_id)
            conversation.plus_tier = "pro"
            await db.commit()
        with patch.object(storage, "direct_transfer_enabled", lambda: True), patch.object(storage, "presign_put", fake_put):
            denied = await self.code("POST", self.path("/uploads"), 403, as_="carol", json={"name": "a", "size": 4, "folder_id": 1})
        self.assertEqual(denied["code"], "folders_require_plus")

    async def test_after_downgrade(self) -> None:
        await self.grant("pro_plus", 30)
        keep = await self.folder("留着")
        drop = await self.folder("删掉")
        inside = await self.put("in.txt", as_="alice", permanent=True, folder_id=keep["id"])
        other = await self.put("other.txt", as_="alice", permanent=True, folder_id=drop["id"])
        root_file = await self.put("root.txt", as_="alice", permanent=True)

        async def check_downgraded() -> None:
            listing = await self.json("GET", self.path(f"?folder_id={keep['id']}"), as_="dave")
            self.assertEqual(([f["id"] for f in listing["items"]], listing["quota"]["folders_enabled"]), ([inside["id"]], False))
            await self.call("GET", self.path(f"/{inside['id']}/download"), as_="dave", expected=200)
            staff = await self.json("GET", self.path(), as_="bob")
            self.assertEqual((staff["can_manage_folders"], staff["can_tidy_folders"]), (False, True))
            # 不能新建、重命名、移入、传进去
            self.assertEqual((await self.code("POST", self.path("/folders"), 403, json={"name": "新"}))["code"], "folders_require_plus")
            self.assertEqual((await self.code("PATCH", self.path(f"/folders/{keep['id']}"), 403, json={"name": "改"}))["code"], "folders_require_plus")
            moved = await self.code("POST", self.path("/move"), 403, json={"file_ids": [root_file["id"]], "folder_id": keep["id"]})
            self.assertEqual(moved["code"], "folders_require_plus")

        # 到期进宽限期（免费档）：浏览下载照常，不能新建 / 重命名 / 移入
        self.tick(days=31)
        await check_downgraded()
        # 降到 Pro：一样；也不能传进文件夹
        await self.grant("pro", 30)
        await check_downgraded()
        self.assertEqual((await self.code("POST", self.path(), 403, as_="carol", **self.upload(folder_id=keep["id"])))["code"], "folders_require_plus")
        # 能移回根目录、删文件夹（文件回根目录）
        back = await self.json("POST", self.path("/move"), as_="bob", json={"file_ids": [inside["id"]], "folder_id": None})
        self.assertIsNone(back["items"][0]["folder_id"])
        deleted = await self.json("DELETE", self.path(f"/folders/{drop['id']}"), as_="bob")
        self.assertEqual(deleted["moved_to_root"], 1)
        root = await self.json("GET", self.path(), as_="dave")
        self.assertEqual({f["id"] for f in root["items"]}, {inside["id"], other["id"], root_file["id"]})
        # 过了宽限期、文件全被清掉之后，文件夹也清掉
        self.tick(days=31 + 31)
        async with self.sessions() as db:
            await file_service.sweep(db)
        self.assertEqual((await self.count(GroupFile), await self.count(GroupFileFolder)), (0, 0))


if __name__ == "__main__":
    unittest.main()
