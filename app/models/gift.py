"""礼物的表（docs/gifts.md）：礼物目录，和每一次送礼的记录。

钱只记在 gift_records 里，没有另外的余额列：一个人送礼花了多少 = 他名下记录的 amount 之和，
余额公式（services/shop.py 的 _balance / available_expression）直接对这张表求和。送礼是真消耗，对方不得币。
收礼人的心意值（users.charm）是这张表的冗余：和记录在同一个事务里加，对账时核对两边是不是相等。

这里的 id 列都不建外键：消息会被清理（每个会话只留最近 1000 条）、群会被解散、商品可能被改，
送礼记录是账，不能跟着被级联删掉；礼物和商品当时叫什么、多少钱也照原样存一份在记录上。
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

UTC = timezone.utc


def utc_now() -> datetime:
    return datetime.now(UTC)


class Gift(Base):
    """目录里的一种礼物。key 建好之后不变；只能下架，不能删（送礼记录按 key 聚合）。"""

    __tablename__ = "gifts"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(20))
    # 金币，正整数
    price: Mapped[int] = mapped_column(Integer)
    # 客户端内置矢量图形的名字；客户端不认识的画成通用礼盒
    icon: Mapped[str] = mapped_column(String(32))
    # 动效规模：1 消息卡片内的小动画 / 2 聊天区内的一段动画 / 3 全屏动画
    tier: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    sort: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class GiftRecord(Base):
    """一次送礼。amount 是送礼人花掉的币，也是收礼人涨的心意值；创建后不变，也不会被撤销（撤回消息不退礼物）。"""

    __tablename__ = "gift_records"
    __table_args__ = (
        Index("ix_gift_records_to_created", "to_user_id", "created_at"),
        Index("ix_gift_records_from_created", "from_user_id", "created_at"),
        Index("ix_gift_records_created", "created_at"),
        Index("ix_gift_records_ccw_oid", "ccw_oid"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # gift 送的是目录里的礼物 / item 送的是一件商店商品
    kind: Mapped[str] = mapped_column(String(8))
    from_user_id: Mapped[int] = mapped_column(Integer)
    to_user_id: Mapped[int] = mapped_column(Integer)
    # kind = gift：送的时候这个礼物的样子（之后改名、改价不影响已经送出的）
    gift_key: Mapped[str | None] = mapped_column(String(32), nullable=True)
    gift_name: Mapped[str | None] = mapped_column(String(20), nullable=True)
    gift_icon: Mapped[str | None] = mapped_column(String(32), nullable=True)
    gift_price: Mapped[int | None] = mapped_column(Integer, nullable=True)
    gift_tier: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # kind = item：送的时候这件商品的样子
    item_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    item_sku: Mapped[str | None] = mapped_column(String(64), nullable=True)
    item_name: Mapped[str | None] = mapped_column(String(40), nullable=True)
    item_category: Mapped[str | None] = mapped_column(String(24), nullable=True)
    item_style: Mapped[str | None] = mapped_column(Text, nullable=True)
    item_duration_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 数量（送商品固定 1）和花掉的币（礼物 = 单价 × 数量，商品 = 当时的价格）
    count: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    amount: Mapped[int] = mapped_column(Integer)
    # 在哪个会话里送的；为空是从对方的个人空间 / 资料卡送的（不落聊天消息）
    conversation_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # direct / group。送的时候记下来：会话之后被删掉了，后台也还知道这是不是私聊里送的（私聊里的留言不给后台看）
    conversation_type: Mapped[str | None] = mapped_column(String(12), nullable=True)
    # 会话里那条 type = gift 的消息
    message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 留言。只有送的人和收的人能在记录里看到
    message: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # 送的那一刻是不是收礼人的生日（UTC+8）
    on_birthday: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    # 送的时候绑定的 CCW 账号。同一个 CCW 账号的币换绑到别的 KukeChat 账号上不能再花一遍，靠它算
    # （和 shop_purchases.ccw_oid、red_packets.ccw_oid 一个用途）
    ccw_oid: Mapped[str | None] = mapped_column(String(24), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
