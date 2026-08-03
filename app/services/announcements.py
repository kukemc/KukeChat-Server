from __future__ import annotations

from datetime import datetime, timezone

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.models.announcement import Announcement
from app.schemas.announcement import AnnouncementCreate, AnnouncementRead, AnnouncementUpdate
from app.schemas.user import UserPublic
from app.services.reports import log_admin_action

UTC = timezone.utc


def _build_read(announcement: Announcement) -> AnnouncementRead:
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
    )


async def list_active(db: AsyncSession, limit: int = 20) -> list[AnnouncementRead]:
    result = await db.execute(
        select(Announcement)
        .options(joinedload(Announcement.created_by))
        .where(Announcement.is_active.is_(True))
        .order_by(Announcement.pinned.desc(), Announcement.created_at.desc(), Announcement.id.desc())
        .limit(limit)
    )
    return [_build_read(item) for item in result.scalars().all()]


async def get_latest(db: AsyncSession) -> AnnouncementRead | None:
    result = await db.execute(
        select(Announcement)
        .options(joinedload(Announcement.created_by))
        .where(Announcement.is_active.is_(True))
        .order_by(Announcement.pinned.desc(), Announcement.created_at.desc(), Announcement.id.desc())
        .limit(1)
    )
    announcement = result.scalar_one_or_none()
    return _build_read(announcement) if announcement is not None else None


# ----- admin -----


async def admin_list(db: AsyncSession, limit: int = 50) -> list[AnnouncementRead]:
    result = await db.execute(
        select(Announcement)
        .options(joinedload(Announcement.created_by))
        .order_by(Announcement.created_at.desc(), Announcement.id.desc())
        .limit(limit)
    )
    return [_build_read(item) for item in result.scalars().all()]


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
    return _build_read(await _get_or_404(db, announcement_id))


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
    await db.delete(announcement)
    await log_admin_action(db, admin_id, "announcement.delete", "announcement", announcement_id, None)
    await db.commit()
