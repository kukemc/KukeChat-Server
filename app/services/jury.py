"""风纪委员：社区成员评审被举报的群聊消息和公开内容。

委员只看得到案件快照里匿名化后的内容，看不到举报人和被举报人是谁；
他们的结论和机器一样受处罚上限约束，拿不准或没有共识的案件交回管理员。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.case import JuryMember, JuryOpinion, JuryVote, ReportCase
from app.models.conversation import Conversation, ConversationMember
from app.models.enums import ConversationType, UserStatus
from app.models.friend import Friendship
from app.models.message import Message
from app.models.report import AdminAuditLog, UserReport
from app.models.user import User
from app.services import cases as case_service
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
# 每 GOLDEN_EVERY 个案件里混入一道已有结论的质控题
GOLDEN_EVERY = settings.jury_golden_every
MIN_SETTLED_FOR_REVIEW = settings.jury_min_settled_for_review
MIN_GOLDEN_FOR_REVIEW = settings.jury_min_golden_for_review
MIN_ACCURACY = settings.jury_min_accuracy
# 公开群成员数达到这个值后不再按群成员回避
LARGE_GROUP_MEMBERS = settings.jury_large_group_members
CONTENT_RETENTION_DAYS = settings.jury_content_retention_days
TREND_DAYS = 30

VOTES = {"none", "l1", "l2", "l3", "abstain"}
_VOTE_LEVEL = {"l1": 1, "l2": 2, "l3": 3}
TIERS: tuple[tuple[str, str], ...] = (("trainee", "见习委员"), ("regular", "正式委员"), ("senior", "资深委员"))
TIER_KEYS = {key for key, _label in TIERS}
# 分案时一批看多少个候选、最多往后翻多少个
PICK_BATCH = 30
PICK_SCAN_LIMIT = 300
AVAILABLE_CASES_CAP = 50


# ---------------------------------------------------------------- 资格


async def eligibility(db: AsyncSession, user: User) -> list[dict[str, Any]]:
    now = datetime.now(UTC)
    created_at = penalty_service.aware(user.created_at) or now
    age_days = (now - created_at).days
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
    recent_messages = int(
        (
            await db.execute(
                select(func.count())
                .select_from(Message)
                .where(Message.sender_id == user.id, Message.created_at >= now - timedelta(days=RECENT_ACTIVITY_DAYS))
            )
        ).scalar_one()
        or 0
    )
    return [
        {"key": "account_age", "ok": age_days >= MIN_ACCOUNT_AGE_DAYS, "text": f"注册满 {MIN_ACCOUNT_AGE_DAYS} 天", "detail": f"已注册 {age_days} 天"},
        {"key": "ccw_bound", "ok": bool(user.ccw_student_oid), "text": "已绑定 CCW 账号", "detail": "已绑定" if user.ccw_student_oid else "未绑定"},
        {
            "key": "clean_record",
            "ok": recent_violations == 0 and points == 0 and user.status == UserStatus.ACTIVE,
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


async def my_status(db: AsyncSession, user: User) -> dict[str, Any]:
    now = datetime.now(UTC)
    member = await get_member(db, user.id)
    requirements = await eligibility(db, user)
    term_ends_at = penalty_service.aware(member.term_ends_at) if member else None
    serving = _is_serving(member, now)
    voted_today = 0
    if member is not None:
        voted_today = await _votes_since(db, user.id, now - timedelta(hours=24))
    return {
        "status": member_state(member, now),
        "eligible": all(item["ok"] for item in requirements),
        "requirements": requirements,
        "term_ends_at": term_ends_at,
        "review_note": member.review_note if member and member.status in {"rejected", "suspended"} else None,
        "stats": member_stats(member) if member else None,
        "daily_limit": DAILY_VOTE_LIMIT,
        "voted_today": voted_today,
        "tier": member.tier if member is not None and serving else None,
        "available_cases": await available_cases(db, user, voted_today=voted_today) if serving else None,
        # 有值时评审、发观点、点赞都会被拒绝（juror_restricted）；不是在任委员恒为 null
        "restriction": restriction(user, now) if serving else None,
    }


async def apply(db: AsyncSession, user: User, statement: str | None) -> JuryMember:
    requirements = await eligibility(db, user)
    failed = [item["text"] for item in requirements if not item["ok"]]
    if failed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "jury_not_eligible", "message": "暂不满足申请条件：" + "、".join(failed)},
        )
    now = datetime.now(UTC)
    member = await get_member(db, user.id)
    if member is None:
        member = JuryMember(user_id=user.id, applied_at=now)
        db.add(member)
    elif member.status == "applied":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="申请正在审核中")
    elif member.status == "suspended":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail={"code": "jury_suspended", "message": "你的风纪委员资格已被暂停"})
    elif _is_serving(member, now):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="你已经是风纪委员")
    elif member.status == "active":
        # 任期届满后续任：之前已通过审批且没有被停职，条件仍满足就直接续期。
        member.term_ends_at = now + timedelta(days=TERM_DAYS)
        await db.commit()
        return member
    member.status = "applied"
    member.statement = statement.strip()[:500] if statement else None
    member.applied_at = now
    member.review_note = None
    await db.commit()
    return member


async def review_member(
    db: AsyncSession, user_id: int, *, action: str, actor_id: int, note: str | None, tier: str | None = None
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
    if action == "set_tier":
        # 只调层级：不动资格状态，也不覆盖之前写给本人看的审核说明。
        if tier is None:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail={"code": "invalid_tier", "message": "请指定委员层级"})
        member.tier = tier
    else:
        if action in {"approve", "reinstate"}:
            member.status = "active"
            member.term_ends_at = now + timedelta(days=TERM_DAYS)
            # 创始任命：可以直接任命为任意层级。
            if tier is not None:
                member.tier = tier
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
    details = text if tier is None else "；".join(part for part in (f"tier={tier}", text) if part)
    db.add(AdminAuditLog(actor_id=actor_id, action=f"jury.member.{action}", target_type="user", target_id=user_id, details=details))
    await db.commit()
    await _notify_member(user_id)
    return member


async def _notify_member(user_id: int) -> None:
    from app.websocket.manager import admin_manager, manager

    try:
        await manager.send_to_user(user_id, "jury.status.changed", {})
        await admin_manager.broadcast("admin.jury.changed", {"user_id": user_id})
    except Exception:
        logger.exception("Failed to notify jury member", extra={"user_id": user_id})


async def notify_case_voters(db: AsyncSession, case_id: int, event_type: str, data: dict[str, Any], *, exclude: int | None = None) -> None:
    """给这个案件投过票的委员发事件（质控题的作答者不算）。发不出去只记日志，不影响主流程。"""
    from app.websocket.manager import manager

    try:
        juror_ids = set(
            (
                await db.execute(
                    select(JuryVote.juror_id).where(JuryVote.case_id == case_id, JuryVote.voted_at.is_not(None), JuryVote.is_quality_check.is_(False))
                )
            ).scalars().all()
        )
        juror_ids.discard(exclude)
        if juror_ids:
            await manager.send_to_users(juror_ids, event_type, data)
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
    }


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
    snapshot = await case_service.case_snapshot(db, case)
    return {item.get("sender_id") for item in snapshot.get("messages") or [] if isinstance(item, dict)}


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
        return juror_id in await _snapshot_speakers(db, case)
    return case.conversation_id in context["conversations"]


async def _assignable_cases(db: AsyncSession, juror_id: int, *, golden: bool, limit: int) -> list[ReportCase]:
    """按分案顺序找出能发给这位委员的案件，最多 limit 个。

    候选分批往后翻：排在前面的案件全都要回避时，不能因此报「没有案件」。
    """
    now = datetime.now(UTC)
    seen = select(JuryVote.case_id).where(JuryVote.juror_id == juror_id)
    stmt = select(ReportCase.id).where(ReportCase.jury_eligible.is_(True), ReportCase.jury_block_reason.is_(None), ReportCase.id.not_in(seen))
    if golden:
        stmt = stmt.where(ReportCase.is_golden.is_(True), ReportCase.status != "open").order_by(func.random())
    else:
        stmt = stmt.where(ReportCase.status == "open", ReportCase.queue == "jury").order_by(ReportCase.priority.desc(), ReportCase.id.asc())
    case_ids = list((await db.execute(stmt.limit(PICK_SCAN_LIMIT))).scalars().all())
    context = await _conflict_context(db, juror_id)
    fresh_after = now - timedelta(hours=ASSIGNMENT_TTL_HOURS)
    found: list[ReportCase] = []
    for start in range(0, len(case_ids), PICK_BATCH):
        batch = case_ids[start : start + PICK_BATCH]
        cases = {case.id: case for case in (await db.execute(select(ReportCase).where(ReportCase.id.in_(batch)))).scalars().all()}
        for case_id in batch:
            case = cases.get(case_id)
            if case is None or await _has_conflict(db, juror_id, case, context):
                continue
            if golden:
                # 题库里有上线前的旧案件，没有标过敏感类别，发出去之前再查一次。
                if await case_service.is_sensitive(db, case):
                    continue
            else:
                # 有效票（弃权不占席位）+ 仍在有效期内的未投分配，占满就不再发给新人。
                seats = await db.execute(
                    select(func.count())
                    .select_from(JuryVote)
                    .where(
                        JuryVote.case_id == case.id,
                        or_(
                            JuryVote.voted_at.is_not(None) & (JuryVote.vote != "abstain"),
                            JuryVote.voted_at.is_(None) & (JuryVote.assigned_at >= fresh_after),
                        ),
                    )
                )
                if int(seats.scalar_one() or 0) >= SEATS_PER_CASE:
                    continue
            found.append(case)
            if len(found) >= limit:
                return found
    return found


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
    pending = await db.execute(
        select(func.count())
        .select_from(JuryVote)
        .join(ReportCase, ReportCase.id == JuryVote.case_id)
        .where(
            JuryVote.juror_id == user.id,
            JuryVote.voted_at.is_(None),
            JuryVote.is_quality_check.is_(False),
            JuryVote.assigned_at >= now - timedelta(hours=ASSIGNMENT_TTL_HOURS),
            ReportCase.status == "open",
            ReportCase.queue == "jury",
        )
    )
    count = int(pending.scalar_one() or 0)
    if count < AVAILABLE_CASES_CAP:
        count += len(await _assignable_cases(db, user.id, golden=False, limit=AVAILABLE_CASES_CAP - count))
    return min(count, AVAILABLE_CASES_CAP)


async def next_case(db: AsyncSession, user: User) -> dict[str, Any] | None:
    """取下一个待评审的案件；没有可分配的返回 None。重复调用返回同一个未投的案件。"""
    member = await _require_serving(db, user)
    now = datetime.now(UTC)
    if await _votes_since(db, user.id, now - timedelta(hours=24)) >= DAILY_VOTE_LIMIT:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"code": "jury_daily_limit", "message": f"今天的评审数量已达上限（{DAILY_VOTE_LIMIT} 件），明天再来"},
        )
    pending = (
        await db.execute(
            select(JuryVote)
            .where(JuryVote.juror_id == user.id, JuryVote.voted_at.is_(None), JuryVote.assigned_at >= now - timedelta(hours=ASSIGNMENT_TTL_HOURS))
            .order_by(JuryVote.id.asc())
        )
    ).scalars().all()
    for assignment in pending:
        case = await db.get(ReportCase, assignment.case_id)
        if case is not None and (assignment.is_quality_check or (case.status == "open" and case.queue == "jury")):
            return await juror_case_view(db, case, assignment)
    case = None
    quality_check = False
    if member.votes_total % GOLDEN_EVERY == GOLDEN_EVERY - 1:
        case = await _pick_case(db, user.id, golden=True)
        quality_check = case is not None
    if case is None:
        case = await _pick_case(db, user.id, golden=False)
    if case is None:
        return None
    assignment = JuryVote(case_id=case.id, juror_id=user.id, assigned_at=now, is_quality_check=quality_check)
    db.add(assignment)
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
                "created_at": item.get("created_at"),
                "offset": index - target_index,
            }
        )
    return items


async def juror_case_view(
    db: AsyncSession, case: ReportCase, assignment: JuryVote | None = None, *, with_content: bool = True
) -> dict[str, Any]:
    """委员看到的案件：匿名化的内容和举报理由分布，不含任何身份信息。"""
    reasons = await db.execute(select(UserReport.reason, func.count()).where(UserReport.case_id == case.id).group_by(UserReport.reason))
    view: dict[str, Any] = {
        "case_id": case.id,
        "kind": case.target_type,
        "reasons": [{"reason": reason, "count": int(count)} for reason, count in reasons.all()],
        "report_count": case.report_count,
        "messages": [],
        "post": None,
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
            "created_at": post.get("created_at"),
        }
    return view


# ---------------------------------------------------------------- 投票与结案


def tally(votes: list[str]) -> tuple[str, int | None] | None:
    """返回 ("violation", 等级) / ("clean", None) / ("escalate", None)；票数还不够返回 None。

    一致性看的是「违规 / 不违规」，等级取违规票的较低中位数：委员对轻重的分歧
    不应该让一个明显违规的案件卡住。
    """
    decided = [vote for vote in votes if vote in {"none", "l1", "l2", "l3"}]
    violation_levels = sorted(_VOTE_LEVEL[vote] for vote in decided if vote in _VOTE_LEVEL)
    clean = len(decided) - len(violation_levels)
    majority = max(len(violation_levels), clean)
    settled = (len(decided) >= 3 and majority == len(decided)) or (len(decided) >= SEATS_PER_CASE and majority >= SEATS_PER_CASE - 1)
    if settled:
        if len(violation_levels) > clean:
            return "violation", violation_levels[(len(violation_levels) - 1) // 2]
        return "clean", None
    if len(decided) >= SEATS_PER_CASE or len(votes) >= SEATS_PER_CASE + 2:
        return "escalate", None
    return None


def _vote_agrees(vote: str, upheld: bool) -> bool | None:
    if vote == "abstain":
        return None
    return (vote in _VOTE_LEVEL) == upheld


async def _settle_votes(db: AsyncSession, case: ReportCase, upheld: bool) -> None:
    """案件有了结论（无论来自委员共识还是管理员）后，回填每一票是否与结论一致。"""
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
        _auto_suspend_if_unreliable(member)


async def resettle_votes(db: AsyncSession, case: ReportCase, upheld: bool) -> None:
    """案件被改判后，按新结论重算每一票是否一致。调用方负责 commit。

    votes_settled 不变，只挪 votes_agreed；把这个案件当质控题答过的人挪 golden_correct。
    改判不触发自动停职：那是管理员纠错，不是委员又投了一票。
    """
    votes = (await db.execute(select(JuryVote).where(JuryVote.case_id == case.id, JuryVote.voted_at.is_not(None)))).scalars().all()
    for vote in votes:
        agreed = _vote_agrees(vote.vote or "abstain", upheld)
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
        else:
            # 案件结案后才投、还没来得及计入的票，在这里补上。
            member.votes_settled += 1 if previous is None else 0
            member.votes_agreed = max(0, member.votes_agreed + delta)


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


async def _vote_result(db: AsyncSession, user: User, case_id: int, *, settled: bool, opinion: JuryOpinion | None = None) -> dict[str, Any]:
    if opinion is not None:
        await notify_case_voters(
            db, case_id, "jury.opinion.created", {"case_id": case_id, "opinion_id": opinion.id, "parent_id": None}, exclude=user.id
        )
    return {"case_id": case_id, "settled": settled, "case": await case_detail(db, user.id, case_id)}


async def cast_vote(db: AsyncSession, user: User, case_id: int, vote: str, note: str | None) -> dict[str, Any]:
    if vote not in VOTES:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Unknown vote")
    member = await _require_serving(db, user)
    assignment = (
        await db.execute(select(JuryVote).where(JuryVote.case_id == case_id, JuryVote.juror_id == user.id))
    ).scalar_one_or_none()
    if assignment is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="这个案件没有分配给你")
    if assignment.voted_at is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="你已经投过票了")
    case = await db.get(ReportCase, case_id)
    if case is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Case not found")
    now = datetime.now(UTC)
    assignment.vote = vote
    assignment.note = note.strip()[:500] if note else None
    assignment.voted_at = now
    member.votes_total += 1

    if assignment.is_quality_check:
        # 质控题：当场对答案，不分配代号、不开观点区，备注只留给管理员看。
        agreed = _vote_agrees(vote, case.status == "resolved")
        if agreed is not None:
            assignment.agreed = agreed
            member.golden_total += 1
            member.golden_correct += 1 if agreed else 0
            _auto_suspend_if_unreliable(member)
        await db.commit()
        return await _vote_result(db, user, case_id, settled=False)

    assignment.alias = await _next_alias(db, case_id)
    opinion: JuryOpinion | None = None
    if assignment.note:
        # 投票时写的备注就是这位委员在观点区的第一条观点。
        opinion = JuryOpinion(case_id=case_id, author_id=user.id, content=assignment.note, is_note=True, created_at=now)
        db.add(opinion)

    if case.status != "open" or case.queue != "jury":
        # 投票期间案件已被管理员处理：这一票照常记，按已有结论计入一致率。
        await db.flush()
        if case.status != "open":
            await _settle_votes(db, case, case.status == "resolved")
        await db.commit()
        return await _vote_result(db, user, case_id, settled=False, opinion=opinion)

    await db.flush()
    votes = (
        await db.execute(
            select(JuryVote.vote).where(JuryVote.case_id == case_id, JuryVote.voted_at.is_not(None), JuryVote.is_quality_check.is_(False))
        )
    ).scalars().all()
    result = tally([item for item in votes if item])
    if result is None:
        await db.commit()
        return await _vote_result(db, user, case_id, settled=False, opinion=opinion)
    outcome, level = result
    if outcome == "escalate":
        case_service._leave_jury_queue(case, now, "no_consensus")
        await db.commit()
        await case_service._broadcast_admin(case, "escalated")
        await case_service.notify_jury_queue_changed(db)
        return await _vote_result(db, user, case_id, settled=False, opinion=opinion)
    upheld = outcome == "violation"
    await _settle_votes(db, case, upheld)
    await case_service.resolve_case(
        db,
        case_id,
        source="jury",
        actor_id=None,
        upheld=upheld,
        level=level,
        note="风纪委员评审认定违规" if upheld else "风纪委员评审认定不违规",
    )
    return await _vote_result(db, user, case_id, settled=True, opinion=opinion)


async def settle_after_admin(db: AsyncSession, case: ReportCase) -> None:
    """管理员直接结案后调用，让已经投过票的委员也能计入一致率。调用方负责 commit。"""
    if case.status != "open":
        await _settle_votes(db, case, case.status == "resolved")


# ---------------------------------------------------------------- 投后视图


def _not_voted() -> HTTPException:
    return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail={"code": "not_voted", "message": "投票之后才能查看这个案件"})


async def my_vote(db: AsyncSession, user_id: int, case_id: int) -> JuryVote | None:
    """这个人对这个案件投出的票；只分到手还没投的不算。"""
    result = await db.execute(select(JuryVote).where(JuryVote.case_id == case_id, JuryVote.juror_id == user_id, JuryVote.voted_at.is_not(None)))
    return result.scalar_one_or_none()


async def require_voted(db: AsyncSession, user_id: int, case_id: int) -> tuple[ReportCase, JuryVote]:
    case = await db.get(ReportCase, case_id)
    if case is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"code": "case_not_found", "message": "案件不存在"})
    vote = await my_vote(db, user_id, case_id)
    if vote is None:
        raise _not_voted()
    return case, vote


def case_stage(case: ReportCase) -> str:
    """jury 委员评审中 / admin 等管理员 / closed 已定案。panel（资深合议）三期才有。"""
    if case.status != "open":
        return "closed"
    return "jury" if case.queue == "jury" else "admin"


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
        # 题库里这道题的讲解，二期才有地方存
        "explanation": None,
    }


async def vote_tallies(db: AsyncSession, case_ids: list[int]) -> dict[int, dict[str, int]]:
    """每个案件各选项的票数。把案件当质控题答的票不算。"""
    tallies = {case_id: {"none": 0, "l1": 0, "l2": 0, "l3": 0, "abstain": 0} for case_id in case_ids}
    if not case_ids:
        return tallies
    rows = await db.execute(
        select(JuryVote.case_id, JuryVote.vote, func.count())
        .where(JuryVote.case_id.in_(case_ids), JuryVote.voted_at.is_not(None), JuryVote.is_quality_check.is_(False))
        .group_by(JuryVote.case_id, JuryVote.vote)
    )
    for case_id, vote, count in rows.all():
        if vote in tallies[case_id]:
            tallies[case_id][vote] = int(count)
    return tallies


def effective_votes(counts: dict[str, int]) -> int:
    """有效票数：不含弃权。"""
    return sum(count for vote, count in counts.items() if vote != "abstain")


async def opinion_counts(db: AsyncSession, case_ids: list[int]) -> dict[int, int]:
    """每个案件观点区里还在的观点数（含回复、含被折叠的，不含已删除的）。"""
    if not case_ids:
        return {}
    rows = await db.execute(
        select(JuryOpinion.case_id, func.count()).where(JuryOpinion.case_id.in_(case_ids), JuryOpinion.state != "deleted").group_by(JuryOpinion.case_id)
    )
    return {case_id: int(count) for case_id, count in rows.all()}


async def case_detail(db: AsyncSession, user_id: int, case_id: int) -> dict[str, Any]:
    """投票之后看到的案件：自己的票、进度，定案后才有票数分布和结论。"""
    case, vote = await require_voted(db, user_id, case_id)
    closed = case.status != "open"
    quality_check = vote.is_quality_check
    available = True
    if closed:
        # 保留期从定案算起；定案之后才投的（质控题都是）从投票算起，否则刚投完就看不到内容。
        since = max(moment for moment in (penalty_service.aware(case.closed_at), penalty_service.aware(vote.voted_at)) if moment is not None)
        available = datetime.now(UTC) < since + timedelta(days=CONTENT_RETENTION_DAYS)
    counts = (await vote_tallies(db, [case.id]))[case.id]
    return {
        **await juror_case_view(db, case, with_content=available),
        "content_available": available,
        "my_vote": {
            "vote": vote.vote,
            "note": vote.note,
            "voted_at": penalty_service.aware(vote.voted_at),
            "alias": vote.alias,
            # 未定案或弃权时为 null；改判后会跟着变
            "agreed": vote.agreed if closed else None,
        },
        "stage": case_stage(case),
        # 定案前只给有效票数，不含弃权：避免从数字变化反推别人投了什么。质控题不透露真实案件的票。
        "votes_cast": 0 if quality_check else effective_votes(counts),
        "tally": counts if closed and not quality_check else None,
        "verdict": verdict_view(case) if closed else None,
        "is_quality_check": quality_check,
        "opinions_enabled": not quality_check,
        "opinion_count": 0 if quality_check else (await opinion_counts(db, [case.id])).get(case.id, 0),
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
                "case_id": case.id,
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
    """我的评审：自己投过的票，按投票时间倒序。"""
    clauses = [JuryVote.juror_id == juror_id, JuryVote.voted_at.is_not(None)]
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
            .order_by(JuryVote.voted_at.desc(), JuryVote.id.desc())
            .offset(offset)
            .limit(limit)
        )
    ).all()
    opinions = await opinion_counts(db, [case.id for vote, case in rows if not vote.is_quality_check])
    items: list[dict[str, Any]] = []
    for vote, case in rows:
        closed = case.status != "open"
        items.append(
            {
                "case_id": case.id,
                "kind": case.target_type,
                "primary_reason": case.primary_reason,
                "vote": vote.vote,
                "voted_at": penalty_service.aware(vote.voted_at),
                "alias": vote.alias,
                "stage": case_stage(case),
                "outcome": ("violation" if case.status == "resolved" else "clean") if closed else None,
                "verdict_source": case.verdict_source if closed else None,
                "verdict_level": case.verdict_level if case.status == "resolved" else None,
                "closed_at": penalty_service.aware(case.closed_at),
                "agreed": vote.agreed if closed else None,
                "overturned": case.overturned_at is not None,
                "is_quality_check": vote.is_quality_check,
                "opinion_count": 0 if vote.is_quality_check else opinions.get(case.id, 0),
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
    clauses = [JuryMember.status == status_filter] if status_filter else []
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
    items: list[dict[str, Any]] = []
    for member, user in rows.all():
        items.append(
            {
                "user_id": member.user_id,
                "user": user,
                "status": member.status,
                "statement": member.statement,
                "applied_at": penalty_service.aware(member.applied_at),
                "reviewed_at": penalty_service.aware(member.reviewed_at),
                "review_note": member.review_note,
                "term_ends_at": penalty_service.aware(member.term_ends_at),
                "tier": member.tier,
                **member_stats(member),
            }
        )
    return items, total
