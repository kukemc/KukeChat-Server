"""风纪值、层级、成就、排行榜，以及委员身份的对外展示（docs/jury-v2.md 2.6–2.9）。

这是风纪系统里靠下的一层：不依赖 jury.py，评审、题库、考试都调它。
加减分、层级变化都不自己提交事务；要推给本人的实时事件先挂在会话上，调用方提交之后用 flush_events 发出去。
"""

from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.case import JuryAchievement, JuryExamAttempt, JuryMember, JuryNotification, JuryOpinion, JuryPointLog, JuryVote
from app.models.user import User
from app.schemas.user import UserPublic
from app.services import penalties as penalty_service

logger = logging.getLogger(__name__)
UTC = timezone.utc
# 「每天」「本周」「本月」都按北京时间的自然日算
LOCAL_TZ = timezone(timedelta(hours=8))

# 事件 kind -> 分值。来自部署配置，GET /jury/config 的 points 原样下发。
POINTS: dict[str, int] = {
    "vote_agreed": settings.jury_points_vote_agreed,
    "vote_disagreed": settings.jury_points_vote_disagreed,
    "quality_correct": settings.jury_points_quality_correct,
    "quality_wrong": settings.jury_points_quality_wrong,
    "daily_first": settings.jury_points_daily_first,
    "streak_7": settings.jury_points_streak_7,
    "opinion_liked": settings.jury_points_opinion_liked,
    "exam_passed": settings.jury_points_exam_passed,
    "overturn_adjust": settings.jury_points_overturn_adjust,
    # 三期：合议票与结论一致 / 不一致；每张申诉复审、复议票
    "panel_agreed": settings.jury_points_panel_agreed,
    "panel_disagreed": settings.jury_points_panel_disagreed,
    "review_vote": settings.jury_points_review_vote,
}
POINT_LABELS: dict[str, str] = {
    "vote_agreed": "与结论一致",
    "vote_disagreed": "参与评审",
    "quality_correct": "质控题答对",
    "quality_wrong": "质控题答错",
    "daily_first": "今日首票",
    "streak_7": "连续 7 天参与",
    "opinion_liked": "观点获赞",
    "exam_passed": "通过入门考试",
    "overturn_adjust": "改判补差",
    "admin_adjust": "管理员调整",
    "quality_fixed": "质控题更正",
    "panel_agreed": "合议与结论一致",
    "panel_disagreed": "参与合议",
    "review_vote": "参与复审",
}
STREAK_REWARD_DAYS = 7
LIKE_REWARD_THRESHOLD = settings.jury_opinion_like_reward_threshold

TIERS: tuple[tuple[str, str], ...] = (("trainee", "见习委员"), ("regular", "正式委员"), ("senior", "资深委员"))
TIER_ORDER: tuple[str, ...] = tuple(key for key, _label in TIERS)
TIER_LABELS: dict[str, str] = dict(TIERS)
# 晋升到这一层的条件，和这一层的降级线。quality_rate 在答过 quality_min 道质控题之后才看；
# 降级看最近 window 张已结票，不足 min_votes 张不判断 —— 所以管理员直接任命的层级不会因为数量不够被降。
TIER_RULES: dict[str, dict[str, dict[str, Any]]] = {
    "regular": {
        "requirements": {
            "votes_settled": settings.jury_regular_votes_settled,
            "agreement_rate": settings.jury_regular_agreement_rate,
            "quality_rate": settings.jury_regular_quality_rate,
            "quality_min": settings.jury_regular_quality_min,
            "days_served": settings.jury_regular_days_served,
        },
        "demote_below": {
            "agreement_rate": settings.jury_regular_demote_rate,
            "window": settings.jury_demote_window,
            "min_votes": settings.jury_demote_min_votes,
            "to": "trainee",
        },
    },
    "senior": {
        "requirements": {
            "votes_settled": settings.jury_senior_votes_settled,
            "agreement_rate": settings.jury_senior_agreement_rate,
            "quality_rate": settings.jury_senior_quality_rate,
            "quality_min": settings.jury_senior_quality_min,
            "days_served": settings.jury_senior_days_served,
        },
        "demote_below": {
            "agreement_rate": settings.jury_senior_demote_rate,
            "window": settings.jury_demote_window,
            "min_votes": settings.jury_demote_min_votes,
            "to": "regular",
        },
    },
}

