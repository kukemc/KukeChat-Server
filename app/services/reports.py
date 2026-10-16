from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging

UTC = timezone.utc
logger = logging.getLogger(__name__)

from fastapi import HTTPException, status
from sqlalchemy import and_, case, exists, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.core.security import hash_password
from app.models.conversation import Conversation, ConversationMember, GroupJoinRequest
from app.models.enums import ConversationType, GroupJoinRequestStatus, MemberRole, MessageType, ModerationAction, PasswordResetStatus, PlatformRole, ReportStatus, ReportTargetType, UserStatus
from app.models.message import Message, MessageVisibility
from app.models.post import Post
from app.models.report import AdminAuditLog, ContentModerationRecord, PasswordResetRequest, UserReport
from app.models.user import AuthSession, User
from app.schemas.report import AdminBanPage, AdminBanRead, AdminConversationRead, AdminConversationUpdate, AdminPasswordResetReview, AdminUserCreate, AdminUserPasswordReset, AdminUserRead, BanEvidence, ReportConversationContext, ReportCreate, ReportRead, ReportReview, ReportTargetContext
from app.schemas.user import UserPublic
from app.services.birthdays import admin_birthday
from app.services.messages import admin_recall_message, get_visible_message_for_user
from app.services.upload_paths import require_message_image_upload_url
from app.services import cases as case_service
from app.services import messages as message_service
from app.services import penalties as penalty_service
from app.services import posts as post_service
from app.models.case import JuryMember, ReportCase
from app.models.penalty import UserViolation


async def _can_view_conversation(db: AsyncSession, conversation_id: int, user_id: int) -> bool:
    result = await db.execute(
        select(ConversationMember.id).where(
            ConversationMember.conversation_id == conversation_id,
            ConversationMember.user_id == user_id,
        )
    )
    return result.scalar_one_or_none() is not None


async def _conversation_has_user(db: AsyncSession, conversation_id: int, user_id: int) -> bool:
    result = await db.execute(
        select(ConversationMember.id).where(
            ConversationMember.conversation_id == conversation_id,
            ConversationMember.user_id == user_id,
        )
    )
    return result.scalar_one_or_none() is not None


async def conversation_member_ids(db: AsyncSession, conversation_id: int) -> list[int]:
    result = await db.execute(select(ConversationMember.user_id).where(ConversationMember.conversation_id == conversation_id))
    return list(result.scalars().all())


async def _can_view_user(db: AsyncSession, target_id: int, reporter_id: int) -> bool:
    user = await db.get(User, target_id)
    if user is None:
        return False
    if target_id == reporter_id:
        return False
    result = await db.execute(
        select(ConversationMember.conversation_id)
        .where(ConversationMember.user_id.in_([target_id, reporter_id]))
        .group_by(ConversationMember.conversation_id)
        .having(func.count(func.distinct(ConversationMember.user_id)) == 2)
        .limit(1)
    )
    return result.scalar_one_or_none() is not None


def _system_message_not_reportable() -> HTTPException:
    # 系统提示的文字不是「发送者」写的：戳一戳的后缀来自被戳的人，拿它举报或当证据都会算到发起人头上，
    # 被戳的人甚至可以先改好后缀再去举报对方。
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail={"code": "not_reportable", "message": "系统提示不能被举报，也不能作为证据；请举报对应的用户并写明情况或附上截图"},
    )


async def create_report(db: AsyncSession, reporter_id: int, payload: ReportCreate) -> UserReport:
    case_service.validate_report_payload(
        payload.target_type, payload.reason.strip(), payload.details, payload.attachments, payload.evidence_message_ids
    )
    credit = await case_service.ensure_quota(db, reporter_id)
    target_snapshot: dict | None = None
    conversation_id: int | None = None
    message_id: int | None = None
    reported_user_id: int | None = None
    if payload.target_type == ReportTargetType.MESSAGE:
        message = await get_visible_message_for_user(db, payload.target_id, reporter_id)
        if message.recalled_at is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"code": "already_handled", "message": "这条消息已经被处理过了"},
            )
        if message.sender_id == reporter_id:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot report your own message")
        if message.type == MessageType.SYSTEM:
            raise _system_message_not_reportable()
        conversation = await db.get(Conversation, message.conversation_id)
        conversation_id = message.conversation_id
        message_id = message.id
        reported_user_id = message.sender_id
        target_snapshot = await _message_report_snapshot(db, message, conversation, reporter_id)
    elif payload.target_type == ReportTargetType.POST:
        post = await post_service.get_visible_post(db, payload.target_id, reporter_id)
        if post.author_id == reporter_id:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot report your own post")
        reported_user_id = post.author_id
        target_snapshot = _post_report_snapshot(post)
    elif payload.target_type == ReportTargetType.CONVERSATION:
        conversation = await db.get(Conversation, payload.target_id)
        if conversation is None or not await _can_view_conversation(db, payload.target_id, reporter_id):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")
        conversation_id = conversation.id
        target_snapshot = await _conversation_report_snapshot(db, conversation, reporter_id)
    else:
        if not await _can_view_user(db, payload.target_id, reporter_id):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
        reported_user = await db.get(User, payload.target_id)
        reported_user_id = reported_user.id if reported_user else payload.target_id
        if payload.conversation_id is not None:
            if not await _can_view_conversation(db, payload.conversation_id, reporter_id):
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")
            if not await _conversation_has_user(db, payload.conversation_id, reported_user_id):
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Reported user is not in the conversation")
            conversation_id = payload.conversation_id
        if reported_user_id == reporter_id:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot report yourself")
        target_snapshot = await _user_report_snapshot(db, reported_user, reporter_id, conversation_id)
        if payload.evidence_message_ids:
            target_snapshot["evidence_messages"] = await _evidence_message_snapshots(
                db, payload.evidence_message_ids, reporter_id, reported_user_id, conversation_id
            )

    attachments = _normalize_report_attachments(payload.attachments)
    snapshot_json = _canonical_json(target_snapshot) if target_snapshot else None
    attachments_json = _canonical_json(attachments) if attachments else None

    reason = payload.reason.strip()
    details = payload.details.strip() if payload.details else None
    open_case = await case_service.find_open_case(
        db, case_service.target_key(payload.target_type, payload.target_id, conversation_id, message_id, reported_user_id)
    )
    if open_case is not None and await case_service.already_reported(db, reporter_id, open_case.id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "already_reported",
                "message": "你已经举报过了，处理结果会通知你",
                "case_id": open_case.id,
                "detail": "A pending report already exists for this target",
            },
        )
    if open_case is None and payload.target_type in {ReportTargetType.MESSAGE, ReportTargetType.POST}:
        # 同一条消息 / 动态近期已经被认定过不违规：不再为它重开案件（原来可以无限重复举报，每次开一个新的）。
        cleared = await case_service.recently_cleared_case(
            db, case_service.target_key(payload.target_type, payload.target_id, conversation_id, message_id, reported_user_id)
        )
        if cleared is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "already_reviewed",
                    "message": f"这条内容近期已经评审过，结论是不违规，{case_service.CLEARED_BLOCK_DAYS} 天内不再重复受理",
                    "case_id": cleared.id,
                },
            )

    report = UserReport(
        reporter_id=reporter_id,
        target_type=payload.target_type,
        target_id=payload.target_id,
        conversation_id=conversation_id,
        message_id=message_id,
        reported_user_id=reported_user_id,
        reason=reason,
        details=details,
        attachments=attachments_json,
        target_snapshot=snapshot_json,
        evidence_hash=_evidence_hash(target_snapshot, attachments, reason=reason, details=details),
    )
    db.add(report)
    await db.flush()
    case, created = await case_service.attach_report(db, report, weight=float(credit["weight"]))
    await db.commit()
    if getattr(case, "_left_jury_queue", False):
        # 这条举报让案件变成了敏感类别，已经从委员队列撤出来。
        await case_service.notify_jury_queue_changed(db)
    result = await get_report(db, report.id)
    # 新案件要先过一遍机器预审，由 API 层在事务提交后调度。
    setattr(result, "_new_case_id", case.id if created else None)
    return result


async def _evidence_message_snapshots(
    db: AsyncSession,
    message_ids: list[int],
    reporter_id: int,
    reported_user_id: int,
    conversation_id: int | None,
) -> list[dict]:
    """举报用户时附带的证据消息。只收举报人看得到、且确实是被举报人发的消息，提交时固化。"""
    if conversation_id is None:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="选择证据消息时必须指明所在会话")
    items: list[dict] = []
    for message_id in list(dict.fromkeys(message_ids)):
        message = await get_visible_message_for_user(db, message_id, reporter_id)
        if message.conversation_id != conversation_id or message.sender_id != reported_user_id:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="证据消息必须是被举报人在该会话中发送的")
        if message.type == MessageType.SYSTEM:
            raise _system_message_not_reportable()
        await db.refresh(message, attribute_names=["sender"])
        items.append(_message_snapshot_item(message, is_target=True))
    return items


async def list_my_reports(db: AsyncSession, reporter_id: int, limit: int = 100) -> list[UserReport]:
    await _backfill_report_context(db)
    result = await db.execute(
        select(UserReport)
        .options(joinedload(UserReport.reporter), joinedload(UserReport.reviewer))
        .where(UserReport.reporter_id == reporter_id)
        .order_by(UserReport.created_at.desc(), UserReport.id.desc())
        .limit(limit)
    )
    return list(result.scalars().all())


