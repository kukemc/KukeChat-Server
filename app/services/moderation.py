from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import re
import threading
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from numbers import Real
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from fastapi import HTTPException, status
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.models.conversation import Conversation, ConversationMember
from app.models.enums import MessageType, ModerationAction, ModerationSource, ModerationStatus, UserStatus
from app.models.message import Message
from app.models.post import Post, PostComment, PostTopic
from app.models.penalty import UserViolation
from app.models.report import AdminAuditLog, ContentModerationRecord
from app.models.teamup import TeamupComment, TeamupProfile
from app.models.user import User
from app.services import penalties as penalty_service
from app.services.email import send_email_internal
from app.services.storage import get_object, storage_key_from_url
from app.websocket.manager import admin_manager, manager


logger = logging.getLogger(__name__)
UTC = timezone.utc
PERMANENT_BAN_HOURS = 24 * 365 * 100
HIGH_CONFIDENCE_ABUSE_PATTERNS = tuple(
    re.compile(pattern)
    for pattern in (
        r"(?:我|你|他|她|它)?(?:操|艹|草|日)(?:你|他|她|它)?妈(?:的|逼|批)?",
        r"(?:你|他|她|它)?妈(?:的)?(?:逼|批)",
        r"(?:傻|煞|沙)(?:逼|批)",
        r"草泥马",
    )
)
_moderation_retry_task: asyncio.Task | None = None
_last_moderation_alert_at: datetime | None = None
_moderation_alert_lock = threading.Lock()


def start_moderation_retry_worker() -> None:
    global _moderation_retry_task
    if not settings.content_moderation_enabled:
        return
    if _moderation_retry_task is not None and not _moderation_retry_task.done():
        return
    _moderation_retry_task = asyncio.create_task(_moderation_retry_worker())


async def stop_moderation_retry_worker() -> None:
    global _moderation_retry_task
    task = _moderation_retry_task
    _moderation_retry_task = None
    if task is None or task.done():
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


def schedule_message_moderation(message_id: int) -> None:
    if not settings.content_moderation_enabled:
        return
    _schedule_task(_moderate_message_task(message_id, ModerationSource.MESSAGE_AUTO, None))


def schedule_post_moderation(post_id: int) -> None:
    if not settings.content_moderation_enabled:
        return
    _schedule_task(_moderate_post_task(post_id))


def schedule_post_comment_moderation(comment_id: int) -> None:
    if not settings.content_moderation_enabled:
        return
    _schedule_task(_moderate_post_comment_task(comment_id))


def schedule_post_topic_moderation(topic_id: int) -> None:
    if not settings.content_moderation_enabled:
        return
    _schedule_task(_moderate_post_topic_task(topic_id))


def schedule_profile_cover_moderation(user_id: int) -> None:
    if not settings.content_moderation_enabled:
        return
    _schedule_task(_moderate_profile_cover_task(user_id))


def schedule_teamup_profile_moderation(profile_id: int) -> None:
    if not settings.content_moderation_enabled:
        return
    _schedule_task(_moderate_teamup_profile_task(profile_id))


def schedule_teamup_comment_moderation(comment_id: int) -> None:
    if not settings.content_moderation_enabled:
        return
    _schedule_task(_moderate_teamup_comment_task(comment_id))


# 审核日志清理的最小间隔。跟着重试循环搭车执行，但没必要每 5 分钟扫一次，
# 一小时一遍足够跟上每天几千行的增长。
MODERATION_LOG_CLEANUP_INTERVAL_SECONDS = 3600
_last_log_cleanup_at: float = 0.0


async def prune_moderation_logs() -> int:
    """删掉过期的低价值审核记录，返回删除行数。

    永久保留真正有价值的：拦下来的（block）、需要人工复核的（review），以及
    产生过处置动作的（撤回、封号）。剩下的 allow / error 只留最近若干天 ——
    它们占全表 98%，admin 页面默认也不显示。

    分批删除：一次性删几十万行会把 WAL 撑得比数据库还大，而且长事务会挡住
    正常的写入。
    """
    retention_days = settings.content_moderation_log_retention_days
    if retention_days <= 0:
        return 0

    cutoff = datetime.now(UTC) - timedelta(days=retention_days)
    batch_size = settings.content_moderation_log_cleanup_batch_size
    deleted = 0
    async with AsyncSessionLocal() as db:
        while True:
            stale_ids = (
                await db.execute(
                    select(ContentModerationRecord.id)
                    .where(
                        ContentModerationRecord.created_at < cutoff,
                        ContentModerationRecord.decision.notin_(("block", "review")),
                        ContentModerationRecord.action_taken.in_(
                            (ModerationAction.NONE, ModerationAction.ERROR)
                        ),
                    )
                    .limit(batch_size)
                )
            ).scalars().all()
            if not stale_ids:
                break
            await db.execute(
                delete(ContentModerationRecord).where(ContentModerationRecord.id.in_(list(stale_ids)))
            )
            await db.commit()
            deleted += len(stale_ids)
            # 让出事件循环，别让清理把请求处理饿着
            await asyncio.sleep(0)
    if deleted:
        logger.info("Pruned %d stale moderation records older than %d days", deleted, retention_days)
    return deleted


async def _moderation_retry_worker() -> None:
    global _last_log_cleanup_at
    while True:
        try:
            await retry_pending_post_moderation()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Pending post moderation retry failed")

        now = time.monotonic()
        if now - _last_log_cleanup_at >= MODERATION_LOG_CLEANUP_INTERVAL_SECONDS:
            _last_log_cleanup_at = now
            try:
                await prune_moderation_logs()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Moderation log cleanup failed")

        await asyncio.sleep(settings.content_moderation_retry_interval_seconds)


async def retry_pending_post_moderation() -> dict[str, int]:
    if not settings.content_moderation_enabled:
        return {"posts": 0, "comments": 0, "topics": 0, "teamup_profiles": 0, "teamup_comments": 0}

    cutoff = datetime.now(UTC) - timedelta(seconds=settings.content_moderation_retry_min_age_seconds)
    limit = settings.content_moderation_retry_batch_size
    async with AsyncSessionLocal() as db:
        post_ids_result = await db.execute(
            select(Post.id)
            .where(Post.deleted_at.is_(None), Post.moderation_status == ModerationStatus.PENDING.value, Post.created_at <= cutoff)
            .order_by(Post.created_at.asc(), Post.id.asc())
            .limit(limit)
        )
        post_ids = list(post_ids_result.scalars().all())
        comment_ids_result = await db.execute(
            select(PostComment.id)
            .join(Post, Post.id == PostComment.post_id)
            .where(
                PostComment.deleted_at.is_(None),
                Post.deleted_at.is_(None),
                PostComment.moderation_status == ModerationStatus.PENDING.value,
                PostComment.created_at <= cutoff,
            )
            .order_by(PostComment.created_at.asc(), PostComment.id.asc())
            .limit(limit)
        )
        comment_ids = list(comment_ids_result.scalars().all())
        topic_ids_result = await db.execute(
            select(PostTopic.id)
            .where(PostTopic.moderation_status == ModerationStatus.PENDING.value, PostTopic.created_at <= cutoff)
            .order_by(PostTopic.created_at.asc(), PostTopic.id.asc())
            .limit(limit)
        )
        topic_ids = list(topic_ids_result.scalars().all())
        teamup_profile_ids_result = await db.execute(
            select(TeamupProfile.id)
            .where(
                TeamupProfile.deleted_at.is_(None),
                TeamupProfile.moderation_status == ModerationStatus.PENDING.value,
                TeamupProfile.created_at <= cutoff,
            )
            .order_by(TeamupProfile.created_at.asc(), TeamupProfile.id.asc())
            .limit(limit)
        )
        teamup_profile_ids = list(teamup_profile_ids_result.scalars().all())
        teamup_comment_ids_result = await db.execute(
            select(TeamupComment.id)
            .join(TeamupProfile, TeamupProfile.id == TeamupComment.profile_id)
            .where(
                TeamupComment.deleted_at.is_(None),
                TeamupProfile.deleted_at.is_(None),
                TeamupComment.moderation_status == ModerationStatus.PENDING.value,
                TeamupComment.created_at <= cutoff,
            )
            .order_by(TeamupComment.created_at.asc(), TeamupComment.id.asc())
            .limit(limit)
        )
        teamup_comment_ids = list(teamup_comment_ids_result.scalars().all())

    for post_id in post_ids:
        await _moderate_post_task(post_id)
    for comment_id in comment_ids:
        await _moderate_post_comment_task(comment_id)
    for topic_id in topic_ids:
        await _moderate_post_topic_task(topic_id)
    for teamup_profile_id in teamup_profile_ids:
        await _moderate_teamup_profile_task(teamup_profile_id)
    for teamup_comment_id in teamup_comment_ids:
        await _moderate_teamup_comment_task(teamup_comment_id)
    total = len(post_ids) + len(comment_ids) + len(topic_ids) + len(teamup_profile_ids) + len(teamup_comment_ids)
    if total:
        logger.info(
            "Retried pending post moderation: posts=%s comments=%s topics=%s teamup_profiles=%s teamup_comments=%s",
            len(post_ids),
            len(comment_ids),
            len(topic_ids),
            len(teamup_profile_ids),
            len(teamup_comment_ids),
        )
    return {
        "posts": len(post_ids),
        "comments": len(comment_ids),
        "topics": len(topic_ids),
        "teamup_profiles": len(teamup_profile_ids),
        "teamup_comments": len(teamup_comment_ids),
    }


