from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any

UTC = timezone.utc

from fastapi import HTTPException, status
from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.core import group_plus_tiers
from app.core.emoji import validate_reaction_emoji
from app.models.conversation import Conversation, ConversationMember
from app.models.enums import ConversationType, MemberRole, MessageType
from app.models.message import FeaturedMessage, MentionAllUsage, Message, MessageBookmark, MessageReaction, MessageVisibility
from app.models.user import User
from app.schemas.message import FeaturedMessageRead, MessageRead, MessageReactionSummary, ReadReceipt
from app.schemas.user import UserPublic
from app.services.penalties import ensure_can_post
from app.services.conversations import _member_mute_expired, clear_expired_member_mute, require_membership
from app.services import group_activity as group_activity_service
from app.services import group_snapshots
from app.services.ccw import enrich_metadata_with_previews, extract_creation_refs
from app.services.upload_paths import require_message_image_upload_url, require_message_or_sticker_image_url, require_message_upload_url


STAFF_ROLES = {MemberRole.OWNER, MemberRole.ADMIN}
MENTION_ALL_DAILY_LIMIT = 20
MAX_MESSAGE_CONTENT_LENGTH = 2000
MAX_BOT_MESSAGE_CONTENT_LENGTH = 8000
MESSAGE_RATE_LIMIT_PER_MINUTE = 20
MAX_MESSAGES_PER_CONVERSATION = 1000
PRUNE_BATCH_SIZE = 25


async def _enforce_message_rate_limit(
    db: AsyncSession,
    sender: User,
    now: datetime,
    conversation_id: int | None = None,
) -> None:
    rate_limit_cutoff = now - timedelta(minutes=1)
    conditions = [
        Message.sender_id == sender.id,
        Message.type != MessageType.SYSTEM,
        Message.created_at >= rate_limit_cutoff,
    ]
    if conversation_id is not None:
        conditions.append(Message.conversation_id == conversation_id)
    recent_count_result = await db.execute(select(func.count(Message.id)).where(*conditions))
    recent_count = int(recent_count_result.scalar_one() or 0)
    if recent_count >= MESSAGE_RATE_LIMIT_PER_MINUTE:
        detail = (
            "Bot can send at most 20 messages per minute in this conversation"
            if sender.is_bot and conversation_id is not None
            else "You can send at most 20 messages per minute"
        )
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=detail)


def _normalize_emoji(emoji: str) -> str:
    normalized = emoji.strip()
    if not normalized:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Emoji is required")
    if len(normalized) > 32:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Emoji is too long")
    validate_reaction_emoji(normalized)
    return normalized




def _metadata_to_json(metadata: dict[str, Any] | None) -> str | None:
    if metadata is None:
        return None
    return json.dumps(metadata, ensure_ascii=False, separators=(",", ":"))


def _metadata_from_json(metadata_json: str | None) -> dict[str, Any] | None:
    if not metadata_json:
        return None
    try:
        metadata = json.loads(metadata_json)
    except (TypeError, json.JSONDecodeError):
        return None
    return metadata if isinstance(metadata, dict) else None


def _message_upload_path(value: str) -> str | None:
    return require_message_upload_url(value)


def _metadata_image_urls(metadata: dict[str, Any] | None) -> list[str]:
    if not isinstance(metadata, dict):
        return []
    raw_images = metadata.get("images")
    if not isinstance(raw_images, list):
        return []
    return [item.strip() for item in raw_images if isinstance(item, str) and item.strip()]


async def _message_limit(db: AsyncSession, conversation_id: int, conversation: Conversation | None = None) -> int:
    """这个会话云端最多留多少条：私聊和免费群是 MAX_MESSAGES_PER_CONVERSATION，付费群按档位（docs/group-plus.md）。"""
    if conversation is None:
        row = (
            await db.execute(
                select(Conversation.type, Conversation.plus_tier, Conversation.plus_expires_at).where(Conversation.id == conversation_id)
            )
        ).one_or_none()
        if row is None:
            return MAX_MESSAGES_PER_CONVERSATION
        kind, plus_tier, plus_expires_at = row
    else:
        kind, plus_tier, plus_expires_at = conversation.type, conversation.plus_tier, conversation.plus_expires_at
    if kind != ConversationType.GROUP:
        return MAX_MESSAGES_PER_CONVERSATION
    tier = group_plus_tiers.effective_tier(plus_tier, plus_expires_at)
    if tier == group_plus_tiers.FREE:
        return MAX_MESSAGES_PER_CONVERSATION
    return max(MAX_MESSAGES_PER_CONVERSATION, group_plus_tiers.TIERS[tier]["message_limit"])


async def _prune_conversation_messages(db: AsyncSession, conversation_id: int, conversation: Conversation | None = None) -> int:
    """Remove at most one bounded batch, independently of global message ids.

    保留条数按群档位（_message_limit）。精华和群待办是独立的快照表（message_id 没有外键），不会被连带删掉。

    The existing (conversation_id, id) index finds the 1001st newest row without
    scanning unrelated chats. Older backlogs drain 25 at a time on subsequent
    sends instead of holding SQLite's single writer lock for a huge ORM cascade.
    Keep uploaded objects: URLs also survive in report/moderation snapshots and
    forwards, and an online reference check cannot prove exclusive ownership.
    """
    cutoff_result = await db.execute(
        select(Message.id)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.id.desc())
        .offset(await _message_limit(db, conversation_id, conversation))
        .limit(1)
    )
    cutoff_id = cutoff_result.scalar_one_or_none()
    if cutoff_id is None:
        return 0

    ids_result = await db.execute(
        select(Message.id)
        .where(Message.conversation_id == conversation_id, Message.id <= cutoff_id)
        .order_by(Message.id.asc())
        .limit(PRUNE_BATCH_SIZE)
    )
    old_ids = ids_result.scalars().all()
    if not old_ids:
        return 0
    # FK ON DELETE actions remove reactions/bookmarks/features and detach
    # moderation references. SQLite enables foreign_keys on each connection.
    await db.execute(delete(Message).where(Message.id.in_(old_ids)))
    return len(old_ids)


def _value_mentions_all(value: Any) -> bool:
    if value == "all":
        return True
    if isinstance(value, dict):
        if value.get("all") is True or value.get("mention_all") is True or value.get("mentionAll") is True:
            return True
        if value.get("user_id") == "all" or value.get("id") == "all" or value.get("type") == "all":
            return True
        return any(_value_mentions_all(item) for item in value.values())
    if isinstance(value, list):
        return any(_value_mentions_all(item) for item in value)
    return False


def _metadata_mentions_all(metadata: dict[str, Any] | None) -> bool:
    if not isinstance(metadata, dict):
        return False
    return metadata.get("mention_all") is True or metadata.get("mentionAll") is True or _value_mentions_all(metadata.get("mentions"))


# 一条消息最多给多少个人记「有人@我」（再多就是刷屏了，客户端也不会点名这么多人）
MAX_RECORDED_MENTIONS = 50


