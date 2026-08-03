from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import BinaryIO
from urllib.parse import quote, unquote, urlsplit

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import HTTPException, Request, status
from fastapi.responses import FileResponse
from PIL import Image, ImageOps, UnidentifiedImageError
from starlette.routing import NoMatchFound

from app.core.config import settings


ALLOWED_IMAGE_TYPES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}
ALLOWED_AUDIO_TYPES = {
    "audio/webm": ".webm",
    "audio/ogg": ".ogg",
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/mp4": ".m4a",
    "audio/aac": ".aac",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
}
IMAGE_FORMAT_CONTENT_TYPES = {
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "WEBP": "image/webp",
    "GIF": "image/gif",
}
MAX_IMAGE_UPLOAD_BYTES = 3 * 1024 * 1024
MAX_AUDIO_UPLOAD_BYTES = 2 * 1024 * 1024
JPEG_CONTENT_TYPE = "image/jpeg"
S3_CONFIG = Config(connect_timeout=3, read_timeout=10, retries={"max_attempts": 2, "mode": "standard"})
_S3_CLIENT = None


@dataclass(frozen=True)
class ProcessedImageUpload:
    key: str
    data: bytes
    content_type: str
    thumbnail_key: str
    thumbnail_data: bytes
    thumbnail_content_type: str
    filename: str


@dataclass(frozen=True)
class ProcessedAudioUpload:
    key: str
    data: bytes
    content_type: str
    filename: str


@dataclass(frozen=True)
class StoredObject:
    data: bytes
    content_type: str


def _normalize_key(key: str) -> str:
    normalized = PurePosixPath(unquote(key).strip().lstrip("/")).as_posix().strip("/")
    if not normalized or ".." in PurePosixPath(normalized).parts:
        raise ValueError("Invalid storage key")
    return normalized


def _join_key(*parts: str) -> str:
    return PurePosixPath(*parts).as_posix().strip("/")


def _storage_error(detail: str = "Storage operation failed") -> HTTPException:
    return HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=detail)


def public_url_for_key(key: str, request: Request | None = None) -> str:
    normalized = _normalize_key(key)
    if settings.storage_backend == "s3":
        if not settings.s3_public_url:
            raise _storage_error("S3 public URL is not configured")
        base_url = settings.s3_public_url.rstrip("/")
        return f"{base_url}/{quote(normalized, safe='/')}"
    if request is None:
        return f"/uploads/{quote(normalized, safe='/')}"
    try:
        return str(request.url_for("uploads", path=normalized))
    except NoMatchFound:
        return f"/uploads/{quote(normalized, safe='/')}"


def storage_key_from_url(value: str) -> str | None:
    raw_value = value.strip()
    if not raw_value:
        return None

    parsed = urlsplit(raw_value)
    raw_path = parsed.path or raw_value
    if raw_path.startswith("uploads/"):
        raw_path = f"/{raw_path}"
    if raw_path.startswith("/uploads/"):
        try:
            return _normalize_key(raw_path.removeprefix("/uploads/"))
        except ValueError:
            return None

    if settings.s3_public_url:
        public = urlsplit(settings.s3_public_url.rstrip("/"))
        if parsed.scheme in {"http", "https"} and parsed.netloc == public.netloc:
            public_path = public.path.rstrip("/")
            if not public_path or raw_path == public_path or raw_path.startswith(f"{public_path}/"):
                key = raw_path.removeprefix(public_path).lstrip("/") if public_path else raw_path.lstrip("/")
                try:
                    return _normalize_key(key)
                except ValueError:
                    return None
    return None


def thumbnail_key_for_key(key: str) -> str:
    normalized = _normalize_key(key)
    path = PurePosixPath(normalized)
    if len(path.parts) < 2:
        return _join_key(path.parent.as_posix(), "thumbs", f"{path.stem}.jpg")
    return _join_key(path.parts[0], "thumbs", f"{path.stem}.jpg")


