"""GET /bots/by-user/{user_id}：聊天里点机器人头像时，按机器人用户账号找到机器人详情。"""
from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.models  # noqa: F401
from app.api.v1 import bots as bots_api
from app.core.database import Base
from app.models.bot import Bot
from app.models.user import User


class BotByUserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with self.sessions() as db:
            self.owner = User(username="owner", hashed_password="x")
            self.viewer = User(username="viewer", hashed_password="x")
            self.public_bot_user = User(username="bot_public", hashed_password="x", is_bot=True)
            self.private_bot_user = User(username="bot_private", hashed_password="x", is_bot=True)
            db.add_all([self.owner, self.viewer, self.public_bot_user, self.private_bot_user])
            await db.flush()
            self.public_bot = Bot(
                user_id=self.public_bot_user.id, owner_id=self.owner.id, name="公开机器人",
                is_public=True, status="active", key_hash="h1", key_prefix="p1",
            )
            self.private_bot = Bot(
                user_id=self.private_bot_user.id, owner_id=self.owner.id, name="私有机器人",
                is_public=False, status="active", key_hash="h2", key_prefix="p2",
            )
            db.add_all([self.public_bot, self.private_bot])
            await db.commit()
        self.online_patch = patch.object(bots_api.bot_manager, "is_online_global", new_callable=AsyncMock, return_value=False)
        self.online_patch.start()

    async def asyncTearDown(self) -> None:
        self.online_patch.stop()
        await self.engine.dispose()

    async def test_returns_same_bot_as_detail_endpoint(self) -> None:
        async with self.sessions() as db:
            by_user = await bots_api.bot_detail_by_user(self.public_bot_user.id, self.viewer, db)
            by_id = await bots_api.bot_detail(self.public_bot.id, self.viewer, db)
        self.assertEqual(by_user.id, self.public_bot.id)
        self.assertEqual(by_user.model_dump(), by_id.model_dump())

    async def test_private_bot_only_visible_to_owner_and_non_bot_user_is_404(self) -> None:
        async with self.sessions() as db:
            with self.assertRaises(HTTPException) as denied:
                await bots_api.bot_detail_by_user(self.private_bot_user.id, self.viewer, db)
            self.assertEqual(denied.exception.status_code, 403)

            mine = await bots_api.bot_detail_by_user(self.private_bot_user.id, self.owner, db)
            self.assertEqual(mine.id, self.private_bot.id)

            with self.assertRaises(HTTPException) as missing:
                await bots_api.bot_detail_by_user(self.viewer.id, self.viewer, db)
            self.assertEqual(missing.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
