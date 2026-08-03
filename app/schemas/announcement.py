from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.user import UserPublic


class AnnouncementRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    content: str
    cover_url: str | None = None
    is_active: bool = True
    pinned: bool = False
    author: UserPublic | None = None
    created_at: datetime
    updated_at: datetime


class AnnouncementCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=20000)
    cover_url: str | None = Field(default=None, max_length=500)
    is_active: bool = True
    pinned: bool = False

    @field_validator("title")
    @classmethod
    def normalize_title(cls, value: str) -> str:
        title = value.strip()
        if not title:
            raise ValueError("Title is required")
        return title

    @field_validator("content")
    @classmethod
    def normalize_content(cls, value: str) -> str:
        content = value.strip()
        if not content:
            raise ValueError("Content is required")
        return content

    @field_validator("cover_url")
    @classmethod
    def normalize_cover(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cover = value.strip()
        return cover or None


class AnnouncementUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    content: str | None = Field(default=None, min_length=1, max_length=20000)
    cover_url: str | None = Field(default=None, max_length=500)
    is_active: bool | None = None
    pinned: bool | None = None
    clear_cover: bool = False
