from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.models.user import User
from app.schemas.notification import UnifiedNotificationListRead
from app.services.notifications import list_unified_notifications


router = APIRouter()


@router.get("", response_model=UnifiedNotificationListRead)
async def notifications(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> UnifiedNotificationListRead:
    return await list_unified_notifications(db, current_user.id, limit)
