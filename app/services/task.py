from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.models.conversation import Conversation, ConversationMember
from app.models.enums import MessageType
from app.models.friend import Friendship
from app.models.task import Task, TaskActivity, TaskAssignee, TaskComment, TaskGroup, TaskGroupItem, TaskWatcher
from app.models.user import User
from app.schemas.task import (
    TaskActivityRead,
    TaskCommentRead,
    TaskCreate,
    TaskDashboardRead,
    TaskGroupRead,
    TaskRead,
    TaskUpdate,
)
from app.schemas.user import UserPublic
from app.services import moderation as moderation_service
from app.services import task_assistant as assistant_service
from app.services.conversations import STAFF_ROLES, conversation_member_ids, require_membership
from app.websocket.manager import manager

logger = logging.getLogger(__name__)
UTC = timezone.utc

OPEN_STATUSES = ("pending", "in_progress")
_task_reminder_task: asyncio.Task | None = None


def _now() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _payload_to_json(payload: dict[str, Any] | None) -> str | None:
    if not payload:
        return None
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)


def _payload_from_json(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _display_name(user: User | None) -> str:
    if user is None:
        return "某成员"
    return (user.nickname or "").strip() or (user.username or "").strip() or "某成员"


async def _ensure_text_allowed(actor: User, text: str, context: str) -> None:
    """Block disallowed task text via the content-moderation service (fail-open on errors)."""
    content = (text or "").strip()
    if not content or not settings.content_moderation_enabled or actor.content_moderation_whitelisted:
        return
    try:
        result = await moderation_service._call_text_moderation_for_user(content, actor.id, context_id=context)
    except Exception:
        return
    decision = result.get("decision")
    severity = int(result.get("severity") or 0)
    if decision != "allow" or severity > 0:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="内容未通过审核,请修改后重试")


# ----- access helpers -----


async def _member_or_403(db: AsyncSession, conversation_id: int, user_id: int) -> ConversationMember:
    result = await db.execute(
        select(ConversationMember).where(
            ConversationMember.conversation_id == conversation_id,
            ConversationMember.user_id == user_id,
        )
    )
    member = result.scalar_one_or_none()
    if member is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You are not a member of this conversation")
    return member


async def _require_conversation_with_tasks(db: AsyncSession, conversation_id: int) -> Conversation:
    conversation = await db.get(Conversation, conversation_id)
    if conversation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")
    if not conversation.tasks_enabled:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="任务功能未在该群开启")
    return conversation


async def get_task(db: AsyncSession, task_id: int) -> Task:
    result = await db.execute(
        select(Task).options(joinedload(Task.creator)).where(Task.id == task_id, Task.deleted_at.is_(None))
    )
    task = result.scalar_one_or_none()
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
    return task


async def _is_task_participant(db: AsyncSession, task: Task, viewer_id: int) -> bool:
    if task.creator_id == viewer_id:
        return True
    assignee = await db.execute(
        select(TaskAssignee.id).where(TaskAssignee.task_id == task.id, TaskAssignee.user_id == viewer_id).limit(1)
    )
    if assignee.scalar_one_or_none() is not None:
        return True
    watcher = await db.execute(
        select(TaskWatcher.id).where(TaskWatcher.task_id == task.id, TaskWatcher.user_id == viewer_id).limit(1)
    )
    return watcher.scalar_one_or_none() is not None


async def get_visible_task(db: AsyncSession, task_id: int, viewer_id: int) -> Task:
    task = await get_task(db, task_id)
    # A group member, or anyone involved in the task (creator/assignee/watcher), can view it.
    member = await db.execute(
        select(ConversationMember.id).where(
            ConversationMember.conversation_id == task.conversation_id,
            ConversationMember.user_id == viewer_id,
        ).limit(1)
    )
    if member.scalar_one_or_none() is not None:
        return task
    if await _is_task_participant(db, task, viewer_id):
        return task
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You cannot view this task")


def _can_edit(task: Task, member: ConversationMember, assignee_ids: set[int] | None = None) -> bool:
    if task.creator_id == member.user_id or member.role in STAFF_ROLES:
        return True
    return assignee_ids is not None and member.user_id in assignee_ids


# ----- read builders -----


async def _members_map(db: AsyncSession, user_ids: set[int]) -> dict[int, UserPublic]:
    if not user_ids:
        return {}
    result = await db.execute(select(User).where(User.id.in_(user_ids)))
    return {user.id: UserPublic.model_validate(user) for user in result.scalars().all()}


