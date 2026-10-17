"""挂在 /conversations 下的投票和群表情接口。"""

from __future__ import annotations

import asyncio
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.conversations import (
    online_conversation_member_ids,
    schedule_realtime_task,
    send_admin_message_signal,
    send_message_updates,
)
from app.core.database import AsyncSessionLocal, get_db
from app.core.dependencies import get_current_user
from app.models.poll import Poll
from app.models.user import User
from app.schemas.common import MessageResponse
from app.schemas.message import MessageRead
from app.schemas.poll import (
    GroupEmojiCreate,
    GroupEmojiRead,
    GroupEmojiOrder,
    GroupEmojiUpdate,
    PollCreate,
    PollState,
    PollVoteRequest,
    PollVoterPage,
)
from app.services import group_emojis as emoji_service
from app.services import messages as message_service
from app.services import moderation as moderation_service
from app.services import polls as poll_service
from app.services import response_cache
from app.websocket.manager import manager

logger = logging.getLogger(__name__)
router = APIRouter()

POLL_EVENT = "poll.updated"
EMOJI_EVENT = "conversation.emojis.updated"
# 票数变化合并推送：一个投票在这段时间内不管来多少票，只推一次最新汇总
POLL_BROADCAST_DELAY_SECONDS = 1.0
_pending_poll_broadcasts: set[int] = set()


async def _broadcast_poll(conversation_id: int, poll_id: int) -> None:
    async with AsyncSessionLocal() as db:
        poll = await db.get(Poll, poll_id)
        if poll is None:
            return
        state = poll_service.public_state(await poll_service.poll_state(db, poll, None))
        member_ids = await online_conversation_member_ids(db, conversation_id)
    if member_ids:
        await manager.send_to_users(member_ids, POLL_EVENT, PollState(**state).model_dump(mode="json", exclude={"my_option_ids"}))


async def _debounced_poll_broadcast(conversation_id: int, poll_id: int) -> None:
    try:
        await asyncio.sleep(POLL_BROADCAST_DELAY_SECONDS)
    finally:
        _pending_poll_broadcasts.discard(poll_id)
    await _broadcast_poll(conversation_id, poll_id)


def _schedule_poll_broadcast(conversation_id: int, poll_id: int, *, immediate: bool = False) -> None:
    if immediate:
        schedule_realtime_task(_broadcast_poll(conversation_id, poll_id))
        return
    if poll_id in _pending_poll_broadcasts:
        return
    _pending_poll_broadcasts.add(poll_id)
    schedule_realtime_task(_debounced_poll_broadcast(conversation_id, poll_id))


@router.post("/{conversation_id}/polls", response_model=MessageRead, status_code=201)
async def create_poll(
    conversation_id: int,
    payload: PollCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> MessageRead:
    message = await poll_service.create_poll(db, conversation_id, current_user, payload)
    message_data = message_service.build_new_message_read(message, current_user.id)
    member_ids = await online_conversation_member_ids(db, conversation_id)
    response_cache.invalidate_user_conversations(set(member_ids) | {current_user.id})
    try:
        await send_message_updates(db, "message.created", message, member_ids)
    except Exception:
        logger.exception("Committed poll message %s realtime delivery failed", message.id)
    schedule_realtime_task(send_admin_message_signal("admin.message.changed", message, "created"))
    if not current_user.content_moderation_whitelisted:
        moderation_service.schedule_message_moderation(message.id)
    return message_data


@router.get("/{conversation_id}/polls/{poll_id}", response_model=PollState)
async def get_poll(
    conversation_id: int,
    poll_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PollState:
    return PollState(**await poll_service.get_state(db, conversation_id, poll_id, current_user))


@router.put("/{conversation_id}/polls/{poll_id}/vote", response_model=PollState)
async def vote_poll(
    conversation_id: int,
    poll_id: int,
    payload: PollVoteRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PollState:
    state = await poll_service.cast_vote(db, conversation_id, poll_id, current_user, payload.option_ids)
    _schedule_poll_broadcast(conversation_id, poll_id)
    return PollState(**state)


@router.post("/{conversation_id}/polls/{poll_id}/close", response_model=PollState)
async def close_poll(
    conversation_id: int,
    poll_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PollState:
    state = await poll_service.close_poll(db, conversation_id, poll_id, current_user)
    _schedule_poll_broadcast(conversation_id, poll_id, immediate=True)
    return PollState(**state)


@router.get("/{conversation_id}/polls/{poll_id}/options/{option_id}/voters", response_model=PollVoterPage)
async def list_poll_voters(
    conversation_id: int,
    poll_id: int,
    option_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> PollVoterPage:
    users, total = await poll_service.list_voters(db, conversation_id, poll_id, option_id, current_user, limit, offset)
    return PollVoterPage(items=users, total=total)


# ---------------------------------------------------------------- 群表情


async def _notify_emojis_changed(db: AsyncSession, conversation_id: int) -> None:
    member_ids = await online_conversation_member_ids(db, conversation_id)
    if member_ids:
        schedule_realtime_task(manager.send_to_users(member_ids, EMOJI_EVENT, {"conversation_id": conversation_id}))


@router.get("/{conversation_id}/emojis", response_model=list[GroupEmojiRead])
async def list_group_emojis(
    conversation_id: int,
    request: Request,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[GroupEmojiRead]:
    items = await emoji_service.list_emojis(db, conversation_id, current_user.id)
    return [emoji_service.build_read(emoji, asset, request) for emoji, asset in items]


@router.post("/{conversation_id}/emojis", response_model=GroupEmojiRead, status_code=201)
async def create_group_emoji(
    conversation_id: int,
    payload: GroupEmojiCreate,
    request: Request,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> GroupEmojiRead:
    emoji, asset = await emoji_service.create_emoji(db, conversation_id, current_user, payload.name, payload.image_url)
    await _notify_emojis_changed(db, conversation_id)
    return emoji_service.build_read(emoji, asset, request)


@router.patch("/{conversation_id}/emojis/{emoji_id}", response_model=GroupEmojiRead)
async def rename_group_emoji(
    conversation_id: int,
    emoji_id: int,
    payload: GroupEmojiUpdate,
    request: Request,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> GroupEmojiRead:
    emoji, asset = await emoji_service.rename_emoji(db, conversation_id, emoji_id, current_user, payload.name)
    await _notify_emojis_changed(db, conversation_id)
    return emoji_service.build_read(emoji, asset, request)


@router.put("/{conversation_id}/emojis/order", response_model=list[GroupEmojiRead])
async def reorder_group_emojis(
    conversation_id: int,
    payload: GroupEmojiOrder,
    request: Request,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[GroupEmojiRead]:
    await emoji_service.reorder_emojis(db, conversation_id, current_user, payload.emoji_ids)
    await _notify_emojis_changed(db, conversation_id)
    items = await emoji_service.list_emojis(db, conversation_id, current_user.id)
    return [emoji_service.build_read(emoji, asset, request) for emoji, asset in items]


@router.delete("/{conversation_id}/emojis/{emoji_id}", response_model=MessageResponse)
async def delete_group_emoji(
    conversation_id: int,
    emoji_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> MessageResponse:
    await emoji_service.delete_emoji(db, conversation_id, emoji_id, current_user)
    await _notify_emojis_changed(db, conversation_id)
    return MessageResponse(detail="Emoji deleted")
