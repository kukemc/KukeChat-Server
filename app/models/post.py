from __future__ import annotations

from datetime import datetime, timezone

UTC = timezone.utc

from sqlalchemy import Boolean, DateTime, Enum as SAEnum, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.enums import ModerationStatus, PostNotificationType, PostVisibility


def utc_now() -> datetime:
    return datetime.now(UTC)


class Post(Base):
    __tablename__ = "posts"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    author_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    content: Mapped[str] = mapped_column(Text, default="")
    ccw_creations_json: Mapped[str | None] = mapped_column("ccw_creations", Text, nullable=True)
    visibility: Mapped[PostVisibility] = mapped_column(
        SAEnum(PostVisibility, native_enum=False, values_callable=lambda enum: [item.value for item in enum]),
        default=PostVisibility.PUBLIC,
        index=True,
    )
    image_urls_json: Mapped[str] = mapped_column("image_urls", Text, default="[]")
    repost_of_id: Mapped[int | None] = mapped_column(ForeignKey("posts.id", ondelete="SET NULL"), nullable=True, index=True)
    moderation_status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    pinned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)

    author: Mapped["User"] = relationship()
    repost_of: Mapped["Post | None"] = relationship(remote_side=[id])
    likes: Mapped[list["PostLike"]] = relationship(back_populates="post", cascade="all, delete-orphan")
    comments: Mapped[list["PostComment"]] = relationship(back_populates="post", cascade="all, delete-orphan")


class PostTopic(Base):
    __tablename__ = "post_topics"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    post_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    moderation_status: Mapped[str] = mapped_column(String(20), default=ModerationStatus.APPROVED.value, server_default="approved", index=True)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)

    created_by: Mapped["User | None"] = relationship(foreign_keys=[created_by_id])


class PostTopicLink(Base):
    __tablename__ = "post_topic_links"
    __table_args__ = (UniqueConstraint("post_id", "topic_id", name="uq_post_topic_link"),)

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    post_id: Mapped[int] = mapped_column(ForeignKey("posts.id", ondelete="CASCADE"), index=True)
    topic_id: Mapped[int] = mapped_column(ForeignKey("post_topics.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)

    post: Mapped[Post] = relationship()
    topic: Mapped[PostTopic] = relationship()


class PostMention(Base):
    __tablename__ = "post_mentions"
    __table_args__ = (UniqueConstraint("post_id", "comment_id", "mentioned_user_id", name="uq_post_mention_target"),)

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    post_id: Mapped[int] = mapped_column(ForeignKey("posts.id", ondelete="CASCADE"), index=True)
    comment_id: Mapped[int | None] = mapped_column(ForeignKey("post_comments.id", ondelete="CASCADE"), nullable=True, index=True)
    mentioned_user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)

    post: Mapped[Post] = relationship()
    comment: Mapped["PostComment | None"] = relationship()
    mentioned_user: Mapped["User"] = relationship()


class PostNotification(Base):
    __tablename__ = "post_notifications"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    recipient_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    actor_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    post_id: Mapped[int] = mapped_column(ForeignKey("posts.id", ondelete="CASCADE"), index=True)
    comment_id: Mapped[int | None] = mapped_column(ForeignKey("post_comments.id", ondelete="CASCADE"), nullable=True, index=True)
    type: Mapped[PostNotificationType] = mapped_column(SAEnum(PostNotificationType, native_enum=False, values_callable=lambda enum: [item.value for item in enum]), index=True)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)

    recipient: Mapped["User"] = relationship(foreign_keys=[recipient_id])
    actor: Mapped["User"] = relationship(foreign_keys=[actor_id])
    post: Mapped[Post] = relationship()
    comment: Mapped["PostComment | None"] = relationship()


class PostLike(Base):
    __tablename__ = "post_likes"
    __table_args__ = (UniqueConstraint("post_id", "user_id", name="uq_post_like_user"),)

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    post_id: Mapped[int] = mapped_column(ForeignKey("posts.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)

    post: Mapped[Post] = relationship(back_populates="likes")
    user: Mapped["User"] = relationship()


class PostComment(Base):
    __tablename__ = "post_comments"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    post_id: Mapped[int] = mapped_column(ForeignKey("posts.id", ondelete="CASCADE"), index=True)
    author_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("post_comments.id", ondelete="CASCADE"), nullable=True, index=True)
    content: Mapped[str] = mapped_column(Text)
    moderation_status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)

    post: Mapped[Post] = relationship(back_populates="comments")
    author: Mapped["User"] = relationship(foreign_keys=[author_id])
    parent: Mapped["PostComment | None"] = relationship(remote_side=[id])
    likes: Mapped[list["PostCommentLike"]] = relationship(back_populates="comment", cascade="all, delete-orphan")


class PostCommentLike(Base):
    __tablename__ = "post_comment_likes"
    __table_args__ = (UniqueConstraint("comment_id", "user_id", name="uq_post_comment_like_user"),)

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    comment_id: Mapped[int] = mapped_column(ForeignKey("post_comments.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)

    comment: Mapped[PostComment] = relationship(back_populates="likes")
    user: Mapped["User"] = relationship()
