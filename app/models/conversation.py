from __future__ import annotations

from datetime import date, datetime, timezone

UTC = timezone.utc

from sqlalchemy import Boolean, Date, DateTime, Enum as SAEnum, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.enums import ConversationType, GroupJoinRequestStatus, MemberRole


def utc_now() -> datetime:
    return datetime.now(UTC)


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    type: Mapped[ConversationType] = mapped_column(SAEnum(ConversationType, native_enum=False), index=True)
    title: Mapped[str | None] = mapped_column(String(120), nullable=True)
    avatar_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    category: Mapped[str | None] = mapped_column(String(40), nullable=True)
    announcement: Mapped[str | None] = mapped_column(Text, nullable=True)
    all_muted: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    slow_mode_seconds: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    message_rate_limit_per_minute: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    join_mode: Mapped[str] = mapped_column(String(20), default="approval", server_default="approval")
    auto_approve: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    join_question: Mapped[str | None] = mapped_column(String(200), nullable=True)
    tasks_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    task_creation_permission: Mapped[str] = mapped_column(String(20), default="members", server_default="members")
    direct_key: Mapped[str | None] = mapped_column(String(80), unique=True, nullable=True, index=True)
    is_temporary: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    temporary_initiator_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    temporary_target_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)

    created_by: Mapped["User | None"] = relationship(foreign_keys=[created_by_id])
    temporary_initiator: Mapped["User | None"] = relationship(foreign_keys=[temporary_initiator_id])
    temporary_target: Mapped["User | None"] = relationship(foreign_keys=[temporary_target_id])
    members: Mapped[list["ConversationMember"]] = relationship(
        back_populates="conversation", cascade="all, delete-orphan"
    )
    messages: Mapped[list["Message"]] = relationship(back_populates="conversation", cascade="all, delete-orphan")
    announcements: Mapped[list["GroupAnnouncement"]] = relationship(
        back_populates="conversation", cascade="all, delete-orphan"
    )
    join_requests: Mapped[list["GroupJoinRequest"]] = relationship(
        back_populates="conversation", cascade="all, delete-orphan"
    )


class ConversationMember(Base):
    __tablename__ = "conversation_members"
    __table_args__ = (UniqueConstraint("conversation_id", "user_id", name="uq_conversation_member"),)

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    role: Mapped[MemberRole] = mapped_column(SAEnum(MemberRole, native_enum=False), default=MemberRole.MEMBER)
    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    last_read_message_id: Mapped[int | None] = mapped_column(ForeignKey("messages.id", ondelete="SET NULL"), nullable=True)
    last_read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    muted: Mapped[bool] = mapped_column(Boolean, default=False)
    muted_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    nickname: Mapped[str | None] = mapped_column(String(120), nullable=True)
    remark: Mapped[str | None] = mapped_column(String(500), nullable=True)
    pinned: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    do_not_disturb: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    message_setting: Mapped[str] = mapped_column(String(20), default="notify", server_default="notify")
    cleared_before_message_id: Mapped[int | None] = mapped_column(ForeignKey("messages.id", ondelete="SET NULL"), nullable=True)
    title: Mapped[str | None] = mapped_column(String(40), nullable=True)
    level: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    level_exp: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    activity_score: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    total_checkins: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    current_checkin_streak: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    best_checkin_streak: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_checkin_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    last_active_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    conversation: Mapped[Conversation] = relationship(back_populates="members")
    user: Mapped["User"] = relationship(back_populates="memberships", foreign_keys=[user_id])

    @property
    def next_level_exp(self) -> int:
        level = 1
        remaining = max(0, self.level_exp)
        while level < 100:
            required = 50 + max(0, level - 1) * max(0, level - 1) * 25
            if remaining < required:
                return required - remaining
            remaining -= required
            level += 1
        return 0


class TemporaryConversationBlock(Base):
    __tablename__ = "temporary_conversation_blocks"
    __table_args__ = (UniqueConstraint("blocker_id", "blocked_user_id", name="uq_temporary_conversation_block_pair"),)

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    blocker_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    blocked_user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)

    blocker: Mapped["User"] = relationship(foreign_keys=[blocker_id])
    blocked_user: Mapped["User"] = relationship(foreign_keys=[blocked_user_id])


class GroupAnnouncement(Base):
    __tablename__ = "group_announcements"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"), index=True)
    author_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    content: Mapped[str] = mapped_column(Text)
    pinned: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)

    conversation: Mapped[Conversation] = relationship(back_populates="announcements")
    author: Mapped["User | None"] = relationship(foreign_keys=[author_id])


class GroupJoinRequest(Base):
    __tablename__ = "group_join_requests"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"), index=True)
    requester_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    reviewer_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    status: Mapped[GroupJoinRequestStatus] = mapped_column(
        SAEnum(GroupJoinRequestStatus, native_enum=False),
        default=GroupJoinRequestStatus.PENDING,
        index=True,
    )
    message: Mapped[str | None] = mapped_column(String(200), nullable=True)
    answer: Mapped[str | None] = mapped_column(String(500), nullable=True)
    decision_note: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)

    conversation: Mapped[Conversation] = relationship(back_populates="join_requests")
    requester: Mapped["User"] = relationship(foreign_keys=[requester_id], back_populates="sent_group_join_requests")
    reviewer: Mapped["User | None"] = relationship(foreign_keys=[reviewer_id], back_populates="reviewed_group_join_requests")
