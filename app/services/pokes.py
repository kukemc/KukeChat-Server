"""戳一戳：一条留在聊天记录里的系统消息。

文案是「发起人 X了X 对方 + 对方自己设置的后缀」，动作和后缀都由**被戳的人**在资料里设置，
发起人可以临时换一个动作。动作只能从预设里选：单个汉字没法做有意义的文本审核，
而任意动词套进「X了X」很容易拼出不该出现的话。
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.conversation import ConversationMember
from app.models.enums import ConversationType, MemberRole, MessageType
from app.models.message import Message
from app.models.user import User
from app.services import messages as message_service
from app.services.conversations import get_conversation, get_member, require_membership
from app.services.penalties import ensure_can_post

UTC = timezone.utc

SYSTEM_EVENT = "conversation.member.poked"
DEFAULT_ACTION = "戳"
PRESET_ACTIONS: tuple[str, ...] = ("戳", "拍", "摸", "亲", "抱", "捏", "揉", "贴", "蹭", "敲", "挠", "咬")
SUFFIX_MAX_LENGTH = 16

# 落库以后刷屏的代价变大了（每个会话只保留最近 1000 条消息），所以比原来的「2 秒防连点」严得多
PAIR_COOLDOWN_SECONDS = 10
ACTOR_LIMIT_PER_MINUTE = 6
CONVERSATION_LIMIT_PER_MINUTE = 12
# 只看会话最近这么多条消息来数频率：走 (conversation_id, id) 索引，代价有上限
RECENT_SCAN_LIMIT = 80

STAFF_ROLES = {MemberRole.OWNER, MemberRole.ADMIN}
_WHITESPACE_RE = re.compile(r"\s+")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f​-‏ -‮⁠-⁤﻿]")


def _error(code: str, message: str, status_code: int = status.HTTP_422_UNPROCESSABLE_ENTITY) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


def options() -> dict[str, Any]:
    return {"actions": list(PRESET_ACTIONS), "default_action": DEFAULT_ACTION, "suffix_max_length": SUFFIX_MAX_LENGTH}


def normalize_action(value: str | None) -> str:
    """空值回到默认动作；其余必须是预设里的一个。"""
    action = (value or "").strip()
    if not action:
        return DEFAULT_ACTION
    if action not in PRESET_ACTIONS:
        raise _error("invalid_poke_action", "不支持这个动作，请从预设里选一个")
    return action


def normalize_suffix(value: str | None) -> str:
    # 先把换行等空白折成空格，再去掉零宽字符之类看不见的东西
    suffix = _CONTROL_RE.sub("", _WHITESPACE_RE.sub(" ", value or "")).strip()
    if len(suffix) > SUFFIX_MAX_LENGTH:
        raise _error("invalid_poke_suffix", f"戳一戳文案最多 {SUFFIX_MAX_LENGTH} 个字")
    return suffix


async def ensure_suffix_allowed(user: User, suffix: str) -> None:
    """保存前过一遍文本审核，明确违规的不让存。

    戳一戳消息是系统消息，不走消息的异步审核，所以这里是这段文字唯一的一道机器检查。
    审核服务不可用时放行，不能因为它挂了就改不了资料。
    """
    if not suffix or not settings.content_moderation_enabled or user.content_moderation_whitelisted:
        return
    from app.services import moderation as moderation_service

    try:
        result = await moderation_service._call_text_moderation_for_user(suffix, user.id, context_id=f"poke_suffix:{user.id}")
    except Exception:
        return
    if result.get("decision") == "block":
        raise _error("poke_suffix_rejected", "戳一戳文案未通过审核，请修改后重试")


def compose_text(actor_name: str, action: str, user_name: str, suffix: str) -> str:
    text = f"{actor_name} {action}了{action} {user_name}"
    return f"{text} {suffix}" if suffix else text


def _member_name(member: ConversationMember | None, user: User | None, fallback: str) -> str:
    nickname = (member.nickname or "").strip() if member is not None else ""
    if nickname:
        return nickname
    if user is None:
        return fallback
    return (user.nickname or "").strip() or user.username or fallback


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _poked_user_id(metadata_json: str) -> int | None:
    try:
        value = json.loads(metadata_json).get("user_id")
    except (TypeError, ValueError, AttributeError):
        return None
    return value if isinstance(value, int) else None


async def _enforce_rate_limit(db: AsyncSession, conversation_id: int, actor_id: int, target_id: int, now: datetime) -> None:
    rows = await db.execute(
        select(Message.sender_id, Message.type, Message.metadata_json, Message.created_at)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.id.desc())
        .limit(RECENT_SCAN_LIMIT)
    )
    minute_ago = now - timedelta(minutes=1)
    pair_cutoff = now - timedelta(seconds=PAIR_COOLDOWN_SECONDS)
    conversation_count = 0
    actor_count = 0
    for sender_id, message_type, metadata_json, created_at in rows.all():
        if message_type != MessageType.SYSTEM or not metadata_json or f'"{SYSTEM_EVENT}"' not in metadata_json:
            continue
        created = _as_utc(created_at)
        if created < minute_ago:
            # 按 id 倒序，后面的只会更早
            break
        conversation_count += 1
        if sender_id != actor_id:
            continue
        actor_count += 1
        if created >= pair_cutoff and _poked_user_id(metadata_json) == target_id:
            raise _error("poke_rate_limited", "戳得太快了，等一会儿再戳 TA", status.HTTP_429_TOO_MANY_REQUESTS)
    if actor_count >= ACTOR_LIMIT_PER_MINUTE:
        raise _error("poke_rate_limited", "戳得太频繁了，歇一会儿吧", status.HTTP_429_TOO_MANY_REQUESTS)
    if conversation_count >= CONVERSATION_LIMIT_PER_MINUTE:
        raise _error("poke_rate_limited", "这个会话里的戳一戳太多了，稍后再试", status.HTTP_429_TOO_MANY_REQUESTS)


async def create_poke(
    db: AsyncSession,
    conversation_id: int,
    actor: User,
    target_id: int,
    action: str | None = None,
) -> tuple[Message, dict[str, Any]]:
    if target_id == actor.id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot poke yourself")
    ensure_can_post(actor)
    membership = await require_membership(db, conversation_id, actor.id)
    target = await get_member(db, conversation_id, target_id)
    conversation = await get_conversation(db, conversation_id)
    if conversation.type == ConversationType.GROUP and membership.role not in STAFF_ROLES:
        # 戳一戳会在聊天里留下一行字，群里禁了言就不能用它绕过去
        if conversation.all_muted or membership.muted:
            raise _error("poke_muted", "你在这个群里被禁言了，不能戳一戳", status.HTTP_403_FORBIDDEN)

    target_user = target.user
    chosen = normalize_action(action) if (action or "").strip() else normalize_stored_action(target_user)
    suffix = (target_user.poke_suffix or "").strip() if target_user is not None else ""
    await _enforce_rate_limit(db, conversation_id, actor.id, target_id, datetime.now(UTC))

    actor_name = _member_name(membership, actor, f"用户 {actor.id}")
    user_name = _member_name(target, target_user, f"用户 {target_id}")
    metadata: dict[str, Any] = {
        "system_event": SYSTEM_EVENT,
        "actor_id": actor.id,
        "user_id": target_id,
        "action": chosen,
        "suffix": suffix,
        "actor_name": actor_name,
        "user_name": user_name,
    }
    message = await message_service.create_system_message(
        db, conversation_id, actor, compose_text(actor_name, chosen, user_name, suffix), metadata
    )
    return message, metadata


def normalize_stored_action(user: User | None) -> str:
    """资料里存的动作；预设调整后旧值可能已经不在表里，回到默认。"""
    stored = (user.poke_action or "").strip() if user is not None else ""
    return stored if stored in PRESET_ACTIONS else DEFAULT_ACTION