def _s3_client():
    global _S3_CLIENT
    missing = [
        name
        for name, value in (
            ("S3_BUCKET", settings.s3_bucket),
            ("S3_ACCESS_KEY_ID", settings.s3_access_key_id),
            ("S3_SECRET_ACCESS_KEY", settings.s3_secret_access_key),
        )
        if not value
    ]
    if missing:
        raise _storage_error(f"Missing S3 settings: {', '.join(missing)}")
    if _S3_CLIENT is None:
        _S3_CLIENT = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint_url,
            aws_access_key_id=settings.s3_access_key_id,
            aws_secret_access_key=settings.s3_secret_access_key,
            region_name=settings.s3_region,
            config=S3_CONFIG,
        )
    return _S3_CLIENT


def put_object(key: str, data: bytes | BinaryIO, content_type: str) -> str:
    normalized = _normalize_key(key)
    if settings.storage_backend == "s3":
        body = data if hasattr(data, "read") else BytesIO(data)
        try:
            _s3_client().upload_fileobj(
                body,
                settings.s3_bucket,
                normalized,
                ExtraArgs={"ContentType": content_type},
            )
        except (BotoCoreError, ClientError) as exc:
            raise _storage_error() from exc
        return normalized

    file_path = (Path(settings.upload_dir) / Path(normalized)).resolve()
    upload_root = Path(settings.upload_dir).resolve()
    if upload_root not in file_path.parents:
        raise _storage_error("Invalid local upload path")
    file_path.parent.mkdir(parents=True, exist_ok=True)
    if hasattr(data, "read"):
        file_path.write_bytes(data.read())
    else:
        file_path.write_bytes(data)
    return normalized


def get_object(key: str) -> StoredObject:
    normalized = _normalize_key(key)
    if settings.storage_backend == "s3":
        try:
            response = _s3_client().get_object(Bucket=settings.s3_bucket, Key=normalized)
            body = response["Body"].read()
        except (BotoCoreError, ClientError) as exc:
            local_file = local_path_for_key(normalized)
            if local_file is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Stored object was not found") from exc
            return StoredObject(data=local_file.read_bytes(), content_type=_content_type_for_key(normalized))
        return StoredObject(data=body, content_type=response.get("ContentType") or _content_type_for_key(normalized))

    file_path = local_path_for_key(normalized)
    if file_path is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Stored object was not found")
    return StoredObject(data=file_path.read_bytes(), content_type=_content_type_for_key(normalized))


def delete_objects(keys: list[str]) -> None:
    normalized_keys = []
    for key in keys:
        try:
            normalized_keys.append(_normalize_key(key))
        except ValueError:
            continue
    if not normalized_keys:
        return

    if settings.storage_backend == "s3":
        try:
            _s3_client().delete_objects(
                Bucket=settings.s3_bucket,
                Delete={"Objects": [{"Key": key} for key in normalized_keys], "Quiet": True},
            )
        except (BotoCoreError, ClientError):
            pass

    for key in normalized_keys:
        file_path = local_path_for_key(key)
        if file_path is None:
            continue
        try:
            file_path.unlink(missing_ok=True)
        except OSError:
            continue


def local_path_for_key(key: str) -> Path | None:
    try:
        normalized = _normalize_key(key)
    except ValueError:
        return None
    upload_root = Path(settings.upload_dir).resolve()
    file_path = (upload_root / Path(normalized)).resolve()
    if upload_root not in file_path.parents or not file_path.is_file():
        return None
    return file_path


