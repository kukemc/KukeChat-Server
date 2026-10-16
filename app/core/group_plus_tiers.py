"""群升级的档位表（docs/group-plus.md 第 1 节）。只放常量和纯函数，不查库、不依赖别的模块：
会话序列化、修剪、精华上限、加人上限都用它。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

UTC = timezone.utc

MB = 1024 * 1024
# 空间文件没有单独的单文件上限（只受剩余空间限制）；放不进空间的可以传成临时文件，24 小时后过期，
# 大小上限按档位（file_temp_max_bytes）。临时文件不占空间配额。
FILE_TEMP_TTL_HOURS = 24
FILE_TTL_OPTIONS_HOURS = (24, 168, 720)
FILE_DEFAULT_TTL_HOURS = 24
FILE_GRACE_DAYS = 30
MONTH_DAYS = 30
YEAR_DAYS = 365
YEAR_MONTHS_PAID = 10

FREE = "free"
PAID_TIERS = ("pro", "pro_plus", "max")
ALL_TIERS = (FREE, *PAID_TIERS)

TIERS: dict[str, dict[str, Any]] = {
    "free": {"name": "免费", "rank": 0, "member_limit": 200, "message_limit": 1000, "file_quota_bytes": None, "file_temp_max_bytes": None, "featured_limit": 100},
    "pro": {"name": "Pro", "rank": 1, "member_limit": 500, "message_limit": 5000, "file_quota_bytes": 0, "file_temp_max_bytes": 10 * MB, "featured_limit": 300},
    "pro_plus": {
        "name": "Pro+", "rank": 2, "member_limit": 1000, "message_limit": 20000, "file_quota_bytes": 200 * MB, "file_temp_max_bytes": 30 * MB, "featured_limit": 800,
    },
    "max": {
        "name": "MAX", "rank": 3, "member_limit": 3000, "message_limit": 50000, "file_quota_bytes": 1024 * MB, "file_temp_max_bytes": 100 * MB, "featured_limit": 3000,
    },
}
# 月价默认值（金币），后台可改
DEFAULT_PRICES: dict[str, int] = {"pro": 80, "pro_plus": 200, "max": 500}
PRICE_MAX = 1_000_000
# 永久档位（只有后台能给）：到期时间写成这个远期的哨兵值，到期判断、修剪、群文件宽限这些现有逻辑不用改。
# 判断「是不是永久」一律用 is_permanent（到期年份 >= 2999），不要和这个值做相等比较
PERMANENT_EXPIRES_AT = datetime(2999, 12, 31, tzinfo=UTC)
PERMANENT_YEAR = 2999
# 每个群单独的人数上限（conversations.member_limit_override）：空 = 跟随档位，0 = 不限，正数 = 就是这个上限
MEMBER_LIMIT_UNLIMITED = 0
MEMBER_LIMIT_OVERRIDE_MAX = 100_000


def _now() -> datetime:
    """现在（UTC）。单独成函数是为了测试里能换掉：服务层的当前时间都经过它。"""
    return datetime.now(UTC)


def aware(value: datetime | None) -> datetime | None:
    """SQLite 读出来的时间不带时区，统一当 UTC。"""
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def is_permanent(expires_at: datetime | None) -> bool:
    """到期时间是不是永久的哨兵值（年份 >= 2999）。"""
    expires = aware(expires_at)
    return expires is not None and expires.year >= PERMANENT_YEAR


def conversation_permanent(conversation: Any, now: datetime | None = None) -> bool:
    """这个群现在的有效档位是不是永久的（免费档恒为 False）。"""
    return conversation_tier(conversation, now) != FREE and is_permanent(getattr(conversation, "plus_expires_at", None))


def member_limit_of(conversation: Any, now: datetime | None = None) -> int | None:
    """这个群现在的人数上限：先看后台单独设的 member_limit_override（0 = 不限，返回 None），没设就按有效档位。"""
    override = getattr(conversation, "member_limit_override", None)
    if override is not None:
        return None if override == MEMBER_LIMIT_UNLIMITED else int(override)
    return TIERS[conversation_tier(conversation, now)]["member_limit"]


def rank(tier: str) -> int:
    return TIERS.get(tier, TIERS[FREE])["rank"]


def effective_tier(tier: str | None, expires_at: datetime | None, now: datetime | None = None) -> str:
    """记录的档位已经过期（或者没有到期时间）就是 free。"""
    if not tier or tier == FREE or tier not in TIERS:
        return FREE
    expires = aware(expires_at)
    if expires is None or expires <= (now or _now()):
        return FREE
    return tier


def conversation_tier(conversation: Any, now: datetime | None = None) -> str:
    kind = getattr(conversation, "type", None)
    if getattr(kind, "value", kind) != "group":
        return FREE
    return effective_tier(getattr(conversation, "plus_tier", None), getattr(conversation, "plus_expires_at", None), now)


def limits_for(tier: str) -> dict[str, Any]:
    spec = TIERS.get(tier, TIERS[FREE])
    return {
        "member_limit": spec["member_limit"],
        "message_limit": spec["message_limit"],
        "file_enabled": spec["file_quota_bytes"] is not None,
        "file_quota_bytes": spec["file_quota_bytes"],
        "file_temp_max_bytes": spec["file_temp_max_bytes"],
        "featured_limit": spec["featured_limit"],
    }


def file_temp_ceiling() -> int:
    """各档位临时文件上限里最大的那个。/group-plus/plans 和后台设置里的 file_max_bytes 是旧字段，留着给旧客户端，取这个值。"""
    return max(spec["file_temp_max_bytes"] or 0 for spec in TIERS.values())


def file_state(conversation: Any, now: datetime | None = None) -> dict[str, Any]:
    """群文件空间现在的状态：locked 未解锁 / active 可用 / grace 宽限期（可读不可传）。"""
    now = now or _now()
    tier = conversation_tier(conversation, now)
    if TIERS[tier]["file_quota_bytes"] is not None:
        return {
            "state": "active", "tier": tier, "grace_until": None,
            "quota_bytes": TIERS[tier]["file_quota_bytes"], "temp_max_bytes": TIERS[tier]["file_temp_max_bytes"],
        }
    files_tier = getattr(conversation, "plus_files_tier", None)
    until = aware(getattr(conversation, "plus_files_until", None))
    if files_tier in TIERS and TIERS[files_tier]["file_quota_bytes"] is not None and until is not None and until > now:
        return {
            "state": "grace", "tier": files_tier, "grace_until": until,
            "quota_bytes": TIERS[files_tier]["file_quota_bytes"], "temp_max_bytes": TIERS[files_tier]["file_temp_max_bytes"],
        }
    return {"state": "locked", "tier": None, "grace_until": None, "quota_bytes": None, "temp_max_bytes": None}