def _schedule_task(coro) -> None:
    task = asyncio.create_task(coro)

    def consume_exception(done_task: asyncio.Task) -> None:
        try:
            done_task.result()
        except Exception:
            logger.exception("Content moderation task failed")

    task.add_done_callback(consume_exception)


async def _moderate_message_task(
    message_id: int,
    source: ModerationSource,
    report_id: int | None,
    db: AsyncSession | None = None,
) -> None:
    owns_session = db is None
    session = db or AsyncSessionLocal()
    try:
        result = await session.execute(
            select(Message)
            .options(joinedload(Message.sender))
            .where(Message.id == message_id)
        )
        message = result.scalar_one_or_none()
        if message is None or message.type == MessageType.SYSTEM:
            return
        if message.sender is not None and message.sender.content_moderation_whitelisted:
            return

        records = await _moderate_message_content(session, message, source, report_id)
        if not records:
            return

        worst = _worst_record(records)
        action = ModerationAction.NONE if source == ModerationSource.REPORT_REVIEW and worst.decision != "block" else await _apply_moderation_action(session, message, worst)
        for record in records:
            record.action_taken = action
            if action != ModerationAction.NONE:
                record.acted_at = datetime.now(UTC)

        await session.commit()
        await penalty_service.flush_notifications(session)
        await _broadcast_moderation_action(session, message, action)
    except Exception as exc:
        await session.rollback()
        penalty_service.discard_notifications(session)
        await _record_moderation_error(session, message_id, source, report_id, exc)
    finally:
        if owns_session:
            await session.close()


async def _moderate_post_task(post_id: int) -> None:
    async with AsyncSessionLocal() as db:
        try:
            result = await db.execute(select(Post).options(joinedload(Post.author)).where(Post.id == post_id))
            post = result.scalar_one_or_none()
            if post is None or post.deleted_at is not None:
                return
            if post.author is not None and post.author.content_moderation_whitelisted:
                post.moderation_status = ModerationStatus.APPROVED.value
                await db.commit()
                await _broadcast_post_moderation_action(db, post, ModerationAction.NONE)
                return
            records = await _moderate_post_content(db, post)
            if not records:
                post.moderation_status = ModerationStatus.APPROVED.value
                await db.commit()
                await _broadcast_post_moderation_action(db, post, ModerationAction.NONE)
                return
            worst = _worst_record(records)
            action = await _apply_post_moderation_action(db, post, worst)
            for record in records:
                record.action_taken = action
                if action != ModerationAction.NONE:
                    record.acted_at = datetime.now(UTC)
            await db.commit()
            await penalty_service.flush_notifications(db)
            await _broadcast_post_moderation_action(db, post, action)
        except Exception as exc:
            await db.rollback()
            await _record_moderation_error(db, None, ModerationSource.MESSAGE_AUTO, None, exc, post_id=post_id)


async def _moderate_post_comment_task(comment_id: int) -> None:
    async with AsyncSessionLocal() as db:
        try:
            result = await db.execute(
                select(PostComment)
                .options(joinedload(PostComment.author), joinedload(PostComment.post))
                .where(PostComment.id == comment_id)
            )
            comment = result.scalar_one_or_none()
            if comment is None or comment.deleted_at is not None or comment.post.deleted_at is not None:
                return
            if comment.author is not None and comment.author.content_moderation_whitelisted:
                comment.moderation_status = ModerationStatus.APPROVED.value
                await db.commit()
                await _broadcast_post_comment_moderation_action(db, comment, ModerationAction.NONE)
                return
            records = await _moderate_post_comment_content(db, comment)
            if not records:
                comment.moderation_status = ModerationStatus.APPROVED.value
                await db.commit()
                await _broadcast_post_comment_moderation_action(db, comment, ModerationAction.NONE)
                return
            worst = _worst_record(records)
            action = await _apply_post_comment_moderation_action(db, comment, worst)
            for record in records:
                record.action_taken = action
                if action != ModerationAction.NONE:
                    record.acted_at = datetime.now(UTC)
            await db.commit()
            await penalty_service.flush_notifications(db)
            await _broadcast_post_comment_moderation_action(db, comment, action)
        except Exception as exc:
            await db.rollback()
            await _record_moderation_error(db, None, ModerationSource.MESSAGE_AUTO, None, exc, post_comment_id=comment_id)


async def _moderate_post_topic_task(topic_id: int) -> None:
    async with AsyncSessionLocal() as db:
        try:
            result = await db.execute(select(PostTopic).options(joinedload(PostTopic.created_by)).where(PostTopic.id == topic_id))
            topic = result.scalar_one_or_none()
            if topic is None:
                return
            if topic.created_by is not None and topic.created_by.content_moderation_whitelisted:
                topic.moderation_status = ModerationStatus.APPROVED.value
                await db.commit()
                await admin_manager.broadcast("admin.moderation.changed", {"post_topic_id": topic.id, "action": ModerationAction.NONE.value, "moderation_status": topic.moderation_status})
                return
            record = await _moderate_post_topic_content(db, topic)
            if record is None:
                topic.moderation_status = ModerationStatus.APPROVED.value
                await db.commit()
                await admin_manager.broadcast("admin.moderation.changed", {"post_topic_id": topic.id, "action": ModerationAction.NONE.value, "moderation_status": topic.moderation_status})
                return
            action = await _apply_post_topic_moderation_action(db, topic, record)
            record.action_taken = action
            if action != ModerationAction.NONE:
                record.acted_at = datetime.now(UTC)
            await db.commit()
            await admin_manager.broadcast("admin.moderation.changed", {"post_topic_id": topic.id, "action": action.value, "moderation_status": topic.moderation_status})
        except Exception as exc:
            await db.rollback()
            await _record_moderation_error(db, None, ModerationSource.MESSAGE_AUTO, None, exc, post_topic_id=topic_id)


async def _moderate_profile_cover_task(user_id: int) -> None:
    async with AsyncSessionLocal() as db:
        try:
            user = await db.get(User, user_id)
            if user is None or not user.profile_cover_pending_url:
                return
            if user.content_moderation_whitelisted:
                user.profile_cover_url = user.profile_cover_pending_url
                user.profile_cover_pending_url = None
                user.profile_cover_moderation_status = ModerationStatus.APPROVED.value
                await db.commit()
                await admin_manager.broadcast("admin.moderation.changed", {"profile_user_id": user.id, "action": ModerationAction.NONE.value, "moderation_status": user.profile_cover_moderation_status})
                return
            image_url = user.profile_cover_pending_url
            result = await _call_image_moderation_for_user(image_url, user.id, context_id=f"profile-cover:{user.id}")
            record = _build_profile_cover_record(user, image_url, result)
            db.add(record)
            normalized = _normalize_result(result, "image")
            if normalized["decision"] != "block":
                user.profile_cover_url = image_url
                user.profile_cover_pending_url = None
                user.profile_cover_moderation_status = ModerationStatus.APPROVED.value
                action = ModerationAction.NONE
            else:
                user.profile_cover_pending_url = None
                user.profile_cover_moderation_status = ModerationStatus.REJECTED.value
                action = await _apply_user_punishment(db, user.id, record, target_type="profile_cover", target_id=user.id)
            record.action_taken = action
            if action != ModerationAction.NONE:
                record.acted_at = datetime.now(UTC)
            await db.commit()
            await penalty_service.flush_notifications(db)
            await admin_manager.broadcast("admin.moderation.changed", {"profile_user_id": user.id, "action": action.value, "moderation_status": user.profile_cover_moderation_status})
            if action in {ModerationAction.USER_BANNED, ModerationAction.PERMANENT_BAN}:
                await _disconnect_suspended_user(db, user.id)
        except Exception as exc:
            await db.rollback()
            failed_user = await db.get(User, user_id)
            if failed_user is not None and failed_user.profile_cover_moderation_status == ModerationStatus.PENDING.value:
                failed_user.profile_cover_pending_url = None
                failed_user.profile_cover_moderation_status = ModerationStatus.REJECTED.value
                await db.commit()
            await _record_moderation_error(db, None, ModerationSource.MESSAGE_AUTO, None, exc, profile_user_id=user_id)


