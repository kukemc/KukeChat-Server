"""风纪系统对所有登录用户公开的部分：规则配置、总览数字、公示栏。

这里的内容任何登录用户都能读，所以只出汇总数字和匿名化的案件，不出任何身份信息。
"""

from __future__ import annotations

import statistics
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.case import JuryMember, ReportCase
from app.models.report import UserReport
from app.models.user import User
from app.services import cases as case_service
from app.services import jury as jury_service
from app.services import jury_exam as exam_service
from app.services import jury_opinions as opinion_service
from app.services import jury_points as points_service
from app.services import jury_settings
from app.services import penalties as penalty_service

UTC = timezone.utc

WINDOW_DAYS = settings.jury_overview_window_days
# 总览里的比率至少要这么多样本才给
MIN_SAMPLE = settings.jury_overview_min_sample
OVERVIEW_CACHE_SECONDS = 60.0
# 后端已经实现的能力，客户端据此决定显示哪些板块。第二行是二期追加的。
CAPABILITIES = [
    "overview", "case_detail", "opinions", "votes", "public_cases",
    "exam", "practice", "votes_v2", "points", "tiers", "achievements", "leaderboard", "badge",
]  # fmt: skip
# 这些类别的案件即使认定违规也不公示内容（题库也用同一份）。
HIDDEN_CONTENT_CATEGORIES = case_service.HIDDEN_CONTENT_CATEGORIES
VOTE_OPTIONS: list[dict[str, Any]] = [
    {"value": "none", "kind": "clean", "label": "不违规", "hint": "没有违反社区规则"},
    {"value": "l1", "kind": "violation", "label": "违规 · 轻微", "hint": "不太合适，但影响很小，比如随口的脏话、轻度刷屏", "level": 1},
    {"value": "l2", "kind": "violation", "label": "违规 · 一般", "hint": "明确违规，比如针对他人的辱骂、广告、引战", "level": 2},
    {"value": "l3", "kind": "violation", "label": "违规 · 严重", "hint": "性质恶劣，比如人身威胁、诈骗、泄露他人隐私", "level": 3},
    {"value": "abstain", "kind": "abstain", "label": "拿不准", "hint": "看不懂或无法判断，不计入表决"},
    {"value": "recuse", "kind": "recuse", "label": "回避", "hint": "我可能认识当事人或有利害关系，改派给别人"},
    {"value": "need_context", "kind": "abstain", "label": "上下文不足", "hint": "看完全部上下文仍然无法判断，不计入表决"},
]

_overview_cache: dict[str, Any] = {"at": None, "data": None}


def reset_overview_cache() -> None:
    _overview_cache["at"] = None
    _overview_cache["data"] = None


# ---------------------------------------------------------------- 配置


