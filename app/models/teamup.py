from __future__ import annotations

from datetime import datetime, timezone

UTC = timezone.utc

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


def utc_now() -> datetime:
    return datetime.now(UTC)


class TeamupEvent(Base):
    __tablename__ = "teamup_events"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    title: Mapped[str] = mapped_column(String(120))
    theme: Mapped[str] = mapped_column(Text, default="")
    cover_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0", index=True)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)

    created_by: Mapped["User | None"] = relationship(foreign_keys=[created_by_id])


class TeamupProfile(Base):
    __tablename__ = "teamup_profiles"
    __table_args__ = (UniqueConstraint("user_id", "event_id", name="uq_teamup_profile_user_event"),)

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    event_id: Mapped[int | None] = mapped_column(ForeignKey("teamup_events.id", ondelete="SET NULL"), nullable=True, index=True)
    headline: Mapped[str] = mapped_column(String(120), default="")
    intro: Mapped[str] = mapped_column(Text, default="")
    skills_json: Mapped[str] = mapped_column("skills", Text, default="[]")
    looking_for_json: Mapped[str] = mapped_column("looking_for", Text, default="[]")
    image_urls_json: Mapped[str] = mapped_column("image_urls", Text, default="[]")
    background_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    ccw_creations_json: Mapped[str | None] = mapped_column("ccw_creations", Text, nullable=True)
    contact_note: Mapped[str | None] = mapped_column(String(200), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="recruiting", index=True)
    moderation_status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    view_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    pinned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)

    user: Mapped["User"] = relationship(foreign_keys=[user_id])
    event: Mapped["TeamupEvent | None"] = relationship(foreign_keys=[event_id])
    comments: Mapped[list["TeamupComment"]] = relationship(back_populates="profile", cascade="all, delete-orphan")


class TeamupComment(Base):
    __tablename__ = "teamup_comments"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    profile_id: Mapped[int] = mapped_column(ForeignKey("teamup_profiles.id", ondelete="CASCADE"), index=True)
    author_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("teamup_comments.id", ondelete="CASCADE"), nullable=True, index=True)
    content: Mapped[str] = mapped_column(Text)
    moderation_status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)

    profile: Mapped[TeamupProfile] = relationship(back_populates="comments")
    author: Mapped["User"] = relationship(foreign_keys=[author_id])
    parent: Mapped["TeamupComment | None"] = relationship(remote_side=[id])
    likes: Mapped[list["TeamupCommentLike"]] = relationship(back_populates="comment", cascade="all, delete-orphan")


class TeamupCommentLike(Base):
    __tablename__ = "teamup_comment_likes"
    __table_args__ = (UniqueConstraint("comment_id", "user_id", name="uq_teamup_comment_like_user"),)

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    comment_id: Mapped[int] = mapped_column(ForeignKey("teamup_comments.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)

    comment: Mapped[TeamupComment] = relationship(back_populates="likes")
    user: Mapped["User"] = relationship(foreign_keys=[user_id])
