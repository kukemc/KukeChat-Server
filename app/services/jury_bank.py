"""题库（docs/jury-v2.md 2.1）。

一道题 = 一个已经有结论的案件，report_cases.is_golden 表示「在题库里」。题有「已核对」标记：
已核对的用于入门考试和评审中的质控计分，未核对的只用于练习。
练习场会公布标准答案，所以练习的题和计分的题不能是同一批：已核对的题按编号固定切出一份只给练习场，见 question_pool。
判例把内容和结论都对在任委员公开了：案件只要还是有效的判例，就恒属于练习那一份，无论核对没核对、编号是多少。
这个模块不依赖 jury.py（评审分案时要用这里的规则），需要委员视图的地方在函数里再导入。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.case import JuryExamAttempt, JuryPointLog, JuryPracticeAnswer, JuryVote, ReportCase
from app.models.jury_hall import JuryPrecedent
from app.models.penalty import UserViolation
from app.models.report import AdminAuditLog
from app.models.user import User
from app.services import cases as case_service
from app.services import jury_settings
from app.services import penalties as penalty_service

logger = logging.getLogger(__name__)
UTC = timezone.utc

# precedent：因为被标成判例才进的题库（jury_precedents），只给练习场
SOURCES = ("admin", "consensus", "legacy", "precedent")
# 自动入库：委员全票一致、有效票不少于这么多，定案满这么久
CONSENSUS_VOTES = settings.jury_golden_consensus_votes
# 举报人还能申请复议的那几天里结论可能被推翻，等窗口过了再自动入库
AUTO_INCLUDE_DELAY_HOURS = max(settings.jury_golden_delay_hours, settings.jury_reconsider_window_days * 24)
AUTO_INCLUDE_BATCH = 200
# 已核对的题里，编号能被它整除的那一份只给练习场，其余的给考试和评审质控。按编号切是为了固定：一道题不会今天练、明天考。
PRACTICE_EVERY = settings.jury_practice_every
POOLS = ("practice", "scored")
DECIDED_VOTES = ("none", "l1", "l2", "l3")
BLOCK_MESSAGES = {
    "open": "案件还没有结论，不能进题库",
    "private": "涉及私聊或账号本身的案件不能进题库",
    "sensitive": "色情低俗 / 涉未成年 / 隐私类的案件不能进题库",
    "public_content": "这个案件的内容会出现在公示栏里，不能进题库（否则可以去公示栏查答案）",
}
# 能进题库、但不能标成「已核对」（不能用于考试和质控计分）的原因，见 verify_block_reason
VERIFY_BLOCK_MESSAGES = {
    "hidden_context": "被举报人在上下文里还有看不到的消息（已撤回 / 已删除）或语音，标准答案是「不违规」的题委员没法确认，不能当计分题",
    "voice_target": "被举报的是一条语音，旧版客户端放不了，不能当计分题",
}
# 质控题答错的人多到这个程度，自动移出计分（改回未核对），等管理员复核。只看上一次核对之后的作答。
PULL_MIN_WRONG = 2
PULL_WRONG_RATE = 0.4
AUTO_SUSPEND_NOTE = "评审准确率过低，系统自动暂停资格"


# ---------------------------------------------------------------- 标准答案


def question_source(case: ReportCase) -> str:
    """题目的来源；上线前回填的旧题没有记来源，按 legacy 算。"""
    return case.golden_source if case.golden_source in SOURCES else "legacy"


def question_upheld(case: ReportCase) -> bool:
    """标准答案是不是「违规」：管理员改过的以改过的为准，否则取案件自身的结论。"""
    if case.golden_outcome in {"violation", "clean"}:
        return case.golden_outcome == "violation"
    return case.status == "resolved"


def question_answer(case: ReportCase) -> dict[str, Any]:
    upheld = question_upheld(case)
    category = (case.golden_category or case.verdict_category) if upheld else None
    level = (case.golden_level if case.golden_level is not None else case.verdict_level) if upheld else None
    return {
        "outcome": "violation" if upheld else "clean",
        "level": level,
        "category": category,
        "category_label": penalty_service.category_label(category) if category else None,
        "explanation": case.golden_explanation,
    }


def is_correct(case: ReportCase, vote: str | None) -> bool:
    """只判「违规 / 不违规」对不对，等级不计分。没答（None）算错。"""
    if vote not in DECIDED_VOTES:
        return False
    return (vote != "none") == question_upheld(case)


# ---------------------------------------------------------------- 入库规则


def shows_public_content(case: ReportCase, group_content: bool) -> bool:
    """公示栏里会不会展示这个案件的内容：公开动态且认定违规；群聊内容公示的开关打开时，委员审过的群聊违规案件也会。"""
    if case.status != "resolved":
        return False
    if case.content_publishable:
        return True
    return bool(group_content and case.publishable and case.target_type == "message")


async def block_reason(db: AsyncSession, case: ReportCase, *, group_content: bool | None = None, sensitive: bool | None = None) -> str | None:
    """这个案件为什么不能当题目；None 表示可以。入库和出题时都按这一套判断。

    sensitive：调用方已经用 cases.sensitive_case_ids 批量查过时把结果传进来，这里就不再逐个案件查库。
    """
    if case.status not in ("resolved", "dismissed"):
        # 未结和已归档（archived，没有结论）的都不能当题目
        return "open"
    if not case.jury_eligible:
        return "private"
    # 和公示栏用同一套敏感类别判断，再加上分案时那套（举报理由 / 机器类别）
    categories = {case.verdict_category, case.golden_category}
    if case.jury_block_reason == "sensitive" or categories & case_service.HIDDEN_CONTENT_CATEGORIES:
        return "sensitive"
    if sensitive if sensitive is not None else await case_service.is_sensitive(db, case):
        return "sensitive"
    if group_content is None:
        group_content = await jury_settings.is_enabled(db, "public_group_content")
    if shows_public_content(case, group_content):
        return "public_content"
    return None


def verify_block_from_view(view: dict[str, Any], upheld: bool) -> str | None:
    """看委员能看到的内容判断这道题能不能当计分题；None 表示可以。

    判「不违规」要看清被举报人在上下文里的全部言行：他有撤回 / 删除的消息（委员看到的是空白）或语音时，
    答案可能恰恰就在看不到的那几条里。判「违规」的题，被举报的那条本身看得见就够了，除非它是语音。
    """
    messages = view.get("messages") or []
    target = next((item for item in messages if item.get("is_target")), None)
    if target is not None and target.get("type") == "voice":
        return "voice_target"
    if upheld:
        return None
    for item in messages:
        if item.get("speaker") != "被举报人" or item.get("type") == "system":
            continue
        if item.get("type") == "voice" or (not (item.get("content") or "").strip() and not item.get("images")):
            return "hidden_context"
    return None


async def verify_block_reason(db: AsyncSession, case: ReportCase) -> str | None:
    from app.services import jury as jury_service

    if case.target_type != "message":
        return None
    view = await jury_service.juror_case_view(db, case)
    return verify_block_from_view(view, question_upheld(case))


async def _has_pending_appeal(db: AsyncSession, case: ReportCase) -> bool:
    clauses = [UserViolation.case_id == case.id]
    if case.violation_id is not None:
        clauses.append(UserViolation.id == case.violation_id)
    result = await db.execute(select(UserViolation.id).where(or_(*clauses), UserViolation.appeal_status == "pending").limit(1))
    return result.scalar_one_or_none() is not None


async def include_after_admin(db: AsyncSession, case: ReportCase) -> bool:
    """管理员结案的案件进题库，但只是「未核对」：随手结一个案不等于核对过题目。调用方负责 commit。"""
    if case.is_golden or case.golden_source == "removed" or await block_reason(db, case) is not None:
        return False
    case.is_golden = True
    case.golden_verified = False
    case.golden_source = "admin"
    return True


async def auto_include(db: AsyncSession) -> int:
    """委员一致通过的案件自动入库并视为已核对。由定时循环调用，自己提交事务。

    条件：委员定案、有效票不少于 CONSENSUS_VOTES 且全票一致、定案满 AUTO_INCLUDE_DELAY_HOURS、没有被改判、
    相关处罚没有待处理的申诉。永远不会满足条件的案件记成 excluded，下次不再看。
    """
    now = datetime.now(UTC)
    rows = await db.execute(
        select(ReportCase)
        .where(
            ReportCase.status != "open",
            ReportCase.jury_outcome == "consensus",
            ReportCase.verdict_source == "jury",
            ReportCase.is_golden.is_(False),
            ReportCase.golden_source.is_(None),
            ReportCase.closed_at <= now - timedelta(hours=AUTO_INCLUDE_DELAY_HOURS),
        )
        .order_by(ReportCase.id.asc())
        .limit(AUTO_INCLUDE_BATCH)
    )
    cases = list(rows.scalars().all())
    if not cases:
        return 0
    group_content = await jury_settings.is_enabled(db, "public_group_content")
    included = 0
    for case in cases:
        if await _has_pending_appeal(db, case):
            # 申诉有了结果再看：驳回就入库，通过的话案件会被改判。
            continue
        votes = (
            await db.execute(
                select(JuryVote.vote).where(
                    JuryVote.case_id == case.id, JuryVote.voted_at.is_not(None), JuryVote.is_quality_check.is_(False), JuryVote.vote.in_(DECIDED_VOTES)
                )
            )
        ).scalars().all()
        violation = sum(1 for vote in votes if vote != "none")
        unanimous = len(votes) >= CONSENSUS_VOTES and violation in {0, len(votes)}
        if case.overturned_at is not None or not unanimous or await block_reason(db, case, group_content=group_content) is not None:
            case.golden_source = "excluded"
            continue
        case.is_golden = True
        # 全票一致也只说明委员看到的部分没有争议；上下文里有看不到的内容时只进练习
        case.golden_verified = await verify_block_reason(db, case) is None
        case.golden_source = "consensus"
        included += 1
    await db.commit()
    return included


# ---------------------------------------------------------------- 出题


def question_pool(case: ReportCase, precedent: bool = False) -> str | None:
    """这道题给谁用：practice 只给练习场 / scored 给入门考试和评审质控；不在题库里为 None。

    未核对的题全部只给练习场。已核对的题里编号能被 PRACTICE_EVERY 整除的也只给练习场，其余的练习场不出。
    precedent：这个案件现在是不是有效的判例（precedent_case_ids / is_precedent 查出来传进来）。是的话恒属于练习那一份；
    不传就是「不看判例」的原规则，也是判例撤下之后恢复的规则。
    """
    if not case.is_golden:
        return None
    if precedent:
        return "practice"
    return "scored" if case.golden_verified and case.id % PRACTICE_EVERY != 0 else "practice"


def _precedent_clause() -> Any:
    """SQL 里的「这个案件现在是有效的判例」：判例没有撤下，案件没有被改判（被改判的判例会被撤下，可能还没来得及）。"""
    return and_(
        exists().where(JuryPrecedent.case_id == ReportCase.id, JuryPrecedent.status == "active"),
        ReportCase.overturned_at.is_(None),
    )


async def precedent_case_ids(db: AsyncSession, cases: list[ReportCase]) -> set[int]:
    """这些案件里现在是有效判例的那些（口径同 _precedent_clause）。"""
    case_ids = [case.id for case in cases if case.status != "open" and case.overturned_at is None]
    if not case_ids:
        return set()
    rows = await db.execute(select(JuryPrecedent.case_id).where(JuryPrecedent.case_id.in_(case_ids), JuryPrecedent.status == "active"))
    return set(rows.scalars().all())


async def is_precedent(db: AsyncSession, case: ReportCase) -> bool:
    return case.id in await precedent_case_ids(db, [case])


def bank_clauses(*, pool: str) -> list[Any]:
    """某一份题在 SQL 里能判断的那部分条件（口径同 question_pool）；类别和公示栏的检查见 block_reason。"""
    clauses: list[Any] = [
        ReportCase.is_golden.is_(True),
        ReportCase.status != "open",
        ReportCase.jury_eligible.is_(True),
        ReportCase.jury_block_reason.is_(None),
    ]
    practice_share = ReportCase.id % PRACTICE_EVERY == 0
    if pool == "scored":
        # 有效的判例不出：内容和结论对在任委员是公开的，拿来考试、质控等于送分
        clauses.extend([ReportCase.golden_verified.is_(True), ~practice_share, ~_precedent_clause()])
    else:
        clauses.append(or_(ReportCase.golden_verified.is_(False), practice_share, _precedent_clause()))
    return clauses


async def verified_count(db: AsyncSession) -> int:
    """能用于考试的题（已核对、且不在练习那一份里）有多少道。只是个便宜的估计，用来告诉客户端「现在能不能考」；真正抽题时还会逐题检查。"""
    result = await db.execute(select(func.count(ReportCase.id)).where(*bank_clauses(pool="scored")))
    return int(result.scalar_one() or 0)


# ---------------------------------------------------------------- 后台


async def _answer_stats(db: AsyncSession, case_ids: list[int]) -> dict[int, tuple[int, int]]:
    """每道题被答了多少次、答对多少次：评审中的质控作答 + 练习场作答。考试的作答存在场次里，不计入。"""
    stats: dict[int, list[int]] = {case_id: [0, 0] for case_id in case_ids}
    if not case_ids:
        return {}
    quality = await db.execute(
        select(JuryVote.case_id, JuryVote.agreed, func.count())
        .where(JuryVote.case_id.in_(case_ids), JuryVote.is_quality_check.is_(True), JuryVote.agreed.is_not(None))
        .group_by(JuryVote.case_id, JuryVote.agreed)
    )
    practice = await db.execute(
        select(JuryPracticeAnswer.case_id, JuryPracticeAnswer.correct, func.count())
        .where(JuryPracticeAnswer.case_id.in_(case_ids))
        .group_by(JuryPracticeAnswer.case_id, JuryPracticeAnswer.correct)
    )
    for rows in (quality, practice):
        for case_id, correct, count in rows.all():
            stats[case_id][0] += int(count)
            stats[case_id][1] += int(count) if correct else 0
    return {case_id: (answered, correct) for case_id, (answered, correct) in stats.items()}


async def question_reads(db: AsyncSession, cases: list[ReportCase]) -> list[dict[str, Any]]:
    stats = await _answer_stats(db, [case.id for case in cases])
    precedents = await precedent_case_ids(db, cases)
    items: list[dict[str, Any]] = []
    for case in cases:
        answer = question_answer(case)
        answered, correct = stats.get(case.id, (0, 0))
        items.append(
            {
                "case_id": case.id,
                "kind": case.target_type,
                "primary_reason": case.primary_reason,
                "summary": case_service._case_summary(await case_service.case_snapshot(db, case)),
                "closed_at": penalty_service.aware(case.closed_at),
                "verdict_source": case.verdict_source,
                "in_bank": bool(case.is_golden),
                "verified": bool(case.is_golden and case.golden_verified),
                # practice 只给练习场 / scored 给考试和质控；不在题库里为 null。仍是判例的恒为 practice
                "pool": question_pool(case, case.id in precedents),
                "source": question_source(case) if case.is_golden else None,
                "outcome": answer["outcome"],
                "level": answer["level"],
                "category": answer["category"],
                "category_label": answer["category_label"],
                "explanation": answer["explanation"],
                # 标准答案是不是管理员改过的
                "answer_overridden": case.golden_outcome is not None or case.golden_level is not None or case.golden_category is not None,
                "answered": answered,
                "correct_rate": round(correct / answered, 3) if answered else None,
            }
        )
    return items


async def list_questions(
    db: AsyncSession,
    *,
    verified: bool | None = None,
    source: str | None = None,
    query: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    clauses: list[Any] = [ReportCase.is_golden.is_(True)]
    if verified is not None:
        clauses.append(ReportCase.golden_verified.is_(verified))
    if source == "legacy":
        clauses.append(or_(ReportCase.golden_source.is_(None), ReportCase.golden_source.not_in([item for item in SOURCES if item != "legacy"])))
    elif source:
        clauses.append(ReportCase.golden_source == source)
    if query and query.strip():
        text = query.strip()
        conditions = [ReportCase.primary_reason.ilike(f"%{text}%"), ReportCase.golden_explanation.ilike(f"%{text}%")]
        if text.isdigit():
            conditions.append(ReportCase.id == int(text))
        clauses.append(or_(*conditions))
    total = int((await db.execute(select(func.count(ReportCase.id)).where(*clauses))).scalar_one() or 0)
    rows = await db.execute(select(ReportCase).where(*clauses).order_by(ReportCase.id.desc()).offset(offset).limit(limit))
    return await question_reads(db, list(rows.scalars().all())), total


async def _case(db: AsyncSession, case_id: int) -> ReportCase:
    case = await db.get(ReportCase, case_id)
    if case is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"code": "case_not_found", "message": "案件不存在"})
    return case


async def question_detail(db: AsyncSession, case_id: int) -> dict[str, Any]:
    """题目内容（匿名视图）+ 列表里的字段。还没入库的已结案件也能看，方便入库前预览。"""
    from app.services import jury as jury_service

    case = await _case(db, case_id)
    view = await jury_service.juror_case_view(db, case)
    reason = await block_reason(db, case)
    verify_reason = verify_block_from_view(view, question_upheld(case)) if case.target_type == "message" else None
    return {
        **(await question_reads(db, [case]))[0],
        "reasons": view["reasons"],
        "report_count": view["report_count"],
        "messages": view["messages"],
        "post": view["post"],
        # 不能当题目的原因；可以时为 null
        "block_reason": reason,
        "block_message": BLOCK_MESSAGES.get(reason) if reason else None,
        # 能进题库、但不能标成已核对（计分）的原因；可以时为 null
        "verify_block_reason": verify_reason,
        "verify_block_message": VERIFY_BLOCK_MESSAGES.get(verify_reason) if verify_reason else None,
    }


async def restore_if_cleared(db: AsyncSession, member: Any) -> bool:
    """被系统按准确率自动暂停的人，质控题更正后准确率回到暂停线以上的，自动恢复资格。调用方负责 commit。"""
    from app.services import jury as jury_service
    from app.services import jury_points as points_service

    if member.status != "suspended" or member.review_note != AUTO_SUSPEND_NOTE:
        return False
    low_agreement = member.votes_settled >= jury_service.MIN_SETTLED_FOR_REVIEW and member.votes_agreed / member.votes_settled < jury_service.MIN_ACCURACY
    low_golden = member.golden_total >= jury_service.MIN_GOLDEN_FOR_REVIEW and member.golden_correct / member.golden_total < jury_service.MIN_ACCURACY
    if low_agreement or low_golden:
        return False
    now = datetime.now(UTC)
    jury_service._activate(member, now)
    member.review_note = "质控题更正后准确率已恢复，系统自动恢复资格"
    member.reviewed_at = now
    member.reviewed_by_id = None
    db.add(AdminAuditLog(actor_id=None, action="jury.member.auto_reinstate", target_type="user", target_id=member.user_id, details="质控题更正后准确率恢复"))
    await points_service.sync_badge(db, member, now=now)
    points_service.notify(
        db, member.user_id, "reinstated", "你的风纪委员资格已恢复", "之前暂停你资格的质控题里有题目标准答案有误，更正后你的准确率已经恢复，资格已自动恢复。"
    )
    return True


async def _rescore_quality_votes(db: AsyncSession, case: ReportCase, *, void: bool = False) -> int:
    """标准答案改了、或者题被移出计分之后，把评审中答错这道质控题的人改判为答对。调用方负责 commit。

    void：这道题不再计分（移出题库 / 改回未核对）—— 题有问题，之前答错的一律算对。
    否则按新的标准答案重新对：原来错、现在对的改判为答对；原来对、现在错的不追溯（不因为题目出错再扣谁的分）。
    改判为答对的：退回当时扣的风纪值并补上答对的分，正确数 +1，被这道题拖到自动暂停的人恢复资格，并发通知说明。
    """
    from app.services import jury as jury_service
    from app.services import jury_points as points_service

    upheld = question_upheld(case)
    votes = (
        await db.execute(select(JuryVote).where(JuryVote.case_id == case.id, JuryVote.is_quality_check.is_(True), JuryVote.agreed.is_(False)))
    ).scalars().all()
    changed = 0
    for vote in votes:
        if vote.vote not in DECIDED_VOTES:
            continue
        if not void and (vote.vote != "none") != upheld:
            continue
        vote.agreed = True
        member = await jury_service.get_member(db, vote.juror_id)
        if member is None:
            continue
        member.golden_correct = min(member.golden_total, member.golden_correct + 1)
        lost = (
            await db.execute(
                select(func.coalesce(func.sum(JuryPointLog.points), 0)).where(
                    JuryPointLog.user_id == vote.juror_id, JuryPointLog.kind == "quality_wrong", JuryPointLog.case_id == case.id
                )
            )
        ).scalar_one()
        refund = -int(lost or 0) + points_service.POINTS["quality_correct"]
        if refund:
            await points_service.award(db, member, "quality_fixed", points=refund, case_id=case.id, note=f"质控题更正：评审 #{vote.id}")
        points_service.notify(
            db,
            vote.juror_id,
            "quality_fixed",
            "一道质控题的答案已更正",
            f"你在评审 #{vote.id} 答的质控题标准答案有误，已改判为答对：退回扣掉的风纪值并补上答对的分（共 {refund:+d}）。",
        )
        await restore_if_cleared(db, member)
        changed += 1
    if changed:
        points_service.reset_leaderboard_cache()
    return changed


async def maybe_pull_question(db: AsyncSession, case: ReportCase) -> bool:
    """质控题刚被答错一次时调用：上次核对之后答错的人数和比例都过线，就把题改回未核对（移出考试和质控），等管理员复核。

    只移出、不改判：题到底错没错由管理员看。管理员改答案或移出题库时，之前答错的人会自动改判（_rescore_quality_votes）。
    调用方负责 commit。
    """
    if not case.is_golden or not case.golden_verified:
        return False
    since = (
        await db.execute(
            select(func.max(AdminAuditLog.created_at)).where(
                AdminAuditLog.target_type == "report_case", AdminAuditLog.target_id == case.id, AdminAuditLog.action == "jury.question.update"
            )
        )
    ).scalar_one_or_none()
    clauses = [JuryVote.case_id == case.id, JuryVote.is_quality_check.is_(True), JuryVote.agreed.is_not(None)]
    if since is not None:
        clauses.append(JuryVote.voted_at > since)
    rows = (await db.execute(select(JuryVote.agreed, func.count()).where(*clauses).group_by(JuryVote.agreed))).all()
    counts = {bool(agreed): int(count) for agreed, count in rows}
    wrong, total = counts.get(False, 0), sum(counts.values())
    if wrong < PULL_MIN_WRONG or wrong / total < PULL_WRONG_RATE:
        return False
    case.golden_verified = False
    db.add(
        AdminAuditLog(
            actor_id=None,
            action="jury.question.auto_pulled",
            target_type="report_case",
            target_id=case.id,
            details=json.dumps({"wrong": wrong, "answered": total}, ensure_ascii=False),
        )
    )
    logger.warning("quality question pulled from scoring", extra={"case_id": case.id, "wrong": wrong, "answered": total})
    return True


async def update_question(db: AsyncSession, case_id: int, *, actor_id: int, changes: dict[str, Any]) -> dict[str, Any]:
    """加入 / 移出题库、核对、改标准答案和说明。changes 里只放请求里出现过的字段，值为 None 表示清空。"""
    from app.services import jury as jury_service

    case = await _case(db, case_id)
    before = {
        "in_bank": bool(case.is_golden),
        "verified": bool(case.golden_verified),
        "source": case.golden_source,
        "outcome": case.golden_outcome,
        "level": case.golden_level,
        "category": case.golden_category,
        "explanation": case.golden_explanation,
    }
    requested = changes.get("in_bank")
    if requested is False:
        # 移出题库：记成 removed，自动入库和管理员结案都不会再把它放回来。计过分的题，之前答错的人改判为答对。
        if case.is_golden and case.golden_verified:
            await _rescore_quality_votes(db, case, void=True)
        case.is_golden = False
        case.golden_verified = False
        case.golden_source = "removed"
    elif not case.is_golden and requested is not True:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={"code": "not_in_bank", "message": "这个案件不在题库里，先把它加入题库"})
    else:
        category = changes.get("category")
        if category is not None and (category not in penalty_service.CATEGORIES or category in case_service.HIDDEN_CONTENT_CATEGORIES):
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail={"code": "invalid_category", "message": "未知或不能用于题库的违规类别"})
        # 新加入、或者要标成已核对时，都按入库规则查一遍（先查再改，不合规就什么都不动）。
        if not case.is_golden or changes.get("verified"):
            reason = await block_reason(db, case)
            if reason is not None:
                raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail={"code": "case_not_eligible", "message": BLOCK_MESSAGES[reason]})
        was_verified = bool(case.is_golden and case.golden_verified)
        # 改完之后还是 / 变成已核对的，按改完之后的标准答案查能不能计分（比如把答案改成「不违规」，就要看被举报人有没有看不到的消息）
        verified_after = bool(changes["verified"]) if changes.get("verified") is not None else was_verified
        if verified_after and case.target_type == "message":
            outcome_after = changes["outcome"] if "outcome" in changes else case.golden_outcome
            upheld_after = outcome_after == "violation" if outcome_after in {"violation", "clean"} else case.status == "resolved"
            reason = verify_block_from_view(await jury_service.juror_case_view(db, case), upheld_after)
            if reason is not None:
                raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail={"code": "question_not_verifiable", "message": VERIFY_BLOCK_MESSAGES[reason]})
        if not case.is_golden:
            case.is_golden = True
            case.golden_verified = False
            case.golden_source = "admin"
        if changes.get("verified") is not None:
            case.golden_verified = bool(changes["verified"])
        if "outcome" in changes:
            case.golden_outcome = changes["outcome"]
        if "level" in changes:
            case.golden_level = changes["level"]
        if "category" in changes:
            case.golden_category = category
        if "explanation" in changes:
            text = (changes["explanation"] or "").strip()
            case.golden_explanation = text[:2000] or None
        if not question_upheld(case):
            # 标准答案是「不违规」时不留等级和类别
            case.golden_level = None
            case.golden_category = None
        if was_verified and not case.golden_verified:
            await _rescore_quality_votes(db, case, void=True)
        elif "outcome" in changes:
            await _rescore_quality_votes(db, case)
    after = {
        "in_bank": bool(case.is_golden),
        "verified": bool(case.golden_verified),
        "source": case.golden_source,
        "outcome": case.golden_outcome,
        "level": case.golden_level,
        "category": case.golden_category,
        "explanation": case.golden_explanation,
    }
    if after != before:
        db.add(
            AdminAuditLog(
                actor_id=actor_id,
                action="jury.question.update",
                target_type="report_case",
                target_id=case.id,
                details=json.dumps({"from": before, "to": after}, ensure_ascii=False),
            )
        )
    await db.commit()
    return await question_detail(db, case.id)


async def list_exams(db: AsyncSession, *, passed: bool | None = None, limit: int = 50, offset: int = 0) -> tuple[list[dict[str, Any]], int]:
    """考试记录：已经有结果的场次，按时间倒序。"""
    clauses: list[Any] = [JuryExamAttempt.submitted_at.is_not(None)]
    if passed is not None:
        clauses.append(JuryExamAttempt.passed.is_(passed))
    total = int((await db.execute(select(func.count(JuryExamAttempt.id)).where(*clauses))).scalar_one() or 0)
    rows = await db.execute(
        select(JuryExamAttempt, User)
        .join(User, User.id == JuryExamAttempt.user_id)
        .where(*clauses)
        .order_by(JuryExamAttempt.submitted_at.desc(), JuryExamAttempt.id.desc())
        .offset(offset)
        .limit(limit)
    )
    items = [
        {
            "id": attempt.id,
            "user_id": attempt.user_id,
            "user": user,
            "score": attempt.score or 0,
            "total": attempt.total,
            "passed": bool(attempt.passed),
            "timed_out": bool(attempt.timed_out),
            "started_at": penalty_service.aware(attempt.started_at),
            "submitted_at": penalty_service.aware(attempt.submitted_at),
        }
        for attempt, user in rows.all()
    ]
    return items, total
