"""Bot realtime broadcasts must finish while the request's DB session is open."""
from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from app.api.v1 import bot_api
from app.models.message import Message
from app.models.user import User
from app.schemas.bot import BotDirectMessageCreate, BotSendMessageCreate


class RequestSession:
    """Simulate the dependency closing the request-scoped session on exit."""

    def __init__(self):
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.closed = True

    def ensure_open(self):
        if self.closed:
            raise RuntimeError("request session already closed")


class BotRealtimeScopeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.user = User(id=2, username="bot_user", hashed_password="x", is_bot=True)
        self.user.content_moderation_whitelisted = True
        self.context = SimpleNamespace(bot=SimpleNamespace(id=7, owner_id=11), user=self.user)
        self.message = Message(id=31, conversation_id=5, sender_id=2, content="hello")
        self.message.sender = self.user
        self.message._sender_display_name = "bot_user"
        self.payload = BotSendMessageCreate(content="hello")

    async def test_group_waits_for_db_dependent_bot_game_and_user_delivery(self):
        db = RequestSession()
        entered = asyncio.Event()
        release = asyncio.Event()
        deliveries = []

        async def users(ids, event, data):
            db.ensure_open()
            deliveries.append(("users", ids, event))
            entered.set()
            await release.wait()
            db.ensure_open()

        async def bot(_db, event, conversation_id, data, *, sender_id=None):
            self.assertIs(_db, db)
            db.ensure_open()
            deliveries.append(("bot", conversation_id, event, sender_id))

        async def game(_db, event, conversation_id, data):
            self.assertIs(_db, db)
            db.ensure_open()
            deliveries.append(("game", conversation_id, event))

        async def send(_db, *args):
            self.assertIs(_db, db)
            db.ensure_open()
            return self.message

        with db, patch.object(bot_api.bot_service, "validate_bot_can_use_message_conversation", new_callable=AsyncMock) as authorize, patch.object(
            bot_api.message_service, "send_message", side_effect=send
        ) as commit, patch.object(bot_api, "online_conversation_member_ids", new_callable=AsyncMock, return_value=[2, 3]), patch.object(
            bot_api.manager, "send_to_users", side_effect=users
        ), patch("app.api.v1.conversations.send_bot_message_updates", side_effect=bot), patch(
            "app.api.v1.conversations.send_game_message_updates", side_effect=game
        ), patch.object(bot_api.message_service, "build_new_message_read", return_value=SimpleNamespace(id=31)), patch.object(
            bot_api, "schedule_realtime_task"
        ) as schedule, patch.object(bot_api.moderation_service, "schedule_message_moderation") as moderation:
            task = asyncio.create_task(bot_api.bot_send_message(5, self.payload, self.context, db))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                self.assertFalse(task.done(), "HTTP handler returned before realtime delivery finished")
                self.assertEqual(deliveries, [("users", {2, 3}, "message.created")])
                release.set()
                response = await asyncio.wait_for(task, 2)
                self.assertEqual(response.id, 31)
                self.assertEqual([entry[0] for entry in deliveries], ["users", "bot", "game"])
                authorize.assert_awaited_once_with(db, self.context, 5)
                commit.assert_awaited_once()
                moderation.assert_not_called()
                # Only the DB-free admin signal may be scheduled after delivery.
                schedule.assert_called_once()
                admin_signal = schedule.call_args.args[0]
                self.assertEqual(admin_signal.cr_code.co_name, "send_admin_message_signal")
                admin_signal.close()
            finally:
                release.set()
                if not task.done():
                    await task
        self.assertTrue(db.closed)

    async def test_direct_waits_for_conversation_then_message_before_return(self):
        db = RequestSession()
        entered = asyncio.Event()
        release = asyncio.Event()
        calls = []
        message_entered = asyncio.Event()
        message_release = asyncio.Event()
        conversation = SimpleNamespace(id=5)

        async def commit(_db, *_args):
            db.ensure_open()
            calls.append("commit")
            return self.message

        async def recipients(_db, conversation_id):
            db.ensure_open()
            calls.append("members")
            return [2, 3]

        async def conversation_update(_db, conversation_id, ids):
            db.ensure_open()
            calls.append("conversation.updated")
            entered.set()
            await release.wait()
            db.ensure_open()

        async def message_update(_db, event, message, ids):
            db.ensure_open()
            calls.append("message.created")
            self.assertEqual(ids, [2, 3])
            message_entered.set()
            await message_release.wait()
            db.ensure_open()

        with db, patch.object(bot_api.conversation_service, "get_or_create_direct_conversation", new_callable=AsyncMock, return_value=conversation) as create, patch.object(
            bot_api.conversation_service, "conversation_member_ids", side_effect=recipients
        ), patch.object(bot_api.message_service, "send_message", side_effect=commit), patch.object(
            bot_api, "send_conversation_updates", side_effect=conversation_update
        ), patch.object(bot_api, "online_conversation_member_ids", new_callable=AsyncMock, return_value=[2, 3]), patch.object(
            bot_api, "send_message_updates", side_effect=message_update
        ), patch.object(bot_api.message_service, "build_new_message_read", return_value=SimpleNamespace(id=31)), patch.object(
            bot_api, "schedule_realtime_task"
        ) as schedule:
            task = asyncio.create_task(bot_api.bot_send_direct_message_by_payload(BotDirectMessageCreate(user_id=3, content="hello"), self.context, db))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                self.assertFalse(task.done(), "HTTP handler returned before conversation.updated completed")
                self.assertEqual(calls, ["commit", "members", "conversation.updated"])
                release.set()
                await asyncio.wait_for(message_entered.wait(), 2)
                self.assertFalse(task.done(), "HTTP handler returned before message.created completed")
                self.assertEqual(calls, ["commit", "members", "conversation.updated", "message.created"])
                message_release.set()
                result = await asyncio.wait_for(task, 2)
                self.assertEqual(result.id, 31)
                create.assert_awaited_once_with(db, self.user, 3, True)
                schedule.assert_called_once()
                schedule.call_args.args[0].close()
            finally:
                release.set()
                message_release.set()
                if not task.done():
                    await task
        self.assertTrue(db.closed)

    async def test_failed_direct_send_does_not_announce_or_schedule(self):
        db = RequestSession()
        with patch.object(bot_api.conversation_service, "get_or_create_direct_conversation", new_callable=AsyncMock, return_value=SimpleNamespace(id=5)), patch.object(
            bot_api.message_service, "send_message", new_callable=AsyncMock, side_effect=HTTPException(status_code=429, detail="rate limited")
        ), patch.object(bot_api.conversation_service, "conversation_member_ids", new_callable=AsyncMock) as recipients, patch.object(
            bot_api, "send_conversation_updates", new_callable=AsyncMock
        ) as conversation_update, patch.object(bot_api, "send_message_updates", new_callable=AsyncMock) as message_update, patch.object(
            bot_api, "schedule_realtime_task"
        ) as schedule:
            with db:
                with self.assertRaises(HTTPException) as caught:
                    await bot_api.bot_send_direct_message(3, self.payload, self.context, db)
            self.assertEqual(caught.exception.status_code, 429)
            recipients.assert_not_awaited()
            conversation_update.assert_not_awaited()
            message_update.assert_not_awaited()
            schedule.assert_not_called()
        self.assertTrue(db.closed)

    async def test_direct_delivery_failure_is_logged_and_other_signals_continue(self):
        db = RequestSession()
        calls = []

        async def send(_db, *_args):
            db.ensure_open()
            return self.message

        async def conversation_update(_db, *_args):
            db.ensure_open()
            calls.append("conversation.updated")
            raise ConnectionError("publish failed")

        async def message_update(_db, *_args):
            db.ensure_open()
            calls.append("message.created")
            raise ConnectionError("publish failed")

        with patch.object(bot_api.conversation_service, "get_or_create_direct_conversation", new_callable=AsyncMock, return_value=SimpleNamespace(id=5)), patch.object(
            bot_api.message_service, "send_message", side_effect=send
        ), patch.object(bot_api.message_service, "build_new_message_read", return_value=SimpleNamespace(id=31)), patch.object(
            bot_api.conversation_service, "conversation_member_ids", new_callable=AsyncMock, return_value=[2, 3]
        ), patch.object(bot_api, "online_conversation_member_ids", new_callable=AsyncMock, return_value=[2, 3]), patch.object(
            bot_api, "send_conversation_updates", side_effect=conversation_update
        ), patch.object(bot_api, "send_message_updates", side_effect=message_update), patch.object(
            bot_api, "schedule_realtime_task"
        ) as schedule, self.assertLogs(bot_api.logger, level="ERROR") as logged:
            with db:
                response = await bot_api.bot_send_direct_message(3, self.payload, self.context, db)
            self.assertEqual(response.id, 31)
            self.assertEqual(calls, ["conversation.updated", "message.created"])
            self.assertEqual(sum("Realtime task failed" in line for line in logged.output), 2)
            schedule.assert_called_once()
            schedule.call_args.args[0].close()
        self.assertTrue(db.closed)

    async def test_group_authorization_and_direct_block_boundary_unchanged(self):
        db = RequestSession()
        denied = HTTPException(status_code=403, detail="not allowed")
        with patch.object(bot_api.bot_service, "validate_bot_can_use_message_conversation", new_callable=AsyncMock, side_effect=denied) as authorize, patch.object(
            bot_api.conversation_service, "get_or_create_direct_conversation", new_callable=AsyncMock, side_effect=denied
        ) as create, patch.object(bot_api.message_service, "send_message", new_callable=AsyncMock) as send, patch.object(
            bot_api, "send_message_updates", new_callable=AsyncMock
        ) as updates, patch.object(bot_api, "schedule_realtime_task") as schedule:
            with db:
                with self.assertRaises(HTTPException) as group:
                    await bot_api.bot_send_message(5, self.payload, self.context, db)
                with self.assertRaises(HTTPException) as self_message:
                    await bot_api.bot_send_direct_message(2, self.payload, self.context, db)
                with self.assertRaises(HTTPException) as blocked:
                    await bot_api.bot_send_direct_message(3, self.payload, self.context, db)
            self.assertEqual((group.exception.status_code, self_message.exception.status_code, blocked.exception.status_code), (403, 400, 403))
            authorize.assert_awaited_once_with(db, self.context, 5)
            create.assert_awaited_once_with(db, self.user, 3, True)
            send.assert_not_awaited()
            updates.assert_not_awaited()
            schedule.assert_not_called()

    async def test_failed_group_send_does_not_leak_background_task(self):
        db = RequestSession()
        with patch.object(bot_api.bot_service, "validate_bot_can_use_message_conversation", new_callable=AsyncMock), patch.object(
            bot_api.message_service, "send_message", new_callable=AsyncMock, side_effect=HTTPException(status_code=429, detail="rate limited")
        ), patch.object(bot_api, "send_message_updates", new_callable=AsyncMock) as update, patch.object(
            bot_api, "schedule_realtime_task"
        ) as schedule:
            with db:
                with self.assertRaises(HTTPException) as caught:
                    await bot_api.bot_send_message(5, self.payload, self.context, db)
            self.assertEqual(caught.exception.status_code, 429)
            update.assert_not_awaited()
            schedule.assert_not_called()