SHARP_EYE_VOTES = 30
SHARP_EYE_RATE = 0.9
# 成就的定义。target 是进度条的终点；判定逻辑在 _achievement_progress。
ACHIEVEMENTS: tuple[dict[str, Any], ...] = (
    {"key": "first_vote", "label": "第一案", "description": "完成第一次评审", "target": 1},
    {"key": "votes_10", "label": "初窥门径", "description": "有 10 张票出了结论", "target": 10},
    {"key": "votes_50", "label": "驾轻就熟", "description": "有 50 张票出了结论", "target": 50},
    {"key": "votes_100", "label": "百案之约", "description": "有 100 张票出了结论", "target": 100},
    {
        "key": "sharp_eye",
        "label": "火眼金睛",
        "description": f"出结论的票不少于 {SHARP_EYE_VOTES} 张，且一致率不低于 {round(SHARP_EYE_RATE * 100)}%",
        "target": SHARP_EYE_VOTES,
    },
    {"key": "streak_7", "label": "七日之约", "description": "连续 7 天参与评审", "target": 7},
    {"key": "streak_30", "label": "风雨无阻", "description": "连续 30 天参与评审", "target": 30},
    {"key": "quality_10", "label": "明察秋毫", "description": "质控题连续答对 10 道", "target": 10},
    {"key": "liked_10", "label": "言之有物", "description": "观点累计获得 10 个赞", "target": 10},
    {"key": "exam_first_try", "label": "一次过关", "description": "入门考试一次通过", "target": 1},
    {"key": "tier_regular", "label": "正式委员", "description": "成为正式委员", "target": 1},
    {"key": "tier_senior", "label": "资深委员", "description": "成为资深委员", "target": 1},
)

LEADERBOARD_CACHE_SECONDS = 60.0
LEADERBOARD_PERIODS = ("week", "month", "all")
_leaderboard_cache: dict[str, tuple[float, datetime, list[dict[str, Any]]]] = {}


def reset_leaderboard_cache() -> None:
    _leaderboard_cache.clear()


def local_day(moment: datetime) -> date:
    return moment.astimezone(LOCAL_TZ).date()


def is_serving(member: JuryMember | None, now: datetime | None = None) -> bool:
    if member is None or member.status != "active":
        return False
    term_ends_at = penalty_service.aware(member.term_ends_at)
    return term_ends_at is None or term_ends_at > (now or datetime.now(UTC))


# ---------------------------------------------------------------- 事件与通知


def queue_event(db: AsyncSession, user_id: int, event: str, data: dict[str, Any]) -> None:
    db.info.setdefault("jury_events", []).append((user_id, event, data))


async def flush_events(db: AsyncSession) -> None:
    """事务提交后调用：把攒下的实时事件发给本人。发不出去只记日志。"""
    events: list[tuple[int, str, dict[str, Any]]] = db.info.pop("jury_events", [])
    if not events:
        return
    from app.websocket.manager import manager

    sent_status: set[int] = set()
    for user_id, event, data in events:
        if event == "jury.status.changed":
            # 一次操作里可能同时晋升、得成就，状态事件每人发一次就够了。
            if user_id in sent_status:
                continue
            sent_status.add(user_id)
        try:
            await manager.send_to_user(user_id, event, data)
        except Exception:
            logger.exception("Failed to send jury event", extra={"user_id": user_id, "event": event})


def notify(db: AsyncSession, user_id: int, kind: str, title: str, body: str | None) -> None:
    """写一条通知中心的通知（type="jury"），并让本人重拉 /jury/me。调用方负责 commit。"""
    try:
        db.add(JuryNotification(user_id=user_id, kind=kind, title=title[:120], body=body, created_at=datetime.now(UTC)))
        queue_event(db, user_id, "jury.status.changed", {})
    except Exception:
        logger.exception("Failed to write jury notification", extra={"user_id": user_id, "kind": kind})