async def config(db: AsyncSession) -> dict[str, Any]:
    """客户端要展示的规则数字都从这里读。"""
    switches = await jury_settings.get_switches(db)
    return {
        "version": 2,
        "capabilities": list(CAPABILITIES),
        "switches": {name: switches[name] for name in jury_settings.PUBLIC_SWITCHES},
        "tiers": [
            {
                "key": key,
                "label": label,
                # 晋升到这一层的条件和这一层的降级线；见习没有
                "requirements": dict(points_service.TIER_RULES[key]["requirements"]) if key in points_service.TIER_RULES else None,
                "demote_below": dict(points_service.TIER_RULES[key]["demote_below"]) if key in points_service.TIER_RULES else None,
            }
            for key, label in jury_service.TIERS
        ],
        "votes": [dict(option) for option in VOTE_OPTIONS],
        "rules": {
            "seats_per_case": jury_service.SEATS_PER_CASE,
            # 表决规则见 jury.tally：3 票一致即定案，否则坐满之后只允许 1 票不同
            "quick_consensus_votes": 3,
            "full_consensus_votes": jury_service.SEATS_PER_CASE - 1,
            "timeout_hours": case_service.JURY_TIMEOUT_HOURS,
            "assignment_ttl_hours": jury_service.ASSIGNMENT_TTL_HOURS,
            "daily_votes": jury_service.DAILY_VOTE_LIMIT,
            "term_days": jury_service.TERM_DAYS,
            "context_near": settings.jury_context_near,
            "content_retention_days": jury_service.CONTENT_RETENTION_DAYS,
            "publish_delay_hours": case_service.PUBLISH_DELAY_HOURS,
            "min_sample": MIN_SAMPLE,
            "max_ban_hours": penalty_service.CAPPED_BAN_HOURS,
            "severe_min_votes": jury_service.SEVERE_MIN_VOTES,
            "need_context_votes": jury_service.NEED_CONTEXT_VOTES,
        },
        "opinions": {
            "max_length": opinion_service.MAX_LENGTH,
            "per_case": opinion_service.PER_CASE,
            "per_minute": opinion_service.PER_MINUTE,
            "per_day": opinion_service.PER_DAY,
            "auto_hide_reports": opinion_service.AUTO_HIDE_REPORTS,
            "report_reasons": list(opinion_service.REPORT_REASONS),
            "like_reward_threshold": points_service.LIKE_REWARD_THRESHOLD,
        },
        "eligibility": {
            "min_account_age_days": jury_service.MIN_ACCOUNT_AGE_DAYS,
            "min_recent_messages": jury_service.MIN_RECENT_MESSAGES,
            "recent_activity_days": jury_service.RECENT_ACTIVITY_DAYS,
        },
        # —— 二期 ——
        "categories": [
            {"key": key, "label": penalty_service.category_label(key), "default_level": penalty_service.default_level(key)}
            for key in jury_service.VOTE_CATEGORIES
        ],
        "points": dict(points_service.POINTS),
        "suspend": {
            "min_settled": jury_service.MIN_SETTLED_FOR_REVIEW,
            "agreement_rate": jury_service.MIN_ACCURACY,
            "quality_min": jury_service.MIN_GOLDEN_FOR_REVIEW,
            "quality_rate": jury_service.MIN_ACCURACY,
        },
        "exam": {
            "total": exam_service.EXAM_TOTAL,
            "pass_score": exam_service.EXAM_PASS_SCORE,
            "time_limit_minutes": exam_service.EXAM_TIME_LIMIT_MINUTES,
            "grace_seconds": exam_service.EXAM_GRACE_SECONDS,
            "attempts_max": exam_service.EXAM_ATTEMPTS_MAX,
            "window_days": exam_service.EXAM_WINDOW_DAYS,
            "retry_hours": exam_service.EXAM_RETRY_HOURS,
            "valid_days": exam_service.EXAM_VALID_DAYS,
        },
        "practice": {"daily_limit": exam_service.PRACTICE_DAILY_LIMIT},
        "recuse": {"daily_limit": jury_service.RECUSE_DAILY_LIMIT},
        "abstain": {
            "window": jury_service.ABSTAIN_WINDOW,
            "min_votes": jury_service.ABSTAIN_MIN_VOTES,
            "warn_rate": jury_service.ABSTAIN_WARN_RATE,
            "cooldown_rate": jury_service.ABSTAIN_COOLDOWN_RATE,
            "cooldown_hours": jury_service.ABSTAIN_COOLDOWN_HOURS,
        },
    }


# ---------------------------------------------------------------- 总览


async def _count(db: AsyncSession, *clauses: Any) -> int:
    return int((await db.execute(select(func.count(ReportCase.id)).where(*clauses))).scalar_one() or 0)


