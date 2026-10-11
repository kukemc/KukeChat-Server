from __future__ import annotations

import asyncio
from uuid import uuid4
from zlib import crc32

from fastapi import HTTPException, Request, status
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.models.sticker import FavoriteSticker, StickerAsset
from app.schemas.sticker import FavoriteStickerRead
from app.services.storage import (
    create_compressed_sticker,
    delete_objects,
    get_object,
    MAX_LEGACY_IMAGE_BYTES,
    public_url_for_key,
    run_image_worker,
    storage_key_from_url,
    thumbnail_key_for_key,
)


ALLOWED_STICKER_SOURCE_FOLDERS = {"messages", "stickers"}
CONTENT_TYPE_BY_SUFFIX = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}
COMPRESSED_STICKER_CONTENT_TYPE = "image/jpeg"
# Fixed-size stripes avoid retaining a lock per source URL indefinitely.
_SOURCE_LOCKS = tuple(asyncio.Lock() for _ in range(64))


def _source_lock(source_path: str) -> asyncio.Lock:
    return _SOURCE_LOCKS[crc32(source_path.encode("utf-8")) % len(_SOURCE_LOCKS)]


def build_favorite_sticker_read(favorite: FavoriteSticker, request: Request) -> FavoriteStickerRead:
    if favorite.asset is None:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Sticker asset is missing")
    return FavoriteStickerRead(
        asset_id=favorite.asset.id,
        url=public_url_for_key(favorite.asset.storage_path, request),
        content_type=favorite.asset.content_type,
        created_at=favorite.created_at,
    )


def _normalize_source_path(source_url: str) -> str:
    relative_path = storage_key_from_url(source_url)
    if relative_path is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Only uploaded message images can be favorited")

    path_parts = relative_path.split("/")
    if not path_parts or path_parts[0] not in ALLOWED_STICKER_SOURCE_FOLDERS or ".." in path_parts:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Only uploaded message images can be favorited")
    return relative_path


def _resolve_source(source_path: str) -> tuple[str, str]:
    """Fetch once and keep read/decode/write under the shared image worker limit."""
    stored = get_object(source_path, MAX_LEGACY_IMAGE_BYTES)
    content_type = stored.content_type
    if content_type == "application/octet-stream":
        suffix = f".{source_path.rsplit('.', 1)[-1].lower()}" if "." in source_path else ""
        content_type = CONTENT_TYPE_BY_SUFFIX.get(suffix)
    if content_type is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Only png, jpeg, webp, and gif images are supported")
    if content_type == "image/gif" or source_path.startswith("stickers/"):
        # Preserve existing GIF frames and already-stored sticker behavior.
        return source_path, content_type
    return create_compressed_sticker(stored.data, uuid4().hex)


async def list_favorite_stickers(db: AsyncSession, user_id: int) -> list[FavoriteSticker]:
    result = await db.execute(
        select(FavoriteSticker)
        .options(joinedload(FavoriteSticker.asset))
        .where(FavoriteSticker.user_id == user_id)
        .order_by(FavoriteSticker.created_at.desc(), FavoriteSticker.id.desc())
    )
    return result.scalars().all()


async def _get_sticker_asset(db: AsyncSession, source_path: str) -> StickerAsset | None:
    result = await db.execute(
        select(StickerAsset).where(
            or_(
                StickerAsset.source_path == source_path,
                StickerAsset.storage_path == source_path,
            )
        )
    )
    return result.scalar_one_or_none()


async def _get_favorite_sticker(db: AsyncSession, user_id: int, asset_id: int) -> FavoriteSticker | None:
    result = await db.execute(
        select(FavoriteSticker)
        .options(joinedload(FavoriteSticker.asset))
        .where(FavoriteSticker.user_id == user_id, FavoriteSticker.asset_id == asset_id)
    )
    return result.scalar_one_or_none()


async def add_favorite_sticker(db: AsyncSession, user_id: int, source_url: str) -> FavoriteSticker:
    source_path = _normalize_source_path(source_url)
    # Hold one of a fixed number of locks until commit: concurrent requests for
    # the same source must see the committed asset before reading/writing it again.
    async with _source_lock(source_path):
        asset = await _get_sticker_asset(db, source_path)
        if asset is None:
            storage_path, content_type = await run_image_worker(_resolve_source, source_path)
            asset = StickerAsset(source_path=source_path, storage_path=storage_path, content_type=content_type if storage_path == source_path else COMPRESSED_STICKER_CONTENT_TYPE)
            db.add(asset)
            await db.flush()

        favorite = await _get_favorite_sticker(db, user_id, asset.id)
        if favorite is None:
            favorite = FavoriteSticker(user_id=user_id, asset_id=asset.id)
            db.add(favorite)
            await db.commit()
            favorite = await _get_favorite_sticker(db, user_id, asset.id)
        else:
            await db.commit()

        if favorite is None:
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Favorite sticker could not be created")
        return favorite


async def remove_favorite_sticker(db: AsyncSession, user_id: int, asset_id: int) -> None:
    favorite = await _get_favorite_sticker(db, user_id, asset_id)
    if favorite is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Favorite sticker not found")
    asset = favorite.asset
    await db.delete(favorite)
    await db.flush()
    keys_to_delete: list[str] = []
    if asset is not None:
        remaining_result = await db.execute(select(FavoriteSticker.id).where(FavoriteSticker.asset_id == asset.id).limit(1))
        if remaining_result.scalar_one_or_none() is None:
            await db.delete(asset)
            if asset.storage_path != asset.source_path and asset.storage_path.startswith("stickers/"):
                keys_to_delete.extend([asset.storage_path, thumbnail_key_for_key(asset.storage_path)])
    await db.commit()
    if keys_to_delete:
        await asyncio.to_thread(delete_objects, keys_to_delete)
