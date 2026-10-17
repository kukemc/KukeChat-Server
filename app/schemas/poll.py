from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.user import UserPublic


class PollCreate(BaseModel):
    title: str
    options: list[str]
    multiple: bool = False
    anonymous: bool = False
    # 为空表示手动结束前一直有效
    closes_at: datetime | None = None


class PollOptionState(BaseModel):
    id: int
    votes: int


class PollState(BaseModel):
    """投票的实时状态。my_option_ids 只在接口返回里有意义，实时事件里不带。"""

    conversation_id: int
    message_id: int
    poll_id: int
    options: list[PollOptionState]
    total_voters: int
    closed: bool
    closed_at: datetime | None = None
    my_option_ids: list[int] = Field(default_factory=list)


class PollVoteRequest(BaseModel):
    # 完整的选项集合；空数组表示撤票
    option_ids: list[int] = Field(default_factory=list, max_length=10)


class PollVoterPage(BaseModel):
    items: list[UserPublic] = Field(default_factory=list)
    total: int = 0


class GroupEmojiRead(BaseModel):
    id: int
    conversation_id: int
    name: str
    url: str
    content_type: str
    created_by_id: int | None = None
    created_at: datetime


class GroupEmojiCreate(BaseModel):
    name: str
    # 先用 /uploads/message-image 上传，再把返回的地址填在这里
    image_url: str = Field(max_length=500)


class GroupEmojiOrder(BaseModel):
    # 这个群全部表情的 id，按想要的顺序；必须一个不多一个不少
    emoji_ids: list[int] = Field(min_length=1, max_length=200)


class GroupEmojiUpdate(BaseModel):
    name: str
