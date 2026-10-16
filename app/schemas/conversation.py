from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator

from app.models.enums import ConversationType, GroupJoinRequestStatus, MemberRole
from app.schemas.message import MessageRead
from app.schemas.user import UserPublic


class DirectConversationCreate(BaseModel):
    user_id: int = Field(gt=0, validation_alias=AliasChoices("user_id", "friend_id"))
    temporary: bool = False


class GroupConversationCreate(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    member_ids: list[int] = Field(default_factory=list)
    avatar_url: str | None = Field(default=None, max_length=500)

    @field_validator("member_ids")
    @classmethod
    def unique_member_ids(cls, value: list[int]) -> list[int]:
        return list(dict.fromkeys(value))


class MemberInvite(BaseModel):
    user_ids: list[int] = Field(min_length=1, validation_alias=AliasChoices("user_ids", "member_ids"))

    @field_validator("user_ids")
    @classmethod
    def unique_user_ids(cls, value: list[int]) -> list[int]:
        return list(dict.fromkeys(value))


class ConversationAnnouncementUpdate(BaseModel):
    announcement: str | None = Field(max_length=5000)


class ConversationProfileUpdate(BaseModel):
    title: str | None = Field(default=None, max_length=120)
    avatar_url: str | None = Field(default=None, max_length=500)
    description: str | None = Field(default=None, max_length=500)
    category: str | None = Field(default=None, max_length=40)


class LevelTitle(BaseModel):
    min_level: int
    title: str


class GroupSettingsUpdate(BaseModel):
    all_muted: bool | None = None
    slow_mode_seconds: int | None = Field(default=None, ge=0)
    message_rate_limit_per_minute: int | None = Field(default=None, ge=0, le=60)
    join_mode: Literal["approval", "question", "invite_only", "open"] | None = None
    auto_approve: bool | None = None
    join_question: str | None = Field(default=None, max_length=200)
    tasks_enabled: bool | None = None
    task_creation_permission: Literal["members", "admins"] | None = None
    level_titles_enabled: bool | None = None
    # 群文件默认保留多少小时：24 / 168 / 720（docs/group-plus.md 第 2 节；取值在服务层校验，返回 422 invalid_file_ttl）
    file_default_ttl_hours: int | None = None
    # 整体替换；传 [] 恢复默认分档
    level_titles: list[LevelTitle] | None = None


class AnnouncementCreate(BaseModel):
    content: str = Field(min_length=1, max_length=600)
    pinned: bool = False


class AnnouncementUpdate(BaseModel):
    content: str = Field(min_length=1, max_length=600)
    pinned: bool = False


class MemberRoleUpdate(BaseModel):
    role: Literal[MemberRole.ADMIN, MemberRole.MEMBER]


class MemberMuteUpdate(BaseModel):
    muted: bool
    muted_until: datetime | None = None


class MemberSettingsUpdate(BaseModel):
    nickname: str | None = Field(default=None, max_length=120)
    remark: str | None = Field(default=None, max_length=500)
    pinned: bool | None = None
    do_not_disturb: bool | None = None
    message_setting: Literal["notify", "silent", "ignore"] | None = None


class MemberTitleUpdate(BaseModel):
    title: str | None = Field(default=None, max_length=40)


class GroupJoinRequestCreate(BaseModel):
    message: str | None = Field(default=None, max_length=200)
    answer: str | None = Field(default=None, max_length=500)


class GroupJoinRequestDecision(BaseModel):
    decision_note: str | None = Field(default=None, max_length=200)


class ConversationMemberRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    conversation_id: int
    user_id: int
    user: UserPublic
    role: MemberRole
    joined_at: datetime
    last_read_message_id: int | None = None
    last_read_at: datetime | None = None
    muted: bool
    muted_until: datetime | None = None
    nickname: str | None = None
    remark: str | None = None
    title: str | None = None
    pinned: bool
    do_not_disturb: bool
    message_setting: Literal["notify", "silent", "ignore"]
    cleared_before_message_id: int | None = None
    level: int = 1
    level_exp: int = 0
    next_level_exp: int = 50
    activity_score: int = 0
    total_checkins: int = 0
    current_checkin_streak: int = 0
    best_checkin_streak: int = 0
    last_checkin_date: date | None = None
    last_active_at: datetime | None = None


class ConversationMemberPage(BaseModel):
    items: list[ConversationMemberRead]
    total: int
    limit: int
    offset: int
    has_more: bool


class AnnouncementRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    conversation_id: int
    author_id: int | None = None
    author: UserPublic | None = None
    content: str
    pinned: bool
    created_at: datetime
    updated_at: datetime


class ConversationRead(BaseModel):
    id: int
    type: ConversationType
    title: str | None = None
    display_title: str | None = None
    avatar_url: str | None = None
    description: str | None = None
    category: str | None = None
    announcement: str | None = None
    all_muted: bool
    slow_mode_seconds: int
    message_rate_limit_per_minute: int = 0
    join_mode: Literal["approval", "question", "invite_only", "open"]
    auto_approve: bool
    join_question: str | None = None
    tasks_enabled: bool = False
    task_creation_permission: Literal["members", "admins"] = "members"
    # 等级头衔。level_titles 始终是生效的分档（没自定义时是默认分档），按 min_level 升序。
    level_titles_enabled: bool = False
    level_titles: list[LevelTitle] = Field(default_factory=list)
    is_temporary: bool = False
    temporary_initiator_id: int | None = None
    temporary_target_id: int | None = None
    is_friend: bool = False
    created_at: datetime
    updated_at: datetime
    member_count: int
    unread_count: int
    # 我读到的最后一条消息（客户端进会话时据此画「以下是新消息」、跳到第一条未读）
    my_last_read_message_id: int | None = None
    last_message: MessageRead | None = None
    direct_user: UserPublic | None = None
    my_role: MemberRole = MemberRole.MEMBER
    my_nickname: str | None = None
    my_remark: str | None = None
    my_pinned: bool = False
    my_do_not_disturb: bool = False
    my_message_setting: Literal["notify", "silent", "ignore"] = "notify"
    pending_join_request_count: int = 0
    # 这个会话里有我还能领的红包（docs/red-packets.md 第 7 节），会话列表据此显示「[有红包]」
    red_packet_pending: bool = False
    # 我的未读消息里有没有提到我（docs/chat-features.md 第 7 节）：me 有人点名 @我 / all 只有 @全体成员 / null 都没有
    mention_pending: Literal["me", "all"] | None = None
    # 群升级（docs/group-plus.md）：有效档位 free / pro / pro_plus / max；到期时间免费时为 null
    plus_tier: str = "free"
    plus_expires_at: datetime | None = None
    # 永久档位（后台给的；plus_expires_at 是 2999 年的哨兵值）。客户端看到 true 显示「永久」，不显示日期 / 剩余天数
    plus_permanent: bool = False
    # 群文件默认保留多少小时
    file_default_ttl_hours: int = 24
    # 群待办：{ message_id, message_exists, summary, snapshot, set_by, set_at … }，没有为 null
    group_todo: dict | None = None


class GroupDiscoveryRead(BaseModel):
    id: int
    type: ConversationType
    title: str | None = None
    display_title: str | None = None
    avatar_url: str | None = None
    description: str | None = None
    category: str | None = None
    announcement: str | None = None
    join_mode: Literal["approval", "question", "invite_only", "open"]
    auto_approve: bool
    join_question: str | None = None
    created_at: datetime
    updated_at: datetime
    member_count: int
    plus_tier: str = "free"
    plus_expires_at: datetime | None = None
    plus_permanent: bool = False
    joined: bool
    join_request_status: GroupJoinRequestStatus | None = None


class GroupJoinRequestRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    conversation_id: int
    conversation_title: str | None = None
    conversation_display_title: str | None = None
    conversation_avatar_url: str | None = None
    conversation_member_count: int = 0
    requester_id: int
    reviewer_id: int | None = None
    status: GroupJoinRequestStatus
    message: str | None = None
    answer: str | None = None
    decision_note: str | None = None
    created_at: datetime
    updated_at: datetime
    requester: UserPublic | None = None
    reviewer: UserPublic | None = None
