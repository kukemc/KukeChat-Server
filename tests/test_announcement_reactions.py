from __future__ import annotations

import asyncio
import hashlib
import time
import unittest
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Annotated
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import quote

import httpx
from fastapi import Depends, FastAPI, HTTPException
from sqlalchemy import event, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.models  # noqa: F401
from app.api.v1 import announcements as announcements_api
from app.core.config import settings
from app.core.database import Base, configure_sqlite_connection, get_db
from app.core.dependencies import get_current_user
from app.models.announcement import Announcement, AnnouncementReaction
from app.models.enums import PlatformRole
from app.models.user import User
from app.schemas.announcement import AnnouncementCreate, AnnouncementUpdate
from app.services import announcements as announcement_service

TABLES = [Base.metadata.tables[name] for name in ("users", "announcements", "announcement_reactions", "admin_audit_logs")]
OLD_FIELDS = {"id", "title", "content", "cover_url", "is_active", "pinned", "author", "created_at", "updated_at"}


class ReactionTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        asyncio.get_running_loop().set_debug(False)
        # 频率限制有一份进程内的记录，别让上一个用例的残留影响这一个
        announcement_service._recent_cancels.clear()
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(lambda sync: Base.metadata.create_all(sync, tables=TABLES))
        async with self.sessions() as db:
            users = [User(username=name, hashed_password="x") for name in ("alice", "bob", "carol", "dave", "admin")]
            users[-1].platform_role = PlatformRole.ADMIN
            db.add_all(users)
            await db.flush()
            self.ids = {user.username: user.id for user in users}
            base = datetime.now(UTC) - timedelta(days=1)
            notices = [
                Announcement(title="第一条", content="内容一", created_by_id=self.ids["admin"], created_at=base),
                Announcement(title="第二条", content="内容二", created_by_id=self.ids["admin"], created_at=base + timedelta(hours=1)),
                Announcement(title="已下线", content="内容三", created_by_id=self.ids["admin"], is_active=False, created_at=base + timedelta(hours=2)),
            ]
            db.add_all(notices)
            await db.commit()
            self.first, self.second, self.offline = (item.id for item in notices)

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def _user(self, db, name: str) -> User:
        return await db.get(User, self.ids[name])

    async def _toggle(self, db, name: str, emoji: str, announcement_id: int | None = None):
        reactions, _changed = await announcement_service.toggle_reaction(
            db, await self._user(db, name), announcement_id or self.first, emoji
        )
        return reactions

    async def _remove(self, db, name: str, emoji: str, announcement_id: int | None = None):
        return await announcement_service.remove_reaction(db, await self._user(db, name), announcement_id or self.first, emoji)

    async def _code(self, awaitable) -> tuple[int, str | None]:
        with self.assertRaises(HTTPException) as caught:
            await awaitable
        detail = caught.exception.detail
        return caught.exception.status_code, detail.get("code") if isinstance(detail, dict) else None

    async def _rows(self, db, announcement_id: int | None = None) -> int:
        query = select(func.count(AnnouncementReaction.id))
        if announcement_id is not None:
            query = query.where(AnnouncementReaction.announcement_id == announcement_id)
        return int(await db.scalar(query) or 0)

    @staticmethod
    def _flat(reactions) -> list[tuple[str, int, bool]]:
        return [(item.emoji, item.count, item.reacted_by_me) for item in reactions]

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


