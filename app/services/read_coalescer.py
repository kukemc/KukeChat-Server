"""已读位置的合并写入。

大群里每来一条消息，所有在看的人都会 POST /read。SQLite 只有一把写锁，抢红包这种时候
几分钟上千次已读写入排队，把连接池占满（2026-10-17 20:27 那次就是这样卡死的）。

做法：同一个人在同一个会话里，上次已读写库不到 COALESCE_SECONDS 秒又来推进，就先记在内存里，
后台每 FLUSH_INTERVAL 秒用一个事务把攒下的一起写掉。只会往前推（WHERE 旧值 < 新值），
所以和直接写库的路径并存不会把已读位置往回拉。进程重启最多丢几秒的已读位置，客户端下次进会话会再报。

只有后台循环在跑（start_read_coalescer）时才合并；测试里不起循环，照旧直接写库。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from sqlalchemy import or_, update

from app.core.database import AsyncSessionLocal
from app.models.conversation import ConversationMember

logger = logging.getLogger(__name__)

COALESCE_SECONDS = 5.0
FLUSH_INTERVAL = 3.0

# (conversation_id, user_id) -> 攒下的最大已读消息 id
_pending: dict[tuple[int, int], int] = {}
_worker_task: asyncio.Task | None = None


def active() -> bool:
    return _worker_task is not None and not _worker_task.done()


def should_defer(last_read_at: datetime | None, now: datetime) -> bool:
    if not active() or last_read_at is None:
        return False
    if last_read_at.tzinfo is None:
        last_read_at = last_read_at.replace(tzinfo=UTC)
    return (now - last_read_at).total_seconds() < COALESCE_SECONDS


def defer(conversation_id: int, user_id: int, message_id: int) -> None:
    key = (conversation_id, user_id)
    if _pending.get(key, 0) < message_id:
        _pending[key] = message_id


def pending_for(conversation_id: int, user_id: int) -> int | None:
    return _pending.get((conversation_id, user_id))


async def flush() -> None:
    if not _pending:
        return
    batch = dict(_pending)
    _pending.clear()
    now = datetime.now(UTC)
    try:
        async with AsyncSessionLocal() as db:
            # 按主键顺序更新：两个 worker 同时 flush、碰到同几行时加锁顺序一致，不会互相死锁
            for (conversation_id, user_id), message_id in sorted(batch.items()):
                await db.execute(
                    update(ConversationMember)
                    .where(
                        ConversationMember.conversation_id == conversation_id,
                        ConversationMember.user_id == user_id,
                        or_(
                            ConversationMember.last_read_message_id.is_(None),
                            ConversationMember.last_read_message_id < message_id,
                        ),
                    )
                    .values(last_read_message_id=message_id, last_read_at=now)
                )
            await db.commit()
    except Exception:
        logger.exception("read coalescer: flush of %d receipts failed, will retry", len(batch))
        for key, message_id in batch.items():
            if _pending.get(key, 0) < message_id:
                _pending[key] = message_id


async def _worker() -> None:
    while True:
        await asyncio.sleep(FLUSH_INTERVAL)
        await flush()


def start_read_coalescer() -> None:
    global _worker_task
    if active():
        return
    _worker_task = asyncio.create_task(_worker())


async def stop_read_coalescer() -> None:
    global _worker_task
    task = _worker_task
    _worker_task = None
    if task is not None and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    await flush()
