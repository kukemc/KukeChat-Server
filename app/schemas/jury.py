"""风纪系统 v2 的接口结构，契约见 docs/jury-v2.md 第 1、2 节。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.schemas.case import JuryCaseMessage, JuryCasePost, JuryReasonCount, JuryStatus
from app.schemas.user import UserPublic


# ---------------------------------------------------------------- 配置


class JuryTierOption(BaseModel):
    key: str
    label: str
    # 晋升到这一层的条件：{ votes_settled, agreement_rate, quality_rate, quality_min, days_served }；见习为 null
    requirements: dict[str, int | float] | None = None
    # 这一层的降级线：{ agreement_rate, window, min_votes, to }；见习为 null
    demote_below: dict[str, Any] | None = None


class JuryVoteOption(BaseModel):
    value: str
    # clean / violation / abstain / recuse
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
    # 定为严重违规至少要几张有效票 / 同一案件累计几张「上下文不足」后转出
    severe_min_votes: int
    need_context_votes: int


class JuryOpinionRules(BaseModel):
    max_length: int
    per_case: int
    per_minute: int
    per_day: int
    auto_hide_reports: int
    report_reasons: list[str]
    # 一条观点的赞数达到多少给作者加分
    like_reward_threshold: int


class JuryEligibilityRules(BaseModel):
    min_account_age_days: int
    min_recent_messages: int
    recent_activity_days: int


class JuryCategoryOption(BaseModel):
    key: str
    label: str
    default_level: int


class JurySuspendRules(BaseModel):
    min_settled: int
    agreement_rate: float
    quality_min: int
    quality_rate: float


class JuryExamRules(BaseModel):
    total: int
    pass_score: int
    time_limit_minutes: int
    grace_seconds: int
    attempts_max: int
    window_days: int
    retry_hours: int
    valid_days: int


class JuryDailyLimit(BaseModel):
    daily_limit: int


class JuryAbstainRules(BaseModel):
    window: int
    min_votes: int
    warn_rate: float
    cooldown_rate: float
    cooldown_hours: int


class JuryConfig(BaseModel):
    version: int
    capabilities: list[str]
    switches: dict[str, bool]
    tiers: list[JuryTierOption]
    votes: list[JuryVoteOption]
    rules: JuryRules
    opinions: JuryOpinionRules
    eligibility: JuryEligibilityRules
    # —— 二期 ——
    # 投违规票时可选的类别（不含色情类）
    categories: list[JuryCategoryOption]
    # 风纪值：事件 kind -> 分值
    points: dict[str, int]
    # 系统自动暂停资格的条件
    suspend: JurySuspendRules
    exam: JuryExamRules
    practice: JuryDailyLimit
    recuse: JuryDailyLimit
    abstain: JuryAbstainRules


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
    # 回避的记录为 false：点不进详情
    can_view: bool = True
    # 案件此刻在公示栏里时是它的公开编号，否则为 null；质控题和回避的记录恒为 null
    public_case_id: int | None = None


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
    # 带 Z 的 ISO 字符串（UTC）
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
    exam_required: bool | None = None


# ---------------------------------------------------------------- 入门考试


class QuestionAnswer(BaseModel):
    """一道题的标准答案。只判「违规 / 不违规」，等级和类别只是给出来看。"""

    outcome: Literal["violation", "clean"]
    level: int | None = None
    category_label: str | None = None
    explanation: str | None = None


class ExamQuestion(BaseModel):
    """和 JuryCaseView 同形，但没有案件编号，用题号标识。"""

    # 从 1 开始的序号
    question: int
    kind: Literal["message", "post"]
    reasons: list[JuryReasonCount]
    report_count: int = 0
    messages: list[JuryCaseMessage] = Field(default_factory=list)
    post: JuryCasePost | None = None


class ExamActive(BaseModel):
    attempt_id: int
    expires_at: datetime
    questions: list[ExamQuestion]


class ExamStart(ExamActive):
    # 服务器当前时间，用来校正本机倒计时
    server_time: datetime


class ExamLastResult(BaseModel):
    score: int
    passed: bool
    submitted_at: datetime | None = None


class ExamState(BaseModel):
    available: bool
    # 四项资格是否都满足；不满足不能考
    eligible: bool
    passed: bool
    passed_at: datetime | None = None
    total: int
    pass_score: int
    time_limit_minutes: int
    attempts_used: int
    attempts_max: int
    window_days: int
    # 现在就能考时为 null
    next_attempt_at: datetime | None = None
    last_result: ExamLastResult | None = None
    # 进行中的一场
    active: ExamActive | None = None
    server_time: datetime


class ExamAnswer(BaseModel):
    # 题号和选项的合法性在服务层校验，不合法返回 invalid_answers 而不是通用的 422，所以这里不限制类型
    question: Any = None
    vote: Any = None


class ExamSubmit(BaseModel):
    answers: list[ExamAnswer] = Field(default_factory=list, max_length=200)


class ExamResultItem(BaseModel):
    question: int
    correct: bool
    # 没答的题为 null，按答错算
    your_vote: str | None = None
    answer: QuestionAnswer | None = None


class ExamResult(BaseModel):
    score: int
    total: int
    pass_score: int
    passed: bool
    results: list[ExamResultItem]
    attempts_left: int
    next_attempt_at: datetime | None = None
    # 通过且 auto_appoint 开时这里已经是 active
    status: JuryStatus


# ---------------------------------------------------------------- 练习场


class PracticeQuestion(BaseModel):
    question_id: int
    # 是否已核对
    verified: bool = False
    # 我以前做过这道题
    answered_before: bool = False
    kind: Literal["message", "post"]
    reasons: list[JuryReasonCount]
    report_count: int = 0
    messages: list[JuryCaseMessage] = Field(default_factory=list)
    post: JuryCasePost | None = None


class PracticeStats(BaseModel):
    answered: int = 0
    correct: int = 0


class PracticeRound(BaseModel):
    # 练习场里我能练的题数 / 其中我做过的（不重复计）。done == total 说明这一轮做完了，之后出的都是重做的题。
    # 练习场只出未核对的题和已核对题里固定切出来的一份；用于考试和质控的题不在这里
    total: int = 0
    done: int = 0


class PracticeNext(BaseModel):
    # 题库为空、或今天已经做满时为 null
    question: PracticeQuestion | None = None
    left_today: int
    daily_limit: int
    stats: PracticeStats
    round: PracticeRound
    # 练习上限按最近 24 小时滚动：下一个名额什么时候释放；最近 24 小时没练过为 null
    resets_at: datetime | None = None


class PracticeAnswerCreate(BaseModel):
    vote: Literal["none", "l1", "l2", "l3"]


class PracticeAnswerResult(BaseModel):
    correct: bool
    answer: QuestionAnswer
    left_today: int
    stats: PracticeStats
    resets_at: datetime | None = None


# ---------------------------------------------------------------- 风纪值、成就、排行榜


class JuryPointItem(BaseModel):
    kind: str
    label: str
    points: int
    case_id: int | None = None
    created_at: datetime | None = None


class JuryPointPage(BaseModel):
    total: int = 0
    items: list[JuryPointItem] = Field(default_factory=list)
    # 明细的总条数
    count: int = 0


class JuryAchievementItem(BaseModel):
    key: str
    label: str
    description: str
    earned_at: datetime | None = None
    current: int = 0
    target: int


class JuryAchievementPage(BaseModel):
    items: list[JuryAchievementItem] = Field(default_factory=list)


class LeaderboardItem(BaseModel):
    rank: int
    points: int
    tier: str
    # 本期投了几张票
    votes: int = 0
    me: bool = False
    # 没公开身份的为 null，显示为「匿名委员」
    user: UserPublic | None = None


class Leaderboard(BaseModel):
    period: Literal["week", "month", "all"]
    items: list[LeaderboardItem] = Field(default_factory=list)
    # 和榜单行同形（me 恒为 true）；不是委员或本期没有得分时为 null
    me: LeaderboardItem | None = None
    generated_at: datetime


# ---------------------------------------------------------------- 后台：题库与考试记录


class AdminQuestionRead(BaseModel):
    case_id: int
    kind: str
    primary_reason: str | None = None
    summary: str | None = None
    closed_at: datetime | None = None
    verdict_source: str | None = None
    in_bank: bool = True
    verified: bool = False
    # 这道题给谁用：practice 只给练习场 / scored 给入门考试和评审质控；不在题库里为 null
    pool: Literal["practice", "scored"] | None = None
    # admin / consensus / legacy；不在题库里为 null
    source: str | None = None
    # 标准答案
    outcome: Literal["violation", "clean"]
    level: int | None = None
    category: str | None = None
    category_label: str | None = None
    explanation: str | None = None
    # 标准答案是不是管理员改过的
    answer_overridden: bool = False
    # 被答次数和正确率（评审中的质控作答 + 练习场）
    answered: int = 0
    correct_rate: float | None = None


class AdminQuestionPage(BaseModel):
    items: list[AdminQuestionRead] = Field(default_factory=list)
    total: int = 0


class AdminQuestionDetail(AdminQuestionRead):
    reasons: list[JuryReasonCount] = Field(default_factory=list)
    report_count: int = 0
    messages: list[JuryCaseMessage] = Field(default_factory=list)
    post: JuryCasePost | None = None
    # 不能当题目的原因：open / private / sensitive / public_content；可以时为 null
    block_reason: str | None = None
    block_message: str | None = None


class AdminQuestionUpdate(BaseModel):
    """只处理请求里出现的字段；outcome / level / category / explanation 传 null 表示清空。"""

    in_bank: bool | None = None
    verified: bool | None = None
    outcome: Literal["violation", "clean"] | None = None
    level: int | None = Field(default=None, ge=0, le=4)
    category: str | None = Field(default=None, max_length=40)
    explanation: str | None = Field(default=None, max_length=2000)


class AdminExamRead(BaseModel):
    id: int
    user_id: int
    user: UserPublic | None = None
    score: int
    total: int
    passed: bool
    # 超时未交，按 0 分记的
    timed_out: bool = False
    started_at: datetime | None = None
    submitted_at: datetime | None = None


class AdminExamPage(BaseModel):
    items: list[AdminExamRead] = Field(default_factory=list)
    total: int = 0
