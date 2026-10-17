from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import PurePosixPath

from fastapi import HTTPException, status
from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.core.config import settings
from app.core.dialect import greatest, hours_since, least
from app.models.enums import ModerationStatus, PostNotificationType, PostVisibility
from app.models.friend import Friendship
from app.models.post import Post, PostComment, PostCommentLike, PostLike, PostMention, PostNotification, PostTopic, PostTopicLink
from app.models.report import UserReport
from app.models.user import User
from app.schemas.post import AdminPostRead, PostCommentRead, PostCreate, PostLikeRead, PostLikeToggleRead, PostNotificationListRead, PostNotificationRead, PostRead, PostReferenceRead, PostStatsRead, PostTopicRead, PostUpdate
from app.schemas.user import UserPublic
from app.services.penalties import ensure_can_post
from app.services.ccw import previews_from_text
from app.services.storage import delete_objects, storage_key_from_url, thumbnail_key_for_key

UTC = timezone.utc
POST_UPLOAD_PREFIX = "posts/"
POST_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
TOPIC_PATTERN = re.compile(r"(?<![\w#])#([\w\u4e00-\u9fff][\w\u4e00-\u9fff-]{0,39})")


def _post_visibility_is(value: PostVisibility):
    return func.lower(Post.visibility) == value.value


def _post_visibility_is_not(value: PostVisibility):
    return func.lower(Post.visibility) != value.value


def _post_is_approved():
    return func.lower(Post.moderation_status) == ModerationStatus.APPROVED.value


def _post_comment_is_approved():
    return func.lower(PostComment.moderation_status) == ModerationStatus.APPROVED.value


def _post_topic_is_approved():
    return func.lower(PostTopic.moderation_status) == ModerationStatus.APPROVED.value


def _status_value(value: object) -> str:
    return str(getattr(value, "value", value)).lower()


def _schedule_post_topic_moderation(topic_id: int) -> None:
    from app.services.moderation import schedule_post_topic_moderation

    schedule_post_topic_moderation(topic_id)


def _image_urls_to_json(image_urls: list[str]) -> str:
    return json.dumps(image_urls, ensure_ascii=False, separators=(",", ":"))


def _image_urls_from_json(value: str | None) -> list[str]:
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(parsed, list):
        return []
    return [item for item in parsed if isinstance(item, str) and item.strip()]


def _ccw_creations_to_json(value: list[dict[str, object]]) -> str | None:
    if not value:
        return None
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _ccw_creations_from_json(value: str | None) -> list[dict[str, object]]:
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(parsed, list):
        return []
    return [item for item in parsed if isinstance(item, dict)]


def _normalize_topic_name(value: str) -> str:
    return value.strip().lstrip("#").strip()[:40]


def _topic_names_from_content(content: str) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for match in TOPIC_PATTERN.finditer(content or ""):
        name = _normalize_topic_name(match.group(1))
        key = name.lower()
        if not name or key in seen:
            continue
        names.append(name)
        seen.add(key)
    return names[:10]


def _merge_topic_names(content: str, explicit_names: list[str] | None) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for item in [*_topic_names_from_content(content), *(explicit_names or [])]:
        name = _normalize_topic_name(item)
        key = name.lower()
        if not name or key in seen:
            continue
        names.append(name)
        seen.add(key)
    return names[:10]


def _post_upload_path(value: str) -> str | None:
    relative_path = storage_key_from_url(value)
    if relative_path is None:
        return None
    path_parts = PurePosixPath(relative_path).parts
    if len(path_parts) != 2 or path_parts[0] != "posts" or path_parts[1] == "thumbs" or ".." in path_parts:
        return None
    if PurePosixPath(path_parts[1]).suffix.lower() not in POST_IMAGE_SUFFIXES:
        return None
    return relative_path


def _post_upload_paths(post: Post) -> set[str]:
    paths: set[str] = set()
    for image_url in _image_urls_from_json(post.image_urls_json):
        path = _post_upload_path(image_url)
        if path is not None:
            paths.add(path)
    return paths


async def _upload_path_still_referenced(db: AsyncSession, relative_path: str) -> bool:
    path_fragment = f"%{relative_path}%"
    result = await db.execute(
        select(Post.id)
        .where(Post.deleted_at.is_(None), Post.image_urls_json.like(path_fragment))
        .limit(1)
    )
    if result.scalar_one_or_none() is not None:
        return True
    report_result = await db.execute(
        select(UserReport.id)
        .where(or_(UserReport.target_snapshot.like(path_fragment), UserReport.attachments.like(path_fragment)))
        .limit(1)
    )
    return report_result.scalar_one_or_none() is not None


def schedule_delete_upload_files(paths: list[str]) -> None:
    if paths:
        asyncio.create_task(asyncio.to_thread(delete_objects, paths))


async def _friend_ids(db: AsyncSession, user_id: int) -> list[int]:
    result = await db.execute(select(Friendship.friend_id).where(Friendship.user_id == user_id))
    return list(result.scalars().all())


async def _existing_user_ids(db: AsyncSession, user_ids: list[int]) -> set[int]:
    if not user_ids:
        return set()
    result = await db.execute(select(User.id).where(User.id.in_(list(dict.fromkeys(user_ids)))))
    return set(result.scalars().all())


async def _sync_post_topics(db: AsyncSession, post_id: int, topic_names: list[str], creator: User) -> list[int]:
    await db.execute(delete(PostTopicLink).where(PostTopicLink.post_id == post_id))
    normalized = [_normalize_topic_name(name) for name in topic_names]
    normalized = list(dict.fromkeys(name for name in normalized if name))[:10]
    if not normalized:
        return []

    topics: list[PostTopic] = []
    created_topic_ids: list[int] = []
    for name in normalized:
        result = await db.execute(select(PostTopic).where(func.lower(PostTopic.name) == name.lower()))
        topic = result.scalar_one_or_none()
        if topic is None:
            topic_status = ModerationStatus.PENDING.value if settings.content_moderation_enabled and not creator.content_moderation_whitelisted else ModerationStatus.APPROVED.value
            topic = PostTopic(name=name, moderation_status=topic_status, created_by_id=creator.id)
            db.add(topic)
            await db.flush()
            if topic_status == ModerationStatus.PENDING.value:
                created_topic_ids.append(topic.id)
        topics.append(topic)
    for topic in topics:
        db.add(PostTopicLink(post_id=post_id, topic_id=topic.id))
    return created_topic_ids


