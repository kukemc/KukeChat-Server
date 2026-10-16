"""群文件（docs/group-plus.md 第 5 节）：Pro 起解锁，所有成员都能传（被禁言的不行），默认 24 小时后过期，
群主 / 管理员可以转永久；空间配额只算没过期的空间文件；下载必须鉴权。
放得进剩余空间的传成空间文件（没有单独的单文件上限）；放不进的按档位上限传成临时文件（固定 24 小时，不占配额）。
Pro 没有空间，传的都是临时文件。

文件存在 group-files/{群 id}/{随机名}（本地存储是 upload_dir 下的目录，S3 是桶里的前缀），对外永远不暴露直链：
本地的 /uploads 静态挂载会拦掉这个目录（main.py），下载只走接口。存储里没有扩展名、对象的 Content-Type 一律是
application/octet-stream；用户声明的类型只存在库里用来显示图标。

直传（S3 存储且配了 S3_PRIVATE_BUCKET）：group-files/ 放私有桶，客户端先开上传单拿预签名 PUT、直接传桶、再 complete；
下载拿 5 分钟的预签名 GET。complete 之后和 multipart 上传走同一条记录 / 落消息的路（_commit_file）。

文件夹（5.5）：只有一层，Pro+ / MAX 才能新建、重命名、移入（上传进去也算移入）；群主 / 管理员管理，普通成员浏览、上传进去。
降档之后照常浏览下载，删文件夹（文件回根目录）和移回根目录仍然可以。临时文件不进文件夹：上传变成临时文件时放根目录。
"""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
import tempfile
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote
from uuid import uuid4

from fastapi import HTTPException, UploadFile, status
from fastapi.responses import FileResponse, Response
from sqlalchemy import case, delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import group_plus_tiers as tiers
from app.core.database import AsyncSessionLocal
from app.models.conversation import Conversation, ConversationMember
from app.models.enums import ConversationType, MemberRole, MessageType
from app.models.group_plus import GroupFile, GroupFileFolder, GroupFileUpload
from app.models.message import Message
from app.models.user import User
from app.schemas.user import UserPublic
from app.services import group_plus as group_plus_service
from app.services import shop as shop_service
from app.services import storage
from app.services.penalties import ensure_can_post

logger = logging.getLogger(__name__)
UTC = timezone.utc
STAFF_ROLES = {MemberRole.OWNER, MemberRole.ADMIN}
FOLDER = "group-files"
NAME_MAX = 120
CHUNK = 1024 * 1024
SWEEP_BATCH = 200
SWEEP_INTERVAL_SECONDS = 3600.0
WORKER_STARTUP_DELAY_SECONDS = 60.0
# 直传：预签名 PUT 15 分钟内有效；上传单 1 小时后没 complete 就作废（worker 删单和对象）；预签名下载链接 5 分钟
UPLOAD_URL_TTL_SECONDS = 900
UPLOAD_TTL_SECONDS = 3600
DOWNLOAD_URL_TTL_SECONDS = 300
_CONTENT_TYPE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,60}/[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,60}$")
_ZERO_WIDTH = dict.fromkeys(map(ord, "​‌‍⁠﻿"), None)

_worker_task: asyncio.Task | None = None


def _now() -> datetime:
    return tiers._now()


def _error(status_code: int, code: str, message: str, **extra: Any) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message, **extra})


def clean_filename(raw: str | None) -> str:
    """去掉路径、控制字符、零宽字符，首尾空格和点，最长 NAME_MAX 个字（尽量保留扩展名）。空的叫 file。"""
    name = (raw or "").replace("\\", "/").split("/")[-1]
    name = unicodedata.normalize("NFC", name).translate(_ZERO_WIDTH)
    name = "".join(ch for ch in name if unicodedata.category(ch) not in ("Cc", "Cf", "Cs", "Co") and ch not in '<>:"|?*')
    name = name.strip().strip(".").strip()
    if len(name) > NAME_MAX:
        stem, dot, ext = name.rpartition(".")
        if dot and 0 < len(ext) <= 12:
            name = stem[: NAME_MAX - len(ext) - 1] + "." + ext
        else:
            name = name[:NAME_MAX]
    return name or "file"


def _clean_content_type(raw: str | None) -> str:
    value = (raw or "").split(";")[0].strip().lower()
    return value if _CONTENT_TYPE_RE.match(value) else "application/octet-stream"


def human_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    if size < 1024 * 1024 * 1024:
        return f"{size / 1024 / 1024:.1f} MB"
    return f"{size / 1024 / 1024 / 1024:.2f} GB"


async def _context(db: AsyncSession, user_id: int, conversation_id: int) -> tuple[Conversation, ConversationMember]:
    member = (
        await db.execute(
            select(ConversationMember).where(ConversationMember.conversation_id == conversation_id, ConversationMember.user_id == user_id)
        )
    ).scalar_one_or_none()
    conversation = await db.get(Conversation, conversation_id)
    if conversation is None or member is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")
    if conversation.type != ConversationType.GROUP:
        raise _error(status.HTTP_400_BAD_REQUEST, "not_group", "私聊没有群文件")
    return conversation, member


def _live(now: datetime) -> Any:
    return or_(GroupFile.expires_at.is_(None), GroupFile.expires_at > now)


