from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, Enum as SAEnum, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.enums import InviteTargetType


UTC = timezone.utc


def utc_now() -> datetime:
    return datetime.now(UTC)


class InviteLink(Base):
    __tablename__ = "invite_links"
    __table_args__ = (UniqueConstraint("target_type", "target_id", name="uq_invite_link_target"),)

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    token: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    target_type: Mapped[InviteTargetType] = mapped_column(SAEnum(InviteTargetType, native_enum=False), index=True)
    target_id: Mapped[int] = mapped_column(Integer, index=True)
    created_by_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)