async def _refresh_topic_counts(db: AsyncSession) -> None:
    topic_ids_result = await db.execute(select(PostTopic.id))
    topic_ids = list(topic_ids_result.scalars().all())
    for topic_id in topic_ids:
        count_result = await db.execute(
            select(func.count(PostTopicLink.id))
            .join(Post, Post.id == PostTopicLink.post_id)
            .where(PostTopicLink.topic_id == topic_id, Post.deleted_at.is_(None), _post_is_approved())
        )
        await db.execute(update(PostTopic).where(PostTopic.id == topic_id).values(post_count=int(count_result.scalar_one() or 0)))


async def _replace_mentions(db: AsyncSession, post_id: int, comment_id: int | None, user_ids: list[int]) -> list[int]:
    query = delete(PostMention).where(PostMention.post_id == post_id)
    if comment_id is None:
        query = query.where(PostMention.comment_id.is_(None))
    else:
        query = query.where(PostMention.comment_id == comment_id)
    await db.execute(query)

    existing_user_ids = await _existing_user_ids(db, user_ids)
    mentioned_ids = [user_id for user_id in dict.fromkeys(user_ids) if user_id in existing_user_ids][:20]
    for user_id in mentioned_ids:
        db.add(PostMention(post_id=post_id, comment_id=comment_id, mentioned_user_id=user_id))
    return mentioned_ids


async def _mention_user_ids(db: AsyncSession, post_id: int, comment_id: int | None) -> set[int]:
    query = select(PostMention.mentioned_user_id).where(PostMention.post_id == post_id)
    if comment_id is None:
        query = query.where(PostMention.comment_id.is_(None))
    else:
        query = query.where(PostMention.comment_id == comment_id)
    result = await db.execute(query)
    return set(result.scalars().all())


def _add_notification(notifications: list[PostNotification], *, recipient_id: int, actor_id: int, post_id: int, type: PostNotificationType, comment_id: int | None = None) -> None:
    if recipient_id == actor_id:
        return
    key = (recipient_id, actor_id, post_id, comment_id, type)
    if any((item.recipient_id, item.actor_id, item.post_id, item.comment_id, item.type) == key for item in notifications):
        return
    notifications.append(PostNotification(recipient_id=recipient_id, actor_id=actor_id, post_id=post_id, comment_id=comment_id, type=type))


async def _visible_mentioned_user_ids(db: AsyncSession, actor_id: int, user_ids: list[int]) -> list[int]:
    if not user_ids:
        return []
    existing_ids = await _existing_user_ids(db, user_ids)
    friend_ids = set(await _friend_ids(db, actor_id))
    allowed_ids = {actor_id, *friend_ids}
    return [user_id for user_id in dict.fromkeys(user_ids) if user_id in existing_ids and user_id in allowed_ids][:20]


async def _create_notifications(db: AsyncSession, notifications: list[PostNotification], *, post: Post | None = None, comment: PostComment | None = None) -> list[PostNotification]:
    if not notifications:
        return []
    if post is not None:
        if _status_value(post.moderation_status) != ModerationStatus.APPROVED.value:
            return []
        if post.visibility == PostVisibility.PRIVATE:
            return []
    if comment is not None and _status_value(comment.moderation_status) != ModerationStatus.APPROVED.value:
        return []
    for notification in notifications:
        db.add(notification)
    await db.flush()
    return notifications


async def create_notifications_for_post(db: AsyncSession, post: Post, mentioned_ids: list[int] | None = None, include_repost: bool = True) -> list[PostNotification]:
    notifications: list[PostNotification] = []
    if mentioned_ids is None:
        result = await db.execute(select(PostMention.mentioned_user_id).where(PostMention.post_id == post.id, PostMention.comment_id.is_(None)))
        mentioned_ids = list(result.scalars().all())
    for mentioned_id in mentioned_ids:
        _add_notification(notifications, recipient_id=mentioned_id, actor_id=post.author_id, post_id=post.id, type=PostNotificationType.MENTION)
    if include_repost and post.repost_of_id is not None:
        reposted_post = await get_post(db, post.repost_of_id)
        _add_notification(notifications, recipient_id=reposted_post.author_id, actor_id=post.author_id, post_id=post.id, type=PostNotificationType.REPOST)
    return await _create_notifications(db, notifications, post=post)


async def create_notifications_for_comment(db: AsyncSession, post: Post, comment: PostComment) -> list[PostNotification]:
    notifications: list[PostNotification] = []
    parent_comment = None
    if comment.parent_id is not None:
        parent_result = await db.execute(select(PostComment).where(PostComment.id == comment.parent_id, PostComment.post_id == post.id, PostComment.deleted_at.is_(None)))
        parent_comment = parent_result.scalar_one_or_none()
    notification_type = PostNotificationType.REPLY if parent_comment is not None else PostNotificationType.COMMENT
    _add_notification(notifications, recipient_id=post.author_id, actor_id=comment.author_id, post_id=post.id, comment_id=comment.id, type=notification_type)
    if parent_comment is not None:
        _add_notification(notifications, recipient_id=parent_comment.author_id, actor_id=comment.author_id, post_id=post.id, comment_id=comment.id, type=PostNotificationType.REPLY)
    result = await db.execute(select(PostMention.mentioned_user_id).where(PostMention.post_id == post.id, PostMention.comment_id == comment.id))
    for mentioned_id in result.scalars().all():
        _add_notification(notifications, recipient_id=mentioned_id, actor_id=comment.author_id, post_id=post.id, comment_id=comment.id, type=PostNotificationType.MENTION)
    return await _create_notifications(db, notifications, post=post, comment=comment)


