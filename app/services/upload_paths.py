from __future__ import annotations

from pathlib import PurePosixPath

from fastapi import HTTPException, status

from app.services.storage import storage_key_from_url


AVATAR_UPLOAD_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
PROFILE_COVER_UPLOAD_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
TEAMUP_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
MESSAGE_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
MESSAGE_AUDIO_SUFFIXES = {".webm", ".ogg", ".mp3", ".m4a", ".aac", ".wav"}
MESSAGE_UPLOAD_SUFFIXES = MESSAGE_IMAGE_SUFFIXES | MESSAGE_AUDIO_SUFFIXES


def _uploaded_file_path(value: str | None, *, folder: str, suffixes: set[str]) -> str | None:
    if value is None or not value.strip():
        return None
    relative_path = storage_key_from_url(value)
    if relative_path is None:
        return None
    path_parts = PurePosixPath(relative_path).parts
    if len(path_parts) != 2 or path_parts[0] != folder or path_parts[1] == "thumbs" or ".." in path_parts:
        return None
    if PurePosixPath(path_parts[1]).suffix.lower() not in suffixes:
        return None
    return relative_path


def require_avatar_upload_url(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    if _uploaded_file_path(value, folder="avatars", suffixes=AVATAR_UPLOAD_SUFFIXES) is None:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Avatar must reference an uploaded avatar image")
    return value.strip()


def require_profile_cover_upload_url(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    if _uploaded_file_path(value, folder="profile-covers", suffixes=PROFILE_COVER_UPLOAD_SUFFIXES) is None:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Profile cover must reference an uploaded profile cover image")
    return value.strip()


def require_teamup_image_upload_url(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    if _uploaded_file_path(value, folder="teamup", suffixes=TEAMUP_IMAGE_SUFFIXES) is None:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Teamup image must reference an uploaded teamup image")
    return value.strip()


def require_message_upload_url(value: str | None) -> str | None:
    return _uploaded_file_path(value, folder="messages", suffixes=MESSAGE_UPLOAD_SUFFIXES)


def require_message_image_upload_url(value: str | None, *, detail: str = "Message image must reference an uploaded image file") -> str:
    path = _uploaded_file_path(value, folder="messages", suffixes=MESSAGE_IMAGE_SUFFIXES)
    if path is None:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=detail)
    return path


def require_message_or_sticker_image_url(value: str | None, *, detail: str = "Image message must reference an uploaded image or sticker file") -> str:
    message_path = _uploaded_file_path(value, folder="messages", suffixes=MESSAGE_IMAGE_SUFFIXES)
    if message_path is not None:
        return message_path
    sticker_path = _uploaded_file_path(value, folder="stickers", suffixes=MESSAGE_IMAGE_SUFFIXES)
    if sticker_path is not None:
        return sticker_path
    raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=detail)
