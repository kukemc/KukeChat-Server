from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from fastapi import HTTPException, status
from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.core.security import hash_password
from app.core.database import release_connection
from app.models.bot import Bot, BotInstallation, BotReview, BotReviewLike, BotReviewReply
from app.models.conversation import Conversation, ConversationMember
from app.models.enums import ConversationType, MemberRole, ModerationStatus, UserStatus
from app.models.message import Message, MessageReaction
from app.models.user import User
from app.schemas.bot import BotCreate, BotDashboardInstallationRead, BotDashboardMetricRead, BotDashboardRatingBucketRead, BotDashboardRead, BotDashboardTrendPointRead, BotInstallationCreate, BotInstallationUpdate, BotReviewCreate, BotReviewRead, BotReviewReplyCreate, BotReviewReplyRead, BotRead, BotSquareStatsRead, BotUpdate
from app.services.conversations import STAFF_ROLES, get_member, require_group_conversation, require_membership
from app.services import moderation as moderation_service
from app.services.upload_paths import require_avatar_upload_url

UTC = timezone.utc
BOT_KEY_PREFIX = "kcb_live"
BOT_KEY_PEPPER = "KukeChatBotKey:v1"
BOT_TITLE = "机器人"
BOT_STATUS_ACTIVE = "active"
BOT_STATUS_DISABLED = "disabled"
BOT_STATUS_DELETED = "deleted"


@dataclass(slots=True)
class BotContext:
    bot: Bot
    user: User


def _now() -> datetime:
    return datetime.now(UTC)


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    return text or None


def _hash_key(key: str) -> str:
    return hashlib.sha256(f"{BOT_KEY_PEPPER}|{key}".encode("utf-8")).hexdigest()


def _new_key(bot_id: int | str = "new") -> tuple[str, str, str]:
    secret = secrets.token_urlsafe(36).replace("-", "").replace("_", "")[:48]
    key = f"{BOT_KEY_PREFIX}_{bot_id}_{secret}"
    return key, key[:32], _hash_key(key)


def _extract_bot_id(key: str) -> int | None:
    parts = key.split("_", 3)
    if len(parts) != 4 or parts[0] != "kcb" or parts[1] != "live":
        return None
    try:
        bot_id = int(parts[2])
    except ValueError:
        return None
    return bot_id if bot_id > 0 else None


def _ensure_active(bot: Bot) -> None:
    if bot.status != BOT_STATUS_ACTIVE:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Bot is disabled")


def _ensure_not_deleted(bot: Bot) -> None:
    if bot.status == BOT_STATUS_DELETED:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Bot not found")


async def _review_moderation_status(db: AsyncSession, user: User, text: str | None, context_id: str) -> str:
    content = (text or "").strip()
    if not content or user.content_moderation_whitelisted:
        return ModerationStatus.APPROVED.value
    # 审核服务可能要几秒：先结束请求的读事务、把数据库连接还回去，不带着连接等
    await release_connection(db)
    try:
        result = await moderation_service._call_text_moderation_for_user(content, user.id, context_id=context_id)
    except Exception:
        return ModerationStatus.PENDING.value
    return ModerationStatus.APPROVED.value if result.get("decision") == "allow" and int(result.get("severity") or 0) <= 0 else ModerationStatus.REJECTED.value


def _visible_review_condition(viewer_id: int):
    return or_(BotReview.moderation_status == ModerationStatus.APPROVED.value, BotReview.user_id == viewer_id)


def _visible_reply_condition(viewer_id: int):
    return or_(BotReviewReply.moderation_status == ModerationStatus.APPROVED.value, BotReviewReply.user_id == viewer_id)


async def create_bot(db: AsyncSession, owner: User, payload: BotCreate) -> tuple[Bot, str]:
    avatar_url = require_avatar_upload_url(payload.avatar_url)
    placeholder_key, placeholder_prefix, placeholder_hash = _new_key("new")
    bot_user = User(
        username=f"bot_pending_{secrets.token_hex(8)}",
        email=None,
        hashed_password=hash_password(secrets.token_urlsafe(48)),
        nickname=payload.name.strip(),
        avatar_url=avatar_url,
        bio=_clean(payload.description),
        profile_title=BOT_TITLE,
        is_bot=True,
        bot_owner_id=owner.id,
        content_moderation_whitelisted=owner.content_moderation_whitelisted,
        status=UserStatus.ACTIVE,
    )
    db.add(bot_user)
    await db.flush()

    bot = Bot(
        user_id=bot_user.id,
        owner_id=owner.id,
        name=payload.name.strip(),
        avatar_url=avatar_url,
        description=_clean(payload.description),
        functions=_clean(payload.functions),
        commands=_clean(payload.commands),
        is_public=payload.is_public,
        status=BOT_STATUS_ACTIVE,
        key_hash=placeholder_hash,
        key_prefix=placeholder_prefix,
        last_key_rotated_at=_now(),
    )
    db.add(bot)
    await db.flush()
    key, key_prefix, key_hash = _new_key(bot.id)
    bot.key_prefix = key_prefix
    bot.key_hash = key_hash
    bot_user.username = f"bot_{bot.id}"
    await db.commit()
    return await get_bot_for_owner(db, bot.id, owner.id), key


