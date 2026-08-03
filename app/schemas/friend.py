from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import FriendRequestStatus
from app.schemas.user import UserPublic


class FriendRequestCreate(BaseModel):
    receiver_id: int = Field(gt=0)


class FriendRequestRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    requester_id: int
    receiver_id: int
    requester: UserPublic
    receiver: UserPublic
    status: FriendRequestStatus
    created_at: datetime
    updated_at: datetime


class FriendRead(BaseModel):
    user: UserPublic
    created_at: datetime


class TemporaryConversationBlockRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    blocked_user_id: int
    blocked_user: UserPublic
    created_at: datetime
