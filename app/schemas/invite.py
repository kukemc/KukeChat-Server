from __future__ import annotations

from datetime import datetime

from typing import Literal

from pydantic import BaseModel, Field

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
    # 群邀请：点「加入」会怎样（见 services/invites.py 的 group_invite_action）
    #   join：直接进群；request：提交入群申请等审核；answer：先回答入群问题再提交申请；
    #   forbidden：这个邀请进不了（仅邀请的群，邀请人不是群主 / 管理员）
    join_action: Literal["join", "request", "answer", "forbidden"] | None = None
    inviter_id: int | None = None


class InviteAcceptRead(BaseModel):
    target_type: InviteTargetType
    target_id: int
    conversation_id: int | None = None
    friend_request_id: int | None = None
    # joined：进群了（joined_now 表示这次才进）；pending：入群申请已提交（join_request_id）；好友邀请是 pending / self
    status: str
    joined_now: bool = False
    join_request_id: int | None = None


class InviteAcceptRequest(BaseModel):
    """接受群邀请时带的入群申请内容：只有邀请人不是群主 / 管理员、群又要审核时才用得上。老客户端不带请求体。"""

    message: str | None = Field(default=None, max_length=200)
    answer: str | None = Field(default=None, max_length=500)