async def can_view_post(db: AsyncSession, post: Post, viewer_id: int) -> bool:
    if post.deleted_at is not None:
        return False
    if post.author_id == viewer_id:
        return True
    if str(post.moderation_status).lower() != ModerationStatus.APPROVED.value:
        return False
    if post.visibility == PostVisibility.PUBLIC or post.visibility == PostVisibility.PUBLIC.value:
        return True
    if post.visibility == PostVisibility.PRIVATE or post.visibility == PostVisibility.PRIVATE.value:
        return False
    friend_result = await db.execute(
        select(Friendship.id).where(Friendship.user_id == post.author_id, Friendship.friend_id == viewer_id).limit(1)
    )
    return friend_result.scalar_one_or_none() is not None


def _visible_post_condition(viewer_id: int, friend_ids: list[int]):
    return or_(
        Post.author_id == viewer_id,
        and_(_post_is_approved(), _post_visibility_is(PostVisibility.PUBLIC)),
        and_(_post_is_approved(), _post_visibility_is(PostVisibility.FRIENDS), Post.author_id.in_(friend_ids or [-1])),
    )


async def create_post(db: AsyncSession, author: User, payload: PostCreate) -> Post:
    ensure_can_post(author)
    if any(_post_upload_path(image_url) is None for image_url in payload.image_urls):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Post images must reference uploaded post images")
    if payload.repost_of_id is not None:
        await get_visible_post(db, payload.repost_of_id, author.id)

    initial_status = ModerationStatus.PENDING.value if settings.content_moderation_enabled and not author.content_moderation_whitelisted else ModerationStatus.APPROVED.value
    # 在写任何东西之前取：要去 CCW 时会先把请求的读事务结束、连接还回去
    ccw_creations = await previews_from_text(payload.content, db=db)
    post = Post(
        author_id=author.id,
        content=payload.content,
        ccw_creations_json=_ccw_creations_to_json(ccw_creations),
        visibility=payload.visibility,
        image_urls_json=_image_urls_to_json(payload.image_urls),
        repost_of_id=payload.repost_of_id,
        moderation_status=initial_status,
    )
    db.add(post)
    await db.flush()
    topic_ids_to_moderate = await _sync_post_topics(db, post.id, _merge_topic_names(payload.content, payload.topic_names), author)
    mentioned_ids = await _visible_mentioned_user_ids(db, author.id, payload.mention_user_ids)
    await _replace_mentions(db, post.id, None, mentioned_ids)
    await db.flush()
    await create_notifications_for_post(db, post)
    await _refresh_topic_counts(db)
    post.author = author
    await db.commit()
    for topic_id in topic_ids_to_moderate:
        _schedule_post_topic_moderation(topic_id)
    return await get_post(db, post.id)


async def get_post(db: AsyncSession, post_id: int) -> Post:
    result = await db.execute(select(Post).options(joinedload(Post.author)).where(Post.id == post_id))
    post = result.scalar_one_or_none()
    if post is None or post.deleted_at is not None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Post not found")
    return post


