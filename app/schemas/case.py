from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from app.schemas.penalty import PenaltyInput
from app.schemas.report import ReportRead
from app.schemas.user import UserPublic


# ---------------------------------------------------------------- 举报（用户侧）


class ReportReasonRead(BaseModel):
    reason: str
    # 选这个理由时 details 是否必填
    details_required: bool = False


class ReportOptions(BaseModel):
    reasons: list[ReportReasonRead]
    details_min_length: int
    max_evidence_messages: int = 10
    max_attachments: int = 6


class ReporterCredit(BaseModel):
    score: float
    daily_quota: int
    used_today: int
    remaining_today: int
    upheld: int
    dismissed: int
    malicious: int
    pending: int
    window_days: int


# ---------------------------------------------------------------- 案件（后台）


class CaseRead(BaseModel):
    id: int
    target_type: Literal["message", "post", "user", "conversation"]
    target_id: int
    conversation_id: int | None = None
    message_id: int | None = None
    reported_user_id: int | None = None
    reported_user: UserPublic | None = None
    status: Literal["open", "resolved", "dismissed"]
    queue: Literal["triage", "jury", "admin"]
    jury_eligible: bool
    report_count: int
    primary_reason: str | None = None
    priority: float
    summary: str | None = None
    machine_decision: str | None = None
    machine_category: str | None = None
    machine_level: int | None = None
    machine_confidence: float | None = None
    verdict_source: str | None = None
    verdict_category: str | None = None
    verdict_category_label: str | None = None
    verdict_level: int | None = None
    verdict_note: str | None = None
    violation_id: int | None = None
    first_report_id: int | None = None
    created_at: datetime
    closed_at: datetime | None = None


class CasePage(BaseModel):
    items: list[CaseRead] = Field(default_factory=list)
    total: int = 0
    # 各队列里未结案件的数量：triage / jury / admin
    open_counts: dict[str, int] = Field(default_factory=dict)


class JuryVoteSummary(BaseModel):
    vote: str | None = None
    note: str | None = None
    voted_at: datetime | None = None
    juror: UserPublic | None = None


class CaseDetail(CaseRead):
    # 证据快照：首条举报提交时固化的内容
    snapshot: dict[str, Any] | None = None
    reports: list[ReportRead] = Field(default_factory=list)
    votes: list[JuryVoteSummary] = Field(default_factory=list)


class CaseResolve(BaseModel):
    outcome: Literal["violation", "dismiss"]
    category: str | None = Field(default=None, max_length=40)
    level: int | None = Field(default=None, ge=0, le=4)
    delete_content: bool = True
    # 不给时按阶梯自动计算
    penalty: PenaltyInput | None = None
    note: str | None = Field(default=None, max_length=2000)
    # 驳回时可以把其中的恶意举报人标出来，扣他们的举报信誉
    malicious_reporter_ids: list[int] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def validate_outcome(self) -> CaseResolve:
        if self.outcome == "dismiss" and (self.category or self.level is not None or self.penalty is not None):
            raise ValueError("驳回的案件不能带处罚")
        if self.outcome == "violation" and self.malicious_reporter_ids:
            raise ValueError("认定违规的案件不能标记恶意举报")
        return self


class CaseMove(BaseModel):
    queue: Literal["jury", "admin"]


# ---------------------------------------------------------------- 风纪委员


class JuryRequirement(BaseModel):
    key: str
    ok: bool
    text: str
    detail: str | None = None


class JuryStats(BaseModel):
    votes_total: int = 0
    votes_settled: int = 0
    votes_agreed: int = 0
    agreement_rate: float | None = None
    golden_total: int = 0
    golden_correct: int = 0


class JuryStatus(BaseModel):
    # none / applied / active / expired / rejected / suspended / retired
    status: str
    eligible: bool
    requirements: list[JuryRequirement]
    term_ends_at: datetime | None = None
    review_note: str | None = None
    stats: JuryStats | None = None
    daily_limit: int
    voted_today: int


class JuryApply(BaseModel):
    statement: str | None = Field(default=None, max_length=500)


class JuryCaseMessage(BaseModel):
    speaker: str
    is_target: bool
    type: str
    content: str | None = None
    images: list[str] = Field(default_factory=list)
    created_at: str | None = None


class JuryCasePost(BaseModel):
    content: str | None = None
    image_urls: list[str] = Field(default_factory=list)
    created_at: str | None = None


class JuryReasonCount(BaseModel):
    reason: str
    count: int


class JuryCaseView(BaseModel):
    case_id: int
    kind: Literal["message", "post"]
    reasons: list[JuryReasonCount]
    report_count: int
    messages: list[JuryCaseMessage] = Field(default_factory=list)
    post: JuryCasePost | None = None


class JuryNextCase(BaseModel):
    # 没有可评审的案件时为 null
    case: JuryCaseView | None = None


class JuryVoteCreate(BaseModel):
    vote: Literal["none", "l1", "l2", "l3", "abstain"]
    note: str | None = Field(default=None, max_length=500)


class JuryVoteResult(BaseModel):
    case_id: int
    settled: bool


class JuryHistoryItem(BaseModel):
    case_id: int
    kind: str
    vote: str | None = None
    voted_at: datetime | None = None
    agreed: bool | None = None
    outcome: Literal["violation", "clean"] | None = None


class JuryMemberRead(JuryStats):
    user_id: int
    user: UserPublic | None = None
    status: str
    statement: str | None = None
    applied_at: datetime | None = None
    reviewed_at: datetime | None = None
    review_note: str | None = None
    term_ends_at: datetime | None = None


class JuryMemberPage(BaseModel):
    items: list[JuryMemberRead] = Field(default_factory=list)
    total: int = 0


class JuryMemberReview(BaseModel):
    action: Literal["approve", "reject", "suspend", "retire", "reinstate"]
    note: str | None = Field(default=None, max_length=500)
