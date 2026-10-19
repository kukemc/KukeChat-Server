from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import BigInteger, DateTime, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

UTC = timezone.utc


def utc_now() -> datetime:
    return datetime.now(UTC)


class SelfTransfer(Base):
    """「我的设备」会话里的临时文件（docs/self-chat.md 第 5 节）。一行从开单（pending）到传完（ready）到过期（expired）。

    - pending：开了单还没 complete；upload_expires_at 之前没 complete 的由 worker 连同存储里可能已经传上去的对象一起删掉。
    - ready：complete 过，落了一条文件消息（message_id）；expires_at = complete 时间 + 24 小时，到点之后下载 410。
    - expired：存储里的对象已经删了（到期 / 撤回了那条消息）。行再留一段时间，让下载返回 410 而不是 404，之后删掉。

    和群文件的上传单一样不建外键：会话 / 用户被删了，行还在，worker 照样能找到对象去删，不会漏在存储里。
    """

    __tablename__ = "self_transfers"
    __table_args__ = (
        Index("ix_self_transfers_state_expires", "state", "expires_at"),
        Index("ix_self_transfers_state_upload_expires", "state", "upload_expires_at"),
        Index("ix_self_transfers_owner_state", "owner_id", "state"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(Integer)
    owner_id: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(160))
    size: Mapped[int] = mapped_column(BigInteger)
    content_type: Mapped[str] = mapped_column(String(120), default="application/octet-stream")
    storage_key: Mapped[str] = mapped_column(String(300))
    # 开单的设备：desktop / web / android / ios（X-Kuke-Platform），落消息时写进 metadata.device
    device: Mapped[str] = mapped_column(String(8), default="web")
    state: Mapped[str] = mapped_column(String(8), default="pending")
    message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    upload_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
