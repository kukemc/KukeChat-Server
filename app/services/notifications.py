from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.models.conversation import Conversation, ConversationMember, GroupJoinRequest
from app.models.enums import FriendRequestStatus, GroupJoinRequestStatus, MemberRole, MessageType, PostNotificationType, ReportStatus
from app.models.case import JuryNotification
from app.models.report import UserReport
from app.models.friend import FriendRequest
from app.models.bot import Bot, BotReview
from app.models.message import Message
from app.models.penalty import UserViolation
from app.models.post import PostNotification
from app.models.teamup import TeamupComment, TeamupProfile
from app.models.user import User
from app.schemas.notification import UnifiedNotificationListRead, UnifiedNotificationRead
from app.schemas.user import UserPublic
from app.services import cases as case_service
from app.services import penalties as penalty_service

UTC = timezone.utc


STAFF_ROLES = {MemberRole.OWNER, MemberRole.ADMIN}


def _metadata(metadata_json: str | None) -> dict[str, Any]:
    if not metadata_json:
        return {}
    try:
        parsed = json.loads(metadata_json)
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _user_name(user: Any, fallback: str = "用户") -> str:
    if user is None:
        return fallback
    return getattr(user, "nickname", None) or getattr(user, "username", None) or fallback


def _conversation_title(conversation: Conversation | None, fallback: str = "群聊") -> str:
    if conversation is None:
        return fallback
    return conversation.title or fallback


def _post_notification_title(notification_type: PostNotificationType) -> str:
    if notification_type == PostNotificationType.LIKE:
        return "点赞了你的动态"
    if notification_type == PostNotificationType.COMMENT:
        return "评论了你的动态"
    if notification_type == PostNotificationType.REPLY:
        return "回复了你的评论"
    if notification_type == PostNotificationType.REPOST:
        return "转发了你的动态"
    if notification_type == PostNotificationType.MENTION:
        return "在动态中提到了你"
    return "动态互动"


async def _friend_request_notifications(db: AsyncSession, user_id: int) -> list[UnifiedNotificationRead]:
    result = await db.execute(
        select(FriendRequest)
        .options(joinedload(FriendRequest.requester), joinedload(FriendRequest.receiver))
        .where(
            or_(
                # 自己忽略掉的申请不再出现；申请方那边照旧（ignored_at 只对接收方生效）
                and_(FriendRequest.receiver_id == user_id, FriendRequest.ignored_at.is_(None)),
                FriendRequest.requester_id == user_id,
            )
        )
        .order_by(FriendRequest.updated_at.desc(), FriendRequest.id.desc())
        .limit(80)
    )
    items: list[UnifiedNotificationRead] = []
    for request in result.scalars().all():
        incoming = request.receiver_id == user_id
        actor = request.requester if incoming else request.receiver
        pending = request.status == FriendRequestStatus.PENDING and incoming
        status_label = "等待处理" if request.status == FriendRequestStatus.PENDING else ("已通过" if request.status == FriendRequestStatus.ACCEPTED else "已拒绝")
        title = f"{_user_name(actor, f'用户 {request.requester_id if incoming else request.receiver_id}')} 请求添加你为好友" if incoming else f"你向 {_user_name(actor, f'用户 {request.receiver_id}')} 发出了好友申请"
        items.append(
            UnifiedNotificationRead(
                id=f"friend:{request.id}",
                category="friend",
                type="friend.request.incoming" if incoming else "friend.request.outgoing",
                title=title,
                body=status_label,
                time=request.updated_at,
                unread=pending,
                pending=pending,
                actor=UserPublic.model_validate(actor) if actor is not None else None,
                friend_request_id=request.id,
                action="friend_request" if pending else "none",
                # 收到的申请处理完了 = 是我自己处理的，不算新通知
                payload={"status": request.status.value, "incoming": incoming, "self_handled": incoming and not pending},
            )
        )
    return items


