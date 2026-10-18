"""「正在输入」提示。纯转发：不落库，只发给会话里其他在线成员。"""

from __future__ import annotations

import time

from sqlalchemy import func, select

from app.core.database import AsyncSessionLocal
from app.models.conversation import Conversation, ConversationMember
from app.models.user import User

# 客户端约 3 秒发一次，这里是兜底限速；大群放得更宽
MIN_INTERVAL_SECONDS = 2.0
LARGE_GROUP_MIN_INTERVAL_SECONDS = 4.0
# typing_indicator 为 auto 的群，超过这么多人就不转发（大群里是对全群的持续广播）；群主 / 管理员可以设成 on 强制打开
MAX_GROUP_MEMBERS = 200
RECIPIENT_CACHE_SECONDS = 15.0
# 大群的成员名单更大、变化更慢，缓存久一点
LARGE_GROUP_CACHE_SECONDS = 60.0
EVENT_TYPE = "conversation.typing"

_last_sent: dict[tuple[int, int], float] = {}
# conversation_id -> (过期时间, 收件人；None 表示不转发, 是不是大群)
_recipients: dict[int, tuple[float, list[int] | None, bool]] = {}


def _allow(user_id: int, conversation_id: int, now: float, interval: float = MIN_INTERVAL_SECONDS) -> bool:
    key = (user_id, conversation_id)
    last = _last_sent.get(key)
    if last is not None and now - last < interval:
        return False
    if len(_last_sent) > 5000:
        cutoff = now - 60
        for stale in [item for item, stamp in _last_sent.items() if stamp < cutoff]:
            _last_sent.pop(stale, None)
    _last_sent[key] = now
    return True


def forget(conversation_id: int) -> None:
    """群设置改了之后调用，下一次输入时重新判断。"""
    _recipients.pop(conversation_id, None)


def _is_large(conversation_id: int) -> bool:
    cached = _recipients.get(conversation_id)
    return bool(cached is not None and cached[2])


async def _recipient_ids(db, conversation_id: int, now: float) -> list[int] | None:
    """会话里愿意收消息的成员；None 表示这个会话不转发输入状态（关了，或者 auto 且群太大）。"""
    cached = _recipients.get(conversation_id)
    if cached is not None and cached[0] > now:
        return cached[1]
    setting = (await db.execute(select(Conversation.typing_indicator).where(Conversation.id == conversation_id))).scalar_one_or_none() or "auto"
    total = int(
        (await db.execute(select(func.count()).select_from(ConversationMember).where(ConversationMember.conversation_id == conversation_id))).scalar_one()
        or 0
    )
    large = total > MAX_GROUP_MEMBERS
    members: list[int] | None = None
    if setting == "on" or (setting == "auto" and not large):
        rows = await db.execute(
            select(ConversationMember.user_id).where(
                ConversationMember.conversation_id == conversation_id,
                ConversationMember.message_setting != "ignore",
            )
        )
        members = list(rows.scalars().all())
    if len(_recipients) > 2000:
        _recipients.clear()
    _recipients[conversation_id] = (now + (LARGE_GROUP_CACHE_SECONDS if large else RECIPIENT_CACHE_SECONDS), members, large)
    return members


async def handle_typing(user_id: int, conversation_id: int) -> None:
    now = time.monotonic()
    if not _allow(user_id, conversation_id, now, LARGE_GROUP_MIN_INTERVAL_SECONDS if _is_large(conversation_id) else MIN_INTERVAL_SECONDS):
        return
    async with AsyncSessionLocal() as db:
        row = (
            await db.execute(
                select(ConversationMember.nickname, User.nickname, User.username, User.share_typing)
                .join(User, User.id == ConversationMember.user_id)
                .where(ConversationMember.conversation_id == conversation_id, ConversationMember.user_id == user_id)
            )
        ).first()
        # 不是成员，或者本人关掉了「让别人看到我在输入」
        if row is None or not row[3]:
            return
        members = await _recipient_ids(db, conversation_id, now)
    if not members:
        return
    targets = [member_id for member_id in members if member_id != user_id]
    if not targets:
        return
    from app.websocket.manager import manager

    online = await manager.online_user_ids_among(targets)
    if not online:
        return
    name = (row[0] or "").strip() or (row[1] or "").strip() or row[2]
    await manager.send_to_users(online, EVENT_TYPE, {"conversation_id": conversation_id, "user_id": user_id, "name": name})
