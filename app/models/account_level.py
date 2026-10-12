"""账号等级（docs/account-level.md）。

累计值和等级冗余在 users 上（account_level / account_active_tenths / account_streak_*），这里是每天的明细和升级通知。
活跃天一律用整数存「十分之一天」，对外再换算成一位小数，避免浮点累加出误差。
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import Date, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

UTC = timezone.utc


def utc_now() -> datetime:
    return datetime.now(UTC)


class AccountLevelDay(Base):
    """一行 = 一个人的一天（UTC+8 自然日）。在线循环往里加分钟，结算把基础和各项加成记到这一行上。"""

    __tablename__ = "account_level_days"
    __table_args__ = (
        UniqueConstraint("user_id", "day", name="uq_account_level_days_user_day"),
        # 月榜按日期范围汇总
        Index("ix_account_level_days_day", "day"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    day: Mapped[date] = mapped_column(Date)
    online_minutes: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # 上次计入在线时长的时间片编号（floor(epoch 秒 / 时间片长度)）。同一个时间片里不管几个进程、几个设备都只记一次
    last_slot: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # 已拿到的基础活跃天（十分之一天）
    base_tenths: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # 各项加成当天拿到了多少（十分之一天），0 = 还没拿到。拿到之后不会收回
    bonus_messages: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    bonus_checkin: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    bonus_ccw_bound: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    bonus_jury: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    bonus_streak_7: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    bonus_streak_30: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # 当天已经见到的发言数（只增不减），每次结算把数到的最大值记下来
    message_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # 其中北京时间 00:00–08:00 那一段见到过的最大值。这一段的群消息只能从 messages 表数，而那张表会被裁剪，
    # 所以单独记住：白天再算总数时用「这一段记下的数 + 白天的数」，不会因为凌晨的消息被裁掉而少算
    night_messages: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # 拿到当天的基础活跃天时，连续活跃到了第几天；没拿到基础为 0
    streak_days: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # 当天已记入累计的合计（基础 + 加成，不超过每日上限）
    earned_tenths: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class AccountLevelNotification(Base):
    """升级通知。只在升到「多一个月亮」的等级时写一条，通知中心从这里读。"""

    __tablename__ = "account_level_notifications"
    __table_args__ = (Index("ix_account_level_notifications_user_created", "user_id", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    level: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(120))
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
