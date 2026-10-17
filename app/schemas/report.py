from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator

from app.models.enums import ModerationAction, ModerationSource, PasswordResetStatus, ReportStatus, ReportTargetType, UserStatus
from app.schemas.message import MessageRead
from app.schemas.post import AdminPostRead
from app.schemas.user import UserPublic


class AdminUserRead(UserPublic):
    email: str | None = None
    updated_at: datetime
    content_moderation_whitelisted: bool = False
    registration_ip: str | None = None
    last_login_ip: str | None = None
    last_login_at: datetime | None = None
    last_seen_ip: str | None = None
    last_seen_at: datetime | None = None
    message_count: int = 0
    report_count: int = 0
    reports_submitted_count: int = 0
    reports_against_count: int = 0
    moderation_flagged_count: int = 0
    moderation_block_count: int = 0
    moderation_recalled_count: int = 0
    moderation_banned_count: int = 0
    moderation_latest_reason: str | None = None
    moderation_latest_at: datetime | None = None
    active_session_count: int = 0
    last_session_ip: str | None = None
    last_session_at: datetime | None = None


class BanEvidence(BaseModel):
    """触发这次封禁的那条内容。

    封禁列表原先只有一句自由文本的 ban_reason，看不出到底因为哪条消息被封 ——
    要判断封得冤不冤，就得翻到审核记录里去比对。这里把证据直接带上来。
    """

    record_id: int
    message_id: int | None = None
    conversation_id: int | None = None
    conversation_title: str | None = None
    content_type: str
    content_text: str | None = None
    categories: list[str] = []
    severity: int = 0
    confidence: float | None = None
    reason: str | None = None
    decision: str
    created_at: datetime


class AdminBanRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    nickname: str | None = None
    avatar_url: str | None = None
    email: str | None = None
    created_at: datetime

    status: UserStatus
    banned_at: datetime | None = None
    banned_until: datetime | None = None
    ban_reason: str | None = None
    # banned_until 为空即永久，单独给个布尔量省得前端到处判空
    permanent: bool = False
    # 距离解封还剩多少秒；永久封禁为 None，已到期为 0
    expires_in_seconds: int | None = None

    ban_source: Literal["auto", "manual", "report", "unknown"] = "unknown"
    operator_id: int | None = None
    operator_name: str | None = None
    report_id: int | None = None
    evidence: BanEvidence | None = None

    # 这个号历史上被自动封过几次，用来一眼看出惯犯
    ban_count: int = 0
    message_count: int = 0


class AdminBanPage(BaseModel):
    items: list[AdminBanRead]
    total: int


class ReportCreate(BaseModel):
    target_type: ReportTargetType
    target_id: int = Field(gt=0)
    reason: str = Field(min_length=1, max_length=120)
    details: str | None = Field(default=None, max_length=2000)
    description: str | None = Field(default=None, max_length=2000)
    conversation_id: int | None = Field(default=None, gt=0)
    message_id: int | None = Field(default=None, gt=0)
    reported_user_id: int | None = Field(default=None, gt=0)
    attachments: list[str] = Field(default_factory=list, max_length=6)
    # 举报用户时选中的证据消息：必须是被举报人在 conversation_id 这个会话里发的
    evidence_message_ids: list[int] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def normalize_details_and_attachments(self) -> ReportCreate:
        if self.details is None and self.description is not None:
            self.details = self.description
        self.attachments = [item.strip() for item in self.attachments if isinstance(item, str) and item.strip()][:6]
        return self


class ReportConversationContext(BaseModel):
    id: int
    type: str
    title: str | None = None
    display_title: str | None = None
    avatar_url: str | None = None
    description: str | None = None
    category: str | None = None
    member_count: int | None = None
    owner_id: int | None = None
    created_by_id: int | None = None


class ReportTargetContext(BaseModel):
    message: MessageRead | None = None
    post: AdminPostRead | None = None
    user: UserPublic | None = None
    conversation: ReportConversationContext | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class ReportRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    reporter_id: int
    target_type: ReportTargetType
    target_id: int
    reason: str
    details: str | None = None
    description: str | None = None
    attachments: list[str] = Field(default_factory=list)
    conversation_id: int | None = None
    message_id: int | None = None
    reported_user_id: int | None = None
    evidence_hash: str | None = None
    status: ReportStatus
    reviewer_id: int | None = None
    review_note: str | None = None
    action: str | None = None
    created_at: datetime
    reviewed_at: datetime | None = None
    case_id: int | None = None
    # 给举报人看的处理结果，未结案时为空
    result_title: str | None = None
    result_text: str | None = None
    reporter: UserPublic | None = None
    reviewer: UserPublic | None = None
    reported_user: UserPublic | None = None
    target: ReportTargetContext | None = None
    snapshot: dict[str, Any] | None = None
    # 举报人复议（docs/jury-v2.md 3.7）：现在能不能申请（我的举报、委员 / 合议认定不违规、定案后期限内、这个案件还没人申请过）
    can_reconsider: bool = False
    # none / pending / upheld（维持原结论）/ overturned（改判为违规）
    reconsider_status: str = "none"