async def get_bot(db: AsyncSession, bot_id: int) -> Bot:
    result = await db.execute(
        select(Bot).options(joinedload(Bot.user), joinedload(Bot.owner)).where(Bot.id == bot_id)
    )
    bot = result.scalar_one_or_none()
    if bot is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Bot not found")
    _ensure_not_deleted(bot)
    return bot


async def get_bot_by_user_id(db: AsyncSession, user_id: int) -> Bot:
    """按机器人对应的用户账号（users.is_bot）找机器人；客户端点聊天里的机器人头像时用。"""
    result = await db.execute(
        select(Bot).options(joinedload(Bot.user), joinedload(Bot.owner)).where(Bot.user_id == user_id)
    )
    bot = result.scalar_one_or_none()
    if bot is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Bot not found")
    _ensure_not_deleted(bot)
    return bot


async def get_bot_for_owner(db: AsyncSession, bot_id: int, owner_id: int) -> Bot:
    bot = await get_bot(db, bot_id)
    if bot.owner_id != owner_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only the bot creator can manage this bot")
    return bot


async def list_owner_bots(db: AsyncSession, owner_id: int) -> list[Bot]:
    result = await db.execute(
        select(Bot)
        .options(joinedload(Bot.user), joinedload(Bot.owner))
        .where(Bot.owner_id == owner_id, Bot.status != BOT_STATUS_DELETED)
        .order_by(Bot.updated_at.desc(), Bot.id.desc())
    )
    return list(result.scalars().unique().all())


async def list_public_bots(db: AsyncSession, q: str | None = None, limit: int = 30, offset: int = 0, online_bot_ids: set[int] | None = None) -> tuple[list[Bot], int]:
    conditions = [Bot.is_public.is_(True), Bot.status == BOT_STATUS_ACTIVE]
    keyword = _clean(q)
    if keyword:
        pattern = f"%{keyword}%"
        conditions.append(or_(Bot.name.ilike(pattern), Bot.description.ilike(pattern), Bot.functions.ilike(pattern), Bot.commands.ilike(pattern)))
    approved_reviews = and_(BotReview.moderation_status == ModerationStatus.APPROVED.value, BotReview.deleted_at.is_(None))
    online_rank = case((Bot.id.in_(online_bot_ids or {-1}), 1), else_=0)
    review_stats = (
        select(BotReview.bot_id.label("bot_id"), func.avg(BotReview.rating).label("rating_average"), func.count(BotReview.id).label("review_count"))
        .where(approved_reviews)
        .group_by(BotReview.bot_id)
        .subquery()
    )
    install_stats = (
        select(BotInstallation.bot_id.label("bot_id"), func.count(BotInstallation.id).label("install_count"))
        .where(BotInstallation.enabled.is_(True))
        .group_by(BotInstallation.bot_id)
        .subquery()
    )
    total_result = await db.execute(select(func.count(Bot.id)).where(*conditions))
    result = await db.execute(
        select(Bot)
        .outerjoin(review_stats, review_stats.c.bot_id == Bot.id)
        .outerjoin(install_stats, install_stats.c.bot_id == Bot.id)
        .options(joinedload(Bot.user), joinedload(Bot.owner))
        .where(*conditions)
        .order_by(
            online_rank.desc(),
            func.coalesce(review_stats.c.rating_average, 0).desc(),
            func.coalesce(review_stats.c.review_count, 0).desc(),
            func.coalesce(install_stats.c.install_count, 0).desc(),
            Bot.updated_at.desc(),
            Bot.id.desc(),
        )
        .offset(offset)
        .limit(limit)
    )
    return list(result.scalars().unique().all()), int(total_result.scalar_one() or 0)


async def bot_square_stats(db: AsyncSession, online_bot_ids: set[int] | None = None) -> BotSquareStatsRead:
    public_active = [Bot.is_public.is_(True), Bot.status == BOT_STATUS_ACTIVE]
    total_result = await db.execute(select(func.count(Bot.id)).where(*public_active))
    online_result = await db.execute(select(func.count(Bot.id)).where(*public_active, Bot.id.in_(online_bot_ids or {-1})))
    installs_result = await db.execute(
        select(func.count(BotInstallation.id), func.count(func.distinct(BotInstallation.conversation_id)))
        .join(Bot, Bot.id == BotInstallation.bot_id)
        .where(*public_active, BotInstallation.enabled.is_(True))
    )
    reviews_result = await db.execute(
        select(func.count(BotReview.id))
        .join(Bot, Bot.id == BotReview.bot_id)
        .where(*public_active, BotReview.moderation_status == ModerationStatus.APPROVED.value, BotReview.deleted_at.is_(None))
    )
    total_installs, installed_groups = installs_result.one()
    return BotSquareStatsRead(
        total_bots=int(total_result.scalar_one() or 0),
        online_bots=int(online_result.scalar_one() or 0),
        installed_groups=int(installed_groups or 0),
        total_installs=int(total_installs or 0),
        total_reviews=int(reviews_result.scalar_one() or 0),
    )


