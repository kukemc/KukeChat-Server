"""群升级的后台接口（docs/group-plus.md 第 9 节），挂在 /admin/group-plus 下，每个接口都要求管理员身份。

隐私边界：这些接口只返回群的档位、人数、群主、流水，不返回任何消息内容。
"""

from __future__ import annotations

import logging
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.conversations import online_conversation_member_ids
from app.core.database import get_db
from app.core.dependencies import get_current_admin
from app.models.user import User
from app.schemas.group_plus import (
    AdminGroupPlusGrant,
    AdminGroupPlusGrantResult,
    AdminGroupPlusGroupPage,
    AdminGroupPlusRecordPage,
    AdminGroupPlusSettings,
    AdminGroupPlusSettingsWrite,
)
from app.services import group_plus as plus_service
from app.services import response_cache
from app.websocket.manager import manager

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/group-plus/settings", response_model=AdminGroupPlusSettings)
async def admin_group_plus_settings(
    _admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AdminGroupPlusSettings:
    return AdminGroupPlusSettings(**await plus_service.admin_settings(db))


@router.put("/group-plus/settings", response_model=AdminGroupPlusSettings)
async def admin_update_group_plus_settings(
    payload: AdminGroupPlusSettingsWrite,
    admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AdminGroupPlusSettings:
    return AdminGroupPlusSettings(**await plus_service.admin_update_settings(db, admin, payload.model_dump(exclude_unset=True)))


@router.get("/group-plus/groups", response_model=AdminGroupPlusGroupPage)
async def admin_group_plus_groups(
    _admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
    tier: Literal["pro", "pro_plus", "max"] | None = None,
    expiring_within_days: int | None = Query(default=None, ge=1, le=3650),
    q: str | None = Query(default=None, max_length=100),
    include_expired: bool = False,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> AdminGroupPlusGroupPage:
    return AdminGroupPlusGroupPage(
        **await plus_service.admin_groups(
            db, tier=tier, expiring_within_days=expiring_within_days, q=q, include_expired=include_expired, limit=limit, offset=offset
        )
    )


@router.post("/group-plus/grant", response_model=AdminGroupPlusGrantResult)
async def admin_group_plus_grant(
    payload: AdminGroupPlusGrant,
    admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AdminGroupPlusGrantResult:
    """赠送 / 取消（tier = free）。不收费，写审计日志和流水，广播 group.plus.updated。"""
    result = await plus_service.admin_grant(db, admin, payload.model_dump())
    conversation_id = result["conversation"]["conversation_id"]
    try:
        member_ids = await online_conversation_member_ids(db, conversation_id)
        response_cache.invalidate_user_conversations(set(member_ids))
        await manager.send_to_users(
            member_ids,
            "group.plus.updated",
            {
                "conversation_id": conversation_id,
                "plus_tier": result["conversation"]["plus_tier"],
                "plus_expires_at": result["conversation"]["plus_expires_at"],
                "reason": result["reason"],
            },
        )
    except Exception:
        logger.exception("Failed to broadcast group.plus.updated")
    return AdminGroupPlusGrantResult(**result)


@router.get("/group-plus/records", response_model=AdminGroupPlusRecordPage)
async def admin_group_plus_records(
    _admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
    conversation_id: int | None = Query(default=None, ge=1),
    user_id: int | None = Query(default=None, ge=1),
    source: Literal["purchase", "grant", "revoke"] | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> AdminGroupPlusRecordPage:
    return AdminGroupPlusRecordPage(
        **await plus_service.admin_records(db, conversation_id=conversation_id, user_id=user_id, source=source, limit=limit, offset=offset)
    )
