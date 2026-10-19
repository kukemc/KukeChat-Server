"""投票。

一个投票就是一条 type=poll 的消息：定义（标题、选项）写进消息 metadata 随消息下发，
票数等实时状态在读取时叠加上去。消息被撤回即投票作废。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import release_connection
from app.models.conversation import Conversation
from app.models.enums import ConversationType, MessageType
from app.models.message import Message
from app.models.poll import Poll, PollVote
from app.models.user import User
from app.schemas.message import MessageRead
from app.schemas.poll import PollCreate
from app.services import messages as message_service
from app.services.conversations import STAFF_ROLES, require_membership, self_chat_unsupported

UTC = timezone.utc
MAX_TITLE_LENGTH = 80
MIN_OPTIONS = 2
MAX_OPTIONS = 10
MAX_OPTION_LENGTH = 40
MAX_DURATION = timedelta(days=30)


def _error(code: str, message: str, status_code: int = status.HTTP_422_UNPROCESSABLE_ENTITY) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _normalize(payload: PollCreate) -> tuple[str, list[str], datetime | None]:
    title = payload.title.strip()
    if not 1 <= len(title) <= MAX_TITLE_LENGTH:
        raise _error("invalid_poll", f"投票标题需要 1 到 {MAX_TITLE_LENGTH} 个字")
    options = [item.strip() for item in payload.options if isinstance(item, str) and item.strip()]
    if not MIN_OPTIONS <= len(options) <= MAX_OPTIONS:
        raise _error("invalid_poll", f"投票需要 {MIN_OPTIONS} 到 {MAX_OPTIONS} 个选项")
    if any(len(item) > MAX_OPTION_LENGTH for item in options):
        raise _error("invalid_poll", f"每个选项最多 {MAX_OPTION_LENGTH} 个字")
    if len(set(options)) != len(options):
        raise _error("invalid_poll", "选项不能重复")
    closes_at = _aware(payload.closes_at)
    if closes_at is not None:
        now = datetime.now(UTC)
        if closes_at <= now + timedelta(seconds=30):
            raise _error("invalid_poll", "截止时间必须在一分钟之后")
        if closes_at > now + MAX_DURATION:
            raise _error("invalid_poll", f"截止时间最长 {MAX_DURATION.days} 天")
    return title, options, closes_at


async def _ensure_text_allowed(db: AsyncSession, actor: User, text: str, context: str) -> None:
    """标题和选项先过一遍文本审核，明确违规的不让发。审核服务不可用时放行，消息仍会走异步审核。"""
    if not settings.content_moderation_enabled or actor.content_moderation_whitelisted:
        return
    from app.services import moderation as moderation_service

    # 审核服务可能要几秒：先结束请求的读事务、把数据库连接还回去，不带着连接等
    await release_connection(db)
    try:
        result = await moderation_service._call_text_moderation_for_user(text, actor.id, context_id=context)
    except Exception:
        return
    if result.get("decision") == "block":
        raise _error("poll_rejected", "投票内容未通过审核，请修改后重试")


def fallback_text(title: str, options: list[str]) -> str:
    """消息正文：给不认识 poll 类型的旧客户端、搜索和通知预览用。"""
    lines = [f"[投票] {title}"] + [f"{index}. {text}" for index, text in enumerate(options, start=1)]
    return "\n".join(lines)


async def create_poll(db: AsyncSession, conversation_id: int, actor: User, payload: PollCreate) -> Message:
    title, options, closes_at = _normalize(payload)
    # 「我的设备」会话里不能发投票（docs/self-chat.md 第 4 节）。在审核标题之前挡掉，不为它调审核服务
    conversation = await db.get(Conversation, conversation_id)
    if conversation is not None and conversation.type == ConversationType.SELF:
        await require_membership(db, conversation_id, actor.id, clear_expired_mute=False)
        raise self_chat_unsupported("发投票")
    await _ensure_text_allowed(db, actor, "\n".join([title, *options]), f"poll:{conversation_id}")
    definition = {
        "id": None,
        "title": title,
        "options": [{"id": index, "text": text} for index, text in enumerate(options, start=1)],
        "multiple": payload.multiple,
        "anonymous": payload.anonymous,
        "closes_at": closes_at.isoformat() if closes_at else None,
    }
    # 走正常的发消息路径：成员校验、禁言、限频、活跃度都和普通消息一致。
    message = await message_service.send_message(
        db, conversation_id, actor, fallback_text(title, options), MessageType.POLL, {"poll": definition}, allow_poll=True
    )
    poll = Poll(
        message_id=message.id,
        conversation_id=conversation_id,
        creator_id=actor.id,
        title=title,
        options=json.dumps(definition["options"], ensure_ascii=False, separators=(",", ":")),
        multiple=payload.multiple,
        anonymous=payload.anonymous,
        closes_at=closes_at,
    )
    db.add(poll)
    await db.flush()
    definition["id"] = poll.id
    message.metadata_json = json.dumps({"poll": definition}, ensure_ascii=False, separators=(",", ":"))
    await db.commit()
    return message


# ---------------------------------------------------------------- 状态


def _options(poll: Poll) -> list[dict[str, Any]]:
    try:
        value = json.loads(poll.options)
    except json.JSONDecodeError:
        return []
    return [item for item in value if isinstance(item, dict) and isinstance(item.get("id"), int)] if isinstance(value, list) else []


def is_closed(poll: Poll, now: datetime | None = None) -> bool:
    if poll.closed_at is not None:
        return True
    closes_at = _aware(poll.closes_at)
    return closes_at is not None and closes_at <= (now or datetime.now(UTC))


async def _states(db: AsyncSession, polls: list[Poll], viewer_id: int | None) -> dict[int, dict[str, Any]]:
    if not polls:
        return {}
    poll_ids = [poll.id for poll in polls]
    counts: dict[tuple[int, int], int] = {}
    rows = await db.execute(
        select(PollVote.poll_id, PollVote.option_id, func.count()).where(PollVote.poll_id.in_(poll_ids)).group_by(PollVote.poll_id, PollVote.option_id)
    )
    for poll_id, option_id, count in rows.all():
        counts[(poll_id, option_id)] = int(count)
    voters = {
        poll_id: int(count)
        for poll_id, count in (
            await db.execute(
                select(PollVote.poll_id, func.count(func.distinct(PollVote.user_id))).where(PollVote.poll_id.in_(poll_ids)).group_by(PollVote.poll_id)
            )
        ).all()
    }
    mine: dict[int, list[int]] = {}
    if viewer_id is not None:
        rows = await db.execute(
            select(PollVote.poll_id, PollVote.option_id).where(PollVote.poll_id.in_(poll_ids), PollVote.user_id == viewer_id).order_by(PollVote.option_id)
        )
        for poll_id, option_id in rows.all():
            mine.setdefault(poll_id, []).append(option_id)
    now = datetime.now(UTC)
    states: dict[int, dict[str, Any]] = {}
    for poll in polls:
        closed = is_closed(poll, now)
        states[poll.id] = {
            "conversation_id": poll.conversation_id,
            "message_id": poll.message_id,
            "poll_id": poll.id,
            "options": [{"id": option["id"], "votes": counts.get((poll.id, option["id"]), 0)} for option in _options(poll)],
            "total_voters": voters.get(poll.id, 0),
            "closed": closed,
            "closed_at": _aware(poll.closed_at) or (_aware(poll.closes_at) if closed else None),
            "my_option_ids": mine.get(poll.id, []),
        }
    return states


async def poll_state(db: AsyncSession, poll: Poll, viewer_id: int | None) -> dict[str, Any]:
    return (await _states(db, [poll], viewer_id))[poll.id]


def _merge_state(metadata: dict[str, Any], state: dict[str, Any] | None) -> dict[str, Any]:
    definition = dict(metadata.get("poll") or {})
    votes = {item["id"]: item["votes"] for item in (state or {}).get("options", [])}
    definition["options"] = [
        {**option, "votes": votes.get(option.get("id"), 0)} for option in definition.get("options", []) if isinstance(option, dict)
    ]
    definition["total_voters"] = (state or {}).get("total_voters", 0)
    definition["my_option_ids"] = (state or {}).get("my_option_ids", [])
    definition["closed"] = (state or {}).get("closed", False)
    closed_at = (state or {}).get("closed_at")
    definition["closed_at"] = closed_at.isoformat() if isinstance(closed_at, datetime) else None
    return {**metadata, "poll": definition}


def _poll_reads(reads: list[MessageRead]) -> list[MessageRead]:
    return [
        read
        for read in reads
        if read.type == MessageType.POLL and isinstance(read.metadata, dict) and isinstance(read.metadata.get("poll"), dict)
    ]


def fill_empty_state(read: MessageRead) -> MessageRead:
    """刚创建的投票还没有票，不用查库。"""
    for item in _poll_reads([read]):
        item.metadata = _merge_state(item.metadata, None)  # type: ignore[arg-type]
    return read


async def attach_states(db: AsyncSession, reads: list[MessageRead], viewer_id: int | None) -> None:
    """给消息列表里的投票消息叠加票数和「我投了什么」。列表里没有投票时不产生任何查询。"""
    targets = _poll_reads(reads)
    if not targets:
        return
    poll_ids = [read.metadata["poll"].get("id") for read in targets]  # type: ignore[index]
    poll_ids = [poll_id for poll_id in poll_ids if isinstance(poll_id, int)]
    polls = list((await db.execute(select(Poll).where(Poll.id.in_(poll_ids)))).scalars().all()) if poll_ids else []
    states = await _states(db, polls, viewer_id)
    for read in targets:
        read.metadata = _merge_state(read.metadata, states.get(read.metadata["poll"].get("id")))  # type: ignore[arg-type,index]


# ---------------------------------------------------------------- 投票 / 结束 / 查看


async def _load(db: AsyncSession, conversation_id: int, poll_id: int) -> tuple[Poll, Message]:
    poll = await db.get(Poll, poll_id)
    if poll is None or poll.conversation_id != conversation_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Poll not found")
    message = await db.get(Message, poll.message_id)
    if message is None or message.recalled_at is not None:
        raise _error("poll_void", "这个投票已被撤回", status.HTTP_409_CONFLICT)
    return poll, message


async def cast_vote(db: AsyncSession, conversation_id: int, poll_id: int, user: User, option_ids: list[int]) -> dict[str, Any]:
    """提交完整的选项集合：可以改票，空集合即撤票。"""
    await require_membership(db, conversation_id, user.id)
    poll, _message = await _load(db, conversation_id, poll_id)
    if is_closed(poll):
        raise _error("poll_closed", "投票已经结束", status.HTTP_409_CONFLICT)
    chosen = list(dict.fromkeys(option_ids))
    valid = {option["id"] for option in _options(poll)}
    if any(option_id not in valid for option_id in chosen):
        raise _error("invalid_poll_option", "选项不存在")
    if not poll.multiple and len(chosen) > 1:
        raise _error("poll_single_choice", "这个投票只能选一项")
    await db.execute(delete(PollVote).where(PollVote.poll_id == poll.id, PollVote.user_id == user.id))
    db.add_all([PollVote(poll_id=poll.id, user_id=user.id, option_id=option_id) for option_id in chosen])
    await db.commit()
    return await poll_state(db, poll, user.id)


async def close_poll(db: AsyncSession, conversation_id: int, poll_id: int, user: User) -> dict[str, Any]:
    member = await require_membership(db, conversation_id, user.id)
    poll, _message = await _load(db, conversation_id, poll_id)
    conversation = await db.get(Conversation, conversation_id)
    is_staff = conversation is not None and conversation.type == ConversationType.GROUP and member.role in STAFF_ROLES
    if poll.creator_id != user.id and not is_staff:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail={"code": "poll_forbidden", "message": "只有发起人或群管理员可以结束投票"})
    if not is_closed(poll):
        poll.closed_at = datetime.now(UTC)
        poll.closed_by_id = user.id
        await db.commit()
    return await poll_state(db, poll, user.id)


async def get_state(db: AsyncSession, conversation_id: int, poll_id: int, user: User) -> dict[str, Any]:
    await require_membership(db, conversation_id, user.id)
    poll, _message = await _load(db, conversation_id, poll_id)
    return await poll_state(db, poll, user.id)


async def list_voters(db: AsyncSession, conversation_id: int, poll_id: int, option_id: int, user: User, limit: int, offset: int) -> tuple[list[User], int]:
    await require_membership(db, conversation_id, user.id)
    poll, _message = await _load(db, conversation_id, poll_id)
    if poll.anonymous:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail={"code": "poll_anonymous", "message": "匿名投票不能查看投票人"})
    if option_id not in {option["id"] for option in _options(poll)}:
        raise _error("invalid_poll_option", "选项不存在")
    clause = (PollVote.poll_id == poll.id, PollVote.option_id == option_id)
    total = int((await db.execute(select(func.count()).select_from(PollVote).where(*clause))).scalar_one() or 0)
    rows = await db.execute(
        select(User).join(PollVote, PollVote.user_id == User.id).where(*clause).order_by(PollVote.id.asc()).offset(offset).limit(limit)
    )
    return list(rows.scalars().all()), total


def public_state(state: dict[str, Any]) -> dict[str, Any]:
    """广播给全会话的汇总：去掉只对本人有意义的字段。"""
    return {key: value for key, value in state.items() if key != "my_option_ids"}