async def _group_join_notifications(db: AsyncSession, user_id: int) -> list[UnifiedNotificationRead]:
    result = await db.execute(
        select(GroupJoinRequest)
        .options(joinedload(GroupJoinRequest.requester), joinedload(GroupJoinRequest.reviewer), joinedload(GroupJoinRequest.conversation))
        .outerjoin(ConversationMember, and_(ConversationMember.conversation_id == GroupJoinRequest.conversation_id, ConversationMember.user_id == user_id))
        .where(or_(GroupJoinRequest.requester_id == user_id, ConversationMember.role.in_(STAFF_ROLES)))
        .order_by(GroupJoinRequest.updated_at.desc(), GroupJoinRequest.id.desc())
        .limit(100)
    )
    items: list[UnifiedNotificationRead] = []
    for request in result.scalars().unique().all():
        incoming = request.requester_id != user_id
        conversation = request.conversation
        pending = request.status == GroupJoinRequestStatus.PENDING and incoming
        status_label = "等待审核" if request.status == GroupJoinRequestStatus.PENDING else ("已通过" if request.status == GroupJoinRequestStatus.ACCEPTED else "已拒绝")
        title = f"{_user_name(request.requester, f'用户 {request.requester_id}')} 申请加入 {_conversation_title(conversation)}" if incoming else f"你申请加入 {_conversation_title(conversation)}"
        body_parts = [status_label]
        if request.message:
            body_parts.append(f"附言：{request.message}")
        if request.answer:
            body_parts.append(f"回答：{request.answer}")
        items.append(
            UnifiedNotificationRead(
                id=f"group_request:{request.id}",
                category="group",
                type="group.join_request.incoming" if incoming else "group.join_request.outgoing",
                title=title,
                body=" · ".join(body_parts),
                time=request.updated_at,
                unread=pending,
                pending=pending,
                actor=UserPublic.model_validate(request.requester) if request.requester is not None else None,
                conversation_id=request.conversation_id,
                conversation_title=_conversation_title(conversation),
                conversation_avatar_url=conversation.avatar_url if conversation is not None else None,
                group_join_request_id=request.id,
                action="group_join_request" if pending else "open_conversation",
                payload={
                    "status": request.status.value,
                    "incoming": incoming,
                    "self_handled": incoming and not pending and request.reviewer_id == user_id,
                },
            )
        )
    return items


async def _post_notifications(db: AsyncSession, user_id: int) -> list[UnifiedNotificationRead]:
    result = await db.execute(
        select(PostNotification)
        .options(joinedload(PostNotification.actor), joinedload(PostNotification.post), joinedload(PostNotification.comment))
        .where(PostNotification.recipient_id == user_id)
        .order_by(PostNotification.created_at.desc(), PostNotification.id.desc())
        .limit(100)
    )
    items: list[UnifiedNotificationRead] = []
    for notification in result.scalars().all():
        actor_name = _user_name(notification.actor, f"用户 {notification.actor_id}")
        body = None
        if notification.comment is not None:
            body = notification.comment.content[:120]
        elif notification.post is not None:
            body = notification.post.content[:120]
        items.append(
            UnifiedNotificationRead(
                id=f"post:{notification.id}",
                category="post",
                type=f"post.{notification.type.value}",
                title=f"{actor_name}{_post_notification_title(notification.type)}",
                body=body,
                time=notification.created_at,
                unread=notification.read_at is None,
                pending=False,
                actor=UserPublic.model_validate(notification.actor) if notification.actor is not None else None,
                post_id=notification.post_id,
                action="open_post",
                payload={"post_notification_id": notification.id, "comment_id": notification.comment_id},
            )
        )
    return items


