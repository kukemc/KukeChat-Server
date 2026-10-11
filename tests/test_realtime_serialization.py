"""WebSocket text framing and one-serialization-per-local-delivery regressions."""
from __future__ import annotations

import asyncio
import json
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi.encoders import jsonable_encoder
from starlette.websockets import WebSocket, WebSocketState

from app.websocket import manager as module
from app.websocket.manager import (
    AdminConnectionManager,
    BotConnectionManager,
    ConnectionManager,
    GameConnectionManager,
    WEBSOCKET_SEND_QUEUE_SIZE,
)


class SocketHarness:
    """Use a real Starlette WebSocket to compare its text-frame serialization."""

    def __init__(self):
        self.frames = []
        self.gate = asyncio.Event()
        self.gate.set()
        self.socket = WebSocket({"type": "websocket", "path": "/ws", "headers": []}, self.receive, self.send)

    async def receive(self):
        return {"type": "websocket.connect"}

    async def send(self, frame):
        if frame["type"] == "websocket.send":
            await self.gate.wait()
        self.frames.append(frame)

    def texts(self):
        return [frame["text"] for frame in self.frames if frame["type"] == "websocket.send"]


class FakeSocket:
    application_state = WebSocketState.CONNECTED


class SerializationTests(unittest.IsolatedAsyncioTestCase):
    async def test_starlette_exact_text_framing_and_jsonable_datetime(self):
        event = {"type": "presence.changed", "data": jsonable_encoder({
            "name": "在线😀", "when": datetime(2025, 1, 2, tzinfo=timezone.utc),
            "special": float("nan"),
        })}
        reference = SocketHarness()
        await reference.socket.accept()
        await reference.socket.send_json(event)
        expected = reference.texts()[0]
        self.assertEqual(expected, json.dumps(event, separators=(",", ":"), ensure_ascii=False))
        self.assertIn('"name":"在线😀"', expected)
        self.assertIn('"special":NaN', expected)  # default allow_nan=True

        channel = GameConnectionManager()
        first, second = SocketHarness(), SocketHarness()
        try:
            await channel.connect(7, first.socket)
            await channel.connect(7, second.socket)
            await channel.send_to_conversation(7, event["type"], event["data"])
            for _ in range(30):
                if first.texts() and second.texts():
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(first.texts(), [expected])
            self.assertEqual(second.texts(), [expected])
            self.assertIsInstance(json.loads(expected)["data"]["when"], str)
        finally:
            await channel.disconnect(7, first.socket)
            await channel.disconnect(7, second.socket)

    async def test_virtual_1000_sockets_one_dump_and_same_queued_str(self):
        # No writer task or real socket per virtual recipient: only the fan-out is tested.
        channel = ConnectionManager()
        sockets = [FakeSocket() for _ in range(1000)]
        queues = [asyncio.Queue(maxsize=WEBSOCKET_SEND_QUEUE_SIZE) for _ in sockets]
        for index, (socket, queue) in enumerate(zip(sockets, queues)):
            channel._connections[index][socket] = SimpleNamespace(queue=queue, close_code=None)
        original = json.dumps
        with patch.object(module.json, "dumps", wraps=original) as dumps:
            await channel.broadcast("presence.changed", {"status": "在线😀"})
        self.assertEqual(dumps.call_count, 1)
        self.assertEqual(dumps.call_args.kwargs, {"separators": (",", ":"), "ensure_ascii": False})
        self.assertTrue(all(queue.qsize() == 1 for queue in queues))
        queued = [queue.get_nowait() for queue in queues]
        self.assertTrue(all(item is queued[0] for item in queued))
        self.assertEqual(queued[0], '{"type":"presence.changed","data":{"status":"在线😀"}}')

    async def test_single_owner_multiple_sockets_one_dump_and_no_empty_owner_dump(self):
        channel = GameConnectionManager()
        for _ in range(100):
            channel._connections[7][FakeSocket()] = SimpleNamespace(queue=asyncio.Queue(maxsize=100), close_code=None)
        original = json.dumps
        with patch.object(module.json, "dumps", wraps=original) as dumps:
            await channel._send_to_owner_local(7, {"type": "direct", "data": {"id": 1}})
            await channel._send_to_owner_local(99, {"type": "missing", "data": {}})
        self.assertEqual(dumps.call_count, 1)
        self.assertEqual(len({id(state.queue.get_nowait()) for state in channel._connections[7].values()}), 1)

    async def test_pong_is_raw_text_and_disconnect_payload_keeps_send_json(self):
        channel = GameConnectionManager()
        socket = SocketHarness()
        await channel.connect(3, socket.socket)
        try:
            await channel.send_text(3, socket.socket, "pong")
            for _ in range(30):
                if socket.texts():
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(socket.texts(), ["pong"])
            await channel.close_connection(3, socket.socket, code=1008, payload={"type": "bye", "data": "再见"})
            for _ in range(30):
                if socket.frames[-1]["type"] == "websocket.close":
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(socket.texts()[-1], '{"type":"bye","data":"再见"}')
            self.assertEqual(socket.frames[-1]["code"], 1008)
        finally:
            await channel.disconnect(3, socket.socket)

    async def test_overflow_and_timeout_still_close_1013_and_reconnect(self):
        channel = GameConnectionManager()
        slow = SocketHarness()
        await channel.connect(3, slow.socket)
        slow.gate.clear()
        try:
            await channel.send_to_conversation(3, "first", {})
            await asyncio.sleep(0)
            for index in range(WEBSOCKET_SEND_QUEUE_SIZE + 1):
                await channel.send_to_conversation(3, "more", {"id": index})
            for _ in range(60):
                if any(frame["type"] == "websocket.close" for frame in slow.frames):
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(slow.frames[-1]["code"], 1013)
            for _ in range(30):
                if channel.connection_count() == 0:
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(channel.connection_count(), 0)
        finally:
            slow.gate.set()
            await channel.disconnect(3, slow.socket)

        reconnect = SocketHarness()
        await channel.connect(3, reconnect.socket)
        try:
            await channel.send_to_conversation(3, "reconnected", {"ok": True})
            for _ in range(30):
                if reconnect.texts():
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(reconnect.texts(), ['{"type":"reconnected","data":{"ok":true}}'])
        finally:
            await channel.disconnect(3, reconnect.socket)

        timeout = SocketHarness()
        await channel.connect(4, timeout.socket)
        timeout.gate.clear()
        try:
            with patch.object(module, "WEBSOCKET_SEND_TIMEOUT_SECONDS", 0.02):
                await channel.send_to_conversation(4, "slow", {})
                for _ in range(30):
                    if any(frame["type"] == "websocket.close" for frame in timeout.frames):
                        break
                    await asyncio.sleep(0.01)
            self.assertEqual(timeout.frames[-1]["code"], 1013)
        finally:
            timeout.gate.set()
            await channel.disconnect(4, timeout.socket)

    async def test_local_remote_redis_event_single_data_encode_and_same_shape(self):
        sender, receiver = ConnectionManager(), ConnectionManager()
        sender._redis = SimpleNamespace(publish=AsyncMock())
        socket = SocketHarness()
        await receiver.connect(42, socket.socket)
        payload = {"title": "你好😀", "when": datetime(2025, 1, 2, tzinfo=timezone.utc)}
        actual_encoder = module.jsonable_encoder
        try:
            with patch.object(module, "jsonable_encoder", wraps=actual_encoder) as encoder:
                await sender.send_to_users({42}, "message.created", payload)
            encoder.assert_called_once_with(payload)
            wire = sender._redis.publish.await_args.args[1]
            envelope = json.loads(wire)
            self.assertEqual(envelope["owner_ids"], [42])
            self.assertEqual(envelope["data"], jsonable_encoder(payload))
            self.assertEqual(envelope["action"], "users")

            class PubSub:
                async def subscribe(self, channel):
                    pass
                async def aclose(self):
                    pass
                async def listen(self):
                    yield {"type": "message", "data": wire}
                    receiver._redis = None  # exit the reconnect loop after this synthetic message

            receiver._pubsub = PubSub()
            receiver._redis = SimpleNamespace()
            await receiver._listen_for_remote_events()
            for _ in range(30):
                if socket.texts():
                    break
                await asyncio.sleep(0.01)
            for _ in range(30):
                if socket.texts():
                    break
                await asyncio.sleep(0.01)
            expected = json.dumps({"type": "message.created", "data": jsonable_encoder(payload)},
                                  ensure_ascii=False, separators=(",", ":"))
            self.assertEqual(socket.texts(), [expected])
        finally:
            receiver._redis = None
            await receiver.disconnect(42, socket.socket)

    async def test_all_manager_variants_share_fanout_serializer(self):
        for factory in (ConnectionManager, AdminConnectionManager, BotConnectionManager, GameConnectionManager):
            with self.subTest(manager=factory.__name__):
                channel = factory()
                socket = FakeSocket()
                queue = asyncio.Queue(maxsize=100)
                channel._connections[1][socket] = SimpleNamespace(queue=queue, close_code=None)
                await channel._broadcast_local({"type": "event", "data": {"count": 1}})
                self.assertEqual(queue.get_nowait(), '{"type":"event","data":{"count":1}}')
