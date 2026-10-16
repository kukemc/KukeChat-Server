"""发版服务的请求 / 响应模型（docs/releases.md）。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class ReleaseArtifactPublic(BaseModel):
    id: int
    source: str
    file_name: str
    url: str | None = None
    size: int | None = None
    sha256: str | None = None
    arch: str | None = None
    signature: str | None = None


class ReleasePublic(BaseModel):
    id: int
    platform: str
    channel: str
    version: str
    build: int
    changelog: str
    min_supported_version: str | None = None
    published_at: datetime | None = None
    artifacts: list[ReleaseArtifactPublic]
    artifact: ReleaseArtifactPublic | None = None


class ReleaseLatest(BaseModel):
    platform: str
    channel: str
    current: str | None = None
    update_available: bool
    forced: bool
    latest: ReleasePublic | None = None


class ReleaseHistory(BaseModel):
    items: list[ReleasePublic]
    total: int


class ReleaseUserBrief(BaseModel):
    id: int
    username: str
    nickname: str | None = None


class ReleaseArtifactAdmin(ReleaseArtifactPublic):
    status: str
    storage_key: str | None = None
    created_at: datetime


class ReleaseAdmin(BaseModel):
    id: int
    platform: str
    channel: str
    version: str
    build: int
    changelog: str
    min_supported_version: str | None = None
    published: bool
    published_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    created_by: ReleaseUserBrief | None = None
    artifacts: list[ReleaseArtifactAdmin]


class ReleaseAdminPage(BaseModel):
    items: list[ReleaseAdmin]
    total: int


class ReleaseConfig(BaseModel):
    direct_upload: bool
    max_artifact_bytes: int
    platforms: list[str]
    channels: list[str]


class ReleaseCreate(BaseModel):
    platform: str
    channel: str = "stable"
    version: str = Field(max_length=64)
    build: int = 0
    changelog: str = ""
    min_supported_version: str | None = Field(default=None, max_length=64)


class ReleaseUpdate(BaseModel):
    platform: str | None = None
    channel: str | None = None
    version: str | None = Field(default=None, max_length=64)
    build: int | None = None
    changelog: str | None = None
    min_supported_version: str | None = Field(default=None, max_length=64)


class ReleasePublish(BaseModel):
    force: bool = False


class ReleaseUploadCreate(BaseModel):
    file_name: str = Field(max_length=400)
    size: int
    sha256: str = Field(max_length=80)
    arch: str | None = Field(default=None, max_length=16)
    signature: str | None = Field(default=None, max_length=8000)


class ReleaseUploadTicket(BaseModel):
    method: str
    url: str
    headers: dict[str, str]
    expires_at: datetime


class ReleaseUploadCreated(BaseModel):
    artifact: ReleaseArtifactAdmin
    upload: ReleaseUploadTicket


class ReleaseExternalCreate(BaseModel):
    url: str = Field(max_length=1000)
    file_name: str | None = Field(default=None, max_length=400)
    size: int | None = None
    sha256: str | None = Field(default=None, max_length=80)
    arch: str | None = Field(default=None, max_length=16)
    signature: str | None = Field(default=None, max_length=8000)


class ReleaseArtifactUpdate(BaseModel):
    arch: str | None = Field(default=None, max_length=16)
    signature: str | None = Field(default=None, max_length=8000)
    url: str | None = Field(default=None, max_length=1000)
    size: int | None = None
    sha256: str | None = Field(default=None, max_length=80)


class ReleaseTokenItem(BaseModel):
    id: int
    label: str
    prefix: str
    created_at: datetime
    last_used_at: datetime | None = None
    expires_at: datetime | None = None
    revoked_at: datetime | None = None
    created_by: ReleaseUserBrief | None = None


class ReleaseTokenList(BaseModel):
    items: list[ReleaseTokenItem]


class ReleaseTokenCreate(BaseModel):
    label: str = Field(default="发版脚本", max_length=80)
    expires_in_days: int | None = Field(default=None, ge=1, le=3650)


class ReleaseTokenCreated(BaseModel):
    token: str
    item: ReleaseTokenItem