async def build_task_reads(db: AsyncSession, tasks: list[Task], viewer_id: int) -> list[TaskRead]:
    if not tasks:
        return []
    task_ids = [task.id for task in tasks]
    conversation_ids = {task.conversation_id for task in tasks}

    assignee_rows = await db.execute(select(TaskAssignee).where(TaskAssignee.task_id.in_(task_ids)))
    watcher_rows = await db.execute(select(TaskWatcher).where(TaskWatcher.task_id.in_(task_ids)))
    assignees_by_task: dict[int, list[int]] = {}
    watchers_by_task: dict[int, list[int]] = {}
    user_ids: set[int] = set()
    for row in assignee_rows.scalars().all():
        assignees_by_task.setdefault(row.task_id, []).append(row.user_id)
        user_ids.add(row.user_id)
    for row in watcher_rows.scalars().all():
        watchers_by_task.setdefault(row.task_id, []).append(row.user_id)
        user_ids.add(row.user_id)
    user_ids.update(task.creator_id for task in tasks)

    # subtask progress
    subtotal: dict[int, int] = {}
    subdone: dict[int, int] = {}
    sub_rows = await db.execute(
        select(Task.parent_id, Task.status, func.count(Task.id))
        .where(Task.parent_id.in_(task_ids), Task.deleted_at.is_(None))
        .group_by(Task.parent_id, Task.status)
    )
    for parent_id, st, count in sub_rows.all():
        subtotal[parent_id] = subtotal.get(parent_id, 0) + int(count or 0)
        if st == "completed":
            subdone[parent_id] = subdone.get(parent_id, 0) + int(count or 0)

    comment_counts: dict[int, int] = {}
    comment_rows = await db.execute(
        select(TaskComment.task_id, func.count(TaskComment.id))
        .where(TaskComment.task_id.in_(task_ids), TaskComment.deleted_at.is_(None))
        .group_by(TaskComment.task_id)
    )
    for tid, count in comment_rows.all():
        comment_counts[tid] = int(count or 0)

    members_map = await _members_map(db, user_ids)

    # viewer's personal 分组 placement for these tasks
    group_by_task: dict[int, tuple[int, str]] = {}
    item_rows = await db.execute(
        select(TaskGroupItem.task_id, TaskGroup.id, TaskGroup.name)
        .join(TaskGroup, TaskGroup.id == TaskGroupItem.group_id)
        .where(
            TaskGroupItem.task_id.in_(task_ids),
            TaskGroupItem.owner_id == viewer_id,
            TaskGroup.deleted_at.is_(None),
        )
    )
    for tid, gid, gname in item_rows.all():
        group_by_task[tid] = (gid, gname)

    conversation_titles: dict[int, str | None] = {}
    if conversation_ids:
        conv_rows = await db.execute(select(Conversation.id, Conversation.title).where(Conversation.id.in_(conversation_ids)))
        conversation_titles = {cid: title for cid, title in conv_rows.all()}

    # viewer staff roles per conversation (for can_edit)
    staff_conversations: set[int] = set()
    viewer_member_rows = await db.execute(
        select(ConversationMember.conversation_id, ConversationMember.role).where(
            ConversationMember.conversation_id.in_(conversation_ids),
            ConversationMember.user_id == viewer_id,
        )
    )
    for cid, role in viewer_member_rows.all():
        if role in STAFF_ROLES:
            staff_conversations.add(cid)

    reads: list[TaskRead] = []
    for task in tasks:
        assignee_ids = assignees_by_task.get(task.id, [])
        watcher_ids = watchers_by_task.get(task.id, [])
        reads.append(
            TaskRead(
                id=task.id,
                conversation_id=task.conversation_id,
                conversation_title=conversation_titles.get(task.conversation_id),
                creator_id=task.creator_id,
                creator=members_map.get(task.creator_id),
                group_id=group_by_task.get(task.id, (None, None))[0],
                group_name=group_by_task.get(task.id, (None, None))[1],
                parent_id=task.parent_id,
                title=task.title,
                description=task.description or "",
                status=task.status,
                priority=task.priority,
                start_at=task.start_at,
                due_at=task.due_at,
                remind_at=task.remind_at,
                completed_at=task.completed_at,
                sort_order=task.sort_order,
                assignees=[members_map[uid] for uid in assignee_ids if uid in members_map],
                watchers=[members_map[uid] for uid in watcher_ids if uid in members_map],
                subtask_total=subtotal.get(task.id, 0),
                subtask_done=subdone.get(task.id, 0),
                comment_count=comment_counts.get(task.id, 0),
                created_at=task.created_at,
                updated_at=task.updated_at,
                is_creator=task.creator_id == viewer_id,
                is_assignee=viewer_id in assignee_ids,
                is_watcher=viewer_id in watcher_ids,
                can_edit=task.creator_id == viewer_id or viewer_id in assignee_ids or task.conversation_id in staff_conversations,
            )
        )
    return reads


async def build_task_read(db: AsyncSession, task: Task, viewer_id: int) -> TaskRead:
    return (await build_task_reads(db, [task], viewer_id))[0]


# ----- listing -----


async def _viewer_conversation_ids(db: AsyncSession, viewer_id: int) -> list[int]:
    result = await db.execute(
        select(ConversationMember.conversation_id).where(ConversationMember.user_id == viewer_id)
    )
    return list(result.scalars().all())


