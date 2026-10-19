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
    size_bytes: int = 0


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
# 每 worker 单项和分区字节预算（缓存实体字节；Python 对象开销另受条目数限制）。
_MAX_ENTRY_BYTES = 512 * 1024
_CACHE_BYTE_BUDGETS = {
    "members": 8 * 1024 * 1024,
    "conversations": 16 * 1024 * 1024,
    "friends": 4 * 1024 * 1024,
    "creations": 4 * 1024 * 1024,
}

_lock = Lock()
_conversation_member_cache: OrderedDict[tuple[int, int | None, int, str], CacheEntry] = OrderedDict()
_user_conversations_cache: OrderedDict[int, CacheEntry] = OrderedDict()
_user_friends_cache: OrderedDict[int, CacheEntry] = OrderedDict()
_online_users_cache: CacheEntry | None = None
_ccw_creation_cache: OrderedDict[str, CacheEntry] = OrderedDict()
_cache_bytes: dict[int, int] = {}


def _drop(cache: OrderedDict, key: Any) -> None:
    removed = cache.pop(key, None)
    if removed is not None:
        _cache_bytes[id(cache)] = _cache_bytes.get(id(cache), 0) - removed.size_bytes


def _clear(cache: OrderedDict) -> None:
    cache.clear()
    _cache_bytes[id(cache)] = 0


def _value_size(value: Any) -> int:
    if isinstance(value, CachedJson):
        return len(value.plain) + len(value.gzip)
    if isinstance(value, (bytes, str)):
        return len(value)
    # 第三方作品预览等非 JSON 缓存：保守估算，同时拒绝过大条目。
    try:
        return len(json.dumps(value, ensure_ascii=False, default=str).encode("utf-8"))
    except (TypeError, ValueError):
        return _MAX_ENTRY_BYTES + 1


def _store(cache: OrderedDict, key: Any, entry: CacheEntry, max_entries: int, budget: int) -> None:
    """写入并维持数量与字节上限；调用方必须已持有 _lock。"""
    _drop(cache, key)
    if entry.size_bytes > min(budget, _MAX_ENTRY_BYTES) or entry.expires_at <= time.monotonic():
        return
    # 通过访问顺序摊还清扫，不生成所有 key/value 的临时列表。
    now = time.monotonic()
    for stale_key in list(cache)[:_SWEEP_PER_WRITE]:
        if cache[stale_key].expires_at <= now:
            _drop(cache, stale_key)
    cache[key] = entry
    current_bytes = _cache_bytes.get(id(cache), 0) + entry.size_bytes
    while len(cache) > max_entries or current_bytes > budget:
        _, removed = cache.popitem(last=False)
        current_bytes -= removed.size_bytes
    _cache_bytes[id(cache)] = current_bytes


def _fetch(cache: OrderedDict, key: Any) -> Any | None:
    """读取并把命中的条目移到队尾（LRU）；调用方必须已持有 _lock。"""
    entry = cache.get(key)
    if entry is None:
        return None
    if entry.expires_at <= time.monotonic():
        _drop(cache, key)
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
            CacheEntry(expires_at=time.monotonic() + ttl_seconds, value=value, size_bytes=_value_size(value)),
            MAX_CONVERSATION_MEMBER_ENTRIES,
            _CACHE_BYTE_BUDGETS["members"],
        )


def _conversations_key(user_id: int, include_self: bool) -> int:
    """带不带「我的设备」会话（docs/self-chat.md）是两份结果：带的那份存在负数键下，失效时两份一起删。"""
    return -user_id if include_self else user_id


def get_user_conversations(user_id: int, *, include_self: bool = False) -> Any | None:
    with _lock:
        return _fetch(_user_conversations_cache, _conversations_key(user_id, include_self))


def set_user_conversations(user_id: int, value: Any, ttl_seconds: float = 5.0, *, include_self: bool = False) -> None:
    with _lock:
        _store(
            _user_conversations_cache,
            _conversations_key(user_id, include_self),
            CacheEntry(expires_at=time.monotonic() + ttl_seconds, value=value, size_bytes=_value_size(value)),
            MAX_USER_CONVERSATION_ENTRIES,
            _CACHE_BYTE_BUDGETS["conversations"],
        )


def get_user_friends(user_id: int) -> Any | None:
    with _lock:
        return _fetch(_user_friends_cache, user_id)


def set_user_friends(user_id: int, value: Any, ttl_seconds: float = 10.0) -> None:
    with _lock:
        _store(
            _user_friends_cache,
            user_id,
            CacheEntry(expires_at=time.monotonic() + ttl_seconds, value=value, size_bytes=_value_size(value)),
            MAX_USER_FRIEND_ENTRIES,
            _CACHE_BYTE_BUDGETS["friends"],
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
    size = _value_size(value)
    with _lock:
        _online_users_cache = (
            CacheEntry(expires_at=time.monotonic() + ttl_seconds, value=value, size_bytes=size)
            if size <= _MAX_ENTRY_BYTES else None
        )


def get_ccw_creation(oid: str) -> Any | None:
    with _lock:
        return _fetch(_ccw_creation_cache, oid)


def set_ccw_creation(oid: str, value: Any, ttl_seconds: float = 1800.0) -> None:
    with _lock:
        _store(
            _ccw_creation_cache,
            oid,
            CacheEntry(expires_at=time.monotonic() + ttl_seconds, value=value, size_bytes=_value_size(value)),
            MAX_CCW_CREATION_ENTRIES,
            _CACHE_BYTE_BUDGETS["creations"],
        )


def invalidate_conversation_members(conversation_id: int | None = None) -> None:
    with _lock:
        if conversation_id is None:
            _clear(_conversation_member_cache)
        else:
            for key in [key for key in _conversation_member_cache if key[0] == conversation_id]:
                _drop(_conversation_member_cache, key)


def invalidate_user_conversations(user_ids: set[int] | list[int] | None = None) -> None:
    with _lock:
        if user_ids is None:
            _clear(_user_conversations_cache)
            return
        for user_id in user_ids:
            _drop(_user_conversations_cache, user_id)
            _drop(_user_conversations_cache, -user_id)


def invalidate_user_friends(user_ids: set[int] | list[int] | None = None) -> None:
    with _lock:
        if user_ids is None:
            _clear(_user_friends_cache)
            return
        for user_id in user_ids:
            _drop(_user_friends_cache, user_id)


def invalidate_online_users() -> None:
    global _online_users_cache
    with _lock:
        _online_users_cache = None


def invalidate_after(conversation_id: int | None, callback: Callable[[], Any]) -> Any:
    result = callback()
    invalidate_conversation_members(conversation_id)
    return result
