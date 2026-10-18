"""设备封禁、注册限制、「封禁此人」（docs/device-bans.md 第 4–6 节）。

热路径只有 find_ban：每个进程把生效中的封禁整张读进内存（通常几十到几百条），CACHE_SECONDS 秒重读一次，
请求上只做几次字典查找。本进程改了封禁立刻清缓存；另一个 worker 最多晚 CACHE_SECONDS 秒看到。

管理员（platform_role = ADMIN）永远不被设备封禁拦，免得把后台锁在外面；机器人账号也不参与。
"""

from __future__ import annotations

import ipaddress
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.core.device import KINDS, DeviceContext, mask, normalize_ip
from app.models.device import DeviceBan, DeviceSignal
from app.models.enums import PlatformRole, UserStatus
from app.models.system import SystemSetting
from app.models.user import User
from app.services import device_signals
from app.services.reports import log_admin_action

logger = logging.getLogger(__name__)
UTC = timezone.utc

CACHE_SECONDS = 30.0
SCOPES = ("all", "register")
# IP 封禁必须有期限，默认 7 天，最长 30 天
IP_DEFAULT_HOURS = 24 * 7
IP_MAX_HOURS = 24 * 30
MAX_HOURS = 24 * 3650
# 「封禁此人」带上 IP 时，只带最近这么多天用过的、每个账号最多这么多个
PERSON_IP_DAYS = 14
PERSON_IP_PER_ACCOUNT = 5
# 新封禁生效时，最近这么多天用过这些信号的账号会被断开实时连接
DISCONNECT_SINCE_DAYS = 30
KIND_LABELS = {"device": "设备令牌", "machine": "机器码", "fp": "浏览器指纹", "ip": "IP"}

# —— 设置：存在 system_settings 表里，后台可以随时改 ——
DEFAULTS: dict[str, Any] = {
    # 每个设备令牌 / 机器码最多注册几个账号（0 = 不限）。站长能接受一两个小号，所以默认 3
    "max_accounts_per_device": 3,
    # 同一个浏览器指纹最多注册几个账号。同型号手机、同一批机房电脑的指纹可能一样，所以放宽一些
    "max_accounts_per_fingerprint": 5,
    # 同一个 IP 网段（/24、/64）最近 24 小时最多注册几个账号
    "ip_registrations_per_day": 5,
    # 注册必须带设备令牌（老版本客户端不带）。等新客户端普及之后再打开
    "require_device_for_register": False,
    # 账号被封时，把它的设备令牌和机器码一起封（封禁不短于 propagate_min_ban_days 天才带；永久封禁一定带）
    "propagate_enabled": False,
    "propagate_min_ban_days": 30,
}
BOUNDS: dict[str, tuple[int, int]] = {
    "max_accounts_per_device": (0, 100),
    "max_accounts_per_fingerprint": (0, 100),
    "ip_registrations_per_day": (0, 1000),
    "propagate_min_ban_days": (0, 3650),
}
_KEY_PREFIX = "device_ban."
SETTINGS_CACHE_SECONDS = 5.0

_settings_cache: dict[str, Any] = {"at": 0.0, "values": None}


@dataclass(frozen=True)
class BanEntry:
    id: int
    kind: str
    value: str
    scope: str
    reason: str | None
    expires_at: datetime | None

    def active(self, now: datetime) -> bool:
        return self.expires_at is None or self.expires_at > now


# (kind, value) -> [BanEntry]
_ban_cache: dict[str, Any] = {"at": 0.0, "index": None}


def reset_cache() -> None:
    _ban_cache["at"] = 0.0
    _ban_cache["index"] = None
    _settings_cache["at"] = 0.0
    _settings_cache["values"] = None


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _error(status_code: int, code: str, message: str, **extra: Any) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message, **extra})


