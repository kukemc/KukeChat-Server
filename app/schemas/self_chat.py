"""「我的电脑 / 我的手机」临时文件的请求响应（docs/self-chat.md 第 5 节）。"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import AliasChoices, BaseModel, Field

from app.schemas.message import MessageRead


class SelfTransferCreate(BaseModel):
    name: str = Field(min_length=1, max_length=1024)
    size: int
    # 声明的类型，只用来显示图标；也认 mime
    content_type: str | None = Field(default=None, max_length=255, validation_alias=AliasChoices("content_type", "mime"))


class SelfTransferRead(BaseModel):
    id: int
    conversation_id: int
    name: str
    size: int
    content_type: str
    # 开单的设备：desktop / web / android / ios
    device: str
    # pending 还没 complete / ready 可以下载 / expired 过期了（或者撤回了那条消息）
    state: Literal["pending", "ready", "expired"]
    expired: bool
    created_at: datetime
    # pending 的上传单在这之前要 complete，否则作废
    upload_expires_at: datetime
    completed_at: datetime | None = None
    # complete 时间 + 24 小时；pending 时为 null
    expires_at: datetime | None = None
    message_id: int | None = None
    download_path: str


class SelfTransferUpload(BaseModel):
    # true：直接传对象存储（url 是预签名地址，不要带 Authorization）；false：传到后端（url 是 /api/v1/... 路径，带平时的登录和客户端头）
    direct: bool
    method: Literal["PUT"] = "PUT"
    url: str
    headers: dict[str, str] = Field(default_factory=dict)
    expires_at: datetime


class SelfTransferCreated(BaseModel):
    transfer_id: int
    transfer: SelfTransferRead
    upload: SelfTransferUpload


class SelfTransferContentResult(BaseModel):
    transfer_id: int
    uploaded_bytes: int


class SelfTransferComplete(BaseModel):
    # 乐观发送用的本地标记，和发普通消息一样原样带回（POST 返回的 message 和 message.created 里）
    client_nonce: str | None = Field(default=None, min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")


class SelfTransferCompleteResult(BaseModel):
    transfer: SelfTransferRead
    # 落下的那条文件消息。重复 complete 时是第一次落的那条；那条消息已经被修剪掉时为 null
    message: MessageRead | None = None


class SelfTransferLink(BaseModel):
    url: str
    expires_at: datetime


class SelfTransferList(BaseModel):
    items: list[SelfTransferRead] = Field(default_factory=list)
    max_file_bytes: int
    quota_bytes: int
    used_bytes: int
    free_bytes: int
    ttl_hours: int
    direct: bool
