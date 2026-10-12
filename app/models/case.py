from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

UTC = timezone.utc


def utc_now() -> datetime:
    return datetime.now(UTC)


class ReportCase(Base):
    """举报案件：同一个目标的所有举报合并成一个案件，只审一次。

    证据沿用首条举报提交时固化的快照（user_reports.target_snapshot），案件本身不存内容。
    """

    __tablename__ = "report_cases"
    __table_args__ = (
        Index("ix_report_cases_queue_status_priority", "queue", "status", "priority"),
        Index("ix_report_cases_target_key_status", "target_key", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # 目标的规范化键：message:<id> / post:<id> / conversation:<id> / user:<id>[@<conversation_id>]
    target_key: Mapped[str] = mapped_column(String(80))
    target_type: Mapped[str] = mapped_column(String(20))
    target_id: Mapped[int] = mapped_column(Integer)
    conversation_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    message_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    reported_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    # 快照来源：案件里的第一条举报
    first_report_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # open / resolved（认定违规）/ dismissed（不违规）
    status: Mapped[str] = mapped_column(String(12), default="open", server_default="open")
    # triage（等机器预审）/ jury / admin
    queue: Mapped[str] = mapped_column(String(12), default="triage", server_default="triage")
    # 私聊内容不给风纪委员看，只有群聊消息和公开内容才能进 jury 队列
    jury_eligible: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")

    report_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    reporter_weight: Mapped[float] = mapped_column(Float, default=0.0, server_default="0")
    primary_reason: Mapped[str | None] = mapped_column(String(120), nullable=True)
    priority: Mapped[float] = mapped_column(Float, default=0.0, server_default="0")

    machine_decision: Mapped[str | None] = mapped_column(String(12), nullable=True)
    machine_category: Mapped[str | None] = mapped_column(String(40), nullable=True)
    machine_level: Mapped[int | None] = mapped_column(Integer, nullable=True)
    machine_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)

    # auto / jury / admin
    verdict_source: Mapped[str | None] = mapped_column(String(12), nullable=True)
    verdict_category: Mapped[str | None] = mapped_column(String(40), nullable=True)
    verdict_level: Mapped[int | None] = mapped_column(Integer, nullable=True)
    verdict_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    verdict_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    violation_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 作为风纪委员的质控题：结论已由管理员给出，混在正常案件里发给委员
    is_golden: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    # 题库（is_golden 表示在题库里）。已核对的题才用于入门考试和评审中的质控计分，未核对的只用于练习
    golden_verified: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    # 在题库里：admin（管理员标注）/ consensus（委员一致通过后自动入库）/ legacy（上线前回填，为空也按它算）。
    # 不在题库里：removed（管理员移出，不再自动入库）/ excluded（自动入库检查过，不符合条件）/ 空
    golden_source: Mapped[str | None] = mapped_column(String(12), nullable=True)
    golden_explanation: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 管理员改过的标准答案：violation / clean、等级、类别。为空取案件自身的结论
    golden_outcome: Mapped[str | None] = mapped_column(String(12), nullable=True)
    golden_level: Mapped[int | None] = mapped_column(Integer, nullable=True)
    golden_category: Mapped[str | None] = mapped_column(String(40), nullable=True)

    # 进入委员队列的时间：超时从这里算起，不被后续举报重置。为空表示从没进过委员队列
    queued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 离开委员队列的方式：consensus（委员定案）/ no_consensus / need_context（上下文不足）/ timeout / manual（管理员接手）
    jury_outcome: Mapped[str | None] = mapped_column(String(16), nullable=True)
    escalated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 不能交给委员的原因，目前只有 sensitive（色情低俗 / 涉未成年类）
    jury_block_reason: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # 结案时固化：有委员真实投过票、且结论不是机器给的，才可能进公示栏
    publishable: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    # 建案时固化：只有公开动态的内容可以公示
    content_publishable: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    # 定案后被改判（管理员改判、撤销处罚、申诉通过）
    overturned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    overturned_by_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    overturn_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class JuryMember(Base):
    __tablename__ = "jury_members"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), unique=True, index=True)
    # applied / active / rejected / suspended / retired
    status: Mapped[str] = mapped_column(String(12), default="applied", server_default="applied", index=True)
    statement: Mapped[str | None] = mapped_column(Text, nullable=True)
    applied_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reviewed_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    review_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    term_ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # trainee（见习）/ regular（正式）/ senior（资深）
    tier: Mapped[str] = mapped_column(String(12), default="trainee", server_default="trainee")

    votes_total: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # 与最终结论一致 / 已出结论的票数
    votes_agreed: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    votes_settled: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    golden_total: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    golden_correct: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

    # 风纪值总分，明细在 jury_point_logs
    points: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # 本人是否选择公开展示委员身份；对外展示的结果冗余在 users.jury_badge
    badge_visible: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    # 本次任职的开始时间（任命 / 复职时写入），晋升条件里的「在任天数」从这里算
    serving_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 连续参与天数（UTC+8 自然日）和最近一次参与是哪天
    streak_days: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    streak_last_day: Mapped[date | None] = mapped_column(Date, nullable=True)
    # 弃权过多被暂停发案到什么时候
    cooldown_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 距上一道质控题投了几张票；在质控题上回避后下一件强制补一道
    votes_since_quality: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    force_quality: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")


