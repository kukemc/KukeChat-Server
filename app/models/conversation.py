from __future__ import annotations

from datetime import date, datetime, timezone

UTC = timezone.utc

from sqlalchemy import Boolean, Date, DateTime, Enum as SAEnum, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.core.group_levels import next_level_remaining_exp
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
    # 游戏模式：把群聊接进一个 CCW 作品，作品内的玩家可实时收发该群消息。
    # 开启后该群历史消息对所有游玩者可见，因此仅允许公开群开启（见 services/game_mode.py）。
    game_mode_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    game_creation_oid: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    tasks_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    # 等级头衔：按等级区间显示的称号（1-10 级「萌新」之类），每个群自己决定开不开、怎么分档。
    # level_titles 存 JSON 数组 [{"min_level": 1, "title": "萌新"}, ...]，为空表示用默认分档。
    level_titles_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    level_titles: Mapped[str | None] = mapped_column(Text, nullable=True)
    task_creation_permission: Mapped[str] = mapped_column(String(20), default="members", server_default="members")
    direct_key: Mapped[str | None] = mapped_column(String(80), unique=True, nullable=True, index=True)
    # 最近一条 @全体成员 的消息（docs/chat-features.md 第 7 节）。不建外键：只拿来和成员的已读位置比大小
    last_mention_all_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 群升级（docs/group-plus.md）：记录的档位和到期时间。有效档位要看到期时间（core/group_plus_tiers.conversation_tier）
    plus_tier: Mapped[str] = mapped_column(String(12), default="free", server_default="free")
    plus_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 群文件空间：最近一次解锁文件的档位，和宽限到期时间（档位到期后 30 天内可读不可传）
    plus_files_tier: Mapped[str | None] = mapped_column(String(12), nullable=True)
    plus_files_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 群文件默认保留多少小时：24 / 168 / 720
    file_default_ttl_hours: Mapped[int] = mapped_column(Integer, default=24, server_default="24")
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
    # 最近一条点名 @这个成员 的消息（docs/chat-features.md 第 7 节）。比已读位置新就是「有人@我」
    last_mention_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
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
        return next_level_remaining_exp(self.level_exp)


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