async def sync_badge(db: AsyncSession, member: JuryMember, user: User | None = None, now: datetime | None = None) -> None:
    """按成员当前的状态、层级和本人的选择重写 users.jury_badge。层级 / 状态 / 公开设置每次变化后都要调。"""
    user = user or await db.get(User, member.user_id)
    if user is None:
        return
    badge = member.tier if is_serving(member, now) and member.badge_visible else None
    if user.jury_badge != badge:
        user.jury_badge = badge


# ---------------------------------------------------------------- 评审编号


async def review_ids(db: AsyncSession, user_id: int, case_ids: list[int] | set[int]) -> dict[int, int]:
    """这个人在这些案件上的评审编号：案件 id -> jury_votes.id。没分到过的案件不在结果里。

    委员侧对外的 case_id 一律是评审编号，不是案件的真实编号（docs/jury-v2.md 1.0）；库里存的仍是真实编号，出去之前在这里换。
    """
    wanted = {case_id for case_id in case_ids if case_id is not None}
    if not wanted:
        return {}
    rows = await db.execute(select(JuryVote.case_id, JuryVote.id).where(JuryVote.juror_id == user_id, JuryVote.case_id.in_(wanted)))
    return {case_id: review_id for case_id, review_id in rows.all()}


async def review_id(db: AsyncSession, user_id: int, case_id: int | None) -> int | None:
    if case_id is None:
        return None
    return (await review_ids(db, user_id, [case_id])).get(case_id)


# ---------------------------------------------------------------- 风纪值


async def award(
    db: AsyncSession,
    member: JuryMember,
    kind: str,
    *,
    points: int | None = None,
    case_id: int | None = None,
    ref_id: int | None = None,
    unique: bool = True,
    announce: bool = True,
    note: str | None = None,
) -> JuryPointLog | None:
    """所有加减分都走这里。调用方负责 commit。

    unique：同一 (用户, kind, 案件, ref) 只给一次，已经给过返回 None。总分不低于 0，流水里记的是实际变动的分值。
    announce：是否排一个 jury.points.changed 事件；投票响应里当场返回的那几项不用再发。
    """
    if unique:
        existing = await db.execute(
            select(JuryPointLog.id)
            .where(
                JuryPointLog.user_id == member.user_id,
                JuryPointLog.kind == kind,
                JuryPointLog.case_id.is_(None) if case_id is None else JuryPointLog.case_id == case_id,
                JuryPointLog.ref_id.is_(None) if ref_id is None else JuryPointLog.ref_id == ref_id,
            )
            .limit(1)
        )
        if existing.scalar_one_or_none() is not None:
            return None
    nominal = POINTS.get(kind, 0) if points is None else points
    delta = max(nominal, -(member.points or 0))
    member.points = (member.points or 0) + delta
    log = JuryPointLog(
        user_id=member.user_id, kind=kind, points=delta, case_id=case_id, ref_id=ref_id, note=note, created_at=datetime.now(UTC)
    )
    db.add(log)
    await db.flush()
    if announce:
        queue_event(
            db,
            member.user_id,
            "jury.points.changed",
            # label 和 /jury/me/points 明细里的同源，客户端直接显示；case_id 是本人的评审编号
            {
                "delta": delta,
                "total": member.points,
                "kind": kind,
                "label": POINT_LABELS.get(kind, kind),
                "case_id": await review_id(db, member.user_id, case_id),
            },
        )
    return log


def reward_view(member: JuryMember, logs: list[JuryPointLog | None]) -> dict[str, Any]:
    """投票响应里的 reward：只含当场结算的部分。"""
    items = [{"kind": log.kind, "points": log.points, "label": POINT_LABELS.get(log.kind, log.kind)} for log in logs if log is not None]
    return {"points": sum(item["points"] for item in items), "total": member.points or 0, "items": items}


async def case_points(db: AsyncSession, user_id: int, case_id: int) -> int:
    """这个案件累计给了这个人多少分。"""
    result = await db.execute(
        select(func.coalesce(func.sum(JuryPointLog.points), 0)).where(JuryPointLog.user_id == user_id, JuryPointLog.case_id == case_id)
    )
    return int(result.scalar_one() or 0)