def _mentioned_user_ids(metadata: dict[str, Any] | None) -> list[int]:
    """metadata.mentions 里点名的用户 id。客户端发的是 [{ user_id, name }]，机器人接口解析 at 元素之后也是这个形状；也认裸的数字。"""
    mentions = metadata.get("mentions") if isinstance(metadata, dict) else None
    if not isinstance(mentions, list):
        return []
    found: list[int] = []
    for item in mentions:
        value = item.get("user_id", item.get("id")) if isinstance(item, dict) else item
        if isinstance(value, str) and value.isdigit():
            value = int(value)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0 and value not in found:
            found.append(value)
    return found[:MAX_RECORDED_MENTIONS]


async def _record_mentions(db: AsyncSession, conversation: Conversation, message: Message, metadata: dict[str, Any] | None) -> None:
    """会话列表上的「有人@我 / @全体成员」（docs/chat-features.md 第 7 节）：记下这条消息提到了谁。在发消息的事务里调。

    点名的：只更新被点名的那几个成员行（一条语句；不在会话里的人、发送者自己不会被更新到）。
    @全体：只在会话那一行上记一下（这一行本来就要改 updated_at），不去动几千个成员行。
    读的时候和各人的已读位置比较（conversations.mention_pending），不扫消息。
    """
    targets = [user_id for user_id in _mentioned_user_ids(metadata) if user_id != message.sender_id]
    if targets:
        await db.execute(
            update(ConversationMember)
            .where(ConversationMember.conversation_id == message.conversation_id, ConversationMember.user_id.in_(targets))
            .values(last_mention_message_id=message.id)
            .execution_options(synchronize_session=False)
        )
    if _metadata_mentions_all(metadata) and conversation.type == ConversationType.GROUP:
        conversation.last_mention_all_message_id = message.id


async def clear_mentions_of(db: AsyncSession, message: Message) -> None:
    """消息被撤回：它如果正是某些人 / 这个会话记着的「最近一条提及」，就清掉，不再提示。调用方提交。

    清掉而不是退回到上一条：更早的那条提及如果也还没读，提示会跟着消失 —— 尽力而为，换来的是不用为了找上一条去扫消息。
    """
    await db.execute(
        update(ConversationMember)
        .where(ConversationMember.conversation_id == message.conversation_id, ConversationMember.last_mention_message_id == message.id)
        .values(last_mention_message_id=None)
        .execution_options(synchronize_session=False)
    )
    await db.execute(
        update(Conversation)
        .where(Conversation.id == message.conversation_id, Conversation.last_mention_all_message_id == message.id)
        .values(last_mention_all_message_id=None)
        .execution_options(synchronize_session=False)
    )


async def _enforce_mention_all_limit(
    db: AsyncSession,
    conversation: Conversation,
    membership: ConversationMember,
    sender_id: int,
    metadata: dict[str, Any] | None,
) -> None:
    if not _metadata_mentions_all(metadata):
        return
    if conversation.type != ConversationType.GROUP:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="@all can only be used in group conversations")
    if membership.role not in STAFF_ROLES:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only group owners or admins can @all")

    today = datetime.now(UTC).date()
    result = await db.execute(
        select(MentionAllUsage).where(MentionAllUsage.user_id == sender_id, MentionAllUsage.usage_date == today)
    )
    usage = result.scalar_one_or_none()
    if usage is not None and usage.count >= MENTION_ALL_DAILY_LIMIT:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="@all daily limit exceeded")
    if usage is None:
        db.add(MentionAllUsage(user_id=sender_id, usage_date=today, count=1))
    else:
        usage.count += 1
        usage.updated_at = datetime.now(UTC)


def _display_name(user: User) -> str:
    return user.nickname or user.username


