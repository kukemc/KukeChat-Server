"""账号等级的响应 / 请求模型（docs/account-level.md）。活跃天对外一律是一位小数。"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field, SerializerFunctionWrapHandler, model_serializer

from app.schemas.user import UserPublic


class LevelIcons(BaseModel):
    crown: int = 0
    sun: int = 0
    moon: int = 0
    star: int = 0


# ---------------------------------------------------------------- 规则


class LevelIconRule(BaseModel):
    key: str
    label: str
    # 这个图标顶多少级
    levels: int


class LevelOnlineRule(BaseModel):
    minutes: int
    days: float


class _OmitEmpty(BaseModel):
    """没有值的可选字段在输出里整个省掉，而不是给 null（契约里这些项就是没有那个键）。"""

    @model_serializer(mode="wrap")
    def _omit_empty(self, handler: SerializerFunctionWrapHandler):  # 不标返回类型：标了的话接口文档里这个模型就只剩一个泛泛的 object
        return {key: value for key, value in handler(self).items() if value is not None}


class LevelBonusRule(_OmitEmpty):
    key: str
    label: str
    days: float
    # 有数量要求的加成才有：发言条数、连续天数
    target: int | None = None


class LevelMilestone(BaseModel):
    level: int
    days: int


class LevelRules(BaseModel):
    max_level: int
    formula: str
    icons: list[LevelIconRule]
    daily_cap: float
    online: list[LevelOnlineRule]
    bonuses: list[LevelBonusRule]
    milestones: list[LevelMilestone]


# ---------------------------------------------------------------- 我的等级 / 别人的等级卡


class LevelBonusState(_OmitEmpty):
    key: str
    earned: bool
    days: float
    # 有数量要求的加成才有：现在到多少了、要多少
    current: int | None = None
    target: int | None = None


class LevelToday(BaseModel):
    date: date
    online_minutes: int
    base: float
    bonuses: list[LevelBonusState]
    earned: float
    cap: float


class LevelMe(BaseModel):
    level: int
    active_days: float
    level_days: int
    next_level: int | None = None
    next_level_days: int | None = None
    days_to_next: float | None = None
    icons: LevelIcons
    streak: int
    rank: int | None = None
    today: LevelToday
    registered_days: int


class LevelCard(BaseModel):
    user_id: int
    level: int
    active_days: float
    icons: LevelIcons
    next_level_days: int | None = None
    days_to_next: float | None = None
    streak: int
    rank: int | None = None
    registered_days: int


# ---------------------------------------------------------------- 排行榜 / 历史


class LevelLeaderboardItem(BaseModel):
    rank: int
    user: UserPublic
    level: int
    active_days: float
    # 本月（UTC+8 自然月）新增的活跃天
    gained: float


class LevelLeaderboard(BaseModel):
    period: Literal["all", "month"]
    items: list[LevelLeaderboardItem]
    total: int
    me: LevelLeaderboardItem | None = None
    generated_at: datetime


class LevelHistoryItem(BaseModel):
    date: date
    online_minutes: int
    base: float
    bonus: float
    earned: float


class LevelHistory(BaseModel):
    items: list[LevelHistoryItem]


# ---------------------------------------------------------------- 后台


class AdminLevelUser(BaseModel):
    user: UserPublic
    level: int
    active_days: float
    streak: int
    # 最近 7 天（含今天）的明细，按日期正序
    recent: list[LevelHistoryItem]


class AdminLevelUserPage(BaseModel):
    items: list[AdminLevelUser]
    total: int


class AdminLevelAdjust(BaseModel):
    # 正数加、负数减，精确到 0.1 天；累计不会被减到 0 以下
    days: float = Field(ge=-100000, le=100000)
    note: str | None = Field(default=None, max_length=500)


class AdminLevelSettings(LevelRules):
    """全部阈值（只读）：/levels/rules 的内容，加上几项只和服务端有关的。"""

    tick_seconds: int
    tick_minutes: int
    leaderboard_cache_seconds: int
    notify_every: int
    # 老用户折算做完的时间；还没做为 null
    legacy_backfill_at: datetime | None = None
