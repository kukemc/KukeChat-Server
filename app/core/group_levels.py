"""群等级曲线与等级头衔。纯函数，模型和服务层共用。

升级所需经验按等级线性增长（旧曲线是二次的，活跃用户到 10 级要四个月）：
升到下一级需要 40 + 12 × (当前等级 - 1)。累计到 10 级约 800，20 级约 2800，
50 级约 1.6 万，100 级约 6.2 万。
"""

from __future__ import annotations

MAX_LEVEL = 100
LEVEL_BASE_EXP = 40
LEVEL_STEP_EXP = 12

MAX_LEVEL_TITLE_TIERS = 12
MAX_LEVEL_TITLE_LENGTH = 8
# 没有自定义时用的头衔分档：(起始等级, 头衔)
DEFAULT_LEVEL_TITLES: tuple[tuple[int, str], ...] = (
    (1, "萌新"),
    (11, "活跃"),
    (21, "熟面孔"),
    (31, "骨干"),
    (46, "元老"),
    (61, "传说"),
    (81, "殿堂"),
)


def exp_required_for_level(level: int) -> int:
    """从 level 升到 level + 1 需要的经验。"""
    return LEVEL_BASE_EXP + LEVEL_STEP_EXP * max(0, level - 1)


def total_exp_for_level(level: int) -> int:
    """从 1 级升到 level 累计需要的经验。"""
    steps = max(0, min(level, MAX_LEVEL) - 1)
    return LEVEL_BASE_EXP * steps + LEVEL_STEP_EXP * steps * (steps - 1) // 2


def level_from_exp(exp: int) -> int:
    level = 1
    remaining = max(0, exp)
    while level < MAX_LEVEL:
        required = exp_required_for_level(level)
        if remaining < required:
            break
        remaining -= required
        level += 1
    return level


def next_level_remaining_exp(exp: int) -> int:
    """距离下一级还差多少经验；满级返回 0。"""
    level = level_from_exp(exp)
    if level >= MAX_LEVEL:
        return 0
    return total_exp_for_level(level + 1) - max(0, exp)


def default_level_titles() -> list[dict[str, object]]:
    return [{"min_level": min_level, "title": title} for min_level, title in DEFAULT_LEVEL_TITLES]


def level_title_for(level: int, titles: list[dict[str, object]]) -> str | None:
    """某个等级对应的头衔：起始等级不超过它的最后一档。"""
    current: str | None = None
    for item in titles:
        if int(item["min_level"]) <= level:  # type: ignore[arg-type]
            current = str(item["title"])
    return current