class JuryVote(Base):
    """一行 = 一次分配。vote 为空表示已分配但还没投；主动回避的 vote 是 recuse、voted_at 仍为空。"""

    __tablename__ = "jury_votes"
    __table_args__ = (
        UniqueConstraint("case_id", "juror_id", name="uq_jury_votes_case_juror"),
        Index("ix_jury_votes_juror_voted", "juror_id", "voted_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("report_cases.id", ondelete="CASCADE"), index=True)
    juror_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    # none（不违规）/ l1 / l2 / l3 / abstain（看不懂）/ need_context（上下文不足）/ recuse（主动回避）
    vote: Mapped[str | None] = mapped_column(String(10), nullable=True)
    # 违规票可以带的类别；没带为空
    category: Mapped[str | None] = mapped_column(String(40), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    assigned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    voted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 案件出结论后回填：这一票是否与结论一致
    agreed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # 投票时分配的代号（委员 A / 委员 B …），同一案件内稳定；质控题不分配
    alias: Mapped[str | None] = mapped_column(String(12), nullable=True)
    # 这次分配是不是质控题。不能用「案件 is_golden 且已结」去猜：真实案件被管理员结案后也会变成 is_golden
    is_quality_check: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    # 主动回避的时间。回避不算投票（voted_at 保持为空），这一行留着是为了不再把同一个案件分给他
    recused_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class JuryOpinion(Base):
    """案件观点区的一条观点或回复。只对投过这个案件的委员可见，展示时用代号。"""

    __tablename__ = "jury_opinions"
    __table_args__ = (Index("ix_jury_opinions_author_created", "author_id", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("report_cases.id", ondelete="CASCADE"), index=True)
    author_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    # 回复只有一层：parent_id 恒指向根观点，reply_to_id 是实际被回复的那条（回复别人的回复时才有）
    parent_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reply_to_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    content: Mapped[str] = mapped_column(Text)
    # 投票时写的备注
    is_note: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    like_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # visible / deleted（本人或管理员删除）/ hidden（多人举报后折叠，等管理员处理）
    state: Mapped[str] = mapped_column(String(12), default="visible", server_default="visible")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_by_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    moderation_note: Mapped[str | None] = mapped_column(Text, nullable=True)


class JuryOpinionLike(Base):
    __tablename__ = "jury_opinion_likes"
    __table_args__ = (UniqueConstraint("opinion_id", "user_id", name="uq_jury_opinion_likes_opinion_user"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    opinion_id: Mapped[int] = mapped_column(ForeignKey("jury_opinions.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class JuryOpinionReport(Base):
    __tablename__ = "jury_opinion_reports"
    __table_args__ = (UniqueConstraint("opinion_id", "reporter_id", name="uq_jury_opinion_reports_opinion_reporter"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    opinion_id: Mapped[int] = mapped_column(ForeignKey("jury_opinions.id", ondelete="CASCADE"), index=True)
    reporter_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    reason: Mapped[str] = mapped_column(String(40))
    details: Mapped[str | None] = mapped_column(Text, nullable=True)
    # pending / handled（观点被删除）/ dismissed（举报不成立）
    status: Mapped[str] = mapped_column(String(12), default="pending", server_default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    handled_by_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    handled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class JuryPointLog(Base):
    """风纪值流水。总分汇总在 jury_members.points，加减分一律走 jury_points.award。"""

    __tablename__ = "jury_point_logs"
    __table_args__ = (
        Index("ix_jury_point_logs_user_created", "user_id", "created_at"),
        Index("ix_jury_point_logs_user_kind_case", "user_id", "kind", "case_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    # vote_agreed / vote_disagreed / quality_correct / quality_wrong / daily_first / streak_7 /
    # opinion_liked / exam_passed / overturn_adjust / admin_adjust
    kind: Mapped[str] = mapped_column(String(24))
    # 实际变动的分值：总分不会低于 0，扣到 0 为止
    points: Mapped[int] = mapped_column(Integer)
    case_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 同一个案件里还要再区分时用：daily_first / streak_7 是日期序数，opinion_liked 是观点 id
    ref_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 管理员调整时写的说明
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class JuryAchievement(Base):
    """已获得的成就。定义在 jury_points.ACHIEVEMENTS，进度实时算，这里只存获得时间。"""

    __tablename__ = "jury_achievements"
    __table_args__ = (UniqueConstraint("user_id", "key", name="uq_jury_achievements_user_key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    key: Mapped[str] = mapped_column(String(24))
    earned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class JuryExamAttempt(Base):
    """入门考试的一场。服务端不保存作答草稿，交卷时一次写入。"""

    __tablename__ = "jury_exam_attempts"
    __table_args__ = (Index("ix_jury_exam_attempts_user_started", "user_id", "started_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # 交卷时间；超时未交的在结算时记成截止时间。为空表示还在进行中
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 题目的案件 id 列表（JSON），下标 + 1 就是给考生看的题号
    question_ids: Mapped[str] = mapped_column(Text)
    # 作答（JSON：题号 -> vote）
    answers: Mapped[str | None] = mapped_column(Text, nullable=True)
    score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    passed: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    # 超时未交，按 0 分记了一次
    timed_out: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")


class JuryPracticeAnswer(Base):
    """练习场的一次作答。不计入一致率，不给风纪值。"""

    __tablename__ = "jury_practice_answers"
    __table_args__ = (
        Index("ix_jury_practice_answers_user_created", "user_id", "created_at"),
        Index("ix_jury_practice_answers_user_case", "user_id", "case_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    case_id: Mapped[int] = mapped_column(ForeignKey("report_cases.id", ondelete="CASCADE"))
    vote: Mapped[str] = mapped_column(String(10))
    correct: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class JuryNotification(Base):
    """给委员本人的系统通知：晋升、降级、被系统暂停、任期届满未能续任。通知中心从这里读。"""

    __tablename__ = "jury_notifications"
    __table_args__ = (Index("ix_jury_notifications_user_created", "user_id", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    # promoted / demoted / suspended / term_expired
    kind: Mapped[str] = mapped_column(String(24))
    title: Mapped[str] = mapped_column(String(120))
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
