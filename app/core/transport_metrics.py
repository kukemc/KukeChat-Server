"""Bounded, privacy-preserving HTTP and realtime transport observations.

HTTP bytes are ASGI body payload lengths outside gzip. WebSocket bytes are text
payloads accepted by the ASGI send call, NOT wire egress or client delivery;
Redis publish bytes are serialized envelope payloads accepted by publish().
Neither paths, headers, frame contents, nor owner identifiers are retained.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from threading import Lock
from typing import Any


# Uvicorn/Gunicorn configure this INFO logger even when root INFO is disabled.
_LOG = logging.getLogger("uvicorn.error")
_INTERVAL_SECONDS = 60
_LATENCY_MS = (10, 50, 100, 300, 1000)
_IMAGE_FOLDERS = frozenset({"avatars", "profile-covers", "messages", "posts", "teamup", "announcements", "stickers"})
_IMAGE_SUFFIXES = frozenset({"jpg", "jpeg", "png", "webp", "gif"})
_AUDIO_SUFFIXES = frozenset({"webm", "ogg", "mp3", "m4a", "aac", "wav"})
# At most 4 methods * 6 status classes * (11 public suffixes + 3 non-media
# route groups) = 336 buckets, even for unbounded attacker-controlled URLs.
_MAX_BUCKETS = 336
_WS_NAMESPACES = frozenset({"user", "admin", "bot", "game"})
# Explicit literal allowlist: never use a frame-supplied event as a dictionary key.
_WS_EVENTS = frozenset({"presence.updated", "message.created", "conversation.updated",
                        "connection.ready", "pong"})
_WS_OPERATIONS = frozenset({"ws_send_accepted", "ws_send_failed", "ws_send_timeout",
                            "ws_queue_overflow", "ws_connection_open", "ws_connection_close",
                            "redis_publish_accepted", "redis_publish_failed"})
_MAX_WS_BUCKETS = len(_WS_NAMESPACES) * (len(_WS_EVENTS) + 1) * len(_WS_OPERATIONS)


def _ws_event(event: object) -> str:
    return event if isinstance(event, str) and event in _WS_EVENTS else "other"


def _ws_namespace(namespace: object) -> str | None:
    return namespace if isinstance(namespace, str) and namespace in _WS_NAMESPACES else None


def _label(path: object, status: int) -> tuple[str, str]:
    # Only explicitly public upload shapes get media-level detail. Unknown or
    # unsuccessful routes never reveal even the submitted suffix or directory.
    if isinstance(path, str) and (200 <= status < 300 or status == 304) and path.startswith("/uploads/"):
        parts = path[len("/uploads/"):].split("/")
        if len(parts) == 2:
            folder, filename = parts
            thumb = False
        elif len(parts) == 3 and parts[1] == "thumbs":
            folder, _, filename = parts
            thumb = True
        else:
            folder, filename, thumb = "", "", False
        suffix = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if filename not in {"", ".", ".."} and suffix in _IMAGE_SUFFIXES and folder in _IMAGE_FOLDERS:
            return "public_media", suffix
        if filename not in {"", ".", ".."} and not thumb and folder == "messages" and suffix in _AUDIO_SUFFIXES:
            return "public_media", suffix
    if isinstance(path, str) and path.startswith("/api/v1/"):
        return "api", "none"
    if path in ("/health", "/health/storage", "/health/realtime"):
        return "health", "none"
    return "other", "none"


def _method(method: object) -> str:
    return method if method in ("GET", "HEAD", "POST") else "OTHER"


def _status(status: int, disconnected: bool) -> str:
    if disconnected or not 100 <= status < 600:
        return "error"
    return f"{status // 100}xx"


def _latency_index(milliseconds: float) -> int:
    for index, bound in enumerate(_LATENCY_MS):
        if milliseconds <= bound:
            return index
    return len(_LATENCY_MS)


class TransportMetrics:
    """One worker's fixed-cardinality counters, flushed once per active window."""

    def __init__(self, interval_seconds: int = _INTERVAL_SECONDS, logger: logging.Logger | None = None):
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        self.interval_seconds = interval_seconds
        self.logger = logger or _LOG
        self._lock = Lock()
        self._started = time.monotonic()
        self._buckets: dict[tuple[str, str, str, str], list[Any]] = {}
        # Exactly four known namespaces, six event classes and eight operations.
        self._ws_buckets: dict[tuple[str, str, str], list[int]] = {}
        self._task: asyncio.Task[None] | None = None

    def record_ws(self, *, namespace: object, event: object, operation: str, payload_bytes: int = 0) -> None:
        """Count one accepted or failed operation; only successful calls supply bytes."""
        group = _ws_namespace(namespace)
        if group is None or operation not in _WS_OPERATIONS:
            return
        key = (group, _ws_event(event), operation)
        with self._lock:
            row = self._ws_buckets.get(key)
            if row is None:
                if len(self._ws_buckets) >= _MAX_WS_BUCKETS:
                    return
                row = [0, 0]
                self._ws_buckets[key] = row
            row[0] += 1
            if operation in ("ws_send_accepted", "redis_publish_accepted"):
                row[1] += payload_bytes

    def record(self, *, method: object, path: object, status: int, request_bytes: int,
               response_bytes: int, header_ms: float | None, final_ms: float,
               disconnected: bool = False) -> None:
        route_group, suffix = _label(path, status)
        key = (_method(method), route_group, _status(status, disconnected), suffix)
        with self._lock:
            row = self._buckets.get(key)
            if row is None:
                # Defensive hard bound even if someone changes the finite labels.
                if len(self._buckets) >= _MAX_BUCKETS:
                    return
                # count, request bytes, response bytes, header hist, final hist
                row = [0, 0, 0, [0] * 6, [0] * 6]
                self._buckets[key] = row
            row[0] += 1
            row[1] += request_bytes
            row[2] += response_bytes
            if header_ms is not None:
                row[3][_latency_index(header_ms)] += 1
            row[4][_latency_index(final_ms)] += 1

    def flush(self) -> None:
        """Atomically swap both sets; emit one bounded summary per active kind."""
        with self._lock:
            buckets, self._buckets = self._buckets, {}
            ws_buckets, self._ws_buckets = self._ws_buckets, {}
            now = time.monotonic()
            elapsed = now - self._started
            self._started = now
        if buckets:
            rows = [
                {"method": method, "route_group": group, "status_class": status,
                 "media_suffix": suffix, "count": values[0], "request_body_bytes": values[1],
                 "response_body_bytes": values[2], "header_ms_buckets": values[3],
                 "final_ms_buckets": values[4]}
                for (method, group, status, suffix), values in sorted(buckets.items())
            ]
            self.logger.info(json.dumps({"event": "http_asgi_transport", "interval_seconds": round(elapsed, 3),
                                         "latency_ms_bounds": _LATENCY_MS, "buckets": rows}, separators=(",", ":")))
        if ws_buckets:
            rows = [
                {"namespace": group, "event_class": event, "operation": operation,
                 "count": values[0], "payload_bytes_accepted": values[1]}
                for (group, event, operation), values in sorted(ws_buckets.items())
            ]
            self.logger.info(json.dumps({"event": "ws_transport", "interval_seconds": round(elapsed, 3),
                                         "buckets": rows}, separators=(",", ":")))

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            task, self._task = self._task, None
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self.flush()

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self.interval_seconds)
            self.flush()


