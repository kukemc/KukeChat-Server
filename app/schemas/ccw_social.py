"""在共创世界上（docs/ccw-social.md）：「正在玩 / 正在创作」和从 CCW 用户查 KukeChat 名片。"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator

from app.schemas.user import UserPublic

ActivityKind = Literal["play", "create", "read"]
Friendship = Literal["self", "friend", "outgoing", "incoming", "none"]

CCW_OID = re.compile(r"^[0-9a-f]{24}$")
# 封面只认 CCW 自己的图床，别的地址一律丢掉（状态会显示在别人的界面上）
COVER_HOSTS = ("ccw.site", "xiguacity.cn", "aliyuncs.com", "myqcloud.com")


class ActivityUpdate(BaseModel):
    """脚本上报的活动。也是心跳：作品没变时只续期，开始时间不动。"""

    kind: ActivityKind
    # 作品或文章的 oid。创作一个还没保存过的新作品时没有 oid
    ccw_oid: str | None = None
    title: str = Field(min_length=1, max_length=80)
    cover_url: str | None = Field(default=None, max_length=500)

    @field_validator("ccw_oid")
    @classmethod
    def _oid(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip().lower()
        if not CCW_OID.match(value):
            raise ValueError("ccw_oid must be a 24-character CCW object id")
        return value

    @field_validator("title")
    @classmethod
    def _title(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value:
            raise ValueError("title is empty")
        return value

    @field_validator("cover_url")
    @classmethod
    def _cover(cls, value: str | None) -> str | None:
        if not value:
            return None
        parsed = urlparse(value)
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or not any(host == h or host.endswith("." + h) for h in COVER_HOSTS):
            return None
        return value


class ActivityRead(BaseModel):
    kind: ActivityKind
    ccw_oid: str | None = None
    title: str
    cover_url: str | None = None
    started_at: datetime
    updated_at: datetime
    # 过了这个时间还没收到新的推送，客户端就当它已经结束
    expires_at: datetime


class UserActivityRead(BaseModel):
    user_id: int
    activity: ActivityRead | None = None


class CcwUserCard(BaseModel):
    """CCW 主页上的 KukeChat 名片。没有账号、或对方关掉了「能从 CCW 主页找到我」时 has_account 为 false，其余为空。"""

    ccw_oid: str
    has_account: bool
    user: UserPublic | None = None
    online: bool = False
    activity: ActivityRead | None = None
    friendship: Friendship = "none"
    mutual_friend_count: int = 0
    mutual_group_count: int = 0


class CcwUserBrief(BaseModel):
    """评论区、粉丝列表这类批量场景只要这几项。"""

    ccw_oid: str
    user_id: int
    nickname: str | None = None
    avatar_url: str | None = None
    account_level: int = 0
    online: bool = False
    activity: ActivityRead | None = None


class CcwUserBatchRequest(BaseModel):
    oids: list[str] = Field(min_length=1, max_length=50)

    @field_validator("oids")
    @classmethod
    def _oids(cls, value: list[str]) -> list[str]:
        seen: list[str] = []
        for raw in value:
            oid = raw.strip().lower()
            if CCW_OID.match(oid) and oid not in seen:
                seen.append(oid)
        return seen
