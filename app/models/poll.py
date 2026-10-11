from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

UTC = timezone.utc


def utc_now() -> datetime:
    return datetime.now(UTC)


class Poll(Base):
    """投票。定义（标题、选项）同时写在所属消息的 metadata 里，随消息下发；这里是权威数据。"""

    __tablename__ = "polls"

    id: Mapped[int] = mapped_column(primary_key=True)
    message_id: Mapped[int] = mapped_column(ForeignKey("messages.id", ondelete="CASCADE"), unique=True, index=True)
    conversation_id: Mapped[int] = mapped_column(Integer, index=True)
    creator_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    title: Mapped[str] = mapped_column(String(120))
    # JSON 数组 [{"id": 1, "text": "..."}]，选项 id 创建后不变
    options: Mapped[str] = mapped_column(Text)
    multiple: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    anonymous: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    closes_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_by_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class PollVote(Base):
    __tablename__ = "poll_votes"
    __table_args__ = (
        UniqueConstraint("poll_id", "user_id", "option_id", name="uq_poll_votes_poll_user_option"),
        Index("ix_poll_votes_poll_option", "poll_id", "option_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    poll_id: Mapped[int] = mapped_column(ForeignKey("polls.id", ondelete="CASCADE"))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    option_id: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class GroupEmoji(Base):
    """群自定义表情。图片复用收藏表情的 StickerAsset，不依赖原始消息图片是否还在。"""

    __tablename__ = "group_emojis"
    __table_args__ = (UniqueConstraint("conversation_id", "name", name="uq_group_emojis_conversation_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(24))
    asset_id: Mapped[int] = mapped_column(ForeignKey("sticker_assets.id", ondelete="CASCADE"))
    created_by_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
