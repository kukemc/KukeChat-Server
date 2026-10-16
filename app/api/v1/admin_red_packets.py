"""红包的后台接口（docs/red-packets.md「后台」一节），挂在 /admin/red-packets 下，每个接口都要求管理员身份。

隐私边界：后台不能借红包看私聊内容。私聊红包的祝福语在这里一律是 null，这些接口也不返回任何消息内容。
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import get_current_admin
from app.models.user import User
from app.schemas.red_packet import (
    AdminRedPacketDetail,
    AdminRedPacketPage,
    AdminRedPacketRevoke,
    AdminRedPacketSettings,
    AdminRedPacketSettingsWrite,
)
from app.services import red_packets as red_packet_service

router = APIRouter()


@router.get("/red-packets/settings", response_model=AdminRedPacketSettings)
async def admin_red_packet_settings(
    _admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AdminRedPacketSettings:
    return AdminRedPacketSettings(**await red_packet_service.admin_settings(db))


@router.put("/red-packets/settings", response_model=AdminRedPacketSettings)
async def admin_update_red_packet_settings(
    payload: AdminRedPacketSettingsWrite,
    admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AdminRedPacketSettings:
    """只改传了的项，改动写审计日志。另一个 worker 进程最多 5 秒后看到新的设置。"""
    return AdminRedPacketSettings(**await red_packet_service.admin_update_settings(db, admin, payload.model_dump(exclude_unset=True)))


@router.get("/red-packets", response_model=AdminRedPacketPage)
@router.get("/red-packets/", response_model=AdminRedPacketPage, include_in_schema=False)
async def admin_list_red_packets(
    _admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
    status: Literal["active", "finished", "expired", "revoked"] | None = None,
    kind: Literal["normal", "lucky"] | None = None,
    sender_id: int | None = Query(default=None, ge=1),
    conversation_id: int | None = Query(default=None, ge=1),
    # 创建时间的范围：created_from <= created_at < created_to
    created_from: datetime | None = None,
    created_to: datetime | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> AdminRedPacketPage:
    return AdminRedPacketPage(
        **await red_packet_service.admin_list(
            db,
            status_filter=status,
            kind=kind,
            sender_id=sender_id,
            conversation_id=conversation_id,
            created_from=created_from,
            created_to=created_to,
            limit=limit,
            offset=offset,
        )
    )


@router.get("/red-packets/{packet_id}", response_model=AdminRedPacketDetail)
async def admin_red_packet_detail(
    packet_id: int,
    _admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AdminRedPacketDetail:
    return AdminRedPacketDetail(**await red_packet_service.admin_detail(db, packet_id))


@router.post("/red-packets/{packet_id}/revoke", response_model=AdminRedPacketDetail)
async def admin_revoke_red_packet(
    packet_id: int,
    payload: AdminRedPacketRevoke,
    admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AdminRedPacketDetail:
    """提前结束：没领的退回发送者，写审计日志。不动会话里的那条消息。"""
    return AdminRedPacketDetail(**await red_packet_service.admin_revoke(db, admin, packet_id, payload.reason))
