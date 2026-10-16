"""好友亲密度的等级表和好友成就目录（docs/intimacy.md 第 1.1、2 节）。只有数据和纯函数，不查库。

台面上的成就名字都是中性的；小马宝莉的梗只放在隐藏成就（彩蛋）里，解锁之前谁也看不到。

成就按「系列」组织：一个系列盯着一项统计（互发消息总数、深夜互聊天数……），统计每跨过一个门槛就解锁这一系列的下一级。
徽章的外观由客户端画：`icon` 是客户端内置图形的名字，`tier` 决定外框（1 铜 / 2 银 / 3 金 / 4 彩虹）。
目录写在代码里而不是库里：图形是客户端内置的，后台改了名字也画不出新图形。改目录（加系列、加等级、改门槛）只改这里，
已经解锁的不会被收回 —— 门槛调高了，之前按旧门槛解锁的徽章照样留着。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Level:
    level: int
    name: str
    threshold: int


@dataclass(frozen=True)
class BadgeLevel:
    level: int
    name: str
    tier: int
    threshold: int


@dataclass(frozen=True)
class Series:
    key: str
    name: str
    # 给人看的条件说明，{n} 换成门槛
    condition: str
    # 统计项，见 services/intimacy.py 的 bond_stats
    stat: str
    icon: str
    levels: tuple[BadgeLevel, ...]
    # 隐藏成就：解锁第一级之前，名字、条件、门槛都不给客户端
    hidden: bool = False


# ---------------------------------------------------------------- 亲密度等级

# 每天最多涨 daily_cap（默认 40）点：顶级要一年多的天天互动
LEVELS: tuple[Level, ...] = (
    Level(0, "点头之交", 0),
    Level(1, "初识", 20),
    Level(2, "熟络", 100),
    Level(3, "好友", 300),
    Level(4, "密友", 700),
    Level(5, "挚友", 1500),
    Level(6, "知己", 3000),
    Level(7, "莫逆之交", 6000),
    Level(8, "灵魂之友", 10000),
    Level(9, "永恒之友", 16000),
)
MAX_LEVEL = LEVELS[-1].level


def level_for(intimacy: int) -> Level:
    current = LEVELS[0]
    for item in LEVELS:
        if intimacy >= item.threshold:
            current = item
    return current


def next_level(level: int) -> Level | None:
    return LEVELS[level + 1] if 0 <= level < MAX_LEVEL else None


# ---------------------------------------------------------------- 好友成就

T1, T2, T3, T4 = 1, 2, 3, 4

SERIES: tuple[Series, ...] = (
    Series(
        "clover", "友谊幸运草", "你们互相发的消息累计 {n} 条", "messages_total", "clover",
        (
            BadgeLevel(1, "友谊幸运草", T1, 30),
            BadgeLevel(2, "三叶之约", T1, 300),
            BadgeLevel(3, "四叶奇迹", T2, 1500),
            BadgeLevel(4, "幸运草原", T3, 6000),
            BadgeLevel(5, "永恒幸运草", T4, 20000),
        ),
    ),
    Series(
        "together", "朝夕相伴", "累计 {n} 天互相聊过天", "mutual_days", "sparkle",
        (
            BadgeLevel(1, "初次相识", T1, 7),
            BadgeLevel(2, "常来常往", T1, 30),
            BadgeLevel(3, "心照不宣", T2, 60),
            BadgeLevel(4, "如影随形", T2, 120),
            BadgeLevel(5, "形影不离", T3, 240),
            BadgeLevel(6, "岁月同行", T4, 500),
        ),
    ),
    Series(
        "streak", "约定", "连续 {n} 天互相聊天", "best_streak", "rings",
        (
            BadgeLevel(1, "三日之约", T1, 3),
            BadgeLevel(2, "一周不断", T1, 7),
            BadgeLevel(3, "月月相守", T2, 30),
            BadgeLevel(4, "百日同行", T3, 100),
            BadgeLevel(5, "年轮", T4, 365),
        ),
    ),
    Series(
        "night", "月下夜话", "{n} 天在深夜（0:00–4:59）互相发过消息", "night_days", "moon",
        (
            BadgeLevel(1, "月下夜话", T1, 1),
            BadgeLevel(2, "夜猫搭档", T2, 7),
            BadgeLevel(3, "星河守望", T3, 30),
            BadgeLevel(4, "永夜同盟", T4, 100),
        ),
    ),
    Series(
        "dawn", "晨光", "{n} 天在清晨（5:00–7:59）互相发过消息", "dawn_days", "sun",
        (
            BadgeLevel(1, "第一缕晨光", T1, 1),
            BadgeLevel(2, "早起二人组", T2, 7),
            BadgeLevel(3, "晨曦之约", T3, 30),
            BadgeLevel(4, "永昼之光", T4, 100),
        ),
    ),
    Series(
        "chatter", "话匣子", "有一天互发消息达到 {n} 条", "best_day_messages", "balloons",
        (
            BadgeLevel(1, "聊不停", T1, 100),
            BadgeLevel(2, "话匣子", T2, 300),
            BadgeLevel(3, "停不下来", T3, 666),
        ),
    ),
    Series(
        "weekend", "周末搭子", "{n} 个周末日（周六、周日）互相聊过天", "weekend_days", "weekend",
        (
            BadgeLevel(1, "周末见", T1, 4),
            BadgeLevel(2, "周末搭子", T2, 20),
            BadgeLevel(3, "假日常客", T3, 52),
            BadgeLevel(4, "无尽周末", T4, 150),
        ),
    ),
    Series(
        "gifts", "心意", "互相送过 {n} 次礼物", "gift_count", "gems",
        (
            BadgeLevel(1, "第一份心意", T1, 1),
            BadgeLevel(2, "礼尚往来", T2, 10),
            BadgeLevel(3, "心意满满", T3, 50),
            BadgeLevel(4, "宝藏收藏家", T4, 200),
        ),
    ),
    Series(
        "birthday", "生日派对", "在对方生日那天送过礼物，累计 {n} 次", "birthday_gifts", "cake",
        (
            BadgeLevel(1, "生日快乐", T2, 1),
            BadgeLevel(2, "年年有你", T3, 3),
            BadgeLevel(3, "岁岁相伴", T4, 6),
        ),
    ),
    Series(
        "bond", "羁绊", "亲密度达到 {n}", "intimacy", "hearts",
        (
            BadgeLevel(1, "友谊的火花", T2, 1000),
            BadgeLevel(2, "默契之信", T3, 5000),
        ),
    ),
    Series(
        "oldfriend", "老朋友", "成为好友满 {n} 天", "friend_days", "house",
        (
            BadgeLevel(1, "新邻居", T1, 30),
            BadgeLevel(2, "老朋友", T2, 100),
            BadgeLevel(3, "一周年", T3, 365),
            BadgeLevel(4, "千日之交", T4, 1000),
        ),
    ),
    # ------------------------------------------------------------ 隐藏成就（彩蛋）
    # 解锁之前接口不给名字、条件、门槛和图形，客户端画成问号。小马宝莉的梗只放在这里，而且只用冷门的说法，不出现角色名
    Series(
        "newyear", "零点钟声", "{n} 个元旦（1 月 1 日）互相聊过天", "newyear_days", "firework",
        (
            BadgeLevel(1, "零点钟声", T3, 1),
            BadgeLevel(2, "岁岁年年", T4, 3),
        ),
        hidden=True,
    ),
    Series(
        "sunmoon", "千年之后", "{n} 天里，你们既在深夜聊过、又在清晨聊过", "sunmoon_days", "sunmoon",
        (
            BadgeLevel(1, "千年之后", T3, 1),
            BadgeLevel(2, "长夜将尽", T4, 10),
        ),
        hidden=True,
    ),
    Series(
        "pinkie", "以糖为誓", "连续 {n} 天互相聊天，一天都没落下", "best_streak", "rings",
        (
            BadgeLevel(1, "以糖为誓", T3, 30),
        ),
        hidden=True,
    ),
    Series(
        "rainboom", "七色音障", "有一天互发消息达到 {n} 条", "best_day_messages", "rainbow",
        (
            BadgeLevel(1, "七色音障", T4, 1000),
        ),
        hidden=True,
    ),
    Series(
        "discord", "混沌茶会", "有一天在 0:00–4:59 互发消息达到 {n} 条", "best_night_messages", "chaos",
        (
            BadgeLevel(1, "混沌茶会", T3, 200),
        ),
        hidden=True,
    ),
    Series(
        "harmony", "和谐之元", "集齐 {n} 个不同系列的好友成就", "series_count", "harmony",
        (
            BadgeLevel(1, "和谐之元", T4, 6),
        ),
        hidden=True,
    ),
    Series(
        "magic", "友谊之书", "亲密度达到 {n}", "intimacy", "horn",
        (
            BadgeLevel(1, "友谊之书", T4, 15000),
        ),
        hidden=True,
    ),
)  # fmt: skip

# 「集齐几个系列」只数台面上的系列（隐藏的彩蛋不算），见 services/intimacy.py 的 _sync_badges
VISIBLE_SERIES: tuple[str, ...] = tuple(series.key for series in SERIES if not series.hidden)

SERIES_BY_KEY: dict[str, Series] = {series.key: series for series in SERIES}
STATS: tuple[str, ...] = tuple(dict.fromkeys(series.stat for series in SERIES))
TOTAL_BADGES = sum(len(series.levels) for series in SERIES)


def reached(series: Series, value: int) -> int:
    """这一系列按统计值 value 能到第几级（0 = 一级都没到）。"""
    level = 0
    for item in series.levels:
        if value >= item.threshold:
            level = item.level
    return level


def badge_level(series_key: str, level: int) -> BadgeLevel | None:
    series = SERIES_BY_KEY.get(series_key)
    if series is None or not 1 <= level <= len(series.levels):
        return None
    return series.levels[level - 1]
