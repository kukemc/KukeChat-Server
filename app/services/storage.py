from __future__ import annotations

import asyncio
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Callable
from urllib.parse import quote, unquote, urlsplit

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import HTTPException, Request, status
from fastapi.responses import FileResponse
from PIL import Image, ImageOps, ImageSequence, UnidentifiedImageError
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
# Pixel caps apply before verify/load, including GIF first-frame thumbnails and
# old local files; Pillow's default decompression warning is not a hard limit.
MAX_IMAGE_PIXELS = 12_000_000
MAX_LEGACY_IMAGE_BYTES = 12 * 1024 * 1024
# 动态 WebP / APNG 转成 GIF 时的上限：帧数、所有帧加起来的像素数（超了就和以前一样只留第一帧）
MAX_ANIMATED_FRAMES = 300
MAX_ANIMATED_TOTAL_PIXELS = 40_000_000


@dataclass(frozen=True)
class ImageUploadLimits:
    """一条上传路径的图片限制：字节数、最长边、解码像素上限、JPEG 质量。

    默认值就是原来的全局常量，所以除了显式指定的目录，其它路径的行为与
    放宽之前逐字节一致（见 _IMAGE_UPLOAD_LIMITS / image_upload_limits）。
    """

    max_bytes: int = MAX_IMAGE_UPLOAD_BYTES
    max_dimension: int | None = None
    max_pixels: int = MAX_IMAGE_PIXELS
    quality: int | None = None


DEFAULT_IMAGE_UPLOAD_LIMITS = ImageUploadLimits()


def _announcement_image_limits() -> ImageUploadLimits:
    return ImageUploadLimits(
        max_bytes=settings.image_announcement_max_bytes,
        max_dimension=settings.image_announcement_max_dimension,
        max_pixels=settings.image_announcement_max_pixels,
        quality=settings.image_announcement_quality,
    )


# 只有公告图放宽（长图缩到 2048 会把字糊掉）；其余目录一律回落默认限制。
_IMAGE_UPLOAD_FOLDER_LIMITS: dict[str, Callable[[], ImageUploadLimits]] = {
    "announcements": _announcement_image_limits,
}

# A worker retains at most one decoded image at a time. Threads already running
# remain counted if their awaiting request is cancelled.
IMAGE_WORKERS = asyncio.Semaphore(2)
JPEG_CONTENT_TYPE = "image/jpeg"
S3_CONFIG = Config(connect_timeout=3, read_timeout=10, retries={"max_attempts": 2, "mode": "standard"})
# Match the local /uploads cache policy; never cache unrelated or private objects.
_PUBLIC_IMAGE_FOLDERS = frozenset({"avatars", "profile-covers", "messages", "posts", "teamup", "announcements", "stickers"})
_PUBLIC_IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".webp", ".gif"})
_PUBLIC_AUDIO_SUFFIXES = frozenset({".webm", ".ogg", ".mp3", ".m4a", ".aac", ".wav"})
_PUBLIC_CACHE_CONTROL = "public, max-age=3600"
_S3_CLIENT = None
_S3_PRESIGN_CLIENT = None
# 私有桶配了单独的凭据时用这两个（见 _client_for）
_S3_PRIVATE_CLIENT = None
_S3_PRIVATE_PRESIGN_CLIENT = None
# 群文件（docs/group-plus.md 第 5 节）：配了 S3_PRIVATE_BUCKET 时这个前缀下的对象放私有桶，其余照旧放主桶（主桶开着公开读）
PRIVATE_PREFIX = "group-files/"
# 「我的设备」会话的临时文件（docs/self-chat.md 第 5 节）同样只能鉴权下载，和群文件一起放私有桶
SELF_TRANSFER_PREFIX = "self-transfers/"
PRIVATE_PREFIXES = (PRIVATE_PREFIX, SELF_TRANSFER_PREFIX)
OCTET_STREAM = "application/octet-stream"


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


def image_upload_limits(folder: str) -> ImageUploadLimits:
    """按目标目录取图片限制。未知目录拿到的就是原来的全局默认值。"""
    try:
        normalized = _normalize_key(folder)
    except ValueError:
        return DEFAULT_IMAGE_UPLOAD_LIMITS
    provider = _IMAGE_UPLOAD_FOLDER_LIMITS.get(normalized)
    return provider() if provider is not None else DEFAULT_IMAGE_UPLOAD_LIMITS


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


def _bucket_for(normalized: str) -> str | None:
    """这个 key 放哪个桶：群文件 / 「我的设备」临时文件前缀且配了私有桶 → 私有桶；否则主桶。endpoint 共用；凭据见 _client_for。"""
    if settings.s3_private_bucket and normalized.startswith(PRIVATE_PREFIXES):
        return settings.s3_private_bucket
    return settings.s3_bucket


