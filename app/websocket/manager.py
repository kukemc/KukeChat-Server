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
from app.core.transport_metrics import TransportMetrics, _ws_event


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

# 在线列表允许短暂缓存用于展示；投递必须使用调用方已验证的收件人集合，
# 否则远端 worker 刚上线的用户会被陈旧缓存漏掉。
PRESENCE_CACHE_TTL_SECONDS = 1.0
REDIS_RECONNECT_MAX_SECONDS = 30


class _EventText(str):
    """Already serialized text with a finite event class; no frame re-parsing."""

    def __new__(cls, text: str, event: str) -> _EventText:
        result = super().__new__(cls, text)
        result.event = _ws_event(event)
        return result


@dataclass
class _ConnectionState:
    connection_id: str
    queue: asyncio.Queue[Any]
    task: asyncio.Task[None]
    close_code: int | None = None
    close_payload: dict[str, Any] | None = None


class _QueuedRealtimeManager:
    def __init__(self, namespace: str) -> None:
        self._namespace = namespace
        # No metrics instance or frame work at all when the application switch is off.
        self.transport_metrics: TransportMetrics | None = None
        self._connections: dict[int, dict[WebSocket, _ConnectionState]] = defaultdict(dict)
        self._origin = uuid.uuid4().hex
        self._instance_id = os.environ.get("REALTIME_INSTANCE_ID") or self._origin
        self._redis: Any | None = None
        self._pubsub: Any | None = None
        # Legacy listener is retained only for isolated callers/tests; production
        # owns one subscriber explicitly in main.lifespan instead.
        self._subscriber_task: asyncio.Task[None] | None = None
        self._shared_subscriber: _SharedRealtimeSubscriber | None = None

    async def start(self) -> None:
        if not settings.redis_url or self._redis is not None:
            return
        # A new lifespan may reuse the module-level managers after the previous
        # shared owner stopped; presence initialization precedes shared.start().
        if self._shared_subscriber is not None and self._shared_subscriber._subscriber_task is None:
            self._shared_subscriber = None
        try:
            import redis.asyncio as redis

            self._redis = redis.from_url(settings.redis_url, decode_responses=True)
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
                await self._pubsub.aclose()
            self._pubsub = None
        if self._redis:
            with contextlib.suppress(Exception):
                await self._redis.aclose()
            self._redis = None

    def redis_enabled(self) -> bool:
        if self._redis is None:
            return False
        # Independent manager start/stop must not own or stop another namespace's
        # subscription. In the application, shared listener health gates presence.
        return self._shared_subscriber is None or self._shared_subscriber.redis_enabled()

    async def connect(self, owner_id: int, websocket: WebSocket) -> str:
        await websocket.accept()
        connection_id = f"{self._instance_id}:{self._origin}:{id(websocket)}:{uuid.uuid4().hex}"
        queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=WEBSOCKET_SEND_QUEUE_SIZE)
        task = asyncio.create_task(self._send_loop(owner_id, websocket, queue))
        self._connections[owner_id][websocket] = _ConnectionState(connection_id=connection_id, queue=queue, task=task)
        if self.transport_metrics is not None:
            self.transport_metrics.record_ws(namespace=self._namespace, event="other", operation="ws_connection_open")
        return connection_id

    async def disconnect(self, owner_id: int, websocket: WebSocket) -> None:
        state = self._remove_connection(owner_id, websocket, cancel_task=True)
        if state:
            # wait_for(send_text) can consume a simultaneous task cancellation.
            # Wake the next queue.get too, without increasing the 100-slot limit.
            state.task.cancel()
            if not state.queue.full():
                state.queue.put_nowait(None)
            if state.task is not asyncio.current_task():
                with contextlib.suppress(asyncio.CancelledError):
                    await state.task
            await self._after_connection_removed(owner_id, state)

    async def send_text(self, owner_id: int, websocket: WebSocket, text: str) -> None:
        # Keep ping replies on the same writer task as events and close frames.
        await self._queue_for_websocket(owner_id, websocket, text)

    async def _queue_for_websocket(self, owner_id: int, websocket: WebSocket, payload: Any) -> None:
        state = self._connections.get(owner_id, {}).get(websocket)
        if state is None or state.close_code is not None:
            return
        if state.queue.full():
            if self.transport_metrics is not None:
                self.transport_metrics.record_ws(namespace=self._namespace, event="other", operation="ws_queue_overflow")
            await self.close_connection(owner_id, websocket, code=1013)
        else:
            state.queue.put_nowait(payload)

    async def close_connection(self, owner_id: int, websocket: WebSocket, *, code: int = 1013,
                               payload: dict[str, Any] | None = None) -> None:
        state = self._connections.get(owner_id, {}).get(websocket)
        if state is None or state.close_code is not None:
            return
        state.close_code = code
        state.close_payload = payload
        # Cancel only the pending queue wait / slow send; the writer owns close().
        state.task.cancel()
        if not state.queue.full():
            state.queue.put_nowait(None)

    def connection_count(self) -> int:
        return sum(len(connections) for connections in self._connections.values())

    async def _send_loop(self, owner_id: int, websocket: WebSocket, queue: asyncio.Queue[Any]) -> None:
        close_code: int | None = None
        try:
            while True:
                payload = await queue.get()
                state = self._connections.get(owner_id, {}).get(websocket)
                if state is None or state.close_code is not None:
                    # Cancellation during wait_for(send_text) may be consumed by
                    # the send task; the wakeup still routes through writer close.
                    if state is None:
                        return
                    raise asyncio.CancelledError
                if websocket.application_state != WebSocketState.CONNECTED:
                    return
                if isinstance(payload, str):
                    # Local fanout already supplies one shared serialized str. Avoid
                    # parsing private data or re-serializing the frame to classify it.
                    event = payload.event if isinstance(payload, _EventText) else ("pong" if payload == "pong" else "other")
                    try:
                        await asyncio.wait_for(websocket.send_text(payload), timeout=WEBSOCKET_SEND_TIMEOUT_SECONDS)
                    except asyncio.TimeoutError:
                        if self.transport_metrics is not None:
                            self.transport_metrics.record_ws(namespace=self._namespace, event=event, operation="ws_send_timeout")
                        raise
                    except Exception:
                        if self.transport_metrics is not None:
                            self.transport_metrics.record_ws(namespace=self._namespace, event=event, operation="ws_send_failed")
                        raise
                    if self.transport_metrics is not None:
                        self.transport_metrics.record_ws(namespace=self._namespace, event=event, operation="ws_send_accepted",
                                                         payload_bytes=len(payload.encode("utf-8")))
                else:
                    # Legacy dict/close path: preserve send_json semantics; do not
                    # re-encode solely for metrics or claim unobserved bytes.
                    await asyncio.wait_for(websocket.send_json(payload), timeout=WEBSOCKET_SEND_TIMEOUT_SECONDS)
        except asyncio.CancelledError:
            state = self._connections.get(owner_id, {}).get(websocket)
            if state is None or state.close_code is None:
                raise
            close_code = state.close_code
            if state.close_payload is not None and websocket.application_state == WebSocketState.CONNECTED:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(websocket.send_json(state.close_payload), timeout=WEBSOCKET_SEND_TIMEOUT_SECONDS)
        except Exception:
            close_code = 1013
            logger.warning("WebSocket writer failed for %s", self._namespace)
        finally:
            if close_code is not None and websocket.application_state == WebSocketState.CONNECTED:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(websocket.close(code=close_code), timeout=WEBSOCKET_SEND_TIMEOUT_SECONDS)
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
        if state and self.transport_metrics is not None:
            self.transport_metrics.record_ws(namespace=self._namespace, event="other", operation="ws_connection_close")
        return state

    async def _after_connection_removed(self, owner_id: int, state: _ConnectionState) -> None:
        return None

    async def _send_to_owner_local(self, owner_id: int, payload: dict[str, Any] | str) -> None:
        stale_connections: list[WebSocket] = []
        text: str | None = payload if isinstance(payload, str) else None
        for websocket, state in list(self._connections.get(owner_id, {}).items()):
            if websocket.application_state != WebSocketState.CONNECTED or state.queue.full():
                if state.queue.full() and state.close_code is None and self.transport_metrics is not None:
                    self.transport_metrics.record_ws(namespace=self._namespace, event="other", operation="ws_queue_overflow")
                stale_connections.append(websocket)
                continue
            if state.close_code is None:
                if text is None:
                    # Match Starlette WebSocket.send_json's default text framing exactly.
                    serialized = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
                    text = (_EventText(serialized, payload.get("type")) if self.transport_metrics is not None
                            else serialized)
                state.queue.put_nowait(text)
        for websocket in stale_connections:
            await self.close_connection(owner_id, websocket, code=1013)

    async def _send_to_owners_local(self, owner_ids: set[int], payload: dict[str, Any]) -> None:
        local_ids = owner_ids & self._connections.keys()
        if not local_ids:
            return
        # One immutable text object is reused by every local socket (including broadcasts).
        serialized = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        text = (_EventText(serialized, payload.get("type")) if self.transport_metrics is not None
                else serialized)
        await asyncio.gather(*(self._send_to_owner_local(owner_id, text) for owner_id in local_ids), return_exceptions=True)

    async def _broadcast_local(self, payload: dict[str, Any]) -> None:
        await self._send_to_owners_local(set(self._connections), payload)

    async def _close_owners_local(self, owner_ids: set[int], payload: dict[str, Any] | None = None) -> None:
        for owner_id in owner_ids:
            for websocket in list(self._connections.get(owner_id, {})):
                await self.close_connection(owner_id, websocket, code=1008, payload=payload)

    async def _publish(self, action: str, event_type: str, encoded_data: Any, owner_ids: set[int] | None = None) -> None:
        """把事件转发给其他 worker。

        ``encoded_data`` 必须是已经过 ``jsonable_encoder`` 的结果 —— 调用方为了
        发给本机连接本来就要编一次，这里再编一次等于每条消息把整个负载递归遍历
        两遍，纯属浪费 CPU。
        """
        if not self._redis:
            return
        message = {
            "origin": self._origin,
            "namespace": self._namespace,
            "action": action,
            "owner_ids": sorted(owner_ids) if owner_ids is not None else None,
            "event_type": event_type,
            "data": encoded_data,
        }
        try:
            # Preserve the historical Redis envelope and its exact JSON serialization.
            serialized = json.dumps(message)
            await self._redis.publish(settings.realtime_redis_channel, serialized)
        except Exception:
            if self.transport_metrics is not None:
                self.transport_metrics.record_ws(namespace=self._namespace, event=event_type,
                                                 operation="redis_publish_failed")
            logger.error("Redis realtime publish failed")
        else:
            if self.transport_metrics is not None:
                self.transport_metrics.record_ws(namespace=self._namespace, event=event_type,
                                                 operation="redis_publish_accepted",
                                                 payload_bytes=len(serialized.encode("utf-8")))

    async def _listen_for_remote_events(self) -> None:
        delay = 1
        while self._redis is not None:
            pubsub = None
            try:
                pubsub = self._pubsub or self._redis.pubsub()
                self._pubsub = pubsub
                await pubsub.subscribe(settings.realtime_redis_channel)
                async for message in pubsub.listen():
                    delay = 1
                    if message.get("type") != "message":
                        continue
                    try:
                        event = json.loads(message["data"])
                        if not isinstance(event, dict):
                            continue
                        if event.get("origin") == self._origin or event.get("namespace") != self._namespace:
                            continue
                        payload = {"type": event.get("event_type"), "data": event.get("data")}
                        action = event.get("action")
                        if action == "broadcast":
                            await self._broadcast_local(payload)
                        elif action in {"user", "users", "disconnect"}:
                            owner_ids = {int(owner_id) for owner_id in event.get("owner_ids") or []}
                            if action == "disconnect":
                                await self._close_owners_local(owner_ids, payload)
                            else:
                                await self._send_to_owners_local(owner_ids, payload)
                    except (TypeError, ValueError, KeyError):
                        logger.warning("Ignoring malformed Redis realtime event")
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Redis realtime subscriber lost connection; retrying")
            finally:
                self._pubsub = None
                if pubsub is not None:
                    with contextlib.suppress(Exception):
                        await pubsub.aclose()
            await asyncio.sleep(delay)
            delay = min(delay * 2, REDIS_RECONNECT_MAX_SECONDS)