async def build_bot_dashboard(db: AsyncSession, bot_id: int, owner_id: int, bot_read: BotRead) -> BotDashboardRead:
    bot = await get_bot_for_owner(db, bot_id, owner_id)
    today = datetime.now(UTC).date()
    start_date = today - timedelta(days=6)
    start_dt = datetime.combine(start_date, datetime.min.time(), tzinfo=UTC)
    today_start = datetime.combine(today, datetime.min.time(), tzinfo=UTC)

    install_result = await db.execute(
        select(BotInstallation, Conversation)
        .join(Conversation, Conversation.id == BotInstallation.conversation_id)
        .where(BotInstallation.bot_id == bot.id)
        .order_by(BotInstallation.updated_at.desc(), BotInstallation.id.desc())
    )
    install_rows = install_result.all()
    conversation_ids = [conversation.id for _, conversation in install_rows]

    sent_total_result = await db.execute(select(func.count(Message.id)).where(Message.sender_id == bot.user_id))
    sent_today_result = await db.execute(select(func.count(Message.id)).where(Message.sender_id == bot.user_id, Message.created_at >= today_start))
    reaction_result = await db.execute(
        select(func.count(MessageReaction.id)).join(Message, Message.id == MessageReaction.message_id).where(Message.sender_id == bot.user_id)
    )

    received_total = 0
    received_today = 0
    if conversation_ids:
        received_total_result = await db.execute(select(func.count(Message.id)).where(Message.conversation_id.in_(conversation_ids), Message.sender_id != bot.user_id))
        received_today_result = await db.execute(select(func.count(Message.id)).where(Message.conversation_id.in_(conversation_ids), Message.sender_id != bot.user_id, Message.created_at >= today_start))
        received_total = int(received_total_result.scalar_one() or 0)
        received_today = int(received_today_result.scalar_one() or 0)

    member_counts: dict[int, int] = {}
    sent_by_conversation: dict[int, int] = {}
    received_by_conversation: dict[int, int] = {}
    if conversation_ids:
        member_count_result = await db.execute(
            select(ConversationMember.conversation_id, func.count(ConversationMember.id)).where(ConversationMember.conversation_id.in_(conversation_ids)).group_by(ConversationMember.conversation_id)
        )
        member_counts = {int(conversation_id): int(count) for conversation_id, count in member_count_result.all()}
        sent_conversation_result = await db.execute(
            select(Message.conversation_id, func.count(Message.id)).where(Message.conversation_id.in_(conversation_ids), Message.sender_id == bot.user_id).group_by(Message.conversation_id)
        )
        sent_by_conversation = {int(conversation_id): int(count) for conversation_id, count in sent_conversation_result.all()}
        received_conversation_result = await db.execute(
            select(Message.conversation_id, func.count(Message.id)).where(Message.conversation_id.in_(conversation_ids), Message.sender_id != bot.user_id).group_by(Message.conversation_id)
        )
        received_by_conversation = {int(conversation_id): int(count) for conversation_id, count in received_conversation_result.all()}

    trend_map: dict[date, dict[str, int]] = {start_date + timedelta(days=index): {"sent": 0, "received": 0} for index in range(7)}
    sent_trend_result = await db.execute(
        select(func.date(Message.created_at), func.count(Message.id)).where(Message.sender_id == bot.user_id, Message.created_at >= start_dt).group_by(func.date(Message.created_at))
    )
    for day_value, count in sent_trend_result.all():
        day = date.fromisoformat(str(day_value)) if not isinstance(day_value, date) else day_value
        if day in trend_map:
            trend_map[day]["sent"] = int(count)
    if conversation_ids:
        received_trend_result = await db.execute(
            select(func.date(Message.created_at), func.count(Message.id)).where(Message.conversation_id.in_(conversation_ids), Message.sender_id != bot.user_id, Message.created_at >= start_dt).group_by(func.date(Message.created_at))
        )
        for day_value, count in received_trend_result.all():
            day = date.fromisoformat(str(day_value)) if not isinstance(day_value, date) else day_value
            if day in trend_map:
                trend_map[day]["received"] = int(count)

    rating_result = await db.execute(
        select(BotReview.rating, func.count(BotReview.id)).where(BotReview.bot_id == bot.id, BotReview.moderation_status == ModerationStatus.APPROVED.value, BotReview.deleted_at.is_(None)).group_by(BotReview.rating)
    )
    rating_counts = {float(rating): int(count) for rating, count in rating_result.all()}

    active_installs = sum(1 for installation, _ in install_rows if installation.enabled)
    installations = [
        BotDashboardInstallationRead(
            conversation_id=conversation.id,
            title=conversation.title or f"群聊 {conversation.id}",
            avatar_url=conversation.avatar_url,
            enabled=installation.enabled,
            receive_messages=installation.receive_messages,
            receive_member_events=installation.receive_member_events,
            installed_at=installation.created_at,
            updated_at=installation.updated_at,
            sent_messages=sent_by_conversation.get(conversation.id, 0),
            received_messages=received_by_conversation.get(conversation.id, 0),
            member_count=member_counts.get(conversation.id, 0),
        )
        for installation, conversation in install_rows
    ]

    return BotDashboardRead(
        bot=bot_read,
        metrics=BotDashboardMetricRead(
            total_sent_messages=int(sent_total_result.scalar_one() or 0),
            total_received_messages=received_total,
            today_sent_messages=int(sent_today_result.scalar_one() or 0),
            today_received_messages=received_today,
            active_installs=active_installs,
            disabled_installs=max(0, len(install_rows) - active_installs),
            installed_groups=len(install_rows),
            total_members_reached=sum(member_counts.values()),
            review_count=bot_read.review_count,
            rating_average=bot_read.rating_average,
            reaction_count=int(reaction_result.scalar_one() or 0),
        ),
        trend=[BotDashboardTrendPointRead(date=day.isoformat(), sent=values["sent"], received=values["received"]) for day, values in trend_map.items()],
        rating_distribution=[BotDashboardRatingBucketRead(rating=rating, count=rating_counts.get(rating, 0)) for rating in [5, 4.5, 4, 3.5, 3, 2.5, 2, 1.5, 1]],
        installations=installations,
    )