def _clean_text(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    return text or None


async def _message_sender_display_names(
    db: AsyncSession,
    messages: list[Message],
) -> dict[int, str]:
    if not messages:
        return {}

    pairs = {(message.conversation_id, message.sender_id) for message in messages}
    if not pairs:
        return {}

    conditions = [
        and_(
            ConversationMember.conversation_id == conversation_id,
            ConversationMember.user_id == sender_id,
        )
        for conversation_id, sender_id in pairs
    ]
    result = await db.execute(
        select(ConversationMember)
        .options(joinedload(ConversationMember.user))
        .where(or_(*conditions))
    )
    members = {
        (member.conversation_id, member.user_id): member
        for member in result.scalars().all()
    }
    names: dict[int, str] = {}
    for message in messages:
        member = members.get((message.conversation_id, message.sender_id))
        if member is not None and member.nickname:
            names[message.id] = member.nickname
        elif message.sender is not None:
            names[message.id] = _display_name(message.sender)
    return names


async def _message_sender_titles(
    db: AsyncSession,
    messages: list[Message],
) -> dict[int, str]:
    if not messages:
        return {}
    pairs = {(message.conversation_id, message.sender_id) for message in messages}
    if not pairs:
        return {}
    conditions = [
        and_(
            ConversationMember.conversation_id == conversation_id,
            ConversationMember.user_id == sender_id,
        )
        for conversation_id, sender_id in pairs
    ]
    result = await db.execute(select(ConversationMember).where(or_(*conditions)))
    members = {(member.conversation_id, member.user_id): member for member in result.scalars().all()}
    titles: dict[int, str] = {}
    for message in messages:
        title = _clean_text(members.get((message.conversation_id, message.sender_id)).title if members.get((message.conversation_id, message.sender_id)) is not None else None)
        if title:
            titles[message.id] = title
    return titles


def _message_read_from_orm(
    message: Message,
    reactions: list[MessageReactionSummary],
    sender_display_name: str | None = None,
    sender_title: str | None = None,
    sender_level: int | None = None,
    bookmarked_by_me: bool = False,
    featured: bool = False,
) -> MessageRead:
    return MessageRead(
        id=message.id,
        conversation_id=message.conversation_id,
        sender_id=message.sender_id,
        sender=UserPublic.model_validate(message.sender),
        sender_display_name=sender_display_name,
        sender_title=sender_title,
        sender_level=sender_level,
        type=message.type,
        content=message.content,
        metadata=_metadata_from_json(message.metadata_json),
        created_at=message.created_at,
        recalled_at=message.recalled_at,
        recalled_by_id=message.recalled_by_id,
        reactions=reactions,
        bookmarked_by_me=bookmarked_by_me,
        featured=featured,
    )


async def build_reaction_summaries_by_message(
    db: AsyncSession,
    message_ids: list[int],
    current_user_id: int,
) -> dict[int, list[MessageReactionSummary]]:
    if not message_ids:
        return {}

    result = await db.execute(
        select(MessageReaction)
        .options(joinedload(MessageReaction.user))
        .where(MessageReaction.message_id.in_(message_ids))
        .order_by(MessageReaction.message_id.asc(), MessageReaction.created_at.asc(), MessageReaction.id.asc())
    )

    grouped: dict[int, dict[str, list[MessageReaction]]] = {message_id: {} for message_id in message_ids}
    for reaction in result.scalars().all():
        grouped.setdefault(reaction.message_id, {}).setdefault(reaction.emoji, []).append(reaction)

    summaries: dict[int, list[MessageReactionSummary]] = {}
    for message_id in message_ids:
        emoji_groups = grouped.get(message_id, {})
        summaries[message_id] = [
            MessageReactionSummary(
                emoji=emoji,
                count=len(reactions),
                reacted_by_me=any(reaction.user_id == current_user_id for reaction in reactions),
                users=[UserPublic.model_validate(reaction.user) for reaction in reactions if reaction.user is not None],
            )
            for emoji, reactions in sorted(emoji_groups.items(), key=lambda item: (-len(item[1]), item[0]))
        ]
    return summaries


async def build_bookmark_flags_by_message(db: AsyncSession, message_ids: list[int], current_user_id: int) -> set[int]:
    if not message_ids:
        return set()
    result = await db.execute(
        select(MessageBookmark.message_id).where(
            MessageBookmark.message_id.in_(message_ids),
            MessageBookmark.user_id == current_user_id,
        )
    )
    return set(result.scalars().all())


async def build_featured_flags_by_message(db: AsyncSession, message_ids: list[int]) -> set[int]:
    if not message_ids:
        return set()
    result = await db.execute(select(FeaturedMessage.message_id).where(FeaturedMessage.message_id.in_(message_ids)))
    return set(result.scalars().all())


async def build_message_read(db: AsyncSession, message: Message, current_user_id: int) -> MessageRead:
    reads = await build_message_reads(db, [message], current_user_id)
    return reads[0]


def build_new_message_read(message: Message, current_user_id: int) -> MessageRead:
    sender_name = _clean_text(getattr(message, "_sender_display_name", None))
    if sender_name is None and message.sender is not None:
        sender_name = _display_name(message.sender)
    sender_title = _clean_text(getattr(message, "_sender_title", None))
    sender_level = getattr(message, "_sender_level", None)
    read = _message_read_from_orm(
        message,
        reactions=[],
        sender_display_name=sender_name,
        sender_title=sender_title,
        sender_level=sender_level if isinstance(sender_level, int) else None,
        bookmarked_by_me=False,
        featured=False,
    )
    if message.type == MessageType.POLL:
        from app.services import polls as poll_service

        poll_service.fill_empty_state(read)
    client_nonce = getattr(message, "_client_nonce", None)
    if isinstance(client_nonce, str) and client_nonce:
        read.client_nonce = client_nonce
    return read


async def build_message_reads(db: AsyncSession, messages: list[Message], current_user_id: int) -> list[MessageRead]:
    message_ids = [message.id for message in messages]
    summaries = await build_reaction_summaries_by_message(db, message_ids, current_user_id)
    bookmarked_ids = await build_bookmark_flags_by_message(db, message_ids, current_user_id)
    featured_ids = await build_featured_flags_by_message(db, message_ids)
    sender_display_names = await _message_sender_display_names(db, messages)
    sender_titles = await _message_sender_titles(db, messages)
    sender_levels = await _message_sender_levels(db, messages)
    reads = [
        _message_read_from_orm(
            message,
            summaries.get(message.id, []),
            sender_display_names.get(message.id),
            sender_titles.get(message.id),
            sender_levels.get(message.id),
            message.id in bookmarked_ids,
            message.id in featured_ids,
        )
        for message in messages
    ]
    if any(message.type == MessageType.POLL for message in messages):
        from app.services import polls as poll_service

        await poll_service.attach_states(db, reads, current_user_id)
    return reads


async def _message_sender_levels(
    db: AsyncSession,
    messages: list[Message],
) -> dict[int, int]:
    if not messages:
        return {}
    conversation_ids = {message.conversation_id for message in messages}
    conversation_result = await db.execute(
        select(Conversation.id).where(
            Conversation.id.in_(conversation_ids),
            Conversation.type == ConversationType.GROUP,
        )
    )
    group_conversation_ids = set(conversation_result.scalars().all())
    pairs = {(message.conversation_id, message.sender_id) for message in messages if message.conversation_id in group_conversation_ids}
    if not pairs:
        return {}
    conditions = [
        and_(
            ConversationMember.conversation_id == conversation_id,
            ConversationMember.user_id == sender_id,
        )
        for conversation_id, sender_id in pairs
    ]
    result = await db.execute(select(ConversationMember).where(or_(*conditions)))
    members = {(member.conversation_id, member.user_id): member for member in result.scalars().all()}
    levels: dict[int, int] = {}
    for message in messages:
        member = members.get((message.conversation_id, message.sender_id))
        if member is not None:
            levels[message.id] = member.level
    return levels


async def get_message_reaction_summaries(
    db: AsyncSession,
    message_id: int,
    current_user_id: int,
) -> list[MessageReactionSummary]:
    summaries = await build_reaction_summaries_by_message(db, [message_id], current_user_id)
    return summaries.get(message_id, [])


async def send_message(
    db: AsyncSession,
    conversation_id: int,
    sender: User,
    content: str,
    message_type: MessageType = MessageType.TEXT,
    metadata: dict[str, Any] | None = None,
    *,
    allow_poll: bool = False,
    allow_red_packet: bool = False,
    allow_gift: bool = False,
    allow_file: bool = False,
    allow_group_todo: bool = False,
    red_packet_chain: bool = False,
    commit: bool = True,
) -> Message:
    """commit=False：消息只 flush 不提交，由调用方和自己的改动一起提交（发红包、送礼用：扣款、记录、消息要么都成要么都不成）。

    red_packet_chain=True：领取接龙红包时自动发的那句话（red_packets.claim，docs/red-packets.md 3.4）。
    允许 metadata 里带 red_packet_chain，不受每人每分钟 20 条和群发言限频的约束（领取次数本身有限：一个红包一人一次），
    也不去抓作品链接的预览（这时调用方拿着写锁，不能等外部请求）。
    """
    ensure_can_post(sender)
    # 「接龙」标记只能由领取接龙红包时自动发的消息带；别的消息（比如客户端把接龙消息原样转发）里的这一项直接去掉
    if not red_packet_chain and isinstance(metadata, dict) and "red_packet_chain" in metadata:
        metadata = {key: value for key, value in metadata.items() if key != "red_packet_chain"} or None
    # 投票消息必须由 polls.create_poll 创建，否则 metadata 里的定义没有对应的投票记录
    if message_type == MessageType.POLL and not allow_poll:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Polls must be created through the poll endpoint")
    # 红包消息同理，只能由 red_packets.send_red_packet 创建（那里扣款）
    if message_type == MessageType.RED_PACKET and not allow_red_packet:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Red packets must be sent through the red packet endpoint")
    # 别的类型的消息也不能在 metadata 里夹带一个红包（把红包消息原样「转发」出去）
    if message_type != MessageType.RED_PACKET and isinstance(metadata, dict) and "red_packet" in metadata:
        raise _red_packet_not_forwardable()
    # 礼物消息只能由 gifts.send_gift 创建（那里扣款、记账）；别的类型的消息也不能在 metadata 里夹带一个礼物卡片
    if message_type == MessageType.GIFT and not allow_gift:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Gifts must be sent through the gift endpoint")
    if message_type != MessageType.GIFT and isinstance(metadata, dict) and "gift" in metadata:
        raise _gift_not_forwardable()
    # 文件消息（metadata.file）只能由群文件上传接口创建，群待办提醒（metadata.group_todo）只能由群待办接口创建
    if isinstance(metadata, dict) and "file" in metadata and not allow_file:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail={"code": "file_not_sendable", "message": "文件消息只能通过群文件上传接口发送"}
        )
    if isinstance(metadata, dict) and "group_todo" in metadata and not allow_group_todo:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail={"code": "group_todo_not_sendable", "message": "群待办提醒只能通过群待办接口发送"}
        )
    max_content_length = MAX_BOT_MESSAGE_CONTENT_LENGTH if sender.is_bot else MAX_MESSAGE_CONTENT_LENGTH
    if len(content) > max_content_length:
        detail = (
            "Bot message content must not exceed 8000 characters"
            if sender.is_bot
            else "Message content must not exceed 2000 characters"
        )
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=detail)
    if message_type == MessageType.IMAGE:
        if isinstance(metadata, dict) and metadata.get("sticker") is True:
            require_message_or_sticker_image_url(content, detail="Sticker message must reference an uploaded image or sticker file")
        else:
            require_message_image_upload_url(content, detail="Image message must reference an uploaded image file")
    if message_type == MessageType.VOICE and _message_upload_path(content) is None:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Voice message must reference an uploaded voice file")
    for image_url in _metadata_image_urls(metadata):
        require_message_image_upload_url(image_url, detail="Message images must reference uploaded image files")

    now = datetime.now(UTC)
    if not red_packet_chain:
        await _enforce_message_rate_limit(db, sender, now, conversation_id if sender.is_bot else None)

    # Membership checks must not flush expired-mute updates before external previews.
    membership = await require_membership(db, conversation_id, sender.id, clear_expired_mute=False)
    conversation = await db.get(Conversation, conversation_id)
    if conversation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")
    if conversation.type == ConversationType.GROUP and membership.role not in STAFF_ROLES:
        if conversation.all_muted:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="This group is muted")
        if membership.muted and not _member_mute_expired(membership, now):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Muted members cannot send messages")
        if conversation.message_rate_limit_per_minute > 0 and not red_packet_chain:
            group_rate_cutoff = now - timedelta(minutes=1)
            group_rate_result = await db.execute(
                select(func.count(Message.id)).where(
                    Message.conversation_id == conversation_id,
                    Message.sender_id == sender.id,
                    Message.recalled_at.is_(None),
                    Message.type != MessageType.SYSTEM,
                    Message.created_at >= group_rate_cutoff,
                )
            )
            group_recent_count = int(group_rate_result.scalar_one() or 0)
            if group_recent_count >= conversation.message_rate_limit_per_minute:
                raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Group message rate limit is enabled")

    # Reject unauthorized @all before touching the network; its usage row must
    # only be inserted after the external request has finished.
    if _metadata_mentions_all(metadata):
        if conversation.type != ConversationType.GROUP:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="@all can only be used in group conversations")
        if membership.role not in STAFF_ROLES:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only group owners or admins can @all")
    if message_type == MessageType.TEXT and not red_packet_chain and extract_creation_refs(content):
        metadata = await enrich_metadata_with_previews(content, metadata)
    if not allow_group_todo:
        # 群待办的提醒走 @全体 的链路，但不占用也不受限于 @全体成员 的每日次数
        await _enforce_mention_all_limit(db, conversation, membership, sender.id, metadata)
    # A prior expired mute may need persistence; do not acquire a write lock until
    # the network lookup has completed.
    if membership.muted:
        await clear_expired_member_mute(db, membership, now)

    message = Message(
        conversation_id=conversation_id,
        sender_id=sender.id,
        type=message_type,
        content=content,
        metadata_json=_metadata_to_json(metadata),
    )
    db.add(message)
    await db.flush()
    message.sender = sender
    setattr(message, "_sender_display_name", membership.nickname)
    setattr(message, "_sender_title", membership.title)
    previous_level = membership.level
    current_level = membership.level
    if conversation.type == ConversationType.GROUP and message_type != MessageType.SYSTEM:
        previous_level, current_level = await group_activity_service.record_message_activity(db, conversation_id, membership, message)
        setattr(message, "_sender_level", membership.level)
        setattr(message, "_sender_level_exp", membership.level_exp)
    setattr(message, "_level_previous", previous_level)
    setattr(message, "_level_current", current_level)
    if message_type != MessageType.SYSTEM:
        await _record_mentions(db, conversation, message, metadata)
    conversation.updated_at = now
    membership.last_read_message_id = message.id
    membership.last_read_at = now
    await _prune_conversation_messages(db, conversation_id, conversation)
    if commit:
        await db.commit()
    return message


