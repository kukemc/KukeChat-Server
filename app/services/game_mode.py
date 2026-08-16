"""游戏模式：把群聊接入一个 CCW 作品。

作品内的玩家可以实时看到群消息；登录后还能直接发言，发第一条消息时自动入群。

**隐私边界**：开启游戏模式意味着该群的近期聊天记录对所有游玩该作品的人可见，
包括未登录的匿名访客。因此这里强制两条约束：

1. 只有群主能开启
2. 只有公开群（`join_mode == "open"`）能开启 —— 审批制 / 邀请制的私密群不能误开

需要说明的是，「作品 oid」由客户端上报，服务端无法验证浏览器真实所在的页面
（ccw.site 的 Referer 不含作品标识）。所以绑定校验防的是误用与串台，不是恶意
嵌入。真正的保护在于：这是群主主动开启的公开群。
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.conversation import Conversation, ConversationMember
from app.models.enums import ConversationType, MemberRole, MessageType
from app.models.message import Message
from app.models.user import User
from app.schemas.game import GameModeRead, GameModeUpdate, GameParticipant, GameSessionRead
from app.schemas.message import MessageRead
from app.services import messages as message_service
from app.services.conversations import require_group_conversation

# 玩家进入作品时能看到的历史消息条数
GAME_HISTORY_LIMIT = 50


async def _member_count(db: AsyncSession, conversation_id: int) -> int:
    result = await db.execute(
        select(func.count()).select_from(ConversationMember).where(ConversationMember.conversation_id == conversation_id)
    )
    return int(result.scalar_one() or 0)


async def _is_member(db: AsyncSession, conversation_id: int, user_id: int) -> bool:
    result = await db.execute(
        select(ConversationMember.id).where(
            ConversationMember.conversation_id == conversation_id,
            ConversationMember.user_id == user_id,
        )
    )
    return result.scalar_one_or_none() is not None


async def _require_owner(db: AsyncSession, conversation_id: int, user_id: int) -> ConversationMember:
    result = await db.execute(
        select(ConversationMember).where(
            ConversationMember.conversation_id == conversation_id,
            ConversationMember.user_id == user_id,
        )
    )
    member = result.scalar_one_or_none()
    if member is None or member.role != MemberRole.OWNER:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="只有群主可以设置游戏模式")
    return member


async def set_game_mode(db: AsyncSession, conversation_id: int, actor: User, payload: GameModeUpdate) -> GameModeRead:
    conversation = await require_group_conversation(db, conversation_id)
    await _require_owner(db, conversation_id, actor.id)

    if payload.enabled:
        if not payload.creation_oid:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="开启游戏模式需要绑定一个 CCW 作品",
            )
        if conversation.join_mode != "open":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="只有公开群可以开启游戏模式。开启后本群近期聊天记录会对所有游玩该作品的人可见，"
                "包括未登录访客，因此审批制或邀请制的群不允许开启。",
            )
        conversation.game_mode_enabled = True
        conversation.game_creation_oid = payload.creation_oid
    else:
        conversation.game_mode_enabled = False
        conversation.game_creation_oid = None

    conversation.updated_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(conversation)

    return GameModeRead(
        conversation_id=conversation.id,
        enabled=conversation.game_mode_enabled,
        creation_oid=conversation.game_creation_oid,
        title=conversation.title,
        member_count=await _member_count(db, conversation.id),
    )


async def get_game_mode(db: AsyncSession, conversation_id: int, actor: User) -> GameModeRead:
    conversation = await require_group_conversation(db, conversation_id)
    await _require_owner(db, conversation_id, actor.id)
    return GameModeRead(
        conversation_id=conversation.id,
        enabled=conversation.game_mode_enabled,
        creation_oid=conversation.game_creation_oid,
        title=conversation.title,
        member_count=await _member_count(db, conversation.id),
    )


async def resolve_game_conversation(db: AsyncSession, conversation_id: int, creation_oid: str) -> Conversation:
    """校验群确实开启了游戏模式且绑定的正是这个作品。"""
    oid = (creation_oid or "").strip().lower()
    if not oid:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="缺少作品标识")

    conversation = await db.get(Conversation, conversation_id)
    if conversation is None or conversation.type != ConversationType.GROUP:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="群聊不存在")
    if not conversation.game_mode_enabled:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="该群没有开启游戏模式")
    if (conversation.game_creation_oid or "").lower() != oid:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="该群没有绑定当前作品")
    return conversation


async def _recent_messages(db: AsyncSession, conversation_id: int, viewer_id: int | None) -> list[MessageRead]:
    stmt = (
        select(Message)
        .where(Message.conversation_id == conversation_id, Message.recalled_at.is_(None))
        .order_by(Message.id.desc())
        .limit(GAME_HISTORY_LIMIT)
    )
    result = await db.execute(stmt)
    items = list(reversed(result.scalars().all()))
    if not items:
        return []
    # build_message_reads 需要一个「当前用户」来计算已读/自己发的等状态。
    # 匿名访客用 0，即所有消息都按「他人发送、未交互」呈现。
    return await message_service.build_message_reads(db, items, viewer_id or 0)


async def build_game_session(db: AsyncSession, conversation: Conversation, viewer: User | None) -> GameSessionRead:
    can_send = viewer is not None
    will_auto_join = False
    participant: GameParticipant | None = None

    if viewer is not None:
        will_auto_join = not await _is_member(db, conversation.id, viewer.id)
        participant = GameParticipant(
            id=viewer.id,
            display_name=viewer.nickname or viewer.username,
            avatar_url=viewer.avatar_url,
        )

    return GameSessionRead(
        conversation_id=conversation.id,
        creation_oid=conversation.game_creation_oid or "",
        title=conversation.title,
        member_count=await _member_count(db, conversation.id),
        can_send=can_send,
        will_auto_join=will_auto_join,
        viewer=participant,
        messages=await _recent_messages(db, conversation.id, viewer.id if viewer else None),
    )


async def send_game_message(
    db: AsyncSession,
    conversation: Conversation,
    sender: User,
    content: str,
) -> tuple[Message, bool]:
    """在游戏模式群里发言，必要时自动入群。

    返回 (消息, 是否本次刚入群)。自动入群让群主原有的禁言、慢速模式、
    成员管理等手段对玩家同样生效 —— 这是选择自动入群而非「游客发言」的原因。
    """
    joined = False
    if not await _is_member(db, conversation.id, sender.id):
        db.add(
            ConversationMember(
                conversation_id=conversation.id,
                user_id=sender.id,
                role=MemberRole.MEMBER,
            )
        )
        conversation.updated_at = datetime.now(UTC)
        await db.commit()
        joined = True

    message = await message_service.send_message(
        db,
        conversation.id,
        sender,
        content,
        MessageType.TEXT,
        {"game_mode": True, "creation_oid": conversation.game_creation_oid},
    )
    return message, joined