async def update_bot(db: AsyncSession, bot_id: int, owner: User, payload: BotUpdate) -> Bot:
    bot = await get_bot_for_owner(db, bot_id, owner.id)
    updates = payload.model_dump(exclude_unset=True)
    if "avatar_url" in updates:
        updates["avatar_url"] = require_avatar_upload_url(updates["avatar_url"])
    for field in ("description", "functions", "commands"):
        if field in updates:
            updates[field] = _clean(updates[field])
    if "name" in updates and updates["name"] is not None:
        updates["name"] = updates["name"].strip()
    for field, value in updates.items():
        setattr(bot, field, value)
    if "name" in updates:
        bot.user.nickname = updates["name"]
    if "avatar_url" in updates:
        bot.user.avatar_url = updates["avatar_url"]
    if "description" in updates:
        bot.user.bio = updates["description"]
    if "status" in updates:
        bot.user.status = UserStatus.DISABLED if updates["status"] == BOT_STATUS_DISABLED else UserStatus.ACTIVE
    bot.updated_at = _now()
    await db.commit()
    return await get_bot(db, bot.id)


async def rotate_bot_key(db: AsyncSession, bot_id: int, owner_id: int) -> tuple[Bot, str]:
    bot = await get_bot_for_owner(db, bot_id, owner_id)
    key, key_prefix, key_hash = _new_key(bot.id)
    bot.key_prefix = key_prefix
    bot.key_hash = key_hash
    bot.last_key_rotated_at = _now()
    bot.updated_at = bot.last_key_rotated_at
    await db.commit()
    return await get_bot(db, bot.id), key


async def delete_bot(db: AsyncSession, bot_id: int, owner: User) -> tuple[Bot, list[int]]:
    bot = await get_bot_for_owner(db, bot_id, owner.id)
    bot.status = BOT_STATUS_DELETED
    bot.is_public = False
    bot.key_hash = "deleted"
    bot.key_prefix = "deleted"
    bot.updated_at = _now()
    bot.user.status = UserStatus.DISABLED
    installation_result = await db.execute(select(BotInstallation).where(BotInstallation.bot_id == bot.id))
    installations = list(installation_result.scalars().all())
    conversation_ids = [installation.conversation_id for installation in installations]
    for installation in installations:
        member_result = await db.execute(
            select(ConversationMember).where(
                ConversationMember.conversation_id == installation.conversation_id,
                ConversationMember.user_id == bot.user_id,
            )
        )
        member = member_result.scalar_one_or_none()
        if member is not None:
            await db.delete(member)
        conversation = await db.get(Conversation, installation.conversation_id)
        if conversation is not None:
            conversation.updated_at = _now()
        await db.delete(installation)
    review_result = await db.execute(select(BotReview).where(BotReview.bot_id == bot.id))
    reviews = list(review_result.scalars().all())
    review_ids = [review.id for review in reviews]
    if review_ids:
        reply_result = await db.execute(select(BotReviewReply).where(BotReviewReply.review_id.in_(review_ids)))
        replies = list(reply_result.scalars().all())
        reply_ids = [reply.id for reply in replies]
        like_conditions = [BotReviewLike.review_id.in_(review_ids)]
        if reply_ids:
            like_conditions.append(BotReviewLike.reply_id.in_(reply_ids))
        like_result = await db.execute(select(BotReviewLike).where(or_(*like_conditions)))
        for like in like_result.scalars().all():
            await db.delete(like)
        for reply in replies:
            await db.delete(reply)
        for review in reviews:
            await db.delete(review)
    await db.commit()
    return bot, conversation_ids


