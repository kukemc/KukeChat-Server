from __future__ import annotations

from datetime import datetime, timezone

UTC = timezone.utc

from sqlalchemy import Boolean, DateTime, Enum as SAEnum, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.enums import ModerationAction, ModerationSource, PasswordResetStatus, ReportStatus, ReportTargetType


def utc_now() -> datetime:
    return datetime.now(UTC)


class UserReport(Base):
    __tablename__ = "user_reports"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    reporter_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    target_type: Mapped[ReportTargetType] = mapped_column(SAEnum(ReportTargetType, native_enum=False), index=True)
    target_id: Mapped[int] = mapped_column(Integer, index=True)
    conversation_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    message_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    reported_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    reason: Mapped[str] = mapped_column(String(120))
    details: Mapped[str | None] = mapped_column(Text, nullable=True)
    attachments: Mapped[str | None] = mapped_column(Text, nullable=True)
    target_snapshot: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    status: Mapped[ReportStatus] = mapped_column(
        SAEnum(ReportStatus, native_enum=False), default=ReportStatus.PENDING, server_default="PENDING", index=True
    )
    reviewer_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    review_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    action: Mapped[str | None] = mapped_column(String(40), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 同一目标的举报并成一个案件（report_cases），结论按案件统一回写。
    case_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    # 被认定为恶意 / 无效举报，会拉低举报人的信誉和每日额度。
    malicious: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")

    reporter: Mapped["User"] = relationship(back_populates="submitted_reports", foreign_keys=[reporter_id])
    reviewer: Mapped["User | None"] = relationship(back_populates="reviewed_reports", foreign_keys=[reviewer_id])


class AdminAuditLog(Base):
    __tablename__ = "admin_audit_logs"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(80), index=True)
    target_type: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    target_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    details: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)

    actor: Mapped["User | None"] = relationship(foreign_keys=[actor_id])


class ContentModerationRecord(Base):
    __tablename__ = "content_moderation_records"

    # 这张表每条消息写一行，索引越多写入越慢。下面几列刻意不建索引：
    # id 与 rowid 主键重复；source / content_type / action_taken / severity
    # 都只有个位数个不同值，过滤时扫表比走索引还快。
    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[ModerationSource] = mapped_column(SAEnum(ModerationSource, native_enum=False))
    message_id: Mapped[int | None] = mapped_column(ForeignKey("messages.id", ondelete="SET NULL"), nullable=True, index=True)
    post_id: Mapped[int | None] = mapped_column(ForeignKey("posts.id", ondelete="SET NULL"), nullable=True, index=True)
    post_topic_id: Mapped[int | None] = mapped_column(ForeignKey("post_topics.id", ondelete="SET NULL"), nullable=True, index=True)
    post_comment_id: Mapped[int | None] = mapped_column(ForeignKey("post_comments.id", ondelete="SET NULL"), nullable=True, index=True)
    teamup_profile_id: Mapped[int | None] = mapped_column(ForeignKey("teamup_profiles.id", ondelete="SET NULL"), nullable=True, index=True)
    teamup_comment_id: Mapped[int | None] = mapped_column(ForeignKey("teamup_comments.id", ondelete="SET NULL"), nullable=True, index=True)
    profile_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    report_id: Mapped[int | None] = mapped_column(ForeignKey("user_reports.id", ondelete="SET NULL"), nullable=True, index=True)
    conversation_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    content_type: Mapped[str] = mapped_column(String(20))
    content_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    decision: Mapped[str] = mapped_column(String(20), index=True)
    severity: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    categories: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_response: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence_snapshot: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    action_taken: Mapped[ModerationAction] = mapped_column(
        SAEnum(ModerationAction, native_enum=False), default=ModerationAction.NONE, server_default="NONE"
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
    acted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 人工复核结论：violation / clean。既是模型拿不准时的最终裁决，也是评测模型用的标注。
    human_verdict: Mapped[str | None] = mapped_column(String(12), nullable=True)
    human_category: Mapped[str | None] = mapped_column(String(40), nullable=True)
    human_level: Mapped[int | None] = mapped_column(Integer, nullable=True)
    human_by_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    human_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 二审（docs/moderation-v2.md 1.10），只有文本消息的记录会有：违规概率、审核服务报的耗时（含排队）、结果。
    # judge_outcome：pending 等二审 / confirmed 一审 block 且二审确认 / downgraded 一审 block 但二审不够把握（不处罚）/
    # cleared 一审拿不准、二审认为没问题（放行）/ flagged 一审拿不准、二审认为值得人看 / scored 举报复审只打分 /
    # unavailable 二审不可用，照一审结论办。没送二审为空
    judge_p: Mapped[float | None] = mapped_column(Float, nullable=True)
    judge_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    judge_outcome: Mapped[str | None] = mapped_column(String(12), nullable=True)

    message: Mapped["Message | None"] = relationship(foreign_keys=[message_id])
    post: Mapped["Post | None"] = relationship(foreign_keys=[post_id])
    post_topic: Mapped["PostTopic | None"] = relationship(foreign_keys=[post_topic_id])
    post_comment: Mapped["PostComment | None"] = relationship(foreign_keys=[post_comment_id])
    teamup_profile: Mapped["TeamupProfile | None"] = relationship(foreign_keys=[teamup_profile_id])
    teamup_comment: Mapped["TeamupComment | None"] = relationship(foreign_keys=[teamup_comment_id])
    profile_user: Mapped["User | None"] = relationship(foreign_keys=[profile_user_id])
    report: Mapped["UserReport | None"] = relationship(foreign_keys=[report_id])
    user: Mapped["User | None"] = relationship(foreign_keys=[user_id])


class PasswordResetRequest(Base):
    __tablename__ = "password_reset_requests"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    username: Mapped[str] = mapped_column(String(50), index=True)
    account_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    ccw_profile_url: Mapped[str] = mapped_column(String(500))
    reason: Mapped[str] = mapped_column(Text)
    requested_password: Mapped[str] = mapped_column(String(128))
    request_ip: Mapped[str] = mapped_column(String(45), index=True)
    account_registration_ip: Mapped[str | None] = mapped_column(String(45), nullable=True)
    status: Mapped[PasswordResetStatus] = mapped_column(
        SAEnum(PasswordResetStatus, native_enum=False), default=PasswordResetStatus.PENDING, server_default="PENDING", index=True
    )
    reviewer_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    review_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    account_user: Mapped["User | None"] = relationship(foreign_keys=[account_user_id])
    reviewer: Mapped["User | None"] = relationship(foreign_keys=[reviewer_id])
