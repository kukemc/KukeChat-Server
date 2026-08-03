from __future__ import annotations

from datetime import datetime, timezone

UTC = timezone.utc

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


def utc_now() -> datetime:
    return datetime.now(UTC)


class StickerAsset(Base):
    __tablename__ = "sticker_assets"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    source_path: Mapped[str] = mapped_column(String(500), unique=True, index=True)
    storage_path: Mapped[str] = mapped_column(String(500), unique=True)
    content_type: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    favorites: Mapped[list["FavoriteSticker"]] = relationship(back_populates="asset")


class FavoriteSticker(Base):
    __tablename__ = "favorite_stickers"
    __table_args__ = (UniqueConstraint("user_id", "asset_id", name="uq_favorite_sticker_user_asset"),)

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    asset_id: Mapped[int] = mapped_column(ForeignKey("sticker_assets.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    user: Mapped["User"] = relationship(back_populates="favorite_stickers")
    asset: Mapped[StickerAsset] = relationship(back_populates="favorites")
