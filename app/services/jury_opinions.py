"""案件观点区：投过票的委员在案件下面交换看法。

展示时只有代号，没有真实身份。这里不过机器文本审核 —— 讨论违规内容免不了转述，
机器会大量误拦；改为委员互相举报，同一条被多人举报后自动折叠，等管理员处理。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.case import JuryMember, JuryOpinion, JuryOpinionLike, JuryOpinionReport, JuryVote, ReportCase
from app.models.report import AdminAuditLog
from app.models.user import User
from app.services import jury as jury_service
from app.services import jury_settings
from app.services import penalties as penalty_service

logger = logging.getLogger(__name__)
UTC = timezone.utc

MAX_LENGTH = settings.jury_opinion_max_length
# 每人每个案件最多发多少条（含回复、含投票时的备注）
PER_CASE = settings.jury_opinions_per_case
PER_MINUTE = settings.jury_opinions_per_minute
PER_DAY = settings.jury_opinions_per_day
# 同一条观点被这么多名委员举报后自动折叠
AUTO_HIDE_REPORTS = settings.jury_opinion_auto_hide_reports
OTHER_REASON = "其他"
REPORT_REASONS = ["人身攻击", "泄露身份或隐私", "照抄违规原文", "与案件无关", OTHER_REASON]
OTHER_REASON_MIN_DETAILS = 5
REPORT_DETAILS_MAX_LENGTH = 500


def _not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"code": "opinion_not_found", "message": "观点不存在"})


# ---------------------------------------------------------------- 权限


async def _reader(db: AsyncSession, user: User, case_id: int) -> tuple[ReportCase, JuryVote | None]:
    """读权限：对这个案件投过票（含弃权）。返回的票为 None 表示是靠开关进来的只读访客。"""
    case = await db.get(ReportCase, case_id)
    if case is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"code": "case_not_found", "message": "案件不存在"})
    vote = await jury_service.my_vote(db, user.id, case_id)
    if vote is None:
        # 开关打开后，已定案案件的观点区对所有在任委员只读；和这个案件有回避关系的仍然不行。
        if (
            case.status != "open"
            and case.jury_eligible
            and await jury_settings.is_enabled(db, "opinions_open_to_jurors")
            and jury_service._is_serving(await jury_service.get_member(db, user.id))
            and not await jury_service._has_conflict(db, user.id, case)
        ):
            return case, None
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail={"code": "not_voted", "message": "投票之后才能进入这个案件的观点区"})
    if vote.is_quality_check:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail={"code": "opinions_closed", "message": "质控题没有观点区"})
    return case, vote


async def _writer(db: AsyncSession, user: User, case_id: int) -> ReportCase:
    """写权限：投过票，并且现在是在任委员、没有受处罚。"""
    case, vote = await _reader(db, user, case_id)
    if vote is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail={"code": "not_voted", "message": "只有对这个案件投过票的委员可以参与讨论"})
    await jury_service._require_serving(db, user)
    return case


async def _opinion(db: AsyncSession, user: User, opinion_id: int, *, write: bool) -> tuple[JuryOpinion, ReportCase]:
    opinion = await db.get(JuryOpinion, opinion_id)
    if opinion is None:
        raise _not_found()
    try:
        case = await _writer(db, user, opinion.case_id) if write else (await _reader(db, user, opinion.case_id))[0]
    except HTTPException as error:
        # 看不到的案件下面的观点，对外就当不存在。
        if isinstance(error.detail, dict) and error.detail.get("code") in {"not_voted", "opinions_closed", "case_not_found"}:
            raise _not_found() from None
        raise
    return opinion, case


# ---------------------------------------------------------------- 展示


async def _views(db: AsyncSession, case: ReportCase, opinions: list[JuryOpinion], viewer_id: int) -> dict[int, dict[str, Any]]:
    """把观点转成给委员看的结构（不含 replies），按 id 索引。"""
    if not opinions:
        return {}
    opinion_ids = [item.id for item in opinions]
    author_ids = {item.author_id for item in opinions}
    votes = await db.execute(
        select(JuryVote.juror_id, JuryVote.alias, JuryVote.vote).where(JuryVote.case_id == case.id, JuryVote.juror_id.in_(author_ids))
    )
    voters = {juror_id: (alias, vote) for juror_id, alias, vote in votes.all()}
    tiers = dict((await db.execute(select(JuryMember.user_id, JuryMember.tier).where(JuryMember.user_id.in_(author_ids)))).all())
    liked = set(
        (await db.execute(select(JuryOpinionLike.opinion_id).where(JuryOpinionLike.user_id == viewer_id, JuryOpinionLike.opinion_id.in_(opinion_ids)))).scalars()
    )
    reported = set(
        (
            await db.execute(
                select(JuryOpinionReport.opinion_id).where(JuryOpinionReport.reporter_id == viewer_id, JuryOpinionReport.opinion_id.in_(opinion_ids))
            )
        ).scalars()
    )
    # 被回复的那条可能不在这一批里（单条返回时）
    by_id = {item.id: item for item in opinions}
    missing = {item.reply_to_id for item in opinions if item.reply_to_id is not None} - set(by_id)
    reply_authors = {item.id: item.author_id for item in opinions}
    if missing:
        rows = await db.execute(select(JuryOpinion.id, JuryOpinion.author_id).where(JuryOpinion.id.in_(missing)))
        reply_authors.update(dict(rows.all()))
        extra = set(reply_authors.values()) - set(voters)
        if extra:
            more = await db.execute(select(JuryVote.juror_id, JuryVote.alias, JuryVote.vote).where(JuryVote.case_id == case.id, JuryVote.juror_id.in_(extra)))
            voters.update({juror_id: (alias, vote) for juror_id, alias, vote in more.all()})
    closed = case.status != "open"
    views: dict[int, dict[str, Any]] = {}
    for item in opinions:
        alias, vote = voters.get(item.author_id, (None, None))
        reply_author = reply_authors.get(item.reply_to_id) if item.reply_to_id is not None else None
        views[item.id] = {
            "id": item.id,
            "parent_id": item.parent_id,
            "alias": alias,
            "tier": tiers.get(item.author_id),
            # 定案前不透露谁投了什么
            "vote": vote if closed else None,
            "content": item.content if item.state == "visible" else None,
            "state": item.state,
            "is_note": item.is_note,
            "mine": item.author_id == viewer_id,
            "like_count": item.like_count,
            "liked": item.id in liked,
            "reported": item.id in reported,
            "created_at": penalty_service.aware(item.created_at),
            "reply_to_alias": voters.get(reply_author, (None, None))[0] if reply_author is not None else None,
            "replies": [],
        }
    return views


async def list_opinions(db: AsyncSession, user: User, case_id: int) -> dict[str, Any]:
    """一次返回全部，按时间正序；回复挂在所属的根观点下面，只有一层。"""
    case, _vote = await _reader(db, user, case_id)
    opinions = list(
        (await db.execute(select(JuryOpinion).where(JuryOpinion.case_id == case_id).order_by(JuryOpinion.created_at.asc(), JuryOpinion.id.asc()))).scalars()
    )
    views = await _views(db, case, opinions, user.id)
    roots: list[dict[str, Any]] = []
    for item in opinions:
        # 已删除的回复直接不出现
        if item.parent_id is not None and item.state != "deleted" and item.parent_id in views:
            views[item.parent_id]["replies"].append(views[item.id])
    for item in opinions:
        # 已删除的根观点：下面还有回复就留一个占位，没有就不出现
        if item.parent_id is None and (item.state != "deleted" or views[item.id]["replies"]):
            roots.append(views[item.id])
    return {"items": roots, "total": sum(1 for item in opinions if item.state != "deleted")}


async def _single_view(db: AsyncSession, case: ReportCase, opinion: JuryOpinion, viewer_id: int) -> dict[str, Any]:
    return (await _views(db, case, [opinion], viewer_id))[opinion.id]


# ---------------------------------------------------------------- 发表 / 删除


async def _check_rate(db: AsyncSession, user_id: int, case_id: int) -> None:
    now = datetime.now(UTC)

    async def count(*clauses: Any) -> int:
        result = await db.execute(select(func.count()).select_from(JuryOpinion).where(JuryOpinion.author_id == user_id, *clauses))
        return int(result.scalar_one() or 0)

    def limited(message: str) -> HTTPException:
        return HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail={"code": "opinion_rate_limited", "message": message})

    # 删掉的也算，否则可以靠删了重发绕过去。
    if await count(JuryOpinion.case_id == case_id) >= PER_CASE:
        raise limited(f"你在这个案件下已经发满 {PER_CASE} 条了")
    # 投票时带的备注不占用发言频率。
    if await count(JuryOpinion.is_note.is_(False), JuryOpinion.created_at >= now - timedelta(minutes=1)) >= PER_MINUTE:
        raise limited("发得太快了，歇一会儿再发")
    if await count(JuryOpinion.is_note.is_(False), JuryOpinion.created_at >= now - timedelta(hours=24)) >= PER_DAY:
        raise limited(f"今天的发言已达上限（{PER_DAY} 条），明天再来")


async def create_opinion(db: AsyncSession, user: User, case_id: int, content: str | None, parent_id: int | None) -> dict[str, Any]:
    case = await _writer(db, user, case_id)
    text = (content or "").strip()
    if not text or len(text) > MAX_LENGTH:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "invalid_opinion", "message": f"观点不能为空，也不能超过 {MAX_LENGTH} 个字"},
        )
    root_id: int | None = None
    reply_to_id: int | None = None
    if parent_id is not None:
        parent = await db.get(JuryOpinion, parent_id)
        if parent is None or parent.case_id != case_id or parent.state == "deleted":
            raise _not_found()
        # 回复只有一层：回复一条回复时挂到同一条根观点下面，另外记下回复的是谁。
        root_id = parent.parent_id or parent.id
        reply_to_id = parent.id if parent.parent_id is not None else None
    await _check_rate(db, user.id, case_id)
    opinion = JuryOpinion(case_id=case_id, author_id=user.id, parent_id=root_id, reply_to_id=reply_to_id, content=text, created_at=datetime.now(UTC))
    db.add(opinion)
    await db.commit()
    await jury_service.notify_case_voters(
        db, case_id, "jury.opinion.created", {"case_id": case_id, "opinion_id": opinion.id, "parent_id": root_id}, exclude=user.id
    )
    return await _single_view(db, case, opinion, user.id)


async def _notify_changed(db: AsyncSession, opinion: JuryOpinion) -> None:
    await jury_service.notify_case_voters(
        db, opinion.case_id, "jury.opinion.changed", {"case_id": opinion.case_id, "opinion_id": opinion.id, "state": opinion.state}
    )


async def delete_opinion(db: AsyncSession, user: User, opinion_id: int) -> dict[str, Any]:
    """本人删除。被停职 / 卸任的前委员也可以删自己以前发的。"""
    opinion = await db.get(JuryOpinion, opinion_id)
    if opinion is None:
        raise _not_found()
    if opinion.author_id != user.id:
        # 看得到这条观点的人会被告知不能删；看不到的只知道不存在。
        await _opinion(db, user, opinion_id, write=False)
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail={"code": "not_opinion_author", "message": "只能删除自己发的观点"})
    if opinion.state != "deleted":
        opinion.state = "deleted"
        opinion.deleted_at = datetime.now(UTC)
        opinion.deleted_by_id = user.id
        await db.commit()
        await _notify_changed(db, opinion)
    return {"id": opinion.id, "state": opinion.state}


# ---------------------------------------------------------------- 点赞 / 举报


async def set_like(db: AsyncSession, user: User, opinion_id: int, liked: bool) -> dict[str, Any]:
    """点赞 / 取消点赞，重复操作是幂等的。"""
    opinion, _case = await _opinion(db, user, opinion_id, write=True)
    if opinion.author_id == user.id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail={"code": "cannot_like_own", "message": "不能给自己的观点点赞"})
    if liked and opinion.state != "visible":
        raise _not_found()
    existing = (
        await db.execute(select(JuryOpinionLike).where(JuryOpinionLike.opinion_id == opinion_id, JuryOpinionLike.user_id == user.id))
    ).scalar_one_or_none()
    try:
        if liked and existing is None:
            db.add(JuryOpinionLike(opinion_id=opinion_id, user_id=user.id))
        elif not liked and existing is not None:
            await db.execute(delete(JuryOpinionLike).where(JuryOpinionLike.id == existing.id))
        await db.flush()
        total = await db.execute(select(func.count()).select_from(JuryOpinionLike).where(JuryOpinionLike.opinion_id == opinion_id))
        await db.execute(update(JuryOpinion).where(JuryOpinion.id == opinion_id).values(like_count=int(total.scalar_one() or 0)))
        await db.commit()
    except IntegrityError:
        # 同一个人的两次点赞同时到达：另一次已经记上了。
        await db.rollback()
    await db.refresh(opinion)
    return {"id": opinion.id, "like_count": opinion.like_count, "liked": liked}


async def report_opinion(db: AsyncSession, user: User, opinion_id: int, reason: str | None, details: str | None) -> dict[str, Any]:
    opinion, _case = await _opinion(db, user, opinion_id, write=True)
    if opinion.state == "deleted":
        raise _not_found()
    if opinion.author_id == user.id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail={"code": "cannot_report_own", "message": "不能举报自己的观点"})
    reason = (reason or "").strip()
    text = (details or "").strip()
    if reason not in REPORT_REASONS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "invalid_reason", "message": "请选择一个举报理由", "reasons": REPORT_REASONS},
        )
    if (reason == OTHER_REASON and len(text) < OTHER_REASON_MIN_DETAILS) or len(text) > REPORT_DETAILS_MAX_LENGTH:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "invalid_reason",
                "message": f"选择「其他」时请写明具体情况（至少 {OTHER_REASON_MIN_DETAILS} 个字）；说明最多 {REPORT_DETAILS_MAX_LENGTH} 个字",
            },
        )
    already = HTTPException(status_code=status.HTTP_409_CONFLICT, detail={"code": "already_reported", "message": "你已经举报过这条观点了"})
    existing = await db.execute(
        select(JuryOpinionReport.id).where(JuryOpinionReport.opinion_id == opinion_id, JuryOpinionReport.reporter_id == user.id).limit(1)
    )
    if existing.scalar_one_or_none() is not None:
        raise already
    db.add(JuryOpinionReport(opinion_id=opinion_id, reporter_id=user.id, reason=reason, details=text or None))
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise already from None
    pending = await db.execute(
        select(func.count()).select_from(JuryOpinionReport).where(JuryOpinionReport.opinion_id == opinion_id, JuryOpinionReport.status == "pending")
    )
    hidden = opinion.state == "visible" and int(pending.scalar_one() or 0) >= AUTO_HIDE_REPORTS
    if hidden:
        opinion.state = "hidden"
    await db.commit()
    if hidden:
        await _notify_changed(db, opinion)
    await _broadcast_admin(opinion, "reported")
    return {"id": opinion.id, "state": opinion.state}


async def _broadcast_admin(opinion: JuryOpinion, action: str) -> None:
    from app.websocket.manager import admin_manager

    try:
        await admin_manager.broadcast(
            "admin.jury.opinion.changed", {"opinion_id": opinion.id, "case_id": opinion.case_id, "state": opinion.state, "action": action}
        )
    except Exception:
        logger.exception("Failed to broadcast opinion change", extra={"opinion_id": opinion.id})


# ---------------------------------------------------------------- 后台


async def admin_reads(db: AsyncSession, opinions: list[JuryOpinion]) -> list[dict[str, Any]]:
    """后台视图：真实作者、原文和举报记录。"""
    if not opinions:
        return []
    opinion_ids = [item.id for item in opinions]
    reports = list(
        (await db.execute(select(JuryOpinionReport).where(JuryOpinionReport.opinion_id.in_(opinion_ids)).order_by(JuryOpinionReport.id.asc()))).scalars()
    )
    user_ids = {item.author_id for item in opinions} | {report.reporter_id for report in reports}
    users = {user.id: user for user in (await db.execute(select(User).where(User.id.in_(user_ids)))).scalars()}
    tiers = dict((await db.execute(select(JuryMember.user_id, JuryMember.tier).where(JuryMember.user_id.in_(user_ids)))).all())
    aliases = {
        (case_id, juror_id): alias
        for case_id, juror_id, alias in (
            await db.execute(select(JuryVote.case_id, JuryVote.juror_id, JuryVote.alias).where(JuryVote.case_id.in_({item.case_id for item in opinions})))
        ).all()
    }
    by_opinion: dict[int, list[JuryOpinionReport]] = {}
    for report in reports:
        by_opinion.setdefault(report.opinion_id, []).append(report)
    items: list[dict[str, Any]] = []
    for item in opinions:
        own = by_opinion.get(item.id, [])
        items.append(
            {
                "id": item.id,
                "case_id": item.case_id,
                "parent_id": item.parent_id,
                "reply_to_id": item.reply_to_id,
                "author_id": item.author_id,
                "author": users.get(item.author_id),
                "alias": aliases.get((item.case_id, item.author_id)),
                "tier": tiers.get(item.author_id),
                "content": item.content,
                "state": item.state,
                "is_note": item.is_note,
                "like_count": item.like_count,
                "created_at": penalty_service.aware(item.created_at),
                "deleted_at": penalty_service.aware(item.deleted_at),
                "deleted_by_id": item.deleted_by_id,
                "moderation_note": item.moderation_note,
                "pending_reports": sum(1 for report in own if report.status == "pending"),
                "reports": [
                    {
                        "id": report.id,
                        "reporter": users.get(report.reporter_id),
                        "reason": report.reason,
                        "details": report.details,
                        "status": report.status,
                        "created_at": penalty_service.aware(report.created_at),
                    }
                    for report in own
                ],
            }
        )
    return items


async def admin_list(
    db: AsyncSession,
    *,
    state: str | None = None,
    reported: bool | None = None,
    case_id: int | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    clauses = []
    if state:
        clauses.append(JuryOpinion.state == state)
    if case_id is not None:
        clauses.append(JuryOpinion.case_id == case_id)
    if reported is not None:
        # reported=true：有还没处理的举报
        pending = select(JuryOpinionReport.opinion_id).where(JuryOpinionReport.status == "pending")
        clauses.append(JuryOpinion.id.in_(pending) if reported else JuryOpinion.id.not_in(pending))
    total = int((await db.execute(select(func.count(JuryOpinion.id)).where(*clauses))).scalar_one() or 0)
    rows = await db.execute(select(JuryOpinion).where(*clauses).order_by(JuryOpinion.id.desc()).offset(offset).limit(limit))
    return await admin_reads(db, list(rows.scalars())), total


async def case_opinions(db: AsyncSession, case_id: int) -> list[dict[str, Any]]:
    rows = await db.execute(select(JuryOpinion).where(JuryOpinion.case_id == case_id).order_by(JuryOpinion.id.asc()))
    return await admin_reads(db, list(rows.scalars()))


async def admin_moderate(db: AsyncSession, opinion_id: int, *, action: str, actor_id: int, note: str | None) -> dict[str, Any]:
    """delete 删除 / restore 恢复 / dismiss_reports 驳回举报（被折叠的会重新显示）。"""
    opinion = await db.get(JuryOpinion, opinion_id)
    if opinion is None:
        raise _not_found()
    now = datetime.now(UTC)
    before = opinion.state
    text = note.strip()[:500] if note else None
    if action == "delete":
        opinion.state = "deleted"
        opinion.deleted_at = now
        opinion.deleted_by_id = actor_id
        report_status = "handled"
    elif action in {"restore", "dismiss_reports"}:
        if action == "restore" and opinion.state == "deleted" and opinion.deleted_by_id == opinion.author_id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail={"code": "deleted_by_author", "message": "这条观点是作者自己删除的，不能恢复"}
            )
        # 驳回举报不会把已经删除的观点放回来。
        if action == "restore" or opinion.state == "hidden":
            opinion.state = "visible"
            opinion.deleted_at = None
            opinion.deleted_by_id = None
        report_status = "dismissed"
    else:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail={"code": "invalid_action", "message": "未知的处置动作"})
    if text is not None:
        opinion.moderation_note = text
    await db.execute(
        update(JuryOpinionReport)
        .where(JuryOpinionReport.opinion_id == opinion_id, JuryOpinionReport.status == "pending")
        .values(status=report_status, handled_by_id=actor_id, handled_at=now)
    )
    db.add(
        AdminAuditLog(
            actor_id=actor_id,
            action=f"jury.opinion.{action}",
            target_type="jury_opinion",
            target_id=opinion.id,
            details=json.dumps({"case_id": opinion.case_id, "from": before, "to": opinion.state, "note": text}, ensure_ascii=False),
        )
    )
    await db.commit()
    if opinion.state != before:
        await _notify_changed(db, opinion)
    await _broadcast_admin(opinion, action)
    return (await admin_reads(db, [opinion]))[0]