class ReactionServiceTests(ReactionTestCase):
    async def test_toggle_adds_then_cancels_and_delete_is_idempotent(self) -> None:
        async with self.sessions() as db:
            alice = await self._user(db, "alice")
            reactions, changed = await announcement_service.toggle_reaction(db, alice, self.first, " 👀 ")
            self.assertEqual((self._flat(reactions), changed), ([("👀", 1, True)], True))
            # 同一个 emoji 再发一次就是取消
            reactions, changed = await announcement_service.toggle_reaction(db, alice, self.first, "👀")
            self.assertEqual((reactions, changed, await self._rows(db)), ([], True, 0))

            await self._toggle(db, "alice", "👀")
            reactions, changed = await self._remove(db, "alice", "👀")
            self.assertEqual((reactions, changed), ([], True))
            # 没回应过也照常返回，不算改动
            reactions, changed = await self._remove(db, "alice", "👀")
            self.assertEqual((reactions, changed), ([], False))
            reactions, changed = await self._remove(db, "alice", "🔥")
            self.assertEqual((reactions, changed, await self._rows(db)), ([], False, 0))

    async def test_duplicate_submit_race_returns_current_state(self) -> None:
        """两个请求同时加同一个回应：后到的撞唯一约束，按现状返回，不报错也不算改动。"""
        async with self.sessions() as db:
            await self._toggle(db, "alice", "👀")
            alice = await self._user(db, "alice")
            with patch.object(announcement_service, "_find_reaction", new=AsyncMock(return_value=None)):
                reactions, changed = await announcement_service.toggle_reaction(db, alice, self.first, "👀")
            self.assertEqual((self._flat(reactions), changed, await self._rows(db)), ([("👀", 1, True)], False, 1))
            # 会话回滚过，后面还能正常用
            self.assertEqual(self._flat(await self._toggle(db, "alice", "🔥")), [("👀", 1, True), ("🔥", 1, True)])

    async def test_reacted_by_me_depends_on_the_viewer(self) -> None:
        async with self.sessions() as db:
            await self._toggle(db, "alice", "👀")
            await self._toggle(db, "bob", "🔥")
            self.assertEqual(self._flat(await self._toggle(db, "bob", "👀")), [("👀", 2, True), ("🔥", 1, True)])
            await self._toggle(db, "carol", "🎉", self.second)

            def flags(items) -> dict[int, list[tuple[str, int, bool]]]:
                return {item.id: self._flat(item.reactions) for item in items}

            self.assertEqual(
                flags(await announcement_service.list_active(db, 20, self.ids["alice"])),
                {self.second: [("🎉", 1, False)], self.first: [("👀", 2, True), ("🔥", 1, False)]},
            )
            self.assertEqual(
                flags(await announcement_service.list_active(db, 20, self.ids["carol"])),
                {self.second: [("🎉", 1, True)], self.first: [("👀", 2, False), ("🔥", 1, False)]},
            )
            # 不传当前用户（旧调用方式）时全是 false
            self.assertEqual(
                flags(await announcement_service.list_active(db)),
                {self.second: [("🎉", 1, False)], self.first: [("👀", 2, False), ("🔥", 1, False)]},
            )
            latest = await announcement_service.get_latest(db, self.ids["carol"])
            self.assertEqual((latest.id, self._flat(latest.reactions)), (self.second, [("🎉", 1, True)]))
            latest = await announcement_service.get_latest(db, self.ids["bob"])
            self.assertEqual(self._flat(latest.reactions), [("🎉", 1, False)])

    async def test_sorted_by_count_then_first_seen(self) -> None:
        async with self.sessions() as db:
            for name, emoji in (("alice", "👍"), ("bob", "👀"), ("carol", "🔥"), ("alice", "🔥"), ("bob", "🔥"), ("carol", "👀"), ("alice", "🎉")):
                await self._toggle(db, name, emoji)
            listed = (await announcement_service.list_active(db, 20, self.ids["dave"]))[-1]
            # 👍 和 🎉 都是 1 个：👍 先出现所以在前（按码位排的话 🎉 会在前）
            self.assertLess("🎉", "👍")
            self.assertEqual([(item.emoji, item.count) for item in listed.reactions], [("🔥", 3), ("👀", 2), ("👍", 1), ("🎉", 1)])
            # 「先出现」看的是现存的回应：最早那个 👍 取消后别人再加，就排到 🎉 后面了
            await self._toggle(db, "alice", "👍")
            reactions = await self._toggle(db, "bob", "👍")
            self.assertEqual([(item.emoji, item.count) for item in reactions], [("🔥", 3), ("👀", 2), ("🎉", 1), ("👍", 1)])

    async def test_list_query_count_does_not_grow_with_announcements(self) -> None:
        async with self.sessions() as db:
            await self._toggle(db, "alice", "👀")
            with self._statements() as few:
                self.assertEqual(len(await announcement_service.list_active(db, 50, self.ids["alice"])), 2)
            with self._statements() as few_admin:
                self.assertEqual(len(await announcement_service.admin_list(db)), 3)

            extra = [Announcement(title=f"公告 {index}", content="内容", created_by_id=self.ids["admin"]) for index in range(12)]
            db.add_all(extra)
            await db.commit()
            for index, item in enumerate(extra):
                await self._toggle(db, ("alice", "bob", "carol")[index % 3], "🔥", item.id)
                await self._toggle(db, "dave", "👀", item.id)

        async with self.sessions() as db:
            with self._statements() as many:
                listed = await announcement_service.list_active(db, 50, self.ids["alice"])
            with self._statements() as many_admin:
                admin_listed = await announcement_service.admin_list(db)
            with self._statements() as latest:
                await announcement_service.get_latest(db, self.ids["alice"])
        self.assertEqual((len(listed), len(admin_listed)), (14, 15))
        self.assertEqual(sum(len(item.reactions) for item in listed), 25)
        # 公告一条、全部回应汇总一条，和公告数量无关
        self.assertEqual((len(few), len(many), len(few_admin), len(many_admin), len(latest)), (2, 2, 2, 2, 2))
        self.assertEqual(sum("announcement_reactions" in statement for statement in many), 1)

    async def test_per_user_limit(self) -> None:
        async with self.sessions() as db:
            self.assertEqual(announcement_service.reaction_options()["per_user"], 5)
            with patch.object(settings, "announcement_reaction_per_user", 2):
                await self._toggle(db, "alice", "👍")
                await self._toggle(db, "alice", "🔥")
                self.assertEqual(await self._code(self._toggle(db, "alice", "👀")), (409, "reaction_limit"))
                # 上限按公告算，换一条公告不受影响；别人也不受影响
                await self._toggle(db, "alice", "👀", self.second)
                await self._toggle(db, "bob", "👀")
                # 取消一个之后又能加了
                self.assertEqual(self._flat(await self._toggle(db, "alice", "👍")), [("🔥", 1, True), ("👀", 1, False)])
                reactions = await self._toggle(db, "alice", "👀")
                self.assertEqual(self._flat(reactions), [("👀", 2, True), ("🔥", 1, True)])
            self.assertEqual(await self._rows(db, self.first), 3)

    async def test_per_announcement_limit_still_allows_existing_emoji(self) -> None:
        async with self.sessions() as db:
            with patch.object(settings, "announcement_reaction_per_announcement", 2):
                await self._toggle(db, "alice", "👍")
                await self._toggle(db, "bob", "🔥")
                self.assertEqual(await self._code(self._toggle(db, "carol", "👀")), (409, "reaction_limit"))
                # 已经在这条公告上出现过的 emoji 仍然可以加
                self.assertEqual(self._flat(await self._toggle(db, "carol", "🔥")), [("🔥", 2, True), ("👍", 1, False)])
                self.assertEqual(self._flat(await self._toggle(db, "carol", "👍")), [("👍", 2, True), ("🔥", 2, True)])
                # 某一种没人用了，位置就空出来
                await self._toggle(db, "bob", "🔥")
                await self._toggle(db, "carol", "🔥")
                self.assertEqual(self._flat(await self._toggle(db, "dave", "👀")), [("👍", 2, False), ("👀", 1, True)])

    async def test_non_emoji_is_rejected(self) -> None:
        async with self.sessions() as db:
            for value in ("好", "ok", "👍ok", "", "   ", "👍" * 17):
                with self.assertRaises(HTTPException) as caught:
                    await self._toggle(db, "alice", value)
                self.assertEqual(caught.exception.status_code, 422, value)
                with self.assertRaises(HTTPException) as caught:
                    await self._remove(db, "alice", value)
                self.assertEqual(caught.exception.status_code, 422, value)
            self.assertEqual(await self._rows(db), 0)
            # 带变体选择符、ZWJ 组合、键帽的都是合法 emoji
            for value in ("❤️", "👨‍👩‍👧", "1️⃣"):
                self.assertEqual(self._flat(await self._toggle(db, "alice", value)), [(value, 1, True)])
                await self._toggle(db, "alice", value)

    async def test_offline_or_missing_announcement_is_404(self) -> None:
        async with self.sessions() as db:
            for announcement_id in (self.offline, 987654):
                self.assertEqual(await self._code(self._toggle(db, "alice", "👀", announcement_id)), (404, "announcement_not_found"))
                self.assertEqual(await self._code(self._remove(db, "alice", "👀", announcement_id)), (404, "announcement_not_found"))
            # 下线的公告本来就不在用户侧列表里，后台列表里有
            self.assertEqual([item.id for item in await announcement_service.list_active(db, 20, self.ids["alice"])], [self.second, self.first])
            self.assertEqual([item.id for item in await announcement_service.admin_list(db)], [self.offline, self.second, self.first])

            # 先回应、后下线：之后不能加也不能取消，重新上线后回应还在
            await self._toggle(db, "alice", "👀")
            await announcement_service.admin_update(db, self.ids["admin"], self.first, AnnouncementUpdate(is_active=False))
            self.assertEqual(await self._code(self._toggle(db, "bob", "👀")), (404, "announcement_not_found"))
            self.assertEqual(await self._code(self._remove(db, "alice", "👀")), (404, "announcement_not_found"))
            self.assertIsNone(await announcement_service.reaction_event_payload(db, self.first))
            self.assertEqual([item.id for item in await announcement_service.list_active(db, 20, self.ids["alice"])], [self.second])
            restored = await announcement_service.admin_update(db, self.ids["admin"], self.first, AnnouncementUpdate(is_active=True))
            self.assertEqual(self._flat(restored.reactions), [("👀", 1, False)])
            self.assertEqual(self._flat(await self._toggle(db, "bob", "👀")), [("👀", 2, True)])

    async def test_muted_user_cannot_add_but_can_cancel(self) -> None:
        async with self.sessions() as db:
            await self._toggle(db, "alice", "👀")
            await self._toggle(db, "alice", "🔥")
            alice = await self._user(db, "alice")
            alice.muted_until = datetime.now(UTC) + timedelta(hours=1)
            await db.commit()

            with self.assertRaises(HTTPException) as caught:
                await self._toggle(db, "alice", "🎉")
            self.assertEqual((caught.exception.status_code, caught.exception.detail["code"]), (403, "account_muted"))
            self.assertIn("muted_until", caught.exception.detail)
            self.assertEqual(await self._rows(db), 2)
            # 两种取消方式都不受禁言限制
            self.assertEqual(self._flat(await self._toggle(db, "alice", "👀")), [("🔥", 1, True)])
            reactions, changed = await self._remove(db, "alice", "🔥")
            self.assertEqual((reactions, changed), ([], True))
            # 取消之后想再加回来就不行了
            self.assertEqual(await self._code(self._toggle(db, "alice", "🔥")), (403, "account_muted"))

            # 禁言到期后恢复
            alice.muted_until = datetime.now(UTC) - timedelta(seconds=1)
            await db.commit()
            self.assertEqual(self._flat(await self._toggle(db, "alice", "🔥")), [("🔥", 1, True)])

    async def test_rate_limit_counts_adds_and_cancels_in_a_rolling_minute(self) -> None:
        limited = (429, "reaction_rate_limited")
        async with self.sessions() as db:
            with patch.object(settings, "announcement_reaction_per_minute", 4):
                await self._toggle(db, "alice", "👍")
                await self._toggle(db, "alice", "🔥", self.second)
                await self._toggle(db, "alice", "👍")  # 取消：行没了，这两次操作由进程内的记录接着算
                await self._toggle(db, "alice", "👀")
                self.assertEqual(await self._code(self._toggle(db, "alice", "🎉")), limited)
                # 取消也是一次操作，同样被限
                self.assertEqual(await self._code(self._toggle(db, "alice", "👀")), limited)
                self.assertEqual(await self._code(self._remove(db, "alice", "🔥", self.second)), limited)
                # 什么都不会改的 DELETE 不占次数，也不被限
                reactions, changed = await self._remove(db, "alice", "🎉")
                self.assertEqual((self._flat(reactions), changed), ([("👀", 1, True)], False))
                # 各人各算
                await self._toggle(db, "bob", "👀")
                self.assertEqual(await self._rows(db), 3)

                # 一分钟后窗口滑过去
                later = datetime.now(UTC) + timedelta(seconds=61)
                with patch.object(announcement_service, "_now", return_value=later):
                    await self._toggle(db, "alice", "🎉")
                    self.assertNotIn(self.ids["bob"], announcement_service._recent_cancels)
                    self.assertEqual(announcement_service._recent_cancels.get(self.ids["alice"]), None)

    async def test_rate_limit_adds_are_counted_from_the_table(self) -> None:
        """另一个 worker 进程没有这边的内存记录，但「加」是数表里的行，照样拦得住。"""
        async with self.sessions() as db:
            with patch.object(settings, "announcement_reaction_per_minute", 3):
                for emoji in ("👍", "🔥", "👀"):
                    await self._toggle(db, "alice", emoji)
                self.assertEqual(announcement_service._recent_cancels, {})
                self.assertEqual(await self._code(self._toggle(db, "alice", "🎉")), (429, "reaction_rate_limited"))
                # 一分钟以前加的不算
                await db.execute(
                    text("UPDATE announcement_reactions SET created_at = :stamp WHERE emoji = '👍'"),
                    {"stamp": (datetime.now(UTC) - timedelta(seconds=61)).strftime("%Y-%m-%d %H:%M:%S.%f")},
                )
                await db.commit()
                await self._toggle(db, "alice", "🎉")
                self.assertEqual(await self._code(self._toggle(db, "alice", "😂")), (429, "reaction_rate_limited"))

    async def test_deleting_an_announcement_removes_its_reactions(self) -> None:
        async with self.sessions() as db:
            await self._toggle(db, "alice", "👀")
            await self._toggle(db, "bob", "🔥")
            await self._toggle(db, "bob", "🔥", self.second)
            # 这个测试库的连接没开 PRAGMA foreign_keys，靠的是 admin_delete 里的显式删除
            self.assertEqual((await db.execute(text("PRAGMA foreign_keys"))).scalar(), 0)
            await announcement_service.admin_delete(db, self.ids["admin"], self.first)
            self.assertEqual((await self._rows(db, self.first), await self._rows(db, self.second)), (0, 1))
            self.assertIsNone(await db.get(Announcement, self.first))
            self.assertEqual(await self._code(self._toggle(db, "alice", "👀")), (404, "announcement_not_found"))

    async def test_foreign_key_cascade_works_on_production_style_connections(self) -> None:
        """线上的连接都会执行 configure_sqlite_connection（PRAGMA foreign_keys=ON），这时外键级联本身也生效。"""
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        event.listen(engine.sync_engine, "connect", configure_sqlite_connection)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with async_sessionmaker(engine, expire_on_commit=False)() as db:
                self.assertEqual((await db.execute(text("PRAGMA foreign_keys"))).scalar(), 1)
                users = [User(username="u1", hashed_password="x"), User(username="u2", hashed_password="x")]
                notices = [Announcement(title="甲", content="内容"), Announcement(title="乙", content="内容")]
                db.add_all([*users, *notices])
                await db.flush()
                for user in users:
                    for notice in notices:
                        db.add(AnnouncementReaction(announcement_id=notice.id, user_id=user.id, emoji="👀"))
                await db.commit()
                self.assertEqual(await self._rows(db), 4)
                # 绕过服务函数直接删公告 / 删用户，回应跟着没
                await db.execute(text("DELETE FROM announcements WHERE id = :id"), {"id": notices[0].id})
                await db.commit()
                self.assertEqual((await self._rows(db), await self._rows(db, notices[1].id)), (2, 2))
                await db.execute(text("DELETE FROM users WHERE id = :id"), {"id": users[0].id})
                await db.commit()
                self.assertEqual(await self._rows(db), 1)
        finally:
            await engine.dispose()

    async def test_admin_views_carry_reactions_without_reacted_by_me(self) -> None:
        async with self.sessions() as db:
            await self._toggle(db, "admin", "👀")
            await self._toggle(db, "alice", "👀")
            await self._toggle(db, "alice", "🔥", self.second)
            listed = {item.id: self._flat(item.reactions) for item in await announcement_service.admin_list(db)}
            # 管理员自己回应过，后台里也是 false
            self.assertEqual(listed, {self.offline: [], self.second: [("🔥", 1, False)], self.first: [("👀", 2, False)]})

            created = await announcement_service.admin_create(db, self.ids["admin"], AnnouncementCreate(title="新公告", content="内容"))
            self.assertEqual(created.reactions, [])
            updated = await announcement_service.admin_update(db, self.ids["admin"], self.first, AnnouncementUpdate(title="改个标题"))
            self.assertEqual((updated.title, self._flat(updated.reactions)), ("改个标题", [("👀", 2, False)]))
            self.assertEqual(self._flat((await announcement_service.admin_get(db, self.first)).reactions), [("👀", 2, False)])

    async def test_event_payload_has_no_reacted_by_me(self) -> None:
        async with self.sessions() as db:
            await self._toggle(db, "alice", "👀")
            await self._toggle(db, "bob", "👀")
            await self._toggle(db, "bob", "🔥")
            self.assertEqual(
                await announcement_service.reaction_event_payload(db, self.first),
                {"id": self.first, "reactions": [{"emoji": "👀", "count": 2}, {"emoji": "🔥", "count": 1}]},
            )
            self.assertEqual(await announcement_service.reaction_event_payload(db, self.second), {"id": self.second, "reactions": []})
            self.assertIsNone(await announcement_service.reaction_event_payload(db, self.offline))
            self.assertIsNone(await announcement_service.reaction_event_payload(db, 987654))


