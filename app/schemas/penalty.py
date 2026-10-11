from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from app.schemas.user import UserPublic


PenaltyKind = Literal["none", "notice", "warning", "mute", "ban"]


class ViolationRead(BaseModel):
    """当事人能看到的处罚说明。"""

    id: int
    category: str
    category_label: str
    level: int
    points: int
    points_total: int
    # auto / report / jury / admin
    source: str
    target_type: str | None = None
    target_id: int | None = None
    conversation_id: int | None = None
    summary: str | None = None
    status: Literal["active", "revoked"]
    penalty_kind: PenaltyKind
    penalty_ends_at: datetime | None = None
    penalty_permanent: bool = False
    under_review: bool = False
    appeal_status: Literal["none", "pending", "accepted", "rejected"]
    can_appeal: bool = False
    created_at: datetime
    expires_at: datetime


class BanState(BaseModel):
    reason: str | None = None
    banned_until: datetime | None = None
    permanent: bool = False


class ModerationStanding(BaseModel):
    points: int
    point_window_days: int
    muted_until: datetime | None = None
    mute_reason: str | None = None
    next_threshold: int | None = None
    # "mute:24" / "ban:168" / "ban:permanent"
    next_penalty: str | None = None
    ban: BanState | None = None
    violations: list[ViolationRead] = Field(default_factory=list)


class AppealCreate(BaseModel):
    text: str = Field(min_length=5, max_length=2000)


class RuleCategory(BaseModel):
    key: str
    label: str
    level: int


class RuleLevel(BaseModel):
    level: int
    points: int
    penalty: str


class RuleTier(BaseModel):
    threshold: int
    penalty: str


class ModerationRules(BaseModel):
    point_window_days: int
    categories: list[RuleCategory]
    levels: list[RuleLevel]
    ladder: list[RuleTier]


# ---------------------------------------------------------------- 后台


class AdminViolationRead(ViolationRead):
    user_id: int
    user: UserPublic | None = None
    raw_source: str
    confidence: float | None = None
    reason: str | None = None
    moderation_record_id: int | None = None
    report_id: int | None = None
    case_id: int | None = None
    actor: UserPublic | None = None
    review_status: Literal["none", "pending", "confirmed", "adjusted"]
    recommended_penalty: str | None = None
    reviewed_at: datetime | None = None
    appeal_text: str | None = None
    appealed_at: datetime | None = None
    appeal_note: str | None = None
    appeal_reviewed_at: datetime | None = None
    revoked_at: datetime | None = None
    revoke_reason: str | None = None


class AdminViolationPage(BaseModel):
    items: list[AdminViolationRead] = Field(default_factory=list)
    total: int = 0


class PenaltyInput(BaseModel):
    kind: PenaltyKind
    hours: int | None = Field(default=None, ge=1, le=24 * 365)
    permanent: bool = False

    @model_validator(mode="after")
    def validate_duration(self) -> PenaltyInput:
        if self.kind == "mute" and (self.permanent or self.hours is None):
            raise ValueError("禁言必须指定时长")
        if self.kind == "ban" and not self.permanent and self.hours is None:
            raise ValueError("封禁必须指定时长或选择永久")
        if self.kind not in {"mute", "ban"} and (self.permanent or self.hours is not None):
            raise ValueError("该处置没有时长")
        return self


class AdminViolationCreate(BaseModel):
    user_id: int = Field(gt=0)
    category: str = Field(min_length=1, max_length=40)
    level: int | None = Field(default=None, ge=0, le=4)
    # 不给 penalty 时按阶梯自动计算
    penalty: PenaltyInput | None = None
    reason: str | None = Field(default=None, max_length=1000)
    summary: str | None = Field(default=None, max_length=500)


class AdminViolationRevoke(BaseModel):
    reason: str = Field(min_length=1, max_length=1000)


class AdminViolationReview(BaseModel):
    # confirm：维持已执行的处置；adjust：改成 penalty 指定的处置
    action: Literal["confirm", "adjust"]
    penalty: PenaltyInput | None = None
    note: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def validate_penalty(self) -> AdminViolationReview:
        if self.action == "adjust" and self.penalty is None:
            raise ValueError("改判必须给出新的处置")
        return self


class AdminAppealReview(BaseModel):
    accept: bool
    note: str | None = Field(default=None, max_length=1000)
