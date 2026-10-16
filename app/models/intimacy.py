"""好友亲密度与好友成就的表（docs/intimacy.md）。

一对好友只有一行 friend_bonds（user_low_id < user_high_id），亲密度、等级和成就要用的各项累计都冗余在这一行上；
friend_bond_days 是这一对每天的明细（双方各发了几条、当天每种来源涨了多少），每日上限靠它算；
friend_badges 一行是一枚解锁了的徽章（某一系列的某一级）。

删好友不删这些行：重新加回好友，亲密度和徽章都还在。只有当前是好友的才会被接口返回、才会继续涨。
id 列都不建外键（和送礼记录一样）：账号被删掉后这些行留着，但好友关系没了，接口不会再返回它们。
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import Boolean, Date, DateTime, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

UTC = timezone.utc


def utc_now() -> datetime:
    return datetime.now(UTC)


def _counter() -> Mapped[int]:
    return mapped_column(Integer, default=0, server_default="0")


class FriendBond(Base):
    __tablename__ = "friend_bonds"
    __table_args__ = (
        UniqueConstraint("user_low_id", "user_high_id", name="uq_friend_bonds_pair"),
        Index("ix_friend_bonds_high", "user_high_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_low_id: Mapped[int] = mapped_column(Integer)
    user_high_id: Mapped[int] = mapped_column(Integer)
    intimacy: Mapped[int] = _counter()
    level: Mapped[int] = _counter()
    # 成就用的累计。low / high 指 user_low_id / user_high_id 那一方
    messages_total: Mapped[int] = _counter()
    messages_low: Mapped[int] = _counter()
    messages_high: Mapped[int] = _counter()
    # 双方当天都发过言的天数，以及其中深夜（0–5 点）、清晨（5–8 点）双方都发过言的、周六日的、元旦的
    mutual_days: Mapped[int] = _counter()
    night_days: Mapped[int] = _counter()
    dawn_days: Mapped[int] = _counter()
    weekend_days: Mapped[int] = _counter()
    newyear_days: Mapped[int] = _counter()
    # 同一天里深夜、清晨双方都聊过的天数
    sunmoon_days: Mapped[int] = _counter()
    best_day_messages: Mapped[int] = _counter()
    best_night_messages: Mapped[int] = _counter()
    # 连续互动：streak_days 只在 last_mutual_day 是今天或昨天时有效
    streak_days: Mapped[int] = _counter()
    best_streak: Mapped[int] = _counter()
    last_mutual_day: Mapped[date | None] = mapped_column(Date, nullable=True)
    gift_count: Mapped[int] = _counter()
    gift_amount: Mapped[int] = _counter()
    # 生日那天送礼的次数（每人每年最多算一次，和生日加成同一条规则）
    birthday_gifts: Mapped[int] = _counter()
    # 某一方最近一次拿到「生日礼物加成」是哪一年（UTC+8），每人每年一次
    birthday_year_low: Mapped[int | None] = mapped_column(Integer, nullable=True)
    birthday_year_high: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 挂在对方名字旁的徽章，各看各的：display_low 是 low 那一方看 high 时挂哪个。
    # null = 自动（等级最高的那枚，一样高挑最近解锁的），"none" = 不挂，否则是一个系列的 key（挂这一系列已解锁的最高级）
    display_low: Mapped[str | None] = mapped_column(String(32), nullable=True)
    display_high: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class FriendBondDay(Base):
    """一对好友的一天（UTC+8 自然日）。*_points 是当天各来源已经记上的亲密度。"""

    __tablename__ = "friend_bond_days"
    __table_args__ = (UniqueConstraint("bond_id", "day", name="uq_friend_bond_days_bond_day"), Index("ix_friend_bond_days_day", "day"))

    id: Mapped[int] = mapped_column(primary_key=True)
    bond_id: Mapped[int] = mapped_column(Integer)
    day: Mapped[date] = mapped_column(Date)
    msgs_low: Mapped[int] = _counter()
    msgs_high: Mapped[int] = _counter()
    night_low: Mapped[int] = _counter()
    night_high: Mapped[int] = _counter()
    dawn_low: Mapped[int] = _counter()
    dawn_high: Mapped[int] = _counter()
    gifts: Mapped[int] = _counter()
    gift_amount: Mapped[int] = _counter()
    # 这一天是不是已经算进了 mutual_days / night_days / dawn_days（双方都发过言的那一刻翻成 true，只翻一次）
    mutual: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    night_mutual: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    dawn_mutual: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    chat_points: Mapped[int] = _counter()
    gift_points: Mapped[int] = _counter()
    streak_points: Mapped[int] = _counter()
    # 生日礼物加成，不算进每日上限
    birthday_points: Mapped[int] = _counter()


class FriendBadge(Base):
    __tablename__ = "friend_badges"
    __table_args__ = (UniqueConstraint("bond_id", "series", "level", name="uq_friend_badges_bond_series_level"), Index("ix_friend_badges_series", "series", "level"))

    id: Mapped[int] = mapped_column(primary_key=True)
    bond_id: Mapped[int] = mapped_column(Integer, index=True)
    series: Mapped[str] = mapped_column(String(32))
    level: Mapped[int] = mapped_column(Integer)
    unlocked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    # 双方各自有没有看过（徽章墙上的「新」）
    seen_low: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    seen_high: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
