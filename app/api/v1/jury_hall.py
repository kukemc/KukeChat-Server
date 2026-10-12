"""风纪系统 v2 三期：判例和议事厅的用户侧接口（docs/jury-v2.md 3.4、3.5）。

和 jury.py 一样挂在 /jury 下：/jury/precedents/...、/jury/hall/...。
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.models.user import User
from app.schemas.jury_hall import (
    HallComment,
    HallCommentCreate,
    HallCommentPage,
    HallCommentState,
    HallPost,
    HallPostCreate,
    HallPostPage,
    HallPostState,
    HallPostUpdate,
    HallReportCreate,
    HallReportResult,
    HallVoteCreate,
    Precedent,
    PrecedentCreate,
    PrecedentDetail,
    PrecedentPage,
    PrecedentState,
    PrecedentUpdate,
)
from app.services import jury_hall as hall_service
from app.services import jury_precedents as precedent_service


router = APIRouter()


# ---------------------------------------------------------------- 判例


@router.get("/precedents", response_model=PrecedentPage)
async def list_precedents(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    category: str | None = Query(default=None, max_length=40),
    limit: int = Query(default=20, ge=1, le=50),
    offset: int = Query(default=0, ge=0),
) -> PrecedentPage:
    items, total = await precedent_service.list_precedents(db, current_user, category=category or None, limit=limit, offset=offset)
    return PrecedentPage(items=[Precedent(**item) for item in items], total=total)


@router.post("/precedents", response_model=Precedent)
async def create_precedent(
    payload: PrecedentCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Precedent:
    """case_id 是本人的评审编号，不是案件的公开编号。"""
    return Precedent(**await precedent_service.create_by_senior(db, current_user, payload.case_id, payload.title, payload.summary))


@router.get("/precedents/{precedent_id}", response_model=PrecedentDetail)
async def precedent_detail(
    precedent_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PrecedentDetail:
    return PrecedentDetail(**await precedent_service.precedent_detail(db, current_user, precedent_id))


@router.put("/precedents/{precedent_id}", response_model=Precedent)
async def update_precedent(
    precedent_id: int,
    payload: PrecedentUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Precedent:
    return Precedent(**await precedent_service.update_precedent(db, current_user, precedent_id, payload.title, payload.summary))


@router.delete("/precedents/{precedent_id}", response_model=PrecedentState)
async def withdraw_precedent(
    precedent_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PrecedentState:
    return PrecedentState(**await precedent_service.withdraw_precedent(db, current_user, precedent_id))


# ---------------------------------------------------------------- 议事厅


@router.get("/hall/posts", response_model=HallPostPage)
async def list_hall_posts(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    # 留空表示不筛选
    kind: Literal["announcement", "proposal", "discussion", ""] | None = None,
    status_filter: Literal["open", "passed", "rejected", "adopted", "declined", "closed", ""] | None = Query(default=None, alias="status"),
    sort: Literal["active", "new", ""] | None = None,
    limit: int = Query(default=20, ge=1, le=50),
    offset: int = Query(default=0, ge=0),
) -> HallPostPage:
    items, total = await hall_service.list_posts(
        db, current_user, kind=kind or None, status_filter=status_filter or None, sort=sort or "active", limit=limit, offset=offset
    )
    return HallPostPage(items=[HallPost(**item) for item in items], total=total)


@router.post("/hall/posts", response_model=HallPost)
async def create_hall_post(
    payload: HallPostCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> HallPost:
    return HallPost(**await hall_service.create_post(db, current_user, payload.kind, payload.title, payload.content))


@router.post("/hall/reports", response_model=HallReportResult)
async def report_hall_content(
    payload: HallReportCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> HallReportResult:
    return HallReportResult(
        **await hall_service.create_report(db, current_user, payload.target_type, payload.target_id, payload.reason, payload.details)
    )


@router.delete("/hall/comments/{comment_id}", response_model=HallCommentState)
async def delete_hall_comment(
    comment_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> HallCommentState:
    return HallCommentState(**await hall_service.delete_comment(db, current_user, comment_id))


@router.get("/hall/posts/{post_id}", response_model=HallPost)
async def hall_post_detail(
    post_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> HallPost:
    return HallPost(**await hall_service.get_post(db, current_user, post_id))


@router.put("/hall/posts/{post_id}", response_model=HallPost)
async def update_hall_post(
    post_id: int,
    payload: HallPostUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> HallPost:
    return HallPost(**await hall_service.update_post(db, current_user, post_id, payload.title, payload.content))


@router.delete("/hall/posts/{post_id}", response_model=HallPostState)
async def delete_hall_post(
    post_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> HallPostState:
    return HallPostState(**await hall_service.delete_post(db, current_user, post_id))


@router.post("/hall/posts/{post_id}/vote", response_model=HallPost)
async def vote_on_hall_post(
    post_id: int,
    payload: HallVoteCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> HallPost:
    return HallPost(**await hall_service.cast_vote(db, current_user, post_id, payload.vote))


@router.get("/hall/posts/{post_id}/comments", response_model=HallCommentPage)
async def list_hall_comments(
    post_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> HallCommentPage:
    items, total = await hall_service.list_comments(db, current_user, post_id, limit=limit, offset=offset)
    return HallCommentPage(items=[HallComment(**item) for item in items], total=total)


@router.post("/hall/posts/{post_id}/comments", response_model=HallComment)
async def create_hall_comment(
    post_id: int,
    payload: HallCommentCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> HallComment:
    return HallComment(**await hall_service.create_comment(db, current_user, post_id, payload.content, payload.parent_id))
