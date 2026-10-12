from __future__ import annotations

from datetime import datetime, timezone

UTC = timezone.utc

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


def utc_now() -> datetime:
    return datetime.now(UTC)


class Announcement(Base):
    __tablename__ = "announcements"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    title: Mapped[str] = mapped_column(String(200), default="")
    content: Mapped[str] = mapped_column(Text, default="")  # Markdown
    cover_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1", index=True)
    pinned: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)

    created_by: Mapped["User | None"] = relationship(foreign_keys=[created_by_id])


class AnnouncementReaction(Base):
    """用户对全站公告的 emoji 回应。只统计数量，不对外展示是谁回应的。"""

    __tablename__ = "announcement_reactions"
    __table_args__ = (
        UniqueConstraint("announcement_id", "user_id", "emoji", name="uq_announcement_reaction_user_emoji"),
        # 频率限制要数「这个人最近一分钟加了几条」，顺带也是 user_id 外键级联删除用的索引
        Index("ix_announcement_reactions_user_created", "user_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    announcement_id: Mapped[int] = mapped_column(ForeignKey("announcements.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    emoji: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
