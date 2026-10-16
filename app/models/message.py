from __future__ import annotations

from datetime import date, datetime, timezone

UTC = timezone.utc

from sqlalchemy import Date, DateTime, Enum as SAEnum, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.group_plus import MessageSnapshotMixin
from app.models.enums import MessageType


def utc_now() -> datetime:
    return datetime.now(UTC)


class Message(Base):
    __tablename__ = "messages"

    # 与 rowid 主键重复，不再单独建索引
    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"), index=True)
    sender_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    type: Mapped[MessageType] = mapped_column(SAEnum(MessageType, native_enum=False), default=MessageType.TEXT)
    content: Mapped[str] = mapped_column(Text)
    metadata_json: Mapped[str | None] = mapped_column("metadata", Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
    recalled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    recalled_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    conversation: Mapped["Conversation"] = relationship(back_populates="messages")
    sender: Mapped["User"] = relationship(back_populates="messages", foreign_keys=[sender_id])
    recalled_by: Mapped["User | None"] = relationship(foreign_keys=[recalled_by_id])
    reactions: Mapped[list["MessageReaction"]] = relationship(back_populates="message", cascade="all, delete-orphan")
    visibility_entries: Mapped[list["MessageVisibility"]] = relationship(back_populates="message", cascade="all, delete-orphan")
    bookmarks: Mapped[list["MessageBookmark"]] = relationship(back_populates="message", cascade="all, delete-orphan")


class MessageReaction(Base):
    __tablename__ = "message_reactions"
    __table_args__ = (UniqueConstraint("message_id", "user_id", "emoji", name="uq_message_reaction_user_emoji"),)

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    message_id: Mapped[int] = mapped_column(ForeignKey("messages.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    emoji: Mapped[str] = mapped_column(String(32), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    message: Mapped[Message] = relationship(back_populates="reactions")
    user: Mapped["User"] = relationship(back_populates="message_reactions")


class MessageVisibility(Base):
    __tablename__ = "message_visibilities"
    __table_args__ = (UniqueConstraint("message_id", "user_id", name="uq_message_visibility_user"),)

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    message_id: Mapped[int] = mapped_column(ForeignKey("messages.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    hidden_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    message: Mapped[Message] = relationship(back_populates="visibility_entries")
    user: Mapped["User"] = relationship()


class MessageBookmark(Base):
    __tablename__ = "message_bookmarks"
    __table_args__ = (UniqueConstraint("message_id", "user_id", name="uq_message_bookmark_user"),)

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    message_id: Mapped[int] = mapped_column(ForeignKey("messages.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    message: Mapped[Message] = relationship(back_populates="bookmarks")
    user: Mapped["User"] = relationship()


class FeaturedMessage(MessageSnapshotMixin, Base):
    """群精华（docs/group-plus.md 第 6 节）。设为精华时把消息固化成快照（snap_*），之后消息被修剪、被撤回都不影响。

    message_id 不建外键（原消息被修剪后只是一个不再指向任何行的数字），也可以为空（回填时原消息已经不在了）。
    """

    __tablename__ = "featured_messages"
    __table_args__ = (UniqueConstraint("message_id", name="uq_featured_message"),)

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    message_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"), index=True)
    set_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)

    conversation: Mapped["Conversation"] = relationship()
    set_by: Mapped["User | None"] = relationship(foreign_keys=[set_by_id])


class MentionAllUsage(Base):
    __tablename__ = "mention_all_usages"
    __table_args__ = (UniqueConstraint("user_id", "usage_date", name="uq_mention_all_usage_user_date"),)

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    usage_date: Mapped[date] = mapped_column(Date, index=True)
    count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)

    user: Mapped["User"] = relationship()
