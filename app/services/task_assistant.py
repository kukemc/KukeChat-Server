from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import AsyncSessionLocal
from app.core.security import hash_password
from app.models.bot import Bot
from app.models.enums import MessageType, PlatformRole, UserStatus
from app.models.user import User
from app.websocket.manager import manager

logger = logging.getLogger(__name__)
UTC = timezone.utc

ASSISTANT_USERNAME = "task_assistant"
ASSISTANT_NICKNAME = "任务助手"
ASSISTANT_TITLE = "机器人"
ASSISTANT_DESCRIPTION = "任务助手会向你推送任务指派、完成、变更与到期提醒。"
ASSISTANT_KEY_PREFIX = "kcb_live"
ASSISTANT_KEY_PEPPER = "KukeChatBotKey:v1"

_assistant_user_id: int | None = None


def _now() -> datetime:
    return datetime.now(UTC)


def _hash_key(key: str) -> str:
    return hashlib.sha256(f"{ASSISTANT_KEY_PEPPER}|{key}".encode("utf-8")).hexdigest()


async def ensure_task_assistant_bot(db: AsyncSession) -> User:
    """Idempotently create (or fetch) the shared Task Assistant system bot user."""
    global _assistant_user_id

    result = await db.execute(select(User).where(User.username == ASSISTANT_USERNAME, User.is_bot.is_(True)))
    bot_user = result.scalar_one_or_none()
    if bot_user is not None:
        _assistant_user_id = bot_user.id
        return bot_user

    admin_result = await db.execute(
        select(User).where(User.platform_role == PlatformRole.ADMIN).order_by(User.id.asc()).limit(1)
    )
    admin = admin_result.scalar_one_or_none()

    bot_user = User(
        username=f"task_assistant_pending_{secrets.token_hex(6)}",
        email=None,
        hashed_password=hash_password(secrets.token_urlsafe(48)),
        nickname=ASSISTANT_NICKNAME,
        bio=ASSISTANT_DESCRIPTION,
        profile_title=ASSISTANT_TITLE,
        is_bot=True,
        bot_owner_id=admin.id if admin is not None else None,
        content_moderation_whitelisted=True,
        status=UserStatus.ACTIVE,
    )
    db.add(bot_user)
    await db.flush()
    if bot_user.bot_owner_id is None:
        bot_user.bot_owner_id = bot_user.id

    placeholder_key = f"{ASSISTANT_KEY_PREFIX}_new_{secrets.token_hex(16)}"
    bot = Bot(
        user_id=bot_user.id,
        owner_id=bot_user.bot_owner_id,
        name=ASSISTANT_NICKNAME,
        description=ASSISTANT_DESCRIPTION,
        functions="任务通知与提醒",
        is_public=False,
        status="active",
        key_hash=_hash_key(placeholder_key),
        key_prefix=placeholder_key[:32],
        last_key_rotated_at=_now(),
    )
    db.add(bot)
    await db.flush()
    bot_user.username = ASSISTANT_USERNAME
    await db.commit()
    await db.refresh(bot_user)
    _assistant_user_id = bot_user.id
    logger.info("Task assistant bot ensured: user_id=%s", bot_user.id)
    return bot_user


async def get_assistant_user(db: AsyncSession) -> User:
    global _assistant_user_id
    if _assistant_user_id is not None:
        bot_user = await db.get(User, _assistant_user_id)
        if bot_user is not None:
            return bot_user
    return await ensure_task_assistant_bot(db)


async def send_assistant_message(
    db: AsyncSession,
    recipient_id: int,
    content: str,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Send a Task Assistant direct message to a single user and broadcast it in realtime."""
    from app.services import conversations as conversation_service
    from app.services import messages as message_service

    bot_user = await get_assistant_user(db)
    if bot_user.id == recipient_id:
        return
    recipient = await db.get(User, recipient_id)
    if recipient is None or recipient.is_bot:
        return
    try:
        conversation = await conversation_service.get_or_create_direct_conversation(
            db, bot_user, recipient_id, temporary=True
        )
        message = await message_service.send_message(
            db, conversation.id, bot_user, content, MessageType.TEXT, metadata
        )
    except Exception:
        logger.exception("Failed to send task assistant message to user %s", recipient_id)
        return

    try:
        data = await message_service.build_message_read(db, message, recipient_id)
        await manager.send_to_user(recipient_id, "message.created", data)
    except Exception:
        logger.exception("Failed to broadcast task assistant message to user %s", recipient_id)


async def send_assistant_message_to_many(
    db: AsyncSession,
    recipient_ids: set[int] | list[int],
    content: str,
    metadata: dict[str, Any] | None = None,
) -> None:
    for recipient_id in dict.fromkeys(int(uid) for uid in recipient_ids):
        await send_assistant_message(db, recipient_id, content, metadata)