async def get_visible_post(db: AsyncSession, post_id: int, viewer_id: int) -> Post:
    post = await get_post(db, post_id)
    if not await can_view_post(db, post, viewer_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Post not found")
    return post


async def list_feed(
    db: AsyncSession,
    viewer_id: int,
    scope: str,
    before_id: int | None,
    limit: int,
    topic: str | None = None,
    sort: str = "latest",
) -> list[Post]:
    friend_ids = await _friend_ids(db, viewer_id)
    query = select(Post).options(joinedload(Post.author)).where(Post.deleted_at.is_(None))
    if before_id is not None:
        query = query.where(Post.id < before_id, Post.pinned_at.is_(None))
    if scope == "friends":
        query = query.where(
            Post.author_id.in_([viewer_id, *friend_ids]),
            _post_visibility_is_not(PostVisibility.PRIVATE),
            or_(Post.author_id == viewer_id, _post_is_approved()),
        )
    elif scope == "mine":
        query = query.where(Post.author_id == viewer_id)
    else:
        query = query.where(
            _post_visibility_is(PostVisibility.PUBLIC),
            or_(Post.author_id == viewer_id, _post_is_approved()),
        )
    if topic:
        topic_name = _normalize_topic_name(topic)
        if topic_name:
            query = query.join(PostTopicLink, PostTopicLink.post_id == Post.id).join(PostTopic, PostTopic.id == PostTopicLink.topic_id).where(func.lower(PostTopic.name) == topic_name.lower(), _post_topic_is_approved())
    if sort == "hot":
        hot_since = datetime.now(UTC) - timedelta(days=14)
        like_counts = select(PostLike.post_id, func.count(PostLike.id).label("like_count")).group_by(PostLike.post_id).subquery()
        comment_counts = (
            select(PostComment.post_id, func.count(PostComment.id).label("comment_count"))
            .where(PostComment.deleted_at.is_(None), _post_comment_is_approved())
            .group_by(PostComment.post_id)
            .subquery()
        )
        query = query.where(Post.created_at >= hot_since)
        query = query.outerjoin(like_counts, like_counts.c.post_id == Post.id).outerjoin(comment_counts, comment_counts.c.post_id == Post.id)
        age_hours = hours_since(Post.created_at)
        heat_score = ((func.coalesce(like_counts.c.like_count, 0) * 3) + (func.coalesce(comment_counts.c.comment_count, 0) * 4) + (greatest(0, 72 - age_hours) * 0.8))
        order_by = (heat_score.desc(), Post.created_at.desc(), Post.id.desc())
    else:
        order_by = (Post.pinned_at.desc().nullslast(), Post.id.desc()) if scope == "mine" else (Post.id.desc(),)
    result = await db.execute(query.order_by(*order_by).limit(limit))
    return list(result.scalars().all())


async def list_user_posts(
    db: AsyncSession,
    profile_user_id: int,
    viewer_id: int,
    before_id: int | None,
    limit: int,
    sort: str = "latest",
) -> list[Post]:
    user_result = await db.execute(select(User.id).where(User.id == profile_user_id).limit(1))
    if user_result.scalar_one_or_none() is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    friend_ids = await _friend_ids(db, viewer_id)
    query = (
        select(Post)
        .options(joinedload(Post.author))
        .where(Post.deleted_at.is_(None), Post.author_id == profile_user_id, _visible_post_condition(viewer_id, friend_ids))
    )
    if before_id is not None:
        query = query.where(Post.id < before_id, Post.pinned_at.is_(None))
    if sort == "hot":
        hot_since = datetime.now(UTC) - timedelta(days=14)
        like_counts = select(PostLike.post_id, func.count(PostLike.id).label("like_count")).group_by(PostLike.post_id).subquery()
        comment_counts = (
            select(PostComment.post_id, func.count(PostComment.id).label("comment_count"))
            .where(PostComment.deleted_at.is_(None), _post_comment_is_approved())
            .group_by(PostComment.post_id)
            .subquery()
        )
        query = query.where(Post.created_at >= hot_since)
        query = query.outerjoin(like_counts, like_counts.c.post_id == Post.id).outerjoin(comment_counts, comment_counts.c.post_id == Post.id)
        age_hours = hours_since(Post.created_at)
        heat_score = ((func.coalesce(like_counts.c.like_count, 0) * 3) + (func.coalesce(comment_counts.c.comment_count, 0) * 4) + (greatest(0, 72 - age_hours) * 0.8))
        order_by = (heat_score.desc(), Post.created_at.desc(), Post.id.desc())
    else:
        order_by = (Post.pinned_at.desc().nullslast(), Post.id.desc())
    result = await db.execute(query.order_by(*order_by).limit(limit))
    return list(result.scalars().all())


async def list_admin_posts(
    db: AsyncSession,
    query: str | None,
    moderation_status: ModerationStatus | None,
    limit: int,
    offset: int,
) -> list[Post]:
    stmt = (
        select(Post)
        .options(joinedload(Post.author))
        .where(Post.deleted_at.is_(None))
        .order_by(Post.created_at.desc(), Post.id.desc())
        .offset(offset)
        .limit(limit)
    )
    if moderation_status is not None:
        stmt = stmt.where(func.lower(Post.moderation_status) == moderation_status.value)
    if query and query.strip():
        term = query.strip()
        keyword = f"%{term}%"
        conditions = [Post.content.ilike(keyword)]
        if term.isdigit():
            conditions.extend([Post.id == int(term), Post.author_id == int(term)])
        stmt = stmt.join(User, User.id == Post.author_id).where(
            or_(
                *conditions,
                User.username.ilike(keyword),
                User.nickname.ilike(keyword),
            )
        )
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def build_admin_post_reads(db: AsyncSession, posts: list[Post]) -> list[AdminPostRead]:
    post_ids = [post.id for post in posts]
    like_counts = await _like_counts(db, post_ids)
    if post_ids:
        comment_result = await db.execute(
            select(PostComment.post_id, func.count(PostComment.id))
            .where(PostComment.post_id.in_(post_ids), PostComment.deleted_at.is_(None))
            .group_by(PostComment.post_id)
        )
        comment_counts = {post_id: int(count or 0) for post_id, count in comment_result.all()}
    else:
        comment_counts = {}
    return [
        AdminPostRead(
            id=post.id,
            author_id=post.author_id,
            author=UserPublic.model_validate(post.author) if post.author is not None else None,
            content=post.content,
            visibility=post.visibility,
            moderation_status=ModerationStatus(post.moderation_status),
            image_urls=_image_urls_from_json(post.image_urls_json),
            repost_of_id=post.repost_of_id,
            created_at=post.created_at,
            updated_at=post.updated_at,
            like_count=like_counts.get(post.id, 0),
            comment_count=comment_counts.get(post.id, 0),
        )
        for post in posts
    ]


async def user_post_stats(db: AsyncSession, profile_user_id: int, viewer_id: int) -> PostStatsRead:
    user_result = await db.execute(select(User.id).where(User.id == profile_user_id).limit(1))
    if user_result.scalar_one_or_none() is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    friend_ids = await _friend_ids(db, viewer_id)
    visible_post_result = await db.execute(
        select(Post.id).where(Post.deleted_at.is_(None), Post.author_id == profile_user_id, _visible_post_condition(viewer_id, friend_ids))
    )
    visible_post_ids = list(visible_post_result.scalars().all())
    if not visible_post_ids:
        return PostStatsRead()
    like_count_result = await db.execute(select(func.count(PostLike.id)).where(PostLike.post_id.in_(visible_post_ids)))
    comment_count_result = await db.execute(
        select(func.count(PostComment.id)).where(
            PostComment.post_id.in_(visible_post_ids),
            PostComment.deleted_at.is_(None),
            or_(PostComment.author_id == viewer_id, _post_comment_is_approved()),
        )
    )
    return PostStatsRead(
        post_count=len(visible_post_ids),
        like_count=int(like_count_result.scalar_one() or 0),
        comment_count=int(comment_count_result.scalar_one() or 0),
    )


async def _like_counts(db: AsyncSession, post_ids: list[int]) -> dict[int, int]:
    if not post_ids:
        return {}
    result = await db.execute(select(PostLike.post_id, func.count(PostLike.id)).where(PostLike.post_id.in_(post_ids)).group_by(PostLike.post_id))
    return {post_id: int(count or 0) for post_id, count in result.all()}


async def _comment_counts(db: AsyncSession, post_ids: list[int], viewer_id: int) -> dict[int, int]:
    if not post_ids:
        return {}
    result = await db.execute(
        select(PostComment.post_id, func.count(PostComment.id))
        .where(
            PostComment.post_id.in_(post_ids),
            PostComment.deleted_at.is_(None),
            or_(PostComment.author_id == viewer_id, _post_comment_is_approved()),
        )
        .group_by(PostComment.post_id)
    )
    return {post_id: int(count or 0) for post_id, count in result.all()}


async def _liked_post_ids(db: AsyncSession, post_ids: list[int], viewer_id: int) -> set[int]:
    if not post_ids:
        return set()
    result = await db.execute(select(PostLike.post_id).where(PostLike.post_id.in_(post_ids), PostLike.user_id == viewer_id))
    return set(result.scalars().all())


async def _topics_for_posts(db: AsyncSession, post_ids: list[int]) -> dict[int, list[PostTopicRead]]:
    if not post_ids:
        return {}
    result = await db.execute(
        select(PostTopicLink.post_id, PostTopic)
        .join(PostTopic, PostTopic.id == PostTopicLink.topic_id)
        .where(PostTopicLink.post_id.in_(post_ids), _post_topic_is_approved())
        .order_by(PostTopic.name.asc())
    )
    topics: dict[int, list[PostTopicRead]] = {}
    for post_id, topic in result.all():
        topics.setdefault(post_id, []).append(PostTopicRead.model_validate(topic))
    return topics


async def _recent_likes(db: AsyncSession, post_ids: list[int], limit_per_post: int = 6) -> dict[int, list[UserPublic]]:
    if not post_ids:
        return {}
    result = await db.execute(
        select(PostLike)
        .options(joinedload(PostLike.user))
        .where(PostLike.post_id.in_(post_ids))
        .order_by(PostLike.post_id.asc(), PostLike.created_at.desc(), PostLike.id.desc())
    )
    grouped: dict[int, list[UserPublic]] = {post_id: [] for post_id in post_ids}
    for like in result.scalars().all():
        users = grouped.setdefault(like.post_id, [])
        if len(users) < limit_per_post and like.user is not None:
            users.append(UserPublic.model_validate(like.user))
    return grouped


async def _recent_comments(db: AsyncSession, post_ids: list[int], viewer_id: int) -> dict[int, list[PostCommentRead]]:
    if not post_ids:
        return {}
    result = await db.execute(
        select(PostComment)
        .options(joinedload(PostComment.author))
        .where(
            PostComment.post_id.in_(post_ids),
            PostComment.deleted_at.is_(None),
            or_(PostComment.author_id == viewer_id, _post_comment_is_approved()),
        )
        .order_by(PostComment.post_id.asc(), PostComment.created_at.asc(), PostComment.id.asc())
    )
    grouped: dict[int, list[PostCommentRead]] = {post_id: [] for post_id in post_ids}
    comments = list(result.scalars().all())
    for comment in await _build_comment_reads(db, comments, viewer_id):
        grouped.setdefault(comment.post_id, []).append(comment)
    return grouped


async def _comment_like_counts(db: AsyncSession, comment_ids: list[int]) -> dict[int, int]:
    if not comment_ids:
        return {}
    result = await db.execute(select(PostCommentLike.comment_id, func.count(PostCommentLike.id)).where(PostCommentLike.comment_id.in_(comment_ids)).group_by(PostCommentLike.comment_id))
    return {comment_id: int(count or 0) for comment_id, count in result.all()}


async def _liked_comment_ids(db: AsyncSession, comment_ids: list[int], viewer_id: int) -> set[int]:
    if not comment_ids:
        return set()
    result = await db.execute(select(PostCommentLike.comment_id).where(PostCommentLike.comment_id.in_(comment_ids), PostCommentLike.user_id == viewer_id))
    return set(result.scalars().all())


async def _comment_parent_authors(db: AsyncSession, comments: list[PostComment]) -> dict[int, UserPublic]:
    parent_ids = {comment.parent_id for comment in comments if comment.parent_id is not None}
    if not parent_ids:
        return {}
    result = await db.execute(select(PostComment).options(joinedload(PostComment.author)).where(PostComment.id.in_(parent_ids)))
    parent_authors: dict[int, UserPublic] = {}
    for parent in result.scalars().all():
        if parent.author is not None:
            parent_authors[parent.id] = UserPublic.model_validate(parent.author)
    return parent_authors


async def _build_comment_reads(db: AsyncSession, comments: list[PostComment], viewer_id: int, comment_ids: list[int] | None = None) -> list[PostCommentRead]:
    ids = comment_ids if comment_ids is not None else [comment.id for comment in comments]
    like_counts = await _comment_like_counts(db, ids)
    liked_ids = await _liked_comment_ids(db, ids, viewer_id)
    parent_authors = await _comment_parent_authors(db, comments)
    return [
        PostCommentRead(
            id=comment.id,
            post_id=comment.post_id,
            author_id=comment.author_id,
            author=UserPublic.model_validate(comment.author) if comment.author is not None else None,
            parent_id=comment.parent_id,
            parent_author=parent_authors.get(comment.parent_id) if comment.parent_id is not None else None,
            content=comment.content,
            moderation_status=ModerationStatus(comment.moderation_status),
            created_at=comment.created_at,
            like_count=like_counts.get(comment.id, 0),
            liked_by_me=comment.id in liked_ids,
        )
        for comment in comments
    ]


async def build_post_reads(db: AsyncSession, posts: list[Post], viewer_id: int) -> list[PostRead]:
    post_ids = [post.id for post in posts]
    like_counts = await _like_counts(db, post_ids)
    comment_counts = await _comment_counts(db, post_ids, viewer_id)
    liked_ids = await _liked_post_ids(db, post_ids, viewer_id)
    recent_likes = await _recent_likes(db, post_ids)
    recent_comments = await _recent_comments(db, post_ids, viewer_id)
    repost_refs = await _repost_references(db, posts, viewer_id)
    topics = await _topics_for_posts(db, post_ids)
    return [
        PostRead(
            id=post.id,
            author_id=post.author_id,
            author=UserPublic.model_validate(post.author) if post.author is not None else None,
            content=post.content,
            ccw_creations=_ccw_creations_from_json(post.ccw_creations_json),
            visibility=post.visibility,
            moderation_status=ModerationStatus(post.moderation_status),
            image_urls=_image_urls_from_json(post.image_urls_json),
            repost_of_id=post.repost_of_id,
            repost_of=repost_refs.get(post.repost_of_id) if post.repost_of_id is not None else None,
            pinned_at=post.pinned_at,
            topics=topics.get(post.id, []),
            created_at=post.created_at,
            updated_at=post.updated_at,
            like_count=like_counts.get(post.id, 0),
            comment_count=comment_counts.get(post.id, 0),
            liked_by_me=post.id in liked_ids,
            recent_likes=recent_likes.get(post.id, []),
            recent_comments=recent_comments.get(post.id, []),
        )
        for post in posts
    ]


async def _repost_references(db: AsyncSession, posts: list[Post], viewer_id: int) -> dict[int, PostReferenceRead]:
    repost_ids = {post.repost_of_id for post in posts if post.repost_of_id is not None}
    if not repost_ids:
        return {}
    result = await db.execute(select(Post).options(joinedload(Post.author)).where(Post.id.in_(repost_ids), Post.deleted_at.is_(None)))
    refs: dict[int, PostReferenceRead] = {}
    topics = await _topics_for_posts(db, list(repost_ids))
    for post in result.scalars().all():
        if not await can_view_post(db, post, viewer_id):
            continue
        refs[post.id] = PostReferenceRead(
            id=post.id,
            author_id=post.author_id,
            author=UserPublic.model_validate(post.author) if post.author is not None else None,
            content=post.content,
            ccw_creations=_ccw_creations_from_json(post.ccw_creations_json),
            visibility=post.visibility,
            moderation_status=ModerationStatus(post.moderation_status),
            image_urls=_image_urls_from_json(post.image_urls_json),
            pinned_at=post.pinned_at,
            topics=topics.get(post.id, []),
            created_at=post.created_at,
            updated_at=post.updated_at,
        )
    return refs


async def build_post_read(db: AsyncSession, post: Post, viewer_id: int) -> PostRead:
    return (await build_post_reads(db, [post], viewer_id))[0]


async def delete_post(db: AsyncSession, post_id: int, actor_id: int) -> Post:
    post = await get_post(db, post_id)
    if post.author_id != actor_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only the author can delete this post")
    if post.deleted_at is None:
        candidate_paths = _post_upload_paths(post)
        post.deleted_at = datetime.now(UTC)
        await db.execute(delete(PostTopicLink).where(PostTopicLink.post_id == post.id))
        await _refresh_topic_counts(db)
        await db.commit()
        keys_to_delete: list[str] = []
        for relative_path in candidate_paths:
            if await _upload_path_still_referenced(db, relative_path):
                continue
            keys_to_delete.append(relative_path)
            keys_to_delete.append(thumbnail_key_for_key(relative_path))
        schedule_delete_upload_files(keys_to_delete)
    return post


async def admin_delete_post(db: AsyncSession, post_id: int, admin_id: int, *, commit: bool = True) -> Post:
    from app.services.reports import log_admin_action

    post = await get_post(db, post_id)
    post.deleted_at = datetime.now(UTC)
    await db.execute(delete(PostTopicLink).where(PostTopicLink.post_id == post.id))
    await _refresh_topic_counts(db)
    await log_admin_action(
        db,
        admin_id,
        "post.delete",
        "post",
        post.id,
        {"author_id": post.author_id, "moderation_status": _status_value(post.moderation_status)},
    )
    if not commit:
        return post
    await db.commit()
    await cleanup_deleted_post_uploads(db, post)
    return post


async def cleanup_deleted_post_uploads(db: AsyncSession, post: Post) -> None:
    candidate_paths = _post_upload_paths(post)
    keys_to_delete: list[str] = []
    for relative_path in candidate_paths:
        if await _upload_path_still_referenced(db, relative_path):
            continue
        keys_to_delete.extend([relative_path, thumbnail_key_for_key(relative_path)])
    schedule_delete_upload_files(keys_to_delete)


async def update_post(db: AsyncSession, post_id: int, actor: User, payload: PostUpdate) -> Post:
    post = await get_post(db, post_id)
    if post.author_id != actor.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only the author can edit this post")
    if payload.image_urls is not None and any(_post_upload_path(image_url) is None for image_url in payload.image_urls):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Post images must reference uploaded post images")

    content = post.content if payload.content is None else payload.content
    image_urls = _image_urls_from_json(post.image_urls_json) if payload.image_urls is None else payload.image_urls
    if not content and not image_urls and post.repost_of_id is None:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Post content or images are required")

    # CCW 作品卡片在改 post 之前取：要去 CCW 时会先把请求的读事务结束、连接还回去，这时会话里不能有改了一半的 post
    ccw_creations_json = _ccw_creations_to_json(await previews_from_text(content, db=db)) if payload.content is not None else None
    candidate_paths = _post_upload_paths(post) if payload.image_urls is not None else set()
    post.content = content
    if payload.content is not None:
        post.ccw_creations_json = ccw_creations_json
    if payload.visibility is not None:
        post.visibility = payload.visibility
    if payload.image_urls is not None:
        post.image_urls_json = _image_urls_to_json(payload.image_urls)
    if settings.content_moderation_enabled and not actor.content_moderation_whitelisted and (payload.content is not None or payload.image_urls is not None):
        post.moderation_status = ModerationStatus.PENDING.value
    post.updated_at = datetime.now(UTC)

    if payload.topic_names is not None or payload.content is not None:
        topic_ids_to_moderate = await _sync_post_topics(db, post.id, _merge_topic_names(post.content, payload.topic_names), actor)
        await _refresh_topic_counts(db)
    else:
        topic_ids_to_moderate = []
    if payload.mention_user_ids is not None:
        previous_mentioned_ids = await _mention_user_ids(db, post.id, None)
        mentioned_ids = await _visible_mentioned_user_ids(db, actor.id, payload.mention_user_ids)
        await _replace_mentions(db, post.id, None, mentioned_ids)
        new_mentioned_ids = [user_id for user_id in mentioned_ids if user_id not in previous_mentioned_ids]
        await create_notifications_for_post(db, post, new_mentioned_ids, include_repost=False)

    await db.commit()
    for topic_id in topic_ids_to_moderate:
        _schedule_post_topic_moderation(topic_id)
    if payload.image_urls is not None:
        keys_to_delete: list[str] = []
        for relative_path in candidate_paths:
            if await _upload_path_still_referenced(db, relative_path):
                continue
            keys_to_delete.append(relative_path)
            keys_to_delete.append(thumbnail_key_for_key(relative_path))
        schedule_delete_upload_files(keys_to_delete)
    return await get_post(db, post.id)


async def set_post_pinned(db: AsyncSession, post_id: int, actor_id: int, pinned: bool) -> Post:
    post = await get_post(db, post_id)
    if post.author_id != actor_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only the author can pin this post")
    if pinned:
        post.pinned_at = datetime.now(UTC)
    else:
        post.pinned_at = None
    post.updated_at = datetime.now(UTC)
    await db.commit()
    return await get_post(db, post.id)


async def toggle_like(db: AsyncSession, post_id: int, actor_id: int) -> PostLikeToggleRead:
    post = await get_visible_post(db, post_id, actor_id)
    if _status_value(post.moderation_status) != ModerationStatus.APPROVED.value:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Post is pending moderation")
    result = await db.execute(select(PostLike).where(PostLike.post_id == post_id, PostLike.user_id == actor_id))
    like = result.scalar_one_or_none()
    liked = like is None
    if like is None:
        db.add(PostLike(post_id=post_id, user_id=actor_id))
        await _create_notifications(
            db,
            [PostNotification(recipient_id=post.author_id, actor_id=actor_id, post_id=post_id, type=PostNotificationType.LIKE)] if post.author_id != actor_id else [],
            post=post,
        )
    else:
        await db.delete(like)
    await db.commit()
    count_result = await db.execute(select(func.count(PostLike.id)).where(PostLike.post_id == post_id))
    return PostLikeToggleRead(liked=liked, like_count=int(count_result.scalar_one() or 0))


async def list_likes(db: AsyncSession, post_id: int, viewer_id: int, limit: int) -> list[PostLikeRead]:
    post = await get_visible_post(db, post_id, viewer_id)
    if _status_value(post.moderation_status) != ModerationStatus.APPROVED.value and post.author_id != viewer_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Post not found")
    result = await db.execute(
        select(PostLike)
        .options(joinedload(PostLike.user))
        .where(PostLike.post_id == post_id)
        .order_by(PostLike.created_at.desc(), PostLike.id.desc())
        .limit(limit)
    )
    return [PostLikeRead.model_validate(like) for like in result.scalars().all()]


async def create_comment(db: AsyncSession, post_id: int, actor: User, content: str, parent_id: int | None = None, mention_user_ids: list[int] | None = None) -> PostComment:
    ensure_can_post(actor)
    post = await get_visible_post(db, post_id, actor.id)
    if _status_value(post.moderation_status) != ModerationStatus.APPROVED.value:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Post is pending moderation")
    if parent_id is not None:
        parent_result = await db.execute(select(PostComment).where(PostComment.id == parent_id, PostComment.post_id == post_id, PostComment.deleted_at.is_(None), _post_comment_is_approved()))
        parent_comment = parent_result.scalar_one_or_none()
        if parent_comment is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Parent comment not found")
    else:
        parent_comment = None
    initial_status = ModerationStatus.PENDING.value if settings.content_moderation_enabled and not actor.content_moderation_whitelisted else ModerationStatus.APPROVED.value
    comment = PostComment(post_id=post_id, author_id=actor.id, parent_id=parent_id, content=content, moderation_status=initial_status)
    db.add(comment)
    await db.flush()
    mentioned_ids = await _visible_mentioned_user_ids(db, actor.id, mention_user_ids or [])
    await _replace_mentions(db, post_id, comment.id, mentioned_ids)
    await create_notifications_for_comment(db, post, comment)
    comment.author = actor
    await db.commit()
    return await get_comment(db, post_id, comment.id, actor.id)


async def get_comment(db: AsyncSession, post_id: int, comment_id: int, viewer_id: int) -> PostComment:
    await get_visible_post(db, post_id, viewer_id)
    result = await db.execute(
        select(PostComment)
        .options(joinedload(PostComment.author))
        .where(
            PostComment.id == comment_id,
            PostComment.post_id == post_id,
            PostComment.deleted_at.is_(None),
            or_(PostComment.author_id == viewer_id, _post_comment_is_approved()),
        )
    )
    comment = result.scalar_one_or_none()
    if comment is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Comment not found")
    return comment


async def list_comments(db: AsyncSession, post_id: int, viewer_id: int, before_id: int | None, limit: int) -> list[PostCommentRead]:
    await get_visible_post(db, post_id, viewer_id)
    query = (
        select(PostComment)
        .options(joinedload(PostComment.author))
        .where(
            PostComment.post_id == post_id,
            PostComment.deleted_at.is_(None),
            or_(PostComment.author_id == viewer_id, _post_comment_is_approved()),
        )
    )
    if before_id is not None:
        query = query.where(PostComment.id < before_id)
    result = await db.execute(query.order_by(PostComment.id.desc()).limit(limit))
    comments = list(result.scalars().all())
    comments.reverse()
    return await _build_comment_reads(db, comments, viewer_id)


async def list_trending_topics(db: AsyncSession, limit: int) -> list[PostTopicRead]:
    limit = min(max(1, limit), 5)
    recent_since = datetime.now(UTC) - timedelta(days=7)
    recent_counts = (
        select(PostTopicLink.topic_id, func.count(PostTopicLink.id).label("recent_count"))
        .join(Post, Post.id == PostTopicLink.post_id)
        .where(Post.deleted_at.is_(None), _post_is_approved(), Post.created_at >= recent_since)
        .group_by(PostTopicLink.topic_id)
        .subquery()
    )
    heat_score = (func.coalesce(recent_counts.c.recent_count, 0) * 5) + least(PostTopic.post_count, 20)
    result = await db.execute(
        select(PostTopic)
        .outerjoin(recent_counts, recent_counts.c.topic_id == PostTopic.id)
        .where(_post_topic_is_approved(), PostTopic.post_count > 0, heat_score > 0)
        .order_by(heat_score.desc(), func.coalesce(recent_counts.c.recent_count, 0).desc(), PostTopic.post_count.desc(), PostTopic.id.desc())
        .limit(limit)
    )
    return [PostTopicRead.model_validate(topic) for topic in result.scalars().all()]


async def search_topics(db: AsyncSession, query: str, limit: int) -> list[PostTopicRead]:
    query = _normalize_topic_name(query)
    limit = min(max(1, limit), 10)
    if not query:
        return await list_trending_topics(db, min(limit, 5))

    result = await db.execute(
        select(PostTopic)
        .where(_post_topic_is_approved(), PostTopic.name.ilike(f"%{query}%"))
        .order_by(
            (func.lower(PostTopic.name) == query.lower()).desc(),
            PostTopic.post_count.desc(),
            PostTopic.id.desc(),
        )
        .limit(limit)
    )
    return [PostTopicRead.model_validate(topic) for topic in result.scalars().all()]


async def list_notifications(db: AsyncSession, viewer_id: int, before_id: int | None, limit: int) -> PostNotificationListRead:
    query = (
        select(PostNotification)
        .options(joinedload(PostNotification.actor), joinedload(PostNotification.post).joinedload(Post.author), joinedload(PostNotification.comment).joinedload(PostComment.author))
        .where(PostNotification.recipient_id == viewer_id)
    )
    if before_id is not None:
        query = query.where(PostNotification.id < before_id)
    result = await db.execute(query.order_by(PostNotification.id.desc()).limit(limit))
    notifications = list(result.scalars().all())
    unread_result = await db.execute(select(func.count(PostNotification.id)).where(PostNotification.recipient_id == viewer_id, PostNotification.read_at.is_(None)))
    unread_count = int(unread_result.scalar_one() or 0)
    comment_reads_by_id: dict[int, PostCommentRead] = {}
    comments = [notification.comment for notification in notifications if notification.comment is not None]
    for comment_read in await _build_comment_reads(db, comments, viewer_id):
        comment_reads_by_id[comment_read.id] = comment_read
    items: list[PostNotificationRead] = []
    for notification in notifications:
        post = notification.post
        post_ref = None
        if post is not None and post.deleted_at is None:
            post_ref = PostReferenceRead(
                id=post.id,
                author_id=post.author_id,
                author=UserPublic.model_validate(post.author) if post.author is not None else None,
                content=post.content,
                visibility=post.visibility,
                moderation_status=ModerationStatus(post.moderation_status),
                image_urls=_image_urls_from_json(post.image_urls_json),
                pinned_at=post.pinned_at,
                topics=[],
                created_at=post.created_at,
                updated_at=post.updated_at,
            )
        items.append(
            PostNotificationRead(
                id=notification.id,
                recipient_id=notification.recipient_id,
                actor_id=notification.actor_id,
                actor=UserPublic.model_validate(notification.actor) if notification.actor is not None else None,
                post_id=notification.post_id,
                comment_id=notification.comment_id,
                type=notification.type,
                read_at=notification.read_at,
                created_at=notification.created_at,
                post=post_ref,
                comment=comment_reads_by_id.get(notification.comment_id or -1),
            )
        )
    return PostNotificationListRead(items=items, unread_count=unread_count)


async def mark_notifications_read(db: AsyncSession, viewer_id: int, notification_id: int | None = None) -> int:
    query = update(PostNotification).where(PostNotification.recipient_id == viewer_id, PostNotification.read_at.is_(None))
    if notification_id is not None:
        query = query.where(PostNotification.id == notification_id)
    result = await db.execute(query.values(read_at=datetime.now(UTC)))
    await db.commit()
    return int(result.rowcount or 0)


async def notification_recipient_ids(db: AsyncSession, post_id: int, actor_id: int) -> list[int]:
    result = await db.execute(
        select(PostNotification.recipient_id)
        .where(PostNotification.post_id == post_id, PostNotification.actor_id == actor_id)
        .distinct()
    )
    return list(result.scalars().all())


async def recent_notification_recipient_ids(db: AsyncSession, post_id: int, actor_id: int, since: datetime) -> list[int]:
    result = await db.execute(
        select(PostNotification.recipient_id)
        .where(PostNotification.post_id == post_id, PostNotification.actor_id == actor_id, PostNotification.created_at >= since)
        .distinct()
    )
    return list(result.scalars().all())


async def unread_notification_count(db: AsyncSession, user_id: int) -> int:
    result = await db.execute(select(func.count(PostNotification.id)).where(PostNotification.recipient_id == user_id, PostNotification.read_at.is_(None)))
    return int(result.scalar_one() or 0)


async def toggle_comment_like(db: AsyncSession, post_id: int, comment_id: int, actor_id: int) -> PostLikeToggleRead:
    post = await get_visible_post(db, post_id, actor_id)
    comment = await get_comment(db, post_id, comment_id, actor_id)
    if _status_value(comment.moderation_status) != ModerationStatus.APPROVED.value:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Comment is pending moderation")
    result = await db.execute(select(PostCommentLike).where(PostCommentLike.comment_id == comment_id, PostCommentLike.user_id == actor_id))
    like = result.scalar_one_or_none()
    liked = like is None
    if like is None:
        db.add(PostCommentLike(comment_id=comment_id, user_id=actor_id))
        await _create_notifications(
            db,
            [PostNotification(recipient_id=comment.author_id, actor_id=actor_id, post_id=post_id, comment_id=comment_id, type=PostNotificationType.LIKE)] if comment.author_id != actor_id else [],
            post=post,
            comment=comment,
        )
    else:
        await db.delete(like)
    await db.commit()
    count_result = await db.execute(select(func.count(PostCommentLike.id)).where(PostCommentLike.comment_id == comment_id))
    return PostLikeToggleRead(liked=liked, like_count=int(count_result.scalar_one() or 0))


async def delete_comment(db: AsyncSession, post_id: int, comment_id: int, actor_id: int) -> PostComment:
    post = await get_visible_post(db, post_id, actor_id)
    result = await db.execute(select(PostComment).where(PostComment.id == comment_id, PostComment.post_id == post_id, PostComment.deleted_at.is_(None)))
    comment = result.scalar_one_or_none()
    if comment is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Comment not found")
    if comment.author_id != actor_id and post.author_id != actor_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You cannot delete this comment")
    comment.deleted_at = datetime.now(UTC)
    await db.commit()
    return comment


async def post_recipient_ids(db: AsyncSession, post: Post) -> set[int]:
    if _status_value(post.moderation_status) != ModerationStatus.APPROVED.value:
        return {post.author_id}
    if _status_value(post.visibility) == PostVisibility.PRIVATE.value:
        return {post.author_id}
    result = await db.execute(select(Friendship.friend_id).where(Friendship.user_id == post.author_id))
    return {post.author_id, *result.scalars().all()}


async def comment_recipient_ids(db: AsyncSession, post: Post, comment: PostComment) -> set[int]:
    if _status_value(comment.moderation_status) != ModerationStatus.APPROVED.value:
        return {comment.author_id}
    return await post_recipient_ids(db, post)
