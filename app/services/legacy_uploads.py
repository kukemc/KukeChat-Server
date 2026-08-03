from __future__ import annotations

import asyncio
import json
import re
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
    local_response_for_key,
    object_exists,
    process_legacy_image,
    public_url_for_key,
    store_processed_image,
    thumbnail_key_for_key,
)


LEGACY_FOLDERS = {"avatars", "messages"}
LEGACY_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp", ".gif")
_MIGRATED_KEYS: set[str] = set()
_UPDATED_KEYS: set[str] = set()
_MIGRATION_LOCKS: dict[str, asyncio.Lock] = {}


def _migration_lock(key: str) -> asyncio.Lock:
    lock = _MIGRATION_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _MIGRATION_LOCKS[key] = lock
    return lock


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
    user_result = await db.execute(select(User).where(User.avatar_url.like(like_pattern)))
    for user in user_result.scalars():
        if user.avatar_url:
            user.avatar_url = _replace_legacy_urls(user.avatar_url, old_key, new_url)

    conversation_result = await db.execute(select(Conversation).where(Conversation.avatar_url.like(like_pattern)))
    for conversation in conversation_result.scalars():
        if conversation.avatar_url:
            conversation.avatar_url = _replace_legacy_urls(conversation.avatar_url, old_key, new_url)

    metadata_pattern = f"%/uploads/{old_key}%"
    message_result = await db.execute(select(Message).where(Message.metadata_json.like(metadata_pattern)))
    for message in message_result.scalars():
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
    result = await db.execute(
        select(Message).where(or_(Message.content.like(like_pattern), Message.metadata_json.like(like_pattern)))
    )
    for message in result.scalars():
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
    source = _legacy_request_source(path)
    if source is None:
        return local_response_for_key(path)

    folder, source_key, requested_thumbnail = source
    source_file = local_path_for_key(source_key)
    if source_file is None:
        return local_response_for_key(path)

    target_key = f"{folder}/{PurePosixPath(source_key).stem}.gif" if PurePosixPath(source_key).suffix.lower() == ".gif" else f"{folder}/{PurePosixPath(source_key).stem}.jpg"
    thumbnail_key = thumbnail_key_for_key(target_key)
    original_url = public_url_for_key(target_key, request)
    thumbnail_url = public_url_for_key(thumbnail_key, request)

    if target_key not in _MIGRATED_KEYS:
        async with _migration_lock(target_key):
            if target_key not in _MIGRATED_KEYS:
                try:
                    target_exists = await asyncio.to_thread(object_exists, target_key)
                    thumbnail_exists = await asyncio.to_thread(object_exists, thumbnail_key)
                    if target_exists and thumbnail_exists:
                        _MIGRATED_KEYS.add(target_key)
                except Exception:
                    pass

            if target_key not in _MIGRATED_KEYS:
                try:
                    content = await asyncio.to_thread(source_file.read_bytes)
                    processed = await asyncio.to_thread(process_legacy_image, content, folder, PurePosixPath(source_key).stem)
                    await asyncio.to_thread(store_processed_image, processed)
                    original_url = public_url_for_key(processed.key, request)
                    thumbnail_url = public_url_for_key(processed.thumbnail_key, request)
                    _MIGRATED_KEYS.add(processed.key)
                except Exception:
                    return local_response_for_key(path)

    if source_key not in _UPDATED_KEYS:
        if folder == "avatars":
            await _update_avatar_references(db, source_key, original_url)
        else:
            await _update_message_references(db, source_key, original_url)
        await db.commit()
        _UPDATED_KEYS.add(source_key)

    return RedirectResponse(thumbnail_url if requested_thumbnail else original_url, status_code=302)
