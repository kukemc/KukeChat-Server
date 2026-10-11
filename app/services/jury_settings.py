"""风纪系统的运行时开关。

开关存在 system_settings 表里，后台可以随时改；阈值是部署配置（app/core/config.py），
这里只负责把它们整理成给客户端 / 后台看的样子。
"""

from __future__ import annotations

import json
import time
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.report import AdminAuditLog
from app.models.system import SystemSetting

# 前三个会改变「谁能处罚用户」，一期只存取、不产生行为；后两个一期就生效。
SWITCHES: tuple[str, ...] = ("auto_appoint", "appeals", "machine_review", "public_group_content", "opinions_open_to_jurors")
PUBLIC_SWITCHES: tuple[str, ...] = ("auto_appoint", "appeals", "machine_review")
_KEY_PREFIX = "jury.switch."
# 服务跑两个 worker 进程，另一个进程改了开关后最多这么久才能看到。
CACHE_SECONDS = 5.0

_cache: dict[str, Any] = {"at": 0.0, "values": None}


def reset_cache() -> None:
    _cache["at"] = 0.0
    _cache["values"] = None


async def get_switches(db: AsyncSession, *, fresh: bool = False) -> dict[str, bool]:
    """全部开关的当前值，没设置过的一律是关。"""
    now = time.monotonic()
    cached = _cache["values"]
    if not fresh and cached is not None and now - _cache["at"] < CACHE_SECONDS:
        return dict(cached)
    rows = await db.execute(select(SystemSetting.key, SystemSetting.value).where(SystemSetting.key.like(f"{_KEY_PREFIX}%")))
    stored = {key[len(_KEY_PREFIX) :]: value for key, value in rows.all()}
    values = {name: stored.get(name) == "1" for name in SWITCHES}
    _cache["at"] = now
    _cache["values"] = values
    return dict(values)


async def is_enabled(db: AsyncSession, name: str) -> bool:
    return (await get_switches(db)).get(name, False)


async def update_switches(db: AsyncSession, admin_id: int, changes: dict[str, bool]) -> dict[str, bool]:
    """改开关并写审计日志。只处理 changes 里出现的项，返回改完之后的全部开关。"""
    before = await get_switches(db, fresh=True)
    changed = {name: bool(value) for name, value in changes.items() if name in SWITCHES and bool(value) != before[name]}
    for name, value in changed.items():
        item = await db.get(SystemSetting, f"{_KEY_PREFIX}{name}")
        if item is None:
            db.add(SystemSetting(key=f"{_KEY_PREFIX}{name}", value="1" if value else "0", updated_by_id=admin_id))
        else:
            item.value = "1" if value else "0"
            item.updated_by_id = admin_id
    if changed:
        db.add(
            AdminAuditLog(
                actor_id=admin_id,
                action="jury.settings.update",
                target_type="system_setting",
                target_id=None,
                details=json.dumps({name: {"old": before[name], "new": value} for name, value in changed.items()}, ensure_ascii=False, sort_keys=True),
            )
        )
        await db.commit()
    reset_cache()
    return await get_switches(db, fresh=True)


def thresholds() -> dict[str, int | float]:
    """后台只读展示用：所有阈值的当前值。"""
    return {
        "min_account_age_days": settings.jury_min_account_age_days,
        "min_recent_messages": settings.jury_min_recent_messages,
        "recent_activity_days": settings.jury_recent_activity_days,
        "term_days": settings.jury_term_days,
        "daily_votes": settings.jury_daily_vote_limit,
        "seats_per_case": settings.jury_seats_per_case,
        "assignment_ttl_hours": settings.jury_assignment_ttl_hours,
        "golden_every": settings.jury_golden_every,
        "min_settled_for_review": settings.jury_min_settled_for_review,
        "min_golden_for_review": settings.jury_min_golden_for_review,
        "min_accuracy": settings.jury_min_accuracy,
        "timeout_hours": settings.jury_timeout_hours,
        "sweep_interval_seconds": settings.jury_sweep_interval_seconds,
        "large_group_members": settings.jury_large_group_members,
        "context_near": settings.jury_context_near,
        "content_retention_days": settings.jury_content_retention_days,
        "publish_delay_hours": settings.jury_publish_delay_hours,
        "overview_window_days": settings.jury_overview_window_days,
        "min_sample": settings.jury_overview_min_sample,
        "opinion_max_length": settings.jury_opinion_max_length,
        "opinions_per_case": settings.jury_opinions_per_case,
        "opinions_per_minute": settings.jury_opinions_per_minute,
        "opinions_per_day": settings.jury_opinions_per_day,
        "opinion_auto_hide_reports": settings.jury_opinion_auto_hide_reports,
    }