async def _interaction_notifications(db: AsyncSession, user_id: int) -> list[UnifiedNotificationRead]:
    """Comments on the user's teamup card and reviews on the user's bots."""
    items: list[UnifiedNotificationRead] = []

    teamup_result = await db.execute(
        select(TeamupComment, TeamupProfile)
        .join(TeamupProfile, TeamupProfile.id == TeamupComment.profile_id)
        .options(joinedload(TeamupComment.author))
        .where(
            TeamupProfile.user_id == user_id,
            TeamupComment.author_id != user_id,
            TeamupComment.deleted_at.is_(None),
            TeamupComment.moderation_status != "rejected",
        )
        .order_by(TeamupComment.created_at.desc(), TeamupComment.id.desc())
        .limit(40)
    )
    for comment, profile in teamup_result.all():
        actor_name = _user_name(comment.author, f"用户 {comment.author_id}")
        items.append(
            UnifiedNotificationRead(
                id=f"teamup_comment:{comment.id}",
                category="interact",
                type="teamup.comment",
                title=f"{actor_name} 评论了你的组队名片",
                body=(comment.content or "")[:120],
                time=comment.created_at,
                unread=False,
                pending=False,
                actor=UserPublic.model_validate(comment.author) if comment.author is not None else None,
                action="open_teamup",
                payload={"profile_id": profile.id, "comment_id": comment.id},
            )
        )

    bot_result = await db.execute(
        select(BotReview, Bot)
        .join(Bot, Bot.id == BotReview.bot_id)
        .options(joinedload(BotReview.user))
        .where(
            Bot.owner_id == user_id,
            BotReview.user_id != user_id,
            BotReview.deleted_at.is_(None),
            BotReview.moderation_status != "rejected",
        )
        .order_by(BotReview.created_at.desc(), BotReview.id.desc())
        .limit(40)
    )
    for review, bot in bot_result.all():
        actor_name = _user_name(review.user, f"用户 {review.user_id}")
        items.append(
            UnifiedNotificationRead(
                id=f"bot_review:{review.id}",
                category="interact",
                type="bot.review",
                title=f"{actor_name} 评论了你的机器人「{bot.name}」",
                body=(review.content or "").strip()[:120] or "给出了评分",
                time=review.created_at,
                unread=False,
                pending=False,
                actor=UserPublic.model_validate(review.user) if review.user is not None else None,
                action="open_bot",
                payload={"bot_id": bot.id, "review_id": review.id},
            )
        )
    return items


async def _managed_group_system_notifications(db: AsyncSession, user_id: int) -> list[UnifiedNotificationRead]:
    result = await db.execute(
        select(Message, Conversation)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .join(ConversationMember, ConversationMember.conversation_id == Conversation.id)
        .where(
            ConversationMember.user_id == user_id,
            ConversationMember.role.in_(STAFF_ROLES),
            Message.type == MessageType.SYSTEM,
            Message.metadata_json.like('%"group.member.left"%'),
        )
        .order_by(Message.created_at.desc(), Message.id.desc())
        .limit(80)
    )
    items: list[UnifiedNotificationRead] = []
    for message, conversation in result.all():
        metadata = _metadata(message.metadata_json)
        items.append(
            UnifiedNotificationRead(
                id=f"system_message:{message.id}",
                category="system",
                type=str(metadata.get("system_event") or "group.member.left"),
                title=f"{_conversation_title(conversation)} 有成员退群",
                body=message.content,
                time=message.created_at,
                unread=False,
                pending=False,
                conversation_id=message.conversation_id,
                conversation_title=_conversation_title(conversation),
                conversation_avatar_url=conversation.avatar_url,
                action="open_conversation",
                payload={"message_id": message.id, "user_id": metadata.get("user_id")},
            )
        )
    return items


async def _moderation_notifications(db: AsyncSession, user_id: int) -> list[UnifiedNotificationRead]:
    """处罚通知。走 system 类别的通用样式，旧客户端不用升级也能看到说明。"""
    cutoff = datetime.now(UTC) - timedelta(days=30)
    result = await db.execute(
        select(UserViolation)
        .where(
            UserViolation.user_id == user_id,
            UserViolation.penalty_kind != "none",
            UserViolation.created_at >= cutoff,
        )
        .order_by(UserViolation.id.desc())
        .limit(30)
    )
    items: list[UnifiedNotificationRead] = []
    for violation in result.scalars().all():
        title, body = penalty_service.describe_violation(violation)
        items.append(
            UnifiedNotificationRead(
                id=f"violation:{violation.id}",
                category="system",
                type="moderation_penalty",
                title=title,
                body=body,
                time=violation.created_at,
                unread=False,
                pending=False,
                conversation_id=violation.conversation_id if violation.target_type == "message" else None,
                action="none",
                payload={"violation": penalty_service.violation_payload(violation)},
            )
        )
    return items


