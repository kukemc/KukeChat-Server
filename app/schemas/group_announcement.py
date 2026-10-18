"""群公告 v2 的请求 / 返回结构（docs/group-announcements.md）。"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from app.models.enums import MemberRole
from app.schemas.user import UserPublic

ANNOUNCEMENT_MAX_LENGTH = 600
ANNOUNCEMENT_MAX_IMAGES = 9


def _clean_content(value: str | None) -> str | None:
    if value is None:
        return None
    content = value.strip()
    if not content:
        raise ValueError("Content is required")
    if len(content) > ANNOUNCEMENT_MAX_LENGTH:
        raise ValueError(f"Content must not exceed {ANNOUNCEMENT_MAX_LENGTH} characters")
    return content


def _clean_images(value: list[str] | None) -> list[str] | None:
    if value is None:
        return None
    # 个数超限交给服务层报 too_many_images（带中文提示）；这里只去空、去重
    return list(dict.fromkeys(item.strip() for item in value if isinstance(item, str) and item.strip()))


class GroupAnnouncementCreate(BaseModel):
    # 上限在 validator 里按去掉首尾空白后的长度算，这里放宽一点，免得首尾换行把 600 字的正文挡掉
    content: str = Field(min_length=1, max_length=ANNOUNCEMENT_MAX_LENGTH + 200)
    images: list[str] = Field(default_factory=list, max_length=50)
    pinned: bool = False
    popup: bool = False
    require_confirm: bool = False
    send_to_new_members: bool = False
    guide_nickname: bool = False

    @field_validator("content")
    @classmethod
    def normalize_content(cls, value: str) -> str:
        return _clean_content(value) or ""

    @field_validator("images")
    @classmethod
    def normalize_images(cls, value: list[str]) -> list[str]:
        return _clean_images(value) or []


class GroupAnnouncementUpdate(BaseModel):
    """每个字段都可以省略，省略表示不改（老客户端只传 content + pinned）。"""

    content: str | None = Field(default=None, min_length=1, max_length=ANNOUNCEMENT_MAX_LENGTH + 200)
    images: list[str] | None = Field(default=None, max_length=50)
    pinned: bool | None = None
    popup: bool | None = None
    require_confirm: bool | None = None
    send_to_new_members: bool | None = None
    guide_nickname: bool | None = None

    @field_validator("content")
    @classmethod
    def normalize_content(cls, value: str | None) -> str | None:
        return _clean_content(value)

    @field_validator("images")
    @classmethod
    def normalize_images(cls, value: list[str] | None) -> list[str] | None:
        return _clean_images(value)


class GroupAnnouncementImage(BaseModel):
    url: str
    thumbnail_url: str | None = None


class GroupAnnouncementRead(BaseModel):
    id: int
    conversation_id: int
    author_id: int | None = None
    author: UserPublic | None = None
    author_display_name: str | None = None
    content: str
    images: list[GroupAnnouncementImage] = Field(default_factory=list)
    pinned: bool = False
    pinned_at: datetime | None = None
    popup: bool = False
    require_confirm: bool = False
    send_to_new_members: bool = False
    guide_nickname: bool = False
    created_at: datetime
    updated_at: datetime
    edited_at: datetime | None = None
    confirmed_count: int = 0
    member_count: int = 0
    my_seen: bool = False
    my_confirmed: bool = False


class GroupAnnouncementSeenRead(BaseModel):
    announcement_id: int
    my_seen: bool = True
    my_confirmed: bool = False


class GroupAnnouncementConfirmationItem(BaseModel):
    user: UserPublic
    nickname: str | None = None
    role: MemberRole
    confirmed_at: datetime | None = None


class GroupAnnouncementConfirmationPage(BaseModel):
    announcement_id: int
    status: Literal["confirmed", "unconfirmed"]
    member_count: int
    confirmed_count: int
    unconfirmed_count: int
    items: list[GroupAnnouncementConfirmationItem]
    limit: int
    offset: int
    has_more: bool