def _space_bytes() -> Any:
    """占用的空间：只算空间文件，临时文件不算。"""
    return func.coalesce(func.sum(case((GroupFile.temporary.is_(False), GroupFile.size), else_=0)), 0)


async def _used_bytes(db: AsyncSession, conversation_id: int, now: datetime) -> int:
    return int(await db.scalar(select(_space_bytes()).where(GroupFile.conversation_id == conversation_id, _live(now))) or 0)


async def quota_of(db: AsyncSession, conversation: Conversation, now: datetime | None = None) -> dict[str, Any]:
    now = now or _now()
    count, used = (
        await db.execute(select(func.count(GroupFile.id), _space_bytes()).where(GroupFile.conversation_id == conversation.id, _live(now)))
    ).one()
    return group_plus_service.file_quota_view(conversation, int(used or 0), int(count or 0), now)


def _too_large(quota: dict[str, Any]) -> HTTPException:
    temp_max = quota["temp_max_bytes"] or 0
    if quota["free_bytes"] > 0:
        message = f"文件太大了：群空间还剩 {human_size(quota['free_bytes'])}，临时文件最大 {human_size(temp_max)}"
    else:
        message = f"群文件空间不够了，临时文件最大 {human_size(temp_max)}" if quota["quota_bytes"] else f"单个文件不能超过 {human_size(temp_max)}"
    return _error(
        status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
        "file_too_large",
        message,
        max_file_bytes=quota["max_file_bytes"],
        temp_max_bytes=quota["temp_max_bytes"],
        free_bytes=quota["free_bytes"],
    )


def _file_view(row: GroupFile, users: dict[int, User], conversation_id: int, now: datetime, folders: dict[int, str] | None = None) -> dict[str, Any]:
    expires = tiers.aware(row.expires_at)
    uploader = users.get(row.uploader_id) if row.uploader_id is not None else None
    # 文件夹已经不在了（删文件夹时会移回根目录，这里兜底）就当在根目录
    folder_name = (folders or {}).get(row.folder_id) if row.folder_id is not None else None
    return {
        "id": row.id,
        "conversation_id": conversation_id,
        "name": row.name,
        "size": int(row.size),
        "content_type": row.content_type,
        "uploader": UserPublic.model_validate(uploader) if uploader is not None else None,
        "uploader_id": row.uploader_id,
        "created_at": tiers.aware(row.created_at),
        "expires_at": expires,
        "permanent": expires is None,
        "temporary": bool(row.temporary),
        "expired": expires is not None and expires <= now,
        "message_id": row.message_id,
        "download_path": f"/api/v1/conversations/{conversation_id}/files/{row.id}/download",
        "folder_id": row.folder_id if folder_name is not None else None,
        "folder_name": folder_name,
    }


async def _views(db: AsyncSession, rows: list[GroupFile], conversation_id: int) -> list[dict[str, Any]]:
    now = _now()
    ids = {row.uploader_id for row in rows if row.uploader_id is not None}
    users: dict[int, User] = {}
    if ids:
        users = {user.id: user for user in (await db.execute(select(User).where(User.id.in_(ids)))).scalars().all()}
    folder_ids = {row.folder_id for row in rows if row.folder_id is not None}
    folders: dict[int, str] = {}
    if folder_ids:
        folders = {
            folder_id: name
            for folder_id, name in (
                await db.execute(
                    select(GroupFileFolder.id, GroupFileFolder.name).where(GroupFileFolder.conversation_id == conversation_id, GroupFileFolder.id.in_(folder_ids))
                )
            ).all()
        }
    return [_file_view(row, users, conversation_id, now, folders) for row in rows]


# ---------------------------------------------------------------- 列表、配额


async def list_files(
    db: AsyncSession,
    user_id: int,
    conversation_id: int,
    *,
    q: str | None,
    uploader_id: int | None,
    limit: int,
    offset: int,
    folder_id: int | None = None,
) -> dict[str, Any]:
    """不带 folder_id：根目录的文件；带了：这个文件夹里的文件。带了 q / uploader_id 是搜索：跨所有文件夹，忽略 folder_id。
    都带上这个群的全部文件夹（folders）和当前用户能不能管理文件夹。"""
    conversation, member = await _context(db, user_id, conversation_id)
    now = _now()
    clauses = [GroupFile.conversation_id == conversation_id, _live(now)]
    term = (q or "").strip()
    search = bool(term) or uploader_id is not None
    if term:
        clauses.append(GroupFile.name.ilike(f"%{term}%"))
    if uploader_id is not None:
        clauses.append(GroupFile.uploader_id == uploader_id)
    current: GroupFileFolder | None = None
    if not search:
        if folder_id is not None:
            current = await _get_folder(db, conversation_id, folder_id)
            clauses.append(GroupFile.folder_id == folder_id)
        else:
            # 根目录；指向已经不在的文件夹的文件也算根目录（兜底）
            clauses.append(
                or_(
                    GroupFile.folder_id.is_(None),
                    GroupFile.folder_id.not_in(select(GroupFileFolder.id).where(GroupFileFolder.conversation_id == conversation_id)),
                )
            )
    total = int(await db.scalar(select(func.count()).select_from(GroupFile).where(*clauses)) or 0)
    rows = (await db.execute(select(GroupFile).where(*clauses).order_by(GroupFile.id.desc()).offset(offset).limit(limit))).scalars().all()
    folders = await _folder_views(db, conversation_id, now)
    return {
        "items": await _views(db, list(rows), conversation_id),
        "total": total,
        "quota": await quota_of(db, conversation, now),
        "folder_id": current.id if current is not None else None,
        "folder": next((view for view in folders if current is not None and view["id"] == current.id), None),
        "folders": folders,
        "folder_limit": tiers.FOLDER_LIMIT,
        "search": search,
        **_folder_permissions(conversation, member, now),
    }


