from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import get_current_admin
from app.models.enums import ConversationType, ModerationAction, ModerationSource, ModerationStatus, PasswordResetStatus, ReportStatus, ReportTargetType
from app.models.post import Post
from app.models.user import User
from app.schemas.message import MessageRead
from app.schemas.post import AdminPostRead
from app.schemas.report import (
    AdminAuditLogRead,
    AdminConversationUpdate,
    AdminConversationRead,
    AdminConversationMemberPage,
    AdminMessageRecall,
    AdminMessageContextRead,
    AdminPasswordResetRequestRead,
    AdminPasswordResetReview,
    AdminUserCreate,
    AdminUserModerationWhitelistUpdate,
    AdminUserPasswordReset,
    AdminUserRoleUpdate,
    AdminStats,
    AdminUserRead,
    AdminUserStatusUpdate,
    ContentModerationRecordRead,
    DownloadSettingsRead,
    DownloadSettingsUpdate,
    ReportRead,
    ReportReview,
)
from app.services import messages as message_service
from app.services import moderation as moderation_service
from app.services import reports as report_service
from app.services import announcements as announcement_service
from app.services import posts as post_service
from app.services import system_settings as settings_service
from app.schemas.announcement import AnnouncementCreate, AnnouncementRead, AnnouncementUpdate
from app.schemas.common import MessageResponse
from app.websocket.manager import admin_manager, manager


router = APIRouter()


def _account_suspension_data(user: User) -> dict[str, object]:
    return {
        "code": "account_suspended",
        "reason": user.ban_reason,
        "banned_until": user.banned_until.isoformat() if user.banned_until else None,
        "permanent": user.banned_until is None,
    }


@router.get("/stats", response_model=AdminStats)
async def admin_overview(
    _admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, int]:
    return await report_service.admin_stats(db)


