from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import case, delete, func, literal, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.core.config import settings
from app.core.emoji import validate_reaction_emoji
from app.models.announcement import Announcement, AnnouncementReaction
from app.models.user import User
from app.schemas.announcement import AnnouncementCreate, AnnouncementReactionSummary, AnnouncementRead, AnnouncementUpdate
from app.schemas.user import UserPublic
from app.services.penalties import ensure_can_post
from app.services.reports import log_admin_action

UTC = timezone.utc

REACTION_RATE_WINDOW_SECONDS = 60
# 取消回应会把那一行删掉，之后表里就数不到这次操作了；每个进程自己记一份补上，见 _enforce_reaction_rate
_recent_cancels: dict[int, list[datetime]] = {}
_RECENT_CANCELS_SWEEP_SIZE = 2048


def _error(code: str, message: str, status_code: int) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


def _now() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _build_read(announcement: Announcement, reactions: list[AnnouncementReactionSummary] | None = None) -> AnnouncementRead:
    return AnnouncementRead(
        id=announcement.id,
        title=announcement.title,
        content=announcement.content,
        cover_url=announcement.cover_url,
        is_active=announcement.is_active,
        pinned=announcement.pinned,
        author=UserPublic.model_validate(announcement.created_by) if announcement.created_by is not None else None,
        created_at=announcement.created_at,
        updated_at=announcement.updated_at,
        reactions=reactions or [],
    )


async def reaction_summaries(
    db: AsyncSession, announcement_ids: list[int], current_user_id: int | None
) -> dict[int, list[AnnouncementReactionSummary]]:
    """一次查询取回这一批公告的回应汇总，列表接口不要逐条公告查。

    每条公告里按数量从多到少排，数量相同的看谁先出现（现存回应里最早的那一行）。
    ``current_user_id`` 传 None 时 reacted_by_me 恒为 false（后台、实时事件）。
    """
    summaries: dict[int, list[AnnouncementReactionSummary]] = {announcement_id: [] for announcement_id in announcement_ids}
    if not announcement_ids:
        return summaries
    count = func.count(AnnouncementReaction.id)
    first_id = func.min(AnnouncementReaction.id)
    mine = (
        func.max(case((AnnouncementReaction.user_id == current_user_id, 1), else_=0))
        if current_user_id is not None
        else literal(0)
    )
    rows = await db.execute(
        select(AnnouncementReaction.announcement_id, AnnouncementReaction.emoji, count, mine)
        .where(AnnouncementReaction.announcement_id.in_(announcement_ids))
        .group_by(AnnouncementReaction.announcement_id, AnnouncementReaction.emoji)
        .order_by(AnnouncementReaction.announcement_id.asc(), count.desc(), first_id.asc())
    )
    for announcement_id, emoji, total, reacted in rows.all():
        summaries.setdefault(announcement_id, []).append(
            AnnouncementReactionSummary(emoji=emoji, count=total, reacted_by_me=bool(reacted))
        )
    return summaries


async def _build_reads(db: AsyncSession, announcements: list[Announcement], current_user_id: int | None) -> list[AnnouncementRead]:
    summaries = await reaction_summaries(db, [item.id for item in announcements], current_user_id)
    return [_build_read(item, summaries.get(item.id)) for item in announcements]


async def list_active(db: AsyncSession, limit: int = 20, current_user_id: int | None = None) -> list[AnnouncementRead]:
    result = await db.execute(
        select(Announcement)
        .options(joinedload(Announcement.created_by))
        .where(Announcement.is_active.is_(True))
        .order_by(Announcement.pinned.desc(), Announcement.created_at.desc(), Announcement.id.desc())
        .limit(limit)
    )
    return await _build_reads(db, list(result.scalars().all()), current_user_id)


async def get_latest(db: AsyncSession, current_user_id: int | None = None) -> AnnouncementRead | None:
    result = await db.execute(
        select(Announcement)
        .options(joinedload(Announcement.created_by))
        .where(Announcement.is_active.is_(True))
        .order_by(Announcement.pinned.desc(), Announcement.created_at.desc(), Announcement.id.desc())
        .limit(1)
    )
    announcement = result.scalar_one_or_none()
    if announcement is None:
        return None
    return (await _build_reads(db, [announcement], current_user_id))[0]


# ----- 回应 -----


def reaction_options() -> dict[str, Any]:
    return {
        "quick": list(settings.announcement_reaction_quick),
        "per_user": settings.announcement_reaction_per_user,
        "per_announcement": settings.announcement_reaction_per_announcement,
        "per_minute": settings.announcement_reaction_per_minute,
    }


async def _is_reactable(db: AsyncSession, announcement_id: int) -> bool:
    found = await db.scalar(
        select(Announcement.id).where(Announcement.id == announcement_id, Announcement.is_active.is_(True))
    )
    return found is not None


