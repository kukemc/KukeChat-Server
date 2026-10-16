"""红包的接口结构，契约见 docs/red-packets.md。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.schemas.message import MessageRead
from app.schemas.shop import AdminShopUser, ShopBalance
from app.schemas.user import UserPublic

RedPacketStatus = Literal["active", "finished", "expired", "revoked"]


class RedPacketLimits(BaseModel):
    # 单个红包的总额上限
    max_total: int
    max_count: int
    # 每一份至少多少
    min_each: int
    # 每人每天（UTC+8）最多发几个
    daily_send_limit: int
    expire_hours: int


class RedPacketConfig(BaseModel):
    enabled: bool = False
    limits: RedPacketLimits
    # 我今天已经发了几个
    sent_today: int = 0


class RedPacketCreate(BaseModel):
    """取值都在服务层校验，这样能返回带 code 的错误（invalid_kind / invalid_amount / invalid_count / greeting_rejected）。"""

    conversation_id: int
    kind: Any = None
    # normal：单个金额；lucky：总额
    amount: Any = None
    count: Any = None
    greeting: Any = None


class RedPacketMyClaim(BaseModel):
    amount: int
    created_at: datetime | None = None


class RedPacketClaim(BaseModel):
    user: UserPublic | None = None
    amount: int
    created_at: datetime | None = None
    # 手气最佳：只在拼手气、而且抢完了的红包里标一个
    is_luckiest: bool = False


class RedPacketRead(BaseModel):
    id: int
    kind: Literal["normal", "lucky"]
    greeting: str
    sender: UserPublic | None = None
    conversation_id: int
    message_id: int | None = None
    # total / claimed_amount / claims 只在「我已经领了」「我是发的人」或「红包已不是 active」时给出，否则为 null
    total: int | None = None
    count: int
    claimed_count: int = 0
    claimed_amount: int | None = None
    # 结束时退回发送者的部分；可见性同 total
    refunded_amount: int | None = None
    status: RedPacketStatus
    my_claim: RedPacketMyClaim | None = None
    expires_at: datetime | None = None
    created_at: datetime | None = None
    finished_at: datetime | None = None
    claims: list[RedPacketClaim] | None = None


class RedPacketSendResult(BaseModel):
    red_packet: RedPacketRead
    message: MessageRead
    balance: ShopBalance


class RedPacketClaimResult(BaseModel):
    amount: int
    red_packet: RedPacketRead
    balance: ShopBalance


class RedPacketReceived(BaseModel):
    """我抢到的一笔。"""

    amount: int
    created_at: datetime | None = None
    red_packet: RedPacketRead


class RedPacketPending(BaseModel):
    """会话里我还能领的一个红包：够客户端画提示、跳到那条消息就行，不带金额"""
    id: int
    message_id: int
    sender_id: int
    kind: Literal["normal", "lucky"]
    greeting: str
    count: int
    claimed_count: int = 0
    expires_at: datetime | None = None
    created_at: datetime | None = None


class RedPacketPendingList(BaseModel):
    items: list[RedPacketPending]


class RedPacketPage(BaseModel):
    # sent：每项直接是红包对象；received：每项是 { amount, created_at, red_packet }
    items: list[RedPacketReceived | RedPacketRead] = Field(default_factory=list)
    total: int = 0
    # sent：发出的整包金额之和；received：抢到的金额之和
    sum: int = 0


# ---------------------------------------------------------------- 后台


class AdminRedPacketBound(BaseModel):
    min: int
    max: int


class AdminRedPacketSettings(RedPacketLimits):
    enabled: bool = False
    # 每一项限制允许设成多少（只读）
    bounds: dict[str, AdminRedPacketBound] = Field(default_factory=dict)


class AdminRedPacketSettingsWrite(BaseModel):
    """只改传了的项。取值范围在服务层校验（422 invalid_settings，detail.field 指出哪一项）。"""

    enabled: Any = None
    max_total: Any = None
    max_count: Any = None
    min_each: Any = None
    daily_send_limit: Any = None
    expire_hours: Any = None


class AdminRedPacketRead(BaseModel):
    id: int
    kind: str
    # 私聊红包恒为 null：后台不能借红包看私聊内容
    greeting: str | None = None
    sender: AdminShopUser | None = None
    sender_id: int
    conversation_id: int
    # direct / group
    conversation_type: str
    message_id: int | None = None
    total: int
    count: int
    claimed_count: int = 0
    claimed_amount: int = 0
    refunded_amount: int = 0
    status: RedPacketStatus
    expires_at: datetime | None = None
    created_at: datetime | None = None
    finished_at: datetime | None = None
    # 后台撤销的才有
    revoked_by_id: int | None = None
    revoke_reason: str | None = None


class AdminRedPacketClaim(BaseModel):
    user: AdminShopUser | None = None
    user_id: int
    amount: int
    created_at: datetime | None = None
    is_luckiest: bool = False


class AdminRedPacketDetail(AdminRedPacketRead):
    claims: list[AdminRedPacketClaim] = Field(default_factory=list)


class AdminRedPacketSummary(BaseModel):
    total_sent: int = 0
    total_claimed: int = 0
    total_refunded: int = 0
    # 还在进行中的红包里没被领走的
    outstanding: int = 0


class AdminRedPacketPage(BaseModel):
    items: list[AdminRedPacketRead] = Field(default_factory=list)
    total: int = 0
    # 全站累计，不随筛选变
    summary: AdminRedPacketSummary = Field(default_factory=AdminRedPacketSummary)


class AdminRedPacketRevoke(BaseModel):
    # 必填，最长 500
    reason: str = Field(min_length=1, max_length=500)