async def list_points(db: AsyncSession, user_id: int, *, limit: int = 20, offset: int = 0) -> dict[str, Any]:
    """风纪值明细，按时间倒序。没当过委员的人总分是 0、明细为空。"""
    total = (await db.execute(select(JuryMember.points).where(JuryMember.user_id == user_id))).scalar_one_or_none()
    count = int((await db.execute(select(func.count(JuryPointLog.id)).where(JuryPointLog.user_id == user_id))).scalar_one() or 0)
    rows = await db.execute(
        select(JuryPointLog)
        .where(JuryPointLog.user_id == user_id)
        .order_by(JuryPointLog.created_at.desc(), JuryPointLog.id.desc())
        .offset(offset)
        .limit(limit)
    )
    logs = rows.scalars().all()
    reviews = await review_ids(db, user_id, [log.case_id for log in logs])
    return {
        "total": int(total or 0),
        "items": [
            {
                "kind": log.kind,
                "label": POINT_LABELS.get(log.kind, log.kind),
                "points": log.points,
                # 评审编号；和案件无关的（每日首票、管理员调整……）为 null
                "case_id": reviews.get(log.case_id) if log.case_id is not None else None,
                "created_at": penalty_service.aware(log.created_at),
            }
            for log in logs
        ],
        "count": count,
    }


def touch_streak(member: JuryMember, day: date) -> bool:
    """记一次当天的参与，返回今天是不是刚好连满 7 天的整数倍。同一天重复调用没有效果。"""
    if member.streak_last_day == day:
        return False
    member.streak_days = (member.streak_days or 0) + 1 if member.streak_last_day == day - timedelta(days=1) else 1
    member.streak_last_day = day
    return member.streak_days % STREAK_REWARD_DAYS == 0


def current_streak(member: JuryMember | None, today: date) -> int:
    """现在的连续参与天数：昨天和今天都没参与就断了。"""
    if member is None or member.streak_last_day is None or member.streak_last_day < today - timedelta(days=1):
        return 0
    return member.streak_days or 0


async def reward_opinion_like(db: AsyncSession, opinion: JuryOpinion) -> None:
    """一条观点的赞数第一次达到阈值时给作者加一次分；之后取消赞不扣。调用方负责 commit。"""
    if (opinion.like_count or 0) < LIKE_REWARD_THRESHOLD:
        return
    member = (await db.execute(select(JuryMember).where(JuryMember.user_id == opinion.author_id))).scalar_one_or_none()
    if member is not None:
        await award(db, member, "opinion_liked", case_id=opinion.case_id, ref_id=opinion.id)


# ---------------------------------------------------------------- 层级


def days_served(member: JuryMember, now: datetime | None = None) -> int:
    """本次任职到现在多少天。不是在任状态的为 0；没有记录开始时间的旧数据退回用审批时间。"""
    if member.status != "active":
        return 0
    since = penalty_service.aware(member.serving_since or member.reviewed_at or member.applied_at)
    if since is None:
        return 0
    return max(0, ((now or datetime.now(UTC)) - since).days)


def _requirement_items(member: JuryMember, requirements: dict[str, Any], now: datetime | None = None) -> list[dict[str, Any]]:
    settled, agreed = member.votes_settled or 0, member.votes_agreed or 0
    agreement = round(agreed / settled, 3) if settled else None
    quality_min = int(requirements["quality_min"])
    quality_counted = (member.golden_total or 0) >= quality_min and (member.golden_total or 0) > 0
    quality = round(member.golden_correct / member.golden_total, 3) if quality_counted else None
    served = days_served(member, now)
    quality_item: dict[str, Any] = {
        "key": "quality_rate",
        "label": "质控正确率",
        "current": quality,
        "target": requirements["quality_rate"],
        "ok": quality is None or quality >= requirements["quality_rate"],
    }
    if quality is None:
        quality_item["note"] = f"答满 {quality_min} 道后开始计算"
    return [
        {
            "key": "votes_settled",
            "label": "已出结论的票",
            "current": settled,
            "target": requirements["votes_settled"],
            "ok": settled >= requirements["votes_settled"],
        },
        {
            "key": "agreement_rate",
            "label": "一致率",
            "current": agreement,
            "target": requirements["agreement_rate"],
            "ok": agreement is not None and agreement >= requirements["agreement_rate"],
        },
        quality_item,
        {"key": "days_served", "label": "在任天数", "current": served, "target": requirements["days_served"], "ok": served >= requirements["days_served"]},
    ]