async def usage(db: AsyncSession, user_id: int, conversation_id: int) -> dict[str, Any]:
    conversation, _member = await _context(db, user_id, conversation_id)
    return await quota_of(db, conversation)


# ---------------------------------------------------------------- 上传


async def _spool(upload: UploadFile, limit: int) -> tuple[Any, int]:
    """把上传的内容读进临时文件，最多读 limit + 1 字节（多读 1 字节用来判断超了）。"""
    spool = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024)
    size = 0
    try:
        while True:
            chunk = await upload.read(CHUNK)
            if not chunk:
                break
            size += len(chunk)
            if size > limit:
                break
            spool.write(chunk)
    except BaseException:
        spool.close()
        raise
    spool.seek(0)
    return spool, size


def _store(key: str, spool: Any) -> None:
    storage.put_object(key, spool, "application/octet-stream")


async def _uploadable(db: AsyncSession, user: User, conversation_id: int) -> tuple[Conversation, bool, datetime]:
    """上传前的权限检查（multipart 上传、直传开单、直传完成共用）：群成员、没被平台禁言、群文件可用、没被群内禁言。
    返回 (群, 是不是群管, 现在)。"""
    conversation, member = await _context(db, user.id, conversation_id)
    is_staff = member.role in STAFF_ROLES
    ensure_can_post(user)
    now = _now()
    state = tiers.file_state(conversation, now)
    if state["state"] != "active":
        raise _error(
            status.HTTP_403_FORBIDDEN,
            "files_locked",
            "群文件需要群升级 Pro 及以上才能使用" if state["state"] == "locked" else "群升级已到期，群文件只能查看和下载，续费后恢复上传",
            state=state["state"],
            grace_until=state["grace_until"].isoformat() if state["grace_until"] else None,
        )
    if not is_staff:
        from app.services.conversations import _member_mute_expired

        if conversation.all_muted or (member.muted and not _member_mute_expired(member, now)):
            raise _error(
                status.HTTP_403_FORBIDDEN,
                "muted",
                "你在这个群里被禁言了，或者群开了全员禁言",
                muted_until=member.muted_until.isoformat() if member.muted_until else None,
            )
    return conversation, is_staff, now


def _check_size(quota: dict[str, Any], size: int) -> None:
    if size > quota["max_file_bytes"]:
        raise _too_large(quota)
    if size <= 0:
        raise _error(status.HTTP_422_UNPROCESSABLE_ENTITY, "empty_file", "文件是空的")


def _decide(quota: dict[str, Any], size: int, keep_forever: bool, ttl_hours: int, now: datetime) -> tuple[bool, datetime | None]:
    """按剩余空间定是空间文件还是临时文件，返回 (temporary, expires_at)；都放不下 413。"""
    if size <= quota["free_bytes"]:
        return False, None if keep_forever else now + timedelta(hours=ttl_hours)
    if size <= (quota["temp_max_bytes"] or 0):
        return True, now + timedelta(hours=tiers.FILE_TEMP_TTL_HOURS)
    raise _too_large(quota)


def _ttl_hours(conversation: Conversation) -> int:
    return int(conversation.file_default_ttl_hours or tiers.FILE_DEFAULT_TTL_HOURS)


async def _commit_file(
    db: AsyncSession,
    user: User,
    conversation: Conversation,
    *,
    key: str,
    name: str,
    size: int,
    content_type: str,
    keep_forever: bool,
    announce: bool,
    upload_id: str | None = None,
    folder_id: int | None = None,
) -> tuple[dict[str, Any], Message | None]:
    """对象已经在存储里了：记录（落消息）并提交。multipart 上传和直传 complete 走这同一条路。
    写事务：按现在的剩余空间再定一遍是空间文件还是临时文件（别的上传可能刚进来）、记录、（落消息）、提交，一起成或一起不成；
    没成就删掉存储里的对象。直传时 upload_id 是上传单，在同一个事务里删掉（删不到说明另一个 complete 抢先了：404，对象不动）。"""
    conversation_id = conversation.id
    row: GroupFile | None = None
    message: Message | None = None
    lost_claim = False
    try:
        async with shop_service._write_lock():
            try:
                if upload_id is not None:
                    claimed = await db.execute(delete(GroupFileUpload).where(GroupFileUpload.id == upload_id))
                    if not claimed.rowcount:
                        lost_claim = True
                        raise _upload_not_found()
                now = _now()
                quota = await quota_of(db, conversation, now)
                temporary, expires_at = _decide(quota, size, keep_forever, _ttl_hours(conversation), now)
                # 临时文件不进文件夹；文件夹这时已经被删了、档位不够了，也放根目录（不让已经传上去的文件白传）
                if folder_id is not None and (
                    temporary or not tiers.folders_enabled(conversation, now) or await _find_folder(db, conversation_id, folder_id) is None
                ):
                    folder_id = None
                row = GroupFile(
                    conversation_id=conversation_id,
                    uploader_id=user.id,
                    name=name,
                    size=size,
                    content_type=content_type,
                    storage_key=key,
                    expires_at=expires_at,
                    temporary=temporary,
                    folder_id=folder_id,
                    created_at=now,
                )
                db.add(row)
                await db.flush()
                if announce:
                    from app.services import messages as message_service

                    message = await message_service.send_message(
                        db,
                        conversation_id,
                        user,
                        f"[文件] {name}（{human_size(size)}）",
                        MessageType.TEXT,
                        {
                            "file": {
                                "id": row.id,
                                "name": name,
                                "size": size,
                                "content_type": content_type,
                                "expires_at": row.expires_at.isoformat() if row.expires_at else None,
                                "permanent": row.expires_at is None,
                                "temporary": temporary,
                            }
                        },
                        allow_file=True,
                        commit=False,
                    )
                    row.message_id = message.id
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
    except BaseException as exc:
        if not lost_claim:
            # 没保存成：存储里的对象删掉；直传的上传单也删掉（事务回滚了，单独删）
            await asyncio.to_thread(storage.delete_objects, [key])
            if upload_id is not None:
                await _drop_upload(db, upload_id)
        if isinstance(exc, HTTPException) and exc.status_code == status.HTTP_429_TOO_MANY_REQUESTS:
            raise _error(status.HTTP_429_TOO_MANY_REQUESTS, "rate_limited", "发得太快了，文件没有保存") from None
        raise
    view = (await _views(db, [row], conversation_id))[0]
    return view, message


