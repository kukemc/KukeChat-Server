from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.core import localtime
from app.models.enums import PlatformRole, PresenceStatus, UserStatus


def _cosmetic_expired(entry: dict[str, Any], now: datetime) -> bool:
    """这一件是限时商品而且已经到期了。到期时间写不对的当成没有到期时间（永久）。"""
    raw = entry.get("expires_at")
    if not isinstance(raw, str) or not raw:
        return False
    try:
        until = datetime.fromisoformat(raw)
    except ValueError:
        return False
    if until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)
    return until <= now


def load_cosmetics(value: Any, now: datetime | None = None) -> dict[str, Any]:
    """users.cosmetics 那一列（JSON 文本）-> { 槽位: { sku, style[, expires_at] } }，已经到期的限时商品不在里面。

    限时商品的到期时间和样式一起冗余在这一列里（expires_at，ISO 时间），所以「到期了就不算装备着」是读的时候现场判断的，
    不依赖定时清扫，也不用查 shop_purchases。空的、解析不了的、形状不对的一律给 {}。
    给 services/shop.py 改这一列时用；对外展示用下面的 parse_cosmetics（不带 expires_at）。
    """
    if not value:
        return {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    if not isinstance(value, dict):
        return {}
    now = now or localtime._now()
    loaded: dict[str, Any] = {}
    for slot, entry in value.items():
        if not (isinstance(slot, str) and isinstance(entry, dict) and isinstance(entry.get("sku"), str) and isinstance(entry.get("style"), dict)):
            continue
        if _cosmetic_expired(entry, now):
            continue
        loaded[slot] = {"sku": entry["sku"], "style": entry["style"]}
        if isinstance(entry.get("expires_at"), str) and entry["expires_at"]:
            loaded[slot]["expires_at"] = entry["expires_at"]
    return loaded


def parse_cosmetics(value: Any) -> dict[str, Any]:
    """users.cosmetics 那一列（JSON 文本）-> { 槽位: { sku, style } }。空的、解析不了的、形状不对的一律给 {}。

    只解析这一列本身，不查库：用户对象在消息发送等热路径上。已经到期的限时商品不出现（见 load_cosmetics）。
    """
    return {slot: {"sku": entry["sku"], "style": entry["style"]} for slot, entry in load_cosmetics(value).items()}


class UserBirthday(BaseModel):
    month: int
    day: int
    # 只有本人选择公开年份时才有
    year: int | None = None


class UserPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    nickname: str | None = None
    avatar_url: str | None = None
    bio: str | None = None
    profile_title: str | None = None
    profile_tagline: str | None = None
    profile_status: str | None = None
    profile_location: str | None = None
    profile_interests: str | None = None
    profile_layout: str | None = None
    profile_card_style: str | None = None
    profile_accent_color: str | None = None
    profile_cover_url: str | None = None
    ccw_student_oid: str | None = None
    ccw_name: str | None = None
    ccw_avatar_url: str | None = None
    ccw_bio: str | None = None
    ccw_homepage_cover_url: str | None = None
    ccw_following_count: int | None = None
    ccw_follower_count: int | None = None
    ccw_like_count: int | None = None
    ccw_favorite_count: int | None = None
    ccw_comment_count: int | None = None
    ccw_creation_count: int | None = None
    ccw_view_count: int | None = None
    ccw_synced_at: datetime | None = None
    platform_role: PlatformRole = PlatformRole.USER
    status: UserStatus = UserStatus.ACTIVE
    presence_status: PresenceStatus = PresenceStatus.ONLINE
    presence_text: str | None = None
    presence_updated_at: datetime | None = None
    banned_until: datetime | None = None
    ban_reason: str | None = None
    is_bot: bool = False
    bot_owner_id: int | None = None
    # 别人戳这个用户时的动作和后缀文案，见 docs/chat-features.md
    poke_action: str = "戳"
    poke_suffix: str = ""
    # 账号等级 0–256，见 docs/account-level.md。直接读 users.account_level 这一列
    account_level: int = 0
    # 公开展示的风纪委员层级：trainee / regular / senior；不是在任委员或本人没选择公开时为 null
    jury_badge: str | None = None
    # 装备着的商店装饰（docs/shop.md 2.3）：{ 槽位: { sku, style } }，没装备时是 {}
    cosmetics: dict[str, Any] = Field(default_factory=dict)
    # 资料累计收到的赞，见 docs/chat-features.md 第 6 节。直接读 users.profile_like_count 这一列
    profile_like_count: int = 0
    # 心意值：收到的礼物花了送礼人多少币，见 docs/gifts.md。直接读 users.charm 这一列
    charm: int = 0
    # 生日（docs/chat-features.md 第 8 节）：没设置为 null；year 只有本人选择公开时才给。
    # is_birthday_today 按 UTC+8 判断，2 月 29 日出生的人在平年按 2 月 28 日过。两项都只读用户这一行上的列
    birthday: UserBirthday | None = None
    is_birthday_today: bool = False
    created_at: datetime

    @field_validator("cosmetics", mode="before")
    @classmethod
    def _load_cosmetics(cls, value: Any) -> dict[str, Any]:
        return parse_cosmetics(value)


class UserRead(UserPublic):
    email: EmailStr | None = None
    share_typing: bool = True
    profile_cover_pending_url: str | None = None
    profile_cover_moderation_status: str = "approved"
    updated_at: datetime


class UserUpdate(BaseModel):
    nickname: str | None = Field(default=None, max_length=80)
    avatar_url: str | None = Field(default=None, max_length=500)
    bio: str | None = Field(default=None, max_length=1000)
    profile_title: str | None = Field(default=None, max_length=80)
    profile_tagline: str | None = Field(default=None, max_length=120)
    profile_status: str | None = Field(default=None, max_length=80)
    profile_location: str | None = Field(default=None, max_length=80)
    profile_interests: str | None = Field(default=None, max_length=240)
    profile_layout: str | None = Field(default=None, max_length=20)
    profile_card_style: str | None = Field(default=None, max_length=20)
    profile_accent_color: str | None = Field(default=None, max_length=20)
    profile_cover_url: str | None = Field(default=None, max_length=500)
    # 关掉后别人看不到「我正在输入」
    share_typing: bool | None = None
    # 长度和取值在服务层校验，这样能返回带 code 的错误
    poke_action: str | None = Field(default=None, max_length=16)
    poke_suffix: str | None = Field(default=None, max_length=200)


class PokeOptionsRead(BaseModel):
    actions: list[str]
    default_action: str
    suffix_max_length: int


class ProfileLikeOptions(BaseModel):
    targets_per_day: int


class ProfileLikeState(BaseModel):
    """我和这个人之间的点赞状态（docs/chat-features.md 6.3）。"""

    user_id: int
    like_count: int
    today_count: int
    # 看自己时恒为 false
    liked_today: bool
    can_like: bool
    # can_like 为 false 时说明为什么不能点，同时成立时取靠前的一个；能点时为 null
    reason: Literal["self", "bot", "muted", "liked_today", "limit"] | None = None
    # 我今天还能给多少个不同的人点赞；看自己时也照常返回
    left_today: int


class ProfileLikeReceivedItem(BaseModel):
    user: UserPublic
    created_at: datetime
    today: bool


class ProfileLikeReceivedPage(BaseModel):
    items: list[ProfileLikeReceivedItem]
    total: int
    today_count: int


class UserPresenceUpdate(BaseModel):
    presence_status: Literal[
        PresenceStatus.ONLINE,
        PresenceStatus.AWAY,
        PresenceStatus.BUSY,
        PresenceStatus.DND,
        PresenceStatus.CREATING,
        PresenceStatus.GAMING,
        PresenceStatus.CUSTOM,
    ] = PresenceStatus.ONLINE
    presence_text: str | None = Field(default=None, max_length=40)


class OnlineCountRead(BaseModel):
    online_count: int


class OnlineUsersRead(BaseModel):
    online_count: int
    users: list[UserPublic]


class CcwCreatorRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    nickname: str | None = None
    avatar_url: str | None = None
    bio: str | None = None
    profile_title: str | None = None
    profile_tagline: str | None = None
    profile_cover_url: str | None = None
    ccw_student_oid: str
    ccw_name: str | None = None
    ccw_avatar_url: str | None = None
    ccw_bio: str | None = None
    ccw_homepage_cover_url: str | None = None
    ccw_following_count: int | None = None
    ccw_follower_count: int
    ccw_like_count: int | None = None
    ccw_creation_count: int | None = None
    ccw_view_count: int | None = None
    ccw_synced_at: datetime | None = None


class CcwCreatorPage(BaseModel):
    items: list[CcwCreatorRead]
    total: int
    limit: int
    offset: int
    has_more: bool


class UserOnlineStatusRead(BaseModel):
    user_id: int
    online: bool
    presence_status: PresenceStatus | Literal["offline"] = PresenceStatus.ONLINE
    presence_text: str | None = None
    presence_updated_at: datetime | None = None
