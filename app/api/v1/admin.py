from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import get_current_admin
from app.models.enums import ConversationType, ModerationAction, ModerationSource, ModerationStatus, PasswordResetStatus, ReportStatus, ReportTargetType
from app.models.post import Post
from app.models.user import User
from app.schemas.message import MessageRead
from app.schemas.post import AdminPostRead
from app.schemas.report import (
    AdminBanPage,
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
    ModerationRecordVerdict,
    DownloadSettingsRead,
    DownloadSettingsUpdate,
    ReportRead,
    ReportReview,
)
from app.models.penalty import UserViolation
from app.models.report import AdminAuditLog
from app.api.v1.moderation import moderation_rules
from app.schemas.penalty import (
    ModerationRules,
    AdminAppealReview,
    AdminViolationCreate,
    AdminViolationPage,
    AdminViolationRead,
    AdminViolationReview,
    AdminViolationRevoke,
    PenaltyInput,
)
from app.models.case import JuryMember, JuryVote, ReportCase
from app.schemas.case import (
    AdminJuryOpinionModerate,
    AdminJuryOpinionPage,
    AdminJuryOpinionRead,
    CaseArchive,
    CaseArchiveResult,
    CaseDetail,
    CaseMove,
    CaseOverturn,
    CasePage,
    CaseRead,
    CaseResolve,
    CaseVoid,
    CaseVoidBulk,
    CaseVoidResult,
    JuryMemberPage,
    JuryMemberRead,
    JuryMemberReview,
    JuryVoteSummary,
)
from app.schemas.jury_senior import AdminReviewDecide, AdminReviewPage, AdminReviewRead
from app.schemas.jury import (
    AdminExamPage,
    AdminExamRead,
    AdminQuestionDetail,
    AdminQuestionAuditStatus,
    AdminQuestionPage,
    AdminQuestionRead,
    AdminQuestionUpdate,
    JurySettingsRead,
    JurySettingsUpdate,
)
from app.services import cases as case_service
from app.services import jury as jury_service
from app.services import jury_reviews as review_service
from app.services import jury_bank as bank_service
from app.services import jury_bank_audit as bank_audit_service
from app.services import jury_points as points_service
from app.services import jury_opinions as opinion_service
from app.services import jury_settings
from app.services import messages as message_service
from app.services import penalties as penalty_service
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


@router.get("/bans", response_model=AdminBanPage)
async def admin_banned_users(
    _admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
    q: Annotated[str | None, Query(max_length=100)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    sort: Annotated[Literal["banned_at", "expires", "username", "id", "created_at"], Query()] = "banned_at",
    order: Annotated[Literal["asc", "desc"], Query()] = "desc",
    scope: Annotated[Literal["all", "permanent", "temporary", "expiring"], Query()] = "all",
    include_bots: Annotated[bool, Query()] = False,
) -> AdminBanPage:
    """封禁名单。

    `q` 同时按用户名 / 昵称 / 邮箱模糊匹配和账号 ID 精确匹配（支持 `#123` 写法）。
    每行会带上这次封禁的来源、操作者和触发它的那条消息。
    机器人账号（创建者停用 / 删除后账号也是 disabled）默认不列，`include_bots=true` 才列；
    不列时 `hidden_bot_count` 是藏起来的个数。
    """
    return await report_service.list_admin_bans(db, limit, offset, q, sort, order, scope, include_bots)


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


async def _broadcast_message_recalled(db: AsyncSession, message) -> None:
    """告诉群里「这条消息被撤回了」。和用户自己撤回消息走同一条路（cases._broadcast_removed 也是）：
    负载只有撤回信息、只序列化一次，只发给此刻在线的成员；不再给每个成员各组装一遍完整消息（几千人的群要二十多秒）。"""
    from app.api.v1.conversations import online_conversation_member_ids, send_message_updates

    member_ids = await online_conversation_member_ids(db, message.conversation_id)
    await send_message_updates(db, "message.recalled", message, member_ids)


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
    await _broadcast_message_recalled(db, message)
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
    post_recipients: set[int] = set()
    if payload.status == ReportStatus.REVIEWED and (payload.delete_content or payload.action in {"recall_message", "delete_post"}):
        if original.target_type == ReportTargetType.POST:
            post = await db.get(Post, original.target_id)
            if post is not None and post.deleted_at is None:
                post_recipients = await post_service.post_recipient_ids(db, post)
    report = await report_service.review_report(db, report_id, admin.id, payload)
    if report.case_id is not None:
        closed_case = await db.get(ReportCase, report.case_id)
        if closed_case is not None and closed_case.status != "open":
            await jury_service.settle_after_admin(db, closed_case)
            await db.commit()
    await admin_manager.broadcast("admin.report.changed", {"report_id": report.id, "action": "reviewed"})
    if "recall_message" in (report.action or "") and report.message_id is not None:
        message = await message_service.get_message(db, report.message_id)
        await _broadcast_message_recalled(db, message)
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
    human: Annotated[str | None, Query(pattern="^(pending|done)$")] = None,
) -> list[ContentModerationRecordRead]:
    items = await moderation_service.list_moderation_records(db, decision, action, source, content_type, include_allow, limit, offset, human)
    return [ContentModerationRecordRead.model_validate(item) for item in items]


