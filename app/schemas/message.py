from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from app.models.enums import MessageType
from app.schemas.user import UserPublic
from app.core.emoji import validate_reaction_emoji


class MessageCreate(BaseModel):
    type: MessageType = MessageType.TEXT
    content: str = Field(default="", max_length=2000)
    metadata: dict[str, Any] | None = None

    @field_validator("type")
    @classmethod
    def validate_sendable_type(cls, value: MessageType) -> MessageType:
        if value in {MessageType.FORWARD_BUNDLE, MessageType.SYSTEM, MessageType.POLL}:
            raise ValueError("This message type cannot be sent from the public message endpoint")
        return value

    @model_validator(mode="after")
    def validate_content_or_images(self) -> MessageCreate:
        metadata = self.metadata if isinstance(self.metadata, dict) else {}
        raw_images = metadata.get("images")
        has_images = isinstance(raw_images, list) and any(isinstance(item, str) and item.strip() for item in raw_images)
        has_text = bool(self.content.strip())
        if self.type == MessageType.IMAGE and not has_text:
            raise ValueError("Image message content is required")
        if self.type == MessageType.VOICE and not has_text:
            raise ValueError("Voice message content is required")
        if self.type == MessageType.TEXT and not has_text and not has_images:
            raise ValueError("Message content is required")
        return self


class ForwardBundleCreate(BaseModel):
    source_conversation_id: int = Field(gt=0)
    message_ids: list[int] = Field(min_length=1, max_length=100)
    note: str | None = Field(default=None, max_length=500)

    @field_validator("message_ids")
    @classmethod
    def normalize_message_ids(cls, value: list[int]) -> list[int]:
        if any(message_id <= 0 for message_id in value):
            raise ValueError("Message ids must be positive")
        return list(dict.fromkeys(value))


class MessageBookmarkToggleRead(BaseModel):
    bookmarked: bool


class MessageFeatureToggleRead(BaseModel):
    featured: bool


class MessageReactionCreate(BaseModel):
    emoji: str = Field(min_length=1, max_length=32)

    @field_validator("emoji")
    @classmethod
    def normalize_emoji(cls, value: str) -> str:
        emoji = value.strip()
        if not emoji:
            raise ValueError("Emoji is required")
        validate_reaction_emoji(emoji)
        return emoji


class MessageInteractionCreate(BaseModel):
    kind: Literal["button", "link"] = "button"
    action: Literal["callback", "input", "open"] = "callback"
    component_id: str | None = Field(default=None, max_length=64)
    action_id: str | None = Field(default=None, max_length=128)
    value: str | None = Field(default=None, max_length=500)
    href: str | None = Field(default=None, max_length=500)
    label: str | None = Field(default=None, max_length=80)
    element_index: int | None = Field(default=None, ge=0, le=200)

    @field_validator("component_id", "action_id", "value", "href", "label")
    @classmethod
    def clean_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        return text or None

    @model_validator(mode="after")
    def validate_target(self) -> MessageInteractionCreate:
        if self.action == "callback" and not self.action_id:
            raise ValueError("Callback interaction action_id is required")
        if self.action == "input" and not self.value:
            raise ValueError("Input interaction value is required")
        if self.action == "open" and not self.href:
            raise ValueError("Open interaction href is required")
        return self


class MessageReactionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    message_id: int
    user_id: int
    emoji: str
    created_at: datetime
    user: UserPublic | None = None


class MessageReactionSummary(BaseModel):
    emoji: str
    count: int
    reacted_by_me: bool
    users: list[UserPublic] = Field(default_factory=list)


class PokeRequest(BaseModel):
    # 发起人临时换一个动作；不传就用对方资料里设置的
    action: str | None = Field(default=None, max_length=16)


class MessageRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    conversation_id: int
    sender_id: int
    sender: UserPublic
    sender_display_name: str | None = None
    sender_title: str | None = None
    sender_level: int | None = None
    type: MessageType
    content: str
    metadata: dict[str, Any] | None = None
    created_at: datetime
    recalled_at: datetime | None = None
    recalled_by_id: int | None = None
    reactions: list[MessageReactionSummary] = Field(default_factory=list)
    bookmarked_by_me: bool = False
    featured: bool = False
    conversation: object | None = None


class MessageSearchResult(BaseModel):
    message: MessageRead
    match_type: str
    snippet: str | None = None


class MessageSearchResponse(BaseModel):
    items: list[MessageSearchResult]
    total: int
    limit: int
    has_more: bool
    next_before_id: int | None = None


class FeaturedMessageRead(BaseModel):
    id: int
    conversation_id: int
    message_id: int
    set_by_id: int | None = None
    set_by: UserPublic | None = None
    created_at: datetime
    message: MessageRead


class ReadConversationRequest(BaseModel):
    last_read_message_id: int | None = Field(default=None, gt=0)


class ReadReceipt(BaseModel):
    conversation_id: int
    user_id: int
    last_read_message_id: int | None
    last_read_at: datetime
