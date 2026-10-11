"""「正在输入」提示。纯转发：不落库，只发给会话里其他在线成员。"""

from __future__ import annotations

import time

from sqlalchemy import func, select

from app.core.database import AsyncSessionLocal
from app.models.conversation import ConversationMember
from app.models.user import User

# 客户端约 3 秒发一次，这里是兜底限速
MIN_INTERVAL_SECONDS = 2.0
# 大群里「谁在输入」没有意义，还会变成对全群的持续广播
MAX_GROUP_MEMBERS = 200
RECIPIENT_CACHE_SECONDS = 15.0
EVENT_TYPE = "conversation.typing"

_last_sent: dict[tuple[int, int], float] = {}
_recipients: dict[int, tuple[float, list[int] | None]] = {}


def _allow(user_id: int, conversation_id: int, now: float) -> bool:
    key = (user_id, conversation_id)
    last = _last_sent.get(key)
    if last is not None and now - last < MIN_INTERVAL_SECONDS:
        return False
    if len(_last_sent) > 5000:
        cutoff = now - 60
        for stale in [item for item, stamp in _last_sent.items() if stamp < cutoff]:
            _last_sent.pop(stale, None)
    _last_sent[key] = now
    return True


async def _recipient_ids(db, conversation_id: int, now: float) -> list[int] | None:
    """会话里愿意收消息的成员；None 表示这个会话不转发输入状态（群太大）。"""
    cached = _recipients.get(conversation_id)
    if cached is not None and cached[0] > now:
        return cached[1]
    total = int(
        (await db.execute(select(func.count()).select_from(ConversationMember).where(ConversationMember.conversation_id == conversation_id))).scalar_one()
        or 0
    )
    members: list[int] | None = None
    if total <= MAX_GROUP_MEMBERS:
        rows = await db.execute(
            select(ConversationMember.user_id).where(
                ConversationMember.conversation_id == conversation_id,
                ConversationMember.message_setting != "ignore",
            )
        )
        members = list(rows.scalars().all())
    if len(_recipients) > 2000:
        _recipients.clear()
    _recipients[conversation_id] = (now + RECIPIENT_CACHE_SECONDS, members)
    return members


async def handle_typing(user_id: int, conversation_id: int) -> None:
    now = time.monotonic()
    if not _allow(user_id, conversation_id, now):
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
