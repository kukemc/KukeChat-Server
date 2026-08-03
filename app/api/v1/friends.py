from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.encoders import jsonable_encoder
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.models.friend import FriendRequest
from app.models.user import User
from app.schemas.common import MessageResponse
from app.schemas.friend import FriendRead, FriendRequestCreate, FriendRequestRead, TemporaryConversationBlockRead
from app.services import friends as friend_service
from app.services import response_cache
from app.websocket.manager import manager


router = APIRouter()


def cached_json_response(payload: response_cache.CachedJson, request: Request, max_age: int) -> Response:
    headers = {"Cache-Control": f"private, max-age={max_age}", "Vary": "Accept-Encoding"}
    if "gzip" in request.headers.get("accept-encoding", "").lower():
        headers["Content-Encoding"] = "gzip"
        return Response(payload.gzip, media_type="application/json", headers=headers)
    return Response(payload.plain, media_type="application/json", headers=headers)


@router.post("/requests", response_model=FriendRequestRead, status_code=201)
async def send_request(
    payload: FriendRequestCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> FriendRequest:
    request = await friend_service.create_friend_request(db, current_user, payload.receiver_id)
    response_cache.invalidate_user_friends({current_user.id, payload.receiver_id})
    response = FriendRequestRead.model_validate(request)
    await manager.send_to_user(request.receiver_id, "friend_request.created", response)
    await manager.send_to_user(current_user.id, "friend_request.created", response)
    return request


@router.get("/requests/incoming", response_model=list[FriendRequestRead])
async def incoming_requests(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[FriendRequest]:
    return await friend_service.list_incoming_requests(db, current_user.id)


@router.get("/requests/outgoing", response_model=list[FriendRequestRead])
async def outgoing_requests(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[FriendRequest]:
    return await friend_service.list_outgoing_requests(db, current_user.id)


@router.post("/requests/{request_id}/accept", response_model=FriendRequestRead)
async def accept_request(
    request_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> FriendRequest:
    request = await friend_service.accept_friend_request(db, request_id, current_user.id)
    response_cache.invalidate_user_friends({request.requester_id, request.receiver_id})
    response = FriendRequestRead.model_validate(request)
    await manager.send_to_users({request.requester_id, request.receiver_id}, "friend_request.accepted", response)
    await manager.send_to_users({request.requester_id, request.receiver_id}, "conversation.updated", {"reason": "friend_request.accepted"})
    return request


@router.get("/temporary-blocks", response_model=list[TemporaryConversationBlockRead])
async def temporary_blocks(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[TemporaryConversationBlockRead]:
    blocks = await friend_service.list_temporary_blocks(db, current_user.id)
    return [TemporaryConversationBlockRead.model_validate(block) for block in blocks]


@router.delete("/temporary-blocks/{block_id}", response_model=MessageResponse)
async def delete_temporary_block(
    block_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> MessageResponse:
    block = await friend_service.delete_temporary_block(db, current_user.id, block_id)
    await manager.send_to_user(current_user.id, "temporary_block.deleted", {"block_id": block.id, "blocked_user_id": block.blocked_user_id})
    return MessageResponse(detail="Temporary conversation block deleted")


@router.post("/requests/{request_id}/reject", response_model=FriendRequestRead)
async def reject_request(
    request_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> FriendRequest:
    request = await friend_service.reject_friend_request(db, request_id, current_user.id)
    response_cache.invalidate_user_friends({request.requester_id, request.receiver_id})
    response = FriendRequestRead.model_validate(request)
    await manager.send_to_users({request.requester_id, request.receiver_id}, "friend_request.rejected", response)
    return request


@router.get("", response_model=list[FriendRead])
async def friends(
    request: Request,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    cached = response_cache.get_user_friends(current_user.id)
    if cached is not None:
        return cached_json_response(cached, request, 10)

    rows = await friend_service.list_friends(db, current_user.id)
    data = jsonable_encoder([FriendRead(user=user, created_at=created_at) for user, created_at in rows])
    payload = response_cache.build_json(data)
    response_cache.set_user_friends(current_user.id, payload)
    return cached_json_response(payload, request, 10)


@router.delete("/{friend_id}", response_model=MessageResponse, status_code=status.HTTP_200_OK)
async def delete_friend(
    friend_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> MessageResponse:
    await friend_service.delete_friend(db, current_user.id, friend_id)
    response_cache.invalidate_user_friends({current_user.id, friend_id})
    await manager.send_to_users({current_user.id, friend_id}, "friendship.deleted", {"user_id": current_user.id, "friend_id": friend_id})
    return MessageResponse(detail="Friend deleted")
