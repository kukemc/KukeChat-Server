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

# 前三个会改变「谁能处罚用户」：auto_appoint 二期起生效（通过考试直接任命），另两个三期才产生行为。
# exam_required：申请是否必须先通过入门考试。默认关 —— 已发布的旧客户端没有考试入口。
# machine_judge：机器二审（docs/moderation-v2.md 1.10）。不归风纪管，但它决定一审 block 的内容罚不罚、拿不准的进不进委员队列，
# 放在这里和 machine_review 一起开关。
# machine_idle_scan：空闲复查（docs/moderation-v2.md 1.11），二审闲着时复查一审放行的可疑群聊文字，可疑的建成机器案件。
# 只建案、不处罚，而且 machine_review 关着时什么也不做，所以默认开。
SWITCHES: tuple[str, ...] = (
    "auto_appoint", "appeals", "machine_review", "public_group_content", "opinions_open_to_jurors", "exam_required", "machine_judge",
    "machine_idle_scan",
)
PUBLIC_SWITCHES: tuple[str, ...] = ("auto_appoint", "appeals", "machine_review")
# 没设置过时默认开的开关；其余没设置过的一律是关
DEFAULT_ON: frozenset[str] = frozenset({"machine_judge", "machine_idle_scan"})
_KEY_PREFIX = "jury.switch."
# 服务跑两个 worker 进程，另一个进程改了开关后最多这么久才能看到。
CACHE_SECONDS = 5.0

_cache: dict[str, Any] = {"at": 0.0, "values": None}


def reset_cache() -> None:
    _cache["at"] = 0.0
    _cache["values"] = None


