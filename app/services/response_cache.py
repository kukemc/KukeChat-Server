from __future__ import annotations

import time
import gzip
import json
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from threading import Lock
from typing import Any


@dataclass
class CacheEntry:
    expires_at: float
    value: Any


@dataclass(frozen=True)
class CachedJson:
    plain: bytes
    gzip: bytes


# 每个缓存的条目上限。
#
# 这些字典原先既无容量上限也无后台清扫：条目只有在「下次读到同一个 key 且已过期」
# 时才被删掉，写进去再没人读的就永久驻留。最危险的是成员缓存 —— 它的 key 里含
# 用户输入的搜索词，key 空间根本没有上界。两个 worker 的堆一度涨到 700 MB，
# 把页缓存挤到只剩 145 MB，而数据库有 500 多 MB，于是读盘、直接回收、卡顿。
#
# 现在按 LRU 淘汰，并在每次写入时顺手清掉少量过期条目（摊还清扫，不额外起任务）。
MAX_CONVERSATION_MEMBER_ENTRIES = 512
MAX_USER_CONVERSATION_ENTRIES = 1024
MAX_USER_FRIEND_ENTRIES = 1024
MAX_CCW_CREATION_ENTRIES = 512
# 每次写入顺带检查的条目数。取小值是为了让写入保持 O(1) 量级。
_SWEEP_PER_WRITE = 8

_lock = Lock()
_conversation_member_cache: OrderedDict[tuple[int, int | None, int, str], CacheEntry] = OrderedDict()
_user_conversations_cache: OrderedDict[int, CacheEntry] = OrderedDict()
_user_friends_cache: OrderedDict[int, CacheEntry] = OrderedDict()
_online_users_cache: CacheEntry | None = None
_ccw_creation_cache: OrderedDict[str, CacheEntry] = OrderedDict()


def _store(cache: OrderedDict, key: Any, entry: CacheEntry, max_entries: int) -> None:
    """写入并维持容量上限；调用方必须已持有 _lock。"""
    # 顺带清掉最老的一小撮过期条目：不设后台任务也能保证过期条目不会永久驻留
    now = time.monotonic()
    for stale_key in [k for k, v in list(cache.items())[:_SWEEP_PER_WRITE] if v.expires_at <= now]:
        cache.pop(stale_key, None)

    cache[key] = entry
    cache.move_to_end(key)
    while len(cache) > max_entries:
        cache.popitem(last=False)


def _fetch(cache: OrderedDict, key: Any) -> Any | None:
    """读取并把命中的条目移到队尾（LRU）；调用方必须已持有 _lock。"""
    entry = cache.get(key)
    if entry is None:
        return None
    if entry.expires_at <= time.monotonic():
        cache.pop(key, None)
        return None
    cache.move_to_end(key)
    return entry.value


def build_json(value: Any) -> CachedJson:
    plain = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return CachedJson(plain=plain, gzip=gzip.compress(plain, compresslevel=1))


def _conversation_member_key(conversation_id: int, limit: int | None = None, offset: int = 0, search: str = "") -> tuple[int, int | None, int, str]:
    return (conversation_id, limit, offset, search.strip().lower())


def get_conversation_members(conversation_id: int, limit: int | None = None, offset: int = 0, search: str = "") -> Any | None:
    key = _conversation_member_key(conversation_id, limit, offset, search)
    with _lock:
        return _fetch(_conversation_member_cache, key)


def set_conversation_members(conversation_id: int, value: Any, ttl_seconds: float = 10.0, limit: int | None = None, offset: int = 0, search: str = "") -> None:
    key = _conversation_member_key(conversation_id, limit, offset, search)
    with _lock:
        _store(
            _conversation_member_cache,
            key,
            CacheEntry(expires_at=time.monotonic() + ttl_seconds, value=value),
            MAX_CONVERSATION_MEMBER_ENTRIES,
        )


def get_user_conversations(user_id: int) -> Any | None:
    with _lock:
        return _fetch(_user_conversations_cache, user_id)


def set_user_conversations(user_id: int, value: Any, ttl_seconds: float = 5.0) -> None:
    with _lock:
        _store(
            _user_conversations_cache,
            user_id,
            CacheEntry(expires_at=time.monotonic() + ttl_seconds, value=value),
            MAX_USER_CONVERSATION_ENTRIES,
        )


def get_user_friends(user_id: int) -> Any | None:
    with _lock:
        return _fetch(_user_friends_cache, user_id)


def set_user_friends(user_id: int, value: Any, ttl_seconds: float = 10.0) -> None:
    with _lock:
        _store(
            _user_friends_cache,
            user_id,
            CacheEntry(expires_at=time.monotonic() + ttl_seconds, value=value),
            MAX_USER_FRIEND_ENTRIES,
        )


def get_online_users() -> Any | None:
    now = time.monotonic()
    with _lock:
        if _online_users_cache is None:
            return None
        if _online_users_cache.expires_at <= now:
            return None
        return _online_users_cache.value


def set_online_users(value: Any, ttl_seconds: float = 3.0) -> None:
    global _online_users_cache
    with _lock:
        _online_users_cache = CacheEntry(
            expires_at=time.monotonic() + ttl_seconds,
            value=value,
        )


def get_ccw_creation(oid: str) -> Any | None:
    with _lock:
        return _fetch(_ccw_creation_cache, oid)


def set_ccw_creation(oid: str, value: Any, ttl_seconds: float = 1800.0) -> None:
    with _lock:
        _store(
            _ccw_creation_cache,
            oid,
            CacheEntry(expires_at=time.monotonic() + ttl_seconds, value=value),
            MAX_CCW_CREATION_ENTRIES,
        )


def invalidate_conversation_members(conversation_id: int | None = None) -> None:
    with _lock:
        if conversation_id is None:
            _conversation_member_cache.clear()
        else:
            for key in [key for key in _conversation_member_cache if key[0] == conversation_id]:
                _conversation_member_cache.pop(key, None)


def invalidate_user_conversations(user_ids: set[int] | list[int] | None = None) -> None:
    with _lock:
        if user_ids is None:
            _user_conversations_cache.clear()
            return
        for user_id in user_ids:
            _user_conversations_cache.pop(user_id, None)


def invalidate_user_friends(user_ids: set[int] | list[int] | None = None) -> None:
    with _lock:
        if user_ids is None:
            _user_friends_cache.clear()
            return
        for user_id in user_ids:
            _user_friends_cache.pop(user_id, None)


def invalidate_online_users() -> None:
    global _online_users_cache
    with _lock:
        _online_users_cache = None


def invalidate_after(conversation_id: int | None, callback: Callable[[], Any]) -> Any:
    result = callback()
    invalidate_conversation_members(conversation_id)
    return result
