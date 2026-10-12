"""资深合议（docs/jury-v2.md 3.1）。

委员没能定案的案件（无共识、上下文不足、超时）先交给三位资深委员：两票一致即定案，48 小时没结果再转管理员。
合议委员的分配记录和原审的在同一张表里（jury_votes，stage = "panel"），所以评审编号、案件详情、观点区、
「我的评审」都照常工作；这里只管合议自己的分案、投票和超时。「进不进合议」由 cases.escalate_from_jury 决定。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.case import JuryMember, JuryOpinion, JuryVote, ReportCase
from app.models.user import User
from app.services import cases as case_service
from app.services import jury as jury_service
from app.services import jury_opinions as opinion_service
from app.services import jury_points as points_service
from app.services import penalties as penalty_service

logger = logging.getLogger(__name__)
UTC = timezone.utc

# 席位数和「几票一致即定案」是规则本身，不是可调的阈值：三个席位、两种立场（违规 / 不违规），所以投满必然有结果。
PANEL_SEATS = 3
PANEL_CONSENSUS_VOTES = 2
PANEL_TIMEOUT_HOURS = settings.jury_panel_timeout_hours
# 合议不能弃权，也没有「上下文不足」
PANEL_VOTES = ("none", "l1", "l2", "l3", "recuse")


def not_senior() -> HTTPException:
    return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail={"code": "not_senior", "message": "只有在任的资深委员可以参加"})


# ---------------------------------------------------------------- 谁能参加


async def require_senior(db: AsyncSession, user: User) -> JuryMember:
    """在任的资深委员，且没有受处罚。合议、申诉复审、复议都用这一条。"""
    member = await jury_service.get_member(db, user.id)
    if await jury_service.ensure_term(db, member, user):
        await db.commit()
        await points_service.flush_events(db)
    if not jury_service._is_serving(member) or member is None or member.tier != "senior":
        raise not_senior()
    if jury_service.restriction(user) is not None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail={"code": "juror_restricted", "message": jury_service.RESTRICTED_MESSAGE})
    return member


async def serving_seniors(db: AsyncSession, now: datetime) -> list[int]:
    """此刻在任、没有受处罚的资深委员。"""
    rows = await db.execute(
        select(User)
        .join(JuryMember, JuryMember.user_id == User.id)
        .where(JuryMember.status == "active", JuryMember.tier == "senior", or_(JuryMember.term_ends_at.is_(None), JuryMember.term_ends_at > now))
        .order_by(User.id.asc())
    )
    return [user.id for user in rows.scalars().all() if jury_service.restriction(user, now) is None]


async def available_seniors(db: AsyncSession, target: Any, now: datetime, *, taken: set[int], speakers: set[Any] | None = None) -> list[int]:
    """此刻能参加的资深委员：在任、没受处罚、通过回避规则、不在 taken 里。

    target 是案件，或者长得像案件的东西（没有案件的机器处罚，见 jury_reviews）；
    speakers 给了就用它当「在上下文里说过话的人」，不再去读案件快照。
    """
    found: list[int] = []
    for user_id in await serving_seniors(db, now):
        if user_id in taken:
            continue
        context = await jury_service._conflict_context(db, user_id)
        if speakers is not None:
            context["speakers"][target.id] = speakers
        if not await jury_service._has_conflict(db, user_id, target, context):
            found.append(user_id)
    return found


async def case_participants(db: AsyncSession, case_id: int) -> set[int]:
    """在这个案件上分到过的人：投过票的、回避过的、分到手没投的都算。他们不能再参加它的合议 / 复审。"""
    rows = await db.execute(select(JuryVote.juror_id).where(JuryVote.case_id == case_id))
    return set(rows.scalars().all())


async def can_convene(db: AsyncSession, case: ReportCase, now: datetime) -> bool:
    """此刻凑不凑得齐合议庭。凑不齐的案件不进合议，免得白等一轮。"""
    if not case.jury_eligible or case.jury_block_reason is not None:
        return False
    return len(await available_seniors(db, case, now, taken=await case_participants(db, case.id))) >= PANEL_SEATS


async def _seats(db: AsyncSession, case_id: int, now: datetime) -> tuple[int, int]:
    """(已经投出的合议票数, 还在有效期内没投的分配数)。回避的不占席位。"""
    fresh_after = now - timedelta(hours=jury_service.ASSIGNMENT_TTL_HOURS)
    rows = await db.execute(
        select(JuryVote.voted_at, JuryVote.assigned_at).where(JuryVote.case_id == case_id, JuryVote.stage == "panel", JuryVote.recused_at.is_(None))
    )
    voted = pending = 0
    for voted_at, assigned_at in rows.all():
        if voted_at is not None:
            voted += 1
        elif penalty_service.aware(assigned_at) >= fresh_after:
            pending += 1
    return voted, pending


async def _still_viable(db: AsyncSession, case: ReportCase, now: datetime) -> bool:
    """这个合议还坐不坐得满：已投的 + 分到手的 + 还能再分给的人 >= 席位数。"""
    voted, pending = await _seats(db, case.id, now)
    if voted + pending >= PANEL_SEATS:
        return True
    free = await available_seniors(db, case, now, taken=await case_participants(db, case.id))
    return voted + pending + len(free) >= PANEL_SEATS


# ---------------------------------------------------------------- 分案


async def _assignable(db: AsyncSession, juror_id: int, now: datetime, *, limit: int) -> list[ReportCase]:
    """能分给这位资深委员的合议案件：没在这个案件上分到过、通过回避规则、席位没坐满。先到期的先分。"""
    mine = select(JuryVote.case_id).where(JuryVote.juror_id == juror_id)
    rows = await db.execute(
        select(ReportCase)
        .where(ReportCase.status == "open", ReportCase.queue == "panel", ReportCase.id.not_in(mine))
        .order_by(ReportCase.priority.desc(), ReportCase.panel_at.asc(), ReportCase.id.asc())
        .limit(jury_service.PICK_SCAN_LIMIT)
    )
    context = await jury_service._conflict_context(db, juror_id)
    found: list[ReportCase] = []
    for case in rows.scalars().all():
        if await jury_service._has_conflict(db, juror_id, case, context):
            continue
        voted, pending = await _seats(db, case.id, now)
        if voted + pending >= PANEL_SEATS:
            continue
        found.append(case)
        if len(found) >= limit:
            break
    return found


async def _pending(db: AsyncSession, juror_id: int, now: datetime) -> list[tuple[JuryVote, ReportCase]]:
    """已经分到手、还没投的合议案件。"""
    rows = await db.execute(
        select(JuryVote, ReportCase)
        .join(ReportCase, ReportCase.id == JuryVote.case_id)
        .where(
            JuryVote.juror_id == juror_id,
            JuryVote.stage == "panel",
            JuryVote.voted_at.is_(None),
            JuryVote.recused_at.is_(None),
            JuryVote.assigned_at >= now - timedelta(hours=jury_service.ASSIGNMENT_TTL_HOURS),
            ReportCase.status == "open",
            ReportCase.queue == "panel",
        )
        .order_by(JuryVote.id.asc())
    )
    return [(assignment, case) for assignment, case in rows.all()]


async def available_count(db: AsyncSession, user: User) -> int:
    """/jury/me.senior.panel：现在能分给我的合议件数（分到手没投的 + 还能新分到的）。"""
    now = datetime.now(UTC)
    if jury_service.restriction(user, now) is not None:
        return 0
    count = len(await _pending(db, user.id, now))
    if count < jury_service.AVAILABLE_CASES_CAP:
        count += len(await _assignable(db, user.id, now, limit=jury_service.AVAILABLE_CASES_CAP - count))
    return min(count, jury_service.AVAILABLE_CASES_CAP)


async def panel_case_view(db: AsyncSession, case: ReportCase, assignment: JuryVote, now: datetime | None = None) -> dict[str, Any]:
    """PanelCaseView：JuryCaseView 加上原审的情况（为什么转出、票数分布、观点）和合议的进度。"""
    now = now or datetime.now(UTC)
    voted, _pending_seats = await _seats(db, case.id, now)
    panel_at = penalty_service.aware(case.panel_at)
    return {
        **await jury_service.juror_case_view(db, case, assignment),
        "origin": {
            "reason": case.jury_outcome,
            "tally": (await jury_service.vote_tallies(db, [case.id]))[case.id],
            "opinions": (await opinion_service.origin_page(db, case, assignment.juror_id))["items"],
        },
        "panel": {
            "seats": PANEL_SEATS,
            "votes_cast": voted,
            "expires_at": panel_at + timedelta(hours=PANEL_TIMEOUT_HOURS) if panel_at is not None else None,
        },
    }


async def next_case(db: AsyncSession, user: User) -> dict[str, Any] | None:
    """GET /jury/panel/next：取下一个合议案件；没有返回 None。重复调用返回同一个没投的案件。"""
    await require_senior(db, user)
    now = datetime.now(UTC)
    for assignment, case in await _pending(db, user.id, now):
        return await panel_case_view(db, case, assignment, now)
    cases = await _assignable(db, user.id, now, limit=1)
    if not cases:
        return None
    assignment = JuryVote(case_id=cases[0].id, juror_id=user.id, assigned_at=now, stage="panel")
    db.add(assignment)
    await db.commit()
    return await panel_case_view(db, cases[0], assignment, now)


# ---------------------------------------------------------------- 投票与定案


def panel_tally(votes: list[str]) -> tuple[str, int | None] | None:
    """返回 ("violation", 等级) / ("clean", None)；还没有两票一致返回 None。

    一致性看的是「违规 / 不违规」，等级取违规票的较低中位数。原审里「严重违规要 5 票」不适用：合议只有三席。
    """
    levels = sorted(jury_service._VOTE_LEVEL[vote] for vote in votes if vote in jury_service._VOTE_LEVEL)
    clean = sum(1 for vote in votes if vote == "none")
    if clean >= PANEL_CONSENSUS_VOTES:
        return "clean", None
    if len(levels) >= PANEL_CONSENSUS_VOTES:
        return "violation", levels[(len(levels) - 1) // 2]
    return None


async def _recuse(db: AsyncSession, user: User, member: JuryMember, case: ReportCase, assignment: JuryVote, note: str | None, now: datetime) -> dict[str, Any]:
    """合议里主动回避：席位让给别的资深委员；没有别人可让了，案件就转管理员。和原审共用每天的回避次数。"""
    if await jury_service._recuses_since(db, user.id, now - timedelta(hours=24)) >= jury_service.RECUSE_DAILY_LIMIT:
        resets_at = await jury_service._recuse_resets_at(db, user.id, now)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "code": "recuse_limit",
                "message": f"今天的回避次数已用完（每天 {jury_service.RECUSE_DAILY_LIMIT} 次）",
                "resets_at": jury_service._iso(resets_at) if resets_at is not None else None,
            },
        )
    assignment.vote = "recuse"
    assignment.note = note
    assignment.recused_at = now
    await db.flush()
    left = case.status == "open" and case.queue == "panel" and not await _still_viable(db, case, now)
    if left:
        await case_service.leave_panel(db, case, now, "unavailable")
    await db.commit()
    if left:
        await case_service.notify_escalated(db, case)
    else:
        await case_service.notify_jury_queue_changed(db)
    return {"case_id": assignment.id, "settled": False, "case": None, "reward": points_service.reward_view(member, [])}


async def cast_vote(db: AsyncSession, user: User, review_id: int, vote: str, note: str | None, category: str | None = None) -> dict[str, Any]:
    """POST /jury/panel/{case_id}/vote。review_id 是评审编号（GET /jury/panel/next 给的 case_id）。响应和普通投票同形。"""
    if vote not in PANEL_VOTES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "invalid_vote", "message": "资深合议只能投不违规、违规（轻微 / 一般 / 严重）或回避"},
        )
    category = jury_service._vote_category(vote, category)
    member = await require_senior(db, user)
    case, assignment = await jury_service.resolve_review(db, user.id, review_id)
    if assignment.stage != "panel":
        raise jury_service._review_not_found()
    if assignment.voted_at is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={"code": "already_voted", "message": "你已经投过票了"})
    if assignment.recused_at is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={"code": "already_recused", "message": "你已经回避了这个案件"})
    now = datetime.now(UTC)
    text = note.strip()[:500] if note else None
    if vote == "recuse":
        return await _recuse(db, user, member, case, assignment, text or None, now)
    assignment.vote = vote
    assignment.note = text
    assignment.category = category
    assignment.voted_at = now
    member.votes_total += 1
    rewards: list[Any] = await jury_service._daily_rewards(db, member, now)
    assignment.alias = await jury_service._next_alias(db, case.id)
    opinion: JuryOpinion | None = None
    if assignment.note:
        # 和原审一样：投票时写的备注就是这位委员在观点区的第一条观点。
        opinion = JuryOpinion(case_id=case.id, author_id=user.id, content=assignment.note, is_note=True, created_at=now)
        db.add(opinion)
    await db.flush()
    await points_service.check_achievements(db, user.id, member)
    points_service.reset_leaderboard_cache()

    if case.status != "open" or case.queue != "panel":
        # 投票期间案件已经定案或被转走：这一票照常记，已有结论的话按结论计入一致率。
        if jury_service.has_verdict(case):
            await jury_service._settle_votes(db, case, case.status == "resolved")
        await db.commit()
        return await jury_service._vote_result(db, user, assignment, settled=False, member=member, rewards=rewards, opinion=opinion)

    rows = [
        (item_vote, item_category)
        for item_vote, item_category in (
            await db.execute(
                select(JuryVote.vote, JuryVote.category).where(JuryVote.case_id == case.id, JuryVote.voted_at.is_not(None), JuryVote.stage == "panel")
            )
        ).all()
        if item_vote
    ]
    result = panel_tally([item_vote for item_vote, _category in rows])
    if result is None:
        await db.commit()
        await case_service.notify_jury_queue_changed(db)
        return await jury_service._vote_result(db, user, assignment, settled=False, member=member, rewards=rewards, opinion=opinion)
    outcome, level = result
    upheld = outcome == "violation"
    # 原审的票和合议的票都按这个结论回填一致率、结算风纪值（合议票是 panel_agreed / panel_disagreed）。
    await jury_service._settle_votes(db, case, upheld)
    await case_service.resolve_case(
        db,
        case.id,
        source="panel",
        actor_id=None,
        upheld=upheld,
        category=jury_service._verdict_category(case, rows) if upheld else None,
        level=level,
        note="资深合议认定违规" if upheld else "资深合议认定不违规",
    )
    return await jury_service._vote_result(db, user, assignment, settled=True, member=member, rewards=rewards, opinion=opinion)


# ---------------------------------------------------------------- 定时整理


async def sweep(db: AsyncSession) -> int:
    """合议队列的定时整理：撤出敏感类别的案件，把超时的、凑不齐人的转出去。返回转出了多少件，自己提交事务。"""
    now = datetime.now(UTC)
    cutoff = now - timedelta(hours=PANEL_TIMEOUT_HOURS)
    cases = (await db.execute(select(ReportCase).where(ReportCase.status == "open", ReportCase.queue == "panel"))).scalars().all()
    moved: list[ReportCase] = []
    for case in cases:
        started = penalty_service.aware(case.panel_at or case.updated_at)
        if await case_service.is_sensitive(db, case):
            case.jury_block_reason = "sensitive"
            await case_service.leave_panel(db, case, now, None)
        elif started is not None and started < cutoff:
            await case_service.leave_panel(db, case, now, "timeout")
        elif not await _still_viable(db, case, now):
            # 资深委员被降级、停职、受处罚之后人不够了：不用等到超时
            await case_service.leave_panel(db, case, now, "unavailable")
        else:
            continue
        moved.append(case)
    if moved:
        await db.commit()
        for case in moved:
            await case_service.notify_escalated(db, case)
    return len(moved)
