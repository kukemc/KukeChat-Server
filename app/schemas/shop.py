"""商店的接口结构，契约见 docs/shop.md。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.schemas.user import UserPublic


class ShopCategory(BaseModel):
    key: str
    label: str
    # 这个类别的商品装在哪个槽里；一个槽同时只能装备一件
    slot: str


class ShopItemBrief(BaseModel):
    sku: str
    name: str
    category: str


class ShopItemBase(BaseModel):
    id: int
    sku: str
    category: str
    name: str
    description: str = ""
    # 金币；0 表示不卖
    price: int = 0
    unlock_level: int | None = None
    style: dict[str, Any] = Field(default_factory=dict)
    preview_text: str | None = None
    # new / hot / limited
    tag: str | None = None
    available_until: datetime | None = None
    # 买一次拥有多少天；null 是永久
    duration_days: int | None = None
    # false 表示已下架。用户侧只会在「我已拥有但已下架」的商品上看到 false
    is_active: bool = True


class ShopItemRead(ShopItemBase):
    owned: bool = False
    equipped: bool = False
    # buy 可以买 / level 等级够了可以免费领 / locked 等级不够且不卖 / owned 已拥有 / renew 我拥有的是限时的，可以再买来续期
    obtainable: Literal["buy", "level", "locked", "owned", "renew"] = "locked"
    # 我拥有的这一件什么时候到期；永久或者没拥有为 null
    expires_at: datetime | None = None


class ShopBalance(BaseModel):
    # 是否已绑定 CCW 账号
    bound: bool = False
    # 服务端核实到的累计投币
    donated: int = 0
    spent: int = 0
    # donated − spent − gift_spent − group_plus_spent − spark_spent − red_packet_sent + red_packet_received + red_packet_refunded + granted；换绑过 CCW 账号的人可能是负数
    balance: int = 0
    # 上次向 CCW 核实成功的时间；从没核实过为 null
    verified_at: datetime | None = None
    # true 表示上次核实失败（或者现在核实不了），余额可能不是最新的
    stale: bool = False
    # 红包（docs/red-packets.md）：发出的整包金额之和、抢到的、没被领完退回来的
    red_packet_sent: int = 0
    red_packet_received: int = 0
    red_packet_refunded: int = 0
    # 送礼物 / 送商品花掉的币（docs/gifts.md）：真消耗，对方不得币
    gift_spent: int = 0
    # 买群升级花掉的币（docs/group-plus.md）：不退款
    group_plus_spent: int = 0
    # 重燃好友火花花掉的币（docs/sparks.md）：不退款
    spark_spent: int = 0
    # 后台赠送的金币净额（赠送 − 扣除，docs/shop.md 5.2）
    granted: int = 0


class ShopOverview(BaseModel):
    categories: list[ShopCategory] = Field(default_factory=list)
    items: list[ShopItemRead] = Field(default_factory=list)
    balance: ShopBalance
    # 每个槽装备的 sku
    equipped: dict[str, str] = Field(default_factory=dict)
    # 去投币的作品页；服务端没配置官方作品时为 null
    donate_url: str | None = None
    level: int = 0


class ShopPurchaseResult(BaseModel):
    item: ShopItemRead
    balance: ShopBalance
    equipped: dict[str, str] = Field(default_factory=dict)


class ShopEquipRequest(BaseModel):
    slot: str = Field(max_length=40)
    # null 表示卸下
    sku: str | None = Field(default=None, max_length=80)


class ShopEquipResult(BaseModel):
    equipped: dict[str, str] = Field(default_factory=dict)


class ShopPurchaseRead(BaseModel):
    id: int
    item: ShopItemBrief
    price: int = 0
    # purchase 买的 / level 等级解锁 / grant 后台赠送 / gift 别的用户送的（price 为 0，带 from_user）
    source: Literal["purchase", "level", "grant", "gift"]
    created_at: datetime | None = None
    # 被后台收回的时间；没被收回为 null
    revoked_at: datetime | None = None
    # 收回时是否退了金币
    refunded: bool = False
    # 限时商品：这一条记录到什么时候为止（续期之前的那几条到续期的那一刻为止）；永久的为 null
    expires_at: datetime | None = None
    # 这一次得到了多少天；永久的为 null
    duration_days: int | None = None
    # source = gift 时是谁送的
    from_user: UserPublic | None = None


class ShopPurchasePage(BaseModel):
    items: list[ShopPurchaseRead] = Field(default_factory=list)
    total: int = 0


# ---------------------------------------------------------------- 后台


class AdminShopItemWrite(BaseModel):
    """新建和修改共用。取值范围在服务层校验，这样能返回带 code 的错误；修改时只动传了的字段。"""

    sku: str | None = Field(default=None, max_length=200)
    category: str | None = Field(default=None, max_length=200)
    name: str | None = Field(default=None, max_length=2000)
    description: str | None = Field(default=None, max_length=10000)
    price: int | None = None
    unlock_level: int | None = None
    style: dict[str, Any] | None = None
    preview_text: str | None = Field(default=None, max_length=10000)
    tag: str | None = Field(default=None, max_length=200)
    is_active: bool | None = None
    sort: int | None = None
    available_until: datetime | None = None
    # 买一次拥有多少天（1–3650）；null 是永久
    duration_days: int | None = None


class AdminShopItemRead(ShopItemBase):
    sort: int = 0
    # 卖出去多少份（只算花金币买的，退款的不算）
    sold: int = 0
    # 现在有多少人拥有（含等级解锁和赠送）
    owners: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None


class AdminShopItemPage(BaseModel):
    items: list[AdminShopItemRead] = Field(default_factory=list)
    total: int = 0


class AdminShopUser(BaseModel):
    id: int
    username: str
    nickname: str | None = None
    avatar_url: str | None = None


class AdminShopPurchaseRead(BaseModel):
    id: int
    user: AdminShopUser | None = None
    item: ShopItemBrief
    item_id: int
    price: int = 0
    source: str
    note: str | None = None
    # 赠送人
    actor_id: int | None = None
    created_at: datetime | None = None
    revoked_at: datetime | None = None
    revoked_by_id: int | None = None
    revoke_note: str | None = None
    refunded: bool = False
    # 限时商品：这一条记录到什么时候为止；永久的为 null。续期之前的那几条到续期的那一刻为止（superseded_at 就是那一刻）
    expires_at: datetime | None = None
    duration_days: int | None = None
    superseded_at: datetime | None = None
    # source = gift 时是谁送的
    from_user: AdminShopUser | None = None


class AdminShopPurchasePage(BaseModel):
    items: list[AdminShopPurchaseRead] = Field(default_factory=list)
    total: int = 0


class AdminShopAccountRead(BaseModel):
    user: AdminShopUser
    # 用户现在绑定的 CCW 账号；没绑定为 null
    ccw_oid: str | None = None
    # 累计投币是按哪个 CCW 账号核实的；和 ccw_oid 不一样说明换绑后还没重新核实
    verified_oid: str | None = None
    donated: int = 0
    spent: int = 0
    # 这个 CCW 账号的币被别的 KukeChat 账号花掉的部分（账号换绑过才会有）
    shared_spent: int = 0
    balance: int = 0
    verified_at: datetime | None = None
    # 上次向 CCW 发起核实的时间（不论成败）
    checked_at: datetime | None = None
    stale: bool = False
    last_error: str | None = None
    # 余额为负（花掉的加上发出去的，比投的加上抢到的多）
    mismatch: bool = False
    # 红包：发出的整包金额之和、抢到的、没被领完退回来的
    red_packet_sent: int = 0
    red_packet_received: int = 0
    red_packet_refunded: int = 0
    # 送礼物 / 送商品花掉的币
    gift_spent: int = 0
    # 买群升级（以及重燃好友火花，docs/sparks.md）花掉的币
    group_plus_spent: int = 0
    # 后台赠送的金币净额（赠送 − 扣除）
    granted: int = 0


class AdminShopAccountPage(BaseModel):
    items: list[AdminShopAccountRead] = Field(default_factory=list)
    total: int = 0


class AdminShopReconcileResult(BaseModel):
    # 余额来源的名单上一共多少个投币人
    donors: int = 0
    # 其中多少个对得上已绑定的用户
    matched: int = 0
    # 累计投币有变化（或者新建了账户）的用户数
    updated: int = 0
    # 核对之后余额为负的账户
    mismatched: list[AdminShopAccountRead] = Field(default_factory=list)
    # 全站红包账的守恒检查：{ total_sent, total_claimed, total_refunded, outstanding, balanced, problems }
    red_packets: dict[str, Any] = Field(default_factory=dict)
    # 送礼的账的守恒检查：{ total_amount, total_count, total_charm, balanced, problems }
    gifts: dict[str, Any] = Field(default_factory=dict)
    # 群升级的账的守恒检查：{ total_amount, total_count, granted_count, revoked_count, balanced, problems }
    group_plus: dict[str, Any] = Field(default_factory=dict)


class AdminShopGrant(BaseModel):
    item_id: int
    note: str | None = Field(default=None, max_length=500)
    # 不传按商品的设置；正整数 = 这一份只给这么多天；0 = 送成永久。范围在服务层校验
    duration_days: int | None = None


class AdminCoinGrantCreate(BaseModel):
    """后台赠送 / 扣除金币。取值在服务层校验（422 invalid_amount / invalid_reason，带 field）。"""

    user_id: int
    # 正数赠送、负数扣除；不能是 0，绝对值不超过 1,000,000
    amount: Any = None
    # 必填，最多 200 字；会展示给用户（通知里）
    reason: Any = None


class AdminCoinGrantRead(BaseModel):
    id: int
    user_id: int
    user: AdminShopUser | None = None
    amount: int
    reason: str
    admin_id: int | None = None
    admin: AdminShopUser | None = None
    created_at: datetime | None = None


class AdminCoinGrantResult(BaseModel):
    grant: AdminCoinGrantRead
    balance_before: int = 0
    # 这个用户写入之后的完整 balance 对象
    balance: ShopBalance


class AdminCoinGrantPage(BaseModel):
    items: list[AdminCoinGrantRead] = Field(default_factory=list)
    total: int = 0
    # 筛选范围内的合计：{ granted, deducted, net }
    summary: dict[str, int] = Field(default_factory=dict)


class AdminShopRevoke(BaseModel):
    item_id: int
    note: str | None = Field(default=None, max_length=500)
    # 这件是花金币买的话，是否把金币退回去；默认不退
    refund: bool = False


class AdminShopSettings(BaseModel):
    # 能不能核实余额（接入了余额来源，并且它需要的配置都齐了）
    configured: bool = False
    creation_oid: str | None = None
    # 作品 oid 取自哪个配置项：ccw_shop_creation_oid / ccw_bind_subject_oid；都没配为 null
    creation_oid_source: str | None = None
    donate_url: str | None = None
    refresh_interval_seconds: int
    ranking_recent_limit: int
    ranking_cache_seconds: int
    changed_interval_seconds: int
    cosmetics_event_interval_seconds: int
    categories: list[ShopCategory] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    animations: list[str] = Field(default_factory=list)
    # 客户端内置的纹样和角饰：style 里的 pattern.key / ornaments[].key 只能用这些名字
    patterns: list[str] = Field(default_factory=list)
    ornaments: list[str] = Field(default_factory=list)
    # 头像框的那几张表（docs/shop.md 2.4）：animation、ornaments[].key、ornaments[].position 各自能用的名字
    frame_animations: list[str] = Field(default_factory=list)
    frame_ornaments: list[str] = Field(default_factory=list)
    frame_positions: list[str] = Field(default_factory=list)
    price_max: int
    unlock_level_max: int
    # 限时商品一次最多多少天
    duration_max: int = 3650
