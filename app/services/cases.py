"""举报案件。

同一个目标的举报合并成一个案件，只审一次，结论回写给案件里的每一条举报。
案件有三个去处：机器预审能直接下结论的当场结案；群聊消息和公开内容交给风纪委员；
其余（私聊、举报用户、举报会话，以及风纪委员没有共识的）留给管理员。
委员没能定案的先尝试资深合议（queue = panel），凑不齐人或合议超时再转管理员。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import exists, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.core.dialect import lock_row
from app.models.case import JuryMember, JuryVote, ReportCase
from app.models.conversation import Conversation
from app.models.enums import ConversationType, ModerationSource, ModerationStatus, PostVisibility, ReportStatus, ReportTargetType
from app.models.message import Message
from app.models.penalty import UserViolation
from app.models.post import Post
from app.models.report import AdminAuditLog, ContentModerationRecord, UserReport
from app.models.user import User
from app.services import penalties as penalty_service

logger = logging.getLogger(__name__)
UTC = timezone.utc

OTHER_REASON = "其他"
# 举报理由 -> (违规类别, 排队权重)。理由只是线索，结案时可以改判类别。
REPORT_REASONS: dict[str, tuple[str, float]] = {
    "辱骂骚扰": ("harassment", 1.5),
    "垃圾广告": ("advertising", 1.0),
    "色情低俗": ("vulgar", 2.0),
    "违法违规": ("illegal", 3.0),
    "诈骗或钓鱼": ("fraud", 3.0),
    OTHER_REASON: ("other", 0.8),
}
OTHER_REASON_MIN_DETAILS = 10
CREDIT_WINDOW_DAYS = 90
JURY_TIMEOUT_HOURS = settings.jury_timeout_hours
PUBLISH_DELAY_HOURS = settings.jury_publish_delay_hours
# 委员池里未成年人占多数：这些举报理由 / 机器类别的案件整类只走管理员。
SENSITIVE_REASONS = {"色情低俗"}
SENSITIVE_MACHINE_CATEGORIES = {"sexual", "minor_sexual"}
# 这些类别的案件即使认定违规也不公示内容、也不进题库：隐私类会二次扩散，色情 / 擦边类不适合给未成年人看。
HIDDEN_CONTENT_CATEGORIES = {"privacy", "sexual", "minor_sexual", "vulgar", "suggestive"}
# 结论来自这些来源、且有委员真实投过票的案件才可能进公示栏（机器自动结案的不进）。
PUBLISHABLE_SOURCES = {"jury", "panel", "admin"}
QUEUE_EVENT_INTERVAL_SECONDS = 30.0
# 一条消息 / 动态被认定不违规后这么多天内不能再举报（docs/jury-v2.md 3.7）
CLEARED_BLOCK_DAYS = settings.jury_cleared_block_days
# 能算「认定不违规」的结论来源；机器来源的案件「未作处理」（auto）不算
CLEARED_SOURCES = ("jury", "panel", "admin")
MACHINE_REASON_PREFIX = "机器筛查"
# 按案件批量查东西时一批多少个：一条查询里的绑定参数要留在老版本 SQLite 的 999 个以内
LOOKUP_BATCH = 200

# 一键归档历史遗留案件（docs/moderation-v2.md）：未经审理直接收起来，不是结论。
# 案件 status = archived、verdict_source = archive；举报 status = rejected、action = archived，不计入举报人信誉。
ARCHIVED = "archived"
ARCHIVE_SOURCE = "archive"
ARCHIVE_ACTION = "archived"
ARCHIVE_NOTE = "历史遗留，未经审理直接归档"
# 真正下过结论的案件状态；open 和 archived 都不算
VERDICT_STATUSES = ("resolved", "dismissed")

_last_queue_event_at: float | None = None
_jury_sweep_task: asyncio.Task[None] | None = None


def reason_category(reason: str | None) -> str:
    return REPORT_REASONS.get(reason or "", ("other", 0.8))[0]


def is_unattended(case: ReportCase) -> bool:
    """机器来源、至今没有人举报的案件：没有举报人在等结果。"""
    return case.case_origin == "machine" and not case.report_count


def default_category(case: ReportCase) -> str:
    """定案时没人指定类别的默认值：按举报理由映射；没有举报的机器案件用机器给的类别。"""
    if is_unattended(case) and case.machine_category in penalty_service.CATEGORIES:
        return case.machine_category
    return reason_category(case.primary_reason)


def machine_reasons(case: ReportCase) -> list[dict[str, Any]]:
    """没有举报的机器案件给委员 / 公示栏看的「举报理由」：一项「机器筛查 · 类别」。有举报的案件返回空列表。"""
    if not is_unattended(case):
        return []
    label = penalty_service.category_label(case.machine_category) if case.machine_category else None
    return [{"reason": f"{MACHINE_REASON_PREFIX} · {label}" if label else MACHINE_REASON_PREFIX, "count": 1}]


def validate_report_payload(target_type: ReportTargetType, reason: str, details: str | None, attachments: list[str], evidence_message_ids: list[int]) -> None:
    """提交门槛：挡掉没有任何信息量的举报。"""
    if reason not in REPORT_REASONS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "invalid_reason", "message": "请选择一个举报理由", "reasons": list(REPORT_REASONS)},
        )
    text = (details or "").strip()
    if reason == OTHER_REASON and len(text) < OTHER_REASON_MIN_DETAILS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "details_required", "message": f"选择「其他」时请写明具体情况（至少 {OTHER_REASON_MIN_DETAILS} 个字）"},
        )
    if target_type in {ReportTargetType.USER, ReportTargetType.CONVERSATION} and not (evidence_message_ids or attachments or len(text) >= OTHER_REASON_MIN_DETAILS):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "evidence_required", "message": "举报用户或群聊需要提供证据：选中相关消息、上传截图，或写明具体情况"},
        )


def target_key(target_type: ReportTargetType, target_id: int, conversation_id: int | None, message_id: int | None, reported_user_id: int | None) -> str:
    if target_type == ReportTargetType.MESSAGE:
        return f"message:{message_id or target_id}"
    if target_type == ReportTargetType.POST:
        return f"post:{target_id}"
    if target_type == ReportTargetType.CONVERSATION:
        return f"conversation:{conversation_id or target_id}"
    key = f"user:{reported_user_id or target_id}"
    return f"{key}@{conversation_id}" if conversation_id is not None else key


# ---------------------------------------------------------------- 举报信誉


async def reporter_credit(db: AsyncSession, reporter_id: int) -> dict[str, Any]:
    """按近 90 天的举报结果算信誉。被认定恶意的举报按三次无效计。

    被归档的举报（action = archived）不算：那是我们没处理，不是举报人乱举报。
    """
    now = datetime.now(UTC)
    since = now - timedelta(days=CREDIT_WINDOW_DAYS)
    rows = await db.execute(
        select(UserReport.status, UserReport.malicious, func.count())
        .where(
            UserReport.reporter_id == reporter_id,
            UserReport.created_at >= since,
            or_(UserReport.action.is_(None), UserReport.action != ARCHIVE_ACTION),
        )
        .group_by(UserReport.status, UserReport.malicious)
    )
    upheld = dismissed = malicious = pending = 0
    for report_status, is_malicious, count in rows.all():
        if is_malicious:
            malicious += count
        elif report_status == ReportStatus.REVIEWED:
            upheld += count
        elif report_status == ReportStatus.REJECTED:
            dismissed += count
        else:
            pending += count
    score = (upheld + 2) / (upheld + dismissed + 3 * malicious + 4)
    if score >= 0.6:
        quota = 20
    elif score >= 0.35:
        quota = 10
    elif score >= 0.2:
        quota = 3
    else:
        quota = 0
    day_start = now - timedelta(hours=24)
    used = int(
        (
            await db.execute(
                select(func.count()).select_from(UserReport).where(UserReport.reporter_id == reporter_id, UserReport.created_at >= day_start)
            )
        ).scalar_one()
        or 0
    )
    return {
        "score": round(score, 3),
        "weight": round(0.5 + score, 3),
        "upheld": upheld,
        "dismissed": dismissed,
        "malicious": malicious,
        "pending": pending,
        "daily_quota": quota,
        "used_today": used,
        "remaining_today": max(0, quota - used),
        "window_days": CREDIT_WINDOW_DAYS,
    }


async def ensure_quota(db: AsyncSession, reporter_id: int) -> dict[str, Any]:
    credit = await reporter_credit(db, reporter_id)
    if credit["daily_quota"] == 0:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"code": "report_restricted", "message": "你近期有多次举报被认定为无效或恶意，暂时不能举报"},
        )
    if credit["remaining_today"] <= 0:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"code": "report_quota_exceeded", "message": f"今天的举报次数已用完（每日 {credit['daily_quota']} 次）"},
        )
    return credit


# ---------------------------------------------------------------- 建案 / 并案


async def _jury_eligible(db: AsyncSession, report: UserReport) -> bool:
    """私聊不给风纪委员看：只有群聊消息和公开动态能进他们的队列。"""
    if report.target_type == ReportTargetType.MESSAGE and report.conversation_id is not None:
        conversation = await db.get(Conversation, report.conversation_id)
        return conversation is not None and conversation.type == ConversationType.GROUP and not conversation.is_temporary
    if report.target_type == ReportTargetType.POST:
        post = await db.get(Post, report.target_id)
        return post is not None and post.visibility == PostVisibility.PUBLIC
    return False


async def is_sensitive(db: AsyncSession, case: ReportCase) -> bool:
    """色情低俗 / 涉未成年类：任意一条举报选了这个理由，或机器把内容判到了这两类。"""
    return case.id in await sensitive_case_ids(db, [case])


async def sensitive_case_ids(db: AsyncSession, cases: list[ReportCase]) -> set[int]:
    """is_sensitive 的批量版：一批案件里属于敏感类别的那些。

    每 LOOKUP_BATCH 个案件两条查询，不随案件数逐个增加（出题时要把整个题库过一遍）。
    """
    from app.services import moderation as moderation_service

    found = {case.id for case in cases if case.machine_category in SENSITIVE_MACHINE_CATEGORIES}
    for start in range(0, len(cases), LOOKUP_BATCH):
        batch = cases[start : start + LOOKUP_BATCH]
        case_ids = [case.id for case in batch]
        report_case: dict[int, int] = {}
        reports = await db.execute(select(UserReport.id, UserReport.case_id, UserReport.reason).where(UserReport.case_id.in_(case_ids)))
        for report_id, case_id, reason in reports.all():
            report_case[report_id] = case_id
            if reason in SENSITIVE_REASONS:
                found.add(case_id)
        by_message: dict[int, list[int]] = {}
        by_post: dict[int, list[int]] = {}
        for case in batch:
            if case.target_type == "message" and case.message_id is not None:
                by_message.setdefault(case.message_id, []).append(case.id)
            elif case.target_type == "post":
                by_post.setdefault(case.target_id, []).append(case.id)
        clauses = [ContentModerationRecord.report_id.in_(select(UserReport.id).where(UserReport.case_id.in_(case_ids)))]
        if by_message:
            clauses.append(ContentModerationRecord.message_id.in_(list(by_message)))
        if by_post:
            clauses.append(ContentModerationRecord.post_id.in_(list(by_post)))
        records = await db.execute(
            select(ContentModerationRecord)
            .where(or_(*clauses), ContentModerationRecord.decision.in_(("review", "block")))
            .order_by(ContentModerationRecord.id.desc())
        )
        for record in records.scalars().all():
            owners = {report_case.get(record.report_id), *by_message.get(record.message_id, ()), *by_post.get(record.post_id, ())} - {None} - found
            if not owners:
                continue
            # 图片管线不给 categories，只在原始结果里带 category；block 的记录再按处罚口径换算一次。
            verdict = moderation_service._verdict_for_record(record)
            categories = [*moderation_service._json_list(record.categories), moderation_service._raw_result(record).get("category"), verdict[0] if verdict else None]
            if any(isinstance(item, str) and item in SENSITIVE_MACHINE_CATEGORIES for item in categories):
                found.update(owners)
    return found


def _enter_jury_queue(case: ReportCase, now: datetime) -> None:
    case.queue = "jury"
    case.queued_at = now
    case.jury_outcome = None
    case.escalated_at = None
    case.updated_at = now


def _leave_jury_queue(case: ReportCase, now: datetime, outcome: str | None) -> None:
    """outcome 取 no_consensus / need_context / timeout / manual；因敏感类别被撤出来的不算委员评审的结果，传 None。"""
    case.queue = "admin"
    case.jury_outcome = outcome
    case.escalated_at = now
    case.updated_at = now


async def escalate_from_jury(db: AsyncSession, case: ReportCase, now: datetime, outcome: str) -> None:
    """委员没能定案（no_consensus / need_context / timeout）时把案件转出去。调用方负责 commit，之后调 notify_escalated。

    「转到哪里」只在这一处决定：此刻凑得齐合议庭的进资深合议；凑不齐的转管理员（和三期之前一样）。
    机器来源、没有举报人的案件不转管理员，凑不齐人就以「未作处理」结案。
    """
    from app.services import jury_panel

    _leave_jury_queue(case, now, outcome)
    try:
        convene = await jury_panel.can_convene(db, case, now)
    except Exception:
        # 查不出能不能凑齐人时按凑不齐处理：案件不能因为合议这一步卡在委员队列里
        logger.exception("Failed to check panel availability", extra={"case_id": case.id})
        convene = False
    if convene:
        case.queue = "panel"
        case.panel_at = now
        case.panel_outcome = None
        return
    case.panel_outcome = "unavailable"
    if is_unattended(case):
        await _close_unhandled(db, case)


async def leave_panel(db: AsyncSession, case: ReportCase, now: datetime, outcome: str | None) -> None:
    """合议没能定案时把案件转出去。outcome 取 timeout / unavailable / manual；因敏感类别被撤出来的传 None。

    调用方负责 commit，之后调 notify_escalated。去向和 escalate_from_jury 凑不齐人时一样。
    """
    case.queue = "admin"
    case.panel_outcome = outcome
    case.updated_at = now
    if is_unattended(case):
        await _close_unhandled(db, case)


async def _close_unhandled(db: AsyncSession, case: ReportCase) -> None:
    """机器来源、没有举报人的案件没人能定案：以「未作处理」结案。

    不是「认定不违规」：不记处罚、不进公示栏、不算委员的一致率，之后有人举报同一条内容照常受理。
    """
    await _close(
        db,
        case,
        upheld=False,
        source="auto",
        actor_id=None,
        category=None,
        level=None,
        note="机器筛查的内容，委员没有形成结论，未作处理",
        violation_id=None,
        action="none",
    )


async def notify_escalated(db: AsyncSession, case: ReportCase) -> None:
    """escalate_from_jury / leave_panel 提交之后调用：通知后台，让委员重拉角标。"""
    if case.status != "open":
        # 没人能定案的机器案件已经结了：投过票的委员刷新一下详情
        await _broadcast_admin(case, "closed")
        await notify_jurors_settled(db, case)
    else:
        await _broadcast_admin(case, "escalated")
    await notify_jury_queue_changed(db)


async def route_case(db: AsyncSession, case: ReportCase, now: datetime) -> None:
    """机器预审之后分队列：能给委员看、且不属于敏感类别的进委员队列，其余留给管理员。"""
    if case.jury_eligible and await is_sensitive(db, case):
        case.jury_block_reason = "sensitive"
    if case.jury_eligible and case.jury_block_reason is None:
        _enter_jury_queue(case, now)
    else:
        case.queue = "admin"
        case.updated_at = now


def _refresh_priority(case: ReportCase) -> None:
    weight = REPORT_REASONS.get(case.primary_reason or "", ("other", 0.8))[1]
    priority = case.reporter_weight * weight
    if case.machine_decision == "allow":
        priority *= 0.7
    elif case.machine_decision == "review":
        priority *= 1.3
    case.priority = round(priority, 3)


async def find_open_case(db: AsyncSession, key: str) -> ReportCase | None:
    result = await db.execute(
        select(ReportCase).where(ReportCase.target_key == key, ReportCase.status == "open").order_by(ReportCase.id.desc()).limit(1)
    )
    return result.scalar_one_or_none()


async def already_reported(db: AsyncSession, reporter_id: int, case_id: int) -> bool:
    result = await db.execute(
        select(UserReport.id).where(UserReport.reporter_id == reporter_id, UserReport.case_id == case_id).limit(1)
    )
    return result.scalar_one_or_none() is not None


async def attach_report(db: AsyncSession, report: UserReport, *, weight: float) -> tuple[ReportCase, bool]:
    """把一条新举报挂到案件上，返回 (案件, 是否新建)。调用方负责 commit。"""
    key = target_key(report.target_type, report.target_id, report.conversation_id, report.message_id, report.reported_user_id)
    case = await find_open_case(db, key)
    created = case is None
    now = datetime.now(UTC)
    if case is None:
        case = ReportCase(
            target_key=key,
            target_type=report.target_type.value,
            target_id=report.target_id,
            conversation_id=report.conversation_id,
            message_id=report.message_id,
            reported_user_id=report.reported_user_id,
            first_report_id=report.id,
            jury_eligible=await _jury_eligible(db, report),
            created_at=now,
        )
        # 只有公开动态的内容可以公示；群聊消息是否公示由开关决定，不在这里固化。
        case.content_publishable = bool(case.jury_eligible and report.target_type == ReportTargetType.POST)
        db.add(case)
        await db.flush()
    report.case_id = case.id
    await db.flush()
    await _recount(db, case)
    case.reporter_weight = round((case.reporter_weight or 0.0) + weight, 3) if not created else round(weight, 3)
    case.updated_at = now
    _refresh_priority(case)
    if case.jury_eligible and report.reason in SENSITIVE_REASONS:
        case.jury_block_reason = "sensitive"
        if case.queue == "jury":
            # 已经在委员队列里的也要撤出来。事务提交后由调用方发 jury.queue.changed。
            _leave_jury_queue(case, now, None)
            setattr(case, "_left_jury_queue", True)
        elif case.queue == "panel":
            # 合议中的同理（案件此时已经有举报，不会走到「未作处理」）
            await leave_panel(db, case, now, None)
            setattr(case, "_left_jury_queue", True)
    return case, created


async def _recount(db: AsyncSession, case: ReportCase) -> None:
    rows = await db.execute(select(UserReport.reason).where(UserReport.case_id == case.id))
    reasons = [row[0] for row in rows.all()]
    case.report_count = len(reasons)
    if reasons:
        case.primary_reason = Counter(reasons).most_common(1)[0][0]


# ---------------------------------------------------------------- 机器预审


def schedule_triage(case_id: int) -> None:
    import asyncio

    task = asyncio.create_task(triage_case(case_id))

    def _consume(done: asyncio.Task) -> None:
        try:
            done.result()
        except Exception:
            logger.exception("Case triage failed", extra={"case_id": case_id})

    task.add_done_callback(_consume)


async def triage_case(case_id: int) -> None:
    """新案件先让审核服务看一遍：明确违规的当场处理结案，其余分到风纪委员或管理员。"""
    async with AsyncSessionLocal() as db:
        case = await db.get(ReportCase, case_id)
        if case is None or case.status != "open" or case.queue != "triage":
            return
        if case.target_type == "message" and case.message_id is not None and settings.content_moderation_enabled:
            from app.services import moderation as moderation_service

            await moderation_service._moderate_message_task(case.message_id, ModerationSource.REPORT_REVIEW, case.first_report_id, db)
            await db.refresh(case)
            records = (
                await db.execute(
                    select(ContentModerationRecord)
                    .where(ContentModerationRecord.report_id == case.first_report_id)
                    .order_by(ContentModerationRecord.id.desc())
                )
            ).scalars().all()
            usable = [record for record in records if record.decision in {"allow", "review", "block"}]
            if usable:
                worst = max(usable, key=lambda item: ({"allow": 0, "review": 1, "block": 2}[item.decision], item.severity))
                case.machine_decision = worst.decision
                case.machine_confidence = worst.confidence
                if worst.decision == "block":
                    violation = (
                        await db.execute(
                            select(UserViolation)
                            .where(UserViolation.target_type == "message", UserViolation.target_id == case.message_id, UserViolation.status == "active")
                            .order_by(UserViolation.id.desc())
                            .limit(1)
                        )
                    ).scalar_one_or_none()
                    case.machine_category = violation.category if violation else None
                    case.machine_level = violation.level if violation else None
                    await _close(
                        db,
                        case,
                        upheld=True,
                        source="auto",
                        actor_id=None,
                        category=violation.category if violation else reason_category(case.primary_reason),
                        level=violation.level if violation else 0,
                        note="自动审核确认违规，内容已处理",
                        violation_id=violation.id if violation else None,
                        action="recall_message",
                    )
                    await db.commit()
                    await notify_case_closed(db, case)
                    return
        if case.status != "open":
            return
        await route_case(db, case, datetime.now(UTC))
        _refresh_priority(case)
        await db.commit()
        await _broadcast_admin(case, "queued")
        if case.queue == "jury":
            await notify_jury_queue_changed(db)


# ---------------------------------------------------------------- 结案


async def _close(
    db: AsyncSession,
    case: ReportCase,
    *,
    upheld: bool,
    source: str,
    actor_id: int | None,
    category: str | None,
    level: int | None,
    note: str | None,
    violation_id: int | None,
    action: str,
    malicious_reporter_ids: set[int] | None = None,
) -> None:
    now = datetime.now(UTC)
    case.status = "resolved" if upheld else "dismissed"
    case.verdict_source = source
    case.verdict_category = category if upheld else None
    case.verdict_level = level if upheld else None
    case.verdict_note = note.strip()[:2000] if note else None
    case.verdict_by_id = actor_id
    case.violation_id = violation_id
    case.closed_at = now
    case.updated_at = now
    if case.queue == "jury" and case.jury_outcome is None:
        # 在委员队列里结的案：委员自己定的是 consensus，管理员直接接手的算 manual。
        case.jury_outcome = "consensus" if source == "jury" else "manual"
        if source != "jury":
            case.escalated_at = now
    if case.queue == "panel" and case.panel_outcome is None:
        # 在合议里结的案：合议自己定的是 consensus，管理员直接接手的算 manual。
        case.panel_outcome = "consensus" if source == "panel" else "manual"
    if source in PUBLISHABLE_SOURCES:
        voted = await db.execute(
            select(JuryVote.id).where(JuryVote.case_id == case.id, JuryVote.voted_at.is_not(None), JuryVote.is_quality_check.is_(False)).limit(1)
        )
        case.publishable = voted.scalar_one_or_none() is not None
        # 结论一出就回填每一票是否一致：委员收到 jury.case.settled 去拉详情时，my_vote.agreed 已经有值了。
        from app.services import jury as jury_service

        await jury_service._settle_votes(db, case, upheld)
    malicious = malicious_reporter_ids or set()
    reports = (await db.execute(select(UserReport).where(UserReport.case_id == case.id))).scalars().all()
    for report in reports:
        if report.status != ReportStatus.PENDING:
            continue
        report.status = ReportStatus.REVIEWED if upheld else ReportStatus.REJECTED
        report.reviewer_id = actor_id
        report.review_note = case.verdict_note
        report.action = action if upheld else "none"
        report.reviewed_at = now
        if not upheld and report.reporter_id in malicious:
            report.malicious = True


async def _remove_content(db: AsyncSession, case: ReportCase, actor_id: int | None) -> tuple[str, dict[str, Any] | None]:
    """处理违规内容，返回 (动作名, 之后要广播的信息)。"""
    now = datetime.now(UTC)
    if case.target_type == "message" and case.message_id is not None:
        message = await db.get(Message, case.message_id)
        if message is None or message.recalled_at is not None:
            return "none", None
        message.recalled_at = now
        message.recalled_by_id = actor_id
        conversation = await db.get(Conversation, message.conversation_id)
        if conversation is not None:
            conversation.updated_at = now
        from app.services.messages import clear_mentions_of

        await clear_mentions_of(db, message)
        return "recall_message", {"kind": "message", "message_id": message.id, "conversation_id": message.conversation_id}
    if case.target_type == "post":
        post = await db.get(Post, case.target_id)
        if post is None or post.deleted_at is not None:
            return "none", None
        from app.services import posts as post_service

        recipients = await post_service.post_recipient_ids(db, post)
        post.deleted_at = now
        post.moderation_status = ModerationStatus.REJECTED.value
        return "delete_post", {"kind": "post", "post_id": post.id, "recipients": list(recipients), "actor_id": actor_id}
    return "none", None


def _case_summary(snapshot: dict[str, Any]) -> str | None:
    for key in ("message", "post"):
        item = snapshot.get(key)
        if isinstance(item, dict) and isinstance(item.get("content"), str) and item["content"].strip():
            return item["content"].strip()[:120]
    return None


def _parse_snapshot(text: str | None) -> dict[str, Any]:
    if not text:
        return {}
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


async def case_snapshot(db: AsyncSession, case: ReportCase) -> dict[str, Any]:
    if case.first_report_id is None:
        # 机器来源的案件没有首条举报，建案时把快照存在了案件自己身上
        return _parse_snapshot(case.snapshot)
    report = await db.get(UserReport, case.first_report_id)
    return _parse_snapshot(report.target_snapshot) if report is not None else {}


async def case_snapshots(db: AsyncSession, cases: list[ReportCase]) -> dict[int, dict[str, Any]]:
    """case_snapshot 的批量版：案件 id -> 快照，没有快照的给空字典。"""
    by_report = {case.first_report_id: case.id for case in cases if case.first_report_id is not None}
    snapshots: dict[int, dict[str, Any]] = {case.id: {} if case.first_report_id is not None else _parse_snapshot(case.snapshot) for case in cases}
    report_ids = list(by_report)
    for start in range(0, len(report_ids), LOOKUP_BATCH):
        rows = await db.execute(
            select(UserReport.id, UserReport.target_snapshot).where(UserReport.id.in_(report_ids[start : start + LOOKUP_BATCH]))
        )
        for report_id, text in rows.all():
            snapshots[by_report[report_id]] = _parse_snapshot(text)
    return snapshots


async def _punish(
    db: AsyncSession,
    case: ReportCase,
    *,
    source: str,
    actor_id: int | None,
    category: str | None,
    level: int | None,
    delete_content: bool,
    override: penalty_service.Penalty | None,
    note: str | None,
) -> tuple[str, int, int | None, str, dict[str, Any] | None]:
    """认定违规时的落地动作：处理内容、记处罚。返回 (类别, 等级, 违规记录 id, 动作名, 之后要广播的信息)。"""
    category = category or default_category(case)
    if category not in penalty_service.CATEGORIES:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Unknown violation category")
    if level is None:
        level = penalty_service.default_level(category)
    broadcast: dict[str, Any] | None = None
    violation_id: int | None = None
    actions: list[str] = []
    if delete_content:
        removed, broadcast = await _remove_content(db, case, actor_id)
        if removed != "none":
            actions.append(removed)
    if case.reported_user_id is not None:
        target_type = case.target_type if case.target_type in {"message", "post"} else None
        outcome = await penalty_service.record_violation(
            db,
            user_id=case.reported_user_id,
            category=category,
            level=level,
            # 资深合议和委员是同一档：同样受 7 天封禁上限约束
            source="jury" if source in {"jury", "panel"} else "report",
            target_type=target_type,
            target_id=(case.message_id if case.target_type == "message" else case.target_id) if target_type else None,
            conversation_id=case.conversation_id,
            report_id=case.first_report_id,
            case_id=case.id,
            actor_id=actor_id,
            summary=_case_summary(await case_snapshot(db, case)),
            reason=note or case.primary_reason,
            override=override,
        )
        if outcome is not None:
            violation_id = outcome.violation.id
            if outcome.banned:
                actions.append("disable_user")
            elif outcome.penalty.kind != "none":
                actions.append(f"penalty_{outcome.penalty.kind}")
    return category, level, violation_id, "+".join(actions) if actions else "none", broadcast


async def resolve_case(
    db: AsyncSession,
    case_id: int,
    *,
    source: str,
    actor_id: int | None,
    upheld: bool,
    category: str | None = None,
    level: int | None = None,
    delete_content: bool = True,
    override: penalty_service.Penalty | None = None,
    note: str | None = None,
    malicious_reporter_ids: set[int] | None = None,
) -> ReportCase:
    """给案件下结论。source 取 jury / panel / admin。提交事务并推送通知。"""
    case = await db.get(ReportCase, case_id)
    if case is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Case not found")
    # PG：锁住案件这一行并读最新状态，两条路径（委员 / 合议 / 管理员）同时结案时后到的那个看到已结、报 409，不会处罚两次
    await lock_row(db, case)
    if case.status != "open":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Case is already closed")
    broadcast: dict[str, Any] | None = None
    violation_id: int | None = None
    action = "none"
    if upheld:
        category, level, violation_id, action, broadcast = await _punish(
            db, case, source=source, actor_id=actor_id, category=category, level=level, delete_content=delete_content, override=override, note=note
        )
    await _close(
        db,
        case,
        upheld=upheld,
        source=source,
        actor_id=actor_id,
        category=category,
        level=level,
        note=note,
        violation_id=violation_id,
        action=action,
        malicious_reporter_ids=malicious_reporter_ids,
    )
    db.add(
        AdminAuditLog(
            actor_id=actor_id,
            action=f"case.{case.status}",
            target_type="report_case",
            target_id=case.id,
            details=json.dumps(
                {"source": source, "category": case.verdict_category, "level": case.verdict_level, "action": action, "note": case.verdict_note},
                ensure_ascii=False,
            ),
        )
    )
    await db.commit()
    await penalty_service.flush_notifications(db)
    if broadcast is not None:
        await _broadcast_removed(db, broadcast)
    await notify_case_closed(db, case)
    return case


async def sync_from_report_review(db: AsyncSession, report: UserReport, *, actor_id: int) -> ReportCase | None:
    """旧的逐条审核接口处理完一条举报后，把结论同步给同案的其它举报。调用方负责 commit。"""
    if report.case_id is None:
        return None
    case = await db.get(ReportCase, report.case_id)
    if case is None or case.status != "open":
        return None
    upheld = report.status == ReportStatus.REVIEWED
    violation = None
    if upheld:
        violation = (
            await db.execute(
                select(UserViolation).where(UserViolation.report_id == report.id, UserViolation.status == "active").order_by(UserViolation.id.desc()).limit(1)
            )
        ).scalar_one_or_none()
        if violation is not None:
            violation.case_id = case.id
    await _close(
        db,
        case,
        upheld=upheld,
        source="admin",
        actor_id=actor_id,
        category=violation.category if violation else (reason_category(case.primary_reason) if upheld else None),
        level=violation.level if violation else (0 if upheld else None),
        note=report.review_note,
        violation_id=violation.id if violation else None,
        action=report.action or "none",
    )
    return case


async def move_case(db: AsyncSession, case_id: int, queue: str, *, actor_id: int | None) -> ReportCase:
    case = await db.get(ReportCase, case_id)
    if case is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Case not found")
    if case.status != "open":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Case is already closed")
    if queue == "jury" and not case.jury_eligible:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="该案件涉及私聊或账号本身，不能交给风纪委员")
    now = datetime.now(UTC)
    was_jury = case.queue == "jury"
    was_panel = case.queue == "panel"
    if queue == "jury":
        if await is_sensitive(db, case):
            case.jury_block_reason = "sensitive"
            await db.commit()
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"code": "case_sensitive", "message": "色情低俗 / 涉未成年类的案件只能由管理员处理，不能交给风纪委员"},
            )
        if not was_jury:
            _enter_jury_queue(case, now)
    elif was_jury:
        _leave_jury_queue(case, now, "manual")
    else:
        case.queue = queue
        case.updated_at = now
    if was_panel and case.queue != "panel":
        # 管理员把合议中的案件挪走，合议到此为止
        case.panel_outcome = "manual"
    await db.commit()
    await _broadcast_admin(case, "queued")
    if was_panel or was_jury != (case.queue == "jury"):
        await notify_jury_queue_changed(db)
    return case


async def escalate_stale_jury_cases(db: AsyncSession) -> int:
    """风纪委员人手不够或都要回避时，案件不能一直挂着：超时未决的转出去（资深合议或管理员，见 escalate_from_jury）。

    从进入委员队列算起，不被后续举报重置；上线前就在队列里的案件没有 queued_at，退回按 updated_at 算。
    """
    now = datetime.now(UTC)
    cutoff = now - timedelta(hours=JURY_TIMEOUT_HOURS)
    result = await db.execute(
        select(ReportCase).where(
            ReportCase.status == "open", ReportCase.queue == "jury", func.coalesce(ReportCase.queued_at, ReportCase.updated_at) < cutoff
        )
    )
    cases = list(result.scalars().all())
    for case in cases:
        await escalate_from_jury(db, case, now, "timeout")
    if cases:
        await db.commit()
        for case in cases:
            await notify_escalated(db, case)
    return len(cases)


async def sweep_jury_queue(db: AsyncSession) -> dict[str, int]:
    """委员队列的定时整理：补 queued_at、撤出敏感类别的案件、把超时的转给管理员。"""
    now = datetime.now(UTC)
    stats = {"backfilled": 0, "sensitive": 0, "timeout": 0}
    queued = (await db.execute(select(ReportCase).where(ReportCase.status == "open", ReportCase.queue == "jury"))).scalars().all()
    blocked: list[ReportCase] = []
    for case in queued:
        if case.queued_at is None:
            case.queued_at = case.updated_at
            stats["backfilled"] += 1
        if await is_sensitive(db, case):
            case.jury_block_reason = "sensitive"
            _leave_jury_queue(case, now, None)
            blocked.append(case)
    stats["sensitive"] = len(blocked)
    if stats["backfilled"] or blocked:
        await db.commit()
    for case in blocked:
        await _broadcast_admin(case, "escalated")
    if blocked:
        await notify_jury_queue_changed(db)
    stats["timeout"] = await escalate_stale_jury_cases(db)
    return stats


async def sweep_jury_housekeeping(db: AsyncSession) -> dict[str, int]:
    """定时循环里和队列无关的两件事：任期届满的委员续任 / 标记到期，一致通过的案件自动进题库。"""
    from app.services import jury as jury_service
    from app.services import jury_bank

    return {"terms": await jury_service.renew_expired_terms(db), "questions": await jury_bank.auto_include(db)}


async def sweep_senior_queues(db: AsyncSession) -> dict[str, int]:
    """定时循环里资深委员那两条队列：合议超时 / 凑不齐人的转出去，申诉复审和复议超时的转管理员。"""
    from app.services import jury_panel, jury_reviews

    return {"panel": await jury_panel.sweep(db), "reviews": await jury_reviews.sweep(db)}


async def sweep_jury_hall(db: AsyncSession) -> dict[str, int]:
    """定时循环里议事厅那一件事：表决到期的提案出结果（通过 / 未通过）。读帖子时也会惰性结算，这里保证没人看的时候也按时出结果。"""
    from app.services import jury_hall

    return {"proposals": await jury_hall.settle_due_proposals(db)}


async def _jury_sweep_worker() -> None:
    while True:
        for name, job in (
            ("queue sweep", sweep_jury_queue),
            ("housekeeping", sweep_jury_housekeeping),
            ("senior sweep", sweep_senior_queues),
            ("hall sweep", sweep_jury_hall),
        ):
            try:
                async with AsyncSessionLocal() as db:
                    await job(db)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Jury %s failed", name)
        await asyncio.sleep(settings.jury_sweep_interval_seconds)


def start_jury_sweep_worker() -> None:
    global _jury_sweep_task
    if _jury_sweep_task is not None and not _jury_sweep_task.done():
        return
    _jury_sweep_task = asyncio.create_task(_jury_sweep_worker())


async def stop_jury_sweep_worker() -> None:
    global _jury_sweep_task
    task = _jury_sweep_task
    _jury_sweep_task = None
    if task is None or task.done():
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


# ---------------------------------------------------------------- 改判


async def _restore_content(db: AsyncSession, case: ReportCase, actions: str) -> bool:
    """改判为不违规后把内容放回去。只恢复这个案件自己处理掉的内容，用户自己撤回 / 删除的不动。"""
    if case.target_type == "message" and case.message_id is not None and "recall_message" in actions:
        message = await db.get(Message, case.message_id)
        if message is None or message.recalled_at is None:
            return False
        message.recalled_at = None
        message.recalled_by_id = None
        return True
    if case.target_type == "post" and "delete_post" in actions:
        post = await db.get(Post, case.target_id)
        # 旧的逐条审核接口删动态时会连图片文件一起清掉，那种恢复不了；按案件结案删的会标成 rejected。
        if post is None or post.deleted_at is None or str(post.moderation_status) != ModerationStatus.REJECTED.value:
            return False
        post.deleted_at = None
        post.moderation_status = ModerationStatus.APPROVED.value
        return True
    return False


async def _case_violations(db: AsyncSession, case: ReportCase) -> list[UserViolation]:
    clauses = [UserViolation.case_id == case.id]
    if case.violation_id is not None:
        clauses.append(UserViolation.id == case.violation_id)
    result = await db.execute(select(UserViolation).where(or_(*clauses), UserViolation.status == "active").order_by(UserViolation.id.asc()))
    return list(result.scalars().all())


async def _apply_overturn(
    db: AsyncSession,
    case: ReportCase,
    *,
    actor_id: int | None,
    upheld: bool,
    category: str | None,
    level: int | None,
    note: str | None,
    source: str = "admin",
) -> dict[str, Any]:
    """改判的落库部分，不提交事务。返回事务提交之后要用的信息。

    source 是谁改判的：admin，或 panel（举报人复议由资深委员改判，补记的处罚同样受 7 天上限约束）。
    """
    now = datetime.now(UTC)
    text = note.strip()[:2000] if note else None
    reports = await case_reports(db, case.id)
    info: dict[str, Any] = {"case_id": case.id, "content_restored": False, "broadcast": None, "revoked": [], "violation_id": None}
    before = {"status": case.status, "category": case.verdict_category, "level": case.verdict_level}
    if upheld:
        category, level, violation_id, action, info["broadcast"] = await _punish(
            db, case, source=source, actor_id=actor_id, category=category, level=level, delete_content=True, override=None, note=text
        )
        case.status = "resolved"
        case.verdict_category = category
        case.verdict_level = level
        case.violation_id = violation_id if violation_id is not None else case.violation_id
        info["violation_id"] = violation_id
    else:
        actions = " ".join(report.action or "" for report in reports)
        for violation in await _case_violations(db, case):
            info["revoked"].append(
                await penalty_service.revoke_violation(db, violation.id, actor_id=actor_id, reason=f"案件改判：{text or ''}".rstrip("："), sync_case=False)
            )
        case.status = "dismissed"
        case.verdict_category = None
        case.verdict_level = None
        info["content_restored"] = await _restore_content(db, case, actions)
        action = "none"
    for report in reports:
        report.status = ReportStatus.REVIEWED if upheld else ReportStatus.REJECTED
        # 改判成违规说明举报是对的；改判成不违规只是不成立，不算恶意。
        report.malicious = False
        report.reviewer_id = actor_id
        report.review_note = text
        report.action = action
        report.reviewed_at = now
    case.overturned_at = now
    case.overturned_by_id = actor_id
    case.overturn_note = text
    case.updated_at = now
    if case.is_golden and case.golden_outcome is None:
        # 题目的标准答案就是案件原来的结论，结论被推翻后不能再当题目；
        # 管理员另写过标准答案的保留（出题时本来就不出被改判的案件）。
        case.is_golden = False
        case.golden_verified = False
        case.golden_source = "excluded"
    # 这三个模块都在模块级导入了 cases，只能在函数里导入
    from app.services import jury as jury_service
    from app.services import jury_precedents, jury_reviews

    await jury_service.resettle_votes(db, case, upheld)
    # 这个案件有还没出结果的举报人复议时，改判就是它的结果
    await jury_reviews.on_case_overturned(db, case, upheld=upheld)
    # 被改判的案件不能再当判例：撤下（因它才进题库的题一并移出）
    info["precedent_withdrawn"] = await jury_precedents.on_case_overturned(db, case)
    db.add(
        AdminAuditLog(
            actor_id=actor_id,
            action="case.overturned",
            target_type="report_case",
            target_id=case.id,
            details=json.dumps(
                {
                    "from": before,
                    "to": {"status": case.status, "category": case.verdict_category, "level": case.verdict_level},
                    "content_restored": info["content_restored"],
                    "revoked_violation_ids": [violation.id for violation in info["revoked"]],
                    "violation_id": info["violation_id"],
                    "precedent_withdrawn": info["precedent_withdrawn"],
                    "note": text,
                    "source": source,
                },
                ensure_ascii=False,
            ),
        )
    )
    return info


async def overturn_case(
    db: AsyncSession,
    case_id: int,
    *,
    actor_id: int | None,
    upheld: bool,
    category: str | None = None,
    level: int | None = None,
    note: str | None,
    source: str = "admin",
) -> dict[str, Any]:
    """改判一个已结案件。返回 {"case", "content_restored", "violation_id", "revoked_violation_ids"}。

    违规 -> 不违规：撤销这个案件产生的处罚，能恢复的内容恢复；不违规 -> 违规：按结案的方式补记处罚、处理内容。
    两个方向都会同步举报结论和委员的一致率。note 会出现在公示栏里。
    """
    case = await db.get(ReportCase, case_id)
    if case is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Case not found")
    if case.status not in VERDICT_STATUSES:
        # 未结和已归档的案件都没有结论
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={"code": "case_not_closed", "message": "案件还没有结论，不能改判"})
    if upheld == (case.status == "resolved"):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={"code": "verdict_unchanged", "message": "改判结果和现有结论相同"})
    info = await _apply_overturn(db, case, actor_id=actor_id, upheld=upheld, category=category, level=level, note=note, source=source)
    await db.commit()
    await penalty_service.flush_notifications(db)
    for violation in info["revoked"]:
        await penalty_service.notify_violation_updated(db, violation, "revoked")
    if info["broadcast"] is not None:
        await _broadcast_removed(db, info["broadcast"])
    await _notify_overturned(db, case)
    return {
        "case": case,
        "content_restored": info["content_restored"],
        "violation_id": info["violation_id"],
        "revoked_violation_ids": [violation.id for violation in info["revoked"]],
    }


async def sync_case_after_revocation(db: AsyncSession, violation: UserViolation, *, actor_id: int | None) -> None:
    """处罚被直接撤销（管理员撤销、申诉通过）后，产生它的案件也按「违规 -> 不违规」改判。

    由 penalties.revoke_violation 调用，不提交事务；通知挂在会话上，等 flush_overturn_notifications 发出去。
    """
    if violation.case_id is None:
        return
    case = await db.get(ReportCase, violation.case_id)
    if case is None or case.status != "resolved":
        return
    if await _case_violations(db, case):
        # 同一个案件还有别的处罚在生效（比如被更高等级取代过），结论仍然成立。
        return
    # 这段说明会出现在公示栏里，不直接照抄管理员写的撤销理由。
    note = "申诉通过，处罚已撤销" if violation.appeal_status == "accepted" else "处罚已撤销"
    await _apply_overturn(db, case, actor_id=actor_id, upheld=False, category=None, level=None, note=note)
    db.info.setdefault("overturned_case_ids", []).append(case.id)


async def flush_overturn_notifications(db: AsyncSession) -> None:
    """事务提交后调用：把 sync_case_after_revocation 攒下的改判通知发出去。"""
    case_ids: list[int] = db.info.pop("overturned_case_ids", [])
    for case_id in case_ids:
        case = await db.get(ReportCase, case_id)
        if case is not None:
            await _notify_overturned(db, case)


async def _notify_overturned(db: AsyncSession, case: ReportCase) -> None:
    await _notify_reporters(db, case)
    await _broadcast_admin(case, "overturned")
    await notify_jurors_settled(db, case)
    await _flush_jury_events(db)


async def _flush_jury_events(db: AsyncSession) -> None:
    """结算票时攒下的风纪值 / 层级 / 成就事件，事务提交之后在这里发出去。"""
    from app.services import jury_points

    await jury_points.flush_events(db)


# ---------------------------------------------------------------- 通知


async def _broadcast_admin(case: ReportCase, action: str) -> None:
    from app.websocket.manager import admin_manager

    try:
        await admin_manager.broadcast("admin.case.changed", {"case_id": case.id, "queue": case.queue, "status": case.status, "action": action})
    except Exception:
        logger.exception("Failed to broadcast case change", extra={"case_id": case.id})


async def _broadcast_removed(db: AsyncSession, info: dict[str, Any]) -> None:
    from app.websocket.manager import admin_manager, manager

    try:
        if info["kind"] == "message":
            # 和用户自己撤回消息走同一条路（conversations.recall_message）：负载只有撤回信息、只序列化一次，
            # 只发给此刻在线的群成员，机器人和游戏模式的观众也一并同步。
            # 原来是给群里每个成员各查一遍、各序列化一遍完整消息，耗时随群人数线性增长，几千人的群要几十秒。
            from app.api.v1.conversations import online_conversation_member_ids, send_message_updates
            from app.services import messages as message_service

            message = await message_service.get_message(db, info["message_id"])
            member_ids = await online_conversation_member_ids(db, info["conversation_id"])
            await send_message_updates(db, "message.recalled", message, member_ids)
            await admin_manager.broadcast(
                "admin.message.changed", {"message_id": message.id, "conversation_id": message.conversation_id, "action": "recalled"}
            )
        else:
            await manager.send_to_users(
                set(info["recipients"]), "post.deleted", {"post_id": info["post_id"], "actor_id": info["actor_id"], "moderation": True}
            )
            await admin_manager.broadcast("admin.post.changed", {"post_id": info["post_id"], "action": "deleted"})
    except Exception:
        logger.exception("Failed to broadcast removed content")


def report_result_text(report: UserReport) -> tuple[str, str]:
    """给举报人看的处理结果 (标题, 正文)。不透露对被举报人的具体处罚。"""
    if report.action == ARCHIVE_ACTION:
        return "你的举报已归档", f"你举报的「{report.reason}」提交得比较早，没能及时处理，已归档。这不影响你的举报信誉，如果问题仍然存在可以重新举报。"
    if report.status == ReportStatus.REVIEWED:
        return "你的举报已处理", f"你举报的「{report.reason}」经核实属实，相关内容和账号已按社区规则处理。感谢你维护社区环境。"
    if report.malicious:
        return "你的举报未被采纳", f"你举报的「{report.reason}」经核实不属实，且被认定为无效举报。多次无效举报会降低每日举报次数。"
    return "你的举报未被采纳", f"你举报的「{report.reason}」经核实暂未发现违规。如有新的证据可以重新举报。"


async def _notify_reporters(db: AsyncSession, case: ReportCase) -> None:
    from app.websocket.manager import manager

    try:
        reports = (await db.execute(select(UserReport).where(UserReport.case_id == case.id))).scalars().all()
        for report in reports:
            title, body = report_result_text(report)
            await manager.send_to_user(
                report.reporter_id,
                "report.resolved",
                {"report_id": report.id, "case_id": case.id, "status": report.status.value, "title": title, "body": body},
            )
    except Exception:
        logger.exception("Failed to notify reporters", extra={"case_id": case.id})


async def notify_case_closed(db: AsyncSession, case: ReportCase) -> None:
    await _notify_reporters(db, case)
    await _broadcast_admin(case, "closed")
    await notify_jurors_settled(db, case)
    await _flush_jury_events(db)
    if case.queue in {"jury", "panel"}:
        await notify_jury_queue_changed(db)


async def notify_jurors_settled(db: AsyncSession, case: ReportCase) -> None:
    """案件定案 / 被改判：让投过票的委员刷新详情和历史。发不出去只记日志。

    每个人收到的 case_id 是他自己的评审编号（docs/jury-v2.md 1.0），所以是逐个发的。
    """
    from app.services import jury as jury_service

    if case.status == "open":
        return
    outcome = "violation" if case.status == "resolved" else "clean"
    await jury_service.notify_case_voters(db, case.id, "jury.case.settled", {"outcome": outcome})


async def notify_jury_queue_changed(db: AsyncSession) -> None:
    """有案件进入 / 离开委员队列：让在任委员重拉角标。同一进程 30 秒内最多发一次，不是逐案通知。"""
    global _last_queue_event_at
    from app.websocket.manager import manager

    tick = time.monotonic()
    if _last_queue_event_at is not None and tick - _last_queue_event_at < QUEUE_EVENT_INTERVAL_SECONDS:
        return
    _last_queue_event_at = tick
    try:
        now = datetime.now(UTC)
        juror_ids = (
            await db.execute(
                select(JuryMember.user_id).where(JuryMember.status == "active", or_(JuryMember.term_ends_at.is_(None), JuryMember.term_ends_at > now))
            )
        ).scalars().all()
        if juror_ids:
            await manager.send_to_users(set(juror_ids), "jury.queue.changed", {})
    except Exception:
        logger.exception("Failed to notify jury queue change")


def published_clauses(now: datetime) -> list[Any]:
    """公示栏的进入条件：有委员真实投过票并已定案、定案满公示延迟、相关处罚没有待处理的申诉。"""
    pending_appeal = exists().where(
        or_(UserViolation.case_id == ReportCase.id, UserViolation.id == ReportCase.violation_id),
        UserViolation.appeal_status == "pending",
    )
    return [
        ReportCase.publishable.is_(True),
        ReportCase.status != "open",
        ReportCase.closed_at <= now - timedelta(hours=PUBLISH_DELAY_HOURS),
        ~pending_appeal,
    ]


async def published_case_ids(db: AsyncSession, case_ids: list[int] | set[int], now: datetime | None = None) -> set[int]:
    """这些案件里此刻正在公示栏里的。和公示栏用同一套条件。"""
    wanted = list(set(case_ids))
    if not wanted:
        return set()
    rows = await db.execute(select(ReportCase.id).where(ReportCase.id.in_(wanted), *published_clauses(now or datetime.now(UTC))))
    return set(rows.scalars().all())


# ---------------------------------------------------------------- 查询


async def list_cases(
    db: AsyncSession,
    *,
    status_filter: str | None = "open",
    queue: str | None = None,
    query: str | None = None,
    sort: str = "newest",
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[ReportCase], int, dict[str, int]]:
    """sort 只对未结案件生效：newest 新的在前（默认）/ oldest 旧的在前 / priority 按排队优先级。
    已结、已归档的列表固定按结案时间倒序。"""
    clauses = []
    if status_filter:
        clauses.append(ReportCase.status == status_filter)
    if queue:
        clauses.append(ReportCase.queue == queue)
    if query and query.strip():
        text = query.strip()
        conditions = [ReportCase.target_key.ilike(f"%{text}%"), ReportCase.primary_reason.ilike(f"%{text}%")]
        if text.isdigit():
            number = int(text)
            conditions.extend([ReportCase.id == number, ReportCase.reported_user_id == number, ReportCase.conversation_id == number])
        clauses.append(or_(*conditions))
    total = int((await db.execute(select(func.count(ReportCase.id)).where(*clauses))).scalar_one() or 0)
    if status_filter != "open":
        order = (ReportCase.closed_at.desc(), ReportCase.id.desc())
    elif sort == "priority":
        order = (ReportCase.priority.desc(), ReportCase.id.asc())
    elif sort == "oldest":
        order = (ReportCase.created_at.asc(), ReportCase.id.asc())
    else:
        order = (ReportCase.created_at.desc(), ReportCase.id.desc())
    result = await db.execute(select(ReportCase).where(*clauses).order_by(*order).offset(offset).limit(limit))
    counts_rows = await db.execute(select(ReportCase.queue, func.count()).where(ReportCase.status == "open").group_by(ReportCase.queue))
    counts = {queue_name: int(count) for queue_name, count in counts_rows.all()}
    return list(result.scalars().all()), total, counts


async def archive_cases(
    db: AsyncSession,
    *,
    before: datetime,
    queue: str | None = None,
    dry_run: bool = False,
    actor_id: int | None,
) -> dict[str, Any]:
    """把创建时间早于 before 的未结案件一次性归档。返回 {"count", "by_queue", "dry_run"}。

    归档不是结论：不记处罚、不进题库、不结算委员的票、不通知举报人，举报人信誉也不受影响（见 reporter_credit）。
    dry_run 只数一下不写库。真正归档时提交事务，并通知后台 / 委员刷新。
    """
    if before.tzinfo is None:
        before = before.replace(tzinfo=UTC)
    clauses = [ReportCase.status == "open", ReportCase.created_at < before]
    if queue:
        clauses.append(ReportCase.queue == queue)
    rows = await db.execute(select(ReportCase.queue, func.count()).where(*clauses).group_by(ReportCase.queue))
    by_queue = {queue_name: int(count) for queue_name, count in rows.all()}
    total = sum(by_queue.values())
    if dry_run or total == 0:
        return {"count": total, "by_queue": by_queue, "dry_run": dry_run}
    now = datetime.now(UTC)
    cases = list((await db.execute(select(ReportCase).where(*clauses))).scalars().all())
    for case in cases:
        case.status = ARCHIVED
        case.verdict_source = ARCHIVE_SOURCE
        case.verdict_note = ARCHIVE_NOTE
        case.verdict_by_id = actor_id
        case.closed_at = now
        case.updated_at = now
    case_ids = [case.id for case in cases]
    for start in range(0, len(case_ids), LOOKUP_BATCH):
        await db.execute(
            update(UserReport)
            .where(UserReport.case_id.in_(case_ids[start : start + LOOKUP_BATCH]), UserReport.status == ReportStatus.PENDING)
            .values(status=ReportStatus.REJECTED, action=ARCHIVE_ACTION, review_note=ARCHIVE_NOTE, reviewer_id=actor_id, reviewed_at=now)
            .execution_options(synchronize_session=False)
        )
    db.add(
        AdminAuditLog(
            actor_id=actor_id,
            action="case.archive",
            target_type="report_case",
            target_id=None,
            details=json.dumps(
                {"before": before.isoformat(), "queue": queue, "count": len(case_ids), "by_queue": by_queue, "case_ids": case_ids},
                ensure_ascii=False,
            ),
        )
    )
    await db.commit()
    if by_queue.get("jury") or by_queue.get("panel"):
        await notify_jury_queue_changed(db)
    return {"count": len(case_ids), "by_queue": by_queue, "dry_run": False}


async def recently_cleared_case(db: AsyncSession, key: str, now: datetime | None = None) -> ReportCase | None:
    """这个目标近期是不是已经被认定过不违规（docs/jury-v2.md 3.7）。

    只认真正下过的结论：案件 dismissed、结论来自委员 / 合议 / 管理员、之后没有被改判过。
    """
    cutoff = (now or datetime.now(UTC)) - timedelta(days=CLEARED_BLOCK_DAYS)
    result = await db.execute(
        select(ReportCase)
        .where(
            ReportCase.target_key == key,
            ReportCase.status == "dismissed",
            ReportCase.verdict_source.in_(CLEARED_SOURCES),
            ReportCase.overturned_at.is_(None),
            ReportCase.closed_at >= cutoff,
        )
        .order_by(ReportCase.id.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def case_reports(db: AsyncSession, case_id: int) -> list[UserReport]:
    result = await db.execute(select(UserReport).where(UserReport.case_id == case_id).order_by(UserReport.id.asc()))
    return list(result.scalars().all())


async def build_case_reads(db: AsyncSession, cases: list[ReportCase]) -> list[dict[str, Any]]:
    user_ids = {case.reported_user_id for case in cases if case.reported_user_id is not None}
    users: dict[int, User] = {}
    if user_ids:
        users = {user.id: user for user in (await db.execute(select(User).where(User.id.in_(user_ids)))).scalars().all()}
    published = await published_case_ids(db, [case.id for case in cases if case.publishable])
    from app.services import jury_reviews

    reconsiders = await jury_reviews.reconsider_statuses(db, [case.id for case in cases])
    items: list[dict[str, Any]] = []
    for case in cases:
        snapshot = await case_snapshot(db, case)
        closed_at = penalty_service.aware(case.closed_at)
        items.append(
            {
                "id": case.id,
                "target_type": case.target_type,
                "target_id": case.target_id,
                "conversation_id": case.conversation_id,
                "message_id": case.message_id,
                "reported_user_id": case.reported_user_id,
                "reported_user": users.get(case.reported_user_id) if case.reported_user_id is not None else None,
                "status": case.status,
                "queue": case.queue,
                "jury_eligible": case.jury_eligible,
                "report_count": case.report_count,
                "primary_reason": case.primary_reason,
                "priority": case.priority,
                "summary": _case_summary(snapshot),
                "machine_decision": case.machine_decision,
                "machine_category": case.machine_category,
                "machine_level": case.machine_level,
                "machine_confidence": case.machine_confidence,
                "verdict_source": case.verdict_source,
                "verdict_category": case.verdict_category,
                "verdict_category_label": penalty_service.category_label(case.verdict_category) if case.verdict_category else None,
                "verdict_level": case.verdict_level,
                "verdict_note": case.verdict_note,
                "violation_id": case.violation_id,
                "first_report_id": case.first_report_id,
                "created_at": penalty_service.aware(case.created_at),
                "closed_at": closed_at,
                "jury_outcome": case.jury_outcome,
                "jury_block_reason": case.jury_block_reason,
                "queued_at": penalty_service.aware(case.queued_at),
                "escalated_at": penalty_service.aware(case.escalated_at),
                # 现在正在公示栏里的案件才有：定案时间 + 公示延迟
                "published_at": closed_at + timedelta(hours=PUBLISH_DELAY_HOURS) if case.id in published and closed_at else None,
                "overturned_at": penalty_service.aware(case.overturned_at),
                "overturn_note": case.overturn_note,
                # —— 三期 ——
                "case_origin": case.case_origin or "report",
                "panel_at": penalty_service.aware(case.panel_at),
                "panel_outcome": case.panel_outcome,
                "reconsider_status": reconsiders.get(case.id, "none"),
            }
        )
    return items


# ---------------------------------------------------------------- 存量迁移


async def backfill_cases(db: AsyncSession) -> dict[str, int]:
    """把案件上线前的举报并成案件。可重复执行：只处理还没有 case_id 的举报。

    待处理的举报里，目标内容已经被撤回 / 删除的直接结案；其余进管理员或风纪委员队列。
    历史上已审完的举报按当时的结论建成已结案件。
    """
    reports = (
        await db.execute(select(UserReport).where(UserReport.case_id.is_(None)).order_by(UserReport.id.asc()))
    ).scalars().all()
    stats = {"reports": len(reports), "cases_open": 0, "cases_closed": 0, "auto_closed": 0}
    groups: dict[tuple[str, bool], list[UserReport]] = {}
    for report in reports:
        key = target_key(report.target_type, report.target_id, report.conversation_id, report.message_id, report.reported_user_id)
        # 已审完的举报各自成案（它们是分别审的，结论可能不同）；待处理的按目标合并。
        bucket = (key, True) if report.status == ReportStatus.PENDING else (f"{key}#{report.id}", False)
        groups.setdefault(bucket, []).append(report)

    now = datetime.now(UTC)
    for (key, pending), items in groups.items():
        first = items[0]
        base_key = key.split("#", 1)[0]
        case = ReportCase(
            target_key=base_key,
            target_type=first.target_type.value,
            target_id=first.target_id,
            conversation_id=first.conversation_id,
            message_id=first.message_id,
            reported_user_id=first.reported_user_id,
            first_report_id=first.id,
            jury_eligible=await _jury_eligible(db, first),
            created_at=first.created_at,
            updated_at=now,
        )
        case.content_publishable = bool(case.jury_eligible and first.target_type == ReportTargetType.POST)
        db.add(case)
        await db.flush()
        for report in items:
            report.case_id = case.id
        await db.flush()
        await _recount(db, case)
        case.reporter_weight = float(len({report.reporter_id for report in items}))
        _refresh_priority(case)
        if not pending:
            upheld = first.status == ReportStatus.REVIEWED
            case.status = "resolved" if upheld else "dismissed"
            case.queue = "admin"
            case.verdict_source = "admin" if first.reviewer_id is not None else "auto"
            case.verdict_category = reason_category(first.reason) if upheld else None
            case.verdict_note = first.review_note
            case.verdict_by_id = first.reviewer_id
            case.closed_at = first.reviewed_at or now
            # 管理员明确处置过（或明确驳回）的历史案件，直接作为风纪委员的质控题库。
            decisive = (upheld and (first.action or "none") != "none") or (not upheld and first.reviewer_id is not None)
            # 这样回填进题库的题来源记 legacy，默认未核对：只用于练习，核对之后才用于考试和质控。
            case.is_golden = bool(case.jury_eligible and decisive)
            case.golden_source = "legacy" if case.is_golden else None
            stats["cases_closed"] += 1
            continue
        handled = False
        if case.target_type == "message" and case.message_id is not None:
            message = await db.get(Message, case.message_id)
            handled = message is None or message.recalled_at is not None
        elif case.target_type == "post":
            post = await db.get(Post, case.target_id)
            handled = post is None or post.deleted_at is not None
        if handled:
            await _close(
                db,
                case,
                upheld=True,
                source="auto",
                actor_id=None,
                category=reason_category(case.primary_reason),
                level=0,
                note="被举报的内容此前已被处理",
                violation_id=None,
                action="none",
            )
            stats["auto_closed"] += 1
            continue
        await route_case(db, case, now)
        stats["cases_open"] += 1
    await db.commit()
    return stats
