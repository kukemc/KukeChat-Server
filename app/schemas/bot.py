from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.conversation import ConversationMemberRead, ConversationRead
from app.models.enums import MessageType
from app.schemas.user import UserPublic


class BotBase(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    avatar_url: str | None = Field(default=None, max_length=500)
    description: str | None = Field(default=None, max_length=500)
    functions: str | None = Field(default=None, max_length=3000)
    commands: str | None = Field(default=None, max_length=3000)
    is_public: bool = False

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        name = value.strip()
        if not name:
            raise ValueError("Bot name is required")
        return name


class BotCreate(BotBase):
    pass


class BotUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    avatar_url: str | None = Field(default=None, max_length=500)
    description: str | None = Field(default=None, max_length=500)
    functions: str | None = Field(default=None, max_length=3000)
    commands: str | None = Field(default=None, max_length=3000)
    is_public: bool | None = None
    status: Literal["active", "disabled"] | None = None

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        name = value.strip()
        if not name:
            raise ValueError("Bot name is required")
        return name


class BotReviewCreate(BaseModel):
    rating: float = Field(ge=1, le=5)
    content: str | None = Field(default=None, max_length=500)

    @field_validator("rating")
    @classmethod
    def clean_rating(cls, value: float) -> float:
        rounded = round(value * 2) / 2
        if abs(rounded - value) > 1e-6:
            raise ValueError("Rating must use 0.5 increments")
        return rounded

    @field_validator("content")
    @classmethod
    def clean_content(cls, value: str | None) -> str | None:
        if value is None:
            return None
        content = value.strip()
        return content or None


class BotReviewReplyCreate(BaseModel):
    content: str = Field(min_length=1, max_length=500)
    parent_id: int | None = Field(default=None, gt=0)

    @field_validator("content")
    @classmethod
    def clean_content(cls, value: str) -> str:
        content = value.strip()
        if not content:
            raise ValueError("Reply content is required")
        return content


class BotReviewReplyRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    review_id: int
    user_id: int
    parent_id: int | None = None
    parent_author: UserPublic | None = None
    content: str
    moderation_status: str = "approved"
    created_at: datetime
    updated_at: datetime
    user: UserPublic | None = None
    like_count: int = 0
    liked_by_me: bool = False


class BotReviewLikeToggleRead(BaseModel):
    liked: bool
    like_count: int


class BotReviewRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    bot_id: int
    user_id: int
    rating: float
    content: str | None = None
    moderation_status: str = "approved"
    created_at: datetime
    updated_at: datetime
    user: UserPublic | None = None
    like_count: int = 0
    liked_by_me: bool = False
    replies: list[BotReviewReplyRead] = Field(default_factory=list)


class BotRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    owner_id: int
    name: str
    avatar_url: str | None = None
    description: str | None = None
    functions: str | None = None
    commands: str | None = None
    is_public: bool
    status: str
    created_at: datetime
    updated_at: datetime
    user: UserPublic | None = None
    owner: UserPublic | None = None
    online: bool = False
    rating_average: float | None = None
    review_count: int = 0
    install_count: int = 0
    my_review: BotReviewRead | None = None


class BotSquareStatsRead(BaseModel):
    total_bots: int = 0
    online_bots: int = 0
    installed_groups: int = 0
    total_installs: int = 0
    total_reviews: int = 0


class BotSquareRead(BaseModel):
    items: list[BotRead] = Field(default_factory=list)
    total: int = 0
    limit: int = 30
    offset: int = 0
    has_more: bool = False
    stats: BotSquareStatsRead = Field(default_factory=BotSquareStatsRead)


class BotDashboardMetricRead(BaseModel):
    total_sent_messages: int = 0
    total_received_messages: int = 0
    today_sent_messages: int = 0
    today_received_messages: int = 0
    active_installs: int = 0
    disabled_installs: int = 0
    installed_groups: int = 0
    total_members_reached: int = 0
    review_count: int = 0
    rating_average: float | None = None
    reaction_count: int = 0


class BotDashboardTrendPointRead(BaseModel):
    date: str
    sent: int = 0
    received: int = 0


class BotDashboardRatingBucketRead(BaseModel):
    rating: float
    count: int = 0


class BotDashboardInstallationRead(BaseModel):
    conversation_id: int
    title: str
    avatar_url: str | None = None
    enabled: bool = True
    receive_messages: bool = True
    receive_member_events: bool = True
    installed_at: datetime
    updated_at: datetime
    sent_messages: int = 0
    received_messages: int = 0
    member_count: int = 0


class BotDashboardRead(BaseModel):
    bot: BotRead
    metrics: BotDashboardMetricRead = Field(default_factory=BotDashboardMetricRead)
    trend: list[BotDashboardTrendPointRead] = Field(default_factory=list)
    rating_distribution: list[BotDashboardRatingBucketRead] = Field(default_factory=list)
    installations: list[BotDashboardInstallationRead] = Field(default_factory=list)


class BotCreateRead(BaseModel):
    bot: BotRead
    key: str


class BotKeyRead(BaseModel):
    bot_id: int
    key: str
    key_prefix: str


class BotInstallationCreate(BaseModel):
    conversation_id: int = Field(gt=0)
    receive_messages: bool = True
    receive_member_events: bool = True


class BotInstallationUpdate(BaseModel):
    enabled: bool | None = None
    receive_messages: bool | None = None
    receive_member_events: bool | None = None


class BotInstallationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    bot_id: int
    conversation_id: int
    installed_by_id: int | None = None
    enabled: bool
    receive_messages: bool
    receive_member_events: bool
    created_at: datetime
    updated_at: datetime
    bot: BotRead | None = None


class BotRuntimeMeRead(BaseModel):
    bot: BotRead
    conversations: list[ConversationRead]


class BotUserPublicInfoRead(BaseModel):
    user: UserPublic
    online: bool


class BotConversationInfoRead(BaseModel):
    id: int
    title: str | None = None
    display_title: str | None = None
    avatar_url: str | None = None
    announcement: str | None = None
    member_count: int
    bot_enabled: bool


class BotMessageElement(BaseModel):
    type: str | None = Field(default=None, max_length=32)
    name: str | None = Field(default=None, max_length=32)
    attrs: dict[str, Any] | None = None
    content: str | None = Field(default=None, max_length=8000)

    model_config = ConfigDict(extra="allow")


class BotSendMessageCreate(BaseModel):
    type: MessageType = MessageType.TEXT
    content: str = Field(default="", max_length=8000)
    message: str | None = Field(default=None, max_length=8000)
    metadata: dict[str, Any] | None = None
    elements: list[BotMessageElement] | None = Field(default=None, max_length=50)

    @field_validator("type")
    @classmethod
    def validate_sendable_type(cls, value: MessageType) -> MessageType:
        if value in {MessageType.FORWARD_BUNDLE, MessageType.SYSTEM}:
            raise ValueError("This message type cannot be sent from the bot endpoint")
        return value

    @model_validator(mode="after")
    def validate_content_or_elements(self) -> BotSendMessageCreate:
        metadata = self.metadata if isinstance(self.metadata, dict) else {}
        raw_images = metadata.get("images")
        has_images = isinstance(raw_images, list) and any(isinstance(item, str) and item.strip() for item in raw_images)
        has_elements = bool(self.elements)
        has_element_message = bool((self.message or "").strip())
        has_text = bool(self.content.strip())
        if self.type == MessageType.IMAGE and not has_text and not has_elements and not has_element_message:
            raise ValueError("Image message content is required")
        if self.type == MessageType.VOICE and not has_text and not has_elements and not has_element_message:
            raise ValueError("Voice message content is required")
        if self.type == MessageType.TEXT and not has_text and not has_images and not has_elements and not has_element_message:
            raise ValueError("Message content or elements are required")
        return self


class BotDirectMessageCreate(BotSendMessageCreate):
    user_id: int = Field(gt=0)


class BotMessageComponentUpdate(BaseModel):
    label: str | None = Field(default=None, max_length=32)
    variant: Literal["default", "success", "danger", "warning", "primary"] | None = None
    border_color: str | None = None
    text_color: str | None = None
    background_color: str | None = None
    disabled: bool | None = None
    scope: Literal["global", "user"] = "global"
    user_id: int | None = Field(default=None, gt=0)

    @field_validator("label")
    @classmethod
    def clean_label(cls, value: str | None) -> str | None:
        if value is None:
            return None
        label = value.strip()
        return label or None

    @field_validator("border_color", "text_color", "background_color")
    @classmethod
    def clean_color(cls, value: str | None) -> str | None:
        if value is None:
            return None
        color = value.strip()
        if not color:
            return None
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
            raise ValueError("Color must be a #RRGGBB hex value")
        return color.lower()


class BotConversationMembersRead(BaseModel):
    items: list[ConversationMemberRead]