def _private_credentials() -> tuple[str, str] | None:
    """私有桶单独的凭据（两项都配了才算）；没配返回 None，私有桶和主桶共用一对凭据。"""
    key_id, secret = settings.s3_private_access_key_id, settings.s3_private_secret_access_key
    return (key_id, secret) if key_id and secret else None


def _client_for(bucket: str | None):
    """访问某个桶用的客户端：私有桶且配了单独凭据 → 私有桶客户端；其余用主客户端。"""
    global _S3_PRIVATE_CLIENT
    credentials = _private_credentials()
    if not (credentials and settings.s3_private_bucket and bucket == settings.s3_private_bucket):
        return _s3_client()
    _s3_client()  # 复用缺配置的检查
    if _S3_PRIVATE_CLIENT is None:
        _S3_PRIVATE_CLIENT = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint_url,
            aws_access_key_id=credentials[0],
            aws_secret_access_key=credentials[1],
            region_name=settings.s3_region,
            config=S3_CONFIG,
        )
    return _S3_PRIVATE_CLIENT


def direct_transfer_enabled() -> bool:
    """群文件直传：S3 存储且配了私有桶时打开，客户端用预签名 URL 直接和桶传输，不经过后端。"""
    return settings.storage_backend == "s3" and bool(settings.s3_private_bucket)


def _s3_presign_client():
    """预签名专用的客户端：固定 SigV4（不指定时 boto3 对自定义 endpoint 可能签成 V2，V2 不签 Content-Length）。"""
    global _S3_PRESIGN_CLIENT
    _s3_client()  # 复用缺配置的检查
    if _S3_PRESIGN_CLIENT is None:
        _S3_PRESIGN_CLIENT = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint_url,
            aws_access_key_id=settings.s3_access_key_id,
            aws_secret_access_key=settings.s3_secret_access_key,
            region_name=settings.s3_region,
            config=S3_CONFIG.merge(Config(signature_version="s3v4")),
        )
    return _S3_PRESIGN_CLIENT


def _presign_client_for(bucket: str | None):
    """预签名用的客户端，选凭据的规则同 _client_for。"""
    global _S3_PRIVATE_PRESIGN_CLIENT
    credentials = _private_credentials()
    if not (credentials and settings.s3_private_bucket and bucket == settings.s3_private_bucket):
        return _s3_presign_client()
    _s3_client()
    if _S3_PRIVATE_PRESIGN_CLIENT is None:
        _S3_PRIVATE_PRESIGN_CLIENT = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint_url,
            aws_access_key_id=credentials[0],
            aws_secret_access_key=credentials[1],
            region_name=settings.s3_region,
            config=S3_CONFIG.merge(Config(signature_version="s3v4")),
        )
    return _S3_PRIVATE_PRESIGN_CLIENT


def presign_put(key: str, size: int, expires_in: int = 900) -> tuple[str, dict[str, str]]:
    """直传用的预签名 PUT。签名里带 Content-Type 和 Content-Length，长度不对的请求桶会拒（取决于服务商，见 scripts/check_s3_direct.py）。
    返回 (url, 客户端必须带上的请求头)；Content-Length 由客户端的 HTTP 库按请求体自己填。"""
    normalized = _normalize_key(key)
    try:
        url = _presign_client_for(_bucket_for(normalized)).generate_presigned_url(
            "put_object",
            Params={"Bucket": _bucket_for(normalized), "Key": normalized, "ContentType": OCTET_STREAM, "ContentLength": int(size)},
            ExpiresIn=expires_in,
        )
    except (BotoCoreError, ClientError) as exc:
        raise _storage_error() from exc
    return url, {"Content-Type": OCTET_STREAM}


def presign_get(key: str, expires_in: int = 300, content_disposition: str | None = None) -> str:
    """直连下载用的预签名 GET：响应头强制 application/octet-stream 和给定的 Content-Disposition。"""
    normalized = _normalize_key(key)
    params = {"Bucket": _bucket_for(normalized), "Key": normalized, "ResponseContentType": OCTET_STREAM}
    if content_disposition:
        params["ResponseContentDisposition"] = content_disposition
    try:
        return _presign_client_for(params["Bucket"]).generate_presigned_url("get_object", Params=params, ExpiresIn=expires_in)
    except (BotoCoreError, ClientError) as exc:
        raise _storage_error() from exc


