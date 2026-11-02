"""按键计数的固定窗口限速。配了 Redis 就跨 worker 共享；没配或 Redis 出错时退回进程内计数。"""

from __future__ import annotations

import logging
import math
import time

from fastapi import HTTPException, status

from app.core.config import settings

logger = logging.getLogger(__name__)

_MAX_KEYS = 8192
_local: dict[str, tuple[int, float]] = {}
_redis = None


def reset() -> None:
    """测试用。"""
    _local.clear()


def _client():  # noqa: ANN202
    global _redis
    if _redis is None and settings.redis_url:
        import redis.asyncio as redis_module

        _redis = redis_module.from_url(settings.redis_url, socket_timeout=1, socket_connect_timeout=1)
    return _redis


def _count_local(key: str, window_seconds: int) -> tuple[int, int]:
    now = time.monotonic()
    count, until = _local.get(key, (0, 0.0))
    if until <= now:
        if len(_local) >= _MAX_KEYS:
            for stale in [k for k, (_, end) in _local.items() if end <= now]:
                _local.pop(stale, None)
            while len(_local) >= _MAX_KEYS:
                _local.pop(next(iter(_local)))
        count, until = 0, now + window_seconds
    count += 1
    _local[key] = (count, until)
    return count, max(1, math.ceil(until - now))


async def _count(key: str, window_seconds: int) -> tuple[int, int]:
    client = _client()
    if client is None:
        return _count_local(key, window_seconds)
    name = f"rl:{key}"
    try:
        count = int(await client.incr(name))
        if count == 1:
            await client.expire(name, window_seconds)
        ttl = int(await client.ttl(name))
        if ttl < 0:
            await client.expire(name, window_seconds)
            ttl = window_seconds
        return count, max(1, ttl)
    except Exception:
        logger.warning("rate limit redis failed, falling back to local counter", exc_info=True)
        return _count_local(key, window_seconds)


async def hit(key: str, limit: int, window_seconds: int) -> None:
    """记一次，超过上限抛 429。"""
    count, retry_after = await _count(key, window_seconds)
    if count > limit:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"code": "rate_limited", "message": "操作太频繁，请稍后再试", "retry_after": retry_after},
            headers={"Retry-After": str(retry_after)},
        )
