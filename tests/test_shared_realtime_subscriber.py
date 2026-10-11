"""A synthetic old-four/new-one worker on the unchanged Redis topic and wire format."""
from __future__ import annotations

import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from starlette.websockets import WebSocketState

from app.websocket import manager as module


class FakeSocket:
    application_state = WebSocketState.CONNECTED


class FakePubSub:
    def __init__(self, broker):
        self.broker = broker
        self.queue = asyncio.Queue()
        self.closed = False
        self.channels = []

    async def subscribe(self, channel):
        assert channel == self.broker.topic
        self.channels.append(channel)
        self.broker.subscribers.add(self)

    async def unsubscribe(self, channel):
        self.broker.subscribers.discard(self)

    async def aclose(self):
        self.closed = True
        self.broker.subscribers.discard(self)
        self.queue.put_nowait(None)

    async def listen(self):
        while not self.closed:
            message = await self.queue.get()
            if message is None:
                if not self.closed:
                    raise ConnectionError("synthetic Redis disconnect")
                return
            yield message

    def fail(self):
        self.queue.put_nowait(None)


class FakeRedis:
    def __init__(self, broker):
        self.broker = broker
        self.pubsubs = []
        self.closed = False

    def pubsub(self):
        pubsub = FakePubSub(self.broker)
        self.pubsubs.append(pubsub)
        return pubsub

    async def publish(self, channel, data):
        assert channel == self.broker.topic
        self.broker.published.append(data)
        await self.broker.inject(data)

    async def aclose(self):
        self.closed = True


class FakeBroker:
    def __init__(self):
        self.topic = "kukechat:test:realtime"
        self.subscribers = set()
        self.published = []
        self.clients = []

    def from_url(self, url, *, decode_responses):
        assert url == "redis://synthetic" and decode_responses
        client = FakeRedis(self)
        self.clients.append(client)
        return client

    async def inject(self, data):
        for pubsub in list(self.subscribers):
            pubsub.queue.put_nowait({"type": "message", "data": data})


class SharedSubscriberTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.broker = FakeBroker()
        self.settings_url = patch.object(module.settings, "redis_url", "redis://synthetic")
        self.settings_topic = patch.object(module.settings, "realtime_redis_channel", self.broker.topic)
        self.redis_factory = patch("redis.asyncio.from_url", side_effect=self.broker.from_url)
        self.settings_url.start()
        self.settings_topic.start()
        self.redis_factory.start()
        self.addCleanup(self.redis_factory.stop)
        self.addCleanup(self.settings_topic.stop)
        self.addCleanup(self.settings_url.stop)
        self.new = {name: factory() for name, factory in (
            ("user", module.ConnectionManager), ("admin", module.AdminConnectionManager),
            ("bot", module.BotConnectionManager), ("game", module.GameConnectionManager),
        )}
        self.old = {name: factory() for name, factory in (
            ("user", module.ConnectionManager), ("admin", module.AdminConnectionManager),
            ("bot", module.BotConnectionManager), ("game", module.GameConnectionManager),
        )}
        self.shared = module._SharedRealtimeSubscriber(*self.new.values())
        for manager in self.new.values():
            # Avoid presence-setup dependencies without changing base manager start.
            await module._QueuedRealtimeManager.start(manager)
        await self.shared.start()
        self.old_tasks = []
        self.old_pubsubs = []
        for name, channel in self.old.items():
            channel._redis = self.broker.from_url("redis://synthetic", decode_responses=True)
            pubsub = channel._redis.pubsub()
            await pubsub.subscribe(self.broker.topic)
            self.old_pubsubs.append(pubsub)
            self.old_tasks.append(asyncio.create_task(self._old_listener(name, channel, pubsub)))
        self.new_queues = self._attach(self.new)
        self.old_queues = self._attach(self.old)

    @staticmethod
    def _attach(managers):
        queues = {}
        for name, channel in managers.items():
            queue = asyncio.Queue(maxsize=100)
            channel._connections[7][FakeSocket()] = SimpleNamespace(queue=queue, close_code=None)
            queues[name] = queue
        return queues

    async def _old_listener(self, name, channel, pubsub):
        # Historical worker: four independent subscribers, each parses every
        # message and checks its own namespace and origin before dispatch.
        async for message in pubsub.listen():
            try:
                event = json.loads(message["data"])
                if event.get("namespace") != name or event.get("origin") == channel._origin:
                    continue
                payload = {"type": event.get("event_type"), "data": event.get("data")}
                if event.get("action") == "broadcast":
                    await channel._broadcast_local(payload)
                elif event.get("action") in ("user", "users", "disconnect"):
                    ids = {int(owner_id) for owner_id in event.get("owner_ids") or []}
                    if event["action"] == "disconnect":
                        await channel._close_owners_local(ids, payload)
                    else:
                        await channel._send_to_owners_local(ids, payload)
            except (TypeError, ValueError, AttributeError):
                pass

    async def asyncTearDown(self):
        await self.shared.stop()
        for pubsub in self.old_pubsubs:
            await pubsub.aclose()
        for task in self.old_tasks:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        for channel in self.new.values():
            await module._QueuedRealtimeManager.stop(channel)
        for channel in self.old.values():
            await module._QueuedRealtimeManager.stop(channel)

    async def _settle(self):
        for _ in range(40):
            await asyncio.sleep(0)
            if all(pubsub.queue.empty() for pubsub in self.broker.subscribers):
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                return
        self.fail("broker did not drain")

    async def test_cross_versions_all_namespaces_wire_bytes_origin_and_parse_once(self):
        actual_loads = module.json.loads
        with patch.object(module.json, "loads", wraps=actual_loads) as loads:
            for name, action in (("user", "users"), ("admin", "broadcast"),
                                 ("bot", "user"), ("game", "user")):
                old = self.old[name]
                await old._publish(action, name + ".old", {"source": "old"}, None if action == "broadcast" else {7})
                await self._settle()
                new = self.new[name]
                await new._publish(action, name + ".new", {"source": "new"}, None if action == "broadcast" else {7})
                await self._settle()
            # 8 publishes, new worker receives every one once, including its
            # own echoes. Old worker has 4 subscriptions, parsing each 4 times.
            self.assertEqual(loads.call_count, 8 * 5)
        self.assertEqual(len(self.broker.subscribers), 5)
        for name in self.new:
            self.assertEqual([json.loads(self.new_queues[name].get_nowait())["type"] for _ in range(1)], [name + ".old"])
            self.assertTrue(self.new_queues[name].empty())
            self.assertEqual([json.loads(self.old_queues[name].get_nowait())["type"] for _ in range(1)], [name + ".new"])
            self.assertTrue(self.old_queues[name].empty())
        for wire in self.broker.published:
            self.assertIsInstance(wire, str)
            envelope = json.loads(wire)
            self.assertEqual(set(envelope), {"origin", "namespace", "action", "owner_ids", "event_type", "data"})
            self.assertEqual(envelope["owner_ids"], None if envelope["action"] == "broadcast" else [7])
            self.assertEqual(wire, json.dumps(envelope))  # historical publisher bytes

    async def test_malformed_namespace_action_target_and_sequential_delivery(self):
        # Test only new subscriber (old workers deliberately lack strict validation).
        for pubsub in self.old_pubsubs:
            await pubsub.aclose()
        valid = {"origin": self.old["user"]._origin, "namespace": "user", "action": "user",
                 "owner_ids": [7], "event_type": "safe", "data": {"ok": True}}
        invalid = ["{broken", "[]", json.dumps({**valid, "namespace": "other"}),
                   json.dumps({**valid, "namespace": "admin", "action": "user"}),
                   json.dumps({**valid, "action": "broadcast", "owner_ids": [7]}),
                   json.dumps({**valid, "owner_ids": "7"}),
                   json.dumps({**valid, "owner_ids": [True]}),
                   json.dumps({**valid, "owner_ids": ["oops"]}),
                   json.dumps({**valid, "origin": self.new["user"]._origin}),
                   json.dumps({**valid, "event_type": None})]
        for wire in invalid:
            await self.broker.inject(wire)
        await self.broker.inject(json.dumps(valid))
        await self.broker.inject(json.dumps({**valid, "event_type": "next"}))
        await self._settle()
        self.assertEqual([json.loads(self.new_queues["user"].get_nowait())["type"] for _ in range(2)], ["safe", "next"])
        self.assertTrue(all(queue.empty() for name, queue in self.new_queues.items() if name != "user"))

    async def test_dispatch_awaits_previous_event_before_reading_next(self):
        for pubsub in self.old_pubsubs:
            await pubsub.aclose()
        entered = asyncio.Event()
        release = asyncio.Event()
        dispatched = []
        channel = self.new["user"]
        original = channel._send_to_owners_local
        async def paused(ids, payload):
            dispatched.append(payload["type"])
            if len(dispatched) == 1:
                entered.set()
                await release.wait()
        channel._send_to_owners_local = paused
        wire = {"origin": self.old["user"]._origin, "namespace": "user", "action": "user",
                "owner_ids": [7], "event_type": "first", "data": {}}
        try:
            await self.broker.inject(json.dumps(wire))
            await asyncio.wait_for(entered.wait(), timeout=1)
            await self.broker.inject(json.dumps({**wire, "event_type": "second"}))
            await asyncio.sleep(0)
            self.assertEqual(dispatched, ["first"])
            release.set()
            await self._settle()
            self.assertEqual(dispatched, ["first", "second"])
        finally:
            release.set()
            channel._send_to_owners_local = original

    async def test_disconnect_dispatch_and_publish_failure_keep_local_delivery(self):
        for pubsub in self.old_pubsubs:
            await pubsub.aclose()
        closed = []
        original = self.new["user"]._close_owners_local
        async def capture(ids, payload):
            closed.append((ids, payload))
        self.new["user"]._close_owners_local = capture
        try:
            sender = self.old["user"]
            await sender._publish("disconnect", "user.banned", {"reason": "test"}, {7})
            await self._settle()
            self.assertEqual(closed, [({7}, {"type": "user.banned", "data": {"reason": "test"}})])
            for name in ("admin", "bot", "game"):
                self.assertTrue(self.new_queues[name].empty())
            with patch.object(self.new["game"]._redis, "publish", new_callable=AsyncMock,
                              side_effect=ConnectionError("synthetic Redis outage")):
                with self.assertLogs(module.logger, "ERROR"):
                    await self.new["game"].send_to_conversation(7, "game.local", {"ok": True})
            self.assertEqual(json.loads(self.new_queues["game"].get_nowait()),
                             {"type": "game.local", "data": {"ok": True}})
        finally:
            self.new["user"]._close_owners_local = original

    async def test_presence_starts_before_explicit_shared_subscriber(self):
        user, bot = module.ConnectionManager(), module.BotConnectionManager()
        try:
            with patch.object(user, "_ensure_presence_instance", new_callable=AsyncMock):
                await user.start()
                await bot.start()
            self.assertIsNotNone(user._presence_task)
            self.assertIsNotNone(bot._presence_task)
            self.assertTrue(user.redis_enabled())
            self.assertTrue(bot.redis_enabled())
        finally:
            await user.stop()
            await bot.stop()
        self.assertIsNone(user._presence_task)
        self.assertIsNone(bot._presence_task)

    async def test_restarting_manager_after_shared_shutdown_restarts_presence(self):
        await self.shared.stop()
        user = self.new["user"]
        await module._QueuedRealtimeManager.stop(user)
        with patch.object(user, "_ensure_presence_instance", new_callable=AsyncMock):
            await user.start()
        self.assertIsNone(user._shared_subscriber)
        self.assertIsNotNone(user._presence_task)
        await user.stop()

    async def test_reconnect_health_stop_and_independent_manager_stop(self):
        first = self.shared._pubsub
        shared_client = self.shared._redis
        self.assertTrue(all(channel.redis_enabled() for channel in self.new.values()))
        # Manager alone owns only its publish client, not the shared stream.
        await module._QueuedRealtimeManager.stop(self.new["admin"])
        self.assertTrue(self.shared.redis_enabled())
        self.assertTrue(self.new["user"].redis_enabled())
        self.assertFalse(self.new["admin"].redis_enabled())
        first.fail()
        for _ in range(100):
            if first.closed:
                break
            await asyncio.sleep(0.001)
        self.assertTrue(first.closed)
        self.assertFalse(self.new["user"].redis_enabled())
        # First retry uses the production one-second backoff.
        for _ in range(130):
            if self.shared._pubsub is not None and self.shared._pubsub is not first:
                break
            await asyncio.sleep(0.01)
        self.assertIsNot(self.shared._pubsub, first)
        self.assertTrue(self.new["user"].redis_enabled())
        await self.old["game"]._publish("user", "game.reconnected", {"ok": True}, {7})
        await self._settle()
        self.assertEqual(json.loads(self.new_queues["game"].get_nowait())["type"], "game.reconnected")
        await self.shared.stop()
        self.assertFalse(self.shared.redis_enabled())
        self.assertEqual(len(self.broker.subscribers), 4)
        self.assertTrue(all(pubsub.closed for pubsub in shared_client.pubsubs))
        self.assertTrue(shared_client.closed)
        await self.old["game"]._publish("user", "no.deliver", {}, {7})
        await self._settle()
        self.assertTrue(self.new_queues["game"].empty())
