from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.enums import ModerationStatus, PostNotificationType, PostVisibility
from app.schemas.user import UserPublic


class PostCreate(BaseModel):
    content: str = Field(default="", max_length=2000)
    visibility: PostVisibility = PostVisibility.PUBLIC
    image_urls: list[str] = Field(default_factory=list, max_length=9)
    repost_of_id: int | None = Field(default=None, gt=0)
    mention_user_ids: list[int] = Field(default_factory=list, max_length=20)
    topic_names: list[str] = Field(default_factory=list, max_length=10)

    @field_validator("content")
    @classmethod
    def normalize_content(cls, value: str) -> str:
        return value.strip()

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

    @field_validator("mention_user_ids")
    @classmethod
    def normalize_mention_user_ids(cls, value: list[int]) -> list[int]:
        return list(dict.fromkeys(user_id for user_id in value if user_id > 0))[:20]

    @field_validator("topic_names")
    @classmethod
    def normalize_topic_names(cls, value: list[str]) -> list[str]:
        topics: list[str] = []
        seen: set[str] = set()
        for item in value:
            topic = item.strip().lstrip("#").strip()
            if not topic or topic in seen:
                continue
            topics.append(topic[:40])
            seen.add(topic)
        return topics[:10]

    @model_validator(mode="after")
    def validate_content_or_images(self) -> PostCreate:
        if not self.content and not self.image_urls and self.repost_of_id is None:
            raise ValueError("Post content or images are required")
        return self


class PostCommentCreate(BaseModel):
    content: str = Field(min_length=1, max_length=500)
    parent_id: int | None = Field(default=None, gt=0)
    mention_user_ids: list[int] = Field(default_factory=list, max_length=20)

    @field_validator("content")
    @classmethod
    def normalize_content(cls, value: str) -> str:
        content = value.strip()
        if not content:
            raise ValueError("Comment content is required")
        return content

    @field_validator("mention_user_ids")
    @classmethod
    def normalize_mention_user_ids(cls, value: list[int]) -> list[int]:
        return list(dict.fromkeys(user_id for user_id in value if user_id > 0))[:20]


class PostUpdate(BaseModel):
    content: str | None = Field(default=None, max_length=2000)
    visibility: PostVisibility | None = None
    image_urls: list[str] | None = Field(default=None, max_length=9)
    mention_user_ids: list[int] | None = Field(default=None, max_length=20)
    topic_names: list[str] | None = Field(default=None, max_length=10)

    @field_validator("content")
    @classmethod
    def normalize_content(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None

    @field_validator("image_urls")
    @classmethod
    def normalize_image_urls(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        return PostCreate.normalize_image_urls(value)

    @field_validator("mention_user_ids")
    @classmethod
    def normalize_mention_user_ids(cls, value: list[int] | None) -> list[int] | None:
        return PostCreate.normalize_mention_user_ids(value) if value is not None else None

    @field_validator("topic_names")
    @classmethod
    def normalize_topic_names(cls, value: list[str] | None) -> list[str] | None:
        return PostCreate.normalize_topic_names(value) if value is not None else None


class PostCommentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    post_id: int
    author_id: int
    author: UserPublic | None = None
    parent_id: int | None = None
    parent_author: UserPublic | None = None
    content: str
    moderation_status: ModerationStatus = ModerationStatus.PENDING
    created_at: datetime
    like_count: int = 0
    liked_by_me: bool = False


class PostLikeRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    post_id: int
    user_id: int
    user: UserPublic | None = None
    created_at: datetime


class PostTopicRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    post_count: int = 0
    moderation_status: ModerationStatus = ModerationStatus.APPROVED


class PostReferenceRead(BaseModel):
    id: int
    author_id: int
    author: UserPublic | None = None
    content: str
    ccw_creations: list[dict[str, object]] = Field(default_factory=list)
    visibility: PostVisibility
    moderation_status: ModerationStatus = ModerationStatus.PENDING
    image_urls: list[str] = Field(default_factory=list)
    pinned_at: datetime | None = None
    topics: list[PostTopicRead] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class PostRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    author_id: int
    author: UserPublic | None = None
    content: str
    ccw_creations: list[dict[str, object]] = Field(default_factory=list)
    visibility: PostVisibility
    moderation_status: ModerationStatus = ModerationStatus.PENDING
    image_urls: list[str] = Field(default_factory=list)
    repost_of_id: int | None = None
    repost_of: PostReferenceRead | None = None
    pinned_at: datetime | None = None
    topics: list[PostTopicRead] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime
    like_count: int = 0
    comment_count: int = 0
    liked_by_me: bool = False
    recent_likes: list[UserPublic] = Field(default_factory=list)
    recent_comments: list[PostCommentRead] = Field(default_factory=list)


class AdminPostRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    author_id: int
    author: UserPublic | None = None
    content: str
    visibility: PostVisibility
    moderation_status: ModerationStatus = ModerationStatus.PENDING
    image_urls: list[str] = Field(default_factory=list)
    repost_of_id: int | None = None
    created_at: datetime
    updated_at: datetime
    like_count: int = 0
    comment_count: int = 0


class PostStatsRead(BaseModel):
    post_count: int = 0
    like_count: int = 0
    comment_count: int = 0


class PostLikeToggleRead(BaseModel):
    liked: bool
    like_count: int


class PostNotificationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    recipient_id: int
    actor_id: int
    actor: UserPublic | None = None
    post_id: int
    comment_id: int | None = None
    type: PostNotificationType
    read_at: datetime | None = None
    created_at: datetime
    post: PostReferenceRead | None = None
    comment: PostCommentRead | None = None


class PostNotificationListRead(BaseModel):
    items: list[PostNotificationRead] = Field(default_factory=list)
    unread_count: int = 0