def _next_tier(tier: str) -> str | None:
    index = TIER_ORDER.index(tier) if tier in TIER_ORDER else 0
    return TIER_ORDER[index + 1] if index + 1 < len(TIER_ORDER) else None


def tier_progress(member: JuryMember, now: datetime | None = None) -> dict[str, Any]:
    """/jury/me.tier_progress：离下一层还差什么。已是最高层时 next_tier 为 null、requirements 为空。"""
    next_tier = _next_tier(member.tier)
    if next_tier is None:
        return {"next_tier": None, "requirements": []}
    return {"next_tier": next_tier, "requirements": _requirement_items(member, TIER_RULES[next_tier]["requirements"], now)}


async def recent_agreement(db: AsyncSession, user_id: int, window: int) -> tuple[int, float]:
    """最近 window 张已结的票（不含质控题）：(样本数, 一致率)。没有样本时一致率是 0。"""
    rows = await db.execute(
        select(JuryVote.agreed)
        .where(JuryVote.juror_id == user_id, JuryVote.agreed.is_not(None), JuryVote.is_quality_check.is_(False))
        .order_by(JuryVote.voted_at.desc(), JuryVote.id.desc())
        .limit(window)
    )
    recent = [bool(value) for value in rows.scalars().all()]
    return len(recent), (sum(recent) / len(recent) if recent else 0.0)


async def _below_demote_line(db: AsyncSession, user_id: int, rule: dict[str, Any]) -> bool:
    """最近 window 张已结的票一致率是否低于这一层的降级线。不足 min_votes 张不判断。"""
    sample, rate = await recent_agreement(db, user_id, int(rule["window"]))
    return sample >= int(rule["min_votes"]) and rate < rule["agreement_rate"]


async def demote_warning(db: AsyncSession, member: JuryMember, margin: float) -> dict[str, Any] | None:
    """最近的一致率接近或低于当前层级的降级线时给一条提醒；见习没有降级线。"""
    rule = TIER_RULES.get(member.tier, {}).get("demote_below")
    if rule is None:
        return None
    sample, rate = await recent_agreement(db, member.user_id, int(rule["window"]))
    if sample < int(rule["min_votes"]) or rate >= rule["agreement_rate"] + margin:
        return None
    line = round(rule["agreement_rate"] * 100)
    return {
        "code": "demote_risk",
        "message": f"你最近 {sample} 张已出结论的票里，与结论一致的占 {rate * 100:.1f}%；低于 {line}% 会降为{TIER_LABELS[rule['to']]}",
        "current": round(rate, 3),
        "threshold": rule["agreement_rate"],
        "sample": sample,
    }


async def check_tier(db: AsyncSession, member: JuryMember, now: datetime | None = None) -> str | None:
    """有票出结论之后检查层级：一致率跌破降级线的降一层，否则条件满足的升一层。

    返回 "promoted" / "demoted" / None。调用方负责 commit；通知和徽章在这里一并处理。
    """
    if member.status != "active":
        return None
    current = TIER_RULES.get(member.tier)
    if current is not None and await _below_demote_line(db, member.user_id, current["demote_below"]):
        old = member.tier
        member.tier = current["demote_below"]["to"]
        notify(
            db,
            member.user_id,
            "demoted",
            f"你的委员层级调整为{TIER_LABELS[member.tier]}",
            f"最近 {current['demote_below']['window']} 张已出结论的票里，与最终结论一致的比例低于"
            f" {round(current['demote_below']['agreement_rate'] * 100)}%，层级由{TIER_LABELS[old]}调整为{TIER_LABELS[member.tier]}。一致率回升后可以再次晋升。",
        )
        await sync_badge(db, member, now=now)
        return "demoted"
    target = _next_tier(member.tier)
    if target is None:
        return None
    rule = TIER_RULES[target]
    if not all(item["ok"] for item in _requirement_items(member, rule["requirements"], now)):
        return None
    # 累计数字够了、但近期一致率在新层级的降级线以下：升上去马上又会被降回来，不升。
    if await _below_demote_line(db, member.user_id, rule["demote_below"]):
        return None
    member.tier = target
    notify(db, member.user_id, "promoted", f"你已晋升为{TIER_LABELS[target]}", "感谢你认真负责的评审。新的层级已经生效。")
    await sync_badge(db, member, now=now)
    return "promoted"


