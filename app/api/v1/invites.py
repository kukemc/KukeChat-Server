from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Path
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.models.enums import InviteTargetType
from app.models.user import User
from app.schemas.invite import InviteAcceptRead, InviteLinkRead, InviteResolveRead
from app.services import invites as invite_service
from app.services import conversations as conversation_service
from app.services import messages as message_service
from app.services import bots as bot_service
from app.websocket.manager import bot_manager, manager


router = APIRouter()


def display_name(user: User, fallback: str) -> str:
    return user.nickname or user.username or fallback


async def _send_invite_join_system_message(db: AsyncSession, conversation_id: int, user: User, member_ids: list[int]) -> None:
    message = await message_service.create_system_message(
        db,
        conversation_id,
        user,
        f"{display_name(user, f'用户 {user.id}')} 加入了群聊",
        {"system_event": "group.member.joined", "user_id": user.id, "source": "invite"},
    )
    online_ids = set(member_ids) & await manager.online_user_ids_global()
    for member_id in online_ids:
        await manager.send_to_user(member_id, "message.created", await message_service.build_message_read(db, message, member_id))


async def _send_bot_conversation_event(db: AsyncSession, event_type: str, conversation_id: int, data: dict) -> None:
    bot_ids = await bot_service.bot_ids_for_member_event(db, conversation_id)
    if bot_ids:
        await bot_manager.send_to_bots(bot_ids, event_type, data)


async def _send_conversation_updates(db: AsyncSession, conversation_id: int, member_ids: list[int]) -> None:
    online_ids = set(member_ids) & await manager.online_user_ids_global()
    if online_ids:
        await manager.send_to_users(online_ids, "conversation.updated", {"conversation_id": conversation_id, "reason": "invite.accepted"})


@router.post("/group/{conversation_id}", response_model=InviteLinkRead)
async def create_group_invite(
    conversation_id: Annotated[int, Path(gt=0)],
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> InviteLinkRead:
    return await invite_service.get_or_create_invite_link(db, InviteTargetType.GROUP, conversation_id, current_user)


@router.post("/user/{user_id}", response_model=InviteLinkRead)
async def create_user_invite(
    user_id: Annotated[int, Path(gt=0)],
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> InviteLinkRead:
    return await invite_service.get_or_create_invite_link(db, InviteTargetType.USER, user_id, current_user)


@router.get("/{token}", response_model=InviteResolveRead)
async def resolve_invite(
    token: Annotated[str, Path(min_length=6, max_length=80)],
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> InviteResolveRead:
    return await invite_service.resolve_invite(db, token, current_user)


@router.post("/{token}/accept", response_model=InviteAcceptRead)
async def accept_invite(
    token: Annotated[str, Path(min_length=6, max_length=80)],
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> InviteAcceptRead:
    result = await invite_service.accept_invite(db, token, current_user)
    if result.target_type == InviteTargetType.GROUP and result.conversation_id is not None:
        member_ids = await conversation_service.conversation_member_ids(db, result.conversation_id)
        if result.joined_now:
            await _send_invite_join_system_message(db, result.conversation_id, current_user, member_ids)
            event_data = {"conversation_id": result.conversation_id, "user_id": current_user.id, "actor_id": current_user.id, "source": "invite"}
            await manager.send_to_users(member_ids, "group.member.joined", event_data)
            await _send_bot_conversation_event(db, "group.member.joined", result.conversation_id, event_data)
        else:
            await manager.send_to_users(member_ids, "group.member.joined", {"conversation_id": result.conversation_id, "user_id": current_user.id})
        await _send_conversation_updates(db, result.conversation_id, member_ids)
    elif result.target_type == InviteTargetType.USER and result.friend_request_id is not None:
        await manager.send_to_user(result.target_id, "friend_request.created", {"request_id": result.friend_request_id})
        await manager.send_to_user(current_user.id, "friend_request.created", {"request_id": result.friend_request_id})
    return result
