"""聊天图片直传的上传单（docs/chat-features.md「图片直传」）。

开单时发两条预签名 PUT（主图 + 缩略图，直接指向最终的 key），complete 校验通过后删掉这一行；
1 小时内没 complete 的由 services/image_uploads.py 的 worker 连同桶里可能已经传上去的对象一起删。
user_id 不建外键：用户被删时上传单照样按过期清理。
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ImageUpload(Base):
    __tablename__ = "image_uploads"
    __table_args__ = (
        Index("ix_image_uploads_expires", "expires_at"),
        Index("ix_image_uploads_user", "user_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer)
    # 上传到哪个目录（现在只有 messages）：决定图片限制，complete 时按它重新取
    folder: Mapped[str] = mapped_column(String(40))
    storage_key: Mapped[str] = mapped_column(String(300))
    thumbnail_key: Mapped[str] = mapped_column(String(300))
    content_type: Mapped[str] = mapped_column(String(40))
    size: Mapped[int] = mapped_column(Integer)
    thumbnail_size: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