async def create_system_message(
    db: AsyncSession,
    conversation_id: int,
    sender: User | int,
    content: str,
    metadata: dict[str, Any] | None = None,
) -> Message:
    conversation = await db.get(Conversation, conversation_id)
    if conversation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")

    sender_id = sender.id if isinstance(sender, User) else int(sender)
    now = datetime.now(UTC)
    message = Message(
        conversation_id=conversation_id,
        sender_id=sender_id,
        type=MessageType.SYSTEM,
        content=content,
        metadata_json=_metadata_to_json(metadata),
    )
    db.add(message)
    await db.flush()
    if isinstance(sender, User):
        message.sender = sender

    conversation.updated_at = now
    membership_result = await db.execute(
        select(ConversationMember).where(
            ConversationMember.conversation_id == conversation_id,
            ConversationMember.user_id == sender_id,
        )
    )
    membership = membership_result.scalar_one_or_none()
    if membership is not None:
        membership.last_read_message_id = message.id
        membership.last_read_at = now

    await _prune_conversation_messages(db, conversation_id, conversation)
    await db.commit()
    return message


async def recall_message(db: AsyncSession, conversation_id: int, actor_id: int, message_id: int) -> Message:
    actor = await require_membership(db, conversation_id, actor_id)
    conversation = await db.get(Conversation, conversation_id)
    if conversation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")

    result = await db.execute(
        select(Message)
        .options(joinedload(Message.sender))
        .where(Message.id == message_id, Message.conversation_id == conversation_id)
    )
    message = result.scalar_one_or_none()
    if message is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Message not found")

    is_sender = message.sender_id == actor_id
    if not is_sender:
        if conversation.type == ConversationType.DIRECT:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only the sender can recall this message")
        if actor.role not in STAFF_ROLES:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only group owners or admins can recall others' messages")

        sender_member_result = await db.execute(
            select(ConversationMember).where(
                ConversationMember.conversation_id == conversation_id,
                ConversationMember.user_id == message.sender_id,
            )
        )
        sender_member = sender_member_result.scalar_one_or_none()
        if actor.role == MemberRole.ADMIN and sender_member is not None and sender_member.role in STAFF_ROLES:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admins cannot recall owner or admin messages")

    now = datetime.now(UTC)
    if message.recalled_at is None:
        message.recalled_at = now
        message.recalled_by_id = actor_id
        conversation.updated_at = now
        await clear_mentions_of(db, message)
        await db.commit()
        await _end_red_packet(db, message)
    return await get_message(db, message.id)


def _red_packet_not_forwardable() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail={"code": "red_packet_not_forwardable", "message": "红包不能转发"}
    )


def _gift_not_forwardable() -> HTTPException:
    """礼物消息不能转发（docs/gifts.md）。撤回礼物消息不需要钩子：只是把消息撤掉，礼物和币都不退。"""
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail={"code": "gift_not_forwardable", "message": "礼物不能转发"})


