"""群自定义表情：每个群一套带名字的图片，群主和管理员维护，全体成员可用。

使用方式就是发一条带 sticker 标记的图片消息（和收藏表情一样），所以发送侧没有新逻辑。
"""

from __future__ import annotations

from fastapi import HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.poll import GroupEmoji
from app.models.sticker import StickerAsset
from app.models.user import User
from app.schemas.poll import GroupEmojiRead
from app.services import stickers as sticker_service
from app.services.conversations import STAFF_ROLES, require_group_conversation, require_membership
from app.services.storage import get_object, public_url_for_key, run_image_worker

MAX_EMOJIS_PER_GROUP = 60
MAX_EMOJI_NAME_LENGTH = 12
MAX_EMOJI_BYTES = 512 * 1024


def _error(code: str, message: str, status_code: int = status.HTTP_422_UNPROCESSABLE_ENTITY) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


def _clean_name(name: str) -> str:
    cleaned = "".join(name.split())
    if not 1 <= len(cleaned) <= MAX_EMOJI_NAME_LENGTH:
        raise _error("invalid_emoji_name", f"表情名称需要 1 到 {MAX_EMOJI_NAME_LENGTH} 个字，不能有空格")
    return cleaned


async def _require_staff(db: AsyncSession, conversation_id: int, user_id: int) -> None:
    await require_group_conversation(db, conversation_id)
    member = await require_membership(db, conversation_id, user_id)
    if member.role not in STAFF_ROLES:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail={"code": "emoji_forbidden", "message": "只有群主和管理员可以管理群表情"})


def build_read(emoji: GroupEmoji, asset: StickerAsset, request: Request | None) -> GroupEmojiRead:
    return GroupEmojiRead(
        id=emoji.id,
        conversation_id=emoji.conversation_id,
        name=emoji.name,
        url=public_url_for_key(asset.storage_path, request),
        content_type=asset.content_type,
        created_by_id=emoji.created_by_id,
        created_at=emoji.created_at,
    )


async def list_emojis(db: AsyncSession, conversation_id: int, user_id: int) -> list[tuple[GroupEmoji, StickerAsset]]:
    await require_group_conversation(db, conversation_id)
    await require_membership(db, conversation_id, user_id)
    rows = await db.execute(
        select(GroupEmoji, StickerAsset)
        .join(StickerAsset, StickerAsset.id == GroupEmoji.asset_id)
        .where(GroupEmoji.conversation_id == conversation_id)
        .order_by(GroupEmoji.id.asc())
    )
    return [(emoji, asset) for emoji, asset in rows.all()]


async def _ensure_image_allowed(actor: User, asset: StickerAsset, conversation_id: int) -> None:
    """群表情全群可见，先过图片审核。只有明确违规才拒绝；审核服务不可用时放行。"""
    if not settings.content_moderation_enabled or actor.content_moderation_whitelisted:
        return
    from app.services import moderation as moderation_service

    try:
        result = await moderation_service._call_image_moderation_for_user(
            public_url_for_key(asset.storage_path), actor.id, context_id=f"group_emoji:{conversation_id}"
        )
        decision = moderation_service._normalize_result(result, "image")["decision"]
    except Exception:
        return
    if decision == "block":
        raise _error("emoji_rejected", "这张图片未通过审核，不能作为群表情")


