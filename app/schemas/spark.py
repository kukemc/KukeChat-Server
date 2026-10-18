"""好友火花（docs/sparks.md）的响应模型。"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.shop import ShopBalance


class SparkView(BaseModel):
    # none 还没有火花 / lit 今天已续 / pending 今天还没续 / dying 昨天没续上（快熄灭了，今天续上还能接着数）/ out 已熄灭
    status: Literal["none", "lit", "pending", "dying", "out"] = "none"
    days: int = 0
    # 0 = 没有火花；1 火苗 / 2 小火花 / 3 火花 / 4 大火花 / 5 烈焰 / 6 永恒之火
    level: int = 0
    level_name: str | None = None
    # ember / spark / flame / blaze / inferno / eternal，客户端按它画图标
    level_key: str | None = None
    today_lit: bool = False
    i_spoke_today: bool = False
    friend_spoke_today: bool = False
    dying: bool = False
    best_days: int = 0
    next_level: int | None = None
    next_level_name: str | None = None
    next_level_days: int | None = None
    days_to_next: int | None = None
    lit_on: date | None = None
    extinguished_at: datetime | None = None
    # 熄灭前的天数（能恢复的）；没有为 0
    lost_days: int = 0
    lost_level_key: str | None = None
    can_relight: bool = False
    # 不能重燃的原因：not_out 没熄灭 / expired 超过期限 / cooldown 30 天内重燃过；能重燃为 null
    relight_block: Literal["not_out", "expired", "cooldown"] | None = None
    relight_cost: int = 0
    relight_deadline: datetime | None = None
    relight_cooldown_until: datetime | None = None
    relit_at: datetime | None = None


class SparkLevelRule(BaseModel):
    level: int
    name: str
    key: str
    min_days: int


class SparkRules(BaseModel):
    timezone: str
    levels: list[SparkLevelRule]
    grace_missed_days: int
    relight_cost: int
    relight_window_days: int
    relight_cooldown_days: int
    rules: list[str] = Field(default_factory=list)


class SparkDetail(BaseModel):
    friend_id: int
    conversation_id: int | None = None
    spark: SparkView
    rules: SparkRules


class SparkListItem(BaseModel):
    friend_id: int
    spark: SparkView


class SparkList(BaseModel):
    items: list[SparkListItem] = Field(default_factory=list)


class SparkRelightResult(BaseModel):
    friend_id: int
    restored_days: int
    spark: SparkView
    balance: ShopBalance
