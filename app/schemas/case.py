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
    # panel：资深合议中（三期）
    queue: Literal["triage", "jury", "panel", "admin"]
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
    # 离开委员队列的方式：consensus / no_consensus / need_context / timeout / manual；没进过或还在队列里为 null
    jury_outcome: str | None = None
    # 不能交给委员的原因，目前只有 sensitive
    jury_block_reason: str | None = None
    queued_at: datetime | None = None
    escalated_at: datetime | None = None
    # 正在公示栏里的案件才有
    published_at: datetime | None = None
    overturned_at: datetime | None = None
    overturn_note: str | None = None
    # —— 三期 ——
    # report 有人举报 / machine 机器拿不准的内容（没有举报人；之后有人举报同一条内容时并进来，仍是 machine）
    case_origin: Literal["report", "machine"] = "report"
    # 进入资深合议的时间；没进过为 null
    panel_at: datetime | None = None
    # 合议的去向：consensus 合议定案 / timeout 超时转管理员 / unavailable 凑不齐人 / manual 管理员接手；合议中或没进过为 null
    panel_outcome: str | None = None
    # 举报人复议：none / pending / upheld / overturned
    reconsider_status: str = "none"


class CasePage(BaseModel):
    items: list[CaseRead] = Field(default_factory=list)
    total: int = 0
    # 各队列里未结案件的数量：triage / jury / panel / admin
    open_counts: dict[str, int] = Field(default_factory=dict)


class JuryVoteSummary(BaseModel):
    # 这位委员在这个案件上的评审编号：委员侧接口里他看到的 case_id 是它，不是案件编号
    review_id: int | None = None
    vote: str | None = None
    note: str | None = None
    voted_at: datetime | None = None
    juror: UserPublic | None = None
    alias: str | None = None
    tier: str | None = None
    # 违规票带的类别；没带为 null
    category: str | None = None
    # 这一票是哪一轮投的：jury 原审 / panel 资深合议
    stage: str = "jury"


class AdminJuryOpinionReport(BaseModel):
    id: int
    reporter: UserPublic | None = None
    reason: str
    details: str | None = None
    # pending / handled / dismissed
    status: str
    created_at: datetime | None = None


class AdminJuryOpinionRead(BaseModel):
    """后台看到的观点：带真实作者、原文（即使已删除 / 被折叠）和举报记录。"""

    id: int
    case_id: int
    parent_id: int | None = None
    reply_to_id: int | None = None
    author_id: int
    author: UserPublic | None = None
    alias: str | None = None
    tier: str | None = None
    content: str
    state: Literal["visible", "deleted", "hidden"]
    is_note: bool = False
    like_count: int = 0
    created_at: datetime | None = None
    deleted_at: datetime | None = None
    deleted_by_id: int | None = None
    moderation_note: str | None = None
    pending_reports: int = 0
    reports: list[AdminJuryOpinionReport] = Field(default_factory=list)


class AdminJuryOpinionPage(BaseModel):
    items: list[AdminJuryOpinionRead] = Field(default_factory=list)
    total: int = 0


class AdminJuryOpinionModerate(BaseModel):
    action: Literal["delete", "restore", "dismiss_reports"]
    note: str | None = Field(default=None, max_length=500)


class CaseDetail(CaseRead):
    # 证据快照：首条举报提交时固化的内容
    snapshot: dict[str, Any] | None = None
    reports: list[ReportRead] = Field(default_factory=list)
    votes: list[JuryVoteSummary] = Field(default_factory=list)
    opinions: list[AdminJuryOpinionRead] = Field(default_factory=list)
    # 只在改判接口的返回里有：改判成不违规后内容是否恢复了
    content_restored: bool | None = None


class CaseOverturn(BaseModel):
    outcome: Literal["violation", "dismiss"]
    category: str | None = Field(default=None, max_length=40)
    level: int | None = Field(default=None, ge=0, le=4)
    # 会出现在公示栏和委员的案件详情里
    note: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def validate_outcome(self) -> CaseOverturn:
        if self.outcome == "dismiss" and (self.category or self.level is not None):
            raise ValueError("改判为不违规时不能带类别和等级")
        if not self.note.strip():
            raise ValueError("请写明改判理由")
        return self


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


class JuryRestriction(BaseModel):
    reason: Literal["muted", "banned"]
    # 什么时候解除；永久封禁为 null
    until: datetime | None = None
    message: str


class JuryExamSummary(BaseModel):
    # false 表示题库里已核对的题不够，暂时不能考
    available: bool = False
    passed: bool = False
    passed_at: datetime | None = None
    attempts_used: int = 0
    attempts_max: int
    # 现在就能考时为 null
    next_attempt_at: datetime | None = None
    last_score: int | None = None
    pass_score: int
    total: int


class JuryTierRequirement(BaseModel):
    # votes_settled / agreement_rate / quality_rate / days_served
    key: str
    label: str
    # 还没有数据（比如质控题没答够）时为 null
    current: int | float | None = None
    target: int | float
    ok: bool
    note: str | None = None


