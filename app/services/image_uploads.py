"""聊天图片直传（docs/chat-features.md「图片直传」）。

后端到对象存储的带宽很小、时快时慢：图片经后端转一道（收 multipart → 压缩 → 传主图和缩略图）时常超时，
客户端显示「发送失败」。直传时客户端自己压缩、生成缩略图，用预签名 PUT 直接传到最终的 key，后端只做三件事：

1. 开单：查大小 / 格式，签两条 PUT（签名里固定了 Content-Type、Content-Length、Cache-Control）。
2. complete：head 一下两个对象核对大小，主图只读开头一段（格式、尺寸、元数据段都在这里），缩略图整张读。
   都合格就原样用——后端不上传任何东西。不合格（老客户端、带 EXIF 定位、尺寸超了、读不出头……）就把主图
   整张拉回来，按 multipart 上传那条路重新处理、覆盖写回，结果和 multipart 上传完全一样。
3. 过期清理：开单 1 小时内没 complete 的，连同桶里的对象一起删。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from fastapi import HTTPException, Request, status
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import AsyncSessionLocal, release_connection
from app.models.image_upload import ImageUpload
from app.models.user import User
from app.services import storage

logger = logging.getLogger(__name__)

UTC = timezone.utc
# 上传单的有效期；预签名 URL 的有效期（URL 先过期，留时间 complete）
UPLOAD_TTL_SECONDS = 60 * 60
UPLOAD_URL_TTL_SECONDS = 15 * 60
# 每人同时开着、还没 complete 的上传单上限（一条消息最多 9 张图，留够余量）
MAX_PENDING_PER_USER = 30
SWEEP_INTERVAL_SECONDS = 10 * 60
WORKER_STARTUP_DELAY_SECONDS = 90
SWEEP_BATCH = 200
# 开放直传的目录：只有聊天图片（/uploads/message-image 用的 messages/）
FOLDERS = frozenset({"messages"})

_worker_task: asyncio.Task | None = None


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(value: datetime) -> datetime:
    # SQLite 读回来的 DateTime 不带时区
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _error(status_code: int, code: str, message: str, **extra: Any) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message, **extra})


def upload_view(key: str, thumbnail_key: str, content_type: str, request: Request | None) -> dict[str, Any]:
    """和 multipart 上传返回的 UploadRead 字段一样。"""
    return {
        "url": storage.public_url_for_key(key, request),
        "thumbnail_url": storage.public_url_for_key(thumbnail_key, request),
        "filename": key.rsplit("/", 1)[-1],
        "content_type": content_type,
    }


async def open_upload(db: AsyncSession, user: User, folder: str, content_type: str, size: int, thumbnail_size: int) -> dict[str, Any]:
    if folder not in FOLDERS:
        raise ValueError(f"direct image upload is not enabled for {folder!r}")
    if not storage.direct_image_upload_enabled():
        raise _error(status.HTTP_409_CONFLICT, "direct_upload_unavailable", "服务器没开图片直传，请用普通上传")
    content_type = (content_type or "").strip().lower()
    if content_type not in storage.DIRECT_IMAGE_TYPES:
        raise _error(status.HTTP_422_UNPROCESSABLE_ENTITY, "unsupported_image_type", "直传只收 JPEG 和 GIF，别的格式请用普通上传")
    limits = storage.image_upload_limits(folder)
    if size <= 0 or thumbnail_size <= 0:
        raise _error(status.HTTP_400_BAD_REQUEST, "empty_file", "图片是空的")
    if size > limits.max_bytes:
        raise _error(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "image_too_large", "图片太大了", max_bytes=limits.max_bytes,
        )
    if thumbnail_size > storage.DIRECT_THUMBNAIL_MAX_BYTES:
        raise _error(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "thumbnail_too_large", "缩略图太大了",
            max_bytes=storage.DIRECT_THUMBNAIL_MAX_BYTES,
        )
    now = _now()
    pending = await db.scalar(
        select(func.count()).select_from(ImageUpload).where(ImageUpload.user_id == user.id, ImageUpload.expires_at > now)
    )
    if (pending or 0) >= MAX_PENDING_PER_USER:
        raise _error(status.HTTP_429_TOO_MANY_REQUESTS, "too_many_pending_uploads", "同时上传的图片太多了，稍后再试")

    object_id = uuid4().hex
    _, extension = storage.DIRECT_IMAGE_TYPES[content_type]
    key = f"{folder}/{object_id}.{extension}"
    thumbnail_key = storage.thumbnail_key_for_key(key)
    # 预签名只是本地算签名，不发网络请求
    image_url, image_headers = storage.presign_put(key, size, UPLOAD_URL_TTL_SECONDS, content_type)
    thumb_url, thumb_headers = storage.presign_put(thumbnail_key, thumbnail_size, UPLOAD_URL_TTL_SECONDS, storage.JPEG_CONTENT_TYPE)
    row = ImageUpload(
        id=uuid4().hex,
        user_id=user.id,
        folder=folder,
        storage_key=key,
        thumbnail_key=thumbnail_key,
        content_type=content_type,
        size=size,
        thumbnail_size=thumbnail_size,
        created_at=now,
        expires_at=now + timedelta(seconds=UPLOAD_TTL_SECONDS),
    )
    db.add(row)
    await db.commit()
    url_expires_at = datetime.now(UTC) + timedelta(seconds=UPLOAD_URL_TTL_SECONDS)
    return {
        "upload_id": row.id,
        "image": {"method": "PUT", "url": image_url, "headers": image_headers},
        "thumbnail": {"method": "PUT", "url": thumb_url, "headers": thumb_headers},
        "expires_at": url_expires_at,
    }


def _verify_and_finish(row: ImageUpload) -> tuple[str, str, str, str | None]:
    """在线程里跑（会发网络请求、可能解码）。返回 (主图 key, 缩略图 key, content_type, 重新处理的原因或 None)。"""
    sizes = (storage.object_size(row.storage_key), storage.object_size(row.thumbnail_key))
    if sizes[0] is None or sizes[1] is None:
        raise _error(status.HTTP_409_CONFLICT, "upload_incomplete", "图片还没传完")
    if sizes[0] != row.size or sizes[1] != row.thumbnail_size:
        storage.delete_objects([row.storage_key, row.thumbnail_key])
        raise _error(status.HTTP_422_UNPROCESSABLE_ENTITY, "upload_size_mismatch", "传上来的大小和开单时不一致")

    limits = storage.image_upload_limits(row.folder)
    head = storage.read_object_prefix(row.storage_key, storage.DIRECT_IMAGE_PROBE_BYTES)
    problem = storage.direct_image_problem(head, row.content_type, limits)
    if problem is None:
        problem = storage.direct_thumbnail_problem(storage.read_object_prefix(row.thumbnail_key, storage.DIRECT_THUMBNAIL_MAX_BYTES))
        if problem is not None:
            problem = f"thumbnail_{problem}"
    if problem is None:
        return row.storage_key, row.thumbnail_key, row.content_type, None

    # 退回后端处理：和 multipart 上传是同一个函数，同一个 object_id（JPEG 写回原 key；GIF 原 key 不变）
    try:
        content = head if row.size <= len(head) else storage.get_object(row.storage_key, max_bytes=limits.max_bytes).data
        object_id = row.storage_key.rsplit("/", 1)[-1].split(".", 1)[0]
        processed = storage.process_image_upload(content, row.folder, object_id, limits)
    except HTTPException:
        storage.delete_objects([row.storage_key, row.thumbnail_key])
        raise
    if problem.startswith("thumbnail_") and processed.key == row.storage_key:
        # 主图本身没问题：只换掉缩略图，主图不用再传一遍（动图可能有好几 MB）
        storage.put_object(processed.thumbnail_key, processed.thumbnail_data, processed.thumbnail_content_type)
        return row.storage_key, processed.thumbnail_key, row.content_type, problem
    storage.store_processed_image(processed)
    if processed.key != row.storage_key:
        storage.delete_objects([row.storage_key])
    return processed.key, processed.thumbnail_key, processed.content_type, problem


async def complete_upload(db: AsyncSession, user: User, upload_id: str, request: Request | None) -> dict[str, Any]:
    row = await db.get(ImageUpload, upload_id)
    if row is None or row.user_id != user.id:
        raise _error(status.HTTP_404_NOT_FOUND, "upload_not_found", "上传单不存在或已经完成")
    if _aware(row.expires_at) <= _now():
        raise _error(status.HTTP_410_GONE, "upload_expired", "上传单过期了，请重新上传")
    # 先把字段取出来：线程里不碰 ORM 会话
    snapshot = ImageUpload(
        id=row.id, user_id=row.user_id, folder=row.folder, storage_key=row.storage_key, thumbnail_key=row.thumbnail_key,
        content_type=row.content_type, size=row.size, thumbnail_size=row.thumbnail_size,
        created_at=row.created_at, expires_at=row.expires_at,
    )
    # 校验要等对象存储（几百毫秒到几秒），先把数据库连接还回去
    await release_connection(db)
    try:
        key, thumbnail_key, content_type, reprocessed = await storage.run_image_worker(_verify_and_finish, snapshot)
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        if detail.get("code") != "upload_incomplete":
            # 对象已经删了（或者根本不是图片）：上传单作废，再 complete 一次 404
            await db.execute(delete(ImageUpload).where(ImageUpload.id == upload_id))
            await db.commit()
        raise
    except OSError as exc:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Image could not be saved") from exc
    await db.execute(delete(ImageUpload).where(ImageUpload.id == upload_id))
    await db.commit()
    if reprocessed:
        logger.info("Direct image upload %s reprocessed by the server: %s", upload_id, reprocessed)
    return upload_view(key, thumbnail_key, content_type, request)


async def sweep(db: AsyncSession, now: datetime | None = None) -> int:
    """删过期的上传单，连同桶里可能已经传上去、但没 complete 的对象。"""
    now = now or _now()
    rows = (
        await db.execute(select(ImageUpload).where(ImageUpload.expires_at <= now).order_by(ImageUpload.expires_at).limit(SWEEP_BATCH))
    ).scalars().all()
    if not rows:
        return 0
    keys = [key for row in rows for key in (row.storage_key, row.thumbnail_key)]
    await db.execute(delete(ImageUpload).where(ImageUpload.id.in_([row.id for row in rows])))
    await db.commit()
    await asyncio.to_thread(storage.delete_objects, keys)
    return len(rows)


async def _worker() -> None:
    delay = WORKER_STARTUP_DELAY_SECONDS
    while True:
        await asyncio.sleep(delay)
        try:
            async with AsyncSessionLocal() as db:
                removed = await sweep(db)
                if removed:
                    logger.info("Image upload sweep removed %s stale direct uploads", removed)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Image upload sweep failed")
        delay = SWEEP_INTERVAL_SECONDS


def start_image_upload_worker() -> None:
    global _worker_task
    if _worker_task is not None and not _worker_task.done():
        return
    _worker_task = asyncio.create_task(_worker())


async def stop_image_upload_worker() -> None:
    global _worker_task
    task = _worker_task
    _worker_task = None
    if task is None or task.done():
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


# ---------------------------------------------------------------- 中转直传（docs/chat-features.md「图片直传」的「中转」）
# 有的网页（共创世界）用 CSP 限制了能连的域名，客户端连不上对象存储，预签名 PUT 会被浏览器直接拦下。
# 这时客户端照样在本地压缩好主图和缩略图，一个请求交给后端；后端在内存里按直传同一套规则校验，
# 合格就原样写进桶 —— 不重新编码、不回读对象存储，经服务器的流量就是压缩好的那点字节。
# 不合格的退回普通上传的处理（process_image_upload），结果和 /uploads/message-image 一样。


def relay_and_store(image: bytes, content_type: str, thumbnail: bytes, folder: str) -> tuple[str, str, str, str | None]:
    """在线程里跑。返回 (主图 key, 缩略图 key, content_type, 重新处理的原因或 None)。"""
    limits = storage.image_upload_limits(folder)
    object_id = uuid4().hex
    content_type = (content_type or "").strip().lower()
    problem: str | None
    if content_type not in storage.DIRECT_IMAGE_TYPES:
        problem = "unsupported_type"
    elif len(thumbnail) == 0 or len(thumbnail) > storage.DIRECT_THUMBNAIL_MAX_BYTES:
        problem = "thumbnail_size"
    else:
        problem = storage.direct_image_problem(image[: storage.DIRECT_IMAGE_PROBE_BYTES], content_type, limits)
        if problem is None:
            problem = storage.relay_image_body_problem(image, content_type)
        if problem is None:
            thumb_problem = storage.direct_thumbnail_problem(thumbnail)
            problem = f"thumbnail_{thumb_problem}" if thumb_problem else None
    if problem is None:
        _, extension = storage.DIRECT_IMAGE_TYPES[content_type]
        key = f"{folder}/{object_id}.{extension}"
        thumbnail_key = storage.thumbnail_key_for_key(key)
        storage.put_object(key, image, content_type)
        storage.put_object(thumbnail_key, thumbnail, storage.JPEG_CONTENT_TYPE)
        return key, thumbnail_key, content_type, None
    processed = storage.process_image_upload(image, folder, object_id, limits)
    storage.store_processed_image(processed)
    return processed.key, processed.thumbnail_key, processed.content_type, problem
