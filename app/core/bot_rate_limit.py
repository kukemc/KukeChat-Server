"""机器人开放接口的频率限制（进程内，每个 worker 各一份）。

两件事，都在查数据库之前做，压力再大也不会占用数据库连接：
  · 令牌桶：每把密钥平均每秒 settings.bot_api_rate_per_second 次、最多连续 settings.bot_api_burst 次，超了回 429 + Retry-After；
  · 认证失败缓存：密钥无效 / 机器人被停用时，同一把密钥 60 秒内直接回同样的错误，不再每次都查库。
密钥只以 sha256 摘要作为键，不在内存里留明文。
"""

from __future__ import annotations

import hashlib
import math
import time
from dataclasses import dataclass

from fastapi import HTTPException, status

from app.core.config import settings

_MAX_KEYS = 4096
_FAILURE_TTL_SECONDS = 60.0


@dataclass
class _Bucket:
    tokens: float
    updated: float


_buckets: dict[str, _Bucket] = {}
_failures: dict[str, tuple[float, int, str]] = {}


def key_digest(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()[:32]


def _prune(now: float) -> None:
    if len(_buckets) > _MAX_KEYS:
        # 丢掉已经攒满（长时间没来）的桶
        full = float(settings.bot_api_burst)
        rate = settings.bot_api_rate_per_second
        for digest in [d for d, b in _buckets.items() if b.tokens + (now - b.updated) * rate >= full]:
            _buckets.pop(digest, None)
        while len(_buckets) > _MAX_KEYS:
            _buckets.pop(next(iter(_buckets)))
    if len(_failures) > _MAX_KEYS:
        for digest in [d for d, (until, _, _) in _failures.items() if until <= now]:
            _failures.pop(digest, None)
        while len(_failures) > _MAX_KEYS:
            _failures.pop(next(iter(_failures)))


def check(digest: str) -> None:
    """进来一个请求：先看最近是否认证失败过，再扣一个令牌。不通过就抛 HTTPException。"""
    now = time.monotonic()
    failure = _failures.get(digest)
    if failure is not None:
        until, code, detail = failure
        if until > now:
            raise HTTPException(status_code=code, detail=detail)
        _failures.pop(digest, None)

    rate = settings.bot_api_rate_per_second
    burst = float(settings.bot_api_burst)
    bucket = _buckets.get(digest)
    if bucket is None:
        _prune(now)
        bucket = _buckets[digest] = _Bucket(tokens=burst, updated=now)
    else:
        bucket.tokens = min(burst, bucket.tokens + (now - bucket.updated) * rate)
        bucket.updated = now
    if bucket.tokens < 1.0:
        retry_after = max(1, math.ceil((1.0 - bucket.tokens) / rate))
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Bot API rate limit exceeded ({rate:g} requests/second, burst {int(burst)}). Slow down.",
            headers={"Retry-After": str(retry_after)},
        )
    bucket.tokens -= 1.0


def remember_failure(digest: str, error: HTTPException) -> None:
    if error.status_code in (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN):
        _prune(time.monotonic())
        _failures[digest] = (time.monotonic() + _FAILURE_TTL_SECONDS, error.status_code, str(error.detail))


def reset() -> None:
    """测试用。"""
    _buckets.clear()
    _failures.clear()
