"""好友之间的「火花」（docs/sparks.md）。

规则（按 UTC+8 自然日，常量都在下面，改数值只改这里）：

- 只在「现在是好友」的两个人的私聊里有火花。某天双方都发过至少一条消息（系统消息不算，戳一戳也是系统消息）＝这一天续上了，
  连续天数 +1（一天只加一次）。只有一方发了不算。
- 断火：昨天没续上，今天「快熄灭了」，今天续上照样 +1；前天、昨天都没续上就熄灭，天数清零（原来的天数记在 lost_days）。
- 熄灭后 RELIGHT_WINDOW_DAYS 天内任何一方可以花 RELIGHT_COST 金币重燃，恢复原来的天数（熄灭后又重新聊起来的，恢复成原来的 + 新的）；
  每对好友 RELIGHT_COOLDOWN_DAYS 天内只能重燃一次。扣币走商店余额那一套（spark_relights 进余额公式）。
- 等级按当前连续天数（LEVELS）。升到新等级（MILESTONE_MIN_LEVEL 起）的那一刻在私聊里发一条系统提示。
- 今天唯一的一条发言被撤回：今天的发言、续火都撤回去（更早的日子不追溯）。

记账和亲密度一样：发消息的接口提交之后起一个任务，用自己的会话记、发事件，失败只记日志，不拖慢发送。
同一个人当天已经发过言之后的消息只读一行就返回，不写库。
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable

from fastapi import HTTPException, status
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import localtime
from app.core.dialect import lock_for_update, upsert_insert
from app.integrations import get_coin_ledger
from app.models.conversation import Conversation
from app.models.enums import ConversationType, MessageType
from app.models.friend import Friendship
from app.models.message import Message
from app.models.shop import ShopAccount
from app.models.spark import FriendSpark, SparkRelight
from app.models.user import User
from app.services import response_cache
from app.services import shop as shop_service

logger = logging.getLogger(__name__)
UTC = timezone.utc

# ---------------------------------------------------------------- 规则常量


@dataclass(frozen=True)
class SparkLevel:
    level: int
    name: str
    # 客户端按 key 画图标、挑颜色
    key: str
    min_days: int


LEVELS: tuple[SparkLevel, ...] = (
    SparkLevel(1, "火苗", "ember", 1),
    SparkLevel(2, "小火花", "spark", 3),
    SparkLevel(3, "火花", "flame", 7),
    SparkLevel(4, "大火花", "blaze", 30),
    SparkLevel(5, "烈焰", "inferno", 100),
    SparkLevel(6, "永恒之火", "eternal", 365),
)
# 断几天还能救：漏 1 天＝快熄灭（今天续上照样接着数），漏 2 天＝熄灭
GRACE_MISSED_DAYS = 1
RELIGHT_COST = 100
RELIGHT_WINDOW_DAYS = 3
RELIGHT_COOLDOWN_DAYS = 30
# 从这一级起升级时在私聊里发系统提示（1 级「火苗」是第一次互相聊天，不打扰）
MILESTONE_MIN_LEVEL = 2
# 不算「发言」的消息
UNCOUNTED_TYPES = (MessageType.SYSTEM,)
SWEEP_SECONDS = 600
SWEEP_BATCH = 500
EVENT_UPDATED = "spark.updated"
SYSTEM_EVENT_LEVEL_UP = "spark.level_up"
SYSTEM_EVENT_RELIT = "spark.relit"

STATUS_NONE, STATUS_LIT, STATUS_PENDING, STATUS_DYING, STATUS_OUT = "none", "lit", "pending", "dying", "out"


def _now() -> datetime:
    return localtime._now()


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _local_date(value: datetime) -> date:
    return _aware(value).astimezone(localtime.LOCAL_TZ).date()


def _midnight(day: date) -> datetime:
    """UTC+8 那天的零点（UTC）。"""
    return datetime(day.year, day.month, day.day, tzinfo=localtime.LOCAL_TZ).astimezone(UTC)


def _pair(a: int, b: int) -> tuple[int, int]:
    return (a, b) if a < b else (b, a)


def _side(spark: FriendSpark, user_id: int) -> str:
    return "low" if user_id == spark.user_low_id else "high"


def _other(side: str) -> str:
    return "high" if side == "low" else "low"


def _error(status_code: int, code: str, message: str, **extra: Any) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message, **extra})


# ---------------------------------------------------------------- 纯函数


def level_for(days: int) -> SparkLevel | None:
    reached = None
    for item in LEVELS:
        if days >= item.min_days:
            reached = item
    return reached


def next_level(days: int) -> SparkLevel | None:
    for item in LEVELS:
        if days < item.min_days:
            return item
    return None


def missed_days(lit_on: date | None, today: date) -> int | None:
    """最后一次续上之后、今天之前，漏了几天。从没续上过是 None。"""
    if lit_on is None:
        return None
    return max(0, (today - lit_on).days - 1)


def status_of(spark: FriendSpark | None, today: date) -> str:
    if spark is None or spark.lit_on is None:
        return STATUS_NONE
    if spark.lit_on >= today:
        return STATUS_LIT
    missed = missed_days(spark.lit_on, today)
    if missed == 0:
        return STATUS_PENDING
    if missed <= GRACE_MISSED_DAYS:
        return STATUS_DYING
    return STATUS_OUT


def current_days(spark: FriendSpark | None, today: date) -> int:
    if spark is None or status_of(spark, today) in (STATUS_NONE, STATUS_OUT):
        return 0
    return int(spark.streak_days or 0)


def out_day(lit_on: date) -> date:
    """lit_on 之后一直没续上的话，哪一天熄灭（那天零点起）。"""
    return lit_on + timedelta(days=GRACE_MISSED_DAYS + 2)


def restorable(spark: FriendSpark | None, today: date) -> tuple[int, date] | None:
    """能恢复的天数和熄灭的那一天。还没被扫描清零的熄灭优先；否则看上次记下的 lost_days。"""
    if spark is None:
        return None
    if status_of(spark, today) == STATUS_OUT and spark.streak_days > 0:
        return int(spark.streak_days), out_day(spark.lit_on)
    if spark.lost_days > 0 and spark.extinguished_at is not None:
        return int(spark.lost_days), _local_date(spark.extinguished_at)
    return None


def relight_state(spark: FriendSpark | None, today: date, now: datetime) -> dict[str, Any]:
    """能不能重燃，不能的话为什么（not_out / expired / cooldown）。"""
    info = restorable(spark, today)
    cooldown_until = None
    if spark is not None and spark.relit_at is not None:
        cooldown_until = _aware(spark.relit_at) + timedelta(days=RELIGHT_COOLDOWN_DAYS)
    if info is None:
        return {"can": False, "block": "not_out", "lost_days": 0, "deadline": None, "cooldown_until": cooldown_until}
    lost, extinguished_on = info
    deadline = _midnight(extinguished_on + timedelta(days=RELIGHT_WINDOW_DAYS))
    if now >= deadline:
        return {"can": False, "block": "expired", "lost_days": lost, "deadline": deadline, "cooldown_until": cooldown_until}
    if cooldown_until is not None and now < cooldown_until:
        return {"can": False, "block": "cooldown", "lost_days": lost, "deadline": deadline, "cooldown_until": cooldown_until}
    return {"can": True, "block": None, "lost_days": lost, "deadline": deadline, "cooldown_until": cooldown_until}


def continue_on(spark: FriendSpark, day: date) -> int | None:
    """day 这一天双方都发过言了：续上。返回升到的新等级（没升级是 None）。调用方保证 spark.lit_on != day。"""
    snapshot = {
        "day": day.isoformat(),
        "streak": spark.streak_days,
        "lit_on": spark.lit_on.isoformat() if spark.lit_on else None,
        "best": spark.best_streak,
        "level": spark.level,
        "lost": spark.lost_days,
        "extinguished_at": _aware(spark.extinguished_at).isoformat() if spark.extinguished_at else None,
    }
    missed = missed_days(spark.lit_on, day)
    if missed is not None and missed <= GRACE_MISSED_DAYS and spark.streak_days > 0:
        spark.streak_days += 1
    else:
        if spark.lit_on is not None and spark.streak_days > 0:
            # 熄灭之后还没被扫描清零：这里补记熄灭
            spark.lost_days = spark.streak_days
            spark.extinguished_at = _midnight(out_day(spark.lit_on))
        spark.streak_days = 1
    spark.lit_on = day
    spark.best_streak = max(int(spark.best_streak or 0), spark.streak_days)
    before = int(spark.level or 0)
    reached = level_for(spark.streak_days)
    spark.level = reached.level if reached else 0
    spark.undo_json = json.dumps(snapshot)
    return spark.level if spark.level > before else None


def undo_today(spark: FriendSpark, day: date) -> bool:
    """撤回 day 这一天的续火（今天唯一一条发言被撤回了）。返回有没有撤。"""
    if spark.lit_on != day or not spark.undo_json:
        return False
    try:
        snapshot = json.loads(spark.undo_json)
    except ValueError:
        return False
    if snapshot.get("day") != day.isoformat():
        return False
    spark.streak_days = int(snapshot.get("streak") or 0)
    spark.lit_on = date.fromisoformat(snapshot["lit_on"]) if snapshot.get("lit_on") else None
    spark.best_streak = int(snapshot.get("best") or 0)
    spark.level = int(snapshot.get("level") or 0)
    spark.lost_days = int(snapshot.get("lost") or 0)
    spark.extinguished_at = datetime.fromisoformat(snapshot["extinguished_at"]) if snapshot.get("extinguished_at") else None
    spark.undo_json = None
    return True


def _level_dict(item: SparkLevel | None) -> dict[str, Any]:
    return {"level": item.level if item else 0, "level_name": item.name if item else None, "level_key": item.key if item else None}


def view(spark: FriendSpark | None, viewer_id: int, today: date, now: datetime) -> dict[str, Any]:
    """viewer 看到的这一对的火花。"""
    state = status_of(spark, today)
    days = current_days(spark, today)
    reached = level_for(days)
    upcoming = next_level(days)
    relight = relight_state(spark, today, now)
    mine = theirs = False
    if spark is not None:
        side = _side(spark, viewer_id)
        mine = getattr(spark, f"{side}_spoke_on") == today
        theirs = getattr(spark, f"{_other(side)}_spoke_on") == today
    extinguished_at = None
    if state == STATUS_OUT and spark is not None:
        extinguished_at = _midnight(out_day(spark.lit_on))
    elif spark is not None and spark.extinguished_at is not None:
        extinguished_at = _aware(spark.extinguished_at)
    lost_level = level_for(relight["lost_days"]) if relight["lost_days"] else None
    return {
        "status": state,
        "days": days,
        **_level_dict(reached),
        "today_lit": state == STATUS_LIT,
        "i_spoke_today": mine,
        "friend_spoke_today": theirs,
        "dying": state == STATUS_DYING,
        "best_days": max(int(spark.best_streak or 0) if spark else 0, days),
        "next_level": upcoming.level if upcoming else None,
        "next_level_name": upcoming.name if upcoming else None,
        "next_level_days": upcoming.min_days if upcoming else None,
        "days_to_next": upcoming.min_days - days if upcoming else None,
        "lit_on": spark.lit_on if spark else None,
        "extinguished_at": extinguished_at,
        "lost_days": relight["lost_days"],
        "lost_level_key": lost_level.key if lost_level else None,
        "can_relight": relight["can"],
        "relight_block": relight["block"],
        "relight_cost": RELIGHT_COST,
        "relight_deadline": relight["deadline"],
        "relight_cooldown_until": relight["cooldown_until"],
        "relit_at": _aware(spark.relit_at) if spark else None,
    }


def worth_showing(item: dict[str, Any]) -> bool:
    """会话列表 / 好友列表里要不要画：有天数，或者熄灭了还在能重燃的期限里。"""
    return item["days"] > 0 or (item["lost_days"] > 0 and item["relight_block"] != "expired" and item["relight_deadline"] is not None)


def rules_view() -> dict[str, Any]:
    return {
        "timezone": "UTC+8",
        "levels": [{"level": item.level, "name": item.name, "key": item.key, "min_days": item.min_days} for item in LEVELS],
        "grace_missed_days": GRACE_MISSED_DAYS,
        "relight_cost": RELIGHT_COST,
        "relight_window_days": RELIGHT_WINDOW_DAYS,
        "relight_cooldown_days": RELIGHT_COOLDOWN_DAYS,
        "rules": [
            "只有好友之间的私聊有火花，按北京时间（UTC+8）自然日算。",
            "同一天里你们双方都至少发了一条消息，就算续上了，火花天数 +1（一天只加一次）。只有一方发不算。",
            "系统消息、戳一戳不算；当天发的消息撤回了也不算。",
            f"昨天没续上，火花就快熄灭了，今天续上还能接着数；连续 {GRACE_MISSED_DAYS + 1} 天没续上，火花熄灭，天数清零。",
            f"熄灭后 {RELIGHT_WINDOW_DAYS} 天内，任何一方都可以花 {RELIGHT_COST} 金币重燃火花，恢复原来的天数；每对好友 {RELIGHT_COOLDOWN_DAYS} 天内只能重燃一次。",
        ],
    }


# ---------------------------------------------------------------- 查询


async def are_friends(db: AsyncSession, a: int, b: int) -> bool:
    found = await db.scalar(select(Friendship.id).where(Friendship.user_id == a, Friendship.friend_id == b).limit(1))
    return found is not None


async def _spark_of(db: AsyncSession, low: int, high: int, *, fresh: bool = False) -> FriendSpark | None:
    statement = select(FriendSpark).where(FriendSpark.user_low_id == low, FriendSpark.user_high_id == high)
    if fresh:
        statement = statement.execution_options(populate_existing=True)
    return (await db.execute(statement)).scalar_one_or_none()


async def _direct_conversation_id(db: AsyncSession, low: int, high: int) -> int | None:
    return await db.scalar(
        select(Conversation.id).where(Conversation.type == ConversationType.DIRECT, Conversation.direct_key == f"{low}:{high}").limit(1)
    )


async def sparks_with(db: AsyncSession, user_id: int, peer_ids: Iterable[int]) -> dict[int, FriendSpark]:
    """user 和这些人之间的火花行，按对方 id。"""
    peers = sorted(set(peer_ids) - {user_id})
    if not peers:
        return {}
    found: dict[int, FriendSpark] = {}
    for start in range(0, len(peers), 400):
        chunk = peers[start : start + 400]
        higher = [peer for peer in chunk if peer > user_id]
        lower = [peer for peer in chunk if peer < user_id]
        clauses = []
        if higher:
            clauses.append(and_(FriendSpark.user_low_id == user_id, FriendSpark.user_high_id.in_(higher)))
        if lower:
            clauses.append(and_(FriendSpark.user_high_id == user_id, FriendSpark.user_low_id.in_(lower)))
        rows = await db.execute(select(FriendSpark).where(or_(*clauses)))
        for spark in rows.scalars():
            found[spark.user_high_id if spark.user_low_id == user_id else spark.user_low_id] = spark
    return found


async def attach_to_reads(db: AsyncSession, reads: list[Any], current_user_id: int) -> None:
    """会话列表的读模型（ConversationRead）：好友私聊带上 spark（没有可画的为 null）。出错只记日志，不影响会话列表。"""
    try:
        targets = [read for read in reads if getattr(read, "is_friend", False) and getattr(read, "direct_user", None) is not None]
        if not targets:
            return
        sparks = await sparks_with(db, current_user_id, [read.direct_user.id for read in targets])
        if not sparks:
            return
        now = _now()
        today = localtime.local_today(now)
        for read in targets:
            spark = sparks.get(read.direct_user.id)
            if spark is None:
                continue
            item = view(spark, current_user_id, today, now)
            read.spark = item if worth_showing(item) else None
    except Exception:
        logger.exception("Failed to attach sparks to conversations", extra={"user_id": current_user_id})


# ---------------------------------------------------------------- 记账


@dataclass
class Change:
    spark_id: int
    low: int
    high: int
    reason: str
    actor_id: int | None = None
    conversation_id: int | None = None
    level_up: int | None = None
    restored_days: int = 0


async def _claim(db: AsyncSession, low: int, high: int, now: datetime) -> FriendSpark:
    """这一对的行（没有就建），锁住读最新的。写事务的第一条语句就是写（SQLite 上从这里起拿着写锁）。"""
    await db.execute(
        upsert_insert(db, FriendSpark.__table__)
        .values(user_low_id=low, user_high_id=high, created_at=now, updated_at=now)
        .on_conflict_do_nothing(index_elements=["user_low_id", "user_high_id"])
    )
    return (
        await db.execute(
            select(FriendSpark)
            .where(FriendSpark.user_low_id == low, FriendSpark.user_high_id == high)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()


def _direct_pair(conversation: Conversation | None) -> tuple[int, int] | None:
    if conversation is None or conversation.type != ConversationType.DIRECT or not conversation.direct_key:
        return None
    try:
        low, high = (int(part) for part in conversation.direct_key.split(":", 1))
    except ValueError:
        return None
    if low == high:
        return None
    return _pair(low, high)


async def record_message(db: AsyncSession, conversation_id: int, sender_id: int, message_type: MessageType, *, at: datetime | None = None) -> Change | None:
    """私聊里发了一条消息之后调用（消息已经提交）。不是好友私聊、系统消息、今天已经发过言的直接返回 None。"""
    if message_type in UNCOUNTED_TYPES:
        return None
    try:
        pair = _direct_pair(await db.get(Conversation, conversation_id))
        if pair is None or sender_id not in pair:
            return None
        low, high = pair
        now = _aware(at) or _now()
        day = _local_date(now)
        existing = await _spark_of(db, low, high, fresh=True)
        side = "low" if sender_id == low else "high"
        if existing is not None and getattr(existing, f"{side}_spoke_on") == day:
            # 今天已经发过言：什么都不会变，不写库
            return None
        if not await are_friends(db, low, high):
            return None
        # 结束上面的读事务：下面的事务要以写开头
        await db.commit()
        async with shop_service._write_lock():
            try:
                spark = await _claim(db, low, high, now)
                if getattr(spark, f"{side}_spoke_on") == day:
                    await db.rollback()
                    return None
                setattr(spark, f"{side}_spoke_on", day)
                change = Change(spark.id, low, high, "spoke", actor_id=sender_id, conversation_id=conversation_id)
                if getattr(spark, f"{_other(side)}_spoke_on") == day and spark.lit_on != day:
                    change.reason = "lit"
                    change.level_up = continue_on(spark, day)
                spark.updated_at = now
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return change
    except Exception:
        logger.exception("Failed to record spark for message", extra={"conversation_id": conversation_id, "sender_id": sender_id})
        try:
            await db.rollback()
        except Exception:
            pass
        return None


async def record_recall(db: AsyncSession, message_id: int) -> Change | None:
    """撤回了一条消息之后调用。只管今天：发的人今天再没有别的（没撤回的）发言了，就把他今天的发言和今天的续火撤回去。"""
    try:
        message = await db.get(Message, message_id, populate_existing=True)
        if message is None or message.recalled_at is None or message.type in UNCOUNTED_TYPES:
            return None
        pair = _direct_pair(await db.get(Conversation, message.conversation_id))
        if pair is None or message.sender_id not in pair:
            return None
        low, high = pair
        now = _now()
        day = _local_date(message.created_at)
        if day != localtime.local_today(now):
            return None
        spark = await _spark_of(db, low, high, fresh=True)
        side = "low" if message.sender_id == low else "high"
        if spark is None or getattr(spark, f"{side}_spoke_on") != day:
            return None
        start = _midnight(day)
        countable = [
            Message.conversation_id == message.conversation_id,
            Message.sender_id == message.sender_id,
            Message.recalled_at.is_(None),
            Message.type.not_in(UNCOUNTED_TYPES),
        ]
        still = await db.scalar(select(Message.id).where(*countable, Message.created_at >= start, Message.created_at < start + timedelta(days=1)).limit(1))
        if still is not None:
            return None
        previous = await db.scalar(select(func.max(Message.created_at)).where(*countable, Message.created_at < start))
        previous_day = _local_date(previous) if previous is not None else None
        conversation_id = message.conversation_id
        await db.commit()
        async with shop_service._write_lock():
            try:
                spark = await _claim(db, low, high, now)
                if getattr(spark, f"{side}_spoke_on") != day:
                    await db.rollback()
                    return None
                setattr(spark, f"{side}_spoke_on", previous_day)
                undo_today(spark, day)
                spark.updated_at = now
                change = Change(spark.id, low, high, "recalled", actor_id=message.sender_id, conversation_id=conversation_id)
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return change
    except Exception:
        logger.exception("Failed to undo spark for recalled message", extra={"message_id": message_id})
        try:
            await db.rollback()
        except Exception:
            pass
        return None


def _display_name(user: User | None) -> str:
    if user is None:
        return "好友"
    return (user.nickname or "").strip() or user.username


async def _post_system_message(db: AsyncSession, conversation_id: int, sender_id: int, content: str, metadata: dict[str, Any]) -> None:
    from app.api.v1.conversations import online_conversation_member_ids, send_message_updates
    from app.services import messages as message_service

    # 传 User 对象：广播 message.created 时要读 message.sender（异步会话里不能懒加载）
    sender = await db.get(User, sender_id)
    if sender is None:
        return
    message = await message_service.create_system_message(db, conversation_id, sender, content, metadata)
    member_ids = await online_conversation_member_ids(db, conversation_id)
    response_cache.invalidate_user_conversations(set(member_ids))
    await send_message_updates(db, "message.created", message, member_ids)


async def announce(db: AsyncSession, change: Change | None) -> None:
    """提交之后：升级 / 重燃发一条系统提示，再给双方各发一份 spark.updated（按各自的视角）。尽力而为，失败只记日志。"""
    if change is None:
        return
    try:
        from app.websocket.manager import manager

        spark = await db.get(FriendSpark, change.spark_id, populate_existing=True)
        if spark is None:
            return
        conversation_id = change.conversation_id or await _direct_conversation_id(db, change.low, change.high)
        response_cache.invalidate_user_conversations({change.low, change.high})
        now = _now()
        today = localtime.local_today(now)
        if conversation_id is not None and change.actor_id is not None:
            if change.level_up is not None and change.level_up >= MILESTONE_MIN_LEVEL:
                reached = level_for(spark.streak_days)
                if reached is not None:
                    await _post_system_message(
                        db,
                        conversation_id,
                        change.actor_id,
                        f"你们的火花升级为「{reached.name}」，已经连续 {spark.streak_days} 天",
                        {"system_event": SYSTEM_EVENT_LEVEL_UP, "spark": {**_level_dict(reached), "days": spark.streak_days}},
                    )
            elif change.reason == "relit":
                actor = await db.get(User, change.actor_id)
                reached = level_for(spark.streak_days)
                await _post_system_message(
                    db,
                    conversation_id,
                    change.actor_id,
                    f"{_display_name(actor)} 重燃了你们的火花，连续 {spark.streak_days} 天",
                    {"system_event": SYSTEM_EVENT_RELIT, "spark": {**_level_dict(reached), "days": spark.streak_days}},
                )
            spark = await db.get(FriendSpark, change.spark_id, populate_existing=True)
        for viewer_id, friend_id in ((change.low, change.high), (change.high, change.low)):
            await manager.send_to_user(
                viewer_id,
                EVENT_UPDATED,
                {
                    "friend_id": friend_id,
                    "conversation_id": conversation_id,
                    "reason": change.reason,
                    "actor_id": change.actor_id,
                    "spark": _jsonable(view(spark, viewer_id, today, now)),
                },
            )
    except Exception:
        logger.exception("Failed to announce spark change", extra={"spark_id": change.spark_id})


def _jsonable(item: dict[str, Any]) -> dict[str, Any]:
    from fastapi.encoders import jsonable_encoder

    return jsonable_encoder(item)


# 接口层不等记账：提交之后起一个任务，用自己的会话记、发事件。测试里可以换掉 session_factory，或者 await drain()
session_factory: Any = None
_tasks: set[asyncio.Task] = set()
TASK_LIMIT = 2000
_factories: dict[int, Any] = {}


def _sessions(bind: Any = None) -> Any:
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
        logger.error("Spark task limit reached; dropping one record")
        return
    task = asyncio.create_task(job())
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


def schedule_message(conversation_id: int, sender_id: int, message_type: MessageType, at: datetime | None = None, *, bind: Any = None) -> None:
    """发消息的接口提交之后调用。bind：发起请求的那个会话的引擎（db.bind），记账用同一个库。"""
    if message_type in UNCOUNTED_TYPES:
        return

    async def job() -> None:
        try:
            async with _sessions(bind)() as db:
                await announce(db, await record_message(db, conversation_id, sender_id, message_type, at=at))
        except Exception:
            logger.exception("Spark message task failed")

    _spawn(job)


def schedule_recall(message_id: int, *, bind: Any = None) -> None:
    async def job() -> None:
        try:
            async with _sessions(bind)() as db:
                await announce(db, await record_recall(db, message_id))
        except Exception:
            logger.exception("Spark recall task failed")

    _spawn(job)


async def drain() -> None:
    """等排着的任务都做完（测试用）。"""
    while _tasks:
        await asyncio.gather(*list(_tasks), return_exceptions=True)


# ---------------------------------------------------------------- 用户侧接口


async def _require_friend(db: AsyncSession, user_id: int, friend_id: int) -> User:
    friend = await db.get(User, friend_id) if friend_id != user_id else None
    if friend is None or not await are_friends(db, user_id, friend_id):
        raise _error(status.HTTP_404_NOT_FOUND, "not_friend", "你们还不是好友")
    return friend


async def view_for(db: AsyncSession, user_id: int, friend_id: int) -> dict[str, Any]:
    """user 看他和 friend 之间的火花（不检查好友关系）。"""
    low, high = _pair(user_id, friend_id)
    now = _now()
    return view(await _spark_of(db, low, high, fresh=True), user_id, localtime.local_today(now), now)


async def detail(db: AsyncSession, user: User, friend_id: int) -> dict[str, Any]:
    """GET /friends/{friend_id}/spark。"""
    user_id = user.id
    await _require_friend(db, user_id, friend_id)
    low, high = _pair(user_id, friend_id)
    return {
        "friend_id": friend_id,
        "conversation_id": await _direct_conversation_id(db, low, high),
        "spark": await view_for(db, user_id, friend_id),
        "rules": rules_view(),
    }


async def list_mine(db: AsyncSession, user: User) -> dict[str, Any]:
    """GET /sparks：我和所有好友之间有得画的火花（好友列表、资料卡用）。"""
    user_id = user.id
    friend_ids = list((await db.execute(select(Friendship.friend_id).where(Friendship.user_id == user_id))).scalars())
    sparks = await sparks_with(db, user_id, friend_ids)
    now = _now()
    today = localtime.local_today(now)
    items = []
    for friend_id, spark in sparks.items():
        item = view(spark, user_id, today, now)
        if worth_showing(item):
            items.append({"friend_id": friend_id, "spark": item})
    items.sort(key=lambda entry: (-entry["spark"]["days"], entry["friend_id"]))
    return {"items": items}


async def relight(db: AsyncSession, user: User, friend_id: int) -> dict[str, Any]:
    """POST /friends/{friend_id}/spark/relight：花 RELIGHT_COST 金币重燃。检查顺序：好友 → 能不能重燃 → 绑定 CCW → 核实余额 → 扣币。"""
    user_id, oid = user.id, shop_service._bound_oid(user)
    await _require_friend(db, user_id, friend_id)
    low, high = _pair(user_id, friend_id)
    now = _now()
    today = localtime.local_today(now)
    state = relight_state(await _spark_of(db, low, high, fresh=True), today, now)
    if not state["can"]:
        raise _not_relightable(state)
    if oid is None:
        raise _error(status.HTTP_403_FORBIDDEN, "ccw_not_bound", "先绑定 CCW 账号才能用金币重燃火花")
    # 累计投币得是刚向 CCW 核实过的，核实不了就 503，不扣币
    await shop_service._fresh_account(db, user_id, oid)

    outcome = "ok"
    change: Change | None = None
    blocked: dict[str, Any] = {}
    now = _now()
    today = localtime.local_today(now)
    async with shop_service._write_lock():
        try:
            # 先锁账户行再锁火花行（发消息那边只锁火花行，顺序固定，不会互相等）
            await lock_for_update(db, select(ShopAccount.user_id).where(ShopAccount.user_id == user_id))
            charged = await db.execute(
                update(ShopAccount)
                .where(ShopAccount.user_id == user_id, ShopAccount.ccw_oid == oid, shop_service.available_expression(user_id, oid) >= RELIGHT_COST)
                .values(updated_at=now)
                .execution_options(synchronize_session=False)
            )
            if charged.rowcount != 1:
                await db.rollback()
                outcome = "short"
            else:
                spark = await _claim(db, low, high, now)
                state = relight_state(spark, today, now)
                if not state["can"]:
                    # 对方刚刚抢先重燃了
                    await db.rollback()
                    outcome = "blocked"
                    blocked = state
                else:
                    lost = state["lost_days"]
                    if status_of(spark, today) == STATUS_OUT:
                        # 还在熄灭状态：恢复原来的天数，今天还要续一次
                        spark.streak_days = lost
                        spark.lit_on = today - timedelta(days=1)
                    else:
                        # 熄灭后又聊起来了：原来的天数加上新攒的
                        spark.streak_days = lost + int(spark.streak_days or 0)
                    spark.extinguished_at = state["deadline"] - timedelta(days=RELIGHT_WINDOW_DAYS)
                    spark.lost_days = 0
                    spark.best_streak = max(int(spark.best_streak or 0), spark.streak_days)
                    reached = level_for(spark.streak_days)
                    spark.level = reached.level if reached else 0
                    spark.relit_at = now
                    spark.relit_by_id = user_id
                    spark.undo_json = None
                    spark.updated_at = now
                    db.add(
                        SparkRelight(
                            spark_id=spark.id, user_id=user_id, friend_id=friend_id, amount=RELIGHT_COST, restored_days=lost, ccw_oid=oid, created_at=now
                        )
                    )
                    change = Change(spark.id, low, high, "relit", actor_id=user_id, restored_days=lost)
                    await db.commit()
        except HTTPException:
            raise
        except BaseException:
            await db.rollback()
            raise
    if outcome == "blocked":
        raise _not_relightable(blocked)
    if outcome == "short":
        balance = (await shop_service._balance(db, user_id, oid))["balance"]
        raise _error(
            status.HTTP_402_PAYMENT_REQUIRED,
            "insufficient_balance",
            "金币不够",
            balance=balance,
            price=RELIGHT_COST,
            shortfall=max(RELIGHT_COST - balance, 0),
            donate_url=get_coin_ledger().topup_url(),
        )
    assert change is not None
    shop_service.announce_balance(user_id, "spark.relit", friend_id=friend_id)
    await announce(db, change)
    return {
        "friend_id": friend_id,
        "restored_days": change.restored_days,
        "spark": await view_for(db, user_id, friend_id),
        "balance": await shop_service._balance(db, user_id, oid),
    }


def _not_relightable(state: dict[str, Any]) -> HTTPException:
    block = state.get("block")
    if block == "expired":
        message = f"火花熄灭超过 {RELIGHT_WINDOW_DAYS} 天，已经不能重燃了"
    elif block == "cooldown":
        message = f"每对好友 {RELIGHT_COOLDOWN_DAYS} 天内只能重燃一次"
    else:
        message = "火花还没熄灭，不用重燃"
    return _error(status.HTTP_409_CONFLICT, "spark_not_relightable", message, reason=block)


# ---------------------------------------------------------------- 熄灭扫描


async def sweep(db: AsyncSession, *, now: datetime | None = None) -> int:
    """把已经熄灭（连续两天没续上）但还没清零的清零：原来的天数挪到 lost_days，记熄灭时间，给双方发 spark.updated。

    一行一个短事务（条件更新，几个进程同时扫只有一个会更新成功、发事件）。返回清零了几对。
    """
    now = _aware(now) or _now()
    today = localtime.local_today(now)
    cutoff = today - timedelta(days=GRACE_MISSED_DAYS + 2)
    total = 0
    while True:
        rows = (
            await db.execute(
                select(FriendSpark.id, FriendSpark.lit_on, FriendSpark.streak_days, FriendSpark.user_low_id, FriendSpark.user_high_id)
                .where(FriendSpark.lit_on <= cutoff, FriendSpark.streak_days > 0)
                .order_by(FriendSpark.id)
                .limit(SWEEP_BATCH)
            )
        ).all()
        await db.commit()
        if not rows:
            return total
        for spark_id, lit_on, streak, low, high in rows:
            async with shop_service._write_lock():
                try:
                    result = await db.execute(
                        update(FriendSpark)
                        .where(FriendSpark.id == spark_id, FriendSpark.lit_on == lit_on, FriendSpark.streak_days == streak)
                        .values(lost_days=streak, streak_days=0, level=0, extinguished_at=_midnight(out_day(lit_on)), undo_json=None, updated_at=now)
                        .execution_options(synchronize_session=False)
                    )
                    await db.commit()
                except BaseException:
                    await db.rollback()
                    raise
            if result.rowcount == 1:
                total += 1
                if await are_friends(db, low, high):
                    await announce(db, Change(spark_id, low, high, "extinguished"))
        if len(rows) < SWEEP_BATCH:
            return total


_worker_task: asyncio.Task | None = None


async def _worker() -> None:
    from app.core.database import AsyncSessionLocal

    while True:
        try:
            async with AsyncSessionLocal() as db:
                await sweep(db)
        except Exception:
            logger.exception("Spark sweep failed")
        await asyncio.sleep(SWEEP_SECONDS)


def start_spark_worker() -> None:
    global _worker_task
    if _worker_task is not None and not _worker_task.done():
        return
    _worker_task = asyncio.create_task(_worker())


async def stop_spark_worker() -> None:
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