async def bot_review_stats(db: AsyncSession, bot_id: int, user_id: int | None = None) -> tuple[float | None, int, BotReview | None]:
    stats_result = await db.execute(
        select(func.avg(BotReview.rating), func.count(BotReview.id)).where(BotReview.bot_id == bot_id, BotReview.moderation_status == ModerationStatus.APPROVED.value, BotReview.deleted_at.is_(None))
    )
    average, count = stats_result.one()
    my_review: BotReview | None = None
    if user_id is not None:
        review_result = await db.execute(
            select(BotReview).options(joinedload(BotReview.user)).where(BotReview.bot_id == bot_id, BotReview.user_id == user_id, BotReview.deleted_at.is_(None))
        )
        my_review = review_result.scalar_one_or_none()
    return (round(float(average), 1) if average is not None else None, int(count or 0), my_review)


async def bot_install_count(db: AsyncSession, bot_id: int) -> int:
    result = await db.execute(select(func.count(BotInstallation.id)).where(BotInstallation.bot_id == bot_id, BotInstallation.enabled.is_(True)))
    return int(result.scalar_one() or 0)


async def _bot_review_like_counts(db: AsyncSession, review_ids: list[int], viewer_id: int) -> tuple[dict[int, int], set[int]]:
    if not review_ids:
        return {}, set()
    count_result = await db.execute(select(BotReviewLike.review_id, func.count(BotReviewLike.id)).where(BotReviewLike.review_id.in_(review_ids)).group_by(BotReviewLike.review_id))
    liked_result = await db.execute(select(BotReviewLike.review_id).where(BotReviewLike.review_id.in_(review_ids), BotReviewLike.user_id == viewer_id))
    return {int(review_id): int(count) for review_id, count in count_result.all() if review_id is not None}, {int(review_id) for (review_id,) in liked_result.all() if review_id is not None}


async def _bot_reply_like_counts(db: AsyncSession, reply_ids: list[int], viewer_id: int) -> tuple[dict[int, int], set[int]]:
    if not reply_ids:
        return {}, set()
    count_result = await db.execute(select(BotReviewLike.reply_id, func.count(BotReviewLike.id)).where(BotReviewLike.reply_id.in_(reply_ids)).group_by(BotReviewLike.reply_id))
    liked_result = await db.execute(select(BotReviewLike.reply_id).where(BotReviewLike.reply_id.in_(reply_ids), BotReviewLike.user_id == viewer_id))
    return {int(reply_id): int(count) for reply_id, count in count_result.all() if reply_id is not None}, {int(reply_id) for (reply_id,) in liked_result.all() if reply_id is not None}


async def _reply_parent_authors(db: AsyncSession, replies: list[BotReviewReply]) -> dict[int, User]:
    parent_ids = [reply.parent_id for reply in replies if reply.parent_id]
    if not parent_ids:
        return {}
    result = await db.execute(select(BotReviewReply).options(joinedload(BotReviewReply.user)).where(BotReviewReply.id.in_(parent_ids)))
    return {reply.id: reply.user for reply in result.scalars().unique().all() if reply.user is not None}


async def build_bot_review_reads(db: AsyncSession, reviews: list[BotReview], viewer_id: int) -> list[BotReviewRead]:
    review_ids = [review.id for review in reviews]
    review_like_counts, liked_review_ids = await _bot_review_like_counts(db, review_ids, viewer_id)
    if not review_ids:
        return []
    reply_result = await db.execute(
        select(BotReviewReply)
        .options(joinedload(BotReviewReply.user), joinedload(BotReviewReply.parent).joinedload(BotReviewReply.user))
        .where(BotReviewReply.review_id.in_(review_ids), BotReviewReply.deleted_at.is_(None), _visible_reply_condition(viewer_id))
        .order_by(BotReviewReply.created_at.asc(), BotReviewReply.id.asc())
    )
    replies = list(reply_result.scalars().unique().all())
    reply_like_counts, liked_reply_ids = await _bot_reply_like_counts(db, [reply.id for reply in replies], viewer_id)
    parent_authors = await _reply_parent_authors(db, replies)
    replies_by_review: dict[int, list[BotReviewReplyRead]] = {review_id: [] for review_id in review_ids}
    for reply in replies:
        replies_by_review.setdefault(reply.review_id, []).append(
            BotReviewReplyRead(
                id=reply.id,
                review_id=reply.review_id,
                user_id=reply.user_id,
                parent_id=reply.parent_id,
                parent_author=parent_authors.get(reply.parent_id) if reply.parent_id else None,
                content=reply.content,
                moderation_status=reply.moderation_status,
                created_at=reply.created_at,
                updated_at=reply.updated_at,
                user=reply.user,
                like_count=reply_like_counts.get(reply.id, 0),
                liked_by_me=reply.id in liked_reply_ids,
            )
        )
    return [
            BotReviewRead(
            id=review.id,
            bot_id=review.bot_id,
            user_id=review.user_id,
            rating=review.rating,
            content=review.content,
            moderation_status=review.moderation_status,
            created_at=review.created_at,
            updated_at=review.updated_at,
            user=review.user,
            like_count=review_like_counts.get(review.id, 0),
            liked_by_me=review.id in liked_review_ids,
            replies=replies_by_review.get(review.id, []),
        )
        for review in reviews
    ]


