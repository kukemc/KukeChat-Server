"""资深委员的接口：资深合议、申诉复审、举报人复议（docs/jury-v2.md 3.1、3.2、3.7）。挂在 /jury 下，见 jury.py。

/panel/... 路径里的 {case_id} 是评审编号（和 /jury/cases/... 一样）；/appeals/... 里的 {review_id} 是复审本身的 id。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.models.user import User
from app.schemas.case import JuryVoteCreate, JuryVoteResult
from app.schemas.jury_senior import (
    AppealMineItem,
    AppealMinePage,
    AppealNext,
    AppealReviewView,
    AppealVoteCreate,
    AppealVoteResult,
    PanelCaseView,
    PanelNextCase,
)
from app.services import jury_panel as panel_service
from app.services import jury_reviews as review_service


router = APIRouter()


@router.get("/panel/next", response_model=PanelNextCase)
async def next_panel_case(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PanelNextCase:
    view = await panel_service.next_case(db, current_user)
    return PanelNextCase(case=PanelCaseView(**view) if view is not None else None)


@router.post("/panel/{case_id}/vote", response_model=JuryVoteResult)
async def vote_on_panel_case(
    case_id: int,
    payload: JuryVoteCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> JuryVoteResult:
    # 请求体和普通投票同形；合议不能投 abstain / need_context，服务层会拒绝（422 invalid_vote）
    return JuryVoteResult(**await panel_service.cast_vote(db, current_user, case_id, payload.vote, payload.note, payload.category))


# 固定路径要声明在带参数的路径前面
@router.get("/appeals/next", response_model=AppealNext)
async def next_appeal_review(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AppealNext:
    view = await review_service.next_review(db, current_user)
    return AppealNext(review=AppealReviewView(**view) if view is not None else None)


@router.get("/appeals/mine", response_model=AppealMinePage)
async def my_appeal_reviews(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: int = Query(default=20, ge=1, le=50),
    offset: int = Query(default=0, ge=0),
) -> AppealMinePage:
    items, total = await review_service.my_reviews(db, current_user.id, limit=limit, offset=offset)
    return AppealMinePage(items=[AppealMineItem(**item) for item in items], total=total)


@router.post("/appeals/{review_id}/vote", response_model=AppealVoteResult)
async def vote_on_appeal_review(
    review_id: int,
    payload: AppealVoteCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AppealVoteResult:
    return AppealVoteResult(
        **await review_service.cast_vote(db, current_user, review_id, payload.vote, payload.note, payload.level, payload.category)
    )