async def get_report(db: AsyncSession, report_id: int) -> UserReport:
    result = await db.execute(
        select(UserReport)
        .options(joinedload(UserReport.reporter), joinedload(UserReport.reviewer))
        .where(UserReport.id == report_id)
    )
    report = result.scalar_one_or_none()
    if report is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Report not found")
    return report


async def list_reports(
    db: AsyncSession,
    report_status: ReportStatus | None = None,
    target_type: ReportTargetType | None = None,
    query: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[UserReport]:
    await _backfill_report_context(db)
    await _restore_auto_rejected_reports(db)
    stmt = (
        select(UserReport)
        .options(joinedload(UserReport.reporter), joinedload(UserReport.reviewer))
        .order_by(UserReport.created_at.desc(), UserReport.id.desc())
        .offset(offset)
        .limit(limit)
    )
    if report_status is not None:
        stmt = stmt.where(UserReport.status == report_status)
    if target_type is not None:
        stmt = stmt.where(UserReport.target_type == target_type)
    if query and query.strip():
        keyword = f"%{query.strip()}%"
        numeric_query = int(query.strip()) if query.strip().isdigit() else None
        query_conditions = [
            UserReport.reason.ilike(keyword),
            UserReport.details.ilike(keyword),
            UserReport.target_snapshot.ilike(keyword),
        ]
        if numeric_query is not None:
            query_conditions.extend(
                [
                    UserReport.id == numeric_query,
                    UserReport.target_id == numeric_query,
                    UserReport.conversation_id == numeric_query,
                    UserReport.message_id == numeric_query,
                    UserReport.reported_user_id == numeric_query,
                    UserReport.reporter_id == numeric_query,
                ]
            )
        stmt = stmt.where(or_(*query_conditions))
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def _restore_auto_rejected_reports(db: AsyncSession) -> None:
    result = await db.execute(
        update(UserReport)
        .where(
            UserReport.status == ReportStatus.REJECTED,
            UserReport.action == "none",
            UserReport.reviewer_id.is_(None),
            UserReport.review_note.in_(["自动复审未发现明确违规。", "自动复审未明确命中违规，已保留待人工审核。"]),
        )
        .values(status=ReportStatus.PENDING, review_note="自动复审未明确命中违规，已保留待人工审核。", reviewed_at=None)
    )
    if result.rowcount:
        await db.commit()


async def _backfill_report_context(db: AsyncSession) -> None:
    result = await db.execute(
        select(UserReport)
        .where(
            or_(
                UserReport.conversation_id.is_(None),
                UserReport.message_id.is_(None),
                UserReport.reported_user_id.is_(None),
            )
        )
        .limit(200)
    )
    changed = False
    for report in result.scalars().all():
        snapshot = _parse_target_snapshot(report)
        conversation = snapshot.get("conversation") if isinstance(snapshot.get("conversation"), dict) else None
        if report.conversation_id is None and isinstance(conversation, dict) and isinstance(conversation.get("id"), int):
            report.conversation_id = conversation["id"]
            changed = True
        if report.message_id is None:
            message_id = snapshot.get("target_message_id") or (snapshot.get("message", {}).get("id") if isinstance(snapshot.get("message"), dict) else None)
            if isinstance(message_id, int):
                report.message_id = message_id
                changed = True
        if report.reported_user_id is None:
            reported_user = snapshot.get("reported_user") if isinstance(snapshot.get("reported_user"), dict) else None
            if isinstance(reported_user, dict) and isinstance(reported_user.get("id"), int):
                report.reported_user_id = reported_user["id"]
                changed = True
            elif isinstance(snapshot.get("message"), dict) and isinstance(snapshot["message"].get("sender_id"), int):
                report.reported_user_id = snapshot["message"]["sender_id"]
                changed = True
        if report.target_type == ReportTargetType.MESSAGE and (report.conversation_id is None or report.reported_user_id is None or report.message_id is None):
            message = await db.get(Message, report.target_id)
            if message is not None:
                report.conversation_id = report.conversation_id or message.conversation_id
                report.message_id = report.message_id or message.id
                report.reported_user_id = report.reported_user_id or message.sender_id
                changed = True
        elif report.target_type == ReportTargetType.USER and report.reported_user_id is None:
            report.reported_user_id = report.target_id
            changed = True
        elif report.target_type == ReportTargetType.POST and report.reported_user_id is None:
            post = await db.get(Post, report.target_id)
            if post is not None:
                report.reported_user_id = post.author_id
                changed = True
        elif report.target_type == ReportTargetType.CONVERSATION and report.conversation_id is None:
            report.conversation_id = report.target_id
            changed = True
        if report.evidence_hash is None and snapshot:
            report.evidence_hash = _evidence_hash(snapshot, _parse_report_attachments(report), reason=report.reason, details=report.details)
            changed = True
    if changed:
        await db.commit()


def _parse_report_attachments(report: UserReport) -> list[str]:
    if not report.attachments:
        return []
    try:
        value = json.loads(report.attachments)
    except json.JSONDecodeError:
        return []
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item.strip()]


def _normalize_report_attachments(values: list[str]) -> list[str]:
    clean_values: list[str] = []
    for value in values:
        if not isinstance(value, str):
            continue
        clean_value = value.strip()
        if not clean_value:
            continue
        require_message_image_upload_url(clean_value, detail="Report attachments must be uploaded message images")
        if clean_value not in clean_values:
            clean_values.append(clean_value)
        if len(clean_values) >= 6:
            break
    return clean_values


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _evidence_hash(snapshot: dict | None, attachments: list[str], *, reason: str | None = None, details: str | None = None) -> str:
    payload = {
        "snapshot": snapshot or {},
        "attachments": attachments,
        "reason": reason or "",
        "details": details or "",
    }
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _parse_target_snapshot(report: UserReport) -> dict:
    if not report.target_snapshot:
        return {}
    try:
        snapshot = json.loads(report.target_snapshot)
    except json.JSONDecodeError:
        return {}
    return snapshot if isinstance(snapshot, dict) else {}


async def _message_report_snapshot(db: AsyncSession, message: Message, conversation: Conversation | None, reporter_id: int | None) -> dict:
    """被举报的消息和它前后各 10 条上下文。上下文按举报人能看到的范围取；
    reporter_id 为 None 是机器来源的案件（没有举报人），取会话里实际的前后文。"""
    member = None
    if reporter_id is not None:
        membership = await db.execute(
            select(ConversationMember).where(
                ConversationMember.conversation_id == message.conversation_id,
                ConversationMember.user_id == reporter_id,
            )
        )
        member = membership.scalar_one_or_none()
    min_message_id = member.cleared_before_message_id if member and member.cleared_before_message_id is not None else None
    before_conditions = [Message.conversation_id == message.conversation_id, Message.id < message.id]
    after_conditions = [Message.conversation_id == message.conversation_id, Message.id > message.id]
    if reporter_id is not None:
        hidden = select(MessageVisibility.message_id).where(MessageVisibility.user_id == reporter_id)
        before_conditions.append(Message.id.not_in(hidden))
        after_conditions.append(Message.id.not_in(hidden))
    if min_message_id is not None:
        before_conditions.append(Message.id > min_message_id)
        after_conditions.append(Message.id > min_message_id)
    before_result = await db.execute(
        select(Message)
        .options(joinedload(Message.sender))
        .where(*before_conditions)
        .order_by(Message.id.desc())
        .limit(10)
    )
    after_result = await db.execute(
        select(Message)
        .options(joinedload(Message.sender))
        .where(*after_conditions)
        .order_by(Message.id.asc())
        .limit(10)
    )
    before_messages = list(reversed(before_result.scalars().all()))
    after_messages = list(after_result.scalars().all())
    messages = before_messages + [message] + after_messages
    reported_membership = await _conversation_member_snapshot(db, message.conversation_id, message.sender_id)
    return {
        "target_type": "message",
        "target_message_id": message.id,
        "reported_user": _user_snapshot(message.sender),
        "reported_member": reported_membership,
        "reporter_member": _member_snapshot(member),
        "message": _message_snapshot_item(message, is_target=True),
        "messages": [_message_snapshot_item(item, is_target=item.id == message.id) for item in messages],
        "conversation": await _conversation_snapshot(db, conversation),
    }


async def _user_report_snapshot(db: AsyncSession, user: User | None, reporter_id: int, conversation_id: int | None = None) -> dict:
    conversation = await db.get(Conversation, conversation_id) if conversation_id is not None else None
    return {
        "target_type": "user",
        "reported_user": _user_snapshot(user),
        "conversation": await _conversation_snapshot(db, conversation),
        "reported_member": await _conversation_member_snapshot(db, conversation_id, user.id if user else None),
        "reporter_member": await _conversation_member_snapshot(db, conversation_id, reporter_id),
        "shared_conversations": await _shared_conversation_snapshots(db, user.id if user else None, reporter_id),
    }


