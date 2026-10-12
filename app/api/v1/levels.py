"""账号等级的用户侧接口（docs/account-level.md 第 3 节）。

router 挂在 /levels 下；users_router 只有一条 GET /users/{user_id}/level，单独挂在 /users 下，这样不用改 users.py。
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.models.user import User
from app.schemas.account_level import LevelCard, LevelHistory, LevelLeaderboard, LevelMe, LevelRules
from app.services import account_levels as level_service


router = APIRouter()
users_router = APIRouter()


@router.get("/rules", response_model=LevelRules)
async def level_rules(_current_user: Annotated[User, Depends(get_current_user)]) -> LevelRules:
    return LevelRules(**level_service.rules_payload())


@router.get("/me", response_model=LevelMe)
async def my_level(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> LevelMe:
    """读取时顺带给本人结算今天的活跃天。"""
    return LevelMe(**await level_service.my_level(db, current_user))


@router.get("/leaderboard", response_model=LevelLeaderboard)
async def level_leaderboard(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    period: Literal["all", "month"] = "all",
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> LevelLeaderboard:
    return LevelLeaderboard(**await level_service.leaderboard(db, current_user, period=period, limit=limit, offset=offset))


@router.get("/history", response_model=LevelHistory)
async def level_history(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    days: int = Query(default=30, ge=1, le=90),
) -> LevelHistory:
    return LevelHistory(**await level_service.history(db, current_user.id, days=days))


@users_router.get("/{user_id}/level", response_model=LevelCard)
async def user_level(
    user_id: int,
    _current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> LevelCard:
    return LevelCard(**await level_service.user_level(db, user_id))
