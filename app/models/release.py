"""发版服务的表（docs/releases.md）：应用版本、安装包、发版令牌。"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base

UTC = timezone.utc


def utc_now() -> datetime:
    return datetime.now(UTC)


class AppRelease(Base):
    __tablename__ = "app_releases"
    __table_args__ = (
        UniqueConstraint("platform", "channel", "version", name="uq_app_release_version"),
        Index("ix_app_releases_lookup", "platform", "channel", "published"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    platform: Mapped[str] = mapped_column(String(16))
    channel: Mapped[str] = mapped_column(String(16), default="stable")
    version: Mapped[str] = mapped_column(String(64))
    build: Mapped[int] = mapped_column(Integer, default=0)
    changelog: Mapped[str] = mapped_column(Text, default="")
    min_supported_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    published: Mapped[bool] = mapped_column(Boolean, default=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)

    created_by: Mapped["User | None"] = relationship(foreign_keys=[created_by_id], lazy="joined")  # noqa: F821
    artifacts: Mapped[list["AppReleaseArtifact"]] = relationship(
        back_populates="release", cascade="all, delete-orphan", order_by="AppReleaseArtifact.id", lazy="selectin"
    )


class AppReleaseArtifact(Base):
    __tablename__ = "app_release_artifacts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    release_id: Mapped[int] = mapped_column(ForeignKey("app_releases.id", ondelete="CASCADE"), index=True)
    # upload：放在我们的存储里；external：手填的外部链接
    source: Mapped[str] = mapped_column(String(16), default="upload")
    # pending：直传单开了还没 complete；ready：可用
    status: Mapped[str] = mapped_column(String(16), default="ready")
    file_name: Mapped[str] = mapped_column(String(160))
    storage_key: Mapped[str | None] = mapped_column(String(400), nullable=True)
    external_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    arch: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # 预留给 tauri-plugin-updater：Tauri 构建产出的 .sig 文件内容（公钥签名，不是私钥）
    signature: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    release: Mapped[AppRelease] = relationship(back_populates="artifacts")


class ReleaseApiToken(Base):
    """发版脚本用的令牌：只能调 /admin/releases/*（令牌管理除外）。只存 sha256，明文只在签发时返回一次。"""

    __tablename__ = "release_api_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    label: Mapped[str] = mapped_column(String(80))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    prefix: Mapped[str] = mapped_column(String(16))
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_by: Mapped["User | None"] = relationship(foreign_keys=[created_by_id], lazy="joined")  # noqa: F821