async def _moderate_teamup_profile_task(profile_id: int) -> None:
    async with AsyncSessionLocal() as db:
        try:
            result = await db.execute(select(TeamupProfile).options(joinedload(TeamupProfile.user)).where(TeamupProfile.id == profile_id))
            profile = result.scalar_one_or_none()
            if profile is None or profile.deleted_at is not None:
                return
            if profile.user is not None and profile.user.content_moderation_whitelisted:
                profile.moderation_status = ModerationStatus.APPROVED.value
                await db.commit()
                await _broadcast_teamup_profile_moderation_action(db, profile, ModerationAction.NONE)
                return
            records = await _moderate_teamup_profile_content(db, profile)
            if not records:
                profile.moderation_status = ModerationStatus.APPROVED.value
                await db.commit()
                await _broadcast_teamup_profile_moderation_action(db, profile, ModerationAction.NONE)
                return
            worst = _worst_record(records)
            action = await _apply_teamup_profile_moderation_action(db, profile, worst)
            for record in records:
                record.action_taken = action
                if action != ModerationAction.NONE:
                    record.acted_at = datetime.now(UTC)
            await db.commit()
            await penalty_service.flush_notifications(db)
            await _broadcast_teamup_profile_moderation_action(db, profile, action)
        except Exception as exc:
            await db.rollback()
            await _record_moderation_error(db, None, ModerationSource.MESSAGE_AUTO, None, exc, teamup_profile_id=profile_id)


async def _moderate_teamup_comment_task(comment_id: int) -> None:
    async with AsyncSessionLocal() as db:
        try:
            result = await db.execute(
                select(TeamupComment)
                .options(joinedload(TeamupComment.author), joinedload(TeamupComment.profile))
                .where(TeamupComment.id == comment_id)
            )
            comment = result.scalar_one_or_none()
            if comment is None or comment.deleted_at is not None or comment.profile.deleted_at is not None:
                return
            if comment.author is not None and comment.author.content_moderation_whitelisted:
                comment.moderation_status = ModerationStatus.APPROVED.value
                await db.commit()
                await _broadcast_teamup_comment_moderation_action(db, comment, ModerationAction.NONE)
                return
            records = await _moderate_teamup_comment_content(db, comment)
            if not records:
                comment.moderation_status = ModerationStatus.APPROVED.value
                await db.commit()
                await _broadcast_teamup_comment_moderation_action(db, comment, ModerationAction.NONE)
                return
            worst = _worst_record(records)
            action = await _apply_teamup_comment_moderation_action(db, comment, worst)
            for record in records:
                record.action_taken = action
                if action != ModerationAction.NONE:
                    record.acted_at = datetime.now(UTC)
            await db.commit()
            await penalty_service.flush_notifications(db)
            await _broadcast_teamup_comment_moderation_action(db, comment, action)
        except Exception as exc:
            await db.rollback()
            await _record_moderation_error(db, None, ModerationSource.MESSAGE_AUTO, None, exc, teamup_comment_id=comment_id)


async def _moderate_message_content(
    db: AsyncSession,
    message: Message,
    source: ModerationSource,
    report_id: int | None,
) -> list[ContentModerationRecord]:
    checks: list[tuple[str, str | None, str | None, dict[str, Any]]] = []
    metadata = _json_object(message.metadata_json)
    text = _text_to_moderate(message, metadata)
    if text:
        checks.append(("text", text, None, await _call_text_moderation(text, message)))

    for image_url in _image_urls_to_moderate(message, metadata):
        image_result = await _call_image_moderation(image_url, message)
        checks.append(("image", None, image_url, image_result))

    records: list[ContentModerationRecord] = []
    for content_type, content_text, content_url, result in checks:
        record = _build_record(message, source, report_id, content_type, content_text, content_url, result)
        db.add(record)
        records.append(record)
    await db.flush()
    return records


async def _moderate_post_content(db: AsyncSession, post: Post) -> list[ContentModerationRecord]:
    checks: list[tuple[str, str | None, str | None, dict[str, Any]]] = []
    text = post.content.strip()
    if text:
        checks.append(("text", text, None, await _call_text_moderation_for_user(text, post.author_id, context_id=f"post:{post.id}")))
    for image_url in _post_image_urls_to_moderate(post):
        checks.append(("image", None, image_url, await _call_image_moderation_for_user(image_url, post.author_id, context_id=f"post:{post.id}")))

    records: list[ContentModerationRecord] = []
    for content_type, content_text, content_url, result in checks:
        record = _build_post_record(post, content_type, content_text, content_url, result)
        db.add(record)
        records.append(record)
    await db.flush()
    return records


async def _moderate_post_comment_content(db: AsyncSession, comment: PostComment) -> list[ContentModerationRecord]:
    text = comment.content.strip()
    if not text:
        return []
    result = await _call_text_moderation_for_user(text, comment.author_id, context_id=f"post_comment:{comment.id}")
    record = _build_post_comment_record(comment, text, result)
    db.add(record)
    await db.flush()
    return [record]


def _teamup_profile_image_urls(profile: TeamupProfile) -> list[str]:
    try:
        value = json.loads(profile.image_urls_json or "[]")
    except json.JSONDecodeError:
        return []
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(item.strip() for item in value if isinstance(item, str) and item.strip()))[:9]


async def _moderate_teamup_profile_content(db: AsyncSession, profile: TeamupProfile) -> list[ContentModerationRecord]:
    checks: list[tuple[str, str | None, str | None, dict[str, Any]]] = []
    text_parts = [part for part in (profile.headline, profile.intro, profile.contact_note) if part and part.strip()]
    text = "\n".join(text_parts).strip()
    if text:
        checks.append(("text", text, None, await _call_text_moderation_for_user(text, profile.user_id, context_id=f"teamup_profile:{profile.id}")))
    moderation_image_urls = list(_teamup_profile_image_urls(profile))
    if profile.background_url and profile.background_url.strip():
        background_url = profile.background_url.strip()
        if background_url not in moderation_image_urls:
            moderation_image_urls.append(background_url)
    for image_url in moderation_image_urls:
        checks.append(("image", None, image_url, await _call_image_moderation_for_user(image_url, profile.user_id, context_id=f"teamup_profile:{profile.id}")))

    records: list[ContentModerationRecord] = []
    for content_type, content_text, content_url, result in checks:
        record = _build_teamup_profile_record(profile, content_type, content_text, content_url, result)
        db.add(record)
        records.append(record)
    await db.flush()
    return records


async def _moderate_teamup_comment_content(db: AsyncSession, comment: TeamupComment) -> list[ContentModerationRecord]:
    text = comment.content.strip()
    if not text:
        return []
    result = await _call_text_moderation_for_user(text, comment.author_id, context_id=f"teamup_comment:{comment.id}")
    record = _build_teamup_comment_record(comment, text, result)
    db.add(record)
    await db.flush()
    return [record]


async def _moderate_post_topic_content(db: AsyncSession, topic: PostTopic) -> ContentModerationRecord | None:
    text = topic.name.strip()
    if not text:
        return None
    result = await _call_text_moderation_for_user(text, topic.created_by_id or 0, context_id=f"post_topic:{topic.id}")
    record = _build_post_topic_record(topic, text, result)
    db.add(record)
    await db.flush()
    return record


def _text_to_moderate(message: Message, metadata: dict[str, Any]) -> str | None:
    if message.type == MessageType.TEXT and message.content.strip():
        return _strip_mention_names(message.content, metadata)
    if message.type == MessageType.POLL:
        poll = metadata.get("poll")
        if not isinstance(poll, dict):
            return None
        parts = [str(poll.get("title") or "")] + [
            str(option.get("text") or "") for option in poll.get("options", []) if isinstance(option, dict)
        ]
        return "\n".join(part.strip() for part in parts if part.strip()) or None
    if message.type == MessageType.FORWARD_BUNDLE:
        parts: list[str] = []
        note = metadata.get("note")
        if isinstance(note, str) and note.strip():
            parts.append(note.strip())
        for item in metadata.get("messages", []):
            if isinstance(item, dict) and isinstance(item.get("content"), str):
                parts.append(item["content"].strip())
        return "\n".join(part for part in parts if part) or None
    return None


def _strip_mention_names(text: str, metadata: dict[str, Any]) -> str | None:
    clean_text = text.strip()
    if not clean_text:
        return None
    mentions = metadata.get("mentions")
    if isinstance(mentions, list):
        for mention in mentions:
            if not isinstance(mention, dict):
                continue
            name = mention.get("name") or mention.get("nickname") or mention.get("username")
            if not isinstance(name, str) or not name.strip():
                continue
            clean_text = re.sub(rf"@{re.escape(name.strip())}(?=\s|$)", " ", clean_text)
    if metadata.get("mention_all") is True or metadata.get("mentionAll") is True:
        clean_text = re.sub(r"@(?:所有人|全体成员|all)(?=\s|$)", " ", clean_text, flags=re.IGNORECASE)
    clean_text = re.sub(r"\s+", " ", clean_text).strip()
    return clean_text or None