def local_response_for_key(key: str) -> FileResponse | None:
    file_path = local_path_for_key(key)
    if file_path is not None:
        return FileResponse(file_path, media_type=_content_type_for_key(key))

    try:
        normalized = _normalize_key(key)
    except ValueError:
        return None
    path = PurePosixPath(normalized)
    parts = path.parts
    if len(parts) != 3 or parts[1] != "thumbs":
        return None

    upload_root = Path(settings.upload_dir).resolve()
    folder = parts[0]
    stem = path.stem
    source_file = next(
        (
            candidate
            for suffix in (".jpg", ".jpeg", ".png", ".webp", ".gif")
            for candidate in [(upload_root / folder / f"{stem}{suffix}").resolve()]
            if upload_root in candidate.parents and candidate.is_file()
        ),
        None,
    )
    if source_file is None:
        return None

    target_file = (upload_root / Path(normalized)).resolve()
    if upload_root not in target_file.parents:
        return None
    try:
        with Image.open(source_file) as image:
            if image.format == "GIF":
                image.seek(0)
            thumbnail_data = _encode_jpeg(image, settings.image_thumbnail_max_dimension)
        target_file.parent.mkdir(parents=True, exist_ok=True)
        target_file.write_bytes(thumbnail_data)
    except (OSError, UnidentifiedImageError):
        return None
    return FileResponse(target_file, media_type=JPEG_CONTENT_TYPE)


def _content_type_for_key(key: str) -> str:
    suffix = PurePosixPath(key).suffix.lower()
    return {
        ".gif": "image/gif",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".webm": "audio/webm",
        ".ogg": "audio/ogg",
        ".mp3": "audio/mpeg",
        ".m4a": "audio/mp4",
        ".aac": "audio/aac",
        ".wav": "audio/wav",
    }.get(suffix, "application/octet-stream")


def object_exists(key: str) -> bool:
    normalized = _normalize_key(key)
    if settings.storage_backend == "s3":
        try:
            _s3_client().head_object(Bucket=settings.s3_bucket, Key=normalized)
            return True
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if code in {"404", "NoSuchKey", "NotFound"}:
                return False
            raise _storage_error() from exc
        except BotoCoreError as exc:
            raise _storage_error() from exc
    return local_path_for_key(normalized) is not None


def _prepare_image_for_jpeg(image: Image.Image, max_dimension: int) -> Image.Image:
    image = ImageOps.exif_transpose(image)
    image.thumbnail((max_dimension, max_dimension), Image.Resampling.LANCZOS)
    if image.mode in {"RGBA", "LA", "P"} or "transparency" in image.info:
        background = Image.new("RGB", image.size, (255, 255, 255))
        background.paste(image.convert("RGBA"), mask=image.convert("RGBA").getchannel("A"))
        return background
    return image.convert("RGB")


def _encode_jpeg(image: Image.Image, max_dimension: int) -> bytes:
    output = BytesIO()
    _prepare_image_for_jpeg(image, max_dimension).save(
        output,
        format="JPEG",
        quality=settings.image_quality,
        optimize=True,
        progressive=True,
    )
    return output.getvalue()


