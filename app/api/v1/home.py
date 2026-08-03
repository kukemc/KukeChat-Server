from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.models.user import User
from app.schemas.home import HomeRead
from app.services import home as home_service


router = APIRouter()


@router.get("", response_model=HomeRead)
async def get_home(
    seed: Annotated[int, Query(ge=0, le=2_147_483_647)] = 0,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> HomeRead:
    return await home_service.get_home(db, current_user.id, seed)