class HttpReactionTests(ReactionTestCase):
    """走真实的路由：路径都挂上了、旧字段还在、实时事件的形状和合并。"""

    DELAY = 0.5

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
        self.broadcast = AsyncMock()
        announcements_api._pending_reaction_broadcasts.clear()
        for patcher in (
            patch.object(announcements_api, "REACTION_BROADCAST_DELAY_SECONDS", self.DELAY),
            patch.object(announcements_api, "AsyncSessionLocal", self.sessions),
            patch.object(announcements_api.manager, "broadcast", self.broadcast),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    async def asyncTearDown(self) -> None:
        await self._drain()
        await self.client.aclose()
        await super().asyncTearDown()

    async def _drain(self) -> None:
        tasks = list(announcements_api._reaction_broadcast_tasks)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _post(self, name: str, emoji: str, announcement_id: int | None = None) -> httpx.Response:
        self.acting = name
        return await self.client.post(f"/api/v1/announcements/{announcement_id or self.first}/reactions", json={"emoji": emoji})

    async def _delete(self, name: str, emoji: str, announcement_id: int | None = None) -> httpx.Response:
        self.acting = name
        return await self.client.delete(f"/api/v1/announcements/{announcement_id or self.first}/reactions/{emoji}")

    async def test_routes_are_mounted_and_old_fields_survive(self) -> None:
        paths = self.api.openapi()["paths"]
        for path, methods in (
            ("/api/v1/announcements", {"get"}),
            ("/api/v1/announcements/latest", {"get"}),
            ("/api/v1/announcements/reaction-options", {"get"}),
            ("/api/v1/announcements/{announcement_id}/reactions", {"post"}),
            ("/api/v1/announcements/{announcement_id}/reactions/{emoji}", {"delete"}),
        ):
            self.assertEqual(set(paths[path]), methods, path)

        response = await self.client.get("/api/v1/announcements/reaction-options")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {"quick": ["👍", "❤️", "😂", "😮", "😢", "🎉", "👀", "🔥"], "per_user": 5, "per_announcement": 30, "per_minute": 20},
        )

        response = await self._post("alice", "👀")
        self.assertEqual((response.status_code, response.json()), (200, {"id": self.first, "reactions": [{"emoji": "👀", "count": 1, "reacted_by_me": True}]}))
        response = await self._post("bob", "❤️")
        self.assertEqual(response.json()["reactions"], [{"emoji": "👀", "count": 1, "reacted_by_me": False}, {"emoji": "❤️", "count": 1, "reacted_by_me": True}])

        self.acting = "alice"
        response = await self.client.get("/api/v1/announcements")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual([item["id"] for item in body], [self.second, self.first])
        for item in body:
            self.assertEqual(set(item), OLD_FIELDS | {"reactions"})
        self.assertEqual((body[1]["title"], body[1]["content"], body[1]["is_active"], body[1]["pinned"], body[1]["cover_url"]), ("第一条", "内容一", True, False, None))
        self.assertEqual(body[1]["author"]["username"], "admin")
        self.assertEqual(body[0]["reactions"], [])
        self.assertEqual(body[1]["reactions"], [{"emoji": "👀", "count": 1, "reacted_by_me": True}, {"emoji": "❤️", "count": 1, "reacted_by_me": False}])

        response = await self.client.get("/api/v1/announcements/latest")
        self.assertEqual((response.status_code, response.json()["id"], response.json()["reactions"]), (200, self.second, []))
        self.assertEqual(set(response.json()), OLD_FIELDS | {"reactions"})

        # DELETE：emoji 在路径里（带变体选择符的也能对上），重复删还是 200
        for _ in range(2):
            response = await self._delete("bob", "❤️")
            self.assertEqual((response.status_code, response.json()), (200, {"id": self.first, "reactions": [{"emoji": "👀", "count": 1, "reacted_by_me": False}]}))
        # 同一个 emoji 再 POST 一次是取消
        response = await self._post("alice", "👀")
        self.assertEqual((response.status_code, response.json()), (200, {"id": self.first, "reactions": []}))

    async def test_error_shapes_over_http(self) -> None:
        response = await self._post("alice", "👀", self.offline)
        self.assertEqual((response.status_code, response.json()["detail"]["code"]), (404, "announcement_not_found"))
        self.assertIsInstance(response.json()["detail"]["message"], str)
        self.assertEqual((await self._delete("alice", "👀", 987654)).json()["detail"]["code"], "announcement_not_found")
        # 422 没有 code：文字、空串、缺字段、超长
        for payload in ({"emoji": "好"}, {"emoji": ""}, {}, {"emoji": "👍" * 40}):
            response = await self.client.post(f"/api/v1/announcements/{self.first}/reactions", json=payload)
            self.assertEqual(response.status_code, 422, payload)
        self.assertEqual((await self._delete("alice", "abc")).status_code, 422)

        with patch.object(settings, "announcement_reaction_per_user", 1):
            self.assertEqual((await self._post("alice", "👀")).status_code, 200)
            response = await self._post("alice", "🔥")
            self.assertEqual((response.status_code, response.json()["detail"]["code"]), (409, "reaction_limit"))
        with patch.object(settings, "announcement_reaction_per_minute", 1):
            response = await self._post("alice", "🔥")
            self.assertEqual((response.status_code, response.json()["detail"]["code"]), (429, "reaction_rate_limited"))

        async with self.sessions() as db:
            (await self._user(db, "bob")).muted_until = datetime.now(UTC) + timedelta(hours=1)
            await db.commit()
        response = await self._post("bob", "👀")
        self.assertEqual((response.status_code, response.json()["detail"]["code"]), (403, "account_muted"))

    async def test_admin_routes_carry_reactions(self) -> None:
        await self._post("admin", "👀")
        await self._post("alice", "👀")
        self.acting = "admin"
        with patch("app.api.v1.admin.manager.broadcast", new=AsyncMock()), patch("app.api.v1.admin.admin_manager.broadcast", new=AsyncMock()):
            response = await self.client.get("/api/v1/admin/announcements")
            self.assertEqual(response.status_code, 200)
            body = {item["id"]: item for item in response.json()}
            self.assertEqual(set(body), {self.first, self.second, self.offline})
            for item in body.values():
                self.assertEqual(set(item), OLD_FIELDS | {"reactions"})
            self.assertEqual(body[self.first]["reactions"], [{"emoji": "👀", "count": 2, "reacted_by_me": False}])
            self.assertEqual((body[self.second]["reactions"], body[self.offline]["reactions"], body[self.offline]["is_active"]), ([], [], False))

            response = await self.client.post("/api/v1/admin/announcements", json={"title": "新公告", "content": "内容"})
            self.assertEqual((response.status_code, response.json()["reactions"]), (201, []))
            response = await self.client.put(f"/api/v1/admin/announcements/{self.first}", json={"pinned": True})
            self.assertEqual((response.status_code, response.json()["pinned"]), (200, True))
            self.assertEqual(response.json()["reactions"], [{"emoji": "👀", "count": 2, "reacted_by_me": False}])
            response = await self.client.delete(f"/api/v1/admin/announcements/{self.first}")
            self.assertEqual(response.status_code, 200)
        async with self.sessions() as db:
            self.assertEqual(await self._rows(db), 0)
        # 普通用户进不了后台接口
        self.acting = "alice"
        self.assertEqual((await self.client.get("/api/v1/admin/announcements")).status_code, 403)

    async def test_realtime_event_is_coalesced_per_announcement(self) -> None:
        # 一秒（这里调短了）内的多次变化：到点只广播一次，内容是最新的汇总
        self.assertEqual((await self._post("alice", "👀")).status_code, 200)
        self.assertEqual((await self._post("bob", "👀")).status_code, 200)
        self.assertEqual((await self._post("bob", "🔥")).status_code, 200)
        self.assertEqual((await self._post("carol", "🔥")).status_code, 200)
        self.assertEqual((await self._delete("alice", "👀")).status_code, 200)
        self.broadcast.assert_not_awaited()
        await self._drain()
        self.broadcast.assert_awaited_once_with(
            "announcement.reaction.updated",
            {"id": self.first, "reactions": [{"emoji": "🔥", "count": 2}, {"emoji": "👀", "count": 1}]},
        )
        self.assertEqual(announcements_api._pending_reaction_broadcasts, set())

        # 之后的变化另算一次；没有改动的 DELETE、被拒绝的请求都不触发
        self.broadcast.reset_mock()
        self.assertEqual((await self._delete("dave", "🎉")).status_code, 200)
        self.assertEqual((await self._post("dave", "好")).status_code, 422)
        self.assertEqual((await self._post("dave", "👀", self.offline)).status_code, 404)
        self.assertEqual(announcements_api._pending_reaction_broadcasts, set())
        self.assertEqual((await self._post("dave", "🎉")).status_code, 200)
        await self._drain()
        self.broadcast.assert_awaited_once_with(
            "announcement.reaction.updated",
            {"id": self.first, "reactions": [{"emoji": "🔥", "count": 2}, {"emoji": "👀", "count": 1}, {"emoji": "🎉", "count": 1}]},
        )

        # 不同公告各自合并
        self.broadcast.reset_mock()
        await self._post("alice", "👍")
        await self._post("alice", "👍", self.second)
        await self._post("bob", "👍", self.second)
        await self._drain()
        self.assertEqual(
            sorted((call.args[1]["id"], call.args[1]["reactions"][0]) for call in self.broadcast.await_args_list),
            sorted([(self.first, {"emoji": "🔥", "count": 2}), (self.second, {"emoji": "👍", "count": 2})]),
        )

    async def test_broadcast_waits_for_the_window(self) -> None:
        started = asyncio.get_running_loop().time()
        await self._post("alice", "👀")
        await asyncio.sleep(self.DELAY / 3)
        self.broadcast.assert_not_awaited()
        await self._drain()
        self.broadcast.assert_awaited_once()
        self.assertGreaterEqual(asyncio.get_running_loop().time() - started, self.DELAY * 0.9)

    async def test_broadcast_failure_is_only_logged(self) -> None:
        self.broadcast.side_effect = RuntimeError("redis down")
        with self.assertLogs(announcements_api.logger, level="ERROR") as logs:
            response = await self._post("alice", "👀")
            self.assertEqual((response.status_code, response.json()["reactions"]), (200, [{"emoji": "👀", "count": 1, "reacted_by_me": True}]))
            await self._drain()
        self.assertIn("reaction broadcast failed", logs.output[0])
        self.broadcast.assert_awaited_once()
        # 失败之后标记已经摘掉，下一次变化照常推
        self.broadcast.side_effect = None
        self.broadcast.reset_mock()
        self.assertEqual((await self._post("bob", "👀")).status_code, 200)
        await self._drain()
        self.broadcast.assert_awaited_once_with("announcement.reaction.updated", {"id": self.first, "reactions": [{"emoji": "👀", "count": 2}]})

    async def test_no_event_for_an_announcement_taken_offline_before_the_flush(self) -> None:
        await self._post("alice", "👀")
        async with self.sessions() as db:
            await announcement_service.admin_update(db, self.ids["admin"], self.first, AnnouncementUpdate(is_active=False))
        await self._drain()
        self.broadcast.assert_not_awaited()


