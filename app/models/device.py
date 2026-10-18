"""防小号：设备信号与设备封禁（docs/device-bans.md）。

库里不存任何原始标识：设备令牌、机器码、浏览器指纹都只存服务端密钥算出来的 HMAC（见 app/core/device.py）。
IP 和别处（users.last_login_ip 等）一样存原文，另记它所在的网段（IPv4 /24、IPv6 /64）。
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base

UTC = timezone.utc


def utc_now() -> datetime:
    return datetime.now(UTC)


class DeviceSignal(Base):
    """一个账号用过的一个信号：一行 = (账号, 种类, 值)。

    kind：device（服务端签发的设备令牌）/ machine（桌面端、安卓的机器码）/ fp（浏览器指纹）/ ip。
    value_hash：前三种是 HMAC 的十六进制；ip 是 IP 原文。写入有节流（同一账号的同一个信号每小时最多写一次），
    所以 last_seen_at 精确到小时级、seen_count 是「被记下的次数」而不是请求数。
    """

    __tablename__ = "device_signals"
    __table_args__ = (
        UniqueConstraint("user_id", "kind", "value_hash", name="uq_device_signal"),
        # 找「还有谁用过这个信号」
        Index("ix_device_signals_kind_value", "kind", "value_hash"),
        # 同一网段一天内注册了几个号
        Index("ix_device_signals_kind_prefix", "kind", "prefix", "registered_here"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(8))
    value_hash: Mapped[str] = mapped_column(String(64))
    # 只有 ip 有：所在网段，如 112.39.99.0/24
    prefix: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # web / desktop / android / ios，最后一次记下时的值
    platform: Mapped[str | None] = mapped_column(String(12), nullable=True)
    # 这个账号是不是用这个信号注册的（注册数限制只数这种）
    registered_here: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    seen_count: Mapped[int] = mapped_column(Integer, default=1, server_default="1")


class DeviceBan(Base):
    """封一个信号。生效条件：lifted_at 为空且（expires_at 为空 = 永久，或还没到期）。

    scope：all（登录、注册、已登录的接口都拦）/ register（只拦注册，IP 默认用这个）。
    source：admin（管理员单封）/ person（「封禁此人」一并封的）/ propagated（账号被封时按设置自动带上的）。
    """

    __tablename__ = "device_bans"
    __table_args__ = (
        Index("ix_device_bans_kind_value", "kind", "value_hash"),
        Index("ix_device_bans_active", "lifted_at", "expires_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(8))
    # 和 device_signals.value_hash 同一套取值；ip 可以是单个 IP，也可以是网段（112.39.99.0/24）
    value_hash: Mapped[str] = mapped_column(String(64))
    scope: Mapped[str] = mapped_column(String(10), default="all", server_default="all")
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(String(12), default="admin", server_default="admin")
    # 从哪个账号身上封的（自动带上的封禁在这个账号解封时一起解除）
    source_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lifted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lifted_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    lift_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_by: Mapped["User | None"] = relationship(foreign_keys=[created_by_id])
    lifted_by: Mapped["User | None"] = relationship(foreign_keys=[lifted_by_id])
    source_user: Mapped["User | None"] = relationship(foreign_keys=[source_user_id])
