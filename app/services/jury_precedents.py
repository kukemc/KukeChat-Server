"""判例（docs/jury-v2.md 3.4）：把一个已定案的案件连同标题和裁判要旨公开出来，供全站参考。

两种编号（1.0）在这里都会出现：资深委员标判例时提交的是自己的评审编号，后台用的是案件的公开编号；
判例对象对外给出的 case_id 一律是公开编号。
判例的内容和结论对在任委员公开，所以成了判例的案件只能留在题库的练习那一份里，不能再用来考试和质控计分。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.case import JuryVote, ReportCase
from app.models.jury_hall import JuryPrecedent
from app.models.report import AdminAuditLog
from app.models.user import User
from app.services import cases as case_service
from app.services import jury as jury_service
from app.services import jury_bank as bank_service
from app.services import jury_overview as overview_service
from app.services import jury_settings
from app.services import penalties as penalty_service

logger = logging.getLogger(__name__)
UTC = timezone.utc

TITLE_MAX = settings.jury_precedent_title_max
SUMMARY_MAX = settings.jury_precedent_summary_max
# 因为成了判例才进题库的题，来源记成它（jury_bank.SOURCES 里有同名的取值）
BANK_SOURCE = "precedent"
BLOCK_MESSAGES = {
    "open": "案件还没有结论，不能标为判例",
    "overturned": "被改判的案件不能标为判例",
    "private": "涉及私聊或账号本身的案件不能标为判例",
    "sensitive": "色情低俗 / 涉未成年 / 隐私类的案件不能标为判例",
    "quality_check": "质控题不能标为判例",
}


def _error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


def _not_found() -> HTTPException:
    return _error(status.HTTP_404_NOT_FOUND, "precedent_not_found", "判例不存在或已撤下")


def _not_eligible(reason: str) -> HTTPException:
    return _error(status.HTTP_422_UNPROCESSABLE_ENTITY, "case_not_eligible", BLOCK_MESSAGES[reason])


# ---------------------------------------------------------------- 规则


async def _reasons(db: AsyncSession, case: ReportCase) -> tuple[str | None, bool]:
    """(这个案件为什么不能成为判例, 它能不能进题库)。前者为 None 表示可以。

    未定案、涉及私聊、敏感类别这三项和题库用同一套判断（题库的敏感类别又和公示栏是同一份）。
    题库另外不收「内容会在公示栏展示」的案件 —— 那不妨碍它成为判例，只是不进题库。
    """
    if case.status == "open":
        return "open", False
    if case.overturned_at is not None:
        return "overturned", False
    reason = await bank_service.block_reason(db, case)
    if reason in {"open", "private", "sensitive"}:
        return reason, False
    return None, reason is None


def _clean_text(title: str | None, summary: str | None) -> tuple[str, str]:
    title, summary = (title or "").strip(), (summary or "").strip()
    if not title or len(title) > TITLE_MAX or not summary or len(summary) > SUMMARY_MAX:
        raise _error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "invalid_precedent",
            f"标题和裁判要旨都不能为空；标题最多 {TITLE_MAX} 个字，要旨最多 {SUMMARY_MAX} 个字",
        )
    return title, summary


def _listed_clauses() -> list[Any]:
    """对外可见的判例：没有撤下，案件仍是已定案、没有被改判。"""
    return [JuryPrecedent.status == "active", ReportCase.status != "open", ReportCase.overturned_at.is_(None)]


# ---------------------------------------------------------------- 题库


def _enter_bank(db: AsyncSession, precedent: JuryPrecedent, case: ReportCase, bank_ok: bool) -> None:
    """判例自动进练习场的题库。未核对的题只给练习场（jury_bank.question_pool），所以进去的时候一律是未核对。

    本来就在计分那一份里的题改成未核对：判例把内容和结论都公开了，不能再拿来考试和质控。
    （还是判例的时候题库本身就把它挡在计分那一份之外，见 jury_bank.question_pool；这里改成未核对，是为了判例撤下之后它也不会自己回到计分那一份。）
    管理员移出过题库的（removed）尊重那次决定，不再放回去。
    """
    precedent.bank_added = False
    if not bank_ok or case.golden_source == "removed":
        return
    if not case.is_golden:
        case.is_golden = True
        case.golden_verified = False
        case.golden_source = BANK_SOURCE
        precedent.bank_added = True
    elif bank_service.question_pool(case) == "scored":
        case.golden_verified = False
        db.add(
            AdminAuditLog(
                actor_id=None,
                action="jury.question.update",
                target_type="report_case",
                target_id=case.id,
                details=json.dumps({"verified": {"old": True, "new": False}, "note": "成为判例，改为只用于练习"}, ensure_ascii=False),
            )
        )


def _leave_bank(precedent: JuryPrecedent, case: ReportCase | None) -> None:
    """撤下判例时把因它才进题库的题移出去。管理员之后核对过的题算他接手了，留着。"""
    if precedent.bank_added and case is not None and case.is_golden and case.golden_source == BANK_SOURCE and not case.golden_verified:
        case.is_golden = False
        # 记成 excluded：它当过判例，内容和结论公开过，以后不再自动入库（管理员仍可以手动加回来）
        case.golden_source = "excluded"
    precedent.bank_added = False


# ---------------------------------------------------------------- 撤下


def _withdraw(precedent: JuryPrecedent, case: ReportCase | None, *, actor_id: int | None, note: str | None, now: datetime) -> None:
    precedent.status = "withdrawn"
    precedent.withdrawn_at = now
    precedent.withdrawn_by_id = actor_id
    precedent.withdraw_note = note
    precedent.updated_at = now
    _leave_bank(precedent, case)


async def on_case_overturned(db: AsyncSession, case: ReportCase) -> bool:
    """案件被改判时调用（cases._apply_overturn 里、写完 overturned_at 之后）：撤下它的判例。不提交事务。

    没接这个钩子也不会把被改判的判例露出去 —— 读取时本来就按案件状态过滤，并且会惰性地做同一件事（sync_overturned）。
    """
    precedent = (
        await db.execute(select(JuryPrecedent).where(JuryPrecedent.case_id == case.id, JuryPrecedent.status == "active"))
    ).scalar_one_or_none()
    if precedent is None:
        return False
    _withdraw(precedent, case, actor_id=None, note="案件被改判，判例自动撤下", now=datetime.now(UTC))
    return True


async def sync_overturned(db: AsyncSession) -> int:
    """把案件已被改判、但判例还标着 active 的撤下来。读列表 / 详情时顺手做，自己提交事务。"""
    rows = (
        await db.execute(
            select(JuryPrecedent, ReportCase)
            .join(ReportCase, ReportCase.id == JuryPrecedent.case_id)
            .where(JuryPrecedent.status == "active", ReportCase.overturned_at.is_not(None))
        )
    ).all()
    now = datetime.now(UTC)
    for precedent, case in rows:
        _withdraw(precedent, case, actor_id=None, note="案件被改判，判例自动撤下", now=now)
    if rows:
        await db.commit()
    return len(rows)


# ---------------------------------------------------------------- 展示


def _view(precedent: JuryPrecedent, case: ReportCase, viewer_id: int | None) -> dict[str, Any]:
    verdict = jury_service.verdict_view(case)
    return {
        "id": precedent.id,
        # 公开编号
        "case_id": case.id,
        "title": precedent.title,
        "summary": precedent.summary,
        "kind": case.target_type,
        "outcome": verdict["outcome"],
        "category": verdict["category"],
        "category_label": verdict["category_label"],
        "level": verdict["level"],
        "by": precedent.created_by_role,
        "mine": viewer_id is not None and precedent.created_by_id == viewer_id,
        "created_at": penalty_service.aware(precedent.created_at),
    }


async def list_precedents(
    db: AsyncSession, user: User, *, category: str | None = None, limit: int = 20, offset: int = 0
) -> tuple[list[dict[str, Any]], int]:
    """GET /jury/precedents。所有登录用户可读，按创建时间倒序。"""
    await sync_overturned(db)
    clauses = _listed_clauses()
    if category:
        clauses.extend([ReportCase.status == "resolved", ReportCase.verdict_category == category])
    on_case = ReportCase.id == JuryPrecedent.case_id
    total = int((await db.execute(select(func.count(JuryPrecedent.id)).join(ReportCase, on_case).where(*clauses))).scalar_one() or 0)
    rows = await db.execute(
        select(JuryPrecedent, ReportCase)
        .join(ReportCase, on_case)
        .where(*clauses)
        .order_by(JuryPrecedent.created_at.desc(), JuryPrecedent.id.desc())
        .offset(offset)
        .limit(limit)
    )
    return [_view(precedent, case, user.id) for precedent, case in rows.all()], total


async def _listed(db: AsyncSession, precedent_id: int) -> tuple[JuryPrecedent, ReportCase]:
    row = (
        await db.execute(
            select(JuryPrecedent, ReportCase)
            .join(ReportCase, ReportCase.id == JuryPrecedent.case_id)
            .where(JuryPrecedent.id == precedent_id, *_listed_clauses())
        )
    ).first()
    if row is None:
        raise _not_found()
    return row[0], row[1]


async def _content(db: AsyncSession, case: ReportCase) -> tuple[dict[str, Any] | None, str | None]:
    """全站可见的那部分内容：(content, content_hidden_reason)，判断和公示栏详情是同一段代码。

    案件此刻不在公示栏里（没到公示时间、有待处理的申诉、或没有经过委员评审）时公示栏不会给内容，这里也不给，
    原因记成 unpublished。
    """
    group_content = await jury_settings.is_enabled(db, "public_group_content")
    reason = overview_service._content_hidden_reason(case, group_content)
    if reason is None and not await case_service.published_case_ids(db, [case.id]):
        reason = "unpublished"
    if reason is not None:
        return None, reason
    return overview_service._public_content(case, await case_service.case_snapshot(db, case)), None


async def precedent_detail(db: AsyncSession, user: User, precedent_id: int) -> dict[str, Any]:
    """GET /jury/precedents/{id}。case（匿名化的完整上下文）只给在任委员。"""
    await sync_overturned(db)
    precedent, case = await _listed(db, precedent_id)
    content, hidden_reason = await _content(db, case)
    case_view: dict[str, Any] | None = None
    if jury_service._is_serving(await jury_service.get_member(db, user.id)):
        # 和评审台是同一个匿名视图
        view = await jury_service.juror_case_view(db, case)
        case_view = {"messages": view["messages"], "post": view["post"]}
    return {**_view(precedent, case, user.id), "content": content, "content_hidden_reason": hidden_reason, "case": case_view}


# ---------------------------------------------------------------- 和案件互相关联的字段


async def _active(db: AsyncSession, case_id: int) -> JuryPrecedent | None:
    result = await db.execute(select(JuryPrecedent).where(JuryPrecedent.case_id == case_id, JuryPrecedent.status == "active"))
    return result.scalar_one_or_none()


async def case_fields(db: AsyncSession, user_id: int, case: ReportCase, vote: JuryVote) -> dict[str, Any]:
    """JuryCaseDetail 里的 precedent 和 can_mark_precedent。

    质控题那条记录上两项恒为空：判例带着公开编号，给出去等于把真实编号还了回去。
    """
    fields: dict[str, Any] = {"precedent": None, "can_mark_precedent": False}
    if vote.is_quality_check or case.status == "open" or case.overturned_at is not None:
        return fields
    precedent = await _active(db, case.id)
    if precedent is not None:
        fields["precedent"] = {"id": precedent.id, "title": precedent.title, "mine": precedent.created_by_id == user_id}
        return fields
    member = await jury_service.get_member(db, user_id)
    if not jury_service._is_serving(member) or member.tier != "senior":
        return fields
    user = await db.get(User, user_id)
    if user is None or jury_service.restriction(user) is not None:
        return fields
    fields["can_mark_precedent"] = (await _reasons(db, case))[0] is None
    return fields


async def case_precedent_ids(db: AsyncSession, case_ids: list[int]) -> dict[int, int]:
    """公示栏用：这些案件里哪些有对外可见的判例，case_id -> precedent_id。"""
    if not case_ids:
        return {}
    rows = await db.execute(
        select(JuryPrecedent.case_id, JuryPrecedent.id)
        .join(ReportCase, ReportCase.id == JuryPrecedent.case_id)
        .where(JuryPrecedent.case_id.in_(case_ids), *_listed_clauses())
    )
    return {case_id: precedent_id for case_id, precedent_id in rows.all()}


# ---------------------------------------------------------------- 创建 / 修改 / 撤下


async def _create(db: AsyncSession, case: ReportCase, *, title: str, summary: str, creator_id: int, role: str) -> JuryPrecedent:
    """调用方已经检查过权限。不提交事务。"""
    reason, bank_ok = await _reasons(db, case)
    if reason is not None:
        raise _not_eligible(reason)
    exists = _error(status.HTTP_409_CONFLICT, "precedent_exists", "这个案件已经是判例了")
    precedent = (await db.execute(select(JuryPrecedent).where(JuryPrecedent.case_id == case.id))).scalar_one_or_none()
    now = datetime.now(UTC)
    if precedent is not None and precedent.status == "active":
        raise exists
    if precedent is None:
        precedent = JuryPrecedent(case_id=case.id, created_at=now)
        db.add(precedent)
    else:
        # 撤下之后重新标：复用同一行（一个案件只有一条），当成一条新的判例
        precedent.created_at = now
        precedent.withdrawn_at = None
        precedent.withdrawn_by_id = None
        precedent.withdraw_note = None
    precedent.title = title
    precedent.summary = summary
    precedent.created_by_id = creator_id
    precedent.created_by_role = role
    precedent.status = "active"
    precedent.updated_at = now
    _enter_bank(db, precedent, case, bank_ok)
    try:
        await db.flush()
    except IntegrityError:
        # 两个人同时标同一个案件：另一个已经记上了。
        await db.rollback()
        raise exists from None
    return precedent


async def create_by_senior(db: AsyncSession, user: User, review_id: int, title: str | None, summary: str | None) -> dict[str, Any]:
    """POST /jury/precedents。case_id 是本人的评审编号；资深委员只能标自己投过票的已定案件。"""
    member = await jury_service._require_serving(db, user)
    if member.tier != "senior":
        raise _error(status.HTTP_403_FORBIDDEN, "not_senior", "只有资深委员可以标判例")
    # 评审编号不是本人的（包括拿公开编号来请求）一律 404；分到手没投、或者回避了的是 403 not_voted
    case, vote = await jury_service.require_voted(db, user.id, review_id)
    if vote.is_quality_check:
        raise _not_eligible("quality_check")
    title, summary = _clean_text(title, summary)
    precedent = await _create(db, case, title=title, summary=summary, creator_id=user.id, role="senior")
    await db.commit()
    await _broadcast_admin(precedent, "created")
    return _view(precedent, case, user.id)


async def _owned(db: AsyncSession, user: User, precedent_id: int) -> tuple[JuryPrecedent, ReportCase]:
    """改 / 撤自己的判例：只认作者本人，和他现在是不是资深无关（mine 说的就是这件事）。"""
    precedent, case = await _listed(db, precedent_id)
    if precedent.created_by_id != user.id:
        raise _error(status.HTTP_403_FORBIDDEN, "not_precedent_author", "只能修改或撤销自己标的判例")
    return precedent, case


def _apply_changes(precedent: JuryPrecedent, title: str | None, summary: str | None) -> dict[str, Any]:
    """改标题 / 要旨，只动传进来的那一项。返回改之前的样子。"""
    before = {"title": precedent.title, "summary": precedent.summary}
    new_title, new_summary = _clean_text(title if title is not None else precedent.title, summary if summary is not None else precedent.summary)
    if (new_title, new_summary) != (precedent.title, precedent.summary):
        precedent.title = new_title
        precedent.summary = new_summary
        precedent.updated_at = datetime.now(UTC)
    return before


async def update_precedent(db: AsyncSession, user: User, precedent_id: int, title: str | None, summary: str | None) -> dict[str, Any]:
    """PUT /jury/precedents/{id}。"""
    precedent, case = await _owned(db, user, precedent_id)
    _apply_changes(precedent, title, summary)
    await db.commit()
    await _broadcast_admin(precedent, "updated")
    return _view(precedent, case, user.id)


async def withdraw_precedent(db: AsyncSession, user: User, precedent_id: int) -> dict[str, Any]:
    """DELETE /jury/precedents/{id}。"""
    precedent, case = await _owned(db, user, precedent_id)
    _withdraw(precedent, case, actor_id=user.id, note=None, now=datetime.now(UTC))
    await db.commit()
    await _broadcast_admin(precedent, "withdrawn")
    return {"id": precedent.id, "status": precedent.status}


async def _broadcast_admin(precedent: JuryPrecedent, action: str) -> None:
    from app.websocket.manager import admin_manager

    try:
        await admin_manager.broadcast(
            "admin.jury.precedent.changed", {"precedent_id": precedent.id, "case_id": precedent.case_id, "status": precedent.status, "action": action}
        )
    except Exception:
        logger.exception("Failed to broadcast precedent change", extra={"precedent_id": precedent.id})


# ---------------------------------------------------------------- 后台


async def admin_reads(db: AsyncSession, rows: list[tuple[JuryPrecedent, ReportCase]], viewer_id: int | None = None) -> list[dict[str, Any]]:
    creator_ids = {precedent.created_by_id for precedent, _case in rows if precedent.created_by_id is not None}
    creators = {user.id: user for user in (await db.execute(select(User).where(User.id.in_(creator_ids)))).scalars()} if creator_ids else {}
    return [
        {
            **_view(precedent, case, viewer_id),
            "status": precedent.status,
            "created_by_id": precedent.created_by_id,
            "created_by": creators.get(precedent.created_by_id),
            "updated_at": penalty_service.aware(precedent.updated_at),
            "withdrawn_at": penalty_service.aware(precedent.withdrawn_at),
            "withdrawn_by_id": precedent.withdrawn_by_id,
            "withdraw_note": precedent.withdraw_note,
            "overturned": case.overturned_at is not None,
            "in_bank": bool(case.is_golden),
            "bank_added": bool(precedent.bank_added),
        }
        for precedent, case in rows
    ]


async def admin_list(
    db: AsyncSession, *, viewer_id: int | None = None, status_filter: str | None = None, limit: int = 50, offset: int = 0
) -> tuple[list[dict[str, Any]], int]:
    """后台列表：含已撤下的，按创建时间倒序。"""
    await sync_overturned(db)
    clauses: list[Any] = []
    if status_filter:
        clauses.append(JuryPrecedent.status == status_filter)
    total = int((await db.execute(select(func.count(JuryPrecedent.id)).where(*clauses))).scalar_one() or 0)
    rows = await db.execute(
        select(JuryPrecedent, ReportCase)
        .join(ReportCase, ReportCase.id == JuryPrecedent.case_id)
        .where(*clauses)
        .order_by(JuryPrecedent.created_at.desc(), JuryPrecedent.id.desc())
        .offset(offset)
        .limit(limit)
    )
    return await admin_reads(db, [(precedent, case) for precedent, case in rows.all()], viewer_id), total


async def _admin_get(db: AsyncSession, precedent_id: int) -> tuple[JuryPrecedent, ReportCase]:
    precedent = await db.get(JuryPrecedent, precedent_id)
    case = await db.get(ReportCase, precedent.case_id) if precedent is not None else None
    if precedent is None or case is None:
        raise _not_found()
    return precedent, case


def _audit(db: AsyncSession, actor_id: int, action: str, precedent: JuryPrecedent, details: dict[str, Any]) -> None:
    db.add(
        AdminAuditLog(
            actor_id=actor_id,
            action=f"jury.precedent.{action}",
            target_type="jury_precedent",
            target_id=precedent.id,
            details=json.dumps({"case_id": precedent.case_id, **details}, ensure_ascii=False),
        )
    )


async def admin_create(db: AsyncSession, admin: User, case_id: int, title: str | None, summary: str | None) -> dict[str, Any]:
    """后台标判例：case_id 是案件的公开编号，任何已定的、允许评审的案件都可以。"""
    case = await db.get(ReportCase, case_id)
    if case is None:
        raise _error(status.HTTP_404_NOT_FOUND, "case_not_found", "案件不存在")
    title, summary = _clean_text(title, summary)
    precedent = await _create(db, case, title=title, summary=summary, creator_id=admin.id, role="admin")
    _audit(db, admin.id, "create", precedent, {"title": title, "bank_added": bool(precedent.bank_added)})
    await db.commit()
    await _broadcast_admin(precedent, "created")
    return (await admin_reads(db, [(precedent, case)], admin.id))[0]


async def admin_update(db: AsyncSession, admin: User, precedent_id: int, title: str | None, summary: str | None) -> dict[str, Any]:
    """后台可以改任何人标的判例，创建人不变。已撤下的不能改。"""
    precedent, case = await _admin_get(db, precedent_id)
    if precedent.status != "active":
        raise _not_found()
    before = _apply_changes(precedent, title, summary)
    after = {"title": precedent.title, "summary": precedent.summary}
    if after != before:
        _audit(db, admin.id, "update", precedent, {"from": before, "to": after})
    await db.commit()
    await _broadcast_admin(precedent, "updated")
    return (await admin_reads(db, [(precedent, case)], admin.id))[0]


async def admin_withdraw(db: AsyncSession, admin: User, precedent_id: int, note: str | None = None) -> dict[str, Any]:
    """后台撤下任何人标的判例。重复撤是幂等的。"""
    precedent, case = await _admin_get(db, precedent_id)
    if precedent.status == "active":
        text = note.strip()[:500] if note else None
        _withdraw(precedent, case, actor_id=admin.id, note=text or None, now=datetime.now(UTC))
        _audit(db, admin.id, "withdraw", precedent, {"title": precedent.title, "note": text or None})
        await db.commit()
        await _broadcast_admin(precedent, "withdrawn")
    return (await admin_reads(db, [(precedent, case)], admin.id))[0]
