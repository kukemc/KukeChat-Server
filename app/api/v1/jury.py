from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.models.user import User
from app.schemas.case import (
    JuryApply,
    JuryCaseView,
    JuryHistoryItem,
    JuryNextCase,
    JuryStatus,
    JuryVoteCreate,
    JuryVoteResult,
)
from app.services import jury as jury_service
from app.websocket.manager import admin_manager


router = APIRouter()


@router.get("/me", response_model=JuryStatus)
async def my_jury_status(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> JuryStatus:
    return JuryStatus(**await jury_service.my_status(db, current_user))


@router.post("/apply", response_model=JuryStatus)
async def apply_for_jury(
    payload: JuryApply,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> JuryStatus:
    await jury_service.apply(db, current_user, payload.statement)
    await admin_manager.broadcast("admin.jury.changed", {"user_id": current_user.id})
    return JuryStatus(**await jury_service.my_status(db, current_user))


@router.get("/cases/next", response_model=JuryNextCase)
async def next_jury_case(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> JuryNextCase:
    view = await jury_service.next_case(db, current_user)
    return JuryNextCase(case=JuryCaseView(**view) if view is not None else None)


@router.post("/cases/{case_id}/vote", response_model=JuryVoteResult)
async def vote_on_case(
    case_id: int,
    payload: JuryVoteCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> JuryVoteResult:
    return JuryVoteResult(**await jury_service.cast_vote(db, current_user, case_id, payload.vote, payload.note))


@router.get("/history", response_model=list[JuryHistoryItem])
async def my_jury_history(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[JuryHistoryItem]:
    return [JuryHistoryItem(**item) for item in await jury_service.history(db, current_user.id)]
