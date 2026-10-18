"""Cookie-session GET write load and security boundaries (in-memory SQLite only)."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import httpx
from fastapi import FastAPI
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.models  # noqa: F401
from app.api.v1 import auth as auth_routes
from app.core.config import settings
from app.core.database import Base, get_db
from app.models.device import DeviceBan
from app.models.enums import UserStatus
from app.models.user import AuthSession, User
from app.services import auth, device_bans

UTC = timezone.utc
START = datetime(2030, 1, 1, tzinfo=UTC)
IP = "192.0.2.10"
UA = "session-test-agent"
TOKEN = "local-test-cookie-token"


class CookieSessionWriteLoadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Never use app.core.database.engine (which is configured by the environment).
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            # device_bans：/auth/session 会查设备封禁（进程内缓存，30 秒才重读一次）
            await connection.run_sync(Base.metadata.create_all, tables=[User.__table__, AuthSession.__table__, DeviceBan.__table__])
        device_bans.reset_cache()
        async with self.sessions() as db:
            user = User(username="cookie_reader", hashed_password="unused", nickname="initial")
            db.add(user)
            await db.flush()
            self.user_id = user.id
            db.add(AuthSession(
                user_id=user.id,
                session_hash=auth.hash_session_token(TOKEN),
                client_ip=IP,
                user_agent=UA,
                expires_at=START + timedelta(days=1),
            ))
            await db.commit()

        self.selects = self.updates = self.commits = 0

        def count_sql(_connection, _cursor, statement, _parameters, _context, _executemany):
            sql = statement.lstrip().upper()
            if sql.startswith("SELECT"):
                self.selects += 1
            if sql.startswith("UPDATE"):
                self.updates += 1

        self.count_sql = count_sql
        event.listen(self.engine.sync_engine, "before_cursor_execute", count_sql)

        async def isolated_db():
            async with self.sessions() as db:
                event.listen(db.sync_session, "after_commit", self.count_commit)
                yield db

        app = FastAPI()
        app.include_router(auth_routes.router, prefix="/auth")
        app.dependency_overrides[get_db] = isolated_db
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")
        self.clock = START
        # Patch only auth's clock, leaving JWT signing and other services unchanged.
        clock_test = self

        class FrozenDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return clock_test.clock.astimezone(tz) if tz is not None else clock_test.clock.replace(tzinfo=None)

        self.frozen_clock = patch.object(auth, "datetime", FrozenDatetime)
        self.frozen_clock.start()

    def count_commit(self, _session):
        self.commits += 1

    async def asyncTearDown(self):
        self.frozen_clock.stop()
        await self.client.aclose()
        event.remove(self.engine.sync_engine, "before_cursor_execute", self.count_sql)
        await self.engine.dispose()

    async def get(self, *, ip=IP, ua=UA, token=TOKEN):
        return await self.client.get(
            "/auth/session",
            headers={"x-forwarded-for": ip, "user-agent": ua, "cookie": f"{settings.cookie_session_name}={token}"},
        )

    async def session_row(self):
        async with self.sessions() as db:
            result = await db.execute(select(AuthSession).where(AuthSession.session_hash == auth.hash_session_token(TOKEN)))
            return result.scalar_one_or_none()

    async def test_repeated_gets_read_both_rows_without_additional_updates_or_commits(self):
        first = await self.get()
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(self.updates, 2)  # User + AuthSession
        self.assertEqual(self.commits, 1)
        last_used = auth.ensure_aware_utc((await self.session_row()).last_used_at)
        self.selects = self.updates = self.commits = 0
        for i in range(20):
            self.clock = START + timedelta(seconds=i + 1)
            response = await self.get()
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["user"]["nickname"], "initial")
        self.assertEqual(self.selects, 40)  # fresh AuthSession and User query on EVERY GET
        self.assertEqual((self.updates, self.commits), (0, 0))
        self.assertEqual(auth.ensure_aware_utc((await self.session_row()).last_used_at), last_used)

    async def test_60_second_boundary_and_ua_ip_changes_write_immediately(self):
        self.assertEqual((await self.get()).status_code, 200)
        self.updates = self.commits = 0
        self.clock = START + timedelta(seconds=59, milliseconds=999)
        self.assertEqual((await self.get()).status_code, 200)
        self.assertEqual((self.updates, self.commits), (0, 0))
        self.clock = START + timedelta(seconds=60)
        self.assertEqual((await self.get()).status_code, 200)
        self.assertEqual((self.updates, self.commits), (2, 1))
        self.updates = self.commits = 0
        self.clock += timedelta(seconds=1)
        self.assertEqual((await self.get(ua="different-agent")).status_code, 200)
        self.assertEqual((self.updates, self.commits), (2, 1))
        self.assertEqual((await self.session_row()).user_agent, "different-agent")
        async with self.sessions() as db:
            user = await db.get(User, self.user_id)
            self.assertEqual(auth.ensure_aware_utc(user.last_seen_at), self.clock)
            self.assertEqual(user.last_seen_ip, IP)
        self.updates = self.commits = 0
        self.clock += timedelta(seconds=1)
        self.assertEqual((await self.get(ip="192.0.2.11", ua="different-agent")).status_code, 200)
        self.assertEqual((self.updates, self.commits), (2, 1))
        self.assertEqual((await self.session_row()).client_ip, "192.0.2.11")
        async with self.sessions() as db:
            user = await db.get(User, self.user_id)
            self.assertEqual(user.last_seen_ip, "192.0.2.11")
            self.assertEqual(auth.ensure_aware_utc(user.last_seen_at), self.clock)

    async def test_missing_user_seen_and_naive_session_time_force_write(self):
        self.assertEqual((await self.get()).status_code, 200)
        async with self.sessions() as db:
            user = await db.get(User, self.user_id)
            user.last_seen_at = None
            await db.commit()
        self.updates = self.commits = 0
        self.clock += timedelta(seconds=1)
        self.assertEqual((await self.get()).status_code, 200)
        self.assertEqual((self.updates, self.commits), (2, 1))
        # SQLite returns naive timestamps. The next fresh read must still throttle.
        self.updates = self.commits = 0
        self.clock += timedelta(seconds=1)
        self.assertEqual((await self.get()).status_code, 200)
        self.assertEqual((self.updates, self.commits), (0, 0))

    async def test_stale_user_seen_is_not_hidden_by_recent_session(self):
        self.assertEqual((await self.get()).status_code, 200)
        async with self.sessions() as db:
            user = await db.get(User, self.user_id)
            user.last_seen_at = START - timedelta(minutes=2)
            await db.commit()
        self.updates = self.commits = 0
        self.clock += timedelta(seconds=1)
        self.assertEqual((await self.get()).status_code, 200)
        self.assertEqual((self.updates, self.commits), (2, 1))

    async def test_revoked_expired_and_disabled_sessions_never_bypass_checks(self):
        self.assertEqual((await self.get()).status_code, 200)
        async with self.sessions() as db:
            user = await db.get(User, self.user_id)
            user.status = UserStatus.DISABLED
            user.ban_reason = "test suspension"
            await db.commit()
        self.updates = self.commits = 0
        self.clock += timedelta(seconds=1)
        denied = await self.get()
        self.assertEqual(denied.status_code, 403)
        self.assertEqual((self.updates, self.commits), (0, 0))
        async with self.sessions() as db:
            user = await db.get(User, self.user_id)
            user.banned_until = self.clock - timedelta(seconds=1)
            await db.commit()
        self.updates = self.commits = 0
        self.assertEqual((await self.get()).status_code, 200)
        self.assertGreaterEqual(self.updates, 1)  # immediately persist the unban
        self.assertGreaterEqual(self.commits, 1)
        async with self.sessions() as db:
            user = await db.get(User, self.user_id)
            self.assertEqual(user.status, UserStatus.ACTIVE)
            session = (await db.execute(select(AuthSession))).scalar_one()
            session.revoked_at = self.clock
            await db.commit()
        self.assertEqual((await self.get()).status_code, 401)
        async with self.sessions() as db:
            session = (await db.execute(select(AuthSession))).scalar_one()
            session.revoked_at = None
            session.expires_at = self.clock
            await db.commit()
        self.assertEqual((await self.get()).status_code, 401)

    async def test_logout_removes_cookie_session_immediately(self):
        self.assertEqual((await self.get()).status_code, 200)
        headers = {"cookie": f"{settings.cookie_session_name}={TOKEN}"}
        self.assertEqual((await self.client.post("/auth/session/logout", headers=headers)).status_code, 200)
        self.assertEqual((await self.get()).status_code, 401)
        self.assertIsNone(await self.session_row())

    async def test_no_write_response_reads_current_db_user_without_fabricating_seen(self):
        self.assertEqual((await self.get()).status_code, 200)
        async with self.sessions() as db:
            user = await db.get(User, self.user_id)
            user.nickname = "freshly-edited"
            user.last_login_at = START - timedelta(hours=1)
            user.last_login_ip = "198.51.100.7"
            await db.commit()
            expected_updated_at = user.updated_at
        self.updates = self.commits = 0
        self.clock += timedelta(seconds=1)
        response = await self.get()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["user"]["nickname"], "freshly-edited")
        self.assertEqual(auth.ensure_aware_utc(datetime.fromisoformat(response.json()["user"]["updated_at"])), expected_updated_at)
        self.assertEqual((self.updates, self.commits), (0, 0))
        async with self.sessions() as db:
            user = await db.get(User, self.user_id)
            self.assertEqual(auth.ensure_aware_utc(user.last_seen_at), START)
            self.assertEqual(user.last_login_ip, "198.51.100.7")
            self.assertEqual(auth.ensure_aware_utc(user.last_login_at), START - timedelta(hours=1))
