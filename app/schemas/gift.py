"""礼物和生日的接口结构，契约见 docs/gifts.md、docs/chat-features.md 第 8 节。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.schemas.message import MessageRead
from app.schemas.shop import ShopBalance
from app.schemas.user import UserPublic


class GiftRead(BaseModel):
    key: str
    name: str
    price: int
    # 客户端内置矢量图形的名字；不认识的画成通用礼盒
    icon: str
    # 动效规模：1 消息卡片内的小动画 / 2 聊天区内的一段动画 / 3 全屏动画
    tier: int = 1
    is_active: bool = True
    sort: int = 0


class GiftLimits(BaseModel):
    message_max: int
    count_max: int
    # 每人每天（UTC+8）最多送几次，礼物和商品合在一起
    daily_send_limit: int


class GiftCatalog(BaseModel):
    enabled: bool = False
    # 收到礼物涨的那个数值叫什么
    charm_label: str
    gifts: list[GiftRead] = Field(default_factory=list)
    limits: GiftLimits
    # 我今天已经送了几次
    sent_today: int = 0


class GiftCreate(BaseModel):
    """取值都在服务层校验，这样能返回带 code 的错误（invalid_target / invalid_gift / invalid_count / message_rejected ……）。"""

    to_user_id: Any = None
    # 在哪个会话里送；null 表示从对方个人空间 / 资料卡送（不落聊天消息）
    conversation_id: Any = None
    kind: Any = None
    gift_key: Any = None
    count: Any = None
    item_sku: Any = None
    message: Any = None


class GiftRef(BaseModel):
    key: str
    name: str
    icon: str
    price: int
    tier: int = 1


class GiftItemRef(BaseModel):
    sku: str
    name: str
    category: str
    style: dict[str, Any] = Field(default_factory=dict)
    duration_days: int | None = None


class GiftRecordRead(BaseModel):
    id: int
    kind: Literal["gift", "item"]
    gift: GiftRef | None = None
    item: GiftItemRef | None = None
    count: int = 1
    # 送礼人花掉的币，也是收礼人涨的心意值
    amount: int
    from_user: UserPublic | None = None
    to_user: UserPublic | None = None
    conversation_id: int | None = None
    message_id: int | None = None
    # 留言：只有送的人和收的人能看到，其他人看到的是 null
    message: str | None = None
    on_birthday: bool = False
    created_at: datetime | None = None


class GiftSendResult(BaseModel):
    record: GiftRecordRead
    # 会话里落下的那条消息；从个人空间 / 资料卡送的是 null
    message: MessageRead | None = None
    balance: ShopBalance


class GiftWallEntry(BaseModel):
    kind: Literal["gift", "item"]
    gift: GiftRef | None = None
    item: GiftItemRef | None = None
    count: int
    last_at: datetime | None = None


class GiftWall(BaseModel):
    charm: int = 0
    # 收到的礼物件数（各种的 count 之和）
    total_count: int = 0
    summary: list[GiftWallEntry] = Field(default_factory=list)


class GiftRecordPage(BaseModel):
    items: list[GiftRecordRead] = Field(default_factory=list)
    total: int = 0


class GiftMinePage(GiftRecordPage):
    sum: int = 0


class CharmBoardItem(BaseModel):
    rank: int
    user: UserPublic | None = None
    charm: int


class CharmBoardMe(BaseModel):
    rank: int | None = None
    charm: int = 0


class CharmBoard(BaseModel):
    items: list[CharmBoardItem] = Field(default_factory=list)
    me: CharmBoardMe


# ---------------------------------------------------------------- 后台


class AdminGiftSettings(BaseModel):
    enabled: bool = False
    daily_send_limit: int
    message_max: int


class AdminGiftSettingsWrite(BaseModel):
    """可以整份提交，也可以只传要改的。范围在服务层校验（422 invalid_settings，detail.field 指出哪一项）。"""

    enabled: Any = None
    daily_send_limit: Any = None
    message_max: Any = None


class AdminGiftWrite(BaseModel):
    """新建和修改共用；修改时只动传了的字段。取值在服务层校验（422 invalid_gift，detail.field 指出哪一项）。"""

    key: Any = None
    name: Any = None
    price: Any = None
    icon: Any = None
    tier: Any = None
    is_active: Any = None
    sort: Any = None


class AdminGiftCatalog(BaseModel):
    gifts: list[GiftRead] = Field(default_factory=list)


class AdminGiftRecordRead(GiftRecordRead):
    # 在哪种会话里送的：group / direct；从个人空间 / 资料卡送的是 null
    conversation_type: str | None = None
    # 群名；私聊和个人空间为 null
    conversation_title: str | None = None


class AdminGiftSummary(BaseModel):
    # 全站累计花在送礼上的币
    total_amount: int = 0
    # 全站累计送礼次数（记录条数）
    total_count: int = 0


class AdminGiftRecordPage(BaseModel):
    items: list[AdminGiftRecordRead] = Field(default_factory=list)
    total: int = 0
    summary: AdminGiftSummary


# ---------------------------------------------------------------- 生日


class BirthdayDate(BaseModel):
    year: int
    month: int
    day: int


class MyBirthday(BaseModel):
    birthday: BirthdayDate | None = None
    show_year: bool = False
    # 已废弃：改日期不再受限，恒为 null。只为兼容旧客户端保留
    next_change_at: datetime | None = None
    # 今天是我的生日，而且今年还没播过庆生动画
    celebrate_today: bool = False


class BirthdayWrite(BaseModel):
    """取值在服务层校验（422 invalid_birthday）。"""

    year: Any = None
    month: Any = None
    day: Any = None
    show_year: Any = None
