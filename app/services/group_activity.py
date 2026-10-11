from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from fastapi import HTTPException, status
from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.core.group_levels import exp_required_for_level, level_from_exp, next_level_remaining_exp  # noqa: F401
from app.models.conversation import ConversationMember
from app.models.enums import ConversationType, MessageType
from app.models.group_activity import GroupCheckin, GroupMemberDailyActivity, GroupMemberExpLog
from app.models.message import Message
from app.models.user import User
from app.schemas.conversation import ConversationMemberRead
from app.schemas.group_activity import GroupLeaderboardItem, GroupLeaderboardRead
from app.schemas.user import UserPublic
from app.services.conversations import require_group_conversation, require_membership

UTC = timezone.utc
# 活跃成员一天大约能拿 130-160 经验：10 级一周内，20 级约三周，50 级约四个月。
MESSAGE_EXP_DAILY_LIMIT = 100
MESSAGE_EXP = 2
MEDIA_MESSAGE_EXP = 3
CHECKIN_BASE_EXP = 30
FIRST_CHECKIN_EXP_BONUS = 15


def _clean_text(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    return text or None


def calculate_checkin_exp(streak_days: int) -> int:
    exp = CHECKIN_BASE_EXP
    if streak_days >= 100:
        exp += 200
    elif streak_days >= 30:
        exp += 80
    elif streak_days >= 7:
        exp += 25
    elif streak_days >= 3:
        exp += 10
    return exp


async def is_first_checkin_today(db: AsyncSession, conversation_id: int, checkin_date: date) -> bool:
    result = await db.execute(
        select(func.count(GroupCheckin.id)).where(
            GroupCheckin.conversation_id == conversation_id,
            GroupCheckin.checkin_date == checkin_date,
        )
    )
    return int(result.scalar_one() or 0) == 0


def activity_score_for_checkin(streak_days: int) -> int:
    return 5 + min(10, max(0, streak_days // 7))


async def get_or_create_daily_activity(
    db: AsyncSession,
    conversation_id: int,
    user_id: int,
    activity_date: date,
) -> GroupMemberDailyActivity:
    result = await db.execute(
        select(GroupMemberDailyActivity).where(
            GroupMemberDailyActivity.conversation_id == conversation_id,
            GroupMemberDailyActivity.user_id == user_id,
            GroupMemberDailyActivity.activity_date == activity_date,
        )
    )
    daily = result.scalar_one_or_none()
    if daily is not None:
        return daily
    daily = GroupMemberDailyActivity(conversation_id=conversation_id, user_id=user_id, activity_date=activity_date)
    db.add(daily)
    await db.flush()
    return daily


def award_group_exp(
    db: AsyncSession,
    member: ConversationMember,
    delta: int,
    source: str,
    reason: str | None = None,
    related_id: int | None = None,
) -> tuple[int, int]:
    previous_level = member.level
    member.level_exp += max(0, delta)
    member.level = level_from_exp(member.level_exp)
    db.add(
        GroupMemberExpLog(
            conversation_id=member.conversation_id,
            user_id=member.user_id,
            source=source,
            delta=delta,
            reason=reason,
            related_id=related_id,
        )
    )
    return previous_level, member.level


async def checkin_group(
    db: AsyncSession,
    conversation_id: int,
    user: User,
    message: str | None = None,
) -> tuple[GroupCheckin, int, int]:
    await require_group_conversation(db, conversation_id)
    member = await require_membership(db, conversation_id, user.id)
    today = datetime.now(UTC).date()

    existing_result = await db.execute(
        select(GroupCheckin).where(
            GroupCheckin.conversation_id == conversation_id,
            GroupCheckin.user_id == user.id,
            GroupCheckin.checkin_date == today,
        )
    )
    if existing_result.scalar_one_or_none() is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Already checked in today")

    yesterday = today - timedelta(days=1)
    streak = member.current_checkin_streak + 1 if member.last_checkin_date == yesterday else 1
    first_checkin = await is_first_checkin_today(db, conversation_id, today)
    exp = calculate_checkin_exp(streak) + (FIRST_CHECKIN_EXP_BONUS if first_checkin else 0)
    score = activity_score_for_checkin(streak)
    now = datetime.now(UTC)

    checkin = GroupCheckin(
        conversation_id=conversation_id,
        user_id=user.id,
        checkin_date=today,
        streak_days=streak,
        exp_awarded=exp,
        message=_clean_text(message),
    )
    db.add(checkin)

    daily = await get_or_create_daily_activity(db, conversation_id, user.id, today)
    daily.checkin_count += 1
    daily.exp_from_checkins += exp
    daily.activity_score += score

    member.total_checkins += 1
    member.current_checkin_streak = streak
    member.best_checkin_streak = max(member.best_checkin_streak, streak)
    member.last_checkin_date = today
    member.last_active_at = now
    member.activity_score += score
    reason = "first_checkin" if first_checkin else None
    previous_level, current_level = award_group_exp(db, member, exp, "checkin", reason=reason, related_id=None)

    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Already checked in today") from exc

    result = await db.execute(select(GroupCheckin).options(joinedload(GroupCheckin.user)).where(GroupCheckin.id == checkin.id))
    return result.scalar_one(), previous_level, current_level


async def get_checkin_status(db: AsyncSession, conversation_id: int, user_id: int) -> dict:
    await require_group_conversation(db, conversation_id)
    member = await require_membership(db, conversation_id, user_id)
    today = datetime.now(UTC).date()
    result = await db.execute(
        select(GroupCheckin)
        .options(joinedload(GroupCheckin.user))
        .where(
            GroupCheckin.conversation_id == conversation_id,
            GroupCheckin.user_id == user_id,
            GroupCheckin.checkin_date == today,
        )
    )
    checkin = result.scalar_one_or_none()
    return {
        "conversation_id": conversation_id,
        "checked_in_today": checkin is not None,
        "today_checkin": checkin,
        "total_checkins": member.total_checkins,
        "current_streak": member.current_checkin_streak,
        "best_streak": member.best_checkin_streak,
        "level": member.level,
        "level_exp": member.level_exp,
        "next_level_exp": next_level_remaining_exp(member.level_exp),
    }


async def record_message_activity(db: AsyncSession, conversation_id: int, member: ConversationMember, message: Message) -> tuple[int, int]:
    if message.type == MessageType.SYSTEM:
        return member.level, member.level
    today = datetime.now(UTC).date()
    daily = await get_or_create_daily_activity(db, conversation_id, member.user_id, today)
    now = datetime.now(UTC)
    is_media = message.type in {MessageType.IMAGE, MessageType.VOICE}
    base_score = 2 if is_media else 1
    base_exp = MEDIA_MESSAGE_EXP if is_media else MESSAGE_EXP
    remaining_exp = max(0, MESSAGE_EXP_DAILY_LIMIT - daily.exp_from_messages)
    exp = min(base_exp, remaining_exp)

    daily.message_count += 1
    daily.activity_score += base_score
    member.activity_score += base_score
    member.last_active_at = now
    previous_level = member.level
    current_level = member.level
    if exp > 0:
        daily.exp_from_messages += exp
        previous_level, current_level = award_group_exp(db, member, exp, "message", related_id=message.id)
    return previous_level, current_level


def _member_read(member: ConversationMember) -> ConversationMemberRead:
    data = ConversationMemberRead.model_validate(member)
    data.next_level_exp = next_level_remaining_exp(member.level_exp)
    return data


async def _leaderboard_rows_for_all(db: AsyncSession, conversation_id: int, leaderboard_type: str, limit: int) -> list[ConversationMember]:
    order_map = {
        "activity": (ConversationMember.activity_score.desc(), ConversationMember.level_exp.desc()),
        "level": (ConversationMember.level_exp.desc(), ConversationMember.activity_score.desc()),
        "checkin_streak": (ConversationMember.current_checkin_streak.desc(), ConversationMember.best_checkin_streak.desc()),
        "checkin_total": (ConversationMember.total_checkins.desc(), ConversationMember.best_checkin_streak.desc()),
        "message": (ConversationMember.activity_score.desc(), ConversationMember.level_exp.desc()),
    }
    result = await db.execute(
        select(ConversationMember)
        .options(joinedload(ConversationMember.user))
        .where(ConversationMember.conversation_id == conversation_id)
        .order_by(*order_map.get(leaderboard_type, order_map["activity"]), ConversationMember.joined_at.asc())
        .limit(limit)
    )
    return list(result.scalars().unique().all())


def _period_start(period: str) -> date | None:
    today = datetime.now(UTC).date()
    if period == "today":
        return today
    if period == "week":
        return today - timedelta(days=6)
    if period == "month":
        return today - timedelta(days=29)
    return None


async def get_group_leaderboard(
    db: AsyncSession,
    conversation_id: int,
    user_id: int,
    leaderboard_type: str = "activity",
    period: str = "all",
    limit: int = 50,
) -> GroupLeaderboardRead:
    await require_group_conversation(db, conversation_id)
    await require_membership(db, conversation_id, user_id)
    limit = max(1, min(100, limit))

    member_stats: dict[int, tuple[int, int, int]] = {}
    if period == "all":
        members = await _leaderboard_rows_for_all(db, conversation_id, leaderboard_type, limit)
    else:
        start = _period_start(period)
        if start is None:
            start = datetime.now(UTC).date()
        sort_value = {
            "activity": func.coalesce(func.sum(GroupMemberDailyActivity.activity_score), 0),
            "level": func.coalesce(func.sum(GroupMemberDailyActivity.exp_from_messages + GroupMemberDailyActivity.exp_from_checkins + GroupMemberDailyActivity.exp_from_activities), 0),
            "checkin_streak": func.coalesce(func.sum(GroupMemberDailyActivity.checkin_count), 0),
            "checkin_total": func.coalesce(func.sum(GroupMemberDailyActivity.checkin_count), 0),
            "message": func.coalesce(func.sum(GroupMemberDailyActivity.message_count), 0),
        }.get(leaderboard_type, func.coalesce(func.sum(GroupMemberDailyActivity.activity_score), 0))
        stats_result = await db.execute(
            select(
                GroupMemberDailyActivity.user_id,
                func.coalesce(func.sum(GroupMemberDailyActivity.activity_score), 0).label("activity_score"),
                func.coalesce(func.sum(GroupMemberDailyActivity.message_count), 0).label("message_count"),
                func.coalesce(func.sum(GroupMemberDailyActivity.checkin_count), 0).label("checkin_count"),
            )
            .where(
                GroupMemberDailyActivity.conversation_id == conversation_id,
                GroupMemberDailyActivity.activity_date >= start,
            )
            .group_by(GroupMemberDailyActivity.user_id)
            .order_by(sort_value.desc(), GroupMemberDailyActivity.user_id.asc())
            .limit(limit)
        )
        rows = stats_result.all()
        member_stats = {int(user_id_value): (int(score), int(messages), int(checkins)) for user_id_value, score, messages, checkins in rows}
        user_ids = list(member_stats)
        if user_ids:
            member_result = await db.execute(
                select(ConversationMember)
                .options(joinedload(ConversationMember.user))
                .where(ConversationMember.conversation_id == conversation_id, ConversationMember.user_id.in_(user_ids))
            )
            member_by_user = {member.user_id: member for member in member_result.scalars().unique().all()}
            members = [member_by_user[user_id_value] for user_id_value in user_ids if user_id_value in member_by_user]
        else:
            members = []

    items: list[GroupLeaderboardItem] = []
    for index, member in enumerate(members, start=1):
        period_score, message_count, checkin_count = member_stats.get(
            member.user_id,
            (member.activity_score, 0, member.total_checkins),
        )
        items.append(
            GroupLeaderboardItem(
                rank=index,
                user_id=member.user_id,
                user=UserPublic.model_validate(member.user),
                member=_member_read(member),
                level=member.level,
                level_exp=member.level_exp,
                activity_score=period_score if period != "all" else member.activity_score,
                total_checkins=member.total_checkins,
                current_checkin_streak=member.current_checkin_streak,
                best_checkin_streak=member.best_checkin_streak,
                message_count=message_count,
                checkin_count=checkin_count,
            )
        )

    my_rank = next((item for item in items if item.user_id == user_id), None)
    return GroupLeaderboardRead(conversation_id=conversation_id, type=leaderboard_type, period=period, items=items, my_rank=my_rank)