async def _community(db: AsyncSession) -> dict[str, Any]:
    """全站的汇总数字。只统计上线之后真正进过委员队列的案件（queued_at 非空）。"""
    now = datetime.now(UTC)
    window_start = now - timedelta(days=WINDOW_DAYS)
    jurors = {"total": 0, **{key: 0 for key, _label in jury_service.TIERS}}
    tier_rows = await db.execute(
        select(JuryMember.tier, func.count())
        .where(JuryMember.status == "active", or_(JuryMember.term_ends_at.is_(None), JuryMember.term_ends_at > now))
        .group_by(JuryMember.tier)
    )
    for tier, count in tier_rows.all():
        jurors["total"] += int(count)
        if tier in jurors:
            jurors[tier] += int(count)

    queued = ReportCase.queued_at.is_not(None)
    # 经过委员评审并已定案
    settled = (queued, ReportCase.publishable.is_(True), ReportCase.status != "open")
    # 离开委员队列的时间：转出的看 escalated_at，委员自己定案的看 closed_at
    left_at = func.coalesce(ReportCase.escalated_at, ReportCase.closed_at)
    outcome_rows = await db.execute(
        select(ReportCase.jury_outcome, func.count())
        .where(queued, ReportCase.jury_outcome.is_not(None), left_at >= window_start)
        .group_by(ReportCase.jury_outcome)
    )
    outcomes = {outcome: int(count) for outcome, count in outcome_rows.all()}
    sample = sum(outcomes.values())
    consensus = outcomes.get("consensus", 0)

    window_rows = (
        await db.execute(
            select(ReportCase.status, ReportCase.overturned_at, ReportCase.created_at, ReportCase.closed_at).where(
                *settled, ReportCase.closed_at >= window_start
            )
        )
    ).all()
    violation = sum(1 for case_status, _overturned, _created, _closed in window_rows if case_status == "resolved")
    overturned = sum(1 for _status, overturned_at, _created, _closed in window_rows if overturned_at is not None)
    hours = [
        max(0.0, (penalty_service.aware(closed_at) - penalty_service.aware(created_at)).total_seconds()) / 3600
        for _status, _overturned, created_at, closed_at in window_rows
        if created_at is not None and closed_at is not None
    ]
    enough = sample >= MIN_SAMPLE
    decided = len(window_rows)

    # 近 30 天每天进队列 / 定案的件数，按北京时间的自然日，没有数据的日子给 0。
    today = now.astimezone(jury_service.LOCAL_TZ).date()
    first_day = today - timedelta(days=jury_service.TREND_DAYS - 1)
    daily = {first_day + timedelta(days=index): {"queued": 0, "settled": 0} for index in range(jury_service.TREND_DAYS)}
    since = datetime(first_day.year, first_day.month, first_day.day, tzinfo=jury_service.LOCAL_TZ).astimezone(UTC)
    queued_rows = await db.execute(select(ReportCase.queued_at).where(ReportCase.queued_at >= since))
    settled_rows = await db.execute(select(ReportCase.closed_at).where(*settled, ReportCase.closed_at >= since))
    for key, rows in (("queued", queued_rows), ("settled", settled_rows)):
        for moment in rows.scalars().all():
            day = daily.get(penalty_service.aware(moment).astimezone(jury_service.LOCAL_TZ).date())
            if day is not None:
                day[key] += 1
    return {
        "jurors": jurors,
        # panel（资深合议）三期才有
        "backlog": {"jury": await _count(db, ReportCase.status == "open", ReportCase.queue == "jury"), "panel": 0},
        "settled": {
            "d7": await _count(db, *settled, ReportCase.closed_at >= now - timedelta(days=7)),
            "d30": await _count(db, *settled, ReportCase.closed_at >= now - timedelta(days=30)),
            "total": await _count(db, *settled),
        },
        "window_days": WINDOW_DAYS,
        "sample": sample,
        "sample_enough": enough,
        "median_hours": round(statistics.median(hours), 1) if hours else None,
        "consensus_rate": round(consensus / sample, 3) if enough else None,
        "violation_rate": round(violation / decided, 3) if enough and decided else None,
        "overturn_rate": round(overturned / decided, 3) if enough and decided else None,
        "counts": {
            "consensus": consensus,
            "escalated": sample - consensus,
            "violation": violation,
            "clean": decided - violation,
            "overturned": overturned,
        },
        "daily": [{"date": day.isoformat(), **values} for day, values in daily.items()],
        "generated_at": now,
    }


async def overview(db: AsyncSession, user: User) -> dict[str, Any]:
    """全站数字在进程内缓存 60 秒；me 是每个人自己的，不缓存。"""
    tick = time.monotonic()
    if _overview_cache["data"] is None or _overview_cache["at"] is None or tick - _overview_cache["at"] >= OVERVIEW_CACHE_SECONDS:
        _overview_cache["data"] = await _community(db)
        _overview_cache["at"] = tick
    now = datetime.now(UTC)
    member = await jury_service.get_member(db, user.id)
    serving = jury_service._is_serving(member, now)
    voted_today = await jury_service._votes_since(db, user.id, now - timedelta(hours=24)) if member is not None else 0
    return {
        **_overview_cache["data"],
        "me": {
            "status": jury_service.member_state(member, now),
            "tier": member.tier if member is not None and serving else None,
            "available_cases": await jury_service.available_cases(db, user, voted_today=voted_today) if serving else None,
            "voted_today": voted_today,
            "daily_limit": jury_service.DAILY_VOTE_LIMIT,
        },
    }


# ---------------------------------------------------------------- 公示栏


def _content_hidden_reason(case: ReportCase, group_content: bool) -> str | None:
    """这个案件为什么不公示内容；None 表示可以带内容。"""
    if case.overturned_at is not None:
        return "overturned"
    if case.status != "resolved":
        # 认定不违规的只公示结论和票数，否则等于把「此人被举报过」连原话挂出来。
        return "clean"
    if case.verdict_category in HIDDEN_CONTENT_CATEGORIES or case.jury_block_reason == "sensitive":
        return "sensitive"
    if case.content_publishable or (group_content and case.target_type == "message" and case.jury_eligible):
        return None
    return "group_message"