async def create_emoji(db: AsyncSession, conversation_id: int, actor: User, name: str, image_url: str) -> tuple[GroupEmoji, StickerAsset]:
    await _require_staff(db, conversation_id, actor.id)
    cleaned = _clean_name(name)
    count = int(
        (await db.execute(select(func.count()).select_from(GroupEmoji).where(GroupEmoji.conversation_id == conversation_id))).scalar_one() or 0
    )
    if count >= MAX_EMOJIS_PER_GROUP:
        raise _error("emoji_limit_reached", f"每个群最多 {MAX_EMOJIS_PER_GROUP} 个表情", status.HTTP_409_CONFLICT)
    taken = await db.execute(select(GroupEmoji.id).where(GroupEmoji.conversation_id == conversation_id, GroupEmoji.name == cleaned))
    if taken.scalar_one_or_none() is not None:
        raise _error("emoji_name_taken", "这个群里已经有同名的表情", status.HTTP_409_CONFLICT)

    try:
        source_path = sticker_service._normalize_source_path(image_url)
    except HTTPException as exc:
        raise _error("invalid_emoji_image", "表情图片必须是已上传的图片") from exc
    async with sticker_service._source_lock(source_path):
        asset = await sticker_service._get_sticker_asset(db, source_path)
        if asset is None:
            storage_path, content_type = await run_image_worker(sticker_service._resolve_source, source_path)
            asset = StickerAsset(
                source_path=source_path,
                storage_path=storage_path,
                content_type=content_type if storage_path == source_path else sticker_service.COMPRESSED_STICKER_CONTENT_TYPE,
            )
            db.add(asset)
            await db.flush()
        try:
            stored = await run_image_worker(get_object, asset.storage_path, MAX_EMOJI_BYTES)
        except HTTPException as exc:
            await db.rollback()
            if exc.status_code == status.HTTP_413_REQUEST_ENTITY_TOO_LARGE:
                raise _error("emoji_too_large", "表情图片不能超过 512KB", status.HTTP_413_REQUEST_ENTITY_TOO_LARGE) from exc
            raise
        if len(stored.data) > MAX_EMOJI_BYTES:
            await db.rollback()
            raise _error("emoji_too_large", "表情图片不能超过 512KB", status.HTTP_413_REQUEST_ENTITY_TOO_LARGE)
        await _ensure_image_allowed(actor, asset, conversation_id)
        emoji = GroupEmoji(conversation_id=conversation_id, name=cleaned, asset_id=asset.id, created_by_id=actor.id)
        db.add(emoji)
        try:
            await db.commit()
        except IntegrityError as exc:
            await db.rollback()
            raise _error("emoji_name_taken", "这个群里已经有同名的表情", status.HTTP_409_CONFLICT) from exc
    return emoji, asset


async def _get(db: AsyncSession, conversation_id: int, emoji_id: int) -> GroupEmoji:
    emoji = await db.get(GroupEmoji, emoji_id)
    if emoji is None or emoji.conversation_id != conversation_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Emoji not found")
    return emoji


async def rename_emoji(db: AsyncSession, conversation_id: int, emoji_id: int, actor: User, name: str) -> tuple[GroupEmoji, StickerAsset]:
    await _require_staff(db, conversation_id, actor.id)
    emoji = await _get(db, conversation_id, emoji_id)
    cleaned = _clean_name(name)
    if cleaned != emoji.name:
        taken = await db.execute(
            select(GroupEmoji.id).where(GroupEmoji.conversation_id == conversation_id, GroupEmoji.name == cleaned, GroupEmoji.id != emoji.id)
        )
        if taken.scalar_one_or_none() is not None:
            raise _error("emoji_name_taken", "这个群里已经有同名的表情", status.HTTP_409_CONFLICT)
        emoji.name = cleaned
        try:
            await db.commit()
        except IntegrityError as exc:
            await db.rollback()
            raise _error("emoji_name_taken", "这个群里已经有同名的表情", status.HTTP_409_CONFLICT) from exc
    asset = await db.get(StickerAsset, emoji.asset_id)
    assert asset is not None
    return emoji, asset


async def delete_emoji(db: AsyncSession, conversation_id: int, emoji_id: int, actor: User) -> None:
    """只删群里的这条引用。图片本身留着：已经发出去的表情消息还在用它。"""
    await _require_staff(db, conversation_id, actor.id)
    emoji = await _get(db, conversation_id, emoji_id)
    await db.delete(emoji)
    await db.commit()
