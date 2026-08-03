from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.enums import ModerationStatus

UTC = timezone.utc


def utc_now() -> datetime:
    return datetime.now(UTC)


class Bot(Base):
    __tablename__ = "bots"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), unique=True, index=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(80), index=True)
    avatar_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    functions: Mapped[str | None] = mapped_column(Text, nullable=True)
    commands: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_public: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0", index=True)
    status: Mapped[str] = mapped_column(String(20), default="active", server_default="active", index=True)
    key_hash: Mapped[str] = mapped_column(String(128))
    key_prefix: Mapped[str] = mapped_column(String(32), index=True)
    last_key_rotated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)

    user: Mapped["User"] = relationship(foreign_keys=[user_id])
    owner: Mapped["User"] = relationship(foreign_keys=[owner_id])
    installations: Mapped[list["BotInstallation"]] = relationship(back_populates="bot", cascade="all, delete-orphan")
    reviews: Mapped[list["BotReview"]] = relationship(back_populates="bot", cascade="all, delete-orphan")


class BotInstallation(Base):
    __tablename__ = "bot_installations"
    __table_args__ = (UniqueConstraint("bot_id", "conversation_id", name="uq_bot_installation_conversation"),)

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id", ondelete="CASCADE"), index=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"), index=True)
    installed_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1", index=True)
    receive_messages: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    receive_member_events: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)

    bot: Mapped[Bot] = relationship(back_populates="installations")
    conversation: Mapped["Conversation"] = relationship()
    installed_by: Mapped["User | None"] = relationship(foreign_keys=[installed_by_id])


class BotReview(Base):
    __tablename__ = "bot_reviews"
    __table_args__ = (UniqueConstraint("bot_id", "user_id", name="uq_bot_review_user"),)

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    rating: Mapped[float] = mapped_column(Float)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    moderation_status: Mapped[str] = mapped_column(String(20), default=ModerationStatus.APPROVED.value, server_default=ModerationStatus.APPROVED.value, index=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)

    bot: Mapped[Bot] = relationship(back_populates="reviews")
    user: Mapped["User"] = relationship(foreign_keys=[user_id])
    replies: Mapped[list["BotReviewReply"]] = relationship(back_populates="review", cascade="all, delete-orphan")


class BotReviewReply(Base):
    __tablename__ = "bot_review_replies"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    review_id: Mapped[int] = mapped_column(ForeignKey("bot_reviews.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("bot_review_replies.id", ondelete="CASCADE"), nullable=True, index=True)
    content: Mapped[str] = mapped_column(Text)
    moderation_status: Mapped[str] = mapped_column(String(20), default=ModerationStatus.APPROVED.value, server_default=ModerationStatus.APPROVED.value, index=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)

    review: Mapped[BotReview] = relationship(back_populates="replies")
    user: Mapped["User"] = relationship(foreign_keys=[user_id])
    parent: Mapped["BotReviewReply | None"] = relationship(remote_side=[id])


class BotReviewLike(Base):
    __tablename__ = "bot_review_likes"
    __table_args__ = (
        UniqueConstraint("review_id", "user_id", name="uq_bot_review_like_user"),
        UniqueConstraint("reply_id", "user_id", name="uq_bot_review_reply_like_user"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    review_id: Mapped[int | None] = mapped_column(ForeignKey("bot_reviews.id", ondelete="CASCADE"), nullable=True, index=True)
    reply_id: Mapped[int | None] = mapped_column(ForeignKey("bot_review_replies.id", ondelete="CASCADE"), nullable=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)

    review: Mapped["BotReview | None"] = relationship(foreign_keys=[review_id])
    reply: Mapped["BotReviewReply | None"] = relationship(foreign_keys=[reply_id])
    user: Mapped["User"] = relationship(foreign_keys=[user_id])
