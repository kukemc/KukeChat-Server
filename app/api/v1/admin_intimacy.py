"""好友亲密度的后台接口（docs/intimacy.md 第 6 节），挂在 /admin/intimacy 下，都要求管理员身份。

隐私边界：后台只能看到每对好友的亲密度、等级和徽章数量，看不到聊天条数、深夜 / 清晨互动这些私聊行为的明细，也看不到是哪几枚徽章。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import get_current_admin
from app.models.user import User
from app.schemas.intimacy import (
    AdminBackfillResult,
    AdminIntimacyAdjust,
    AdminIntimacyAdjustResult,
    AdminIntimacyOverview,
    AdminIntimacySettings,
    AdminIntimacySettingsWrite,
    AdminUserBonds,
)
from app.services import intimacy as intimacy_service
from app.services.reports import log_admin_action

router = APIRouter()


@router.get("/intimacy/settings", response_model=AdminIntimacySettings)
async def admin_intimacy_settings(
    _admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AdminIntimacySettings:
    return AdminIntimacySettings(**await intimacy_service.get_settings(db, fresh=True))


@router.put("/intimacy/settings", response_model=AdminIntimacySettings)
async def admin_update_intimacy_settings(
    payload: AdminIntimacySettingsWrite,
    admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AdminIntimacySettings:
    return AdminIntimacySettings(**await intimacy_service.admin_update_settings(db, admin, payload.model_dump(exclude_unset=True)))


@router.get("/intimacy/overview", response_model=AdminIntimacyOverview)
async def admin_intimacy_overview(
    _admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AdminIntimacyOverview:
    return AdminIntimacyOverview(**await intimacy_service.admin_overview(db))


@router.get("/intimacy/users/{user_id}", response_model=AdminUserBonds)
async def admin_intimacy_user(
    user_id: int,
    _admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AdminUserBonds:
    return AdminUserBonds(**await intimacy_service.admin_user_bonds(db, user_id))


@router.post("/intimacy/adjust", response_model=AdminIntimacyAdjustResult)
async def admin_intimacy_adjust(
    payload: AdminIntimacyAdjust,
    admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AdminIntimacyAdjustResult:
    return AdminIntimacyAdjustResult(**await intimacy_service.admin_adjust(db, admin, payload.user_id, payload.friend_id, payload.delta, payload.note))


@router.post("/intimacy/backfill", response_model=AdminBackfillResult)
async def admin_intimacy_backfill(
    admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AdminBackfillResult:
    """再跑一遍回填：只处理还没有亲密度记录的好友，可以反复跑。"""
    admin_id = admin.id
    stats = await intimacy_service.backfill(db)
    await log_admin_action(db, admin_id, "intimacy.backfill", None, None, stats)
    await db.commit()
    return AdminBackfillResult(**stats)
