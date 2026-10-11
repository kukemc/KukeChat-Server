from __future__ import annotations

import asyncio
import json
import re
from collections import OrderedDict
from time import monotonic
from pathlib import PurePosixPath
from typing import Any

from fastapi import Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.conversation import Conversation
from app.models.message import Message
from app.models.user import User
from app.services.storage import (
    local_path_for_key,
    MAX_LEGACY_IMAGE_BYTES,
    local_response_for_key,
    object_exists,
    process_legacy_image,
    run_image_worker,
    public_url_for_key,
    store_processed_image,
    thumbnail_key_for_key,
)


LEGACY_FOLDERS = {"avatars", "messages"}
LEGACY_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp", ".gif")
# Fixed stripes avoid storing one lock per historic image. The bounded,
# short-lived cache suppresses hot-path LIKE scans, not permanent migration
# state: evictions, worker restarts and other processes may rescan the DB.
_MIGRATION_LOCKS = tuple(asyncio.Lock() for _ in range(64))
_UPDATED_CACHE_LIMIT = 512
_UPDATED_CACHE_TTL_SECONDS = 600
_UPDATED_KEYS: OrderedDict[str, float] = OrderedDict()


def _recently_updated(key: str) -> bool:
    until = _UPDATED_KEYS.get(key)
    if until is None:
        return False
    if until <= monotonic():
        _UPDATED_KEYS.pop(key, None)
        return False
    _UPDATED_KEYS.move_to_end(key)
    return True


def _mark_updated(key: str) -> None:
    _UPDATED_KEYS[key] = monotonic() + _UPDATED_CACHE_TTL_SECONDS
    _UPDATED_KEYS.move_to_end(key)
    if len(_UPDATED_KEYS) > _UPDATED_CACHE_LIMIT:
        _UPDATED_KEYS.popitem(last=False)


def _migration_lock(key: str) -> asyncio.Lock:
    return _MIGRATION_LOCKS[hash(key) % len(_MIGRATION_LOCKS)]


def _read_legacy_image(path) -> bytes:
    with path.open("rb") as stream:
        content = stream.read(MAX_LEGACY_IMAGE_BYTES + 1)
    if len(content) > MAX_LEGACY_IMAGE_BYTES:
        raise ValueError("Legacy image exceeds 12MB")
    return content


def _process_and_store_legacy(path, folder: str, object_id: str) -> None:
    processed = process_legacy_image(_read_legacy_image(path), folder, object_id)
    store_processed_image(processed)


def _legacy_request_source(path: str) -> tuple[str, str, bool] | None:
    normalized = PurePosixPath(path.strip().lstrip("/")).as_posix().strip("/")
    parts = PurePosixPath(normalized).parts
    if ".." in parts or not parts or parts[0] not in LEGACY_FOLDERS:
        return None

    folder = parts[0]
    if len(parts) == 2:
        return folder, normalized, False
    if len(parts) == 3 and parts[1] == "thumbs":
        stem = PurePosixPath(parts[2]).stem
        for suffix in LEGACY_SUFFIXES:
            candidate = f"{folder}/{stem}{suffix}"
            if local_path_for_key(candidate) is not None:
                return folder, candidate, True
    return None


def _legacy_url_variants(key: str) -> set[str]:
    key = PurePosixPath(key).as_posix().strip("/")
    path = PurePosixPath(key)
    variants = {f"/uploads/{key}", f"uploads/{key}"}
    if len(path.parts) >= 2:
        thumb_key = f"{path.parts[0]}/thumbs/{path.stem}.jpg"
        variants.update({f"/uploads/{thumb_key}", f"uploads/{thumb_key}"})
        for suffix in LEGACY_SUFFIXES:
            thumb_key = f"{path.parts[0]}/thumbs/{path.stem}{suffix}"
            variants.update({f"/uploads/{thumb_key}", f"uploads/{thumb_key}"})
    return variants


def _replace_legacy_urls(value: str, old_key: str, new_url: str) -> str:
    thumbnail_url = public_url_for_key(thumbnail_key_for_key(old_key))
    old_key_pattern = re.escape(PurePosixPath(old_key).as_posix().strip("/"))
    updated = re.sub(rf"https?://[^\s\"'<>]+/uploads/{old_key_pattern}", new_url, value)
    old_path = PurePosixPath(old_key)
    if len(old_path.parts) >= 2:
        for suffix in LEGACY_SUFFIXES:
            thumb_key = f"{old_path.parts[0]}/thumbs/{old_path.stem}{suffix}"
            thumb_key_pattern = re.escape(thumb_key)
            updated = re.sub(rf"https?://[^\s\"'<>]+/uploads/{thumb_key_pattern}", thumbnail_url, updated)
    for variant in _legacy_url_variants(old_key):
        target_url = public_url_for_key(thumbnail_key_for_key(old_key)) if "/thumbs/" in variant else new_url
        updated = updated.replace(variant, target_url)
    return updated


