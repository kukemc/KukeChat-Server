from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
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

    votes_total: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # 与最终结论一致 / 已出结论的票数
    votes_agreed: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    votes_settled: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    golden_total: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    golden_correct: Mapped[int] = mapped_column(Integer, default=0, server_default="0")


class JuryVote(Base):
    """一行 = 一次分配。vote 为空表示已分配但还没投。"""

    __tablename__ = "jury_votes"
    __table_args__ = (
        UniqueConstraint("case_id", "juror_id", name="uq_jury_votes_case_juror"),
        Index("ix_jury_votes_juror_voted", "juror_id", "voted_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("report_cases.id", ondelete="CASCADE"), index=True)
    juror_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    # none（不违规）/ l1 / l2 / l3 / abstain（看不懂）
    vote: Mapped[str | None] = mapped_column(String(10), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    assigned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    voted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 案件出结论后回填：这一票是否与结论一致
    agreed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
