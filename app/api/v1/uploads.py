from __future__ import annotations

import asyncio
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile, status
from pydantic import BaseModel

from app.core.dependencies import get_current_user
from app.models.user import User
from app.services.storage import MAX_AUDIO_UPLOAD_BYTES, MAX_IMAGE_UPLOAD_BYTES, process_audio_upload, process_image_upload, public_url_for_key, run_image_worker, store_processed_audio, store_processed_image


router = APIRouter()
# Multipart parsing has already happened before this handler; gate the copies
# retained by application code, in addition to the separate decode worker slots.
_IMAGE_READ_SLOTS = asyncio.Semaphore(2)
_AUDIO_READ_SLOTS = asyncio.Semaphore(2)

class UploadRead(BaseModel):
    url: str
    thumbnail_url: str | None = None
    filename: str
    content_type: str


def _process_and_store_image(content: bytes, folder: str, object_id: str):
    processed = process_image_upload(content, folder, object_id)
    store_processed_image(processed)
    return processed


async def save_image_upload(file: UploadFile, folder: str, request: Request) -> UploadRead:
    async with _IMAGE_READ_SLOTS:
        content = await file.read(MAX_IMAGE_UPLOAD_BYTES + 1)
        if len(content) > MAX_IMAGE_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="Image size must not exceed 3MB")
        try:
            # One slot covers decode AND synchronous local/S3 writes. Cancellation
            # leaves the worker slot occupied until the thread exits.
            processed = await run_image_worker(_process_and_store_image, content, folder, uuid4().hex)
        except HTTPException:
            raise
        except OSError as exc:
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Image could not be saved") from exc
    return UploadRead(
        url=public_url_for_key(processed.key, request),
        thumbnail_url=public_url_for_key(processed.thumbnail_key, request),
        filename=processed.filename,
        content_type=processed.content_type,
    )


async def save_audio_upload(file: UploadFile, folder: str, request: Request) -> UploadRead:
    async with _AUDIO_READ_SLOTS:
        content = await file.read(MAX_AUDIO_UPLOAD_BYTES + 1)
        if len(content) > MAX_AUDIO_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="Voice size must not exceed 2MB")
        try:
            processed = process_audio_upload(content, folder, uuid4().hex, file.content_type)
            await asyncio.to_thread(store_processed_audio, processed)
        except HTTPException:
            raise
        except OSError as exc:
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Voice could not be saved") from exc
    return UploadRead(
        url=public_url_for_key(processed.key, request),
        thumbnail_url=None,
        filename=processed.filename,
        content_type=processed.content_type,
    )


@router.post("/avatar", response_model=UploadRead, status_code=201)
async def upload_avatar(
    request: Request,
    file: Annotated[UploadFile, File()],
    _current_user: Annotated[User, Depends(get_current_user)],
) -> UploadRead:
    return await save_image_upload(file, "avatars", request)


@router.post("/message-image", response_model=UploadRead, status_code=201)
async def upload_message_image(
    request: Request,
    file: Annotated[UploadFile, File()],
    _current_user: Annotated[User, Depends(get_current_user)],
) -> UploadRead:
    return await save_image_upload(file, "messages", request)


@router.post("/post-image", response_model=UploadRead, status_code=201)
async def upload_post_image(
    request: Request,
    file: Annotated[UploadFile, File()],
    _current_user: Annotated[User, Depends(get_current_user)],
) -> UploadRead:
    return await save_image_upload(file, "posts", request)


@router.post("/teamup-image", response_model=UploadRead, status_code=201)
async def upload_teamup_image(
    request: Request,
    file: Annotated[UploadFile, File()],
    _current_user: Annotated[User, Depends(get_current_user)],
) -> UploadRead:
    return await save_image_upload(file, "teamup", request)


@router.post("/announcement-image", response_model=UploadRead, status_code=201)
async def upload_announcement_image(
    request: Request,
    file: Annotated[UploadFile, File()],
    _current_user: Annotated[User, Depends(get_current_user)],
) -> UploadRead:
    return await save_image_upload(file, "announcements", request)


@router.post("/profile-cover", response_model=UploadRead, status_code=201)
async def upload_profile_cover(
    request: Request,
    file: Annotated[UploadFile, File()],
    _current_user: Annotated[User, Depends(get_current_user)],
) -> UploadRead:
    return await save_image_upload(file, "profile-covers", request)


@router.post("/message-voice", response_model=UploadRead, status_code=201)
async def upload_message_voice(
    request: Request,
    file: Annotated[UploadFile, File()],
    _current_user: Annotated[User, Depends(get_current_user)],
) -> UploadRead:
    return await save_audio_upload(file, "messages", request)