def _invalid(message: str, field: str | None = None) -> HTTPException:
    extra = {"field": field} if field else {}
    return _error(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_device_ban", message, **extra)


# ---------------------------------------------------------------- 设置


def _parse_setting(name: str, raw: str | None) -> Any:
    if isinstance(DEFAULTS[name], bool):
        return raw == "1"
    try:
        value = int(raw) if raw is not None else DEFAULTS[name]
    except ValueError:
        return DEFAULTS[name]
    low, high = BOUNDS[name]
    return value if low <= value <= high else DEFAULTS[name]


async def get_settings(db: AsyncSession, *, fresh: bool = False) -> dict[str, Any]:
    now = time.monotonic()
    cached = _settings_cache["values"]
    if not fresh and cached is not None and now - _settings_cache["at"] < SETTINGS_CACHE_SECONDS:
        return dict(cached)
    rows = await db.execute(select(SystemSetting.key, SystemSetting.value).where(SystemSetting.key.like(f"{_KEY_PREFIX}%")))
    stored = {key[len(_KEY_PREFIX) :]: value for key, value in rows.all()}
    values = {name: _parse_setting(name, stored[name]) if name in stored else DEFAULTS[name] for name in DEFAULTS}
    _settings_cache["at"] = now
    _settings_cache["values"] = values
    return dict(values)


async def admin_update_settings(db: AsyncSession, admin: User, changes: dict[str, Any]) -> dict[str, Any]:
    """只处理传了的项，有变化才写审计日志。返回改完之后的全部设置。"""
    before = await get_settings(db, fresh=True)
    cleaned: dict[str, Any] = {}
    for name, value in changes.items():
        if name not in DEFAULTS or value is None:
            continue
        if isinstance(DEFAULTS[name], bool):
            if not isinstance(value, bool):
                raise _error(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_settings", f"{name} 只能是 true / false", field=name)
            cleaned[name] = value
            continue
        low, high = BOUNDS[name]
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise _error(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_settings", f"{name} 要在 {low}–{high} 之间", field=name)
        cleaned[name] = value
    changed = {name: value for name, value in cleaned.items() if value != before[name]}
    for name, value in changed.items():
        text = ("1" if value else "0") if isinstance(value, bool) else str(value)
        item = await db.get(SystemSetting, f"{_KEY_PREFIX}{name}")
        if item is None:
            db.add(SystemSetting(key=f"{_KEY_PREFIX}{name}", value=text, updated_by_id=admin.id))
        else:
            item.value = text
            item.updated_by_id = admin.id
    if changed:
        await log_admin_action(
            db, admin.id, "device_ban.settings.update", "system_setting", None, {name: {"old": before[name], "new": value} for name, value in changed.items()}
        )
        await db.commit()
    reset_cache()
    return await get_settings(db, fresh=True)


# ---------------------------------------------------------------- 封禁的查找（热路径）


async def _index(db: AsyncSession) -> dict[tuple[str, str], list[BanEntry]]:
    now_mono = time.monotonic()
    cached = _ban_cache["index"]
    if cached is not None and now_mono - _ban_cache["at"] < CACHE_SECONDS:
        return cached
    now = _now()
    rows = await db.execute(
        select(DeviceBan.id, DeviceBan.kind, DeviceBan.value_hash, DeviceBan.scope, DeviceBan.reason, DeviceBan.expires_at).where(
            DeviceBan.lifted_at.is_(None), or_(DeviceBan.expires_at.is_(None), DeviceBan.expires_at > now)
        )
    )
    index: dict[tuple[str, str], list[BanEntry]] = {}
    for ban_id, kind, value, scope, reason, expires_at in rows.all():
        index.setdefault((kind, value), []).append(BanEntry(ban_id, kind, value, scope, reason, _aware(expires_at)))
    _ban_cache["index"] = index
    _ban_cache["at"] = now_mono
    return index


def _candidates(context: DeviceContext) -> list[tuple[str, str]]:
    keys = list(context.hashes.items())
    if context.ip:
        keys.append(("ip", context.ip))
    if context.prefix:
        keys.append(("ip", context.prefix))
    return keys


async def find_ban(db: AsyncSession, context: DeviceContext | None, *, registering: bool = False) -> BanEntry | None:
    """这次请求带的信号命中的封禁（取到期最晚的那条）。registering=False 时只看 scope=all 的。"""
    if context is None:
        return None
    index = await _index(db)
    if not index:
        return None
    now = _now()
    hits = [
        entry
        for key in _candidates(context)
        for entry in index.get(key, ())
        if entry.active(now) and (registering or entry.scope == "all")
    ]
    if not hits:
        return None
    return max(hits, key=lambda entry: entry.expires_at or datetime.max.replace(tzinfo=UTC))


def ban_detail(entry: BanEntry) -> dict[str, Any]:
    return {
        "code": "device_banned",
        "message": "这台设备（或当前网络）已被限制使用 KukeChat",
        "reason": entry.reason,
        "expires_at": entry.expires_at.isoformat() if entry.expires_at else None,
        "permanent": entry.expires_at is None,
    }


def _exempt(user: User | None) -> bool:
    return user is not None and (user.platform_role == PlatformRole.ADMIN or user.is_bot)


async def guard(db: AsyncSession, context: DeviceContext | None, user: User | None = None, *, registering: bool = False) -> None:
    """命中封禁就抛 403 device_banned。"""
    if _exempt(user):
        return
    entry = await find_ban(db, context, registering=registering)
    if entry is not None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=ban_detail(entry))


async def guard_request(db: AsyncSession, context: DeviceContext | None, user: User) -> None:
    """已登录请求：先拦封禁，再在内存里记一笔信号（不写库）。"""
    if user.is_bot:
        return
    await guard(db, context, user)
    device_signals.note(user.id, context)


# ---------------------------------------------------------------- 注册与登录


async def _registered_count(db: AsyncSession, kind: str, value: str) -> int:
    count = await db.scalar(
        select(func.count(func.distinct(DeviceSignal.user_id))).where(
            DeviceSignal.kind == kind, DeviceSignal.value_hash == value, DeviceSignal.registered_here.is_(True)
        )
    )
    return int(count or 0)


async def check_registration(db: AsyncSession, context: DeviceContext | None) -> None:
    """注册前的检查：信号被封 → 403 device_banned；要求设备令牌却没带 → 403 device_required；
    这台设备注册的号太多 → 403 too_many_accounts_on_device；同一网段今天注册太多 → 429 registration_ip_throttled。"""
    await guard(db, context, registering=True)
    if context is None:
        return
    config = await get_settings(db)
    if config["require_device_for_register"] and not context.has_device:
        raise _error(status.HTTP_403_FORBIDDEN, "device_required", "请更新到最新版 KukeChat 后再注册")
    too_many = "这台设备上注册的账号已经够多了，请用已有的账号登录"
    device_limit = config["max_accounts_per_device"]
    if device_limit:
        for kind in ("device", "machine"):
            value = context.hashes.get(kind)
            if value and await _registered_count(db, kind, value) >= device_limit:
                raise _error(status.HTTP_403_FORBIDDEN, "too_many_accounts_on_device", too_many, limit=device_limit)
    fp_limit = config["max_accounts_per_fingerprint"]
    fingerprint = context.hashes.get("fp")
    if fp_limit and fingerprint and await _registered_count(db, "fp", fingerprint) >= fp_limit:
        raise _error(status.HTTP_403_FORBIDDEN, "too_many_accounts_on_device", too_many, limit=fp_limit)
    ip_limit = config["ip_registrations_per_day"]
    if ip_limit and context.prefix:
        count = await db.scalar(
            select(func.count(func.distinct(DeviceSignal.user_id))).where(
                DeviceSignal.kind == "ip",
                DeviceSignal.prefix == context.prefix,
                DeviceSignal.registered_here.is_(True),
                DeviceSignal.first_seen_at >= _now() - timedelta(days=1),
            )
        )
        if int(count or 0) >= ip_limit:
            raise _error(status.HTTP_429_TOO_MANY_REQUESTS, "registration_ip_throttled", "当前网络今天注册的账号太多了，请明天再试")


def _ban_qualifies(user: User, min_days: int, now: datetime) -> bool:
    """账号的封禁够不够格带上设备：永久的一定够；有期限的看总时长。"""
    if user.status != UserStatus.DISABLED:
        return False
    until = _aware(user.banned_until)
    if until is None:
        return True
    if until <= now:
        return False
    start = _aware(user.banned_at) or now
    return until - start >= timedelta(days=min_days)


async def on_login(db: AsyncSession, user: User, context: DeviceContext | None) -> None:
    """密码验证通过之后、判断账号是否被封之前调用：记下这次用的信号并提交。
    账号正在被封、且开了自动带上设备时，把这台设备也封上（被封的号换台设备来登录，新设备也就封了）。"""
    if context is None or user.is_bot:
        return
    await device_signals.record_now(db, user.id, context)
    if user.status == UserStatus.DISABLED and user.platform_role != PlatformRole.ADMIN:
        await propagate_account_ban(db, user)
    await db.commit()


# ---------------------------------------------------------------- 自动带上（账号被封 → 设备）


async def _active_ban_keys(db: AsyncSession, keys: list[tuple[str, str]]) -> set[tuple[str, str]]:
    if not keys:
        return set()
    now = _now()
    rows = await db.execute(
        select(DeviceBan.kind, DeviceBan.value_hash).where(
            or_(*[(DeviceBan.kind == kind) & (DeviceBan.value_hash == value) for kind, value in keys]),
            DeviceBan.lifted_at.is_(None),
            or_(DeviceBan.expires_at.is_(None), DeviceBan.expires_at > now),
        )
    )
    return {(kind, value) for kind, value in rows.all()}


async def propagate_account_ban(db: AsyncSession, user: User) -> list[DeviceBan]:
    """设置打开时，把这个被封账号最近 90 天用过的设备令牌、机器码一起封，到期时间和账号的封禁一致。不提交。"""
    config = await get_settings(db)
    now = _now()
    if not config["propagate_enabled"] or user.is_bot or not _ban_qualifies(user, config["propagate_min_ban_days"], now):
        return []
    rows = await db.execute(
        select(DeviceSignal.kind, DeviceSignal.value_hash).where(
            DeviceSignal.user_id == user.id,
            DeviceSignal.kind.in_(("device", "machine")),
            DeviceSignal.last_seen_at >= now - timedelta(days=90),
        )
    )
    keys = list(dict.fromkeys(rows.all()))
    if not keys:
        return []
    existing = await _active_ban_keys(db, keys)
    created = []
    for kind, value in keys:
        if (kind, value) in existing:
            continue
        ban = DeviceBan(
            kind=kind,
            value_hash=value,
            scope="all",
            reason=user.ban_reason,
            source="propagated",
            source_user_id=user.id,
            created_at=now,
            expires_at=_aware(user.banned_until),
        )
        db.add(ban)
        created.append(ban)
    if created:
        await db.flush()
        _pending_disconnect(db).extend(created)
        reset_cache()
    return created


async def lift_propagated(db: AsyncSession, user_id: int, actor_id: int | None) -> int:
    """账号解封时，把当初自动带上的设备封禁一起解除。不提交。"""
    now = _now()
    rows = await db.execute(
        select(DeviceBan).where(DeviceBan.source == "propagated", DeviceBan.source_user_id == user_id, DeviceBan.lifted_at.is_(None))
    )
    items = list(rows.scalars())
    for ban in items:
        ban.lifted_at = now
        ban.lifted_by_id = actor_id
        ban.lift_note = "账号已解封"
    if items:
        reset_cache()
    return len(items)


def _pending_disconnect(db: AsyncSession) -> list[DeviceBan]:
    return db.info.setdefault("device_bans_created", [])


async def flush_disconnects(db: AsyncSession) -> None:
    """事务提交后调用：把最近用过新封信号的在线用户断开，客户端会收到 device.banned。"""
    bans: list[DeviceBan] = db.info.pop("device_bans_created", [])
    bans = [ban for ban in bans if ban.scope == "all"]
    if not bans:
        return
    from app.websocket.manager import manager

    try:
        since = _now() - timedelta(days=DISCONNECT_SINCE_DAYS)
        user_ids = await device_signals.users_on_signals(db, [(ban.kind, ban.value_hash) for ban in bans], since=since)
        if not user_ids:
            return
        exempt = await db.execute(select(User.id).where(User.id.in_(user_ids), or_(User.platform_role == PlatformRole.ADMIN, User.is_bot.is_(True))))
        user_ids -= {user_id for (user_id,) in exempt.all()}
        newest = max(bans, key=lambda ban: _aware(ban.expires_at) or datetime.max.replace(tzinfo=UTC))
        entry = BanEntry(newest.id, newest.kind, newest.value_hash, newest.scope, newest.reason, _aware(newest.expires_at))
        for user_id in user_ids:
            await manager.disconnect_user(user_id, "device.banned", ban_detail(entry))
    except Exception:
        logger.exception("device bans: failed to disconnect users")


# ---------------------------------------------------------------- 后台：读


def _admin_brief(user: User | None) -> dict[str, Any] | None:
    if user is None:
        return None
    return {"id": user.id, "username": user.username, "nickname": user.nickname}


def ban_status(ban: DeviceBan, now: datetime | None = None) -> str:
    now = now or _now()
    if ban.lifted_at is not None:
        return "lifted"
    expires_at = _aware(ban.expires_at)
    if expires_at is not None and expires_at <= now:
        return "expired"
    return "active"


async def ban_reads(db: AsyncSession, bans: list[DeviceBan]) -> list[dict[str, Any]]:
    counts = await device_signals.shared_account_counts(db, list(dict.fromkeys((ban.kind, ban.value_hash) for ban in bans)))
    now = _now()
    return [
        {
            "id": ban.id,
            "kind": ban.kind,
            "masked": mask(ban.kind, ban.value_hash),
            "value": ban.value_hash if ban.kind == "ip" else None,
            "scope": ban.scope,
            "reason": ban.reason,
            "status": ban_status(ban, now),
            "source": ban.source,
            "created_at": ban.created_at,
            "created_by": _admin_brief(ban.created_by),
            "expires_at": ban.expires_at,
            "permanent": ban.expires_at is None,
            "lifted_at": ban.lifted_at,
            "lifted_by": _admin_brief(ban.lifted_by),
            "source_user": _admin_brief(ban.source_user),
            "affected_accounts": counts.get((ban.kind, ban.value_hash), 0),
        }
        for ban in bans
    ]


def _ban_options():  # noqa: ANN202
    return (joinedload(DeviceBan.created_by), joinedload(DeviceBan.lifted_by), joinedload(DeviceBan.source_user))


async def _load_bans(db: AsyncSession, ids: list[int]) -> list[DeviceBan]:
    if not ids:
        return []
    rows = await db.execute(select(DeviceBan).options(*_ban_options()).where(DeviceBan.id.in_(ids)).order_by(DeviceBan.id))
    return list(rows.scalars().unique())


async def list_bans(
    db: AsyncSession, *, state: str = "active", kind: str | None = None, q: str | None = None, limit: int = 50, offset: int = 0
) -> dict[str, Any]:
    now = _now()
    query = select(DeviceBan)
    if state == "active":
        query = query.where(DeviceBan.lifted_at.is_(None), or_(DeviceBan.expires_at.is_(None), DeviceBan.expires_at > now))
    elif state == "expired":
        query = query.where(DeviceBan.lifted_at.is_(None), DeviceBan.expires_at.is_not(None), DeviceBan.expires_at <= now)
    elif state == "lifted":
        query = query.where(DeviceBan.lifted_at.is_not(None))
    if kind in KINDS:
        query = query.where(DeviceBan.kind == kind)
    text = (q or "").strip()
    if text:
        digits = text.lstrip("#")
        if digits.isdigit():
            # 账号 id：从它身上封的，或者封的正是它用过的信号
            user_id = int(digits)
            used = select(DeviceSignal.value_hash).where(DeviceSignal.user_id == user_id)
            query = query.where(or_(DeviceBan.source_user_id == user_id, DeviceBan.value_hash.in_(used)))
        else:
            # IP / 网段 / 哈希前缀（哈希和 IPv6 都是小写存的）
            query = query.where(DeviceBan.value_hash.like(f"{text.lower()}%"))
    total = await db.scalar(select(func.count()).select_from(query.subquery()))
    rows = await db.execute(query.options(*_ban_options()).order_by(DeviceBan.created_at.desc(), DeviceBan.id.desc()).limit(limit).offset(offset))
    return {"items": await ban_reads(db, list(rows.scalars().unique())), "total": int(total or 0)}


def linked_user(user: User) -> dict[str, Any]:
    return {
        "id": user.id,
        "username": user.username,
        "nickname": user.nickname,
        "avatar_url": user.avatar_url,
        "status": "disabled" if user.status == UserStatus.DISABLED else "active",
        "is_bot": bool(user.is_bot),
        "created_at": user.created_at,
        "banned_until": user.banned_until,
        "ban_reason": user.ban_reason,
    }


async def _users(db: AsyncSession, ids: list[int]) -> dict[int, User]:
    if not ids:
        return {}
    rows = await db.execute(select(User).where(User.id.in_(ids)))
    return {user.id: user for user in rows.scalars()}


async def _get_user(db: AsyncSession, user_id: int) -> User:
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    return user


async def user_devices(db: AsyncSession, user_id: int) -> dict[str, Any]:
    """GET /admin/users/{id}/devices：这个账号的信号（脱敏）和直接关联的账号。"""
    user = await _get_user(db, user_id)
    signals = await device_signals.user_signals(db, user)
    index = await _index(db)
    now = _now()

    def ban_for(kind: str, value: str, prefix: str | None) -> dict[str, Any] | None:
        keys = [(kind, value)] + ([("ip", prefix)] if kind == "ip" and prefix else [])
        hits = [entry for key in keys for entry in index.get(key, ()) if entry.active(now)]
        if not hits:
            return None
        entry = max(hits, key=lambda item: item.expires_at or datetime.max.replace(tzinfo=UTC))
        return {"id": entry.id, "expires_at": entry.expires_at, "permanent": entry.expires_at is None, "scope": entry.scope}

    rows = [
        {
            "id": row.id,
            "kind": row.kind,
            "masked": mask(row.kind, row.value_hash),
            "ip": row.value_hash if row.kind == "ip" else None,
            "prefix": row.prefix,
            # 没有接入 IP 归属地库，先留空
            "location": None,
            "platform": row.platform,
            "first_seen_at": row.first_seen_at,
            "last_seen_at": row.last_seen_at,
            "seen_count": row.seen_count,
            "registered_here": bool(row.registered_here),
            "shared_accounts": signals.shared.get((row.kind, row.value_hash), 0),
            "ban": ban_for(row.kind, row.value_hash, row.prefix),
        }
        for row in signals.rows
    ]
    links = device_signals.sorted_links(signals.links)
    users = await _users(db, [item.user_id for item in links])
    linked = [
        {"user": linked_user(users[item.user_id]), "strength": item.strength, "score": item.score, "reasons": item.reasons}
        for item in links
        if item.user_id in users
    ]
    return {"user_id": user.id, "signals": rows, "linked": linked}


async def user_cluster(db: AsyncSession, user_id: int) -> dict[str, Any]:
    """GET /admin/users/{id}/cluster：只走强 / 中关联能连到的账号。"""
    user = await _get_user(db, user_id)
    found, truncated = await device_signals.cluster(db, user)
    users = await _users(db, list(found))
    ordered = sorted(found.items(), key=lambda item: (-device_signals.STRENGTH_RANK[item[1][0]], -item[1][1].score, item[0]))
    accounts = [
        {"user": linked_user(users[uid]), "strength": strength, "score": link.score, "reasons": link.reasons}
        for uid, (strength, link) in ordered
        if uid in users
    ]
    return {"root_id": user.id, "accounts": accounts, "truncated": truncated}


# ---------------------------------------------------------------- 后台：写


def _parse_ip_value(value: str) -> str:
    """单个 IP 或网段。网段只接受 IPv4 /24、IPv6 /64（和信号表里记的 prefix 一致）。"""
    text = value.strip()
    if "/" in text:
        try:
            network = ipaddress.ip_network(text, strict=False)
        except ValueError as exc:
            raise _invalid("IP 网段格式不对", "value") from exc
        if (network.version == 4 and network.prefixlen != 24) or (network.version == 6 and network.prefixlen != 64):
            raise _invalid("网段只支持 IPv4 /24 或 IPv6 /64", "value")
        return str(network)
    ip = normalize_ip(text)
    if ip is None:
        raise _invalid("IP 格式不对", "value")
    return ip


def _expiry(kind: str, duration_hours: int | None, permanent: bool, now: datetime) -> datetime | None:
    if kind == "ip":
        if permanent:
            raise _invalid("IP 封禁必须有期限（IP 会被别人重新分到）", "permanent")
        hours = duration_hours or IP_DEFAULT_HOURS
        if not 1 <= hours <= IP_MAX_HOURS:
            raise _invalid(f"IP 封禁期限要在 1–{IP_MAX_HOURS} 小时之间", "duration_hours")
        return now + timedelta(hours=hours)
    if permanent:
        return None
    if duration_hours is None:
        raise _invalid("请选择封禁期限，或者选永久", "duration_hours")
    if not 1 <= duration_hours <= MAX_HOURS:
        raise _invalid(f"封禁期限要在 1–{MAX_HOURS} 小时之间", "duration_hours")
    return now + timedelta(hours=duration_hours)


async def create_ban(db: AsyncSession, admin: User, payload: dict[str, Any]) -> dict[str, Any]:
    """POST /admin/device-bans：按信号行（signal_id）封，或者直接给 IP / 网段 / 完整哈希。"""
    now = _now()
    source_user_id = None
    if payload.get("signal_id") is not None:
        signal = await db.get(DeviceSignal, int(payload["signal_id"]))
        if signal is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Signal not found")
        kind, value, source_user_id = signal.kind, signal.value_hash, signal.user_id
    else:
        kind = payload.get("kind")
        raw = str(payload.get("value") or "").strip()
        if kind not in KINDS:
            raise _invalid("kind 只能是 device / machine / fp / ip", "kind")
        if not raw:
            raise _invalid("要封的值不能为空", "value")
        if kind == "ip":
            value = _parse_ip_value(raw)
        else:
            value = raw.lower()
            if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
                raise _invalid("要给完整的 64 位哈希（或者从账号的设备列表里封）", "value")
    scope = payload.get("scope") or ("register" if kind == "ip" else "all")
    if scope not in SCOPES:
        raise _invalid("scope 只能是 all / register", "scope")
    expires_at = _expiry(kind, payload.get("duration_hours"), bool(payload.get("permanent")), now)
    reason = (payload.get("reason") or "").strip()[:500] or None
    ban = DeviceBan(
        kind=kind,
        value_hash=value,
        scope=scope,
        reason=reason,
        source="admin",
        source_user_id=source_user_id,
        created_by_id=admin.id,
        created_at=now,
        expires_at=expires_at,
    )
    db.add(ban)
    await db.flush()
    await log_admin_action(
        db,
        admin.id,
        "device_ban.create",
        "device_ban",
        ban.id,
        {"kind": kind, "masked": mask(kind, value), "scope": scope, "expires_at": expires_at.isoformat() if expires_at else None, "reason": reason},
    )
    _pending_disconnect(db).append(ban)
    await db.commit()
    reset_cache()
    await flush_disconnects(db)
    return (await ban_reads(db, await _load_bans(db, [ban.id])))[0]


async def lift_ban(db: AsyncSession, admin: User, ban_id: int, note: str | None) -> dict[str, Any]:
    ban = await db.get(DeviceBan, ban_id)
    if ban is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Device ban not found")
    if ban.lifted_at is None:
        ban.lifted_at = _now()
        ban.lifted_by_id = admin.id
        ban.lift_note = (note or "").strip()[:500] or None
        await log_admin_action(db, admin.id, "device_ban.lift", "device_ban", ban.id, {"kind": ban.kind, "masked": mask(ban.kind, ban.value_hash), "note": ban.lift_note})
        await db.commit()
        reset_cache()
    db.expire(ban)
    return (await ban_reads(db, await _load_bans(db, [ban_id])))[0]


# ---------------------------------------------------------------- 封禁此人


async def _person_plan(db: AsyncSession, payload: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[int, User]]:
    """要封的账号和信号。账号第一个是本人。"""
    root = await _get_user(db, int(payload["user_id"]))
    entries: list[tuple[int, str, int, list[dict[str, Any]]]] = [(root.id, "strong", 0, [])]
    if payload.get("include_linked"):
        found, _truncated = await device_signals.cluster(db, root)
        for uid, (strength, link) in found.items():
            entries.append((uid, strength, link.score, link.reasons))
    users = await _users(db, [uid for uid, *_ in entries])
    now = _now()
    hours = None if payload.get("permanent") else payload.get("duration_hours")
    requested_until = None if hours is None else now + timedelta(hours=int(hours))
    accounts = []
    for uid, strength, score, reasons in entries:
        user = users.get(uid)
        if user is None:
            continue
        skip = None
        if user.platform_role == PlatformRole.ADMIN:
            skip = "admin"
        elif user.status == UserStatus.DISABLED:
            current = _aware(user.banned_until)
            if current is None or (requested_until is not None and current >= requested_until):
                skip = "already_banned"
        accounts.append(
            {"user": linked_user(user), "strength": strength, "score": score, "reasons": reasons, "will_ban": skip is None, "skip_reason": skip}
        )

    kinds = ["device", "machine"] + (["fp"] if payload.get("include_fingerprint", True) else [])
    target_ids = [item["user"]["id"] for item in accounts if item["skip_reason"] != "admin"]
    keys: dict[tuple[str, str], int] = {}
    if target_ids:
        rows = await db.execute(
            select(DeviceSignal.kind, DeviceSignal.value_hash, DeviceSignal.user_id)
            .where(DeviceSignal.user_id.in_(target_ids), DeviceSignal.kind.in_(kinds))
            .order_by(DeviceSignal.last_seen_at.desc())
        )
        for kind, value, uid in rows.all():
            keys.setdefault((kind, value), uid)
        if payload.get("include_ip"):
            per_account: dict[int, int] = {}
            rows = await db.execute(
                select(DeviceSignal.value_hash, DeviceSignal.user_id)
                .where(
                    DeviceSignal.user_id.in_(target_ids),
                    DeviceSignal.kind == "ip",
                    DeviceSignal.last_seen_at >= now - timedelta(days=PERSON_IP_DAYS),
                )
                .order_by(DeviceSignal.last_seen_at.desc())
            )
            for value, uid in rows.all():
                if per_account.get(uid, 0) >= PERSON_IP_PER_ACCOUNT:
                    continue
                per_account[uid] = per_account.get(uid, 0) + 1
                keys.setdefault(("ip", value), uid)
    banned = await _active_ban_keys(db, list(keys))
    counts = await device_signals.shared_account_counts(db, list(keys))
    signals = [
        {
            "kind": kind,
            "masked": mask(kind, value),
            "value": value if kind == "ip" else None,
            "accounts": counts.get((kind, value), 0),
            "already_banned": (kind, value) in banned,
            "_value": value,
            "_owner": owner,
        }
        for (kind, value), owner in keys.items()
    ]
    return accounts, signals, users


def _public_signal(item: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in item.items() if not key.startswith("_")}


async def preview_person(db: AsyncSession, payload: dict[str, Any]) -> dict[str, Any]:
    accounts, signals, _users_by_id = await _person_plan(db, payload)
    return {"accounts": accounts, "signals": [_public_signal(item) for item in signals], "ip_duration_hours": IP_DEFAULT_HOURS}


async def ban_person(db: AsyncSession, admin: User, payload: dict[str, Any]) -> dict[str, Any]:
    """POST /admin/device-bans/person：本人（可选连同关联圈）的账号 + 强 / 中信号（可选 IP）一次封掉。"""
    from app.services import penalties as penalty_service

    reason = (payload.get("reason") or "").strip()[:500]
    if not reason:
        raise _invalid("请写封禁理由（会展示给被封的人）", "reason")
    if int(payload["user_id"]) == admin.id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot penalize yourself")
    permanent = bool(payload.get("permanent"))
    duration_hours = payload.get("duration_hours")
    now = _now()
    device_expiry = _expiry("device", duration_hours, permanent, now)
    ip_expiry = _expiry("ip", payload.get("ip_duration_hours") or IP_DEFAULT_HOURS, False, now) if payload.get("include_ip") else None
    accounts, signals, users = await _person_plan(db, payload)

    created: list[DeviceBan] = []
    for item in signals:
        if item["already_banned"]:
            continue
        kind = item["kind"]
        ban = DeviceBan(
            kind=kind,
            value_hash=item["_value"],
            scope="all",
            reason=reason,
            source="person",
            source_user_id=item["_owner"],
            created_by_id=admin.id,
            created_at=now,
            expires_at=ip_expiry if kind == "ip" else device_expiry,
        )
        db.add(ban)
        created.append(ban)
    await db.flush()

    banned_user_ids: list[int] = []
    skipped: list[dict[str, Any]] = []
    for item in accounts:
        uid = item["user"]["id"]
        if not payload.get("ban_accounts", True):
            continue
        if not item["will_ban"]:
            skipped.append({"user_id": uid, "reason": item["skip_reason"]})
            continue
        await penalty_service.record_violation(
            db,
            user_id=uid,
            category="admin_action",
            level=0,
            source="admin",
            actor_id=admin.id,
            reason=reason,
            override=penalty_service.Penalty("ban", None if permanent else int(duration_hours)),
        )
        users[uid].ban_reason = reason
        banned_user_ids.append(uid)

    await log_admin_action(
        db,
        admin.id,
        "device_ban.person",
        "user",
        int(payload["user_id"]),
        {
            "reason": reason,
            "permanent": permanent,
            "duration_hours": duration_hours,
            "include_linked": bool(payload.get("include_linked")),
            "include_fingerprint": bool(payload.get("include_fingerprint", True)),
            "include_ip": bool(payload.get("include_ip")),
            "banned_user_ids": banned_user_ids,
            "device_ban_ids": [ban.id for ban in created],
        },
    )
    _pending_disconnect(db).extend(created)
    await db.commit()
    reset_cache()
    await penalty_service.flush_notifications(db)
    await flush_disconnects(db)
    return {"bans": await ban_reads(db, await _load_bans(db, [ban.id for ban in created])), "banned_user_ids": banned_user_ids, "skipped": skipped}
