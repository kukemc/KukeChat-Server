from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.user import UserPublic

TaskStatus = Literal["pending", "in_progress", "completed", "cancelled"]
TaskPriority = Literal["low", "normal", "high", "urgent"]


def _to_utc(value: datetime | None) -> datetime | None:
    """带时区的时间先换算成 UTC。

    SQLite 只存年月日时分秒，时区信息会被直接丢掉；不换算的话，客户端传
    `18:00+08:00` 存下来就是 18:00，之后被当成 UTC，提醒会晚 8 小时。不带时区的按 UTC 理解，保持原样。
    """
    if value is None or value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc)


class TaskCreate(BaseModel):
    conversation_id: int = Field(gt=0)
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=4000)
    group_id: int | None = Field(default=None, gt=0)
    parent_id: int | None = Field(default=None, gt=0)
    priority: TaskPriority = "normal"
    start_at: datetime | None = None
    due_at: datetime | None = None
    remind_at: datetime | None = None
    assignee_ids: list[int] = Field(default_factory=list, max_length=30)
    watcher_ids: list[int] = Field(default_factory=list, max_length=30)
    send_to_conversation: bool = False

    @field_validator("title")
    @classmethod
    def normalize_title(cls, value: str) -> str:
        title = value.strip()
        if not title:
            raise ValueError("Title is required")
        return title

    @field_validator("description")
    @classmethod
    def normalize_description(cls, value: str) -> str:
        return value.strip()

    @field_validator("start_at", "due_at", "remind_at")
    @classmethod
    def normalize_times(cls, value: datetime | None) -> datetime | None:
        return _to_utc(value)

    @field_validator("assignee_ids", "watcher_ids")
    @classmethod
    def dedupe_ids(cls, value: list[int]) -> list[int]:
        return list(dict.fromkeys(int(item) for item in value if int(item) > 0))[:30]


class TaskUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=4000)
    list_id: int | None = Field(default=None, gt=0)
    priority: TaskPriority | None = None
    status: TaskStatus | None = None
    start_at: datetime | None = None
    due_at: datetime | None = None
    remind_at: datetime | None = None
    assignee_ids: list[int] | None = Field(default=None, max_length=30)
    watcher_ids: list[int] | None = Field(default=None, max_length=30)
    sort_order: int | None = None
    clear_start_at: bool = False
    clear_due_at: bool = False
    clear_remind_at: bool = False
    clear_list: bool = False

    @field_validator("title")
    @classmethod
    def normalize_title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        title = value.strip()
        if not title:
            raise ValueError("Title cannot be empty")
        return title

    @field_validator("description")
    @classmethod
    def normalize_description(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None

    @field_validator("start_at", "due_at", "remind_at")
    @classmethod
    def normalize_times(cls, value: datetime | None) -> datetime | None:
        return _to_utc(value)

    @field_validator("assignee_ids", "watcher_ids")
    @classmethod
    def dedupe_ids(cls, value: list[int] | None) -> list[int] | None:
        if value is None:
            return None
        return list(dict.fromkeys(int(item) for item in value if int(item) > 0))[:30]


class TaskCompleteUpdate(BaseModel):
    completed: bool = True


class TaskMemberUpdate(BaseModel):
    user_id: int = Field(gt=0)


class TaskGroupCreate(BaseModel):
    name: str = Field(min_length=1, max_length=60)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        name = value.strip()
        if not name:
            raise ValueError("Name is required")
        return name


class TaskGroupUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=60)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        name = value.strip()
        if not name:
            raise ValueError("Name is required")
        return name


class TaskGroupRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    sort_order: int = 0
    created_at: datetime


class TaskGroupMove(BaseModel):
    group_id: int | None = Field(default=None, gt=0)


class TaskRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    conversation_id: int
    conversation_title: str | None = None
    creator_id: int
    creator: UserPublic | None = None
    group_id: int | None = None
    group_name: str | None = None
    parent_id: int | None = None
    title: str
    description: str = ""
    status: TaskStatus = "pending"
    priority: TaskPriority = "normal"
    start_at: datetime | None = None
    due_at: datetime | None = None
    remind_at: datetime | None = None
    completed_at: datetime | None = None
    sort_order: int = 0
    assignees: list[UserPublic] = Field(default_factory=list)
    watchers: list[UserPublic] = Field(default_factory=list)
    subtask_total: int = 0
    subtask_done: int = 0
    comment_count: int = 0
    created_at: datetime
    updated_at: datetime
    is_creator: bool = False
    is_assignee: bool = False
    is_watcher: bool = False
    can_edit: bool = False


class TaskActivityRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    task_id: int
    task_title: str | None = None
    actor: UserPublic | None = None
    type: str
    payload: dict[str, object] = Field(default_factory=dict)
    created_at: datetime


class TaskCommentCreate(BaseModel):
    content: str = Field(min_length=1, max_length=2000)

    @field_validator("content")
    @classmethod
    def normalize_content(cls, value: str) -> str:
        content = value.strip()
        if not content:
            raise ValueError("Comment content is required")
        return content


class TaskCommentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    task_id: int
    author: UserPublic | None = None
    content: str
    created_at: datetime


class TaskDashboardRead(BaseModel):
    total: int = 0
    pending: int = 0
    in_progress: int = 0
    completed: int = 0
    overdue: int = 0
    due_today: int = 0
    due_soon: int = 0