async def list_tasks(
    db: AsyncSession,
    viewer_id: int,
    scope: str,
    conversation_id: int | None,
    status_filter: str | None,
    include_completed: bool,
    limit: int,
) -> list[Task]:
    conversation_ids = await _viewer_conversation_ids(db, viewer_id)
    query = select(Task).options(joinedload(Task.creator)).where(Task.deleted_at.is_(None))

    if scope == "assigned":
        # tasks I'm responsible for, even in groups I'm not a member of
        query = query.where(Task.id.in_(select(TaskAssignee.task_id).where(TaskAssignee.user_id == viewer_id)))
    elif scope == "watching":
        query = query.where(Task.id.in_(select(TaskWatcher.task_id).where(TaskWatcher.user_id == viewer_id)))
    elif scope == "created":
        query = query.where(Task.creator_id == viewer_id)
    else:
        # scope == "all" -> any task in the viewer's conversations, or tasks I'm involved in
        if not conversation_ids:
            query = query.where(
                or_(
                    Task.creator_id == viewer_id,
                    Task.id.in_(select(TaskAssignee.task_id).where(TaskAssignee.user_id == viewer_id)),
                    Task.id.in_(select(TaskWatcher.task_id).where(TaskWatcher.user_id == viewer_id)),
                )
            )
        else:
            query = query.where(
                or_(
                    Task.conversation_id.in_(conversation_ids),
                    Task.creator_id == viewer_id,
                    Task.id.in_(select(TaskAssignee.task_id).where(TaskAssignee.user_id == viewer_id)),
                    Task.id.in_(select(TaskWatcher.task_id).where(TaskWatcher.user_id == viewer_id)),
                )
            )

    if conversation_id is not None:
        query = query.where(Task.conversation_id == conversation_id)

    if status_filter:
        query = query.where(Task.status == status_filter)
    elif not include_completed:
        query = query.where(Task.status.in_(OPEN_STATUSES))

    query = query.order_by(Task.sort_order.asc(), Task.created_at.desc(), Task.id.desc()).limit(limit)
    result = await db.execute(query)
    tasks = list(result.scalars().all())

    # Always include the direct subtasks of any returned parent, even if the
    # viewer isn't the creator/assignee/watcher of those subtasks or no longer a
    # member of their conversation. Otherwise a parent's `subtask_total` counts a
    # child that never gets returned, so the list can't render it on expand.
    parent_ids = [task.id for task in tasks]
    if parent_ids:
        seen_ids = {task.id for task in tasks}
        sub_result = await db.execute(
            select(Task)
            .options(joinedload(Task.creator))
            .where(Task.parent_id.in_(parent_ids), Task.deleted_at.is_(None))
        )
        for subtask in sub_result.scalars().all():
            if subtask.id not in seen_ids:
                tasks.append(subtask)
                seen_ids.add(subtask.id)
    return tasks


async def list_activities(db: AsyncSession, viewer_id: int, conversation_id: int | None, limit: int) -> list[TaskActivityRead]:
    conversation_ids = await _viewer_conversation_ids(db, viewer_id)
    conds = [
        Task.creator_id == viewer_id,
        Task.id.in_(select(TaskAssignee.task_id).where(TaskAssignee.user_id == viewer_id)),
        Task.id.in_(select(TaskWatcher.task_id).where(TaskWatcher.user_id == viewer_id)),
    ]
    if conversation_ids:
        conds.append(Task.conversation_id.in_(conversation_ids))
    task_filter = select(Task.id).where(Task.deleted_at.is_(None), or_(*conds))
    if conversation_id is not None:
        task_filter = task_filter.where(Task.conversation_id == conversation_id)
    result = await db.execute(
        select(TaskActivity, Task.title)
        .join(Task, Task.id == TaskActivity.task_id)
        .options(joinedload(TaskActivity.actor))
        .where(TaskActivity.task_id.in_(task_filter))
        .order_by(TaskActivity.created_at.desc(), TaskActivity.id.desc())
        .limit(limit)
    )
    reads: list[TaskActivityRead] = []
    for activity, task_title in result.all():
        reads.append(
            TaskActivityRead(
                id=activity.id,
                task_id=activity.task_id,
                task_title=task_title,
                actor=UserPublic.model_validate(activity.actor) if activity.actor is not None else None,
                type=activity.type,
                payload=_payload_from_json(activity.payload_json),
                created_at=activity.created_at,
            )
        )
    return reads


async def dashboard(db: AsyncSession, viewer_id: int) -> TaskDashboardRead:
    assigned_subquery = select(TaskAssignee.task_id).where(TaskAssignee.user_id == viewer_id)
    base = select(Task).where(
        Task.deleted_at.is_(None),
        or_(Task.creator_id == viewer_id, Task.id.in_(assigned_subquery)),
    )
    result = await db.execute(base)
    tasks = list(result.scalars().all())
    now = _now()
    soon_cutoff = now + timedelta(hours=settings.task_reminder_advance_hours)
    today_end = now + timedelta(days=1)
    data = TaskDashboardRead(total=len(tasks))
    for task in tasks:
        if task.status == "completed":
            data.completed += 1
            continue
        if task.status == "in_progress":
            data.in_progress += 1
        elif task.status == "pending":
            data.pending += 1
        due = _as_utc(task.due_at)
        if due is not None and task.status in OPEN_STATUSES:
            if due < now:
                data.overdue += 1
            elif due <= today_end:
                data.due_today += 1
            elif due <= soon_cutoff:
                data.due_soon += 1
    return data


