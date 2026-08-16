from __future__ import annotations

import asyncio
import html
import json
import re
import shlex
from datetime import datetime, timezone
from typing import Annotated

UTC = timezone.utc

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, Response, status
from fastapi.encoders import jsonable_encoder
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.models.conversation import Conversation, ConversationMember
from app.models.enums import ConversationType
from app.models.bot import Bot, BotInstallation
from app.models.message import Message
from app.models.user import User
from app.schemas.common import MessageResponse
from app.schemas.conversation import (
    AnnouncementCreate,
    AnnouncementRead,
    AnnouncementUpdate,
    ConversationAnnouncementUpdate,
    ConversationMemberPage,
    ConversationMemberRead,
    ConversationProfileUpdate,
    ConversationRead,
    DirectConversationCreate,
    GroupDiscoveryRead,
    GroupJoinRequestCreate,
    GroupJoinRequestDecision,
    GroupJoinRequestRead,
    GroupConversationCreate,
    GroupSettingsUpdate,
    MemberInvite,
    MemberMuteUpdate,
    MemberRoleUpdate,
    MemberSettingsUpdate,
    MemberTitleUpdate,
)
from app.schemas.group_activity import GroupCheckinCreate, GroupCheckinRead, GroupCheckinStatusRead, GroupLeaderboardRead
from app.schemas.message import (
    FeaturedMessageRead,
    ForwardBundleCreate,
    MessageBookmarkToggleRead,
    MessageCreate,
    MessageFeatureToggleRead,
    MessageInteractionCreate,
    MessageReactionCreate,
    MessageReactionSummary,
    MessageRead,
    MessageSearchResponse,
    MessageSearchResult,
    ReadConversationRequest,
    ReadReceipt,
)
from app.services import bots as bot_service
from app.services import conversations as conversation_service
from app.services import group_activity as group_activity_service
from app.services import messages as message_service
from app.services import moderation as moderation_service
from app.services import response_cache
from app.websocket.manager import admin_manager, bot_manager, game_manager, manager


router = APIRouter()
_BOT_MARKDOWN_INTERACTION_TAG_RE = re.compile(r"<\s*(button|btn|link)\b([^<>]*?)(?:/\s*>|>(.*?)<\s*/\s*\1\s*>)", re.IGNORECASE | re.DOTALL)


def cached_json_response(payload: response_cache.CachedJson, request: Request, max_age: int) -> Response:
    cache_control = "private, no-store, max-age=0" if max_age <= 0 else f"private, max-age={max_age}"
    headers = {"Cache-Control": cache_control, "Vary": "Accept-Encoding"}
    if "gzip" in request.headers.get("accept-encoding", "").lower():
        headers["Content-Encoding"] = "gzip"
        return Response(payload.gzip, media_type="application/json", headers=headers)
    return Response(payload.plain, media_type="application/json", headers=headers)


def display_name(user: User | None, fallback: str) -> str:
    if user is None:
        return fallback
    return user.nickname or user.username or fallback


def _metadata_from_json(metadata_json: str | None) -> dict | None:
    if not metadata_json:
        return None
    try:
        value = json.loads(metadata_json)
    except (TypeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _interaction_attr(attrs: dict, *names: str) -> str | None:
    for name in names:
        value = attrs.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, (int, float)):
            return str(value)
    return None