async def _ensure_reactable(db: AsyncSession, announcement_id: int) -> None:
    """下线的公告和不存在的一样对待：不能回应，也不能取消。"""
    if not await _is_reactable(db, announcement_id):
        raise _error("announcement_not_found", "公告不存在或已下线", status.HTTP_404_NOT_FOUND)


async def _find_reaction(db: AsyncSession, announcement_id: int, user_id: int, emoji: str) -> tuple[int, datetime] | None:
    row = (
        await db.execute(
            select(AnnouncementReaction.id, AnnouncementReaction.created_at).where(
                AnnouncementReaction.announcement_id == announcement_id,
                AnnouncementReaction.user_id == user_id,
                AnnouncementReaction.emoji == emoji,
            )
        )
    ).first()
    return (row[0], _as_utc(row[1])) if row is not None else None


def _recent_cancel_count(user_id: int, cutoff: datetime) -> int:
    entries = [stamp for stamp in _recent_cancels.get(user_id, ()) if stamp >= cutoff]
    if entries:
        _recent_cancels[user_id] = entries
    else:
        _recent_cancels.pop(user_id, None)
    return len(entries)


async def _enforce_reaction_rate(db: AsyncSession, user_id: int, now: datetime) -> None:
    """最近一分钟（滚动）里这个人操作了几次，「加」和「取消」各算一次。

    「加」直接数表里他最近一分钟写的行，多个 worker 进程看到的是同一个数；
    「取消」把行删掉了，表里数不到，由处理它的那个进程记在 ``_recent_cancels`` 里
    （连同被删掉的那次「加」）。所以单进程下是准的；多进程下只有反复「加了又取消」
    会被少算——每个进程只知道自己经手的取消，最坏放宽到进程数的倍数。
    """
    cutoff = now - timedelta(seconds=REACTION_RATE_WINDOW_SECONDS)
    added = await db.scalar(
        select(func.count(AnnouncementReaction.id)).where(
            AnnouncementReaction.user_id == user_id, AnnouncementReaction.created_at >= cutoff
        )
    )
    if int(added or 0) + _recent_cancel_count(user_id, cutoff) >= settings.announcement_reaction_per_minute:
        raise _error("reaction_rate_limited", "操作太快了，歇一会儿再试", status.HTTP_429_TOO_MANY_REQUESTS)


def _record_cancel(user_id: int, added_at: datetime, now: datetime) -> None:
    cutoff = now - timedelta(seconds=REACTION_RATE_WINDOW_SECONDS)
    entries = _recent_cancels.setdefault(user_id, [])
    if added_at >= cutoff:
        # 被取消的这一行是刚加的：它那次「加」也跟着从表里消失了，一并记上
        entries.append(added_at)
    entries.append(now)
    if len(_recent_cancels) > _RECENT_CANCELS_SWEEP_SIZE:
        for stale_id in [key for key, stamps in _recent_cancels.items() if max(stamps) < cutoff]:
            _recent_cancels.pop(stale_id, None)


async def _own_summaries(db: AsyncSession, announcement_id: int, user_id: int) -> list[AnnouncementReactionSummary]:
    return (await reaction_summaries(db, [announcement_id], user_id))[announcement_id]


async def _cancel_reaction(
    db: AsyncSession, announcement_id: int, user_id: int, reaction: tuple[int, datetime]
) -> tuple[list[AnnouncementReactionSummary], bool]:
    reaction_id, added_at = reaction
    now = _now()
    await _enforce_reaction_rate(db, user_id, now)
    # 用语句删而不是 ORM：两个请求同时取消同一个回应时，后到的删 0 行也不报错
    await db.execute(delete(AnnouncementReaction).where(AnnouncementReaction.id == reaction_id))
    await db.commit()
    _record_cancel(user_id, added_at, now)
    return await _own_summaries(db, announcement_id, user_id), True


async def toggle_reaction(
    db: AsyncSession, user: User, announcement_id: int, emoji: str
) -> tuple[list[AnnouncementReactionSummary], bool]:
    """开关语义：没回应过就加上，回应过就取消。返回（最新汇总，这次有没有改动）。"""
    user_id = user.id
    emoji = validate_reaction_emoji(emoji)
    await _ensure_reactable(db, announcement_id)
    existing = await _find_reaction(db, announcement_id, user_id, emoji)
    if existing is not None:
        # 取消自己的回应不看禁言
        return await _cancel_reaction(db, announcement_id, user_id, existing)

    ensure_can_post(user)
    now = _now()
    await _enforce_reaction_rate(db, user_id, now)
    current = await _own_summaries(db, announcement_id, user_id)
    per_user = settings.announcement_reaction_per_user
    if sum(1 for item in current if item.reacted_by_me) >= per_user:
        raise _error("reaction_limit", f"每条公告最多用 {per_user} 种表情回应", status.HTTP_409_CONFLICT)
    if len(current) >= settings.announcement_reaction_per_announcement and all(item.emoji != emoji for item in current):
        # 种类满了以后只能跟已有的表情，不能再添新的
        raise _error("reaction_limit", "这条公告的表情种类已经满了，可以点已有的表情", status.HTTP_409_CONFLICT)
    db.add(AnnouncementReaction(announcement_id=announcement_id, user_id=user_id, emoji=emoji, created_at=now))
    try:
        await db.commit()
    except IntegrityError:
        # 并发：同一个回应被重复提交（唯一约束），或者公告刚好被删（外键）。按现状返回，不算改动
        await db.rollback()
        await _ensure_reactable(db, announcement_id)
        return await _own_summaries(db, announcement_id, user_id), False
    return await _own_summaries(db, announcement_id, user_id), True


