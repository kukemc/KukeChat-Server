"""好友之间的「火花」（docs/sparks.md）。

一对好友只有一行 friend_sparks（user_low_id < user_high_id）。规则（按 UTC+8 自然日）：某天双方都在私聊里发过消息
＝这一天「续上了」，连续天数 +1；只差一天没续上还能救，连续两天没续上就熄灭、天数清零，熄灭后 3 天内可以花金币重燃。
spark_relights 一行是一次重燃（花了多少币，余额公式对它求和，和送礼、买群升级一样）。

删好友不删这些行；接口只返回现在还是好友的。id 列都不建外键（和亲密度的表一样）。
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import Date, DateTime, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

UTC = timezone.utc


def utc_now() -> datetime:
    return datetime.now(UTC)


class FriendSpark(Base):
    __tablename__ = "friend_sparks"
    __table_args__ = (
        UniqueConstraint("user_low_id", "user_high_id", name="uq_friend_sparks_pair"),
        Index("ix_friend_sparks_high", "user_high_id"),
        Index("ix_friend_sparks_lit_on", "lit_on"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_low_id: Mapped[int] = mapped_column(Integer)
    user_high_id: Mapped[int] = mapped_column(Integer)
    # 当前连续天数。熄灭后清零（被扫描或下一次续上时），原来的天数挪到 lost_days
    streak_days: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # 最近一次「续上」的那一天（UTC+8）。今天 / 昨天 = 正常，前天 = 快熄灭，更早 = 熄灭
    lit_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    # 双方各自最后一次（算数的）发言是哪一天
    low_spoke_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    high_spoke_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    # 历史最高连续天数
    best_streak: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # 当前连续天数对应的等级（0 = 没有火花），用来判断「升级的那一刻」
    level: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # 最近一次熄灭的时刻（UTC+8 那天零点）和熄灭前的天数（重燃恢复的就是它；重燃后清零）
    extinguished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lost_days: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # 上次重燃
    relit_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    relit_by_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 今天「续上」之前的快照（JSON）。今天唯一的一条发言被撤回时，按它把今天的续火撤回去
    undo_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class SparkRelight(Base):
    """一次花金币重燃。amount 是花掉的币，不退。ccw_oid：重燃时绑定的 CCW 账号（换绑防二次花，和送礼记录一样）。"""

    __tablename__ = "spark_relights"
    __table_args__ = (
        Index("ix_spark_relights_user", "user_id", "id"),
        Index("ix_spark_relights_ccw_oid", "ccw_oid"),
        Index("ix_spark_relights_spark", "spark_id", "id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    spark_id: Mapped[int] = mapped_column(Integer)
    user_id: Mapped[int] = mapped_column(Integer)
    friend_id: Mapped[int] = mapped_column(Integer)
    amount: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    restored_days: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    ccw_oid: Mapped[str | None] = mapped_column(String(24), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
