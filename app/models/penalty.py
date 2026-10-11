from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base

UTC = timezone.utc


def utc_now() -> datetime:
    return datetime.now(UTC)


class UserViolation(Base):
    """违规台账：一行 = 一次违规认定 + 它触发的处罚。

    所有处罚来源（自动审核、举报、风纪委员、管理员手动）都只往这里写，由
    services/penalties.py 统一计算阶梯。封禁 / 禁言在 users 表上的字段只是这张表
    的派生状态，方便鉴权时不用再查一次。
    """

    __tablename__ = "user_violations"
    __table_args__ = (
        Index("ix_user_violations_user_status_expires", "user_id", "status", "expires_at"),
        Index("ix_user_violations_appeal_status", "appeal_status", "appealed_at"),
        Index("ix_user_violations_review_status", "review_status", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)

    category: Mapped[str] = mapped_column(String(40))
    level: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    points: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # auto_rule / auto_ai / auto_image / report / jury / admin
    source: Mapped[str] = mapped_column(String(20))
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)

    # 违规内容的定位。target_type 取 message / post / post_comment / post_topic /
    # teamup_profile / teamup_comment / profile_cover / user。
    target_type: Mapped[str | None] = mapped_column(String(30), nullable=True)
    target_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    conversation_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    moderation_record_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    report_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    case_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    # summary 是给当事人看的违规内容摘要，reason 是判定理由。
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # active / revoked。过期不改状态，靠 expires_at 过滤。
    status: Mapped[str] = mapped_column(String(12), default="active", server_default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    revoke_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # none / notice / warning / mute / ban
    penalty_kind: Mapped[str] = mapped_column(String(12), default="none", server_default="none")
    penalty_ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    penalty_permanent: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    points_total_after: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

    # 机器和风纪委员的处罚有上限。阶梯算出来更重时先按上限执行，
    # review_status=pending 等管理员确认，recommended_penalty 记下原本该给的档位。
    # none / pending / confirmed / adjusted
    review_status: Mapped[str] = mapped_column(String(12), default="none", server_default="none")
    recommended_penalty: Mapped[str | None] = mapped_column(String(40), nullable=True)
    reviewed_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # none / pending / accepted / rejected。每条处罚只能申诉一次。
    appeal_status: Mapped[str] = mapped_column(String(12), default="none", server_default="none")
    appeal_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    appealed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    appeal_reviewer_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    appeal_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    appeal_reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped["User"] = relationship(foreign_keys=[user_id])
    actor: Mapped["User | None"] = relationship(foreign_keys=[actor_id])
