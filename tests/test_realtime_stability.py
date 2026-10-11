"""Realtime delivery and reminder worker regression tests."""
from __future__ import annotations

import asyncio
import logging
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.websockets import WebSocketState

import app.models  # noqa: F401
from app.api.v1 import conversations as api
from app.core.database import Base
from app.models.conversation import Conversation, ConversationMember
from app.models.enums import ConversationType
from app.models.message import Message
from app.models.task import Task
from app.models.user import User
from app.services import task as task_service
from app.websocket import routes
from app.websocket.manager import ConnectionManager, GameConnectionManager, WEBSOCKET_SEND_QUEUE_SIZE


class FakeWebSocket:
    application_state = WebSocketState.CONNECTED

    def __init__(self):
        self.sent = []
        self.close_codes = []
        self.busy = False
        self.overlap = False
        self.block = asyncio.Event()
        self.slow = False

    async def accept(self):
        return None

    async def send_json(self, data):
        if self.busy:
            self.overlap = True
        self.busy = True
        try:
            if self.slow:
                await self.block.wait()
            self.sent.append(data)
        finally:
            self.busy = False

    async def send_text(self, data):
        self.sent.append(data)

    async def close(self, code, reason=""):
        if self.busy:
            self.overlap = True
        self.close_codes.append(code)
        self.application_state = WebSocketState.DISCONNECTED


class CountingPresenceRedis:
    def __init__(self, online_ids):
        self.online_ids = set(online_ids)
        self.smembers_calls = 0
        self.pipeline_calls = 0
        self.exists_keys = []

    async def smembers(self, key):
        self.smembers_calls += 1
        return {str(user_id).encode() for user_id in self.online_ids}

    def pipeline(self):
        self.pipeline_calls += 1
        self.pending = []
        return self

    def exists(self, key):
        self.exists_keys.append(key)
        self.pending.append(int(key.rsplit(":", 1)[-1]))
        return self

    async def execute(self):
        return [int(user_id in self.online_ids) for user_id in self.pending]


class RealtimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_message_updates_reuses_authorized_recipients_without_second_presence_scan(self):
        message = Message(id=9, conversation_id=5, sender_id=2, content="hello")
        message.sender = User(id=2, username="sender", hashed_password="x")
        message._sender_display_name = "sender"
        with patch.object(api.manager, "online_user_ids_global", new_callable=AsyncMock) as presence, patch.object(
            api.manager, "send_to_users", new_callable=AsyncMock
        ) as deliver, patch.object(api, "send_bot_message_updates", new_callable=AsyncMock), patch.object(
            api, "send_game_message_updates", new_callable=AsyncMock
        ), patch.object(api.message_service, "build_new_message_read", return_value={"id": 9}):
            await api.send_message_updates(object(), "message.created", message, {2, 42})
        presence.assert_not_awaited()
        self.assertEqual(deliver.await_args.args[:2], ({2, 42}, "message.created"))

    async def test_online_member_selection_reads_presence_once_and_filters_membership(self):
        class SmallGroupDB:
            async def execute(self, statement):
                return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [2, 42]))

        with patch.object(api.manager, "online_user_ids_global", new_callable=AsyncMock) as presence, patch.object(
            api.manager, "online_user_ids_among", new_callable=AsyncMock, return_value={2}
        ) as targeted:
            self.assertEqual(await api.online_conversation_member_ids(SmallGroupDB(), 5), [2])
        presence.assert_not_awaited()
        targeted.assert_awaited_once_with([2, 42])

    async def test_5000_online_five_members_uses_five_targeted_exists(self):
        redis = CountingPresenceRedis(range(1, 5001))
        channel = ConnectionManager()
        channel._redis = redis
        channel.redis_enabled = lambda: True
        channel._connections = {5: {object(): object()}}  # local connection survives missing remote presence

        class SmallGroupDB:
            async def execute(self, statement):
                return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [1, 5, 6000, 6001, 6002]))

        with patch.object(api, "manager", channel):
            result = await api.online_conversation_member_ids(SmallGroupDB(), 9)
        self.assertEqual(set(result), {1, 5})
        self.assertEqual(redis.smembers_calls, 0)
        self.assertEqual(redis.pipeline_calls, 1)
        self.assertEqual(len(redis.exists_keys), 4)
        self.assertEqual({int(key.rsplit(":", 1)[-1]) for key in redis.exists_keys}, {1, 6000, 6001, 6002})

    async def test_50_online_2000_members_uses_global_presence(self):
        redis = CountingPresenceRedis(set(range(1, 51)) | {3000})
        channel = ConnectionManager()
        channel._redis = redis
        channel.redis_enabled = lambda: True

        class LargeGroupDB:
            async def execute(self, statement):
                return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: list(range(1, 130))))

        with patch.object(api, "manager", channel), patch.object(
            api.conversation_service, "conversation_member_ids_in", new_callable=AsyncMock,
            side_effect=lambda db, conversation_id, ids: sorted(ids & set(range(1, 2001))),
        ) as members:
            result = await api.online_conversation_member_ids(LargeGroupDB(), 9)
        self.assertEqual(result, list(range(1, 51)))
        self.assertEqual(redis.smembers_calls, 1)
        self.assertEqual(redis.pipeline_calls, 1)
        self.assertEqual(len(redis.exists_keys), 51)
        self.assertEqual(members.await_args.args[2], set(range(1, 51)) | {3000})

    async def test_targeted_presence_without_redis_or_on_failure_uses_local_only(self):
        channel = ConnectionManager()
        channel._connections = {2: {object(): object()}, 99: {object(): object()}}
        self.assertEqual(await channel.online_user_ids_among([2, 3]), {2})
        redis = CountingPresenceRedis([3])
        channel._redis = redis
        channel.redis_enabled = lambda: True
        async def fail():
            raise ConnectionError("Redis unavailable")
        redis.execute = fail
        with self.assertLogs("app.websocket.manager", logging.ERROR):
            self.assertEqual(await channel.online_user_ids_among([2, 3]), {2})
        self.assertEqual(redis.smembers_calls, 0)

    async def test_sqlite_legacy_parameter_limit_batches_large_group_presence(self):
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        bound_counts = []

        def reject_legacy_oversized_statement(connection, cursor, statement, parameters, context, executemany):
            if "FROM conversation_members" in statement and " IN (" in statement:
                count = len(parameters)
                bound_counts.append(count)
                if count > 999:
                    raise AssertionError(f"legacy SQLite bind limit exceeded: {count}")

        event.listen(engine.sync_engine, "before_cursor_execute", reject_legacy_oversized_statement)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            # Use known SQLite bind ceilings even if this local build supports >999.
            users = set(range(1, 2501))
            async with sessions() as db:
                db.add(Conversation(id=1, type=ConversationType.GROUP, title="large"))
                db.add_all(ConversationMember(conversation_id=1, user_id=user_id) for user_id in range(1, 2001))
                await db.commit()
                with patch.object(api.manager, "online_user_ids_global", new_callable=AsyncMock, return_value=users), patch.object(
                    api.manager, "online_user_ids", return_value=set()
                ), patch.object(api.conversation_service, "conversation_member_ids_in", wraps=api.conversation_service.conversation_member_ids_in) as members:
                    result = await api.online_conversation_member_ids(db, 1)
            self.assertEqual(set(result), set(range(1, 2001)))
            self.assertEqual(members.await_count, 4)
            self.assertEqual(bound_counts, [801, 801, 801, 101])
            self.assertTrue(all(len(call.args[2]) <= 800 for call in members.await_args_list))
        finally:
            await engine.dispose()

    async def test_remote_recipient_not_filtered_by_stale_presence(self):
        channel = ConnectionManager()
        channel._redis = SimpleNamespace(publish=AsyncMock())
        channel.online_user_ids_global = AsyncMock(return_value=set())
        await channel.send_to_users({42}, "message.created", {"id": 1})
        channel._redis.publish.assert_awaited_once()
        self.assertIn('"owner_ids": [42]', channel._redis.publish.call_args.args[1])
        await channel.send_to_user(42, "task.changed", {"id": 1})
        self.assertEqual(channel._redis.publish.await_count, 2)

    async def test_queue_overflow_closes_through_writer_without_overlap(self):
        channel = GameConnectionManager()
        socket = FakeWebSocket()
        socket.slow = True
        await channel.connect(3, socket)
        await channel.send_to_conversation(3, "first", {})
        await asyncio.sleep(0)
        for index in range(WEBSOCKET_SEND_QUEUE_SIZE + 1):
            await channel.send_to_conversation(3, "queued", {"index": index})
        for _ in range(30):
            if socket.close_codes:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(socket.close_codes, [1013])
        self.assertFalse(socket.overlap)
        for _ in range(30):
            if channel.connection_count() == 0:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(channel.connection_count(), 0)

    async def test_game_websocket_rejects_invalid_group_without_name_error(self):
        socket = FakeWebSocket()
        async def reject(*args):
            raise HTTPException(status_code=403, detail="not allowed")
        class Session:
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass
        with patch("app.core.database.AsyncSessionLocal", return_value=Session()), patch(
            "app.services.game_mode.resolve_game_conversation", side_effect=reject
        ):
            await routes.game_websocket_endpoint(socket, conversation_id=4, creation_oid="creation")
        self.assertEqual(socket.close_codes, [4403])

    async def test_background_task_limit_closes_coroutine_and_logs_failures(self):
        async def fails():
            raise ValueError("failure")
        with self.assertLogs(api.logger, logging.ERROR) as records:
            api.schedule_realtime_task(fails())
            await asyncio.sleep(0)
            await asyncio.sleep(0)
        self.assertTrue(any("Realtime task failed" in record for record in records.output))
        self.assertFalse(api._realtime_tasks)


class ReminderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with self.sessions() as db:
            user = User(username="reminder_user", hashed_password="x")
            db.add(user)
            await db.flush()
            conversation = Conversation(type=ConversationType.GROUP, title="reminders")
            db.add(conversation)
            await db.flush()
            db.add(Task(conversation_id=conversation.id, creator_id=user.id, title="due", due_at=datetime.now(timezone.utc) - timedelta(minutes=2)))
            await db.commit()

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def test_claim_only_sends_once_across_scans(self):
        sent = []
        async def notify(*args, **kwargs):
            sent.append(args[1].id)
        with patch.object(task_service, "_notify", side_effect=notify):
            async with self.sessions() as db:
                self.assertEqual(await task_service.scan_and_send_reminders(db), 1)
            async with self.sessions() as db:
                self.assertEqual(await task_service.scan_and_send_reminders(db), 0)
        self.assertEqual(len(sent), 1)

    async def test_parallel_workers_only_claim_once(self):
        # Separate sessions compete for the same task; only one conditional UPDATE wins.
        sent = []
        async def notify(*args, **kwargs):
            sent.append(args[1].id)
        with patch.object(task_service, "_notify", side_effect=notify):
            async with self.sessions() as first, self.sessions() as second:
                counts = await asyncio.gather(
                    task_service.scan_and_send_reminders(first),
                    task_service.scan_and_send_reminders(second),
                )
        self.assertEqual(sum(counts), 1)
        self.assertEqual(len(sent), 1)

    async def test_failed_notification_resets_claim_for_retry(self):
        with patch.object(task_service, "_notify", side_effect=RuntimeError("temporary failure")):
            async with self.sessions() as db:
                with self.assertLogs(task_service.logger, logging.ERROR):
                    self.assertEqual(await task_service.scan_and_send_reminders(db), 0)
        with patch.object(task_service, "_notify", new_callable=AsyncMock) as notify:
            async with self.sessions() as db:
                self.assertEqual(await task_service.scan_and_send_reminders(db), 1)
            notify.assert_awaited_once()
