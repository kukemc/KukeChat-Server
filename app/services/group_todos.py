"""群待办（docs/group-plus.md 第 7 节）：每个群最多一条，群主 / 管理员设置，设置时固化消息快照，并给全群发一条 @全体 式的提醒。

提醒是一条普通文本消息（type = text，content = "[群待办] 摘要"，metadata = { mention_all, group_todo }），
走现有 @全体成员 的链路（conversations.last_mention_all_message_id → 会话列表的 mention_pending = "all"），
但不占用 @全体成员 的每日次数（messages.send_message 的 allow_group_todo）。
"""

from __future__ import annotations

from datetime import timezone
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import group_plus_tiers
from app.models.conversation import Conversation, ConversationMember
from app.models.enums import ConversationType, MemberRole, MessageType
from app.models.group_plus import GroupTodo
from app.models.message import Message
from app.models.user import User
from app.services import group_snapshots
from app.services import messages as message_service

UTC = timezone.utc
STAFF_ROLES = {MemberRole.OWNER, MemberRole.ADMIN}
TODOABLE_TYPES = {MessageType.TEXT, MessageType.IMAGE, MessageType.VOICE, MessageType.FORWARD_BUNDLE, MessageType.POLL}


def _error(status_code: int, code: str, message: str, **extra: Any) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message, **extra})


def todo_view(todo: GroupTodo, *, message_exists: bool) -> dict[str, Any]:
    recalled = todo.snap_recalled_at is not None
    return {
        "conversation_id": todo.conversation_id,
        "message_id": todo.message_id,
        "message_exists": message_exists and not recalled,
        "recalled": recalled,
        "summary": group_snapshots.summary_of(todo),
        "snapshot": group_snapshots.snapshot_view(todo),
        "set_by": {"id": todo.set_by_id, "display_name": todo.set_by_name or "", "avatar_url": todo.set_by_avatar},
        "set_at": group_snapshots._aware(todo.set_at),
    }


async def views_for(db: AsyncSession, conversation_ids: list[int]) -> dict[int, dict[str, Any]]:
    """批量给会话列表用：{ 会话 id: GroupTodo 对象 }，没有待办的会话不在里面。一到两条查询。"""
    if not conversation_ids:
        return {}
    todos = (await db.execute(select(GroupTodo).where(GroupTodo.conversation_id.in_(conversation_ids)))).scalars().all()
    if not todos:
        return {}
    message_ids = [todo.message_id for todo in todos if todo.message_id is not None]
    existing: set[int] = set()
    if message_ids:
        existing = set((await db.execute(select(Message.id).where(Message.id.in_(message_ids), Message.recalled_at.is_(None)))).scalars().all())
    return {todo.conversation_id: todo_view(todo, message_exists=todo.message_id in existing) for todo in todos}


async def get_todo(db: AsyncSession, conversation_id: int, user_id: int) -> dict[str, Any] | None:
    await _membership(db, conversation_id, user_id)
    return (await views_for(db, [conversation_id])).get(conversation_id)


async def _membership(db: AsyncSession, conversation_id: int, user_id: int) -> tuple[Conversation, ConversationMember]:
    member = (
        await db.execute(
            select(ConversationMember).where(ConversationMember.conversation_id == conversation_id, ConversationMember.user_id == user_id)
        )
    ).scalar_one_or_none()
    conversation = await db.get(Conversation, conversation_id)
    if conversation is None or member is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")
    if conversation.type != ConversationType.GROUP:
        raise _error(status.HTTP_400_BAD_REQUEST, "not_group", "私聊没有群待办")
    return conversation, member


def _require_staff(member: ConversationMember) -> None:
    if member.role not in STAFF_ROLES:
        raise _error(status.HTTP_403_FORBIDDEN, "not_group_staff", "只有群主和管理员可以设置群待办")


async def set_todo(db: AsyncSession, user: User, conversation_id: int, message_id: Any) -> tuple[dict[str, Any], Message]:
    """设置 / 替换群待办。返回 (GroupTodo 对象, 落下的提醒消息)；消息的广播由接口层做。"""
    user_id = user.id
    _conversation, member = await _membership(db, conversation_id, user_id)
    _require_staff(member)
    if isinstance(message_id, bool) or not isinstance(message_id, int) or message_id <= 0:
        raise _error(status.HTTP_422_UNPROCESSABLE_ENTITY, "not_todoable", "message_id 不对")
    # 和精华一样：要是我能看到的、本群的消息
    message = await message_service._get_visible_message(db, conversation_id, user_id, message_id)
    if message.recalled_at is not None or message.type not in TODOABLE_TYPES:
        raise _error(status.HTTP_422_UNPROCESSABLE_ENTITY, "not_todoable", "这条消息不能设为群待办")
    fields = await group_snapshots.message_snapshot_fields(db, message)
    set_by_name = member.nickname or user.nickname or user.username
    set_by_avatar = user.avatar_url
    now = group_plus_tiers._now()

    todo = (await db.execute(select(GroupTodo).where(GroupTodo.conversation_id == conversation_id))).scalar_one_or_none()
    if todo is None:
        todo = GroupTodo(conversation_id=conversation_id)
        db.add(todo)
    for key, value in fields.items():
        setattr(todo, key, value)
    todo.message_id = message_id
    todo.set_by_id = user_id
    todo.set_by_name = set_by_name
    todo.set_by_avatar = set_by_avatar
    todo.set_at = now
    await db.flush()
    summary = group_snapshots.summary_of(todo)
    # 提醒消息和待办在同一个事务里：发消息那一步被拒（限频、被禁言），待办也不设
    try:
        reminder = await message_service.send_message(
            db,
            conversation_id,
            user,
            f"[群待办] {summary}",
            MessageType.TEXT,
            {"mention_all": True, "group_todo": {"message_id": message_id}},
            allow_group_todo=True,
            commit=False,
        )
        await db.commit()
    except BaseException:
        await db.rollback()
        raise
    return todo_view(todo, message_exists=True), reminder


async def clear_todo(db: AsyncSession, user_id: int, conversation_id: int) -> None:
    _conversation, member = await _membership(db, conversation_id, user_id)
    _require_staff(member)
    todo = (await db.execute(select(GroupTodo).where(GroupTodo.conversation_id == conversation_id))).scalar_one_or_none()
    if todo is None:
        raise _error(status.HTTP_404_NOT_FOUND, "todo_not_found", "这个群没有待办")
    await db.delete(todo)
    await db.commit()