class JuryTierProgress(BaseModel):
    # 已是资深时为 null
    next_tier: str | None = None
    requirements: list[JuryTierRequirement] = Field(default_factory=list)


class JuryWarning(BaseModel):
    # high_abstain 弃权偏多 / demote_risk 最近的一致率接近或低于降级线 / suspend_risk 一致率或质控正确率接近或低于暂停线
    code: str
    message: str
    # 服务端实际用来判断的那个数、它要比较的线、它的样本数；客户端直接展示，不要自己估算
    current: float | None = None
    threshold: float | None = None
    sample: int | None = None


class JurySeniorCounts(BaseModel):
    panel: int = 0
    appeals: int = 0


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
    # 评审上限按最近 24 小时滚动：下一个名额什么时候释放（最早那一票的时间 + 24 小时）；最近 24 小时没投过票为 null
    daily_resets_at: datetime | None = None
    # trainee / regular / senior；不是在任委员时为 null
    tier: str | None = None
    # 现在能分给我的未结案件数（上限 50）；不是在任委员时为 null
    available_cases: int | None = None
    # 在任委员正在受处罚时有值：评审、发观点、点赞都会被拒绝（juror_restricted）
    restriction: JuryRestriction | None = None
    # —— 二期 ——
    exam: JuryExamSummary | None = None
    # 申请是否必须先通过入门考试
    exam_required: bool = False
    # 风纪值；没当过委员为 0
    points: int = 0
    # 离下一层还差什么；不是在任委员为 null
    tier_progress: JuryTierProgress | None = None
    # 是否公开展示委员身份
    badge_visible: bool = False
    warnings: list[JuryWarning] = Field(default_factory=list)
    # 弃权过多被暂停发案到什么时候
    cooldown_until: datetime | None = None
    # 今天还能主动回避几次；不是在任委员为 null
    recuse_left_today: int | None = None
    # 回避上限的下一个名额什么时候释放；最近 24 小时没回避过为 null
    recuse_resets_at: datetime | None = None
    # —— 三期 ——
    # 资深委员：现在能分给我的合议 / 复审件数（复审里含举报人复议）；其他人为 null
    senior: JurySeniorCounts | None = None


class JuryMeSettings(BaseModel):
    badge_visible: bool | None = None


class JuryApply(BaseModel):
    statement: str | None = Field(default=None, max_length=500)


class JuryCaseMessage(BaseModel):
    speaker: str
    is_target: bool
    type: str
    content: str | None = None
    images: list[str] = Field(default_factory=list)
    # 带 Z 的 ISO 字符串（UTC）
    created_at: str | None = None
    # 相对被举报消息的位置：目标是 0，之前为负，之后为正
    offset: int = 0


class JuryCasePost(BaseModel):
    content: str | None = None
    image_urls: list[str] = Field(default_factory=list)
    # 带 Z 的 ISO 字符串（UTC）
    created_at: str | None = None


class JuryReasonCount(BaseModel):
    reason: str
    count: int


class JuryCaseContent(BaseModel):
    # 评审编号：对每个委员、每个案件各不相同，只对本人有效；不是案件的公开编号（docs/jury-v2.md 1.0）
    case_id: int
    kind: Literal["message", "post"]
    reasons: list[JuryReasonCount]
    report_count: int
    # 快照里的全部上下文（前后最多各 10 条）
    messages: list[JuryCaseMessage] = Field(default_factory=list)
    post: JuryCasePost | None = None
    # report 有人举报 / machine 机器拿不准的内容：没有举报人，report_count 为 0，reasons 是一项「机器筛查 · 类别」
    case_origin: Literal["report", "machine"] = "report"


class JuryCaseView(JuryCaseContent):
    # 分到手的时间和让出的时间
    assigned_at: datetime | None = None
    expires_at: datetime | None = None
    # 投违规票时默认选中的类别：按举报理由映射；映射不到可选类别时为 null
    suggested_category: str | None = None


class JuryNextCase(BaseModel):
    # 没有可评审的案件时为 null
    case: JuryCaseView | None = None


class JuryVoteCreate(BaseModel):
    # recuse 主动回避 / need_context 上下文不足是二期加的；旧客户端只会发前五种
    vote: Literal["none", "l1", "l2", "l3", "abstain", "recuse", "need_context"]
    note: str | None = Field(default=None, max_length=500)
    # 投违规票时可以带类别（config.categories 里的 key）；合法性在服务层校验
    category: str | None = Field(default=None, max_length=40)


class JuryMyVote(BaseModel):
    vote: str
    note: str | None = None
    voted_at: datetime | None = None
    # 我在这个案件里的代号；质控题没有
    alias: str | None = None
    # 是否与结论一致；未定案或我弃权时为 null
    agreed: bool | None = None
    # 我投违规票时选的类别
    category: str | None = None
    # 这个案件累计给了我多少风纪值
    points: int = 0


