"""「我的电脑 / 我的手机」（docs/self-chat.md）：每个人一个只有自己的会话（type = self），在自己的几台设备之间传消息和文件。

会话本身（get_or_create）：每人一个，conversations.self_owner_id 上的唯一索引兜底，两个请求同时建也只会留一个。
会话列表（GET /conversations）里没有时顺手建上，所以每个人的列表里一定有它。

消息照常走 messages.send_message：那边看到 type = self 时拦掉投票 / 红包 / 礼物 / @，并把发送设备（X-Kuke-Platform）
写进 metadata.device；接口层看到 _self_chat 标记就不送机器审核（只有自己看得到）。

临时文件（第 5 节）：任意类型，24 小时后过期，过期后下载 410、存储里的对象删掉；不能收藏 / 转发。和群文件一样放私有前缀
（self-transfers/{用户 id}/{随机名}，配了 S3_PRIVATE_BUCKET 时放私有桶），对外不暴露直链：
开单（pending）→ 客户端 PUT 上传（直传时直接传桶，否则传到后端的 /content）→ complete 核对大小、落一条文件消息（ready）
→ 到期由 worker 删对象（expired）。
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from fastapi import HTTPException, Request, status
from fastapi.responses import Response
from sqlalchemy import delete, func, or_, and_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import AsyncSessionLocal, release_connection
from app.core.device import PLATFORMS, context_for_request
from app.models.conversation import Conversation, ConversationMember
from app.models.enums import ConversationType, MemberRole, MessageType
from app.models.message import Message
from app.models.self_chat import SelfTransfer
from app.models.user import User
from app.services import group_files, storage
from app.services.conversations import require_membership
from app.services.penalties import ensure_can_post

logger = logging.getLogger(__name__)
UTC = timezone.utc

DEFAULT_DEVICE = "web"
# 桌面端 / 网页算一类（「我的电脑」），安卓 / iOS 算一类（「我的手机」）。客户端据此决定气泡放左边还是右边
DEVICE_CLASSES = {"desktop": "computer", "web": "computer", "android": "phone", "ios": "phone"}

FOLDER = storage.SELF_TRANSFER_PREFIX.rstrip("/")
TTL_HOURS = 24
# 开单之后 1 小时内要 complete，否则作废；直传的预签名 PUT 15 分钟有效（过了可以重新开单）；下载链接 5 分钟
UPLOAD_TTL_SECONDS = 3600
UPLOAD_URL_TTL_SECONDS = group_files.UPLOAD_URL_TTL_SECONDS
DOWNLOAD_URL_TTL_SECONDS = group_files.DOWNLOAD_URL_TTL_SECONDS
# 过期之后行再留 7 天（这期间下载返回 410 file_expired，之后 404）
EXPIRED_ROW_RETENTION = timedelta(days=7)
SWEEP_BATCH = 200
SWEEP_INTERVAL_SECONDS = 600.0
WORKER_STARTUP_DELAY_SECONDS = 60.0
_CHUNK = 1024 * 1024

PENDING, READY, EXPIRED = "pending", "ready", "expired"

_worker_task: asyncio.Task | None = None


def _now() -> datetime:
    """现在（UTC）。单独成函数是为了测试里能换掉。"""
    return datetime.now(UTC)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _error(status_code: int, code: str, message: str, **extra: Any) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message, **extra})


def normalize_device(value: str | None) -> str:
    value = (value or "").strip().lower()
    return value if value in PLATFORMS else DEFAULT_DEVICE


def device_of(request: Request) -> str:
    """这个请求是哪种设备发的：X-Kuke-Platform（desktop / web / android / ios），没带时按 X-Kuke-Client 猜，都没有算 web。"""
    return normalize_device(context_for_request(request).platform)


# ---------------------------------------------------------------- 会话


async def find_self_conversation(db: AsyncSession, user_id: int) -> Conversation | None:
    return (
        await db.execute(select(Conversation).where(Conversation.self_owner_id == user_id, Conversation.type == ConversationType.SELF))
    ).scalar_one_or_none()


async def get_or_create(db: AsyncSession, user: User) -> Conversation:
    """这个人的「我的设备」会话，没有就建。幂等；两个请求同时建时唯一索引让后到的那个失败，回头读先建好的那个。"""
    existing = await find_self_conversation(db, user.id)
    if existing is not None:
        return existing
    user_id = user.id
    conversation = Conversation(
        type=ConversationType.SELF,
        self_owner_id=user_id,
        created_by_id=user_id,
        # 不会出现在找群 / 推荐里（那边只看群），这里再挡一层
        join_mode="invite_only",
    )
    db.add(conversation)
    try:
        await db.flush()
        db.add(ConversationMember(conversation_id=conversation.id, user_id=user_id, role=MemberRole.MEMBER))
        await db.commit()
    except IntegrityError:
        await db.rollback()
        existing = await find_self_conversation(db, user_id)
        if existing is None:
            raise
        return existing
    return conversation


async def require_self_conversation(db: AsyncSession, user_id: int, conversation_id: int) -> Conversation:
    """临时文件接口的前置检查：会话是我的「我的设备」会话。不是成员 404（和别的会话接口一样）；是成员但不是这种会话 400。"""
    await require_membership(db, conversation_id, user_id, clear_expired_mute=False)
    conversation = await db.get(Conversation, conversation_id)
    if conversation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")
    if conversation.type != ConversationType.SELF or conversation.self_owner_id != user_id:
        raise _error(status.HTTP_400_BAD_REQUEST, "not_self_conversation", "临时文件只能在「我的设备」会话里传")
    return conversation


async def is_self_conversation(db: AsyncSession, conversation_id: int) -> bool:
    conversation = await db.get(Conversation, conversation_id)
    return conversation is not None and conversation.type == ConversationType.SELF


# ---------------------------------------------------------------- 临时文件：视图


def _state(row: SelfTransfer, now: datetime) -> str:
    if row.state == READY and (expires := _aware(row.expires_at)) is not None and expires <= now:
        return EXPIRED
    return row.state


def transfer_view(row: SelfTransfer, now: datetime | None = None) -> dict[str, Any]:
    now = now or _now()
    state = _state(row, now)
    return {
        "id": row.id,
        "conversation_id": row.conversation_id,
        "name": row.name,
        "size": int(row.size),
        "content_type": row.content_type,
        "device": row.device,
        "state": state,
        "expired": state == EXPIRED,
        "created_at": _aware(row.created_at),
        "upload_expires_at": _aware(row.upload_expires_at),
        "completed_at": _aware(row.completed_at),
        "expires_at": _aware(row.expires_at),
        "message_id": row.message_id,
        "download_path": f"/api/v1/conversations/{row.conversation_id}/transfers/{row.id}/download",
    }


def transfer_metadata_view(raw: dict[str, Any]) -> dict[str, Any]:
    """消息 metadata.transfer 返回给客户端时现场补上 expired（按 expires_at 和现在比）。不查库：撤回之类提前删掉的，消息本身已经撤回了。"""
    expired = True
    value = raw.get("expires_at")
    if isinstance(value, str):
        try:
            expired = _aware(datetime.fromisoformat(value.replace("Z", "+00:00"))) <= _now()
        except ValueError:
            expired = True
    return {**raw, "expired": expired}


def _limits(used: int) -> dict[str, Any]:
    return {
        "max_file_bytes": settings.self_transfer_max_bytes,
        "quota_bytes": settings.self_transfer_quota_bytes,
        "used_bytes": used,
        "free_bytes": max(0, settings.self_transfer_quota_bytes - used),
        "ttl_hours": TTL_HOURS,
        "direct": storage.direct_transfer_enabled(),
    }


def _live(now: datetime) -> Any:
    """还占着存储的：没作废的上传单，和没过期的文件。"""
    return or_(
        and_(SelfTransfer.state == PENDING, SelfTransfer.upload_expires_at > now),
        and_(SelfTransfer.state == READY, SelfTransfer.expires_at > now),
    )


async def _used_bytes(db: AsyncSession, owner_id: int, now: datetime) -> int:
    return int(
        await db.scalar(select(func.coalesce(func.sum(SelfTransfer.size), 0)).where(SelfTransfer.owner_id == owner_id, _live(now))) or 0
    )


async def list_transfers(db: AsyncSession, user: User, conversation_id: int) -> dict[str, Any]:
    """GET /conversations/{cid}/transfers：还能下载的文件（新的在前，最多 100 个）和额度。"""
    await require_self_conversation(db, user.id, conversation_id)
    now = _now()
    rows = (
        await db.execute(
            select(SelfTransfer)
            .where(SelfTransfer.owner_id == user.id, SelfTransfer.state == READY, SelfTransfer.expires_at > now)
            .order_by(SelfTransfer.id.desc())
            .limit(100)
        )
    ).scalars().all()
    return {"items": [transfer_view(row, now) for row in rows], **_limits(await _used_bytes(db, user.id, now))}


async def _owned(db: AsyncSession, user_id: int, conversation_id: int, transfer_id: int) -> SelfTransfer:
    row = await db.get(SelfTransfer, transfer_id, populate_existing=True)
    if row is None or row.owner_id != user_id or row.conversation_id != conversation_id:
        raise _error(status.HTTP_404_NOT_FOUND, "transfer_not_found", "文件不存在")
    return row


async def get_transfer(db: AsyncSession, user: User, conversation_id: int, transfer_id: int) -> dict[str, Any]:
    await require_self_conversation(db, user.id, conversation_id)
    return transfer_view(await _owned(db, user.id, conversation_id, transfer_id))


# ---------------------------------------------------------------- 临时文件：上传


def _upload_gone() -> HTTPException:
    return _error(status.HTTP_404_NOT_FOUND, "upload_not_found", "上传不存在或已过期，请重新发送")


async def _pending(db: AsyncSession, user_id: int, conversation_id: int, transfer_id: int) -> SelfTransfer:
    row = await _owned(db, user_id, conversation_id, transfer_id)
    if row.state != PENDING:
        raise _error(status.HTTP_409_CONFLICT, "already_completed", "这个文件已经发出去了")
    expires = _aware(row.upload_expires_at)
    if expires is None or expires <= _now():
        raise _upload_gone()
    return row


async def create_transfer(
    db: AsyncSession, user: User, conversation_id: int, *, name: str, size: int, content_type: str | None, device: str
) -> dict[str, Any]:
    """POST /conversations/{cid}/transfers：检查大小 / 额度，开一张上传单，给上传地址（直传是预签名 PUT，否则是后端的 /content）。"""
    await require_self_conversation(db, user.id, conversation_id)
    ensure_can_post(user)
    if size <= 0:
        raise _error(status.HTTP_422_UNPROCESSABLE_ENTITY, "empty_file", "文件是空的")
    if size > settings.self_transfer_max_bytes:
        raise _error(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            "file_too_large",
            f"单个文件不能超过 {group_files.human_size(settings.self_transfer_max_bytes)}",
            max_file_bytes=settings.self_transfer_max_bytes,
        )
    now = _now()
    used = await _used_bytes(db, user.id, now)
    if used + size > settings.self_transfer_quota_bytes:
        raise _error(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            "transfer_quota_exceeded",
            f"同时存着的临时文件最多 {group_files.human_size(settings.self_transfer_quota_bytes)}，等之前的过期或撤回后再发",
            quota_bytes=settings.self_transfer_quota_bytes,
            used_bytes=used,
            size=size,
        )
    key = f"{FOLDER}/{user.id}/{uuid4().hex}"
    row = SelfTransfer(
        conversation_id=conversation_id,
        owner_id=user.id,
        name=group_files.clean_filename(name),
        size=size,
        content_type=group_files._clean_content_type(content_type),
        storage_key=key,
        device=normalize_device(device),
        state=PENDING,
        created_at=now,
        upload_expires_at=now + timedelta(seconds=UPLOAD_TTL_SECONDS),
    )
    db.add(row)
    await db.flush()
    if storage.direct_transfer_enabled():
        # 预签名只是本地算签名，不发网络请求
        url, headers = storage.presign_put(key, size, UPLOAD_URL_TTL_SECONDS)
        upload = {
            "direct": True,
            "method": "PUT",
            "url": url,
            "headers": headers,
            # 链接的有效期按真实时间算（签名用的是机器时钟）
            "expires_at": datetime.now(UTC) + timedelta(seconds=UPLOAD_URL_TTL_SECONDS),
        }
    else:
        upload = {
            "direct": False,
            "method": "PUT",
            "url": f"/api/v1/conversations/{conversation_id}/transfers/{row.id}/content",
            "headers": {"Content-Type": storage.OCTET_STREAM},
            "expires_at": row.upload_expires_at,
        }
    await db.commit()
    return {"transfer_id": row.id, "transfer": transfer_view(row, now), "upload": upload}


async def receive_content(db: AsyncSession, user: User, conversation_id: int, transfer_id: int, request: Request) -> dict[str, Any]:
    """PUT /conversations/{cid}/transfers/{tid}/content：没开直传时由后端收文件（请求体就是文件本身），收完存进存储。
    可以重传（覆盖上一次传了一半的）。complete 之前随时可以再 PUT。"""
    await require_self_conversation(db, user.id, conversation_id)
    row = await _pending(db, user.id, conversation_id, transfer_id)
    if storage.direct_transfer_enabled():
        raise _error(status.HTTP_409_CONFLICT, "direct_upload_required", "服务器开了直传，请把文件直接传到开单时给的 upload.url")
    key, size = row.storage_key, int(row.size)
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) != size:
        raise _error(status.HTTP_422_UNPROCESSABLE_ENTITY, "upload_size_mismatch", "传上来的文件大小和开单时不一致", size=size, uploaded_size=int(declared))
    # 收文件、写存储可能要很久（文件大）：先结束读事务、把数据库连接还回去
    await release_connection(db)
    spool = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024)
    received = 0
    try:
        async for chunk in request.stream():
            received += len(chunk)
            if received > size:
                break
            spool.write(chunk)
        if received != size:
            raise _error(status.HTTP_422_UNPROCESSABLE_ENTITY, "upload_size_mismatch", "传上来的文件大小和开单时不一致", size=size, uploaded_size=received)
        spool.seek(0)
        try:
            await asyncio.to_thread(storage.put_object, key, spool, storage.OCTET_STREAM)
        except OSError as exc:
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="File could not be saved") from exc
    finally:
        spool.close()
    return {"transfer_id": transfer_id, "uploaded_bytes": received}


def _file_text(name: str, size: int) -> str:
    return f"[文件] {name}（{group_files.human_size(size)}）"


async def _existing_result(db: AsyncSession, row: SelfTransfer) -> tuple[dict[str, Any], Message | None]:
    from app.services import messages as message_service

    message = None
    if row.message_id is not None:
        try:
            message = await message_service.get_message(db, row.message_id)
        except HTTPException:
            message = None
    return transfer_view(row), message


async def complete_transfer(
    db: AsyncSession, user: User, conversation_id: int, transfer_id: int, *, client_nonce: str | None = None
) -> tuple[dict[str, Any], Message | None, bool]:
    """POST /conversations/{cid}/transfers/{tid}/complete：核对存储里的对象，落一条文件消息。返回 (transfer, 消息, 这次是不是新落的)。

    已经 complete 过的再调一次：原样返回那条消息（created = False），不重复落消息 —— 客户端网络断了重试是安全的。
    两个 complete 同时进来：都去落消息，只有把上传单从 pending 改成 ready 的那个提交，另一个回滚后返回前一个的结果。
    """
    from app.services import messages as message_service

    await require_self_conversation(db, user.id, conversation_id)
    row = await _owned(db, user.id, conversation_id, transfer_id)
    if row.state != PENDING:
        if row.state == EXPIRED and row.message_id is None:
            raise _upload_gone()
        view, message = await _existing_result(db, row)
        return view, message, False
    expires = _aware(row.upload_expires_at)
    if expires is None or expires <= _now():
        raise _upload_gone()
    ensure_can_post(user)
    key, size, name, content_type, device = row.storage_key, int(row.size), row.name, row.content_type, row.device
    # 去存储核对（网络）：先结束读事务、把连接还回去
    await release_connection(db)
    stored_size = await asyncio.to_thread(storage.object_size, key)
    if stored_size is None:
        raise _error(status.HTTP_409_CONFLICT, "upload_incomplete", "文件还没传完")
    if stored_size != size:
        # 传错了：对象删掉，上传单留着（还是 pending），客户端可以重新 PUT 再 complete
        await asyncio.to_thread(storage.delete_objects, [key])
        raise _error(status.HTTP_422_UNPROCESSABLE_ENTITY, "upload_size_mismatch", "传上去的文件大小和开单时不一致", size=size, uploaded_size=stored_size)

    now = _now()
    expires_at = now + timedelta(hours=TTL_HOURS)
    try:
        message = await message_service.send_message(
            db,
            conversation_id,
            user,
            _file_text(name, size),
            MessageType.TEXT,
            {"transfer": {"id": transfer_id, "name": name, "size": size, "content_type": content_type, "expires_at": expires_at.isoformat()}},
            allow_transfer=True,
            device=device,
            commit=False,
        )
        claimed = await db.execute(
            update(SelfTransfer)
            .where(SelfTransfer.id == transfer_id, SelfTransfer.state == PENDING)
            .values(state=READY, completed_at=now, expires_at=expires_at, message_id=message.id)
            .execution_options(synchronize_session=False)
        )
        if not claimed.rowcount:
            await db.rollback()
            row = await _owned(db, user.id, conversation_id, transfer_id)
            if row.state == PENDING:
                raise _upload_gone()
            view, existing = await _existing_result(db, row)
            return view, existing, False
        await db.commit()
    except HTTPException as exc:
        await db.rollback()
        if exc.status_code == status.HTTP_429_TOO_MANY_REQUESTS:
            # 撞上发消息限频：上传单和对象都留着（文件可能很大，别让人重传），过一会儿再 complete 就行
            raise _error(status.HTTP_429_TOO_MANY_REQUESTS, "rate_limited", "发得太快了，过一会儿再点发送") from None
        raise
    except BaseException:
        await db.rollback()
        raise
    if client_nonce:
        setattr(message, "_client_nonce", client_nonce)
    row = await _owned(db, user.id, conversation_id, transfer_id)
    return transfer_view(row), message, True


# ---------------------------------------------------------------- 临时文件：下载


async def _downloadable(db: AsyncSession, user_id: int, conversation_id: int, transfer_id: int) -> SelfTransfer:
    await require_self_conversation(db, user_id, conversation_id)
    row = await _owned(db, user_id, conversation_id, transfer_id)
    if row.state == PENDING:
        raise _error(status.HTTP_409_CONFLICT, "upload_incomplete", "文件还没传完")
    if _state(row, _now()) == EXPIRED:
        raise _error(status.HTTP_410_GONE, "file_expired", "文件已过期")
    return row


async def download(db: AsyncSession, user: User, conversation_id: int, transfer_id: int) -> Response:
    """GET .../download：带鉴权下载，attachment。直传开着时 302 到 5 分钟的预签名地址（和群文件的 /download 一样）。"""
    row = await _downloadable(db, user.id, conversation_id, transfer_id)
    return await group_files.stored_file_response(db, row.storage_key, row.name)


async def download_link(db: AsyncSession, user: User, conversation_id: int, transfer_id: int) -> dict[str, Any]:
    """GET .../link：直传开着时给一个 5 分钟的预签名 GET，可以直接交给系统下载器。没开 409，改用 /download。"""
    row = await _downloadable(db, user.id, conversation_id, transfer_id)
    if not storage.direct_transfer_enabled():
        raise _error(status.HTTP_409_CONFLICT, "direct_download_unavailable", "服务器没开直连下载，请用下载接口")
    url = storage.presign_get(row.storage_key, DOWNLOAD_URL_TTL_SECONDS, group_files._disposition(row.name))
    return {"url": url, "expires_at": datetime.now(UTC) + timedelta(seconds=DOWNLOAD_URL_TTL_SECONDS)}


# ---------------------------------------------------------------- 撤回、清理


async def expire_for_message(db: AsyncSession, message: Message) -> bool:
    """撤回了一条临时文件消息：文件跟着作废（马上删对象，之后下载 410）。不是临时文件消息什么都不做。"""
    from app.services.messages import _metadata_from_json

    meta = _metadata_from_json(message.metadata_json) or {}
    transfer = meta.get("transfer")
    transfer_id = transfer.get("id") if isinstance(transfer, dict) else None
    if not isinstance(transfer_id, int):
        return False
    row = await db.get(SelfTransfer, transfer_id, populate_existing=True)
    if row is None or row.owner_id != message.sender_id or row.state == EXPIRED:
        return False
    key = row.storage_key
    done = await db.execute(
        update(SelfTransfer)
        .where(SelfTransfer.id == transfer_id, SelfTransfer.state != EXPIRED)
        .values(state=EXPIRED, expires_at=func.coalesce(SelfTransfer.expires_at, _now()))
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    if done.rowcount:
        await asyncio.to_thread(storage.delete_objects, [key])
    return bool(done.rowcount)


async def sweep(db: AsyncSession, now: datetime | None = None) -> dict[str, int]:
    """删过期的临时文件（标成 expired、删对象）、作废的上传单（删行、删对象），以及过期超过 7 天的行。可以重复跑；
    几个 worker 同时跑也没关系：改状态的语句都带着原状态做条件，删存储里的对象本来就是幂等的（删不存在的不报错）。"""
    now = now or _now()
    keys: list[str] = []
    stale = (
        await db.execute(
            select(SelfTransfer.id, SelfTransfer.storage_key)
            .where(SelfTransfer.state == PENDING, SelfTransfer.upload_expires_at <= now)
            .order_by(SelfTransfer.id)
            .limit(SWEEP_BATCH)
        )
    ).all()
    if stale:
        await db.execute(
            delete(SelfTransfer)
            .where(SelfTransfer.id.in_([row_id for row_id, _key in stale]), SelfTransfer.state == PENDING)
            .execution_options(synchronize_session=False)
        )
        keys.extend(key for _row_id, key in stale)
    expired = (
        await db.execute(
            select(SelfTransfer.id, SelfTransfer.storage_key)
            .where(SelfTransfer.state == READY, SelfTransfer.expires_at <= now)
            .order_by(SelfTransfer.id)
            .limit(SWEEP_BATCH)
        )
    ).all()
    if expired:
        await db.execute(
            update(SelfTransfer)
            .where(SelfTransfer.id.in_([row_id for row_id, _key in expired]), SelfTransfer.state == READY)
            .values(state=EXPIRED)
            .execution_options(synchronize_session=False)
        )
        keys.extend(key for _row_id, key in expired)
    purged = await db.execute(
        delete(SelfTransfer)
        .where(SelfTransfer.state == EXPIRED, SelfTransfer.expires_at <= now - EXPIRED_ROW_RETENTION)
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    if keys:
        await asyncio.to_thread(storage.delete_objects, keys)
    return {"stale_uploads": len(stale), "expired": len(expired), "purged": int(purged.rowcount or 0)}


async def _worker() -> None:
    delay = WORKER_STARTUP_DELAY_SECONDS
    while True:
        await asyncio.sleep(delay)
        try:
            async with AsyncSessionLocal() as db:
                done = await sweep(db)
                if done["expired"] or done["stale_uploads"]:
                    logger.info("Self transfer sweep removed %s expired files and %s stale uploads", done["expired"], done["stale_uploads"])
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Self transfer sweep failed")
        delay = SWEEP_INTERVAL_SECONDS


def start_self_transfer_worker() -> None:
    global _worker_task
    if _worker_task is not None and not _worker_task.done():
        return
    _worker_task = asyncio.create_task(_worker())


async def stop_self_transfer_worker() -> None:
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
