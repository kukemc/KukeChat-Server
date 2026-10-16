"""消息快照（精华、群待办共用，docs/group-plus.md 第 6、7 节）：把一条消息固化成几列，之后消息被修剪 / 发送人改名都不影响。

撤回：触发器（services/schema_extras.py）在消息被撤回的那一刻把 snap_content / snap_metadata 清掉、记下 snap_recalled_at，
所以读的时候看到 snap_recalled_at 就是「已撤回」，内容已经不在库里了。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.conversation import ConversationMember
from app.models.enums import MessageType
from app.models.message import Message
from app.models.user import User
from app.schemas.message import MessageRead
from app.schemas.user import UserPublic

UTC = timezone.utc
SUMMARY_LENGTH = 60
RECALLED_TEXT = "[消息已撤回]"
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

_PLACEHOLDERS = {
    "IMAGE": "[图片]",
    "VOICE": "[语音]",
    "FORWARD_BUNDLE": "[聊天记录]",
    "POLL": "[投票]",
    "RED_PACKET": "[红包]",
    "GIFT": "[礼物]",
}


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


async def message_snapshot_fields(db: AsyncSession, message: Message) -> dict[str, Any]:
    """这条消息现在的样子（发送人取群昵称 > 昵称 > 用户名、群头衔），作为 snap_* 列的值。"""
    sender = message.sender if "sender" in message.__dict__ and message.sender is not None else await db.get(User, message.sender_id)
    member = (
        await db.execute(
            select(ConversationMember).where(ConversationMember.conversation_id == message.conversation_id, ConversationMember.user_id == message.sender_id)
        )
    ).scalar_one_or_none()
    nickname = (member.nickname or "").strip() if member is not None and member.nickname else ""
    name = nickname or ((sender.nickname or "").strip() if sender is not None and sender.nickname else "") or (sender.username if sender is not None else "")
    title = (member.title or "").strip() if member is not None and member.title else None
    recalled = message.recalled_at is not None
    return {
        "snap_type": message.type.name if isinstance(message.type, MessageType) else str(message.type),
        "snap_content": "" if recalled else message.content,
        "snap_metadata": None if recalled else message.metadata_json,
        "snap_sender_id": message.sender_id,
        "snap_sender_username": sender.username if sender is not None else None,
        "snap_sender_name": name or None,
        "snap_sender_avatar": sender.avatar_url if sender is not None else None,
        "snap_sender_title": title or None,
        "snap_created_at": message.created_at,
        "snap_recalled_at": message.recalled_at,
        "snapshot_missing": False,
    }


def summary_of(row: Any) -> str:
    if row.snap_recalled_at is not None:
        return RECALLED_TEXT
    kind = row.snap_type or "TEXT"
    content = " ".join((row.snap_content or "").split())
    if kind in ("IMAGE", "VOICE"):
        return _PLACEHOLDERS[kind]
    if kind == "TEXT" and not content:
        metadata = parse_metadata(row.snap_metadata)
        if metadata and metadata.get("images"):
            return "[图片]"
    text = content[:SUMMARY_LENGTH]
    if not text:
        return _PLACEHOLDERS.get(kind, "")
    return text + ("…" if len(content) > SUMMARY_LENGTH else "")


def parse_metadata(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def sender_view(row: Any) -> dict[str, Any]:
    return {
        "id": row.snap_sender_id,
        "display_name": row.snap_sender_name or row.snap_sender_username or "",
        "avatar_url": row.snap_sender_avatar,
        "title": row.snap_sender_title,
    }


def snapshot_view(row: Any) -> dict[str, Any]:
    """契约里的 snapshot 对象（群待办用）。"""
    return {
        "type": (row.snap_type or "TEXT").lower(),
        "content": row.snap_content or "",
        "metadata": parse_metadata(row.snap_metadata),
        "sender": sender_view(row),
        "created_at": _aware(row.snap_created_at),
    }


def snapshot_message_read(row: Any, *, message_id: int, conversation_id: int, sender: User | None) -> MessageRead:
    """由快照构造一条 MessageRead。撤回的内容已经是空的。"""
    if sender is not None:
        public = UserPublic.model_validate(sender)
    else:
        public = UserPublic(
            id=row.snap_sender_id or 0,
            username=row.snap_sender_username or "",
            nickname=row.snap_sender_name,
            avatar_url=row.snap_sender_avatar,
            created_at=_aware(row.snap_created_at) or _EPOCH,
        )
    try:
        message_type = MessageType[row.snap_type or "TEXT"]
    except KeyError:
        message_type = MessageType.TEXT
    recalled_at = _aware(row.snap_recalled_at)
    return MessageRead(
        id=message_id,
        conversation_id=conversation_id,
        sender_id=row.snap_sender_id or 0,
        sender=public,
        sender_display_name=row.snap_sender_name,
        sender_title=row.snap_sender_title,
        type=message_type,
        content="" if recalled_at is not None else (row.snap_content or ""),
        metadata=None if recalled_at is not None else parse_metadata(row.snap_metadata),
        created_at=_aware(row.snap_created_at) or _EPOCH,
        recalled_at=recalled_at,
        featured=True,
    )