class RealAppReactionTests(ReactionTestCase):
    """用真实的应用（app.main.app，带 ClientProofMiddleware 等全部中间件）走完整的 ASGI 栈。

    客户端证明头按 ``request.url.path``（解码后的路径）算哈希：客户端拿未转义的 emoji 参与计算，
    实际发出去的路径是百分号编码的。
    """

    # 带变体选择符的（❤️ = U+2764 U+FE0F）、ZWJ 组合、肤色、旗帜、不含 # 的键帽
    EMOJIS = ("👀", "❤️", "👨‍👩‍👧", "🏳️‍🌈", "👍🏽", "🇨🇳", "1️⃣", "*️⃣")
    HASH_KEYCAP = "#️⃣"

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
        # 这一组只看请求能不能走通，实时事件在 HttpReactionTests 里测
        patcher = patch.object(announcements_api, "_schedule_reaction_broadcast", MagicMock())
        self.scheduled = patcher.start()
        self.addCleanup(patcher.stop)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=real_app), base_url="http://testserver")

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        await super().asyncTearDown()

    @staticmethod
    def _proof(method: str, decoded_path: str) -> dict[str, str]:
        """和客户端同一个算法：METHOD|去掉 /api/v1 的未转义路径|时间戳|nonce|盐。"""
        timestamp, nonce = str(int(time.time() * 1000)), uuid.uuid4().hex
        digest = hashlib.sha256(f"{method}|{decoded_path}|{timestamp}|{nonce}|KukeChat".encode("utf-8")).hexdigest()
        return {"X-Kuke-Client": "kukechat-web/test", "X-Kuke-Timestamp": timestamp, "X-Kuke-Nonce": nonce, "X-Kuke-Proof": digest}

    async def _call(self, method: str, decoded_path: str, *, wire_path: str | None = None, proof_path: str | None = None, **kwargs) -> httpx.Response:
        return await self.client.request(
            method, "/api/v1" + (wire_path or decoded_path), headers=self._proof(method, proof_path or decoded_path), **kwargs
        )

    async def _delete(self, emoji: str, announcement_id: int | None = None, **kwargs) -> httpx.Response:
        """路径里是百分号编码的 emoji，证明头按解码后的路径算。"""
        base = f"/announcements/{announcement_id or self.first}/reactions/"
        response = await self._call("DELETE", base + emoji, wire_path=base + quote(emoji, safe=""), **kwargs)
        # 确认真正发出去的是编码后的纯 ASCII 路径
        self.assertEqual(response.request.url.raw_path, ("/api/v1" + base + quote(emoji, safe="")).encode("ascii"))
        return response

    async def _post(self, emoji: str, announcement_id: int | None = None) -> httpx.Response:
        return await self._call("POST", f"/announcements/{announcement_id or self.first}/reactions", json={"emoji": emoji})

    async def _stored(self) -> list[str]:
        async with self.sessions() as db:
            return list((await db.execute(select(AnnouncementReaction.emoji).order_by(AnnouncementReaction.id))).scalars())

    async def test_requests_without_a_valid_proof_never_reach_the_route(self) -> None:
        path = f"/api/v1/announcements/{self.first}/reactions"
        response = await self.client.post(path, json={"emoji": "👀"})
        self.assertEqual((response.status_code, response.json()), (403, {"detail": "Invalid client"}))
        headers = self._proof("POST", f"/announcements/{self.second}/reactions")
        response = await self.client.post(path, json={"emoji": "👀"}, headers=headers)
        self.assertEqual((response.status_code, response.json()), (403, {"detail": "Invalid request proof"}))
        self.assertEqual(await self._stored(), [])

    async def test_delete_with_percent_encoded_emoji_and_proof_over_decoded_path(self) -> None:
        with patch.object(settings, "announcement_reaction_per_user", len(self.EMOJIS)):
            for emoji in self.EMOJIS:
                response = await self._post(emoji)
                self.assertEqual(response.status_code, 200, emoji)
                self.assertIn({"emoji": emoji, "count": 1, "reacted_by_me": True}, response.json()["reactions"])
            # 存进去的和发过来的是同一个字符串（变体选择符、ZWJ 都原样保留，不做归一化）
            self.assertEqual(await self._stored(), list(self.EMOJIS))
            listed = (await self._call("GET", "/announcements")).json()[-1]
            self.assertEqual((listed["id"], [item["emoji"] for item in listed["reactions"]]), (self.first, list(self.EMOJIS)))

            for index, emoji in enumerate(self.EMOJIS):
                # 证明头如果按编码后的路径算，对不上
                base = f"/announcements/{self.first}/reactions/"
                response = await self._delete(emoji, proof_path=base + quote(emoji, safe=""))
                self.assertEqual((response.status_code, response.json()), (403, {"detail": "Invalid request proof"}), emoji)
                self.assertEqual(await self._stored(), list(self.EMOJIS[index:]))

                response = await self._delete(emoji)
                self.assertEqual(response.status_code, 200, emoji)
                self.assertEqual([item["emoji"] for item in response.json()["reactions"]], list(self.EMOJIS[index + 1 :]), emoji)
                self.assertEqual(await self._stored(), list(self.EMOJIS[index + 1 :]))
                # 再删一次还是 200
                self.assertEqual((await self._delete(emoji)).status_code, 200, emoji)
        self.assertEqual(self.scheduled.call_count, len(self.EMOJIS) * 2)

    async def test_variation_selector_is_part_of_the_identity(self) -> None:
        """❤️（带 U+FE0F）和 ❤（不带）是两个回应：客户端取消时要用服务端返回的那个字符串。"""
        with_selector, bare = "❤️", "❤"
        self.assertEqual((await self._post(with_selector)).status_code, 200)
        response = await self._delete(bare)
        self.assertEqual((response.status_code, [item["emoji"] for item in response.json()["reactions"]]), (200, [with_selector]))
        self.assertEqual(await self._stored(), [with_selector])
        self.assertEqual((await self._post(bare)).json()["reactions"], [
            {"emoji": with_selector, "count": 1, "reacted_by_me": True},
            {"emoji": bare, "count": 1, "reacted_by_me": True},
        ])
        self.assertEqual((await self._delete(with_selector)).json()["reactions"], [{"emoji": bare, "count": 1, "reacted_by_me": True}])

    async def test_post_toggle_reliably_cancels_including_hash_keycap(self) -> None:
        """带 # 的键帽客户端不走 DELETE 路径，用 POST 开关取消：已存在的回应再 POST 一次必须被取消。"""
        # 这里连着操作几十次，把频率上限放开
        with patch.object(settings, "announcement_reaction_per_minute", 1000):
            for emoji in (self.HASH_KEYCAP, *self.EMOJIS):
                for _round in range(2):
                    response = await self._post(emoji)
                    self.assertEqual((response.status_code, response.json()), (200, {"id": self.first, "reactions": [{"emoji": emoji, "count": 1, "reacted_by_me": True}]}), emoji)
                    self.assertEqual(await self._stored(), [emoji])
                    response = await self._post(emoji)
                    self.assertEqual((response.status_code, response.json()), (200, {"id": self.first, "reactions": []}), emoji)
                    self.assertEqual(await self._stored(), [])

            # 别人也回应了同一个 emoji：POST 只取消自己的那一个
            await self._post(self.HASH_KEYCAP)
            self.acting = "bob"
            self.assertEqual((await self._post(self.HASH_KEYCAP)).json()["reactions"], [{"emoji": self.HASH_KEYCAP, "count": 2, "reacted_by_me": True}])
            self.acting = "alice"
            self.assertEqual((await self._post(self.HASH_KEYCAP)).json()["reactions"], [{"emoji": self.HASH_KEYCAP, "count": 1, "reacted_by_me": False}])

    async def test_hash_keycap_in_the_delete_path(self) -> None:
        """记录现状：# 解码后会被 request.url 当成 fragment 的开头，证明头校验看到的路径在 # 处截断。"""
        await self._post(self.HASH_KEYCAP)
        base = f"/announcements/{self.first}/reactions/"
        # 按完整的解码后路径算证明头：对不上，被中间件挡掉
        response = await self._delete(self.HASH_KEYCAP)
        self.assertEqual((response.status_code, response.json()), (403, {"detail": "Invalid request proof"}))
        self.assertEqual(await self._stored(), [self.HASH_KEYCAP])
        # 按截断后的路径算才能过；路由本身拿到的 emoji 是完整的
        response = await self._delete(self.HASH_KEYCAP, proof_path=base)
        self.assertEqual((response.status_code, response.json()), (200, {"id": self.first, "reactions": []}))
        self.assertEqual(await self._stored(), [])

    async def test_404_always_carries_the_code_unless_the_path_itself_is_unknown(self) -> None:
        """客户端把「404 / 405 且 detail 里没有 code」当成服务器不支持这个功能，所以业务上的 404 必须带 code。"""
        await self._post("👀")
        for announcement_id in (self.offline, 987654):
            for response in (
                await self._post("👀", announcement_id),
                await self._delete("👀", announcement_id),
            ):
                self.assertEqual(response.status_code, 404)
                detail = response.json()["detail"]
                self.assertEqual((detail["code"], set(detail)), ("announcement_not_found", {"code", "message"}))
        # 公告被删除、被下线之后同样
        async with self.sessions() as db:
            await announcement_service.admin_update(db, self.ids["admin"], self.first, AnnouncementUpdate(is_active=False))
            await announcement_service.admin_delete(db, self.ids["admin"], self.second)
        for announcement_id in (self.first, self.second):
            for response in (await self._post("🔥", announcement_id), await self._delete("👀", announcement_id)):
                self.assertEqual((response.status_code, response.json()["detail"]["code"]), (404, "announcement_not_found"))

        # 路径本身不存在 / 方法不对：框架的 404 / 405，detail 是字符串，没有 code
        response = await self._call("POST", f"/announcements/{self.first}/reaction", json={"emoji": "👀"})
        self.assertEqual((response.status_code, response.json()), (404, {"detail": "Not Found"}))
        response = await self._call("GET", f"/announcements/{self.first}/reactions")
        self.assertEqual((response.status_code, response.json()), (405, {"detail": "Method Not Allowed"}))
        response = await self._call("DELETE", f"/announcements/{self.first}/reactions")
        self.assertEqual((response.status_code, response.json()), (405, {"detail": "Method Not Allowed"}))
        # 固定路径没有被带参数的路径盖住
        response = await self._call("GET", "/announcements/reaction-options")
        self.assertEqual((response.status_code, response.json()["per_user"]), (200, 5))
        response = await self._call("GET", "/announcements/latest")
        self.assertEqual((response.status_code, response.json()), (200, None))


if __name__ == "__main__":
    unittest.main()