class _SharedRealtimeSubscriber:
    """One ordered Redis stream for all four namespaces in this worker.

    Managers keep their own publish/presence clients. Only the application
    lifespan owns this subscriber; stopping one manager never stops another.
    """

    _allowed_actions = {
        "user": frozenset({"user", "users", "disconnect", "broadcast"}),
        "admin": frozenset({"broadcast"}),
        "bot": frozenset({"user", "users"}),
        "game": frozenset({"user"}),
    }

    def __init__(self, user: _QueuedRealtimeManager, admin: _QueuedRealtimeManager,
                 bot: _QueuedRealtimeManager, game: _QueuedRealtimeManager) -> None:
        self._managers = {"user": user, "admin": admin, "bot": bot, "game": game}
        self._redis: Any | None = None
        self._pubsub: Any | None = None
        self._subscriber_task: asyncio.Task[None] | None = None

    def redis_enabled(self) -> bool:
        return (self._redis is not None and self._pubsub is not None
                and self._subscriber_task is not None and not self._subscriber_task.done())

    async def start(self) -> None:
        if not settings.redis_url or self._subscriber_task is not None:
            return
        # The managers must already be started. A shared failure disables their
        # presence reads without sacrificing each manager's local delivery.
        for channel in self._managers.values():
            channel._shared_subscriber = self
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
                await self._pubsub.aclose()
            self._pubsub = None
        if self._redis:
            with contextlib.suppress(Exception):
                await self._redis.aclose()
            self._redis = None

    async def _dispatch(self, raw: Any) -> None:
        try:
            event = json.loads(raw)
            if not isinstance(event, dict):
                raise ValueError("envelope must be an object")
            namespace = event.get("namespace")
            # No dynamic manager lookup: unknown/foreign namespaces have no route.
            channel = self._managers.get(namespace) if isinstance(namespace, str) else None
            if channel is None:
                return
            origin = event.get("origin")
            if not isinstance(origin, str) or not origin:
                raise ValueError("missing origin")
            if origin == channel._origin:
                return
            action = event.get("action")
            if action not in self._allowed_actions[namespace]:
                raise ValueError("unauthorized action")
            event_type = event.get("event_type")
            if not isinstance(event_type, str) or not event_type or "data" not in event:
                raise ValueError("invalid payload")
            raw_ids = event.get("owner_ids")
            if action == "broadcast":
                if raw_ids is not None:
                    raise ValueError("broadcast cannot target owners")
                owner_ids = None
            else:
                if not isinstance(raw_ids, list) or not raw_ids:
                    raise ValueError("missing owners")
                owner_ids = set()
                for raw_id in raw_ids:
                    if isinstance(raw_id, bool) or not isinstance(raw_id, (int, str)):
                        raise ValueError("invalid owner")
                    owner_id = int(raw_id)
                    if owner_id <= 0:
                        raise ValueError("invalid owner")
                    owner_ids.add(owner_id)
            payload = {"type": event_type, "data": event["data"]}
            # Await every dispatch before reading the next event, preserving order.
            if action == "broadcast":
                await channel._broadcast_local(payload)
            elif action == "disconnect":
                await channel._close_owners_local(owner_ids, payload)
            else:
                await channel._send_to_owners_local(owner_ids, payload)
        except (TypeError, ValueError, KeyError):
            logger.warning("Ignoring malformed Redis realtime event")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Redis realtime dispatch failed; skipping event")

    async def _listen_for_remote_events(self) -> None:
        delay = 1
        while self._redis is not None:
            pubsub = None
            try:
                pubsub = self._pubsub
                if pubsub is None:
                    pubsub = self._redis.pubsub()
                    self._pubsub = pubsub
                    await pubsub.subscribe(settings.realtime_redis_channel)
                async for message in pubsub.listen():
                    delay = 1
                    if message.get("type") == "message":
                        await self._dispatch(message.get("data"))
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Redis realtime subscriber lost connection; retrying")
            finally:
                self._pubsub = None
                if pubsub is not None:
                    with contextlib.suppress(Exception):
                        await pubsub.aclose()
            await asyncio.sleep(delay)
            delay = min(delay * 2, REDIS_RECONNECT_MAX_SECONDS)