class TransportMetricsMiddleware:
    """Pure ASGI middleware; bypasses WS/lifespan and never buffers messages."""

    def __init__(self, app: Any, metrics: TransportMetrics):
        self.app = app
        self.metrics = metrics

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = time.perf_counter()
        request_bytes = response_bytes = 0
        status = 0
        header_ms: float | None = None
        disconnected = False
        finished = False

        async def counting_receive() -> Any:
            nonlocal request_bytes, disconnected
            message = await receive()
            if message["type"] == "http.request":
                request_bytes += len(message.get("body", b""))
            elif message["type"] == "http.disconnect":
                disconnected = True
            return message

        async def counting_send(message: dict[str, Any]) -> None:
            nonlocal response_bytes, status, header_ms, finished
            await send(message)
            if message["type"] == "http.response.start":
                status = message["status"]
                header_ms = (time.perf_counter() - started) * 1000
            elif message["type"] == "http.response.body":
                response_bytes += len(message.get("body", b""))
                if not message.get("more_body", False):
                    finished = True

        try:
            await self.app(scope, counting_receive, counting_send)
        finally:
            self.metrics.record(method=scope.get("method"), path=scope.get("path"),
                                status=status if finished else 0, request_bytes=request_bytes,
                                response_bytes=response_bytes, header_ms=header_ms,
                                final_ms=(time.perf_counter() - started) * 1000,
                                disconnected=disconnected and not finished)