async def _report_result_notifications(db: AsyncSession, user_id: int) -> list[UnifiedNotificationRead]:
    """举报处理结果。举报人此前收不到任何反馈。"""
    cutoff = datetime.now(UTC) - timedelta(days=30)
    result = await db.execute(
        select(UserReport)
        .where(
            UserReport.reporter_id == user_id,
            UserReport.status != ReportStatus.PENDING,
            UserReport.reviewed_at.is_not(None),
            UserReport.reviewed_at >= cutoff,
        )
        .order_by(UserReport.reviewed_at.desc())
        .limit(30)
    )
    items: list[UnifiedNotificationRead] = []
    for report in result.scalars().all():
        title, body = case_service.report_result_text(report)
        items.append(
            UnifiedNotificationRead(
                id=f"report_result:{report.id}",
                category="system",
                type="report_result",
                title=title,
                body=body,
                time=report.reviewed_at,
                unread=False,
                pending=False,
                action="none",
                payload={"report_id": report.id, "case_id": report.case_id, "status": report.status.value},
            )
        )
    return items


async def _jury_notifications(db: AsyncSession, user_id: int) -> list[UnifiedNotificationRead]:
    """风纪委员的资格 / 层级变化：晋升、降级、被系统暂停、任期届满未能续任。"""
    cutoff = datetime.now(UTC) - timedelta(days=30)
    result = await db.execute(
        select(JuryNotification)
        .where(JuryNotification.user_id == user_id, JuryNotification.created_at >= cutoff)
        .order_by(JuryNotification.id.desc())
        .limit(30)
    )
    return [
        UnifiedNotificationRead(
            id=f"jury:{notification.id}",
            category="system",
            type="jury",
            title=notification.title,
            body=notification.body,
            time=notification.created_at,
            unread=False,
            pending=False,
            action="none",
            payload={"kind": notification.kind},
        )
        for notification in result.scalars().all()
    ]


def _as_utc(value: datetime | None) -> datetime | None:
    # SQLite 读回来的时间不带时区；库里存的都是 UTC
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


async def get_seen_at(db: AsyncSession, user_id: int) -> datetime | None:
    result = await db.execute(select(User.notifications_seen_at).where(User.id == user_id))
    return _as_utc(result.scalar_one_or_none())


async def mark_seen(db: AsyncSession, user_id: int) -> datetime:
    """通知中心「看过了」：之后来的通知才算新的。"""
    now = datetime.now(UTC)
    await db.execute(update(User).where(User.id == user_id).values(notifications_seen_at=now).execution_options(synchronize_session=False))
    await db.commit()
    return now


async def list_unified_notifications(db: AsyncSession, user_id: int, limit: int = 100) -> UnifiedNotificationListRead:
    groups = await _group_join_notifications(db, user_id)
    friends = await _friend_request_notifications(db, user_id)
    posts = await _post_notifications(db, user_id)
    interactions = await _interaction_notifications(db, user_id)
    systems = await _managed_group_system_notifications(db, user_id)
    penalties = await _moderation_notifications(db, user_id)
    report_results = await _report_result_notifications(db, user_id)
    jury = await _jury_notifications(db, user_id)
    items = [*friends, *groups, *posts, *interactions, *systems, *penalties, *report_results, *jury]
    # 账号等级的升级通知（docs/account-level.md 第 4 节）
    from app.services import account_levels as account_level_service

    items.extend(await account_level_service.notification_items(db, user_id))
    items.sort(key=lambda item: item.time, reverse=True)
    items = items[:limit]
    # 只提醒一次：动态类通知仍按各自的已读标记；其余类别一律按「上次打开通知中心」之后来的才算新（pending 和这个无关）
    seen_at = await get_seen_at(db, user_id)
    for item in items:
        if item.category != "post":
            # 自己刚处理掉的申请（updated_at 因此变新）不该再给自己算一条新通知
            item.unread = not item.payload.get("self_handled") and (seen_at is None or _as_utc(item.time) > seen_at)
    new_count = sum(1 for item in items if item.unread)
    counts = {"all": len(items), "friend": 0, "group": 0, "post": 0, "interact": 0, "system": 0}
    for item in items:
        counts[item.category] = counts.get(item.category, 0) + 1
    return UnifiedNotificationListRead(
        items=items,
        unread_count=new_count,
        new_count=new_count,
        seen_at=seen_at,
        pending_count=sum(1 for item in items if item.pending),
        counts=counts,
    )
