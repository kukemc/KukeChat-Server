"""Committed user sends survive optional realtime delivery failures."""

from __future__ import annotations

import asyncio
import logging
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import Depends, FastAPI, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.models  # noqa: F401
from app.api.v1 import conversations as api
from app.core.database import Base, get_db
from app.core.dependencies import get_current_user
from app.models.conversation import Conversation, ConversationMember
from app.models.enums import ConversationType, MessageType
from app.models.message import Message
from app.models.user import User


class CommittedSendDeliveryFailureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Never connect to the configured application database or Redis.
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with self.sessions() as db:
            user = User(username="committed_sender", hashed_password="unused")
            db.add(user)
            await db.flush()
            self.user_id = user.id
            source = Conversation(type=ConversationType.DIRECT)
            target = Conversation(type=ConversationType.DIRECT)
            game = Conversation(type=ConversationType.GROUP, title="game", game_mode_enabled=True)
            db.add_all([source, target, game])
            await db.flush()
            self.source_id, self.target_id, self.game_id = source.id, target.id, game.id
            db.add_all(
                ConversationMember(conversation_id=conversation_id, user_id=user.id)
                for conversation_id in (self.source_id, self.target_id, self.game_id)
            )
            original = Message(conversation_id=self.source_id, sender_id=user.id, content="original")
            db.add(original)
            await db.commit()
            self.original_id = original.id

        async def isolated_db():
            async with self.sessions() as db:
                yield db

        async def isolated_user(db: AsyncSession = Depends(get_db)):
            return await db.get(User, self.user_id)

        app = FastAPI()
        app.include_router(api.router, prefix="/api/v1/conversations")
        app.dependency_overrides[get_db] = isolated_db
        app.dependency_overrides[get_current_user] = isolated_user
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.engine.dispose()

    async def _send(self, operation, conversation_id):
        if operation == "message":
            return await self.client.post(
                f"/api/v1/conversations/{conversation_id}/messages", json={"content": "new message"}
            )
        return await self.client.post(
            f"/api/v1/conversations/{conversation_id}/forward-bundle",
            json={"source_conversation_id": self.source_id, "message_ids": [self.original_id]},
        )

    async def _messages(self, conversation_id):
        # A separate session verifies the result really committed, not merely flushed.
        async with self.sessions() as db:
            return (await db.execute(select(Message).where(Message.conversation_id == conversation_id))).scalars().all()

    async def test_committed_send_returns_201_on_bot_database_or_redis_delivery_failure(self):
        for operation in ("message", "forward"):
            for failure in ("bot_db", "redis_publish", "game_publish"):
                with self.subTest(operation=operation, failure=failure):
                    conversation_id = self.game_id if failure == "game_publish" else self.target_id
                    before = {item.id for item in await self._messages(conversation_id)}
                    scheduled = []
                    moderated = []

                    def record_task(coro):
                        # In particular, no request-bound AsyncSession may escape into a task.
                        scheduled.append((coro.cr_code.co_name, dict(coro.cr_frame.f_locals)))
                        coro.close()

                    async def fail_bot(*_args, **_kwargs):
                        raise RuntimeError("bot database unavailable")

                    async def fail_publish(*_args, **_kwargs):
                        raise ConnectionError("Redis publisher unavailable")

                    async def recipients(_db, _conversation_id):
                        return [self.user_id] if failure == "redis_publish" else []

                    with patch.object(api, "online_conversation_member_ids", side_effect=recipients), patch.object(
                        api, "schedule_realtime_task", side_effect=record_task
                    ), patch.object(
                        api.moderation_service, "schedule_message_moderation", side_effect=moderated.append
                    ), patch.object(
                        api.bot_service, "bot_conversation_ids_for_message_event", side_effect=fail_bot if failure == "bot_db" else AsyncMock(return_value=[])
                    ), patch.object(
                        api.manager, "send_to_users", side_effect=fail_publish if failure == "redis_publish" else AsyncMock()
                    ), patch.object(
                        api.game_manager, "send_to_conversation", side_effect=fail_publish if failure == "game_publish" else AsyncMock()
                    ), self.assertLogs(api.logger, level=logging.ERROR) as logs:
                        response = await self._send(operation, conversation_id)

                    self.assertEqual(response.status_code, 201, response.text)
                    data = response.json()
                    self.assertEqual(data["conversation_id"], conversation_id)
                    self.assertEqual(data["type"], "text" if operation == "message" else "forward_bundle")
                    after = await self._messages(conversation_id)
                    newly_inserted = [item for item in after if item.id not in before]
                    self.assertEqual(len(newly_inserted), 1)
                    self.assertEqual(data["id"], newly_inserted[0].id)
                    self.assertEqual(moderated, [data["id"]])
                    self.assertEqual([name for name, _locals in scheduled], ["send_admin_message_signal"])
                    self.assertTrue(all("db" not in locals_ for _name, locals_ in scheduled))
                    self.assertTrue(any(str(data["id"]) in line and "delivery failed" in line for line in logs.output))

    async def test_service_failure_before_commit_remains_failure(self):
        for operation in ("message", "forward"):
            with self.subTest(operation=operation):
                before = await self._messages(self.target_id)
                service_name = "send_message" if operation == "message" else "create_forward_bundle"
                with patch.object(api.message_service, service_name, new_callable=AsyncMock, side_effect=HTTPException(403, "not permitted")), patch.object(
                    api, "send_message_updates", new_callable=AsyncMock
                ) as deliver:
                    response = await self._send(operation, self.target_id)
                self.assertEqual(response.status_code, 403, response.text)
                self.assertEqual(len(await self._messages(self.target_id)), len(before))
                deliver.assert_not_awaited()

    async def test_real_membership_denial_does_not_insert_or_deliver(self):
        async with self.sessions() as db:
            other = Conversation(type=ConversationType.DIRECT)
            db.add(other)
            await db.commit()
            other_id = other.id
        for operation in ("message", "forward"):
            with self.subTest(operation=operation):
                with patch.object(api, "send_message_updates", new_callable=AsyncMock) as deliver:
                    response = await self._send(operation, other_id)
                self.assertEqual(response.status_code, 404, response.text)
                self.assertEqual(await self._messages(other_id), [])
                deliver.assert_not_awaited()

    async def test_cancellation_during_delivery_propagates(self):
        # asyncio.CancelledError is not an optional delivery Exception.
        for operation in ("message", "forward"):
            with self.subTest(operation=operation):
                with patch.object(api, "online_conversation_member_ids", new_callable=AsyncMock, return_value=[]), patch.object(
                    api, "send_message_updates", new_callable=AsyncMock, side_effect=asyncio.CancelledError
                ):
                    with self.assertRaises(asyncio.CancelledError):
                        await self._send(operation, self.target_id)
