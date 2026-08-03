from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.models.user import User
from app.schemas.common import MessageResponse
from app.schemas.task import (
    TaskActivityRead,
    TaskCommentCreate,
    TaskCommentRead,
    TaskCompleteUpdate,
    TaskCreate,
    TaskDashboardRead,
    TaskGroupCreate,
    TaskGroupMove,
    TaskGroupRead,
    TaskGroupUpdate,
    TaskMemberUpdate,
    TaskRead,
    TaskUpdate,
)
from app.services import task as task_service

router = APIRouter()


@router.get("", response_model=list[TaskRead])
async def list_tasks(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    scope: Annotated[Literal["assigned", "watching", "created", "all"], Query()] = "assigned",
    conversation_id: Annotated[int | None, Query(gt=0)] = None,
    status_filter: Annotated[Literal["pending", "in_progress", "completed", "cancelled"] | None, Query(alias="status")] = None,
    include_completed: Annotated[bool, Query()] = False,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> list[TaskRead]:
    tasks = await task_service.list_tasks(
        db, current_user.id, scope, conversation_id, status_filter, include_completed, limit
    )
    return await task_service.build_task_reads(db, tasks, current_user.id)


@router.post("", response_model=TaskRead, status_code=status.HTTP_201_CREATED)
async def create_task(
    payload: TaskCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> TaskRead:
    task, _card = await task_service.create_task(db, current_user, payload)
    return await task_service.build_task_read(db, task, current_user.id)


@router.get("/activities", response_model=list[TaskActivityRead])
async def list_activities(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    conversation_id: Annotated[int | None, Query(gt=0)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 60,
) -> list[TaskActivityRead]:
    return await task_service.list_activities(db, current_user.id, conversation_id, limit)


@router.get("/dashboard", response_model=TaskDashboardRead)
async def dashboard(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> TaskDashboardRead:
    return await task_service.dashboard(db, current_user.id)


@router.get("/groups", response_model=list[TaskGroupRead])
async def list_task_groups(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[TaskGroupRead]:
    groups = await task_service.list_groups(db, current_user.id)
    return task_service.group_reads(groups)


@router.post("/groups", response_model=TaskGroupRead, status_code=status.HTTP_201_CREATED)
async def create_task_group(
    payload: TaskGroupCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> TaskGroupRead:
    group = await task_service.create_group(db, current_user.id, payload.name)
    return TaskGroupRead.model_validate(group)


@router.put("/groups/{group_id}", response_model=TaskGroupRead)
async def update_task_group(
    group_id: int,
    payload: TaskGroupUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> TaskGroupRead:
    group = await task_service.update_group(db, current_user.id, group_id, payload.name)
    return TaskGroupRead.model_validate(group)


@router.delete("/groups/{group_id}", response_model=MessageResponse)
async def delete_task_group(
    group_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> MessageResponse:
    await task_service.delete_group(db, current_user.id, group_id)
    return MessageResponse(detail="Group deleted")


@router.get("/{task_id}", response_model=TaskRead)
async def get_task(
    task_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> TaskRead:
    task = await task_service.get_visible_task(db, task_id, current_user.id)
    return await task_service.build_task_read(db, task, current_user.id)


@router.put("/{task_id}", response_model=TaskRead)
async def update_task(
    task_id: int,
    payload: TaskUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> TaskRead:
    task = await task_service.update_task(db, current_user, task_id, payload)
    return await task_service.build_task_read(db, task, current_user.id)


@router.post("/{task_id}/complete", response_model=TaskRead)
async def complete_task(
    task_id: int,
    payload: TaskCompleteUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> TaskRead:
    task = await task_service.set_complete(db, current_user, task_id, payload.completed)
    return await task_service.build_task_read(db, task, current_user.id)


@router.delete("/{task_id}", response_model=MessageResponse)
async def delete_task(
    task_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> MessageResponse:
    await task_service.delete_task(db, current_user, task_id)
    return MessageResponse(detail="Task deleted")


@router.get("/{task_id}/activities", response_model=list[TaskActivityRead])
async def list_task_activities(
    task_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=100)] = 60,
) -> list[TaskActivityRead]:
    task = await task_service.get_visible_task(db, task_id, current_user.id)
    return await task_service.list_activities(db, current_user.id, task.conversation_id, limit)


@router.post("/{task_id}/group", response_model=MessageResponse)
async def move_task_to_group(
    task_id: int,
    payload: TaskGroupMove,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> MessageResponse:
    await task_service.move_task_to_group(db, current_user.id, task_id, payload.group_id)
    return MessageResponse(detail="Moved")


@router.get("/{task_id}/comments", response_model=list[TaskCommentRead])
async def list_task_comments(
    task_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[TaskCommentRead]:
    return await task_service.list_task_comments(db, current_user.id, task_id)


@router.post("/{task_id}/comments", response_model=TaskCommentRead, status_code=status.HTTP_201_CREATED)
async def create_task_comment(
    task_id: int,
    payload: TaskCommentCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> TaskCommentRead:
    return await task_service.create_task_comment(db, current_user, task_id, payload.content)


@router.post("/{task_id}/assignees", response_model=TaskRead)
async def add_assignee(
    task_id: int,
    payload: TaskMemberUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> TaskRead:
    task = await task_service.get_task(db, task_id)
    from app.models.task import TaskAssignee

    existing = await task_service._current_member_ids(db, task.id, TaskAssignee)
    merged = list(existing | {payload.user_id})
    task = await task_service.update_task(db, current_user, task_id, TaskUpdate(assignee_ids=merged))
    return await task_service.build_task_read(db, task, current_user.id)


@router.delete("/{task_id}/assignees/{user_id}", response_model=TaskRead)
async def remove_assignee(
    task_id: int,
    user_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> TaskRead:
    task = await task_service.get_task(db, task_id)
    from app.models.task import TaskAssignee

    existing = await task_service._current_member_ids(db, task.id, TaskAssignee)
    merged = [uid for uid in existing if uid != user_id]
    task = await task_service.update_task(db, current_user, task_id, TaskUpdate(assignee_ids=merged))
    return await task_service.build_task_read(db, task, current_user.id)


@router.post("/{task_id}/watchers", response_model=TaskRead)
async def add_watcher(
    task_id: int,
    payload: TaskMemberUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> TaskRead:
    task = await task_service.get_task(db, task_id)
    from app.models.task import TaskWatcher

    existing = await task_service._current_member_ids(db, task.id, TaskWatcher)
    merged = list(existing | {payload.user_id})
    task = await task_service.update_task(db, current_user, task_id, TaskUpdate(watcher_ids=merged))
    return await task_service.build_task_read(db, task, current_user.id)


@router.delete("/{task_id}/watchers/{user_id}", response_model=TaskRead)
async def remove_watcher(
    task_id: int,
    user_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> TaskRead:
    task = await task_service.get_task(db, task_id)
    from app.models.task import TaskWatcher

    existing = await task_service._current_member_ids(db, task.id, TaskWatcher)
    merged = [uid for uid in existing if uid != user_id]
    task = await task_service.update_task(db, current_user, task_id, TaskUpdate(watcher_ids=merged))
    return await task_service.build_task_read(db, task, current_user.id)
