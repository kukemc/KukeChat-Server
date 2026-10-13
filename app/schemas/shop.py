"""商店的接口结构，契约见 docs/shop.md。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


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
    # false 表示已下架。用户侧只会在「我已拥有但已下架」的商品上看到 false
    is_active: bool = True


class ShopItemRead(ShopItemBase):
    owned: bool = False
    equipped: bool = False
    # buy 可以买 / level 等级够了可以免费领 / locked 等级不够且不卖 / owned 已拥有
    obtainable: Literal["buy", "level", "locked", "owned"] = "locked"


class ShopBalance(BaseModel):
    # 是否已绑定 CCW 账号
    bound: bool = False
    # 服务端核实到的累计投币
    donated: int = 0
    spent: int = 0
    # donated − spent；换绑过 CCW 账号的人可能是负数
    balance: int = 0
    # 上次向 CCW 核实成功的时间；从没核实过为 null
    verified_at: datetime | None = None
    # true 表示上次核实失败（或者现在核实不了），余额可能不是最新的
    stale: bool = False


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
    source: Literal["purchase", "level", "grant"]
    created_at: datetime | None = None
    # 被后台收回的时间；没被收回为 null
    revoked_at: datetime | None = None
    # 收回时是否退了金币
    refunded: bool = False


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
    # 花的比投的多
    mismatch: bool = False


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
    # 核对之后「花的比投的多」的账户
    mismatched: list[AdminShopAccountRead] = Field(default_factory=list)


class AdminShopGrant(BaseModel):
    item_id: int
    note: str | None = Field(default=None, max_length=500)


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
    price_max: int
    unlock_level_max: int
