"""风纪委员：社区成员评审被举报的群聊消息和公开内容。

委员只看得到案件快照里匿名化后的内容，看不到举报人和被举报人是谁；
他们的结论和机器一样受处罚上限约束，拿不准或没有共识的案件交回管理员。
"""

from __future__ import annotations

import json
import logging
import random
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.case import JuryExamAttempt, JuryMember, JuryNotification, JuryOpinion, JuryPracticeAnswer, JuryVote, ReportCase
from app.models.conversation import Conversation, ConversationMember
from app.models.enums import ConversationType, UserStatus
from app.models.friend import Friendship
from app.models.group_activity import GroupMemberDailyActivity
from app.models.message import Message
from app.models.report import AdminAuditLog, UserReport
from app.models.user import User
from app.services import cases as case_service
from app.services import jury_bank as bank_service
from app.services import jury_points as points_service
from app.services import jury_settings
from app.services import penalties as penalty_service

logger = logging.getLogger(__name__)
UTC = timezone.utc
# 个人统计里的「每天」按北京时间的自然日算
LOCAL_TZ = timezone(timedelta(hours=8))

# 阈值都来自部署配置（app/core/config.py 里 jury_ 开头的项），这里留同名的模块变量方便引用和测试替换。
MIN_ACCOUNT_AGE_DAYS = settings.jury_min_account_age_days
MIN_RECENT_MESSAGES = settings.jury_min_recent_messages
RECENT_ACTIVITY_DAYS = settings.jury_recent_activity_days
TERM_DAYS = settings.jury_term_days
DAILY_VOTE_LIMIT = settings.jury_daily_vote_limit
SEATS_PER_CASE = settings.jury_seats_per_case
ASSIGNMENT_TTL_HOURS = settings.jury_assignment_ttl_hours
# 平均每 GOLDEN_EVERY 个案件里混入一道已有结论的质控题；位置是随机的，见 _wants_quality_check
GOLDEN_EVERY = settings.jury_golden_every
QUALITY_MIN_GAP = settings.jury_quality_min_gap
QUALITY_MAX_GAP = settings.jury_quality_max_gap
MIN_SETTLED_FOR_REVIEW = settings.jury_min_settled_for_review
MIN_GOLDEN_FOR_REVIEW = settings.jury_min_golden_for_review
MIN_ACCURACY = settings.jury_min_accuracy
# 公开群成员数达到这个值后不再按群成员回避
LARGE_GROUP_MEMBERS = settings.jury_large_group_members
CONTENT_RETENTION_DAYS = settings.jury_content_retention_days
TREND_DAYS = 30

# 定为严重违规（l3）至少要这么多张有效票；同一案件累计这么多张「上下文不足」后转出
SEVERE_MIN_VOTES = settings.jury_severe_min_votes
NEED_CONTEXT_VOTES = settings.jury_need_context_votes
RECUSE_DAILY_LIMIT = settings.jury_recuse_daily_limit
# 弃权过多：近 ABSTAIN_WINDOW 张票里「拿不准 + 上下文不足」的比例，不足 ABSTAIN_MIN_VOTES 张不判断
ABSTAIN_WINDOW = settings.jury_abstain_window
ABSTAIN_MIN_VOTES = settings.jury_abstain_min_votes
ABSTAIN_WARN_RATE = settings.jury_abstain_warn_rate
ABSTAIN_COOLDOWN_RATE = settings.jury_abstain_cooldown_rate
ABSTAIN_COOLDOWN_HOURS = settings.jury_abstain_cooldown_hours
# 「快要被降级 / 暂停」的提醒：比率离那条线不到 WARNING_MARGIN、暂停线的样本数达到要求的 WARNING_SAMPLE_RATIO 倍时开始提醒
WARNING_MARGIN = settings.jury_warning_margin
WARNING_SAMPLE_RATIO = settings.jury_warning_sample_ratio

VOTES = {"none", "l1", "l2", "l3", "abstain", "need_context", "recuse"}
# 计入表决的票（有效票）；abstain / need_context 算投了票但不计入表决，recuse 根本不算投票
DECIDED_VOTES = ("none", "l1", "l2", "l3")
UNDECIDED_VOTES = ("abstain", "need_context")
_VOTE_LEVEL = {"l1": 1, "l2": 2, "l3": 3}
# 投违规票时可选的类别：不含色情类（那类案件不进委员队列），也不含只给系统用的
VOTE_CATEGORIES: tuple[str, ...] = tuple(
    key
    for key in penalty_service.CATEGORIES
    if key not in {"sexual", "minor_sexual", "vulgar", "suggestive", "unverified_media", "admin_action"}
)
TIERS: tuple[tuple[str, str], ...] = (("trainee", "见习委员"), ("regular", "正式委员"), ("senior", "资深委员"))
TIER_KEYS = {key for key, _label in TIERS}
# 分案时一批看多少个候选、最多往后翻多少个
PICK_BATCH = 30
PICK_SCAN_LIMIT = 300
AVAILABLE_CASES_CAP = 50


# ---------------------------------------------------------------- 资格


async def _clean_record(db: AsyncSession, user: User, now: datetime) -> tuple[bool, int]:
    """「没有违规记录」这一项：(是否满足, 近期违规条数)。申请和任期届满续任都看它。"""
    points = await penalty_service.active_points(db, user.id, now)
    recent_violations = int(
        (
            await db.execute(
                select(func.count())
                .select_from(penalty_service.UserViolation)
                .where(
                    penalty_service.UserViolation.user_id == user.id,
                    penalty_service.UserViolation.status == "active",
                    penalty_service.UserViolation.level > 0,
                    penalty_service.UserViolation.created_at >= now - timedelta(days=penalty_service.POINT_WINDOW_DAYS),
                )
            )
        ).scalar_one()
        or 0
    )
    return recent_violations == 0 and points == 0 and user.status == UserStatus.ACTIVE, recent_violations


async def _recent_messages(db: AsyncSession, user_id: int, now: datetime) -> int:
    """近期发言数：消息表里的条数和群活跃日报里的发言数，取较大的那个。

    会话只留最近 1000 条消息，活跃的大群里消息表会少算；日报不受这个影响，但只有群聊。
    """
    since = now - timedelta(days=RECENT_ACTIVITY_DAYS)
    messages = await db.execute(select(func.count()).select_from(Message).where(Message.sender_id == user_id, Message.created_at >= since))
    # 日报按 UTC 日期记
    daily = await db.execute(
        select(func.coalesce(func.sum(GroupMemberDailyActivity.message_count), 0)).where(
            GroupMemberDailyActivity.user_id == user_id, GroupMemberDailyActivity.activity_date >= since.date()
        )
    )
    return max(int(messages.scalar_one() or 0), int(daily.scalar_one() or 0))


async def eligibility(db: AsyncSession, user: User) -> list[dict[str, Any]]:
    now = datetime.now(UTC)
    created_at = penalty_service.aware(user.created_at) or now
    age_days = (now - created_at).days
    clean, recent_violations = await _clean_record(db, user, now)
    recent_messages = await _recent_messages(db, user.id, now)
    return [
        {"key": "account_age", "ok": age_days >= MIN_ACCOUNT_AGE_DAYS, "text": f"注册满 {MIN_ACCOUNT_AGE_DAYS} 天", "detail": f"已注册 {age_days} 天"},
        {"key": "ccw_bound", "ok": bool(user.ccw_student_oid), "text": "已绑定 CCW 账号", "detail": "已绑定" if user.ccw_student_oid else "未绑定"},
        {
            "key": "clean_record",
            "ok": clean,
            "text": f"近 {penalty_service.POINT_WINDOW_DAYS} 天没有违规记录",
            "detail": "无违规" if recent_violations == 0 else f"有 {recent_violations} 条违规记录",
        },
        {
            "key": "activity",
            "ok": recent_messages >= MIN_RECENT_MESSAGES,
            "text": f"近 {RECENT_ACTIVITY_DAYS} 天发言不少于 {MIN_RECENT_MESSAGES} 条",
            "detail": f"已发言 {recent_messages} 条",
        },
    ]


async def get_member(db: AsyncSession, user_id: int) -> JuryMember | None:
    result = await db.execute(select(JuryMember).where(JuryMember.user_id == user_id))
    return result.scalar_one_or_none()


def _is_serving(member: JuryMember | None, now: datetime | None = None) -> bool:
    if member is None or member.status != "active":
        return False
    term_ends_at = penalty_service.aware(member.term_ends_at)
    return term_ends_at is None or term_ends_at > (now or datetime.now(UTC))


def member_state(member: JuryMember | None, now: datetime | None = None) -> str:
    """none / applied / active / expired / rejected / suspended / retired。"""
    if member is None:
        return "none"
    term_ends_at = penalty_service.aware(member.term_ends_at)
    if member.status == "active" and term_ends_at is not None and term_ends_at <= (now or datetime.now(UTC)):
        return "expired"
    return member.status


RESTRICTED_MESSAGE = "受处罚期间不能参与评审"


def restriction(user: User, now: datetime | None = None) -> dict[str, Any] | None:
    """委员本人正在受处罚（被封禁或被平台禁言）时不能评审。没有受限返回 None。"""
    if user.status != UserStatus.ACTIVE:
        # until 为 null 表示永久
        return {"reason": "banned", "until": penalty_service.aware(user.banned_until), "message": RESTRICTED_MESSAGE}
    muted_until = penalty_service.mute_state(user, now)
    if muted_until is not None:
        return {"reason": "muted", "until": muted_until, "message": RESTRICTED_MESSAGE}
    return None


def member_stats(member: JuryMember) -> dict[str, Any]:
    return {
        "votes_total": member.votes_total,
        "votes_settled": member.votes_settled,
        "votes_agreed": member.votes_agreed,
        "agreement_rate": round(member.votes_agreed / member.votes_settled, 3) if member.votes_settled else None,
        "golden_total": member.golden_total,
        "golden_correct": member.golden_correct,
    }


