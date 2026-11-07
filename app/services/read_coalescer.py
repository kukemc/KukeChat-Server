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

from sqlalchemy import bindparam, or_, update

from app.core.database import AsyncSessionLocal
from app.models.conversation import ConversationMember

logger = logging.getLogger(__name__)

COALESCE_SECONDS = 5.0
FLUSH_INTERVAL = 3.0
# 一个事务最多写这么多行
FLUSH_CHUNK = 100

_members = ConversationMember.__table__
# 用 Core 语句 + 参数列表走 executemany（ORM 的 update + 参数列表会被当成「按主键批量更新」）
_FLUSH_STATEMENT = (
    update(_members)
    .where(
        _members.c.conversation_id == bindparam("cid"),
        _members.c.user_id == bindparam("uid"),
        or_(_members.c.last_read_message_id.is_(None), _members.c.last_read_message_id < bindparam("mid")),
    )
    .values(last_read_message_id=bindparam("mid"), last_read_at=bindparam("now"))
)

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
    # 按主键顺序更新：两个 worker 同时 flush、碰到同几行时加锁顺序一致，不会互相死锁
    items = sorted(batch.items())
    for start in range(0, len(items), FLUSH_CHUNK):
        chunk = items[start:start + FLUSH_CHUNK]
        try:
            async with AsyncSessionLocal() as db:
                # 一个 executemany 发出去（asyncpg 是流水线执行），每块单独提交：以前几百条 UPDATE 一条条往返、
                # 整批一个事务，这些成员行的锁要拿到最后一条写完，同时来的已读、发消息都在等（2026-11-07 单行 UPDATE 300–500ms）
                conn = await db.connection()
                await conn.execute(
                    _FLUSH_STATEMENT,
                    [{"cid": cid, "uid": uid, "mid": mid, "now": now} for (cid, uid), mid in chunk],
                )
                await db.commit()
        except Exception:
            logger.exception("read coalescer: flush of %d receipts failed, will retry", len(chunk))
            for key, message_id in chunk:
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
