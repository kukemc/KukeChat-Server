"""资深复审：申诉复审（docs/jury-v2.md 3.2）和举报人复议（3.7）。

两种复审共用一张表、一套流程：三位资深委员各投一票，两票相同即出结果；凑不齐人或 72 小时没结果就转给管理员。
申诉复审看的是一条处罚（被处罚的人申诉），复议看的是一个「认定不违规」的案件（举报人不服）。
这里的 review_id 是复审本身的 id，和案件的评审编号没有关系。

依赖方向：这个模块在 penalties / cases 之上；它们需要回调这里时（处罚被撤销、案件被改判、管理员处理申诉）在函数里导入。
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.case import JuryMember, JuryNotification, JuryReview, JuryReviewVote, ReportCase
from app.models.conversation import Conversation
from app.models.enums import ConversationType, MessageType, PostVisibility, ReportStatus
from app.models.message import Message
from app.models.penalty import UserViolation
from app.models.post import Post
from app.models.report import AdminAuditLog, ContentModerationRecord, UserReport
from app.models.user import User
from app.services import cases as case_service
from app.services import jury as jury_service
from app.services import jury_opinions as opinion_service
from app.services import jury_panel as panel_service
from app.services import jury_points as points_service
from app.services import jury_settings
from app.services import penalties as penalty_service

logger = logging.getLogger(__name__)
UTC = timezone.utc

# 和合议一样：席位数和「几票相同即出结果」是规则本身，不做成配置。
REVIEW_SEATS = 3
REVIEW_CONSENSUS_VOTES = 2
REVIEW_TIMEOUT_HOURS = settings.jury_appeal_timeout_hours
APPEAL_VOTES = ("uphold", "reduce", "revoke")
RECONSIDER_VOTES = ("uphold", "overturn")
# 举报人复议已经实现；GET /jury/config 据此决定下不下发 reconsider 能力和配置
RECONSIDER_ENABLED = True
RECONSIDER_WINDOW_DAYS = settings.jury_reconsider_window_days
RECONSIDER_TEXT_MIN = settings.jury_reconsider_text_min
# 复审要看原内容，这些类别的处罚不给资深委员（委员池里未成年人占多数），直接到管理员
BLOCKED_CATEGORIES = {"sexual", "minor_sexual", "vulgar", "suggestive"}
REVIEW_SCAN_LIMIT = 200
_APPEAL_RESULTS = {"uphold": "upheld", "reduce": "reduced", "revoke": "revoked"}
_APPEAL_NOTES = {
    "uphold": "资深委员复审：维持原处罚",
    "reduce": "资深委员复审：减轻处罚（解除禁言 / 封禁，违规记录保留）",
    "revoke": "资深委员复审：撤销处罚",
}


def _not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"code": "review_not_found", "message": "这件复审没有分配给你"})


# ---------------------------------------------------------------- 谁能参加


async def _conflict_target(db: AsyncSession, review: JuryReview | None, *, violation: UserViolation | None = None, case: ReportCase | None = None) -> tuple[Any, set[int], set[Any] | None]:
    """复审的回避对象：(案件或它的替身, 不能参加的人, 上下文里的发言人)。

    来自案件的（委员 / 合议定案的处罚、复议）按案件走合议那套回避规则，并排除在这个案件上分到过的所有人（原审和合议都算）。
    机器处罚没有案件：造一个只带当事人和所在会话的替身，回避当事人、他的好友和那个群的成员（大型公开群只回避当事人）。
    """
    if review is not None:
        if case is None and review.case_id is not None:
            case = await db.get(ReportCase, review.case_id)
        if violation is None and review.violation_id is not None:
            violation = await db.get(UserViolation, review.violation_id)
    elif case is None and violation is not None and violation.case_id is not None:
        case = await db.get(ReportCase, violation.case_id)
    taken = {review.requester_id} if review is not None else set()
    if violation is not None:
        taken.add(violation.user_id)
    if case is not None:
        # 这个案件别的复审里投过票的人也不行：比如复议改判之后，被处罚的人再申诉，不能还是那几位来审
        others = await db.execute(
            select(JuryReviewVote.juror_id)
            .join(JuryReview, JuryReview.id == JuryReviewVote.review_id)
            .where(JuryReview.case_id == case.id, JuryReview.id != (review.id if review is not None and review.id is not None else 0))
        )
        return case, taken | await panel_service.case_participants(db, case.id) | set(others.scalars().all()), None
    user_id = violation.user_id if violation is not None else None
    target = SimpleNamespace(
        # 负数：不会和真实案件的 id 撞上（回避关系里按案件 id 查「我举报过的案件」）
        id=-(violation.id if violation is not None else 0),
        reported_user_id=user_id,
        conversation_id=violation.conversation_id if violation is not None and violation.target_type == "message" else None,
        first_report_id=None,
        snapshot=None,
    )
    return target, taken, {user_id}


async def _review_seats(db: AsyncSession, review_id: int, now: datetime) -> tuple[int, int, set[int]]:
    """(已投的票数, 还在有效期内没投的分配数, 分到过的所有人)。"""
    fresh_after = now - timedelta(hours=jury_service.ASSIGNMENT_TTL_HOURS)
    rows = await db.execute(select(JuryReviewVote.juror_id, JuryReviewVote.voted_at, JuryReviewVote.assigned_at).where(JuryReviewVote.review_id == review_id))
    voted = pending = 0
    assigned: set[int] = set()
    for juror_id, voted_at, assigned_at in rows.all():
        assigned.add(juror_id)
        if voted_at is not None:
            voted += 1
        elif penalty_service.aware(assigned_at) >= fresh_after:
            pending += 1
    return voted, pending, assigned


async def _still_viable(db: AsyncSession, review: JuryReview, now: datetime) -> bool:
    voted, pending, assigned = await _review_seats(db, review.id, now)
    if voted + pending >= REVIEW_SEATS:
        return True
    target, taken, speakers = await _conflict_target(db, review)
    free = await panel_service.available_seniors(db, target, now, taken=taken | assigned, speakers=speakers)
    return voted + pending + len(free) >= REVIEW_SEATS


# ---------------------------------------------------------------- 申诉进不进复审


async def _is_public_content(db: AsyncSession, violation: UserViolation, case: ReportCase | None) -> bool:
    """被处罚的内容是不是群聊消息或公开动态。私聊、临时会话、评论、资料、名片等都不算。"""
    if violation.target_type not in {"message", "post"} or violation.target_id is None:
        return False
    if case is not None:
        # 来自案件的：建案时已经判断过
        return bool(case.jury_eligible)
    if violation.target_type == "message":
        conversation_id = violation.conversation_id
        if conversation_id is None:
            message = await db.get(Message, violation.target_id)
            conversation_id = message.conversation_id if message is not None else None
        conversation = await db.get(Conversation, conversation_id) if conversation_id is not None else None
        return conversation is not None and conversation.type == ConversationType.GROUP and not conversation.is_temporary
    post = await db.get(Post, violation.target_id)
    return post is not None and post.visibility == PostVisibility.PUBLIC


def _is_heavy_penalty(violation: UserViolation) -> bool:
    """永久封禁、超过上限（7 天）的封禁，以及被上限压住、还在等管理员确认更重处置的：这些只能管理员看。"""
    if violation.review_status == "pending":
        return True
    if violation.penalty_kind != "ban":
        return False
    created_at, ends_at = penalty_service.aware(violation.created_at), penalty_service.aware(violation.penalty_ends_at)
    if violation.penalty_permanent or created_at is None or ends_at is None:
        return True
    return ends_at - created_at > timedelta(hours=penalty_service.CAPPED_BAN_HOURS, minutes=5)


async def appeal_block_reason(db: AsyncSession, violation: UserViolation, now: datetime | None = None) -> str | None:
    """这条申诉为什么不进资深复审；None 表示进（docs/jury-v2.md 3.2 的五个条件，外加开关）。

    switch_off 开关没开 / source 处罚不是委员、合议或机器给的 / content 不是群聊消息或公开动态 /
    category 色情、涉未成年类 / penalty 永久或超过 7 天的封禁 / seniors 此刻凑不齐三位资深委员。
    """
    if not await jury_settings.is_enabled(db, "appeals"):
        return "switch_off"
    case = await db.get(ReportCase, violation.case_id) if violation.case_id is not None else None
    if violation.source == "jury":
        if case is None:
            return "source"
    elif not violation.source.startswith("auto_"):
        return "source"
    if not await _is_public_content(db, violation, case):
        return "content"
    if violation.category in BLOCKED_CATEGORIES or (case is not None and case.jury_block_reason == "sensitive"):
        return "category"
    if _is_heavy_penalty(violation):
        return "penalty"
    target, taken, speakers = await _conflict_target(db, None, violation=violation, case=case)
    seniors = await panel_service.available_seniors(db, target, now or datetime.now(UTC), taken=taken, speakers=speakers)
    if len(seniors) < REVIEW_SEATS:
        return "seniors"
    return None


async def open_appeal_review(db: AsyncSession, violation: UserViolation, now: datetime) -> JuryReview | None:
    """penalties.submit_appeal 调用：条件都满足时建一件资深复审并把申诉标成「资深复审中」。不提交事务。"""
    if await appeal_block_reason(db, violation, now) is not None:
        return None
    review = JuryReview(
        kind="appeal",
        violation_id=violation.id,
        case_id=violation.case_id,
        requester_id=violation.user_id,
        created_at=now,
        expires_at=now + timedelta(hours=REVIEW_TIMEOUT_HOURS),
    )
    db.add(review)
    violation.appeal_stage = "senior"
    return review


# ---------------------------------------------------------------- 复审视图


def _penalty_view(kind: str | None, created_at: datetime | None, ends_at: datetime | None, permanent: bool) -> dict[str, Any]:
    """{ kind, hours }：禁言 / 封禁给时长（小时），永久和没有时长的处置为 null。"""
    hours: int | None = None
    created_at, ends_at = penalty_service.aware(created_at), penalty_service.aware(ends_at)
    if kind in {"mute", "ban"} and not permanent and created_at is not None and ends_at is not None:
        hours = max(1, round((ends_at - created_at).total_seconds() / 3600))
    return {"kind": kind or "none", "hours": hours}


def _original_penalty(review: JuryReview, violation: UserViolation) -> dict[str, Any]:
    """被申诉的那个处罚。复审「减轻」之后记录上的处置已经变了，这里给的仍是原来的。"""
    before = _parse_penalty_before(review.penalty_before)
    if before is not None:
        return _penalty_view(before["kind"], violation.created_at, before["ends_at"], before["permanent"])
    return _penalty_view(violation.penalty_kind, violation.created_at, violation.penalty_ends_at, bool(violation.penalty_permanent))


def _parse_penalty_before(text: str | None) -> dict[str, Any] | None:
    if not text:
        return None
    try:
        value = json.loads(text)
        ends_at = datetime.fromisoformat(value["ends_at"]) if value.get("ends_at") else None
        return {"kind": str(value["kind"]), "ends_at": ends_at, "permanent": bool(value.get("permanent"))}
    except (KeyError, TypeError, ValueError):
        return None


def _source(violation: UserViolation | None, case: ReportCase | None) -> str:
    """jury / panel / machine：原结论是谁给的。"""
    if violation is not None and violation.source.startswith("auto_"):
        return "machine"
    return "panel" if case is not None and case.verdict_source == "panel" else "jury"


async def _machine_content(db: AsyncSession, violation: UserViolation) -> dict[str, Any]:
    """机器处罚没有案件快照，只给被处罚的那条内容：文字取审查记录里的原文（没有就用处罚摘要），图片只给数量。"""
    record = await db.get(ContentModerationRecord, violation.moderation_record_id) if violation.moderation_record_id is not None else None
    text: str | None = None
    if record is not None:
        text = record.content_text if record.content_type == "text" else None
    elif violation.summary and violation.summary != "[图片]":
        text = violation.summary
    image_count = 0
    if violation.target_type == "message":
        message = await db.get(Message, violation.target_id)
        if message is not None:
            try:
                metadata = json.loads(message.metadata_json or "{}")
            except json.JSONDecodeError:
                metadata = {}
            images = metadata.get("images") if isinstance(metadata, dict) and isinstance(metadata.get("images"), list) else []
            image_count = len(images) + (1 if message.type == MessageType.IMAGE else 0)
    elif violation.target_type == "post":
        post = await db.get(Post, violation.target_id)
        if post is not None:
            try:
                urls = json.loads(post.image_urls_json or "[]")
            except json.JSONDecodeError:
                urls = []
            image_count = len(urls) if isinstance(urls, list) else 0
    if image_count == 0 and record is not None and record.content_type == "image":
        image_count = 1
    return {"text": (text or "").strip() or None, "image_count": image_count}


async def _case_material(db: AsyncSession, case: ReportCase, viewer_id: int) -> dict[str, Any]:
    """来自案件的复审：案件快照的匿名视图，加上原审（和合议）的票数分布与观点。"""
    view = await jury_service.juror_case_view(db, case)
    tally: dict[str, Any] = (await jury_service.vote_tallies(db, [case.id]))[case.id]
    if case.panel_at is not None:
        tally = {**tally, "panel": (await jury_service.panel_tallies(db, [case.id]))[case.id]}
    return {
        "messages": view["messages"],
        "post": view["post"],
        "origin": {"tally": tally, "opinions": (await opinion_service.origin_page(db, case, viewer_id))["items"]},
    }


async def _summary(db: AsyncSession, review: JuryReview) -> dict[str, Any]:
    """列表和视图共用的那部分：这件复审是关于什么的。"""
    case = await db.get(ReportCase, review.case_id) if review.case_id is not None else None
    if review.kind == "reconsider":
        return {
            "review_id": review.id,
            "type": "reconsider",
            "kind": case.target_type if case is not None else "message",
            "source": _source(None, case),
            "category": None,
            "category_label": None,
            "level": None,
            "penalty": None,
        }
    violation = await db.get(UserViolation, review.violation_id)
    return {
        "review_id": review.id,
        "type": "appeal",
        "kind": (violation.target_type if violation is not None else None) or (case.target_type if case is not None else "message"),
        "source": _source(violation, case),
        "category": violation.category if violation is not None else None,
        "category_label": penalty_service.category_label(violation.category) if violation is not None else None,
        "level": violation.level if violation is not None else None,
        "penalty": _original_penalty(review, violation) if violation is not None else None,
    }


async def review_view(db: AsyncSession, review: JuryReview, viewer_id: int, now: datetime | None = None) -> dict[str, Any]:
    """AppealReviewView：资深委员投票前看到的全部材料。不含任何身份信息。"""
    now = now or datetime.now(UTC)
    voted, _pending, _assigned = await _review_seats(db, review.id, now)
    case = await db.get(ReportCase, review.case_id) if review.case_id is not None else None
    view: dict[str, Any] = {
        **await _summary(db, review),
        "messages": [],
        "post": None,
        "content": None,
        "origin": None,
        "appeal_text": None,
        "appealed_at": None,
        "verdict": None,
        "reconsider_text": None,
        "requested_at": None,
        "expires_at": penalty_service.aware(review.expires_at),
        "seats": REVIEW_SEATS,
        "votes_cast": voted,
    }
    if case is not None:
        view.update(await _case_material(db, case, viewer_id))
    if review.kind == "reconsider":
        view["reconsider_text"] = review.text
        view["requested_at"] = penalty_service.aware(review.created_at)
        if case is not None:
            # 原结论。案件之后被改判的话复审也就结束了，所以这里恒为 clean
            view["verdict"] = {"outcome": "clean", "source": case.verdict_source, "closed_at": penalty_service.aware(case.closed_at)}
        return view
    violation = await db.get(UserViolation, review.violation_id)
    if violation is not None:
        view["appeal_text"] = violation.appeal_text
        view["appealed_at"] = penalty_service.aware(violation.appealed_at)
        if case is None:
            view["content"] = await _machine_content(db, violation)
    return view


# ---------------------------------------------------------------- 分案


async def _assignable(db: AsyncSession, juror_id: int, now: datetime, *, limit: int) -> list[JuryReview]:
    """能分给这位资深委员的复审：没分到过、不是当事人、通过回避规则、席位没坐满。先到期的先分。"""
    mine = select(JuryReviewVote.review_id).where(JuryReviewVote.juror_id == juror_id)
    rows = await db.execute(
        select(JuryReview)
        .where(JuryReview.status == "open", JuryReview.stage == "senior", JuryReview.expires_at > now, JuryReview.id.not_in(mine))
        .order_by(JuryReview.expires_at.asc(), JuryReview.id.asc())
        .limit(REVIEW_SCAN_LIMIT)
    )
    context = await jury_service._conflict_context(db, juror_id)
    found: list[JuryReview] = []
    for review in rows.scalars().all():
        target, taken, speakers = await _conflict_target(db, review)
        if juror_id in taken:
            continue
        if speakers is not None:
            context["speakers"][target.id] = speakers
        if await jury_service._has_conflict(db, juror_id, target, context):
            continue
        voted, pending, _assigned = await _review_seats(db, review.id, now)
        if voted + pending >= REVIEW_SEATS:
            continue
        found.append(review)
        if len(found) >= limit:
            break
    return found


async def _pending(db: AsyncSession, juror_id: int, now: datetime) -> list[JuryReview]:
    """已经分到手、还没投的复审。"""
    rows = await db.execute(
        select(JuryReview)
        .join(JuryReviewVote, JuryReviewVote.review_id == JuryReview.id)
        .where(
            JuryReviewVote.juror_id == juror_id,
            JuryReviewVote.voted_at.is_(None),
            JuryReviewVote.assigned_at >= now - timedelta(hours=jury_service.ASSIGNMENT_TTL_HOURS),
            JuryReview.status == "open",
            JuryReview.stage == "senior",
        )
        .order_by(JuryReviewVote.id.asc())
    )
    return list(rows.scalars().all())


async def available_count(db: AsyncSession, user: User) -> int:
    """/jury/me.senior.appeals：现在能分给我的复审件数（含举报人复议）。"""
    now = datetime.now(UTC)
    if jury_service.restriction(user, now) is not None:
        return 0
    count = len(await _pending(db, user.id, now))
    if count < jury_service.AVAILABLE_CASES_CAP:
        count += len(await _assignable(db, user.id, now, limit=jury_service.AVAILABLE_CASES_CAP - count))
    return min(count, jury_service.AVAILABLE_CASES_CAP)


async def next_review(db: AsyncSession, user: User) -> dict[str, Any] | None:
    """GET /jury/appeals/next：取下一件复审；没有返回 None。重复调用返回同一件没投的。"""
    await panel_service.require_senior(db, user)
    now = datetime.now(UTC)
    for review in await _pending(db, user.id, now):
        return await review_view(db, review, user.id, now)
    reviews = await _assignable(db, user.id, now, limit=1)
    if not reviews:
        return None
    db.add(JuryReviewVote(review_id=reviews[0].id, juror_id=user.id, assigned_at=now))
    await db.commit()
    return await review_view(db, reviews[0], user.id, now)


# ---------------------------------------------------------------- 投票与结果


def review_tally(kind: str, votes: list[str]) -> str | None:
    """两票相同即出结果；申诉复审三票各不相同时取「减轻」。还没有结果返回 None。"""
    ranked = Counter(votes).most_common(1)
    if ranked and ranked[0][1] >= REVIEW_CONSENSUS_VOTES:
        return ranked[0][0]
    if kind == "appeal" and len(votes) >= REVIEW_SEATS:
        return "reduce"
    return None


def _notify_requester(db: AsyncSession, user_id: int, kind: str, title: str, body: str) -> None:
    """给申诉人 / 申请复议的举报人在通知中心留一条结果。调用方负责 commit。"""
    db.add(JuryNotification(user_id=user_id, kind=kind, title=title[:120], body=body, created_at=datetime.now(UTC)))


async def _apply_appeal_outcome(db: AsyncSession, review: JuryReview, violation: UserViolation, outcome: str, now: datetime) -> None:
    """把资深复审的结果落到处罚上。不提交事务。"""
    votes = (await db.execute(select(JuryReviewVote.vote).where(JuryReviewVote.review_id == review.id, JuryReviewVote.voted_at.is_not(None)))).scalars().all()
    violation.appeal_reviewer_id = None
    violation.appeal_reviewed_at = now
    violation.appeal_note = _APPEAL_NOTES[outcome]
    violation.appeal_stage = None
    violation.appeal_result = _APPEAL_RESULTS[outcome]
    violation.appeal_decided_by = "senior"
    if outcome == "uphold":
        violation.appeal_status = "rejected"
        body = "资深委员复审后维持原处罚。"
    elif outcome == "reduce":
        violation.appeal_status = "accepted"
        ends_at = penalty_service.aware(violation.penalty_ends_at)
        review.penalty_before = json.dumps(
            {"kind": violation.penalty_kind, "ends_at": ends_at.isoformat() if ends_at else None, "permanent": bool(violation.penalty_permanent)}
        )
        if violation.penalty_kind in {"mute", "ban"}:
            # 保留违规记录和记分，只解除这条处罚带来的禁言 / 封禁：和管理员把处置改成「警告」是同一件事
            await penalty_service.adjust_penalty(db, violation.id, actor_id=None, penalty=penalty_service.Penalty("warning"), note="资深复审：减轻")
        body = "资深委员复审后决定减轻处罚：这条处罚带来的禁言 / 封禁已解除，违规记录和记分保留。"
    else:
        # revoke_violation 会连带把产生这条处罚的案件改判成不违规、恢复内容；它看 appeal_status 决定公示栏里的说明
        violation.appeal_status = "accepted"
        await penalty_service.revoke_violation(db, violation.id, actor_id=None, reason="申诉通过（资深复审）")
        body = "资深委员复审后决定撤销这条处罚，记分一并取消。"
    db.add(
        AdminAuditLog(
            actor_id=None,
            action="violation.appeal.reviewed",
            target_type="user",
            target_id=violation.user_id,
            details=json.dumps(
                {
                    "violation_id": violation.id,
                    "accepted": outcome != "uphold",
                    "result": violation.appeal_result,
                    "by": "senior_review",
                    "review_id": review.id,
                    "votes": dict(Counter(votes)),
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
        )
    )
    _notify_requester(db, violation.user_id, "appeal_result", "你的申诉有结果了", body)


async def _settle_appeal(db: AsyncSession, review: JuryReview, outcome: str, now: datetime) -> str | None:
    violation = await db.get(UserViolation, review.violation_id)
    review.status = "closed"
    review.closed_at = now
    if violation is None or violation.status != "active" or violation.appeal_status != "pending":
        # 投票期间处罚已经被撤销，或者申诉已经被管理员处理：复审的结果不再落地
        await db.commit()
        return None
    review.outcome = outcome
    review.decided_by = "senior"
    await _apply_appeal_outcome(db, review, violation, outcome, now)
    await db.commit()
    await penalty_service.notify_violation_updated(db, violation, "appeal_rejected" if outcome == "uphold" else "appeal_accepted")
    return outcome


async def _settle_reconsider(db: AsyncSession, review: JuryReview, outcome: str, now: datetime) -> str | None:
    case = await db.get(ReportCase, review.case_id)
    review.status = "closed"
    review.closed_at = now
    if case is None or case.status != "dismissed":
        # 投票期间案件已经被管理员改判了
        await db.commit()
        return None
    review.outcome = outcome
    review.decided_by = "senior"
    if outcome == "uphold":
        _notify_requester(db, review.requester_id, "reconsider_result", "你申请的复议有结果了", RECONSIDER_UPHELD_BY_SENIOR)
        await db.commit()
        await _notify_reconsider_upheld(db, review, case)
        return outcome
    rows = (
        await db.execute(
            select(JuryReviewVote.level, JuryReviewVote.category).where(
                JuryReviewVote.review_id == review.id, JuryReviewVote.voted_at.is_not(None), JuryReviewVote.vote == "overturn"
            )
        )
    ).all()
    # 类别取 overturn 票里的多数（平票或都没带时按举报理由映射），等级取这些票的较低中位数；没带等级的票按类别的默认等级算
    category = jury_service._verdict_category(case, [("l1", item_category) for _level, item_category in rows])
    levels = sorted(
        max(1, min(3, level if level is not None else penalty_service.default_level(item_category or category))) for level, item_category in rows
    )
    _notify_requester(db, review.requester_id, "reconsider_result", "你申请的复议有结果了", "资深委员复审后改判为违规，相关内容和账号已按社区规则处理。")
    # 改判会提交事务，并通知举报人（report.resolved）、后台和投过票的委员
    await case_service.overturn_case(
        db,
        case.id,
        actor_id=None,
        upheld=True,
        category=category,
        level=levels[(len(levels) - 1) // 2] if levels else None,
        note="举报人申请复议，资深委员复审后改判为违规",
        source="panel",
    )
    return outcome


RECONSIDER_UPHELD_BY_SENIOR = "资深委员复审后维持原结论：暂未发现违规。"
RECONSIDER_UPHELD_BY_ADMIN = "管理员复核后维持原结论：暂未发现违规。"


async def _notify_reconsider_upheld(db: AsyncSession, review: JuryReview, case: ReportCase, body: str = RECONSIDER_UPHELD_BY_SENIOR) -> None:
    """复议维持原结论：给申请人补发一次 report.resolved（改判的那条路由 overturn_case 发）。"""
    from app.websocket.manager import manager

    try:
        report = await db.get(UserReport, review.report_id) if review.report_id is not None else None
        if report is None:
            return
        await manager.send_to_user(
            report.reporter_id,
            "report.resolved",
            {
                "report_id": report.id,
                "case_id": case.id,
                "status": report.status.value,
                "title": "你申请的复议有结果了",
                "body": body,
            },
        )
        await case_service._broadcast_admin(case, "reconsidered")
    except Exception:
        logger.exception("Failed to notify reconsider result", extra={"review_id": review.id})


def _overturn_details(level: int | None, category: str | None) -> tuple[int | None, str | None]:
    """复议投 overturn 时带的等级和类别。等级 1–3；类别要在可选范围里。"""
    if level is not None and (isinstance(level, bool) or not 1 <= level <= 3):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail={"code": "invalid_vote", "message": "等级只能是 1–3"})
    return level, jury_service._vote_category("l1", category)


async def cast_vote(
    db: AsyncSession, user: User, review_id: int, vote: str, note: str | None = None, level: int | None = None, category: str | None = None
) -> dict[str, Any]:
    """POST /jury/appeals/{review_id}/vote。"""
    member = await panel_service.require_senior(db, user)
    review = await db.get(JuryReview, review_id)
    assignment = (
        await db.execute(select(JuryReviewVote).where(JuryReviewVote.review_id == review_id, JuryReviewVote.juror_id == user.id))
    ).scalar_one_or_none()
    if review is None or assignment is None:
        raise _not_found()
    if assignment.voted_at is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={"code": "already_voted", "message": "你已经投过票了"})
    allowed = RECONSIDER_VOTES if review.kind == "reconsider" else APPEAL_VOTES
    if vote not in allowed:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "invalid_vote", "message": "这件复审只能投：" + " / ".join(allowed)},
        )
    if review.status != "open" or review.stage != "senior":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={"code": "review_closed", "message": "这件复审已经结束或已转给管理员"})
    if review.kind == "reconsider" and vote == "overturn":
        assignment.level, assignment.category = _overturn_details(level, category)
    now = datetime.now(UTC)
    assignment.vote = vote
    assignment.note = note.strip()[:500] or None if note else None
    assignment.voted_at = now
    # 每张复审票当场给分，不看结果
    reward = await points_service.award(db, member, "review_vote", ref_id=review.id, announce=False)
    await points_service.check_achievements(db, user.id, member)
    await db.flush()
    points_service.reset_leaderboard_cache()
    votes = (await db.execute(select(JuryReviewVote.vote).where(JuryReviewVote.review_id == review.id, JuryReviewVote.voted_at.is_not(None)))).scalars().all()
    outcome = review_tally(review.kind, [item for item in votes if item])
    settled: str | None = None
    if outcome is None:
        await db.commit()
    elif review.kind == "reconsider":
        settled = await _settle_reconsider(db, review, outcome, now)
    else:
        settled = await _settle_appeal(db, review, outcome, now)
    await points_service.flush_events(db)
    await case_service.notify_jury_queue_changed(db)
    return {"review_id": review.id, "settled": review.status != "open", "outcome": settled, "reward": points_service.reward_view(member, [reward])}


async def my_reviews(db: AsyncSession, user_id: int, *, limit: int = 20, offset: int = 0) -> tuple[list[dict[str, Any]], int]:
    """GET /jury/appeals/mine：我投过票的复审，按投票时间倒序。"""
    clauses = [JuryReviewVote.juror_id == user_id, JuryReviewVote.voted_at.is_not(None)]
    total = int((await db.execute(select(func.count(JuryReviewVote.id)).where(*clauses))).scalar_one() or 0)
    rows = await db.execute(
        select(JuryReviewVote, JuryReview)
        .join(JuryReview, JuryReview.id == JuryReviewVote.review_id)
        .where(*clauses)
        .order_by(JuryReviewVote.voted_at.desc(), JuryReviewVote.id.desc())
        .offset(offset)
        .limit(limit)
    )
    items: list[dict[str, Any]] = []
    for assignment, review in rows.all():
        summary = await _summary(db, review)
        items.append(
            {
                "review_id": review.id,
                "type": summary["type"],
                "kind": summary["kind"],
                "source": summary["source"],
                "category_label": summary["category_label"],
                "level": summary["level"],
                "penalty": summary["penalty"],
                "my_vote": assignment.vote,
                "voted_at": penalty_service.aware(assignment.voted_at),
                # 还没出结果，或者转给了管理员还没处理时为 null
                "outcome": review.outcome,
                "settled_at": penalty_service.aware(review.closed_at) if review.outcome is not None else None,
            }
        )
    return items, total


# ---------------------------------------------------------------- 转管理员


async def _escalate(db: AsyncSession, review: JuryReview, now: datetime, reason: str) -> UserViolation | None:
    """凑不齐人或超时：这件复审转给管理员。不提交事务；返回要通知的那条处罚（申诉复审才有）。"""
    review.stage = "admin"
    review.escalation = reason
    review.escalated_at = now
    if review.kind != "appeal":
        return None
    violation = await db.get(UserViolation, review.violation_id)
    if violation is None or violation.status != "active" or violation.appeal_status != "pending":
        # 处罚已经不在了，或者申诉已经有结果：没有什么可转的
        review.status = "closed"
        review.closed_at = now
        return None
    violation.appeal_stage = "admin"
    return violation


async def sweep(db: AsyncSession) -> int:
    """复审的定时整理：超时的、凑不齐人的转给管理员。返回转了多少件，自己提交事务。"""
    now = datetime.now(UTC)
    reviews = (await db.execute(select(JuryReview).where(JuryReview.status == "open", JuryReview.stage == "senior"))).scalars().all()
    moved: list[tuple[JuryReview, UserViolation | None]] = []
    for review in reviews:
        expired = penalty_service.aware(review.expires_at) <= now
        if not expired and await _still_viable(db, review, now):
            continue
        moved.append((review, await _escalate(db, review, now, "timeout" if expired else "unavailable")))
    if not moved:
        return 0
    await db.commit()
    for review, violation in moved:
        if violation is not None:
            # 申诉人那边「资深复审中」变成「等管理员」；后台的申诉列表也要刷新
            await penalty_service.notify_violation_updated(db, violation, "appeal_escalated")
        elif review.kind == "reconsider" and review.case_id is not None:
            case = await db.get(ReportCase, review.case_id)
            if case is not None:
                await case_service._broadcast_admin(case, "reconsider")
    await case_service.notify_jury_queue_changed(db)
    return len(moved)


# ---------------------------------------------------------------- penalties / cases 的回调


async def on_admin_appeal_decision(db: AsyncSession, violation: UserViolation, *, accept: bool, now: datetime) -> JuryReview | None:
    """管理员处理了申诉：进行中的复审到此为止，已经出了结果的被管理员的结论覆盖。

    返回这条处罚的复审记录（没有则 None），不提交事务。
    """
    review = (
        await db.execute(
            select(JuryReview).where(JuryReview.kind == "appeal", JuryReview.violation_id == violation.id).order_by(JuryReview.id.desc()).limit(1)
        )
    ).scalar_one_or_none()
    if review is None:
        return None
    review.status = "closed"
    review.outcome = "revoke" if accept else "uphold"
    review.decided_by = "admin"
    review.closed_at = now
    return review


async def on_violation_revoked(db: AsyncSession, violation: UserViolation, now: datetime) -> None:
    """处罚被直接撤销（管理员撤销、案件改判）时，它还没出结果的申诉复审到此为止。不提交事务。"""
    reviews = (
        await db.execute(select(JuryReview).where(JuryReview.kind == "appeal", JuryReview.violation_id == violation.id, JuryReview.status == "open"))
    ).scalars().all()
    for review in reviews:
        review.status = "closed"
        review.closed_at = now
    if violation.appeal_stage == "senior":
        violation.appeal_stage = "admin"


async def on_case_overturned(db: AsyncSession, case: ReportCase, *, upheld: bool) -> None:
    """案件被改判成违规时，它还没出结果的举报人复议就此有了结果（管理员给的）。不提交事务。"""
    if not upheld:
        return
    reviews = (
        await db.execute(select(JuryReview).where(JuryReview.kind == "reconsider", JuryReview.case_id == case.id, JuryReview.status == "open"))
    ).scalars().all()
    now = datetime.now(UTC)
    for review in reviews:
        review.status = "closed"
        review.outcome = "overturn"
        review.decided_by = "admin"
        review.closed_at = now
        _notify_requester(db, review.requester_id, "reconsider_result", "你申请的复议有结果了", "管理员复核后改判为违规，相关内容和账号已按社区规则处理。")


# ---------------------------------------------------------------- 举报人复议


def _reconsider_status(review: JuryReview | None) -> str:
    """none / pending / upheld（维持原结论）/ overturned（改判为违规）。"""
    if review is None:
        return "none"
    if review.status == "open":
        return "pending"
    return {"uphold": "upheld", "overturn": "overturned"}.get(review.outcome or "", "none")


async def reconsider_statuses(db: AsyncSession, case_ids: list[int]) -> dict[int, str]:
    """案件 id -> 复议状态；没人申请过的不在结果里。"""
    if not case_ids:
        return {}
    rows = await db.execute(
        select(JuryReview).where(JuryReview.kind == "reconsider", JuryReview.case_id.in_(case_ids)).order_by(JuryReview.id.asc())
    )
    return {review.case_id: _reconsider_status(review) for review in rows.scalars().all()}


def _reconsider_open_to(case: ReportCase | None, now: datetime) -> bool:
    """这个案件现在还能不能申请复议：由委员 / 合议认定不违规、没被改判、定案后在期限内。"""
    if case is None or case.status != "dismissed" or case.verdict_source not in {"jury", "panel"} or case.overturned_at is not None:
        return False
    closed_at = penalty_service.aware(case.closed_at)
    return closed_at is not None and now <= closed_at + timedelta(days=RECONSIDER_WINDOW_DAYS)


async def report_fields(db: AsyncSession, report: UserReport, viewer_id: int | None) -> dict[str, Any]:
    """ReportRead 的 can_reconsider / reconsider_status。"""
    if not RECONSIDER_ENABLED or report.case_id is None or report.status == ReportStatus.PENDING:
        # 还没处理完的举报，案件一定还没定案：不用查
        return {"can_reconsider": False, "reconsider_status": "none"}
    review = (
        await db.execute(select(JuryReview).where(JuryReview.kind == "reconsider", JuryReview.case_id == report.case_id).order_by(JuryReview.id.desc()).limit(1))
    ).scalar_one_or_none()
    can = False
    if review is None and viewer_id is not None and viewer_id == report.reporter_id:
        can = _reconsider_open_to(await db.get(ReportCase, report.case_id), datetime.now(UTC))
    return {"can_reconsider": can, "reconsider_status": _reconsider_status(review)}


async def request_reconsider(db: AsyncSession, user_id: int, report_id: int, text: str | None) -> UserReport:
    """POST /reports/{report_id}/reconsider。一个案件只能复议一次，不管有几个举报人。"""
    report = await db.get(UserReport, report_id)
    if report is None or report.reporter_id != user_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"code": "report_not_found", "message": "举报不存在"})
    content = (text or "").strip()
    if len(content) < RECONSIDER_TEXT_MIN:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "text_required", "message": f"请写明申请复议的理由（至少 {RECONSIDER_TEXT_MIN} 个字）"},
        )
    now = datetime.now(UTC)
    case = await db.get(ReportCase, report.case_id) if report.case_id is not None else None
    existing = None
    if case is not None:
        existing = (
            await db.execute(select(JuryReview.id).where(JuryReview.kind == "reconsider", JuryReview.case_id == case.id).limit(1))
        ).scalar_one_or_none()
    if existing is not None or not _reconsider_open_to(case, now):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "reconsider_unavailable", "message": "这条举报现在不能申请复议：只有委员认定不违规的结论可以复议，定案后限期内每个案件一次"},
        )
    review = JuryReview(
        kind="reconsider",
        case_id=case.id,
        report_id=report.id,
        requester_id=user_id,
        text=content[:2000],
        created_at=now,
        expires_at=now + timedelta(hours=REVIEW_TIMEOUT_HOURS),
    )
    target, taken, speakers = await _conflict_target(db, review, case=case)
    if len(await panel_service.available_seniors(db, target, now, taken=taken, speakers=speakers)) < REVIEW_SEATS:
        # 凑不齐三位资深委员：直接转给管理员，由管理员决定要不要改判
        review.stage = "admin"
        review.escalation = "unavailable"
        review.escalated_at = now
    db.add(review)
    await db.commit()
    await case_service._broadcast_admin(case, "reconsider")
    if review.stage == "senior":
        await case_service.notify_jury_queue_changed(db)
    return report


# ---------------------------------------------------------------- 后台


async def admin_list(
    db: AsyncSession, *, kind: str | None = None, status_filter: str | None = None, limit: int = 50, offset: int = 0
) -> tuple[list[dict[str, Any]], int]:
    """GET /admin/jury/reviews：资深复审的监控，带每位复审委员的票（实名）。"""
    clauses: list[Any] = []
    if kind:
        clauses.append(JuryReview.kind == kind)
    if status_filter:
        clauses.append(JuryReview.status == status_filter)
    total = int((await db.execute(select(func.count(JuryReview.id)).where(*clauses))).scalar_one() or 0)
    reviews = list((await db.execute(select(JuryReview).where(*clauses).order_by(JuryReview.id.desc()).offset(offset).limit(limit))).scalars().all())
    return await admin_reads(db, reviews), total


async def admin_reads(db: AsyncSession, reviews: list[JuryReview]) -> list[dict[str, Any]]:
    """后台看到的复审：处罚 / 案件、申诉或复议的理由、每位复审委员的票（实名）、结果。"""
    if not reviews:
        return []
    review_ids = [review.id for review in reviews]
    vote_rows = (
        await db.execute(
            select(JuryReviewVote, User, JuryMember.tier)
            .join(User, User.id == JuryReviewVote.juror_id)
            .outerjoin(JuryMember, JuryMember.user_id == JuryReviewVote.juror_id)
            .where(JuryReviewVote.review_id.in_(review_ids))
            .order_by(JuryReviewVote.id.asc())
        )
    ).all()
    votes: dict[int, list[dict[str, Any]]] = {review_id: [] for review_id in review_ids}
    for assignment, juror, tier in vote_rows:
        votes[assignment.review_id].append(
            {
                "juror": juror,
                "tier": tier,
                # 分到手还没投的为 null
                "vote": assignment.vote,
                "level": assignment.level,
                "category": assignment.category,
                "note": assignment.note,
                "assigned_at": penalty_service.aware(assignment.assigned_at),
                "voted_at": penalty_service.aware(assignment.voted_at),
            }
        )
    requesters = {
        user.id: user for user in (await db.execute(select(User).where(User.id.in_({review.requester_id for review in reviews})))).scalars().all()
    }
    items: list[dict[str, Any]] = []
    for review in reviews:
        summary = await _summary(db, review)
        violation = await db.get(UserViolation, review.violation_id) if review.violation_id is not None else None
        items.append(
            {
                "id": review.id,
                "kind": review.kind,
                "stage": review.stage,
                "status": review.status,
                "outcome": review.outcome,
                "decided_by": review.decided_by,
                "escalation": review.escalation,
                "violation_id": review.violation_id,
                "case_id": review.case_id,
                "report_id": review.report_id,
                "requester": requesters.get(review.requester_id),
                # 申诉理由 / 复议理由
                "text": violation.appeal_text if violation is not None else review.text,
                "target_type": summary["kind"],
                "source": summary["source"],
                "category": summary["category"],
                "category_label": summary["category_label"],
                "level": summary["level"],
                "penalty": summary["penalty"],
                "created_at": penalty_service.aware(review.created_at),
                "expires_at": penalty_service.aware(review.expires_at),
                "escalated_at": penalty_service.aware(review.escalated_at),
                "closed_at": penalty_service.aware(review.closed_at),
                "votes": votes[review.id],
            }
        )
    return items


async def admin_decide(
    db: AsyncSession, admin: User, review_id: int, action: str, *, level: int | None = None, category: str | None = None, note: str | None = None
) -> dict[str, Any]:
    """POST /admin/jury/reviews/{review_id}/decide：管理员给一件举报人复议出结果。

    uphold：维持原结论（不违规）。overturn：改判为违规，走改判那一套（补记处罚、处理内容、同步举报和委员的一致率）。
    转给管理员的（凑不齐人 / 超时）和还在资深阶段的都可以处理，后者等于管理员直接接手，资深委员没投完的票作废。
    没有这个接口时，转给管理员的复议只能用改判接口处理，想维持原结论就没有出口，会一直是 pending。
    申诉复审不在这里处理：用原有的 POST /admin/violations/{id}/appeal（那边会连带了结复审）。
    """
    review = await db.get(JuryReview, review_id)
    if review is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"code": "review_not_found", "message": "复审不存在"})
    if review.kind != "reconsider":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "appeal_review",
                "message": f"这是一件申诉复审，请用 POST /admin/violations/{review.violation_id}/appeal 处理申诉",
                "violation_id": review.violation_id,
            },
        )
    if action not in RECONSIDER_VOTES:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail={"code": "invalid_action", "message": "处理结果只能是 uphold 或 overturn"})
    if level is not None and (isinstance(level, bool) or not 1 <= level <= 3):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail={"code": "invalid_level", "message": "等级只能是 1–3"})
    if category is not None and category not in penalty_service.CATEGORIES:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail={"code": "invalid_category", "message": "未知的违规类别"})
    if review.status != "open":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={"code": "review_closed", "message": "这件复议已经有结果了"})
    case = await db.get(ReportCase, review.case_id) if review.case_id is not None else None
    if case is None or case.status != "dismissed":
        # 正常不会走到这里：案件被改判时，它的复议会一起了结（on_case_overturned）
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={"code": "reconsider_unavailable", "message": "案件现在的结论已经不是「不违规」，没有什么可复议的"})
    now = datetime.now(UTC)
    text = note.strip()[:2000] if note and note.strip() else None
    was_senior = review.stage == "senior"
    if action == "overturn":
        # 改判会提交事务、通知举报人（report.resolved）和投过票的委员；on_case_overturned 在同一个事务里把这件复议记成 overturn / admin
        await case_service.overturn_case(
            db, case.id, actor_id=admin.id, upheld=True, category=category, level=level, note=text or "举报人申请复议，管理员复核后改判为违规"
        )
    else:
        review.status = "closed"
        review.outcome = "uphold"
        review.decided_by = "admin"
        review.closed_at = now
        _notify_requester(db, review.requester_id, "reconsider_result", "你申请的复议有结果了", RECONSIDER_UPHELD_BY_ADMIN)
    db.add(
        AdminAuditLog(
            actor_id=admin.id,
            action="jury.review.decide",
            target_type="jury_review",
            target_id=review.id,
            details=json.dumps(
                {"kind": review.kind, "case_id": case.id, "action": action, "stage": "senior" if was_senior else "admin", "level": level, "category": category, "note": text},
                ensure_ascii=False,
                sort_keys=True,
            ),
        )
    )
    await db.commit()
    if action == "uphold":
        await _notify_reconsider_upheld(db, review, case, RECONSIDER_UPHELD_BY_ADMIN)
    if was_senior:
        # 资深委员那边的待审件数变了
        await case_service.notify_jury_queue_changed(db)
    await db.refresh(review)
    return (await admin_reads(db, [review]))[0]
