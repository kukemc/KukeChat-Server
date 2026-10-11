"""风纪系统 v2 一期的接口结构，契约见 docs/jury-v2.md 第 1 节。"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.case import JuryReasonCount


# ---------------------------------------------------------------- 配置


class JuryTierOption(BaseModel):
    key: str
    label: str


class JuryVoteOption(BaseModel):
    value: str
    # clean / violation / abstain
    kind: str
    label: str
    hint: str
    level: int | None = None


class JuryRules(BaseModel):
    seats_per_case: int
    quick_consensus_votes: int
    full_consensus_votes: int
    timeout_hours: int
    assignment_ttl_hours: int
    daily_votes: int
    term_days: int
    context_near: int
    content_retention_days: int
    publish_delay_hours: int
    min_sample: int
    # 委员和机器给出的封禁最长多久；更重的要管理员确认
    max_ban_hours: int


class JuryOpinionRules(BaseModel):
    max_length: int
    per_case: int
    per_minute: int
    per_day: int
    auto_hide_reports: int
    report_reasons: list[str]


class JuryEligibilityRules(BaseModel):
    min_account_age_days: int
    min_recent_messages: int
    recent_activity_days: int


class JuryConfig(BaseModel):
    version: int
    capabilities: list[str]
    switches: dict[str, bool]
    tiers: list[JuryTierOption]
    votes: list[JuryVoteOption]
    rules: JuryRules
    opinions: JuryOpinionRules
    eligibility: JuryEligibilityRules


# ---------------------------------------------------------------- 总览


class JuryOverviewMe(BaseModel):
    status: str
    tier: str | None = None
    available_cases: int | None = None
    voted_today: int = 0
    daily_limit: int


class JuryOverviewDay(BaseModel):
    # UTC+8 的自然日
    date: str
    # 当天进入委员队列的案件数
    queued: int = 0
    # 当天定案的（经过委员评审并已定案）
    settled: int = 0


class JuryOverview(BaseModel):
    jurors: dict[str, int]
    backlog: dict[str, int]
    settled: dict[str, int]
    window_days: int
    # 窗口内离开委员队列（定案或转出）的案件数
    sample: int
    # false 时下面三个比率都是 null
    sample_enough: bool
    median_hours: float | None = None
    consensus_rate: float | None = None
    violation_rate: float | None = None
    overturn_rate: float | None = None
    counts: dict[str, int]
    me: JuryOverviewMe
    # 近 30 天每天一项，按日期正序；样本不足时照样给
    daily: list[JuryOverviewDay] = Field(default_factory=list)
    generated_at: datetime


# ---------------------------------------------------------------- 观点区


class JuryOpinion(BaseModel):
    id: int
    parent_id: int | None = None
    alias: str | None = None
    tier: str | None = None
    # 这位委员投的票；定案后才给
    vote: str | None = None
    # 已删除 / 被折叠时为 null
    content: str | None = None
    state: Literal["visible", "deleted", "hidden"]
    is_note: bool = False
    mine: bool = False
    like_count: int = 0
    liked: bool = False
    reported: bool = False
    created_at: datetime | None = None
    reply_to_alias: str | None = None
    replies: list[JuryOpinion] = Field(default_factory=list)


class JuryOpinionPage(BaseModel):
    items: list[JuryOpinion] = Field(default_factory=list)
    total: int = 0


class JuryOpinionCreate(BaseModel):
    # 长度在服务层校验，超长要返回 invalid_opinion 而不是通用的 422
    content: str = ""
    parent_id: int | None = Field(default=None, gt=0)


class JuryOpinionState(BaseModel):
    id: int
    state: Literal["visible", "deleted", "hidden"]


class JuryOpinionLikeResult(BaseModel):
    id: int
    like_count: int
    liked: bool


class JuryOpinionReportCreate(BaseModel):
    reason: str = ""
    details: str | None = None


# ---------------------------------------------------------------- 我的评审


class JuryVoteItem(BaseModel):
    case_id: int
    kind: str
    primary_reason: str | None = None
    vote: str | None = None
    voted_at: datetime | None = None
    alias: str | None = None
    stage: Literal["jury", "panel", "admin", "closed"]
    outcome: Literal["violation", "clean"] | None = None
    verdict_source: str | None = None
    verdict_level: int | None = None
    closed_at: datetime | None = None
    agreed: bool | None = None
    overturned: bool = False
    is_quality_check: bool = False
    opinion_count: int = 0


class JuryVotePage(BaseModel):
    items: list[JuryVoteItem] = Field(default_factory=list)
    total: int = 0


class JuryTrendDay(BaseModel):
    # UTC+8 的自然日
    date: str
    votes: int = 0
    settled: int = 0
    agreed: int = 0


class JuryMyStats(BaseModel):
    votes_total: int = 0
    votes_settled: int = 0
    votes_agreed: int = 0
    agreement_rate: float = 0
    golden_total: int = 0
    golden_correct: int = 0
    golden_rate: float = 0
    abstain_total: int = 0
    abstain_rate: float = 0
    opinions_total: int = 0
    likes_received: int = 0
    trend: list[JuryTrendDay] = Field(default_factory=list)


# ---------------------------------------------------------------- 公示栏


class PublicCase(BaseModel):
    case_id: int
    kind: Literal["message", "post"]
    reasons: list[JuryReasonCount]
    report_count: int
    outcome: Literal["violation", "clean"]
    category: str | None = None
    category_label: str | None = None
    level: int | None = None
    # jury / panel / admin
    source: str | None = None
    tally: dict[str, int]
    # 有效票数（不含弃权）
    votes: int = 0
    closed_at: datetime | None = None
    hours: float | None = None
    overturned: bool = False
    # 详情里是否带内容
    has_content: bool = False


class PublicCasePage(BaseModel):
    items: list[PublicCase] = Field(default_factory=list)
    total: int = 0


class PublicCaseContent(BaseModel):
    text: str | None = None
    image_count: int = 0
    created_at: str | None = None


class PublicCaseDetail(PublicCase):
    content: PublicCaseContent | None = None
    # group_message / clean / sensitive / overturned；有内容时为 null
    content_hidden_reason: str | None = None
    overturn_note: str | None = None


# ---------------------------------------------------------------- 后台


class JurySettingsRead(BaseModel):
    switches: dict[str, bool]
    # 部署配置里的阈值，只读
    thresholds: dict[str, int | float]


class JurySettingsUpdate(BaseModel):
    auto_appoint: bool | None = None
    appeals: bool | None = None
    machine_review: bool | None = None
    public_group_content: bool | None = None
    opinions_open_to_jurors: bool | None = None
