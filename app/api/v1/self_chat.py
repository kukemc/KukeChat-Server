"""「我的电脑 / 我的手机」的接口（docs/self-chat.md）：GET /conversations/self，和 /conversations/{cid}/transfers/... 临时文件。

普通消息（文字、图片、语音、表情、撤回、引用、合并转发、搜索）照常用 conversations.py 里的接口。
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.responses import JSONResponse
from fastapi.encoders import jsonable_encoder
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.conversations import online_conversation_member_ids, send_message_updates
from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.models.user import User
from app.schemas.conversation import ConversationRead
from app.schemas.self_chat import (
    SelfTransferComplete,
    SelfTransferCompleteResult,
    SelfTransferContentResult,
    SelfTransferCreate,
    SelfTransferCreated,
    SelfTransferLink,
    SelfTransferList,
    SelfTransferRead,
)
from app.services import conversations as conversation_service
from app.services import messages as message_service
from app.services import response_cache
from app.services import self_chat as self_chat_service

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/self", response_model=ConversationRead)
async def self_conversation(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ConversationRead:
    """我的「我的设备」会话，没有就建（幂等）。"""
    conversation = await self_chat_service.get_or_create(db, current_user)
    response_cache.invalidate_user_conversations({current_user.id})
    return await conversation_service.build_conversation_read(db, conversation, current_user.id)


@router.get("/{conversation_id}/transfers", response_model=SelfTransferList)
async def list_transfers(
    conversation_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SelfTransferList:
    return SelfTransferList(**await self_chat_service.list_transfers(db, current_user, conversation_id))


@router.post("/{conversation_id}/transfers", response_model=SelfTransferCreated, status_code=status.HTTP_201_CREATED)
async def create_transfer(
    conversation_id: int,
    payload: SelfTransferCreate,
    request: Request,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SelfTransferCreated:
    result = await self_chat_service.create_transfer(
        db,
        current_user,
        conversation_id,
        name=payload.name,
        size=payload.size,
        content_type=payload.content_type,
        device=self_chat_service.device_of(request),
    )
    return SelfTransferCreated(**result)


@router.get("/{conversation_id}/transfers/{transfer_id}", response_model=SelfTransferRead)
async def get_transfer(
    conversation_id: int,
    transfer_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SelfTransferRead:
    return SelfTransferRead(**await self_chat_service.get_transfer(db, current_user, conversation_id, transfer_id))


@router.put("/{conversation_id}/transfers/{transfer_id}/content", response_model=SelfTransferContentResult)
async def upload_transfer_content(
    conversation_id: int,
    transfer_id: int,
    request: Request,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SelfTransferContentResult:
    """没开直传时的上传：请求体就是文件本身（Content-Type: application/octet-stream）。"""
    return SelfTransferContentResult(**await self_chat_service.receive_content(db, current_user, conversation_id, transfer_id, request))


@router.post("/{conversation_id}/transfers/{transfer_id}/complete", response_model=SelfTransferCompleteResult, status_code=status.HTTP_201_CREATED)
async def complete_transfer(
    conversation_id: int,
    transfer_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    payload: SelfTransferComplete | None = None,
) -> Response:
    transfer, message, created = await self_chat_service.complete_transfer(
        db, current_user, conversation_id, transfer_id, client_nonce=payload.client_nonce if payload is not None else None
    )
    message_data = None
    if message is not None:
        message_data = message_service.build_new_message_read(message, current_user.id) if created else await message_service.build_message_read(db, message, current_user.id)
    if created and message is not None:
        # 和普通发消息一样广播 message.created（发到自己所有在线的设备）；不送机器审核、不发后台刷新信号（docs/self-chat.md 第 4 节）
        member_ids = await online_conversation_member_ids(db, conversation_id)
        response_cache.invalidate_user_conversations(set(member_ids) | {current_user.id})
        try:
            await send_message_updates(db, "message.created", message, member_ids)
        except Exception:
            logger.exception("Committed transfer message %s realtime delivery failed", message.id)
    body = SelfTransferCompleteResult(transfer=SelfTransferRead(**transfer), message=message_data)
    return JSONResponse(jsonable_encoder(body), status_code=status.HTTP_201_CREATED if created else status.HTTP_200_OK)


@router.get("/{conversation_id}/transfers/{transfer_id}/download")
async def download_transfer(
    conversation_id: int,
    transfer_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    return await self_chat_service.download(db, current_user, conversation_id, transfer_id)


@router.get("/{conversation_id}/transfers/{transfer_id}/link", response_model=SelfTransferLink)
async def transfer_link(
    conversation_id: int,
    transfer_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SelfTransferLink:
    return SelfTransferLink(**await self_chat_service.download_link(db, current_user, conversation_id, transfer_id))
