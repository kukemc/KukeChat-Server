"""举报案件。

同一个目标的举报合并成一个案件，只审一次，结论回写给案件里的每一条举报。
案件有三个去处：机器预审能直接下结论的当场结案；群聊消息和公开内容交给风纪委员；
其余（私聊、举报用户、举报会话，以及风纪委员没有共识的）留给管理员。
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.models.case import ReportCase
from app.models.conversation import Conversation, ConversationMember
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
JURY_TIMEOUT_HOURS = 72


def reason_category(reason: str | None) -> str:
    return REPORT_REASONS.get(reason or "", ("other", 0.8))[0]


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
    """按近 90 天的举报结果算信誉。被认定恶意的举报按三次无效计。"""
    now = datetime.now(UTC)
    since = now - timedelta(days=CREDIT_WINDOW_DAYS)
    rows = await db.execute(
        select(UserReport.status, UserReport.malicious, func.count())
        .where(UserReport.reporter_id == reporter_id, UserReport.created_at >= since)
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
        db.add(case)
        await db.flush()
    report.case_id = case.id
    await db.flush()
    await _recount(db, case)
    case.reporter_weight = round((case.reporter_weight or 0.0) + weight, 3) if not created else round(weight, 3)
    case.updated_at = now
    _refresh_priority(case)
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
        case.queue = "jury" if case.jury_eligible else "admin"
        case.updated_at = datetime.now(UTC)
        _refresh_priority(case)
        await db.commit()
        await _broadcast_admin(case, "queued")


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


async def case_snapshot(db: AsyncSession, case: ReportCase) -> dict[str, Any]:
    if case.first_report_id is None:
        return {}
    report = await db.get(UserReport, case.first_report_id)
    if report is None or not report.target_snapshot:
        return {}
    try:
        value = json.loads(report.target_snapshot)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


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
    """给案件下结论。source 取 jury / admin。提交事务并推送通知。"""
    case = await db.get(ReportCase, case_id)
    if case is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Case not found")
    if case.status != "open":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Case is already closed")
    broadcast: dict[str, Any] | None = None
    violation_id: int | None = None
    action = "none"
    if upheld:
        category = category or reason_category(case.primary_reason)
        if category not in penalty_service.CATEGORIES:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Unknown violation category")
        if level is None:
            level = penalty_service.default_level(category)
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
                source="jury" if source == "jury" else "report",
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
        action = "+".join(actions) if actions else "none"
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
    case.queue = queue
    case.updated_at = datetime.now(UTC)
    await db.commit()
    await _broadcast_admin(case, "queued")
    return case


async def escalate_stale_jury_cases(db: AsyncSession) -> int:
    """风纪委员人手不够或都要回避时，案件不能一直挂着：超时未决的交回管理员。"""
    cutoff = datetime.now(UTC) - timedelta(hours=JURY_TIMEOUT_HOURS)
    result = await db.execute(
        select(ReportCase).where(ReportCase.status == "open", ReportCase.queue == "jury", ReportCase.updated_at < cutoff)
    )
    cases = list(result.scalars().all())
    for case in cases:
        case.queue = "admin"
        case.updated_at = datetime.now(UTC)
    if cases:
        await db.commit()
    return len(cases)


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
            from app.services import messages as message_service

            message = await message_service.get_message(db, info["message_id"])
            member_ids = (
                await db.execute(select(ConversationMember.user_id).where(ConversationMember.conversation_id == info["conversation_id"]))
            ).scalars().all()
            for user_id in set(member_ids):
                await manager.send_to_user(user_id, "message.recalled", await message_service.build_message_read(db, message, user_id))
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
    if report.status == ReportStatus.REVIEWED:
        return "你的举报已处理", f"你举报的「{report.reason}」经核实属实，相关内容和账号已按社区规则处理。感谢你维护社区环境。"
    if report.malicious:
        return "你的举报未被采纳", f"你举报的「{report.reason}」经核实不属实，且被认定为无效举报。多次无效举报会降低每日举报次数。"
    return "你的举报未被采纳", f"你举报的「{report.reason}」经核实暂未发现违规。如有新的证据可以重新举报。"


async def notify_case_closed(db: AsyncSession, case: ReportCase) -> None:
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
    await _broadcast_admin(case, "closed")


# ---------------------------------------------------------------- 查询


async def list_cases(
    db: AsyncSession,
    *,
    status_filter: str | None = "open",
    queue: str | None = None,
    query: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[ReportCase], int, dict[str, int]]:
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
    order = (ReportCase.priority.desc(), ReportCase.id.asc()) if status_filter == "open" else (ReportCase.closed_at.desc(), ReportCase.id.desc())
    result = await db.execute(select(ReportCase).where(*clauses).order_by(*order).offset(offset).limit(limit))
    counts_rows = await db.execute(select(ReportCase.queue, func.count()).where(ReportCase.status == "open").group_by(ReportCase.queue))
    counts = {queue_name: int(count) for queue_name, count in counts_rows.all()}
    return list(result.scalars().all()), total, counts


async def case_reports(db: AsyncSession, case_id: int) -> list[UserReport]:
    result = await db.execute(select(UserReport).where(UserReport.case_id == case_id).order_by(UserReport.id.asc()))
    return list(result.scalars().all())


async def build_case_reads(db: AsyncSession, cases: list[ReportCase]) -> list[dict[str, Any]]:
    user_ids = {case.reported_user_id for case in cases if case.reported_user_id is not None}
    users: dict[int, User] = {}
    if user_ids:
        users = {user.id: user for user in (await db.execute(select(User).where(User.id.in_(user_ids)))).scalars().all()}
    items: list[dict[str, Any]] = []
    for case in cases:
        snapshot = await case_snapshot(db, case)
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
                "closed_at": penalty_service.aware(case.closed_at),
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
            case.is_golden = bool(case.jury_eligible and decisive)
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
        case.queue = "jury" if case.jury_eligible else "admin"
        stats["cases_open"] += 1
    await db.commit()
    return stats