async def _conversation_report_snapshot(db: AsyncSession, conversation: Conversation, reporter_id: int) -> dict:
    members_result = await db.execute(
        select(ConversationMember)
        .options(joinedload(ConversationMember.user))
        .where(ConversationMember.conversation_id == conversation.id)
        .order_by(ConversationMember.role.asc(), ConversationMember.joined_at.asc())
        .limit(30)
    )
    members = list(members_result.scalars().all())
    return {
        "target_type": "conversation",
        "conversation": await _conversation_snapshot(db, conversation),
        "reporter_membership": _member_snapshot(next((member for member in members if member.user_id == reporter_id), None)),
        "members": [_member_snapshot(member) for member in members],
    }


def _post_report_snapshot(post: Post) -> dict:
    return {
        "target_type": "post",
        "reported_user": _user_snapshot(post.author),
        "post": {
            "id": post.id,
            "author_id": post.author_id,
            "author": _user_snapshot(post.author),
            "content": post.content,
            "image_urls": _json_list(post.image_urls_json),
            "visibility": post.visibility.value if hasattr(post.visibility, "value") else str(post.visibility),
            "moderation_status": str(post.moderation_status),
            "repost_of_id": post.repost_of_id,
            "created_at": post.created_at.isoformat(),
            "updated_at": post.updated_at.isoformat(),
        },
    }


async def _shared_conversation_snapshots(db: AsyncSession, user_id: int | None, reporter_id: int) -> list[dict]:
    if user_id is None:
        return []
    result = await db.execute(
        select(Conversation)
        .join(ConversationMember, ConversationMember.conversation_id == Conversation.id)
        .where(ConversationMember.user_id.in_([user_id, reporter_id]))
        .group_by(Conversation.id)
        .having(func.count(func.distinct(ConversationMember.user_id)) == 2)
        .order_by(Conversation.updated_at.desc())
        .limit(10)
    )
    return [item for item in [await _conversation_snapshot(db, conversation) for conversation in result.scalars().all()] if item is not None]


def _message_snapshot_item(message: Message, *, is_target: bool) -> dict:
    sender = message.sender
    sender_name = (sender.nickname or sender.username) if sender else None
    return {
        "id": message.id,
        "conversation_id": message.conversation_id,
        "sender_id": message.sender_id,
        "sender_name": sender_name,
        "sender_username": sender.username if sender else None,
        "sender_avatar_url": sender.avatar_url if sender else None,
        "sender": {
            "id": sender.id,
            "username": sender.username,
            "nickname": sender.nickname,
            "avatar_url": sender.avatar_url,
        } if sender else None,
        "sender_display_name": sender_name,
        "type": message.type.value if hasattr(message.type, "value") else str(message.type),
        "content": message.content,
        "metadata": _json_object(message.metadata_json),
        "created_at": message.created_at.isoformat(),
        "recalled_at": message.recalled_at.isoformat() if message.recalled_at else None,
        "recalled_by_id": message.recalled_by_id,
        "is_target": is_target,
    }


def _user_snapshot(user: User | None) -> dict | None:
    if user is None:
        return None
    return {
        "id": user.id,
        "username": user.username,
        "nickname": user.nickname,
        "avatar_url": user.avatar_url,
        "bio": user.bio,
        "platform_role": user.platform_role.value if hasattr(user.platform_role, "value") else str(user.platform_role),
        "status": user.status.value if hasattr(user.status, "value") else str(user.status),
        "banned_until": user.banned_until.isoformat() if user.banned_until else None,
        "ban_reason": user.ban_reason,
        "created_at": user.created_at.isoformat(),
    }


def _member_snapshot(member: ConversationMember | None) -> dict | None:
    if member is None:
        return None
    return {
        "user_id": member.user_id,
        "role": member.role.value if hasattr(member.role, "value") else str(member.role),
        "nickname": member.nickname,
        "remark": member.remark,
        "title": member.title,
        "joined_at": member.joined_at.isoformat() if member.joined_at else None,
        "user": _user_snapshot(member.user),
    }


async def _conversation_member_snapshot(db: AsyncSession, conversation_id: int | None, user_id: int | None) -> dict | None:
    if conversation_id is None or user_id is None:
        return None
    result = await db.execute(
        select(ConversationMember)
        .options(joinedload(ConversationMember.user))
        .where(ConversationMember.conversation_id == conversation_id, ConversationMember.user_id == user_id)
        .limit(1)
    )
    return _member_snapshot(result.scalar_one_or_none())


async def _conversation_snapshot(db: AsyncSession, conversation: Conversation | None) -> dict | None:
    if conversation is None:
        return None
    owner_result = await db.execute(
        select(ConversationMember.user_id)
        .where(ConversationMember.conversation_id == conversation.id, ConversationMember.role == MemberRole.OWNER)
        .limit(1)
    )
    return {
        "id": conversation.id,
        "type": conversation.type.value if hasattr(conversation.type, "value") else str(conversation.type),
        "title": conversation.title,
        "display_title": conversation.title,
        "avatar_url": conversation.avatar_url,
        "description": conversation.description,
        "category": conversation.category,
        "member_count": await conversation_member_count(db, conversation.id),
        "owner_id": owner_result.scalar_one_or_none(),
        "created_by_id": conversation.created_by_id,
        "created_at": conversation.created_at.isoformat(),
        "updated_at": conversation.updated_at.isoformat(),
    }