class ReportReconsider(BaseModel):
    # 长度在服务层校验：太短要返回 text_required，而不是通用的 422
    text: str = Field(default="", max_length=2000)


class ReportReview(BaseModel):
    status: Literal[ReportStatus.REVIEWED, ReportStatus.REJECTED] = ReportStatus.REVIEWED
    review_note: str | None = Field(default=None, max_length=2000)
    action: Literal["none", "recall_message", "disable_user", "delete_post"] = "none"
    delete_content: bool = False
    disable_user: bool = False
    ban_until: datetime | None = None
    ban_duration_hours: int | None = Field(default=None, ge=1, le=24 * 365)
    # 认定违规：给出类别（等级缺省取类别默认值），处罚由统一阶梯计算。
    # 同时带 disable_user 时，以管理员指定的封禁时长为准。
    violation_category: str | None = Field(default=None, max_length=40)
    violation_level: int | None = Field(default=None, ge=0, le=4)

    @model_validator(mode="after")
    def validate_action(self) -> ReportReview:
        if self.status == ReportStatus.REJECTED and (
            self.action != "none" or self.delete_content or self.disable_user or self.violation_category
        ):
            raise ValueError("Rejected reports cannot perform actions")
        return self


class AdminMessageRecall(BaseModel):
    reason: str | None = Field(default=None, max_length=500)


class AdminMessageContextRead(BaseModel):
    conversation: AdminConversationRead | None = None
    target_message_id: int
    reported_user: UserPublic | None = None
    messages: list[MessageRead]


class AdminUserStatusUpdate(BaseModel):
    status: Literal["active", "disabled"]
    reason: str | None = Field(default=None, max_length=500)
    banned_until: datetime | None = None
    ban_duration_hours: int | None = Field(default=None, ge=1, le=24 * 365)


class AdminUserCreate(BaseModel):
    username: str = Field(min_length=3, max_length=50, pattern=r"^[A-Za-z0-9_.-]+$")
    email: str | None = Field(default=None, max_length=255)
    password: str = Field(min_length=6, max_length=128)
    nickname: str | None = Field(default=None, max_length=80)


class AdminUserRoleUpdate(BaseModel):
    platform_role: Literal["user", "admin"]
    reason: str | None = Field(default=None, max_length=500)


class AdminUserModerationWhitelistUpdate(BaseModel):
    content_moderation_whitelisted: bool
    reason: str | None = Field(default=None, max_length=500)


class AdminUserPasswordReset(BaseModel):
    new_password: str = Field(min_length=6, max_length=128)
    reason: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def normalize_reason(self) -> AdminUserPasswordReset:
        self.reason = self.reason.strip()
        if not self.reason:
            raise ValueError("Password reset reason is required")
        return self


class AdminConversationUpdate(BaseModel):
    title: str | None = Field(default=None, max_length=120)
    description: str | None = Field(default=None, max_length=500)
    category: str | None = Field(default=None, max_length=40)
    announcement: str | None = Field(default=None, max_length=5000)
    all_muted: bool | None = None
    slow_mode_seconds: int | None = Field(default=None, ge=0)
    message_rate_limit_per_minute: int | None = Field(default=None, ge=0, le=60)
    join_mode: Literal["approval", "question", "invite_only", "open"] | None = None
    auto_approve: bool | None = None


class AdminConversationRead(BaseModel):
    id: int
    type: str
    title: str | None = None
    display_title: str | None = None
    avatar_url: str | None = None
    description: str | None = None
    category: str | None = None
    announcement: str | None = None
    all_muted: bool
    slow_mode_seconds: int
    message_rate_limit_per_minute: int = 0
    join_mode: str
    auto_approve: bool
    created_by_id: int | None = None
    owner_id: int | None = None
    owner: UserPublic | None = None
    pending_join_request_count: int = 0
    created_at: datetime
    updated_at: datetime
    member_count: int
    participants: list[UserPublic] = Field(default_factory=list)
    message_count: int = 0
    last_message_at: datetime | None = None


class AdminConversationMemberPage(BaseModel):
    items: list[UserPublic]
    total: int
    limit: int
    offset: int
    has_more: bool


class AdminStats(BaseModel):
    users_total: int
    users_active: int
    users_banned: int = 0
    conversations_total: int
    direct_conversations: int
    group_conversations: int
    messages_total: int
    private_messages_total: int
    reports_pending: int
    audit_actions_today: int
    appeals_pending: int = 0
    penalty_reviews_pending: int = 0
    cases_admin_pending: int = 0
    cases_jury_pending: int = 0
    jury_applications_pending: int = 0


class AdminAuditLogRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    actor_id: int | None = None
    actor: UserPublic | None = None
    action: str
    target_type: str | None = None
    target_id: int | None = None
    details: str | None = None
    created_at: datetime


class ContentModerationRecordRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    source: ModerationSource
    message_id: int | None = None
    post_id: int | None = None
    post_comment_id: int | None = None
    report_id: int | None = None
    conversation_id: int | None = None
    user_id: int | None = None
    user: UserPublic | None = None
    content_type: str
    content_text: str | None = None
    content_url: str | None = None
    decision: str
    severity: int
    categories: str | None = None
    confidence: float | None = None
    reason: str | None = None
    raw_response: str | None = None
    evidence_snapshot: str | None = None
    evidence_hash: str | None = None
    action_taken: ModerationAction
    created_at: datetime
    acted_at: datetime | None = None
    teamup_profile_id: int | None = None
    teamup_comment_id: int | None = None
    profile_user_id: int | None = None
    human_verdict: str | None = None
    human_category: str | None = None
    human_level: int | None = None
    human_by_id: int | None = None
    human_at: datetime | None = None

    @computed_field
    @property
    def target_type(self) -> str:
        if self.post_comment_id is not None:
            return "post_comment"
        if self.post_id is not None:
            return "post"
        if self.message_id is not None:
            return "message"
        return "unknown"


class ModerationRecordVerdict(BaseModel):
    verdict: Literal["violation", "clean"]
    category: str | None = Field(default=None, max_length=40)
    level: int | None = Field(default=None, ge=0, le=4)

    @model_validator(mode="after")
    def validate_verdict(self) -> ModerationRecordVerdict:
        if self.verdict == "clean" and (self.category or self.level is not None):
            raise ValueError("判为不违规时不能带类别和等级")
        if self.verdict == "violation" and not self.category:
            raise ValueError("判为违规时必须选择类别")
        return self


class DownloadSettingsRead(BaseModel):
    url: str | None = None
    pc_url: str | None = None
    android_url: str | None = None
    scratch_url: str | None = None
    version: str | None = None
    pc_version: str | None = None
    mobile_version: str | None = None
    # Mac 版只来自发版服务（stable 渠道的 dmg），后台「下载配置」没有手填项
    mac_url: str | None = None
    mac_version: str | None = None
    updated_at: datetime | None = None
    updated_by: UserPublic | None = None


class DownloadSettingsUpdate(BaseModel):
    url: str | None = Field(default=None, max_length=1000)
    pc_url: str | None = Field(default=None, max_length=1000)
    android_url: str | None = Field(default=None, max_length=1000)
    scratch_url: str | None = Field(default=None, max_length=1000)
    version: str | None = Field(default=None, max_length=80)
    pc_version: str | None = Field(default=None, max_length=80)
    mobile_version: str | None = Field(default=None, max_length=80)

    @model_validator(mode="after")
    def validate_download_url(self) -> DownloadSettingsUpdate:
        self.pc_url = self._normalize_url(self.pc_url if self.pc_url is not None else self.url, "PC download URL")
        self.android_url = self._normalize_url(self.android_url, "Android download URL")
        self.scratch_url = self._normalize_url(self.scratch_url, "Scratch extension URL")
        self.url = self.pc_url
        self.version = self.version.strip() if self.version else None
        self.pc_version = (self.pc_version if self.pc_version is not None else self.version)
        self.pc_version = self.pc_version.strip() if self.pc_version else None
        self.mobile_version = self.mobile_version.strip() if self.mobile_version else None
        return self

    @staticmethod
    def _normalize_url(value: str | None, label: str) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            return None
        if not normalized.startswith(("https://", "http://")):
            raise ValueError(f"{label} must start with http:// or https://")
        return normalized


class AdminPasswordResetRequestRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    account_user_id: int | None = None
    account_user: UserPublic | None = None
    ccw_profile_url: str
    reason: str
    requested_password: str
    request_ip: str
    account_registration_ip: str | None = None
    status: PasswordResetStatus
    reviewer_id: int | None = None
    reviewer: UserPublic | None = None
    review_note: str | None = None
    created_at: datetime
    reviewed_at: datetime | None = None


class AdminPasswordResetReview(BaseModel):
    status: Literal[PasswordResetStatus.APPROVED, PasswordResetStatus.REJECTED]
    review_note: str | None = Field(default=None, max_length=2000)
    new_password: str | None = Field(default=None, min_length=6, max_length=128)

    @model_validator(mode="after")
    def validate_new_password(self) -> AdminPasswordResetReview:
        if self.status == PasswordResetStatus.APPROVED and not self.new_password:
            raise ValueError("Approved password reset requests require a new password")
        return self
