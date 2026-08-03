from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import Date, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base

UTC = timezone.utc


def utc_now() -> datetime:
    return datetime.now(UTC)


class GroupCheckin(Base):
    __tablename__ = "group_checkins"
    __table_args__ = (UniqueConstraint("conversation_id", "user_id", "checkin_date", name="uq_group_checkin_user_date"),)

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    checkin_date: Mapped[date] = mapped_column(Date, index=True)
    streak_days: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    exp_awarded: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    message: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)

    user: Mapped["User"] = relationship(foreign_keys=[user_id])


class GroupMemberDailyActivity(Base):
    __tablename__ = "group_member_daily_activity"
    __table_args__ = (UniqueConstraint("conversation_id", "user_id", "activity_date", name="uq_group_member_daily_activity"),)

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    activity_date: Mapped[date] = mapped_column(Date, index=True)
    message_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    checkin_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    exp_from_messages: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    exp_from_checkins: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    exp_from_activities: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    activity_score: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class GroupMemberExpLog(Base):
    __tablename__ = "group_member_exp_logs"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    source: Mapped[str] = mapped_column(String(30), index=True)
    delta: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str | None] = mapped_column(String(120), nullable=True)
    related_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
