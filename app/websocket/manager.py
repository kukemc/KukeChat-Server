from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from fastapi.encoders import jsonable_encoder
from starlette.websockets import WebSocket, WebSocketState

from app.core.config import settings


logger = logging.getLogger(__name__)

WEBSOCKET_SEND_TIMEOUT_SECONDS = 1.5
WEBSOCKET_SEND_QUEUE_SIZE = 100
PRESENCE_TTL_SECONDS = 45
PRESENCE_REFRESH_SECONDS = 15
PRESENCE_INSTANCE_KEY = "kukechat:presence:instance"
PRESENCE_INIT_LOCK_KEY = "kukechat:presence:init_lock"
PRESENCE_USERS_KEY = "kukechat:presence:users"
PRESENCE_USER_KEY_PREFIX = "kukechat:presence:user:"
BOT_PRESENCE_USERS_KEY = "kukechat:presence:bots"
BOT_PRESENCE_KEY_PREFIX = "kukechat:presence:bot:"


@dataclass
class _ConnectionState:
    connection_id: str
    queue: asyncio.Queue[dict[str, Any]]
    task: asyncio.Task[None]


class _QueuedRealtimeManager:
    def __init__(self, namespace: str) -> None:
        self._namespace = namespace
        self._connections: dict[int, dict[WebSocket, _ConnectionState]] = defaultdict(dict)
        self._origin = uuid.uuid4().hex
        self._instance_id = os.environ.get("REALTIME_INSTANCE_ID") or self._origin
        self._redis: Any | None = None
        self._pubsub: Any | None = None
        self._subscriber_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if not settings.redis_url or self._subscriber_task:
            return
        try:
            import redis.asyncio as redis

            self._redis = redis.from_url(settings.redis_url, decode_responses=True)
            self._pubsub = self._redis.pubsub()
            await self._pubsub.subscribe(settings.realtime_redis_channel)
            self._subscriber_task = asyncio.create_task(self._listen_for_remote_events())
        except Exception:
            logger.exception("Redis realtime startup failed; falling back to local WebSocket delivery")
            await self.stop()

    async def stop(self) -> None:
        if self._subscriber_task:
            self._subscriber_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._subscriber_task
            self._subscriber_task = None
        if self._pubsub:
            with contextlib.suppress(Exception):
                await self._pubsub.unsubscribe(settings.realtime_redis_channel)
            with contextlib.suppress(Exception):
                await self._pubsub.close()
            self._pubsub = None
        if self._redis:
            with contextlib.suppress(Exception):
                await self._redis.aclose()
            self._redis = None

    def redis_enabled(self) -> bool:
        return self._redis is not None and self._subscriber_task is not None and not self._subscriber_task.done()

    async def connect(self, owner_id: int, websocket: WebSocket) -> str:
        await websocket.accept()
        connection_id = f"{self._instance_id}:{self._origin}:{id(websocket)}:{uuid.uuid4().hex}"
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=WEBSOCKET_SEND_QUEUE_SIZE)
        task = asyncio.create_task(self._send_loop(owner_id, websocket, queue))
        self._connections[owner_id][websocket] = _ConnectionState(connection_id=connection_id, queue=queue, task=task)
        return connection_id

    async def disconnect(self, owner_id: int, websocket: WebSocket) -> None:
        state = self._remove_connection(owner_id, websocket, cancel_task=True)
        if state:
            await self._after_connection_removed(owner_id, state)

    def connection_count(self) -> int:
        return sum(len(connections) for connections in self._connections.values())

    async def _send_loop(self, owner_id: int, websocket: WebSocket, queue: asyncio.Queue[dict[str, Any]]) -> None:
        try:
            while True:
                payload = await queue.get()
                if websocket.application_state != WebSocketState.CONNECTED:
                    return
                await asyncio.wait_for(websocket.send_json(payload), timeout=WEBSOCKET_SEND_TIMEOUT_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        finally:
            state = self._remove_connection(owner_id, websocket, cancel_task=False)
            if state:
                await self._after_connection_removed(owner_id, state)

    def _remove_connection(self, owner_id: int, websocket: WebSocket, *, cancel_task: bool) -> _ConnectionState | None:
        connections = self._connections.get(owner_id)
        if not connections:
            return None
        state = connections.pop(websocket, None)
        if state and cancel_task:
            state.task.cancel()
        if not connections:
            self._connections.pop(owner_id, None)
        return state

    async def _after_connection_removed(self, owner_id: int, state: _ConnectionState) -> None:
        return None

    async def _send_to_owner_local(self, owner_id: int, payload: dict[str, Any]) -> None:
        stale_connections: list[WebSocket] = []
        for websocket, state in list(self._connections.get(owner_id, {}).items()):
            if websocket.application_state != WebSocketState.CONNECTED or state.queue.full():
                stale_connections.append(websocket)
                continue
            state.queue.put_nowait(payload)
        for websocket in stale_connections:
            await self.disconnect(owner_id, websocket)

    async def _send_to_owners_local(self, owner_ids: set[int], payload: dict[str, Any]) -> None:
        await asyncio.gather(*(self._send_to_owner_local(owner_id, payload) for owner_id in owner_ids), return_exceptions=True)

    async def _broadcast_local(self, payload: dict[str, Any]) -> None:
        await self._send_to_owners_local(set(self._connections), payload)

    async def _close_owners_local(self, owner_ids: set[int], payload: dict[str, Any] | None = None) -> None:
        for owner_id in owner_ids:
            for websocket in list(self._connections.get(owner_id, {})):
                if payload is not None and websocket.application_state == WebSocketState.CONNECTED:
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(websocket.send_json(payload), timeout=WEBSOCKET_SEND_TIMEOUT_SECONDS)
                with contextlib.suppress(Exception):
                    await websocket.close(code=1008)
                await self.disconnect(owner_id, websocket)

    async def _publish(self, action: str, event_type: str, data: Any, owner_ids: set[int] | None = None) -> None:
        if not self._redis:
            return
        message = {
            "origin": self._origin,
            "namespace": self._namespace,
            "action": action,
            "owner_ids": sorted(owner_ids) if owner_ids is not None else None,
            "event_type": event_type,
            "data": jsonable_encoder(data),
        }
        try:
            await self._redis.publish(settings.realtime_redis_channel, json.dumps(message))
        except Exception:
            logger.exception("Redis realtime publish failed")

    async def _listen_for_remote_events(self) -> None:
        if not self._pubsub:
            return
        try:
            async for message in self._pubsub.listen():
                if message.get("type") != "message":
                    continue
                try:
                    event = json.loads(message["data"])
                except (TypeError, json.JSONDecodeError):
                    continue
                if event.get("origin") == self._origin or event.get("namespace") != self._namespace:
                    continue
                payload = {"type": event.get("event_type"), "data": event.get("data")}
                action = event.get("action")
                if action == "broadcast":
                    await self._broadcast_local(payload)
                elif action in {"user", "users"}:
                    owner_ids = {int(owner_id) for owner_id in event.get("owner_ids") or []}
                    await self._send_to_owners_local(owner_ids, payload)
                elif action == "disconnect":
                    owner_ids = {int(owner_id) for owner_id in event.get("owner_ids") or []}
                    await self._close_owners_local(owner_ids, payload)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Redis realtime subscriber stopped")


class ConnectionManager(_QueuedRealtimeManager):
    def __init__(self) -> None:
        super().__init__("user")
        self._presence_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        await super().start()
        if self.redis_enabled() and not self._presence_task:
            await self._ensure_presence_instance()
            self._presence_task = asyncio.create_task(self._refresh_presence_loop())

    async def stop(self) -> None:
        if self._presence_task:
            self._presence_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._presence_task
            self._presence_task = None
        await super().stop()

    async def connect(self, user_id: int, websocket: WebSocket) -> bool:
        was_online = await self.is_online_global(user_id)
        connection_id = await super().connect(user_id, websocket)
        await self._add_presence(user_id, connection_id)
        return not was_online

    async def disconnect(self, user_id: int, websocket: WebSocket) -> bool:
        await super().disconnect(user_id, websocket)
        return not await self.is_online_global(user_id)

    async def _after_connection_removed(self, user_id: int, state: _ConnectionState) -> None:
        await self._remove_presence(user_id, state.connection_id)

    def _presence_key(self, user_id: int) -> str:
        return f"{PRESENCE_USER_KEY_PREFIX}{user_id}"

    async def _ensure_presence_instance(self) -> None:
        if not self.redis_enabled() or not self._redis:
            return
        try:
            instance_id = await self._redis.get(PRESENCE_INSTANCE_KEY)
            if instance_id == self._instance_id:
                return
            lock_token = f"{self._instance_id}:{self._origin}"
            locked = await self._redis.set(PRESENCE_INIT_LOCK_KEY, lock_token, nx=True, ex=15)
            if not locked:
                return
            try:
                instance_id = await self._redis.get(PRESENCE_INSTANCE_KEY)
                if instance_id == self._instance_id:
                    return
                await self._clear_presence_keys()
                await self._redis.set(PRESENCE_INSTANCE_KEY, self._instance_id)
            finally:
                if await self._redis.get(PRESENCE_INIT_LOCK_KEY) == lock_token:
                    await self._redis.delete(PRESENCE_INIT_LOCK_KEY)
        except Exception:
            logger.exception("Redis presence instance initialization failed")

    async def _clear_presence_keys(self) -> None:
        if not self._redis:
            return
        cursor = 0
        while True:
            cursor, keys = await self._redis.scan(cursor=cursor, match=f"{PRESENCE_USER_KEY_PREFIX}*", count=200)
            if keys:
                await self._redis.delete(*keys)
            if cursor == 0:
                break
        await self._redis.delete(PRESENCE_USERS_KEY)

    async def _add_presence(self, user_id: int, connection_id: str) -> None:
        if not self.redis_enabled() or not self._redis:
            return
        try:
            key = self._presence_key(user_id)
            await self._redis.sadd(key, connection_id)
            await self._redis.expire(key, PRESENCE_TTL_SECONDS)
            await self._redis.sadd(PRESENCE_USERS_KEY, user_id)
        except Exception:
            logger.exception("Redis presence add failed")

    async def _remove_presence(self, user_id: int, connection_id: str) -> None:
        if not self.redis_enabled() or not self._redis:
            return
        try:
            key = self._presence_key(user_id)
            await self._redis.srem(key, connection_id)
            if await self._redis.scard(key) == 0:
                await self._redis.srem(PRESENCE_USERS_KEY, user_id)
        except Exception:
            logger.exception("Redis presence remove failed")

    async def _refresh_presence_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(PRESENCE_REFRESH_SECONDS)
                await self._refresh_local_presence()
        except asyncio.CancelledError:
            raise

    async def _refresh_local_presence(self) -> None:
        if not self.redis_enabled() or not self._redis:
            return
        try:
            now = int(time.time())
            for user_id, connections in list(self._connections.items()):
                key = self._presence_key(user_id)
                if not connections:
                    continue
                await self._redis.sadd(key, *(state.connection_id for state in connections.values()))
                await self._redis.expire(key, PRESENCE_TTL_SECONDS)
                await self._redis.sadd(PRESENCE_USERS_KEY, user_id)
            await self._redis.set("kukechat:presence:last_refresh", now, ex=PRESENCE_TTL_SECONDS)
        except Exception:
            logger.exception("Redis presence refresh failed")

    async def online_user_ids_global(self) -> set[int]:
        if not self.redis_enabled() or not self._redis:
            return self.online_user_ids()
        try:
            raw_ids = await self._redis.smembers(PRESENCE_USERS_KEY)
            online_ids: set[int] = set()
            stale_ids: list[int] = []
            for raw_id in raw_ids:
                try:
                    user_id = int(raw_id)
                except (TypeError, ValueError):
                    continue
                if await self._redis.exists(self._presence_key(user_id)):
                    online_ids.add(user_id)
                else:
                    stale_ids.append(user_id)
            if stale_ids:
                await self._redis.srem(PRESENCE_USERS_KEY, *stale_ids)
            return online_ids
        except Exception:
            logger.exception("Redis presence read failed")
            return self.online_user_ids()

    async def online_count_global(self) -> int:
        return len(await self.online_user_ids_global())

    async def is_online_global(self, user_id: int) -> bool:
        if self.is_online(user_id):
            return True
        if not self.redis_enabled() or not self._redis:
            return False
        try:
            if await self._redis.exists(self._presence_key(user_id)):
                return True
            await self._redis.srem(PRESENCE_USERS_KEY, user_id)
            return False
        except Exception:
            logger.exception("Redis presence status read failed")
            return self.is_online(user_id)

    def is_online(self, user_id: int) -> bool:
        return bool(self._connections.get(user_id))

    def online_count(self) -> int:
        return len(self._connections)

    def online_user_ids(self) -> set[int]:
        return set(self._connections)

    async def send_to_user(self, user_id: int, event_type: str, data: Any) -> None:
        if not await self.is_online_global(user_id):
            return
        payload = {"type": event_type, "data": jsonable_encoder(data)}
        await self._send_to_owner_local(user_id, payload)
        await self._publish("user", event_type, data, {user_id})

    async def send_to_users(self, user_ids: list[int] | set[int], event_type: str, data: Any) -> None:
        owner_ids = set(user_ids) & await self.online_user_ids_global()
        if not owner_ids:
            return
        payload = {"type": event_type, "data": jsonable_encoder(data)}
        await self._send_to_owners_local(owner_ids, payload)
        await self._publish("users", event_type, data, owner_ids)

    async def disconnect_user(self, user_id: int, event_type: str, data: Any) -> None:
        payload = {"type": event_type, "data": jsonable_encoder(data)}
        await self._close_owners_local({user_id}, payload)
        await self._publish("disconnect", event_type, data, {user_id})

    async def broadcast(self, event_type: str, data: Any) -> None:
        payload = {"type": event_type, "data": jsonable_encoder(data)}
        await self._broadcast_local(payload)
        await self._publish("broadcast", event_type, data)


class AdminConnectionManager(_QueuedRealtimeManager):
    def __init__(self) -> None:
        super().__init__("admin")

    async def connect(self, admin_id: int, websocket: WebSocket) -> None:
        await super().connect(admin_id, websocket)

    async def disconnect(self, admin_id: int, websocket: WebSocket) -> None:
        await super().disconnect(admin_id, websocket)

    async def broadcast(self, event_type: str, data: Any) -> None:
        payload = {"type": event_type, "data": jsonable_encoder(data)}
        await self._broadcast_local(payload)
        await self._publish("broadcast", event_type, data)


class BotConnectionManager(_QueuedRealtimeManager):
    def __init__(self) -> None:
        super().__init__("bot")
        self._presence_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        await super().start()
        if self.redis_enabled() and not self._presence_task:
            self._presence_task = asyncio.create_task(self._refresh_presence_loop())

    async def stop(self) -> None:
        if self._presence_task:
            self._presence_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._presence_task
            self._presence_task = None
        await super().stop()

    async def connect(self, bot_id: int, websocket: WebSocket) -> None:
        connection_id = await super().connect(bot_id, websocket)
        await self._add_presence(bot_id, connection_id)

    async def disconnect(self, bot_id: int, websocket: WebSocket) -> None:
        await super().disconnect(bot_id, websocket)

    async def _after_connection_removed(self, bot_id: int, state: _ConnectionState) -> None:
        await self._remove_presence(bot_id, state.connection_id)

    def _presence_key(self, bot_id: int) -> str:
        return f"{BOT_PRESENCE_KEY_PREFIX}{bot_id}"

    async def _add_presence(self, bot_id: int, connection_id: str) -> None:
        if not self._redis:
            return
        try:
            await self._redis.sadd(BOT_PRESENCE_USERS_KEY, bot_id)
            await self._redis.sadd(self._presence_key(bot_id), connection_id)
            await self._redis.expire(self._presence_key(bot_id), PRESENCE_TTL_SECONDS)
        except Exception:
            logger.exception("Bot presence add failed")

    async def _remove_presence(self, bot_id: int, connection_id: str) -> None:
        if not self._redis:
            return
        try:
            key = self._presence_key(bot_id)
            await self._redis.srem(key, connection_id)
            if not await self._redis.scard(key):
                await self._redis.delete(key)
                await self._redis.srem(BOT_PRESENCE_USERS_KEY, bot_id)
        except Exception:
            logger.exception("Bot presence remove failed")

    async def _refresh_presence_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(PRESENCE_REFRESH_SECONDS)
                await self._refresh_local_presence()
        except asyncio.CancelledError:
            raise

    async def _refresh_local_presence(self) -> None:
        if not self.redis_enabled() or not self._redis:
            return
        try:
            for bot_id, connections in list(self._connections.items()):
                if not connections:
                    continue
                key = self._presence_key(bot_id)
                await self._redis.sadd(BOT_PRESENCE_USERS_KEY, bot_id)
                await self._redis.sadd(key, *(state.connection_id for state in connections.values()))
                await self._redis.expire(key, PRESENCE_TTL_SECONDS)
        except Exception:
            logger.exception("Bot presence refresh failed")

    async def online_bot_ids_global(self) -> set[int]:
        if not self.redis_enabled() or not self._redis:
            return set(self._connections)
        try:
            raw_ids = await self._redis.smembers(BOT_PRESENCE_USERS_KEY)
            online_ids: set[int] = set()
            stale_ids: list[int] = []
            for raw_id in raw_ids:
                try:
                    bot_id = int(raw_id)
                except (TypeError, ValueError):
                    continue
                if await self._redis.exists(self._presence_key(bot_id)):
                    online_ids.add(bot_id)
                else:
                    stale_ids.append(bot_id)
            if stale_ids:
                await self._redis.srem(BOT_PRESENCE_USERS_KEY, *stale_ids)
            return online_ids
        except Exception:
            logger.exception("Bot presence read failed")
            return set(self._connections)

    async def is_online_global(self, bot_id: int) -> bool:
        if self._connections.get(bot_id):
            return True
        if not self.redis_enabled() or not self._redis:
            return False
        try:
            if await self._redis.exists(self._presence_key(bot_id)):
                return True
            await self._redis.srem(BOT_PRESENCE_USERS_KEY, bot_id)
            return False
        except Exception:
            logger.exception("Bot presence status read failed")
            return bool(self._connections.get(bot_id))

    async def send_to_bot(self, bot_id: int, event_type: str, data: Any) -> None:
        payload = {"type": event_type, "data": jsonable_encoder(data)}
        await self._send_to_owner_local(bot_id, payload)
        await self._publish("user", event_type, data, {bot_id})

    async def send_to_bots(self, bot_ids: list[int] | set[int], event_type: str, data: Any) -> None:
        owner_ids = set(bot_ids)
        if not owner_ids:
            return
        payload = {"type": event_type, "data": jsonable_encoder(data)}
        await self._send_to_owners_local(owner_ids, payload)
        await self._publish("users", event_type, data, owner_ids)


class GameConnectionManager(_QueuedRealtimeManager):
    """游戏模式的匿名订阅通道。

    与其它通道不同，这里的连接按「会话 id」分组而不是按用户 —— 作品里的
    游玩者可能没有登录，只需要收到某个群的消息即可。
    """

    def __init__(self) -> None:
        super().__init__("game")

    async def connect(self, conversation_id: int, websocket: WebSocket) -> str:
        return await super().connect(conversation_id, websocket)

    async def disconnect(self, conversation_id: int, websocket: WebSocket) -> None:
        await super().disconnect(conversation_id, websocket)

    def viewer_count(self, conversation_id: int) -> int:
        return len(self._connections.get(conversation_id, {}))

    async def send_to_conversation(self, conversation_id: int, event_type: str, data: Any) -> None:
        payload = {"type": event_type, "data": jsonable_encoder(data)}
        await self._send_to_owner_local(conversation_id, payload)
        await self._publish("user", event_type, data, {conversation_id})


manager = ConnectionManager()
admin_manager = AdminConnectionManager()
bot_manager = BotConnectionManager()
game_manager = GameConnectionManager()