async def _end_red_packet(db: AsyncSession, message: Message) -> None:
    """红包消息被撤回 = 红包提前结束，没领的退回发送者（docs/red-packets.md）。要在撤回已经提交之后调。

    别的撤回路径（机器审核、案件结案）不经过这里，由红包的定时清扫在一分钟内收尾；
    不管哪条路径，消息一旦撤回就领不了了（领取那条语句自己会看消息有没有被撤回）。
    """
    if message.type != MessageType.RED_PACKET:
        return
    from app.services import red_packets as red_packet_service

    await red_packet_service.end_for_recalled_message(db, message.id)


async def get_message(db: AsyncSession, message_id: int) -> Message:
    result = await db.execute(select(Message).options(joinedload(Message.sender)).where(Message.id == message_id))
    message = result.scalar_one_or_none()
    if message is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Message not found")
    return message


async def get_visible_message_for_user(db: AsyncSession, message_id: int, user_id: int) -> Message:
    message = await get_message(db, message_id)
    return await _get_visible_message(db, message.conversation_id, user_id, message_id)


async def admin_recall_message(db: AsyncSession, actor_id: int, message_id: int, *, commit: bool = True) -> Message:
    message = await get_message(db, message_id)
    if message.recalled_at is None:
        message.recalled_at = datetime.now(UTC)
        message.recalled_by_id = actor_id
        conversation = await db.get(Conversation, message.conversation_id)
        if conversation is not None:
            conversation.updated_at = message.recalled_at
        await clear_mentions_of(db, message)
        if commit:
            await db.commit()
            await _end_red_packet(db, message)
    return await get_message(db, message.id)


async def _is_message_hidden(db: AsyncSession, message_id: int, user_id: int) -> bool:
    result = await db.execute(
        select(MessageVisibility.id).where(
            MessageVisibility.message_id == message_id,
            MessageVisibility.user_id == user_id,
        )
    )
    return result.scalar_one_or_none() is not None


async def _get_visible_message(
    db: AsyncSession,
    conversation_id: int,
    actor_id: int,
    message_id: int,
    *,
    allow_recalled: bool = True,
    check_hidden: bool = True,
) -> Message:
    membership = await require_membership(db, conversation_id, actor_id)
    result = await db.execute(
        select(Message)
        .options(joinedload(Message.sender))
        .where(Message.id == message_id, Message.conversation_id == conversation_id)
    )
    message = result.scalar_one_or_none()
    if message is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Message not found")
    if membership.cleared_before_message_id is not None and message.id <= membership.cleared_before_message_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Message not found")
    if check_hidden and await _is_message_hidden(db, message.id, actor_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Message not found")
    if not allow_recalled and message.recalled_at is not None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot react to recalled messages")
    return message


async def _get_reactable_message(db: AsyncSession, conversation_id: int, actor_id: int, message_id: int) -> Message:
    return await _get_visible_message(db, conversation_id, actor_id, message_id, allow_recalled=False)


async def toggle_message_reaction(
    db: AsyncSession,
    conversation_id: int,
    actor_id: int,
    message_id: int,
    emoji: str,
) -> list[MessageReactionSummary]:
    emoji = _normalize_emoji(emoji)
    await _get_reactable_message(db, conversation_id, actor_id, message_id)

    result = await db.execute(
        select(MessageReaction).where(
            MessageReaction.message_id == message_id,
            MessageReaction.user_id == actor_id,
            MessageReaction.emoji == emoji,
        )
    )
    reaction = result.scalar_one_or_none()
    if reaction is None:
        db.add(MessageReaction(message_id=message_id, user_id=actor_id, emoji=emoji))
    else:
        await db.delete(reaction)
    await db.commit()
    return await get_message_reaction_summaries(db, message_id, actor_id)


async def delete_message_reaction(
    db: AsyncSession,
    conversation_id: int,
    actor_id: int,
    message_id: int,
    emoji: str,
) -> list[MessageReactionSummary]:
    emoji = _normalize_emoji(emoji)
    await _get_reactable_message(db, conversation_id, actor_id, message_id)

    result = await db.execute(
        select(MessageReaction).where(
            MessageReaction.message_id == message_id,
            MessageReaction.user_id == actor_id,
            MessageReaction.emoji == emoji,
        )
    )
    reaction = result.scalar_one_or_none()
    if reaction is not None:
        await db.delete(reaction)
        await db.commit()
    return await get_message_reaction_summaries(db, message_id, actor_id)


async def hide_message_for_user(db: AsyncSession, conversation_id: int, actor_id: int, message_id: int) -> Message:
    message = await _get_visible_message(db, conversation_id, actor_id, message_id, check_hidden=False)
    existing = await db.execute(
        select(MessageVisibility).where(
            MessageVisibility.message_id == message_id,
            MessageVisibility.user_id == actor_id,
        )
    )
    if existing.scalar_one_or_none() is None:
        db.add(MessageVisibility(message_id=message_id, user_id=actor_id))
        await db.commit()
    return message


async def toggle_message_bookmark(db: AsyncSession, conversation_id: int, actor_id: int, message_id: int) -> bool:
    await _get_visible_message(db, conversation_id, actor_id, message_id, allow_recalled=False)
    result = await db.execute(
        select(MessageBookmark).where(
            MessageBookmark.message_id == message_id,
            MessageBookmark.user_id == actor_id,
        )
    )
    bookmark = result.scalar_one_or_none()
    if bookmark is None:
        db.add(MessageBookmark(message_id=message_id, user_id=actor_id))
        bookmarked = True
    else:
        await db.delete(bookmark)
        bookmarked = False
    await db.commit()
    return bookmarked


async def list_bookmarked_messages(db: AsyncSession, conversation_id: int, actor_id: int) -> list[Message]:
    membership = await require_membership(db, conversation_id, actor_id)
    hidden_message_ids = select(MessageVisibility.message_id).where(MessageVisibility.user_id == actor_id)
    stmt = (
        select(Message)
        .join(MessageBookmark, MessageBookmark.message_id == Message.id)
        .options(joinedload(Message.sender))
        .where(
            Message.conversation_id == conversation_id,
            MessageBookmark.user_id == actor_id,
            Message.id.not_in(hidden_message_ids),
        )
        .order_by(MessageBookmark.created_at.desc(), MessageBookmark.id.desc())
    )
    if membership.cleared_before_message_id is not None:
        stmt = stmt.where(Message.id > membership.cleared_before_message_id)
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def list_all_bookmarked_messages(db: AsyncSession, actor_id: int, limit: int = 100) -> list[Message]:
    member_rows = await db.execute(
        select(ConversationMember.conversation_id, ConversationMember.cleared_before_message_id).where(
            ConversationMember.user_id == actor_id
        )
    )
    cleared_by_conversation = {conversation_id: cleared_before for conversation_id, cleared_before in member_rows.all()}
    if not cleared_by_conversation:
        return []

    conversation_ids = list(cleared_by_conversation)
    hidden_message_ids = select(MessageVisibility.message_id).where(MessageVisibility.user_id == actor_id)
    stmt = (
        select(Message)
        .join(MessageBookmark, MessageBookmark.message_id == Message.id)
        .options(joinedload(Message.sender))
        .where(
            Message.conversation_id.in_(conversation_ids),
            MessageBookmark.user_id == actor_id,
            Message.id.not_in(hidden_message_ids),
        )
        .order_by(MessageBookmark.created_at.desc(), MessageBookmark.id.desc())
        .limit(limit)
    )
    cleared_filters = [
        (Message.conversation_id == conversation_id) & (Message.id <= cleared_before)
        for conversation_id, cleared_before in cleared_by_conversation.items()
        if cleared_before is not None
    ]
    if cleared_filters:
        stmt = stmt.where(~or_(*cleared_filters))
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def toggle_featured_message(db: AsyncSession, conversation_id: int, actor_id: int, message_id: int) -> bool:
    """切换精华。设为精华时把消息固化成快照（docs/group-plus.md 第 6 节）；数量上限按群档位，超了 409 featured_limit。取消不受限。"""
    actor = await require_membership(db, conversation_id, actor_id)
    conversation = await db.get(Conversation, conversation_id)
    if conversation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")
    if conversation.type != ConversationType.GROUP:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Only group messages can be featured")
    if actor.role not in STAFF_ROLES:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only group owners or admins can feature messages")

    message = await _get_visible_message(db, conversation_id, actor_id, message_id, allow_recalled=False)
    result = await db.execute(select(FeaturedMessage.id).where(FeaturedMessage.message_id == message.id).limit(1))
    if result.scalar_one_or_none() is None:
        tier = group_plus_tiers.conversation_tier(conversation)
        limit = group_plus_tiers.TIERS[tier]["featured_limit"]
        used = int(await db.scalar(select(func.count()).select_from(FeaturedMessage).where(FeaturedMessage.conversation_id == conversation_id)) or 0)
        if used >= limit:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "featured_limit",
                    "message": f"精华消息已达上限（{used}/{limit}），升级群档位可以提高上限",
                    "limit": limit,
                    "used": used,
                    "tier": tier,
                },
            )
        fields = await group_snapshots.message_snapshot_fields(db, message)
        db.add(FeaturedMessage(message_id=message.id, conversation_id=conversation_id, set_by_id=actor_id, **fields))
        featured = True
    else:
        await db.execute(delete(FeaturedMessage).where(FeaturedMessage.message_id == message.id))
        featured = False
    await db.commit()
    return featured