# ----- personal 分组 (groups) -----


async def list_groups(db: AsyncSession, owner_id: int) -> list[TaskGroup]:
    result = await db.execute(
        select(TaskGroup)
        .where(TaskGroup.owner_id == owner_id, TaskGroup.deleted_at.is_(None))
        .order_by(TaskGroup.sort_order.asc(), TaskGroup.id.asc())
    )
    return list(result.scalars().all())


async def create_group(db: AsyncSession, owner_id: int, name: str) -> TaskGroup:
    max_order = await db.execute(
        select(func.coalesce(func.max(TaskGroup.sort_order), 0)).where(TaskGroup.owner_id == owner_id)
    )
    group = TaskGroup(owner_id=owner_id, name=name, sort_order=int(max_order.scalar_one() or 0) + 1)
    db.add(group)
    await db.commit()
    await db.refresh(group)
    return group


async def update_group(db: AsyncSession, owner_id: int, group_id: int, name: str) -> TaskGroup:
    group = await db.get(TaskGroup, group_id)
    if group is None or group.deleted_at is not None or group.owner_id != owner_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Group not found")
    group.name = name
    await db.commit()
    await db.refresh(group)
    return group


async def delete_group(db: AsyncSession, owner_id: int, group_id: int) -> None:
    group = await db.get(TaskGroup, group_id)
    if group is None or group.deleted_at is not None or group.owner_id != owner_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Group not found")
    group.deleted_at = _now()
    await db.execute(TaskGroupItem.__table__.delete().where(TaskGroupItem.group_id == group_id))
    await db.commit()


async def move_task_to_group(db: AsyncSession, owner_id: int, task_id: int, group_id: int | None) -> None:
    task = await get_visible_task(db, task_id, owner_id)
    await db.execute(
        TaskGroupItem.__table__.delete().where(
            TaskGroupItem.owner_id == owner_id, TaskGroupItem.task_id == task.id
        )
    )
    if group_id is not None:
        group = await db.get(TaskGroup, group_id)
        if group is None or group.deleted_at is not None or group.owner_id != owner_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Group not found")
        db.add(TaskGroupItem(owner_id=owner_id, task_id=task.id, group_id=group_id))
    await db.commit()


def group_reads(groups: list[TaskGroup]) -> list[TaskGroupRead]:
    return [TaskGroupRead.model_validate(item) for item in groups]


# ----- activity + realtime helpers -----


async def _add_activity(db: AsyncSession, task: Task, actor_id: int | None, type_: str, payload: dict[str, Any] | None = None) -> None:
    db.add(TaskActivity(task_id=task.id, actor_id=actor_id, type=type_, payload_json=_payload_to_json(payload)))


async def _broadcast_task_changed(db: AsyncSession, conversation_id: int, task_id: int, actor_id: int, event: str) -> None:
    try:
        recipients = set(await conversation_member_ids(db, conversation_id))
        recipients |= await _current_member_ids(db, task_id, TaskAssignee)
        recipients |= await _current_member_ids(db, task_id, TaskWatcher)
        if recipients:
            await manager.send_to_users(
                recipients,
                "task.changed",
                {"conversation_id": conversation_id, "task_id": task_id, "actor_id": actor_id, "event": event},
            )
    except Exception:
        logger.exception("Failed to broadcast task.changed for task %s", task_id)


def _task_event_metadata(task: Task, event_type: str, actor: User | None, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": event_type,
        "task_id": task.id,
        "conversation_id": task.conversation_id,
        "title": task.title,
        "status": task.status,
        "due_at": _as_utc(task.due_at).isoformat() if task.due_at is not None else None,
    }
    if actor is not None:
        payload["actor_name"] = _display_name(actor)
    if extra:
        payload.update(extra)
    return {"task_event": payload}


async def _notify(
    db: AsyncSession,
    task: Task,
    recipient_ids: set[int],
    actor_id: int | None,
    content: str,
    event_type: str,
    actor: User | None,
    extra: dict[str, Any] | None = None,
    include_actor: bool = False,
) -> None:
    if include_actor:
        targets = {uid for uid in recipient_ids if uid}
    else:
        targets = {uid for uid in recipient_ids if uid and uid != actor_id}
    if not targets:
        return
    metadata = _task_event_metadata(task, event_type, actor, extra)
    await assistant_service.send_assistant_message_to_many(db, targets, content, metadata)


# ----- create / update / complete / delete -----


