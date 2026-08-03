from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from app.models.enums import InviteTargetType
from app.schemas.conversation import GroupDiscoveryRead
from app.schemas.user import UserPublic


class InviteLinkRead(BaseModel):
    token: str
    url: str
    target_type: InviteTargetType
    target_id: int
    created_at: datetime


class InviteResolveRead(BaseModel):
    token: str
    target_type: InviteTargetType
    target_id: int
    group: GroupDiscoveryRead | None = None
    user: UserPublic | None = None
    already_joined: bool = False
    already_friends: bool = False


class InviteAcceptRead(BaseModel):
    target_type: InviteTargetType
    target_id: int
    conversation_id: int | None = None
    friend_request_id: int | None = None
    status: str
    joined_now: bool = False
