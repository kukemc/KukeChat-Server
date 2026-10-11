from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.models.enums import PlatformRole, PresenceStatus, UserStatus


class UserPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    nickname: str | None = None
    avatar_url: str | None = None
    bio: str | None = None
    profile_title: str | None = None
    profile_tagline: str | None = None
    profile_status: str | None = None
    profile_location: str | None = None
    profile_interests: str | None = None
    profile_layout: str | None = None
    profile_card_style: str | None = None
    profile_accent_color: str | None = None
    profile_cover_url: str | None = None
    ccw_student_oid: str | None = None
    ccw_name: str | None = None
    ccw_avatar_url: str | None = None
    ccw_bio: str | None = None
    ccw_homepage_cover_url: str | None = None
    ccw_following_count: int | None = None
    ccw_follower_count: int | None = None
    ccw_like_count: int | None = None
    ccw_favorite_count: int | None = None
    ccw_comment_count: int | None = None
    ccw_creation_count: int | None = None
    ccw_view_count: int | None = None
    ccw_synced_at: datetime | None = None
    platform_role: PlatformRole = PlatformRole.USER
    status: UserStatus = UserStatus.ACTIVE
    presence_status: PresenceStatus = PresenceStatus.ONLINE
    presence_text: str | None = None
    presence_updated_at: datetime | None = None
    banned_until: datetime | None = None
    ban_reason: str | None = None
    is_bot: bool = False
    bot_owner_id: int | None = None
    created_at: datetime


class UserRead(UserPublic):
    email: EmailStr | None = None
    share_typing: bool = True
    profile_cover_pending_url: str | None = None
    profile_cover_moderation_status: str = "approved"
    updated_at: datetime


class UserUpdate(BaseModel):
    nickname: str | None = Field(default=None, max_length=80)
    avatar_url: str | None = Field(default=None, max_length=500)
    bio: str | None = Field(default=None, max_length=1000)
    profile_title: str | None = Field(default=None, max_length=80)
    profile_tagline: str | None = Field(default=None, max_length=120)
    profile_status: str | None = Field(default=None, max_length=80)
    profile_location: str | None = Field(default=None, max_length=80)
    profile_interests: str | None = Field(default=None, max_length=240)
    profile_layout: str | None = Field(default=None, max_length=20)
    profile_card_style: str | None = Field(default=None, max_length=20)
    profile_accent_color: str | None = Field(default=None, max_length=20)
    profile_cover_url: str | None = Field(default=None, max_length=500)
    # 关掉后别人看不到「我正在输入」
    share_typing: bool | None = None


class UserPresenceUpdate(BaseModel):
    presence_status: Literal[
        PresenceStatus.ONLINE,
        PresenceStatus.AWAY,
        PresenceStatus.BUSY,
        PresenceStatus.DND,
        PresenceStatus.CREATING,
        PresenceStatus.GAMING,
        PresenceStatus.CUSTOM,
    ] = PresenceStatus.ONLINE
    presence_text: str | None = Field(default=None, max_length=40)


class OnlineCountRead(BaseModel):
    online_count: int


class OnlineUsersRead(BaseModel):
    online_count: int
    users: list[UserPublic]


class CcwCreatorRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    nickname: str | None = None
    avatar_url: str | None = None
    bio: str | None = None
    profile_title: str | None = None
    profile_tagline: str | None = None
    profile_cover_url: str | None = None
    ccw_student_oid: str
    ccw_name: str | None = None
    ccw_avatar_url: str | None = None
    ccw_bio: str | None = None
    ccw_homepage_cover_url: str | None = None
    ccw_following_count: int | None = None
    ccw_follower_count: int
    ccw_like_count: int | None = None
    ccw_creation_count: int | None = None
    ccw_view_count: int | None = None
    ccw_synced_at: datetime | None = None


class CcwCreatorPage(BaseModel):
    items: list[CcwCreatorRead]
    total: int
    limit: int
    offset: int
    has_more: bool


class UserOnlineStatusRead(BaseModel):
    user_id: int
    online: bool
    presence_status: PresenceStatus | Literal["offline"] = PresenceStatus.ONLINE
    presence_text: str | None = None
    presence_updated_at: datetime | None = None