async def _validate_member_ids(db: AsyncSession, conversation_id: int, user_ids: list[int], actor_id: int | None = None) -> list[int]:
    """Assignees/watchers may be group members OR friends of the actor (chat-app model)."""
    if not user_ids:
        return []
    member_rows = await db.execute(
        select(ConversationMember.user_id).where(
            ConversationMember.conversation_id == conversation_id,
            ConversationMember.user_id.in_(user_ids),
        )
    )
    valid = set(member_rows.scalars().all())
    if actor_id is not None:
        friend_rows = await db.execute(
            select(Friendship.friend_id).where(
                Friendship.user_id == actor_id,
                Friendship.friend_id.in_(user_ids),
            )
        )
        valid.update(friend_rows.scalars().all())
        valid.add(actor_id)
    return [uid for uid in user_ids if uid in valid]


async def create_task(db: AsyncSession, actor: User, payload: TaskCreate) -> tuple[Task, dict[str, Any] | None]:
    conversation = await _require_conversation_with_tasks(db, payload.conversation_id)
    member = await _member_or_403(db, conversation.id, actor.id)
    if conversation.task_creation_permission == "admins" and member.role not in STAFF_ROLES:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="仅管理员可以创建任务")
    await _ensure_text_allowed(actor, f"{payload.title}\n{payload.description or ''}", f"task:{conversation.id}")

    if payload.parent_id is not None:
        parent = await db.get(Task, payload.parent_id)
        if parent is None or parent.deleted_at is not None or parent.conversation_id != conversation.id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Parent task not found")
    if payload.group_id is not None:
        group = await db.get(TaskGroup, payload.group_id)
        if group is None or group.deleted_at is not None or group.owner_id != actor.id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Group not found")

    assignee_ids = await _validate_member_ids(db, conversation.id, payload.assignee_ids, actor.id)
    watcher_ids = await _validate_member_ids(db, conversation.id, payload.watcher_ids, actor.id)

    task = Task(
        conversation_id=conversation.id,
        creator_id=actor.id,
        parent_id=payload.parent_id,
        title=payload.title,
        description=payload.description,
        priority=payload.priority,
        start_at=payload.start_at,
        due_at=payload.due_at,
        remind_at=payload.remind_at,
    )
    db.add(task)
    await db.flush()
    for uid in assignee_ids:
        db.add(TaskAssignee(task_id=task.id, user_id=uid))
    for uid in watcher_ids:
        db.add(TaskWatcher(task_id=task.id, user_id=uid))
    if payload.group_id is not None:
        db.add(TaskGroupItem(owner_id=actor.id, task_id=task.id, group_id=payload.group_id))
    await _add_activity(db, task, actor.id, "created", {"title": task.title})
    task.creator = actor
    await db.commit()
    await db.refresh(task)

    posted_card: dict[str, Any] | None = None
    if payload.send_to_conversation:
        posted_card = await _post_task_card(db, conversation, task, actor, assignee_ids)

    await _notify(
        db,
        task,
        set(assignee_ids),
        actor.id,
        f"{_display_name(actor)} 创建了任务并指派给你：{task.title}",
        "assigned",
        actor,
        include_actor=True,
    )
    await _broadcast_task_changed(db, conversation.id, task.id, actor.id, "created")
    return task, posted_card


async def _assignee_brief(db: AsyncSession, user_ids: list[int]) -> list[dict[str, Any]]:
    if not user_ids:
        return []
    result = await db.execute(select(User).where(User.id.in_(user_ids)))
    users = {user.id: user for user in result.scalars().all()}
    brief: list[dict[str, Any]] = []
    for uid in user_ids:
        user = users.get(uid)
        if user is not None:
            brief.append({"id": user.id, "name": _display_name(user), "avatar_url": user.avatar_url})
    return brief


async def _post_task_card(
    db: AsyncSession, conversation: Conversation, task: Task, actor: User, assignee_ids: list[int]
) -> dict[str, Any] | None:
    from app.services import messages as message_service

    description = (task.description or "").strip()
    metadata = {
        "share_card": {
            "type": "task",
            "task_id": task.id,
            "conversation_id": conversation.id,
            "title": task.title,
            "description": description[:200] if description else None,
            "status": task.status,
            "priority": task.priority,
            "due_at": _as_utc(task.due_at).isoformat() if task.due_at is not None else None,
            "assignee_count": len(assignee_ids),
            "assignees": await _assignee_brief(db, assignee_ids),
            "creator_name": _display_name(actor),
        }
    }
    try:
        message = await message_service.send_message(
            db, conversation.id, actor, f"[任务] {task.title}", MessageType.TEXT, metadata
        )
    except Exception:
        logger.exception("Failed to post task card for task %s", task.id)
        return None
    try:
        from app.api.v1.conversations import send_message_updates

        member_ids = await conversation_member_ids(db, conversation.id)
        await send_message_updates(db, "message.created", message, member_ids)
    except Exception:
        logger.exception("Failed to broadcast task card for task %s", task.id)
    return {"message_id": message.id}


