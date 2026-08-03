from __future__ import annotations

import time
import gzip
import json
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


_lock = Lock()
_conversation_member_cache: dict[tuple[int, int | None, int, str], CacheEntry] = {}
_user_conversations_cache: dict[int, CacheEntry] = {}
_user_friends_cache: dict[int, CacheEntry] = {}
_online_users_cache: CacheEntry | None = None
_ccw_creation_cache: dict[str, CacheEntry] = {}


def build_json(value: Any) -> CachedJson:
    plain = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return CachedJson(plain=plain, gzip=gzip.compress(plain, compresslevel=1))


def _conversation_member_key(conversation_id: int, limit: int | None = None, offset: int = 0, search: str = "") -> tuple[int, int | None, int, str]:
    return (conversation_id, limit, offset, search.strip().lower())


def get_conversation_members(conversation_id: int, limit: int | None = None, offset: int = 0, search: str = "") -> Any | None:
    now = time.monotonic()
    key = _conversation_member_key(conversation_id, limit, offset, search)
    with _lock:
        entry = _conversation_member_cache.get(key)
        if entry is None:
            return None
        if entry.expires_at <= now:
            _conversation_member_cache.pop(key, None)
            return None
        return entry.value


def set_conversation_members(conversation_id: int, value: Any, ttl_seconds: float = 10.0, limit: int | None = None, offset: int = 0, search: str = "") -> None:
    key = _conversation_member_key(conversation_id, limit, offset, search)
    with _lock:
        _conversation_member_cache[key] = CacheEntry(
            expires_at=time.monotonic() + ttl_seconds,
            value=value,
        )


def get_user_conversations(user_id: int) -> Any | None:
    now = time.monotonic()
    with _lock:
        entry = _user_conversations_cache.get(user_id)
        if entry is None:
            return None
        if entry.expires_at <= now:
            _user_conversations_cache.pop(user_id, None)
            return None
        return entry.value


def set_user_conversations(user_id: int, value: Any, ttl_seconds: float = 5.0) -> None:
    with _lock:
        _user_conversations_cache[user_id] = CacheEntry(
            expires_at=time.monotonic() + ttl_seconds,
            value=value,
        )


def get_user_friends(user_id: int) -> Any | None:
    now = time.monotonic()
    with _lock:
        entry = _user_friends_cache.get(user_id)
        if entry is None:
            return None
        if entry.expires_at <= now:
            _user_friends_cache.pop(user_id, None)
            return None
        return entry.value


def set_user_friends(user_id: int, value: Any, ttl_seconds: float = 10.0) -> None:
    with _lock:
        _user_friends_cache[user_id] = CacheEntry(
            expires_at=time.monotonic() + ttl_seconds,
            value=value,
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
    now = time.monotonic()
    with _lock:
        entry = _ccw_creation_cache.get(oid)
        if entry is None:
            return None
        if entry.expires_at <= now:
            _ccw_creation_cache.pop(oid, None)
            return None
        return entry.value


def set_ccw_creation(oid: str, value: Any, ttl_seconds: float = 1800.0) -> None:
    with _lock:
        _ccw_creation_cache[oid] = CacheEntry(
            expires_at=time.monotonic() + ttl_seconds,
            value=value,
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