def _json_object(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _json_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


async def build_report_read(db: AsyncSession, report: UserReport, viewer_id: int | None = None, *, include_snapshot: bool = False) -> ReportRead:
    target: ReportTargetContext | None = None
    target_snapshot = _parse_target_snapshot(report)
    reported_user: User | UserPublic | None = None
    if report.reported_user_id is not None:
        reported_user = await db.get(User, report.reported_user_id)
    if reported_user is None and include_snapshot:
        reported_user = _user_from_snapshot(target_snapshot.get("reported_user"))

    if report.target_type == ReportTargetType.MESSAGE:
        lookup_message_id = report.message_id or report.target_id
        result = await db.execute(select(Message).options(joinedload(Message.sender)).where(Message.id == lookup_message_id))
        message = result.scalar_one_or_none()
        conversation_snapshot = target_snapshot.get("conversation") if isinstance(target_snapshot.get("conversation"), dict) else None
        if message is not None:
            message_read = await message_service.build_message_read(db, message, viewer_id or report.reporter_id)
            conversation = await db.get(Conversation, message.conversation_id)
            target = ReportTargetContext(
                message=message_read,
                user=message.sender or reported_user,
                conversation=_conversation_context(conversation) or _conversation_context_from_snapshot(conversation_snapshot),
                extra={"snapshot": target_snapshot} if include_snapshot and target_snapshot else {},
            )
        elif include_snapshot and target_snapshot:
            target = ReportTargetContext(
                user=reported_user,
                conversation=_conversation_context_from_snapshot(conversation_snapshot),
                extra={"snapshot": target_snapshot},
            )
    elif report.target_type == ReportTargetType.POST:
        result = await db.execute(select(Post).options(joinedload(Post.author)).where(Post.id == report.target_id, Post.deleted_at.is_(None)))
        post = result.scalar_one_or_none()
        post_read = None
        if post is not None:
            post_reads = await post_service.build_admin_post_reads(db, [post])
            post_read = post_reads[0] if post_reads else None
        target = ReportTargetContext(
            post=post_read,
            user=(post.author if post else reported_user),
            extra={"snapshot": target_snapshot} if include_snapshot and target_snapshot else {},
        )
    elif report.target_type == ReportTargetType.USER:
        user = await db.get(User, report.reported_user_id or report.target_id)
        conversation = await db.get(Conversation, report.conversation_id) if report.conversation_id is not None else None
        conversation_snapshot = target_snapshot.get("conversation") if isinstance(target_snapshot.get("conversation"), dict) else None
        target = ReportTargetContext(
            user=user or reported_user,
            conversation=_conversation_context(conversation) or _conversation_context_from_snapshot(conversation_snapshot),
            extra={"snapshot": target_snapshot} if include_snapshot and target_snapshot else {},
        )
    elif report.target_type == ReportTargetType.CONVERSATION:
        conversation = await db.get(Conversation, report.conversation_id or report.target_id)
        conversation_snapshot = target_snapshot.get("conversation") if isinstance(target_snapshot.get("conversation"), dict) else None
        target = ReportTargetContext(
            conversation=_conversation_context(conversation) or _conversation_context_from_snapshot(conversation_snapshot),
            extra={"snapshot": target_snapshot} if include_snapshot and target_snapshot else {},
        )

    result_title = result_text = None
    if report.status != ReportStatus.PENDING:
        result_title, result_text = case_service.report_result_text(report)
    from app.services import jury_reviews

    reconsider = await jury_reviews.report_fields(db, report, viewer_id)
    return ReportRead(
        id=report.id,
        reporter_id=report.reporter_id,
        target_type=report.target_type,
        target_id=report.target_id,
        reason=report.reason,
        details=report.details,
        description=report.details,
        attachments=_parse_report_attachments(report),
        conversation_id=report.conversation_id,
        message_id=report.message_id,
        reported_user_id=report.reported_user_id,
        evidence_hash=report.evidence_hash,
        status=report.status,
        reviewer_id=report.reviewer_id,
        review_note=report.review_note,
        action=report.action,
        created_at=report.created_at,
        reviewed_at=report.reviewed_at,
        case_id=report.case_id,
        result_title=result_title,
        result_text=result_text,
        reporter=report.reporter,
        reviewer=report.reviewer,
        reported_user=reported_user,
        target=target,
        snapshot=target_snapshot if include_snapshot and target_snapshot else None,
        can_reconsider=reconsider["can_reconsider"],
        reconsider_status=reconsider["reconsider_status"],
    )


async def build_report_reads(db: AsyncSession, reports: list[UserReport], viewer_id: int | None = None, *, include_snapshot: bool = False) -> list[ReportRead]:
    return [await build_report_read(db, report, viewer_id, include_snapshot=include_snapshot) for report in reports]


def _conversation_context(conversation: Conversation | None) -> ReportConversationContext | None:
    if conversation is None:
        return None
    return ReportConversationContext(
        id=conversation.id,
        type=conversation.type.value,
        title=conversation.title,
        display_title=conversation.title,
        avatar_url=conversation.avatar_url,
        description=conversation.description,
        category=conversation.category,
        owner_id=None,
        created_by_id=conversation.created_by_id,
    )


def _conversation_context_from_snapshot(snapshot: dict | None) -> ReportConversationContext | None:
    if not isinstance(snapshot, dict):
        return None
    conversation_id = snapshot.get("id")
    if not isinstance(conversation_id, int):
        return None
    return ReportConversationContext(
        id=conversation_id,
        type=str(snapshot.get("type") or "conversation"),
        title=_optional_text(snapshot.get("title")),
        display_title=_optional_text(snapshot.get("display_title")) or _optional_text(snapshot.get("title")),
        avatar_url=_optional_text(snapshot.get("avatar_url")),
        description=_optional_text(snapshot.get("description")),
        category=_optional_text(snapshot.get("category")),
        member_count=snapshot.get("member_count") if isinstance(snapshot.get("member_count"), int) else None,
        owner_id=snapshot.get("owner_id") if isinstance(snapshot.get("owner_id"), int) else None,
        created_by_id=snapshot.get("created_by_id") if isinstance(snapshot.get("created_by_id"), int) else None,
    )


def _user_from_snapshot(snapshot: object) -> UserPublic | None:
    if not isinstance(snapshot, dict):
        return None
    user_id = snapshot.get("id")
    username = snapshot.get("username")
    created_at_raw = snapshot.get("created_at")
    if not isinstance(user_id, int) or not isinstance(username, str) or not isinstance(created_at_raw, str):
        return None
    try:
        created_at = datetime.fromisoformat(created_at_raw)
    except ValueError:
        return None
    return UserPublic(
        id=user_id,
        username=username,
        nickname=_optional_text(snapshot.get("nickname")),
        avatar_url=_optional_text(snapshot.get("avatar_url")),
        bio=_optional_text(snapshot.get("bio")),
        platform_role=snapshot.get("platform_role") or PlatformRole.USER,
        status=snapshot.get("status") or UserStatus.ACTIVE,
        banned_until=_datetime_from_snapshot(snapshot.get("banned_until")),
        ban_reason=_optional_text(snapshot.get("ban_reason")),
        created_at=created_at,
    )


def _datetime_from_snapshot(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _optional_text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


async def message_is_under_report(db: AsyncSession, message_id: int) -> bool:
    """某条消息是否是某个举报的对象。

    管理员读取私聊内容的唯一合法入口是「有人举报了它」。这个判定同时覆盖
    直接以消息为目标的举报（target_type=MESSAGE）和以会话/用户为目标、但
    在举报时记录了具体消息的情形（message_id 字段）。
    """
    stmt = select(UserReport.id).where(
        or_(
            UserReport.message_id == message_id,
            and_(UserReport.target_type == ReportTargetType.MESSAGE, UserReport.target_id == message_id),
        )
    ).limit(1)
    result = await db.execute(stmt)
    return result.scalar_one_or_none() is not None


async def ensure_message_is_under_report(db: AsyncSession, message_id: int) -> None:
    """没有对应举报时拒绝访问。

    这是隐私边界的执行点：后台无法主动翻阅任意私聊，只能查看被举报的内容。
    """
    if not await message_is_under_report(db, message_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="仅可查看被举报的消息。该消息没有对应的举报记录。",
        )


async def review_report(db: AsyncSession, report_id: int, reviewer_id: int, payload: ReportReview) -> UserReport:
    result = await db.execute(select(UserReport).where(UserReport.id == report_id).with_for_update())
    report = result.scalar_one_or_none()
    if report is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Report not found")
    if report.status != ReportStatus.PENDING:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Report has already been reviewed")
    claim = await db.execute(
        update(UserReport)
        .where(UserReport.id == report_id, UserReport.status == ReportStatus.PENDING)
        .values(status=payload.status)
    )
    if claim.rowcount != 1:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Report is being reviewed")
    report.status = payload.status
    now = datetime.now(UTC)
    delete_content = payload.delete_content or payload.action in {"recall_message", "delete_post"}
    disable_user = payload.disable_user or payload.action == "disable_user"
    violation_category = payload.violation_category
    if violation_category is not None and violation_category not in penalty_service.CATEGORIES:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Unknown violation category")
    punish = disable_user or violation_category is not None
    actions: list[str] = []
    banned_until: datetime | None = None
    target_user: User | None = None
    deleted_post: Post | None = None

    if payload.action == "recall_message" and report.target_type != ReportTargetType.MESSAGE:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Recall action requires a message report")
    if payload.action == "delete_post" and report.target_type != ReportTargetType.POST:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Delete action requires a post report")

    if payload.status == ReportStatus.REVIEWED and punish:
        if report.target_type == ReportTargetType.USER:
            target_user = await db.get(User, report.reported_user_id or report.target_id)
        elif report.target_type == ReportTargetType.MESSAGE:
            result = await db.execute(select(Message).options(joinedload(Message.sender)).where(Message.id == (report.message_id or report.target_id)))
            message = result.scalar_one_or_none()
            target_user = message.sender if message else (await db.get(User, report.reported_user_id) if report.reported_user_id is not None else None)
        elif report.target_type == ReportTargetType.POST:
            target_user = await db.get(User, report.reported_user_id) if report.reported_user_id is not None else None
        if target_user is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
        if target_user.id == reviewer_id:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot disable yourself")
        if disable_user and target_user.platform_role == PlatformRole.ADMIN:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cannot disable an admin")
        report.reported_user_id = target_user.id

    if payload.status == ReportStatus.REVIEWED and delete_content:
        if report.target_type == ReportTargetType.MESSAGE:
            await admin_recall_message(db, reviewer_id, report.message_id or report.target_id, commit=False)
            actions.append("recall_message")
        elif report.target_type == ReportTargetType.POST:
            post = await db.get(Post, report.target_id)
            if post is not None and post.deleted_at is None:
                deleted_post = await post_service.admin_delete_post(db, report.target_id, reviewer_id, commit=False)
                actions.append("delete_post")
        else:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="This report target has no deletable content")

    if payload.status == ReportStatus.REVIEWED and punish:
        assert target_user is not None
        category = violation_category or REPORT_REASON_CATEGORIES.get(report.reason, "other")
        override = None
        if disable_user:
            override = penalty_service.Penalty("ban", _ban_hours(payload.ban_until, payload.ban_duration_hours, now))
        target_type, target_id = _violation_target(report)
        outcome = await penalty_service.record_violation(
            db,
            user_id=target_user.id,
            category=category,
            level=payload.violation_level,
            source="report",
            target_type=target_type,
            target_id=target_id,
            conversation_id=report.conversation_id,
            report_id=report.id,
            actor_id=reviewer_id,
            summary=_report_violation_summary(report),
            reason=payload.review_note or report.reason,
            override=override,
        )
        if outcome is not None:
            if outcome.banned:
                banned_until = penalty_service.aware(target_user.banned_until)
                # 管理员手动封禁时，用户看到的理由沿用审核备注 / 举报理由的原话。
                if disable_user:
                    target_user.ban_reason = payload.review_note or report.reason
                actions.append("disable_user")
            elif outcome.penalty.kind != "none":
                actions.append(f"penalty_{outcome.penalty.kind}")

    report.reviewer_id = reviewer_id
    report.review_note = payload.review_note.strip() if payload.review_note else None
    action = "+".join(actions) if actions else "none"
    report.action = action
    report.reviewed_at = now
    closed_case = await case_service.sync_from_report_review(db, report, actor_id=reviewer_id)
    db.add(
        AdminAuditLog(
            actor_id=reviewer_id,
            action=f"report.{payload.status.value}",
            target_type="report",
            target_id=report.id,
            details=json.dumps({"action": action, "note": report.review_note, "ban_until": banned_until.isoformat() if banned_until else None}, ensure_ascii=False),
        )
    )
    await db.commit()
    await penalty_service.flush_notifications(db)
    if closed_case is not None:
        await case_service.notify_case_closed(db, closed_case)
    if deleted_post is not None:
        try:
            await post_service.cleanup_deleted_post_uploads(db, deleted_post)
        except Exception:
            logger.exception("Post upload cleanup failed after report review", extra={"post_id": deleted_post.id, "report_id": report.id})
    return await get_report(db, report.id)


# 举报理由 -> 违规类别。举报人选的理由只是线索，管理员可以在处置时改判类别。
REPORT_REASON_CATEGORIES = {
    "辱骂骚扰": "harassment",
    "垃圾广告": "advertising",
    "色情低俗": "vulgar",
    "违法违规": "illegal",
    "诈骗或钓鱼": "fraud",
    "其他": "other",
}


def _ban_hours(ban_until: datetime | None, ban_duration_hours: int | None, now: datetime) -> int | None:
    """管理员给的封禁期限换算成小时；都没给即永久。"""
    if ban_until is not None:
        target = ban_until if ban_until.tzinfo is not None else ban_until.replace(tzinfo=UTC)
        return max(1, int((target - now).total_seconds() // 3600) + 1)
    return ban_duration_hours


def _violation_target(report: UserReport) -> tuple[str | None, int | None]:
    if report.target_type == ReportTargetType.MESSAGE:
        return "message", report.message_id or report.target_id
    if report.target_type == ReportTargetType.POST:
        return "post", report.target_id
    return None, None


def _report_violation_summary(report: UserReport) -> str | None:
    snapshot = _parse_target_snapshot(report)
    for key in ("message", "post"):
        item = snapshot.get(key)
        if isinstance(item, dict) and isinstance(item.get("content"), str) and item["content"].strip():
            return item["content"].strip()[:120]
    return None


async def list_users(db: AsyncSession, limit: int = 100, offset: int = 0, query: str | None = None) -> list[User]:
    stmt = select(User).order_by(User.created_at.desc(), User.id.desc()).offset(offset).limit(limit)
    if query and query.strip():
        keyword = f"%{query.strip()}%"
        stmt = stmt.where(or_(User.username.ilike(keyword), User.nickname.ilike(keyword), User.email.ilike(keyword)))
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def list_admin_users(db: AsyncSession, limit: int = 100, offset: int = 0, query: str | None = None) -> list[AdminUserRead]:
    return await _list_admin_users(db, limit, offset, query)


async def get_admin_user(db: AsyncSession, user_id: int) -> AdminUserRead:
    users = await _list_admin_users(db, 1, 0, None, User.id == user_id)
    if not users:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    return users[0]


def _aware_utc(value: datetime) -> datetime:
    """SQLite 存回来的时间没有时区信息，直接和 aware 的 now 相减会抛异常。"""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


# 封禁列表支持的排序方式。写死成白名单而不是直接拼字段名，避免把排序参数
# 变成一个能碰到任意列的注入面。
BAN_SORT_COLUMNS = {
    "banned_at": User.banned_at,
    "expires": User.banned_until,
    "username": User.username,
    "id": User.id,
    "created_at": User.created_at,
}


def _ban_search_clause(query: str | None):
    """封禁列表的搜索条件。

    原来只匹配用户名 / 昵称 / 邮箱，想按 ID 找人只能靠肉眼翻页。现在纯数字
    （以及 "#123" 这种写法）按 ID 精确匹配，其余按文本模糊匹配；数字串同时
    也当文本匹配一次，这样用户名里含数字的账号照样能搜到。
    """
    keyword = (query or "").strip()
    if not keyword:
        return None
    clauses = [
        User.username.ilike(f"%{keyword}%"),
        User.nickname.ilike(f"%{keyword}%"),
        User.email.ilike(f"%{keyword}%"),
    ]
    digits = keyword[1:] if keyword.startswith("#") else keyword
    if digits.isdigit():
        try:
            clauses.append(User.id == int(digits))
        except ValueError:
            pass
    return or_(*clauses)


def _ban_scope_clauses(scope: str, now: datetime) -> list:
    """按封禁类型筛选。"""
    if scope == "permanent":
        return [User.banned_until.is_(None)]
    if scope == "temporary":
        return [User.banned_until.is_not(None)]
    if scope == "expiring":
        # 24 小时内到期，方便盯住马上要放出来的账号
        return [User.banned_until.is_not(None), User.banned_until <= now + timedelta(hours=24)]
    return []


async def list_admin_bans(
    db: AsyncSession,
    limit: int = 50,
    offset: int = 0,
    query: str | None = None,
    sort: str = "banned_at",
    order: str = "desc",
    scope: str = "all",
) -> AdminBanPage:
    """封禁名单。

    相比原来的实现，这里多做三件事：可排序、可筛选、把封禁的来龙去脉一并带上
    （谁封的、因为哪条消息、命中了什么类别），省得为了看一次原因还要去翻审核
    记录。
    """
    await _release_expired_bans(db)
    now = datetime.now(UTC)

    conditions = [User.status == UserStatus.DISABLED, *_ban_scope_clauses(scope, now)]
    search = _ban_search_clause(query)
    if search is not None:
        conditions.append(search)

    total = int((await db.execute(select(func.count(User.id)).where(*conditions))).scalar_one() or 0)

    column = BAN_SORT_COLUMNS.get(sort, User.banned_at)
    direction = column.asc() if order == "asc" else column.desc()
    # banned_at 是后加的字段，老数据为空；用 id 兜底保证顺序稳定、翻页不跳行
    stmt = (
        select(User)
        .where(*conditions)
        .order_by(direction, User.id.desc())
        .offset(offset)
        .limit(limit)
    )
    users = list((await db.execute(stmt)).scalars().all())
    if not users:
        return AdminBanPage(items=[], total=total)

    user_ids = [user.id for user in users]
    evidence_map = await _latest_ban_evidence(db, user_ids)
    audit_map = await _latest_ban_audit(db, user_ids)
    ban_counts = await _count_by_user(
        db,
        select(ContentModerationRecord.user_id, func.count())
        .where(
            ContentModerationRecord.user_id.in_(user_ids),
            ContentModerationRecord.action_taken.in_([ModerationAction.USER_BANNED, ModerationAction.PERMANENT_BAN]),
        )
        .group_by(ContentModerationRecord.user_id),
    )
    message_counts = await _count_by_user(
        db,
        select(Message.sender_id, func.count()).where(Message.sender_id.in_(user_ids)).group_by(Message.sender_id),
    )

    items: list[AdminBanRead] = []
    for user in users:
        evidence = evidence_map.get(user.id)
        audit = audit_map.get(user.id)
        source, operator_id, operator_name, report_id = _resolve_ban_source(evidence, audit)
        banned_until = _aware_utc(user.banned_until) if user.banned_until else None
        items.append(
            AdminBanRead(
                id=user.id,
                username=user.username,
                nickname=user.nickname,
                avatar_url=user.avatar_url,
                email=user.email,
                created_at=user.created_at,
                status=user.status,
                banned_at=user.banned_at,
                banned_until=user.banned_until,
                ban_reason=user.ban_reason,
                permanent=user.banned_until is None,
                expires_in_seconds=None if banned_until is None else max(0, int((banned_until - now).total_seconds())),
                ban_source=source,
                operator_id=operator_id,
                operator_name=operator_name,
                report_id=report_id,
                evidence=evidence,
                ban_count=ban_counts.get(user.id, 0),
                message_count=message_counts.get(user.id, 0),
            )
        )
    return AdminBanPage(items=items, total=total)


def _resolve_ban_source(evidence: BanEvidence | None, audit: AdminAuditLog | None):
    """判断这次封禁是自动审核触发的还是人工操作的。

    两边都可能留下记录，取时间更晚的那条为准；实在判断不出就老实说 unknown，
    别硬编一个来源误导人。
    """
    audit_at = _aware_utc(audit.created_at) if audit is not None else None
    evidence_at = _aware_utc(evidence.created_at) if evidence is not None else None

    if evidence_at is not None and (audit_at is None or evidence_at >= audit_at):
        return "auto", None, None, None
    if audit is None:
        return "unknown", None, None, None

    report_id = None
    if audit.details:
        try:
            payload = json.loads(audit.details)
            if isinstance(payload, dict):
                raw = payload.get("report_id")
                report_id = int(raw) if isinstance(raw, (int, str)) and str(raw).isdigit() else None
        except (TypeError, ValueError, json.JSONDecodeError):
            report_id = None

    source = "report" if report_id is not None or audit.action.startswith("report.") else "manual"
    operator = audit.actor
    operator_name = None
    if operator is not None:
        operator_name = operator.nickname or operator.username
    return source, audit.actor_id, operator_name, report_id


async def _latest_ban_evidence(db: AsyncSession, user_ids: list[int]) -> dict[int, BanEvidence]:
    """每个用户最近一条「导致封号」的审核记录。

    审核日志的保留策略会永久保留产生过处置动作的记录，所以这里的证据不会因为
    清理而丢失（见 app/services/moderation.py 的 prune_moderation_logs）。
    """
    if not user_ids:
        return {}
    rows = list(
        (
            await db.execute(
                select(ContentModerationRecord)
                .where(
                    ContentModerationRecord.user_id.in_(user_ids),
                    ContentModerationRecord.action_taken.in_(
                        [ModerationAction.USER_BANNED, ModerationAction.PERMANENT_BAN]
                    ),
                )
                .order_by(ContentModerationRecord.created_at.desc(), ContentModerationRecord.id.desc())
            )
        ).scalars().all()
    )
    latest: dict[int, ContentModerationRecord] = {}
    for row in rows:
        if row.user_id is not None and row.user_id not in latest:
            latest[row.user_id] = row

    conversation_ids = {r.conversation_id for r in latest.values() if r.conversation_id}
    titles: dict[int, str | None] = {}
    if conversation_ids:
        title_rows = await db.execute(
            select(Conversation.id, Conversation.title).where(Conversation.id.in_(conversation_ids))
        )
        titles = {cid: title for cid, title in title_rows.all()}

    result: dict[int, BanEvidence] = {}
    for user_id, row in latest.items():
        categories: list[str] = []
        if row.categories:
            try:
                parsed = json.loads(row.categories)
                if isinstance(parsed, list):
                    categories = [str(item) for item in parsed]
                elif isinstance(parsed, dict):
                    categories = [str(key) for key in parsed]
            except (TypeError, ValueError, json.JSONDecodeError):
                categories = [row.categories]
        result[user_id] = BanEvidence(
            record_id=row.id,
            message_id=row.message_id,
            conversation_id=row.conversation_id,
            conversation_title=titles.get(row.conversation_id) if row.conversation_id else None,
            content_type=row.content_type,
            content_text=row.content_text,
            categories=categories,
            severity=row.severity,
            confidence=row.confidence,
            reason=row.reason,
            decision=row.decision,
            created_at=row.created_at,
        )
    return result


async def _latest_ban_audit(db: AsyncSession, user_ids: list[int]) -> dict[int, AdminAuditLog]:
    """每个用户最近一条与封禁有关的人工操作记录。"""
    if not user_ids:
        return {}
    rows = list(
        (
            await db.execute(
                select(AdminAuditLog)
                .options(joinedload(AdminAuditLog.actor))
                .where(
                    AdminAuditLog.target_type == "user",
                    AdminAuditLog.target_id.in_(user_ids),
                    AdminAuditLog.action.in_(["user.status.update", "report.review", "report.reviewed"]),
                )
                .order_by(AdminAuditLog.created_at.desc(), AdminAuditLog.id.desc())
            )
        ).scalars().unique().all()
    )
    latest: dict[int, AdminAuditLog] = {}
    for row in rows:
        if row.target_id is not None and row.target_id not in latest:
            latest[row.target_id] = row
    return latest


async def list_admin_banned_users(db: AsyncSession, limit: int = 100, offset: int = 0, query: str | None = None) -> list[AdminUserRead]:
    await _release_expired_bans(db)
    return await _list_admin_users(db, limit, offset, query, User.status == UserStatus.DISABLED)


async def list_platform_admins(db: AsyncSession, limit: int = 100, offset: int = 0, query: str | None = None) -> list[AdminUserRead]:
    return await _list_admin_users(db, limit, offset, query, User.platform_role == PlatformRole.ADMIN)


async def _release_expired_bans(db: AsyncSession) -> None:
    result = await db.execute(
        update(User)
        .where(User.status == UserStatus.DISABLED, User.banned_until.is_not(None), User.banned_until <= datetime.now(UTC))
        .values(status=UserStatus.ACTIVE, banned_until=None, banned_at=None, ban_reason=None)
    )
    if result.rowcount:
        await db.commit()


async def _list_admin_users(db: AsyncSession, limit: int = 100, offset: int = 0, query: str | None = None, *filters) -> list[AdminUserRead]:
    stmt = select(User).order_by(User.created_at.desc(), User.id.desc()).offset(offset).limit(limit)
    if query and query.strip():
        keyword = f"%{query.strip()}%"
        stmt = stmt.where(or_(User.username.ilike(keyword), User.nickname.ilike(keyword), User.email.ilike(keyword)))
    if filters:
        stmt = stmt.where(*filters)
    result = await db.execute(stmt)
    users = list(result.scalars().all())
    user_ids = [user.id for user in users]
    if not user_ids:
        return []

    message_counts = await _count_by_user(db, select(Message.sender_id, func.count()).where(Message.sender_id.in_(user_ids)).group_by(Message.sender_id))
    submitted_counts = await _count_by_user(db, select(UserReport.reporter_id, func.count()).where(UserReport.reporter_id.in_(user_ids)).group_by(UserReport.reporter_id))
    message_report_counts = await _count_by_user(
        db,
        select(UserReport.reported_user_id, func.count())
        .where(UserReport.reported_user_id.in_(user_ids))
        .group_by(UserReport.reported_user_id),
    )
    user_report_counts = await _count_by_user(
        db,
        select(UserReport.target_id, func.count())
        .where(UserReport.target_type == ReportTargetType.USER, UserReport.target_id.in_(user_ids))
        .group_by(UserReport.target_id),
    )
    active_sessions = await _active_session_summary(db, user_ids)
    moderation_flagged_counts = await _count_by_user(
        db,
        select(ContentModerationRecord.user_id, func.count())
        .where(ContentModerationRecord.user_id.in_(user_ids), ContentModerationRecord.decision != "allow")
        .group_by(ContentModerationRecord.user_id),
    )
    moderation_block_counts = await _count_by_user(
        db,
        select(ContentModerationRecord.user_id, func.count())
        .where(ContentModerationRecord.user_id.in_(user_ids), ContentModerationRecord.decision == "block")
        .group_by(ContentModerationRecord.user_id),
    )
    moderation_recalled_counts = await _count_by_user(
        db,
        select(ContentModerationRecord.user_id, func.count())
        .where(ContentModerationRecord.user_id.in_(user_ids), ContentModerationRecord.action_taken == ModerationAction.RECALLED)
        .group_by(ContentModerationRecord.user_id),
    )
    moderation_banned_counts = await _count_by_user(
        db,
        select(ContentModerationRecord.user_id, func.count())
        .where(ContentModerationRecord.user_id.in_(user_ids), ContentModerationRecord.action_taken.in_([ModerationAction.USER_BANNED, ModerationAction.PERMANENT_BAN]))
        .group_by(ContentModerationRecord.user_id),
    )
    moderation_latest = await _latest_moderation_by_user(db, user_ids)

    # 先铺一层公开资料（CCW 绑定、账号等级、装扮……都是用户这一行上的列），再叠后台专有的字段。
    # 以前是逐个字段手写的，UserPublic 上后来加的字段到不了后台：已绑定的用户在后台显示成「未绑定」、等级恒为 0
    return [
        AdminUserRead(**{**UserPublic.model_validate(user).model_dump(), **dict(
            id=user.id,
            username=user.username,
            nickname=user.nickname,
            avatar_url=user.avatar_url,
            bio=user.bio,
            platform_role=user.platform_role,
            status=user.status,
            banned_until=user.banned_until,
            ban_reason=user.ban_reason,
            created_at=user.created_at,
            # 心意值和生日（docs/gifts.md、docs/chat-features.md 第 8 节）。后台总是带完整生日（含年份），不管本人选没选公开
            charm=user.charm or 0,
            birthday=admin_birthday(user),
            is_birthday_today=user.is_birthday_today,
            email=user.email,
            updated_at=user.updated_at,
            content_moderation_whitelisted=user.content_moderation_whitelisted,
            registration_ip=user.registration_ip,
            last_login_ip=user.last_login_ip,
            last_login_at=user.last_login_at,
            last_seen_ip=user.last_seen_ip,
            last_seen_at=user.last_seen_at,
            message_count=message_counts.get(user.id, 0),
            report_count=submitted_counts.get(user.id, 0),
            reports_submitted_count=submitted_counts.get(user.id, 0),
            reports_against_count=message_report_counts.get(user.id, 0) + user_report_counts.get(user.id, 0),
            moderation_flagged_count=moderation_flagged_counts.get(user.id, 0),
            moderation_block_count=moderation_block_counts.get(user.id, 0),
            moderation_recalled_count=moderation_recalled_counts.get(user.id, 0),
            moderation_banned_count=moderation_banned_counts.get(user.id, 0),
            moderation_latest_reason=moderation_latest.get(user.id, {}).get("reason"),
            moderation_latest_at=moderation_latest.get(user.id, {}).get("at"),
            active_session_count=active_sessions.get(user.id, {}).get("count", 0),
            last_session_ip=active_sessions.get(user.id, {}).get("ip"),
            last_session_at=active_sessions.get(user.id, {}).get("at"),
        )})
        for user in users
    ]


async def create_platform_admin(db: AsyncSession, payload: AdminUserCreate, actor_id: int | None = None) -> User:
    email = payload.email.strip() if payload.email and payload.email.strip() else None
    checks = [User.username == payload.username]
    if email is not None:
        checks.append(User.email == email)
    existing = await db.execute(select(User).where(or_(*checks)))
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Username or email already exists")

    user = User(
        username=payload.username,
        email=email,
        hashed_password=hash_password(payload.password),
        nickname=payload.nickname or payload.username,
        platform_role=PlatformRole.ADMIN,
    )
    db.add(user)
    await db.flush()
    db.add(
        AdminAuditLog(
            actor_id=actor_id,
            action="user.admin.create",
            target_type="user",
            target_id=user.id,
            details=json.dumps({"username": user.username, "email": user.email}, ensure_ascii=False),
        )
    )
    await db.commit()
    await db.refresh(user)
    return user


async def update_user_platform_role(
    db: AsyncSession,
    user_id: int,
    role: str,
    actor_id: int | None = None,
    reason: str | None = None,
) -> User:
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    platform_role = PlatformRole(role)
    if actor_id == user.id and platform_role != PlatformRole.ADMIN:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot remove your own admin privileges")
    if user.platform_role == platform_role:
        return user
    if user.platform_role == PlatformRole.ADMIN and platform_role != PlatformRole.ADMIN:
        admin_count_result = await db.execute(select(func.count()).select_from(User).where(User.platform_role == PlatformRole.ADMIN))
        if int(admin_count_result.scalar_one() or 0) <= 1:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot remove the last admin")

    previous_role = user.platform_role.value
    user.platform_role = platform_role
    db.add(
        AdminAuditLog(
            actor_id=actor_id,
            action="user.platform_role.update",
            target_type="user",
            target_id=user.id,
            details=json.dumps({"from": previous_role, "to": platform_role.value, "reason": reason}, ensure_ascii=False),
        )
    )
    await db.commit()
    await db.refresh(user)
    return user


async def _count_by_user(db: AsyncSession, stmt) -> dict[int, int]:
    result = await db.execute(stmt)
    return {int(user_id): int(count) for user_id, count in result.all() if user_id is not None}


async def _active_session_summary(db: AsyncSession, user_ids: list[int]) -> dict[int, dict[str, object]]:
    now = datetime.now(UTC)
    result = await db.execute(
        select(AuthSession.user_id, AuthSession.client_ip, AuthSession.last_used_at, AuthSession.created_at)
        .where(
            AuthSession.user_id.in_(user_ids),
            AuthSession.revoked_at.is_(None),
            AuthSession.expires_at > now,
        )
        .order_by(AuthSession.user_id, func.coalesce(AuthSession.last_used_at, AuthSession.created_at).desc())
    )
    summary: dict[int, dict[str, object]] = {}
    for user_id, client_ip, last_used_at, created_at in result.all():
        info = summary.setdefault(int(user_id), {"count": 0, "ip": None, "at": None})
        info["count"] = int(info["count"]) + 1
        if info["ip"] is None:
            info["ip"] = client_ip
            info["at"] = last_used_at or created_at
    return summary


async def _latest_moderation_by_user(db: AsyncSession, user_ids: list[int]) -> dict[int, dict[str, object]]:
    result = await db.execute(
        select(ContentModerationRecord.user_id, ContentModerationRecord.reason, ContentModerationRecord.created_at)
        .where(ContentModerationRecord.user_id.in_(user_ids), ContentModerationRecord.decision != "allow")
        .order_by(ContentModerationRecord.user_id, ContentModerationRecord.created_at.desc(), ContentModerationRecord.id.desc())
    )
    summary: dict[int, dict[str, object]] = {}
    for user_id, reason, created_at in result.all():
        if user_id is not None and int(user_id) not in summary:
            summary[int(user_id)] = {"reason": reason, "at": created_at}
    return summary


async def update_user_status(
    db: AsyncSession,
    user_id: int,
    user_status: str,
    actor_id: int | None = None,
    reason: str | None = None,
    banned_until: datetime | None = None,
    ban_duration_hours: int | None = None,
) -> User:
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    if actor_id == user.id and user_status == UserStatus.DISABLED.value:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot disable yourself")
    if user.platform_role == PlatformRole.ADMIN and user_status == UserStatus.DISABLED.value:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cannot disable an admin")
    if UserStatus(user_status) == UserStatus.DISABLED:
        now = datetime.now(UTC)
        await penalty_service.record_violation(
            db,
            user_id=user.id,
            category="admin_action",
            level=0,
            source="admin",
            actor_id=actor_id,
            reason=reason,
            override=penalty_service.Penalty("ban", _ban_hours(banned_until, ban_duration_hours, now)),
        )
        # 管理员手动封禁时写的理由就是要给用户看的原话，不用台账拼出来的那句。
        if reason:
            user.ban_reason = reason
    else:
        await penalty_service.lift_ban(db, user, actor_id=actor_id)
    db.add(
        AdminAuditLog(
            actor_id=actor_id,
            action="user.status.update",
            target_type="user",
            target_id=user.id,
            details=json.dumps({"status": user_status, "reason": reason, "banned_until": user.banned_until.isoformat() if user.banned_until else None}, ensure_ascii=False),
        )
    )
    await db.commit()
    await penalty_service.flush_notifications(db)
    await db.refresh(user)
    return user


async def reset_user_password(
    db: AsyncSession,
    user_id: int,
    actor_id: int,
    payload: AdminUserPasswordReset,
) -> User:
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    if user.id == actor_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot reset your own password here")
    if user.platform_role == PlatformRole.ADMIN:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cannot reset another admin password")

    now = datetime.now(UTC)
    user.hashed_password = hash_password(payload.new_password)
    user.ip_login_expires_at = None
    await db.execute(
        update(AuthSession)
        .where(AuthSession.user_id == user.id, AuthSession.revoked_at.is_(None))
        .values(revoked_at=now)
    )
    await log_admin_action(
        db,
        actor_id,
        "user.password.reset",
        "user",
        user.id,
        {"username": user.username, "reason": payload.reason.strip()},
    )
    await db.commit()
    await db.refresh(user)
    return user


async def update_user_moderation_whitelist(
    db: AsyncSession,
    user_id: int,
    whitelisted: bool,
    actor_id: int | None = None,
    reason: str | None = None,
) -> User:
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    user.content_moderation_whitelisted = whitelisted
    db.add(
        AdminAuditLog(
            actor_id=actor_id,
            action="user.moderation_whitelist.update",
            target_type="user",
            target_id=user.id,
            details=json.dumps({"content_moderation_whitelisted": whitelisted, "reason": reason}, ensure_ascii=False),
        )
    )
    await db.commit()
    await db.refresh(user)
    return user


async def list_conversations(
    db: AsyncSession,
    limit: int = 100,
    offset: int = 0,
    query: str | None = None,
    conversation_type: ConversationType | None = None,
    participant_user_id: int | None = None,
) -> list[Conversation]:
    stmt = select(Conversation)
    if conversation_type is not None:
        stmt = stmt.where(Conversation.type == conversation_type)
    if participant_user_id is not None:
        stmt = stmt.where(
            exists().where(
                ConversationMember.conversation_id == Conversation.id,
                ConversationMember.user_id == participant_user_id,
            )
        )
    if query and query.strip():
        term = query.strip()
        keyword = f"%{term}%"
        participant_match = exists().where(
            ConversationMember.conversation_id == Conversation.id,
            ConversationMember.user_id == User.id,
            or_(User.username.ilike(keyword), User.nickname.ilike(keyword)),
        )
        conditions = [Conversation.title.ilike(keyword), participant_match]
        if term.isdigit():
            conditions.append(Conversation.id == int(term))
        stmt = stmt.where(or_(*conditions))
    result = await db.execute(stmt.order_by(Conversation.updated_at.desc(), Conversation.id.desc()).offset(offset).limit(limit))
    return list(result.scalars().all())


async def build_conversation_reads(db: AsyncSession, items: list[Conversation]) -> list[AdminConversationRead]:
    if not items:
        return []
    conversation_ids = [item.id for item in items]
    owner_result = await db.execute(
        select(ConversationMember)
        .options(joinedload(ConversationMember.user))
        .where(
            ConversationMember.conversation_id.in_(conversation_ids),
            ConversationMember.role == MemberRole.OWNER,
        )
    )
    owners = {member.conversation_id: member for member in owner_result.scalars().unique().all()}
    member_count_result = await db.execute(
        select(ConversationMember.conversation_id, func.count(ConversationMember.id))
        .where(ConversationMember.conversation_id.in_(conversation_ids))
        .group_by(ConversationMember.conversation_id)
    )
    member_counts = {conversation_id: int(count) for conversation_id, count in member_count_result.all()}
    message_summary_result = await db.execute(
        select(Message.conversation_id, func.count(Message.id), func.max(Message.created_at))
        .where(Message.conversation_id.in_(conversation_ids))
        .group_by(Message.conversation_id)
    )
    message_summaries = {
        conversation_id: (int(count), last_message_at)
        for conversation_id, count, last_message_at in message_summary_result.all()
    }
    pending_result = await db.execute(
        select(GroupJoinRequest.conversation_id, func.count(GroupJoinRequest.id))
        .where(
            GroupJoinRequest.conversation_id.in_(conversation_ids),
            GroupJoinRequest.status == GroupJoinRequestStatus.PENDING,
        )
        .group_by(GroupJoinRequest.conversation_id)
    )
    pending_counts = {conversation_id: int(count) for conversation_id, count in pending_result.all()}
    direct_ids = [
        item.id for item in items
        if (item.type.value if isinstance(item.type, ConversationType) else item.type) == ConversationType.DIRECT.value
    ]
    direct_names: dict[int, list[str]] = {}
    if direct_ids:
        participant_result = await db.execute(
            select(ConversationMember.conversation_id, User)
            .join(User, User.id == ConversationMember.user_id)
            .where(ConversationMember.conversation_id.in_(direct_ids))
            .order_by(ConversationMember.conversation_id, ConversationMember.joined_at.asc(), ConversationMember.id.asc())
        )
        for conversation_id, participant in participant_result.all():
            direct_names.setdefault(conversation_id, []).append(participant.nickname or participant.username)

    reads: list[AdminConversationRead] = []
    for item in items:
        conversation_type = item.type.value if isinstance(item.type, ConversationType) else item.type
        owner_member = owners.get(item.id)
        message_count, last_message_at = message_summaries.get(item.id, (0, None))
        reads.append(AdminConversationRead(
            id=item.id,
            type=conversation_type,
            title=item.title,
            display_title=" ↔ ".join(direct_names.get(item.id, [])) if conversation_type == ConversationType.DIRECT.value else item.title or "未命名群聊",
            avatar_url=item.avatar_url,
            description=item.description,
            category=item.category,
            announcement=item.announcement,
            all_muted=item.all_muted,
            slow_mode_seconds=item.slow_mode_seconds,
            message_rate_limit_per_minute=item.message_rate_limit_per_minute,
            join_mode=item.join_mode,
            auto_approve=item.auto_approve,
            created_by_id=item.created_by_id,
            owner_id=owner_member.user_id if owner_member else None,
            owner=owner_member.user if owner_member else None,
            pending_join_request_count=pending_counts.get(item.id, 0),
            created_at=item.created_at,
            updated_at=item.updated_at,
            member_count=member_counts.get(item.id, 0),
            participants=[],
            message_count=message_count,
            last_message_at=last_message_at,
        ))
    return reads


async def get_conversation_read(db: AsyncSession, conversation_id: int) -> AdminConversationRead | None:
    item = await db.get(Conversation, conversation_id)
    if item is None:
        return None
    reads = await build_conversation_reads(db, [item])
    return reads[0]


async def list_admin_conversation_members(
    db: AsyncSession,
    conversation_id: int,
    *,
    limit: int = 40,
    offset: int = 0,
    search: str | None = None,
) -> tuple[list[User], int]:
    if await db.get(Conversation, conversation_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")
    conditions = [ConversationMember.conversation_id == conversation_id]
    normalized_search = search.strip() if search else ""
    if normalized_search:
        pattern = f"%{normalized_search}%"
        conditions.append(
            or_(
                ConversationMember.nickname.ilike(pattern),
                ConversationMember.remark.ilike(pattern),
                ConversationMember.title.ilike(pattern),
                User.username.ilike(pattern),
                User.nickname.ilike(pattern),
            )
        )
    total_result = await db.execute(
        select(func.count(ConversationMember.id))
        .join(User, User.id == ConversationMember.user_id)
        .where(*conditions)
    )
    result = await db.execute(
        select(User)
        .join(ConversationMember, ConversationMember.user_id == User.id)
        .where(*conditions)
        .order_by(
            case(
                (ConversationMember.role == MemberRole.OWNER, 0),
                (ConversationMember.role == MemberRole.ADMIN, 1),
                else_=2,
            ),
            ConversationMember.joined_at.asc(),
            ConversationMember.id.asc(),
        )
        .offset(offset)
        .limit(limit)
    )
    return list(result.scalars().all()), int(total_result.scalar_one() or 0)


async def update_conversation(
    db: AsyncSession,
    conversation_id: int,
    payload: AdminConversationUpdate,
    actor_id: int,
) -> Conversation:
    conversation = await db.get(Conversation, conversation_id)
    if conversation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")
    if conversation.type != ConversationType.GROUP:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Only group conversations can be managed here")

    updates = payload.model_dump(exclude_unset=True)
    for field, value in updates.items():
        setattr(conversation, field, value)
    if updates:
        conversation.updated_at = datetime.now(UTC)
        db.add(
            AdminAuditLog(
                actor_id=actor_id,
                action="conversation.update",
                target_type="conversation",
                target_id=conversation.id,
                details=json.dumps(updates, ensure_ascii=False),
            )
        )
        await db.commit()
        await db.refresh(conversation)
    return conversation


async def conversation_member_count(db: AsyncSession, conversation_id: int) -> int:
    result = await db.execute(
        select(func.count()).select_from(ConversationMember).where(ConversationMember.conversation_id == conversation_id)
    )
    return result.scalar_one()


async def admin_stats(db: AsyncSession) -> dict[str, int]:
    today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    return {
        "users_total": await _count(db, select(func.count()).select_from(User)),
        "users_active": await _count(db, select(func.count()).select_from(User).where(User.status == UserStatus.ACTIVE)),
        "users_banned": await _count(db, select(func.count()).select_from(User).where(User.status == UserStatus.DISABLED)),
        "conversations_total": await _count(db, select(func.count()).select_from(Conversation)),
        "direct_conversations": await _count(db, select(func.count()).select_from(Conversation).where(Conversation.type == ConversationType.DIRECT)),
        "group_conversations": await _count(db, select(func.count()).select_from(Conversation).where(Conversation.type == ConversationType.GROUP)),
        "messages_total": await _count(db, select(func.count()).select_from(Message)),
        "private_messages_total": await _count(
            db,
            select(func.count()).select_from(Message).join(Conversation).where(Conversation.type == ConversationType.DIRECT),
        ),
        "reports_pending": await _count(db, select(func.count()).select_from(UserReport).where(UserReport.status == ReportStatus.PENDING)),
        "audit_actions_today": await _count(db, select(func.count()).select_from(AdminAuditLog).where(AdminAuditLog.created_at >= today)),
        "appeals_pending": await _count(db, select(func.count()).select_from(UserViolation).where(UserViolation.appeal_status == "pending")),
        "cases_admin_pending": await _count(
            db, select(func.count()).select_from(ReportCase).where(ReportCase.status == "open", ReportCase.queue != "jury")
        ),
        "cases_jury_pending": await _count(
            db, select(func.count()).select_from(ReportCase).where(ReportCase.status == "open", ReportCase.queue == "jury")
        ),
        "jury_applications_pending": await _count(db, select(func.count()).select_from(JuryMember).where(JuryMember.status == "applied")),
        "penalty_reviews_pending": await _count(
            db,
            select(func.count()).select_from(UserViolation).where(UserViolation.review_status == "pending", UserViolation.status == "active"),
        ),
    }


async def list_audit_logs(db: AsyncSession, limit: int = 100, offset: int = 0) -> list[AdminAuditLog]:
    result = await db.execute(
        select(AdminAuditLog)
        .options(joinedload(AdminAuditLog.actor))
        .order_by(AdminAuditLog.created_at.desc(), AdminAuditLog.id.desc())
        .offset(offset)
        .limit(limit)
    )
    return list(result.scalars().all())


async def list_password_reset_requests(db: AsyncSession, request_status: PasswordResetStatus | None = None, limit: int = 100, offset: int = 0) -> list[PasswordResetRequest]:
    stmt = (
        select(PasswordResetRequest)
        .options(joinedload(PasswordResetRequest.account_user), joinedload(PasswordResetRequest.reviewer))
        .order_by(PasswordResetRequest.created_at.desc(), PasswordResetRequest.id.desc())
        .offset(offset)
        .limit(limit)
    )
    if request_status is not None:
        stmt = stmt.where(PasswordResetRequest.status == request_status)
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def review_password_reset_request(db: AsyncSession, request_id: int, admin_id: int, payload: AdminPasswordResetReview) -> PasswordResetRequest:
    item = await db.get(PasswordResetRequest, request_id)
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Password reset request not found")
    if item.status != PasswordResetStatus.PENDING:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Password reset request has already been reviewed")

    item.status = payload.status
    item.reviewer_id = admin_id
    item.review_note = payload.review_note
    item.reviewed_at = datetime.now(UTC)

    if payload.status == PasswordResetStatus.APPROVED:
        if item.account_user_id is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Target account not found")
        user = await db.get(User, item.account_user_id)
        if user is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Target account not found")
        if user.platform_role == PlatformRole.ADMIN:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cannot reset another admin password from a request")
        assert payload.new_password is not None
        user.hashed_password = hash_password(payload.new_password)
        user.ip_login_expires_at = None
        await db.execute(
            update(AuthSession)
            .where(AuthSession.user_id == user.id, AuthSession.revoked_at.is_(None))
            .values(revoked_at=datetime.now(UTC))
        )
        await log_admin_action(db, admin_id, "password_reset.approve", "password_reset_request", item.id, {"user_id": user.id, "username": user.username})
    else:
        await log_admin_action(db, admin_id, "password_reset.reject", "password_reset_request", item.id, {"username": item.username})

    await db.commit()
    result = await db.execute(
        select(PasswordResetRequest)
        .options(joinedload(PasswordResetRequest.account_user), joinedload(PasswordResetRequest.reviewer))
        .where(PasswordResetRequest.id == item.id)
    )
    refreshed = result.scalar_one()
    return refreshed


async def log_admin_action(
    db: AsyncSession,
    actor_id: int,
    action: str,
    target_type: str | None = None,
    target_id: int | None = None,
    details: dict[str, object | None] | None = None,
) -> None:
    db.add(
        AdminAuditLog(
            actor_id=actor_id,
            action=action,
            target_type=target_type,
            target_id=target_id,
            details=json.dumps(details, ensure_ascii=False) if details is not None else None,
        )
    )


async def log_sensitive_read(
    db: AsyncSession,
    actor_id: int,
    action: str,
    target_type: str,
    target_id: int,
    details: dict[str, object | None] | None = None,
) -> None:
    try:
        await log_admin_action(db, actor_id, action, target_type, target_id, details)
        await db.commit()
    except Exception:
        await db.rollback()
        logger.exception(
            "Failed to persist admin sensitive-read audit",
            extra={"actor_id": actor_id, "action": action, "target_type": target_type, "target_id": target_id},
        )


async def _count(db: AsyncSession, stmt) -> int:
    result = await db.execute(stmt)
    return int(result.scalar_one() or 0)