async def _current_member_ids(db: AsyncSession, task_id: int, table) -> set[int]:
    result = await db.execute(select(table.user_id).where(table.task_id == task_id))
    return set(result.scalars().all())


async def _optional_member(db: AsyncSession, conversation_id: int, user_id: int) -> ConversationMember | None:
    result = await db.execute(
        select(ConversationMember).where(
            ConversationMember.conversation_id == conversation_id,
            ConversationMember.user_id == user_id,
        )
    )
    return result.scalar_one_or_none()


async def update_task(db: AsyncSession, actor: User, task_id: int, payload: TaskUpdate) -> Task:
    task = await get_task(db, task_id)
    member = await _optional_member(db, task.conversation_id, actor.id)
    current_assignees = await _current_member_ids(db, task.id, TaskAssignee)
    can_edit = (
        task.creator_id == actor.id
        or actor.id in current_assignees
        or (member is not None and member.role in STAFF_ROLES)
    )
    if not can_edit:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="只有负责人或创建者可以修改任务")

    data = payload.model_dump(exclude_unset=True)
    changed: list[str] = []
    notify_extra: dict[str, Any] = {}

    new_title = data.get("title") if "title" in data and data["title"] is not None else None
    new_desc = data.get("description") if "description" in data and data["description"] is not None else None
    if new_title is not None or new_desc is not None:
        await _ensure_text_allowed(actor, f"{new_title or task.title}\n{new_desc if new_desc is not None else (task.description or '')}", f"task:{task.conversation_id}")

    if "title" in data and data["title"] is not None and data["title"] != task.title:
        task.title = data["title"]
        changed.append("title")
    if "description" in data and data["description"] is not None:
        task.description = data["description"]
        changed.append("description")
    if "priority" in data and data["priority"] is not None:
        task.priority = data["priority"]
        changed.append("priority")
    if "sort_order" in data and data["sort_order"] is not None:
        task.sort_order = int(data["sort_order"])
        changed.append("sort_order")

    if "start_at" in data and not payload.clear_start_at:
        task.start_at = data["start_at"]
        changed.append("start_at")
    if payload.clear_start_at:
        task.start_at = None
        changed.append("start_at")

    due_changed = False
    if "due_at" in data and not payload.clear_due_at:
        task.due_at = data["due_at"]
        task.reminder_due_sent_at = None
        task.reminder_soon_sent_at = None
        changed.append("due_at")
        due_changed = True
    if payload.clear_due_at:
        task.due_at = None
        task.reminder_due_sent_at = None
        task.reminder_soon_sent_at = None
        changed.append("due_at")
        due_changed = True

    if "remind_at" in data and not payload.clear_remind_at:
        task.remind_at = data["remind_at"]
        task.reminder_custom_sent_at = None
        changed.append("remind_at")
    if payload.clear_remind_at:
        task.remind_at = None
        task.reminder_custom_sent_at = None
        changed.append("remind_at")

    status_changed = False
    if "status" in data and data["status"] is not None and data["status"] != task.status:
        task.status = data["status"]
        status_changed = True
        if task.status == "completed":
            task.completed_at = _now()
            task.completed_by_id = actor.id
        else:
            task.completed_at = None
            task.completed_by_id = None
        changed.append("status")

    # assignees / watchers diff
    assignee_added: list[int] = []
    assignee_removed: list[int] = []
    if payload.assignee_ids is not None:
        valid = set(await _validate_member_ids(db, task.conversation_id, payload.assignee_ids, actor.id))
        current = await _current_member_ids(db, task.id, TaskAssignee)
        assignee_added = list(valid - current)
        assignee_removed = list(current - valid)
        for uid in assignee_removed:
            await db.execute(
                TaskAssignee.__table__.delete().where(TaskAssignee.task_id == task.id, TaskAssignee.user_id == uid)
            )
        for uid in assignee_added:
            db.add(TaskAssignee(task_id=task.id, user_id=uid))
        if assignee_added or assignee_removed:
            changed.append("assignees")

    if payload.watcher_ids is not None:
        valid = set(await _validate_member_ids(db, task.conversation_id, payload.watcher_ids, actor.id))
        current = await _current_member_ids(db, task.id, TaskWatcher)
        for uid in current - valid:
            await db.execute(
                TaskWatcher.__table__.delete().where(TaskWatcher.task_id == task.id, TaskWatcher.user_id == uid)
            )
        for uid in valid - current:
            db.add(TaskWatcher(task_id=task.id, user_id=uid))
        if valid != current:
            changed.append("watchers")

    if not changed:
        return task

    task.updated_at = _now()
    if due_changed:
        notify_extra["due_at"] = _as_utc(task.due_at).isoformat() if task.due_at is not None else None
    if status_changed:
        if task.parent_id is not None:
            status_activity = "subtask_completed" if task.status == "completed" else "subtask_reopened"
        else:
            status_activity = "completed" if task.status == "completed" else "status_changed"
        await _add_activity(db, task, actor.id, status_activity, {"status": task.status})
    if assignee_added:
        for uid in assignee_added:
            await _add_activity(db, task, actor.id, "assignee_added", {"user_id": uid})
    if assignee_removed:
        for uid in assignee_removed:
            await _add_activity(db, task, actor.id, "assignee_removed", {"user_id": uid})
    if due_changed:
        await _add_activity(db, task, actor.id, "due_changed", {"due_at": notify_extra.get("due_at")})
    meaningful = [c for c in changed if c != "sort_order"]
    if meaningful and not (status_changed or assignee_added or assignee_removed or due_changed):
        await _add_activity(db, task, actor.id, "updated", {"fields": meaningful})
    await db.commit()
    await db.refresh(task)

    if not meaningful:
        # silent reorder — refresh board without notifications
        await _broadcast_task_changed(db, task.conversation_id, task.id, actor.id, "reordered")
        return task

    # notifications
    assignee_ids_now = await _current_member_ids(db, task.id, TaskAssignee)
    watcher_ids_now = await _current_member_ids(db, task.id, TaskWatcher)
    actor_name = _display_name(actor)
    if assignee_added:
        await _notify(db, task, set(assignee_added), actor.id, f"{actor_name} 将你设为任务负责人：{task.title}", "assignee_added", actor, include_actor=True)
    if assignee_removed:
        await _notify(db, task, set(assignee_removed), actor.id, f"{actor_name} 取消了你的任务负责人：{task.title}", "assignee_removed", actor)
    if status_changed and task.status == "completed":
        await _notify(db, task, assignee_ids_now | watcher_ids_now | {task.creator_id}, actor.id, f"{actor_name} 完成了任务：{task.title}", "completed", actor)
    elif status_changed:
        await _notify(db, task, assignee_ids_now | watcher_ids_now, actor.id, f"{actor_name} 重新开启了任务：{task.title}", "reopened", actor)
    elif due_changed:
        due_text = notify_extra.get("due_at") or "无"
        await _notify(db, task, assignee_ids_now | watcher_ids_now, actor.id, f"{actor_name} 修改了任务截止时间：{task.title}", "due_changed", actor, {"due_at": notify_extra.get("due_at")})
    elif changed:
        await _notify(db, task, assignee_ids_now | watcher_ids_now, actor.id, f"{actor_name} 修改了任务：{task.title}", "updated", actor)

    await _broadcast_task_changed(db, task.conversation_id, task.id, actor.id, "updated")
    return task