def _verified_image(content: bytes) -> tuple[str, str]:
    try:
        with Image.open(BytesIO(content)) as image:
            image.verify()
            image_format = image.format or ""
    except (UnidentifiedImageError, OSError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Uploaded file is not a valid image") from exc

    content_type = IMAGE_FORMAT_CONTENT_TYPES.get(image_format.upper())
    if content_type not in ALLOWED_IMAGE_TYPES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Only png, jpeg, webp, and gif images are allowed")
    return image_format.upper(), content_type


def process_image_upload(content: bytes, folder: str, object_id: str) -> ProcessedImageUpload:
    if not content:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Uploaded file is empty")
    if len(content) > MAX_IMAGE_UPLOAD_BYTES:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="Image size must not exceed 3MB")

    image_format, _detected_content_type = _verified_image(content)
    folder = _normalize_key(folder)
    if image_format == "GIF":
        key = _join_key(folder, f"{object_id}.gif")
        data = content
        content_type = "image/gif"
    else:
        key = _join_key(folder, f"{object_id}.jpg")
        with Image.open(BytesIO(content)) as image:
            data = _encode_jpeg(image, settings.image_max_dimension)
        content_type = JPEG_CONTENT_TYPE

    thumbnail_key = thumbnail_key_for_key(key)
    try:
        with Image.open(BytesIO(content)) as image:
            if image_format == "GIF":
                image.seek(0)
            thumbnail_data = _encode_jpeg(image, settings.image_thumbnail_max_dimension)
    except (UnidentifiedImageError, OSError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Image thumbnail could not be generated") from exc

    return ProcessedImageUpload(
        key=key,
        data=data,
        content_type=content_type,
        thumbnail_key=thumbnail_key,
        thumbnail_data=thumbnail_data,
        thumbnail_content_type=JPEG_CONTENT_TYPE,
        filename=PurePosixPath(key).name,
    )


def process_legacy_image(content: bytes, folder: str, object_id: str) -> ProcessedImageUpload:
    if not content:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Uploaded file is empty")

    image_format, _detected_content_type = _verified_image(content)
    folder = _normalize_key(folder)
    if image_format == "GIF":
        key = _join_key(folder, f"{object_id}.gif")
        data = content
        content_type = "image/gif"
    else:
        key = _join_key(folder, f"{object_id}.jpg")
        with Image.open(BytesIO(content)) as image:
            data = _encode_jpeg(image, settings.image_max_dimension)
        content_type = JPEG_CONTENT_TYPE

    thumbnail_key = thumbnail_key_for_key(key)
    try:
        with Image.open(BytesIO(content)) as image:
            if image_format == "GIF":
                image.seek(0)
            thumbnail_data = _encode_jpeg(image, settings.image_thumbnail_max_dimension)
    except (UnidentifiedImageError, OSError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Image thumbnail could not be generated") from exc

    return ProcessedImageUpload(
        key=key,
        data=data,
        content_type=content_type,
        thumbnail_key=thumbnail_key,
        thumbnail_data=thumbnail_data,
        thumbnail_content_type=JPEG_CONTENT_TYPE,
        filename=PurePosixPath(key).name,
    )


def store_processed_image(image: ProcessedImageUpload) -> None:
    put_object(image.key, image.data, image.content_type)
    put_object(image.thumbnail_key, image.thumbnail_data, image.thumbnail_content_type)


def process_audio_upload(content: bytes, folder: str, object_id: str, content_type: str | None) -> ProcessedAudioUpload:
    if not content:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Uploaded file is empty")
    if len(content) > MAX_AUDIO_UPLOAD_BYTES:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="Voice size must not exceed 2MB")

    normalized_content_type = (content_type or "").split(";", 1)[0].strip().lower()
    suffix = ALLOWED_AUDIO_TYPES.get(normalized_content_type)
    if suffix is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Only webm, ogg, mp3, m4a, aac, and wav audio are allowed")

    folder = _normalize_key(folder)
    key = _join_key(folder, f"{object_id}{suffix}")
    return ProcessedAudioUpload(key=key, data=content, content_type=normalized_content_type, filename=PurePosixPath(key).name)


def store_processed_audio(audio: ProcessedAudioUpload) -> None:
    put_object(audio.key, audio.data, audio.content_type)


def create_compressed_sticker(source_key: str, object_id: str) -> tuple[str, str]:
    source = get_object(source_key)
    _verified_image(source.data)
    storage_key = _join_key("stickers", f"{object_id}.jpg")
    with Image.open(BytesIO(source.data)) as image:
        data = _encode_jpeg(image, settings.image_max_dimension)
    with Image.open(BytesIO(source.data)) as image:
        thumbnail_data = _encode_jpeg(image, settings.image_thumbnail_max_dimension)
    put_object(storage_key, data, JPEG_CONTENT_TYPE)
    put_object(thumbnail_key_for_key(storage_key), thumbnail_data, JPEG_CONTENT_TYPE)
    return storage_key, JPEG_CONTENT_TYPE
