"""群公告 v2 的接口（docs/group-announcements.md），挂在 /conversations 前缀下。"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.conversations import online_conversation_member_ids, send_conversation_updates, send_message_updates
from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.models.user import User
from app.schemas.common import MessageResponse
from app.schemas.group_announcement import (
    GroupAnnouncementConfirmationPage,
    GroupAnnouncementCreate,
    GroupAnnouncementRead,
    GroupAnnouncementSeenRead,
    GroupAnnouncementUpdate,
)
from app.services import group_announcements as announcement_service
from app.websocket.manager import manager

router = APIRouter()


def _event_payload(read: GroupAnnouncementRead) -> GroupAnnouncementRead:
    # 同一份数据推给所有人：「我的」字段没有意义，统一给 false，客户端保留本地值
    return read.model_copy(update={"my_seen": False, "my_confirmed": False})


@router.get("/{conversation_id}/announcements", response_model=list[GroupAnnouncementRead])
async def list_group_announcements(
    conversation_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[GroupAnnouncementRead]:
    return await announcement_service.list_announcements(db, conversation_id, current_user.id)


# 固定路径放在带 {announcement_id} 的路径前面
@router.get("/{conversation_id}/announcements/popups", response_model=list[GroupAnnouncementRead])
async def group_announcement_popups(
    conversation_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[GroupAnnouncementRead]:
    return await announcement_service.pending_popups(db, conversation_id, current_user.id)


@router.get("/{conversation_id}/announcements/{announcement_id}", response_model=GroupAnnouncementRead)
async def get_group_announcement(
    conversation_id: int,
    announcement_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> GroupAnnouncementRead:
    return await announcement_service.get_announcement(db, conversation_id, announcement_id, current_user.id)


@router.post("/{conversation_id}/announcements", response_model=GroupAnnouncementRead, status_code=status.HTTP_201_CREATED)
async def create_group_announcement(
    conversation_id: int,
    payload: GroupAnnouncementCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> GroupAnnouncementRead:
    read, card = await announcement_service.create_announcement(db, conversation_id, current_user, payload)
    member_ids = await online_conversation_member_ids(db, conversation_id)
    await manager.send_to_users(member_ids, "group.announcement.created", _event_payload(read))
    await send_message_updates(db, "message.created", card, member_ids)
    await send_conversation_updates(db, conversation_id, member_ids)
    return read


@router.patch("/{conversation_id}/announcements/{announcement_id}", response_model=GroupAnnouncementRead)
async def update_group_announcement(
    conversation_id: int,
    announcement_id: int,
    payload: GroupAnnouncementUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> GroupAnnouncementRead:
    read, card = await announcement_service.update_announcement(db, conversation_id, announcement_id, current_user, payload)
    member_ids = await online_conversation_member_ids(db, conversation_id)
    await manager.send_to_users(member_ids, "group.announcement.updated", _event_payload(read))
    if card is not None:
        await send_message_updates(db, "message.created", card, member_ids)
    await send_conversation_updates(db, conversation_id, member_ids)
    return read


@router.delete("/{conversation_id}/announcements/{announcement_id}", response_model=MessageResponse)
async def delete_group_announcement(
    conversation_id: int,
    announcement_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> MessageResponse:
    card_ids = await announcement_service.delete_announcement(db, conversation_id, announcement_id, current_user)
    member_ids = await online_conversation_member_ids(db, conversation_id)
    await manager.send_to_users(
        member_ids,
        "group.announcement.deleted",
        {"conversation_id": conversation_id, "announcement_id": announcement_id, "card_message_ids": card_ids},
    )
    await send_conversation_updates(db, conversation_id, member_ids)
    return MessageResponse(detail="Announcement deleted")


@router.post("/{conversation_id}/announcements/{announcement_id}/seen", response_model=GroupAnnouncementSeenRead)
async def mark_group_announcement_seen(
    conversation_id: int,
    announcement_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> GroupAnnouncementSeenRead:
    return await announcement_service.mark_seen(db, conversation_id, announcement_id, current_user.id)


@router.post("/{conversation_id}/announcements/{announcement_id}/confirm", response_model=GroupAnnouncementRead)
async def confirm_group_announcement(
    conversation_id: int,
    announcement_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> GroupAnnouncementRead:
    read = await announcement_service.confirm(db, conversation_id, announcement_id, current_user.id)
    staff_ids = await announcement_service.staff_member_ids(db, conversation_id)
    online = await manager.online_user_ids_among(list({*staff_ids, current_user.id}))
    if online:
        await manager.send_to_users(
            online,
            "group.announcement.confirmations",
            {
                "conversation_id": conversation_id,
                "announcement_id": announcement_id,
                "confirmed_count": read.confirmed_count,
                "member_count": read.member_count,
            },
        )
    return read


@router.get("/{conversation_id}/announcements/{announcement_id}/confirmations", response_model=GroupAnnouncementConfirmationPage)
async def group_announcement_confirmations(
    conversation_id: int,
    announcement_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    status_filter: Annotated[Literal["confirmed", "unconfirmed"], Query(alias="status")] = "confirmed",
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> GroupAnnouncementConfirmationPage:
    return await announcement_service.confirmation_page(
        db, conversation_id, announcement_id, current_user.id, status_filter, limit, offset
    )