async def upload_file(
    db: AsyncSession, user: User, conversation_id: int, upload: UploadFile, *, announce: bool, permanent: bool, folder_id: int | None = None
) -> tuple[dict[str, Any], Message | None]:
    """POST /conversations/{cid}/files。返回 (GroupFileRead, 落下的文件消息或 None)；消息的广播由接口层做。"""
    conversation, is_staff, now = await _uploadable(db, user, conversation_id)
    folder_id = await _target_folder(db, conversation, folder_id, now)
    name = clean_filename(upload.filename)
    content_type = _clean_content_type(upload.content_type)
    quota = await quota_of(db, conversation, now)
    spool, size = await _spool(upload, quota["max_file_bytes"])
    try:
        _check_size(quota, size)
        key = f"{FOLDER}/{conversation_id}/{uuid4().hex}"
        try:
            await asyncio.to_thread(_store, key, spool)
        except OSError as exc:
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="File could not be saved") from exc
    finally:
        spool.close()
    return await _commit_file(
        db,
        user,
        conversation,
        key=key,
        name=name,
        size=size,
        content_type=content_type,
        keep_forever=bool(permanent and is_staff),
        announce=announce,
        folder_id=folder_id,
    )


# ---------------------------------------------------------------- 直传（预签名 URL）


def _upload_not_found() -> HTTPException:
    return _error(status.HTTP_404_NOT_FOUND, "upload_not_found", "上传不存在或已过期")


async def _drop_upload(db: AsyncSession, upload_id: str) -> None:
    try:
        await db.execute(delete(GroupFileUpload).where(GroupFileUpload.id == upload_id))
        await db.commit()
    except Exception:
        await db.rollback()
        logger.exception("Could not drop group file upload %s", upload_id)


async def create_upload(
    db: AsyncSession,
    user: User,
    conversation_id: int,
    *,
    name: str,
    size: int,
    content_type: str | None,
    permanent: bool,
    folder_id: int | None = None,
) -> dict[str, Any]:
    """POST /conversations/{cid}/files/uploads：检查和 multipart 上传一样，通过了发一个预签名 PUT。
    temporary 是按现在的剩余空间预估的，complete 时会再定一遍。"""
    conversation, is_staff, now = await _uploadable(db, user, conversation_id)
    folder_id = await _target_folder(db, conversation, folder_id, now)
    quota = await quota_of(db, conversation, now)
    _check_size(quota, size)
    keep_forever = bool(permanent and is_staff)
    temporary, _expires = _decide(quota, size, keep_forever, _ttl_hours(conversation), now)
    if not storage.direct_transfer_enabled():
        raise _error(status.HTTP_409_CONFLICT, "direct_upload_unavailable", "服务器没开直传，请用普通上传")
    key = f"{FOLDER}/{conversation_id}/{uuid4().hex}"
    url, headers = storage.presign_put(key, size, UPLOAD_URL_TTL_SECONDS)
    # 链接的有效期按真实时间算（签名用的是机器时钟）；上传单的过期按业务时钟
    url_expires = datetime.now(UTC) + timedelta(seconds=UPLOAD_URL_TTL_SECONDS)
    upload_id = secrets.token_urlsafe(24)
    db.add(
        GroupFileUpload(
            id=upload_id,
            conversation_id=conversation_id,
            uploader_id=user.id,
            storage_key=key,
            name=clean_filename(name),
            size=size,
            content_type=_clean_content_type(content_type),
            permanent=keep_forever,
            temporary=temporary,
            folder_id=folder_id,
            created_at=now,
            expires_at=now + timedelta(seconds=UPLOAD_TTL_SECONDS),
        )
    )
    await db.commit()
    return {
        "upload_id": upload_id,
        "url": url,
        "method": "PUT",
        "headers": headers,
        "expires_at": url_expires,
        "temporary": temporary,
        "folder_id": None if temporary else folder_id,
    }


