"""风纪系统 v2 三期里资深委员那部分的接口结构：资深合议、申诉复审、举报人复议。契约见 docs/jury-v2.md 3.1、3.2、3.7、3.8。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.schemas.case import JuryCaseMessage, JuryCasePost, JuryCaseView, JuryReward
from app.schemas.jury import JuryOpinion
from app.schemas.user import UserPublic


# ---------------------------------------------------------------- 资深合议


class PanelOrigin(BaseModel):
    """原审的情况。"""

    # 为什么转出：no_consensus / need_context / timeout
    reason: str | None = None
    # 原审的票数分布：{ none, l1, l2, l3, abstain, need_context }
    tally: dict[str, int]
    # 原审的观点区，只读；这里每条都带 vote
    opinions: list[JuryOpinion] = Field(default_factory=list)


class PanelProgress(BaseModel):
    seats: int
    # 已经投出的合议票数
    votes_cast: int = 0
    # 合议的期限：到这时还没定案就转管理员
    expires_at: datetime | None = None


class PanelCaseView(JuryCaseView):
    origin: PanelOrigin
    panel: PanelProgress


class PanelNextCase(BaseModel):
    # 没有可分给我的合议案件时为 null
    case: PanelCaseView | None = None


# ---------------------------------------------------------------- 申诉复审 / 举报人复议


class ReviewPenalty(BaseModel):
    # none / notice / warning / mute / ban
    kind: str
    # 禁言 / 封禁的时长（小时）；其它处置为 null
    hours: int | None = None


class ReviewContent(BaseModel):
    text: str | None = None
    image_count: int = 0


class ReviewOrigin(BaseModel):
    # 原审的票数分布；进过合议的多一项 panel
    tally: dict[str, Any]
    opinions: list[JuryOpinion] = Field(default_factory=list)


class ReviewVerdict(BaseModel):
    """举报人复议时的原结论。"""

    outcome: Literal["clean"] = "clean"
    # jury / panel
    source: str | None = None
    closed_at: datetime | None = None


class AppealReviewView(BaseModel):
    # 复审本身的 id，不是评审编号
    review_id: int
    # appeal 申诉复审 / reconsider 举报人复议
    type: Literal["appeal", "reconsider"]
    kind: Literal["message", "post"]
    # 原结论是谁给的：jury / panel / machine
    source: str
    # 复议时下面四项为 null
    category: str | None = None
    category_label: str | None = None
    level: int | None = None
    penalty: ReviewPenalty | None = None
    # 来自案件的：匿名上下文，同 JuryCaseView
    messages: list[JuryCaseMessage] = Field(default_factory=list)
    post: JuryCasePost | None = None
    # 机器处罚没有案件快照，只有被处罚的那条内容；来自案件的为 null
    content: ReviewContent | None = None
    # 原审票数分布和观点；机器处罚时为 null
    origin: ReviewOrigin | None = None
    appeal_text: str | None = None
    appealed_at: datetime | None = None
    # 以下三项只有复议有
    verdict: ReviewVerdict | None = None
    reconsider_text: str | None = None
    requested_at: datetime | None = None
    expires_at: datetime | None = None
    seats: int
    votes_cast: int = 0


class AppealNext(BaseModel):
    review: AppealReviewView | None = None


class AppealVoteCreate(BaseModel):
    # 申诉复审：uphold / reduce / revoke；复议：uphold / overturn。是否合法在服务层按复审的类型校验
    vote: Literal["uphold", "reduce", "revoke", "overturn"]
    note: str | None = Field(default=None, max_length=500)
    # 复议投 overturn 时可以带：等级 1–3、类别（config.categories 里的 key）
    level: int | None = Field(default=None, ge=1, le=3)
    category: str | None = Field(default=None, max_length=40)


class AppealVoteResult(BaseModel):
    review_id: int
    # 这件复审是否已经结束
    settled: bool
    # uphold / reduce / revoke / overturn；还没出结果为 null
    outcome: str | None = None
    reward: JuryReward | None = None


class AppealMineItem(BaseModel):
    review_id: int
    type: Literal["appeal", "reconsider"]
    kind: Literal["message", "post"]
    source: str
    # 复议时下面三项为 null
    category_label: str | None = None
    level: int | None = None
    penalty: ReviewPenalty | None = None
    my_vote: str | None = None
    voted_at: datetime | None = None
    # 还没出结果为 null
    outcome: str | None = None
    settled_at: datetime | None = None


class AppealMinePage(BaseModel):
    items: list[AppealMineItem] = Field(default_factory=list)
    total: int = 0


# ---------------------------------------------------------------- 后台


class AdminReviewVote(BaseModel):
    juror: UserPublic | None = None
    tier: str | None = None
    # 分到手还没投的为 null
    vote: str | None = None
    level: int | None = None
    category: str | None = None
    note: str | None = None
    assigned_at: datetime | None = None
    voted_at: datetime | None = None


class AdminReviewRead(BaseModel):
    id: int
    kind: Literal["appeal", "reconsider"]
    # senior 等资深委员投票 / admin 凑不齐人或超时，已转给管理员
    stage: str
    status: Literal["open", "closed"]
    # uphold / reduce / revoke / overturn；没有结果为 null
    outcome: str | None = None
    # 结果是谁给的：senior / admin
    decided_by: str | None = None
    # 转给管理员的原因：timeout / unavailable
    escalation: str | None = None
    violation_id: int | None = None
    case_id: int | None = None
    report_id: int | None = None
    # 申诉人 / 申请复议的举报人
    requester: UserPublic | None = None
    # 申诉理由 / 复议理由
    text: str | None = None
    target_type: str | None = None
    source: str | None = None
    category: str | None = None
    category_label: str | None = None
    level: int | None = None
    penalty: ReviewPenalty | None = None
    created_at: datetime | None = None
    expires_at: datetime | None = None
    escalated_at: datetime | None = None
    closed_at: datetime | None = None
    votes: list[AdminReviewVote] = Field(default_factory=list)


class AdminReviewDecide(BaseModel):
    """管理员给一件举报人复议出结果。申诉复审不走这里（用 POST /admin/violations/{id}/appeal）。"""

    # uphold 维持原结论（不违规）/ overturn 改判为违规
    action: Literal["uphold", "overturn"]
    # 以下两项只在 overturn 时有用：不给时等级按类别的默认值、类别按举报理由映射
    level: int | None = Field(default=None, ge=1, le=3)
    category: str | None = Field(default=None, max_length=40)
    # overturn 时会成为改判说明（出现在公示栏和委员的案件详情里）；uphold 时只留在审计日志里
    note: str | None = Field(default=None, max_length=2000)


class AdminReviewPage(BaseModel):
    items: list[AdminReviewRead] = Field(default_factory=list)
    total: int = 0
