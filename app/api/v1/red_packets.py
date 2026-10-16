"""红包的用户侧接口（docs/red-packets.md），挂在 /red-packets 下，都要求登录。"""

from __future__ import annotations

import logging
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.conversations import online_conversation_member_ids, schedule_realtime_task, send_admin_message_signal, send_message_updates
from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.models.user import User
from app.schemas.red_packet import RedPacketClaimResult, RedPacketConfig, RedPacketCreate, RedPacketPage, RedPacketPending, RedPacketPendingList, RedPacketRead, RedPacketSendResult
from app.services import messages as message_service
from app.services import moderation as moderation_service
from app.services import red_packets as red_packet_service
from app.services import response_cache
from app.websocket.manager import manager

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/config", response_model=RedPacketConfig)
async def red_packet_config(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> RedPacketConfig:
    """开关、限制和我今天已经发了几个。功能关着也能调（enabled 为 false：不能发新的，已经发出的照常能领）。"""
    return RedPacketConfig(**await red_packet_service.config_view(db, current_user))


# 带不带结尾的斜杠都接：契约写的是 POST /，不能让客户端吃一次 307
@router.post("", response_model=RedPacketSendResult, status_code=201)
@router.post("/", response_model=RedPacketSendResult, status_code=201, include_in_schema=False)
async def send_red_packet(
    payload: RedPacketCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> RedPacketSendResult:
    """发红包：立刻扣整包金额，会话里落一条 type = red_packet 的消息。消息的广播和发普通消息一样。"""
    user_id, whitelisted = current_user.id, current_user.content_moderation_whitelisted
    result, message = await red_packet_service.send_red_packet(db, current_user, payload.model_dump())
    conversation_id = message.conversation_id
    message_data = message_service.build_new_message_read(message, user_id)
    member_ids = await online_conversation_member_ids(db, conversation_id)
    response_cache.invalidate_user_conversations(set(member_ids) | {user_id})
    try:
        await send_message_updates(db, "message.created", message, member_ids)
    except Exception:
        logger.exception("Committed red packet message %s realtime delivery failed", message.id)
    schedule_realtime_task(send_admin_message_signal("admin.message.changed", message, "created"))
    previous_level = getattr(message, "_level_previous", None)
    current_level = getattr(message, "_level_current", None)
    if isinstance(previous_level, int) and isinstance(current_level, int) and current_level > previous_level:
        schedule_realtime_task(
            manager.send_to_users(
                member_ids,
                "group.member.level_updated",
                {"conversation_id": conversation_id, "user_id": user_id, "level": current_level, "level_exp": getattr(message, "_sender_level_exp", 0)},
            )
        )
    if not whitelisted:
        # 祝福语发之前已经审过一遍；这里和别的消息一样再走一次异步审核（审核服务当时不可用的话靠它兜底）
        moderation_service.schedule_message_moderation(message.id)
    return RedPacketSendResult(red_packet=result["red_packet"], message=message_data, balance=result["balance"])


@router.get("/mine", response_model=RedPacketPage)
async def my_red_packets(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    direction: Literal["sent", "received"] = "received",
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> RedPacketPage:
    return RedPacketPage(**await red_packet_service.list_mine(db, current_user, direction, limit, offset))


@router.get("/pending", response_model=RedPacketPendingList)
async def list_pending_red_packets(
    conversation_id: Annotated[int, Query(ge=1)],
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> RedPacketPendingList:
    """这个会话里我还能领的红包（新的在前，最多 20 个）。不是会话成员 403。"""
    packets = await red_packet_service.pending_in_conversation(db, current_user.id, conversation_id)
    return RedPacketPendingList(
        items=[
            RedPacketPending(
                id=packet.id, message_id=packet.message_id, sender_id=packet.sender_id, kind=packet.kind, greeting=packet.greeting,  # type: ignore[arg-type]
                count=packet.count, claimed_count=packet.claimed_count, expires_at=packet.expires_at, created_at=packet.created_at,
            )
            for packet in packets
        ]
    )


@router.get("/{packet_id}", response_model=RedPacketRead)
async def get_red_packet(
    packet_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> RedPacketRead:
    return RedPacketRead(**await red_packet_service.get_red_packet(db, current_user, packet_id))


@router.post("/{packet_id}/claim", response_model=RedPacketClaimResult)
async def claim_red_packet(
    packet_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> RedPacketClaimResult:
    user_id = current_user.id
    result = await red_packet_service.claim(db, current_user, packet_id)
    # 会话列表里的 red_packet_pending 变了，别让这个人拿到 5 秒前缓存的那份
    response_cache.invalidate_user_conversations({user_id})
    return RedPacketClaimResult(**result)
