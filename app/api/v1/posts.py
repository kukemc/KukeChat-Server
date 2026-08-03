from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.models.enums import ModerationStatus
from app.models.user import User
from app.schemas.common import MessageResponse
from app.schemas.post import PostCommentCreate, PostCommentRead, PostCreate, PostLikeRead, PostLikeToggleRead, PostNotificationListRead, PostRead, PostStatsRead, PostTopicRead, PostUpdate
from app.services import moderation as moderation_service
from app.services import posts as post_service
from app.websocket.manager import manager

router = APIRouter()


async def send_post_signal(db: AsyncSession, event_type: str, post_id: int, actor_id: int) -> None:
    post = await post_service.get_post(db, post_id)
    recipients = await post_service.post_recipient_ids(db, post)
    await manager.send_to_users(recipients, event_type, {"post_id": post_id, "actor_id": actor_id})


async def send_notification_signals(db: AsyncSession, post_id: int, actor_id: int) -> None:
    recipient_ids = await post_service.notification_recipient_ids(db, post_id, actor_id)
    for recipient_id in recipient_ids:
        unread_count = await post_service.unread_notification_count(db, recipient_id)
        await manager.send_to_user(recipient_id, "post.notification.created", {"post_id": post_id, "actor_id": actor_id, "unread_count": unread_count})


async def send_recent_notification_signals(db: AsyncSession, post_id: int, actor_id: int, since: datetime) -> None:
    recipient_ids = await post_service.recent_notification_recipient_ids(db, post_id, actor_id, since)
    for recipient_id in recipient_ids:
        unread_count = await post_service.unread_notification_count(db, recipient_id)
        await manager.send_to_user(recipient_id, "post.notification.created", {"post_id": post_id, "actor_id": actor_id, "unread_count": unread_count})


@router.get("/feed", response_model=list[PostRead])
async def feed(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    scope: Annotated[Literal["square", "friends", "mine"], Query()] = "friends",
    before_id: Annotated[int | None, Query(gt=0)] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
    topic: Annotated[str | None, Query(max_length=40)] = None,
    sort: Annotated[Literal["latest", "hot"], Query()] = "latest",
) -> list[PostRead]:
    posts = await post_service.list_feed(db, current_user.id, scope, before_id, limit, topic, sort)
    return await post_service.build_post_reads(db, posts, current_user.id)


@router.post("", response_model=PostRead, status_code=status.HTTP_201_CREATED)
async def create_post(
    payload: PostCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PostRead:
    notification_since = datetime.now(timezone.utc)
    post = await post_service.create_post(db, current_user, payload)
    read = await post_service.build_post_read(db, post, current_user.id)
    if post.moderation_status == ModerationStatus.APPROVED.value:
        recipients = await post_service.post_recipient_ids(db, post)
        await manager.send_to_users(recipients, "post.created", {"post_id": post.id, "actor_id": current_user.id})
        await send_recent_notification_signals(db, post.id, current_user.id, notification_since)
    else:
        await manager.send_to_user(current_user.id, "post.created", {"post_id": post.id, "actor_id": current_user.id, "moderation_status": post.moderation_status})
    if not current_user.content_moderation_whitelisted:
        moderation_service.schedule_post_moderation(post.id)
    return read


@router.get("/topics/trending", response_model=list[PostTopicRead])
async def trending_topics(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=5)] = 5,
) -> list[PostTopicRead]:
    del current_user
    return await post_service.list_trending_topics(db, limit)


@router.get("/topics/search", response_model=list[PostTopicRead])
async def search_topics(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    q: Annotated[str, Query(max_length=40)] = "",
    limit: Annotated[int, Query(ge=1, le=10)] = 8,
) -> list[PostTopicRead]:
    del current_user
    return await post_service.search_topics(db, q, limit)


