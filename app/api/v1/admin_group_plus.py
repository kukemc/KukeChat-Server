"""群升级的后台接口（docs/group-plus.md 第 9 节），挂在 /admin/group-plus 下，每个接口都要求管理员身份。

隐私边界：这些接口只返回群的档位、人数、群主、流水，不返回任何消息内容。
"""

from __future__ import annotations

import logging
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.conversations import online_conversation_member_ids
from app.core.database import get_db
from app.core.dependencies import get_current_admin
from app.models.user import User
from app.schemas.group_plus import (
    AdminGroupMemberLimitResult,
    AdminGroupMemberLimitWrite,
    AdminGroupPlusGrant,
    AdminGroupPlusGrantResult,
    AdminGroupPlusGroup,
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


async def announce_plus_updated(db: AsyncSession, state: dict[str, Any], reason: str) -> None:
    """group.plus.updated 发给群里的在线成员，并让他们的会话列表缓存失效。尽力而为，失败只记日志。

    state 是 { conversation_id, plus_tier, plus_expires_at, plus_permanent }。reason：grant / revoke / member_limit。
    scripts/admin_group_tools.py 也调它（多 worker 部署配了 REDIS_URL 时事件能经 Redis 转给在线的人）。
    """
    conversation_id = state["conversation_id"]
    try:
        member_ids = await online_conversation_member_ids(db, conversation_id)
        response_cache.invalidate_user_conversations(set(member_ids))
        await manager.send_to_users(
            member_ids,
            "group.plus.updated",
            {
                "conversation_id": conversation_id,
                "plus_tier": state["plus_tier"],
                "plus_expires_at": state["plus_expires_at"],
                "plus_permanent": bool(state.get("plus_permanent")),
                "reason": reason,
            },
        )
    except Exception:
        logger.exception("Failed to broadcast group.plus.updated")


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
    # true：列所有群（含免费群），按群名 / id 找群设人数上限用
    include_free: bool = False,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> AdminGroupPlusGroupPage:
    return AdminGroupPlusGroupPage(
        **await plus_service.admin_groups(
            db,
            tier=tier,
            expiring_within_days=expiring_within_days,
            q=q,
            include_expired=include_expired,
            limit=limit,
            offset=offset,
            include_free=include_free,
        )
    )


@router.get("/group-plus/groups/{conversation_id}", response_model=AdminGroupPlusGroup)
async def admin_group_plus_group(
    conversation_id: int,
    _admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AdminGroupPlusGroup:
    return AdminGroupPlusGroup(**await plus_service.admin_group(db, conversation_id))


@router.put("/group-plus/groups/{conversation_id}/member-limit", response_model=AdminGroupMemberLimitResult)
async def admin_group_plus_member_limit(
    conversation_id: int,
    payload: AdminGroupMemberLimitWrite,
    admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AdminGroupMemberLimitResult:
    """单独设这个群的人数上限：null 跟随档位 / 0 不限 / 1–100000。写审计日志 group_plus.member_limit，广播 group.plus.updated（reason = member_limit）。"""
    result = await plus_service.admin_set_member_limit(db, admin, conversation_id, payload.member_limit_override)
    if result["changed"]:
        await announce_plus_updated(db, result["conversation"], "member_limit")
    return AdminGroupMemberLimitResult(**result)


@router.post("/group-plus/grant", response_model=AdminGroupPlusGrantResult)
async def admin_group_plus_grant(
    payload: AdminGroupPlusGrant,
    admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AdminGroupPlusGrantResult:
    """赠送 / 取消（tier = free）。permanent: true 送成永久。不收费，写审计日志和流水，广播 group.plus.updated。"""
    result = await plus_service.admin_grant(db, admin, payload.model_dump())
    await announce_plus_updated(db, result["conversation"], result["reason"])
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