async def set_complete(db: AsyncSession, actor: User, task_id: int, completed: bool) -> Task:
    task = await get_task(db, task_id)
    member = await _optional_member(db, task.conversation_id, actor.id)
    assignee_ids = await _current_member_ids(db, task.id, TaskAssignee)
    if not (task.creator_id == actor.id or (member is not None and member.role in STAFF_ROLES) or actor.id in assignee_ids):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only the assignee, creator or admins can change completion")
    new_status = "completed" if completed else "pending"
    if task.status == new_status:
        return task
    task.status = new_status
    task.updated_at = _now()
    if completed:
        task.completed_at = _now()
        task.completed_by_id = actor.id
    else:
        task.completed_at = None
        task.completed_by_id = None
    is_subtask = task.parent_id is not None
    noun = "子任务" if is_subtask else "任务"
    if completed:
        activity_type = "subtask_completed" if is_subtask else "completed"
    else:
        activity_type = "subtask_reopened" if is_subtask else "reopened"
    await _add_activity(db, task, actor.id, activity_type, {"status": task.status})
    await db.commit()
    await db.refresh(task)

    watcher_ids = await _current_member_ids(db, task.id, TaskWatcher)
    actor_name = _display_name(actor)
    if completed:
        await _notify(db, task, assignee_ids | watcher_ids | {task.creator_id}, actor.id, f"{actor_name} 完成了{noun}：{task.title}", activity_type, actor)
    else:
        await _notify(db, task, assignee_ids | watcher_ids, actor.id, f"{actor_name} 重新开启了{noun}：{task.title}", activity_type, actor)
    await _broadcast_task_changed(db, task.conversation_id, task.id, actor.id, "completed" if completed else "reopened")
    return task


async def delete_task(db: AsyncSession, actor: User, task_id: int) -> int:
    task = await get_task(db, task_id)
    member = await _optional_member(db, task.conversation_id, actor.id)
    assignees = await _current_member_ids(db, task.id, TaskAssignee)
    if not (task.creator_id == actor.id or actor.id in assignees or (member is not None and member.role in STAFF_ROLES)):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You cannot delete this task")
    conversation_id = task.conversation_id
    task.deleted_at = _now()
    # cascade soft-delete subtasks
    rows = await db.execute(select(Task).where(Task.parent_id == task.id, Task.deleted_at.is_(None)))
    for subtask in rows.scalars().all():
        subtask.deleted_at = _now()
    await db.commit()
    await _broadcast_task_changed(db, conversation_id, task_id, actor.id, "deleted")
    return conversation_id


