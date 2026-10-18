"""设备封禁的接口结构（docs/device-bans.md）。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

Kind = Literal["device", "machine", "fp", "ip"]
Strength = Literal["strong", "medium", "weak"]


class DeviceIssueRead(BaseModel):
    device: str
    # 这次是不是新发的（客户端没带、或者带的令牌签名不对）
    issued: bool


class AdminBrief(BaseModel):
    id: int
    username: str
    nickname: str | None = None


class LinkedUser(BaseModel):
    id: int
    username: str
    nickname: str | None = None
    avatar_url: str | None = None
    status: Literal["active", "disabled"]
    is_bot: bool = False
    created_at: datetime | None = None
    banned_until: datetime | None = None
    ban_reason: str | None = None


class LinkReason(BaseModel):
    kind: Literal["device", "machine", "fp", "ip", "bot_owner"]
    masked: str | None = None
    detail: str | None = None


class LinkedAccount(BaseModel):
    user: LinkedUser
    strength: Strength
    score: int
    reasons: list[LinkReason] = Field(default_factory=list)


class DeviceBanBrief(BaseModel):
    id: int
    expires_at: datetime | None = None
    permanent: bool
    scope: Literal["all", "register"]


class SignalRead(BaseModel):
    id: int
    kind: Kind
    masked: str
    ip: str | None = None
    prefix: str | None = None
    location: str | None = None
    platform: str | None = None
    first_seen_at: datetime
    last_seen_at: datetime
    seen_count: int
    registered_here: bool
    shared_accounts: int
    ban: DeviceBanBrief | None = None


class UserDevicesRead(BaseModel):
    user_id: int
    signals: list[SignalRead] = Field(default_factory=list)
    linked: list[LinkedAccount] = Field(default_factory=list)


class UserClusterRead(BaseModel):
    root_id: int
    accounts: list[LinkedAccount] = Field(default_factory=list)
    truncated: bool = False


class DeviceBanRead(BaseModel):
    id: int
    kind: Kind
    masked: str
    value: str | None = None
    scope: Literal["all", "register"]
    reason: str | None = None
    status: Literal["active", "expired", "lifted"]
    source: Literal["admin", "person", "propagated"]
    created_at: datetime
    created_by: AdminBrief | None = None
    expires_at: datetime | None = None
    permanent: bool
    lifted_at: datetime | None = None
    lifted_by: AdminBrief | None = None
    source_user: AdminBrief | None = None
    affected_accounts: int = 0


class DeviceBanPage(BaseModel):
    items: list[DeviceBanRead] = Field(default_factory=list)
    total: int = 0


class DeviceBanCreate(BaseModel):
    """按信号行封（signal_id），或者直接给 kind + value（IP / 网段 / 完整哈希）。取值在服务层校验（422 invalid_device_ban）。"""

    signal_id: int | None = None
    kind: Any = None
    value: Any = None
    reason: str | None = Field(default=None, max_length=500)
    duration_hours: int | None = None
    permanent: bool = False
    scope: Any = None


class DeviceBanLift(BaseModel):
    note: str | None = Field(default=None, max_length=500)


class PersonBanPreviewRequest(BaseModel):
    user_id: int
    include_linked: bool = False
    include_fingerprint: bool = True
    include_ip: bool = False


class PersonBanRequest(PersonBanPreviewRequest):
    reason: str = Field(default="", max_length=500)
    duration_hours: int | None = None
    permanent: bool = False
    ban_accounts: bool = True
    ip_duration_hours: int | None = None


class PersonBanAccount(LinkedAccount):
    will_ban: bool
    skip_reason: Literal["admin", "already_banned"] | None = None


class PersonBanSignal(BaseModel):
    kind: Kind
    masked: str
    value: str | None = None
    accounts: int
    already_banned: bool


class PersonBanPreview(BaseModel):
    accounts: list[PersonBanAccount] = Field(default_factory=list)
    signals: list[PersonBanSignal] = Field(default_factory=list)
    ip_duration_hours: int


class PersonBanSkipped(BaseModel):
    user_id: int
    reason: str | None = None


class PersonBanResult(BaseModel):
    bans: list[DeviceBanRead] = Field(default_factory=list)
    banned_user_ids: list[int] = Field(default_factory=list)
    skipped: list[PersonBanSkipped] = Field(default_factory=list)


class DeviceBanSettings(BaseModel):
    max_accounts_per_device: int
    max_accounts_per_fingerprint: int
    ip_registrations_per_day: int
    require_device_for_register: bool
    propagate_enabled: bool
    propagate_min_ban_days: int


class DeviceBanSettingsWrite(BaseModel):
    """可以整份提交，也可以只传要改的。范围在服务层校验（422 invalid_settings，detail.field 指出哪一项）。"""

    max_accounts_per_device: Any = None
    max_accounts_per_fingerprint: Any = None
    ip_registrations_per_day: Any = None
    require_device_for_register: Any = None
    propagate_enabled: Any = None
    propagate_min_ban_days: Any = None