async def get_switches(db: AsyncSession, *, fresh: bool = False) -> dict[str, bool]:
    """全部开关的当前值，没设置过的除 DEFAULT_ON 外一律是关。"""
    now = time.monotonic()
    cached = _cache["values"]
    if not fresh and cached is not None and now - _cache["at"] < CACHE_SECONDS:
        return dict(cached)
    rows = await db.execute(select(SystemSetting.key, SystemSetting.value).where(SystemSetting.key.like(f"{_KEY_PREFIX}%")))
    stored = {key[len(_KEY_PREFIX) :]: value for key, value in rows.all()}
    values = {name: stored.get(name, "1" if name in DEFAULT_ON else "0") == "1" for name in SWITCHES}
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
        # 二期
        "severe_min_votes": settings.jury_severe_min_votes,
        "need_context_votes": settings.jury_need_context_votes,
        "quality_min_gap": settings.jury_quality_min_gap,
        "quality_max_gap": settings.jury_quality_max_gap,
        "recuse_daily_limit": settings.jury_recuse_daily_limit,
        "abstain_window": settings.jury_abstain_window,
        "abstain_min_votes": settings.jury_abstain_min_votes,
        "abstain_warn_rate": settings.jury_abstain_warn_rate,
        "abstain_cooldown_rate": settings.jury_abstain_cooldown_rate,
        "abstain_cooldown_hours": settings.jury_abstain_cooldown_hours,
        "warning_margin": settings.jury_warning_margin,
        "warning_sample_ratio": settings.jury_warning_sample_ratio,
        "points_vote_agreed": settings.jury_points_vote_agreed,
        "points_vote_disagreed": settings.jury_points_vote_disagreed,
        "points_quality_correct": settings.jury_points_quality_correct,
        "points_quality_wrong": settings.jury_points_quality_wrong,
        "points_daily_first": settings.jury_points_daily_first,
        "points_streak_7": settings.jury_points_streak_7,
        "points_opinion_liked": settings.jury_points_opinion_liked,
        "points_exam_passed": settings.jury_points_exam_passed,
        "points_overturn_adjust": settings.jury_points_overturn_adjust,
        "opinion_like_reward_threshold": settings.jury_opinion_like_reward_threshold,
        "regular_votes_settled": settings.jury_regular_votes_settled,
        "regular_agreement_rate": settings.jury_regular_agreement_rate,
        "regular_quality_rate": settings.jury_regular_quality_rate,
        "regular_quality_min": settings.jury_regular_quality_min,
        "regular_days_served": settings.jury_regular_days_served,
        "regular_demote_rate": settings.jury_regular_demote_rate,
        "senior_votes_settled": settings.jury_senior_votes_settled,
        "senior_agreement_rate": settings.jury_senior_agreement_rate,
        "senior_quality_rate": settings.jury_senior_quality_rate,
        "senior_quality_min": settings.jury_senior_quality_min,
        "senior_days_served": settings.jury_senior_days_served,
        "senior_demote_rate": settings.jury_senior_demote_rate,
        "demote_window": settings.jury_demote_window,
        "demote_min_votes": settings.jury_demote_min_votes,
        "exam_total": settings.jury_exam_total,
        "exam_pass_score": settings.jury_exam_pass_score,
        "exam_time_limit_minutes": settings.jury_exam_time_limit_minutes,
        "exam_grace_seconds": settings.jury_exam_grace_seconds,
        "exam_attempts_max": settings.jury_exam_attempts_max,
        "exam_window_days": settings.jury_exam_window_days,
        "exam_retry_hours": settings.jury_exam_retry_hours,
        "exam_valid_days": settings.jury_exam_valid_days,
        "practice_daily_limit": settings.jury_practice_daily_limit,
        "practice_every": settings.jury_practice_every,
        "golden_consensus_votes": settings.jury_golden_consensus_votes,
        "golden_delay_hours": settings.jury_golden_delay_hours,
        # 三期：资深合议、申诉复审、机器案源、复议
        "panel_timeout_hours": settings.jury_panel_timeout_hours,
        "appeal_timeout_hours": settings.jury_appeal_timeout_hours,
        "points_panel_agreed": settings.jury_points_panel_agreed,
        "points_panel_disagreed": settings.jury_points_panel_disagreed,
        "points_review_vote": settings.jury_points_review_vote,
        "machine_daily_per_juror": settings.jury_machine_daily_per_juror,
        "machine_daily_min": settings.jury_machine_daily_min,
        "machine_idle_share": settings.jury_machine_idle_share,
        # 空闲复查（docs/moderation-v2.md 1.11）
        "idle_scan_threshold": settings.moderation_idle_scan_threshold,
        "idle_scan_min_risk": settings.moderation_idle_scan_min_risk,
        "idle_scan_batch": settings.moderation_idle_scan_batch,
        "idle_scan_lookback_hours": settings.moderation_idle_scan_lookback_hours,
        "idle_scan_hourly_cap": settings.moderation_idle_scan_hourly_cap,
        "appeal_low_p": settings.moderation_appeal_low_p,
        "cleared_block_days": settings.jury_cleared_block_days,
        "reconsider_window_days": settings.jury_reconsider_window_days,
        "reconsider_text_min": settings.jury_reconsider_text_min,
        # 三期：判例与议事厅
        "precedent_title_max": settings.jury_precedent_title_max,
        "precedent_summary_max": settings.jury_precedent_summary_max,
        "hall_title_max": settings.jury_hall_title_max,
        "hall_content_max": settings.jury_hall_content_max,
        "hall_comment_max": settings.jury_hall_comment_max,
        "hall_list_excerpt": settings.jury_hall_list_excerpt,
        "hall_posts_per_day": settings.jury_hall_posts_per_day,
        "hall_comments_per_day": settings.jury_hall_comments_per_day,
        "hall_reports_per_day": settings.jury_hall_reports_per_day,
        "hall_vote_days": settings.jury_hall_vote_days,
        "hall_quorum_ratio": settings.jury_hall_quorum_ratio,
        "hall_quorum_min": settings.jury_hall_quorum_min,
        "hall_pass_num": settings.jury_hall_pass_num,
        "hall_pass_den": settings.jury_hall_pass_den,
        "hall_auto_hide_reports": settings.jury_hall_auto_hide_reports,
        # 质控题：回避后多久可以再出、展示时内容时间平移的范围
        "quality_recuse_retry_days": settings.jury_quality_recuse_retry_days,
        "quality_shift_min_hours": settings.jury_quality_shift_min_hours,
        "quality_shift_max_hours": settings.jury_quality_shift_max_hours,
    }
