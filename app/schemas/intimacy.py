"""好友亲密度与好友成就（docs/intimacy.md）的请求响应模型。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from app.schemas.user import UserPublic


class BadgeRef(BaseModel):
    series: str
    series_name: str
    level: int
    name: str
    tier: int
    icon: str
    unlocked_at: datetime | None = None


class IntimacySource(BaseModel):
    key: str
    label: str
    cap: int | None = None
    detail: str


class IntimacyLevelRule(BaseModel):
    level: int
    name: str
    threshold: int


class SeriesLevelRule(BaseModel):
    level: int
    tier: int
    name: str | None = None
    threshold: int | None = None


class SeriesRule(BaseModel):
    key: str
    hidden: bool
    name: str | None = None
    condition: str | None = None
    icon: str
    levels: list[SeriesLevelRule]


class IntimacyRules(BaseModel):
    enabled: bool
    daily_cap: int
    sources: list[IntimacySource]
    levels: list[IntimacyLevelRule]
    series: list[SeriesRule]
    total_badges: int


class FriendIntimacy(BaseModel):
    rank: int
    friend: UserPublic
    intimacy: int
    level: int
    level_name: str
    today_gained: int
    streak_days: int
    badge: BadgeRef | None = None
    badge_count: int
    new_badges: int
    friends_since: datetime | None = None


class FriendIntimacyList(BaseModel):
    enabled: bool
    daily_cap: int
    items: list[FriendIntimacy]
    total: int


class IntimacyLevelView(BaseModel):
    level: int
    name: str
    threshold: int
    next_level: int | None = None
    next_name: str | None = None
    next_threshold: int | None = None


class IntimacyToday(BaseModel):
    date: str
    gained: int
    counted: int
    cap: int
    chat: int
    gift: int
    streak: int
    birthday: int
    messages_mine: int
    messages_theirs: int
    chat_cap: int
    gift_cap: int


class IntimacyStats(BaseModel):
    messages_total: int
    messages_mine: int
    messages_theirs: int
    mutual_days: int
    streak_days: int
    best_streak: int
    night_days: int
    dawn_days: int
    weekend_days: int
    best_day_messages: int
    gift_count: int
    birthday_gifts: int


class SeriesLevelView(BaseModel):
    level: int
    name: str | None = None
    tier: int
    threshold: int | None = None
    unlocked_at: datetime | None = None
    is_new: bool = False


class SeriesView(BaseModel):
    key: str
    hidden: bool
    revealed: bool
    name: str | None = None
    condition: str | None = None
    icon: str
    current: int | None = None
    level: int
    max_level: int
    next_threshold: int | None = None
    levels: list[SeriesLevelView]


class FriendIntimacyDetail(BaseModel):
    friend: UserPublic
    friends_since: datetime | None = None
    friend_days: int
    intimacy: int
    level: IntimacyLevelView
    rank: int | None = None
    friend_count: int
    display: str
    badge: BadgeRef | None = None
    today: IntimacyToday
    stats: IntimacyStats
    badge_count: int
    total_badges: int
    series: list[SeriesView]


class BadgeDisplayWrite(BaseModel):
    badge: str


class BadgeDisplayResult(BaseModel):
    friend_id: int
    display: str
    badge: BadgeRef | None = None


class SeenResult(BaseModel):
    cleared: int


class IntimacyHistoryDay(BaseModel):
    date: str
    gained: int
    chat: int
    gift: int
    streak: int
    birthday: int
    mutual: bool


class IntimacyHistory(BaseModel):
    items: list[IntimacyHistoryDay]


# ---------------------------------------------------------------- 后台


class AdminIntimacySettings(BaseModel):
    enabled: bool
    daily_cap: int
    chat_mutual: int
    chat_per_round: int
    chat_cap: int
    gift_base: int
    gift_coins_per_point: int
    gift_cap: int
    streak_min: int
    streak_bonus: int
    birthday_bonus: int


class AdminIntimacySettingsWrite(BaseModel):
    enabled: bool | None = None
    daily_cap: int | None = None
    chat_mutual: int | None = None
    chat_per_round: int | None = None
    chat_cap: int | None = None
    gift_base: int | None = None
    gift_coins_per_point: int | None = None
    gift_cap: int | None = None
    streak_min: int | None = None
    streak_bonus: int | None = None
    birthday_bonus: int | None = None


class AdminLevelCount(BaseModel):
    level: int
    name: str
    threshold: int
    count: int


class AdminSeriesLevel(BaseModel):
    level: int
    name: str
    tier: int
    threshold: int
    count: int


class AdminSeries(BaseModel):
    key: str
    name: str
    condition: str
    icon: str
    hidden: bool
    stat: str
    levels: list[AdminSeriesLevel]


class AdminIntimacyOverview(BaseModel):
    friend_pairs: int
    bonds: int
    total_intimacy: int
    active_today: int
    gained_today: int
    levels: list[AdminLevelCount]
    series: list[AdminSeries]
    total_badges: int
    unlocked_badges: int
    backfill: dict | None = None


class AdminBond(BaseModel):
    friend: UserPublic
    friends_since: datetime | None = None
    intimacy: int
    level: int
    level_name: str
    badge_count: int
    updated_at: datetime | None = None


class AdminUserBonds(BaseModel):
    user: UserPublic
    items: list[AdminBond]
    total: int


class AdminIntimacyAdjust(BaseModel):
    user_id: int
    friend_id: int
    delta: int
    note: str


class AdminIntimacyAdjustResult(BaseModel):
    intimacy: int
    level: int
    level_name: str
    new_badge_count: int


class AdminBackfillResult(BaseModel):
    pairs: int
    badges: int
    skipped: int
