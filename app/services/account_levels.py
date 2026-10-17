"""账号等级：活跃天数的累计、等级、排行榜（docs/account-level.md）。

几件事分得比较开：

- 在线时长：后台循环每个时间片（默认 5 分钟）给当时在线的人记一次。每个 worker 都跑这个循环，
  靠「上次计入的时间片编号」去重，同一个时间片不管几个进程、几个设备都只记一次。
- 结算：settle_users 按某一天的在线分钟算基础、逐项检查加成，把新拿到的差额加到累计上并重算等级。
  可以反复调用，不会重复加（日活行上的条件更新保证并发时只有一个能记上）。
  调用时机只有三个：在线循环加完分钟之后、GET /levels/me、跨天后给昨天收尾。发消息的路径上没有它。
- 等级和累计冗余在 users 上（account_level / account_active_tenths），序列化用户不用查这里的表。

活跃天在库里一律是整数的「十分之一天」，对外换算成一位小数。「天」都是 UTC+8 的自然日。
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import logging
import math
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, NamedTuple

from fastapi import HTTPException
from sqlalchemy import Date, and_, bindparam, case, distinct, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.core.dialect import greatest, is_postgres, upsert_insert
from app.models.account_level import AccountLevelDay, AccountLevelNotification
from app.models.case import JuryMember, JuryReviewVote, JuryVote
from app.models.conversation import Conversation
from app.models.enums import ConversationType, MessageType, UserStatus
from app.models.group_activity import GroupCheckin, GroupMemberDailyActivity
from app.models.message import Message
from app.models.report import AdminAuditLog
from app.models.system import SystemSetting
from app.models.user import User
from app.schemas.notification import UnifiedNotificationRead
from app.schemas.user import UserPublic

logger = logging.getLogger(__name__)
UTC = timezone.utc
# 「每天」「本月」都按北京时间的自然日 / 自然月算
LOCAL_TZ = timezone(timedelta(hours=8))

# 从大到小：一个图标顶多少级
ICONS: tuple[tuple[str, str, int], ...] = (("crown", "皇冠", 64), ("sun", "太阳", 16), ("moon", "月亮", 4), ("star", "星星", 1))
FORMULA = "N² + 4N"
MILESTONE_LEVELS = (4, 16, 64)
# 加成的 key，顺序就是对外的顺序；日活行上对应的列是 bonus_<key>
BONUS_KEYS: tuple[str, ...] = ("messages", "checkin", "ccw_bound", "jury", "streak_7", "streak_30")
BACKFILL_KEY = "account_level.legacy_backfill"
SHOP_MODULE = "app.services.shop"
# IN (...) 一次带多少个 id。旧版本的 SQLite 一条语句最多 999 个变量
CHUNK = 400
# 查当天签到时，要查的人不超过这个数才把用户条件带进 SQL
CHECKIN_FILTER_BY_USER_MAX = 20
# 群里的发言数取自 group_member_daily_activity，它按 UTC 日期分行；那一行在本地日结束之后还会继续涨，
# 所以给过去的某一天收尾时，超过这个宽限就不再读它，只用当天已经记下的数
GROUP_COUNTER_GRACE = timedelta(minutes=15)

_day_t = AccountLevelDay.__table__
_user_t = User.__table__


# ---------------------------------------------------------------- 等级公式（纯函数）


def days_for_level(level: int) -> int:
    """升到 level 级需要的累计活跃天数：N² + 4N。"""
    level = max(0, int(level))
    return level * level + 4 * level


def _level_for_tenths(tenths: int, max_level: int | None = None) -> int:
    """累计（十分之一天）对应的等级。n² + 4n <= d 等价于 (n + 2)² <= d + 4，用整数开方，边界上不会差一级。"""
    whole_days = max(0, int(tenths)) // 10
    level = math.isqrt(whole_days + 4) - 2
    return max(0, min(level, settings.account_level_max if max_level is None else max_level))


def level_for_days(days: float, max_level: int | None = None) -> int:
    """days_for_level 的反函数：累计 days 个活跃天是几级。"""
    return _level_for_tenths(_to_tenths(days), max_level)


def icons_for_level(level: int) -> dict[str, int]:
    """四进制拆成皇冠 / 太阳 / 月亮 / 星星。0 级全是 0；256 级是 4 个皇冠。"""
    remaining = max(0, int(level))
    icons: dict[str, int] = {}
    for key, _label, size in ICONS:
        icons[key], remaining = divmod(remaining, size)
    return icons


def _to_tenths(days: float) -> int:
    return int(round(float(days) * 10))


def _to_days(tenths: int) -> float:
    return round(int(tenths) / 10, 1)


# ---------------------------------------------------------------- 规则（来自部署配置）


@dataclass(frozen=True)
class Rules:
    max_level: int
    cap: int
    # (分钟门槛, 十分之一天)，门槛高的在前
    online: tuple[tuple[int, int], ...]
    messages_target: int
    streak_short_target: int
    streak_long_target: int
    # key -> 十分之一天
    bonus: dict[str, int]
    tick_seconds: int

    @property
    def tick_minutes(self) -> int:
        return max(1, self.tick_seconds // 60)

    @property
    def min_online_minutes(self) -> int:
        return min(minutes for minutes, _tenths in self.online)

    def base_for(self, minutes: int) -> int:
        """当天在线这么多分钟能拿到的基础活跃天（十分之一天）。"""
        return max((tenths for threshold, tenths in self.online if minutes >= threshold), default=0)


def rules() -> Rules:
    """每次现读配置：数值很少，而且测试里会改。"""
    return Rules(
        max_level=settings.account_level_max,
        cap=_to_tenths(settings.account_level_daily_cap),
        online=tuple(
            sorted(
                (
                    (settings.account_level_online_full_minutes, _to_tenths(settings.account_level_online_full_days)),
                    (settings.account_level_online_half_minutes, _to_tenths(settings.account_level_online_half_days)),
                ),
                reverse=True,
            )
        ),
        messages_target=settings.account_level_bonus_messages_target,
        streak_short_target=settings.account_level_streak_short_target,
        streak_long_target=settings.account_level_streak_long_target,
        bonus={
            "messages": _to_tenths(settings.account_level_bonus_messages_days),
            "checkin": _to_tenths(settings.account_level_bonus_checkin_days),
            "ccw_bound": _to_tenths(settings.account_level_bonus_ccw_bound_days),
            "jury": _to_tenths(settings.account_level_bonus_jury_days),
            "streak_7": _to_tenths(settings.account_level_streak_short_days),
            "streak_30": _to_tenths(settings.account_level_streak_long_days),
        },
        tick_seconds=settings.account_level_tick_seconds,
    )


def rules_payload() -> dict[str, Any]:
    """GET /levels/rules。"""
    r = rules()
    return {
        "max_level": r.max_level,
        "formula": FORMULA,
        "icons": [{"key": key, "label": label, "levels": size} for key, label, size in ICONS],
        "daily_cap": _to_days(r.cap),
        "online": [{"minutes": minutes, "days": _to_days(tenths)} for minutes, tenths in r.online],
        "bonuses": [
            {"key": "messages", "label": f"当天发言满 {r.messages_target} 条", "days": _to_days(r.bonus["messages"]), "target": r.messages_target},
            {"key": "checkin", "label": "当天在任意群签到", "days": _to_days(r.bonus["checkin"])},
            {"key": "ccw_bound", "label": "已绑定 CCW 账号", "days": _to_days(r.bonus["ccw_bound"])},
            {"key": "jury", "label": "风纪委员当天参与评审", "days": _to_days(r.bonus["jury"])},
            {"key": "streak_7", "label": f"连续活跃 {r.streak_short_target} 天", "days": _to_days(r.bonus["streak_7"]), "target": r.streak_short_target},
            {"key": "streak_30", "label": f"连续活跃 {r.streak_long_target} 天", "days": _to_days(r.bonus["streak_30"]), "target": r.streak_long_target},
        ],
        "milestones": [{"level": level, "days": days_for_level(level)} for level in MILESTONE_LEVELS if level <= r.max_level],
    }


# ---------------------------------------------------------------- 时间


def _moment(now: datetime | None) -> datetime:
    """现在，UTC。调用方传进来的时刻（测试、补跑）也统一换成 UTC：库里的时间是按 UTC 的字面值存的。"""
    return datetime.now(UTC) if now is None else now.astimezone(UTC)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def local_day(moment: datetime) -> date:
    return moment.astimezone(LOCAL_TZ).date()


def day_bounds(day: date) -> tuple[datetime, datetime]:
    """这个自然日（UTC+8）的起止时刻，UTC。库里的时间都是按 UTC 存的，比较前必须换成 UTC。"""
    start = datetime(day.year, day.month, day.day, tzinfo=LOCAL_TZ).astimezone(UTC)
    return start, start + timedelta(days=1)


def slot_of(moment: datetime, tick_seconds: int | None = None) -> int:
    """时间片编号 = floor(epoch 秒 / 时间片长度)。"""
    return int(moment.timestamp()) // (tick_seconds or settings.account_level_tick_seconds)


def _chunks(values: list[int]) -> Iterable[list[int]]:
    for index in range(0, len(values), CHUNK):
        yield values[index : index + CHUNK]


def _unique_ids(values: Iterable[int]) -> list[int]:
    return sorted({int(value) for value in values})


# ---------------------------------------------------------------- 在线时长


async def add_online_minutes(db: AsyncSession, user_ids: Iterable[int], *, now: datetime | None = None) -> tuple[list[int], int]:
    """给这些人当天的行加一个时间片的在线时长，返回 (实际参与的用户, 这次真正记上的行数)。

    一条带条件的 upsert 批量执行：行不存在就建，存在且「上次计入的时间片 < 当前时间片」才加。
    所以同一个时间片里再调一次（另一个 worker、另一台设备、重启后补跑）什么都不会发生。
    """
    now = _moment(now)
    r = rules()
    day, slot = local_day(now), slot_of(now, r.tick_seconds)
    wanted = _unique_ids(user_ids)
    eligible: list[int] = []
    for chunk in _chunks(wanted):
        # 在线名单来自 Redis，可能有已经删掉的账号；机器人不参与账号等级
        rows = await db.execute(select(_user_t.c.id).where(_user_t.c.id.in_(chunk), _user_t.c.is_bot.is_(False)))
        eligible.extend(rows.scalars().all())
    if not eligible:
        return [], 0
    statement = upsert_insert(db, _day_t)
    statement = statement.on_conflict_do_update(
        index_elements=[_day_t.c.user_id, _day_t.c.day],
        set_={
            "online_minutes": _day_t.c.online_minutes + statement.excluded.online_minutes,
            "last_slot": statement.excluded.last_slot,
            "updated_at": statement.excluded.updated_at,
        },
        where=_day_t.c.last_slot < statement.excluded.last_slot,
    )
    rows = [
        {"user_id": user_id, "day": day, "online_minutes": r.tick_minutes, "last_slot": slot, "created_at": now, "updated_at": now}
        for user_id in sorted(eligible)
    ]
    if is_postgres(db):
        # asyncpg 的 executemany 不报 rowcount：改成一条多行 INSERT ... RETURNING，返回几行就是记上了几个人
        result = await db.execute(statement.values(rows).returning(_day_t.c.user_id))
        credited = len(result.all())
    else:
        result = await db.execute(statement, rows)
        credited = result.rowcount if result.rowcount is not None and result.rowcount >= 0 else len(eligible)
    await db.commit()
    return sorted(eligible), int(credited)


# ---------------------------------------------------------------- 加成的条件


class _Messages(NamedTuple):
    """一个人一天的发言数，按来源分成三段（时间上不重叠，加起来就是总数）。"""

    # 群，北京时间 08:00 之后：group_member_daily_activity 的计数
    group: int = 0
    # 北京时间 00:00–08:00，群和私聊都在内：从 messages 数
    night: int = 0
    # 私聊，北京时间 08:00 之后：从 messages 数
    private: int = 0


def _message_total(counted: _Messages | None, seen_total: int, seen_night: int) -> tuple[int, int]:
    """这次数到的和日活行上已经记下的合起来：(发言总数, 凌晨那一段)。两个数都只增不减。"""
    if counted is None:
        return seen_total, seen_night
    night = max(seen_night, counted.night)
    return max(seen_total, night + counted.group + counted.private), night


async def _message_counts(
    db: AsyncSession, user_ids: list[int], day: date, now: datetime, *, enough: int | None = None
) -> dict[int, _Messages]:
    """这些人在这一天（UTC+8）的发言数。

    群消息不能直接从 messages 数：每个会话只留最近一千条，热闹的群里当天的消息白天就被裁掉了。
    所以群里的用 group_member_daily_activity 的计数，但它是按 UTC 日期分行的 —— 日期等于这一天的那一行
    覆盖的是北京时间 08:00 之后；00:00–08:00 的群消息落在前一个 UTC 日期的行里，分不出来，这一段改从 messages 数。
    私聊全天都从 messages 数。几部分在时间上不重叠，群消息不会被数两遍。

    enough：群计数已经达到这个数的人不再去数 messages（只需要知道够不够的时候用）。
    """
    if not user_ids:
        return {}
    start, end = day_bounds(day)
    group = dict.fromkeys(user_ids, 0)
    if now <= end + GROUP_COUNTER_GRACE:
        for chunk in _chunks(user_ids):
            rows = await db.execute(
                select(GroupMemberDailyActivity.user_id, func.coalesce(func.sum(GroupMemberDailyActivity.message_count), 0))
                .where(GroupMemberDailyActivity.user_id.in_(chunk), GroupMemberDailyActivity.activity_date == day)
                .group_by(GroupMemberDailyActivity.user_id)
            )
            for user_id, total in rows.all():
                group[user_id] = int(total or 0)
    counts = {user_id: _Messages(group=group[user_id]) for user_id in user_ids}
    # 这一天里属于前一个 UTC 日期的那一段（北京时间 00:00–08:00）到这里为止
    utc_midnight = datetime(day.year, day.month, day.day, tzinfo=UTC)
    at_night = case((Message.created_at < utc_midnight, 1), else_=0)
    remaining = [user_id for user_id in user_ids if enough is None or group[user_id] < enough]
    for chunk in _chunks(remaining):
        rows = await db.execute(
            select(Message.sender_id, func.sum(at_night), func.sum(1 - at_night))
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(
                Message.sender_id.in_(chunk),
                Message.created_at >= start,
                Message.created_at < end,
                Message.type != MessageType.SYSTEM,
                or_(Message.created_at < utc_midnight, Conversation.type == ConversationType.DIRECT),
            )
            .group_by(Message.sender_id)
        )
        for user_id, night, private in rows.all():
            counts[user_id] = _Messages(group=group[user_id], night=int(night or 0), private=int(private or 0))
    return counts


async def _checkin_users(db: AsyncSession, user_ids: list[int], day: date) -> set[int]:
    """这一天（UTC+8）在任意群签过到的人。群签到自己的「一天」是 UTC 日期，这里按签到发生的时刻算。"""
    if not user_ids:
        return set()
    start, end = day_bounds(day)
    today = select(distinct(GroupCheckin.user_id)).where(GroupCheckin.created_at >= start, GroupCheckin.created_at < end)
    if len(user_ids) <= CHECKIN_FILTER_BY_USER_MAX:
        today = today.where(GroupCheckin.user_id.in_(user_ids))
    # 人多的时候不带用户条件：带上的话 SQLite 会按 user_id 的索引把每个人历史上的签到全翻一遍，
    # 不如按时间索引取出当天全部签到（也就几百行）再在内存里取交集
    return set((await db.execute(today)).scalars().all()) & set(user_ids)


async def _jury_voters(db: AsyncSession, user_ids: list[int], day: date, now: datetime) -> set[int]:
    """在任的风纪委员里，这一天（UTC+8）投出过票的人。原审、资深合议、申诉复审 / 复议的票都算；回避不算投票。"""
    start, end = day_bounds(day)
    serving: list[int] = []
    for chunk in _chunks(user_ids):
        rows = await db.execute(
            select(JuryMember.user_id).where(
                JuryMember.user_id.in_(chunk), JuryMember.status == "active", or_(JuryMember.term_ends_at.is_(None), JuryMember.term_ends_at > now)
            )
        )
        serving.extend(rows.scalars().all())
    found: set[int] = set()
    for chunk in _chunks(serving):
        for model in (JuryVote, JuryReviewVote):
            rows = await db.execute(
                select(distinct(model.juror_id)).where(model.juror_id.in_(chunk), model.voted_at >= start, model.voted_at < end)
            )
            found.update(rows.scalars().all())
    return found


# ---------------------------------------------------------------- 连续活跃


def _next_streak(streak_days: int, streak_day: date | None, day: date) -> tuple[int, bool]:
    """day 这天第一次拿到基础时，连续天数变成多少、要不要写回用户。

    前一天拿到过就续上，否则从 1 重新算。streak_day 比 day 还新只会出现在给过去的某一天补结算时，那时不动用户身上的数。
    """
    if streak_day == day - timedelta(days=1):
        return max(0, streak_days or 0) + 1, True
    if streak_day == day:
        return max(1, streak_days or 0), False
    if streak_day is not None and streak_day > day:
        return 1, False
    return 1, True


def current_streak(streak_days: int | None, streak_day: date | None, today: date) -> int:
    """现在的连续活跃天数：今天拿到了基础就含今天；昨天和今天都没拿到就断了。"""
    if streak_day is None or streak_day < today - timedelta(days=1):
        return 0
    return int(streak_days or 0)


# ---------------------------------------------------------------- 结算


@dataclass
class LevelUp:
    user_id: int
    old_level: int
    level: int
    active_tenths: int


@dataclass
class _Change:
    """一行日活要写成什么样。day_params 是 _DAY_SETTLE 的参数。"""

    user_id: int
    day_params: dict[str, Any]
    # 这次新记入累计的差额
    delta: int
    # 这次是当天第一次拿到基础、并且要把连续天数写回用户时有值
    streak: int | None = None


# 结算的两条写语句，参数名都带 p_ 前缀（不能和列名重）。写成固定的形状是为了能整批执行：
# 跨过门槛的人常常是同一个时间片里一起跨的（零点起一直在线的人会在同一刻满 30 分钟），逐条执行要在写锁里来回几百趟。
# 日活行：带着读到的旧值做条件；各列只增不减，所以哪怕和另一个结算撞上也不会把已经记上的加成冲掉。
_DAY_SETTLE = (
    _day_t.update()
    .where(_day_t.c.id == bindparam("p_id"), _day_t.c.base_tenths == bindparam("p_old_base"), _day_t.c.earned_tenths == bindparam("p_old_earned"))
    .values(
        base_tenths=bindparam("p_base"),
        earned_tenths=bindparam("p_earned"),
        streak_days=greatest(_day_t.c.streak_days, bindparam("p_streak")),
        message_count=greatest(_day_t.c.message_count, bindparam("p_messages")),
        night_messages=greatest(_day_t.c.night_messages, bindparam("p_night")),
        updated_at=bindparam("p_now"),
        **{f"bonus_{key}": greatest(_day_t.c[f"bonus_{key}"], bindparam(f"p_bonus_{key}")) for key in BONUS_KEYS},
    )
)
# 用户：累计加上差额；p_touch = 1 时把连续天数写上（只往后写，不会被补结算的旧日期盖掉）。不动 updated_at —— 这不是资料修改
_STREAK_MOVES = and_(
    bindparam("p_touch") == 1,
    or_(_user_t.c.account_streak_day.is_(None), _user_t.c.account_streak_day < bindparam("p_day", type_=Date)),
)
_USER_CREDIT = (
    _user_t.update()
    .where(_user_t.c.id == bindparam("p_id"))
    .values(
        account_active_tenths=_user_t.c.account_active_tenths + bindparam("p_delta"),
        account_streak_days=case((_STREAK_MOVES, bindparam("p_streak")), else_=_user_t.c.account_streak_days),
        account_streak_day=case((_STREAK_MOVES, bindparam("p_day", type_=Date)), else_=_user_t.c.account_streak_day),
        updated_at=_user_t.c.updated_at,
    )
)


async def _load_day_rows(db: AsyncSession, user_ids: list[int], day: date) -> dict[int, Any]:
    rows: dict[int, Any] = {}
    for chunk in _chunks(user_ids):
        result = await db.execute(select(_day_t).where(_day_t.c.day == day, _day_t.c.user_id.in_(chunk)))
        for row in result.all():
            rows[row.user_id] = row
    return rows


async def settle_users(
    db: AsyncSession,
    user_ids: Iterable[int],
    *,
    day: date | None = None,
    now: datetime | None = None,
    message_counts: dict[int, _Messages] | None = None,
) -> list[LevelUp]:
    """重算这些人在 day（默认今天）的活跃天，把新拿到的差额记到累计上并重算等级。自己提交事务。

    幂等：基础、各项加成、当天合计都记在日活行上，只增不减；再调一次算出来的是同样的数，差额为 0。
    并发：写日活行时带上读到的旧值做条件，两个 worker 同时结算同一个人只有一个能写进去，另一个直接跳过。
    返回升级了的人。通知中心的那条通知已经在同一个事务里写好；实时事件和商店钩子由调用方交给 announce_level_ups。
    message_counts：调用方已经数好的发言数（/levels/me 要展示确切的条数，数过一遍就不用再数）。
    """
    now = _moment(now)
    day = day or local_day(now)
    r = rules()
    rows = await _load_day_rows(db, _unique_ids(user_ids), day)
    if not rows:
        return []

    # 发言数：还没拿到这项加成、记下的数也还不够的人才去数。没拿到基础的人也数 —— 把数先记下来，免得凌晨的消息之后被裁掉
    counting = [user_id for user_id, row in rows.items() if not row.bonus_messages and row.message_count < r.messages_target]
    if message_counts is None:
        message_counts = await _message_counts(db, counting, day, now, enough=r.messages_target) if r.bonus["messages"] > 0 else {}

    base = {user_id: max(row.base_tenths, r.base_for(row.online_minutes)) for user_id, row in rows.items()}
    based = [user_id for user_id in rows if base[user_id] > 0]
    users: dict[int, Any] = {}
    for chunk in _chunks(based):
        result = await db.execute(
            select(_user_t.c.id, _user_t.c.ccw_student_oid, _user_t.c.account_streak_days, _user_t.c.account_streak_day).where(_user_t.c.id.in_(chunk))
        )
        users.update({row.id: row for row in result.all()})
    checked_in = await _checkin_users(db, [user_id for user_id in based if not rows[user_id].bonus_checkin], day) if r.bonus["checkin"] > 0 else set()
    jurors = await _jury_voters(db, [user_id for user_id in based if not rows[user_id].bonus_jury], day, now) if r.bonus["jury"] > 0 else set()

    changes: list[_Change] = []
    for user_id, row in rows.items():
        count, night = _message_total(message_counts.get(user_id), row.message_count, row.night_messages)
        streak_to_user: int | None = None
        # 没拿到基础的行：除了发言数，别的都保持原样
        new_base, earned, streak = row.base_tenths, row.earned_tenths, row.streak_days
        flags = {key: getattr(row, f"bonus_{key}") for key in BONUS_KEYS}
        user = users.get(user_id)
        if base[user_id] > 0 and user is not None:
            new_base = base[user_id]
            if row.base_tenths == 0:
                streak, touch_user = _next_streak(user.account_streak_days, user.account_streak_day, day)
                streak_to_user = streak if touch_user else None
            # 加成拿到之后不收回：已经记上的保持原值，没记上的看现在满不满足
            satisfied = {
                "messages": count >= r.messages_target,
                "checkin": user_id in checked_in,
                "ccw_bound": bool(user.ccw_student_oid),
                "jury": user_id in jurors,
                # 连满长的那档只给长的，短的那档不再给
                "streak_7": r.streak_short_target <= streak < r.streak_long_target,
                "streak_30": streak >= r.streak_long_target,
            }
            for key in BONUS_KEYS:
                if not flags[key] and satisfied[key]:
                    flags[key] = r.bonus[key]
            earned = max(row.earned_tenths, min(r.cap, new_base + sum(flags.values())))
        target = {
            "base_tenths": new_base,
            "earned_tenths": earned,
            "streak_days": streak,
            "message_count": count,
            "night_messages": night,
            **{f"bonus_{key}": flags[key] for key in BONUS_KEYS},
        }
        if all(getattr(row, column) == value for column, value in target.items()):
            continue
        changes.append(
            _Change(
                user_id=user_id,
                day_params={
                    "p_id": row.id,
                    "p_old_base": row.base_tenths,
                    "p_old_earned": row.earned_tenths,
                    "p_base": new_base,
                    "p_earned": earned,
                    "p_streak": streak,
                    "p_messages": count,
                    "p_night": night,
                    "p_now": now,
                    **{f"p_bonus_{key}": flags[key] for key in BONUS_KEYS},
                },
                delta=earned - row.earned_tenths,
                streak=streak_to_user,
            )
        )
    if not changes:
        return []

    try:
        written = await _write_changes(db, changes, day)
        ups = await _sync_levels(db, [change.user_id for change in written if change.delta > 0], now)
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    return ups


async def _write_changes(db: AsyncSession, changes: list[_Change], day: date) -> list[_Change]:
    """把算好的结果写进去，返回真正写上的那些。在调用方的事务里执行，不提交。

    先整批写日活行。影响的行数对得上，说明每一行的条件都成立；对不上就是有的行刚被另一个 worker 结算过，
    这时整批撤掉、改成一行一行写，没写上的那些它已经记过了，跳过。
    """
    if is_postgres(db):
        # asyncpg 的 executemany 不报影响的行数，没法判断整批是否都写上：直接一行一行写（不用撤整批）
        written = [change for change in changes if (await db.execute(_DAY_SETTLE, change.day_params)).rowcount == 1]
    else:
        result = await db.execute(_DAY_SETTLE, [change.day_params for change in changes])
        if result.rowcount == len(changes):
            written = changes
        else:
            await db.rollback()
            written = []
            for change in changes:
                if (await db.execute(_DAY_SETTLE, change.day_params)).rowcount == 1:
                    written.append(change)
    credits = [
        {"p_id": change.user_id, "p_delta": change.delta, "p_touch": 0 if change.streak is None else 1, "p_streak": change.streak or 0, "p_day": day}
        for change in written
        if change.delta > 0 or change.streak is not None
    ]
    if credits:
        await db.execute(_USER_CREDIT, credits)
    return written


def _icons_text(level: int) -> str:
    icons = icons_for_level(level)
    units = {"crown": "个皇冠", "sun": "个太阳", "moon": "个月亮", "star": "颗星星"}
    return " ".join(f"{icons[key]} {units[key]}" for key, _label, _size in ICONS if icons[key])


async def _sync_levels(db: AsyncSession, user_ids: list[int], now: datetime, *, allow_down: bool = False) -> list[LevelUp]:
    """累计变了之后按公式重写 users.account_level，返回升级了的人。在调用方的事务里执行，不提交。

    结算只会让等级往上走；allow_down 给后台手动扣减用。升到 account_level_notify_every 的倍数时顺带写一条通知。
    """
    ups: list[LevelUp] = []
    every = settings.account_level_notify_every
    for chunk in _chunks(_unique_ids(user_ids)):
        result = await db.execute(select(_user_t.c.id, _user_t.c.account_active_tenths, _user_t.c.account_level).where(_user_t.c.id.in_(chunk)))
        for row in result.all():
            level = _level_for_tenths(row.account_active_tenths)
            if level == row.account_level or (level < row.account_level and not allow_down):
                continue
            await db.execute(_user_t.update().where(_user_t.c.id == row.id).values(account_level=level, updated_at=_user_t.c.updated_at))
            if level < row.account_level:
                continue
            ups.append(LevelUp(user_id=row.id, old_level=row.account_level, level=level, active_tenths=row.account_active_tenths))
            # 一次跨了好几级（后台手动加）也只写一条，写的是最后到的那一级
            if level // every > row.account_level // every:
                db.add(
                    AccountLevelNotification(
                        user_id=row.id,
                        level=level,
                        title=f"账号等级升到了 {level} 级",
                        body=f"累计活跃 {_to_days(row.account_active_tenths)} 天，等级图标是 {_icons_text(level)}。",
                        created_at=now,
                    )
                )
    return ups


# ---------------------------------------------------------------- 升级之后：实时事件、商店钩子


async def _shop_on_level_up(db: AsyncSession, user_id: int, level: int) -> None:
    """升级时通知商店：模块存在才调 app.services.shop.on_level_up(db, user_id, level)，失败只记日志。

    在结算的事务提交之后调用。钩子可以自己提交；没提交的改动这里补一次提交，抛了异常就回滚。
    """
    try:
        shop = importlib.import_module(SHOP_MODULE)
    except ModuleNotFoundError as exc:
        if exc.name != SHOP_MODULE:
            logger.exception("Failed to import shop module for level-up hook")
        return
    except Exception:
        logger.exception("Failed to import shop module for level-up hook")
        return
    hook = getattr(shop, "on_level_up", None)
    if hook is None:
        return
    try:
        result = hook(db, user_id, level)
        if inspect.isawaitable(result):
            await result
        await db.commit()
    except Exception:
        logger.exception("Shop level-up hook failed", extra={"user_id": user_id, "level": level})
        try:
            await db.rollback()
        except Exception:
            logger.exception("Rollback after shop level-up hook failed")


async def announce_level_ups(db: AsyncSession, ups: list[LevelUp]) -> None:
    """升级已经落库之后调用：给本人发 account.level.up，再调商店的钩子。哪一步失败都只记日志，不影响结算。"""
    if not ups:
        return
    from app.websocket.manager import manager

    for up in ups:
        try:
            await manager.send_to_user(
                up.user_id,
                "account.level.up",
                {"level": up.level, "icons": icons_for_level(up.level), "active_days": _to_days(up.active_tenths)},
            )
        except Exception:
            logger.exception("Failed to send account level event", extra={"user_id": up.user_id, "level": up.level})
        await _shop_on_level_up(db, up.user_id, up.level)


# ---------------------------------------------------------------- 后台循环


async def run_tick(db: AsyncSession, *, now: datetime | None = None, online_ids: Iterable[int] | None = None) -> dict[str, int]:
    """一个时间片：给现在在线的人记在线时长，再给这批人结算。"""
    now = _moment(now)
    if online_ids is None:
        from app.websocket.manager import manager

        online_ids = await manager.online_user_ids_global(fresh=True)
    user_ids, credited = await add_online_minutes(db, online_ids, now=now)
    stats = {"online": len(user_ids), "credited": credited, "level_ups": 0}
    # 一行都没记上 = 这个时间片别的 worker 已经记过、也结算过了
    if not credited:
        return stats
    ups = await settle_users(db, user_ids, now=now)
    stats["level_ups"] = len(ups)
    await announce_level_ups(db, ups)
    return stats


async def finalize_day(db: AsyncSession, day: date, *, now: datetime | None = None) -> int:
    """给已经过去的一天收尾：在线时长够得上基础的人再结算一次。

    在线循环只结算当时在线的人；下线之后才满足的加成（最后几分钟发的言、短暂上线签的到）靠这里补上。可重复执行。
    """
    now = _moment(now)
    result = await db.execute(select(_day_t.c.user_id).where(_day_t.c.day == day, _day_t.c.online_minutes >= rules().min_online_minutes))
    user_ids = sorted(result.scalars().all())
    level_ups = 0
    for chunk in _chunks(user_ids):
        ups = await settle_users(db, chunk, day=day, now=now)
        level_ups += len(ups)
        await announce_level_ups(db, ups)
    return level_ups


def seconds_until_next_tick(now_seconds: float | None = None) -> float:
    """睡到下一个时间片开始后两秒。对齐到时间片边界，这样每个时间片恰好跑一次，不会因为循环本身的耗时慢慢漂过一个时间片。"""
    tick = settings.account_level_tick_seconds
    now_seconds = time.time() if now_seconds is None else now_seconds
    return tick - (now_seconds % tick) + 2.0


_worker_task: asyncio.Task[None] | None = None
_backfill_done = False
_finalized_day: date | None = None
WORKER_STARTUP_DELAY_SECONDS = 5.0


async def _backfill_job(db: AsyncSession) -> None:
    global _backfill_done
    if not _backfill_done:
        _backfill_done = await backfill_legacy(db)


async def _finalize_job(db: AsyncSession) -> None:
    """进程启动后、以及每次跨天后的第一个时间片，给昨天收尾。"""
    global _finalized_day
    now = datetime.now(UTC)
    yesterday = local_day(now) - timedelta(days=1)
    if _finalized_day != yesterday:
        await finalize_day(db, yesterday, now=now)
        _finalized_day = yesterday


async def _tick_job(db: AsyncSession) -> None:
    await run_tick(db)


async def _account_level_worker() -> None:
    delay = WORKER_STARTUP_DELAY_SECONDS
    while True:
        await asyncio.sleep(delay)
        for name, job in (("legacy backfill", _backfill_job), ("day finalize", _finalize_job), ("online tick", _tick_job)):
            try:
                async with AsyncSessionLocal() as db:
                    await job(db)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Account level %s failed", name)
        delay = seconds_until_next_tick()


def start_account_level_worker() -> None:
    global _worker_task
    if _worker_task is not None and not _worker_task.done():
        return
    _worker_task = asyncio.create_task(_account_level_worker())


async def stop_account_level_worker() -> None:
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


# ---------------------------------------------------------------- 老用户折算


async def _resync_all_levels(db: AsyncSession) -> None:
    """按累计把所有人的等级重写一遍，一条语句。在调用方的事务里执行。"""
    highest = (await db.execute(select(func.max(_user_t.c.account_active_tenths)))).scalar_one_or_none() or 0
    top = _level_for_tenths(highest)
    if top <= 0:
        return
    tiers = [(_user_t.c.account_active_tenths >= days_for_level(level) * 10, level) for level in range(top, 0, -1)]
    await db.execute(
        _user_t.update().where(_user_t.c.account_active_tenths > 0).values(account_level=case(*tiers, else_=0), updated_at=_user_t.c.updated_at)
    )


async def backfill_legacy(db: AsyncSession, *, now: datetime | None = None) -> bool:
    """老用户的起点（docs/account-level.md 1.3）：今天之前，有过群发言或群签到记录的每一天算 1 个活跃天。

    只做一次，做没做过记在 system_settings 里。两个 worker 同时启动时：先插标记的那个拿到写锁，
    标记和折算在同一个事务里提交；另一个等锁放开后发现标记已经在了，什么都不做。
    返回 True 表示折算已经完成（这次做的，或者之前就做过）。不发升级事件、不写通知。
    """
    now = _moment(now)
    if (await db.execute(select(SystemSetting.key).where(SystemSetting.key == BACKFILL_KEY))).scalar_one_or_none() is not None:
        return True
    setting_t = SystemSetting.__table__
    activity_t = GroupMemberDailyActivity.__table__
    try:
        claimed = await db.execute(
            upsert_insert(db, setting_t).values(key=BACKFILL_KEY, value=None, created_at=now, updated_at=now).on_conflict_do_nothing(index_elements=[setting_t.c.key])
        )
        if claimed.rowcount != 1:
            await db.rollback()
            return True
        active = or_(activity_t.c.message_count > 0, activity_t.c.checkin_count > 0)
        # 今天的不折算：今天的活跃天按新规则实时攒
        before = activity_t.c.activity_date < local_day(now)
        active_days = (
            select(func.count(distinct(activity_t.c.activity_date))).where(activity_t.c.user_id == _user_t.c.id, active, before).scalar_subquery()
        )
        result = await db.execute(
            _user_t.update()
            .where(_user_t.c.is_bot.is_(False), _user_t.c.id.in_(select(activity_t.c.user_id).where(active, before)))
            .values(account_active_tenths=_user_t.c.account_active_tenths + 10 * active_days, updated_at=_user_t.c.updated_at)
        )
        await _resync_all_levels(db)
        await db.execute(
            setting_t.update()
            .where(setting_t.c.key == BACKFILL_KEY)
            .values(value=json.dumps({"at": now.isoformat(), "users": int(result.rowcount or 0)}), updated_at=now)
        )
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    reset_leaderboard_cache()
    logger.info("Account level legacy backfill done", extra={"users": int(result.rowcount or 0)})
    return True


# ---------------------------------------------------------------- 排行榜


@dataclass
class _Board:
    tick: float
    generated_at: datetime
    # period -> [(user_id, level, active_tenths, gained_tenths)]，已排好序
    rows: dict[str, list[tuple[int, int, int, int]]] = field(default_factory=dict)
    # period -> {user_id: 在 rows 里的下标}
    index: dict[str, dict[int, int]] = field(default_factory=dict)


LEADERBOARD_PERIODS = ("all", "month")
_board: _Board | None = None


def reset_leaderboard_cache() -> None:
    global _board
    _board = None


def _is_banned(user_status: UserStatus, banned_until: datetime | None, now: datetime) -> bool:
    """封禁中：状态是 disabled，且没有到期时间（永久）或还没到期。到期的封禁要等本人下次登录才会改回状态。"""
    if user_status != UserStatus.DISABLED:
        return False
    until = _aware(banned_until)
    return until is None or until > now


async def _build_board(db: AsyncSession, now: datetime) -> _Board:
    today = local_day(now)
    this_month = (_day_t.c.day >= today.replace(day=1), _day_t.c.day <= today, _day_t.c.earned_tenths > 0)
    gained_rows = await db.execute(select(_day_t.c.user_id, func.sum(_day_t.c.earned_tenths)).where(*this_month).group_by(_day_t.c.user_id))
    gained = {user_id: int(total or 0) for user_id, total in gained_rows.all()}
    result = await db.execute(
        select(User.id, User.account_level, User.account_active_tenths, User.created_at, User.status, User.banned_until).where(
            User.is_bot.is_(False),
            or_(User.account_active_tenths > 0, User.id.in_(select(_day_t.c.user_id).where(*this_month))),
        )
    )
    people = [row for row in result.all() if not _is_banned(row.status, row.banned_until, now)]
    board = _Board(tick=time.monotonic(), generated_at=now)
    for period in LEADERBOARD_PERIODS:
        # 并列时注册早的在前
        if period == "all":
            ranked = sorted((row for row in people if row.account_active_tenths > 0), key=lambda row: (-row.account_active_tenths, row.created_at, row.id))
        else:
            ranked = sorted((row for row in people if gained.get(row.id, 0) > 0), key=lambda row: (-gained[row.id], row.created_at, row.id))
        board.rows[period] = [(row.id, row.account_level, row.account_active_tenths, gained.get(row.id, 0)) for row in ranked]
        board.index[period] = {row[0]: position for position, row in enumerate(board.rows[period])}
    return board


async def _get_board(db: AsyncSession, now: datetime | None = None) -> _Board:
    """名次在进程内缓存（默认 60 秒）。两种周期一起算、一起过期。"""
    global _board
    board = _board
    if board is None or time.monotonic() - board.tick >= settings.account_level_leaderboard_cache_seconds:
        board = await _build_board(db, _moment(now))
        _board = board
    return board


async def rank_of(db: AsyncSession, user_id: int, *, now: datetime | None = None) -> int | None:
    """全站名次（按累计活跃天），和排行榜用同一份缓存。没有活跃天或不上榜的为 None。"""
    position = (await _get_board(db, now)).index["all"].get(user_id)
    return None if position is None else position + 1


def _board_item(rank: int, row: tuple[int, int, int, int], user: User) -> dict[str, Any]:
    _user_id, level, tenths, gained = row
    return {"rank": rank, "user": UserPublic.model_validate(user), "level": level, "active_days": _to_days(tenths), "gained": _to_days(gained)}


async def leaderboard(
    db: AsyncSession, user: User, *, period: str = "all", limit: int = 50, offset: int = 0, now: datetime | None = None
) -> dict[str, Any]:
    """排行榜。名次和数字来自缓存；这一页的用户资料、「我在第几」每次请求现取。"""
    board = await _get_board(db, now)
    rows = board.rows[period]
    page = rows[offset : offset + limit]
    people: dict[int, User] = {}
    if page:
        result = await db.execute(select(User).where(User.id.in_([row[0] for row in page])))
        people = {person.id: person for person in result.scalars().all()}
    items = [_board_item(offset + position + 1, row, people[row[0]]) for position, row in enumerate(page) if row[0] in people]
    mine = board.index[period].get(user.id)
    return {
        "period": period,
        "items": items,
        "total": len(rows),
        "me": None if mine is None else _board_item(mine + 1, rows[mine], user),
        "generated_at": board.generated_at,
    }


# ---------------------------------------------------------------- 等级卡


def registered_days(created_at: datetime | None, today: date) -> int:
    """注册到今天的自然日数（UTC+8），注册当天算第 1 天。"""
    created = _aware(created_at)
    if created is None:
        return 1
    return max(1, (today - local_day(created)).days + 1)


def _progress(level: int, tenths: int, max_level: int) -> dict[str, Any]:
    if level >= max_level:
        return {"level_days": days_for_level(level), "next_level": None, "next_level_days": None, "days_to_next": None}
    next_days = days_for_level(level + 1)
    return {
        "level_days": days_for_level(level),
        "next_level": level + 1,
        "next_level_days": next_days,
        "days_to_next": _to_days(max(0, next_days * 10 - tenths)),
    }


def _history_item(day: date, row: Any | None) -> dict[str, Any]:
    if row is None:
        return {"date": day, "online_minutes": 0, "base": 0.0, "bonus": 0.0, "earned": 0.0}
    return {
        "date": day,
        "online_minutes": row.online_minutes,
        "base": _to_days(row.base_tenths),
        # 加成 = 当天记入的合计 - 基础（合计已经按每日上限截过）
        "bonus": _to_days(max(0, row.earned_tenths - row.base_tenths)),
        "earned": _to_days(row.earned_tenths),
    }


async def _user_row(db: AsyncSession, user_id: int) -> Any | None:
    result = await db.execute(
        select(
            _user_t.c.id, _user_t.c.account_level, _user_t.c.account_active_tenths, _user_t.c.account_streak_days, _user_t.c.account_streak_day, _user_t.c.created_at
        ).where(_user_t.c.id == user_id)
    )
    return result.first()


async def my_level(db: AsyncSession, user: User, *, now: datetime | None = None) -> dict[str, Any]:
    """GET /levels/me：先给本人结算今天，再返回等级、进度和今天的明细。"""
    now = _moment(now)
    today = local_day(now)
    r = rules()
    # 先把 id 取出来：下面任何一步回滚都会让会话里的 ORM 对象过期，之后再碰 user 的属性会触发同步加载
    user_id = user.id
    counts = await _message_counts(db, [user_id], today, now)
    try:
        ups = await settle_users(db, [user_id], day=today, now=now, message_counts=counts)
        await announce_level_ups(db, ups)
        if ups:
            # 请求里的 user 是结算之前读出来的
            await db.refresh(user)
    except Exception:
        # 结算失败（比如库正忙）不该让等级卡打不开；在线循环下一轮会补上
        logger.exception("Account level settle on read failed", extra={"user_id": user_id})
    me = await _user_row(db, user_id)
    row = (await _load_day_rows(db, [user_id], today)).get(user_id)
    streak = current_streak(me.account_streak_days, me.account_streak_day, today)
    message_count, _night = _message_total(counts.get(user_id), row.message_count if row is not None else 0, row.night_messages if row is not None else 0)
    extras: dict[str, dict[str, int]] = {
        "messages": {"current": message_count, "target": r.messages_target},
        "streak_7": {"current": streak, "target": r.streak_short_target},
        "streak_30": {"current": streak, "target": r.streak_long_target},
    }
    bonuses = [
        {"key": key, "earned": bool(row is not None and getattr(row, f"bonus_{key}")), "days": _to_days(r.bonus[key]), **extras.get(key, {})}
        for key in BONUS_KEYS
    ]
    return {
        "level": me.account_level,
        "active_days": _to_days(me.account_active_tenths),
        **_progress(me.account_level, me.account_active_tenths, r.max_level),
        "icons": icons_for_level(me.account_level),
        "streak": streak,
        "rank": await rank_of(db, user_id, now=now),
        "today": {
            "date": today,
            "online_minutes": row.online_minutes if row is not None else 0,
            "base": _to_days(row.base_tenths) if row is not None else 0.0,
            "bonuses": bonuses,
            "earned": _to_days(row.earned_tenths) if row is not None else 0.0,
            "cap": _to_days(r.cap),
        },
        "registered_days": registered_days(me.created_at, today),
    }


async def user_level(db: AsyncSession, user_id: int, *, now: datetime | None = None) -> dict[str, Any]:
    """GET /users/{user_id}/level：别人的等级卡，不含当天的明细，也不触发结算。"""
    now = _moment(now)
    today = local_day(now)
    row = await _user_row(db, user_id)
    if row is None:
        raise HTTPException(status_code=404, detail={"code": "user_not_found", "message": "用户不存在"})
    progress = _progress(row.account_level, row.account_active_tenths, rules().max_level)
    return {
        "user_id": row.id,
        "level": row.account_level,
        "active_days": _to_days(row.account_active_tenths),
        "icons": icons_for_level(row.account_level),
        "next_level_days": progress["next_level_days"],
        "days_to_next": progress["days_to_next"],
        "streak": current_streak(row.account_streak_days, row.account_streak_day, today),
        "rank": await rank_of(db, row.id, now=now),
        "registered_days": registered_days(row.created_at, today),
    }


async def _recent_days(db: AsyncSession, user_ids: list[int], days: int, today: date) -> dict[int, list[dict[str, Any]]]:
    """这些人最近 days 天（含今天）每天的明细，按日期正序，没有记录的日子补 0。"""
    first = today - timedelta(days=days - 1)
    found: dict[tuple[int, date], Any] = {}
    for chunk in _chunks(user_ids):
        result = await db.execute(select(_day_t).where(_day_t.c.user_id.in_(chunk), _day_t.c.day >= first, _day_t.c.day <= today))
        for row in result.all():
            found[(row.user_id, row.day)] = row
    return {
        user_id: [_history_item(first + timedelta(days=offset), found.get((user_id, first + timedelta(days=offset)))) for offset in range(days)]
        for user_id in user_ids
    }


async def history(db: AsyncSession, user_id: int, *, days: int = 30, now: datetime | None = None) -> dict[str, Any]:
    """GET /levels/history。"""
    today = local_day(_moment(now))
    return {"items": (await _recent_days(db, [user_id], days, today))[user_id]}


# ---------------------------------------------------------------- 通知中心


async def notification_items(db: AsyncSession, user_id: int) -> list[UnifiedNotificationRead]:
    """通知中心里的升级通知（type="account_level"，走 system 类别的通用样式）。"""
    cutoff = datetime.now(UTC) - timedelta(days=30)
    table = AccountLevelNotification.__table__
    # 按列读而不是读对象：时间要和通知中心里其他来源一样是库里读出来的原样（不带时区），那边会把所有通知放在一起按时间排序
    result = await db.execute(
        select(table.c.id, table.c.level, table.c.title, table.c.body, table.c.created_at)
        .where(table.c.user_id == user_id, table.c.created_at >= cutoff)
        .order_by(table.c.id.desc())
        .limit(30)
    )
    return [
        UnifiedNotificationRead(
            id=f"account_level:{notification.id}",
            category="system",
            type="account_level",
            title=notification.title,
            body=notification.body,
            time=notification.created_at,
            unread=False,
            pending=False,
            action="none",
            payload={"level": notification.level, "icons": icons_for_level(notification.level)},
        )
        for notification in result.all()
    ]


# ---------------------------------------------------------------- 后台


ADMIN_RECENT_DAYS = 7


async def _admin_items(db: AsyncSession, users: list[User], today: date) -> list[dict[str, Any]]:
    recent = await _recent_days(db, [user.id for user in users], ADMIN_RECENT_DAYS, today)
    return [
        {
            "user": UserPublic.model_validate(user),
            "level": user.account_level,
            "active_days": _to_days(user.account_active_tenths),
            "streak": current_streak(user.account_streak_days, user.account_streak_day, today),
            "recent": recent[user.id],
        }
        for user in users
    ]


async def admin_list_users(
    db: AsyncSession, *, q: str | None = None, limit: int = 50, offset: int = 0, now: datetime | None = None
) -> tuple[list[dict[str, Any]], int]:
    """按用户查等级。q 匹配用户名 / 昵称（包含），纯数字时也匹配用户 id；不带 q 按累计活跃天从高到低。"""
    today = local_day(_moment(now))
    conditions = []
    keyword = (q or "").strip()
    if keyword:
        pattern = "%" + keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        matches = [User.username.ilike(pattern, escape="\\"), User.nickname.ilike(pattern, escape="\\")]
        if keyword.isdigit():
            matches.append(User.id == int(keyword))
        conditions.append(or_(*matches))
    total = int((await db.execute(select(func.count(User.id)).where(*conditions))).scalar_one() or 0)
    result = await db.execute(
        select(User).where(*conditions).order_by(User.account_active_tenths.desc(), User.id.asc()).offset(offset).limit(limit).execution_options(populate_existing=True)
    )
    return await _admin_items(db, list(result.scalars().all()), today), total


async def admin_adjust(db: AsyncSession, admin: User, user_id: int, *, days: float, note: str | None = None, now: datetime | None = None) -> dict[str, Any]:
    """手动增减活跃天。累计不低于 0（审计日志里记实际变动的数）；等级跟着重算，可能往下掉。"""
    now = _moment(now)
    # 下面重试时的回滚会让 admin 这个 ORM 对象过期，先把 id 取出来
    admin_id = admin.id
    delta = _to_tenths(days)
    if delta == 0:
        raise HTTPException(status_code=422, detail={"code": "invalid_days", "message": "调整的天数不能为 0，最小 0.1 天"})
    try:
        # 在线循环随时可能给这个人加活跃天：带着读到的旧值更新，没更新上就重读再来
        for _attempt in range(5):
            row = await _user_row(db, user_id)
            if row is None:
                raise HTTPException(status_code=404, detail={"code": "user_not_found", "message": "用户不存在"})
            before = row.account_active_tenths
            after = max(0, before + delta)
            result = await db.execute(
                _user_t.update()
                .where(_user_t.c.id == user_id, _user_t.c.account_active_tenths == before)
                .values(account_active_tenths=after, updated_at=_user_t.c.updated_at)
            )
            if result.rowcount == 1:
                break
            await db.rollback()
        else:
            raise HTTPException(status_code=409, detail={"code": "level_busy", "message": "这个用户的活跃天正在变化，请重试"})
        ups = await _sync_levels(db, [user_id], now, allow_down=True)
        level_after = _level_for_tenths(after)
        db.add(
            AdminAuditLog(
                actor_id=admin_id,
                action="account_level.adjust",
                target_type="user",
                target_id=user_id,
                details=json.dumps(
                    {
                        "days": _to_days(delta),
                        "applied": _to_days(after - before),
                        "before": _to_days(before),
                        "after": _to_days(after),
                        "level_before": row.account_level,
                        "level_after": level_after,
                        "note": (note or "").strip() or None,
                    },
                    ensure_ascii=False,
                ),
            )
        )
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    reset_leaderboard_cache()
    await announce_level_ups(db, ups)
    user = (await db.execute(select(User).where(User.id == user_id).execution_options(populate_existing=True))).scalar_one()
    return (await _admin_items(db, [user], local_day(now)))[0]


async def admin_settings(db: AsyncSession) -> dict[str, Any]:
    """全部阈值（只读）。"""
    r = rules()
    marker = (await db.execute(select(SystemSetting.value).where(SystemSetting.key == BACKFILL_KEY))).scalar_one_or_none()
    backfill_at: str | None = None
    if marker:
        try:
            backfill_at = json.loads(marker).get("at")
        except (TypeError, ValueError, AttributeError):
            backfill_at = None
    return {
        **rules_payload(),
        "tick_seconds": r.tick_seconds,
        "tick_minutes": r.tick_minutes,
        "leaderboard_cache_seconds": settings.account_level_leaderboard_cache_seconds,
        "notify_every": settings.account_level_notify_every,
        "legacy_backfill_at": backfill_at,
    }
