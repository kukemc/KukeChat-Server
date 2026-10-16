"""商店的表（docs/shop.md）：商品目录、购买记录（同时也是「谁拥有什么」）、每个用户的金币账户。

装备着什么不在这里：它冗余在 users.cosmetics 上，序列化用户时不用再查这几张表。
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

UTC = timezone.utc


def utc_now() -> datetime:
    return datetime.now(UTC)


class ShopItem(Base):
    """一件商品。sku 建好之后不变，客户端和 users.cosmetics 都用它指代这件商品。"""

    __tablename__ = "shop_items"
    __table_args__ = (Index("ix_shop_items_active_sort", "is_active", "sort"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    sku: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    # 类别决定装备槽和 style 的校验规则，可选值是 services/shop.py 里的 CATEGORIES
    category: Mapped[str] = mapped_column(String(24))
    name: Mapped[str] = mapped_column(String(40))
    description: Mapped[str] = mapped_column(String(200), default="", server_default="")
    # 金币；0 表示不卖，只能靠等级解锁或赠送获得
    price: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # 账号等级达到多少可以免费领；为空表示不能靠等级解锁
    unlock_level: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 校验并规范化之后的样式（JSON），见 services/shop_style.py
    style: Mapped[str] = mapped_column(Text)
    preview_text: Mapped[str | None] = mapped_column(String(80), nullable=True)
    # new / hot / limited
    tag: Mapped[str | None] = mapped_column(String(12), nullable=True)
    # 下架只是不再出售，已经拥有的人照常使用
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    sort: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # 限时商品到这个时间自动停售
    available_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 买一次拥有多少天；为空是永久。改它只影响之后的购买 / 赠送，已有的拥有记录不动
    duration_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ShopPurchase(Base):
    """一条获得记录：买的、等级解锁的、后台赠送的、别人送的。

    「现在拥有」= 这个人这件商品**当前的那一行**（没被收回、也没被后来的记录接替），而且它没到期
    （expires_at 为空是永久；到期是读的时候现场比较时间，不依赖清扫）。

    记录只增不删：收回是写上 revoked_at；续期（或者到期之后再买）是写一行新的，把原来那一行标上 superseded_at，
    新的一行带着累加之后的到期时间 —— 所以每一次花的币、得到的天数都各有一行。
    唯一索引只管「当前的那一行」，所以同一件商品同时只有一行当前记录（并发的重复购买 / 续期会撞上它，或者抢不到接替的那条 UPDATE）。
    """

    __tablename__ = "shop_purchases"
    __table_args__ = (
        Index(
            "uq_shop_purchases_live",
            "user_id",
            "item_id",
            unique=True,
            sqlite_where=text("revoked_at IS NULL AND superseded_at IS NULL"),
            postgresql_where=text("revoked_at IS NULL AND superseded_at IS NULL"),
        ),
        Index("ix_shop_purchases_user_created", "user_id", "created_at"),
        Index("ix_shop_purchases_item", "item_id"),
        Index("ix_shop_purchases_ccw_oid", "ccw_oid"),
        # 到期清扫按它找「快到期 / 已到期」的行
        Index("ix_shop_purchases_expires", "expires_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    item_id: Mapped[int] = mapped_column(ForeignKey("shop_items.id", ondelete="CASCADE"))
    # 实际扣了多少金币；等级解锁和赠送是 0
    price: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # purchase 买的 / level 等级解锁 / grant 后台赠送 / gift 别的用户花币送的（docs/gifts.md，price 为 0，币记在送礼记录上）
    source: Mapped[str] = mapped_column(String(12))
    # 付款时绑定的 CCW 账号（只有 purchase 有）。同一个 CCW 账号的币不管换到哪个 KukeChat 账号上都只能花一次，靠它算
    ccw_oid: Mapped[str | None] = mapped_column(String(24), nullable=True)
    # 赠送人（管理员），其余来源为空
    actor_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_by_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    revoke_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 收回时把金币退了：这一行不再计入已花
    refunded: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    # 限时商品：这一行到什么时候为止算拥有（已经把续期前剩下的时间加进去了）；永久的为空
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 这一次得到了多少天；永久的为空
    duration_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 被后来的记录接替的时间（续期、到期之后再买、限时的换成了永久的）。接替之后这一行只是历史
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # source = gift 时是谁送的、对应哪条送礼记录
    from_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    gift_record_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 到期提醒（shop.item.expiring）和到期处理（卸下 + shop.item.expired）各只做一次：做的那个进程用带条件的 UPDATE 把时间写在这里
    expiring_notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expired_notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ShopAccount(Base):
    """一个用户的金币账户。KukeChat 不发币也不存币：余额 = 向 CCW 核实到的累计投币 − 已花。"""

    __tablename__ = "shop_accounts"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    # donated 是按哪个 CCW 账号核实的。和用户现在绑定的不一样（换绑了）时 donated 作废，要重新核实
    ccw_oid: Mapped[str | None] = mapped_column(String(24), nullable=True)
    # 上次核实到的累计投币（向下取整）
    donated: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # 已花：这个用户所有没退款的 purchase 记录的 price 之和，和购买记录在同一个事务里一起改
    spent: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # 上次核实成功的时间；从没成功过为空
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 上次核实是否成功
    verify_ok: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    # 上次向 CCW 发起核实的时间（不论成败），限频用
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