async def list_bot_reviews(db: AsyncSession, bot_id: int, current_user_id: int) -> list[BotReviewRead]:
    bot = await get_bot(db, bot_id)
    if not bot.is_public and bot.owner_id != current_user_id:
        await get_bot_for_owner(db, bot_id, current_user_id)
    result = await db.execute(
        select(BotReview)
        .options(joinedload(BotReview.user))
        .where(BotReview.bot_id == bot_id, BotReview.deleted_at.is_(None), _visible_review_condition(current_user_id))
        .order_by(BotReview.updated_at.desc(), BotReview.id.desc())
    )
    return await build_bot_review_reads(db, list(result.scalars().unique().all()), current_user_id)


async def upsert_bot_review(db: AsyncSession, bot_id: int, user: User, payload: BotReviewCreate) -> BotReview:
    bot = await get_bot(db, bot_id)
    if bot.status != BOT_STATUS_ACTIVE:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Bot is disabled")
    if user.id == bot.owner_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Bot creator cannot review their own bot")
    result = await db.execute(
        select(BotReview).options(joinedload(BotReview.user)).where(BotReview.bot_id == bot_id, BotReview.user_id == user.id, BotReview.deleted_at.is_(None))
    )
    review = result.scalar_one_or_none()
    moderation_status = await _review_moderation_status(db, user, payload.content, context_id=f"bot_review:{bot_id}:{user.id}")
    if review is None:
        review = BotReview(bot_id=bot_id, user_id=user.id, rating=payload.rating, content=payload.content, moderation_status=moderation_status)
        if moderation_status == ModerationStatus.REJECTED.value:
            review.deleted_at = _now()
        db.add(review)
    else:
        review.rating = payload.rating
        review.content = payload.content
        review.moderation_status = moderation_status
        review.deleted_at = None if moderation_status != ModerationStatus.REJECTED.value else _now()
        review.updated_at = _now()
    await db.commit()
    review_result = await db.execute(select(BotReview).options(joinedload(BotReview.user)).where(BotReview.id == review.id))
    return review_result.scalar_one()


async def create_bot_review_reply(db: AsyncSession, bot_id: int, review_id: int, user: User, payload: BotReviewReplyCreate) -> BotReviewRead:
    await get_bot(db, bot_id)
    review_result = await db.execute(select(BotReview).where(BotReview.id == review_id, BotReview.bot_id == bot_id))
    review = review_result.scalar_one_or_none()
    if review is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Review not found")
    if payload.parent_id is not None:
        parent_result = await db.execute(select(BotReviewReply).where(BotReviewReply.id == payload.parent_id, BotReviewReply.review_id == review_id))
        if parent_result.scalar_one_or_none() is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Parent reply not found")
    moderation_status = await _review_moderation_status(db, user, payload.content, context_id=f"bot_review_reply:{bot_id}:{review_id}:{user.id}")
    db.add(BotReviewReply(review_id=review_id, user_id=user.id, parent_id=payload.parent_id, content=payload.content, moderation_status=moderation_status, deleted_at=_now() if moderation_status == ModerationStatus.REJECTED.value else None))
    await db.commit()
    review_reload = await db.execute(select(BotReview).options(joinedload(BotReview.user)).where(BotReview.id == review_id))
    return (await build_bot_review_reads(db, [review_reload.scalar_one()], user.id))[0]


async def toggle_bot_review_like(db: AsyncSession, bot_id: int, review_id: int, user_id: int) -> tuple[bool, int]:
    review_result = await db.execute(select(BotReview).where(BotReview.id == review_id, BotReview.bot_id == bot_id))
    if review_result.scalar_one_or_none() is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Review not found")
    existing = await db.execute(select(BotReviewLike).where(BotReviewLike.review_id == review_id, BotReviewLike.user_id == user_id))
    like = existing.scalar_one_or_none()
    liked = like is None
    if like is None:
        db.add(BotReviewLike(review_id=review_id, user_id=user_id))
    else:
        await db.delete(like)
    await db.commit()
    count = await db.execute(select(func.count(BotReviewLike.id)).where(BotReviewLike.review_id == review_id))
    return liked, int(count.scalar_one() or 0)