async def complete_upload(
    db: AsyncSession, user: User, conversation_id: int, upload_id: str, *, announce: bool
) -> tuple[dict[str, Any], Message | None]:
    """POST /conversations/{cid}/files/uploads/{upload_id}/complete：核对桶里的对象，然后和 multipart 上传走同一条记录 / 落消息的路。"""
    await _context(db, user.id, conversation_id)
    pending = await db.get(GroupFileUpload, upload_id)
    expires = tiers.aware(pending.expires_at) if pending is not None else None
    if pending is None or pending.conversation_id != conversation_id or pending.uploader_id != user.id or expires is None or expires <= _now():
        raise _upload_not_found()
    key, size, name, content_type, keep_forever = pending.storage_key, int(pending.size), pending.name, pending.content_type, bool(pending.permanent)
    folder_id = pending.folder_id
    try:
        # 开单之后可能被禁言了、群升级到期了：不让落库，传上去的对象删掉
        conversation, _is_staff, _at = await _uploadable(db, user, conversation_id)
    except HTTPException:
        await asyncio.to_thread(storage.delete_objects, [key])
        await _drop_upload(db, upload_id)
        raise
    stored_size = await asyncio.to_thread(storage.object_size, key)
    if stored_size is None:
        raise _error(status.HTTP_409_CONFLICT, "upload_incomplete", "文件还没传完")
    if stored_size != size:
        await asyncio.to_thread(storage.delete_objects, [key])
        await _drop_upload(db, upload_id)
        raise _error(status.HTTP_422_UNPROCESSABLE_ENTITY, "upload_size_mismatch", "传上去的文件大小和申请时不一致", size=size, uploaded_size=stored_size)
    return await _commit_file(
        db,
        user,
        conversation,
        key=key,
        name=name,
        size=size,
        content_type=content_type,
        keep_forever=keep_forever,
        announce=announce,
        upload_id=upload_id,
        folder_id=folder_id,
    )


# ---------------------------------------------------------------- 下载、删除、转永久


async def _get(db: AsyncSession, conversation_id: int, file_id: int) -> GroupFile:
    row = await db.get(GroupFile, file_id)
    if row is None or row.conversation_id != conversation_id:
        raise _error(status.HTTP_404_NOT_FOUND, "file_not_found", "文件不存在")
    return row


async def _downloadable(db: AsyncSession, user_id: int, conversation_id: int, file_id: int) -> GroupFile:
    await _context(db, user_id, conversation_id)
    row = await _get(db, conversation_id, file_id)
    expires = tiers.aware(row.expires_at)
    if expires is not None and expires <= _now():
        raise _error(status.HTTP_410_GONE, "file_expired", "文件已过期")
    return row


def _disposition(name: str) -> str:
    ascii_name = re.sub(r"[^A-Za-z0-9._-]", "_", name) or "file"
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name, safe='')}"


async def download(db: AsyncSession, user_id: int, conversation_id: int, file_id: int) -> Response:
    row = await _downloadable(db, user_id, conversation_id, file_id)
    name, key = row.name, row.storage_key
    headers = {
        "Content-Disposition": _disposition(name),
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "private, no-store",
        "Content-Security-Policy": "sandbox; default-src 'none'",
    }
    path = storage.local_path_for_key(key)
    if path is not None:
        return FileResponse(path, media_type="application/octet-stream", headers=headers)
    try:
        stored = await asyncio.to_thread(storage.get_object, key)
    except HTTPException:
        raise _error(status.HTTP_404_NOT_FOUND, "file_not_found", "文件已经不存在了") from None
    return Response(content=stored.data, media_type="application/octet-stream", headers=headers)


async def download_link(db: AsyncSession, user_id: int, conversation_id: int, file_id: int) -> dict[str, Any]:
    """GET /conversations/{cid}/files/{file_id}/link：检查和 /download 一样，直传开着时给一个 5 分钟的预签名 GET。"""
    row = await _downloadable(db, user_id, conversation_id, file_id)
    if not storage.direct_transfer_enabled():
        raise _error(status.HTTP_409_CONFLICT, "direct_download_unavailable", "服务器没开直连下载，请用下载接口")
    url = storage.presign_get(row.storage_key, DOWNLOAD_URL_TTL_SECONDS, _disposition(row.name))
    return {"url": url, "expires_at": datetime.now(UTC) + timedelta(seconds=DOWNLOAD_URL_TTL_SECONDS)}


async def delete_file(db: AsyncSession, user_id: int, conversation_id: int, file_id: int) -> GroupFile:
    _conversation, member = await _context(db, user_id, conversation_id)
    row = await _get(db, conversation_id, file_id)
    if row.uploader_id != user_id and member.role not in STAFF_ROLES:
        raise _error(status.HTTP_403_FORBIDDEN, "not_group_staff", "只有上传人、群主和管理员可以删除文件")
    key = row.storage_key
    await db.delete(row)
    await db.commit()
    await asyncio.to_thread(storage.delete_objects, [key])
    return row