def _replace_json_value(value: Any, old_key: str, new_url: str) -> tuple[Any, bool]:
    if isinstance(value, str):
        updated = _replace_legacy_urls(value, old_key, new_url)
        return updated, updated != value
    if isinstance(value, list):
        changed = False
        items = []
        for item in value:
            updated, item_changed = _replace_json_value(item, old_key, new_url)
            items.append(updated)
            changed = changed or item_changed
        return items, changed
    if isinstance(value, dict):
        changed = False
        items = {}
        for key, item in value.items():
            updated, item_changed = _replace_json_value(item, old_key, new_url)
            items[key] = updated
            changed = changed or item_changed
        return items, changed
    return value, False


async def _update_avatar_references(db: AsyncSession, old_key: str, new_url: str) -> None:
    like_pattern = f"%/uploads/{old_key}%"
    user_result = await db.stream(select(User).where(User.avatar_url.like(like_pattern)).execution_options(yield_per=100))
    async for user in user_result.scalars():
        if user.avatar_url:
            user.avatar_url = _replace_legacy_urls(user.avatar_url, old_key, new_url)

    conversation_result = await db.stream(select(Conversation).where(Conversation.avatar_url.like(like_pattern)).execution_options(yield_per=100))
    async for conversation in conversation_result.scalars():
        if conversation.avatar_url:
            conversation.avatar_url = _replace_legacy_urls(conversation.avatar_url, old_key, new_url)

    metadata_pattern = f"%/uploads/{old_key}%"
    message_result = await db.stream(select(Message).where(Message.metadata_json.like(metadata_pattern)).execution_options(yield_per=100))
    async for message in message_result.scalars():
        if not message.metadata_json:
            continue
        try:
            metadata = json.loads(message.metadata_json)
        except json.JSONDecodeError:
            continue
        updated_metadata, changed = _replace_json_value(metadata, old_key, new_url)
        if changed:
            message.metadata_json = json.dumps(updated_metadata, ensure_ascii=False, separators=(",", ":"))


async def _update_message_references(db: AsyncSession, old_key: str, new_url: str) -> None:
    like_pattern = f"%/uploads/{old_key}%"
    result = await db.stream(
        select(Message).where(or_(Message.content.like(like_pattern), Message.metadata_json.like(like_pattern)))
        .execution_options(yield_per=100)
    )
    async for message in result.scalars():
        message.content = _replace_legacy_urls(message.content, old_key, new_url)
        if not message.metadata_json:
            continue
        try:
            metadata = json.loads(message.metadata_json)
        except json.JSONDecodeError:
            continue
        updated_metadata, changed = _replace_json_value(metadata, old_key, new_url)
        if changed:
            message.metadata_json = json.dumps(updated_metadata, ensure_ascii=False, separators=(",", ":"))


async def migrate_legacy_upload_response(path: str, db: AsyncSession, request: Request) -> Response | None:
    source = await asyncio.to_thread(_legacy_request_source, path)
    if source is None:
        return await asyncio.to_thread(local_response_for_key, path)

    folder, source_key, requested_thumbnail = source
    source_file = await asyncio.to_thread(local_path_for_key, source_key)
    if source_file is None:
        return await asyncio.to_thread(local_response_for_key, path)

    target_key = f"{folder}/{PurePosixPath(source_key).stem}.gif" if PurePosixPath(source_key).suffix.lower() == ".gif" else f"{folder}/{PurePosixPath(source_key).stem}.jpg"
    thumbnail_key = thumbnail_key_for_key(target_key)
    original_url = public_url_for_key(target_key, request)
    thumbnail_url = public_url_for_key(thumbnail_key, request)

    async with _migration_lock(target_key):
        if not _recently_updated(source_key):
            try:
                target_exists = await asyncio.to_thread(object_exists, target_key)
                thumbnail_exists = await asyncio.to_thread(object_exists, thumbnail_key)
                if not (target_exists and thumbnail_exists):
                    await run_image_worker(_process_and_store_legacy, source_file, folder, PurePosixPath(source_key).stem)
            except Exception:
                # Existing legacy URLs remain readable even when a large/damaged
                # historic image cannot be migrated under the new decode limits.
                return await asyncio.to_thread(local_response_for_key, path)

            # Keep the update under the same stripe as migration so simultaneous
            # requests for this key cannot both launch full-table LIKE scans.
            if folder == "avatars":
                await _update_avatar_references(db, source_key, original_url)
            else:
                await _update_message_references(db, source_key, original_url)
            await db.commit()
            _mark_updated(source_key)

    return RedirectResponse(thumbnail_url if requested_thumbnail else original_url, status_code=302)