def _parse_interaction_attrs(raw: str) -> dict[str, str | bool]:
    attrs: dict[str, str | bool] = {}
    lexer = shlex.shlex(raw, posix=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    for token in lexer:
        if "=" not in token:
            attrs[token] = True
            continue
        key, value = token.split("=", 1)
        key = key.strip()
        if key:
            attrs[key] = html.unescape(value.strip().strip('"').strip("'"))
    return attrs


def _normalized_interaction_action(value: str | None, default: str) -> str:
    raw = (value or default).strip().lower().replace("-", "_")
    if raw in {"input", "insert", "fill", "preset", "text"}:
        return "input"
    if raw == "open":
        return "open"
    return "callback"


def _interaction_matches_payload(candidate: dict, payload: MessageInteractionCreate) -> bool:
    if candidate.get("kind") != payload.kind or candidate.get("action") != payload.action:
        return False
    if payload.component_id and candidate.get("component_id") != payload.component_id:
        return False
    if payload.action == "callback":
        return bool(payload.action_id and candidate.get("action_id") == payload.action_id)
    if payload.action == "input":
        return bool(payload.value and candidate.get("value") == payload.value)
    return bool(payload.href and candidate.get("href") == payload.href)


def _element_interaction_candidate(element: dict) -> dict | None:
    element_type = str(element.get("type") or element.get("name") or "").lower().replace("-", "_")
    if element_type not in {"button", "btn", "link"}:
        return None
    attrs = element.get("attrs") if isinstance(element.get("attrs"), dict) else {}
    attrs = {**{key: value for key, value in element.items() if key not in {"type", "name", "attrs", "content"}}, **attrs}
    kind = "link" if element_type == "link" else "button"
    action = _normalized_interaction_action(_interaction_attr(attrs, "action", "type", "mode"), "open" if kind == "link" else "callback")
    return {
        "kind": kind,
        "action": action,
        "component_id": _interaction_attr(attrs, "component_id", "componentId", "button_id", "buttonId", "id"),
        "action_id": _interaction_attr(attrs, "action_id", "actionId", "id", "key", "value"),
        "value": _interaction_attr(attrs, "value", "text", "input", "command"),
        "href": _interaction_attr(attrs, "href", "url"),
    }


def _markdown_interaction_candidates(content: str) -> list[dict]:
    candidates: list[dict] = []
    for match in _BOT_MARKDOWN_INTERACTION_TAG_RE.finditer(content):
        tag = match.group(1).strip().lower()
        attrs = _parse_interaction_attrs(match.group(2).strip())
        label = html.unescape(match.group(3) or "").strip()
        attrs_with_content = {**attrs}
        if label:
            attrs_with_content["content"] = label
        kind = "link" if tag == "link" else "button"
        action = _normalized_interaction_action(_interaction_attr(attrs_with_content, "action", "type", "mode"), "open" if kind == "link" else "callback")
        candidates.append(
            {
                "kind": kind,
                "action": action,
                "component_id": _interaction_attr(attrs_with_content, "component_id", "componentId", "button_id", "buttonId", "id"),
                "action_id": _interaction_attr(attrs_with_content, "action_id", "actionId", "id", "key", "value", "content"),
                "value": _interaction_attr(attrs_with_content, "value", "text", "input", "command", "content"),
                "href": _interaction_attr(attrs_with_content, "href", "url"),
            }
        )
    return candidates


def _message_has_interaction(message: Message, payload: MessageInteractionCreate) -> bool:
    metadata = _metadata_from_json(message.metadata_json) or {}
    raw_elements = metadata.get("elements")
    if not isinstance(raw_elements, list):
        return False
    for element in raw_elements:
        if not isinstance(element, dict):
            continue
        candidate = _element_interaction_candidate(element)
        if candidate and _interaction_matches_payload(candidate, payload):
            return True
        element_type = str(element.get("type") or element.get("name") or "").lower().replace("-", "_")
        if element_type in {"markdown", "md"} and isinstance(element.get("content"), str):
            if any(_interaction_matches_payload(candidate, payload) for candidate in _markdown_interaction_candidates(element["content"])):
                return True
    return False


def mute_duration_label(muted_until: datetime | None) -> str:
    if muted_until is None:
        return ""
    now = datetime.now(UTC)
    until = muted_until if muted_until.tzinfo is not None else muted_until.replace(tzinfo=UTC)
    seconds = max(0, int((until - now).total_seconds()))
    if seconds <= 0:
        return ""
    minutes = max(1, round(seconds / 60))
    if minutes < 60:
        return f" {minutes}分钟"
    if minutes < 24 * 60:
        hours = minutes / 60
        return f" {int(hours) if hours.is_integer() else round(hours, 1)}小时"
    days = minutes / (24 * 60)
    return f" {int(days) if days.is_integer() else round(days, 1)}天"


async def emit_system_message(
    db: AsyncSession,
    conversation_id: int,
    sender: User | int,
    content: str,
    user_ids: list[int] | set[int],
    metadata: dict | None = None,
) -> Message | None:
    message = await message_service.create_system_message(db, conversation_id, sender, content, metadata)
    recipients = list(dict.fromkeys(user_ids))
    if not recipients:
        return message
    await send_message_updates(db, "message.created", message, recipients)
    return message


async def send_conversation_updates(db: AsyncSession, conversation_id: int, user_ids: list[int] | set[int]) -> None:
    await send_conversation_patch(db, conversation_id, user_ids)


async def online_conversation_member_ids(db: AsyncSession, conversation_id: int) -> list[int]:
    return await conversation_service.conversation_member_ids_in(
        db,
        conversation_id,
        await manager.online_user_ids_global(),
    )


async def send_conversation_patch(
    db: AsyncSession,
    conversation_id: int,
    user_ids: list[int] | set[int],
    extra: dict | None = None,
) -> None:
    online_ids = set(user_ids) & await manager.online_user_ids_global()
    response_cache.invalidate_user_conversations(online_ids)
    if not online_ids:
        return
    conversation = await conversation_service.get_conversation(db, conversation_id)
    data = {
        "id": conversation.id,
        "conversation_id": conversation.id,
        "type": conversation.type,
        "title": conversation.title,
        "avatar_url": conversation.avatar_url,
        "description": conversation.description,
        "category": conversation.category,
        "announcement": conversation.announcement,
        "all_muted": conversation.all_muted,
        "slow_mode_seconds": conversation.slow_mode_seconds,
        "message_rate_limit_per_minute": conversation.message_rate_limit_per_minute,
        "join_mode": conversation.join_mode,
        "auto_approve": conversation.auto_approve,
        "join_question": conversation.join_question,
        "is_temporary": conversation.is_temporary,
        "temporary_initiator_id": conversation.temporary_initiator_id,
        "temporary_target_id": conversation.temporary_target_id,
        "updated_at": conversation.updated_at,
    }
    if extra:
        data.update(extra)
    await manager.send_to_users(online_ids, "conversation.updated", data)


async def send_message_updates(db: AsyncSession, event_type: str, message: Message, user_ids: list[int] | set[int]) -> None:
    online_ids = set(user_ids) & await manager.online_user_ids_global()
    if event_type == "message.created" and message.sender is not None:
        if getattr(message, "_sender_display_name", None) is None:
            data = await message_service.build_message_read(db, message, message.sender_id)
        else:
            data = message_service.build_new_message_read(message, message.sender_id)
        if online_ids:
            await manager.send_to_users(online_ids, event_type, data)
        await send_bot_message_updates(db, event_type, message.conversation_id, data, sender_id=message.sender_id)
        await send_game_message_updates(db, event_type, message.conversation_id, data)
        return
    if event_type == "message.recalled":
        data = {
            "id": message.id,
            "conversation_id": message.conversation_id,
            "message_id": message.id,
            "recalled_at": message.recalled_at,
            "recalled_by_id": message.recalled_by_id,
        }
        if online_ids:
            await manager.send_to_users(online_ids, event_type, data)
        await send_bot_message_updates(
            db,
            event_type,
            message.conversation_id,
            data,
        )
        await send_game_message_updates(db, event_type, message.conversation_id, data)
        return
    if not online_ids:
        return
    for user_id in online_ids:
        data = await message_service.build_message_read(db, message, user_id)
        await manager.send_to_user(user_id, event_type, data)


async def send_game_message_updates(db: AsyncSession, event_type: str, conversation_id: int, data) -> None:
    """把消息同步给游戏模式的观众。

    观众可能未登录，按会话 id 分组订阅，因此不能复用按用户投递的通道。

    **不能用本地连接数做短路。** viewer_count 只统计当前进程的连接，而线上是
    多 worker：观众连在 A，消息可能由 B 处理，B 看到本地计数为 0 就跳过投递，
    结果是按 worker 数比例丢消息。判据改为「该群是否开启了游戏模式」——
    conversation 在发消息路径上刚被加载过，这里通常是一次 identity map 命中，
    比多发一次 Redis 广播更省。
    """
    conversation = await db.get(Conversation, conversation_id)
    if conversation is None or not conversation.game_mode_enabled:
        return
    await game_manager.send_to_conversation(conversation_id, event_type, data)


async def send_bot_message_updates(db: AsyncSession, event_type: str, conversation_id: int, data, *, sender_id: int | None = None) -> None:
    bot_ids = await bot_service.bot_conversation_ids_for_message_event(db, conversation_id, sender_id)
    if bot_ids:
        await bot_manager.send_to_bots(bot_ids, event_type, data)


async def send_bot_conversation_event(db: AsyncSession, event_type: str, conversation_id: int, data) -> None:
    bot_ids = await bot_service.bot_ids_for_member_event(db, conversation_id)
    if bot_ids:
        await bot_manager.send_to_bots(bot_ids, event_type, data)


async def send_admin_message_signal(event_type: str, message: Message, action: str) -> None:
    await admin_manager.broadcast(
        event_type,
        {"message_id": message.id, "conversation_id": message.conversation_id, "action": action},
    )


def schedule_realtime_task(coro) -> None:
    task = asyncio.create_task(coro)

    def consume_exception(done_task: asyncio.Task) -> None:
        try:
            done_task.result()
        except Exception:
            pass

    task.add_done_callback(consume_exception)


async def send_reaction_updates(db: AsyncSession, conversation_id: int, message_id: int, user_ids: list[int] | set[int]) -> None:
    reactions = await message_service.get_message_reaction_summaries(db, message_id, 0)
    event_data = {"conversation_id": conversation_id, "message_id": message_id, "reactions": reactions}
    online_ids = set(user_ids) & await manager.online_user_ids_global()
    if online_ids:
        await manager.send_to_users(online_ids, "message.reaction.updated", event_data)
    await send_bot_conversation_event(db, "message.reaction.updated", conversation_id, event_data)


@router.get("", response_model=list[ConversationRead])
async def conversations(
    request: Request,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    cached = response_cache.get_user_conversations(current_user.id)
    if cached is not None:
        return cached_json_response(cached, request, 5)

    items = await conversation_service.list_user_conversations(db, current_user.id)
    data = jsonable_encoder(await conversation_service.build_conversation_reads(db, items, current_user.id))
    payload = response_cache.build_json(data)
    response_cache.set_user_conversations(current_user.id, payload)
    return cached_json_response(payload, request, 5)


@router.post("/direct", response_model=ConversationRead, status_code=201)
async def direct_conversation(
    payload: DirectConversationCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ConversationRead:
    conversation = await conversation_service.get_or_create_direct_conversation(db, current_user, payload.user_id, payload.temporary)
    response_cache.invalidate_user_conversations({current_user.id, payload.user_id})
    member_ids = await online_conversation_member_ids(db, conversation.id)
    await send_conversation_updates(db, conversation.id, member_ids)
    return await conversation_service.build_conversation_read(db, conversation, current_user.id)


@router.post("/{conversation_id}/temporary/close", response_model=MessageResponse)
async def close_temporary_conversation(
    conversation_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> MessageResponse:
    member_ids = await online_conversation_member_ids(db, conversation_id)
    await conversation_service.close_temporary_conversation(db, conversation_id, current_user)
    await manager.send_to_users(member_ids, "conversation.temporary.closed", {"conversation_id": conversation_id})
    return MessageResponse(detail="Temporary conversation closed")


@router.post("/{conversation_id}/temporary/block", response_model=MessageResponse)
async def block_temporary_conversation(
    conversation_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> MessageResponse:
    member_ids = await online_conversation_member_ids(db, conversation_id)
    blocked_user_id = await conversation_service.block_temporary_conversation(db, conversation_id, current_user)
    await manager.send_to_users(member_ids, "conversation.temporary.blocked", {"conversation_id": conversation_id, "blocked_user_id": blocked_user_id})
    return MessageResponse(detail="Temporary conversation blocked")


@router.post("/groups", response_model=ConversationRead, status_code=201)
async def group_conversation(
    payload: GroupConversationCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ConversationRead:
    conversation = await conversation_service.create_group_conversation(
        db, current_user, payload.title, payload.member_ids, payload.avatar_url
    )
    response_cache.invalidate_user_conversations(set(payload.member_ids) | {current_user.id})
    member_ids = await online_conversation_member_ids(db, conversation.id)
    await send_conversation_updates(db, conversation.id, member_ids)
    return await conversation_service.build_conversation_read(db, conversation, current_user.id)


@router.get("/groups/search", response_model=list[GroupDiscoveryRead])
async def search_groups(
    q: Annotated[str, Query(min_length=1, max_length=100)],
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[GroupDiscoveryRead]:
    return await conversation_service.search_public_groups(db, q, current_user.id)


@router.get("/groups/recommended", response_model=list[GroupDiscoveryRead])
async def recommended_groups(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[GroupDiscoveryRead]:
    return await conversation_service.recommended_public_groups(db, current_user.id)


@router.get("/bookmarks/all", response_model=list[MessageRead])
async def all_bookmarked_messages(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> list[MessageRead]:
    items = await message_service.list_all_bookmarked_messages(db, current_user.id, limit)
    return await message_service.build_message_reads(db, items, current_user.id)


@router.post("/{conversation_id}/join", response_model=ConversationRead)
async def join_group(
    conversation_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ConversationRead:
    conversation, created = await conversation_service.join_group(db, conversation_id, current_user.id)
    member_ids = await online_conversation_member_ids(db, conversation_id)
    if created:
        await emit_system_message(
            db,
            conversation_id,
            current_user,
            f"{display_name(current_user, f'用户 {current_user.id}')} 加入了群聊",
            member_ids,
            {"system_event": "group.member.joined", "user_id": current_user.id},
        )
        event_data = {"conversation_id": conversation_id, "user_id": current_user.id}
        await manager.send_to_users(member_ids, "group.member.joined", event_data)
        await send_bot_conversation_event(db, "group.member.joined", conversation_id, event_data)
        await send_conversation_updates(db, conversation_id, member_ids)
    return await conversation_service.build_conversation_read(db, conversation, current_user.id)


@router.post("/{conversation_id}/join-requests", response_model=GroupJoinRequestRead, status_code=status.HTTP_201_CREATED)
async def create_group_join_request(
    conversation_id: int,
    payload: GroupJoinRequestCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> GroupJoinRequestRead:
    join_request = await conversation_service.create_group_join_request(db, conversation_id, current_user, payload)
    response = await conversation_service.build_group_join_request_read(db, join_request, current_user.id)
    reviewer_ids = await conversation_service.pending_join_request_reviewer_ids(db, conversation_id)
    await manager.send_to_users(reviewer_ids, "group.join_request.created", response)
    await manager.send_to_user(current_user.id, "group.join_request.created", response)
    # 也推给群里的机器人，这样机器人才能做自动审批
    await send_bot_conversation_event(
        db,
        "group.join_request.created",
        conversation_id,
        {
            "conversation_id": conversation_id,
            "request_id": join_request.id,
            "user_id": join_request.requester_id,
            "message": join_request.message,
            "answer": join_request.answer,
        },
    )
    await send_conversation_updates(db, conversation_id, reviewer_ids)
    return response


@router.get("/join-requests/incoming", response_model=list[GroupJoinRequestRead])
async def incoming_group_join_requests(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[GroupJoinRequestRead]:
    items = await conversation_service.list_incoming_group_join_requests(db, current_user.id)
    return [await conversation_service.build_group_join_request_read(db, item, current_user.id) for item in items]


@router.get("/join-requests/outgoing", response_model=list[GroupJoinRequestRead])
async def outgoing_group_join_requests(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[GroupJoinRequestRead]:
    items = await conversation_service.list_outgoing_group_join_requests(db, current_user.id)
    return [await conversation_service.build_group_join_request_read(db, item, current_user.id) for item in items]


@router.post("/join-requests/{request_id}/accept", response_model=GroupJoinRequestRead)
async def accept_group_join_request(
    request_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    payload: GroupJoinRequestDecision = Body(default_factory=GroupJoinRequestDecision),
) -> GroupJoinRequestRead:
    join_request, added = await conversation_service.review_group_join_request(db, request_id, current_user.id, True, payload)
    response = await conversation_service.build_group_join_request_read(db, join_request, current_user.id)
    reviewers = await conversation_service.pending_join_request_reviewer_ids(db, join_request.conversation_id)
    recipients = set(reviewers) | {join_request.requester_id, current_user.id}
    await manager.send_to_users(recipients, "group.join_request.accepted", response)
    member_ids = await online_conversation_member_ids(db, join_request.conversation_id)
    if added:
        requester_member = await conversation_service.get_member(db, join_request.conversation_id, join_request.requester_id)
        requester_name = display_name(requester_member.user, f"用户 {join_request.requester_id}")
        await emit_system_message(
            db,
            join_request.conversation_id,
            current_user,
            f"{requester_name} 加入了群聊",
            member_ids,
            {"system_event": "group.member.joined", "user_id": join_request.requester_id},
        )
        event_data = {"conversation_id": join_request.conversation_id, "user_id": join_request.requester_id}
        await manager.send_to_users(member_ids, "group.member.joined", event_data)
        await send_bot_conversation_event(db, "group.member.joined", join_request.conversation_id, event_data)
    await send_conversation_updates(db, join_request.conversation_id, member_ids)
    return response


@router.post("/join-requests/{request_id}/reject", response_model=GroupJoinRequestRead)
async def reject_group_join_request(
    request_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    payload: GroupJoinRequestDecision = Body(default_factory=GroupJoinRequestDecision),
) -> GroupJoinRequestRead:
    join_request, _added = await conversation_service.review_group_join_request(db, request_id, current_user.id, False, payload)
    response = await conversation_service.build_group_join_request_read(db, join_request, current_user.id)
    reviewers = await conversation_service.pending_join_request_reviewer_ids(db, join_request.conversation_id)
    recipients = set(reviewers) | {join_request.requester_id, current_user.id}
    await manager.send_to_users(recipients, "group.join_request.rejected", response)
    await send_conversation_updates(db, join_request.conversation_id, reviewers)
    return response


@router.get("/{conversation_id}/announcements", response_model=list[AnnouncementRead])
async def announcements(
    conversation_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[AnnouncementRead]:
    items = await conversation_service.list_announcements(db, conversation_id, current_user.id)
    return [AnnouncementRead.model_validate(item) for item in items]


@router.post("/{conversation_id}/announcements", response_model=AnnouncementRead, status_code=status.HTTP_201_CREATED)
async def create_announcement(
    conversation_id: int,
    payload: AnnouncementCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AnnouncementRead:
    announcement = await conversation_service.create_announcement(
        db, conversation_id, current_user, payload.content, payload.pinned
    )
    announcement_data = AnnouncementRead.model_validate(announcement)
    member_ids = await online_conversation_member_ids(db, conversation_id)
    await manager.send_to_users(member_ids, "group.announcement.created", announcement_data)
    await send_conversation_updates(db, conversation_id, member_ids)
    return announcement_data


@router.delete("/{conversation_id}/announcements/{announcement_id}", response_model=MessageResponse)
async def delete_announcement(
    conversation_id: int,
    announcement_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> MessageResponse:
    await conversation_service.delete_announcement(db, conversation_id, announcement_id, current_user)
    member_ids = await online_conversation_member_ids(db, conversation_id)
    await manager.send_to_users(member_ids, "group.announcement.deleted", {"conversation_id": conversation_id, "announcement_id": announcement_id})
    await send_conversation_updates(db, conversation_id, member_ids)
    return MessageResponse(detail="Announcement deleted")


@router.patch("/{conversation_id}/announcements/{announcement_id}", response_model=AnnouncementRead)
async def update_group_announcement(
    conversation_id: int,
    announcement_id: int,
    payload: AnnouncementUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AnnouncementRead:
    announcement = await conversation_service.update_announcement(
        db, conversation_id, announcement_id, current_user, payload.content, payload.pinned
    )
    announcement_data = AnnouncementRead.model_validate(announcement)
    member_ids = await online_conversation_member_ids(db, conversation_id)
    await manager.send_to_users(member_ids, "group.announcement.updated", announcement_data)
    await send_conversation_updates(db, conversation_id, member_ids)
    return announcement_data


@router.get("/{conversation_id}/members", response_model=list[ConversationMemberRead] | ConversationMemberPage)
async def members(
    conversation_id: int,
    request: Request,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: int | None = Query(default=None, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    search: str = Query(default="", max_length=50),
) -> Response:
    await conversation_service.require_membership(db, conversation_id, current_user.id, clear_expired_mute=False)
    if await conversation_service.clear_expired_mutes_for_conversation(db, conversation_id):
        await db.commit()
        response_cache.invalidate_conversation_members(conversation_id)
    normalized_search = search.strip()
    cached = response_cache.get_conversation_members(conversation_id, limit, offset, normalized_search)
    if cached is not None:
        return cached_json_response(cached, request, 0)

    if limit is None:
        members = await conversation_service.list_members(db, conversation_id, current_user.id)
        data = jsonable_encoder([ConversationMemberRead.model_validate(member) for member in members])
    else:
        members, total = await conversation_service.list_members_page(
            db,
            conversation_id,
            current_user.id,
            limit=limit,
            offset=offset,
            search=normalized_search,
        )
        data = jsonable_encoder(
            ConversationMemberPage(
                items=[ConversationMemberRead.model_validate(member) for member in members],
                total=total,
                limit=limit,
                offset=offset,
                has_more=offset + len(members) < total,
            )
        )
    payload = response_cache.build_json(data)
    response_cache.set_conversation_members(conversation_id, payload, limit=limit, offset=offset, search=normalized_search)
    return cached_json_response(payload, request, 0)


@router.get("/{conversation_id}/members/search", response_model=list[ConversationMemberRead])
async def search_members(
    conversation_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    q: str = Query(default="", max_length=50),
    limit: int = Query(default=12, ge=1, le=20),
) -> list[ConversationMember]:
    return await conversation_service.search_members(db, conversation_id, current_user.id, query=q, limit=limit)


@router.get("/{conversation_id}/members/{user_id}", response_model=ConversationMemberRead)
async def member_detail(
    conversation_id: int,
    user_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ConversationMember:
    await conversation_service.require_membership(db, conversation_id, current_user.id, clear_expired_mute=False)
    member = await conversation_service.get_member(db, conversation_id, user_id, clear_expired_mute=False)
    if await conversation_service.clear_expired_member_mute(db, member):
        await db.commit()
        response_cache.invalidate_conversation_members(conversation_id)
    return member


@router.get("/{conversation_id}/checkin/me", response_model=GroupCheckinStatusRead)
async def my_group_checkin_status(
    conversation_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    return await group_activity_service.get_checkin_status(db, conversation_id, current_user.id)


@router.post("/{conversation_id}/checkin", response_model=GroupCheckinRead, status_code=status.HTTP_201_CREATED)
async def checkin_group(
    conversation_id: int,
    payload: GroupCheckinCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> GroupCheckinRead:
    checkin, previous_level, current_level = await group_activity_service.checkin_group(
        db, conversation_id, current_user, payload.message
    )
    checkin_data = GroupCheckinRead.model_validate(checkin)
    member_ids = await online_conversation_member_ids(db, conversation_id)
    await manager.send_to_users(
        member_ids,
        "group.checkin.created",
        {
            "conversation_id": conversation_id,
            "user_id": current_user.id,
            "checkin": checkin_data,
            "previous_level": previous_level,
            "current_level": current_level,
        },
    )
    if current_level > previous_level:
        await manager.send_to_users(
            member_ids,
            "group.member.level_updated",
            {"conversation_id": conversation_id, "user_id": current_user.id, "level": current_level},
        )
    if checkin.streak_days in {7, 30, 100}:
        await emit_system_message(
            db,
            conversation_id,
            current_user,
            f"{display_name(current_user, f'用户 {current_user.id}')} 已连续签到 {checkin.streak_days} 天",
            member_ids,
            {"system_event": "group.checkin.milestone", "user_id": current_user.id, "streak_days": checkin.streak_days},
        )
    return checkin_data


@router.get("/{conversation_id}/leaderboard", response_model=GroupLeaderboardRead)
async def group_leaderboard(
    conversation_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    type: Annotated[str, Query(pattern="^(activity|level|checkin_streak|checkin_total|message)$")] = "activity",
    period: Annotated[str, Query(pattern="^(today|week|month|all)$")] = "all",
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> GroupLeaderboardRead:
    return await group_activity_service.get_group_leaderboard(db, conversation_id, current_user.id, type, period, limit)


@router.post("/{conversation_id}/members", response_model=list[ConversationMemberRead])
async def invite_members(
    conversation_id: int,
    payload: MemberInvite,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[ConversationMember]:
    response_cache.invalidate_conversation_members(conversation_id)
    before_members = await conversation_service.list_members(db, conversation_id, current_user.id)
    before_ids = {member.user_id for member in before_members}
    members = await conversation_service.invite_members(db, conversation_id, current_user.id, payload.user_ids)
    after_ids = {member.user_id for member in members}
    new_ids = after_ids - before_ids
    online_ids = set(await online_conversation_member_ids(db, conversation_id))
    for new_id in new_ids:
        invited_member = await conversation_service.get_member(db, conversation_id, new_id)
        invited_name = display_name(invited_member.user, f"用户 {new_id}")
        await emit_system_message(
            db,
            conversation_id,
            current_user,
            f"{invited_name} 加入了群聊",
            online_ids,
            {"system_event": "group.member.joined", "user_id": new_id},
        )
    for new_id in new_ids:
        await send_bot_conversation_event(db, "group.member.joined", conversation_id, {"conversation_id": conversation_id, "user_id": new_id, "actor_id": current_user.id})
    await manager.send_to_users(new_ids, "group.member.invited", {"conversation_id": conversation_id, "inviter_id": current_user.id})
    await send_conversation_updates(db, conversation_id, online_ids | new_ids)
    response_cache.invalidate_conversation_members(conversation_id)
    return members


@router.patch("/{conversation_id}/announcement", response_model=ConversationRead)
async def update_announcement(
    conversation_id: int,
    payload: ConversationAnnouncementUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ConversationRead:
    conversation = await conversation_service.update_group_announcement(
        db, conversation_id, current_user.id, payload.announcement
    )
    member_ids = await online_conversation_member_ids(db, conversation_id)
    await send_conversation_updates(db, conversation_id, member_ids)
    return await conversation_service.build_conversation_read(db, conversation, current_user.id)


@router.patch("/{conversation_id}/profile", response_model=ConversationRead)
async def update_group_profile(
    conversation_id: int,
    payload: ConversationProfileUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ConversationRead:
    conversation = await conversation_service.update_group_profile(db, conversation_id, current_user.id, payload)
    member_ids = await online_conversation_member_ids(db, conversation_id)
    await send_conversation_updates(db, conversation_id, member_ids)
    return await conversation_service.build_conversation_read(db, conversation, current_user.id)


@router.patch("/{conversation_id}/settings", response_model=ConversationRead)
async def update_group_settings(
    conversation_id: int,
    payload: GroupSettingsUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ConversationRead:
    previous = await conversation_service.get_conversation(db, conversation_id)
    previous_all_muted = previous.all_muted
    previous_message_rate_limit_per_minute = previous.message_rate_limit_per_minute
    conversation = await conversation_service.update_group_settings(db, conversation_id, current_user.id, payload)
    member_ids = await online_conversation_member_ids(db, conversation_id)
    actor_name = display_name(current_user, f"用户 {current_user.id}")
    if payload.all_muted is not None and payload.all_muted != previous_all_muted:
        await emit_system_message(
            db,
            conversation_id,
            current_user,
            f"{actor_name}{'开启' if conversation.all_muted else '关闭'}了全员禁言",
            member_ids,
            {"system_event": "group.all_muted.updated", "all_muted": conversation.all_muted, "actor_id": current_user.id},
        )
    if payload.message_rate_limit_per_minute is not None and payload.message_rate_limit_per_minute != previous_message_rate_limit_per_minute:
        slow_mode_text = "关闭了发言限频" if conversation.message_rate_limit_per_minute <= 0 else f"将发言限频设置为每分钟 {conversation.message_rate_limit_per_minute} 条"
        await emit_system_message(
            db,
            conversation_id,
            current_user,
            f"{actor_name}{slow_mode_text}",
            member_ids,
            {"system_event": "group.slow_mode.updated", "message_rate_limit_per_minute": conversation.message_rate_limit_per_minute, "actor_id": current_user.id},
        )
    await send_conversation_updates(db, conversation_id, member_ids)
    return await conversation_service.build_conversation_read(db, conversation, current_user.id)


@router.patch("/{conversation_id}/members/me/settings", response_model=ConversationMemberRead)
async def update_my_member_settings(
    conversation_id: int,
    payload: MemberSettingsUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ConversationMember:
    member = await conversation_service.update_my_member_settings(db, conversation_id, current_user.id, payload)
    response_cache.invalidate_conversation_members(conversation_id)
    await send_conversation_updates(db, conversation_id, {current_user.id})
    return member


@router.patch("/{conversation_id}/members/{user_id}/title", response_model=ConversationMemberRead)
async def update_member_title(
    conversation_id: int,
    user_id: int,
    payload: MemberTitleUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ConversationMember:
    member = await conversation_service.update_member_title(db, conversation_id, current_user.id, user_id, payload)
    response_cache.invalidate_conversation_members(conversation_id)
    member_ids = await online_conversation_member_ids(db, conversation_id)
    await manager.send_to_users(member_ids, "group.member.title_updated", {"conversation_id": conversation_id, "user_id": user_id, "title": member.title})
    return member


@router.patch("/{conversation_id}/members/{user_id}/role", response_model=ConversationMemberRead)
async def update_member_role(
    conversation_id: int,
    user_id: int,
    payload: MemberRoleUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ConversationMember:
    previous_member = await conversation_service.get_member(db, conversation_id, user_id)
    previous_role = previous_member.role
    member = await conversation_service.update_member_role(db, conversation_id, current_user.id, user_id, payload.role)
    response_cache.invalidate_conversation_members(conversation_id)
    member_ids = await online_conversation_member_ids(db, conversation_id)
    if member.role != previous_role:
        actor_name = display_name(current_user, f"用户 {current_user.id}")
        target_name = display_name(member.user, f"用户 {user_id}")
        role_text = "设为管理员" if member.role.value == "admin" else "取消管理员"
        await emit_system_message(
            db,
            conversation_id,
            current_user,
            f"{actor_name} 将 {target_name} {role_text}",
            member_ids,
            {"system_event": "group.member.role_updated", "actor_id": current_user.id, "user_id": user_id, "role": member.role.value},
        )
    event_data = {"conversation_id": conversation_id, "user_id": user_id, "role": member.role}
    await manager.send_to_users(
        member_ids,
        "group.member.role_updated",
        event_data,
    )
    await send_bot_conversation_event(db, "group.member.role_updated", conversation_id, event_data)
    await send_conversation_updates(db, conversation_id, member_ids)
    return member


@router.patch("/{conversation_id}/members/{user_id}/mute", response_model=ConversationMemberRead)
async def update_member_mute(
    conversation_id: int,
    user_id: int,
    payload: MemberMuteUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ConversationMember:
    previous_member = await conversation_service.get_member(db, conversation_id, user_id)
    previous_muted = bool(previous_member.muted)
    member = await conversation_service.update_member_mute(db, conversation_id, current_user.id, user_id, payload.muted, payload.muted_until)
    response_cache.invalidate_conversation_members(conversation_id)
    member_ids = await online_conversation_member_ids(db, conversation_id)
    if bool(member.muted) != previous_muted:
        actor_name = display_name(current_user, f"用户 {current_user.id}")
        target_name = display_name(member.user, f"用户 {user_id}")
        duration_label = mute_duration_label(member.muted_until) if member.muted else ""
        await emit_system_message(
            db,
            conversation_id,
            current_user,
            f"{actor_name}{'禁言了' if member.muted else '解除了'} {target_name}{duration_label if member.muted else ' 的禁言'}",
            member_ids,
            {"system_event": "group.member.mute_updated", "actor_id": current_user.id, "user_id": user_id, "muted": member.muted, "muted_until": member.muted_until.isoformat() if member.muted_until else None},
        )
    event_data = {"conversation_id": conversation_id, "user_id": user_id, "muted": member.muted, "muted_until": member.muted_until.isoformat() if member.muted_until else None}
    await manager.send_to_users(
        member_ids,
        "group.member.mute_updated",
        event_data,
    )
    await send_bot_conversation_event(db, "group.member.mute_updated", conversation_id, event_data)
    await send_conversation_updates(db, conversation_id, member_ids)
    return member


@router.delete("/{conversation_id}/members/me", response_model=MessageResponse)
async def leave_group(
    conversation_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> MessageResponse:
    before_ids = set(await online_conversation_member_ids(db, conversation_id))
    await conversation_service.leave_group(db, conversation_id, current_user.id)
    response_cache.invalidate_conversation_members(conversation_id)
    remaining_ids = before_ids - {current_user.id}
    if remaining_ids:
        await emit_system_message(
            db,
            conversation_id,
            current_user,
            f"{display_name(current_user, f'用户 {current_user.id}')} 退出了群聊",
            remaining_ids,
            {"system_event": "group.member.left", "user_id": current_user.id},
        )
    event_data = {"conversation_id": conversation_id, "user_id": current_user.id}
    await manager.send_to_users(remaining_ids, "group.member.left", event_data)
    await send_bot_conversation_event(db, "group.member.left", conversation_id, event_data)
    await manager.send_to_user(current_user.id, "conversation.deleted", {"conversation_id": conversation_id, "reason": "left", "actor_id": current_user.id})
    if remaining_ids:
        await send_conversation_updates(db, conversation_id, remaining_ids)
    return MessageResponse(detail="Left group conversation")


@router.delete("/{conversation_id}", response_model=MessageResponse)
async def dissolve_group(
    conversation_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> MessageResponse:
    member_ids = await online_conversation_member_ids(db, conversation_id)
    await conversation_service.dissolve_group(db, conversation_id, current_user.id)
    response_cache.invalidate_conversation_members(conversation_id)
    await manager.send_to_users(member_ids, "conversation.deleted", {"conversation_id": conversation_id, "actor_id": current_user.id, "reason": "dissolved"})
    return MessageResponse(detail="Dissolved group conversation")


@router.delete("/{conversation_id}/members/{user_id}", response_model=MessageResponse)
async def remove_member(
    conversation_id: int,
    user_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> MessageResponse:
    before_ids = set(await online_conversation_member_ids(db, conversation_id))
    target_member = await conversation_service.get_member(db, conversation_id, user_id)
    target_name = display_name(target_member.user, f"用户 {user_id}")
    await conversation_service.remove_member(db, conversation_id, current_user.id, user_id)
    response_cache.invalidate_conversation_members(conversation_id)
    remaining_ids = before_ids - {user_id}
    if remaining_ids:
        await emit_system_message(
            db,
            conversation_id,
            current_user,
            f"{display_name(current_user, f'用户 {current_user.id}')} 将 {target_name} 移出群聊",
            remaining_ids,
            {"system_event": "group.member.removed", "actor_id": current_user.id, "user_id": user_id},
        )
    event_data = {"conversation_id": conversation_id, "user_id": user_id, "actor_id": current_user.id}
    await manager.send_to_users(
        remaining_ids,
        "group.member.removed",
        event_data,
    )
    await send_bot_conversation_event(db, "group.member.removed", conversation_id, event_data)
    await manager.send_to_user(user_id, "conversation.deleted", {"conversation_id": conversation_id, "actor_id": current_user.id, "reason": "removed"})
    if remaining_ids:
        await send_conversation_updates(db, conversation_id, remaining_ids)
    return MessageResponse(detail="Removed group member")


@router.post("/{conversation_id}/clear", response_model=MessageResponse)
async def clear_conversation(
    conversation_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> MessageResponse:
    await conversation_service.clear_conversation_history(db, conversation_id, current_user.id)
    await send_conversation_updates(db, conversation_id, {current_user.id})
    return MessageResponse(detail="Cleared conversation history")

@router.get("/{conversation_id}/messages", response_model=list[MessageRead])
async def messages(
    conversation_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    before_id: Annotated[int | None, Query(gt=0)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> list[MessageRead]:
    items = await message_service.list_messages(db, conversation_id, current_user.id, before_id, limit)
    return await message_service.build_message_reads(db, items, current_user.id)


@router.get("/{conversation_id}/messages/search", response_model=MessageSearchResponse)
async def search_messages(
    conversation_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    q: Annotated[str | None, Query(min_length=1, max_length=100, description="Keyword matched against message content and searchable metadata")] = None,
    category: Annotated[str, Query(pattern="^(all|text|image|sticker|voice|link|forward_bundle|system)$")] = "all",
    sender_id: Annotated[int | None, Query(gt=0, description="Optional sender filter for group conversations only")] = None,
    start_at: Annotated[datetime | None, Query(description="Inclusive created_at lower bound, ISO 8601")] = None,
    end_at: Annotated[datetime | None, Query(description="Inclusive created_at upper bound, ISO 8601")] = None,
    before_id: Annotated[int | None, Query(gt=0, description="Return matches older than this message id")] = None,
    include_recalled: bool = False,
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
) -> MessageSearchResponse:
    if start_at is not None and end_at is not None and start_at > end_at:
        raise HTTPException(status_code=422, detail="start_at must be earlier than or equal to end_at")
    items, total, has_more, next_before_id = await message_service.search_messages(
        db,
        conversation_id,
        current_user.id,
        q,
        category,
        sender_id,
        start_at,
        end_at,
        before_id,
        include_recalled,
        limit,
    )
    message_reads = await message_service.build_message_reads(db, items, current_user.id)
    message_by_id = {message.id: message for message in items}
    results = []
    for message_read in message_reads:
        match_type, snippet = message_service.build_search_match(message_by_id[message_read.id], q)
        results.append(MessageSearchResult(message=message_read, match_type=match_type, snippet=snippet))
    return MessageSearchResponse(items=results, total=total, limit=limit, has_more=has_more, next_before_id=next_before_id)


@router.get("/{conversation_id}/messages/{message_id}/context", response_model=list[MessageRead])
async def message_context(
    conversation_id: int,
    message_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    before: Annotated[int, Query(ge=0, le=100)] = 25,
    after: Annotated[int, Query(ge=0, le=100)] = 25,
) -> list[MessageRead]:
    items = await message_service.get_message_context(db, conversation_id, message_id, current_user.id, before, after)
    return await message_service.build_message_reads(db, items, current_user.id)


@router.post("/{conversation_id}/messages/{message_id}/interactions", response_model=MessageResponse)
async def trigger_message_interaction(
    conversation_id: int,
    message_id: int,
    payload: MessageInteractionCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> MessageResponse:
    member = await conversation_service.require_membership(db, conversation_id, current_user.id)
    result = await db.execute(
        select(Message, Bot.id, Bot.user_id, Conversation.type)
        .join(Bot, Bot.user_id == Message.sender_id)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .where(
            Message.id == message_id,
            Message.conversation_id == conversation_id,
            Message.recalled_at.is_(None),
            Bot.status == bot_service.BOT_STATUS_ACTIVE,
        )
    )
    row = result.first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Interactive bot message not found")
    message, bot_id, bot_user_id, conversation_type = row
    if conversation_type == ConversationType.GROUP:
        installation_result = await db.execute(
            select(BotInstallation.id).where(
                BotInstallation.bot_id == bot_id,
                BotInstallation.conversation_id == conversation_id,
                BotInstallation.enabled.is_(True),
                BotInstallation.receive_messages.is_(True),
            )
        )
        if installation_result.scalar_one_or_none() is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Interactive bot message not found")
    elif conversation_type == ConversationType.DIRECT:
        bot_member = await conversation_service.get_member(db, conversation_id, bot_user_id)
        if bot_member is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Interactive bot message not found")
    else:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Interactive bot message not found")
    if not _message_has_interaction(message, payload):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Interaction is not defined on this message")
    await bot_manager.send_to_bot(
        bot_id,
        "message.interaction",
        {
            "conversation_id": conversation_id,
            "message_id": message.id,
            "user_id": current_user.id,
            "user_name": display_name(current_user, f"用户 {current_user.id}"),
            "member_role": member.role.value if hasattr(member.role, "value") else str(member.role),
            "kind": payload.kind,
            "action": payload.action,
            "component_id": payload.component_id,
            "action_id": payload.action_id,
            "value": payload.value,
            "href": payload.href,
            "label": payload.label,
            "element_index": payload.element_index,
        },
    )
    return MessageResponse(detail="Interaction delivered")


@router.post("/{conversation_id}/messages", response_model=MessageRead, status_code=201)
async def send_message(
    conversation_id: int,
    payload: MessageCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> MessageRead:
    message = await message_service.send_message(db, conversation_id, current_user, payload.content, payload.type, payload.metadata)
    message_data = message_service.build_new_message_read(message, current_user.id)
    member_ids = await online_conversation_member_ids(db, conversation_id)
    response_cache.invalidate_user_conversations(set(member_ids) | {current_user.id})
    schedule_realtime_task(send_message_updates(db, "message.created", message, member_ids))
    schedule_realtime_task(send_admin_message_signal("admin.message.changed", message, "created"))
    previous_level = getattr(message, "_level_previous", None)
    current_level = getattr(message, "_level_current", None)
    if isinstance(previous_level, int) and isinstance(current_level, int) and current_level > previous_level:
        schedule_realtime_task(
            manager.send_to_users(
                member_ids,
                "group.member.level_updated",
                {
                    "conversation_id": conversation_id,
                    "user_id": current_user.id,
                    "level": current_level,
                    "level_exp": getattr(message, "_sender_level_exp", 0),
                },
            )
        )
    if not current_user.content_moderation_whitelisted:
        moderation_service.schedule_message_moderation(message.id)
    return message_data


@router.post("/{target_conversation_id}/forward-bundle", response_model=MessageRead, status_code=201)
async def forward_bundle(
    target_conversation_id: int,
    payload: ForwardBundleCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> MessageRead:
    message = await message_service.create_forward_bundle(
        db,
        target_conversation_id,
        current_user,
        payload.source_conversation_id,
        payload.message_ids,
        payload.note,
    )
    message_data = message_service.build_new_message_read(message, current_user.id)
    member_ids = await online_conversation_member_ids(db, target_conversation_id)
    response_cache.invalidate_user_conversations(set(member_ids) | {current_user.id})
    schedule_realtime_task(send_message_updates(db, "message.created", message, member_ids))
    schedule_realtime_task(send_admin_message_signal("admin.message.changed", message, "created"))
    previous_level = getattr(message, "_level_previous", None)
    current_level = getattr(message, "_level_current", None)
    if isinstance(previous_level, int) and isinstance(current_level, int) and current_level > previous_level:
        schedule_realtime_task(
            manager.send_to_users(
                member_ids,
                "group.member.level_updated",
                {
                    "conversation_id": target_conversation_id,
                    "user_id": current_user.id,
                    "level": current_level,
                    "level_exp": getattr(message, "_sender_level_exp", 0),
                },
            )
        )
    if not current_user.content_moderation_whitelisted:
        moderation_service.schedule_message_moderation(message.id)
    return message_data


@router.post("/{conversation_id}/messages/{message_id}/recall", response_model=MessageRead)
async def recall_message(
    conversation_id: int,
    message_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> MessageRead:
    message = await message_service.recall_message(db, conversation_id, current_user.id, message_id)
    message_data = await message_service.build_message_read(db, message, current_user.id)
    member_ids = await online_conversation_member_ids(db, conversation_id)
    await send_message_updates(db, "message.recalled", message, member_ids)
    await send_admin_message_signal("admin.message.changed", message, "recalled")
    await send_conversation_updates(db, conversation_id, member_ids)
    return message_data


@router.delete("/{conversation_id}/messages/{message_id}", response_model=MessageRead)
async def hide_message(
    conversation_id: int,
    message_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> MessageRead:
    message = await message_service.hide_message_for_user(db, conversation_id, current_user.id, message_id)
    await send_conversation_updates(db, conversation_id, {current_user.id})
    return await message_service.build_message_read(db, message, current_user.id)


@router.post("/{conversation_id}/messages/{message_id}/bookmark", response_model=MessageBookmarkToggleRead)
async def toggle_message_bookmark(
    conversation_id: int,
    message_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> MessageBookmarkToggleRead:
    bookmarked = await message_service.toggle_message_bookmark(db, conversation_id, current_user.id, message_id)
    return MessageBookmarkToggleRead(bookmarked=bookmarked)


@router.get("/{conversation_id}/bookmarks", response_model=list[MessageRead])
async def bookmarked_messages(
    conversation_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[MessageRead]:
    items = await message_service.list_bookmarked_messages(db, conversation_id, current_user.id)
    return await message_service.build_message_reads(db, items, current_user.id)


@router.post("/{conversation_id}/messages/{message_id}/feature", response_model=MessageFeatureToggleRead)
async def toggle_message_feature(
    conversation_id: int,
    message_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> MessageFeatureToggleRead:
    featured = await message_service.toggle_featured_message(db, conversation_id, current_user.id, message_id)
    member_ids = await online_conversation_member_ids(db, conversation_id)
    await manager.send_to_users(member_ids, "message.featured.updated", {"conversation_id": conversation_id, "message_id": message_id, "featured": featured})
    return MessageFeatureToggleRead(featured=featured)


@router.get("/{conversation_id}/featured-messages", response_model=list[FeaturedMessageRead])
async def featured_messages(
    conversation_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[FeaturedMessageRead]:
    return await message_service.list_featured_messages(db, conversation_id, current_user.id)


@router.post("/{conversation_id}/messages/{message_id}/reactions", response_model=list[MessageReactionSummary])
async def toggle_message_reaction(
    conversation_id: int,
    message_id: int,
    payload: MessageReactionCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[MessageReactionSummary]:
    reactions = await message_service.toggle_message_reaction(
        db, conversation_id, current_user.id, message_id, payload.emoji
    )
    member_ids = await online_conversation_member_ids(db, conversation_id)
    await send_reaction_updates(db, conversation_id, message_id, member_ids)
    return reactions


@router.delete("/{conversation_id}/messages/{message_id}/reactions/{emoji}", response_model=list[MessageReactionSummary])
async def delete_message_reaction(
    conversation_id: int,
    message_id: int,
    emoji: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[MessageReactionSummary]:
    reactions = await message_service.delete_message_reaction(db, conversation_id, current_user.id, message_id, emoji)
    member_ids = await online_conversation_member_ids(db, conversation_id)
    await send_reaction_updates(db, conversation_id, message_id, member_ids)
    return reactions


@router.post("/{conversation_id}/read", response_model=ReadReceipt)
async def read_conversation(
    conversation_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    payload: Annotated[ReadConversationRequest | None, Body()] = None,
) -> ReadReceipt:
    receipt = await message_service.mark_conversation_read(
        db, conversation_id, current_user.id, payload.last_read_message_id if payload is not None else None
    )
    member_ids = await online_conversation_member_ids(db, conversation_id)
    await manager.send_to_users(member_ids, "conversation.read", receipt)
    await send_conversation_updates(db, conversation_id, {current_user.id})
    return receipt


# ---------------------------------------------------------------------------
# 戳一戳
#
# 完全不落库：它只是一条转瞬即逝的互动提示，存下来只会把消息表撑大，
# 也没人会想翻历史「谁戳过我」。所以只走 WebSocket 广播。
# ---------------------------------------------------------------------------

POKE_COOLDOWN_SECONDS = 2.0
# key 是 (发起者, 被戳者)，value 是上次成功的时间戳。进程内缓存即可 ——
# 多 worker 下最坏情况是冷却被放宽几倍，而这只是防手抖连点，不是安全边界。
_poke_cooldowns: dict[tuple[int, int], float] = {}


def _poke_allowed(actor_id: int, target_id: int) -> bool:
    now = asyncio.get_running_loop().time()
    key = (actor_id, target_id)
    last = _poke_cooldowns.get(key)
    if last is not None and now - last < POKE_COOLDOWN_SECONDS:
        return False
    _poke_cooldowns[key] = now
    if len(_poke_cooldowns) > 5000:
        # 简单封顶，别让长期运行的进程无限攒 key
        stale = [k for k, v in _poke_cooldowns.items() if now - v > 60]
        for k in stale:
            _poke_cooldowns.pop(k, None)
    return True


@router.post("/{conversation_id}/members/{user_id}/poke", response_model=MessageResponse)
async def poke_member(
    conversation_id: int,
    user_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> MessageResponse:
    if user_id == current_user.id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot poke yourself")
    await conversation_service.require_membership(db, conversation_id, current_user.id)
    target = await conversation_service.get_member(db, conversation_id, user_id)
    if not _poke_allowed(current_user.id, user_id):
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Poking too fast")

    member_ids = await online_conversation_member_ids(db, conversation_id)
    await manager.send_to_users(
        member_ids,
        "conversation.member.poked",
        {
            "conversation_id": conversation_id,
            "actor_id": current_user.id,
            "actor_name": display_name(current_user, f"用户 {current_user.id}"),
            "user_id": user_id,
            "user_name": display_name(target.user, f"用户 {user_id}"),
        },
    )
    return MessageResponse(detail="Poked")