async def remove_reaction(
    db: AsyncSession, user: User, announcement_id: int, emoji: str
) -> tuple[list[AnnouncementReactionSummary], bool]:
    """取消回应，没回应过也照常返回（幂等）。禁言期间也能取消。"""
    user_id = user.id
    emoji = validate_reaction_emoji(emoji)
    await _ensure_reactable(db, announcement_id)
    existing = await _find_reaction(db, announcement_id, user_id, emoji)
    if existing is None:
        return await _own_summaries(db, announcement_id, user_id), False
    return await _cancel_reaction(db, announcement_id, user_id, existing)


async def reaction_event_payload(db: AsyncSession, announcement_id: int) -> dict[str, Any] | None:
    """实时事件 announcement.reaction.updated 的负载：只有 emoji 和数量，没有 reacted_by_me。

    公告已经下线或删除时返回 None，不用再推。
    """
    if not await _is_reactable(db, announcement_id):
        return None
    reactions = (await reaction_summaries(db, [announcement_id], None))[announcement_id]
    return {"id": announcement_id, "reactions": [{"emoji": item.emoji, "count": item.count} for item in reactions]}


# ----- admin -----


async def admin_list(db: AsyncSession, limit: int = 50) -> list[AnnouncementRead]:
    result = await db.execute(
        select(Announcement)
        .options(joinedload(Announcement.created_by))
        .order_by(Announcement.created_at.desc(), Announcement.id.desc())
        .limit(limit)
    )
    return await _build_reads(db, list(result.scalars().all()), None)


async def _get_or_404(db: AsyncSession, announcement_id: int) -> Announcement:
    result = await db.execute(
        select(Announcement).options(joinedload(Announcement.created_by)).where(Announcement.id == announcement_id)
    )
    announcement = result.scalar_one_or_none()
    if announcement is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Announcement not found")
    return announcement


async def admin_create(db: AsyncSession, admin_id: int, payload: AnnouncementCreate) -> AnnouncementRead:
    announcement = Announcement(
        title=payload.title,
        content=payload.content,
        cover_url=payload.cover_url,
        is_active=payload.is_active,
        pinned=payload.pinned,
        created_by_id=admin_id,
    )
    db.add(announcement)
    await db.flush()
    await log_admin_action(db, admin_id, "announcement.create", "announcement", announcement.id, {"title": payload.title})
    await db.commit()
    return await admin_get(db, announcement.id)


async def admin_get(db: AsyncSession, announcement_id: int) -> AnnouncementRead:
    return (await _build_reads(db, [await _get_or_404(db, announcement_id)], None))[0]


async def admin_update(db: AsyncSession, admin_id: int, announcement_id: int, payload: AnnouncementUpdate) -> AnnouncementRead:
    announcement = await _get_or_404(db, announcement_id)
    data = payload.model_dump(exclude_unset=True)
    if "title" in data and data["title"] is not None:
        announcement.title = data["title"].strip()
    if "content" in data and data["content"] is not None:
        announcement.content = data["content"].strip()
    if "cover_url" in data and not payload.clear_cover:
        announcement.cover_url = data["cover_url"]
    if payload.clear_cover:
        announcement.cover_url = None
    if "is_active" in data and data["is_active"] is not None:
        announcement.is_active = data["is_active"]
    if "pinned" in data and data["pinned"] is not None:
        announcement.pinned = data["pinned"]
    announcement.updated_at = datetime.now(UTC)
    await log_admin_action(db, admin_id, "announcement.update", "announcement", announcement.id, None)
    await db.commit()
    return await admin_get(db, announcement.id)


async def admin_delete(db: AsyncSession, admin_id: int, announcement_id: int) -> None:
    announcement = await _get_or_404(db, announcement_id)
    # 回应表有 ON DELETE CASCADE，但 SQLite 只在连接开了 PRAGMA foreign_keys 时才执行；这里显式删，不依赖它
    await db.execute(delete(AnnouncementReaction).where(AnnouncementReaction.announcement_id == announcement_id))
    await db.delete(announcement)
    await log_admin_action(db, admin_id, "announcement.delete", "announcement", announcement_id, None)
    await db.commit()