# ---------------------------------------------------------------- 成就


async def _quality_run(db: AsyncSession, user_id: int, limit: int) -> int:
    """质控题最近连续答对了几道。"""
    rows = await db.execute(
        select(JuryVote.agreed)
        .where(JuryVote.juror_id == user_id, JuryVote.is_quality_check.is_(True), JuryVote.agreed.is_not(None))
        .order_by(JuryVote.voted_at.desc(), JuryVote.id.desc())
        .limit(limit)
    )
    run = 0
    for agreed in rows.scalars().all():
        if not agreed:
            break
        run += 1
    return run


async def _achievement_progress(db: AsyncSession, user_id: int, member: JuryMember | None) -> dict[str, tuple[int, bool]]:
    """每个成就现在的 (进度, 是否达成)。"""
    now = datetime.now(UTC)
    settled = (member.votes_settled or 0) if member else 0
    agreed = (member.votes_agreed or 0) if member else 0
    votes_total = (member.votes_total or 0) if member else 0
    streak = current_streak(member, local_day(now))
    tier_rank = TIER_ORDER.index(member.tier) if member is not None and member.tier in TIER_ORDER else -1
    likes = int(
        (
            await db.execute(
                select(func.coalesce(func.sum(JuryOpinion.like_count), 0)).where(JuryOpinion.author_id == user_id, JuryOpinion.state != "deleted")
            )
        ).scalar_one()
        or 0
    )
    quality_run = await _quality_run(db, user_id, 10) if member is not None and (member.golden_total or 0) > 0 else 0
    first_exam = (
        await db.execute(
            select(JuryExamAttempt.passed)
            .where(JuryExamAttempt.user_id == user_id, JuryExamAttempt.submitted_at.is_not(None))
            .order_by(JuryExamAttempt.id.asc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return {
        "first_vote": (min(votes_total, 1), votes_total >= 1),
        "votes_10": (settled, settled >= 10),
        "votes_50": (settled, settled >= 50),
        "votes_100": (settled, settled >= 100),
        "sharp_eye": (settled, settled >= SHARP_EYE_VOTES and agreed / settled >= SHARP_EYE_RATE),
        "streak_7": (streak, streak >= 7),
        "streak_30": (streak, streak >= 30),
        "quality_10": (quality_run, quality_run >= 10),
        "liked_10": (likes, likes >= 10),
        "exam_first_try": (1 if first_exam else 0, bool(first_exam)),
        "tier_regular": (1 if tier_rank >= 1 else 0, tier_rank >= 1),
        "tier_senior": (1 if tier_rank >= 2 else 0, tier_rank >= 2),
    }


async def check_achievements(db: AsyncSession, user_id: int, member: JuryMember | None = None) -> list[str]:
    """把已经达成但还没记下的成就记上，返回新获得的 key。调用方负责 commit。"""
    if member is None:
        member = (await db.execute(select(JuryMember).where(JuryMember.user_id == user_id))).scalar_one_or_none()
    progress = await _achievement_progress(db, user_id, member)
    satisfied = [item for item in ACHIEVEMENTS if progress[item["key"]][1]]
    if not satisfied:
        return []
    earned = set((await db.execute(select(JuryAchievement.key).where(JuryAchievement.user_id == user_id))).scalars().all())
    fresh: list[str] = []
    now = datetime.now(UTC)
    for item in satisfied:
        if item["key"] in earned:
            continue
        db.add(JuryAchievement(user_id=user_id, key=item["key"], earned_at=now))
        queue_event(db, user_id, "jury.achievement.earned", {"key": item["key"], "label": item["label"]})
        fresh.append(item["key"])
    if fresh:
        await db.flush()
    return fresh


async def list_achievements(db: AsyncSession, user_id: int) -> list[dict[str, Any]]:
    member = (await db.execute(select(JuryMember).where(JuryMember.user_id == user_id))).scalar_one_or_none()
    progress = await _achievement_progress(db, user_id, member)
    earned = dict((await db.execute(select(JuryAchievement.key, JuryAchievement.earned_at).where(JuryAchievement.user_id == user_id))).all())
    items: list[dict[str, Any]] = []
    for item in ACHIEVEMENTS:
        earned_at = penalty_service.aware(earned.get(item["key"]))
        current = progress[item["key"]][0]
        items.append(
            {
                "key": item["key"],
                "label": item["label"],
                "description": item["description"],
                "earned_at": earned_at,
                # 已经获得的成就进度条保持满格：连续天数之类的数字之后会掉回去
                "current": item["target"] if earned_at is not None else min(current, item["target"]),
                "target": item["target"],
            }
        )
    return items


# ---------------------------------------------------------------- 排行榜


def period_start(period: str, now: datetime) -> datetime | None:
    """自然周 / 自然月（UTC+8）的起点；all 没有起点。"""
    if period == "all":
        return None
    today = local_day(now)
    first = today - timedelta(days=today.weekday()) if period == "week" else today.replace(day=1)
    return datetime(first.year, first.month, first.day, tzinfo=LOCAL_TZ).astimezone(UTC)


async def _ranking(db: AsyncSession, period: str, now: datetime) -> list[dict[str, Any]]:
    """本期有得分的在任委员，按分数从高到低。"""
    members = (
        await db.execute(
            select(JuryMember, User)
            .join(User, User.id == JuryMember.user_id)
            .where(JuryMember.status == "active", or_(JuryMember.term_ends_at.is_(None), JuryMember.term_ends_at > now))
        )
    ).all()
    start = period_start(period, now)
    if start is None:
        points = {member.user_id: member.points or 0 for member, _user in members}
        votes = {member.user_id: member.votes_total or 0 for member, _user in members}
    else:
        point_rows = await db.execute(
            select(JuryPointLog.user_id, func.coalesce(func.sum(JuryPointLog.points), 0)).where(JuryPointLog.created_at >= start).group_by(JuryPointLog.user_id)
        )
        points = {user_id: int(total or 0) for user_id, total in point_rows.all()}
        vote_rows = await db.execute(select(JuryVote.juror_id, func.count()).where(JuryVote.voted_at >= start).group_by(JuryVote.juror_id))
        votes = {user_id: int(count or 0) for user_id, count in vote_rows.all()}
    rows = [
        {
            "user_id": member.user_id,
            "points": points.get(member.user_id, 0),
            "tier": member.tier,
            "votes": votes.get(member.user_id, 0),
            # 没公开身份的只留名次和分数
            "user": UserPublic.model_validate(user) if member.badge_visible else None,
        }
        for member, user in members
        if points.get(member.user_id, 0) > 0
    ]
    rows.sort(key=lambda row: (-row["points"], -row["votes"], row["user_id"]))
    return rows


async def leaderboard(db: AsyncSession, user_id: int, *, period: str = "week", limit: int = 50) -> dict[str, Any]:
    """排行榜。名次在进程内缓存 60 秒；「哪一行是我」每次请求现算，不进缓存。"""
    tick = time.monotonic()
    cached = _leaderboard_cache.get(period)
    if cached is None or tick - cached[0] >= LEADERBOARD_CACHE_SECONDS:
        now = datetime.now(UTC)
        cached = (tick, now, await _ranking(db, period, now))
        _leaderboard_cache[period] = cached
    _tick, generated_at, rows = cached
    # me 和榜单行同形：即使排在 limit 之外，也能知道自己的名次
    me: dict[str, Any] | None = None
    items: list[dict[str, Any]] = []
    for rank, row in enumerate(rows, start=1):
        mine = row["user_id"] == user_id
        item = {"rank": rank, "points": row["points"], "tier": row["tier"], "votes": row["votes"], "me": mine, "user": row["user"]}
        if mine:
            me = item
        if rank <= limit:
            items.append(item)
    return {"period": period, "items": items, "me": me, "generated_at": generated_at}
