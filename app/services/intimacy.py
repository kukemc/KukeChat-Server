"""好友亲密度、好友成就、好友亲密榜（docs/intimacy.md）。

几件事：

- 记账只在两个地方发生：好友之间的私聊里发了一条消息（record_message），好友之间送了一次礼物（record_gift）。
  都在原来的操作提交之后由接口层调用，单独一个短事务；这里出错只记日志，不影响消息和礼物本身。
- 每天（UTC+8）每对好友能涨的亲密度有上限（daily_cap），聊天、送礼、连续互动三种来源各自还有自己的上限；
  生日礼物的加成不算进每日上限，但每人每年只有一次。数值都在 system_settings 里，后台能改。
- 聊天要「互相」：只有一方在说话涨不了多少（见 chat_target）。
- 成就（intimacy_catalog.SERIES）由 friend_bonds 上的累计判断，解锁一枚写一行 friend_badges。成为好友的天数这一项
  不靠聊天触发，后台循环每小时扫一遍。
- 写事务的第一条语句就是写（插入 / 更新），从那里起拿着 SQLite 的写锁直到提交，后面的读改写不会和别的进程交错；
  同一个进程里先在 shop._write_lock() 上排队（原因见那里的说明）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable

from fastapi import HTTPException, status
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import localtime
from app.models.conversation import Conversation
from app.models.enums import ConversationType, MessageType, UserStatus
from app.models.friend import Friendship
from app.models.gift import GiftRecord
from app.models.intimacy import FriendBadge, FriendBond, FriendBondDay
from app.models.message import Message
from app.models.system import SystemSetting
from app.models.user import User
from app.schemas.user import UserPublic
from app.services import intimacy_catalog as catalog
from app.services import shop as shop_service
from app.services.reports import log_admin_action

logger = logging.getLogger(__name__)
UTC = timezone.utc

# 深夜 / 清晨按 UTC+8 的小时算：[0, 5) 和 [5, 8)
NIGHT_HOURS = range(0, 5)
DAWN_HOURS = range(5, 8)
# 不算聊天的消息：系统消息；礼物消息另按礼物算
UNCOUNTED_TYPES = (MessageType.SYSTEM, MessageType.GIFT)
HISTORY_MAX_DAYS = 90
BACKFILL_KEY = "intimacy.legacy_backfill"
# 成为好友满 N 天的成就没有别的触发点，后台循环隔这么久扫一遍
FRIEND_DAYS_SWEEP_SECONDS = 3600
CHUNK = 200
EVENT_UPDATED = "intimacy.updated"
EVENT_LEVEL_UP = "intimacy.level.up"
EVENT_BADGES = "intimacy.badge.unlocked"
DISPLAY_AUTO, DISPLAY_NONE = "auto", "none"
# 没揭晓的隐藏成就（彩蛋）连图形也不给，客户端画问号
HIDDEN_ICON = "unknown"

# ---------------------------------------------------------------- 设置

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "daily_cap": 40,
    "chat_mutual": 5,
    "chat_per_round": 1,
    "chat_cap": 25,
    "gift_base": 3,
    "gift_coins_per_point": 10,
    "gift_cap": 15,
    "streak_min": 3,
    "streak_bonus": 3,
    "birthday_bonus": 30,
}
BOUNDS: dict[str, tuple[int, int]] = {
    "daily_cap": (1, 1000),
    "chat_mutual": (0, 100),
    "chat_per_round": (0, 100),
    "chat_cap": (0, 1000),
    "gift_base": (0, 100),
    "gift_coins_per_point": (1, 100000),
    "gift_cap": (0, 1000),
    "streak_min": (1, 365),
    "streak_bonus": (0, 100),
    "birthday_bonus": (0, 1000),
}
_KEY_PREFIX = "intimacy.cfg."
# 另一个 worker 进程改了设置后最多这么久才能看到
CACHE_SECONDS = 5.0
_cache: dict[str, Any] = {"at": 0.0, "values": None}


def reset_runtime_state() -> None:
    """清掉进程内的状态（测试用）。"""
    _cache["at"] = 0.0
    _cache["values"] = None


def _now() -> datetime:
    return localtime._now()


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _local(value: datetime) -> datetime:
    return _aware(value).astimezone(localtime.LOCAL_TZ)


def _error(status_code: int, code: str, message: str, **extra: Any) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message, **extra})


def _parse_setting(name: str, raw: str | None) -> Any:
    if name == "enabled":
        return raw == "1"
    try:
        value = int(raw) if raw is not None else DEFAULTS[name]
    except ValueError:
        return DEFAULTS[name]
    low, high = BOUNDS[name]
    return value if low <= value <= high else DEFAULTS[name]


async def get_settings(db: AsyncSession, *, fresh: bool = False) -> dict[str, Any]:
    now = time.monotonic()
    cached = _cache["values"]
    if not fresh and cached is not None and now - _cache["at"] < CACHE_SECONDS:
        return dict(cached)
    rows = await db.execute(select(SystemSetting.key, SystemSetting.value).where(SystemSetting.key.like(f"{_KEY_PREFIX}%")))
    stored = {key[len(_KEY_PREFIX) :]: value for key, value in rows.all()}
    values = {name: _parse_setting(name, stored[name]) if name in stored else DEFAULTS[name] for name in DEFAULTS}
    _cache["at"] = now
    _cache["values"] = values
    return dict(values)


async def admin_update_settings(db: AsyncSession, admin: User, changes: dict[str, Any]) -> dict[str, Any]:
    """PUT /admin/intimacy/settings：只处理传了的项，有变化才写审计日志。"""
    admin_id = admin.id
    before = await get_settings(db, fresh=True)
    cleaned: dict[str, Any] = {}
    for name, value in changes.items():
        if name not in DEFAULTS or value is None:
            continue
        if name == "enabled":
            if not isinstance(value, bool):
                raise _error(422, "invalid_settings", "enabled 只能是 true / false", field=name)
            cleaned[name] = value
            continue
        low, high = BOUNDS[name]
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise _error(422, "invalid_settings", f"{name} 要在 {low}–{high} 之间", field=name)
        cleaned[name] = value
    changed = {name: value for name, value in cleaned.items() if value != before[name]}
    if changed:
        async with shop_service._write_lock():
            for name, value in changed.items():
                text_value = ("1" if value else "0") if name == "enabled" else str(value)
                item = await db.get(SystemSetting, f"{_KEY_PREFIX}{name}")
                if item is None:
                    db.add(SystemSetting(key=f"{_KEY_PREFIX}{name}", value=text_value, updated_by_id=admin_id))
                else:
                    item.value = text_value
                    item.updated_by_id = admin_id
            await log_admin_action(
                db, admin_id, "intimacy.settings.update", "system_setting", None, {name: {"old": before[name], "new": value} for name, value in changed.items()}
            )
            await db.commit()
    reset_runtime_state()
    return await get_settings(db, fresh=True)


# ---------------------------------------------------------------- 规则（纯函数）


def chat_target(msgs_a: int, msgs_b: int, cfg: dict[str, Any]) -> int:
    """当天聊天这一项应得多少：双方都开口了先给 chat_mutual，之后每「一来一回」（较少那一方的条数）给 chat_per_round，封顶 chat_cap。

    只有一方在说话是 0：刷屏涨不了亲密度。
    """
    rounds = min(msgs_a, msgs_b)
    if rounds <= 0:
        return 0
    return min(cfg["chat_cap"], cfg["chat_mutual"] + cfg["chat_per_round"] * rounds)


def gift_points(amount: int, cfg: dict[str, Any]) -> int:
    """一次送礼应得多少（还要再受当天送礼上限 gift_cap 和每日总上限约束）。"""
    return cfg["gift_base"] + max(0, int(amount)) // cfg["gift_coins_per_point"]


def next_streak(streak: int, last_day: date | None, day: date) -> int:
    """day 这一天第一次「双方都开口」之后的连续天数。"""
    if last_day == day:
        return streak
    if last_day is not None and last_day == day - timedelta(days=1):
        return streak + 1
    return 1


def current_streak(streak: int, last_day: date | None, today: date) -> int:
    """今天看到的连续天数：最近一次互动是今天或昨天才算数（今天还没聊不算断）。"""
    if last_day is None or last_day < today - timedelta(days=1):
        return 0
    return streak


def _room(day: FriendBondDay, cfg: dict[str, Any]) -> int:
    return max(0, cfg["daily_cap"] - (day.chat_points + day.gift_points + day.streak_points))


def _grant(day: FriendBondDay, cfg: dict[str, Any], source: str, want: int) -> int:
    """给 day 的 source 这一项加 want（受每日总上限约束），返回实际加了多少。"""
    gained = max(0, min(want, _room(day, cfg)))
    if gained:
        setattr(day, f"{source}_points", getattr(day, f"{source}_points") + gained)
    return gained


def _side(bond_low: int, user_id: int) -> str:
    return "low" if user_id == bond_low else "high"


def _pair(a: int, b: int) -> tuple[int, int]:
    return (a, b) if a < b else (b, a)


def _friend_days(created_at: datetime | None, today: date) -> int:
    if created_at is None:
        return 0
    return max(0, (today - _local(created_at).date()).days)


def bond_stats(bond: FriendBond, friend_days: int, series_count: int = 0) -> dict[str, int]:
    """成就要看的各项统计（catalog.STATS）。series_count：已经解锁了几个台面上的系列（「集齐」那个彩蛋用）。"""
    return {
        "messages_total": bond.messages_total,
        "mutual_days": bond.mutual_days,
        "best_streak": bond.best_streak,
        "night_days": bond.night_days,
        "dawn_days": bond.dawn_days,
        "best_day_messages": bond.best_day_messages,
        "best_night_messages": bond.best_night_messages,
        "weekend_days": bond.weekend_days,
        "gift_count": bond.gift_count,
        "birthday_gifts": bond.birthday_gifts,
        "intimacy": bond.intimacy,
        "friend_days": friend_days,
        "newyear_days": bond.newyear_days,
        "sunmoon_days": bond.sunmoon_days,
        "series_count": series_count,
    }


def _new_bond(low: int, high: int, now: datetime) -> FriendBond:
    """内存里的一行（回填、还没有 bond 时的详情页用），计数全是 0。"""
    bond = FriendBond(user_low_id=low, user_high_id=high, created_at=now, updated_at=now, last_mutual_day=None)
    for column in FriendBond.__table__.columns:
        if column.server_default is not None:
            setattr(bond, column.key, 0)
    return bond


def _new_day(day: date) -> FriendBondDay:
    row = FriendBondDay(day=day)
    for column in FriendBondDay.__table__.columns:
        if column.server_default is not None:
            setattr(row, column.key, False if column.key in ("mutual", "night_mutual", "dawn_mutual") else 0)
    return row


def _apply_mutual(bond: FriendBond, day: FriendBondDay) -> None:
    """day 上双方的条数变了之后：第一次双方都开口的那一刻把这一天算进各项天数，更新连续天数。"""
    if not day.mutual and day.msgs_low > 0 and day.msgs_high > 0:
        day.mutual = True
        bond.mutual_days += 1
        if day.day.weekday() >= 5:
            bond.weekend_days += 1
        if (day.day.month, day.day.day) == (1, 1):
            bond.newyear_days += 1
        bond.streak_days = next_streak(bond.streak_days, bond.last_mutual_day, day.day)
        bond.last_mutual_day = day.day
        bond.best_streak = max(bond.best_streak, bond.streak_days)
    if not day.night_mutual and day.night_low > 0 and day.night_high > 0:
        day.night_mutual = True
        bond.night_days += 1
        if day.dawn_mutual:
            bond.sunmoon_days += 1
    if not day.dawn_mutual and day.dawn_low > 0 and day.dawn_high > 0:
        day.dawn_mutual = True
        bond.dawn_days += 1
        if day.night_mutual:
            bond.sunmoon_days += 1
    bond.best_day_messages = max(bond.best_day_messages, day.msgs_low + day.msgs_high)
    bond.best_night_messages = max(bond.best_night_messages, day.night_low + day.night_high)


def _chat_and_streak(bond: FriendBond, day: FriendBondDay, cfg: dict[str, Any]) -> int:
    """按 day 现在的条数补记聊天和连续互动两项，返回这次新加的。"""
    gained = _grant(day, cfg, "chat", chat_target(day.msgs_low, day.msgs_high, cfg) - day.chat_points)
    if day.mutual and bond.last_mutual_day == day.day and bond.streak_days >= cfg["streak_min"]:
        gained += _grant(day, cfg, "streak", cfg["streak_bonus"] - day.streak_points)
    return gained


def _gift(bond: FriendBond, day: FriendBondDay, cfg: dict[str, Any], *, to_side: str, amount: int, on_birthday: bool) -> int:
    """记一次送礼，返回这次新加的亲密度（含生日加成）。"""
    day.gifts += 1
    day.gift_amount += amount
    bond.gift_count += 1
    bond.gift_amount += amount
    gained = _grant(day, cfg, "gift", min(gift_points(amount, cfg), cfg["gift_cap"] - day.gift_points))
    year_column = f"birthday_year_{to_side}"
    if on_birthday and getattr(bond, year_column) != day.day.year:
        # 每人每年一次，不算进每日上限
        setattr(bond, year_column, day.day.year)
        bond.birthday_gifts += 1
        bonus = cfg["birthday_bonus"]
        day.birthday_points += bonus
        gained += bonus
    return gained


# ---------------------------------------------------------------- 结果与事件


@dataclass
class Outcome:
    """一次记账的结果。接口层在提交之后交给 announce 发事件。"""

    bond_id: int
    low: int
    high: int
    gained: int = 0
    level_before: int = 0
    level_after: int = 0
    new_badges: list[tuple[str, int]] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.gained or self.new_badges or self.level_after != self.level_before)


def badge_ref(series_key: str, level: int, unlocked_at: datetime | None) -> dict[str, Any] | None:
    series = catalog.SERIES_BY_KEY.get(series_key)
    item = catalog.badge_level(series_key, level)
    if series is None or item is None:
        return None
    return {
        "series": series.key,
        "series_name": series.name,
        "level": item.level,
        "name": item.name,
        "tier": item.tier,
        "icon": series.icon,
        "unlocked_at": _aware(unlocked_at),
    }


def _display_choice(bond: FriendBond, viewer_id: int) -> str:
    raw = bond.display_low if viewer_id == bond.user_low_id else bond.display_high
    return raw or DISPLAY_AUTO


def resolve_display(bond: FriendBond, viewer_id: int, badges: Iterable[FriendBadge]) -> dict[str, Any] | None:
    """viewer 在对方名字旁挂哪一枚。自动 = 等级（tier）最高的，一样高挑最近解锁的；指定系列 = 这一系列已解锁的最高级。"""
    choice = _display_choice(bond, viewer_id)
    if choice == DISPLAY_NONE:
        return None
    best: tuple[Any, ...] | None = None
    best_badge: FriendBadge | None = None
    for badge in badges:
        item = catalog.badge_level(badge.series, badge.level)
        if item is None:
            continue
        if choice != DISPLAY_AUTO and badge.series != choice:
            continue
        key = (badge.level,) if choice != DISPLAY_AUTO else (item.tier, _aware(badge.unlocked_at), badge.level)
        if best is None or key > best:
            best, best_badge = key, badge
    if best_badge is None:
        if choice != DISPLAY_AUTO:
            # 指定的系列被收回了（目录改了）就退回自动
            return resolve_display(_with_choice(bond, viewer_id, None), viewer_id, badges)
        return None
    return badge_ref(best_badge.series, best_badge.level, best_badge.unlocked_at)


def _with_choice(bond: FriendBond, viewer_id: int, choice: str | None) -> FriendBond:
    copy = FriendBond(user_low_id=bond.user_low_id, user_high_id=bond.user_high_id)
    if viewer_id == bond.user_low_id:
        copy.display_low = choice
    else:
        copy.display_high = choice
    return copy


def _brief(user: User) -> dict[str, Any]:
    return {"id": user.id, "display_name": (user.nickname or "").strip() or user.username, "avatar_url": user.avatar_url}


async def _badges_of(db: AsyncSession, bond_ids: Iterable[int]) -> dict[int, list[FriendBadge]]:
    ids = list(bond_ids)
    grouped: dict[int, list[FriendBadge]] = defaultdict(list)
    for start in range(0, len(ids), 400):
        rows = await db.execute(select(FriendBadge).where(FriendBadge.bond_id.in_(ids[start : start + 400])))
        for badge in rows.scalars():
            grouped[badge.bond_id].append(badge)
    return grouped


async def _today_gained(db: AsyncSession, bond_ids: Iterable[int], today: date) -> dict[int, int]:
    ids = list(bond_ids)
    gained: dict[int, int] = {}
    for start in range(0, len(ids), 400):
        rows = await db.execute(
            select(
                FriendBondDay.bond_id,
                FriendBondDay.chat_points + FriendBondDay.gift_points + FriendBondDay.streak_points + FriendBondDay.birthday_points,
            ).where(FriendBondDay.bond_id.in_(ids[start : start + 400]), FriendBondDay.day == today)
        )
        gained.update({bond_id: int(value or 0) for bond_id, value in rows.all()})
    return gained


async def announce(db: AsyncSession, outcome: Outcome | None) -> None:
    """记账提交之后给双方发事件（各自看到的挂件不一样）。只是尽力而为，失败只记日志。"""
    if outcome is None or not outcome.changed:
        return
    try:
        from app.websocket.manager import manager

        bond = await db.get(FriendBond, outcome.bond_id, populate_existing=True)
        users = {user.id: user for user in (await db.execute(select(User).where(User.id.in_([outcome.low, outcome.high])))).scalars()}
        if bond is None or len(users) != 2:
            return
        badges = (await _badges_of(db, [bond.id]))[bond.id]
        unlocked_at = {(badge.series, badge.level): badge.unlocked_at for badge in badges}
        today_gained = (await _today_gained(db, [bond.id], localtime.local_today(_now()))).get(bond.id, 0)
        level = catalog.level_for(bond.intimacy)
        for viewer_id, friend_id in ((outcome.low, outcome.high), (outcome.high, outcome.low)):
            friend = _brief(users[friend_id])
            await manager.send_to_user(
                viewer_id,
                EVENT_UPDATED,
                {
                    "friend_id": friend_id,
                    "intimacy": bond.intimacy,
                    "level": level.level,
                    "level_name": level.name,
                    "today_gained": today_gained,
                    "badge": resolve_display(bond, viewer_id, badges),
                },
            )
            if outcome.level_after > outcome.level_before:
                await manager.send_to_user(viewer_id, EVENT_LEVEL_UP, {"friend_id": friend_id, "friend": friend, "level": level.level, "level_name": level.name})
            if outcome.new_badges:
                refs = [badge_ref(key, lv, unlocked_at.get((key, lv))) for key, lv in outcome.new_badges]
                await manager.send_to_user(viewer_id, EVENT_BADGES, {"friend_id": friend_id, "friend": friend, "badges": [ref for ref in refs if ref]})
    except Exception:
        logger.exception("Failed to announce intimacy change", extra={"bond_id": outcome.bond_id})


# ---------------------------------------------------------------- 记账


async def _friend_since(db: AsyncSession, low: int, high: int) -> datetime | None:
    """两个人现在是不是好友；是的话返回成为好友的时间（两条好友关系里早的那条）。"""
    value = await db.scalar(
        select(func.min(Friendship.created_at)).where(
            or_(and_(Friendship.user_id == low, Friendship.friend_id == high), and_(Friendship.user_id == high, Friendship.friend_id == low))
        )
    )
    return _aware(value) if value is not None else None


async def _claim_bond(db: AsyncSession, low: int, high: int, now: datetime) -> FriendBond:
    """这一对的行（没有就建），读最新的。调用时已经在写事务里。"""
    await db.execute(
        sqlite_insert(FriendBond.__table__)
        .values(user_low_id=low, user_high_id=high, created_at=now, updated_at=now)
        .on_conflict_do_nothing(index_elements=["user_low_id", "user_high_id"])
    )
    bond = (
        await db.execute(
            select(FriendBond).where(FriendBond.user_low_id == low, FriendBond.user_high_id == high).execution_options(populate_existing=True)
        )
    ).scalar_one()
    return bond


async def _claim_day(db: AsyncSession, bond_id: int, day: date) -> FriendBondDay:
    await db.execute(sqlite_insert(FriendBondDay.__table__).values(bond_id=bond_id, day=day).on_conflict_do_nothing(index_elements=["bond_id", "day"]))
    return (
        await db.execute(select(FriendBondDay).where(FriendBondDay.bond_id == bond_id, FriendBondDay.day == day).execution_options(populate_existing=True))
    ).scalar_one()


async def _sync_badges(db: AsyncSession, bond: FriendBond, friend_days: int, now: datetime) -> list[tuple[str, int]]:
    """按 bond 现在的累计补上该解锁的徽章，返回新解锁的 (系列, 级别)。已经解锁的不会收回。"""
    owned = {(series, level) for series, level in (await db.execute(select(FriendBadge.series, FriendBadge.level).where(FriendBadge.bond_id == bond.id))).all()}
    fresh: list[tuple[str, int]] = []
    # 「集齐几个系列」要等别的系列这一轮的结果，所以放到最后算
    ordered = sorted(catalog.SERIES, key=lambda series: series.stat == "series_count")
    for series in ordered:
        unlocked = {key for key, _level in owned | set(fresh)}
        stats = bond_stats(bond, friend_days, sum(1 for key in catalog.VISIBLE_SERIES if key in unlocked))
        for level in range(1, catalog.reached(series, stats[series.stat]) + 1):
            if (series.key, level) in owned:
                continue
            inserted = await db.execute(
                sqlite_insert(FriendBadge.__table__)
                .values(bond_id=bond.id, series=series.key, level=level, unlocked_at=now)
                .on_conflict_do_nothing(index_elements=["bond_id", "series", "level"])
            )
            if inserted.rowcount == 1:
                fresh.append((series.key, level))
    return fresh


async def _settle(db: AsyncSession, bond: FriendBond, outcome: Outcome, friend_since: datetime | None, now: datetime) -> None:
    bond.level = catalog.level_for(bond.intimacy).level
    bond.updated_at = now
    outcome.level_after = bond.level
    outcome.new_badges = await _sync_badges(db, bond, _friend_days(friend_since, localtime.local_today(now)), now)


async def record_message(db: AsyncSession, conversation_id: int, sender_id: int, message_type: MessageType, *, at: datetime | None = None) -> Outcome | None:
    """私聊里发了一条消息之后调用（消息已经提交）。对方不是好友、不是私聊、功能关着都直接返回 None。"""
    if message_type in UNCOUNTED_TYPES:
        return None
    try:
        conversation = await db.get(Conversation, conversation_id)
        if conversation is None or conversation.type != ConversationType.DIRECT or not conversation.direct_key:
            return None
        try:
            low, high = (int(part) for part in conversation.direct_key.split(":", 1))
        except ValueError:
            return None
        if sender_id not in (low, high) or low == high:
            return None
        cfg = await get_settings(db)
        if not cfg["enabled"]:
            return None
        since = await _friend_since(db, low, high)
        if since is None:
            return None
        # 结束上面的读事务：下面的事务要以写开头
        await db.commit()
        now = _aware(at) or _now()
        moment = _local(now)
        day_key = moment.date()
        side = _side(low, sender_id)
        async with shop_service._write_lock():
            try:
                bond = await _claim_bond(db, low, high, now)
                day = await _claim_day(db, bond.id, day_key)
                outcome = Outcome(bond.id, low, high, level_before=bond.level, level_after=bond.level)
                setattr(day, f"msgs_{side}", getattr(day, f"msgs_{side}") + 1)
                if moment.hour in NIGHT_HOURS:
                    setattr(day, f"night_{side}", getattr(day, f"night_{side}") + 1)
                elif moment.hour in DAWN_HOURS:
                    setattr(day, f"dawn_{side}", getattr(day, f"dawn_{side}") + 1)
                bond.messages_total += 1
                setattr(bond, f"messages_{side}", getattr(bond, f"messages_{side}") + 1)
                _apply_mutual(bond, day)
                outcome.gained = _chat_and_streak(bond, day, cfg)
                bond.intimacy += outcome.gained
                await _settle(db, bond, outcome, since, now)
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return outcome
    except Exception:
        logger.exception("Failed to record intimacy for message", extra={"conversation_id": conversation_id, "sender_id": sender_id})
        try:
            await db.rollback()
        except Exception:
            pass
        return None


async def record_gift(db: AsyncSession, record_id: int) -> Outcome | None:
    """送礼提交之后调用。双方不是好友、功能关着都直接返回 None。生日那天送的（record.on_birthday）有加成，每人每年一次。"""
    try:
        record = await db.get(GiftRecord, record_id)
        if record is None or record.from_user_id == record.to_user_id:
            return None
        cfg = await get_settings(db)
        if not cfg["enabled"]:
            return None
        low, high = _pair(record.from_user_id, record.to_user_id)
        since = await _friend_since(db, low, high)
        if since is None:
            return None
        to_side = _side(low, record.to_user_id)
        amount, on_birthday = int(record.amount or 0), bool(record.on_birthday)
        now = _aware(record.created_at) or _now()
        await db.commit()
        async with shop_service._write_lock():
            try:
                bond = await _claim_bond(db, low, high, now)
                day = await _claim_day(db, bond.id, _local(now).date())
                outcome = Outcome(bond.id, low, high, level_before=bond.level, level_after=bond.level)
                outcome.gained = _gift(bond, day, cfg, to_side=to_side, amount=amount, on_birthday=on_birthday)
                bond.intimacy += outcome.gained
                await _settle(db, bond, outcome, since, now)
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return outcome
    except Exception:
        logger.exception("Failed to record intimacy for gift", extra={"gift_record_id": record_id})
        try:
            await db.rollback()
        except Exception:
            pass
        return None


# 接口层不等记账：发消息 / 送礼提交之后起一个任务，用自己的会话记账、发事件。测试里可以换掉 session_factory，或者 await drain()
session_factory: Any = None
_tasks: set[asyncio.Task] = set()
TASK_LIMIT = 2000


_factories: dict[int, Any] = {}


def _sessions(bind: Any = None) -> Any:
    """记账任务用哪个会话工厂：测试换掉的 > 和发起请求的会话同一个引擎 > 默认的。"""
    if session_factory is not None:
        return session_factory
    if bind is not None:
        factory = _factories.get(id(bind))
        if factory is None:
            from sqlalchemy.ext.asyncio import async_sessionmaker

            factory = _factories[id(bind)] = async_sessionmaker(bind, class_=AsyncSession, expire_on_commit=False)
        return factory
    from app.core.database import AsyncSessionLocal

    return AsyncSessionLocal


def _spawn(job: Any) -> None:
    if len(_tasks) >= TASK_LIMIT:
        logger.error("Intimacy task limit reached; dropping one record")
        return
    task = asyncio.create_task(job())
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


def schedule_message(conversation_id: int, sender_id: int, message_type: MessageType, at: datetime | None = None, *, bind: Any = None) -> None:
    """bind：发起请求的那个会话的引擎（db.bind），记账用同一个库。"""
    if message_type in UNCOUNTED_TYPES:
        return

    async def job() -> None:
        try:
            async with _sessions(bind)() as db:
                await announce(db, await record_message(db, conversation_id, sender_id, message_type, at=at))
        except Exception:
            logger.exception("Intimacy message task failed")

    _spawn(job)


def schedule_gift(record_id: int | None, *, bind: Any = None) -> None:
    if not record_id:
        return

    async def job() -> None:
        try:
            async with _sessions(bind)() as db:
                await announce(db, await record_gift(db, record_id))
        except Exception:
            logger.exception("Intimacy gift task failed")

    _spawn(job)


async def drain() -> None:
    """等排着的记账任务都做完（测试用）。"""
    while _tasks:
        await asyncio.gather(*list(_tasks), return_exceptions=True)


# ---------------------------------------------------------------- 用户侧接口


def level_view(intimacy: int) -> dict[str, Any]:
    level = catalog.level_for(intimacy)
    upcoming = catalog.next_level(level.level)
    return {
        "level": level.level,
        "name": level.name,
        "threshold": level.threshold,
        "next_level": upcoming.level if upcoming else None,
        "next_name": upcoming.name if upcoming else None,
        "next_threshold": upcoming.threshold if upcoming else None,
    }


async def rules_view(db: AsyncSession) -> dict[str, Any]:
    """GET /intimacy/rules。隐藏成就只给系列的 key、图形和级数，其余等解锁了在好友详情里看。"""
    cfg = await get_settings(db)
    series = []
    for item in catalog.SERIES:
        if item.hidden:
            series.append({"key": item.key, "hidden": True, "name": None, "condition": None, "icon": HIDDEN_ICON, "levels": [{"level": lv.level, "tier": lv.tier, "name": None, "threshold": None} for lv in item.levels]})
        else:
            series.append(
                {
                    "key": item.key,
                    "hidden": False,
                    "name": item.name,
                    "condition": item.condition.replace("{n}", "N"),
                    "icon": item.icon,
                    "levels": [{"level": lv.level, "tier": lv.tier, "name": lv.name, "threshold": lv.threshold} for lv in item.levels],
                }
            )
    return {
        "enabled": cfg["enabled"],
        "daily_cap": cfg["daily_cap"],
        "sources": [
            {"key": "chat", "label": "互相聊天", "cap": cfg["chat_cap"], "detail": f"当天双方都发过消息 +{cfg['chat_mutual']}，之后每一来一回 +{cfg['chat_per_round']}"},
            {"key": "gift", "label": "送礼物", "cap": cfg["gift_cap"], "detail": f"每次 +{cfg['gift_base']}，每 {cfg['gift_coins_per_point']} 币再 +1"},
            {"key": "streak", "label": "连续互动", "cap": cfg["streak_bonus"], "detail": f"连续 {cfg['streak_min']} 天以上互相聊天，每天 +{cfg['streak_bonus']}"},
            {"key": "birthday", "label": "生日礼物", "cap": None, "detail": f"在对方生日那天送礼 +{cfg['birthday_bonus']}，每人每年一次，不占每日上限"},
        ],
        "levels": [{"level": lv.level, "name": lv.name, "threshold": lv.threshold} for lv in catalog.LEVELS],
        "series": series,
        "total_badges": catalog.TOTAL_BADGES,
    }


async def _friends_of(db: AsyncSession, user_id: int) -> list[tuple[User, datetime]]:
    rows = await db.execute(
        select(User, Friendship.created_at).join(Friendship, Friendship.friend_id == User.id).where(Friendship.user_id == user_id)
    )
    return [(user, _aware(created_at)) for user, created_at in rows.all()]


async def _bonds_of(db: AsyncSession, user_id: int) -> dict[int, FriendBond]:
    """user 的所有 bond，按对方 id。"""
    rows = await db.execute(select(FriendBond).where(or_(FriendBond.user_low_id == user_id, FriendBond.user_high_id == user_id)))
    return {(bond.user_high_id if bond.user_low_id == user_id else bond.user_low_id): bond for bond in rows.scalars()}


def _unseen(bond: FriendBond, viewer_id: int, badges: list[FriendBadge]) -> int:
    column = "seen_low" if viewer_id == bond.user_low_id else "seen_high"
    return sum(1 for badge in badges if not getattr(badge, column))


async def list_friends(db: AsyncSession, user: User) -> dict[str, Any]:
    """GET /intimacy/friends：我的全部好友，按亲密度从高到低（好友亲密榜，也是客户端画好友名字旁挂件的数据源）。"""
    user_id = user.id
    cfg = await get_settings(db)
    friends = await _friends_of(db, user_id)
    bonds = await _bonds_of(db, user_id)
    bond_ids = [bond.id for bond in bonds.values()]
    badges = await _badges_of(db, bond_ids)
    today = localtime.local_today(_now())
    gained = await _today_gained(db, bond_ids, today)
    rows = []
    for friend, since in friends:
        bond = bonds.get(friend.id)
        intimacy = bond.intimacy if bond else 0
        own = badges.get(bond.id, []) if bond else []
        level = catalog.level_for(intimacy)
        rows.append(
            {
                "friend": UserPublic.model_validate(friend),
                "intimacy": intimacy,
                "level": level.level,
                "level_name": level.name,
                "today_gained": gained.get(bond.id, 0) if bond else 0,
                "streak_days": current_streak(bond.streak_days, bond.last_mutual_day, today) if bond else 0,
                "badge": resolve_display(bond, user_id, own) if bond else None,
                "badge_count": len(own),
                "new_badges": _unseen(bond, user_id, own) if bond else 0,
                "friends_since": since,
            }
        )
    rows.sort(key=lambda row: (-row["intimacy"], row["friends_since"] or datetime.max.replace(tzinfo=UTC), row["friend"].id))
    for rank, row in enumerate(rows, start=1):
        row["rank"] = rank
    return {"enabled": cfg["enabled"], "daily_cap": cfg["daily_cap"], "items": rows, "total": len(rows)}


async def _require_friend(db: AsyncSession, user_id: int, friend_id: int) -> tuple[User, datetime]:
    friend = await db.get(User, friend_id) if friend_id != user_id else None
    since = await _friend_since(db, *_pair(user_id, friend_id)) if friend is not None else None
    if friend is None or since is None:
        raise _error(status.HTTP_404_NOT_FOUND, "not_friend", "你们还不是好友")
    return friend, since


async def _bond_for(db: AsyncSession, user_id: int, friend_id: int) -> FriendBond | None:
    low, high = _pair(user_id, friend_id)
    return (await db.execute(select(FriendBond).where(FriendBond.user_low_id == low, FriendBond.user_high_id == high))).scalar_one_or_none()


def _series_view(series: catalog.Series, value: int, owned: dict[int, FriendBadge], seen_column: str) -> dict[str, Any]:
    top = max(owned, default=0)
    if series.hidden and top == 0:
        return {
            "key": series.key, "hidden": True, "revealed": False, "name": None, "condition": None, "icon": HIDDEN_ICON,
            "current": None, "level": 0, "max_level": len(series.levels), "next_threshold": None,
            "levels": [{"level": lv.level, "name": None, "tier": lv.tier, "threshold": None, "unlocked_at": None, "is_new": False} for lv in series.levels],
        }  # fmt: skip
    upcoming = next((lv for lv in series.levels if lv.level > top), None)
    return {
        "key": series.key,
        "hidden": series.hidden,
        "revealed": True,
        "name": series.name,
        "condition": series.condition,
        "icon": series.icon,
        "current": value,
        "level": top,
        "max_level": len(series.levels),
        "next_threshold": upcoming.threshold if upcoming else None,
        "levels": [
            {
                "level": lv.level,
                "name": lv.name,
                "tier": lv.tier,
                "threshold": lv.threshold,
                "unlocked_at": _aware(owned[lv.level].unlocked_at) if lv.level in owned else None,
                "is_new": lv.level in owned and not getattr(owned[lv.level], seen_column),
            }
            for lv in series.levels
        ],
    }


async def friend_detail(db: AsyncSession, user: User, friend_id: int) -> dict[str, Any]:
    """GET /intimacy/friends/{friend_id}：我和这个好友的亲密度、今天的进度、各项统计、全部成就。"""
    user_id = user.id
    friend, since = await _require_friend(db, user_id, friend_id)
    cfg = await get_settings(db)
    bond = await _bond_for(db, user_id, friend_id)
    today = localtime.local_today(_now())
    friend_days = _friend_days(since, today)
    if bond is None:
        bond = _new_bond(*_pair(user_id, friend_id), _now())
        badges: list[FriendBadge] = []
        day = None
    else:
        badges = (await _badges_of(db, [bond.id]))[bond.id]
        day = (await db.execute(select(FriendBondDay).where(FriendBondDay.bond_id == bond.id, FriendBondDay.day == today))).scalar_one_or_none()
    mine, theirs = _side(bond.user_low_id, user_id), _side(bond.user_low_id, friend_id)
    seen_column = f"seen_{mine}"
    stats = bond_stats(bond, friend_days)
    by_series: dict[str, dict[int, FriendBadge]] = defaultdict(dict)
    for badge in badges:
        by_series[badge.series][badge.level] = badge
    points = {source: getattr(day, f"{source}_points") if day else 0 for source in ("chat", "gift", "streak", "birthday")}
    friend_list = await list_friends(db, user)
    rank = next((row["rank"] for row in friend_list["items"] if row["friend"].id == friend_id), None)
    return {
        "friend": UserPublic.model_validate(friend),
        "friends_since": since,
        "friend_days": friend_days,
        "intimacy": bond.intimacy,
        "level": level_view(bond.intimacy),
        "rank": rank,
        "friend_count": friend_list["total"],
        "display": _display_choice(bond, user_id),
        "badge": resolve_display(bond, user_id, badges),
        "today": {
            "date": today.isoformat(),
            "gained": sum(points.values()),
            "counted": points["chat"] + points["gift"] + points["streak"],
            "cap": cfg["daily_cap"],
            **points,
            "messages_mine": getattr(day, f"msgs_{mine}") if day else 0,
            "messages_theirs": getattr(day, f"msgs_{theirs}") if day else 0,
            "chat_cap": cfg["chat_cap"],
            "gift_cap": cfg["gift_cap"],
        },
        "stats": {
            "messages_total": bond.messages_total,
            "messages_mine": getattr(bond, f"messages_{mine}"),
            "messages_theirs": getattr(bond, f"messages_{theirs}"),
            "mutual_days": bond.mutual_days,
            "streak_days": current_streak(bond.streak_days, bond.last_mutual_day, today),
            "best_streak": bond.best_streak,
            "night_days": bond.night_days,
            "dawn_days": bond.dawn_days,
            "weekend_days": bond.weekend_days,
            "best_day_messages": bond.best_day_messages,
            "gift_count": bond.gift_count,
            "birthday_gifts": bond.birthday_gifts,
        },
        "badge_count": len(badges),
        "total_badges": catalog.TOTAL_BADGES,
        "series": [_series_view(series, stats[series.stat], by_series.get(series.key, {}), seen_column) for series in catalog.SERIES],
    }


async def set_display(db: AsyncSession, user: User, friend_id: int, choice: Any) -> dict[str, Any]:
    """PUT /intimacy/friends/{friend_id}/badge：在这个好友名字旁挂哪一枚。auto / none / 某个已解锁的系列。"""
    user_id = user.id
    await _require_friend(db, user_id, friend_id)
    if not isinstance(choice, str) or (choice not in (DISPLAY_AUTO, DISPLAY_NONE) and choice not in catalog.SERIES_BY_KEY):
        raise _error(422, "invalid_badge", "没有这个徽章")
    bond = await _bond_for(db, user_id, friend_id)
    badges = (await _badges_of(db, [bond.id]))[bond.id] if bond else []
    if choice not in (DISPLAY_AUTO, DISPLAY_NONE) and not any(badge.series == choice for badge in badges):
        raise _error(422, "badge_locked", "这个徽章还没有解锁")
    low, high = _pair(user_id, friend_id)
    column = "display_low" if user_id == low else "display_high"
    stored = None if choice == DISPLAY_AUTO else choice
    async with shop_service._write_lock():
        bond = await _claim_bond(db, low, high, _now())
        setattr(bond, column, stored)
        await db.commit()
    return {"friend_id": friend_id, "display": choice, "badge": resolve_display(bond, user_id, badges)}


async def mark_seen(db: AsyncSession, user: User, friend_id: int) -> dict[str, Any]:
    """POST /intimacy/friends/{friend_id}/seen：我看过了和这个好友的全部徽章（「新」标记消掉）。"""
    user_id = user.id
    await _require_friend(db, user_id, friend_id)
    bond = await _bond_for(db, user_id, friend_id)
    if bond is None:
        return {"cleared": 0}
    column = FriendBadge.seen_low if user_id == bond.user_low_id else FriendBadge.seen_high
    bond_id = bond.id
    async with shop_service._write_lock():
        result = await db.execute(update(FriendBadge).where(FriendBadge.bond_id == bond_id, column.is_(False)).values({column: True}))
        await db.commit()
    return {"cleared": int(result.rowcount or 0)}


async def history(db: AsyncSession, user: User, friend_id: int, days: int) -> dict[str, Any]:
    """GET /intimacy/friends/{friend_id}/history?days=N：近 N 天每天涨了多少，正序，没有记录的日子给 0。"""
    user_id = user.id
    await _require_friend(db, user_id, friend_id)
    days = max(1, min(HISTORY_MAX_DAYS, int(days)))
    today = localtime.local_today(_now())
    first = today - timedelta(days=days - 1)
    bond = await _bond_for(db, user_id, friend_id)
    rows: dict[date, FriendBondDay] = {}
    if bond is not None:
        result = await db.execute(select(FriendBondDay).where(FriendBondDay.bond_id == bond.id, FriendBondDay.day >= first, FriendBondDay.day <= today))
        rows = {row.day: row for row in result.scalars()}
    items = []
    for offset in range(days):
        current = first + timedelta(days=offset)
        row = rows.get(current)
        points = {source: getattr(row, f"{source}_points") if row else 0 for source in ("chat", "gift", "streak", "birthday")}
        items.append({"date": current.isoformat(), "gained": sum(points.values()), **points, "mutual": bool(row and row.mutual)})
    return {"items": items}


# ---------------------------------------------------------------- 成为好友的天数（后台循环）


async def sweep_friend_days(db: AsyncSession, *, now: datetime | None = None, announce_events: bool = True) -> int:
    """给所有好友补「老朋友」（成为好友满 N 天）这一系列（以及别的已经够了但还没记上的）徽章。返回新解锁了徽章的对数。"""
    now = _aware(now) or _now()
    cfg = await get_settings(db)
    if not cfg["enabled"]:
        return 0
    today = localtime.local_today(now)
    series = catalog.SERIES_BY_KEY["oldfriend"]
    first_threshold = series.levels[0].threshold
    rows = await db.execute(select(Friendship.user_id, Friendship.friend_id, Friendship.created_at).where(Friendship.user_id < Friendship.friend_id))
    due = [(low, high, _aware(created)) for low, high, created in rows.all() if _friend_days(_aware(created), today) >= first_threshold]
    if not due:
        await db.commit()
        return 0
    bond_ids = {(low, high): bond_id for bond_id, low, high in (await db.execute(select(FriendBond.id, FriendBond.user_low_id, FriendBond.user_high_id))).all()}
    owned = await db.execute(select(FriendBadge.bond_id, func.max(FriendBadge.level)).where(FriendBadge.series == series.key).group_by(FriendBadge.bond_id))
    owned_by_bond = dict(owned.all())
    todo = [
        (low, high)
        for low, high, created in due
        if catalog.reached(series, _friend_days(created, today)) > owned_by_bond.get(bond_ids.get((low, high)), 0)
    ]
    await db.commit()
    outcomes: list[Outcome] = []
    for low, high in todo:
        async with shop_service._write_lock():
            try:
                since = await _friend_since(db, low, high)
                if since is None:
                    await db.rollback()
                    continue
                bond = await _claim_bond(db, low, high, now)
                outcome = Outcome(bond.id, low, high, level_before=bond.level, level_after=bond.level)
                outcome.new_badges = await _sync_badges(db, bond, _friend_days(since, today), now)
                await db.commit()
            except Exception:
                await db.rollback()
                logger.exception("Friend-days badge sweep failed", extra={"pair": (low, high)})
                continue
        if outcome.new_badges:
            outcomes.append(outcome)
    if announce_events:
        for outcome in outcomes:
            await announce(db, outcome)
    return len(outcomes)


# ---------------------------------------------------------------- 老好友的回填


async def backfill(db: AsyncSession, *, now: datetime | None = None) -> dict[str, int]:
    """按库里还留着的私聊消息和送礼记录，给还没有 bond 的每一对好友算一遍（每天的上限照算），解锁该有的徽章。

    只处理还没有 friend_bonds 行的好友：可以反复跑，不会重复加。不发事件（徽章墙上照样显示「新」）。
    私聊每个会话只留最近 1000 条消息，再早的算不到。只算成为好友之后的消息和礼物。
    """
    now = _aware(now) or _now()
    cfg = await get_settings(db, fresh=True)
    today = localtime.local_today(now)
    pairs = {(low, high): _aware(created) for low, high, created in (await db.execute(select(Friendship.user_id, Friendship.friend_id, Friendship.created_at).where(Friendship.user_id < Friendship.friend_id))).all()}  # fmt: skip
    existing = {tuple(row) for row in (await db.execute(select(FriendBond.user_low_id, FriendBond.user_high_id))).all()}
    pending = [pair for pair in pairs if pair not in existing]
    await db.commit()
    stats = {"pairs": 0, "badges": 0, "skipped": len(pairs) - len(pending)}
    for start in range(0, len(pending), CHUNK):
        chunk = pending[start : start + CHUNK]
        keys = {f"{low}:{high}": (low, high) for low, high in chunk}
        conversations = dict((await db.execute(select(Conversation.id, Conversation.direct_key).where(Conversation.direct_key.in_(list(keys))))).all())
        # (pair) -> {day: FriendBondDay}；gifts 按时间顺序
        days: dict[tuple[int, int], dict[date, FriendBondDay]] = defaultdict(dict)
        events: dict[tuple[int, int], list[tuple[datetime, str, Any]]] = defaultdict(list)
        if conversations:
            message_rows = await db.execute(
                select(Message.conversation_id, Message.sender_id, Message.created_at).where(
                    Message.conversation_id.in_(list(conversations)), Message.type.not_in(UNCOUNTED_TYPES), Message.recalled_at.is_(None)
                )
            )
            for conversation_id, sender_id, created in message_rows.all():
                pair = keys[conversations[conversation_id]]
                created = _aware(created)
                if sender_id not in pair or created < pairs[pair]:
                    continue
                events[pair].append((created, "message", sender_id))
        users = sorted({uid for pair in chunk for uid in pair})
        gift_rows = await db.execute(
            select(GiftRecord.from_user_id, GiftRecord.to_user_id, GiftRecord.amount, GiftRecord.on_birthday, GiftRecord.created_at).where(
                GiftRecord.from_user_id.in_(users), GiftRecord.to_user_id.in_(users)
            )
        )
        for from_id, to_id, amount, on_birthday, created in gift_rows.all():
            pair = _pair(from_id, to_id)
            created = _aware(created)
            if pair in keys.values() and created >= pairs[pair]:
                events[pair].append((created, "gift", (to_id, int(amount or 0), bool(on_birthday))))
        await db.commit()
        for pair in chunk:
            items = sorted(events.get(pair, []), key=lambda item: item[0])
            bond = _new_bond(pair[0], pair[1], now)
            for created, kind, payload in items:
                moment = _local(created)
                day = days[pair].get(moment.date())
                if day is None:
                    day = days[pair][moment.date()] = _new_day(moment.date())
                if kind == "message":
                    side = _side(pair[0], payload)
                    setattr(day, f"msgs_{side}", getattr(day, f"msgs_{side}") + 1)
                    if moment.hour in NIGHT_HOURS:
                        setattr(day, f"night_{side}", getattr(day, f"night_{side}") + 1)
                    elif moment.hour in DAWN_HOURS:
                        setattr(day, f"dawn_{side}", getattr(day, f"dawn_{side}") + 1)
                    bond.messages_total += 1
                    setattr(bond, f"messages_{side}", getattr(bond, f"messages_{side}") + 1)
                    _apply_mutual(bond, day)
                    bond.intimacy += _chat_and_streak(bond, day, cfg)
                else:
                    to_id, amount, on_birthday = payload
                    bond.intimacy += _gift(bond, day, cfg, to_side=_side(pair[0], to_id), amount=amount, on_birthday=on_birthday)
            if not items and _friend_days(pairs[pair], today) < catalog.SERIES_BY_KEY["oldfriend"].levels[0].threshold:
                continue
            bond.level = catalog.level_for(bond.intimacy).level
            values = {column.key: getattr(bond, column.key) for column in FriendBond.__table__.columns if column.key != "id"}
            async with shop_service._write_lock():
                try:
                    inserted = await db.execute(sqlite_insert(FriendBond.__table__).values(**values).on_conflict_do_nothing(index_elements=["user_low_id", "user_high_id"]))
                    if inserted.rowcount != 1:
                        # 回填的同时这一对刚聊上：按实时的为准
                        await db.rollback()
                        continue
                    bond_id = (await db.execute(select(FriendBond.id).where(FriendBond.user_low_id == pair[0], FriendBond.user_high_id == pair[1]))).scalar_one()
                    day_values = [
                        {column.key: getattr(row, column.key) for column in FriendBondDay.__table__.columns if column.key not in ("id", "bond_id")} | {"bond_id": bond_id}
                        for row in days[pair].values()
                    ]
                    if day_values:
                        await db.execute(sqlite_insert(FriendBondDay.__table__), day_values)
                    stored = (await db.execute(select(FriendBond).where(FriendBond.id == bond_id))).scalar_one()
                    fresh = await _sync_badges(db, stored, _friend_days(pairs[pair], today), now)
                    await db.commit()
                except Exception:
                    await db.rollback()
                    logger.exception("Intimacy backfill failed for pair", extra={"pair": pair})
                    continue
            stats["pairs"] += 1
            stats["badges"] += len(fresh)
    return stats


async def backfill_once(db: AsyncSession, *, now: datetime | None = None) -> bool:
    """上线后第一次启动时跑一次回填。两个 worker 同时启动时只有先插上标记的那个做。"""
    now = _aware(now) or _now()
    claimed = await db.execute(
        sqlite_insert(SystemSetting.__table__).values(key=BACKFILL_KEY, value=None, created_at=now, updated_at=now).on_conflict_do_nothing(index_elements=["key"])
    )
    await db.commit()
    if claimed.rowcount != 1:
        return False
    stats = await backfill(db, now=now)
    item = await db.get(SystemSetting, BACKFILL_KEY)
    if item is not None:
        item.value = json.dumps({"at": now.isoformat(), **stats})
        await db.commit()
    logger.info("Intimacy legacy backfill done", extra=stats)
    return True


_worker_task: asyncio.Task | None = None


async def _worker() -> None:
    from app.core.database import AsyncSessionLocal

    try:
        async with AsyncSessionLocal() as db:
            await backfill_once(db)
    except Exception:
        logger.exception("Intimacy backfill failed")
    while True:
        try:
            async with AsyncSessionLocal() as db:
                await sweep_friend_days(db)
        except Exception:
            logger.exception("Intimacy friend-days sweep failed")
        await asyncio.sleep(FRIEND_DAYS_SWEEP_SECONDS)


def start_intimacy_worker() -> None:
    global _worker_task
    if _worker_task is not None and not _worker_task.done():
        return
    _worker_task = asyncio.create_task(_worker())


async def stop_intimacy_worker() -> None:
    global _worker_task
    task = _worker_task
    _worker_task = None
    if task is None or task.done():
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


# ---------------------------------------------------------------- 后台


async def admin_overview(db: AsyncSession) -> dict[str, Any]:
    """GET /admin/intimacy/overview：全站的汇总。不含任何一对好友的明细。"""
    today = localtime.local_today(_now())
    bonds = int(await db.scalar(select(func.count()).select_from(FriendBond)) or 0)
    total = int(await db.scalar(select(func.coalesce(func.sum(FriendBond.intimacy), 0))) or 0)
    active = int(await db.scalar(select(func.count()).select_from(FriendBondDay).where(FriendBondDay.day == today)) or 0)
    gained_today = int(
        await db.scalar(
            select(func.coalesce(func.sum(FriendBondDay.chat_points + FriendBondDay.gift_points + FriendBondDay.streak_points + FriendBondDay.birthday_points), 0)).where(
                FriendBondDay.day == today
            )
        )
        or 0
    )
    friend_pairs = int(await db.scalar(select(func.count()).select_from(Friendship).where(Friendship.user_id < Friendship.friend_id)) or 0)
    by_level = dict((await db.execute(select(FriendBond.level, func.count()).group_by(FriendBond.level))).all())
    # 没有 bond 的好友算 0 级
    by_level[0] = by_level.get(0, 0) + max(0, friend_pairs - bonds)
    unlocks = {(series, level): count for series, level, count in (await db.execute(select(FriendBadge.series, FriendBadge.level, func.count()).group_by(FriendBadge.series, FriendBadge.level))).all()}  # fmt: skip
    backfill_row = await db.get(SystemSetting, BACKFILL_KEY)
    return {
        "friend_pairs": friend_pairs,
        "bonds": bonds,
        "total_intimacy": total,
        "active_today": active,
        "gained_today": gained_today,
        "levels": [{"level": lv.level, "name": lv.name, "threshold": lv.threshold, "count": int(by_level.get(lv.level, 0))} for lv in catalog.LEVELS],
        "series": [
            {
                "key": series.key,
                "name": series.name,
                "condition": series.condition,
                "icon": series.icon,
                "hidden": series.hidden,
                "stat": series.stat,
                "levels": [{"level": lv.level, "name": lv.name, "tier": lv.tier, "threshold": lv.threshold, "count": int(unlocks.get((series.key, lv.level), 0))} for lv in series.levels],
            }
            for series in catalog.SERIES
        ],
        "total_badges": catalog.TOTAL_BADGES,
        "unlocked_badges": sum(unlocks.values()),
        "backfill": json.loads(backfill_row.value) if backfill_row is not None and backfill_row.value else None,
    }


async def admin_user_bonds(db: AsyncSession, user_id: int) -> dict[str, Any]:
    """GET /admin/intimacy/users/{user_id}：这个人和每个好友的亲密度、等级、徽章数。

    不给聊天的条数、深夜 / 清晨这些和私聊行为有关的明细：后台不能借亲密度窥探私聊。
    """
    user = await db.get(User, user_id)
    if user is None:
        raise _error(404, "user_not_found", "用户不存在")
    friends = await _friends_of(db, user_id)
    bonds = await _bonds_of(db, user_id)
    counts = dict((await db.execute(select(FriendBadge.bond_id, func.count()).where(FriendBadge.bond_id.in_([b.id for b in bonds.values()] or [0])).group_by(FriendBadge.bond_id))).all())  # fmt: skip
    items = []
    for friend, since in friends:
        bond = bonds.get(friend.id)
        intimacy = bond.intimacy if bond else 0
        level = catalog.level_for(intimacy)
        items.append(
            {
                "friend": UserPublic.model_validate(friend),
                "friends_since": since,
                "intimacy": intimacy,
                "level": level.level,
                "level_name": level.name,
                "badge_count": int(counts.get(bond.id, 0)) if bond else 0,
                "updated_at": _aware(bond.updated_at) if bond else None,
            }
        )
    items.sort(key=lambda row: (-row["intimacy"], row["friend"].id))
    return {"user": UserPublic.model_validate(user), "items": items, "total": len(items)}


async def admin_adjust(db: AsyncSession, admin: User, user_id: int, friend_id: int, delta: Any, note: Any) -> dict[str, Any]:
    """POST /admin/intimacy/adjust：手动增减一对好友的亲密度（不受每日上限），写审计日志。减到 0 为止。"""
    admin_id = admin.id
    if isinstance(delta, bool) or not isinstance(delta, int) or delta == 0 or abs(delta) > 100000:
        raise _error(422, "invalid_delta", "调整量要是 ±1 到 ±100000 的整数")
    note = (note or "").strip() if isinstance(note, str) else ""
    if not note or len(note) > 200:
        raise _error(422, "invalid_note", "要写调整原因（200 字以内）")
    if user_id == friend_id:
        raise _error(404, "not_friend", "这两个人不是好友")
    low, high = _pair(user_id, friend_id)
    since = await _friend_since(db, low, high)
    if since is None:
        raise _error(404, "not_friend", "这两个人不是好友")
    await db.commit()
    now = _now()
    async with shop_service._write_lock():
        try:
            bond = await _claim_bond(db, low, high, now)
            outcome = Outcome(bond.id, low, high, level_before=bond.level, level_after=bond.level)
            before = bond.intimacy
            bond.intimacy = max(0, before + delta)
            outcome.gained = bond.intimacy - before
            await _settle(db, bond, outcome, since, now)
            await log_admin_action(db, admin_id, "intimacy.adjust", "friend_bond", bond.id, {"users": [low, high], "delta": delta, "before": before, "after": bond.intimacy, "note": note})
            await db.commit()
        except BaseException:
            await db.rollback()
            raise
    await announce(db, outcome)
    level = catalog.level_for(bond.intimacy)
    # 只给数量：后台看不到具体是哪几枚徽章（徽章本身透露了私聊的时间习惯）
    return {"intimacy": bond.intimacy, "level": level.level, "level_name": level.name, "new_badge_count": len(outcome.new_badges)}
