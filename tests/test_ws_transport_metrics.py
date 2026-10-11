"""Synthetic ASGI WebSockets/Redis only; no app.main or production configuration."""
from __future__ import annotations

import asyncio
import json
import logging
import unittest
from io import StringIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from starlette.websockets import WebSocket

from app.core.transport_metrics import TransportMetrics
from app.websocket import manager as module


class Socket:
    def __init__(self):
        self.frames = []
        self.gate = asyncio.Event()
        self.gate.set()
        self.socket = WebSocket({"type": "websocket", "path": "/ws/private-token", "headers": []},
                                self.receive, self.send)

    async def receive(self):
        return {"type": "websocket.connect"}

    async def send(self, frame):
        if frame["type"] == "websocket.send":
            await self.gate.wait()
        self.frames.append(frame)

    @property
    def texts(self):
        return [frame["text"] for frame in self.frames if frame["type"] == "websocket.send"]


class MetricsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.stream = StringIO()
        self.logger = logging.Logger("ws-transport-test", level=logging.INFO)
        self.logger.addHandler(logging.StreamHandler(self.stream))
        self.metrics = TransportMetrics(logger=self.logger)
        self.channels = []
        self.sockets = []

    async def asyncTearDown(self):
        for channel, owner, socket in self.sockets:
            await channel.disconnect(owner, socket.socket)

    def channel(self, factory=module.GameConnectionManager):
        channel = factory()
        channel.transport_metrics = self.metrics
        self.channels.append(channel)
        return channel

    async def connect(self, channel, owner=7):
        socket = Socket()
        await channel.connect(owner, socket.socket)
        self.sockets.append((channel, owner, socket))
        return socket

    async def settle(self, predicate):
        for _ in range(100):
            if predicate():
                # Fake ASGI send captures a frame before wait_for/send_text
                # resumes; let the writer finish and record acceptance.
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                return
            await asyncio.sleep(0.01)
        self.fail("synthetic writer failed to settle")

    def take(self):
        self.metrics.flush()
        raw = self.stream.getvalue()
        self.stream.seek(0)
        self.stream.truncate()
        parsed = [json.loads(line) for line in raw.splitlines()]
        return parsed, raw

    @staticmethod
    def row(logs, namespace, event, operation):
        return next(row for log in logs for row in log["buckets"]
                    if (row["namespace"], row["event_class"], row["operation"]) == (namespace, event, operation))

    async def test_two_sockets_exact_unicode_bytes_and_fixed_event_labels(self):
        channel = self.channel()
        first, second = await self.connect(channel), await self.connect(channel)
        await channel.send_to_conversation(7, "message.created", {"body": "你好😀", "secret": "DO-NOT-LOG"})
        await self.settle(lambda: bool(first.texts and second.texts))
        text = first.texts[0]
        self.assertEqual(second.texts, [text])
        await channel.send_to_conversation(7, "presence.updated", {"status": "在线"})
        await self.settle(lambda: len(first.texts) == 2 and len(second.texts) == 2)
        await channel.send_text(7, first.socket, "pong")
        await self.settle(lambda: len(first.texts) == 3)
        logs, raw = self.take()
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]["event"], "ws_transport")
        self.assertEqual(self.row(logs, "game", "message.created", "ws_send_accepted"),
                         {"namespace": "game", "event_class": "message.created", "operation": "ws_send_accepted",
                          "count": 2, "payload_bytes_accepted": 2 * len(text.encode("utf-8"))})
        self.assertEqual(self.row(logs, "game", "presence.updated", "ws_send_accepted")["count"], 2)
        self.assertEqual(self.row(logs, "game", "pong", "ws_send_accepted")["payload_bytes_accepted"], 4)
        self.assertEqual(self.row(logs, "game", "other", "ws_connection_open")["count"], 2)
        for private in ("DO-NOT-LOG", "你好", "/ws/private-token", "secret"):
            self.assertNotIn(private, raw)
        self.assertEqual(self.take()[0], [])
        await channel.disconnect(7, first.socket)
        await channel.disconnect(7, first.socket)
        await channel.disconnect(7, second.socket)
        self.assertEqual(self.row(self.take()[0], "game", "other", "ws_connection_close")["count"], 2)

    async def test_timeout_and_queue_overflow_never_claim_payload_accepted(self):
        channel = self.channel()
        slow = await self.connect(channel)
        slow.gate.clear()
        with patch.object(module, "WEBSOCKET_SEND_TIMEOUT_SECONDS", 0.02):
            await channel.send_to_conversation(7, "message.created", {"body": "private"})
            await self.settle(lambda: channel.connection_count() == 0)
        logs, _ = self.take()
        self.assertEqual(self.row(logs, "game", "message.created", "ws_send_timeout")["count"], 1)
        self.assertFalse(any(row["operation"] == "ws_send_accepted" for log in logs for row in log["buckets"]))
        slow.gate.set()
        overflow = await self.connect(channel, 8)
        overflow.gate.clear()
        await channel.send_to_conversation(8, "first", {})
        await asyncio.sleep(0)
        for index in range(module.WEBSOCKET_SEND_QUEUE_SIZE + 1):
            await channel.send_to_conversation(8, "overflow-event", {"index": index})
        await self.settle(lambda: channel.connection_count() == 0)
        logs, _ = self.take()
        self.assertEqual(self.row(logs, "game", "other", "ws_queue_overflow")["count"], 1)
        self.assertFalse(any(row["operation"] == "ws_send_accepted" for log in logs for row in log["buckets"]))
        overflow.gate.set()

    async def test_redis_success_failure_and_remote_listener_not_double_counted(self):
        sender, receiver = self.channel(module.ConnectionManager), self.channel(module.ConnectionManager)
        socket = await self.connect(receiver, 31415926)
        sender._redis = SimpleNamespace(publish=AsyncMock())
        data = {"body": "密钥DO-NOT-LOG", "owner": "31415926"}
        await sender.send_to_users({31415926}, "message.created", data)
        wire = sender._redis.publish.await_args.args[1]
        self.assertEqual(json.loads(wire)["data"], data)
        shared = module._SharedRealtimeSubscriber(receiver, module.AdminConnectionManager(),
                                                   module.BotConnectionManager(), module.GameConnectionManager())
        await shared._dispatch(wire)  # local receiver ASGI bytes only; no second publish accounting.
        await self.settle(lambda: len(socket.texts) == 1)
        logs, raw = self.take()
        self.assertEqual(self.row(logs, "user", "message.created", "redis_publish_accepted")["payload_bytes_accepted"],
                         len(wire.encode("utf-8")))
        self.assertEqual(self.row(logs, "user", "message.created", "ws_send_accepted")["count"], 1)
        self.assertEqual(sum(row["count"] for log in logs for row in log["buckets"]
                             if row["operation"] == "redis_publish_accepted"), 1)
        for private in ("密钥", "DO-NOT-LOG", "31415926", "/ws/private-token"):
            self.assertNotIn(private, raw)
        sender._redis.publish = AsyncMock(side_effect=ConnectionError("synthetic Redis outage"))
        with self.assertLogs(module.logger, "ERROR"):
            await sender.send_to_users({31415926}, "message.created", data)
        logs, _ = self.take()
        self.assertEqual(self.row(logs, "user", "message.created", "redis_publish_failed")["count"], 1)
        self.assertFalse(any(row["operation"] == "redis_publish_accepted" for log in logs for row in log["buckets"]))

    async def test_send_error_is_bounded_and_never_logs_exception_contents(self):
        channel = self.channel()
        socket = await self.connect(channel, 99188271)
        async def broken_send(frame):
            if frame["type"] == "websocket.send":
                raise RuntimeError("LEAK-THIS-KEY 99188271 /ws/private-token")
            socket.frames.append(frame)
        socket.socket._send = broken_send
        with self.assertLogs(module.logger, "WARNING") as logs:
            await channel.send_to_conversation(99188271, "message.created", {"body": "LEAK-THIS-KEY"})
            await self.settle(lambda: channel.connection_count() == 0)
        self.assertNotIn("LEAK-THIS-KEY", " ".join(logs.output))
        self.assertNotIn("99188271", " ".join(logs.output))
        records, raw = self.take()
        self.assertEqual(self.row(records, "game", "message.created", "ws_send_failed")["count"], 1)
        self.assertFalse(any(row["operation"] == "ws_send_accepted" for log in records for row in log["buckets"]))
        self.assertNotIn("LEAK-THIS-KEY", raw)

    async def test_ten_thousand_dynamic_event_names_cannot_create_labels(self):
        for index in range(10000):
            self.metrics.record_ws(namespace="bot", event=f"token-{index}/private", operation="ws_send_accepted", payload_bytes=3)
        logs, raw = self.take()
        self.assertEqual(len(logs[0]["buckets"]), 1)
        self.assertEqual(self.row(logs, "bot", "other", "ws_send_accepted")["payload_bytes_accepted"], 30000)
        self.assertNotIn("token-", raw)
        disabled = module.GameConnectionManager()
        self.assertIsNone(disabled.transport_metrics)
        self.assertEqual(self.take()[0], [])


if __name__ == "__main__":
    unittest.main()