@router.post("/moderation-records/{record_id}/verdict", response_model=ContentModerationRecordRead)
async def admin_moderation_record_verdict(
    record_id: int,
    payload: ModerationRecordVerdict,
    admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ContentModerationRecordRead:
    record = await moderation_service.set_human_verdict(
        db, record_id, actor_id=admin.id, verdict=payload.verdict, category=payload.category, level=payload.level
    )
    return ContentModerationRecordRead.model_validate(record)


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


# ---------------------------------------------------------------- 违规台账


def _penalty_from_input(value: PenaltyInput) -> penalty_service.Penalty:
    if value.kind in {"none", "notice", "warning"}:
        return penalty_service.Penalty(value.kind, 0)
    return penalty_service.Penalty(value.kind, None if value.permanent else value.hours)


async def _admin_violation_read(db: AsyncSession, violation: UserViolation) -> AdminViolationRead:
    return AdminViolationRead(**(await penalty_service.build_admin_reads(db, [violation]))[0])


@router.get("/moderation/rules", response_model=ModerationRules)
async def admin_moderation_rules(_admin: Annotated[User, Depends(get_current_admin)]) -> ModerationRules:
    # 后台不带客户端证明头，过不了 /api/v1/moderation/rules 前面的 request_guard，这里给一个等价入口。
    return await moderation_rules()


@router.get("/violations", response_model=AdminViolationPage)
async def admin_list_violations(
    _admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
    user_id: int | None = Query(default=None, gt=0),
    status_filter: Literal["active", "revoked"] | None = Query(default=None, alias="status"),
    appeal_status: Literal["none", "pending", "accepted", "rejected"] | None = None,
    review_status: Literal["none", "pending", "confirmed", "adjusted"] | None = None,
    source: Literal["auto", "auto_rule", "auto_ai", "auto_image", "report", "jury", "admin"] | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> AdminViolationPage:
    items, total = await penalty_service.list_violations(
        db,
        user_id=user_id,
        status_filter=status_filter,
        appeal_status=appeal_status,
        review_status=review_status,
        source=source,
        limit=limit,
        offset=offset,
    )
    return AdminViolationPage(
        items=[AdminViolationRead(**item) for item in await penalty_service.build_admin_reads(db, items)],
        total=total,
    )


@router.post("/violations", response_model=AdminViolationRead, status_code=201)
async def admin_create_violation(
    payload: AdminViolationCreate,
    admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AdminViolationRead:
    if payload.category not in penalty_service.CATEGORIES:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Unknown violation category")
    target = await db.get(User, payload.user_id)
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    if target.id == admin.id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot penalize yourself")
    outcome = await penalty_service.record_violation(
        db,
        user_id=target.id,
        category=payload.category,
        level=payload.level,
        source="admin",
        actor_id=admin.id,
        summary=payload.summary,
        reason=payload.reason,
        override=_penalty_from_input(payload.penalty) if payload.penalty is not None else None,
    )
    assert outcome is not None
    await db.commit()
    await penalty_service.flush_notifications(db)
    return await _admin_violation_read(db, outcome.violation)


@router.post("/violations/{violation_id}/revoke", response_model=AdminViolationRead)
async def admin_revoke_violation(
    violation_id: int,
    payload: AdminViolationRevoke,
    admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AdminViolationRead:
    violation = await penalty_service.revoke_violation(db, violation_id, actor_id=admin.id, reason=payload.reason)
    await db.commit()
    await penalty_service.notify_violation_updated(db, violation, "revoked")
    return await _admin_violation_read(db, violation)


@router.post("/violations/{violation_id}/review", response_model=AdminViolationRead)
async def admin_review_violation(
    violation_id: int,
    payload: AdminViolationReview,
    admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AdminViolationRead:
    penalty = _penalty_from_input(payload.penalty) if payload.action == "adjust" and payload.penalty is not None else None
    violation = await penalty_service.adjust_penalty(db, violation_id, actor_id=admin.id, penalty=penalty, note=payload.note)
    await db.commit()
    await penalty_service.notify_violation_updated(db, violation, "reviewed")
    if violation.penalty_kind == "ban":
        user = await db.get(User, violation.user_id)
        if user is not None and user.status.value == "disabled":
            await manager.disconnect_user(user.id, "account.suspended", _account_suspension_data(user))
    return await _admin_violation_read(db, violation)


@router.post("/violations/{violation_id}/appeal", response_model=AdminViolationRead)
async def admin_review_appeal(
    violation_id: int,
    payload: AdminAppealReview,
    admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AdminViolationRead:
    violation = await penalty_service.review_appeal(db, violation_id, reviewer_id=admin.id, accept=payload.accept, note=payload.note)
    await penalty_service.notify_violation_updated(db, violation, "appeal_accepted" if payload.accept else "appeal_rejected")
    if not payload.accept and violation.status == "active" and violation.penalty_kind == "ban":
        # 驳回的是资深复审「减轻」过的申诉时，原来的封禁会恢复：把人踢下线。平时这里什么都不会发生（本来就封着）。
        user = await db.get(User, violation.user_id)
        if user is not None and user.status.value == "disabled":
            await manager.disconnect_user(user.id, "account.suspended", _account_suspension_data(user))
    return await _admin_violation_read(db, violation)


@router.post("/users/{user_id}/unmute", response_model=AdminUserRead)
async def admin_unmute_user(
    user_id: int,
    admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AdminUserRead:
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    await penalty_service.lift_mute(db, user)
    db.add(AdminAuditLog(actor_id=admin.id, action="user.unmute", target_type="user", target_id=user.id))
    await db.commit()
    await manager.send_to_user(
        user.id,
        "moderation.violation.updated",
        {"action": "unmuted", "violation": None, "standing": await penalty_service.user_standing(db, user)},
    )
    await admin_manager.broadcast("admin.user.changed", {"user_id": user.id, "action": "unmuted"})
    return await report_service.get_admin_user(db, user.id)


# ---------------------------------------------------------------- 举报案件


async def _case_detail(db: AsyncSession, case: ReportCase, admin_id: int, *, content_restored: bool | None = None) -> CaseDetail:
    base = (await case_service.build_case_reads(db, [case]))[0]
    reports = await case_service.case_reports(db, case.id)
    full_reports = [await report_service.get_report(db, report.id) for report in reports]
    votes = (
        await db.execute(
            select(JuryVote, User, JuryMember.tier)
            .join(User, User.id == JuryVote.juror_id)
            .outerjoin(JuryMember, JuryMember.user_id == JuryVote.juror_id)
            .where(JuryVote.case_id == case.id, JuryVote.voted_at.is_not(None))
            .order_by(JuryVote.voted_at.asc(), JuryVote.id.asc())
        )
    ).all()
    return CaseDetail(
        **base,
        snapshot=await case_service.case_snapshot(db, case) or None,
        reports=await report_service.build_report_reads(db, full_reports, admin_id),
        votes=[
            JuryVoteSummary(
                review_id=vote.id,
                vote=vote.vote,
                note=vote.note,
                voted_at=penalty_service.aware(vote.voted_at),
                juror=juror,
                alias=vote.alias,
                tier=tier,
                category=vote.category,
                stage=vote.stage or "jury",
            )
            for vote, juror, tier in votes
        ],
        opinions=[AdminJuryOpinionRead(**item) for item in await opinion_service.case_opinions(db, case.id)],
        content_restored=content_restored,
    )


@router.get("/cases", response_model=CasePage)
async def admin_list_cases(
    _admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
    status_filter: Literal["open", "resolved", "dismissed", "archived"] | None = Query(default="open", alias="status"),
    queue: Literal["triage", "jury", "panel", "admin"] | None = None,
    q: str | None = Query(default=None, max_length=80),
    # 只对未结案件生效：newest 新的在前 / oldest 旧的在前 / priority 按排队优先级
    sort: Literal["newest", "oldest", "priority"] = "newest",
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> CasePage:
    await case_service.escalate_stale_jury_cases(db)
    items, total, counts = await case_service.list_cases(
        db, status_filter=status_filter, queue=queue, query=q, sort=sort, limit=limit, offset=offset
    )
    return CasePage(items=[CaseRead(**item) for item in await case_service.build_case_reads(db, items)], total=total, open_counts=counts)


@router.post("/cases/archive", response_model=CaseArchiveResult)
async def admin_archive_cases(
    payload: CaseArchive,
    admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CaseArchiveResult:
    """一键归档历史遗留的未结案件：不处罚任何人、不影响举报人信誉、不通知举报人，只是不再出现在待处理列表里。"""
    result = await case_service.archive_cases(db, before=payload.before, queue=payload.queue, dry_run=payload.dry_run, actor_id=admin.id)
    if not result["dry_run"] and result["count"]:
        await admin_manager.broadcast("admin.report.changed", {"action": "archived", "count": result["count"]})
    return CaseArchiveResult(**result)


@router.post("/cases/void", response_model=CaseVoidResult)
async def admin_void_cases(
    payload: CaseVoidBulk,
    admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CaseVoidResult:
    """批量作废未结的机器案件（例如空闲复查建的全部：machine_source = idle_scan）。不处罚、委员的票不结算，见 cases.void_machine_cases。"""
    if payload.machine_source is None and payload.case_ids is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "void_scope_required", "message": "要给出 machine_source 或 case_ids"},
        )
    result = await case_service.void_machine_cases(
        db, case_ids=payload.case_ids, machine_source=payload.machine_source, note=payload.note, dry_run=payload.dry_run, actor_id=admin.id
    )
    if not result["dry_run"] and result["count"]:
        await admin_manager.broadcast("admin.report.changed", {"action": "voided", "count": result["count"]})
    return CaseVoidResult(**result)


@router.get("/cases/{case_id}", response_model=CaseDetail)
async def admin_case_detail(
    case_id: int,
    admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CaseDetail:
    case = await db.get(ReportCase, case_id)
    if case is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Case not found")
    detail = await _case_detail(db, case, admin.id)
    await report_service.log_sensitive_read(db, admin.id, "case.snapshot.read", "report_case", case.id, {"target": case.target_key})
    return detail


@router.post("/cases/{case_id}/resolve", response_model=CaseDetail)
async def admin_resolve_case(
    case_id: int,
    payload: CaseResolve,
    admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CaseDetail:
    case = await case_service.resolve_case(
        db,
        case_id,
        source="admin",
        actor_id=admin.id,
        upheld=payload.outcome == "violation",
        category=payload.category,
        level=payload.level,
        delete_content=payload.delete_content,
        override=_penalty_from_input(payload.penalty) if payload.penalty is not None else None,
        note=payload.note,
        malicious_reporter_ids=set(payload.malicious_reporter_ids),
    )
    # 管理员的结论是委员一致率的基准。案件同时进题库，但只是「未核对」的练习题：
    # 随手结一个案不等于核对过题目，要用于考试和质控得在题库里单独核对。
    await jury_service.settle_after_admin(db, case)
    await bank_service.include_after_admin(db, case)
    await db.commit()
    await points_service.flush_events(db)
    await admin_manager.broadcast("admin.report.changed", {"case_id": case.id, "action": "reviewed"})
    return await _case_detail(db, case, admin.id)


@router.post("/cases/{case_id}/overturn", response_model=CaseDetail)
async def admin_overturn_case(
    case_id: int,
    payload: CaseOverturn,
    admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CaseDetail:
    result = await case_service.overturn_case(
        db,
        case_id,
        actor_id=admin.id,
        upheld=payload.outcome == "violation",
        category=payload.category,
        level=payload.level,
        note=payload.note,
    )
    await admin_manager.broadcast("admin.report.changed", {"case_id": case_id, "action": "overturned"})
    return await _case_detail(db, result["case"], admin.id, content_restored=result["content_restored"])


@router.post("/cases/{case_id}/void", response_model=CaseDetail)
async def admin_void_case(
    case_id: int,
    payload: CaseVoid,
    admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CaseDetail:
    """作废一件机器案件：机器误报，没有结论、不处罚，从委员队列里拿掉，委员已经投的票不结算。"""
    case = await case_service.void_case(db, case_id, note=payload.note, actor_id=admin.id)
    await admin_manager.broadcast("admin.report.changed", {"case_id": case.id, "action": "voided"})
    return await _case_detail(db, case, admin.id)


@router.post("/cases/{case_id}/queue", response_model=CaseRead)
async def admin_move_case(
    case_id: int,
    payload: CaseMove,
    admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CaseRead:
    case = await case_service.move_case(db, case_id, payload.queue, actor_id=admin.id)
    return CaseRead(**(await case_service.build_case_reads(db, [case]))[0])


# ---------------------------------------------------------------- 风纪委员


@router.get("/jury/members", response_model=JuryMemberPage)
async def admin_list_jury_members(
    _admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
    # expired：任期届满没能续任的；这些人不算在 active 里
    status_filter: Literal["applied", "active", "expired", "rejected", "suspended", "retired"] | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> JuryMemberPage:
    items, total = await jury_service.list_members(db, status_filter, limit, offset)
    return JuryMemberPage(items=[JuryMemberRead(**item) for item in items], total=total)


@router.post("/jury/members/{user_id}/review", response_model=JuryMemberRead)
async def admin_review_jury_member(
    user_id: int,
    payload: JuryMemberReview,
    admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> JuryMemberRead:
    await jury_service.review_member(
        db, user_id, action=payload.action, actor_id=admin.id, note=payload.note, tier=payload.tier, points=payload.points
    )
    items, _total = await jury_service.list_members(db, None, 1, 0, user_id=user_id)
    if not items:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Jury member not found")
    return JuryMemberRead(**items[0])


async def _jury_settings_read(db: AsyncSession) -> JurySettingsRead:
    return JurySettingsRead(switches=await jury_settings.get_switches(db, fresh=True), thresholds=jury_settings.thresholds())


@router.get("/jury/settings", response_model=JurySettingsRead)
async def admin_jury_settings(
    _admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> JurySettingsRead:
    return await _jury_settings_read(db)


@router.put("/jury/settings", response_model=JurySettingsRead)
async def admin_update_jury_settings(
    payload: JurySettingsUpdate,
    admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> JurySettingsRead:
    await jury_settings.update_switches(db, admin.id, payload.model_dump(exclude_none=True))
    await admin_manager.broadcast("admin.jury.settings.changed", {})
    return await _jury_settings_read(db)


@router.get("/jury/reviews", response_model=AdminReviewPage)
async def admin_list_jury_reviews(
    _admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
    # 留空不筛选
    kind: Literal["appeal", "reconsider", ""] | None = None,
    status_filter: Literal["open", "closed", ""] | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> AdminReviewPage:
    """资深复审（申诉复审、举报人复议）的监控：每件带复审委员的票（实名）和结果。"""
    items, total = await review_service.admin_list(db, kind=kind or None, status_filter=status_filter or None, limit=limit, offset=offset)
    return AdminReviewPage(items=[AdminReviewRead(**item) for item in items], total=total)


@router.post("/jury/reviews/{review_id}/decide", response_model=AdminReviewRead)
async def admin_decide_jury_review(
    review_id: int,
    payload: AdminReviewDecide,
    admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AdminReviewRead:
    """给一件举报人复议出结果：维持原结论，或改判为违规。申诉复审返回 409，请用 POST /admin/violations/{id}/appeal。"""
    item = await review_service.admin_decide(
        db, admin, review_id, payload.action, level=payload.level, category=payload.category, note=payload.note
    )
    if payload.action == "overturn":
        await admin_manager.broadcast("admin.report.changed", {"case_id": item["case_id"], "action": "overturned"})
    return AdminReviewRead(**item)


@router.get("/jury/questions", response_model=AdminQuestionPage)
async def admin_list_jury_questions(
    _admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
    # 留空不筛选
    verified: Literal["true", "false", ""] | None = None,
    source: Literal["admin", "consensus", "legacy", "precedent", ""] | None = None,
    q: str | None = Query(default=None, max_length=80),
    # 题库体检的结论：suspect 疑似答案有误 / ok / skipped / none 没体检过
    audit: Literal["suspect", "ok", "skipped", "none", ""] | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> AdminQuestionPage:
    items, total = await bank_service.list_questions(
        db,
        verified={"true": True, "false": False}.get(verified or ""),
        source=source or None,
        query=q,
        audit=audit or None,
        limit=limit,
        offset=offset,
    )
    return AdminQuestionPage(items=[AdminQuestionRead(**item) for item in items], total=total)


# 题库体检（docs/jury-v2.md 2.1.1）。要写在 /jury/questions/{case_id} 前面，否则 audit 会被当成题号
@router.get("/jury/questions/audit", response_model=AdminQuestionAuditStatus)
async def admin_jury_question_audit_status(
    _admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AdminQuestionAuditStatus:
    return AdminQuestionAuditStatus(**await bank_audit_service.audit_status(db))


@router.post("/jury/questions/audit", response_model=AdminQuestionAuditStatus)
async def admin_start_jury_question_audit(
    admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AdminQuestionAuditStatus:
    """让二审把题库里的题全看一遍，标出可能答案有误的题。后台慢慢跑，进度用 GET 查；不会改任何答案。"""
    return AdminQuestionAuditStatus(**await bank_audit_service.start_audit(db, admin.id))


@router.get("/jury/questions/{case_id}", response_model=AdminQuestionDetail)
async def admin_jury_question(
    case_id: int,
    _admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AdminQuestionDetail:
    return AdminQuestionDetail(**await bank_service.question_detail(db, case_id))


@router.put("/jury/questions/{case_id}", response_model=AdminQuestionDetail)
async def admin_update_jury_question(
    case_id: int,
    payload: AdminQuestionUpdate,
    admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AdminQuestionDetail:
    # 只处理请求里出现的字段：显式传 null 表示清空，没出现的保持原样。
    changes = payload.model_dump(exclude_unset=True)
    return AdminQuestionDetail(**await bank_service.update_question(db, case_id, actor_id=admin.id, changes=changes))


@router.get("/jury/exams", response_model=AdminExamPage)
async def admin_list_jury_exams(
    _admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
    passed: Literal["true", "false", ""] | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> AdminExamPage:
    items, total = await bank_service.list_exams(db, passed={"true": True, "false": False}.get(passed or ""), limit=limit, offset=offset)
    return AdminExamPage(items=[AdminExamRead(**item) for item in items], total=total)


@router.get("/jury/opinions", response_model=AdminJuryOpinionPage)
async def admin_list_jury_opinions(
    _admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
    state: Literal["visible", "deleted", "hidden", ""] | None = None,
    # true 只看有待处理举报的，false 只看没有的；留空不筛选
    reported: Literal["true", "false", ""] | None = None,
    # 和另外两个筛选一样允许留空，所以不直接声明成 int
    case_id: str | None = Query(default=None, pattern=r"^\d{0,12}$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> AdminJuryOpinionPage:
    items, total = await opinion_service.admin_list(
        db,
        state=state or None,
        reported={"true": True, "false": False}.get(reported or ""),
        case_id=int(case_id) if case_id else None,
        limit=limit,
        offset=offset,
    )
    return AdminJuryOpinionPage(items=[AdminJuryOpinionRead(**item) for item in items], total=total)


@router.post("/jury/opinions/{opinion_id}/moderate", response_model=AdminJuryOpinionRead)
async def admin_moderate_jury_opinion(
    opinion_id: int,
    payload: AdminJuryOpinionModerate,
    admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AdminJuryOpinionRead:
    item = await opinion_service.admin_moderate(db, opinion_id, action=payload.action, actor_id=admin.id, note=payload.note)
    return AdminJuryOpinionRead(**item)