def _image_urls_to_moderate(message: Message, metadata: dict[str, Any]) -> list[str]:
    urls: list[str] = []
    if message.type == MessageType.IMAGE and message.content.strip():
        urls.append(message.content.strip())
    raw_images = metadata.get("images")
    if isinstance(raw_images, list):
        urls.extend(item.strip() for item in raw_images if isinstance(item, str) and item.strip())
    return list(dict.fromkeys(urls))[:6]


def _post_image_urls_to_moderate(post: Post) -> list[str]:
    try:
        value = json.loads(post.image_urls_json or "[]")
    except json.JSONDecodeError:
        return []
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(item.strip() for item in value if isinstance(item, str) and item.strip()))[:9]


async def _call_text_moderation(text: str, message: Message) -> dict[str, Any]:
    return await _call_text_moderation_for_user(text, message.sender_id, str(message.conversation_id))


async def _call_text_moderation_for_user(text: str, user_id: int, context_id: str | None = None) -> dict[str, Any]:
    async def call(candidate: str) -> dict[str, Any]:
        payload = {"text": candidate, "user_id": str(user_id)}
        if context_id is not None:
            payload["conversation_id"] = context_id
        result = await asyncio.to_thread(_post_json, "/moderate/text", payload)
        _ensure_moderation_response_usable(result)
        return result

    normalized_text = _normalized_text_variant(text)
    local_result = _local_text_moderation_result(normalized_text)
    if local_result is not None:
        return local_result
    result = await call(text)
    if not normalized_text or normalized_text == text:
        return result
    normalized_result = await call(normalized_text)
    candidates = [result, normalized_result]
    return max(candidates, key=lambda item: (_normalize_result(item, "text")["severity"], _decision_rank(_normalize_result(item, "text")["decision"])))


def _normalized_text_variant(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text)
    normalized = "".join(character for character in normalized if unicodedata.category(character) != "Cf")
    normalized = re.sub(r"\s+", "", normalized)
    return normalized.strip()


def _local_text_moderation_result(normalized_text: str) -> dict[str, Any] | None:
    if not normalized_text:
        return None
    if any(pattern.search(normalized_text) for pattern in HIGH_CONFIDENCE_ABUSE_PATTERNS):
        return {
            "type": "text",
            "decision": "block",
            "severity": 1,
            "categories": ["harassment"],
            "confidence": 1.0,
            "reason": "High-confidence abusive language matched local safety rule",
            "model": "kukechat-local-safety",
        }
    return None


def _ensure_moderation_response_usable(result: dict[str, Any]) -> None:
    raw_output = result.get("raw_model_output")
    if not isinstance(raw_output, str) or not raw_output.strip():
        return
    try:
        upstream = json.loads(raw_output)
    except json.JSONDecodeError:
        return
    if not isinstance(upstream, dict):
        return
    code = upstream.get("code")
    upstream_status = upstream.get("status")
    if code not in (None, 0, "0", "0000") or upstream_status not in (None, 0, "0", "success"):
        reason = upstream.get("msg") or upstream.get("message") or "unknown upstream error"
        raise RuntimeError(f"Moderation upstream failed: code={code}, status={upstream_status}, reason={reason}")


async def _call_image_moderation(image_url: str, message: Message) -> dict[str, Any]:
    return await _call_image_moderation_for_user(image_url, message.sender_id, str(message.conversation_id))


async def _call_image_moderation_for_user(image_url: str, user_id: int, context_id: str | None = None) -> dict[str, Any]:
    key = storage_key_from_url(image_url)
    if key is None:
        # 外链图片没法送审。review 不再触发撤回，所以这里必须明确判 block，
        # 否则任意外链就成了绕过图片审核的口子。severity 1 只撤回、不处罚。
        return {
            "type": "image",
            "decision": "block",
            "severity": 1,
            "confidence": None,
            "reason": "Image URL is not a local moderated upload",
        }
    image = await asyncio.to_thread(get_object, key)
    image_base64 = base64.b64encode(image.data).decode("ascii")
    payload = {
        "image_base64": image_base64,
        "user_id": str(user_id),
    }
    if context_id is not None:
        payload["conversation_id"] = context_id
    return await asyncio.to_thread(_post_json, "/moderate", payload)


