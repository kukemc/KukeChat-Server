"""风纪系统 v2 三期：判例和议事厅的后台接口（docs/jury-v2.md 3.8）。

和 admin.py 一样挂在 /admin 下，每个接口都要求管理员身份。
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import get_current_admin
from app.models.user import User
from app.schemas.jury_hall import (
    AdminHallAnnouncementCreate,
    AdminHallCommentModerate,
    AdminHallCommentRead,
    AdminHallDecision,
    AdminHallPostDetail,
    AdminHallPostPage,
    AdminHallPostRead,
    AdminHallReportHandle,
    AdminHallReportPage,
    AdminHallReportRead,
    AdminPrecedentPage,
    AdminPrecedentRead,
    AdminPrecedentWithdraw,
    PrecedentCreate,
    PrecedentUpdate,
)
from app.services import jury_hall as hall_service
from app.services import jury_precedents as precedent_service


router = APIRouter()


# ---------------------------------------------------------------- 判例


@router.get("/jury/precedents", response_model=AdminPrecedentPage)
async def admin_list_precedents(
    admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
    # 留空表示不筛选（含已撤下的）
    status_filter: Literal["active", "withdrawn", ""] | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> AdminPrecedentPage:
    items, total = await precedent_service.admin_list(db, viewer_id=admin.id, status_filter=status_filter or None, limit=limit, offset=offset)
    return AdminPrecedentPage(items=[AdminPrecedentRead(**item) for item in items], total=total)


@router.post("/jury/precedents", response_model=AdminPrecedentRead)
async def admin_create_precedent(
    payload: PrecedentCreate,
    admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AdminPrecedentRead:
    """case_id 是案件的公开编号。"""
    return AdminPrecedentRead(**await precedent_service.admin_create(db, admin, payload.case_id, payload.title, payload.summary))


@router.put("/jury/precedents/{precedent_id}", response_model=AdminPrecedentRead)
async def admin_update_precedent(
    precedent_id: int,
    payload: PrecedentUpdate,
    admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AdminPrecedentRead:
    return AdminPrecedentRead(**await precedent_service.admin_update(db, admin, precedent_id, payload.title, payload.summary))


@router.delete("/jury/precedents/{precedent_id}", response_model=AdminPrecedentRead)
async def admin_withdraw_precedent(
    precedent_id: int,
    admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
    payload: AdminPrecedentWithdraw | None = None,
) -> AdminPrecedentRead:
    return AdminPrecedentRead(**await precedent_service.admin_withdraw(db, admin, precedent_id, payload.note if payload else None))


# ---------------------------------------------------------------- 议事厅


@router.get("/jury/hall/posts", response_model=AdminHallPostPage)
async def admin_list_hall_posts(
    _admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
    kind: Literal["announcement", "proposal", "discussion", ""] | None = None,
    status_filter: Literal["open", "passed", "rejected", "adopted", "declined", "closed", ""] | None = Query(default=None, alias="status"),
    state: Literal["visible", "deleted", "hidden", ""] | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> AdminHallPostPage:
    items, total = await hall_service.admin_list_posts(
        db, kind=kind or None, status_filter=status_filter or None, state=state or None, limit=limit, offset=offset
    )
    return AdminHallPostPage(items=[AdminHallPostRead(**item) for item in items], total=total)


@router.post("/jury/hall/posts", response_model=AdminHallPostRead)
async def admin_create_hall_announcement(
    payload: AdminHallAnnouncementCreate,
    admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AdminHallPostRead:
    return AdminHallPostRead(**await hall_service.admin_create_announcement(db, admin, payload.title, payload.content, payload.pinned))


@router.get("/jury/hall/reports", response_model=AdminHallReportPage)
async def admin_list_hall_reports(
    _admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
    status_filter: Literal["pending", "handled", "dismissed", ""] | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> AdminHallReportPage:
    items, total = await hall_service.admin_list_reports(db, status_filter=status_filter or None, limit=limit, offset=offset)
    return AdminHallReportPage(items=[AdminHallReportRead(**item) for item in items], total=total)


@router.post("/jury/hall/reports/{report_id}/handle", response_model=AdminHallReportRead)
async def admin_handle_hall_report(
    report_id: int,
    payload: AdminHallReportHandle,
    admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AdminHallReportRead:
    return AdminHallReportRead(**await hall_service.admin_handle_report(db, admin, report_id, payload.action, payload.note))


@router.post("/jury/hall/comments/{comment_id}/moderate", response_model=AdminHallCommentRead)
async def admin_moderate_hall_comment(
    comment_id: int,
    payload: AdminHallCommentModerate,
    admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AdminHallCommentRead:
    """不经过举报，直接删除 / 恢复一条评论。"""
    return AdminHallCommentRead(**await hall_service.admin_moderate_comment(db, admin, comment_id, payload.action, payload.note))


@router.get("/jury/hall/posts/{post_id}", response_model=AdminHallPostDetail)
async def admin_hall_post_detail(
    post_id: int,
    _admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AdminHallPostDetail:
    return AdminHallPostDetail(**await hall_service.admin_post_detail(db, post_id))


@router.post("/jury/hall/posts/{post_id}/decision", response_model=AdminHallPostRead)
async def admin_decide_hall_post(
    post_id: int,
    payload: AdminHallDecision,
    admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AdminHallPostRead:
    return AdminHallPostRead(**await hall_service.admin_decide(db, admin, post_id, payload.action, payload.note))
