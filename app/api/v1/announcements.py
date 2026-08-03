from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.models.user import User
from app.schemas.announcement import AnnouncementRead
from app.services import announcements as announcement_service

router = APIRouter()


@router.get("", response_model=list[AnnouncementRead])
async def list_announcements(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
) -> list[AnnouncementRead]:
    del current_user
    return await announcement_service.list_active(db, limit)


@router.get("/latest", response_model=AnnouncementRead | None)
async def latest_announcement(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AnnouncementRead | None:
    del current_user
    return await announcement_service.get_latest(db)
