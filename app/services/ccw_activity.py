"""「正在玩 / 正在创作」：油猴脚本在 CCW 页面上上报的活动（docs/ccw-social.md）。

活动变化很快、丢了也无所谓，所以不进数据库：配了 Redis 就存 Redis（跨 worker 共享），没配就存进程内存。
每条活动 3 分钟过期，脚本每分钟心跳一次续期；离开页面时脚本会主动清除，没清掉的也会在 3 分钟后自然消失。

推送节流：作品变了立刻推；同一个作品的心跳最多每 2 分钟推一次，只是为了让好友那边的过期时间往后顺延。

活动由用户自己上报，和 Discord 的「正在玩」一样只代表本人愿意公开的状态，服务端只保证「只能改自己的」。
"""

from __future__ import annotations

import json
import logging
import time
from datetime import UTC, datetime, timedelta

from app.core.config import settings
from app.schemas.ccw_social import ActivityRead, ActivityUpdate

logger = logging.getLogger(__name__)

ACTIVITY_TTL_SECONDS = 180
PUSH_INTERVAL_SECONDS = 120
_KEY_PREFIX = "kukechat:ccw-activity:"

_local: dict[int, tuple[str, float]] = {}
_redis = None


def reset() -> None:
    """测试用：清掉进程内的活动。"""
    _local.clear()


def _client():  # noqa: ANN202
    global _redis
    if _redis is None and settings.redis_url:
        import redis.asyncio as redis_module

        _redis = redis_module.from_url(settings.redis_url, socket_timeout=1, socket_connect_timeout=1, decode_responses=True)
    return _redis


async def _load(user_ids: list[int]) -> dict[int, dict]:
    if not user_ids:
        return {}
    raw: dict[int, str] = {}
    client = _client()
    if client is not None:
        try:
            values = await client.mget([f"{_KEY_PREFIX}{uid}" for uid in user_ids])
            raw = {uid: value for uid, value in zip(user_ids, values) if value}
        except Exception:
            logger.exception("Redis activity read failed")
            client = None
    if client is None:
        now = time.monotonic()
        for uid in user_ids:
            entry = _local.get(uid)
            if entry and entry[1] > now:
                raw[uid] = entry[0]
            elif entry:
                _local.pop(uid, None)
    out: dict[int, dict] = {}
    for uid, value in raw.items():
        try:
            out[uid] = json.loads(value)
        except (TypeError, ValueError):
            continue
    return out


async def _save(user_id: int, data: dict) -> None:
    value = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    client = _client()
    if client is not None:
        try:
            await client.set(f"{_KEY_PREFIX}{user_id}", value, ex=ACTIVITY_TTL_SECONDS)
            return
        except Exception:
            logger.exception("Redis activity write failed")
    _local[user_id] = (value, time.monotonic() + ACTIVITY_TTL_SECONDS)


async def clear_activity(user_id: int) -> bool:
    existed = bool(await _load([user_id]))
    client = _client()
    if client is not None:
        try:
            await client.delete(f"{_KEY_PREFIX}{user_id}")
        except Exception:
            logger.exception("Redis activity delete failed")
    _local.pop(user_id, None)
    return existed


def _to_read(data: dict) -> ActivityRead:
    updated = datetime.fromisoformat(data["updated_at"])
    return ActivityRead(
        kind=data["kind"],
        ccw_oid=data.get("ccw_oid"),
        title=data["title"],
        cover_url=data.get("cover_url"),
        started_at=datetime.fromisoformat(data["started_at"]),
        updated_at=updated,
        expires_at=updated + timedelta(seconds=ACTIVITY_TTL_SECONDS),
    )


async def set_activity(user_id: int, payload: ActivityUpdate, *, now: datetime | None = None) -> tuple[ActivityRead, bool]:
    """保存活动，返回 (活动, 这次要不要推给好友)。调用方负责先检查用户是否允许公开。"""
    now = now or datetime.now(UTC)
    previous = (await _load([user_id])).get(user_id)
    same = bool(previous) and previous.get("kind") == payload.kind and previous.get("ccw_oid") == payload.ccw_oid and (
        payload.ccw_oid is not None or previous.get("title") == payload.title
    )
    changed = not same or previous.get("title") != payload.title or previous.get("cover_url") != payload.cover_url
    pushed_at = datetime.fromisoformat(previous["pushed_at"]) if same and previous.get("pushed_at") else None
    push = changed or pushed_at is None or (now - pushed_at).total_seconds() >= PUSH_INTERVAL_SECONDS
    data = {
        "kind": payload.kind,
        "ccw_oid": payload.ccw_oid,
        "title": payload.title,
        "cover_url": payload.cover_url,
        "started_at": previous["started_at"] if same else now.isoformat(),
        "updated_at": now.isoformat(),
        "pushed_at": now.isoformat() if push else previous["pushed_at"],
    }
    await _save(user_id, data)
    return _to_read(data), push


async def get_activities(user_ids: list[int]) -> dict[int, ActivityRead]:
    """批量读。调用方负责只给允许公开的用户调用（或者自己过滤）。"""
    return {uid: _to_read(data) for uid, data in (await _load(list(dict.fromkeys(user_ids)))).items()}
