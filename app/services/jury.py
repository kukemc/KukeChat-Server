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

from app.models.case import JuryMember, JuryVote, ReportCase
from app.models.conversation import ConversationMember
from app.models.enums import UserStatus
from app.models.friend import Friendship
from app.models.message import Message
from app.models.report import AdminAuditLog, UserReport
from app.models.user import User
from app.services import cases as case_service
from app.services import penalties as penalty_service

logger = logging.getLogger(__name__)
UTC = timezone.utc

MIN_ACCOUNT_AGE_DAYS = 30
MIN_RECENT_MESSAGES = 30
RECENT_ACTIVITY_DAYS = 30
TERM_DAYS = 30
DAILY_VOTE_LIMIT = 30
SEATS_PER_CASE = 5
ASSIGNMENT_TTL_HOURS = 24
# 每 GOLDEN_EVERY 个案件里混入一道已有结论的质控题
GOLDEN_EVERY = 5
MIN_SETTLED_FOR_REVIEW = 20
MIN_GOLDEN_FOR_REVIEW = 5
MIN_ACCURACY = 0.6

VOTES = {"none", "l1", "l2", "l3", "abstain"}
_VOTE_LEVEL = {"l1": 1, "l2": 2, "l3": 3}
CONTEXT_BEFORE = 5
CONTEXT_AFTER = 3


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
    state = "none"
    if member is not None:
        state = member.status
        if member.status == "active" and term_ends_at is not None and term_ends_at <= now:
            state = "expired"
    voted_today = 0
    if member is not None:
        voted_today = await _votes_since(db, user.id, now - timedelta(hours=24))
    return {
        "status": state,
        "eligible": all(item["ok"] for item in requirements),
        "requirements": requirements,
        "term_ends_at": term_ends_at,
        "review_note": member.review_note if member and member.status in {"rejected", "suspended"} else None,
        "stats": member_stats(member) if member else None,
        "daily_limit": DAILY_VOTE_LIMIT,
        "voted_today": voted_today,
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


async def review_member(db: AsyncSession, user_id: int, *, action: str, actor_id: int, note: str | None) -> JuryMember:
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
    if action in {"approve", "reinstate"}:
        member.status = "active"
        member.term_ends_at = now + timedelta(days=TERM_DAYS)
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
    member.review_note = note.strip()[:500] if note else None
    db.add(AdminAuditLog(actor_id=actor_id, action=f"jury.member.{action}", target_type="user", target_id=user_id, details=member.review_note))
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
    if user.status != UserStatus.ACTIVE or penalty_service.mute_state(user) is not None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail={"code": "juror_restricted", "message": "受处罚期间不能参与评审"})
    assert member is not None
    return member


async def _has_conflict(db: AsyncSession, juror_id: int, case: ReportCase) -> bool:
    """回避：本人涉案、举报人、被举报人的好友、案发会话的成员。"""
    if case.reported_user_id == juror_id:
        return True
    reporter = await db.execute(select(UserReport.id).where(UserReport.case_id == case.id, UserReport.reporter_id == juror_id).limit(1))
    if reporter.scalar_one_or_none() is not None:
        return True
    if case.reported_user_id is not None:
        friend = await db.execute(
            select(Friendship.id)
            .where(
                or_(
                    (Friendship.user_id == juror_id) & (Friendship.friend_id == case.reported_user_id),
                    (Friendship.user_id == case.reported_user_id) & (Friendship.friend_id == juror_id),
                )
            )
            .limit(1)
        )
        if friend.scalar_one_or_none() is not None:
            return True
    if case.conversation_id is not None:
        member = await db.execute(
            select(ConversationMember.id)
            .where(ConversationMember.conversation_id == case.conversation_id, ConversationMember.user_id == juror_id)
            .limit(1)
        )
        if member.scalar_one_or_none() is not None:
            return True
    return False


async def _pick_case(db: AsyncSession, juror_id: int, *, golden: bool) -> ReportCase | None:
    now = datetime.now(UTC)
    seen = select(JuryVote.case_id).where(JuryVote.juror_id == juror_id)
    stmt = select(ReportCase).where(ReportCase.jury_eligible.is_(True), ReportCase.id.not_in(seen))
    if golden:
        stmt = stmt.where(ReportCase.is_golden.is_(True), ReportCase.status != "open").order_by(func.random())
    else:
        stmt = stmt.where(ReportCase.status == "open", ReportCase.queue == "jury").order_by(ReportCase.priority.desc(), ReportCase.id.asc())
    candidates = (await db.execute(stmt.limit(30))).scalars().all()
    fresh_after = now - timedelta(hours=ASSIGNMENT_TTL_HOURS)
    for case in candidates:
        if await _has_conflict(db, juror_id, case):
            continue
        if not golden:
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
        return case
    return None


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
        if case is not None and (case.is_golden or (case.status == "open" and case.queue == "jury")):
            return await juror_case_view(db, case)
    case = None
    if member.votes_total % GOLDEN_EVERY == GOLDEN_EVERY - 1:
        case = await _pick_case(db, user.id, golden=True)
    if case is None:
        case = await _pick_case(db, user.id, golden=False)
    if case is None:
        return None
    db.add(JuryVote(case_id=case.id, juror_id=user.id, assigned_at=now))
    await db.commit()
    return await juror_case_view(db, case)


