from __future__ import annotations

from datetime import datetime, timezone

UTC = timezone.utc

from sqlalchemy import Boolean, DateTime, Enum as SAEnum, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.enums import ModerationStatus, PlatformRole, PresenceStatus, UserStatus

# 账号等级的 account_streak_day 用到。单独成行，不去改上面那两行 import
from datetime import date  # noqa: E402
from sqlalchemy import Date  # noqa: E402

from app.core.localtime import is_birthday_on, local_today  # noqa: E402


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
    # 账号等级（docs/account-level.md）。等级和累计活跃天冗余在这里：用户对象出现在消息发送等热路径上，序列化时不能去查别的表。
    # 累计活跃天用整数存十分之一天；连续活跃天数只在 account_streak_day 是今天或昨天时有效。由 services/account_levels.py 维护
    account_level: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    account_active_tenths: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    account_streak_days: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    account_streak_day: Mapped[date | None] = mapped_column(Date, nullable=True)
    # 对外展示的风纪委员层级（trainee / regular / senior）。冗余自 jury_members：只有在任且本人选择公开时有值，
    # 由 jury_points.sync_badge 维护。用户对象出现在消息发送等热路径上，序列化时不能再去查 jury_members
    jury_badge: Mapped[str | None] = mapped_column(String(12), nullable=True)
    # 商店里装备着的装饰（docs/shop.md 2.3）：{"bubble": {"sku": ..., "style": {...}}, "frame": {...}} 的 JSON，没装备为空。
    # 冗余自 shop_items / shop_purchases，由 services/shop.py 维护；和 jury_badge 一样，序列化用户时不再查别的表
    cosmetics: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 资料累计收到的赞（docs/chat-features.md 第 6 节），只增不减。冗余自 profile_likes，由 services/profile_likes.py
    # 在点赞的同一个事务里用 UPDATE ... SET x = x + 1 维护；序列化用户时直接读这一列，不查别的表
    profile_like_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # 心意值（docs/gifts.md）：收到的礼物花了送礼人多少币，只增不减。冗余自 gift_records（= 这个人收到的记录的 amount 之和），
    # 由 services/gifts.py 在送礼的同一个事务里用 UPDATE ... SET charm = charm + ? 维护；序列化用户时直接读这一列
    charm: Mapped[int] = mapped_column(Integer, default=0, server_default="0", index=True)
    # 生日（docs/chat-features.md 第 8 节）。年月日分三列存：公开资料上只给月日，年份要本人选择公开（birthday_show_year）才给。
    # 用户对象出现在消息、成员列表等热路径上，「今天是不是生日」只看这几列现算，不查别的表
    birthday_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    birthday_month: Mapped[int | None] = mapped_column(Integer, nullable=True)
    birthday_day: Mapped[int | None] = mapped_column(Integer, nullable=True)
    birthday_show_year: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    # 已停用：以前「365 天内只能改一次」用的两列（第一次设置的时间、上次改日期的时间）。限制取消后不再读写，列留着免得动迁移
    birthday_set_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    birthday_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 哪一年（UTC+8）的庆生动画已经播过了
    birthday_celebrated_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    registration_ip: Mapped[str | None] = mapped_column(String(45), unique=True, index=True, nullable=True)
    last_login_ip: Mapped[str | None] = mapped_column(String(45), nullable=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_seen_ip: Mapped[str | None] = mapped_column(String(45), nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 通知中心「看过了」的时间点：之后来的通知才算新的、才进角标（动态类通知仍按各自的已读标记）。为空表示从没打开过
    notifications_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
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

    @property
    def birthday(self) -> dict[str, int | None] | None:
        """公开资料上的生日（UserPublic.birthday）：{ month, day, year }，year 只有本人选择公开时才给；没设置为 None。"""
        if not self.birthday_month or not self.birthday_day:
            return None
        return {"month": self.birthday_month, "day": self.birthday_day, "year": self.birthday_year if self.birthday_show_year else None}

    @property
    def is_birthday_today(self) -> bool:
        """今天（UTC+8）是不是这个人的生日。只读这一行上的列。"""
        return is_birthday_on(self.birthday_month, self.birthday_day, local_today())


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