class JuryVerdict(BaseModel):
    outcome: Literal["violation", "clean"]
    # jury 委员共识 / panel 资深合议 / admin 管理员 / auto 机器
    source: str | None = None
    category: str | None = None
    category_label: str | None = None
    level: int | None = None
    closed_at: datetime | None = None
    # 举报到定案用了多少小时
    hours: float | None = None
    overturned: bool = False
    overturn_note: str | None = None
    # 题库里这道题的讲解；只有质控题且管理员写了才有
    explanation: str | None = None


class JuryCasePrecedent(BaseModel):
    """案件详情里指向判例的那一小块。"""

    id: int
    title: str
    mine: bool = False


class JuryCaseDetail(JuryCaseContent):
    """投票之后看到的案件。只有对这个案件投过票的人能读。"""

    # 定案满内容保留期后为 false，此时 messages 为空、post 为 null
    content_available: bool = True
    my_vote: JuryMyVote
    # jury 委员评审中 / panel 资深合议中 / admin 等管理员 / closed 已定案
    stage: Literal["jury", "panel", "admin", "closed"]
    # 已有几张有效票（不含弃权）
    votes_cast: int = 0
    # 定案后才给：{ none, l1, l2, l3, abstain, need_context }；进过资深合议的多一项 panel: { none, l1, l2, l3 }
    tally: dict[str, Any] | None = None
    verdict: JuryVerdict | None = None
    is_quality_check: bool = False
    opinions_enabled: bool = True
    opinion_count: int = 0
    # 案件此刻在公示栏里时是它的公开编号，否则为 null；质控题恒为 null
    public_case_id: int | None = None
    # 这个案件是判例时为 { id, title, mine }；质控题恒为 null
    precedent: JuryCasePrecedent | None = None
    # 我现在能不能把它标为判例（资深、已定案、未改判、类别允许、还不是判例）
    can_mark_precedent: bool = False


class JuryRewardItem(BaseModel):
    kind: str
    points: int
    label: str


class JuryReward(BaseModel):
    """投票当场结算的风纪值。案件定案时的加分走 jury.points.changed 事件。"""

    points: int = 0
    total: int = 0
    items: list[JuryRewardItem] = Field(default_factory=list)


class JuryVoteResult(BaseModel):
    # 评审编号，和请求路径里的一样
    case_id: int
    settled: bool
    # 回避之后看不了这个案件，为 null
    case: JuryCaseDetail | None = None
    reward: JuryReward | None = None


class JuryHistoryItem(BaseModel):
    # 评审编号
    case_id: int
    kind: str
    vote: str | None = None
    voted_at: datetime | None = None
    agreed: bool | None = None
    outcome: Literal["violation", "clean"] | None = None


class JuryMemberRead(JuryStats):
    user_id: int
    user: UserPublic | None = None
    # applied / active / expired（任期届满未续任）/ rejected / suspended / retired，和本人在 /jury/me 看到的一致
    status: str
    statement: str | None = None
    applied_at: datetime | None = None
    reviewed_at: datetime | None = None
    review_note: str | None = None
    term_ends_at: datetime | None = None
    tier: str = "trainee"
    # —— 二期 ——
    points: int = 0
    # 最近一场考试的分数；没考过为 null（显示「未考试」）
    exam_score: int | None = None
    exam_passed_at: datetime | None = None
    # 「拿不准 + 上下文不足」占全部票的比例 / 回避占全部分配的比例
    abstain_rate: float = 0
    recuse_rate: float = 0
    badge_visible: bool = False
    # 本次任职到现在多少天；不是在任状态为 0
    days_served: int = 0


class JuryMemberPage(BaseModel):
    items: list[JuryMemberRead] = Field(default_factory=list)
    total: int = 0


class JuryMemberReview(BaseModel):
    action: Literal["approve", "reject", "suspend", "retire", "reinstate", "set_tier", "adjust_points"]
    note: str | None = Field(default=None, max_length=500)
    # approve / reinstate 时可选（创始任命：直接任命为任意层级）；set_tier 时必填
    tier: Literal["trainee", "regular", "senior"] | None = None
    # adjust_points 时必填：要增减的风纪值，可以为负（总分扣到 0 为止）
    points: int | None = Field(default=None, ge=-100000, le=100000)

    @model_validator(mode="after")
    def validate_tier(self) -> JuryMemberReview:
        if self.action == "set_tier" and self.tier is None:
            raise ValueError("set_tier 需要指定 tier")
        if self.tier is not None and self.action not in {"approve", "reinstate", "set_tier"}:
            raise ValueError("只有任命、复职和调整层级时可以指定 tier")
        if self.action == "adjust_points" and not self.points:
            raise ValueError("adjust_points 需要指定非零的 points")
        if self.points is not None and self.action != "adjust_points":
            raise ValueError("只有调整风纪值时可以指定 points")
        return self
