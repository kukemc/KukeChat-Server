"""后台封禁名单（GET /admin/bans → reports.list_admin_bans）。

线上名单一度 289 个号里大半是机器人：创建者停用 / 删除机器人时，机器人账号状态也是 disabled，但没有封禁时间、
原因和操作记录，显示成「来源不明」「未填写原因」，PG 降序又把空的封禁时间排在最前。这里锁定：
默认不列机器人（给出藏起来的个数）、按封禁时间倒序且空值在后、机器人列出来时标明是创建者停用 / 删除并带上创建者。
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.models  # noqa: F401
from app.core.database import Base
from app.models.bot import Bot
from app.models.enums import UserStatus
from app.models.user import User
from app.services import reports as report_service

UTC = timezone.utc


class AdminBanListTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        now = datetime.now(UTC)
        self.now = now
        async with self.sessions() as db:
            owner = User(username="owner", hashed_password="x", nickname="创建者", status=UserStatus.DISABLED, banned_at=now - timedelta(days=3), ban_reason="辱骂")
            older = User(username="older", hashed_password="x", status=UserStatus.DISABLED, banned_at=now - timedelta(days=10), ban_reason="刷屏")
            newer = User(username="newer", hashed_password="x", status=UserStatus.DISABLED, banned_at=now - timedelta(hours=1), ban_reason="广告")
            # 台账上线前的老封禁：没有 banned_at
            legacy = User(username="legacy", hashed_password="x", status=UserStatus.DISABLED)
            active = User(username="active", hashed_password="x")
            deleted_bot_user = User(username="bot_701", hashed_password="x", is_bot=True, status=UserStatus.DISABLED)
            disabled_bot_user = User(username="bot_702", hashed_password="x", is_bot=True, status=UserStatus.DISABLED)
            db.add_all([owner, older, newer, legacy, active, deleted_bot_user, disabled_bot_user])
            await db.flush()
            deleted_bot_user.bot_owner_id = owner.id
            disabled_bot_user.bot_owner_id = owner.id
            db.add_all(
                [
                    Bot(user_id=deleted_bot_user.id, owner_id=owner.id, name="删掉的", status="deleted", key_hash="d", key_prefix="d", updated_at=now - timedelta(days=1)),
                    Bot(user_id=disabled_bot_user.id, owner_id=owner.id, name="停用的", status="disabled", key_hash="s", key_prefix="s", updated_at=now - timedelta(days=20)),
                ]
            )
            await db.commit()
            self.ids = {user.username: user.id for user in (owner, older, newer, legacy, deleted_bot_user, disabled_bot_user)}

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def test_bots_are_hidden_by_default_with_a_count(self) -> None:
        async with self.sessions() as db:
            page = await report_service.list_admin_bans(db)
        self.assertEqual(page.total, 4)
        self.assertEqual(page.hidden_bot_count, 2)
        self.assertFalse(any(item.is_bot for item in page.items))

    async def test_sorted_by_ban_time_desc_with_unknown_last(self) -> None:
        async with self.sessions() as db:
            page = await report_service.list_admin_bans(db)
        names = [item.username for item in page.items]
        # legacy 没有 banned_at：退回账号更新时间（刚建的号 = 现在），排在前面但标成推测的
        self.assertEqual(names[1:], ["newer", "owner", "older"])
        legacy = next(item for item in page.items if item.username == "legacy")
        self.assertTrue(legacy.banned_at_estimated)
        self.assertIsNotNone(legacy.banned_at)
        self.assertFalse(next(item for item in page.items if item.username == "newer").banned_at_estimated)
        async with self.sessions() as db:
            ascending = await report_service.list_admin_bans(db, order="asc")
        self.assertEqual([item.username for item in ascending.items][:3], ["older", "owner", "newer"])

    async def test_bots_listed_on_request_with_owner_and_source(self) -> None:
        async with self.sessions() as db:
            page = await report_service.list_admin_bans(db, include_bots=True)
        self.assertEqual(page.total, 6)
        self.assertEqual(page.hidden_bot_count, 0)
        by_name = {item.username: item for item in page.items}
        deleted, disabled = by_name["bot_701"], by_name["bot_702"]
        self.assertEqual(deleted.ban_source, "bot_deleted")
        self.assertEqual(disabled.ban_source, "bot_disabled")
        for item in (deleted, disabled):
            self.assertTrue(item.is_bot)
            self.assertEqual(item.bot_owner_id, self.ids["owner"])
            self.assertEqual(item.bot_owner_name, "创建者")
            self.assertTrue(item.bot_owner_banned)
            self.assertTrue(item.banned_at_estimated)
        # 停用 / 删除的时间当作「封禁于」，参与排序：删掉的（1 天前）在 owner（3 天前）之前，停用的（20 天前）最后
        names = [item.username for item in page.items]
        self.assertLess(names.index("bot_701"), names.index("owner"))
        self.assertEqual(names[-1], "bot_702")

    async def test_search_counts_hidden_bots_under_the_same_filter(self) -> None:
        async with self.sessions() as db:
            page = await report_service.list_admin_bans(db, query="bot_70")
        self.assertEqual((page.total, page.hidden_bot_count, page.items), (0, 2, []))


if __name__ == "__main__":
    unittest.main()
