from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.enums import FriendRequestStatus, ModerationStatus, PresenceStatus, UserStatus
from app.models.friend import FriendRequest, Friendship
from app.models.user import User
from app.schemas.user import UserPresenceUpdate, UserUpdate
from app.services import pokes as poke_service
from app.services.upload_paths import require_avatar_upload_url, require_profile_cover_upload_url


PROFILE_ACCENT_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
PROFILE_LAYOUTS = {"classic", "banner", "compact"}
PROFILE_CARD_STYLES = {"soft", "glass", "solid"}
UTC = timezone.utc


async def update_profile(db: AsyncSession, user: User, payload: UserUpdate) -> User:
    updates = payload.model_dump(exclude_unset=True)
    # 头像 / 封面没改就不校验：用 CCW 资料覆盖过的号，头像和封面是 CCW 的图片地址，不是本站上传的；
    # 客户端保存资料时会把整份资料（包括没动的头像）一起提交，校验了就什么都改不了
    if "avatar_url" in updates and (updates["avatar_url"] or None) == (user.avatar_url or None):
        updates.pop("avatar_url")
    if "profile_cover_url" in updates and (updates["profile_cover_url"] or None) == (user.profile_cover_url or None):
        updates.pop("profile_cover_url")
    if "avatar_url" in updates:
        updates["avatar_url"] = require_avatar_upload_url(updates["avatar_url"])
    if "profile_accent_color" in updates and updates["profile_accent_color"]:
        if PROFILE_ACCENT_RE.fullmatch(updates["profile_accent_color"]) is None:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Profile accent color must be a hex color")
    if "profile_layout" in updates and updates["profile_layout"] and updates["profile_layout"] not in PROFILE_LAYOUTS:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid profile layout")
    if "profile_card_style" in updates and updates["profile_card_style"] and updates["profile_card_style"] not in PROFILE_CARD_STYLES:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid profile card style")
    # 戳一戳文案要同步过审核（外部服务，可能要几秒）：在改 user 之前做 —— 审核前会提交掉请求的读事务、把连接还回去，
    # 这时会话里不能有改了一半的 user
    poke_suffix: str | None = None
    if "poke_suffix" in updates:
        poke_suffix = poke_service.normalize_suffix(updates.pop("poke_suffix"))
        if poke_suffix != (user.poke_suffix or ""):
            await poke_service.ensure_suffix_allowed(db, user, poke_suffix)
    if "profile_cover_url" in updates:
        cover_url = require_profile_cover_upload_url(updates.pop("profile_cover_url"))
        if cover_url:
            if settings.content_moderation_enabled and not user.content_moderation_whitelisted:
                user.profile_cover_pending_url = cover_url
                user.profile_cover_moderation_status = ModerationStatus.PENDING.value
            else:
                user.profile_cover_url = cover_url
                user.profile_cover_pending_url = None
                user.profile_cover_moderation_status = ModerationStatus.APPROVED.value
        else:
            user.profile_cover_url = None
            user.profile_cover_pending_url = None
            user.profile_cover_moderation_status = ModerationStatus.APPROVED.value
    if updates.get("share_typing") is None:
        updates.pop("share_typing", None)
    # 这两个字段不可为空，传 null 或空串表示恢复默认
    if "poke_action" in updates:
        user.poke_action = poke_service.normalize_action(updates.pop("poke_action"))
    if poke_suffix is not None:
        user.poke_suffix = poke_suffix
    for key, value in updates.items():
        if isinstance(value, str):
            value = value.strip() or None
        setattr(user, key, value)
    await db.commit()
    await db.refresh(user)
    if user.profile_cover_pending_url and user.profile_cover_moderation_status == ModerationStatus.PENDING.value:
        from app.services.moderation import schedule_profile_cover_moderation

        schedule_profile_cover_moderation(user.id)
    return user


async def update_presence(db: AsyncSession, user: User, payload: UserPresenceUpdate) -> User:
    presence_text = payload.presence_text.strip() if payload.presence_text else None
    if payload.presence_status != PresenceStatus.CUSTOM:
        presence_text = presence_text or None
    elif not presence_text:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Custom presence text is required")
    user.presence_status = payload.presence_status
    user.presence_text = presence_text
    user.presence_updated_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(user)
    return user


async def resolve_stale_profile_cover_moderation(db: AsyncSession, user: User) -> User:
    if user.profile_cover_moderation_status != ModerationStatus.PENDING.value or not user.profile_cover_pending_url:
        return user
    updated_at = user.updated_at
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=UTC)
    stale_after = timedelta(seconds=max(settings.content_moderation_timeout_seconds + 30, 120))
    if datetime.now(UTC) - updated_at < stale_after:
        return user
    user.profile_cover_pending_url = None
    user.profile_cover_moderation_status = ModerationStatus.REJECTED.value
    await db.commit()
    await db.refresh(user)
    return user


async def search_users(db: AsyncSession, q: str, current_user_id: int, limit: int = 20) -> list[User]:
    term = q.strip()
    if not term:
        return []
    keyword = f"%{term}%"
    conditions = [
        User.username.ilike(keyword),
        User.email.ilike(keyword),
        User.nickname.ilike(keyword),
        User.bio.ilike(keyword),
    ]
    if term.isdigit():
        conditions.append(User.id == int(term))
    result = await db.execute(
        select(User)
        .where(or_(*conditions))
        .order_by(User.username.asc())
        .limit(limit)
    )
    return list(result.scalars().all())


async def recommended_users(db: AsyncSession, current_user_id: int, limit: int = 20) -> list[User]:
    friend_ids = select(Friendship.friend_id).where(Friendship.user_id == current_user_id)
    sent_pending_ids = select(FriendRequest.receiver_id).where(
        FriendRequest.requester_id == current_user_id,
        FriendRequest.status == FriendRequestStatus.PENDING,
    )
    received_pending_ids = select(FriendRequest.requester_id).where(
        FriendRequest.receiver_id == current_user_id,
        FriendRequest.status == FriendRequestStatus.PENDING,
    )
    result = await db.execute(
        select(User)
        .where(
            User.id != current_user_id,
            User.id.not_in(friend_ids),
            User.id.not_in(sent_pending_ids),
            User.id.not_in(received_pending_ids),
        )
        .order_by(User.created_at.desc(), User.id.desc())
        .limit(limit)
    )
    return list(result.scalars().all())


async def list_ccw_creators(db: AsyncSession, limit: int = 30, offset: int = 0) -> tuple[list[User], int]:
    filters = (
        User.status == UserStatus.ACTIVE,
        User.is_bot.is_(False),
        User.ccw_student_oid.is_not(None),
        User.ccw_student_oid != "",
        User.ccw_follower_count > 1000,
    )
    total_result = await db.execute(select(func.count(User.id)).where(*filters))
    result = await db.execute(
        select(User)
        .where(*filters)
        .order_by(User.ccw_follower_count.desc(), User.id.asc())
        .offset(offset)
        .limit(limit)
    )
    return list(result.scalars().all()), int(total_result.scalar_one() or 0)