async def toggle_bot_review_reply_like(db: AsyncSession, bot_id: int, review_id: int, reply_id: int, user_id: int) -> tuple[bool, int]:
    reply_result = await db.execute(select(BotReviewReply).join(BotReview, BotReview.id == BotReviewReply.review_id).where(BotReview.id == review_id, BotReview.bot_id == bot_id, BotReviewReply.id == reply_id))
    if reply_result.scalar_one_or_none() is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Reply not found")
    existing = await db.execute(select(BotReviewLike).where(BotReviewLike.reply_id == reply_id, BotReviewLike.user_id == user_id))
    like = existing.scalar_one_or_none()
    liked = like is None
    if like is None:
        db.add(BotReviewLike(reply_id=reply_id, user_id=user_id))
    else:
        await db.delete(like)
    await db.commit()
    count = await db.execute(select(func.count(BotReviewLike.id)).where(BotReviewLike.reply_id == reply_id))
    return liked, int(count.scalar_one() or 0)


async def authenticate_bot_key(db: AsyncSession, raw_key: str) -> BotContext:
    bot_id = _extract_bot_id(raw_key.strip())
    if bot_id is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid bot key")
    result = await db.execute(
        select(Bot)
        .options(joinedload(Bot.user), joinedload(Bot.owner))
        .where(Bot.id == bot_id)
    )
    bot = result.scalar_one_or_none()
    if bot is None or not secrets.compare_digest(bot.key_hash, _hash_key(raw_key.strip())):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid bot key")
    _ensure_active(bot)
    if bot.user.status != UserStatus.ACTIVE:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Bot user is disabled")
    return BotContext(bot=bot, user=bot.user)


async def require_installation(db: AsyncSession, bot: Bot, conversation_id: int, *, require_enabled: bool = True) -> BotInstallation:
    result = await db.execute(
        select(BotInstallation).where(BotInstallation.bot_id == bot.id, BotInstallation.conversation_id == conversation_id)
    )
    installation = result.scalar_one_or_none()
    if installation is None or (require_enabled and not installation.enabled):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Bot is not installed in this conversation")
    return installation


async def install_bot(db: AsyncSession, bot_id: int, actor: User, payload: BotInstallationCreate) -> BotInstallation:
    bot = await get_bot(db, bot_id)
    _ensure_active(bot)
    if not bot.is_public and bot.owner_id != actor.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="This bot is not public")
    conversation = await require_group_conversation(db, payload.conversation_id)
    actor_member = await get_member(db, payload.conversation_id, actor.id)
    if actor_member.role not in STAFF_ROLES:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only group owners or admins can add bots")

    result = await db.execute(
        select(BotInstallation).where(BotInstallation.bot_id == bot.id, BotInstallation.conversation_id == payload.conversation_id)
    )
    installation = result.scalar_one_or_none()
    if installation is None:
        installation = BotInstallation(
            bot_id=bot.id,
            conversation_id=payload.conversation_id,
            installed_by_id=actor.id,
            enabled=True,
            receive_messages=payload.receive_messages,
            receive_member_events=payload.receive_member_events,
        )
        db.add(installation)
    else:
        installation.enabled = True
        installation.receive_messages = payload.receive_messages
        installation.receive_member_events = payload.receive_member_events
        installation.installed_by_id = actor.id
        installation.updated_at = _now()

    member_result = await db.execute(
        select(ConversationMember).where(
            ConversationMember.conversation_id == payload.conversation_id,
            ConversationMember.user_id == bot.user_id,
        )
    )
    if member_result.scalar_one_or_none() is None:
        from app.services.conversations import ensure_capacity

        await ensure_capacity(db, conversation, 1)
        db.add(
            ConversationMember(
                conversation_id=payload.conversation_id,
                user_id=bot.user_id,
                role=MemberRole.MEMBER,
                title=BOT_TITLE,
            )
        )
    conversation.updated_at = _now()
    await db.commit()
    return await get_installation(db, bot.id, payload.conversation_id)


async def get_installation(db: AsyncSession, bot_id: int, conversation_id: int) -> BotInstallation:
    result = await db.execute(
        select(BotInstallation)
        .options(joinedload(BotInstallation.bot).joinedload(Bot.user), joinedload(BotInstallation.bot).joinedload(Bot.owner))
        .where(BotInstallation.bot_id == bot_id, BotInstallation.conversation_id == conversation_id)
    )
    installation = result.scalar_one_or_none()
    if installation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Bot installation not found")
    return installation


async def update_installation(db: AsyncSession, bot_id: int, conversation_id: int, actor: User, payload: BotInstallationUpdate) -> BotInstallation:
    installation = await get_installation(db, bot_id, conversation_id)
    if installation.bot.owner_id != actor.id:
        actor_member = await get_member(db, conversation_id, actor.id)
        if actor_member.role not in STAFF_ROLES:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only group staff or bot creator can update this installation")
    updates = payload.model_dump(exclude_unset=True)
    for field, value in updates.items():
        setattr(installation, field, value)
    if updates:
        installation.updated_at = _now()
        await db.commit()
    return await get_installation(db, bot_id, conversation_id)


