"""好友亲密度、好友成就、好友亲密榜（docs/intimacy.md）。挂在 /intimacy 下，都要求登录，只能看自己和自己好友之间的。"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.models.user import User
from app.schemas.intimacy import (
    BadgeDisplayResult,
    BadgeDisplayWrite,
    FriendIntimacyDetail,
    FriendIntimacyList,
    IntimacyHistory,
    IntimacyRules,
    SeenResult,
)
from app.services import intimacy as intimacy_service

router = APIRouter()


@router.get("/rules", response_model=IntimacyRules)
async def intimacy_rules(
    _user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> IntimacyRules:
    return IntimacyRules(**await intimacy_service.rules_view(db))


@router.get("/friends", response_model=FriendIntimacyList)
async def intimacy_friends(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> FriendIntimacyList:
    """我的全部好友，按亲密度从高到低（好友亲密榜）。每一项带上我在 TA 名字旁挂的那枚徽章。"""
    return FriendIntimacyList(**await intimacy_service.list_friends(db, current_user))


@router.get("/friends/{friend_id}", response_model=FriendIntimacyDetail)
async def intimacy_friend(
    friend_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> FriendIntimacyDetail:
    return FriendIntimacyDetail(**await intimacy_service.friend_detail(db, current_user, friend_id))


@router.put("/friends/{friend_id}/badge", response_model=BadgeDisplayResult)
async def intimacy_set_badge(
    friend_id: int,
    payload: BadgeDisplayWrite,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> BadgeDisplayResult:
    return BadgeDisplayResult(**await intimacy_service.set_display(db, current_user, friend_id, payload.badge))


@router.post("/friends/{friend_id}/seen", response_model=SeenResult)
async def intimacy_mark_seen(
    friend_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SeenResult:
    return SeenResult(**await intimacy_service.mark_seen(db, current_user, friend_id))


@router.get("/friends/{friend_id}/history", response_model=IntimacyHistory)
async def intimacy_history(
    friend_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    days: int = Query(default=30, ge=1, le=intimacy_service.HISTORY_MAX_DAYS),
) -> IntimacyHistory:
    return IntimacyHistory(**await intimacy_service.history(db, current_user, friend_id, days))