def _is_public_media_key(key: str) -> bool:
    parts = PurePosixPath(key).parts
    if len(parts) == 2:
        folder, filename = parts
    elif len(parts) == 3 and parts[1] == "thumbs":
        folder, _, filename = parts
    else:
        return False
    suffix = PurePosixPath(filename).suffix.lower()
    return (folder in _PUBLIC_IMAGE_FOLDERS and suffix in _PUBLIC_IMAGE_SUFFIXES) or (
        folder == "messages" and len(parts) == 2 and suffix in _PUBLIC_AUDIO_SUFFIXES
    )


def put_object(key: str, data: bytes | BinaryIO, content_type: str) -> str:
    normalized = _normalize_key(key)
    if settings.storage_backend == "s3":
        body = data if hasattr(data, "read") else BytesIO(data)
        extra_args = {"ContentType": content_type}
        if _is_public_media_key(normalized):
            extra_args["CacheControl"] = _PUBLIC_CACHE_CONTROL
        try:
            _client_for(_bucket_for(normalized)).upload_fileobj(
                body,
                _bucket_for(normalized),
                normalized,
                ExtraArgs=extra_args,
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
        # 大文件（群文件可能有几百 MB）分块拷，不一次读进内存
        with file_path.open("wb") as target:
            while True:
                chunk = data.read(1024 * 1024)
                if not chunk:
                    break
                target.write(chunk)
    else:
        file_path.write_bytes(data)
    return normalized


def get_object(key: str, max_bytes: int | None = None) -> StoredObject:
    normalized = _normalize_key(key)
    if settings.storage_backend == "s3":
        try:
            response = _client_for(_bucket_for(normalized)).get_object(Bucket=_bucket_for(normalized), Key=normalized)
            stream = response["Body"]
            try:
                if max_bytes is not None and (response.get("ContentLength") or 0) > max_bytes:
                    raise HTTPException(status_code=413, detail="Stored image exceeds 12MB")
                body = stream.read(max_bytes + 1 if max_bytes is not None else -1)
            finally:
                stream.close()
        except (BotoCoreError, ClientError) as exc:
            local_file = local_path_for_key(normalized)
            if local_file is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Stored object was not found") from exc
            return StoredObject(data=_read_local_object(local_file, max_bytes), content_type=_content_type_for_key(normalized))
        if max_bytes is not None and len(body) > max_bytes:
            raise HTTPException(status_code=413, detail="Stored image exceeds 12MB")
        return StoredObject(data=body, content_type=response.get("ContentType") or _content_type_for_key(normalized))

    file_path = local_path_for_key(normalized)
    if file_path is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Stored object was not found")
    return StoredObject(data=_read_local_object(file_path, max_bytes), content_type=_content_type_for_key(normalized))


def open_object_stream(key: str) -> tuple[Any, int | None] | None:
    """打开对象存储里的对象、返回 (流, 字节数)，调用方分块读、读完 close。本地存储返回 None（走 FileResponse）。

    大文件（群文件可能上百 MB）不能像 get_object 那样整份读进内存：几个人同时下载就能把内存吃光。
    在线程里调用（会发网络请求）。对象不存在抛 404。
    """
    normalized = _normalize_key(key)
    if settings.storage_backend != "s3":
        return None
    try:
        response = _client_for(_bucket_for(normalized)).get_object(Bucket=_bucket_for(normalized), Key=normalized)
    except (BotoCoreError, ClientError) as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Stored object was not found") from exc
    length = response.get("ContentLength")
    return response["Body"], int(length) if length is not None else None


def _read_local_object(file_path: Path, max_bytes: int | None) -> bytes:
    with file_path.open("rb") as stream:
        data = stream.read(max_bytes + 1 if max_bytes is not None else -1)
    if max_bytes is not None and len(data) > max_bytes:
        raise HTTPException(status_code=413, detail="Stored image exceeds 12MB")
    return data


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
        by_bucket: dict[str | None, list[str]] = {}
        for key in normalized_keys:
            by_bucket.setdefault(_bucket_for(key), []).append(key)
        for bucket, bucket_keys in by_bucket.items():
            # 一次最多 1000 个（S3 的上限）
            for start in range(0, len(bucket_keys), 1000):
                try:
                    _client_for(bucket).delete_objects(
                        Bucket=bucket,
                        Delete={"Objects": [{"Key": key} for key in bucket_keys[start : start + 1000]], "Quiet": True},
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
            thumbnail_data = _encode_jpeg(image, settings.image_thumbnail_max_dimension, quality=min(settings.image_quality, 65))
        target_file.parent.mkdir(parents=True, exist_ok=True)
        target_file.write_bytes(thumbnail_data)
    except (OSError, UnidentifiedImageError, HTTPException, Image.DecompressionBombError):
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
    return object_size(key) is not None


def object_size(key: str) -> int | None:
    """对象的字节数；不存在返回 None（S3 走 head_object）。"""
    normalized = _normalize_key(key)
    if settings.storage_backend == "s3":
        try:
            response = _client_for(_bucket_for(normalized)).head_object(Bucket=_bucket_for(normalized), Key=normalized)
            return int(response.get("ContentLength") or 0)
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if code in {"404", "NoSuchKey", "NotFound"}:
                return None
            raise _storage_error() from exc
        except BotoCoreError as exc:
            raise _storage_error() from exc
    path = local_path_for_key(normalized)
    return path.stat().st_size if path is not None else None


def _check_image_pixels(image: Image.Image, max_pixels: int = MAX_IMAGE_PIXELS) -> None:
    width, height = image.size
    if width * height > max_pixels:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Image pixel count exceeds {max_pixels // 1_000_000} million",
        )


def _prepare_image_for_jpeg(image: Image.Image, max_dimension: int, max_pixels: int = MAX_IMAGE_PIXELS) -> Image.Image:
    _check_image_pixels(image, max_pixels)
    # JPEG draft decoding reduces the intermediate raster for large JPEGs.
    if image.format == "JPEG":
        image.draft("RGB", (max_dimension, max_dimension))
    # Resize before EXIF transpose to avoid copying a full-resolution raster.
    image.thumbnail((max_dimension, max_dimension), Image.Resampling.LANCZOS)
    image = ImageOps.exif_transpose(image)
    if image.mode in {"RGBA", "LA", "P"} or "transparency" in image.info:
        rgba = image.convert("RGBA")
        background = Image.new("RGB", image.size, (255, 255, 255))
        background.paste(rgba, mask=rgba.getchannel("A"))
        return background
    return image.convert("RGB")


def _encode_jpeg(image: Image.Image, max_dimension: int, *, quality: int | None = None, max_pixels: int = MAX_IMAGE_PIXELS) -> bytes:
    output = BytesIO()
    _prepare_image_for_jpeg(image, max_dimension, max_pixels).save(
        output,
        format="JPEG",
        quality=quality if quality is not None else settings.image_quality,
        optimize=True,
        progressive=True,
    )
    return output.getvalue()


def _animated_to_gif(image: Image.Image, max_dimension: int, max_bytes: int) -> bytes | None:
    """动态 WebP / APNG（常见的是 QQ、微信存下来、扩展名还是 .jpg 的表情）转成 GIF，保住动画。

    所有客户端都只把 .gif 当动图播，所以统一转成 GIF。帧太多、太大或者转完超过上传上限时返回 None，按静态图处理。
    """
    frame_count = getattr(image, "n_frames", 1)
    width, height = image.size
    if frame_count < 2 or frame_count > MAX_ANIMATED_FRAMES or width * height * frame_count > MAX_ANIMATED_TOTAL_PIXELS:
        return None
    frames: list[Image.Image] = []
    durations: list[int] = []
    for frame in ImageSequence.Iterator(image):
        rgba = frame.convert("RGBA")
        rgba.thumbnail((max_dimension, max_dimension), Image.Resampling.LANCZOS)
        frames.append(rgba)
        durations.append(max(20, int(frame.info.get("duration") or 100)))
    output = BytesIO()
    frames[0].save(output, format="GIF", save_all=True, append_images=frames[1:], duration=durations, loop=0, disposal=2)
    data = output.getvalue()
    return data if len(data) <= max_bytes else None


async def run_image_worker(function, *args):
    """Hold the decode slot until its thread finishes, even on cancellation.

    asyncio.to_thread cannot interrupt Pillow or blocking S3 calls; shielding the
    wait prevents cancellation from admitting a third worker while one continues.
    """
    await IMAGE_WORKERS.acquire()
    try:
        worker = asyncio.create_task(asyncio.to_thread(function, *args))
    except BaseException:
        IMAGE_WORKERS.release()
        raise
    def _finish(completed: asyncio.Task) -> None:
        IMAGE_WORKERS.release()
        # Retrieve failures even if the HTTP request was cancelled.
        try:
            completed.exception()
        except asyncio.CancelledError:
            pass

    worker.add_done_callback(_finish)
    return await asyncio.shield(worker)


def _verified_image(content: bytes, max_pixels: int = MAX_IMAGE_PIXELS) -> tuple[str, str]:
    try:
        with Image.open(BytesIO(content)) as image:
            _check_image_pixels(image, max_pixels)
            image.verify()
            image_format = image.format or ""
    except HTTPException:
        raise
    except Exception as exc:
        # Pillow 对损坏的文件会抛各种异常（OSError、SyntaxError、IndexError、struct.error……），
        # 对用户来说都是「这不是一张能用的图片」，不能漏成 500。
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Uploaded file is not a valid image") from exc

    content_type = IMAGE_FORMAT_CONTENT_TYPES.get(image_format.upper())
    if content_type not in ALLOWED_IMAGE_TYPES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Only png, jpeg, webp, and gif images are allowed")
    return image_format.upper(), content_type


def _format_size_mb(value: int) -> str:
    megabytes = value / (1024 * 1024)
    return str(int(megabytes)) if megabytes == int(megabytes) else f"{megabytes:g}"


def process_image_upload(content: bytes, folder: str, object_id: str, limits: ImageUploadLimits | None = None) -> ProcessedImageUpload:
    """limits=None 表示按目录取限制；只有 announcements/ 与默认不同。

    未传 limits 时以目录为准；显式传入时以传入的为准（老调用点和测试都走
    None 这条路，除了 announcements/ 之外拿到的就是原来的全局常量）。
    """
    active = limits if limits is not None else image_upload_limits(folder)
    if not content:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Uploaded file is empty")
    if len(content) > active.max_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Image size must not exceed {_format_size_mb(active.max_bytes)}MB",
        )

    return _process_verified_image(content, folder, object_id, active)


def _process_verified_image(content: bytes, folder: str, object_id: str, limits: ImageUploadLimits | None = None) -> ProcessedImageUpload:
    active = limits if limits is not None else DEFAULT_IMAGE_UPLOAD_LIMITS
    image_format, _detected_content_type = _verified_image(content, active.max_pixels)
    folder = _normalize_key(folder)
    max_dimension = active.max_dimension if active.max_dimension is not None else settings.image_max_dimension
    base_quality = active.quality if active.quality is not None else settings.image_quality
    try:
        with Image.open(BytesIO(content)) as image:
            _check_image_pixels(image, active.max_pixels)
            animated = None
            if image_format in {"WEBP", "PNG"} and getattr(image, "is_animated", False):
                animated = _animated_to_gif(image, max_dimension, active.max_bytes)
            # Preserve every GIF frame; only the first frame is decoded for its thumbnail.
            if image_format == "GIF":
                data = content
                content_type = "image/gif"
            elif animated is not None:
                data = animated
                content_type = "image/gif"
            else:
                # 默认（quality=None）走 min(settings.image_quality, 78)，与放宽前一致；
                # 公告图用自己的质量，见 config.image_announcement_quality。
                data = _encode_jpeg(
                    image,
                    max_dimension,
                    quality=min(base_quality, 78) if active.quality is None else base_quality,
                    max_pixels=active.max_pixels,
                )
                content_type = JPEG_CONTENT_TYPE
        with Image.open(BytesIO(content)) as image:
            thumbnail_data = _encode_jpeg(
                image,
                settings.image_thumbnail_max_dimension,
                quality=min(settings.image_quality, 65),
                max_pixels=active.max_pixels,
            )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Image could not be decoded") from exc

    key = _join_key(folder, f"{object_id}.gif" if content_type == "image/gif" else f"{object_id}.jpg")
    thumbnail_key = thumbnail_key_for_key(key)
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
    if len(content) > MAX_LEGACY_IMAGE_BYTES:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="Legacy image exceeds 12MB")
    # 老图迁移一律按默认限制走，不受公告放宽影响。
    return _process_verified_image(content, folder, object_id, DEFAULT_IMAGE_UPLOAD_LIMITS)


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


def create_compressed_sticker(source_data: bytes, object_id: str) -> tuple[str, str]:
    # The caller already fetched the bounded source in the same image worker.
    if len(source_data) > MAX_LEGACY_IMAGE_BYTES:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="Sticker source exceeds 12MB")
    _verified_image(source_data)
    storage_key = _join_key("stickers", f"{object_id}.jpg")
    try:
        with Image.open(BytesIO(source_data)) as image:
            data = _encode_jpeg(image, settings.image_max_dimension, quality=min(settings.image_quality, 78))
        with Image.open(BytesIO(source_data)) as image:
            thumbnail_data = _encode_jpeg(image, settings.image_thumbnail_max_dimension, quality=min(settings.image_quality, 65))
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
        raise HTTPException(status_code=400, detail="Sticker image could not be decoded") from exc
    put_object(storage_key, data, JPEG_CONTENT_TYPE)
    put_object(thumbnail_key_for_key(storage_key), thumbnail_data, JPEG_CONTENT_TYPE)
    return storage_key, JPEG_CONTENT_TYPE