async def remove_featured_by_id(db: AsyncSession, conversation_id: int, actor_id: int, featured_id: int) -> int | None:
    """按精华 id 取消（原消息已经被修剪时用）。返回原来的 message_id。"""
    actor = await require_membership(db, conversation_id, actor_id)
    if actor.role not in STAFF_ROLES:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only group owners or admins can feature messages")
    row = (
        await db.execute(select(FeaturedMessage).where(FeaturedMessage.id == featured_id, FeaturedMessage.conversation_id == conversation_id))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Featured message not found")
    message_id = row.message_id
    await db.delete(row)
    await db.commit()
    return message_id


async def list_featured_messages(
    db: AsyncSession, conversation_id: int, actor_id: int, limit: int = 50, offset: int = 0
) -> tuple[list[FeaturedMessageRead], int]:
    """精华列表（来自快照，原消息被修剪后仍在）。返回 (本页, 总数)。"""
    membership = await require_membership(db, conversation_id, actor_id)
    hidden_message_ids = select(MessageVisibility.message_id).where(MessageVisibility.user_id == actor_id)
    clauses: list[Any] = [
        FeaturedMessage.conversation_id == conversation_id,
        or_(FeaturedMessage.message_id.is_(None), FeaturedMessage.message_id.not_in(hidden_message_ids)),
    ]
    if membership.cleared_before_message_id is not None:
        clauses.append(or_(FeaturedMessage.message_id.is_(None), FeaturedMessage.message_id > membership.cleared_before_message_id))
    total = int(await db.scalar(select(func.count()).select_from(FeaturedMessage).where(*clauses)) or 0)
    rows = list(
        (
            await db.execute(
                select(FeaturedMessage)
                .options(joinedload(FeaturedMessage.set_by))
                .where(*clauses)
                .order_by(FeaturedMessage.created_at.desc(), FeaturedMessage.id.desc())
                .offset(offset)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    message_ids = [row.message_id for row in rows if row.message_id is not None]
    live: dict[int, Message] = {}
    if message_ids:
        live = {
            message.id: message
            for message in (
                await db.execute(select(Message).options(joinedload(Message.sender)).where(Message.id.in_(message_ids)))
            ).scalars().all()
        }
    live_reads = {read.id: read for read in await build_message_reads(db, [m for m in live.values() if m.recalled_at is None], actor_id)}
    sender_ids = {row.snap_sender_id for row in rows if row.snap_sender_id is not None and row.message_id not in live}
    senders: dict[int, User] = {}
    if sender_ids:
        senders = {user.id: user for user in (await db.execute(select(User).where(User.id.in_(sender_ids)))).scalars().all()}
    items: list[FeaturedMessageRead] = []
    for row in rows:
        message = live.get(row.message_id) if row.message_id is not None else None
        recalled = row.snap_recalled_at is not None or (message is not None and message.recalled_at is not None)
        if message is not None and message.id in live_reads and not recalled:
            read = live_reads[message.id]
        else:
            read = group_snapshots.snapshot_message_read(
                row,
                message_id=row.message_id or 0,
                conversation_id=conversation_id,
                sender=message.sender if message is not None else senders.get(row.snap_sender_id or 0),
            )
            if recalled and read.recalled_at is None and message is not None:
                read = read.model_copy(update={"recalled_at": message.recalled_at, "content": "", "metadata": None})
        items.append(
            FeaturedMessageRead(
                id=row.id,
                conversation_id=row.conversation_id,
                message_id=row.message_id,
                set_by_id=row.set_by_id,
                set_by=UserPublic.model_validate(row.set_by) if row.set_by is not None else None,
                created_at=row.created_at,
                message=read,
                message_exists=message is not None and message.recalled_at is None,
                recalled=recalled,
                snapshot_missing=bool(row.snapshot_missing),
            )
        )
    return items, total


async def list_messages(
    db: AsyncSession,
    conversation_id: int,
    user_id: int,
    before_id: int | None = None,
    limit: int = 50,
) -> list[Message]:
    membership_result = await db.execute(
        select(ConversationMember).where(
            ConversationMember.conversation_id == conversation_id,
            ConversationMember.user_id == user_id,
        )
    )
    membership = membership_result.scalar_one_or_none()
    if membership is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Message not found")
    hidden_message_ids = select(MessageVisibility.message_id).where(MessageVisibility.user_id == user_id)
    stmt = (
        select(Message)
        .options(joinedload(Message.sender))
        .where(Message.conversation_id == conversation_id, Message.id.not_in(hidden_message_ids))
        .order_by(Message.id.desc())
        .limit(limit)
    )
    if before_id is not None:
        stmt = stmt.where(Message.id < before_id)
    if membership.cleared_before_message_id is not None:
        stmt = stmt.where(Message.id > membership.cleared_before_message_id)
    result = await db.execute(stmt)
    return list(reversed(result.scalars().all()))


async def list_messages_after(
    db: AsyncSession,
    conversation_id: int,
    user_id: int,
    after_id: int,
    limit: int = 50,
) -> list[Message]:
    membership_result = await db.execute(
        select(ConversationMember).where(
            ConversationMember.conversation_id == conversation_id,
            ConversationMember.user_id == user_id,
        )
    )
    membership = membership_result.scalar_one_or_none()
    if membership is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Message not found")
    hidden_message_ids = select(MessageVisibility.message_id).where(MessageVisibility.user_id == user_id)
    stmt = (
        select(Message)
        .options(joinedload(Message.sender))
        .where(Message.conversation_id == conversation_id, Message.id > after_id, Message.id.not_in(hidden_message_ids))
        .order_by(Message.id.asc())
        .limit(limit)
    )
    if membership.cleared_before_message_id is not None:
        stmt = stmt.where(Message.id > membership.cleared_before_message_id)
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def get_message_context(
    db: AsyncSession,
    conversation_id: int,
    message_id: int,
    user_id: int,
    before: int = 25,
    after: int = 25,
) -> list[Message]:
    membership = await require_membership(db, conversation_id, user_id)
    hidden_message_ids = select(MessageVisibility.message_id).where(MessageVisibility.user_id == user_id)
    visibility_filters: list[Any] = [
        Message.conversation_id == conversation_id,
        Message.id.not_in(hidden_message_ids),
    ]
    if membership.cleared_before_message_id is not None:
        visibility_filters.append(Message.id > membership.cleared_before_message_id)

    target_result = await db.execute(
        select(Message.id).where(Message.id == message_id, *visibility_filters).limit(1)
    )
    if target_result.scalar_one_or_none() is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Message not found")

    before_result = await db.execute(
        select(Message)
        .options(joinedload(Message.sender))
        .where(Message.id < message_id, *visibility_filters)
        .order_by(Message.id.desc())
        .limit(before)
    )
    before_messages = list(reversed(before_result.scalars().all()))

    target_message_result = await db.execute(
        select(Message)
        .options(joinedload(Message.sender))
        .where(Message.id == message_id, *visibility_filters)
    )
    target_message = target_message_result.scalar_one_or_none()
    if target_message is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Message not found")

    after_result = await db.execute(
        select(Message)
        .options(joinedload(Message.sender))
        .where(Message.id > message_id, *visibility_filters)
        .order_by(Message.id.asc())
        .limit(after)
    )
    after_messages = list(after_result.scalars().all())
    return [*before_messages, target_message, *after_messages]


async def search_messages(
    db: AsyncSession,
    conversation_id: int,
    user_id: int,
    query: str | None = None,
    category: str = "all",
    sender_id: int | None = None,
    start_at: datetime | None = None,
    end_at: datetime | None = None,
    before_id: int | None = None,
    include_recalled: bool = False,
    limit: int = 30,
) -> tuple[list[Message], int, bool, int | None]:
    membership = await require_membership(db, conversation_id, user_id)
    conversation = await db.get(Conversation, conversation_id)
    if conversation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")

    if sender_id is not None:
        if conversation.type != ConversationType.GROUP:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="sender_id filter is only supported in group conversations")
        sender_result = await db.execute(
            select(ConversationMember.id).where(
                ConversationMember.conversation_id == conversation_id,
                ConversationMember.user_id == sender_id,
            )
        )
        if sender_result.scalar_one_or_none() is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sender is not a group member")

    filters = _message_search_filters(
        conversation_id=conversation_id,
        user_id=user_id,
        membership=membership,
        query=query,
        category=category,
        sender_id=sender_id,
        start_at=start_at,
        end_at=end_at,
        before_id=before_id,
        include_recalled=include_recalled,
    )
    count_result = await db.execute(select(func.count(Message.id)).where(*filters))
    total = int(count_result.scalar_one() or 0)
    result = await db.execute(
        select(Message)
        .options(joinedload(Message.sender))
        .where(*filters)
        .order_by(Message.id.desc())
        .limit(limit + 1)
    )
    rows = list(result.scalars().all())
    has_more = len(rows) > limit
    messages = rows[:limit]
    next_before_id = messages[-1].id if has_more and messages else None
    return messages, total, has_more, next_before_id


def build_search_match(message: Message, query: str | None) -> tuple[str, str | None]:
    metadata = _metadata_from_json(message.metadata_json) or {}
    if message.type == MessageType.IMAGE:
        return ("sticker" if metadata.get("sticker") is True else "image", None)
    if message.type == MessageType.VOICE:
        return "voice", None
    if message.type == MessageType.FORWARD_BUNDLE:
        snippet = _snippet(_forward_bundle_search_text(metadata, message.content), query)
        return "forward_bundle", snippet
    if message.type == MessageType.SYSTEM:
        return "system", _snippet(message.content, query)
    if _message_has_link(message):
        return "link", _snippet(message.content, query)
    return "text", _snippet(message.content, query)


def _message_search_filters(
    *,
    conversation_id: int,
    user_id: int,
    membership: ConversationMember,
    query: str | None,
    category: str,
    sender_id: int | None,
    start_at: datetime | None,
    end_at: datetime | None,
    before_id: int | None,
    include_recalled: bool,
) -> list[Any]:
    hidden_message_ids = select(MessageVisibility.message_id).where(MessageVisibility.user_id == user_id)
    filters: list[Any] = [Message.conversation_id == conversation_id, Message.id.not_in(hidden_message_ids)]
    if membership.cleared_before_message_id is not None:
        filters.append(Message.id > membership.cleared_before_message_id)
    if not include_recalled:
        filters.append(Message.recalled_at.is_(None))
    if sender_id is not None:
        filters.append(Message.sender_id == sender_id)
    if start_at is not None:
        filters.append(Message.created_at >= start_at)
    if end_at is not None:
        filters.append(Message.created_at <= end_at)
    if before_id is not None:
        filters.append(Message.id < before_id)

    category_filter = _category_filter(category)
    if category_filter is not None:
        filters.append(category_filter)

    if query and query.strip():
        keyword = f"%{query.strip()}%"
        filters.append(or_(Message.content.ilike(keyword), Message.metadata_json.ilike(keyword)))
    return filters


def _category_filter(category: str) -> Any | None:
    if category == "all":
        return None
    if category == "text":
        return Message.type == MessageType.TEXT
    if category == "image":
        return and_(Message.type == MessageType.IMAGE, or_(Message.metadata_json.is_(None), Message.metadata_json.not_like('%"sticker"%true%')))
    if category == "sticker":
        return and_(Message.type == MessageType.IMAGE, Message.metadata_json.like('%"sticker"%true%'))
    if category == "voice":
        return Message.type == MessageType.VOICE
    if category == "link":
        return and_(Message.type == MessageType.TEXT, or_(Message.content.ilike("%http://%"), Message.content.ilike("%https://%"), Message.content.ilike("%www.%")))
    if category == "forward_bundle":
        return Message.type == MessageType.FORWARD_BUNDLE
    if category == "system":
        return Message.type == MessageType.SYSTEM
    raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid message search category")


def _message_has_link(message: Message) -> bool:
    return message.type == MessageType.TEXT and re.search(r"https?://|www\.", message.content, re.IGNORECASE) is not None


def _forward_bundle_search_text(metadata: dict[str, Any], fallback: str) -> str:
    parts = [fallback]
    note = metadata.get("note")
    if isinstance(note, str):
        parts.append(note)
    source_title = metadata.get("source_title")
    if isinstance(source_title, str):
        parts.append(source_title)
    for item in metadata.get("items", []):
        if isinstance(item, dict):
            sender_name = item.get("sender_display_name")
            content = item.get("content")
            if isinstance(sender_name, str):
                parts.append(sender_name)
            if isinstance(content, str):
                parts.append(content)
    return "\n".join(part for part in parts if part)


def _snippet(text: str | None, query: str | None, radius: int = 36) -> str | None:
    if not text:
        return None
    compact = " ".join(text.split())
    needle = query.strip() if query else ""
    if not needle:
        return compact[: radius * 2] + ("..." if len(compact) > radius * 2 else "")
    index = compact.lower().find(needle.lower())
    if index < 0:
        return compact[: radius * 2] + ("..." if len(compact) > radius * 2 else "")
    start = max(0, index - radius)
    end = min(len(compact), index + len(needle) + radius)
    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(compact) else ""
    return f"{prefix}{compact[start:end]}{suffix}"


async def admin_message_context(
    db: AsyncSession,
    conversation_id: int,
    message_id: int,
    before: int = 20,
    after: int = 20,
) -> list[Message]:
    target = await db.get(Message, message_id)
    if target is None or target.conversation_id != conversation_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Message not found")
    before_result = await db.execute(
        select(Message)
        .options(joinedload(Message.sender))
        .where(Message.conversation_id == conversation_id, Message.id < message_id)
        .order_by(Message.id.desc())
        .limit(before)
    )
    after_result = await db.execute(
        select(Message)
        .options(joinedload(Message.sender))
        .where(Message.conversation_id == conversation_id, Message.id > message_id)
        .order_by(Message.id.asc())
        .limit(after)
    )
    target_result = await db.execute(select(Message).options(joinedload(Message.sender)).where(Message.id == message_id))
    target_loaded = target_result.scalar_one()
    before_messages = list(reversed(before_result.scalars().all()))
    after_messages = list(after_result.scalars().all())
    return before_messages + [target_loaded] + after_messages


async def admin_message_context_by_id(
    db: AsyncSession,
    message_id: int,
    before: int = 20,
    after: int = 20,
) -> list[Message]:
    target = await db.get(Message, message_id)
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Message not found")
    return await admin_message_context(db, target.conversation_id, message_id, before, after)


async def _conversation_snapshot_title(db: AsyncSession, conversation: Conversation, actor_id: int) -> str:
    membership_result = await db.execute(
        select(ConversationMember).where(
            ConversationMember.conversation_id == conversation.id,
            ConversationMember.user_id == actor_id,
        )
    )
    membership = membership_result.scalar_one_or_none()
    if conversation.type == ConversationType.GROUP:
        if membership is not None and membership.remark:
            return membership.remark
        if conversation.title:
            return conversation.title
    if conversation.type == ConversationType.DIRECT:
        result = await db.execute(
            select(User)
            .join(ConversationMember, ConversationMember.user_id == User.id)
            .where(ConversationMember.conversation_id == conversation.id, User.id != actor_id)
            .limit(1)
        )
        direct_user = result.scalar_one_or_none()
        if direct_user is not None:
            return _display_name(direct_user)
    return f"会话 {conversation.id}"


async def create_forward_bundle(
    db: AsyncSession,
    target_conversation_id: int,
    actor: User,
    source_conversation_id: int,
    message_ids: list[int],
    note: str | None = None,
) -> Message:
    ensure_can_post(actor)
    source_membership = await require_membership(db, source_conversation_id, actor.id)
    await require_membership(db, target_conversation_id, actor.id)
    source_conversation = await db.get(Conversation, source_conversation_id)
    if source_conversation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Source conversation not found")

    unique_ids = list(dict.fromkeys(message_ids))
    hidden_message_ids = select(MessageVisibility.message_id).where(MessageVisibility.user_id == actor.id)
    stmt = (
        select(Message)
        .options(joinedload(Message.sender))
        .where(
            Message.conversation_id == source_conversation_id,
            Message.id.in_(unique_ids),
            Message.recalled_at.is_(None),
            Message.id.not_in(hidden_message_ids),
        )
    )
    if source_membership.cleared_before_message_id is not None:
        stmt = stmt.where(Message.id > source_membership.cleared_before_message_id)
    result = await db.execute(stmt)
    # 系统消息（戳一戳、入群提示）不进合并转发：没有发送者，旧客户端画转发记录时会崩。旧客户端多选里带上的悄悄去掉
    visible_messages = sorted((message for message in result.scalars().all() if message.type != MessageType.SYSTEM), key=lambda message: message.id)
    if not visible_messages:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No visible messages to forward")
    # 红包不能转发，夹在合并转发里也不行（docs/red-packets.md）。引用红包消息不受影响
    if any(message.type == MessageType.RED_PACKET for message in visible_messages):
        raise _red_packet_not_forwardable()
    # 礼物同理（docs/gifts.md）
    if any(message.type == MessageType.GIFT for message in visible_messages):
        raise _gift_not_forwardable()
    # 文件消息（metadata.file）同理：下载要鉴权，转发出去别的会话的人下不了（docs/group-plus.md 5.2）
    for message in visible_messages:
        meta = _metadata_from_json(message.metadata_json)
        if isinstance(meta, dict) and "file" in meta:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail={"code": "file_not_forwardable", "message": "文件消息不能转发"}
            )

    source_title = await _conversation_snapshot_title(db, source_conversation, actor.id)
    metadata: dict[str, Any] = {
        "source_conversation_id": source_conversation_id,
        "source_title": source_title,
        "items": [
            {
                "message_id": message.id,
                "sender_id": message.sender_id,
                "sender_display_name": _display_name(message.sender),
                "sender_avatar_url": message.sender.avatar_url,
                "type": message.type.value,
                "content": message.content,
                "metadata": _metadata_from_json(message.metadata_json),
                "created_at": message.created_at.isoformat(),
            }
            for message in visible_messages
        ],
    }
    if note is not None:
        metadata["note"] = note

    return await send_message(
        db,
        target_conversation_id,
        actor,
        f"{source_title} 的聊天记录",
        MessageType.FORWARD_BUNDLE,
        metadata,
    )


async def mark_conversation_read(
    db: AsyncSession,
    conversation_id: int,
    user_id: int,
    last_read_message_id: int | None = None,
) -> ReadReceipt:
    membership = await require_membership(db, conversation_id, user_id)
    if last_read_message_id is None:
        latest_result = await db.execute(
            select(Message.id)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.id.desc())
            .limit(1)
        )
        last_read_message_id = latest_result.scalar_one_or_none()
    else:
        message = await db.get(Message, last_read_message_id)
        if message is None or message.conversation_id != conversation_id:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Message does not belong to this conversation")

    now = datetime.now(UTC)
    membership.last_read_message_id = last_read_message_id
    membership.last_read_at = now
    await db.commit()
    from app.services.conversations import mention_pending

    conversation = await db.get(Conversation, conversation_id)
    return ReadReceipt(
        conversation_id=conversation_id,
        user_id=user_id,
        last_read_message_id=last_read_message_id,
        last_read_at=now,
        mention_pending=mention_pending(conversation, membership) if conversation is not None else None,
    )
