"""资料点赞（docs/chat-features.md 第 6 节）。

累计赞数冗余在 users.profile_like_count 上，序列化用户不用查这里；这张表是明细：谁在哪一天给谁点了赞。
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import Date, DateTime, ForeignKey, Index, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

UTC = timezone.utc


def utc_now() -> datetime:
    return datetime.now(UTC)


class ProfileLike(Base):
    """一行 = 一个人在某一天（UTC+8 自然日）给另一个人点的那一个赞。赞只增不减，行不会被用户删掉。"""

    __tablename__ = "profile_likes"
    __table_args__ = (
        # 同一个人对同一个人每天只能点一次；并发的重复提交靠它挡住。顺带也是 liker_id 外键级联删除用的索引
        UniqueConstraint("liker_id", "target_id", "day", name="uq_profile_likes_liker_target_day"),
        # 「谁赞了我」按时间倒序翻页、数今天收到几个
        Index("ix_profile_likes_target_created", "target_id", "created_at"),
        # 数这个人今天已经给多少个不同的人点过
        Index("ix_profile_likes_liker_day", "liker_id", "day"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    liker_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    target_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    # created_at 所在的 UTC+8 自然日，写入时算好
    day: Mapped[date] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
