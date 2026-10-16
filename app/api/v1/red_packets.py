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
from app.schemas.message import MessageRead
from app.schemas.red_packet import RedPacketClaimResult, RedPacketConfig, RedPacketCreate, RedPacketPage, RedPacketPending, RedPacketPendingList, RedPacketRead, RedPacketSendResult
from app.services import intimacy as intimacy_service
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
    message_data = await _deliver_created(db, message, user_id)
    if not whitelisted:
        # 祝福语、接龙口令发之前已经审过一遍；这里和别的消息一样再走一次异步审核（审核服务当时不可用的话靠它兜底）
        moderation_service.schedule_message_moderation(message.id)
    return RedPacketSendResult(red_packet=result["red_packet"], message=message_data, balance=result["balance"])


async def _deliver_created(db: AsyncSession, message, user_id: int) -> MessageRead:
    """已经提交的一条新消息：和发普通消息一样广播（message.created、会话列表缓存、后台信号、群等级）。推送失败只记日志。"""
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
    return message_data


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
    return RedPacketPendingList(items=[RedPacketPending(**item) for item in await red_packet_service.pending_items(db, packets)])


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
    result, chain_message = await red_packet_service.claim(db, current_user, packet_id)
    # 会话列表里的 red_packet_pending 变了，别让这个人拿到 5 秒前缓存的那份
    response_cache.invalidate_user_conversations({user_id})
    message_data = None
    if chain_message is not None:
        # 接龙红包：领取时自动发的那句话已经和领取一起提交了，现在照普通消息广播。
        # 不再单独送异步审核：口令是发红包的人写的，发之前审过，红包消息的异步审核也会再看一遍（算在发的人头上）
        message_data = await _deliver_created(db, chain_message, user_id)
        intimacy_service.schedule_message(chain_message.conversation_id, user_id, chain_message.type, chain_message.created_at, bind=db.bind)
    return RedPacketClaimResult(**result, message=message_data)