async def set_permanent(db: AsyncSession, user_id: int, conversation_id: int, file_id: int, permanent: Any) -> dict[str, Any]:
    conversation, member = await _context(db, user_id, conversation_id)
    if member.role not in STAFF_ROLES:
        raise _error(status.HTTP_403_FORBIDDEN, "not_group_staff", "只有群主和管理员可以修改文件的保留时间")
    if not isinstance(permanent, bool):
        raise _error(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_permanent", "permanent 只能是 true / false")
    row = await _get(db, conversation_id, file_id)
    now = _now()
    expires = tiers.aware(row.expires_at)
    if expires is not None and expires <= now:
        raise _error(status.HTTP_410_GONE, "file_expired", "文件已过期")
    if row.temporary:
        # 临时文件：转回限时什么都不做；转永久要剩余空间放得下，转完就是普通的永久空间文件
        if permanent:
            async with shop_service._write_lock():
                quota = await quota_of(db, conversation, _now())
                size = int(row.size)
                if quota["state"] != "active" or size > quota["free_bytes"]:
                    raise _error(
                        status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        "quota_exceeded",
                        "群文件空间不够了，临时文件不能转为永久",
                        quota_bytes=quota["quota_bytes"],
                        used_bytes=quota["used_bytes"],
                        size=size,
                    )
                row.temporary = False
                row.expires_at = None
                await db.commit()
        return (await _views(db, [row], conversation_id))[0]
    if permanent:
        row.expires_at = None
    else:
        row.expires_at = now + timedelta(hours=int(conversation.file_default_ttl_hours or tiers.FILE_DEFAULT_TTL_HOURS))
    await db.commit()
    return (await _views(db, [row], conversation_id))[0]


# ---------------------------------------------------------------- 文件夹（5.5）


_FOLDER_FORBIDDEN = set("/\\  ")


def clean_folder_name(raw: Any) -> tuple[str, str]:
    """文件夹名：去掉首尾空白，1–40 个字，不能有换行 / 控制字符 / 不可见的格式字符，不能有 / 和 \\。
    返回 (名字, 查重用的 key：NFKC + casefold，不区分大小写)。"""
    if not isinstance(raw, str):
        raise _error(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_folder_name", "文件夹名不能为空")
    name = unicodedata.normalize("NFC", raw).strip()
    if not 1 <= len(name) <= tiers.FOLDER_NAME_MAX:
        raise _error(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_folder_name", f"文件夹名需要 1 到 {tiers.FOLDER_NAME_MAX} 个字")
    if any(ch in _FOLDER_FORBIDDEN or unicodedata.category(ch) in ("Cc", "Cf", "Cs", "Co") for ch in name):
        raise _error(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_folder_name", "文件夹名不能包含换行、/ 或特殊字符")
    return name, unicodedata.normalize("NFKC", name).casefold()


def _folder_permissions(conversation: Conversation, member: ConversationMember, now: datetime) -> dict[str, bool]:
    """can_manage_folders：能新建、重命名文件夹、把文件移进去（群管 + 档位够）；
    can_tidy_folders：能删文件夹、把文件移回根目录（群管，降档后也行）。"""
    staff = member.role in STAFF_ROLES
    return {"can_manage_folders": staff and tiers.folders_enabled(conversation, now), "can_tidy_folders": staff}


def _require_staff(member: ConversationMember) -> None:
    if member.role not in STAFF_ROLES:
        raise _error(status.HTTP_403_FORBIDDEN, "not_group_staff", "只有群主和管理员可以管理文件夹")


def _require_folders(conversation: Conversation, now: datetime) -> None:
    if not tiers.folders_enabled(conversation, now):
        raise _error(status.HTTP_403_FORBIDDEN, "folders_require_plus", "文件夹需要群升级 Pro+ 或 MAX", tier=tiers.conversation_tier(conversation, now))


def _folder_not_found() -> HTTPException:
    return _error(status.HTTP_404_NOT_FOUND, "folder_not_found", "文件夹不存在")


async def _find_folder(db: AsyncSession, conversation_id: int, folder_id: int) -> GroupFileFolder | None:
    row = await db.get(GroupFileFolder, folder_id, populate_existing=True)
    return row if row is not None and row.conversation_id == conversation_id else None


async def _get_folder(db: AsyncSession, conversation_id: int, folder_id: int) -> GroupFileFolder:
    row = await _find_folder(db, conversation_id, folder_id)
    if row is None:
        raise _folder_not_found()
    return row


async def _target_folder(db: AsyncSession, conversation: Conversation, folder_id: int | None, now: datetime) -> int | None:
    """上传时指定的文件夹：档位要够、文件夹要是这个群的。"""
    if folder_id is None:
        return None
    _require_folders(conversation, now)
    return (await _get_folder(db, conversation.id, folder_id)).id


async def _folder_views(db: AsyncSession, conversation_id: int, now: datetime, only_id: int | None = None) -> list[dict[str, Any]]:
    """这个群的文件夹，按创建先后（id 升序）。file_count / total_size 只算没过期的文件。"""
    query = select(GroupFileFolder).where(GroupFileFolder.conversation_id == conversation_id)
    if only_id is not None:
        query = query.where(GroupFileFolder.id == only_id)
    rows = (await db.execute(query.order_by(GroupFileFolder.id))).scalars().all()
    if not rows:
        return []
    stats = {
        folder_id: (int(count or 0), int(total or 0))
        for folder_id, count, total in (
            await db.execute(
                select(GroupFile.folder_id, func.count(GroupFile.id), func.coalesce(func.sum(GroupFile.size), 0))
                .where(GroupFile.conversation_id == conversation_id, GroupFile.folder_id.in_([row.id for row in rows]), _live(now))
                .group_by(GroupFile.folder_id)
            )
        ).all()
    }
    creator_ids = {row.created_by_id for row in rows if row.created_by_id is not None}
    users: dict[int, User] = {}
    if creator_ids:
        users = {user.id: user for user in (await db.execute(select(User).where(User.id.in_(creator_ids)))).scalars().all()}
    views = []
    for row in rows:
        count, total = stats.get(row.id, (0, 0))
        creator = users.get(row.created_by_id) if row.created_by_id is not None else None
        views.append(
            {
                "id": row.id,
                "conversation_id": conversation_id,
                "name": row.name,
                "file_count": count,
                "total_size": total,
                "created_by": UserPublic.model_validate(creator) if creator is not None else None,
                "created_by_id": row.created_by_id,
                "created_at": tiers.aware(row.created_at),
                "updated_at": tiers.aware(row.updated_at),
            }
        )
    return views


async def _folder_view(db: AsyncSession, conversation_id: int, folder_id: int) -> dict[str, Any]:
    views = await _folder_views(db, conversation_id, _now(), only_id=folder_id)
    if not views:
        raise _folder_not_found()
    return views[0]


def _name_taken() -> HTTPException:
    return _error(status.HTTP_409_CONFLICT, "folder_name_taken", "已经有同名的文件夹了")


async def _name_in_use(db: AsyncSession, conversation_id: int, key: str, exclude_id: int | None = None) -> bool:
    query = select(GroupFileFolder.id).where(GroupFileFolder.conversation_id == conversation_id, GroupFileFolder.name_key == key)
    if exclude_id is not None:
        query = query.where(GroupFileFolder.id != exclude_id)
    return (await db.scalar(query.limit(1))) is not None


async def list_folders(db: AsyncSession, user_id: int, conversation_id: int) -> dict[str, Any]:
    conversation, member = await _context(db, user_id, conversation_id)
    now = _now()
    return {"items": await _folder_views(db, conversation_id, now), "limit": tiers.FOLDER_LIMIT, **_folder_permissions(conversation, member, now)}


async def create_folder(db: AsyncSession, user_id: int, conversation_id: int, name: Any) -> dict[str, Any]:
    conversation, member = await _context(db, user_id, conversation_id)
    _require_staff(member)
    _require_folders(conversation, _now())
    name, key = clean_folder_name(name)
    async with shop_service._write_lock():
        try:
            count = int(await db.scalar(select(func.count()).select_from(GroupFileFolder).where(GroupFileFolder.conversation_id == conversation_id)) or 0)
            if count >= tiers.FOLDER_LIMIT:
                raise _error(status.HTTP_409_CONFLICT, "folder_limit", f"每个群最多 {tiers.FOLDER_LIMIT} 个文件夹", limit=tiers.FOLDER_LIMIT)
            if await _name_in_use(db, conversation_id, key):
                raise _name_taken()
            now = _now()
            row = GroupFileFolder(conversation_id=conversation_id, name=name, name_key=key, created_by_id=user_id, created_at=now, updated_at=now)
            db.add(row)
            await db.commit()
        except IntegrityError:
            # 另一个 worker 同时建了同名的
            await db.rollback()
            raise _name_taken() from None
        except BaseException:
            await db.rollback()
            raise
    return await _folder_view(db, conversation_id, row.id)


async def rename_folder(db: AsyncSession, user_id: int, conversation_id: int, folder_id: int, name: Any) -> dict[str, Any]:
    conversation, member = await _context(db, user_id, conversation_id)
    _require_staff(member)
    _require_folders(conversation, _now())
    name, key = clean_folder_name(name)
    async with shop_service._write_lock():
        try:
            row = await _get_folder(db, conversation_id, folder_id)
            if await _name_in_use(db, conversation_id, key, exclude_id=row.id):
                raise _name_taken()
            row.name, row.name_key, row.updated_at = name, key, _now()
            await db.commit()
        except IntegrityError:
            await db.rollback()
            raise _name_taken() from None
        except BaseException:
            await db.rollback()
            raise
    return await _folder_view(db, conversation_id, folder_id)


async def delete_folder(db: AsyncSession, user_id: int, conversation_id: int, folder_id: int) -> dict[str, Any]:
    """删文件夹：里面的文件移回根目录，不删文件本身。降档后也能删（方便收拾）。"""
    _conversation, member = await _context(db, user_id, conversation_id)
    _require_staff(member)
    async with shop_service._write_lock():
        try:
            row = await _get_folder(db, conversation_id, folder_id)
            moved = await db.execute(
                update(GroupFile).where(GroupFile.conversation_id == conversation_id, GroupFile.folder_id == folder_id).values(folder_id=None)
            )
            await db.execute(
                update(GroupFileUpload)
                .where(GroupFileUpload.conversation_id == conversation_id, GroupFileUpload.folder_id == folder_id)
                .values(folder_id=None)
            )
            await db.delete(row)
            await db.commit()
        except BaseException:
            await db.rollback()
            raise
    return {"folder_id": folder_id, "moved_to_root": int(moved.rowcount or 0)}


async def move_files(db: AsyncSession, user_id: int, conversation_id: int, file_ids: list[int], folder_id: int | None) -> dict[str, Any]:
    """批量移动：folder_id 为 null 是移回根目录（降档后也行）；移进文件夹要档位够，临时文件、已过期的文件不能移进去。
    要么全部移动，要么一个都不动。"""
    conversation, member = await _context(db, user_id, conversation_id)
    _require_staff(member)
    ids = list(dict.fromkeys(file_ids))
    if folder_id is not None:
        _require_folders(conversation, _now())
    async with shop_service._write_lock():
        try:
            if folder_id is not None:
                await _get_folder(db, conversation_id, folder_id)
            rows = list(
                (await db.execute(select(GroupFile).where(GroupFile.conversation_id == conversation_id, GroupFile.id.in_(ids)))).scalars().all()
            )
            found = {row.id for row in rows}
            missing = [file_id for file_id in ids if file_id not in found]
            if missing:
                raise _error(status.HTTP_404_NOT_FOUND, "file_not_found", "有文件不存在", file_ids=missing)
            if folder_id is not None:
                now = _now()
                temporary_ids = [row.id for row in rows if row.temporary]
                expired_ids = [row.id for row in rows if (expires := tiers.aware(row.expires_at)) is not None and expires <= now]
                if temporary_ids or expired_ids:
                    raise _error(
                        status.HTTP_422_UNPROCESSABLE_ENTITY,
                        "file_not_movable",
                        "临时文件和已过期的文件不能放进文件夹",
                        temporary_ids=temporary_ids,
                        expired_ids=expired_ids,
                    )
            for row in rows:
                row.folder_id = folder_id
            await db.commit()
        except BaseException:
            await db.rollback()
            raise
    order = {file_id: index for index, file_id in enumerate(ids)}
    rows.sort(key=lambda row: order[row.id])
    return {"folder_id": folder_id, "moved": len(rows), "items": await _views(db, rows, conversation_id)}


# ---------------------------------------------------------------- 清理


async def sweep(db: AsyncSession, now: datetime | None = None) -> dict[str, int]:
    """删过期的文件，以及不再有权益（未解锁且过了宽限期，或者群已经不在了）的群的全部文件。"""
    now = now or _now()
    keys: list[str] = []
    expired = 0
    rows = (
        await db.execute(select(GroupFile).where(GroupFile.expires_at.is_not(None), GroupFile.expires_at <= now).order_by(GroupFile.id).limit(SWEEP_BATCH))
    ).scalars().all()
    for row in rows:
        keys.append(row.storage_key)
        await db.delete(row)
        expired += 1
    orphaned = 0
    conversation_ids = (await db.execute(select(GroupFile.conversation_id).distinct().limit(SWEEP_BATCH * 5))).scalars().all()
    for conversation_id in conversation_ids:
        conversation = await db.get(Conversation, conversation_id)
        if conversation is not None and tiers.file_state(conversation, now)["state"] != "locked":
            continue
        doomed = (await db.execute(select(GroupFile).where(GroupFile.conversation_id == conversation_id).limit(SWEEP_BATCH))).scalars().all()
        for row in doomed:
            keys.append(row.storage_key)
            await db.delete(row)
            orphaned += 1
    # 失效的群（未解锁且过了宽限期 / 群不在了）：文件夹也删掉
    folder_conversation_ids = (await db.execute(select(GroupFileFolder.conversation_id).distinct().limit(SWEEP_BATCH * 5))).scalars().all()
    for conversation_id in folder_conversation_ids:
        conversation = await db.get(Conversation, conversation_id)
        if conversation is not None and tiers.file_state(conversation, now)["state"] != "locked":
            continue
        await db.execute(delete(GroupFileFolder).where(GroupFileFolder.conversation_id == conversation_id))
    await db.commit()
    if keys:
        await asyncio.to_thread(storage.delete_objects, keys)
    return {"expired": expired, "orphaned": orphaned}


async def sweep_uploads(db: AsyncSession, now: datetime | None = None) -> int:
    """删过期的直传上传单，连同桶里可能已经传上去、但没 complete 的对象。"""
    now = now or _now()
    rows = (
        await db.execute(select(GroupFileUpload).where(GroupFileUpload.expires_at <= now).order_by(GroupFileUpload.expires_at).limit(SWEEP_BATCH))
    ).scalars().all()
    if not rows:
        return 0
    keys = [row.storage_key for row in rows]
    await db.execute(delete(GroupFileUpload).where(GroupFileUpload.id.in_([row.id for row in rows])))
    await db.commit()
    await asyncio.to_thread(storage.delete_objects, keys)
    return len(rows)


async def _worker() -> None:
    delay = WORKER_STARTUP_DELAY_SECONDS
    while True:
        await asyncio.sleep(delay)
        try:
            async with AsyncSessionLocal() as db:
                done = await sweep(db)
                if done["expired"] or done["orphaned"]:
                    logger.info("Group file sweep removed %s expired and %s orphaned files", done["expired"], done["orphaned"])
                stale = await sweep_uploads(db)
                if stale:
                    logger.info("Group file sweep removed %s stale direct uploads", stale)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Group file sweep failed")
        delay = SWEEP_INTERVAL_SECONDS


def start_group_file_worker() -> None:
    global _worker_task
    if _worker_task is not None and not _worker_task.done():
        return
    _worker_task = asyncio.create_task(_worker())


async def stop_group_file_worker() -> None:
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