def _post_json(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    if not settings.content_moderation_url:
        raise RuntimeError("CONTENT_MODERATION_URL 未配置，无法调用内容审核服务")
    base_url = settings.content_moderation_url.rstrip("/")
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if settings.content_moderation_token:
        headers["X-Moderation-Token"] = settings.content_moderation_token
    request = Request(f"{base_url}{path}", data=data, headers=headers, method="POST")
    try:
        with urlopen(request, timeout=settings.content_moderation_timeout_seconds) as response:
            body = response.read().decode("utf-8")
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        _notify_moderation_service_failure(f"HTTP {exc.code}: {body}")
        raise RuntimeError(f"Moderation service returned HTTP {exc.code}: {body}") from exc
    except URLError as exc:
        _notify_moderation_service_failure(f"Unavailable: {exc.reason}")
        raise RuntimeError(f"Moderation service unavailable: {exc.reason}") from exc
    except TimeoutError as exc:
        _notify_moderation_service_failure(f"Timeout after {settings.content_moderation_timeout_seconds}s")
        raise RuntimeError(f"Moderation service timed out after {settings.content_moderation_timeout_seconds}s") from exc
    try:
        value = json.loads(body)
    except json.JSONDecodeError as exc:
        _notify_moderation_service_failure(f"Invalid JSON response: {body[:500]}")
        raise RuntimeError(f"Moderation service returned invalid JSON: {body[:500]}") from exc
    return value if isinstance(value, dict) else {"decision": "review", "severity": 1, "reason": "Invalid moderation response", "raw": value}


def _notify_moderation_service_failure(reason: str) -> None:
    global _last_moderation_alert_at
    now = datetime.now(UTC)
    with _moderation_alert_lock:
        if _last_moderation_alert_at is not None and now - _last_moderation_alert_at < timedelta(minutes=settings.moderation_alert_cooldown_minutes):
            return
        _last_moderation_alert_at = now
    if not settings.moderation_alert_email:
        return
    success, error = send_email_internal(
        settings.moderation_alert_email,
        "KukeChat 审核服务异常",
        "KukeChat 内容审核服务调用失败。\n"
        f"时间: {now.isoformat()}\n"
        f"审核服务: {settings.content_moderation_url}\n"
        f"错误: {reason}\n"
        "系统会定时重试仍处于 pending 的动态、评论和话题。",
    )
    if not success:
        logger.warning("Failed to send moderation outage alert email: %s", error)


def _build_record(
    message: Message,
    source: ModerationSource,
    report_id: int | None,
    content_type: str,
    content_text: str | None,
    content_url: str | None,
    result: dict[str, Any],
) -> ContentModerationRecord:
    normalized = _normalize_result(result, content_type)
    keep_evidence = not (normalized["decision"] == "allow" and normalized["severity"] <= 0)
    evidence_json = None
    if keep_evidence:
        evidence = {
            "message": _message_snapshot(message),
            "moderation": normalized,
        }
        evidence_json = _canonical_json(evidence)
    return ContentModerationRecord(
        source=source,
        message_id=message.id,
        report_id=report_id,
        conversation_id=message.conversation_id,
        user_id=message.sender_id,
        content_type=content_type,
        content_text=content_text,
        content_url=content_url,
        decision=normalized["decision"],
        severity=normalized["severity"],
        categories=_canonical_json(normalized.get("categories") or []),
        confidence=float(normalized.get("confidence")) if isinstance(normalized.get("confidence"), Real) else None,
        reason=normalized.get("reason"),
        raw_response=_canonical_json(result),
        evidence_snapshot=evidence_json,
        evidence_hash=hashlib.sha256(evidence_json.encode("utf-8")).hexdigest() if evidence_json else None,
    )


def _build_post_record(
    post: Post,
    content_type: str,
    content_text: str | None,
    content_url: str | None,
    result: dict[str, Any],
) -> ContentModerationRecord:
    normalized = _normalize_result(result, content_type)
    keep_evidence = not (normalized["decision"] == "allow" and normalized["severity"] <= 0)
    evidence_json = None
    if keep_evidence:
        evidence_json = _canonical_json({"post": _post_snapshot(post), "moderation": normalized})
    return ContentModerationRecord(
        source=ModerationSource.MESSAGE_AUTO,
        post_id=post.id,
        user_id=post.author_id,
        content_type=content_type,
        content_text=content_text,
        content_url=content_url,
        decision=normalized["decision"],
        severity=normalized["severity"],
        categories=_canonical_json(normalized.get("categories") or []),
        confidence=float(normalized.get("confidence")) if isinstance(normalized.get("confidence"), Real) else None,
        reason=normalized.get("reason"),
        raw_response=_canonical_json(result),
        evidence_snapshot=evidence_json,
        evidence_hash=hashlib.sha256(evidence_json.encode("utf-8")).hexdigest() if evidence_json else None,
    )


def _build_post_comment_record(comment: PostComment, content_text: str, result: dict[str, Any]) -> ContentModerationRecord:
    normalized = _normalize_result(result, "text")
    keep_evidence = not (normalized["decision"] == "allow" and normalized["severity"] <= 0)
    evidence_json = None
    if keep_evidence:
        evidence_json = _canonical_json({"post_comment": _post_comment_snapshot(comment), "moderation": normalized})
    return ContentModerationRecord(
        source=ModerationSource.MESSAGE_AUTO,
        post_id=comment.post_id,
        post_comment_id=comment.id,
        user_id=comment.author_id,
        content_type="text",
        content_text=content_text,
        decision=normalized["decision"],
        severity=normalized["severity"],
        categories=_canonical_json(normalized.get("categories") or []),
        confidence=float(normalized.get("confidence")) if isinstance(normalized.get("confidence"), Real) else None,
        reason=normalized.get("reason"),
        raw_response=_canonical_json(result),
        evidence_snapshot=evidence_json,
        evidence_hash=hashlib.sha256(evidence_json.encode("utf-8")).hexdigest() if evidence_json else None,
    )


def _build_post_topic_record(topic: PostTopic, content_text: str, result: dict[str, Any]) -> ContentModerationRecord:
    normalized = _normalize_result(result, "text")
    keep_evidence = not (normalized["decision"] == "allow" and normalized["severity"] <= 0)
    evidence_json = None
    if keep_evidence:
        evidence_json = _canonical_json({"post_topic": {"id": topic.id, "name": topic.name, "created_by_id": topic.created_by_id}, "moderation": normalized})
    return ContentModerationRecord(
        source=ModerationSource.MESSAGE_AUTO,
        post_topic_id=topic.id,
        user_id=topic.created_by_id,
        content_type="text",
        content_text=content_text,
        decision=normalized["decision"],
        severity=normalized["severity"],
        categories=_canonical_json(normalized.get("categories") or []),
        confidence=float(normalized.get("confidence")) if isinstance(normalized.get("confidence"), Real) else None,
        reason=normalized.get("reason"),
        raw_response=_canonical_json(result),
        evidence_snapshot=evidence_json,
        evidence_hash=hashlib.sha256(evidence_json.encode("utf-8")).hexdigest() if evidence_json else None,
    )


def _build_profile_cover_record(user: User, content_url: str, result: dict[str, Any]) -> ContentModerationRecord:
    normalized = _normalize_result(result, "image")
    keep_evidence = not (normalized["decision"] == "allow" and normalized["severity"] <= 0)
    evidence_json = None
    if keep_evidence:
        evidence_json = _canonical_json({"profile_cover": {"user_id": user.id, "url": content_url}, "moderation": normalized})
    return ContentModerationRecord(
        source=ModerationSource.MESSAGE_AUTO,
        profile_user_id=user.id,
        user_id=user.id,
        content_type="image",
        content_url=content_url,
        decision=normalized["decision"],
        severity=normalized["severity"],
        categories=_canonical_json(normalized.get("categories") or []),
        confidence=float(normalized.get("confidence")) if isinstance(normalized.get("confidence"), Real) else None,
        reason=normalized.get("reason"),
        raw_response=_canonical_json(result),
        evidence_snapshot=evidence_json,
        evidence_hash=hashlib.sha256(evidence_json.encode("utf-8")).hexdigest() if evidence_json else None,
    )


def _build_teamup_profile_record(
    profile: TeamupProfile,
    content_type: str,
    content_text: str | None,
    content_url: str | None,
    result: dict[str, Any],
) -> ContentModerationRecord:
    normalized = _normalize_result(result, content_type)
    keep_evidence = not (normalized["decision"] == "allow" and normalized["severity"] <= 0)
    evidence_json = None
    if keep_evidence:
        evidence_json = _canonical_json({"teamup_profile": _teamup_profile_snapshot(profile), "moderation": normalized})
    return ContentModerationRecord(
        source=ModerationSource.MESSAGE_AUTO,
        teamup_profile_id=profile.id,
        user_id=profile.user_id,
        content_type=content_type,
        content_text=content_text,
        content_url=content_url,
        decision=normalized["decision"],
        severity=normalized["severity"],
        categories=_canonical_json(normalized.get("categories") or []),
        confidence=float(normalized.get("confidence")) if isinstance(normalized.get("confidence"), Real) else None,
        reason=normalized.get("reason"),
        raw_response=_canonical_json(result),
        evidence_snapshot=evidence_json,
        evidence_hash=hashlib.sha256(evidence_json.encode("utf-8")).hexdigest() if evidence_json else None,
    )


def _build_teamup_comment_record(comment: TeamupComment, content_text: str, result: dict[str, Any]) -> ContentModerationRecord:
    normalized = _normalize_result(result, "text")
    keep_evidence = not (normalized["decision"] == "allow" and normalized["severity"] <= 0)
    evidence_json = None
    if keep_evidence:
        evidence_json = _canonical_json({"teamup_comment": _teamup_comment_snapshot(comment), "moderation": normalized})
    return ContentModerationRecord(
        source=ModerationSource.MESSAGE_AUTO,
        teamup_profile_id=comment.profile_id,
        teamup_comment_id=comment.id,
        user_id=comment.author_id,
        content_type="text",
        content_text=content_text,
        decision=normalized["decision"],
        severity=normalized["severity"],
        categories=_canonical_json(normalized.get("categories") or []),
        confidence=float(normalized.get("confidence")) if isinstance(normalized.get("confidence"), Real) else None,
        reason=normalized.get("reason"),
        raw_response=_canonical_json(result),
        evidence_snapshot=evidence_json,
        evidence_hash=hashlib.sha256(evidence_json.encode("utf-8")).hexdigest() if evidence_json else None,
    )


def _normalize_result(result: dict[str, Any], fallback_type: str) -> dict[str, Any]:
    candidates = result.get("results") if isinstance(result.get("results"), list) else [result]
    typed_candidates = [item for item in candidates if isinstance(item, dict) and item.get("type") == fallback_type]
    selected = typed_candidates[0] if typed_candidates else next((item for item in candidates if isinstance(item, dict)), result)
    decision = str(selected.get("decision") or result.get("decision") or "review").lower()
    if decision not in {"allow", "review", "block"}:
        decision = "review"
    severity = _safe_int(selected.get("severity", result.get("severity", 1)))
    return {
        "type": selected.get("type") or fallback_type,
        "decision": decision,
        "severity": severity,
        "categories": selected.get("categories") if isinstance(selected.get("categories"), list) else [],
        "confidence": selected.get("confidence"),
        "reason": selected.get("reason") or result.get("reason"),
        "scores": selected.get("scores"),
    }


async def _apply_moderation_action(db: AsyncSession, message: Message, record: ContentModerationRecord) -> ModerationAction:
    if not _needs_action(record):
        return ModerationAction.NONE

    now = datetime.now(UTC)
    if message.recalled_at is None:
        message.recalled_at = now
        message.recalled_by_id = None
        conversation = await db.get(Conversation, message.conversation_id)
        if conversation is not None:
            conversation.updated_at = now

    action = await _apply_user_punishment(
        db,
        message.sender_id,
        record,
        target_type="message",
        target_id=message.id,
        conversation_id=message.conversation_id,
    )

    db.add(
        AdminAuditLog(
            actor_id=None,
            action="moderation.message.auto_action",
            target_type="message",
            target_id=message.id,
            details=_canonical_json(
                {
                    "action": action.value,
                    "user_id": message.sender_id,
                    "conversation_id": message.conversation_id,
                    "decision": record.decision,
                    "severity": record.severity,
                    "categories": _json_list(record.categories),
                    "reason": record.reason,
                    "evidence_hash": record.evidence_hash,
                }
            ),
        )
    )
    return action if action != ModerationAction.NONE else ModerationAction.RECALLED


async def _apply_post_moderation_action(db: AsyncSession, post: Post, record: ContentModerationRecord) -> ModerationAction:
    if not _needs_action(record):
        post.moderation_status = ModerationStatus.APPROVED.value
        return ModerationAction.NONE
    now = datetime.now(UTC)
    post.moderation_status = ModerationStatus.REJECTED.value
    if post.deleted_at is None:
        post.deleted_at = now
    action = await _apply_user_punishment(db, post.author_id, record, target_type="post", target_id=post.id)
    db.add(
        AdminAuditLog(
            actor_id=None,
            action="moderation.post.auto_action",
            target_type="post",
            target_id=post.id,
            details=_canonical_json(_action_details(action, post.author_id, record)),
        )
    )
    return action if action != ModerationAction.NONE else ModerationAction.RECALLED


async def _apply_post_comment_moderation_action(db: AsyncSession, comment: PostComment, record: ContentModerationRecord) -> ModerationAction:
    if not _needs_action(record):
        comment.moderation_status = ModerationStatus.APPROVED.value
        return ModerationAction.NONE
    now = datetime.now(UTC)
    comment.moderation_status = ModerationStatus.REJECTED.value
    if comment.deleted_at is None:
        comment.deleted_at = now
    action = await _apply_user_punishment(db, comment.author_id, record, target_type="post_comment", target_id=comment.id)
    db.add(
        AdminAuditLog(
            actor_id=None,
            action="moderation.post_comment.auto_action",
            target_type="post_comment",
            target_id=comment.id,
            details=_canonical_json({**_action_details(action, comment.author_id, record), "post_id": comment.post_id}),
        )
    )
    return action if action != ModerationAction.NONE else ModerationAction.RECALLED


async def _apply_teamup_profile_moderation_action(db: AsyncSession, profile: TeamupProfile, record: ContentModerationRecord) -> ModerationAction:
    if not _needs_action(record):
        profile.moderation_status = ModerationStatus.APPROVED.value
        return ModerationAction.NONE
    now = datetime.now(UTC)
    profile.moderation_status = ModerationStatus.REJECTED.value
    if profile.deleted_at is None:
        profile.deleted_at = now
    action = await _apply_user_punishment(db, profile.user_id, record, target_type="teamup_profile", target_id=profile.id)
    db.add(
        AdminAuditLog(
            actor_id=None,
            action="moderation.teamup_profile.auto_action",
            target_type="teamup_profile",
            target_id=profile.id,
            details=_canonical_json(_action_details(action, profile.user_id, record)),
        )
    )
    return action if action != ModerationAction.NONE else ModerationAction.RECALLED


async def _apply_teamup_comment_moderation_action(db: AsyncSession, comment: TeamupComment, record: ContentModerationRecord) -> ModerationAction:
    if not _needs_action(record):
        comment.moderation_status = ModerationStatus.APPROVED.value
        return ModerationAction.NONE
    now = datetime.now(UTC)
    comment.moderation_status = ModerationStatus.REJECTED.value
    if comment.deleted_at is None:
        comment.deleted_at = now
    action = await _apply_user_punishment(db, comment.author_id, record, target_type="teamup_comment", target_id=comment.id)
    db.add(
        AdminAuditLog(
            actor_id=None,
            action="moderation.teamup_comment.auto_action",
            target_type="teamup_comment",
            target_id=comment.id,
            details=_canonical_json({**_action_details(action, comment.author_id, record), "teamup_profile_id": comment.profile_id}),
        )
    )
    return action if action != ModerationAction.NONE else ModerationAction.RECALLED


async def _apply_post_topic_moderation_action(db: AsyncSession, topic: PostTopic, record: ContentModerationRecord) -> ModerationAction:
    if not _needs_action(record):
        topic.moderation_status = ModerationStatus.APPROVED.value
        return ModerationAction.NONE
    topic.moderation_status = ModerationStatus.REJECTED.value
    db.add(
        AdminAuditLog(
            actor_id=None,
            action="moderation.post_topic.auto_action",
            target_type="post_topic",
            target_id=topic.id,
            details=_canonical_json({**_action_details(ModerationAction.RECALLED, topic.created_by_id or 0, record), "topic_name": topic.name}),
        )
    )
    return ModerationAction.RECALLED


async def _apply_user_punishment(
    db: AsyncSession,
    user_id: int,
    record: ContentModerationRecord,
    *,
    target_type: str,
    target_id: int,
    conversation_id: int | None = None,
) -> ModerationAction:
    """内容已经处理掉了，这里只决定要不要给人记违规。处罚轻重交给统一处罚引擎。"""
    verdict = _verdict_for_record(record)
    if verdict is None:
        return ModerationAction.RECALLED
    category, level = verdict
    await db.flush()
    summary = (record.content_text or "").strip()[:120] or ("[图片]" if record.content_type == "image" else None)
    outcome = await penalty_service.record_violation(
        db,
        user_id=user_id,
        category=category,
        level=level,
        source=_violation_source(record),
        target_type=target_type,
        target_id=target_id,
        conversation_id=conversation_id,
        moderation_record_id=record.id,
        confidence=record.confidence,
        summary=summary,
        reason=record.reason,
    )
    if outcome is None:
        return ModerationAction.RECALLED
    if outcome.penalty.kind == "ban":
        return ModerationAction.PERMANENT_BAN if outcome.penalty.permanent else ModerationAction.USER_BANNED
    if outcome.penalty.kind == "mute":
        return ModerationAction.MUTED
    return ModerationAction.RECALLED


def _action_details(action: ModerationAction, user_id: int, record: ContentModerationRecord) -> dict[str, Any]:
    return {
        "action": action.value,
        "user_id": user_id,
        "decision": record.decision,
        "severity": record.severity,
        "categories": _json_list(record.categories),
        "reason": record.reason,
        "evidence_hash": record.evidence_hash,
    }


def _needs_action(record: ContentModerationRecord) -> bool:
    if record.decision == "block":
        return True
    if record.decision == "allow" and record.severity <= 0:
        return False
    # 剩下的都是模型「拿不准」，不是违规结论，只留记录等人工看。图片在这一档的
    # 误判最多，永远不动；文本是否先撤回由配置决定。无论哪种都不记违规。
    if record.content_type == "image":
        return False
    return settings.content_moderation_text_review_recall


def _worst_record(records: list[ContentModerationRecord]) -> ContentModerationRecord:
    return max(records, key=lambda item: (_needs_action(item), item.severity, _decision_rank(item.decision)))


# 旧版审核服务只给 decision / severity / categories，这里把它翻译成统一的
# (类别, 等级)。新版服务直接在结果里带 category / level，见 _verdict_for_record。
_LEGACY_TEXT_CATEGORIES: tuple[tuple[str, str, int], ...] = (
    ("minor_sexual", "minor_sexual", 4),
    ("sexual", "sexual", 3),
    ("fraud", "fraud", 3),
    ("gambling", "gambling", 3),
    ("drug", "drugs", 3),
    ("privacy", "privacy", 3),
    ("violence", "threat", 3),
    ("hate", "hate", 3),
    ("illegal", "illegal", 3),
    ("political_sensitive", "political", 2),
    ("spam", "advertising", 2),
    ("harassment", "profanity", 1),
)
_RULE_MODELS = {"rules-first", "kukechat-local-safety", "fast-safe-rules"}


def _raw_result(record: ContentModerationRecord) -> dict[str, Any]:
    """审核服务对这条内容的原始结论。统一入口 /moderate 把各类型的结论包在 results 里。"""
    raw = _json_object(record.raw_response)
    results = raw.get("results")
    if isinstance(results, list):
        for item in results:
            if isinstance(item, dict) and item.get("type") == record.content_type:
                return item
    return raw


def _verdict_for_record(record: ContentModerationRecord) -> tuple[str, int] | None:
    """返回这条审核记录对应的 (违规类别, 等级)；None 表示不构成违规认定。"""
    if record.decision != "block":
        return None
    raw = _raw_result(record)
    category, level = raw.get("category"), raw.get("level")
    if isinstance(category, str) and category in penalty_service.CATEGORIES and isinstance(level, int) and not isinstance(level, bool):
        return category, max(0, min(4, level))

    categories = set(_json_list(record.categories))
    if record.content_type == "image":
        if "minor_sexual" in categories:
            return "minor_sexual", 4
        # severity 1 的图片 block 是「送审不了」（外链），撤回即可，不算违规。
        return ("sexual", 3) if record.severity >= 2 else ("unverified_media", 0)

    matched = [(name, level) for legacy, name, level in _LEGACY_TEXT_CATEGORIES if legacy in categories]
    if not matched:
        return ("other", 2) if record.severity >= 2 else ("other", 1)
    name, level = max(matched, key=lambda item: item[1])
    # 旧服务用 severity 1 表示「违规但不严重」，不能和明确的严重违规同罚。
    if level == 3 and record.severity <= 1:
        return ("vulgar", 2) if name == "sexual" else (name, 2)
    return name, level


def _violation_source(record: ContentModerationRecord) -> str:
    if record.content_type == "image":
        return "auto_image"
    model = _raw_result(record).get("model")
    return "auto_rule" if model in _RULE_MODELS else "auto_ai"


async def _broadcast_moderation_action(db: AsyncSession, message: Message, action: ModerationAction) -> None:
    if action == ModerationAction.NONE:
        return
    from app.services import messages as message_service

    members_result = await db.execute(select(ConversationMember.user_id).where(ConversationMember.conversation_id == message.conversation_id))
    member_ids = set(members_result.scalars().all())
    for user_id in member_ids:
        data = await message_service.build_message_read(db, message, user_id)
        await manager.send_to_user(user_id, "message.recalled", data)
    await admin_manager.broadcast("admin.message.changed", {"message_id": message.id, "conversation_id": message.conversation_id, "action": action.value})
    await admin_manager.broadcast("admin.moderation.changed", {"message_id": message.id, "conversation_id": message.conversation_id, "action": action.value})
    if action in {ModerationAction.USER_BANNED, ModerationAction.PERMANENT_BAN}:
        await _disconnect_suspended_user(db, message.sender_id)


async def _broadcast_post_moderation_action(db: AsyncSession, post: Post, action: ModerationAction) -> None:
    from app.services import posts as post_service

    if action == ModerationAction.NONE:
        notifications = await post_service.create_notifications_for_post(db, post)
        await db.commit()
        recipients = await post_service.post_recipient_ids(db, post)
        await manager.send_to_users(recipients, "post.created", {"post_id": post.id, "actor_id": None, "moderation": True, "moderation_status": post.moderation_status})
        for notification in notifications:
            unread_count = await post_service.unread_notification_count(db, notification.recipient_id)
            await manager.send_to_user(notification.recipient_id, "post.notification.created", {"post_id": post.id, "actor_id": post.author_id, "unread_count": unread_count})
        await admin_manager.broadcast("admin.moderation.changed", {"post_id": post.id, "action": action.value, "moderation_status": post.moderation_status})
        return

    recipients = await post_service.post_recipient_ids(db, post)
    await manager.send_to_users(recipients, "post.deleted", {"post_id": post.id, "actor_id": None, "moderation": True, "action": action.value, "moderation_status": post.moderation_status})
    await admin_manager.broadcast("admin.moderation.changed", {"post_id": post.id, "action": action.value, "moderation_status": post.moderation_status})
    if action in {ModerationAction.USER_BANNED, ModerationAction.PERMANENT_BAN}:
        await _disconnect_suspended_user(db, post.author_id)


async def _broadcast_post_comment_moderation_action(db: AsyncSession, comment: PostComment, action: ModerationAction) -> None:
    from app.services import posts as post_service

    post = await db.get(Post, comment.post_id)
    if post is None:
        return
    if action == ModerationAction.NONE:
        notifications = await post_service.create_notifications_for_comment(db, post, comment)
        await db.commit()
        recipients = await post_service.comment_recipient_ids(db, post, comment)
        await manager.send_to_users(recipients, "post.comment.created", {"post_id": comment.post_id, "comment_id": comment.id, "actor_id": None, "moderation": True, "moderation_status": comment.moderation_status})
        for notification in notifications:
            unread_count = await post_service.unread_notification_count(db, notification.recipient_id)
            await manager.send_to_user(notification.recipient_id, "post.notification.created", {"post_id": post.id, "actor_id": comment.author_id, "unread_count": unread_count})
        await admin_manager.broadcast("admin.moderation.changed", {"post_id": comment.post_id, "post_comment_id": comment.id, "action": action.value, "moderation_status": comment.moderation_status})
        return

    recipients = await post_service.comment_recipient_ids(db, post, comment)
    await manager.send_to_users(recipients, "post.comment.deleted", {"post_id": comment.post_id, "comment_id": comment.id, "actor_id": None, "moderation": True, "action": action.value, "moderation_status": comment.moderation_status})
    await admin_manager.broadcast("admin.moderation.changed", {"post_id": comment.post_id, "post_comment_id": comment.id, "action": action.value, "moderation_status": comment.moderation_status})
    if action in {ModerationAction.USER_BANNED, ModerationAction.PERMANENT_BAN}:
        await _disconnect_suspended_user(db, comment.author_id)


async def _broadcast_teamup_profile_moderation_action(db: AsyncSession, profile: TeamupProfile, action: ModerationAction) -> None:
    if action == ModerationAction.NONE:
        await manager.send_to_user(profile.user_id, "teamup.profile.updated", {"profile_id": profile.id, "actor_id": None, "moderation": True, "moderation_status": profile.moderation_status})
        await admin_manager.broadcast("admin.moderation.changed", {"teamup_profile_id": profile.id, "action": action.value, "moderation_status": profile.moderation_status})
        return
    await manager.send_to_user(profile.user_id, "teamup.profile.deleted", {"profile_id": profile.id, "actor_id": None, "moderation": True, "action": action.value, "moderation_status": profile.moderation_status})
    await admin_manager.broadcast("admin.moderation.changed", {"teamup_profile_id": profile.id, "action": action.value, "moderation_status": profile.moderation_status})
    if action in {ModerationAction.USER_BANNED, ModerationAction.PERMANENT_BAN}:
        await _disconnect_suspended_user(db, profile.user_id)


async def _broadcast_teamup_comment_moderation_action(db: AsyncSession, comment: TeamupComment, action: ModerationAction) -> None:
    profile = await db.get(TeamupProfile, comment.profile_id)
    if profile is None:
        return
    recipients = {profile.user_id, comment.author_id}
    if action == ModerationAction.NONE:
        await manager.send_to_users(recipients, "teamup.comment.created", {"profile_id": comment.profile_id, "comment_id": comment.id, "actor_id": None, "moderation": True, "moderation_status": comment.moderation_status})
        await admin_manager.broadcast("admin.moderation.changed", {"teamup_profile_id": comment.profile_id, "teamup_comment_id": comment.id, "action": action.value, "moderation_status": comment.moderation_status})
        return
    await manager.send_to_users(recipients, "teamup.comment.deleted", {"profile_id": comment.profile_id, "comment_id": comment.id, "actor_id": None, "moderation": True, "action": action.value, "moderation_status": comment.moderation_status})
    await admin_manager.broadcast("admin.moderation.changed", {"teamup_profile_id": comment.profile_id, "teamup_comment_id": comment.id, "action": action.value, "moderation_status": comment.moderation_status})
    if action in {ModerationAction.USER_BANNED, ModerationAction.PERMANENT_BAN}:
        await _disconnect_suspended_user(db, comment.author_id)


async def _disconnect_suspended_user(db: AsyncSession, user_id: int) -> None:
    user = await db.get(User, user_id)
    if user is None or user.status != UserStatus.DISABLED:
        return
    await manager.disconnect_user(user.id, "account.suspended", {
        "code": "account_suspended",
        "reason": user.ban_reason,
        "banned_until": user.banned_until.isoformat() if user.banned_until else None,
        "permanent": user.banned_until is None,
    })


_RECORD_TARGETS: tuple[tuple[str, str], ...] = (
    ("post_comment_id", "post_comment"),
    ("teamup_comment_id", "teamup_comment"),
    ("teamup_profile_id", "teamup_profile"),
    ("post_id", "post"),
    ("message_id", "message"),
    ("profile_user_id", "profile_cover"),
)


def _record_target(record: ContentModerationRecord) -> tuple[str | None, int | None]:
    for column, target_type in _RECORD_TARGETS:
        value = getattr(record, column)
        if value is not None:
            return target_type, value
    return None, None


async def _remove_record_content(db: AsyncSession, record: ContentModerationRecord) -> Message | None:
    """人工判定违规后处理掉内容。返回被撤回的消息，供调用方广播。"""
    now = datetime.now(UTC)
    target_type, target_id = _record_target(record)
    if target_type == "message":
        message = await db.get(Message, target_id)
        if message is not None and message.recalled_at is None:
            message.recalled_at = now
            message.recalled_by_id = None
            return message
        return None
    model = {"post": Post, "post_comment": PostComment, "teamup_profile": TeamupProfile, "teamup_comment": TeamupComment}.get(target_type or "")
    if model is not None:
        item = await db.get(model, target_id)
        if item is not None and item.deleted_at is None:
            item.deleted_at = now
            item.moderation_status = ModerationStatus.REJECTED.value
    elif target_type == "profile_cover":
        user = await db.get(User, target_id)
        if user is not None and record.content_url and user.profile_cover_url == record.content_url:
            user.profile_cover_url = None
    return None


async def set_human_verdict(
    db: AsyncSession,
    record_id: int,
    *,
    actor_id: int,
    verdict: str,
    category: str | None = None,
    level: int | None = None,
) -> ContentModerationRecord:
    """管理员对一条机器审核记录给出最终结论。

    violation：处理内容并按统一阶梯记违规（机器已经罚过的不重复罚，等级更高时改判）。
    clean：撤销机器因这条记录给出的处罚。已经撤回 / 删除的内容不会自动恢复。
    """
    record = await get_moderation_record(db, record_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Moderation record not found")
    if record.human_verdict is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="这条记录已经复核过了")
    if verdict == "violation" and category not in penalty_service.CATEGORIES:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Unknown violation category")
    now = datetime.now(UTC)
    record.human_verdict = verdict
    record.human_by_id = actor_id
    record.human_at = now
    target_type, target_id = _record_target(record)
    recalled: Message | None = None
    revoked: list[UserViolation] = []
    if verdict == "violation":
        record.human_category = category
        record.human_level = penalty_service.default_level(category) if level is None else level
        recalled = await _remove_record_content(db, record)
        if record.user_id is not None:
            summary = (record.content_text or "").strip()[:120] or ("[图片]" if record.content_type == "image" else None)
            await penalty_service.record_violation(
                db,
                user_id=record.user_id,
                category=category,
                level=record.human_level,
                source="admin",
                target_type=target_type,
                target_id=target_id,
                conversation_id=record.conversation_id,
                moderation_record_id=record.id,
                actor_id=actor_id,
                confidence=record.confidence,
                summary=summary,
                reason=record.reason,
            )
    else:
        result = await db.execute(
            select(UserViolation).where(UserViolation.moderation_record_id == record.id, UserViolation.status == "active")
        )
        for violation in result.scalars().all():
            revoked.append(await penalty_service.revoke_violation(db, violation.id, actor_id=actor_id, reason="人工复核：机器误判"))
    db.add(
        AdminAuditLog(
            actor_id=actor_id,
            action=f"moderation.record.{verdict}",
            target_type="moderation_record",
            target_id=record.id,
            details=_canonical_json({"category": record.human_category, "level": record.human_level, "machine_decision": record.decision}),
        )
    )
    await db.commit()
    await penalty_service.flush_notifications(db)
    for violation in revoked:
        await penalty_service.notify_violation_updated(db, violation, "revoked")
    if recalled is not None:
        await _broadcast_moderation_action(db, recalled, ModerationAction.RECALLED)
    await admin_manager.broadcast("admin.moderation.changed", {"record_id": record.id, "action": f"human_{verdict}"})
    return record


async def list_moderation_records(
    db: AsyncSession,
    decision: str | None = None,
    action: ModerationAction | None = None,
    source: ModerationSource | None = None,
    content_type: str | None = None,
    include_allow: bool = True,
    limit: int = 100,
    offset: int = 0,
    human: str | None = None,
) -> list[ContentModerationRecord]:
    stmt = (
        select(ContentModerationRecord)
        .options(joinedload(ContentModerationRecord.user), joinedload(ContentModerationRecord.message))
        .order_by(ContentModerationRecord.created_at.desc(), ContentModerationRecord.id.desc())
        .offset(offset)
        .limit(limit)
    )
    if decision is not None:
        stmt = stmt.where(ContentModerationRecord.decision == decision)
    elif not include_allow:
        stmt = stmt.where(ContentModerationRecord.decision != "allow")
    if action is not None:
        stmt = stmt.where(ContentModerationRecord.action_taken == action)
    if source is not None:
        stmt = stmt.where(ContentModerationRecord.source == source)
    if content_type is not None:
        stmt = stmt.where(ContentModerationRecord.content_type == content_type)
    if human == "pending":
        stmt = stmt.where(ContentModerationRecord.human_verdict.is_(None))
    elif human == "done":
        stmt = stmt.where(ContentModerationRecord.human_verdict.is_not(None))
    result = await db.execute(stmt)
    records = list(result.scalars().all())
    changed = False
    for record in records:
        message = record.message
        if message is None:
            continue
        metadata = _json_object(message.metadata_json)
        if record.content_type == "text" and not record.content_text:
            content_text = _text_to_moderate(message, metadata)
            if content_text:
                record.content_text = content_text
                changed = True
        elif record.content_type == "image" and not record.content_url:
            image_urls = _image_urls_to_moderate(message, metadata)
            if image_urls:
                record.content_url = image_urls[0]
                changed = True
    if changed:
        await db.commit()
    return records


async def get_moderation_record(db: AsyncSession, record_id: int) -> ContentModerationRecord | None:
    result = await db.execute(
        select(ContentModerationRecord)
        .options(joinedload(ContentModerationRecord.user), joinedload(ContentModerationRecord.message))
        .where(ContentModerationRecord.id == record_id)
    )
    return result.scalar_one_or_none()


async def _record_moderation_error(
    db: AsyncSession,
    message_id: int | None,
    source: ModerationSource,
    report_id: int | None,
    exc: Exception,
    post_id: int | None = None,
    post_topic_id: int | None = None,
    post_comment_id: int | None = None,
    profile_user_id: int | None = None,
    teamup_profile_id: int | None = None,
    teamup_comment_id: int | None = None,
) -> None:
    try:
        message = await db.get(Message, message_id) if message_id is not None else None
        post = await db.get(Post, post_id) if post_id is not None else None
        topic = await db.get(PostTopic, post_topic_id) if post_topic_id is not None else None
        comment = await db.get(PostComment, post_comment_id) if post_comment_id is not None else None
        profile_user = await db.get(User, profile_user_id) if profile_user_id is not None else None
        teamup_profile = await db.get(TeamupProfile, teamup_profile_id) if teamup_profile_id is not None else None
        teamup_comment = await db.get(TeamupComment, teamup_comment_id) if teamup_comment_id is not None else None
        record = ContentModerationRecord(
            source=source,
            message_id=message_id,
            post_id=post_id,
            post_topic_id=post_topic_id,
            post_comment_id=post_comment_id,
            profile_user_id=profile_user_id,
            teamup_profile_id=teamup_profile_id,
            teamup_comment_id=teamup_comment_id,
            report_id=report_id,
            conversation_id=message.conversation_id if message else None,
            user_id=message.sender_id if message else (post.author_id if post else (topic.created_by_id if topic else (comment.author_id if comment else (profile_user.id if profile_user else (teamup_profile.user_id if teamup_profile else (teamup_comment.author_id if teamup_comment else None)))))),
            content_type="unknown",
            decision="error",
            severity=0,
            reason=str(exc),
            action_taken=ModerationAction.ERROR,
            acted_at=datetime.now(UTC),
        )
        db.add(record)
        await db.commit()
    except Exception:
        await db.rollback()
        logger.exception("Failed to record moderation error")


def _message_snapshot(message: Message) -> dict[str, Any]:
    sender = message.sender
    return {
        "id": message.id,
        "conversation_id": message.conversation_id,
        "sender_id": message.sender_id,
        "sender_username": sender.username if sender else None,
        "sender_nickname": sender.nickname if sender else None,
        "type": message.type.value if hasattr(message.type, "value") else str(message.type),
        "content": message.content,
        "metadata": _json_object(message.metadata_json),
        "created_at": message.created_at.isoformat(),
        "recalled_at": message.recalled_at.isoformat() if message.recalled_at else None,
    }


def _post_snapshot(post: Post) -> dict[str, Any]:
    author = post.author
    return {
        "id": post.id,
        "author_id": post.author_id,
        "author_username": author.username if author else None,
        "author_nickname": author.nickname if author else None,
        "visibility": post.visibility.value if hasattr(post.visibility, "value") else str(post.visibility),
        "content": post.content,
        "image_urls": _post_image_urls_to_moderate(post),
        "created_at": post.created_at.isoformat(),
        "deleted_at": post.deleted_at.isoformat() if post.deleted_at else None,
    }


def _post_comment_snapshot(comment: PostComment) -> dict[str, Any]:
    author = comment.author
    return {
        "id": comment.id,
        "post_id": comment.post_id,
        "author_id": comment.author_id,
        "author_username": author.username if author else None,
        "author_nickname": author.nickname if author else None,
        "parent_id": comment.parent_id,
        "content": comment.content,
        "created_at": comment.created_at.isoformat(),
        "deleted_at": comment.deleted_at.isoformat() if comment.deleted_at else None,
    }


def _teamup_profile_snapshot(profile: TeamupProfile) -> dict[str, Any]:
    user = profile.user
    return {
        "id": profile.id,
        "user_id": profile.user_id,
        "author_username": user.username if user else None,
        "author_nickname": user.nickname if user else None,
        "event_id": profile.event_id,
        "headline": profile.headline,
        "intro": profile.intro,
        "contact_note": profile.contact_note,
        "image_urls": _teamup_profile_image_urls(profile),
        "background_url": profile.background_url,
        "created_at": profile.created_at.isoformat(),
        "deleted_at": profile.deleted_at.isoformat() if profile.deleted_at else None,
    }


def _teamup_comment_snapshot(comment: TeamupComment) -> dict[str, Any]:
    author = comment.author
    return {
        "id": comment.id,
        "profile_id": comment.profile_id,
        "author_id": comment.author_id,
        "author_username": author.username if author else None,
        "author_nickname": author.nickname if author else None,
        "parent_id": comment.parent_id,
        "content": comment.content,
        "created_at": comment.created_at.isoformat(),
        "deleted_at": comment.deleted_at.isoformat() if comment.deleted_at else None,
    }


def _decision_rank(value: str) -> int:
    return {"allow": 0, "review": 1, "block": 2, "error": -1}.get(value, 0)


def _safe_int(value: object) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 1


def _json_object(raw: str | None) -> dict[str, Any]:
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
    return [str(item) for item in value] if isinstance(value, list) else []


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
