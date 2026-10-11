from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.enums import ModerationStatus, TeamupProfileStatus, TeamupSkill, TeamupSkillLevel
from app.schemas.user import UserPublic


class TeamupSkillItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    skill: TeamupSkill
    level: TeamupSkillLevel = TeamupSkillLevel.SKILLED


class TeamupProfileSave(BaseModel):
    headline: str = Field(min_length=1, max_length=120)
    intro: str = Field(default="", max_length=2000)
    skills: list[TeamupSkillItem] = Field(default_factory=list, max_length=8)
    looking_for: list[TeamupSkill] = Field(default_factory=list, max_length=8)
    image_urls: list[str] = Field(default_factory=list, max_length=9)
    background_url: str | None = Field(default=None, max_length=500)
    contact_note: str | None = Field(default=None, max_length=200)
    status: TeamupProfileStatus = TeamupProfileStatus.RECRUITING

    @field_validator("headline")
    @classmethod
    def normalize_headline(cls, value: str) -> str:
        headline = value.strip()
        if not headline:
            raise ValueError("Headline is required")
        return headline

    @field_validator("intro")
    @classmethod
    def normalize_intro(cls, value: str) -> str:
        return value.strip()

    @field_validator("contact_note")
    @classmethod
    def normalize_contact_note(cls, value: str | None) -> str | None:
        if value is None:
            return None
        note = value.strip()
        return note or None

    @field_validator("background_url")
    @classmethod
    def normalize_background_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        background = value.strip()
        return background or None

    @field_validator("skills")
    @classmethod
    def dedupe_skills(cls, value: list[TeamupSkillItem]) -> list[TeamupSkillItem]:
        seen: set[TeamupSkill] = set()
        items: list[TeamupSkillItem] = []
        for item in value:
            if item.skill in seen:
                continue
            seen.add(item.skill)
            items.append(item)
        return items[:8]

    @field_validator("looking_for")
    @classmethod
    def dedupe_looking_for(cls, value: list[TeamupSkill]) -> list[TeamupSkill]:
        return list(dict.fromkeys(value))[:8]

    @field_validator("image_urls")
    @classmethod
    def normalize_image_urls(cls, value: list[str]) -> list[str]:
        images: list[str] = []
        seen: set[str] = set()
        for item in value:
            image_url = item.strip()
            if not image_url or image_url in seen:
                continue
            if len(image_url) > 500:
                raise ValueError("Image URL is too long")
            images.append(image_url)
            seen.add(image_url)
        return images[:9]


class TeamupCommentCreate(BaseModel):
    content: str = Field(min_length=1, max_length=500)
    parent_id: int | None = Field(default=None, gt=0)

    @field_validator("content")
    @classmethod
    def normalize_content(cls, value: str) -> str:
        content = value.strip()
        if not content:
            raise ValueError("Comment content is required")
        return content


class TeamupCommentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    profile_id: int
    author_id: int
    author: UserPublic | None = None
    parent_id: int | None = None
    parent_author: UserPublic | None = None
    content: str
    moderation_status: ModerationStatus = ModerationStatus.PENDING
    created_at: datetime
    like_count: int = 0
    liked_by_me: bool = False


class TeamupProfileRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    author: UserPublic | None = None
    event_id: int | None = None
    headline: str
    intro: str
    skills: list[TeamupSkillItem] = Field(default_factory=list)
    looking_for: list[TeamupSkill] = Field(default_factory=list)
    image_urls: list[str] = Field(default_factory=list)
    background_url: str | None = None
    ccw_creations: list[dict[str, object]] = Field(default_factory=list)
    contact_note: str | None = None
    status: TeamupProfileStatus = TeamupProfileStatus.RECRUITING
    moderation_status: ModerationStatus = ModerationStatus.PENDING
    view_count: int = 0
    pinned_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    comment_count: int = 0
    recent_comments: list[TeamupCommentRead] = Field(default_factory=list)
    is_friend: bool = False
    is_self: bool = False


class TeamupEventRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    theme: str = ""
    cover_url: str | None = None
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    is_active: bool = False
    seconds_remaining: int | None = None
    profile_count: int = 0
    recruiting_count: int = 0
    total_profile_count: int = 0
    total_recruiting_count: int = 0


class TeamupStatsRead(BaseModel):
    """组队中心概览，不依赖是否有进行中的赛季。"""

    # 广场当前展示的范围：有进行中的赛季就是这个赛季的名片，没有就是不属于任何赛季的名片
    profile_count: int = 0
    recruiting_count: int = 0
    # 全部赛季加起来
    total_profile_count: int = 0
    total_recruiting_count: int = 0
    total_view_count: int = 0
    total_comment_count: int = 0


class TeamupEventUpsert(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    theme: str = Field(default="", max_length=2000)
    cover_url: str | None = Field(default=None, max_length=500)
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    is_active: bool = False

    @field_validator("title")
    @classmethod
    def normalize_title(cls, value: str) -> str:
        title = value.strip()
        if not title:
            raise ValueError("Title is required")
        return title

    @field_validator("theme")
    @classmethod
    def normalize_theme(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def validate_dates(self) -> TeamupEventUpsert:
        if self.starts_at is not None and self.ends_at is not None and self.ends_at < self.starts_at:
            raise ValueError("Event end time must be after start time")
        return self


class TeamupSkillStat(BaseModel):
    skill: TeamupSkill
    count: int = 0


class TeamupCommentToggleRead(BaseModel):
    liked: bool
    like_count: int
