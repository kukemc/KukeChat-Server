"""群公告 v2（docs/group-announcements.md）：发布 / 修改 / 删除、公告卡片消息、看过 / 确认回执、进群弹窗、确认统计。"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any, Literal

from fastapi import HTTPException, status
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.core.config import settings
from app.core.database import release_connection
from app.models.conversation import Conversation, ConversationMember, GroupAnnouncement, GroupAnnouncementReceipt
from app.models.enums import MemberRole, MessageType
from app.models.message import Message
from app.models.user import User
from app.schemas.group_announcement import (
    ANNOUNCEMENT_MAX_IMAGES,
    GroupAnnouncementConfirmationItem,
    GroupAnnouncementConfirmationPage,
    GroupAnnouncementCreate,
    GroupAnnouncementImage,
    GroupAnnouncementRead,
    GroupAnnouncementSeenRead,
    GroupAnnouncementUpdate,
)
from app.schemas.user import UserPublic
from app.services.conversations import STAFF_ROLES, require_group_conversation, require_membership
from app.services.storage import storage_key_from_url, thumbnail_key_for_key

UTC = timezone.utc

CARD_SYSTEM_EVENT = "group.announcement.card"
CARD_SUMMARY_LENGTH = 200
# 旧客户端 / 会话列表预览看到的那一行文字里带多少字正文
CARD_PREVIEW_LENGTH = 40
_IMAGE_FOLDERS = ("messages", "announcements")
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}


def _error(code: str, message: str, status_code: int) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


def _not_found() -> HTTPException:
    return _error("announcement_not_found", "公告不存在或已被删除", status.HTTP_404_NOT_FOUND)


def _forbidden() -> HTTPException:
    return _error("announcement_forbidden", "只有群主和管理员可以管理群公告", status.HTTP_403_FORBIDDEN)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    return text or None


def _user_name(user: User | None) -> str:
    if user is None:
        return "成员"
    return _clean(user.nickname) or _clean(user.username) or "成员"


# ------------------------------------------------------------------ 图片


def _image_key(url: str) -> str | None:
    """已上传到 messages/ 或 announcements/ 的图片 → 存储 key；别的一律 None。"""
    key = storage_key_from_url(url)
    if key is None:
        return None
    parts = PurePosixPath(key).parts
    if len(parts) != 2 or parts[0] not in _IMAGE_FOLDERS or ".." in parts:
        return None
    if PurePosixPath(parts[1]).suffix.lower() not in _IMAGE_SUFFIXES:
        return None
    return key


def _validate_images(urls: list[str]) -> list[str]:
    if len(urls) > ANNOUNCEMENT_MAX_IMAGES:
        raise _error("too_many_images", f"最多添加 {ANNOUNCEMENT_MAX_IMAGES} 张图片", status.HTTP_422_UNPROCESSABLE_ENTITY)
    for url in urls:
        if _image_key(url) is None:
            raise _error("invalid_announcement_image", "公告图片必须是已上传的图片", status.HTTP_422_UNPROCESSABLE_ENTITY)
    return urls


def _thumbnail_url(url: str) -> str | None:
    key = _image_key(url)
    if key is None:
        return None
    thumb_key = thumbnail_key_for_key(key)
    # 保留原 url 的域名 / 前缀，只换掉末尾的 key
    if url.endswith(key):
        return url[: len(url) - len(key)] + thumb_key
    return None


def _load_images(announcement: GroupAnnouncement) -> list[str]:
    if not announcement.images_json:
        return []
    try:
        value = json.loads(announcement.images_json)
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]


def _dump_images(urls: list[str]) -> str | None:
    return json.dumps(urls, ensure_ascii=False) if urls else None


# ------------------------------------------------------------------ 审核


async def _ensure_allowed(db: AsyncSession, actor: User, conversation_id: int, content: str | None, new_images: list[str]) -> None:
    """公告全群可见，发布前同步过审核。只有明确违规才拒绝；审核服务不可用时放行。

    调用时会话里不能有未提交的写：审核服务可能要几秒，先结束读事务、把连接还回去再等。
    """
    if not settings.content_moderation_enabled or actor.content_moderation_whitelisted:
        return
    if not content and not new_images:
        return
    from app.services import moderation as moderation_service

    await release_connection(db)
    context_id = f"group_announcement:{conversation_id}"

    async def check_text() -> str | None:
        if not content:
            return None
        try:
            result = await moderation_service._call_text_moderation_for_user(content, actor.id, context_id=context_id)
            return moderation_service._normalize_result(result, "text")["decision"]
        except Exception:
            return None

    async def check_image(url: str) -> str | None:
        try:
            result = await moderation_service._call_image_moderation_for_user(url, actor.id, context_id=context_id)
            return moderation_service._normalize_result(result, "image")["decision"]
        except Exception:
            return None

    decisions = await asyncio.gather(check_text(), *(check_image(url) for url in new_images))
    if decisions[0] == "block":
        raise _error("announcement_rejected", "公告里有不合适的内容，请修改后重试", status.HTTP_422_UNPROCESSABLE_ENTITY)
    if any(decision == "block" for decision in decisions[1:]):
        raise _error("announcement_image_rejected", "有图片未通过审核，请更换后重试", status.HTTP_422_UNPROCESSABLE_ENTITY)


# ------------------------------------------------------------------ 读


async def _require_member(db: AsyncSession, conversation_id: int, user_id: int) -> tuple[Conversation, ConversationMember]:
    # 不顺手清过期禁言：那是一次写，后面还要把连接还回去等审核
    member = await require_membership(db, conversation_id, user_id, clear_expired_mute=False)
    conversation = await require_group_conversation(db, conversation_id)
    return conversation, member


async def _require_staff(db: AsyncSession, conversation_id: int, user_id: int) -> tuple[Conversation, ConversationMember]:
    conversation, member = await _require_member(db, conversation_id, user_id)
    if member.role not in STAFF_ROLES:
        raise _forbidden()
    return conversation, member


async def _get(db: AsyncSession, conversation_id: int, announcement_id: int) -> GroupAnnouncement:
    result = await db.execute(
        select(GroupAnnouncement)
        .options(joinedload(GroupAnnouncement.author))
        .where(GroupAnnouncement.id == announcement_id, GroupAnnouncement.conversation_id == conversation_id)
    )
    announcement = result.scalar_one_or_none()
    if announcement is None:
        raise _not_found()
    return announcement


def _sort_key(announcement: GroupAnnouncement) -> tuple:
    epoch = datetime.min.replace(tzinfo=UTC)
    pinned_at = _as_utc(announcement.pinned_at) or _as_utc(announcement.updated_at) or epoch
    created_at = _as_utc(announcement.created_at) or epoch
    if announcement.pinned:
        return (0, -pinned_at.timestamp(), -created_at.timestamp(), -announcement.id)
    return (1, 0.0, -created_at.timestamp(), -announcement.id)


async def _member_count(db: AsyncSession, conversation_id: int) -> int:
    result = await db.execute(select(func.count(ConversationMember.id)).where(ConversationMember.conversation_id == conversation_id))
    return int(result.scalar_one() or 0)


async def _build_reads(
    db: AsyncSession,
    conversation_id: int,
    announcements: list[GroupAnnouncement],
    viewer_id: int | None,
) -> list[GroupAnnouncementRead]:
    if not announcements:
        return []
    ids = [item.id for item in announcements]
    member_count = await _member_count(db, conversation_id)

    # 只数现任成员的确认
    confirmed_rows = await db.execute(
        select(GroupAnnouncementReceipt.announcement_id, func.count(GroupAnnouncementReceipt.id))
        .join(
            ConversationMember,
            (ConversationMember.user_id == GroupAnnouncementReceipt.user_id) & (ConversationMember.conversation_id == conversation_id),
        )
        .where(GroupAnnouncementReceipt.announcement_id.in_(ids), GroupAnnouncementReceipt.confirmed_at.is_not(None))
        .group_by(GroupAnnouncementReceipt.announcement_id)
    )
    confirmed_counts = {announcement_id: int(count) for announcement_id, count in confirmed_rows.all()}

    mine: dict[int, GroupAnnouncementReceipt] = {}
    if viewer_id is not None:
        receipt_rows = await db.execute(
            select(GroupAnnouncementReceipt).where(
                GroupAnnouncementReceipt.announcement_id.in_(ids), GroupAnnouncementReceipt.user_id == viewer_id
            )
        )
        mine = {receipt.announcement_id: receipt for receipt in receipt_rows.scalars().all()}

    author_ids = {item.author_id for item in announcements if item.author_id is not None}
    nicknames: dict[int, str | None] = {}
    if author_ids:
        nickname_rows = await db.execute(
            select(ConversationMember.user_id, ConversationMember.nickname).where(
                ConversationMember.conversation_id == conversation_id, ConversationMember.user_id.in_(author_ids)
            )
        )
        nicknames = {user_id: nickname for user_id, nickname in nickname_rows.all()}

    reads: list[GroupAnnouncementRead] = []
    for item in announcements:
        receipt = mine.get(item.id)
        author_name = None
        if item.author is not None:
            author_name = _clean(nicknames.get(item.author_id)) or _user_name(item.author)
        reads.append(
            GroupAnnouncementRead(
                id=item.id,
                conversation_id=item.conversation_id,
                author_id=item.author_id,
                author=UserPublic.model_validate(item.author) if item.author is not None else None,
                author_display_name=author_name,
                content=item.content,
                images=[GroupAnnouncementImage(url=url, thumbnail_url=_thumbnail_url(url)) for url in _load_images(item)],
                pinned=bool(item.pinned),
                pinned_at=_as_utc(item.pinned_at) if item.pinned else None,
                popup=bool(item.popup),
                require_confirm=bool(item.require_confirm),
                send_to_new_members=bool(item.send_to_new_members),
                guide_nickname=bool(item.guide_nickname),
                created_at=_as_utc(item.created_at),
                updated_at=_as_utc(item.updated_at),
                edited_at=_as_utc(item.edited_at),
                confirmed_count=confirmed_counts.get(item.id, 0),
                member_count=member_count,
                my_seen=receipt is not None and (receipt.seen_at is not None or receipt.confirmed_at is not None),
                my_confirmed=receipt is not None and receipt.confirmed_at is not None,
            )
        )
    return reads


async def build_read(db: AsyncSession, announcement: GroupAnnouncement, viewer_id: int | None) -> GroupAnnouncementRead:
    return (await _build_reads(db, announcement.conversation_id, [announcement], viewer_id))[0]


async def list_announcements(db: AsyncSession, conversation_id: int, viewer_id: int) -> list[GroupAnnouncementRead]:
    await _require_member(db, conversation_id, viewer_id)
    result = await db.execute(
        select(GroupAnnouncement)
        .options(joinedload(GroupAnnouncement.author))
        .where(GroupAnnouncement.conversation_id == conversation_id)
    )
    announcements = sorted(result.scalars().all(), key=_sort_key)
    return await _build_reads(db, conversation_id, announcements, viewer_id)


async def get_announcement(db: AsyncSession, conversation_id: int, announcement_id: int, viewer_id: int) -> GroupAnnouncementRead:
    await _require_member(db, conversation_id, viewer_id)
    return await build_read(db, await _get(db, conversation_id, announcement_id), viewer_id)


def _popup_applies(announcement: GroupAnnouncement, joined_at: datetime | None) -> bool:
    joined = _as_utc(joined_at)
    if announcement.popup:
        since = _as_utc(announcement.popup_since) or _as_utc(announcement.created_at)
        if joined is None or (since is not None and joined <= since):
            return True
    if announcement.send_to_new_members:
        since = _as_utc(announcement.new_members_since) or _as_utc(announcement.created_at)
        if joined is not None and since is not None and joined > since:
            return True
    return False


async def pending_popups(db: AsyncSession, conversation_id: int, viewer_id: int) -> list[GroupAnnouncementRead]:
    _conversation, member = await _require_member(db, conversation_id, viewer_id)
    result = await db.execute(
        select(GroupAnnouncement)
        .options(joinedload(GroupAnnouncement.author))
        .where(
            GroupAnnouncement.conversation_id == conversation_id,
            (GroupAnnouncement.popup.is_(True)) | (GroupAnnouncement.send_to_new_members.is_(True)),
        )
    )
    candidates = [item for item in result.scalars().all() if _popup_applies(item, member.joined_at)]
    if not candidates:
        return []
    receipt_rows = await db.execute(
        select(GroupAnnouncementReceipt).where(
            GroupAnnouncementReceipt.announcement_id.in_([item.id for item in candidates]),
            GroupAnnouncementReceipt.user_id == viewer_id,
        )
    )
    receipts = {receipt.announcement_id: receipt for receipt in receipt_rows.scalars().all()}
    pending: list[GroupAnnouncement] = []
    for item in candidates:
        receipt = receipts.get(item.id)
        if receipt is not None and receipt.confirmed_at is not None:
            continue
        if not item.require_confirm and receipt is not None and receipt.seen_at is not None:
            continue
        pending.append(item)
    epoch = datetime.min.replace(tzinfo=UTC)
    pending.sort(key=lambda item: (_as_utc(item.created_at) or epoch, item.id))
    return await _build_reads(db, conversation_id, pending, viewer_id)


# ------------------------------------------------------------------ 卡片消息


def _card_metadata(announcement: GroupAnnouncement, action: Literal["created", "updated"], author_name: str) -> dict[str, Any]:
    images = _load_images(announcement)
    first = images[0] if images else None
    return {
        "system_event": CARD_SYSTEM_EVENT,
        "announcement_id": announcement.id,
        "action": action,
        "summary": announcement.content[:CARD_SUMMARY_LENGTH],
        "image_url": first,
        "image_thumbnail_url": _thumbnail_url(first) if first else None,
        "image_count": len(images),
        "author_id": announcement.author_id,
        "author_name": author_name,
        "require_confirm": bool(announcement.require_confirm),
    }


def _card_text(author_name: str, action: str, content: str) -> str:
    preview = " ".join(content.split())
    if len(preview) > CARD_PREVIEW_LENGTH:
        preview = preview[:CARD_PREVIEW_LENGTH] + "…"
    verb = "发布了群公告" if action == "created" else "更新了群公告"
    return f"{author_name} {verb}：{preview}"


async def _emit_card(
    db: AsyncSession,
    announcement: GroupAnnouncement,
    actor: User,
    actor_member: ConversationMember,
    action: Literal["created", "updated"],
) -> Message:
    """在调用方的事务里发卡片：create_system_message 会一起提交公告本身的改动。"""
    from app.services import messages as message_service

    author_name = _clean(actor_member.nickname) or _user_name(actor)
    return await message_service.create_system_message(
        db,
        announcement.conversation_id,
        actor,
        _card_text(author_name, action, announcement.content),
        _card_metadata(announcement, action, author_name),
    )


async def _card_messages(db: AsyncSession, conversation_id: int, announcement_id: int) -> list[Message]:
    # metadata 是紧凑 JSON（messages._metadata_to_json）：先用 LIKE 粗筛，再解析确认
    pattern_mid = f'%"announcement_id":{announcement_id},%'
    pattern_end = f'%"announcement_id":{announcement_id}}}%'
    result = await db.execute(
        select(Message).where(
            Message.conversation_id == conversation_id,
            Message.type == MessageType.SYSTEM,
            Message.metadata_json.like(f'%"{CARD_SYSTEM_EVENT}"%'),
            Message.metadata_json.like(pattern_mid) | Message.metadata_json.like(pattern_end),
        )
    )
    cards: list[Message] = []
    for message in result.scalars().all():
        try:
            metadata = json.loads(message.metadata_json or "")
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(metadata, dict) and metadata.get("system_event") == CARD_SYSTEM_EVENT and metadata.get("announcement_id") == announcement_id:
            cards.append(message)
    return cards


# ------------------------------------------------------------------ 写


async def _clear_other_new_member_flags(db: AsyncSession, conversation_id: int, keep_id: int | None, now: datetime) -> None:
    conditions = [GroupAnnouncement.conversation_id == conversation_id, GroupAnnouncement.send_to_new_members.is_(True)]
    if keep_id is not None:
        conditions.append(GroupAnnouncement.id != keep_id)
    await db.execute(update(GroupAnnouncement).where(*conditions).values(send_to_new_members=False, new_members_since=None, updated_at=now))


async def _upsert_receipt(db: AsyncSession, announcement_id: int, user_id: int, *, seen: bool, confirmed: bool, now: datetime) -> GroupAnnouncementReceipt:
    result = await db.execute(
        select(GroupAnnouncementReceipt).where(
            GroupAnnouncementReceipt.announcement_id == announcement_id, GroupAnnouncementReceipt.user_id == user_id
        )
    )
    receipt = result.scalar_one_or_none()
    if receipt is None:
        receipt = GroupAnnouncementReceipt(announcement_id=announcement_id, user_id=user_id)
        db.add(receipt)
    if seen and receipt.seen_at is None:
        receipt.seen_at = now
    if confirmed and receipt.confirmed_at is None:
        receipt.confirmed_at = now
        if receipt.seen_at is None:
            receipt.seen_at = now
    return receipt


async def _save_receipt(db: AsyncSession, announcement_id: int, user_id: int, *, confirmed: bool) -> GroupAnnouncementReceipt:
    """看过 / 确认落库。同一个人两个请求同时插入撞了唯一约束：回滚后再来一次，这回能读到已有的那行。"""
    for attempt in range(2):
        receipt = await _upsert_receipt(db, announcement_id, user_id, seen=True, confirmed=confirmed, now=datetime.now(UTC))
        try:
            await db.commit()
            return receipt
        except IntegrityError:
            await db.rollback()
            if attempt:
                raise
    raise AssertionError("unreachable")


async def create_announcement(
    db: AsyncSession, conversation_id: int, actor: User, payload: GroupAnnouncementCreate
) -> tuple[GroupAnnouncementRead, Message]:
    _conversation, member = await _require_staff(db, conversation_id, actor.id)
    images = _validate_images(payload.images)
    await _ensure_allowed(db, actor, conversation_id, payload.content, images)

    now = datetime.now(UTC)
    if payload.send_to_new_members:
        await _clear_other_new_member_flags(db, conversation_id, None, now)
    announcement = GroupAnnouncement(
        conversation_id=conversation_id,
        author_id=actor.id,
        content=payload.content,
        images_json=_dump_images(images),
        pinned=payload.pinned,
        pinned_at=now if payload.pinned else None,
        popup=payload.popup,
        popup_since=now if payload.popup else None,
        require_confirm=payload.require_confirm,
        send_to_new_members=payload.send_to_new_members,
        new_members_since=now if payload.send_to_new_members else None,
        guide_nickname=payload.guide_nickname,
        created_at=now,
        updated_at=now,
    )
    db.add(announcement)
    await db.flush()
    # 发布人自己不用弹、也不算未确认
    await _upsert_receipt(db, announcement.id, actor.id, seen=True, confirmed=True, now=now)
    conversation = await db.get(Conversation, conversation_id)
    conversation.announcement = payload.content
    conversation.updated_at = now
    message = await _emit_card(db, announcement, actor, member, "created")
    announcement = await _get(db, conversation_id, announcement.id)
    return await build_read(db, announcement, actor.id), message


async def update_announcement(
    db: AsyncSession, conversation_id: int, announcement_id: int, actor: User, payload: GroupAnnouncementUpdate
) -> tuple[GroupAnnouncementRead, Message | None]:
    _conversation, member = await _require_staff(db, conversation_id, actor.id)
    announcement = await _get(db, conversation_id, announcement_id)
    old_images = _load_images(announcement)
    images = _validate_images(payload.images) if payload.images is not None else old_images

    content_changed = payload.content is not None and payload.content != announcement.content
    images_changed = images != old_images
    new_images = [url for url in images if url not in set(old_images)]
    await _ensure_allowed(db, actor, conversation_id, payload.content if content_changed else None, new_images)

    now = datetime.now(UTC)
    options_changed = False
    if payload.pinned is not None and payload.pinned != bool(announcement.pinned):
        announcement.pinned = payload.pinned
        announcement.pinned_at = now if payload.pinned else None
    if payload.popup is not None and payload.popup != bool(announcement.popup):
        announcement.popup = payload.popup
        announcement.popup_since = now if payload.popup else None
        options_changed = True
    if payload.require_confirm is not None and payload.require_confirm != bool(announcement.require_confirm):
        announcement.require_confirm = payload.require_confirm
        options_changed = True
    if payload.send_to_new_members is not None and payload.send_to_new_members != bool(announcement.send_to_new_members):
        if payload.send_to_new_members:
            await _clear_other_new_member_flags(db, conversation_id, announcement.id, now)
        announcement.send_to_new_members = payload.send_to_new_members
        announcement.new_members_since = now if payload.send_to_new_members else None
        options_changed = True
    if payload.guide_nickname is not None and payload.guide_nickname != bool(announcement.guide_nickname):
        announcement.guide_nickname = payload.guide_nickname
        options_changed = True
    if content_changed:
        announcement.content = payload.content
    if images_changed:
        announcement.images_json = _dump_images(images)

    edited = content_changed or images_changed or options_changed
    announcement.updated_at = now
    message: Message | None = None
    if edited:
        announcement.edited_at = now
        conversation = await db.get(Conversation, conversation_id)
        conversation.announcement = announcement.content
        conversation.updated_at = now
        # 只改置顶不发卡片；其它改动再发一张「公告已更新」
        message = await _emit_card(db, announcement, actor, member, "updated")
    else:
        await db.commit()
    announcement = await _get(db, conversation_id, announcement_id)
    return await build_read(db, announcement, actor.id), message


async def delete_announcement(db: AsyncSession, conversation_id: int, announcement_id: int, actor: User) -> list[int]:
    """删除公告，把它的卡片都标成已删除。返回被标记的卡片消息 id。"""
    conversation, _member = await _require_staff(db, conversation_id, actor.id)
    announcement = await _get(db, conversation_id, announcement_id)
    deleted_content = announcement.content

    cards = await _card_messages(db, conversation_id, announcement_id)
    for message in cards:
        metadata = json.loads(message.metadata_json or "{}")
        metadata["deleted"] = True
        message.metadata_json = json.dumps(metadata, ensure_ascii=False, separators=(",", ":"))

    # SQLite 不一定开着外键级联，回执自己删
    await db.execute(delete(GroupAnnouncementReceipt).where(GroupAnnouncementReceipt.announcement_id == announcement_id))
    await db.delete(announcement)
    if conversation.announcement == deleted_content:
        latest_result = await db.execute(
            select(GroupAnnouncement)
            .where(GroupAnnouncement.conversation_id == conversation_id, GroupAnnouncement.id != announcement_id)
            .order_by(GroupAnnouncement.created_at.desc(), GroupAnnouncement.id.desc())
            .limit(1)
        )
        latest = latest_result.scalar_one_or_none()
        conversation.announcement = latest.content if latest else None
    conversation.updated_at = datetime.now(UTC)
    await db.commit()
    return [message.id for message in cards]


async def mark_seen(db: AsyncSession, conversation_id: int, announcement_id: int, user_id: int) -> GroupAnnouncementSeenRead:
    await _require_member(db, conversation_id, user_id)
    await _get(db, conversation_id, announcement_id)
    receipt = await _save_receipt(db, announcement_id, user_id, confirmed=False)
    return GroupAnnouncementSeenRead(announcement_id=announcement_id, my_seen=True, my_confirmed=receipt.confirmed_at is not None)


async def confirm(db: AsyncSession, conversation_id: int, announcement_id: int, user_id: int) -> GroupAnnouncementRead:
    await _require_member(db, conversation_id, user_id)
    announcement = await _get(db, conversation_id, announcement_id)
    if not announcement.require_confirm:
        raise _error("confirm_not_required", "这条公告不需要确认", status.HTTP_409_CONFLICT)
    await _save_receipt(db, announcement_id, user_id, confirmed=True)
    # 撞约束回滚过的话对象已经过期，重新取一次
    return await build_read(db, await _get(db, conversation_id, announcement_id), user_id)


async def confirmation_page(
    db: AsyncSession,
    conversation_id: int,
    announcement_id: int,
    actor_id: int,
    status_filter: Literal["confirmed", "unconfirmed"],
    limit: int,
    offset: int,
) -> GroupAnnouncementConfirmationPage:
    await _require_staff(db, conversation_id, actor_id)
    await _get(db, conversation_id, announcement_id)
    member_count = await _member_count(db, conversation_id)
    confirmed_join = (
        (GroupAnnouncementReceipt.user_id == ConversationMember.user_id)
        & (GroupAnnouncementReceipt.announcement_id == announcement_id)
        & GroupAnnouncementReceipt.confirmed_at.is_not(None)
    )
    confirmed_count = int(
        (
            await db.execute(
                select(func.count(ConversationMember.id))
                .join(GroupAnnouncementReceipt, confirmed_join)
                .where(ConversationMember.conversation_id == conversation_id)
            )
        ).scalar_one()
        or 0
    )
    stmt = (
        select(ConversationMember, GroupAnnouncementReceipt.confirmed_at)
        .options(joinedload(ConversationMember.user))
        .outerjoin(GroupAnnouncementReceipt, confirmed_join)
        .where(ConversationMember.conversation_id == conversation_id)
    )
    if status_filter == "confirmed":
        stmt = stmt.where(GroupAnnouncementReceipt.id.is_not(None)).order_by(
            GroupAnnouncementReceipt.confirmed_at.desc(), ConversationMember.id.asc()
        )
    else:
        stmt = stmt.where(GroupAnnouncementReceipt.id.is_(None)).order_by(ConversationMember.joined_at.asc(), ConversationMember.id.asc())
    rows = (await db.execute(stmt.limit(limit + 1).offset(offset))).all()
    items = [
        GroupAnnouncementConfirmationItem(
            user=UserPublic.model_validate(member.user),
            nickname=_clean(member.nickname),
            role=member.role,
            confirmed_at=_as_utc(confirmed_at),
        )
        for member, confirmed_at in rows[:limit]
    ]
    return GroupAnnouncementConfirmationPage(
        announcement_id=announcement_id,
        status=status_filter,
        member_count=member_count,
        confirmed_count=confirmed_count,
        unconfirmed_count=max(0, member_count - confirmed_count),
        items=items,
        limit=limit,
        offset=offset,
        has_more=len(rows) > limit,
    )


async def confirmation_counts(db: AsyncSession, conversation_id: int, announcement_id: int) -> tuple[int, int]:
    """(confirmed_count, member_count)，给 group.announcement.confirmations 事件用。"""
    member_count = await _member_count(db, conversation_id)
    confirmed = await db.execute(
        select(func.count(GroupAnnouncementReceipt.id))
        .join(
            ConversationMember,
            (ConversationMember.user_id == GroupAnnouncementReceipt.user_id) & (ConversationMember.conversation_id == conversation_id),
        )
        .where(GroupAnnouncementReceipt.announcement_id == announcement_id, GroupAnnouncementReceipt.confirmed_at.is_not(None))
    )
    return int(confirmed.scalar_one() or 0), member_count


async def staff_member_ids(db: AsyncSession, conversation_id: int) -> list[int]:
    result = await db.execute(
        select(ConversationMember.user_id).where(
            ConversationMember.conversation_id == conversation_id,
            ConversationMember.role.in_([MemberRole.OWNER, MemberRole.ADMIN]),
        )
    )
    return list(result.scalars().all())
