from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.conversation import ConversationMemberRead
from app.schemas.user import UserPublic


GroupLeaderboardType = Literal["activity", "level", "checkin_streak", "checkin_total", "message"]
GroupLeaderboardPeriod = Literal["today", "week", "month", "all"]


class GroupCheckinCreate(BaseModel):
    message: str | None = Field(default=None, max_length=80)


class GroupCheckinRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    conversation_id: int
    user_id: int
    user: UserPublic | None = None
    checkin_date: date
    streak_days: int
    exp_awarded: int
    message: str | None = None
    created_at: datetime


class GroupCheckinStatusRead(BaseModel):
    conversation_id: int
    checked_in_today: bool
    today_checkin: GroupCheckinRead | None = None
    total_checkins: int
    current_streak: int
    best_streak: int
    level: int
    level_exp: int
    next_level_exp: int


class GroupLeaderboardItem(BaseModel):
    rank: int
    user_id: int
    user: UserPublic
    member: ConversationMemberRead
    level: int
    level_exp: int
    activity_score: int
    total_checkins: int
    current_checkin_streak: int
    best_checkin_streak: int
    message_count: int = 0
    checkin_count: int = 0


class GroupLeaderboardRead(BaseModel):
    conversation_id: int
    type: GroupLeaderboardType
    period: GroupLeaderboardPeriod
    items: list[GroupLeaderboardItem]
    my_rank: GroupLeaderboardItem | None = None
