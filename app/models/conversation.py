from __future__ import annotations

from datetime import date, datetime, timezone

UTC = timezone.utc

from sqlalchemy import Boolean, Date, DateTime, Enum as SAEnum, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.core.group_levels import next_level_remaining_exp
from app.models.enums import ConversationType, GroupJoinRequestStatus, MemberRole


def utc_now() -> datetime:
    return datetime.now(UTC)


class Conversation(Base):
    __tablename__ = "conversations"
    # 每人最多一个「我的设备」会话（docs/self-chat.md）。NULL 不参与唯一约束，私聊 / 群聊都是 NULL
    __table_args__ = (Index("uq_conversations_self_owner", "self_owner_id", unique=True),)

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
    # 「正在输入」：auto 只在 200 人以下的群显示 / on 大群也显示 / off 不显示（services/typing.py）
    typing_indicator: Mapped[str] = mapped_column(String(8), default="auto", server_default="auto")
    # 等级头衔：按等级区间显示的称号（1-10 级「萌新」之类），每个群自己决定开不开、怎么分档。
    # level_titles 存 JSON 数组 [{"min_level": 1, "title": "萌新"}, ...]，为空表示用默认分档。
    level_titles_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    level_titles: Mapped[str | None] = mapped_column(Text, nullable=True)
    task_creation_permission: Mapped[str] = mapped_column(String(20), default="members", server_default="members")
    direct_key: Mapped[str | None] = mapped_column(String(80), unique=True, nullable=True, index=True)
    # 「我的设备」会话（type = self）的主人；别的会话为空。不建外键：线上 SQLite 这一列是后加的，本来就加不上外键，
    # 两种库保持一致；用户注销时成员行级联删掉，这一行留着也没人能进来
    self_owner_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 最近一条 @全体成员 的消息（docs/chat-features.md 第 7 节）。不建外键：只拿来和成员的已读位置比大小
    last_mention_all_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 群升级（docs/group-plus.md）：记录的档位和到期时间。有效档位要看到期时间（core/group_plus_tiers.conversation_tier）
    plus_tier: Mapped[str] = mapped_column(String(12), default="free", server_default="free")
    plus_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 群文件空间：最近一次解锁文件的档位，和宽限到期时间（档位到期后 30 天内可读不可传）
    plus_files_tier: Mapped[str | None] = mapped_column(String(12), nullable=True)
    plus_files_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 后台单独给这个群设的人数上限（docs/group-plus.md 9.2）：空 = 跟随档位，0 = 不限，正数 = 就是这个上限。
    # 用 core/group_plus_tiers.member_limit_of() 取，不要直接读
    member_limit_override: Mapped[int | None] = mapped_column(Integer, nullable=True)
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
    # 「清空聊天记录」的水位线：id 不超过它的消息对这个成员不可见。只是一个数，不建外键 —— 那条消息被修剪 / 随发送者
    # 注销级联删掉之后水位线也得留着，否则 ON DELETE SET NULL 会让清空过的旧消息重新冒出来（线上 SQLite 这一列是
    # 后加的，本来就没有外键；PostgreSQL 会严格执行模型里的外键，所以这里不能写）。
    cleared_before_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
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
    # 群公告 v2（docs/group-announcements.md）
    # 图片：JSON 数组，元素是已上传图片的 url
    images_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 置顶时间：置顶的公告按它从新到旧排（可以有多条置顶）
    pinned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 使用弹窗展示：popup_since 之前就在群里的成员进群聊时弹出，直到看过 / 确认
    popup: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    popup_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    require_confirm: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    # 发送给新成员：new_members_since 之后入群的成员会弹出这条。全群最多一条为 true
    send_to_new_members: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    new_members_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    guide_nickname: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    # 正文 / 图片 / 选项最后一次被修改的时间；没改过为空（updated_at 会被置顶之类的小改动刷新，不能拿来显示「已编辑」）
    edited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)

    conversation: Mapped[Conversation] = relationship(back_populates="announcements")
    author: Mapped["User | None"] = relationship(foreign_keys=[author_id])


class GroupAnnouncementReceipt(Base):
    """成员对一条群公告的回执：看过（关掉弹窗 / 打开详情）、确认收到。"""

    __tablename__ = "group_announcement_receipts"
    __table_args__ = (UniqueConstraint("announcement_id", "user_id", name="uq_group_announcement_receipt"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    announcement_id: Mapped[int] = mapped_column(ForeignKey("group_announcements.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


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