async def remove_installation(db: AsyncSession, bot_id: int, conversation_id: int, actor: User) -> BotInstallation:
    installation = await get_installation(db, bot_id, conversation_id)
    if installation.bot.owner_id != actor.id:
        actor_member = await get_member(db, conversation_id, actor.id)
        if actor_member.role not in STAFF_ROLES:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only group staff or bot creator can remove this bot")
    member_result = await db.execute(
        select(ConversationMember).where(
            ConversationMember.conversation_id == conversation_id,
            ConversationMember.user_id == installation.bot.user_id,
        )
    )
    member = member_result.scalar_one_or_none()
    if member is not None:
        await db.delete(member)
    conversation = await db.get(Conversation, conversation_id)
    if conversation is not None:
        conversation.updated_at = _now()
    await db.delete(installation)
    await db.commit()
    return installation


async def list_conversation_bots(db: AsyncSession, conversation_id: int, actor_id: int) -> list[BotInstallation]:
    await get_member(db, conversation_id, actor_id)
    result = await db.execute(
        select(BotInstallation)
        .options(joinedload(BotInstallation.bot).joinedload(Bot.user), joinedload(BotInstallation.bot).joinedload(Bot.owner))
        .where(BotInstallation.conversation_id == conversation_id)
        .order_by(BotInstallation.created_at.desc(), BotInstallation.id.desc())
    )
    return list(result.scalars().unique().all())


async def list_bot_installations(db: AsyncSession, bot_id: int, owner_id: int) -> list[BotInstallation]:
    bot = await get_bot_for_owner(db, bot_id, owner_id)
    result = await db.execute(
        select(BotInstallation)
        .options(joinedload(BotInstallation.bot).joinedload(Bot.user), joinedload(BotInstallation.bot).joinedload(Bot.owner))
        .where(BotInstallation.bot_id == bot.id)
        .order_by(BotInstallation.created_at.desc(), BotInstallation.id.desc())
    )
    return list(result.scalars().unique().all())


async def bot_conversation_ids_for_message_event(db: AsyncSession, conversation_id: int, sender_id: int | None = None) -> list[int]:
    conversation = await db.get(Conversation, conversation_id)
    if conversation is None:
        return []
    if conversation.type == ConversationType.DIRECT:
        result = await db.execute(
            select(Bot.id)
            .join(ConversationMember, ConversationMember.user_id == Bot.user_id)
            .where(
                ConversationMember.conversation_id == conversation_id,
                Bot.status == BOT_STATUS_ACTIVE,
            )
        )
    else:
        result = await db.execute(
            select(Bot.id)
            .join(BotInstallation, BotInstallation.bot_id == Bot.id)
            .where(
                BotInstallation.conversation_id == conversation_id,
                BotInstallation.enabled.is_(True),
                BotInstallation.receive_messages.is_(True),
                Bot.status == BOT_STATUS_ACTIVE,
            )
        )
    bot_ids = list(result.scalars().all())
    if sender_id is None or not bot_ids:
        return bot_ids
    sender_bot_result = await db.execute(select(Bot.id).where(Bot.user_id == sender_id))
    sender_bot_id = sender_bot_result.scalar_one_or_none()
    if sender_bot_id is None:
        return bot_ids
    return [bot_id for bot_id in bot_ids if bot_id != sender_bot_id]


async def bot_ids_for_member_event(db: AsyncSession, conversation_id: int) -> list[int]:
    result = await db.execute(
        select(Bot.id)
        .join(BotInstallation, BotInstallation.bot_id == Bot.id)
        .where(
            BotInstallation.conversation_id == conversation_id,
            BotInstallation.enabled.is_(True),
            BotInstallation.receive_member_events.is_(True),
            Bot.status == BOT_STATUS_ACTIVE,
        )
    )
    return list(result.scalars().all())


async def validate_bot_can_use_conversation(db: AsyncSession, context: BotContext, conversation_id: int) -> BotInstallation:
    await require_installation(db, context.bot, conversation_id, require_enabled=True)
    conversation = await db.get(Conversation, conversation_id)
    if conversation is None or conversation.type != ConversationType.GROUP:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")
    return await require_installation(db, context.bot, conversation_id, require_enabled=True)


async def validate_bot_can_use_message_conversation(db: AsyncSession, context: BotContext, conversation_id: int) -> None:
    conversation = await db.get(Conversation, conversation_id)
    if conversation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")
    if conversation.type == ConversationType.DIRECT:
        await require_membership(db, conversation_id, context.user.id)
        return
    await validate_bot_can_use_conversation(db, context, conversation_id)


async def public_bot_count(db: AsyncSession, q: str | None = None) -> int:
    conditions = [Bot.is_public.is_(True), Bot.status == BOT_STATUS_ACTIVE]
    keyword = _clean(q)
    if keyword:
        pattern = f"%{keyword}%"
        conditions.append(or_(Bot.name.ilike(pattern), Bot.description.ilike(pattern), Bot.functions.ilike(pattern), Bot.commands.ilike(pattern)))
    result = await db.execute(select(func.count(Bot.id)).where(and_(*conditions)))
    return int(result.scalar_one() or 0)
