"""防小号：设备令牌、信号记录、关联强度、设备封禁、注册限制（docs/device-bans.md）。"""

from __future__ import annotations

import time
import unittest
from datetime import UTC, datetime, timedelta

import httpx
from fastapi import FastAPI, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.datastructures import Headers

import app.models  # noqa: F401
from app.api.v1 import auth as auth_api
from app.core import device as device_core
from app.core.database import Base
from app.core.security import hash_password
from app.models.device import DeviceBan, DeviceSignal
from app.models.enums import PlatformRole, UserStatus
from app.models.user import User
from app.schemas.auth import UserLogin, UserRegister
from app.services import auth as auth_service
from app.services import device_bans, device_signals
from app.services import penalties as penalty_service

MACHINE_A = "a" * 64
FP_A = "f" * 64


def _create_tables(connection) -> None:
    names = (
        "users",
        "auth_sessions",
        "device_signals",
        "device_bans",
        "system_settings",
        "admin_audit_logs",
        "user_violations",
        "jury_reviews",
        "conversations",
        "conversation_members",
    )
    Base.metadata.create_all(connection, tables=[Base.metadata.tables[name] for name in names])


def _context(ip: str = "10.0.0.1", *, token: str | None = None, machine: str | None = None, fp: str | None = None, platform: str = "desktop"):
    headers = {"x-forwarded-for": ip, "x-kuke-platform": platform}
    if token:
        headers["x-kuke-device"] = token
    if machine:
        headers["x-kuke-machine"] = machine
    if fp:
        headers["x-kuke-fp"] = fp
    return device_core.context_from_headers(Headers(headers), None)


class TokenTests(unittest.TestCase):
    def test_issued_token_verifies_and_tampering_fails(self) -> None:
        token = device_core.issue_token()
        self.assertRegex(token, r"^kd_[A-Za-z0-9_-]{22}\.[A-Za-z0-9_-]{22}$")
        device_id = device_core.verify_token(token)
        self.assertEqual(token[3:25], device_id)
        tampered = token[:-1] + ("A" if token[-1] != "A" else "B")
        self.assertIsNone(device_core.verify_token(tampered))
        forged = f"kd_{'x' * 22}.{token.split('.')[1]}"
        self.assertIsNone(device_core.verify_token(forged))
        self.assertIsNone(device_core.verify_token("garbage"))
        self.assertIsNone(device_core.verify_token(None))

    def test_context_stores_only_hashes(self) -> None:
        token = device_core.issue_token()
        context = _context("112.39.99.12", token=token, machine=MACHINE_A.upper(), fp=FP_A)
        self.assertTrue(context.has_device)
        self.assertEqual(set(context.hashes), {"device", "machine", "fp"})
        for value in context.hashes.values():
            self.assertEqual(len(value), 64)
            self.assertNotIn(value, (MACHINE_A, FP_A, token))
        # 同一个值不同种类算出来不一样，大小写不影响
        self.assertEqual(context.hashes["machine"], device_core.signal_hash("machine", MACHINE_A))
        self.assertNotEqual(device_core.signal_hash("machine", MACHINE_A), device_core.signal_hash("fp", MACHINE_A))
        self.assertEqual(context.prefix, "112.39.99.0/24")
        self.assertEqual(context.platform, "desktop")
        # 格式不对的头直接忽略
        bad = _context(machine="not-a-hash", fp="12" * 10)
        self.assertEqual(bad.hashes, {})
        self.assertFalse(bad.has_device)

    def test_ipv6_prefix_is_slash_64(self) -> None:
        self.assertEqual(device_core.ip_prefix("2408:8207:1:2:3:4:5:6"), "2408:8207:1:2::/64")
        self.assertIsNone(device_core.ip_prefix("unknown"))


class DeviceTokenEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        api = FastAPI()
        api.include_router(auth_api.router, prefix="/api/v1/auth")
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=device_core.DeviceTokenMiddleware(api)), base_url="http://testserver")

    async def asyncTearDown(self) -> None:
        await self.client.aclose()

    async def test_issues_when_missing_and_echoes_when_valid(self) -> None:
        response = await self.client.get("/api/v1/auth/device")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["issued"])
        self.assertEqual(response.headers[device_core.ISSUE_HEADER], body["device"])
        self.assertIsNotNone(device_core.verify_token(body["device"]))

        again = await self.client.get("/api/v1/auth/device", headers={"X-Kuke-Device": body["device"]})
        self.assertEqual(again.json(), {"device": body["device"], "issued": False})
        self.assertNotIn(device_core.ISSUE_HEADER, again.headers)


class DeviceBanTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(_create_tables)
        device_signals.reset_runtime_state()
        device_bans.reset_cache()
        self.password = hash_password("secret123")
        async with self.sessions() as db:
            db.add(User(username="admin", hashed_password="x", platform_role=PlatformRole.ADMIN))
            await db.commit()
        self.admin_id = 1

    async def asyncTearDown(self) -> None:
        device_signals.reset_runtime_state()
        device_bans.reset_cache()
        await self.engine.dispose()

    async def _user(self, db, name: str, **extra) -> User:
        user = User(username=name, hashed_password=self.password, **extra)
        db.add(user)
        await db.commit()
        return user

    async def _set(self, db, **changes) -> None:
        admin = await db.get(User, self.admin_id)
        await device_bans.admin_update_settings(db, admin, changes)

    async def _register(self, db, name: str, context, ip: str | None = None):
        return await auth_service.register_user(db, UserRegister(username=name, password="secret123"), ip or context.ip, context)

    # ---------------------------------------------------------------- 记录

    async def test_signal_writes_are_throttled_per_user_and_signal(self) -> None:
        token = device_core.issue_token()
        async with self.sessions() as db:
            user = await self._user(db, "member")
        context = _context("10.1.1.1", token=token, machine=MACHINE_A)
        for _ in range(5):
            device_signals.note(user.id, context)
        self.assertEqual(await device_signals.flush(self.sessions), 3)  # device + machine + ip，各一条
        device_signals.note(user.id, context)
        self.assertEqual(await device_signals.flush(self.sessions), 0)  # 一小时内不再记

        # 过了节流间隔再来一次，同一行 seen_count + 1
        for key in list(device_signals._last_noted):
            device_signals._last_noted[key] = time.monotonic() - device_signals.SEEN_INTERVAL - 1
        device_signals.note(user.id, context)
        self.assertEqual(await device_signals.flush(self.sessions), 3)
        async with self.sessions() as db:
            rows = (await db.execute(select(DeviceSignal).where(DeviceSignal.user_id == user.id))).scalars().all()
        self.assertEqual(len(rows), 3)
        self.assertEqual({row.seen_count for row in rows}, {2})
        ip_row = next(row for row in rows if row.kind == "ip")
        self.assertEqual((ip_row.value_hash, ip_row.prefix, ip_row.platform), ("10.1.1.1", "10.1.1.0/24", "desktop"))
        device_row = next(row for row in rows if row.kind == "device")
        self.assertNotIn(token, device_row.value_hash)

    # ---------------------------------------------------------------- 关联

    async def _linked_family(self, db):
        """root 和 alt 同一个设备令牌；fp_twin 只是浏览器指纹一样；ip_neighbor 只是 IP 一样。"""
        token = device_core.issue_token()
        root = await self._user(db, "root")
        alt = await self._user(db, "alt")
        fp_twin = await self._user(db, "fp_twin")
        neighbor = await self._user(db, "neighbor")
        await device_signals.record_now(db, root.id, _context("10.2.2.2", token=token, fp=FP_A))
        await device_signals.record_now(db, alt.id, _context("10.9.9.9", token=token))
        await device_signals.record_now(db, fp_twin.id, _context("10.8.8.8", fp=FP_A))
        await device_signals.record_now(db, neighbor.id, _context("10.2.2.2"))
        await db.commit()
        return root, alt, fp_twin, neighbor

    async def test_link_strengths_and_ip_alone_is_weak(self) -> None:
        async with self.sessions() as db:
            root, alt, fp_twin, neighbor = await self._linked_family(db)
            result = await device_bans.user_devices(db, root.id)
        strengths = {item["user"]["id"]: item["strength"] for item in result["linked"]}
        self.assertEqual(strengths, {alt.id: "strong", fp_twin.id: "medium", neighbor.id: "weak"})
        self.assertEqual([item["user"]["id"] for item in result["linked"]], [alt.id, fp_twin.id, neighbor.id])
        neighbor_link = next(item for item in result["linked"] if item["user"]["id"] == neighbor.id)
        self.assertEqual({reason["kind"] for reason in neighbor_link["reasons"]}, {"ip"})
        # 同一时间用的同一个 IP：仍然是弱，只是附上说明
        self.assertIsNotNone(neighbor_link["reasons"][0]["detail"])
        # 脱敏：哈希只给前 8 位
        device_row = next(row for row in result["signals"] if row["kind"] == "device")
        self.assertTrue(device_row["masked"].endswith("…"))
        self.assertEqual(len(device_row["masked"]), 9)
        self.assertEqual(device_row["shared_accounts"], 1)

    async def test_legacy_user_ip_columns_count_as_weak_links(self) -> None:
        async with self.sessions() as db:
            root = await self._user(db, "root", registration_ip="112.39.99.3", last_login_ip="112.41.59.194")
            other = await self._user(db, "other", registration_ip="112.41.59.194", last_seen_ip="112.41.59.194")
            result = await device_bans.user_devices(db, root.id)
            cluster = await device_bans.user_cluster(db, root.id)
        self.assertEqual([(item["user"]["id"], item["strength"]) for item in result["linked"]], [(other.id, "weak")])
        self.assertEqual(cluster["accounts"], [])

    async def test_cluster_follows_strong_and_medium_only(self) -> None:
        async with self.sessions() as db:
            root, alt, fp_twin, neighbor = await self._linked_family(db)
            # alt 还有一台机器，另一个号 far 也在那台机器上：两跳也能连到
            far = await self._user(db, "far")
            await device_signals.record_now(db, alt.id, _context("10.7.7.7", machine=MACHINE_A))
            await device_signals.record_now(db, far.id, _context("10.6.6.6", machine=MACHINE_A))
            await db.commit()
            cluster = await device_bans.user_cluster(db, root.id)
        found = {item["user"]["id"]: item["strength"] for item in cluster["accounts"]}
        self.assertEqual(found, {alt.id: "strong", far.id: "strong", fp_twin.id: "medium"})
        self.assertNotIn(neighbor.id, found)

    async def test_bot_and_owner_are_linked(self) -> None:
        async with self.sessions() as db:
            owner = await self._user(db, "owner")
            bot = await self._user(db, "bot_1", is_bot=True, bot_owner_id=owner.id)
            result = await device_bans.user_devices(db, bot.id)
        self.assertEqual([(item["user"]["id"], item["strength"]) for item in result["linked"]], [(owner.id, "strong")])

    # ---------------------------------------------------------------- 封禁生效

    async def test_device_ban_blocks_api_login_and_register_but_not_admins(self) -> None:
        token = device_core.issue_token()
        context = _context("10.3.3.3", token=token)
        async with self.sessions() as db:
            member = await self._user(db, "member")
            await device_signals.record_now(db, member.id, context)
            await db.commit()
            signal = (await db.execute(select(DeviceSignal).where(DeviceSignal.kind == "device"))).scalar_one()
            admin = await db.get(User, self.admin_id)
            ban = await device_bans.create_ban(db, admin, {"signal_id": signal.id, "reason": "小号", "permanent": True})
        self.assertEqual((ban["kind"], ban["status"], ban["permanent"], ban["affected_accounts"]), ("device", "active", True, 1))

        async with self.sessions() as db:
            member = await db.get(User, member.id)
            with self.assertRaises(HTTPException) as caught:
                await device_bans.guard_request(db, context, member)
            self.assertEqual(caught.exception.status_code, 403)
            self.assertEqual(caught.exception.detail["code"], "device_banned")
            self.assertTrue(caught.exception.detail["permanent"])
            # 管理员不被拦
            await device_bans.guard_request(db, context, await db.get(User, self.admin_id))
            # 没带这个令牌的请求不受影响
            await device_bans.guard_request(db, _context("10.3.3.3"), member)

            # 登录：密码对了也拦，但先把这次的信号记下来
            with self.assertRaises(HTTPException) as caught:
                await auth_service.login_user(db, UserLogin(username_or_email="member", password="secret123"), "10.3.3.4", _context("10.3.3.4", token=token))
            self.assertEqual(caught.exception.detail["code"], "device_banned")
            ips = (await db.execute(select(DeviceSignal.value_hash).where(DeviceSignal.user_id == member.id, DeviceSignal.kind == "ip"))).scalars().all()
            self.assertIn("10.3.3.4", ips)

            with self.assertRaises(HTTPException) as caught:
                await self._register(db, "newbie", _context("10.4.4.4", token=token))
            self.assertEqual(caught.exception.detail["code"], "device_banned")
            self.assertIsNone((await db.execute(select(User).where(User.username == "newbie"))).scalar_one_or_none())

            # 解除后恢复
            await device_bans.lift_ban(db, admin, ban["id"], "误封")
            await device_bans.guard_request(db, context, member)

    async def test_ip_ban_defaults_to_register_only_and_must_expire(self) -> None:
        async with self.sessions() as db:
            admin = await db.get(User, self.admin_id)
            member = await self._user(db, "member")
            with self.assertRaises(HTTPException) as caught:
                await device_bans.create_ban(db, admin, {"kind": "ip", "value": "112.39.99.0/24", "permanent": True})
            self.assertEqual(caught.exception.status_code, 422)
            ban = await device_bans.create_ban(db, admin, {"kind": "ip", "value": "112.39.99.0/24", "reason": "换 IP 注册小号"})
        self.assertEqual((ban["scope"], ban["permanent"], ban["value"]), ("register", False, "112.39.99.0/24"))
        expires_at = datetime.fromisoformat(str(ban["expires_at"])) if isinstance(ban["expires_at"], str) else ban["expires_at"]
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        self.assertAlmostEqual((expires_at - datetime.now(UTC)).total_seconds(), 7 * 86400, delta=120)

        async with self.sessions() as db:
            # 只拦注册：同一网段已登录的人照常用
            await device_bans.guard_request(db, _context("112.39.99.50"), await db.get(User, member.id))
            with self.assertRaises(HTTPException) as caught:
                await self._register(db, "fresh", _context("112.39.99.51"))
            self.assertEqual(caught.exception.detail["code"], "device_banned")

    # ---------------------------------------------------------------- 注册限制

    async def test_registration_limit_per_device_and_machine(self) -> None:
        token = device_core.issue_token()
        async with self.sessions() as db:
            for index in range(3):
                await self._register(db, f"kid{index}", _context(f"10.{index}.0.1", token=token))
            with self.assertRaises(HTTPException) as caught:
                await self._register(db, "kid3", _context("10.50.0.1", token=token))
            self.assertEqual(caught.exception.status_code, 403)
            self.assertEqual(caught.exception.detail["code"], "too_many_accounts_on_device")
            self.assertEqual(caught.exception.detail["limit"], 3)

            # 清掉令牌（换浏览器 / 清数据）但机器码还在：照样拦
            await self._register(db, "pc0", _context("10.60.0.1", machine=MACHINE_A))
            await self._set(db, max_accounts_per_device=1)
            with self.assertRaises(HTTPException) as caught:
                await self._register(db, "pc1", _context("10.61.0.1", token=device_core.issue_token(), machine=MACHINE_A))
            self.assertEqual(caught.exception.detail["code"], "too_many_accounts_on_device")
            # 0 = 不限
            await self._set(db, max_accounts_per_device=0)
            await self._register(db, "pc2", _context("10.62.0.1", machine=MACHINE_A))
            registered = await db.scalar(
                select(func.count()).select_from(DeviceSignal).where(DeviceSignal.kind == "machine", DeviceSignal.registered_here.is_(True))
            )
            self.assertEqual(registered, 2)

    async def test_registration_ip_prefix_throttle_and_device_required(self) -> None:
        async with self.sessions() as db:
            await self._set(db, ip_registrations_per_day=2)
            await self._register(db, "aaa1", _context("10.70.0.1", token=device_core.issue_token()))
            await self._register(db, "aaa2", _context("10.70.0.2", token=device_core.issue_token()))
            with self.assertRaises(HTTPException) as caught:
                await self._register(db, "aaa3", _context("10.70.0.3", token=device_core.issue_token()))
            self.assertEqual(caught.exception.status_code, 429)
            self.assertEqual(caught.exception.detail["code"], "registration_ip_throttled")
            # 别的网段不受影响
            await self._register(db, "bbb1", _context("10.71.0.1"))

            await self._set(db, require_device_for_register=True)
            with self.assertRaises(HTTPException) as caught:
                await self._register(db, "bbb2", _context("10.72.0.1"))
            self.assertEqual(caught.exception.detail["code"], "device_required")
            await self._register(db, "bbb3", _context("10.72.0.2", token=device_core.issue_token()))

    # ---------------------------------------------------------------- 封禁此人

    async def test_person_ban_preview_then_apply(self) -> None:
        async with self.sessions() as db:
            root, alt, fp_twin, neighbor = await self._linked_family(db)
            preview = await device_bans.preview_person(db, {"user_id": root.id, "include_linked": True, "include_fingerprint": False, "include_ip": False})
        accounts = {item["user"]["id"]: item for item in preview["accounts"]}
        self.assertEqual(preview["accounts"][0]["user"]["id"], root.id)
        self.assertEqual(set(accounts), {root.id, alt.id, fp_twin.id})
        self.assertNotIn(neighbor.id, accounts)  # 只靠 IP 的不会被带上
        self.assertEqual({item["kind"] for item in preview["signals"]}, {"device"})
        self.assertTrue(all(item["will_ban"] for item in preview["accounts"]))

        async with self.sessions() as db:
            admin = await db.get(User, self.admin_id)
            result = await device_bans.ban_person(
                db,
                admin,
                {"user_id": root.id, "include_linked": False, "include_fingerprint": True, "include_ip": True, "reason": "反复注册小号", "permanent": True},
            )
            penalty_service.discard_notifications(db)
        self.assertEqual(result["banned_user_ids"], [root.id])
        self.assertEqual(sorted(ban["kind"] for ban in result["bans"]), ["device", "fp", "ip"])
        ip_ban = next(ban for ban in result["bans"] if ban["kind"] == "ip")
        self.assertFalse(ip_ban["permanent"])

        async with self.sessions() as db:
            root = await db.get(User, root.id)
            self.assertEqual(root.status, UserStatus.DISABLED)
            self.assertIsNone(root.banned_until)
            self.assertEqual(root.ban_reason, "反复注册小号")
            # alt 用的是同一个设备令牌：账号没封，但这台设备上用不了了
            alt_context = _context("10.9.9.9", token=None)
            alt_context.device_id = "x"
            alt_context.hashes["device"] = (await db.execute(select(DeviceSignal.value_hash).where(DeviceSignal.user_id == alt.id, DeviceSignal.kind == "device"))).scalar_one()
            with self.assertRaises(HTTPException):
                await device_bans.guard_request(db, alt_context, await db.get(User, alt.id))

            # 已经封过的不会重复建；已经永久封禁的账号会被跳过
            again = await device_bans.preview_person(db, {"user_id": root.id, "include_fingerprint": True})
            self.assertTrue(all(item["already_banned"] for item in again["signals"]))
            self.assertEqual(again["accounts"][0]["skip_reason"], "already_banned")

    # ---------------------------------------------------------------- 自动带上

    async def test_account_ban_propagates_only_when_enabled(self) -> None:
        token = device_core.issue_token()
        async with self.sessions() as db:
            first = await self._user(db, "first")
            second = await self._user(db, "second")
            await device_signals.record_now(db, first.id, _context("10.5.5.5", token=token, fp=FP_A))
            await device_signals.record_now(db, second.id, _context("10.5.5.6", machine=MACHINE_A))
            await db.commit()

            # 默认关：封号不带设备
            await penalty_service.record_violation(db, user_id=first.id, category="admin_action", level=0, source="admin", override=penalty_service.Penalty("ban", None))
            await db.commit()
            penalty_service.discard_notifications(db)
            self.assertEqual(await db.scalar(select(func.count()).select_from(DeviceBan)), 0)

            await self._set(db, propagate_enabled=True, propagate_min_ban_days=30)
            # 7 天的封禁不够 30 天：不带
            await penalty_service.record_violation(db, user_id=second.id, category="admin_action", level=0, source="admin", override=penalty_service.Penalty("ban", 24 * 7))
            await db.commit()
            penalty_service.discard_notifications(db)
            self.assertEqual(await db.scalar(select(func.count()).select_from(DeviceBan)), 0)
            # 永久封禁：带上设备令牌和机器码（不带指纹和 IP）
            await penalty_service.record_violation(db, user_id=second.id, category="admin_action", level=0, source="admin", override=penalty_service.Penalty("ban", None))
            await db.commit()
            penalty_service.discard_notifications(db)
            bans = (await db.execute(select(DeviceBan))).scalars().all()
            self.assertEqual([(ban.kind, ban.source, ban.source_user_id, ban.expires_at) for ban in bans], [("machine", "propagated", second.id, None)])

            # 被封的 first 换台设备来登录：新设备也封上
            with self.assertRaises(HTTPException) as caught:
                await auth_service.login_user(db, UserLogin(username_or_email="first", password="secret123"), "10.5.5.9", _context("10.5.5.9", token=device_core.issue_token()))
            self.assertEqual(caught.exception.detail["code"], "account_suspended")
            kinds = (await db.execute(select(DeviceBan.kind).where(DeviceBan.source_user_id == first.id))).scalars().all()
            self.assertEqual(sorted(kinds), ["device", "device"])

            # 解封账号：自动带上的设备封禁一起解除
            await penalty_service.lift_ban(db, await db.get(User, second.id), actor_id=self.admin_id)
            await db.commit()
            active = await db.scalar(select(func.count()).select_from(DeviceBan).where(DeviceBan.source_user_id == second.id, DeviceBan.lifted_at.is_(None)))
            self.assertEqual(active, 0)

    async def test_ban_listing_search_and_settings_validation(self) -> None:
        async with self.sessions() as db:
            admin = await db.get(User, self.admin_id)
            await device_bans.create_ban(db, admin, {"kind": "ip", "value": "10.0.0.9", "duration_hours": 2, "scope": "all"})
            lifted = await device_bans.create_ban(db, admin, {"kind": "ip", "value": "10.0.1.9"})
            await device_bans.lift_ban(db, admin, lifted["id"], None)
            active = await device_bans.list_bans(db, state="active")
            self.assertEqual([item["value"] for item in active["items"]], ["10.0.0.9"])
            everything = await device_bans.list_bans(db, state="all", q="10.0.1")
            self.assertEqual([(item["value"], item["status"]) for item in everything["items"]], [("10.0.1.9", "lifted")])
            with self.assertRaises(HTTPException) as caught:
                await device_bans.admin_update_settings(db, admin, {"max_accounts_per_device": -1})
            self.assertEqual(caught.exception.detail["field"], "max_accounts_per_device")
            values = await device_bans.get_settings(db, fresh=True)
            self.assertEqual(values, device_bans.DEFAULTS)

    async def test_ban_cache_reload_sees_other_workers_bans(self) -> None:
        context = _context("10.10.10.10")
        async with self.sessions() as db:
            member = await self._user(db, "member")
            await device_bans.guard_request(db, context, member)  # 缓存里现在是「没有封禁」
            db.add(DeviceBan(kind="ip", value_hash="10.10.10.10", scope="all", expires_at=datetime.now(UTC) + timedelta(hours=1)))
            await db.commit()
            await device_bans.guard_request(db, context, member)  # 另一个进程写的，缓存没过期还看不到
            device_bans._ban_cache["at"] = 0.0
            with self.assertRaises(HTTPException):
                await device_bans.guard_request(db, context, member)


if __name__ == "__main__":
    unittest.main()
