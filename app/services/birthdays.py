"""生日（docs/chat-features.md 第 8 节）。

生日存在用户自己那一行上（年、月、日三列，外加是否公开年份）：公开资料（UserPublic.birthday / is_birthday_today）
序列化时只读这几列现算，不查别的表 —— 见 models/user.py 的那两个属性和 core/localtime.py。「今天」一律按 UTC+8。

改日期不受限制：随时可以设置、改、清空。以前的「365 天只能改一次」已经取消；返回里的 next_change_at 为兼容旧客户端保留，
恒为 null。users.birthday_set_at / birthday_changed_at 两列留在表里（已有迁移），但不再读写。

庆生按年记（birthday_celebrated_year）：改日期不重置它，所以同一年里反复改日期（哪怕改成今天）最多播一次。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import localtime
from app.models.user import User

MIN_YEAR = 1900


def _invalid() -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail={"code": "invalid_birthday", "message": "生日日期不合法"})


def view(user: User, now: datetime | None = None) -> dict[str, Any]:
    """GET /users/me/birthday 的形状（PUT 也返回它）。本人看自己，年份总是给。"""
    now = now or localtime._now()
    today = localtime.local_today(now)
    has = bool(user.birthday_month and user.birthday_day)
    return {
        "birthday": {"year": user.birthday_year, "month": user.birthday_month, "day": user.birthday_day} if has and user.birthday_year else None,
        "show_year": bool(user.birthday_show_year),
        # 已废弃：改日期不再受限，恒为 null（为兼容旧客户端保留这个字段）
        "next_change_at": None,
        # 今天是我的生日，而且今年（UTC+8）还没播过庆生动画
        "celebrate_today": has and localtime.is_birthday_on(user.birthday_month, user.birthday_day, today) and user.birthday_celebrated_year != today.year,
    }


def _clean_date(payload: dict[str, Any], today: date) -> tuple[int, int, int]:
    parts = [payload.get(name) for name in ("year", "month", "day")]
    if any(isinstance(part, bool) or not isinstance(part, int) for part in parts):
        raise _invalid()
    year, month, day = parts
    if year < MIN_YEAR:
        raise _invalid()
    try:
        # 2 月 29 日只有闰年才合法：年份是填了的，按那一年的日历算
        value = date(year, month, day)
    except ValueError:
        raise _invalid() from None
    if value > today:
        raise _invalid()
    return year, month, day


async def set_birthday(db: AsyncSession, user: User, payload: dict[str, Any]) -> dict[str, Any]:
    """PUT /users/me/birthday。日期非法 / 在未来 / 早于 1900 是 422 invalid_birthday；改日期不受次数限制。"""
    now = localtime._now()
    year, month, day = _clean_date(payload, localtime.local_today(now))
    show_year = payload.get("show_year")
    if show_year is not None and not isinstance(show_year, bool):
        raise _invalid()
    # 不动 birthday_celebrated_year：今年已经播过的话，改成今天也不会再播
    user.birthday_year, user.birthday_month, user.birthday_day = year, month, day
    if show_year is not None:
        user.birthday_show_year = show_year
    await db.commit()
    await db.refresh(user)
    return view(user, now)


async def clear_birthday(db: AsyncSession, user: User) -> dict[str, Any]:
    """DELETE /users/me/birthday：清空，随时可以。今年是否播过庆生不清（清空再填回今天也不会再播）。"""
    user.birthday_year = user.birthday_month = user.birthday_day = None
    await db.commit()
    await db.refresh(user)
    return view(user)


async def mark_celebrated(db: AsyncSession, user: User) -> dict[str, Any]:
    """POST /users/me/birthday/celebrated：标记今年的庆生动画已经播过。今天不是生日时什么都不改。"""
    now = localtime._now()
    today = localtime.local_today(now)
    if localtime.is_birthday_on(user.birthday_month, user.birthday_day, today) and user.birthday_celebrated_year != today.year:
        user.birthday_celebrated_year = today.year
        await db.commit()
        await db.refresh(user)
    return view(user, now)


def admin_birthday(user: User) -> dict[str, int | None] | None:
    """后台的用户详情：总是带完整生日（含年份），不管本人选没选公开。"""
    if not user.birthday_month or not user.birthday_day:
        return None
    return {"month": user.birthday_month, "day": user.birthday_day, "year": user.birthday_year}
