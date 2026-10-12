"""风纪系统 v2 三期：判例和议事厅的接口结构，契约见 docs/jury-v2.md 3.4、3.5、3.8。

这个文件不能反过来导入 app.schemas.jury —— JuryConfig 要从这里拿 JuryHallRules。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.case import JuryCaseMessage, JuryCasePost
from app.schemas.user import UserPublic

# 长度上限是配置项，超了由服务端返回带 code 的 422；这里只挡住离谱的请求体。
_TEXT_HARD_LIMIT = 100000


# ---------------------------------------------------------------- 判例


class Precedent(BaseModel):
    id: int
    # 案件的公开编号
    case_id: int
    title: str
    summary: str
    kind: Literal["message", "post"]
    outcome: Literal["violation", "clean"]
    category: str | None = None
    category_label: str | None = None
    level: int | None = None
    # senior 资深委员标的 / admin 管理员标的
    by: Literal["senior", "admin"]
    # 是不是我创建的（能否编辑 / 撤销）
    mine: bool = False
    created_at: datetime | None = None


class PrecedentPage(BaseModel):
    items: list[Precedent] = Field(default_factory=list)
    total: int = 0


class PrecedentContent(BaseModel):
    """和公示栏详情的 content 同形。"""

    text: str | None = None
    image_count: int = 0
    # 带 Z 的 ISO 字符串（UTC）
    created_at: str | None = None


class PrecedentCase(BaseModel):
    """匿名化的案件内容，只给在任委员。"""

    messages: list[JuryCaseMessage] = Field(default_factory=list)
    post: JuryCasePost | None = None


class PrecedentDetail(Precedent):
    content: PrecedentContent | None = None
    # 公示栏的那几种（group_message / clean / sensitive / overturned），外加 unpublished：案件此刻不在公示栏里；有内容时为 null
    content_hidden_reason: str | None = None
    # 在任委员才有
    case: PrecedentCase | None = None


class PrecedentCreate(BaseModel):
    # 委员侧是评审编号，后台是案件的公开编号（docs/jury-v2.md 1.0）
    case_id: int
    title: str = Field(max_length=_TEXT_HARD_LIMIT)
    summary: str = Field(max_length=_TEXT_HARD_LIMIT)


class PrecedentUpdate(BaseModel):
    """只改请求里出现的字段。"""

    title: str | None = Field(default=None, max_length=_TEXT_HARD_LIMIT)
    summary: str | None = Field(default=None, max_length=_TEXT_HARD_LIMIT)


class PrecedentState(BaseModel):
    id: int
    status: Literal["active", "withdrawn"]


class AdminPrecedentRead(Precedent):
    """后台看到的判例：含已撤下的，带创建人。"""

    status: Literal["active", "withdrawn"]
    created_by_id: int | None = None
    created_by: UserPublic | None = None
    updated_at: datetime | None = None
    withdrawn_at: datetime | None = None
    # 为空且已撤下：案件被改判后系统撤下的
    withdrawn_by_id: int | None = None
    withdraw_note: str | None = None
    # 案件此刻是否被改判（被改判的判例对外不可见）
    overturned: bool = False
    # 案件此刻在不在题库里 / 是不是因为这条判例才进去的
    in_bank: bool = False
    bank_added: bool = False


class AdminPrecedentPage(BaseModel):
    items: list[AdminPrecedentRead] = Field(default_factory=list)
    total: int = 0


class AdminPrecedentWithdraw(BaseModel):
    note: str | None = Field(default=None, max_length=500)


# ---------------------------------------------------------------- 议事厅：配置


class JuryHallRules(BaseModel):
    title_max: int
    content_max: int
    comment_max: int
    posts_per_day: int
    comments_per_day: int
    vote_days: int
    quorum_ratio: float
    quorum_min: int
    # 通过线：服务端按整数比较 yes × pass_den >= (yes + no) × pass_num；pass_ratio 只是给展示用的近似值
    pass_num: int
    pass_den: int
    pass_ratio: float
    # 要展示的守则，纯文本，含换行
    guidelines: str
    report_reasons: list[str]
    # 帖子或评论被多少名在任委员举报后自动折叠（其他用户的举报只记录，不计入）
    auto_hide_reports: int = 3
    # 每人最近 24 小时最多举报多少次
    reports_per_day: int = 20


# ---------------------------------------------------------------- 议事厅：帖子


class HallVote(BaseModel):
    yes: int = 0
    no: int = 0
    ends_at: datetime | None = None
    my_vote: Literal["yes", "no"] | None = None
    can_vote: bool = False
    # 这个提案需要的最少表决人数，发帖时固定
    quorum: int
    pass_num: int
    pass_den: int


class HallPost(BaseModel):
    id: int
    kind: Literal["announcement", "proposal", "discussion"]
    title: str
    # 列表里是前 200 字，详情里是全文；被折叠（state 为 hidden）时为 null
    content: str | None = None
    # 列表里的正文是不是被截断过
    content_truncated: bool = False
    author: UserPublic | None = None
    # 公告为 null；作者现在不是在任委员时也为 null
    author_tier: str | None = None
    status: Literal["open", "passed", "rejected", "adopted", "declined", "closed"]
    # visible / hidden（多人举报后折叠，等管理员处理）。已删除的帖子不会出现
    state: Literal["visible", "hidden"] = "visible"
    pinned: bool = False
    # 非提案为 null
    vote: HallVote | None = None
    admin_note: str | None = None
    comment_count: int = 0
    can_comment: bool = False
    mine: bool = False
    can_edit: bool = False
    can_delete: bool = False
    # 我是否举报过
    reported: bool = False
    created_at: datetime | None = None
    updated_at: datetime | None = None


class HallPostPage(BaseModel):
    items: list[HallPost] = Field(default_factory=list)
    total: int = 0


class HallPostCreate(BaseModel):
    # proposal / discussion；公告只能在后台发
    kind: str = Field(max_length=20)
    title: str = Field(max_length=_TEXT_HARD_LIMIT)
    content: str = Field(max_length=_TEXT_HARD_LIMIT)


class HallPostUpdate(BaseModel):
    """只改请求里出现的字段。"""

    title: str | None = Field(default=None, max_length=_TEXT_HARD_LIMIT)
    content: str | None = Field(default=None, max_length=_TEXT_HARD_LIMIT)


class HallPostState(BaseModel):
    id: int
    state: Literal["visible", "deleted", "hidden"]


class HallVoteCreate(BaseModel):
    # yes / no
    vote: str = Field(max_length=10)


# ---------------------------------------------------------------- 议事厅：评论与举报


class HallComment(BaseModel):
    id: int
    # 回复只有一层：恒指向根评论；根评论为 null
    parent_id: int | None = None
    author: UserPublic | None = None
    author_tier: str | None = None
    # 非 visible 时为 null
    content: str | None = None
    state: Literal["visible", "deleted", "hidden"]
    mine: bool = False
    reported: bool = False
    created_at: datetime | None = None
    # 回复别人的回复时，被回复的那个人；直接回复根评论时为 null
    reply_to: UserPublic | None = None


class HallCommentPage(BaseModel):
    items: list[HallComment] = Field(default_factory=list)
    total: int = 0


class HallCommentCreate(BaseModel):
    content: str = Field(max_length=_TEXT_HARD_LIMIT)
    parent_id: int | None = None


class HallCommentState(BaseModel):
    id: int
    state: Literal["visible", "deleted", "hidden"]


class HallReportCreate(BaseModel):
    target_type: Literal["post", "comment"]
    target_id: int
    reason: str = Field(max_length=40)
    details: str | None = Field(default=None, max_length=2000)


class HallReportResult(BaseModel):
    target_type: Literal["post", "comment"]
    target_id: int
    # 举报之后目标的状态：达到阈值会变成 hidden
    state: Literal["visible", "deleted", "hidden"]


# ---------------------------------------------------------------- 议事厅：后台


class AdminHallAnnouncementCreate(BaseModel):
    title: str = Field(max_length=_TEXT_HARD_LIMIT)
    content: str = Field(max_length=_TEXT_HARD_LIMIT)
    pinned: bool = False


class AdminHallDecision(BaseModel):
    action: Literal["adopt", "decline", "close", "delete", "pin", "unpin"]
    note: str | None = Field(default=None, max_length=2000)


class AdminHallPostRead(BaseModel):
    """后台看到的帖子：含已删除 / 被折叠的，正文始终是原文。"""

    id: int
    kind: Literal["announcement", "proposal", "discussion"]
    title: str
    content: str
    author_id: int
    author: UserPublic | None = None
    author_tier: str | None = None
    status: Literal["open", "passed", "rejected", "adopted", "declined", "closed"]
    state: Literal["visible", "deleted", "hidden"]
    pinned: bool = False
    # 非提案为 null：{ yes, no, ends_at, quorum, pass_num, pass_den }
    vote: dict[str, int | datetime | None] | None = None
    settled_at: datetime | None = None
    admin_note: str | None = None
    decided_by_id: int | None = None
    decided_at: datetime | None = None
    comment_count: int = 0
    pending_reports: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None
    last_activity_at: datetime | None = None
    deleted_at: datetime | None = None
    deleted_by_id: int | None = None
    moderation_note: str | None = None


class AdminHallPostPage(BaseModel):
    items: list[AdminHallPostRead] = Field(default_factory=list)
    total: int = 0


class AdminHallCommentRead(BaseModel):
    id: int
    post_id: int
    parent_id: int | None = None
    reply_to_id: int | None = None
    author_id: int
    author: UserPublic | None = None
    content: str
    state: Literal["visible", "deleted", "hidden"]
    created_at: datetime | None = None
    deleted_at: datetime | None = None
    deleted_by_id: int | None = None
    moderation_note: str | None = None


class AdminHallCommentModerate(BaseModel):
    # delete 删除 / restore 恢复显示（被折叠的、或被管理员删掉的；作者自己删的不能恢复）
    action: Literal["delete", "restore"]
    note: str | None = Field(default=None, max_length=500)


class AdminHallPostDetail(AdminHallPostRead):
    # 全部评论，平铺、时间正序，含已删除 / 被折叠的原文
    comments: list[AdminHallCommentRead] = Field(default_factory=list)


class AdminHallReportTarget(BaseModel):
    """被举报的帖子或评论此刻的样子。"""

    type: Literal["post", "comment"]
    id: int
    post_id: int
    post_title: str | None = None
    # 帖子的 kind；评论为所在帖子的 kind
    kind: str | None = None
    content: str | None = None
    state: Literal["visible", "deleted", "hidden"] | None = None
    author_id: int | None = None
    author: UserPublic | None = None
    # 这个目标还有多少条待处理的举报
    pending_reports: int = 0


class AdminHallReportRead(BaseModel):
    id: int
    target_type: Literal["post", "comment"]
    target_id: int
    # 目标已经不存在时为 null
    target: AdminHallReportTarget | None = None
    reporter_id: int
    reporter: UserPublic | None = None
    reason: str
    details: str | None = None
    status: Literal["pending", "handled", "dismissed"]
    created_at: datetime | None = None
    handled_by_id: int | None = None
    handled_at: datetime | None = None
    handle_note: str | None = None


class AdminHallReportPage(BaseModel):
    items: list[AdminHallReportRead] = Field(default_factory=list)
    total: int = 0


class AdminHallReportHandle(BaseModel):
    # delete 删除被举报的帖子 / 评论；dismiss 举报不成立（被折叠的会恢复显示）
    action: Literal["delete", "dismiss"]
    note: str | None = Field(default=None, max_length=500)
