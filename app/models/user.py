from __future__ import annotations

from datetime import datetime, timezone

UTC = timezone.utc

from sqlalchemy import Boolean, DateTime, Enum as SAEnum, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.enums import ModerationStatus, PlatformRole, PresenceStatus, UserStatus


def utc_now() -> datetime:
    return datetime.now(UTC)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    username: Mapped[str] = mapped_column(String(50), unique=True, index=True)
    email: Mapped[str | None] = mapped_column(String(255), unique=True, index=True, nullable=True)
    hashed_password: Mapped[str] = mapped_column(String(255))
    nickname: Mapped[str | None] = mapped_column(String(80), nullable=True)
    avatar_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    bio: Mapped[str | None] = mapped_column(Text, nullable=True)
    profile_title: Mapped[str | None] = mapped_column(String(80), nullable=True)
    profile_tagline: Mapped[str | None] = mapped_column(String(120), nullable=True)
    profile_status: Mapped[str | None] = mapped_column(String(80), nullable=True)
    profile_location: Mapped[str | None] = mapped_column(String(80), nullable=True)
    profile_interests: Mapped[str | None] = mapped_column(String(240), nullable=True)
    profile_layout: Mapped[str | None] = mapped_column(String(20), nullable=True)
    profile_card_style: Mapped[str | None] = mapped_column(String(20), nullable=True)
    profile_accent_color: Mapped[str | None] = mapped_column(String(20), nullable=True)
    profile_cover_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    profile_cover_pending_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    profile_cover_moderation_status: Mapped[str] = mapped_column(String(20), default=ModerationStatus.APPROVED.value, server_default=ModerationStatus.APPROVED.value)
    ccw_student_oid: Mapped[str | None] = mapped_column(String(24), unique=True, index=True, nullable=True)
    ccw_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    ccw_avatar_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    ccw_bio: Mapped[str | None] = mapped_column(Text, nullable=True)
    ccw_homepage_cover_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    ccw_following_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ccw_follower_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ccw_like_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ccw_favorite_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ccw_comment_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ccw_creation_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ccw_view_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ccw_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    content_moderation_whitelisted: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    is_bot: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0", index=True)
    bot_owner_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    platform_role: Mapped[PlatformRole] = mapped_column(
        SAEnum(PlatformRole, native_enum=False), default=PlatformRole.USER, server_default="USER"
    )
    status: Mapped[UserStatus] = mapped_column(
        SAEnum(UserStatus, native_enum=False), default=UserStatus.ACTIVE, server_default="ACTIVE"
    )
    banned_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 封禁发生的时刻。没有它就只能拿注册时间给封禁列表排序，而那毫无意义。
    banned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    ban_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 禁言：能登录能看，不能发。到期靠读取时比较时间，不需要后台任务清理。
    muted_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    mute_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 是否让别人看到「对方正在输入」
    share_typing: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    # 别人戳我时用的动作（预设里的一个字）和接在我名字后面的文案
    poke_action: Mapped[str] = mapped_column(String(4), default="戳", server_default="戳")
    poke_suffix: Mapped[str] = mapped_column(String(32), default="", server_default="")
    registration_ip: Mapped[str | None] = mapped_column(String(45), unique=True, index=True, nullable=True)
    last_login_ip: Mapped[str | None] = mapped_column(String(45), nullable=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_seen_ip: Mapped[str | None] = mapped_column(String(45), nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ip_login_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    presence_status: Mapped[PresenceStatus] = mapped_column(
        SAEnum(PresenceStatus, native_enum=False, values_callable=lambda enum: [item.value for item in enum]),
        default=PresenceStatus.ONLINE,
        server_default=PresenceStatus.ONLINE.value,
    )
    presence_text: Mapped[str | None] = mapped_column(String(40), nullable=True)
    presence_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)

    sent_friend_requests: Mapped[list["FriendRequest"]] = relationship(
        back_populates="requester", foreign_keys="FriendRequest.requester_id"
    )
    received_friend_requests: Mapped[list["FriendRequest"]] = relationship(
        back_populates="receiver", foreign_keys="FriendRequest.receiver_id"
    )
    sent_group_join_requests: Mapped[list["GroupJoinRequest"]] = relationship(
        back_populates="requester", foreign_keys="GroupJoinRequest.requester_id"
    )
    reviewed_group_join_requests: Mapped[list["GroupJoinRequest"]] = relationship(
        back_populates="reviewer", foreign_keys="GroupJoinRequest.reviewer_id"
    )
    memberships: Mapped[list["ConversationMember"]] = relationship(back_populates="user", foreign_keys="ConversationMember.user_id")
    messages: Mapped[list["Message"]] = relationship(back_populates="sender", foreign_keys="Message.sender_id")
    message_reactions: Mapped[list["MessageReaction"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    favorite_stickers: Mapped[list["FavoriteSticker"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    submitted_reports: Mapped[list["UserReport"]] = relationship(
        back_populates="reporter", foreign_keys="UserReport.reporter_id"
    )
    reviewed_reports: Mapped[list["UserReport"]] = relationship(
        back_populates="reviewer", foreign_keys="UserReport.reviewer_id"
    )
    bot_owner: Mapped["User | None"] = relationship(remote_side=[id], foreign_keys=[bot_owner_id])


class CcwBindingChallenge(Base):
    __tablename__ = "ccw_binding_challenges"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    code: Mapped[str] = mapped_column(String(9), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(20), default="pending", server_default="pending", index=True)
    subject_oid: Mapped[str] = mapped_column(String(24), index=True)
    matched_student_oid: Mapped[str | None] = mapped_column(String(24), nullable=True)
    matched_comment_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    matched_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    matched_avatar_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped[User] = relationship()


class CcwPasswordChallenge(Base):
    __tablename__ = "ccw_password_challenges"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    code: Mapped[str] = mapped_column(String(9), index=True)
    purpose: Mapped[str] = mapped_column(String(20), index=True)
    status: Mapped[str] = mapped_column(String(20), default="pending", server_default="pending", index=True)
    matched_comment_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped[User] = relationship()


class AuthSession(Base):
    __tablename__ = "auth_sessions"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    session_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    client_ip: Mapped[str | None] = mapped_column(String(45), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped[User] = relationship()
