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
from app.models.enums import ConversationType, GroupJoinRequestStatus, MemberRole, ModerationAction, PasswordResetStatus, PlatformRole, ReportStatus, ReportTargetType, UserStatus
from app.models.message import Message, MessageVisibility
from app.models.post import Post
from app.models.report import AdminAuditLog, ContentModerationRecord, PasswordResetRequest, UserReport
from app.models.user import AuthSession, User
from app.schemas.report import AdminConversationRead, AdminConversationUpdate, AdminPasswordResetReview, AdminUserCreate, AdminUserPasswordReset, AdminUserRead, ReportConversationContext, ReportCreate, ReportRead, ReportReview, ReportTargetContext
from app.schemas.user import UserPublic
from app.services.messages import admin_recall_message, get_visible_message_for_user
from app.services.upload_paths import require_message_image_upload_url
from app.services import messages as message_service
from app.services import posts as post_service


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


async def create_report(db: AsyncSession, reporter_id: int, payload: ReportCreate) -> UserReport:
    target_snapshot: dict | None = None
    conversation_id: int | None = None
    message_id: int | None = None
    reported_user_id: int | None = None
    if payload.target_type == ReportTargetType.MESSAGE:
        message = await get_visible_message_for_user(db, payload.target_id, reporter_id)
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
        target_snapshot = await _user_report_snapshot(db, reported_user, reporter_id, conversation_id)

    attachments = _normalize_report_attachments(payload.attachments)
    snapshot_json = _canonical_json(target_snapshot) if target_snapshot else None
    attachments_json = _canonical_json(attachments) if attachments else None

    reason = payload.reason.strip()
    details = payload.details.strip() if payload.details else None
    existing = await _find_recent_duplicate_report(
        db,
        reporter_id=reporter_id,
        target_type=payload.target_type,
        target_id=payload.target_id,
        conversation_id=conversation_id,
        message_id=message_id,
        reported_user_id=reported_user_id,
    )
    if existing is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="A pending report already exists for this target")

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
    await db.commit()
    return await get_report(db, report.id)


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


async def _find_recent_duplicate_report(
    db: AsyncSession,
    *,
    reporter_id: int,
    target_type: ReportTargetType,
    target_id: int,
    conversation_id: int | None,
    message_id: int | None,
    reported_user_id: int | None,
) -> UserReport | None:
    stmt = select(UserReport).where(
        UserReport.reporter_id == reporter_id,
        UserReport.target_type == target_type,
        UserReport.status == ReportStatus.PENDING,
    )
    if target_type == ReportTargetType.MESSAGE:
        stmt = stmt.where(UserReport.message_id == (message_id or target_id))
    elif target_type == ReportTargetType.POST:
        stmt = stmt.where(UserReport.target_id == target_id)
    elif target_type == ReportTargetType.CONVERSATION:
        stmt = stmt.where(UserReport.conversation_id == (conversation_id or target_id))
    else:
        stmt = stmt.where(UserReport.reported_user_id == (reported_user_id or target_id))
        if conversation_id is not None:
            stmt = stmt.where(UserReport.conversation_id == conversation_id)
    result = await db.execute(stmt.order_by(UserReport.created_at.desc()).limit(1))
    return result.scalar_one_or_none()


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


async def _message_report_snapshot(db: AsyncSession, message: Message, conversation: Conversation | None, reporter_id: int) -> dict:
    membership = await db.execute(
        select(ConversationMember).where(
            ConversationMember.conversation_id == message.conversation_id,
            ConversationMember.user_id == reporter_id,
        )
    )
    member = membership.scalar_one_or_none()
    min_message_id = member.cleared_before_message_id if member and member.cleared_before_message_id is not None else None
    before_conditions = [
        Message.conversation_id == message.conversation_id,
        Message.id < message.id,
        Message.id.not_in(select(MessageVisibility.message_id).where(MessageVisibility.user_id == reporter_id)),
    ]
    after_conditions = [
        Message.conversation_id == message.conversation_id,
        Message.id > message.id,
        Message.id.not_in(select(MessageVisibility.message_id).where(MessageVisibility.user_id == reporter_id)),
    ]
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
        reporter=report.reporter,
        reviewer=report.reviewer,
        reported_user=reported_user,
        target=target,
        snapshot=target_snapshot if include_snapshot and target_snapshot else None,
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
    actions: list[str] = []
    banned_until: datetime | None = None
    target_user: User | None = None
    deleted_post: Post | None = None

    if payload.action == "recall_message" and report.target_type != ReportTargetType.MESSAGE:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Recall action requires a message report")
    if payload.action == "delete_post" and report.target_type != ReportTargetType.POST:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Delete action requires a post report")

    if payload.status == ReportStatus.REVIEWED and disable_user:
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
        if target_user.platform_role == PlatformRole.ADMIN:
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

    if payload.status == ReportStatus.REVIEWED and disable_user:
        assert target_user is not None
        target_user.status = UserStatus.DISABLED
        target_user.banned_until = _resolve_ban_until(payload.ban_until, payload.ban_duration_hours)
        target_user.ban_reason = payload.review_note or report.reason
        banned_until = target_user.banned_until
        await db.execute(
            update(AuthSession)
            .where(AuthSession.user_id == target_user.id, AuthSession.revoked_at.is_(None))
            .values(revoked_at=now)
        )
        actions.append("disable_user")

    report.reviewer_id = reviewer_id
    report.review_note = payload.review_note.strip() if payload.review_note else None
    action = "+".join(actions) if actions else "none"
    report.action = action
    report.reviewed_at = now
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
    if deleted_post is not None:
        try:
            await post_service.cleanup_deleted_post_uploads(db, deleted_post)
        except Exception:
            logger.exception("Post upload cleanup failed after report review", extra={"post_id": deleted_post.id, "report_id": report.id})
    return await get_report(db, report.id)


def _resolve_ban_until(ban_until: datetime | None, ban_duration_hours: int | None) -> datetime | None:
    if ban_until is not None:
        return ban_until
    if ban_duration_hours is not None:
        return datetime.now(UTC) + timedelta(hours=ban_duration_hours)
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


async def list_admin_banned_users(db: AsyncSession, limit: int = 100, offset: int = 0, query: str | None = None) -> list[AdminUserRead]:
    await _release_expired_bans(db)
    return await _list_admin_users(db, limit, offset, query, User.status == UserStatus.DISABLED)


async def list_platform_admins(db: AsyncSession, limit: int = 100, offset: int = 0, query: str | None = None) -> list[AdminUserRead]:
    return await _list_admin_users(db, limit, offset, query, User.platform_role == PlatformRole.ADMIN)


async def _release_expired_bans(db: AsyncSession) -> None:
    result = await db.execute(
        update(User)
        .where(User.status == UserStatus.DISABLED, User.banned_until.is_not(None), User.banned_until <= datetime.now(UTC))
        .values(status=UserStatus.ACTIVE, banned_until=None, ban_reason=None)
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

    return [
        AdminUserRead(
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
        )
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
    user.status = UserStatus(user_status)
    if user.status == UserStatus.DISABLED:
        user.banned_until = _resolve_ban_until(banned_until, ban_duration_hours)
        user.ban_reason = reason
        await db.execute(
            update(AuthSession)
            .where(AuthSession.user_id == user.id, AuthSession.revoked_at.is_(None))
            .values(revoked_at=datetime.now(UTC))
        )
    else:
        user.banned_until = None
        user.ban_reason = None
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
