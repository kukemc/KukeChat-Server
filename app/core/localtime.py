"""「今天」按 UTC+8 算的那几处共用的日期函数（生日、送礼的每日上限、心意榜的本月榜）。

只做日期运算，不查库、不依赖别的模块：用户模型（models/user.py）序列化时要用它判断「今天是不是生日」。
"""

from __future__ import annotations

from calendar import isleap
from datetime import date, datetime, timedelta, timezone

UTC = timezone.utc
LOCAL_TZ = timezone(timedelta(hours=8))


def _now() -> datetime:
    """现在（UTC）。单独成函数是为了测试里能换掉。"""
    return datetime.now(UTC)


def local_today(now: datetime | None = None) -> date:
    """UTC+8 的今天。"""
    return (now or _now()).astimezone(LOCAL_TZ).date()


def day_start(now: datetime | None = None) -> datetime:
    """UTC+8 的今天零点，换成 UTC（库里的时间都按 UTC 存）。"""
    today = local_today(now)
    return datetime(today.year, today.month, today.day, tzinfo=LOCAL_TZ).astimezone(UTC)


def month_start(now: datetime | None = None) -> datetime:
    """UTC+8 的这个自然月 1 日零点，换成 UTC。"""
    today = local_today(now)
    return datetime(today.year, today.month, 1, tzinfo=LOCAL_TZ).astimezone(UTC)


def is_birthday_on(month: int | None, day: int | None, today: date) -> bool:
    """month 月 day 日出生的人，today 这一天是不是过生日。2 月 29 日出生的人在平年按 2 月 28 日过。"""
    if not month or not day:
        return False
    if (today.month, today.day) == (month, day):
        return True
    return (month, day) == (2, 29) and (today.month, today.day) == (2, 28) and not isleap(today.year)
