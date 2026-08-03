from __future__ import annotations

from pydantic import BaseModel

from app.schemas.conversation import GroupDiscoveryRead
from app.schemas.user import CcwCreatorRead


class HomeStatsRead(BaseModel):
    registered_users: int
    messages: int
    messages_last_7_days: int
    groups: int
    public_groups: int
    public_posts: int
    friendships: int
    post_likes: int


class HomeGroupRead(GroupDiscoveryRead):
    recent_message_count: int = 0
    heat_score: int = 0


class HomeRead(BaseModel):
    stats: HomeStatsRead
    groups: list[HomeGroupRead]
    creators: list[CcwCreatorRead]
    creator_total: int
    recommendation_seed: int
