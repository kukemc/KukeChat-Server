"""群升级、群文件、群待办、聊天记录搜索的接口结构，契约见 docs/group-plus.md。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.schemas.message import MessageRead
from app.schemas.shop import ShopBalance
from app.schemas.user import UserPublic


class TierRead(BaseModel):
    key: str
    name: str
    rank: int
    member_limit: int
    message_limit: int
    file_enabled: bool
    file_quota_bytes: int | None = None
    file_temp_max_bytes: int | None = None
    featured_limit: int
    price_month: int = 0
    price_year: int = 0


class PlansRead(BaseModel):
    enabled: bool = True
    year_months_paid: int
    month_days: int
    year_days: int
    file_max_bytes: int  # 旧字段：各档位临时文件上限里最大的
    file_temp_ttl_hours: int = 24
    file_ttl_options_hours: list[int]
    file_grace_days: int
    tiers: list[TierRead]


class PlusLimits(BaseModel):
    # 这个群现在的人数上限；null = 不限（后台设的，见 member_limit_unlimited）
    member_limit: int | None = None
    message_limit: int
    file_enabled: bool
    file_quota_bytes: int | None = None
    file_temp_max_bytes: int | None = None
    featured_limit: int
    # 人数不限（后台给这个群单独设的）；为 true 时 member_limit 是 null
    member_limit_unlimited: bool = False
    # 后台单独设的人数上限：null 跟随档位 / 0 不限 / 正数
    member_limit_override: int | None = None


class PlusUsage(BaseModel):
    member_count: int = 0
    featured_count: int = 0
    file_bytes_used: int = 0
    file_count: int = 0


class FileQuota(BaseModel):
    state: str
    tier: str | None = None
    grace_until: datetime | None = None
    quota_bytes: int | None = None
    used_bytes: int = 0
    file_count: int = 0
    default_ttl_hours: int = 24
    free_bytes: int = 0
    temp_max_bytes: int | None = None
    temp_ttl_hours: int = 24
    max_file_bytes: int
    # 直传直下：true 时用 /files/uploads 和 /files/{id}/link，false 时走 multipart 上传和 /download
    direct: bool = False
    # 档位能不能用文件夹（Pro+ / MAX）
    folders_enabled: bool = False


class PlusRead(BaseModel):
    conversation_id: int
    tier: str
    tier_name: str
    expires_at: datetime | None = None
    # 永久档位（后台给的，expires_at 是 2999 年的哨兵值）：不能再购买
    permanent: bool = False
    limits: PlusLimits
    usage: PlusUsage
    files: FileQuota
    can_purchase: bool = False


class PlusQuoteRequest(BaseModel):
    """取值在服务层校验（返回带 code 的错误）。"""

    tier: Any = None
    period: Any = None


class PlusPurchaseRequest(BaseModel):
    tier: Any = None
    period: Any = None
    confirm_forfeit: bool = False
    expected_price: Any = None


class PlusQuoteRead(BaseModel):
    conversation_id: int
    action: str
    tier: str
    period: str
    days: int
    price: int
    current_tier: str
    current_expires_at: datetime | None = None
    new_expires_at: datetime
    forfeited_seconds: int = 0
    forfeited_days: int = 0
    forfeit_notice: str | None = None
    balance: int = 0
    enough: bool = False


class GroupPlusRecordRead(BaseModel):
    id: int
    conversation_id: int
    source: str
    action: str
    tier: str
    period: str | None = None
    days: int = 0
    amount: int = 0
    prev_tier: str = "free"
    prev_expires_at: datetime | None = None
    new_tier: str = "free"
    new_expires_at: datetime | None = None
    # 这条记录给出的是永久档位
    permanent: bool = False
    forfeited_days: int = 0
    user: UserPublic | None = None
    note: str | None = None
    created_at: datetime
    # 只有后台的流水里有
    conversation_title: str | None = None


class PlusStateRead(BaseModel):
    conversation_id: int
    plus_tier: str
    plus_expires_at: datetime | None = None
    plus_permanent: bool = False


class PlusPurchaseResult(BaseModel):
    record: GroupPlusRecordRead
    conversation: PlusStateRead
    plus: PlusRead
    balance: ShopBalance


class PlusRecordPage(BaseModel):
    items: list[GroupPlusRecordRead] = Field(default_factory=list)
    total: int = 0


# ---------------------------------------------------------------- 群文件


class GroupFileRead(BaseModel):
    id: int
    conversation_id: int
    name: str
    size: int
    content_type: str
    uploader: UserPublic | None = None
    uploader_id: int | None = None
    created_at: datetime
    expires_at: datetime | None = None
    permanent: bool
    temporary: bool = False
    expired: bool = False
    message_id: int | None = None
    download_path: str
    # 所在的文件夹；根目录为 null（5.5）
    folder_id: int | None = None
    folder_name: str | None = None


class GroupFileFolderRead(BaseModel):
    id: int
    conversation_id: int
    name: str
    file_count: int = 0
    total_size: int = 0
    created_by: UserPublic | None = None
    created_by_id: int | None = None
    created_at: datetime
    updated_at: datetime


class GroupFileFolderList(BaseModel):
    items: list[GroupFileFolderRead] = Field(default_factory=list)
    limit: int = 50
    can_manage_folders: bool = False
    can_tidy_folders: bool = False


class GroupFileFolderWrite(BaseModel):
    name: Any = None


class GroupFileFolderDeleted(BaseModel):
    folder_id: int
    moved_to_root: int = 0


class GroupFileMoveRequest(BaseModel):
    file_ids: list[int] = Field(min_length=1, max_length=100)
    folder_id: int | None = None


class GroupFileMoveResult(BaseModel):
    folder_id: int | None = None
    moved: int = 0
    items: list["GroupFileRead"] = Field(default_factory=list)


class GroupFileUploadResult(BaseModel):
    file: GroupFileRead
    message: MessageRead | None = None


class GroupFileUploadRequest(BaseModel):
    name: str = Field(max_length=1024)
    size: int
    content_type: str | None = Field(default=None, max_length=255)
    permanent: bool = False
    folder_id: int | None = None


class GroupFileUploadTicket(BaseModel):
    upload_id: str
    url: str
    method: str = "PUT"
    headers: dict[str, str] = Field(default_factory=dict)
    expires_at: datetime
    temporary: bool = False
    # 预估会放进的文件夹：预估是临时文件时为 null（临时文件不进文件夹）；以 complete 的结果为准
    folder_id: int | None = None


class GroupFileUploadCompleteRequest(BaseModel):
    announce: bool = True


class GroupFileLink(BaseModel):
    url: str
    expires_at: datetime


class GroupFilePage(BaseModel):
    items: list[GroupFileRead] = Field(default_factory=list)
    total: int = 0
    quota: FileQuota
    # 文件夹（5.5）：folders 是这个群的全部文件夹；folder 是现在看的文件夹（根目录 / 搜索为 null）
    folder_id: int | None = None
    folder: GroupFileFolderRead | None = None
    folders: list[GroupFileFolderRead] = Field(default_factory=list)
    folder_limit: int = 50
    # 搜索模式（带了 q 或 uploader_id）：跨所有文件夹搜，忽略 folder_id
    search: bool = False
    can_manage_folders: bool = False
    can_tidy_folders: bool = False


class GroupFilePatch(BaseModel):
    permanent: Any = None


# ---------------------------------------------------------------- 群待办


class GroupTodoSender(BaseModel):
    id: int | None = None
    display_name: str = ""
    avatar_url: str | None = None
    title: str | None = None


class GroupTodoSnapshot(BaseModel):
    type: str
    content: str = ""
    metadata: dict[str, Any] | None = None
    sender: GroupTodoSender
    created_at: datetime | None = None


class GroupTodoSetBy(BaseModel):
    id: int | None = None
    display_name: str = ""
    avatar_url: str | None = None


class GroupTodoRead(BaseModel):
    conversation_id: int
    message_id: int | None = None
    message_exists: bool = True
    recalled: bool = False
    summary: str = ""
    snapshot: GroupTodoSnapshot
    set_by: GroupTodoSetBy
    set_at: datetime


class GroupTodoEnvelope(BaseModel):
    todo: GroupTodoRead | None = None


class GroupTodoSetRequest(BaseModel):
    message_id: Any = None


class GroupTodoSetResult(BaseModel):
    todo: GroupTodoRead
    message: MessageRead


# ---------------------------------------------------------------- 搜索


class SearchConversation(BaseModel):
    id: int
    type: str
    title: str | None = None
    display_title: str | None = None
    avatar_url: str | None = None
    plus_tier: str = "free"
    plus_expires_at: datetime | None = None
    plus_permanent: bool = False
    direct_user: UserPublic | None = None


class SearchHit(BaseModel):
    message: MessageRead
    conversation: SearchConversation
    category: str
    snippet: str = ""
    snippet_highlights: list[list[int]] = Field(default_factory=list)
    highlights: list[list[int]] = Field(default_factory=list)


class SearchPage(BaseModel):
    items: list[SearchHit] = Field(default_factory=list)
    total: int = 0
    total_capped: bool = False
    limit: int
    offset: int = 0
    has_more: bool = False
    next_before_id: int | None = None


class SearchSummaryItem(BaseModel):
    conversation: SearchConversation
    count: int
    latest_message_id: int
    latest_at: datetime | None = None


class SearchSummary(BaseModel):
    total: int = 0
    items: list[SearchSummaryItem] = Field(default_factory=list)


# ---------------------------------------------------------------- 后台


class AdminGroupPlusSettings(BaseModel):
    enabled: bool = True
    prices: dict[str, int]
    year_months_paid: int
    defaults: dict[str, int]
    tiers: list[TierRead]
    file_max_bytes: int
    file_grace_days: int


class AdminGroupPlusSettingsWrite(BaseModel):
    """取值在服务层校验（422 invalid_settings 带 field）。"""

    enabled: Any = None
    price_pro: Any = None
    price_pro_plus: Any = None
    price_max: Any = None


class AdminGroupPlusGrant(BaseModel):
    conversation_id: Any = None
    tier: Any = None
    days: Any = None
    # true = 送成永久（days 不用传）；tier = free 时忽略
    permanent: Any = None
    note: str | None = Field(default=None, max_length=500)


class AdminGroupPlusGrantResult(BaseModel):
    record: GroupPlusRecordRead
    conversation: PlusStateRead
    reason: str


class AdminGroupPlusGroup(BaseModel):
    conversation_id: int
    title: str | None = None
    avatar_url: str | None = None
    member_count: int = 0
    owner: dict[str, Any] | None = None
    tier: str
    recorded_tier: str
    expires_at: datetime | None = None
    days_left: int = 0
    expired: bool = False
    permanent: bool = False
    # 现在的人数上限（null = 不限）、是否不限、后台单独设的值（null 跟随档位 / 0 不限 / 正数）
    member_limit: int | None = None
    member_limit_unlimited: bool = False
    member_limit_override: int | None = None
    files_state: str = "locked"
    featured_count: int = 0


class AdminGroupPlusGroupPage(BaseModel):
    items: list[AdminGroupPlusGroup] = Field(default_factory=list)
    total: int = 0


class AdminGroupMemberLimitWrite(BaseModel):
    """null = 跟随档位，0 = 不限，1–100000 = 自定义。取值在服务层校验（422 invalid_member_limit）。"""

    member_limit_override: Any = None


class AdminGroupMemberLimitResult(BaseModel):
    group: AdminGroupPlusGroup
    conversation: PlusStateRead
    changed: bool = False
    previous: int | None = None


class AdminGroupPlusRecordPage(BaseModel):
    items: list[GroupPlusRecordRead] = Field(default_factory=list)
    total: int = 0
    summary: dict[str, int] = Field(default_factory=dict)