# ----- comments -----


async def list_task_comments(db: AsyncSession, viewer_id: int, task_id: int) -> list[TaskCommentRead]:
    task = await get_visible_task(db, task_id, viewer_id)
    result = await db.execute(
        select(TaskComment)
        .options(joinedload(TaskComment.author))
        .where(TaskComment.task_id == task.id, TaskComment.deleted_at.is_(None))
        .order_by(TaskComment.created_at.asc(), TaskComment.id.asc())
    )
    return [
        TaskCommentRead(
            id=comment.id,
            task_id=comment.task_id,
            author=UserPublic.model_validate(comment.author) if comment.author is not None else None,
            content=comment.content,
            created_at=comment.created_at,
        )
        for comment in result.scalars().all()
    ]


async def create_task_comment(db: AsyncSession, actor: User, task_id: int, content: str) -> TaskCommentRead:
    task = await get_task(db, task_id)
    # creator/assignee/watcher or group member may comment
    member = await _optional_member(db, task.conversation_id, actor.id)
    if member is None and not await _is_task_participant(db, task, actor.id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You cannot comment on this task")
    await _ensure_text_allowed(actor, content, f"task_comment:{task.id}")
    comment = TaskComment(task_id=task.id, author_id=actor.id, content=content)
    db.add(comment)
    await db.flush()
    comment.author = actor
    await db.commit()
    await db.refresh(comment)

    assignee_ids = await _current_member_ids(db, task.id, TaskAssignee)
    watcher_ids = await _current_member_ids(db, task.id, TaskWatcher)
    recipients = assignee_ids | watcher_ids | {task.creator_id}
    await _notify(db, task, recipients, actor.id, f"{_display_name(actor)} 评论了任务：{task.title}", "commented", actor, {"comment": content[:80]})
    await _broadcast_task_changed(db, task.conversation_id, task.id, actor.id, "commented")

    return TaskCommentRead(
        id=comment.id,
        task_id=comment.task_id,
        author=UserPublic.model_validate(actor),
        content=comment.content,
        created_at=comment.created_at,
    )


# ----- reminder worker -----


def start_task_reminder_worker() -> None:
    global _task_reminder_task
    if _task_reminder_task is not None and not _task_reminder_task.done():
        return
    _task_reminder_task = asyncio.create_task(_task_reminder_worker())


async def stop_task_reminder_worker() -> None:
    global _task_reminder_task
    task = _task_reminder_task
    _task_reminder_task = None
    if task is None or task.done():
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def _task_reminder_worker() -> None:
    while True:
        try:
            async with AsyncSessionLocal() as db:
                await scan_and_send_reminders(db)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Task reminder scan failed")
        await asyncio.sleep(settings.task_reminder_check_interval_seconds)


async def _assignee_ids_for(db: AsyncSession, task_id: int) -> set[int]:
    return await _current_member_ids(db, task_id, TaskAssignee)


async def scan_and_send_reminders(db: AsyncSession) -> int:
    now = _now()
    advance = now + timedelta(hours=settings.task_reminder_advance_hours)
    sent = 0
    reminder_types = (
        (Task.reminder_due_sent_at, Task.due_at <= now, "due_reminder", "任务已到期："),
        (Task.reminder_soon_sent_at, and_(Task.due_at > now, Task.due_at <= advance), "due_soon", "任务即将到期："),
        (Task.reminder_custom_sent_at, Task.remind_at <= now, "remind", "任务提醒："),
    )
    for marker, window, event_type, label in reminder_types:
        # Fetch scalar ids, not ORM objects: bounded memory even with a large backlog.
        criteria = (Task.deleted_at.is_(None), Task.status.in_(OPEN_STATUSES), marker.is_(None), window)
        rows = await db.execute(select(Task.id).where(*criteria).order_by(Task.id).limit(100))
        for task_id in rows.scalars().all():
            try:
                # Atomic conditional claim: workers may select the same id, but only
                # one can change its NULL marker. Commit before any assistant writes.
                claim = await db.execute(update(Task).where(Task.id == task_id, *criteria).values({marker.key: now}))
                if claim.rowcount != 1:
                    await db.rollback()
                    continue
                await db.commit()
                task = await db.get(Task, task_id, populate_existing=True)
                recipients = await _assignee_ids_for(db, task_id) | {task.creator_id}
                await _notify(db, task, recipients, None, f"{label}{task.title}", event_type, None,
                              {"due_at": _as_utc(task.due_at).isoformat() if task.due_at else None})
                sent += 1
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Failed to send %s for task %s", event_type, task_id)
                await db.rollback()
                # A failed notification may already have reached a recipient. Clearing
                # the claim permits retry; exact-once requires a durable outbox.
                await db.execute(update(Task).where(Task.id == task_id, marker == now).values({marker.key: None}))
                await db.commit()
    return sent