async def _public_items(db: AsyncSession, cases: list[ReportCase], group_content: bool) -> list[dict[str, Any]]:
    case_ids = [case.id for case in cases]
    tallies = await jury_service.vote_tallies(db, case_ids)
    reasons: dict[int, list[dict[str, Any]]] = {case_id: [] for case_id in case_ids}
    if case_ids:
        rows = await db.execute(
            select(UserReport.case_id, UserReport.reason, func.count()).where(UserReport.case_id.in_(case_ids)).group_by(UserReport.case_id, UserReport.reason)
        )
        for case_id, reason, count in rows.all():
            reasons[case_id].append({"reason": reason, "count": int(count)})
    items: list[dict[str, Any]] = []
    for case in cases:
        verdict = jury_service.verdict_view(case)
        items.append(
            {
                "case_id": case.id,
                "kind": case.target_type,
                "reasons": reasons[case.id],
                "report_count": case.report_count,
                "outcome": verdict["outcome"],
                "category": verdict["category"],
                "category_label": verdict["category_label"],
                "level": verdict["level"],
                "source": verdict["source"],
                "tally": tallies[case.id],
                "votes": jury_service.effective_votes(tallies[case.id]),
                "closed_at": verdict["closed_at"],
                "hours": verdict["hours"],
                "overturned": verdict["overturned"],
                "has_content": _content_hidden_reason(case, group_content) is None,
            }
        )
    return items


async def public_cases(
    db: AsyncSession,
    *,
    limit: int = 20,
    offset: int = 0,
    kind: str | None = None,
    outcome: str | None = None,
    category: str | None = None,
) -> tuple[list[dict[str, Any]], int]:
    clauses = case_service.published_clauses(datetime.now(UTC))
    if kind:
        clauses.append(ReportCase.target_type == kind)
    if outcome:
        clauses.append(ReportCase.status == ("resolved" if outcome == "violation" else "dismissed"))
    if category:
        clauses.append(ReportCase.status == "resolved")
        clauses.append(ReportCase.verdict_category == category)
    total = await _count(db, *clauses)
    rows = await db.execute(
        select(ReportCase).where(*clauses).order_by(ReportCase.closed_at.desc(), ReportCase.id.desc()).offset(offset).limit(limit)
    )
    group_content = await jury_settings.is_enabled(db, "public_group_content")
    return await _public_items(db, list(rows.scalars()), group_content), total


def _public_content(case: ReportCase, snapshot: dict[str, Any]) -> dict[str, Any]:
    """只给被举报的那一条的文字，图片只给数量。"""
    if case.target_type == "post":
        post = snapshot.get("post") if isinstance(snapshot.get("post"), dict) else {}
        images = post.get("image_urls") if isinstance(post.get("image_urls"), list) else []
        return {"text": post.get("content") or None, "image_count": len(images), "created_at": jury_service.snapshot_time(post.get("created_at"))}
    message = snapshot.get("message") if isinstance(snapshot.get("message"), dict) else {}
    metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
    images = metadata.get("images") if isinstance(metadata.get("images"), list) else []
    is_text = (message.get("type") or "text") == "text"
    return {
        # 图片消息的 content 是图片地址，不能当文字给出去
        "text": (message.get("content") or None) if is_text else None,
        "image_count": len(images) + (1 if message.get("type") == "image" else 0),
        "created_at": jury_service.snapshot_time(message.get("created_at")),
    }


async def published_case(db: AsyncSession, case_id: int) -> ReportCase:
    """按公开编号取一个此刻在公示栏里的案件。"""
    result = await db.execute(select(ReportCase).where(ReportCase.id == case_id, *case_service.published_clauses(datetime.now(UTC))))
    case = result.scalar_one_or_none()
    if case is None:
        # 没有公示的案件对外一律当不存在，不区分「没有这个案件」和「还没公示」。
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"code": "case_not_found", "message": "案件不存在或尚未公示"})
    return case


async def public_case(db: AsyncSession, case_id: int) -> dict[str, Any]:
    case = await published_case(db, case_id)
    group_content = await jury_settings.is_enabled(db, "public_group_content")
    item = (await _public_items(db, [case], group_content))[0]
    hidden_reason = _content_hidden_reason(case, group_content)
    return {
        **item,
        "content": _public_content(case, await case_service.case_snapshot(db, case)) if hidden_reason is None else None,
        "content_hidden_reason": hidden_reason,
        "overturn_note": case.overturn_note,
    }