class ConnectionManager(_QueuedRealtimeManager):
    def __init__(self) -> None:
        super().__init__("user")
        self._presence_task: asyncio.Task[None] | None = None
        self._online_ids_cache: tuple[float, frozenset[int]] | None = None

    def _invalidate_online_cache(self) -> None:
        """本进程的连接有变化时立刻作废缓存，别让自己看到自己的陈旧状态。"""
        self._online_ids_cache = None

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
        self._invalidate_online_cache()
        return not was_online

    async def disconnect(self, user_id: int, websocket: WebSocket) -> bool:
        await super().disconnect(user_id, websocket)
        self._invalidate_online_cache()
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
            # 每个本地用户原本要 3 次串行往返，本进程连着几百人时这个心跳自己
            # 就能占满一整轮；合成一个 pipeline 后是一次往返。
            pipe = self._redis.pipeline()
            for user_id, connections in list(self._connections.items()):
                if not connections:
                    continue
                key = self._presence_key(user_id)
                pipe.sadd(key, *(state.connection_id for state in connections.values()))
                pipe.expire(key, PRESENCE_TTL_SECONDS)
                pipe.sadd(PRESENCE_USERS_KEY, user_id)
            pipe.set("kukechat:presence:last_refresh", now, ex=PRESENCE_TTL_SECONDS)
            await pipe.execute()
        except Exception:
            logger.exception("Redis presence refresh failed")

    async def online_user_ids_global(self, *, fresh: bool = False) -> set[int]:
        if not self.redis_enabled() or not self._redis:
            return self.online_user_ids()

        cached = self._online_ids_cache
        if not fresh and cached is not None and time.monotonic() - cached[0] < PRESENCE_CACHE_TTL_SECONDS:
            return set(cached[1])

        try:
            raw_ids = await self._redis.smembers(PRESENCE_USERS_KEY)
            user_ids: list[int] = []
            for raw_id in raw_ids:
                try:
                    user_ids.append(int(raw_id))
                except (TypeError, ValueError):
                    continue

            online_ids: set[int] = set()
            stale_ids: list[int] = []
            if user_ids:
                # 原来是逐个 await EXISTS —— 在线 N 人就是 N 次串行往返，
                # 而这条路径每发一条消息都要走。pipeline 把它压成一次往返。
                pipe = self._redis.pipeline()
                for user_id in user_ids:
                    pipe.exists(self._presence_key(user_id))
                alive_flags = await pipe.execute()
                for user_id, alive in zip(user_ids, alive_flags):
                    if alive:
                        online_ids.add(user_id)
                    else:
                        stale_ids.append(user_id)

            if stale_ids:
                await self._redis.srem(PRESENCE_USERS_KEY, *stale_ids)
            self._online_ids_cache = (time.monotonic(), frozenset(online_ids))
            return online_ids
        except Exception:
            logger.exception("Redis presence read failed")
            return self.online_user_ids()

    async def online_user_ids_among(self, user_ids: list[int]) -> set[int]:
        """Check only authorized candidate IDs; Redis failures preserve local delivery."""
        local_ids = self.online_user_ids() & set(user_ids)
        if not user_ids or not self.redis_enabled() or not self._redis:
            return local_ids
        try:
            remote_ids = [user_id for user_id in user_ids if user_id not in local_ids]
            if not remote_ids:
                return local_ids
            pipe = self._redis.pipeline()
            for user_id in remote_ids:
                pipe.exists(self._presence_key(user_id))
            alive_flags = await pipe.execute()
            return local_ids | {
                user_id for user_id, alive in zip(remote_ids, alive_flags) if alive
            }
        except Exception:
            logger.exception("Redis targeted presence read failed")
            return local_ids

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
        # Publish for authorized recipients even when presence reads are stale/unavailable.
        payload = {"type": event_type, "data": jsonable_encoder(data)}
        await self._send_to_owner_local(user_id, payload)
        await self._publish("user", event_type, payload["data"], {user_id})

    async def send_to_users(self, user_ids: list[int] | set[int], event_type: str, data: Any) -> None:
        # Membership/authorization is decided by the caller. A process-local presence
        # cache cannot be used as a delivery filter for users on another worker.
        owner_ids = set(user_ids)
        if not owner_ids:
            return
        payload = {"type": event_type, "data": jsonable_encoder(data)}
        await self._send_to_owners_local(owner_ids, payload)
        await self._publish("users", event_type, payload["data"], owner_ids)

    async def disconnect_user(self, user_id: int, event_type: str, data: Any) -> None:
        payload = {"type": event_type, "data": jsonable_encoder(data)}
        await self._close_owners_local({user_id}, payload)
        await self._publish("disconnect", event_type, payload["data"], {user_id})

    async def broadcast(self, event_type: str, data: Any) -> None:
        payload = {"type": event_type, "data": jsonable_encoder(data)}
        await self._broadcast_local(payload)
        await self._publish("broadcast", event_type, payload["data"])


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
        await self._publish("broadcast", event_type, payload["data"])


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
        await self._publish("user", event_type, payload["data"], {bot_id})

    async def send_to_bots(self, bot_ids: list[int] | set[int], event_type: str, data: Any) -> None:
        owner_ids = set(bot_ids)
        if not owner_ids:
            return
        payload = {"type": event_type, "data": jsonable_encoder(data)}
        await self._send_to_owners_local(owner_ids, payload)
        await self._publish("users", event_type, payload["data"], owner_ids)


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
        await self._publish("user", event_type, payload["data"], {conversation_id})


manager = ConnectionManager()
admin_manager = AdminConnectionManager()
bot_manager = BotConnectionManager()
game_manager = GameConnectionManager()
shared_realtime_subscriber = _SharedRealtimeSubscriber(manager, admin_manager, bot_manager, game_manager)
