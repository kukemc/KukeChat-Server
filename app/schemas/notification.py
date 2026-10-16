from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.schemas.user import UserPublic


NotificationCategory = Literal["all", "friend", "group", "post", "interact", "system"]
NotificationAction = Literal["friend_request", "group_join_request", "open_post", "open_conversation", "open_teamup", "open_bot", "none"]


class UnifiedNotificationRead(BaseModel):
    id: str
    category: NotificationCategory
    type: str
    title: str
    body: str | None = None
    time: datetime
    unread: bool = False
    pending: bool = False
    actor: UserPublic | None = None
    conversation_id: int | None = None
    conversation_title: str | None = None
    conversation_avatar_url: str | None = None
    post_id: int | None = None
    friend_request_id: int | None = None
    group_join_request_id: int | None = None
    action: NotificationAction = "none"
    payload: dict[str, Any] = Field(default_factory=dict)


class UnifiedNotificationListRead(BaseModel):
    items: list[UnifiedNotificationRead] = Field(default_factory=list)
    # 和 new_count 相同，留着兼容旧客户端
    unread_count: int = 0
    pending_count: int = 0
    counts: dict[str, int] = Field(default_factory=dict)
    # 上次打开通知中心的时间（POST /notifications/seen），从没打开过为 null
    seen_at: datetime | None = None
    # 角标该显示的数：seen_at 之后来的非动态通知 + 未读的动态通知
    new_count: int = 0


class NotificationSeenRead(BaseModel):
    seen_at: datetime