def _iso(moment: datetime) -> str:
    """错误详情里的时间：和响应模型输出的格式保持一致。"""
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def snapshot_time(value: Any) -> str | None:
    """快照里的时间是当时直接 isoformat() 存下来的字符串，不带时区后缀，实际是 UTC。

    输出时统一成带 Z 的 ISO 字符串；本来就带时区的换算到 UTC，认不出来的原样返回。
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value)
    except ValueError:
        return value
    return _iso(penalty_service.aware(moment))


async def ensure_term(db: AsyncSession, member: JuryMember | None, user: User | None = None, now: datetime | None = None) -> bool:
    """任期届满时的处理：没有违规记录就自动续任，否则保持 expired 并通知一次。

    返回有没有改动，调用方负责 commit 和 flush_events。判过「不续任」之后不会再自动续，等本人重新申请。
    """
    now = now or datetime.now(UTC)
    if member is None or member.status != "active":
        return False
    term_ends_at = penalty_service.aware(member.term_ends_at)
    if term_ends_at is None or term_ends_at > now:
        return False
    notified = await db.execute(
        select(JuryNotification.id)
        .where(JuryNotification.user_id == member.user_id, JuryNotification.kind == "term_expired", JuryNotification.created_at >= term_ends_at)
        .limit(1)
    )
    if notified.scalar_one_or_none() is not None:
        return False
    user = user or await db.get(User, member.user_id)
    if user is None:
        return False
    clean, _recent = await _clean_record(db, user, now)
    if clean:
        member.term_ends_at = now + timedelta(days=TERM_DAYS)
        points_service.queue_event(db, member.user_id, "jury.status.changed", {})
    else:
        points_service.notify(
            db,
            member.user_id,
            "term_expired",
            "你的风纪委员任期已届满",
            "任期届满时你有尚未过期的违规记录或正在受处罚，没有自动续任。条件恢复后可以重新申请。",
        )
    await points_service.sync_badge(db, member, user, now)
    return True


async def renew_expired_terms(db: AsyncSession) -> int:
    """定时循环里调用：处理所有任期已经届满的委员，自己提交事务。返回处理了多少人。"""
    now = datetime.now(UTC)
    members = (
        await db.execute(select(JuryMember).where(JuryMember.status == "active", JuryMember.term_ends_at.is_not(None), JuryMember.term_ends_at <= now))
    ).scalars().all()
    changed = 0
    for member in members:
        if await ensure_term(db, member, now=now):
            changed += 1
    if changed:
        await db.commit()
        await points_service.flush_events(db)
    return changed


async def _abstain_state(db: AsyncSession, user_id: int) -> tuple[int, float]:
    """近 ABSTAIN_WINDOW 张票（不含回避）的张数，和其中「拿不准 + 上下文不足」的比例。"""
    rows = await db.execute(
        select(JuryVote.vote)
        .where(JuryVote.juror_id == user_id, JuryVote.voted_at.is_not(None))
        .order_by(JuryVote.voted_at.desc(), JuryVote.id.desc())
        .limit(ABSTAIN_WINDOW)
    )
    votes = rows.scalars().all()
    if not votes:
        return 0, 0.0
    return len(votes), sum(1 for vote in votes if vote in UNDECIDED_VOTES) / len(votes)


async def _recuses_since(db: AsyncSession, juror_id: int, since: datetime) -> int:
    result = await db.execute(
        select(func.count()).select_from(JuryVote).where(JuryVote.juror_id == juror_id, JuryVote.recused_at.is_not(None), JuryVote.recused_at >= since)
    )
    return int(result.scalar_one() or 0)


ROLLING_WINDOW = timedelta(hours=24)


async def rolling_resets_at(db: AsyncSession, column: Any, *clauses: Any, now: datetime) -> datetime | None:
    """按最近 24 小时滚动的上限，下一个名额什么时候释放：窗口里最早那一条的时间 + 24 小时；窗口里没有则为 None。

    评审上限、回避上限、练习上限都是这个口径（不在零点恢复）。column 是计数用的那个时间列。
    """
    oldest = penalty_service.aware((await db.execute(select(func.min(column)).where(*clauses, column >= now - ROLLING_WINDOW))).scalar_one_or_none())
    return oldest + ROLLING_WINDOW if oldest is not None else None


async def _daily_resets_at(db: AsyncSession, juror_id: int, now: datetime) -> datetime | None:
    return await rolling_resets_at(db, JuryVote.voted_at, JuryVote.juror_id == juror_id, now=now)


async def _recuse_resets_at(db: AsyncSession, juror_id: int, now: datetime) -> datetime | None:
    return await rolling_resets_at(db, JuryVote.recused_at, JuryVote.juror_id == juror_id, now=now)


def _suspend_warning(member: JuryMember) -> dict[str, Any] | None:
    """一致率或质控正确率接近或低于自动暂停线时给一条提醒；两项都触发时给离线更近（或跌得更深）的那一项。"""
    candidates: list[tuple[float, int, str]] = []
    settled, golden = member.votes_settled or 0, member.golden_total or 0
    if settled > 0 and settled >= MIN_SETTLED_FOR_REVIEW * WARNING_SAMPLE_RATIO:
        candidates.append((member.votes_agreed / settled, settled, "一致率"))
    if golden > 0 and golden >= MIN_GOLDEN_FOR_REVIEW * WARNING_SAMPLE_RATIO:
        candidates.append((member.golden_correct / golden, golden, "质控题正确率"))
    risky = [item for item in candidates if item[0] < MIN_ACCURACY + WARNING_MARGIN]
    if not risky:
        return None
    rate, sample, label = min(risky, key=lambda item: item[0])
    return {
        "code": "suspend_risk",
        "message": f"你的{label}是 {round(rate * 100)}%（{sample} 张）；低于 {round(MIN_ACCURACY * 100)}% 会被系统暂停资格",
        "current": round(rate, 3),
        "threshold": MIN_ACCURACY,
        "sample": sample,
    }


async def _warnings(db: AsyncSession, member: JuryMember) -> list[dict[str, Any]]:
    """/jury/me.warnings。current 就是服务端实际用来判断的那个数，客户端直接展示。"""
    warnings: list[dict[str, Any]] = []
    count, rate = await _abstain_state(db, member.user_id)
    if count >= ABSTAIN_MIN_VOTES and rate > ABSTAIN_WARN_RATE:
        warnings.append(
            {
                "code": "high_abstain",
                "message": f"你最近 {count} 张票里「拿不准」和「上下文不足」占了 {round(rate * 100)}%，超过 {round(ABSTAIN_COOLDOWN_RATE * 100)}% 会暂停发案",
                "current": round(rate, 3),
                "threshold": ABSTAIN_WARN_RATE,
                "sample": count,
            }
        )
    demote = await points_service.demote_warning(db, member, WARNING_MARGIN)
    if demote is not None:
        warnings.append(demote)
    suspend = _suspend_warning(member)
    if suspend is not None:
        warnings.append(suspend)
    return warnings


async def my_status(db: AsyncSession, user: User) -> dict[str, Any]:
    from app.services import jury_exam as exam_service

    now = datetime.now(UTC)
    member = await get_member(db, user.id)
    if await ensure_term(db, member, user, now):
        await db.commit()
        await points_service.flush_events(db)
    requirements = await eligibility(db, user)
    term_ends_at = penalty_service.aware(member.term_ends_at) if member else None
    serving = _is_serving(member, now)
    voted_today = 0
    cooldown_until = None
    if member is not None:
        voted_today = await _votes_since(db, user.id, now - timedelta(hours=24))
        cooldown_until = penalty_service.aware(member.cooldown_until)
        if cooldown_until is not None and cooldown_until <= now:
            cooldown_until = None
    return {
        "status": member_state(member, now),
        "eligible": all(item["ok"] for item in requirements),
        "requirements": requirements,
        "term_ends_at": term_ends_at,
        "review_note": member.review_note if member and member.status in {"rejected", "suspended"} else None,
        "stats": member_stats(member) if member else None,
        "daily_limit": DAILY_VOTE_LIMIT,
        "voted_today": voted_today,
        # 评审上限按最近 24 小时滚动：下一个名额什么时候释放；最近 24 小时没投过票为 null
        "daily_resets_at": await _daily_resets_at(db, user.id, now) if member is not None else None,
        "tier": member.tier if member is not None and serving else None,
        "available_cases": await available_cases(db, user, voted_today=voted_today) if serving else None,
        # 有值时评审、发观点、点赞都会被拒绝（juror_restricted）；不是在任委员恒为 null
        "restriction": restriction(user, now) if serving else None,
        "exam": await exam_service.summary(db, user.id, member, now),
        "exam_required": await jury_settings.is_enabled(db, "exam_required"),
        "points": (member.points or 0) if member else 0,
        "tier_progress": points_service.tier_progress(member, now) if member is not None and serving else None,
        "badge_visible": bool(member.badge_visible) if member else False,
        "warnings": await _warnings(db, member) if member is not None and serving else [],
        "cooldown_until": cooldown_until,
        "recuse_left_today": max(0, RECUSE_DAILY_LIMIT - await _recuses_since(db, user.id, now - timedelta(hours=24))) if serving else None,
        # 回避上限同样按最近 24 小时滚动；最近 24 小时没回避过为 null
        "recuse_resets_at": await _recuse_resets_at(db, user.id, now) if serving else None,
        # 三期：现在能分给我的合议 / 复审件数；不是在任的资深委员为 null
        "senior": await _senior_counts(db, user, member) if serving else None,
    }


async def _senior_counts(db: AsyncSession, user: User, member: JuryMember | None) -> dict[str, int] | None:
    if member is None or member.tier != "senior":
        return None
    from app.services import jury_panel as panel_service
    from app.services import jury_reviews as review_service

    return {"panel": await panel_service.available_count(db, user), "appeals": await review_service.available_count(db, user)}


def _activate(member: JuryMember, now: datetime, tier: str | None = None) -> None:
    """任命 / 复职：管理员批准和考试通过后的自动任命都走这里。"""
    if member.status != "active" or member.serving_since is None:
        # 「本次任职」从这里算起；已经在任的人被再批准一次不重置。
        member.serving_since = now
    member.status = "active"
    member.term_ends_at = now + timedelta(days=TERM_DAYS)
    if tier is not None:
        member.tier = tier


async def grant_exam_points(db: AsyncSession, member: JuryMember) -> None:
    """通过入门考试的风纪值只给一次。考试可能发生在成为委员之前，那时没有地方记分，等有了成员记录再补。"""
    passed = await db.execute(
        select(JuryExamAttempt.id).where(JuryExamAttempt.user_id == member.user_id, JuryExamAttempt.passed.is_(True)).limit(1)
    )
    if passed.scalar_one_or_none() is not None:
        await points_service.award(db, member, "exam_passed")


def can_auto_appoint(member: JuryMember | None) -> bool:
    """自动任命只给从没被处理过的人：被拒绝、卸任、暂停过的仍然要管理员点头。"""
    return member is None or member.status == "applied"


async def auto_appoint(db: AsyncSession, user: User, member: JuryMember | None, now: datetime) -> JuryMember:
    """通过考试且 auto_appoint 打开时直接任命为见习委员。审计日志里 actor 为空。调用方负责 commit 和 flush_events。"""
    if member is None:
        member = JuryMember(user_id=user.id, applied_at=now)
        db.add(member)
    _activate(member, now)
    member.reviewed_at = now
    member.reviewed_by_id = None
    member.review_note = None
    db.add(AdminAuditLog(actor_id=None, action="jury.member.auto_appoint", target_type="user", target_id=user.id, details="通过入门考试，自动任命"))
    await db.flush()
    await grant_exam_points(db, member)
    await points_service.sync_badge(db, member, user, now)
    await points_service.check_achievements(db, user.id, member)
    points_service.queue_event(db, user.id, "jury.status.changed", {})
    return member


async def apply(db: AsyncSession, user: User, statement: str | None) -> JuryMember:
    from app.services import jury_exam as exam_service

    requirements = await eligibility(db, user)
    failed = [item["text"] for item in requirements if not item["ok"]]
    if failed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "jury_not_eligible", "message": "暂不满足申请条件：" + "、".join(failed)},
        )
    now = datetime.now(UTC)
    member = await get_member(db, user.id)
    if member is not None:
        if member.status == "applied":
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="申请正在审核中")
        if member.status == "suspended":
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail={"code": "jury_suspended", "message": "你的风纪委员资格已被暂停"})
        if _is_serving(member, now):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="你已经是风纪委员")
        if member.status == "active":
            # 任期届满后续任：之前已通过审批且没有被停职，条件仍满足就直接续期。
            member.term_ends_at = now + timedelta(days=TERM_DAYS)
            await points_service.sync_badge(db, member, user, now)
            await db.commit()
            return member
    passed = await exam_service.has_valid_pass(db, user.id, member, now)
    switches = await jury_settings.get_switches(db)
    if not passed and switches["exam_required"]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail={"code": "exam_required", "message": "申请前需要先通过入门考试"})
    text = statement.strip()[:500] if statement else None
    if passed and switches["auto_appoint"] and can_auto_appoint(member):
        member = await auto_appoint(db, user, member, now)
        member.statement = text
        await db.commit()
        await points_service.flush_events(db)
        return member
    if member is None:
        member = JuryMember(user_id=user.id, applied_at=now)
        db.add(member)
    member.status = "applied"
    member.statement = text
    member.applied_at = now
    member.review_note = None
    await db.commit()
    return member


async def review_member(
    db: AsyncSession, user_id: int, *, action: str, actor_id: int, note: str | None, tier: str | None = None, points: int | None = None
) -> JuryMember:
    if tier is not None and tier not in TIER_KEYS:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail={"code": "invalid_tier", "message": "未知的委员层级"})
    member = await get_member(db, user_id)
    if member is None:
        if action != "approve":
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Jury member not found")
        # 管理员可以直接任命，不要求对方先申请。
        if await db.get(User, user_id) is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
        member = JuryMember(user_id=user_id)
        db.add(member)
    now = datetime.now(UTC)
    text = note.strip()[:500] if note else None
    details = text if tier is None else "；".join(part for part in (f"tier={tier}", text) if part)
    if action == "set_tier":
        # 只调层级：不动资格状态，也不覆盖之前写给本人看的审核说明。
        if tier is None:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail={"code": "invalid_tier", "message": "请指定委员层级"})
        member.tier = tier
    elif action == "adjust_points":
        # 只动风纪值，同样不碰资格状态和审核说明；note 记在流水和审计日志里。
        if not points:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail={"code": "invalid_points", "message": "请填写要增减的风纪值"})
        log = await points_service.award(db, member, "admin_adjust", points=points, unique=False, note=text)
        details = "；".join(part for part in (f"points={points}", f"applied={log.points}" if log is not None else None, text) if part)
    else:
        if action in {"approve", "reinstate"}:
            # 创始任命：可以直接任命为任意层级。
            _activate(member, now, tier)
        elif action == "reject":
            member.status = "rejected"
        elif action == "suspend":
            member.status = "suspended"
        elif action == "retire":
            member.status = "retired"
        else:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Unknown action")
        member.reviewed_at = now
        member.reviewed_by_id = actor_id
        member.review_note = text
    db.add(AdminAuditLog(actor_id=actor_id, action=f"jury.member.{action}", target_type="user", target_id=user_id, details=details))
    await db.flush()
    if action in {"approve", "reinstate"}:
        await grant_exam_points(db, member)
    # 状态或层级变了，对外展示的徽章跟着变。
    await points_service.sync_badge(db, member, now=now)
    await points_service.check_achievements(db, user_id, member)
    await db.commit()
    points_service.reset_leaderboard_cache()
    await points_service.flush_events(db)
    await _notify_member(user_id)
    return member


async def update_settings(db: AsyncSession, user: User, *, badge_visible: bool | None = None) -> JuryMember:
    """本人的设置，目前只有是否公开展示委员身份。还没有委员记录的人没有东西可设。"""
    member = await get_member(db, user.id)
    if member is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail={"code": "not_juror", "message": "成为风纪委员之后才能设置"})
    if badge_visible is not None and bool(member.badge_visible) != badge_visible:
        member.badge_visible = badge_visible
        await points_service.sync_badge(db, member, user)
        await db.commit()
        points_service.reset_leaderboard_cache()
    return member


async def _notify_member(user_id: int) -> None:
    from app.websocket.manager import admin_manager, manager

    try:
        await manager.send_to_user(user_id, "jury.status.changed", {})
        await admin_manager.broadcast("admin.jury.changed", {"user_id": user_id})
    except Exception:
        logger.exception("Failed to notify jury member", extra={"user_id": user_id})


async def notify_case_voters(db: AsyncSession, case_id: int, event_type: str, data: dict[str, Any], *, exclude: int | None = None) -> None:
    """给这个案件投过票的委员发事件（质控题的作答者不算）。发不出去只记日志，不影响主流程。

    case_id 是案件的真实编号；每个人收到的 data.case_id 是他自己的评审编号，所以逐个发（一个案件最多七个人）。
    """
    from app.websocket.manager import manager

    try:
        rows = await db.execute(
            select(JuryVote.juror_id, JuryVote.id).where(
                JuryVote.case_id == case_id, JuryVote.voted_at.is_not(None), JuryVote.is_quality_check.is_(False)
            )
        )
        for juror_id, review_id in rows.all():
            if juror_id != exclude:
                await manager.send_to_user(juror_id, event_type, {**data, "case_id": review_id})
    except Exception:
        logger.exception("Failed to notify case voters", extra={"case_id": case_id, "event": event_type})


# ---------------------------------------------------------------- 分案


async def _votes_since(db: AsyncSession, juror_id: int, since: datetime) -> int:
    result = await db.execute(
        select(func.count()).select_from(JuryVote).where(JuryVote.juror_id == juror_id, JuryVote.voted_at.is_not(None), JuryVote.voted_at >= since)
    )
    return int(result.scalar_one() or 0)


async def _require_serving(db: AsyncSession, user: User) -> JuryMember:
    member = await get_member(db, user.id)
    # 任期刚好届满的在这里惰性续任，不用等定时循环。
    if await ensure_term(db, member, user):
        await db.commit()
        await points_service.flush_events(db)
    if not _is_serving(member):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail={"code": "not_juror", "message": "你不是在任的风纪委员"})
    if restriction(user) is not None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail={"code": "juror_restricted", "message": RESTRICTED_MESSAGE})
    assert member is not None
    return member


async def _conflict_context(db: AsyncSession, juror_id: int) -> dict[str, Any]:
    """一位委员的回避关系，一次查出来给整批案件用。"""
    friends = await db.execute(
        select(Friendship.user_id, Friendship.friend_id).where(or_(Friendship.user_id == juror_id, Friendship.friend_id == juror_id))
    )
    conversations = await db.execute(select(ConversationMember.conversation_id).where(ConversationMember.user_id == juror_id))
    reported = await db.execute(select(UserReport.case_id).where(UserReport.reporter_id == juror_id, UserReport.case_id.is_not(None)))
    return {
        "friends": {other for pair in friends.all() for other in pair if other != juror_id},
        "conversations": set(conversations.scalars().all()),
        "reported": set(reported.scalars().all()),
        # conversation_id -> 是不是大型公开群
        "large_groups": {},
        # case_id -> 快照里的发言人；只有 _prefetch_conflicts 预取过的才在里面
        "speakers": {},
    }


def _speaker_ids(snapshot: dict[str, Any]) -> set[Any]:
    return {item.get("sender_id") for item in snapshot.get("messages") or [] if isinstance(item, dict)}


async def _prefetch_conflicts(db: AsyncSession, context: dict[str, Any], cases: list[ReportCase]) -> None:
    """把一批案件判断回避要用到的东西一次查出来放进 context，之后 _has_conflict 不再逐个案件查库。

    三类查询（哪些是公开群、它们的成员数、大型公开群里那些案件的快照，快照按批取），不随案件数逐个增加。
    """
    cache: dict[int, bool] = context["large_groups"]
    conversation_ids = {case.conversation_id for case in cases if case.conversation_id is not None} - set(cache)
    if conversation_ids:
        open_groups = set(
            (
                await db.execute(
                    select(Conversation.id).where(
                        Conversation.id.in_(conversation_ids), Conversation.type == ConversationType.GROUP, Conversation.join_mode == "open"
                    )
                )
            ).scalars().all()
        )
        counts: dict[int, int] = {}
        if open_groups:
            rows = await db.execute(
                select(ConversationMember.conversation_id, func.count())
                .where(ConversationMember.conversation_id.in_(open_groups))
                .group_by(ConversationMember.conversation_id)
            )
            counts = {conversation_id: int(count) for conversation_id, count in rows.all()}
        for conversation_id in conversation_ids:
            cache[conversation_id] = conversation_id in open_groups and counts.get(conversation_id, 0) >= LARGE_GROUP_MEMBERS
    in_large = [case for case in cases if case.conversation_id is not None and cache.get(case.conversation_id) and case.id not in context["speakers"]]
    if in_large:
        for case_id, snapshot in (await case_service.case_snapshots(db, in_large)).items():
            context["speakers"][case_id] = _speaker_ids(snapshot)


async def _is_large_open_group(db: AsyncSession, conversation_id: int, cache: dict[int, bool]) -> bool:
    if conversation_id not in cache:
        conversation = await db.get(Conversation, conversation_id)
        large = False
        if conversation is not None and conversation.type == ConversationType.GROUP and conversation.join_mode == "open":
            members = await db.execute(
                select(func.count()).select_from(ConversationMember).where(ConversationMember.conversation_id == conversation_id)
            )
            large = int(members.scalar_one() or 0) >= LARGE_GROUP_MEMBERS
        cache[conversation_id] = large
    return cache[conversation_id]


async def _snapshot_speakers(db: AsyncSession, case: ReportCase) -> set[Any]:
    return _speaker_ids(await case_service.case_snapshot(db, case))


async def _has_conflict(db: AsyncSession, juror_id: int, case: ReportCase, context: dict[str, Any] | None = None) -> bool:
    """回避：本人涉案、举报人、被举报人的好友、案发会话的成员。

    大型公开群里几乎人人都是成员，按成员回避会让案件没人能审：这类群只回避在案件上下文里说过话的人。
    """
    if context is None:
        context = await _conflict_context(db, juror_id)
    if case.reported_user_id == juror_id or case.id in context["reported"]:
        return True
    if case.reported_user_id is not None and case.reported_user_id in context["friends"]:
        return True
    if case.conversation_id is None:
        return False
    if await _is_large_open_group(db, case.conversation_id, context["large_groups"]):
        speakers = context["speakers"].get(case.id)
        return juror_id in (speakers if speakers is not None else await _snapshot_speakers(db, case))
    return case.conversation_id in context["conversations"]


async def _assignable_cases(db: AsyncSession, juror_id: int, *, golden: bool, limit: int) -> list[ReportCase]:
    """按分案顺序找出能发给这位委员的案件，最多 limit 个。

    候选分批往后翻：排在前面的案件全都要回避时，不能因此报「没有案件」。
    """
    now = datetime.now(UTC)
    seen = select(JuryVote.case_id).where(JuryVote.juror_id == juror_id)
    stmt = select(ReportCase.id).where(ReportCase.jury_eligible.is_(True), ReportCase.jury_block_reason.is_(None), ReportCase.id.not_in(seen))
    if golden:
        # 评审中的质控题只出已核对、且不在练习场那一份里的题；这位委员在练习场做过的（当时还没核对）也不出 —— 他看过标准答案。
        practiced = select(JuryPracticeAnswer.case_id).where(JuryPracticeAnswer.user_id == juror_id)
        stmt = stmt.where(*bank_service.bank_clauses(pool="scored"), ReportCase.id.not_in(practiced)).order_by(func.random())
    else:
        stmt = stmt.where(ReportCase.status == "open", ReportCase.queue == "jury").order_by(ReportCase.priority.desc(), ReportCase.id.asc())
    case_ids = list((await db.execute(stmt.limit(PICK_SCAN_LIMIT))).scalars().all())
    if golden and case_ids:
        # 入门考试里见过的题也不出：考完会公布每道题的标准答案。
        examined = await _exam_question_ids(db, juror_id)
        case_ids = [case_id for case_id in case_ids if case_id not in examined]
    context = await _conflict_context(db, juror_id)
    fresh_after = now - timedelta(hours=ASSIGNMENT_TTL_HOURS)
    group_content = await jury_settings.is_enabled(db, "public_group_content") if golden else False
    found: list[ReportCase] = []
    for start in range(0, len(case_ids), PICK_BATCH):
        batch = case_ids[start : start + PICK_BATCH]
        cases = {case.id: case for case in (await db.execute(select(ReportCase).where(ReportCase.id.in_(batch)))).scalars().all()}
        for case_id in batch:
            case = cases.get(case_id)
            if case is None or await _has_conflict(db, juror_id, case, context):
                continue
            if golden:
                # 入库之后情况可能变了（比如后来的举报带了敏感理由、公示开关被打开），发出去之前按入库规则再查一次。
                if await bank_service.block_reason(db, case, group_content=group_content) is not None:
                    continue
            else:
                # 有效票（弃权、上下文不足不占席位）+ 仍在有效期内的未投分配（回避的不算），占满就不再发给新人。
                seats = await db.execute(
                    select(func.count())
                    .select_from(JuryVote)
                    .where(
                        JuryVote.case_id == case.id,
                        JuryVote.stage == "jury",
                        or_(
                            JuryVote.voted_at.is_not(None) & JuryVote.vote.not_in(UNDECIDED_VOTES),
                            JuryVote.voted_at.is_(None) & JuryVote.recused_at.is_(None) & (JuryVote.assigned_at >= fresh_after),
                        ),
                    )
                )
                if int(seats.scalar_one() or 0) >= SEATS_PER_CASE:
                    continue
            found.append(case)
            if len(found) >= limit:
                return found
    return found


async def _exam_question_ids(db: AsyncSession, user_id: int) -> set[int]:
    """这个人在入门考试里见过的题（案件 id）。场次里记了题目列表。"""
    rows = await db.execute(select(JuryExamAttempt.question_ids).where(JuryExamAttempt.user_id == user_id))
    seen: set[int] = set()
    for text in rows.scalars().all():
        try:
            seen.update(int(case_id) for case_id in json.loads(text or "[]"))
        except (TypeError, ValueError):
            continue
    return seen


async def _pick_case(db: AsyncSession, juror_id: int, *, golden: bool) -> ReportCase | None:
    cases = await _assignable_cases(db, juror_id, golden=golden, limit=1)
    return cases[0] if cases else None


async def available_cases(db: AsyncSession, user: User, *, voted_today: int | None = None) -> int:
    """现在能分给这位委员的未结案件数：已经分到手还没投的 + 还能新分到的，最多数到 AVAILABLE_CASES_CAP。"""
    now = datetime.now(UTC)
    if restriction(user, now) is not None:
        return 0
    if voted_today is None:
        voted_today = await _votes_since(db, user.id, now - timedelta(hours=24))
    if voted_today >= DAILY_VOTE_LIMIT:
        return 0
    if _cooldown_until(await get_member(db, user.id), now) is not None:
        return 0
    pending = await db.execute(
        select(func.count())
        .select_from(JuryVote)
        .join(ReportCase, ReportCase.id == JuryVote.case_id)
        .where(
            JuryVote.juror_id == user.id,
            JuryVote.voted_at.is_(None),
            JuryVote.recused_at.is_(None),
            JuryVote.is_quality_check.is_(False),
            JuryVote.stage == "jury",
            JuryVote.assigned_at >= now - timedelta(hours=ASSIGNMENT_TTL_HOURS),
            ReportCase.status == "open",
            ReportCase.queue == "jury",
        )
    )
    count = int(pending.scalar_one() or 0)
    if count < AVAILABLE_CASES_CAP:
        count += len(await _assignable_cases(db, user.id, golden=False, limit=AVAILABLE_CASES_CAP - count))
    return min(count, AVAILABLE_CASES_CAP)


def _cooldown_until(member: JuryMember | None, now: datetime) -> datetime | None:
    """弃权过多被暂停发案到什么时候；没有或已经过了返回 None。"""
    until = penalty_service.aware(member.cooldown_until) if member is not None else None
    return until if until is not None and until > now else None


def _quality_roll() -> float:
    """质控题位置的随机源。单独拿出来是为了测试里能替换掉。"""
    return random.random()


def _wants_quality_check(member: JuryMember) -> bool:
    """这次取件要不要出质控题。

    位置是随机的，委员没法靠数数认出质控题：距上一道至少隔 QUALITY_MIN_GAP 张票后，每次取件按 1 / GOLDEN_EVERY 的概率出；
    隔了超过 QUALITY_MAX_GAP 张还没出就强制出。在质控题上回避过的，下一件强制补一道。
    """
    if member.force_quality:
        return True
    gap = member.votes_since_quality or 0
    if gap > QUALITY_MAX_GAP:
        return True
    return gap >= QUALITY_MIN_GAP and _quality_roll() < 1 / GOLDEN_EVERY


async def next_case(db: AsyncSession, user: User) -> dict[str, Any] | None:
    """取下一个待评审的案件；没有可分配的返回 None。重复调用返回同一个未投的案件。"""
    member = await _require_serving(db, user)
    now = datetime.now(UTC)
    cooldown_until = _cooldown_until(member, now)
    if cooldown_until is not None:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "code": "jury_cooldown",
                "message": "你最近「拿不准」和「上下文不足」投得太多了，先休息一下，稍后再来",
                "until": _iso(cooldown_until),
            },
        )
    if await _votes_since(db, user.id, now - timedelta(hours=24)) >= DAILY_VOTE_LIMIT:
        resets_at = await _daily_resets_at(db, user.id, now)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "code": "jury_daily_limit",
                "message": f"今天的评审数量已达上限（{DAILY_VOTE_LIMIT} 件），明天再来",
                "resets_at": _iso(resets_at) if resets_at is not None else None,
            },
        )
    pending = (
        await db.execute(
            select(JuryVote)
            .where(
                JuryVote.juror_id == user.id,
                JuryVote.voted_at.is_(None),
                JuryVote.recused_at.is_(None),
                JuryVote.stage == "jury",
                JuryVote.assigned_at >= now - timedelta(hours=ASSIGNMENT_TTL_HOURS),
            )
            .order_by(JuryVote.id.asc())
        )
    ).scalars().all()
    for assignment in pending:
        case = await db.get(ReportCase, assignment.case_id)
        if case is not None and (assignment.is_quality_check or (case.status == "open" and case.queue == "jury")):
            return await juror_case_view(db, case, assignment)
    case = None
    quality_check = False
    if _wants_quality_check(member):
        case = await _pick_case(db, user.id, golden=True)
        quality_check = case is not None
    if case is None:
        case = await _pick_case(db, user.id, golden=False)
    if case is None:
        return None
    assignment = JuryVote(case_id=case.id, juror_id=user.id, assigned_at=now, is_quality_check=quality_check)
    db.add(assignment)
    if quality_check:
        member.force_quality = False
    await db.commit()
    return await juror_case_view(db, case, assignment)


def _anonymize_messages(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """快照里的全部上下文（前后最多各 10 条），每条带相对被举报消息的位置。"""
    messages = [item for item in snapshot.get("messages") or [] if isinstance(item, dict)]
    target_index = next((index for index, item in enumerate(messages) if item.get("is_target")), None)
    if target_index is None:
        target = snapshot.get("message")
        messages, target_index = ([target], 0) if isinstance(target, dict) else ([], None)
    if target_index is None:
        return []
    target_sender = messages[target_index].get("sender_id")
    aliases: dict[Any, str] = {target_sender: "被举报人"}
    items: list[dict[str, Any]] = []
    for index, item in enumerate(messages):
        sender_id = item.get("sender_id")
        if sender_id not in aliases:
            aliases[sender_id] = f"成员{chr(ord('A') + (len(aliases) - 1) % 26)}"
        metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        images = [url for url in metadata.get("images", []) if isinstance(url, str)] if isinstance(metadata.get("images"), list) else []
        items.append(
            {
                "speaker": aliases[sender_id],
                "is_target": bool(item.get("is_target")),
                "type": item.get("type") or "text",
                # 已撤回的上下文消息不展示内容；被举报的那条始终展示，否则没法评审。
                "content": item.get("content") if (item.get("is_target") or not item.get("recalled_at")) else None,
                "images": images,
                "created_at": snapshot_time(item.get("created_at")),
                "offset": index - target_index,
            }
        )
    return items


async def juror_case_view(
    db: AsyncSession, case: ReportCase, assignment: JuryVote | None = None, *, with_content: bool = True
) -> dict[str, Any]:
    """委员看到的案件：匿名化的内容和举报理由分布，不含任何身份信息。

    case_id 是这位委员的评审编号（分配记录的 id），不是案件的真实编号 —— 见 docs/jury-v2.md 1.0。
    没有分配记录的调用方（考试、练习、后台题库）只用内容字段，case_id 为 None。
    """
    reasons = await db.execute(select(UserReport.reason, func.count()).where(UserReport.case_id == case.id).group_by(UserReport.reason))
    suggested = case_service.default_category(case)
    view: dict[str, Any] = {
        "case_id": assignment.id if assignment is not None else None,
        "kind": case.target_type,
        # 机器来源的案件没有举报：理由是一项「机器筛查 · 类别」
        "reasons": [{"reason": reason, "count": int(count)} for reason, count in reasons.all()] or case_service.machine_reasons(case),
        "report_count": case.report_count,
        "case_origin": case.case_origin or "report",
        "messages": [],
        "post": None,
        # 投违规票时默认选中的类别：按举报理由映射
        "suggested_category": suggested if suggested in VOTE_CATEGORIES else None,
    }
    if assignment is not None:
        assigned_at = penalty_service.aware(assignment.assigned_at)
        view["assigned_at"] = assigned_at
        view["expires_at"] = assigned_at + timedelta(hours=ASSIGNMENT_TTL_HOURS) if assigned_at else None
    if not with_content:
        return view
    snapshot = await case_service.case_snapshot(db, case)
    if case.target_type == "message":
        view["messages"] = _anonymize_messages(snapshot)
    elif case.target_type == "post":
        post = snapshot.get("post") if isinstance(snapshot.get("post"), dict) else {}
        view["post"] = {
            "content": post.get("content"),
            "image_urls": [url for url in post.get("image_urls") or [] if isinstance(url, str)],
            "created_at": snapshot_time(post.get("created_at")),
        }
    return view


# ---------------------------------------------------------------- 投票与结案


def tally(votes: list[str]) -> tuple[str, int | None] | None:
    """返回 ("violation", 等级) / ("clean", None) / ("escalate", None)；票数还不够返回 None。

    一致性看的是「违规 / 不违规」，等级取违规票的较低中位数：委员对轻重的分歧
    不应该让一个明显违规的案件卡住。abstain / need_context 不计入表决，但算在「投了几张」里。
    """
    votes = [vote for vote in votes if vote != "recuse"]
    decided = [vote for vote in votes if vote in DECIDED_VOTES]
    violation_levels = sorted(_VOTE_LEVEL[vote] for vote in decided if vote in _VOTE_LEVEL)
    clean = len(decided) - len(violation_levels)
    majority = max(len(violation_levels), clean)
    settled = (len(decided) >= 3 and majority == len(decided)) or (len(decided) >= SEATS_PER_CASE and majority >= SEATS_PER_CASE - 1)
    if settled:
        if len(violation_levels) <= clean:
            return "clean", None
        level = violation_levels[(len(violation_levels) - 1) // 2]
        # 严重违规要更多人看过：按现有票会定成 l3 但有效票还不够时先不定案，继续收票。
        # 席位只有 SEATS_PER_CASE 个，所以最多等到坐满；之后按原规则表决。
        if level < 3 or len(decided) >= min(SEVERE_MIN_VOTES, SEATS_PER_CASE):
            return "violation", level
    if len(decided) >= SEATS_PER_CASE or len(votes) >= SEATS_PER_CASE + 2:
        return "escalate", None
    return None


def _vote_agrees(vote: str, upheld: bool) -> bool | None:
    """这一票和结论是否一致；不计入表决的票（弃权、上下文不足）返回 None。"""
    if vote not in DECIDED_VOTES:
        return None
    return (vote in _VOTE_LEVEL) == upheld


async def _after_settled(db: AsyncSession, member: JuryMember) -> None:
    """一张票出了结论之后：准确率过低的暂停资格，否则检查层级；最后看有没有新成就。调用方负责 commit。"""
    was_active = member.status == "active"
    _auto_suspend_if_unreliable(member)
    if was_active and member.status == "suspended":
        points_service.notify(
            db, member.user_id, "suspended", "你的风纪委员资格已被暂停", "评审准确率过低，系统自动暂停了你的资格。如有疑问可以联系管理员。"
        )
        await points_service.sync_badge(db, member)
    else:
        await points_service.check_tier(db, member)
    await points_service.check_achievements(db, member.user_id, member)


def _settle_kind(vote: JuryVote, agreed: bool) -> str:
    """一张票出结论时的加分项：合议票和原审票分值不同。"""
    if vote.stage == "panel":
        return "panel_agreed" if agreed else "panel_disagreed"
    return "vote_agreed" if agreed else "vote_disagreed"


async def _settle_votes(db: AsyncSession, case: ReportCase, upheld: bool) -> None:
    """案件有了结论（无论来自委员共识还是管理员）后，回填每一票是否与结论一致，并结算风纪值。"""
    votes = (
        await db.execute(
            select(JuryVote).where(
                JuryVote.case_id == case.id, JuryVote.voted_at.is_not(None), JuryVote.agreed.is_(None), JuryVote.is_quality_check.is_(False)
            )
        )
    ).scalars().all()
    for vote in votes:
        agreed = _vote_agrees(vote.vote or "abstain", upheld)
        if agreed is None:
            continue
        vote.agreed = agreed
        member = await get_member(db, vote.juror_id)
        if member is None:
            continue
        member.votes_settled += 1
        member.votes_agreed += 1 if agreed else 0
        await points_service.award(db, member, _settle_kind(vote, agreed), case_id=case.id)
        await _after_settled(db, member)
    if votes:
        points_service.reset_leaderboard_cache()


async def resettle_votes(db: AsyncSession, case: ReportCase, upheld: bool) -> None:
    """案件被改判后，按新结论重算每一票是否一致。调用方负责 commit。

    votes_settled 不变，只挪 votes_agreed，风纪值用 overturn_adjust 补差；把这个案件当质控题答过的人挪 golden_correct
    （标准答案被管理员改过的题不跟着案件变）。改判不触发自动停职：那是管理员纠错，不是委员又投了一票。
    """
    votes = (await db.execute(select(JuryVote).where(JuryVote.case_id == case.id, JuryVote.voted_at.is_not(None)))).scalars().all()
    for vote in votes:
        agreed = _vote_agrees(vote.vote or "abstain", bank_service.question_upheld(case) if vote.is_quality_check else upheld)
        if agreed is None or vote.agreed == agreed:
            continue
        previous = vote.agreed
        vote.agreed = agreed
        member = await get_member(db, vote.juror_id)
        if member is None:
            continue
        delta = (1 if agreed else 0) - (1 if previous else 0)
        if vote.is_quality_check:
            member.golden_total += 1 if previous is None else 0
            member.golden_correct = max(0, member.golden_correct + delta)
        elif previous is None:
            # 案件结案后才投、还没来得及计入的票，在这里补上。
            member.votes_settled += 1
            member.votes_agreed = max(0, member.votes_agreed + delta)
            await points_service.award(db, member, _settle_kind(vote, agreed), case_id=case.id)
        else:
            member.votes_agreed = max(0, member.votes_agreed + delta)
            adjust = points_service.POINTS["overturn_adjust"]
            # 一致变不一致扣回差额，反之补上；同一个案件可能被改判不止一次，所以不去重。
            await points_service.award(db, member, "overturn_adjust", points=adjust if agreed else -adjust, case_id=case.id, unique=False)
    points_service.reset_leaderboard_cache()


def _auto_suspend_if_unreliable(member: JuryMember) -> None:
    if member.status != "active":
        return
    low_agreement = member.votes_settled >= MIN_SETTLED_FOR_REVIEW and member.votes_agreed / member.votes_settled < MIN_ACCURACY
    low_golden = member.golden_total >= MIN_GOLDEN_FOR_REVIEW and member.golden_correct / member.golden_total < MIN_ACCURACY
    if low_agreement or low_golden:
        member.status = "suspended"
        member.review_note = "评审准确率过低，系统自动暂停资格"
        member.reviewed_at = datetime.now(UTC)


async def _next_alias(db: AsyncSession, case_id: int) -> str:
    """按这个案件已经发出去的代号个数依次分配：委员 A、委员 B……"""
    used = await db.execute(select(func.count()).select_from(JuryVote).where(JuryVote.case_id == case_id, JuryVote.alias.is_not(None)))
    index = int(used.scalar_one() or 0)
    return f"委员 {chr(ord('A') + index % 26)}{index // 26 + 1 if index >= 26 else ''}"


async def _vote_result(
    db: AsyncSession,
    user: User,
    assignment: JuryVote,
    *,
    settled: bool,
    member: JuryMember,
    rewards: list[Any],
    opinion: JuryOpinion | None = None,
) -> dict[str, Any]:
    await points_service.flush_events(db)
    if opinion is not None:
        await notify_case_voters(db, assignment.case_id, "jury.opinion.created", {"opinion_id": opinion.id, "parent_id": None}, exclude=user.id)
    return {
        # 评审编号，和请求里的一样
        "case_id": assignment.id,
        "settled": settled,
        "case": await case_detail(db, user.id, assignment.id),
        # 只含当场结算的部分；案件定案时的加分走 jury.points.changed 事件
        "reward": points_service.reward_view(member, rewards),
    }


def _vote_category(vote: str, category: str | None) -> str | None:
    """违规票可以带类别；别的票带了也不记。"""
    if vote not in _VOTE_LEVEL or not category:
        return None
    if category not in VOTE_CATEGORIES:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail={"code": "invalid_category", "message": "未知的违规类别"})
    return category


def _verdict_category(case: ReportCase, rows: list[tuple[str | None, str | None]]) -> str:
    """定案类别：带了类别的违规票里的多数；平票或都没带时退回按举报理由映射的默认值。"""
    default = case_service.default_category(case)
    ranked = Counter(category for vote, category in rows if vote in _VOTE_LEVEL and category).most_common(2)
    if not ranked or (len(ranked) > 1 and ranked[0][1] == ranked[1][1]):
        return default
    return ranked[0][0]


async def _daily_rewards(db: AsyncSession, member: JuryMember, now: datetime) -> list[Any]:
    """每天第一张计入表决的票、每连满 7 天各给一次分。「天」是 UTC+8 的自然日。"""
    day = points_service.local_day(now)
    logs = [await points_service.award(db, member, "daily_first", ref_id=day.toordinal(), announce=False)]
    if points_service.touch_streak(member, day):
        logs.append(await points_service.award(db, member, "streak_7", ref_id=day.toordinal(), announce=False))
    return logs


async def _apply_abstain_cooldown(db: AsyncSession, member: JuryMember, now: datetime) -> None:
    """刚投了「拿不准 / 上下文不足」之后调用：近期比例过高就暂停发案一段时间。"""
    count, rate = await _abstain_state(db, member.user_id)
    if count >= ABSTAIN_MIN_VOTES and rate > ABSTAIN_COOLDOWN_RATE:
        member.cooldown_until = now + timedelta(hours=ABSTAIN_COOLDOWN_HOURS)


async def _recuse(db: AsyncSession, user: User, member: JuryMember, assignment: JuryVote, note: str | None, now: datetime) -> dict[str, Any]:
    """主动回避：这次分配作废，案件改派给别人。不算投票，之后也看不了这个案件。"""
    if await _recuses_since(db, user.id, now - timedelta(hours=24)) >= RECUSE_DAILY_LIMIT:
        resets_at = await _recuse_resets_at(db, user.id, now)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "code": "recuse_limit",
                "message": f"今天的回避次数已用完（每天 {RECUSE_DAILY_LIMIT} 次）",
                "resets_at": _iso(resets_at) if resets_at is not None else None,
            },
        )
    assignment.vote = "recuse"
    assignment.note = note
    assignment.recused_at = now
    if assignment.is_quality_check:
        # 质控题不能靠回避躲掉：下一件补一道。
        member.force_quality = True
    await db.commit()
    return {"case_id": assignment.id, "settled": False, "case": None, "reward": points_service.reward_view(member, [])}


async def cast_vote(db: AsyncSession, user: User, review_id: int, vote: str, note: str | None, category: str | None = None) -> dict[str, Any]:
    """投票。review_id 是评审编号（GET /jury/cases/next 给的 case_id），不是案件的真实编号。"""
    if vote not in VOTES:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Unknown vote")
    category = _vote_category(vote, category)
    member = await _require_serving(db, user)
    # 老接口的错误是字符串，保持原样
    case, assignment = await resolve_review(db, user.id, review_id, detail="这个案件没有分配给你")
    if assignment.stage == "panel":
        # 资深合议的分配走 POST /jury/panel/{case_id}/vote，规则不一样
        raise _review_not_found("这个案件没有分配给你")
    if assignment.voted_at is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="你已经投过票了")
    if assignment.recused_at is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="你已经回避了这个案件")
    case_id = case.id
    now = datetime.now(UTC)
    text = note.strip()[:500] if note else None
    if vote == "recuse":
        return await _recuse(db, user, member, assignment, text or None, now)
    assignment.vote = vote
    assignment.note = text
    assignment.category = category
    assignment.voted_at = now
    member.votes_total += 1
    # 当场结算、随投票响应返回的加分。弃权和上下文不足不给分。
    rewards: list[Any] = await _daily_rewards(db, member, now) if vote in DECIDED_VOTES else []

    if assignment.is_quality_check:
        # 质控题：当场对答案，不分配代号、不开观点区，备注只留给管理员看。
        member.votes_since_quality = 0
        agreed = _vote_agrees(vote, bank_service.question_upheld(case))
        if agreed is not None:
            assignment.agreed = agreed
            member.golden_total += 1
            member.golden_correct += 1 if agreed else 0
            rewards.append(await points_service.award(db, member, "quality_correct" if agreed else "quality_wrong", case_id=case_id, announce=False))
            await _after_settled(db, member)
        else:
            await _apply_abstain_cooldown(db, member, now)
            await points_service.check_achievements(db, user.id, member)
        await db.commit()
        points_service.reset_leaderboard_cache()
        return await _vote_result(db, user, assignment, settled=False, member=member, rewards=rewards)

    member.votes_since_quality = (member.votes_since_quality or 0) + 1
    assignment.alias = await _next_alias(db, case_id)
    opinion: JuryOpinion | None = None
    if assignment.note:
        # 投票时写的备注就是这位委员在观点区的第一条观点。
        opinion = JuryOpinion(case_id=case_id, author_id=user.id, content=assignment.note, is_note=True, created_at=now)
        db.add(opinion)
    await db.flush()
    if vote in UNDECIDED_VOTES:
        await _apply_abstain_cooldown(db, member, now)
    await points_service.check_achievements(db, user.id, member)
    if rewards:
        points_service.reset_leaderboard_cache()

    if case.status != "open" or case.queue != "jury":
        # 投票期间案件已被管理员处理：这一票照常记，按已有结论计入一致率。
        if has_verdict(case):
            await _settle_votes(db, case, case.status == "resolved")
        await db.commit()
        return await _vote_result(db, user, assignment, settled=False, member=member, rewards=rewards, opinion=opinion)

    rows = [
        (item_vote, item_category)
        for item_vote, item_category in (
            await db.execute(
                select(JuryVote.vote, JuryVote.category).where(
                    JuryVote.case_id == case_id, JuryVote.voted_at.is_not(None), JuryVote.is_quality_check.is_(False), JuryVote.stage == "jury"
                )
            )
        ).all()
        if item_vote
    ]
    votes = [item_vote for item_vote, _category in rows]
    # 「上下文不足」攒够了就转出，不用等别的票。
    escalation = "need_context" if vote == "need_context" and votes.count("need_context") >= NEED_CONTEXT_VOTES else None
    result = None if escalation else tally(votes)
    if escalation is None and result is None:
        await db.commit()
        return await _vote_result(db, user, assignment, settled=False, member=member, rewards=rewards, opinion=opinion)
    if escalation is not None or result[0] == "escalate":
        await case_service.escalate_from_jury(db, case, now, escalation or "no_consensus")
        await db.commit()
        await case_service.notify_escalated(db, case)
        return await _vote_result(db, user, assignment, settled=False, member=member, rewards=rewards, opinion=opinion)
    outcome, level = result
    upheld = outcome == "violation"
    await _settle_votes(db, case, upheld)
    await case_service.resolve_case(
        db,
        case_id,
        source="jury",
        actor_id=None,
        upheld=upheld,
        category=_verdict_category(case, rows) if upheld else None,
        level=level,
        note="风纪委员评审认定违规" if upheld else "风纪委员评审认定不违规",
    )
    return await _vote_result(db, user, assignment, settled=True, member=member, rewards=rewards, opinion=opinion)


def has_verdict(case: ReportCase) -> bool:
    """案件有没有真正的结论。机器来源的案件没人能定案时是以「未作处理」结的（auto），那不是结论，票不计一致率。"""
    return case.status != "open" and case.verdict_source != "auto"


async def settle_after_admin(db: AsyncSession, case: ReportCase) -> None:
    """管理员直接结案后调用，让已经投过票的委员也能计入一致率。调用方负责 commit。"""
    if case.status != "open":
        await _settle_votes(db, case, case.status == "resolved")


# ---------------------------------------------------------------- 投后视图


def _not_voted() -> HTTPException:
    return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail={"code": "not_voted", "message": "投票之后才能查看这个案件"})


def _review_not_found(detail: Any = None) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail or {"code": "case_not_found", "message": "案件不存在"})


async def resolve_review(db: AsyncSession, user_id: int, review_id: int, *, detail: Any = None) -> tuple[ReportCase, JuryVote]:
    """评审编号 -> (案件, 本人的那条分配记录)。docs/jury-v2.md 1.0。

    委员侧接口的路径参数都走这里：只认「这条 jury_votes 行存在、并且是本人的」，其余一律 404。
    不回退去按案件的真实编号查 —— 那样质控题（早就结案的旧案件）就能从编号大小、或者去公示栏查一下认出来。
    """
    assignment = await db.get(JuryVote, review_id)
    if assignment is None or assignment.juror_id != user_id:
        raise _review_not_found(detail)
    case = await db.get(ReportCase, assignment.case_id)
    if case is None:
        raise _review_not_found(detail)
    return case, assignment


async def my_vote(db: AsyncSession, user_id: int, case_id: int) -> JuryVote | None:
    """这个人对这个案件投出的票；只分到手还没投的不算。

    case_id 是案件的真实编号，只给服务端内部用（比如从一条观点找回它所在的案件）；用户传进来的编号走 resolve_review。
    """
    result = await db.execute(select(JuryVote).where(JuryVote.case_id == case_id, JuryVote.juror_id == user_id, JuryVote.voted_at.is_not(None)))
    return result.scalar_one_or_none()


async def has_recused(db: AsyncSession, user_id: int, case_id: int) -> bool:
    """这个人是不是主动回避过这个案件。"""
    result = await db.execute(
        select(JuryVote.id).where(JuryVote.case_id == case_id, JuryVote.juror_id == user_id, JuryVote.recused_at.is_not(None)).limit(1)
    )
    return result.scalar_one_or_none() is not None


async def require_voted(db: AsyncSession, user_id: int, review_id: int) -> tuple[ReportCase, JuryVote]:
    """按评审编号取案件，并要求已经投过票：分到手还没投的、主动回避的都是 403 not_voted。"""
    case, vote = await resolve_review(db, user_id, review_id)
    if vote.voted_at is None:
        raise _not_voted()
    return case, vote


def case_stage(case: ReportCase) -> str:
    """jury 委员评审中 / panel 资深合议中 / admin 等管理员 / closed 已定案。"""
    if case.status != "open":
        return "closed"
    return case.queue if case.queue in {"jury", "panel"} else "admin"


def case_hours(case: ReportCase) -> float | None:
    """举报到定案用了多少小时。"""
    created_at, closed_at = penalty_service.aware(case.created_at), penalty_service.aware(case.closed_at)
    if created_at is None or closed_at is None:
        return None
    return round(max(0.0, (closed_at - created_at).total_seconds()) / 3600, 1)


def verdict_view(case: ReportCase) -> dict[str, Any]:
    upheld = case.status == "resolved"
    category = case.verdict_category if upheld else None
    return {
        "outcome": "violation" if upheld else "clean",
        "source": case.verdict_source,
        "category": category,
        "category_label": penalty_service.category_label(category) if category else None,
        "level": case.verdict_level if upheld else None,
        "closed_at": penalty_service.aware(case.closed_at),
        "hours": case_hours(case),
        "overturned": case.overturned_at is not None,
        "overturn_note": case.overturn_note,
        # 题库里这道题的讲解：只在质控题的详情里给，见 case_detail
        "explanation": None,
    }


def quality_verdict_view(case: ReportCase) -> dict[str, Any]:
    """质控题揭晓的是题库里的标准答案（管理员可能改过），连同讲解。"""
    answer = bank_service.question_answer(case)
    return {
        **verdict_view(case),
        "outcome": answer["outcome"],
        "category": answer["category"],
        "category_label": answer["category_label"],
        "level": answer["level"],
        "explanation": answer["explanation"],
    }


async def vote_tallies(db: AsyncSession, case_ids: list[int]) -> dict[int, dict[str, int]]:
    """每个案件原审各选项的票数。把案件当质控题答的票不算；回避不是票，不在里面；资深合议的票另算，见 panel_tallies。"""
    tallies = {case_id: {"none": 0, "l1": 0, "l2": 0, "l3": 0, "abstain": 0, "need_context": 0} for case_id in case_ids}
    if not case_ids:
        return tallies
    rows = await db.execute(
        select(JuryVote.case_id, JuryVote.vote, func.count())
        .where(JuryVote.case_id.in_(case_ids), JuryVote.voted_at.is_not(None), JuryVote.is_quality_check.is_(False), JuryVote.stage == "jury")
        .group_by(JuryVote.case_id, JuryVote.vote)
    )
    for case_id, vote, count in rows.all():
        if vote in tallies[case_id]:
            tallies[case_id][vote] = int(count)
    return tallies


async def panel_tallies(db: AsyncSession, case_ids: list[int]) -> dict[int, dict[str, int]]:
    """每个案件资深合议各选项的票数（合议不能弃权，只有四个选项）。"""
    tallies = {case_id: {vote: 0 for vote in DECIDED_VOTES} for case_id in case_ids}
    if not case_ids:
        return tallies
    rows = await db.execute(
        select(JuryVote.case_id, JuryVote.vote, func.count())
        .where(JuryVote.case_id.in_(case_ids), JuryVote.voted_at.is_not(None), JuryVote.stage == "panel")
        .group_by(JuryVote.case_id, JuryVote.vote)
    )
    for case_id, vote, count in rows.all():
        if vote in tallies[case_id]:
            tallies[case_id][vote] = int(count)
    return tallies


def effective_votes(counts: dict[str, int]) -> int:
    """有效票数：不含弃权和上下文不足。"""
    return sum(count for vote, count in counts.items() if vote not in UNDECIDED_VOTES)


async def opinion_counts(db: AsyncSession, case_ids: list[int]) -> dict[int, int]:
    """每个案件观点区里还在的观点数（含回复、含被折叠的，不含已删除的）。"""
    if not case_ids:
        return {}
    rows = await db.execute(
        select(JuryOpinion.case_id, func.count()).where(JuryOpinion.case_id.in_(case_ids), JuryOpinion.state != "deleted").group_by(JuryOpinion.case_id)
    )
    return {case_id: int(count) for case_id, count in rows.all()}


async def case_detail(db: AsyncSession, user_id: int, review_id: int) -> dict[str, Any]:
    """投票之后看到的案件：自己的票、进度，定案后才有票数分布和结论。review_id 是评审编号。"""
    from app.services import jury_precedents as precedent_service

    case, vote = await require_voted(db, user_id, review_id)
    closed = case.status != "open"
    quality_check = vote.is_quality_check
    available = True
    if closed:
        # 保留期从定案算起；定案之后才投的（质控题都是）从投票算起，否则刚投完就看不到内容。
        since = max(moment for moment in (penalty_service.aware(case.closed_at), penalty_service.aware(vote.voted_at)) if moment is not None)
        available = datetime.now(UTC) < since + timedelta(days=CONTENT_RETENTION_DAYS)
    counts = (await vote_tallies(db, [case.id]))[case.id]
    tally: dict[str, Any] | None = None
    if closed and not quality_check:
        # 进过资深合议的案件多一项 panel；原审的分布保持原样
        tally = {**counts, "panel": (await panel_tallies(db, [case.id]))[case.id]} if case.panel_at is not None else counts
    return {
        **await juror_case_view(db, case, with_content=available),
        "case_id": vote.id,
        "content_available": available,
        "my_vote": {
            "vote": vote.vote,
            "note": vote.note,
            "voted_at": penalty_service.aware(vote.voted_at),
            "alias": vote.alias,
            # 未定案或弃权时为 null；改判后会跟着变
            "agreed": vote.agreed if closed else None,
            "category": vote.category,
            # 这个案件累计给了我多少风纪值
            "points": await points_service.case_points(db, user_id, case.id),
        },
        "stage": case_stage(case),
        # 定案前只给有效票数，不含弃权：避免从数字变化反推别人投了什么。质控题不透露真实案件的票。
        "votes_cast": 0 if quality_check else effective_votes(counts),
        "tally": tally,
        "verdict": (quality_verdict_view(case) if quality_check else verdict_view(case)) if closed else None,
        "is_quality_check": quality_check,
        "opinions_enabled": not quality_check,
        "opinion_count": 0 if quality_check else (await opinion_counts(db, [case.id])).get(case.id, 0),
        # 案件此刻在公示栏里时是它的公开编号；质控题恒为 null，否则等于把真实编号还了回去
        "public_case_id": case.id if not quality_check and await case_service.published_case_ids(db, [case.id]) else None,
        # 三期：precedent（这个案件的判例）和 can_mark_precedent
        **await precedent_service.case_fields(db, user_id, case, vote),
    }


async def history(db: AsyncSession, juror_id: int, limit: int = 50) -> list[dict[str, Any]]:
    rows = await db.execute(
        select(JuryVote, ReportCase)
        .join(ReportCase, ReportCase.id == JuryVote.case_id)
        .where(JuryVote.juror_id == juror_id, JuryVote.voted_at.is_not(None))
        .order_by(JuryVote.voted_at.desc())
        .limit(limit)
    )
    items: list[dict[str, Any]] = []
    for vote, case in rows.all():
        items.append(
            {
                # 评审编号
                "case_id": vote.id,
                "kind": case.target_type,
                "vote": vote.vote,
                "voted_at": penalty_service.aware(vote.voted_at),
                # 案件未结 / 弃权时为 null
                "agreed": vote.agreed,
                "outcome": None if case.status == "open" else ("violation" if case.status == "resolved" else "clean"),
            }
        )
    return items


async def list_votes(
    db: AsyncSession,
    juror_id: int,
    *,
    limit: int = 20,
    offset: int = 0,
    state: str | None = None,
    outcome: str | None = None,
    agreed: bool | None = None,
    votes: list[str] | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """我的评审：自己投过的票和主动回避的记录，按时间倒序。"""
    # 回避的记录没有 voted_at，用回避时间排
    moment = func.coalesce(JuryVote.voted_at, JuryVote.recused_at)
    clauses = [JuryVote.juror_id == juror_id, moment.is_not(None)]
    if state == "open":
        clauses.append(ReportCase.status == "open")
    elif state == "closed":
        clauses.append(ReportCase.status != "open")
    if outcome is not None:
        clauses.append(ReportCase.status == ("resolved" if outcome == "violation" else "dismissed"))
    if agreed is not None:
        clauses.append(JuryVote.agreed.is_(agreed))
    if votes:
        clauses.append(JuryVote.vote.in_(votes))
    total = int(
        (
            await db.execute(select(func.count(JuryVote.id)).join(ReportCase, ReportCase.id == JuryVote.case_id).where(*clauses))
        ).scalar_one()
        or 0
    )
    rows = (
        await db.execute(
            select(JuryVote, ReportCase)
            .join(ReportCase, ReportCase.id == JuryVote.case_id)
            .where(*clauses)
            .order_by(moment.desc(), JuryVote.id.desc())
            .offset(offset)
            .limit(limit)
        )
    ).all()
    viewable = [case.id for vote, case in rows if not vote.is_quality_check and vote.recused_at is None]
    opinions = await opinion_counts(db, viewable)
    published = await case_service.published_case_ids(db, [case.id for vote, case in rows if case.publishable and case.id in viewable])
    items: list[dict[str, Any]] = []
    for vote, case in rows:
        recused = vote.recused_at is not None
        # 回避的人看不了这个案件，结论也不在列表里透露。
        closed = case.status != "open" and not recused
        items.append(
            {
                # 评审编号
                "case_id": vote.id,
                "kind": case.target_type,
                "primary_reason": case.primary_reason,
                "vote": vote.vote,
                "voted_at": penalty_service.aware(vote.voted_at or vote.recused_at),
                "alias": vote.alias,
                "stage": case_stage(case),
                # 这一票是哪一轮投的；stage 是案件现在所处的阶段
                "vote_stage": vote.stage or "jury",
                "outcome": ("violation" if case.status == "resolved" else "clean") if closed else None,
                "verdict_source": case.verdict_source if closed else None,
                "verdict_level": case.verdict_level if closed and case.status == "resolved" else None,
                "closed_at": penalty_service.aware(case.closed_at) if not recused else None,
                "agreed": vote.agreed if closed else None,
                "overturned": case.overturned_at is not None and not recused,
                "is_quality_check": vote.is_quality_check,
                "opinion_count": 0 if vote.is_quality_check or recused else opinions.get(case.id, 0),
                # 回避的记录点不进详情
                "can_view": not recused,
                # 此刻在公示栏里的案件才有；质控题和回避的记录恒为 null
                "public_case_id": case.id if case.id in published and not vote.is_quality_check and not recused else None,
            }
        )
    return items, total


async def my_stats(db: AsyncSession, user_id: int) -> dict[str, Any]:
    """个人统计。从没当过委员的人全是 0。"""
    member = await get_member(db, user_id)
    now = datetime.now(UTC)
    today = now.astimezone(LOCAL_TZ).date()
    first_day = today - timedelta(days=TREND_DAYS - 1)
    trend = {first_day + timedelta(days=index): {"votes": 0, "settled": 0, "agreed": 0} for index in range(TREND_DAYS)}
    since = datetime(first_day.year, first_day.month, first_day.day, tzinfo=LOCAL_TZ).astimezone(UTC)
    rows = await db.execute(
        select(JuryVote.voted_at, JuryVote.agreed, JuryVote.is_quality_check).where(JuryVote.juror_id == user_id, JuryVote.voted_at >= since)
    )
    for voted_at, agreed, quality_check in rows.all():
        day = trend.get(penalty_service.aware(voted_at).astimezone(LOCAL_TZ).date())
        if day is None:
            continue
        day["votes"] += 1
        # settled / agreed 与一致率同口径：质控题另算，不在这里。
        if agreed is not None and not quality_check:
            day["settled"] += 1
            day["agreed"] += 1 if agreed else 0
    abstain_total = int(
        (
            await db.execute(
                select(func.count()).select_from(JuryVote).where(JuryVote.juror_id == user_id, JuryVote.voted_at.is_not(None), JuryVote.vote == "abstain")
            )
        ).scalar_one()
        or 0
    )
    opinions_total, likes_received = (
        await db.execute(
            select(func.count(), func.coalesce(func.sum(JuryOpinion.like_count), 0)).where(JuryOpinion.author_id == user_id, JuryOpinion.state != "deleted")
        )
    ).one()
    votes_total = member.votes_total if member else 0
    votes_settled = member.votes_settled if member else 0
    votes_agreed = member.votes_agreed if member else 0
    golden_total = member.golden_total if member else 0
    golden_correct = member.golden_correct if member else 0
    return {
        "votes_total": votes_total,
        "votes_settled": votes_settled,
        "votes_agreed": votes_agreed,
        "agreement_rate": round(votes_agreed / votes_settled, 3) if votes_settled else 0,
        "golden_total": golden_total,
        "golden_correct": golden_correct,
        "golden_rate": round(golden_correct / golden_total, 3) if golden_total else 0,
        "abstain_total": abstain_total,
        "abstain_rate": round(abstain_total / votes_total, 3) if votes_total else 0,
        "opinions_total": int(opinions_total or 0),
        "likes_received": int(likes_received or 0),
        "trend": [{"date": day.isoformat(), **values} for day, values in trend.items()],
    }


async def list_members(
    db: AsyncSession, status_filter: str | None, limit: int, offset: int, user_id: int | None = None
) -> tuple[list[dict[str, Any]], int]:
    """后台的成员列表。status 和本人在 /jury/me 看到的一致：任期届满没能续任的是 expired，不是 active。"""
    # 先把届满的处理掉（条件满足的自动续任），和本人打开 /jury/me 时做的是同一件事。
    await renew_expired_terms(db)
    now = datetime.now(UTC)
    expired = (JuryMember.status == "active") & JuryMember.term_ends_at.is_not(None) & (JuryMember.term_ends_at <= now)
    clauses: list[Any] = []
    if status_filter == "expired":
        clauses.append(expired)
    elif status_filter == "active":
        clauses.extend([JuryMember.status == "active", ~expired])
    elif status_filter:
        clauses.append(JuryMember.status == status_filter)
    if user_id is not None:
        clauses.append(JuryMember.user_id == user_id)
    total = int((await db.execute(select(func.count(JuryMember.id)).where(*clauses))).scalar_one() or 0)
    rows = await db.execute(
        select(JuryMember, User)
        .join(User, User.id == JuryMember.user_id)
        .where(*clauses)
        .order_by(JuryMember.applied_at.desc(), JuryMember.id.desc())
        .offset(offset)
        .limit(limit)
    )
    members = rows.all()
    user_ids = [member.user_id for member, _user in members]
    undecided: dict[int, int] = {}
    recused: dict[int, int] = {}
    exams: dict[int, dict[str, Any]] = {}
    if user_ids:
        undecided = dict(
            (
                await db.execute(
                    select(JuryVote.juror_id, func.count())
                    .where(JuryVote.juror_id.in_(user_ids), JuryVote.voted_at.is_not(None), JuryVote.vote.in_(UNDECIDED_VOTES))
                    .group_by(JuryVote.juror_id)
                )
            ).all()
        )
        recused = dict(
            (
                await db.execute(
                    select(JuryVote.juror_id, func.count()).where(JuryVote.juror_id.in_(user_ids), JuryVote.recused_at.is_not(None)).group_by(JuryVote.juror_id)
                )
            ).all()
        )
        attempts = await db.execute(
            select(JuryExamAttempt)
            .where(JuryExamAttempt.user_id.in_(user_ids), JuryExamAttempt.submitted_at.is_not(None))
            .order_by(JuryExamAttempt.id.asc())
        )
        for attempt in attempts.scalars().all():
            # 成绩取最近一场；通过时间取最近一次通过的
            exam = exams.setdefault(attempt.user_id, {"score": None, "passed_at": None})
            exam["score"] = attempt.score or 0
            if attempt.passed:
                exam["passed_at"] = penalty_service.aware(attempt.submitted_at)
    items: list[dict[str, Any]] = []
    for member, user in members:
        votes_total = member.votes_total or 0
        recuses = int(recused.get(member.user_id, 0))
        exam = exams.get(member.user_id, {})
        items.append(
            {
                "user_id": member.user_id,
                "user": user,
                "status": member_state(member, now),
                "statement": member.statement,
                "applied_at": penalty_service.aware(member.applied_at),
                "reviewed_at": penalty_service.aware(member.reviewed_at),
                "review_note": member.review_note,
                "term_ends_at": penalty_service.aware(member.term_ends_at),
                "tier": member.tier,
                **member_stats(member),
                "points": member.points or 0,
                # 没考过试的为 null（后台显示「未考试」）
                "exam_score": exam.get("score"),
                "exam_passed_at": exam.get("passed_at"),
                "abstain_rate": round(int(undecided.get(member.user_id, 0)) / votes_total, 3) if votes_total else 0,
                "recuse_rate": round(recuses / (votes_total + recuses), 3) if votes_total + recuses else 0,
                "badge_visible": bool(member.badge_visible),
                "days_served": points_service.days_served(member, now),
            }
        )
    return items, total