@router.get("/notifications", response_model=PostNotificationListRead)
async def notifications(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    before_id: Annotated[int | None, Query(gt=0)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
) -> PostNotificationListRead:
    return await post_service.list_notifications(db, current_user.id, before_id, limit)


@router.post("/notifications/read", response_model=PostNotificationListRead)
async def mark_all_notifications_read(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PostNotificationListRead:
    await post_service.mark_notifications_read(db, current_user.id)
    result = await post_service.list_notifications(db, current_user.id, None, 30)
    await manager.send_to_user(current_user.id, "post.notification.updated", {"unread_count": result.unread_count})
    return result


@router.post("/notifications/{notification_id}/read", response_model=PostNotificationListRead)
async def mark_notification_read(
    notification_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PostNotificationListRead:
    await post_service.mark_notifications_read(db, current_user.id, notification_id)
    result = await post_service.list_notifications(db, current_user.id, None, 30)
    await manager.send_to_user(current_user.id, "post.notification.updated", {"unread_count": result.unread_count})
    return result


@router.get("/users/{user_id}", response_model=list[PostRead])
async def user_posts(
    user_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    before_id: Annotated[int | None, Query(gt=0)] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
    sort: Annotated[Literal["latest", "hot"], Query()] = "latest",
) -> list[PostRead]:
    posts = await post_service.list_user_posts(db, user_id, current_user.id, before_id, limit, sort)
    return await post_service.build_post_reads(db, posts, current_user.id)


@router.get("/users/{user_id}/stats", response_model=PostStatsRead)
async def user_post_stats(
    user_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PostStatsRead:
    return await post_service.user_post_stats(db, user_id, current_user.id)


@router.get("/{post_id}", response_model=PostRead)
async def get_post(
    post_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PostRead:
    post = await post_service.get_visible_post(db, post_id, current_user.id)
    return await post_service.build_post_read(db, post, current_user.id)


@router.delete("/{post_id}", response_model=MessageResponse)
async def delete_post(
    post_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> MessageResponse:
    post = await post_service.get_visible_post(db, post_id, current_user.id)
    recipients = await post_service.post_recipient_ids(db, post)
    await post_service.delete_post(db, post_id, current_user.id)
    await manager.send_to_users(recipients, "post.deleted", {"post_id": post_id, "actor_id": current_user.id})
    return MessageResponse(detail="Post deleted")


@router.patch("/{post_id}", response_model=PostRead)
async def update_post(
    post_id: int,
    payload: PostUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PostRead:
    notification_since = datetime.now(timezone.utc)
    post = await post_service.update_post(db, post_id, current_user, payload)
    read = await post_service.build_post_read(db, post, current_user.id)
    if post.moderation_status == ModerationStatus.APPROVED.value:
        await send_post_signal(db, "post.updated", post.id, current_user.id)
        await send_recent_notification_signals(db, post.id, current_user.id, notification_since)
    else:
        await manager.send_to_user(current_user.id, "post.updated", {"post_id": post.id, "actor_id": current_user.id, "moderation_status": post.moderation_status})
        moderation_service.schedule_post_moderation(post.id)
    return read


@router.post("/{post_id}/pin", response_model=PostRead)
async def pin_post(
    post_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PostRead:
    post = await post_service.set_post_pinned(db, post_id, current_user.id, True)
    await send_post_signal(db, "post.updated", post.id, current_user.id)
    return await post_service.build_post_read(db, post, current_user.id)


@router.delete("/{post_id}/pin", response_model=PostRead)
async def unpin_post(
    post_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PostRead:
    post = await post_service.set_post_pinned(db, post_id, current_user.id, False)
    await send_post_signal(db, "post.updated", post.id, current_user.id)
    return await post_service.build_post_read(db, post, current_user.id)


@router.post("/{post_id}/like", response_model=PostLikeToggleRead)
async def toggle_like(
    post_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PostLikeToggleRead:
    notification_since = datetime.now(timezone.utc)
    result = await post_service.toggle_like(db, post_id, current_user.id)
    await send_post_signal(db, "post.like.updated", post_id, current_user.id)
    if result.liked:
        await send_recent_notification_signals(db, post_id, current_user.id, notification_since)
    return result


@router.get("/{post_id}/likes", response_model=list[PostLikeRead])
async def likes(
    post_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> list[PostLikeRead]:
    return await post_service.list_likes(db, post_id, current_user.id, limit)


@router.get("/{post_id}/comments", response_model=list[PostCommentRead])
async def comments(
    post_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    before_id: Annotated[int | None, Query(gt=0)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> list[PostCommentRead]:
    return await post_service.list_comments(db, post_id, current_user.id, before_id, limit)


@router.post("/{post_id}/comments", response_model=PostCommentRead, status_code=status.HTTP_201_CREATED)
async def create_comment(
    post_id: int,
    payload: PostCommentCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PostCommentRead:
    notification_since = datetime.now(timezone.utc)
    comment = await post_service.create_comment(db, post_id, current_user, payload.content, payload.parent_id, payload.mention_user_ids)
    if comment.moderation_status == ModerationStatus.APPROVED.value:
        await send_post_signal(db, "post.comment.created", post_id, current_user.id)
        await send_recent_notification_signals(db, post_id, current_user.id, notification_since)
    else:
        await manager.send_to_user(current_user.id, "post.comment.created", {"post_id": post_id, "comment_id": comment.id, "actor_id": current_user.id, "moderation_status": comment.moderation_status})
    if not current_user.content_moderation_whitelisted:
        moderation_service.schedule_post_comment_moderation(comment.id)
    return (await post_service._build_comment_reads(db, [comment], current_user.id))[0]


@router.post("/{post_id}/comments/{comment_id}/like", response_model=PostLikeToggleRead)
async def toggle_comment_like(
    post_id: int,
    comment_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PostLikeToggleRead:
    notification_since = datetime.now(timezone.utc)
    result = await post_service.toggle_comment_like(db, post_id, comment_id, current_user.id)
    await send_post_signal(db, "post.comment.like.updated", post_id, current_user.id)
    if result.liked:
        await send_recent_notification_signals(db, post_id, current_user.id, notification_since)
    return result


@router.delete("/{post_id}/comments/{comment_id}", response_model=MessageResponse)
async def delete_comment(
    post_id: int,
    comment_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> MessageResponse:
    await post_service.delete_comment(db, post_id, comment_id, current_user.id)
    await send_post_signal(db, "post.comment.deleted", post_id, current_user.id)
    return MessageResponse(detail="Comment deleted")