@router.get("/users", response_model=list[AdminUserRead])
async def admin_users(
    _admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
    q: Annotated[str | None, Query(max_length=100)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[AdminUserRead]:
    return await report_service.list_admin_users(db, limit, offset, q)


@router.get("/users/{user_id}", response_model=AdminUserRead)
async def admin_user_detail(
    user_id: int,
    _admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AdminUserRead:
    return await report_service.get_admin_user(db, user_id)


@router.get("/bans", response_model=list[AdminUserRead])
async def admin_banned_users(
    _admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
    q: Annotated[str | None, Query(max_length=100)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[AdminUserRead]:
    return await report_service.list_admin_banned_users(db, limit, offset, q)


@router.get("/platform-admins", response_model=list[AdminUserRead])
async def admin_platform_admins(
    _admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
    q: Annotated[str | None, Query(max_length=100)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[AdminUserRead]:
    return await report_service.list_platform_admins(db, limit, offset, q)


@router.post("/platform-admins", response_model=AdminUserRead, status_code=201)
async def admin_create_platform_admin(
    payload: AdminUserCreate,
    admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AdminUserRead:
    user = await report_service.create_platform_admin(db, payload, admin.id)
    await admin_manager.broadcast("admin.user.changed", {"user_id": user.id, "action": "admin_created"})
    return await report_service.get_admin_user(db, user.id)


@router.patch("/users/{user_id}/status", response_model=AdminUserRead)
async def admin_update_user_status(
    user_id: int,
    payload: AdminUserStatusUpdate,
    _admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AdminUserRead:
    user = await report_service.update_user_status(
        db,
        user_id,
        payload.status,
        _admin.id,
        payload.reason,
        payload.banned_until,
        payload.ban_duration_hours,
    )
    await admin_manager.broadcast("admin.user.changed", {"user_id": user.id, "action": "status_updated"})
    if payload.status == "disabled":
        await manager.disconnect_user(user.id, "account.suspended", _account_suspension_data(user))
    return await report_service.get_admin_user(db, user.id)


@router.patch("/users/{user_id}/platform-role", response_model=AdminUserRead)
async def admin_update_user_platform_role(
    user_id: int,
    payload: AdminUserRoleUpdate,
    admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AdminUserRead:
    user = await report_service.update_user_platform_role(
        db,
        user_id,
        payload.platform_role,
        admin.id,
        payload.reason,
    )
    await admin_manager.broadcast("admin.user.changed", {"user_id": user.id, "action": "platform_role_updated"})
    return await report_service.get_admin_user(db, user.id)


@router.patch("/users/{user_id}/moderation-whitelist", response_model=AdminUserRead)
async def admin_update_user_moderation_whitelist(
    user_id: int,
    payload: AdminUserModerationWhitelistUpdate,
    admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AdminUserRead:
    user = await report_service.update_user_moderation_whitelist(
        db,
        user_id,
        payload.content_moderation_whitelisted,
        admin.id,
        payload.reason,
    )
    await admin_manager.broadcast("admin.user.changed", {"user_id": user.id, "action": "moderation_whitelist_updated"})
    return await report_service.get_admin_user(db, user.id)


@router.post("/users/{user_id}/password-reset", response_model=AdminUserRead)
async def admin_reset_user_password(
    user_id: int,
    payload: AdminUserPasswordReset,
    admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AdminUserRead:
    user = await report_service.reset_user_password(db, user_id, admin.id, payload)
    await admin_manager.broadcast("admin.user.changed", {"user_id": user.id, "action": "password_reset"})
    await manager.disconnect_user(user.id, "auth.password_reset", {"reason": "管理员已重置账号密码，请使用新密码重新登录"})
    return await report_service.get_admin_user(db, user.id)


@router.get("/conversations", response_model=list[AdminConversationRead])
async def admin_conversations(
    _admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
    q: Annotated[str | None, Query(max_length=120)] = None,
    conversation_type: Annotated[ConversationType | None, Query(alias="type")] = None,
    participant_user_id: Annotated[int | None, Query(gt=0)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[AdminConversationRead]:
    items = await report_service.list_conversations(db, limit, offset, q, conversation_type, participant_user_id)
    return await report_service.build_conversation_reads(db, items)


@router.get("/conversations/{conversation_id}", response_model=AdminConversationRead)
async def admin_conversation_detail(
    conversation_id: int,
    _admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AdminConversationRead:
    item = await report_service.get_conversation_read(db, conversation_id)
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")
    return item


@router.get("/conversations/{conversation_id}/members", response_model=AdminConversationMemberPage)
async def admin_conversation_members(
    conversation_id: int,
    admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=100)] = 40,
    offset: Annotated[int, Query(ge=0)] = 0,
    search: Annotated[str | None, Query(max_length=50)] = None,
) -> AdminConversationMemberPage:
    members, total = await report_service.list_admin_conversation_members(
        db, conversation_id, limit=limit, offset=offset, search=search
    )
    await report_service.log_sensitive_read(
        db,
        admin.id,
        "conversation.members.read",
        "conversation",
        conversation_id,
        {"limit": limit, "offset": offset, "search": search, "result_count": len(members)},
    )
    return AdminConversationMemberPage(
        items=members,
        total=total,
        limit=limit,
        offset=offset,
        has_more=offset + len(members) < total,
    )


@router.patch("/conversations/{conversation_id}", response_model=AdminConversationRead)
async def admin_update_conversation(
    conversation_id: int,
    payload: AdminConversationUpdate,
    admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AdminConversationRead:
    item = await report_service.update_conversation(db, conversation_id, payload, admin.id)
    await admin_manager.broadcast("admin.conversation.changed", {"conversation_id": item.id, "action": "updated"})
    conversation_read = await report_service.get_conversation_read(db, item.id)
    assert conversation_read is not None
    return conversation_read


@router.get("/conversations/{conversation_id}/messages/{message_id}/context", response_model=AdminMessageContextRead)
async def admin_message_context(
    conversation_id: int,
    message_id: int,
    admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
    before: Annotated[int, Query(ge=0, le=100)] = 20,
    after: Annotated[int, Query(ge=0, le=100)] = 20,
) -> AdminMessageContextRead:
    await report_service.ensure_message_is_under_report(db, message_id)
    items = await message_service.admin_message_context(db, conversation_id, message_id, before, after)
    messages = await message_service.build_message_reads(db, items, admin.id)
    conversation = await report_service.get_conversation_read(db, conversation_id)
    target = next((message for message in messages if message.id == message_id), None)
    response = AdminMessageContextRead(
        conversation=conversation,
        target_message_id=message_id,
        reported_user=target.sender if target else None,
        messages=messages,
    )
    await report_service.log_sensitive_read(
        db,
        admin.id,
        "message.context.read",
        "message",
        message_id,
        {"conversation_id": conversation_id, "before": before, "after": after, "result_count": len(messages)},
    )
    return response


@router.get("/messages/{message_id}/context", response_model=AdminMessageContextRead)
async def admin_message_context_by_id(
    message_id: int,
    admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
    before: Annotated[int, Query(ge=0, le=100)] = 20,
    after: Annotated[int, Query(ge=0, le=100)] = 20,
) -> AdminMessageContextRead:
    await report_service.ensure_message_is_under_report(db, message_id)
    items = await message_service.admin_message_context_by_id(db, message_id, before, after)
    messages = await message_service.build_message_reads(db, items, admin.id)
    conversation_id = items[0].conversation_id
    conversation = await report_service.get_conversation_read(db, conversation_id)
    for message in messages:
        message.conversation = conversation
    target = next(message for message in messages if message.id == message_id)
    response = AdminMessageContextRead(
        conversation=conversation,
        target_message_id=message_id,
        reported_user=target.sender,
        messages=messages,
    )
    await report_service.log_sensitive_read(
        db,
        admin.id,
        "message.context.read",
        "message",
        message_id,
        {"conversation_id": conversation_id, "before": before, "after": after, "result_count": len(messages)},
    )
    return response


@router.get("/posts", response_model=list[AdminPostRead])
async def admin_posts(
    _admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
    q: Annotated[str | None, Query(max_length=200)] = None,
    moderation_status: Annotated[ModerationStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[AdminPostRead]:
    posts = await post_service.list_admin_posts(db, q, moderation_status, limit, offset)
    return await post_service.build_admin_post_reads(db, posts)


@router.delete("/posts/{post_id}", response_model=MessageResponse)
async def admin_delete_post(
    post_id: int,
    admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> MessageResponse:
    post = await post_service.get_post(db, post_id)
    recipients = await post_service.post_recipient_ids(db, post)
    await post_service.admin_delete_post(db, post_id, admin.id)
    await manager.send_to_users(recipients, "post.deleted", {"post_id": post_id, "actor_id": admin.id, "admin": True})
    await admin_manager.broadcast("admin.post.changed", {"post_id": post_id, "action": "deleted"})
    return MessageResponse(detail="Post deleted")


@router.post("/messages/{message_id}/recall", response_model=MessageRead)
async def admin_recall_message(
    message_id: int,
    payload: AdminMessageRecall,
    admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> MessageRead:
    message = await message_service.admin_recall_message(db, admin.id, message_id)
    await report_service.log_admin_action(
        db,
        admin.id,
        "message.recall",
        "message",
        message_id,
        {"reason": payload.reason},
    )
    await db.commit()
    await admin_manager.broadcast("admin.message.changed", {"message_id": message.id, "conversation_id": message.conversation_id, "action": "recalled"})
    member_ids = await report_service.conversation_member_ids(db, message.conversation_id)
    for user_id in member_ids:
        item = await message_service.build_message_read(db, message, user_id)
        await manager.send_to_user(user_id, "message.recalled", item)
    return await message_service.build_message_read(db, message, admin.id)


@router.get("/reports", response_model=list[ReportRead])
async def admin_reports(
    _admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
    report_status: Annotated[ReportStatus | None, Query(alias="status")] = None,
    target_type: Annotated[ReportTargetType | None, Query(alias="target_type")] = None,
    q: Annotated[str | None, Query(max_length=120)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[ReportRead]:
    items = await report_service.list_reports(db, report_status, target_type, q, limit, offset)
    return await report_service.build_report_reads(db, items, _admin.id, include_snapshot=True)


@router.get("/reports/{report_id}", response_model=ReportRead)
async def admin_report_detail(
    report_id: int,
    admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ReportRead:
    report = await report_service.get_report(db, report_id)
    return await report_service.build_report_read(db, report, admin.id, include_snapshot=True)


@router.get("/password-reset-requests", response_model=list[AdminPasswordResetRequestRead])
async def admin_password_reset_requests(
    _admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
    request_status: Annotated[PasswordResetStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[AdminPasswordResetRequestRead]:
    items = await report_service.list_password_reset_requests(db, request_status, limit, offset)
    return [AdminPasswordResetRequestRead.model_validate(item) for item in items]


@router.post("/password-reset-requests/{request_id}/review", response_model=AdminPasswordResetRequestRead)
async def admin_review_password_reset_request(
    request_id: int,
    payload: AdminPasswordResetReview,
    admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AdminPasswordResetRequestRead:
    item = await report_service.review_password_reset_request(db, request_id, admin.id, payload)
    await admin_manager.broadcast("admin.password_reset.changed", {"request_id": item.id, "action": item.status})
    if item.status == PasswordResetStatus.APPROVED and item.account_user_id is not None:
        await manager.disconnect_user(item.account_user_id, "auth.password_reset", {"reason": "密码已重置，请使用新密码重新登录"})
    return AdminPasswordResetRequestRead.model_validate(item)


@router.post("/reports/{report_id}/review", response_model=ReportRead)
async def admin_review_report(
    report_id: int,
    payload: ReportReview,
    admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ReportRead:
    original = await report_service.get_report(db, report_id)
    message_recipients: list[int] = []
    post_recipients: set[int] = set()
    if payload.status == ReportStatus.REVIEWED and (payload.delete_content or payload.action in {"recall_message", "delete_post"}):
        if original.target_type == ReportTargetType.MESSAGE and original.conversation_id is not None:
            message_recipients = await report_service.conversation_member_ids(db, original.conversation_id)
        elif original.target_type == ReportTargetType.POST:
            post = await db.get(Post, original.target_id)
            if post is not None and post.deleted_at is None:
                post_recipients = await post_service.post_recipient_ids(db, post)
    report = await report_service.review_report(db, report_id, admin.id, payload)
    await admin_manager.broadcast("admin.report.changed", {"report_id": report.id, "action": "reviewed"})
    if "recall_message" in (report.action or "") and report.message_id is not None:
        message = await message_service.get_message(db, report.message_id)
        for user_id in message_recipients:
            item = await message_service.build_message_read(db, message, user_id)
            await manager.send_to_user(user_id, "message.recalled", item)
        await admin_manager.broadcast("admin.message.changed", {"message_id": message.id, "conversation_id": message.conversation_id, "action": "recalled"})
    if "delete_post" in (report.action or ""):
        await manager.send_to_users(post_recipients, "post.deleted", {"post_id": report.target_id, "actor_id": admin.id, "admin": True})
        await admin_manager.broadcast("admin.post.changed", {"post_id": report.target_id, "action": "deleted"})
    if "disable_user" in (report.action or "") and report.reported_user_id is not None:
        user = await db.get(User, report.reported_user_id)
        if user is not None:
            await manager.disconnect_user(user.id, "account.suspended", _account_suspension_data(user))
            await admin_manager.broadcast("admin.user.changed", {"user_id": user.id, "action": "status_updated"})
    return await report_service.build_report_read(db, report, admin.id, include_snapshot=True)


@router.get("/audit-logs", response_model=list[AdminAuditLogRead])
async def admin_audit_logs(
    _admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[AdminAuditLogRead]:
    items = await report_service.list_audit_logs(db, limit, offset)
    return [AdminAuditLogRead.model_validate(item) for item in items]


@router.get("/moderation-records", response_model=list[ContentModerationRecordRead])
async def admin_moderation_records(
    _admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
    decision: Annotated[str | None, Query(pattern="^(allow|review|block|error)$")] = None,
    action: Annotated[ModerationAction | None, Query(alias="action")] = None,
    source: Annotated[ModerationSource | None, Query(alias="source")] = None,
    content_type: Annotated[str | None, Query(pattern="^(text|image|unknown)$")] = None,
    include_allow: bool = False,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[ContentModerationRecordRead]:
    items = await moderation_service.list_moderation_records(db, decision, action, source, content_type, include_allow, limit, offset)
    return [ContentModerationRecordRead.model_validate(item) for item in items]


@router.get("/download-settings", response_model=DownloadSettingsRead)
async def admin_download_settings(
    _admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> DownloadSettingsRead:
    return await settings_service.get_download_settings(db)


@router.patch("/download-settings", response_model=DownloadSettingsRead)
async def admin_update_download_settings(
    payload: DownloadSettingsUpdate,
    admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> DownloadSettingsRead:
    return await settings_service.update_download_settings(db, admin.id, payload.pc_url, payload.android_url, payload.scratch_url, payload.pc_version, payload.mobile_version)


@router.get("/announcements", response_model=list[AnnouncementRead])
async def admin_list_announcements(
    _admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[AnnouncementRead]:
    return await announcement_service.admin_list(db)


@router.post("/announcements", response_model=AnnouncementRead, status_code=201)
async def admin_create_announcement(
    payload: AnnouncementCreate,
    admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AnnouncementRead:
    announcement = await announcement_service.admin_create(db, admin.id, payload)
    if announcement.is_active:
        await manager.broadcast("announcement.published", {"id": announcement.id})
    await admin_manager.broadcast("admin.announcement.changed", {"id": announcement.id, "action": "created"})
    return announcement


@router.put("/announcements/{announcement_id}", response_model=AnnouncementRead)
async def admin_update_announcement(
    announcement_id: int,
    payload: AnnouncementUpdate,
    admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AnnouncementRead:
    announcement = await announcement_service.admin_update(db, admin.id, announcement_id, payload)
    if announcement.is_active and payload.is_active is True:
        await manager.broadcast("announcement.published", {"id": announcement.id})
    await admin_manager.broadcast("admin.announcement.changed", {"id": announcement.id, "action": "updated"})
    return announcement


@router.post("/announcements/{announcement_id}/push", response_model=AnnouncementRead)
async def admin_push_announcement(
    announcement_id: int,
    admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AnnouncementRead:
    del admin
    announcement = await announcement_service.admin_get(db, announcement_id)
    await manager.broadcast("announcement.published", {"id": announcement.id})
    return announcement


@router.delete("/announcements/{announcement_id}", response_model=MessageResponse)
async def admin_delete_announcement(
    announcement_id: int,
    admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> MessageResponse:
    await announcement_service.admin_delete(db, admin.id, announcement_id)
    await admin_manager.broadcast("admin.announcement.changed", {"id": announcement_id, "action": "deleted"})
    return MessageResponse(detail="Announcement deleted")