def _anonymize_messages(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    messages = [item for item in snapshot.get("messages") or [] if isinstance(item, dict)]
    target_index = next((index for index, item in enumerate(messages) if item.get("is_target")), None)
    if target_index is None:
        target = snapshot.get("message")
        messages, target_index = ([target], 0) if isinstance(target, dict) else ([], None)
    if target_index is None:
        return []
    window = messages[max(0, target_index - CONTEXT_BEFORE) : target_index + CONTEXT_AFTER + 1]
    target_sender = messages[target_index].get("sender_id")
    aliases: dict[Any, str] = {target_sender: "被举报人"}
    items: list[dict[str, Any]] = []
    for item in window:
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
            }
        )
    return items


async def juror_case_view(db: AsyncSession, case: ReportCase) -> dict[str, Any]:
    """委员看到的案件：匿名化的内容和举报理由分布，不含任何身份信息。"""
    snapshot = await case_service.case_snapshot(db, case)
    reasons = await db.execute(select(UserReport.reason, func.count()).where(UserReport.case_id == case.id).group_by(UserReport.reason))
    view: dict[str, Any] = {
        "case_id": case.id,
        "kind": case.target_type,
        "reasons": [{"reason": reason, "count": int(count)} for reason, count in reasons.all()],
        "report_count": case.report_count,
        "messages": [],
        "post": None,
    }
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
    votes = (await db.execute(select(JuryVote).where(JuryVote.case_id == case.id, JuryVote.voted_at.is_not(None), JuryVote.agreed.is_(None)))).scalars().all()
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


def _auto_suspend_if_unreliable(member: JuryMember) -> None:
    if member.status != "active":
        return
    low_agreement = member.votes_settled >= MIN_SETTLED_FOR_REVIEW and member.votes_agreed / member.votes_settled < MIN_ACCURACY
    low_golden = member.golden_total >= MIN_GOLDEN_FOR_REVIEW and member.golden_correct / member.golden_total < MIN_ACCURACY
    if low_agreement or low_golden:
        member.status = "suspended"
        member.review_note = "评审准确率过低，系统自动暂停资格"
        member.reviewed_at = datetime.now(UTC)


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

    if case.is_golden and case.status != "open":
        agreed = _vote_agrees(vote, case.status == "resolved")
        if agreed is not None:
            assignment.agreed = agreed
            member.golden_total += 1
            member.golden_correct += 1 if agreed else 0
            _auto_suspend_if_unreliable(member)
        await db.commit()
        return {"case_id": case_id, "settled": False}

    if case.status != "open" or case.queue != "jury":
        # 投票期间案件已被管理员处理：这一票照常记，按已有结论计入一致率。
        await db.flush()
        if case.status != "open":
            await _settle_votes(db, case, case.status == "resolved")
        await db.commit()
        return {"case_id": case_id, "settled": False}

    await db.flush()
    votes = (await db.execute(select(JuryVote.vote).where(JuryVote.case_id == case_id, JuryVote.voted_at.is_not(None)))).scalars().all()
    result = tally([item for item in votes if item])
    if result is None:
        await db.commit()
        return {"case_id": case_id, "settled": False}
    outcome, level = result
    if outcome == "escalate":
        case.queue = "admin"
        case.updated_at = now
        await db.commit()
        await case_service._broadcast_admin(case, "escalated")
        return {"case_id": case_id, "settled": False}
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
    return {"case_id": case_id, "settled": True}


async def settle_after_admin(db: AsyncSession, case: ReportCase) -> None:
    """管理员直接结案后调用，让已经投过票的委员也能计入一致率。调用方负责 commit。"""
    if case.status != "open":
        await _settle_votes(db, case, case.status == "resolved")


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


async def list_members(db: AsyncSession, status_filter: str | None, limit: int, offset: int) -> tuple[list[dict[str, Any]], int]:
    clauses = [JuryMember.status == status_filter] if status_filter else []
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
                **member_stats(member),
            }
        )
    return items, total
