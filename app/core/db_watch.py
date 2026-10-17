"""数据库连接占用监控：谁借走连接太久不还，就把它此刻停在哪儿写进日志。

线上两次因为连接池被占满整个服务卡死（2026-10-16 21:50、2026-10-17 10:00），光看报错只能看到排队等连接的受害者，
看不到占着连接的那一方。这里在连接借出时记下借它的 asyncio 任务，后台每 5 秒扫一遍：占用超过
HOLD_WARN_SECONDS 的，打印那个任务当前的协程栈（只打 app/ 里的帧）。同一次借用只报一次。
"""

from __future__ import annotations

import asyncio
import logging
import time
import traceback
from typing import Any

logger = logging.getLogger("app.db_watch")

HOLD_WARN_SECONDS = 20.0
SCAN_SECONDS = 5.0

_holders: dict[int, tuple[float, asyncio.Task[Any] | None, bool]] = {}
# 已经 flush 过写、还没提交 / 回滚的会话 = 正拿着 SQLite 整库写锁的一方（别人只能 database is locked）
WRITE_WARN_SECONDS = 5.0
_writers: dict[int, tuple[float, asyncio.Task[Any] | None, bool]] = {}
_monitor: asyncio.Task[None] | None = None


def _current_task() -> asyncio.Task[Any] | None:
    try:
        return asyncio.current_task()
    except RuntimeError:
        return None


def _app_stack(task: asyncio.Task[Any]) -> str:
    """沿着 await 链从最外层一路走到最里层，只留 app/ 里的帧。

    task.get_stack() 对挂起的协程只给最外层一帧（线上日志里全是中间件那一层，看不出卡在哪），所以自己走 cr_await。
    """
    frames: list[str] = []
    coro: Any = task.get_coro()
    depth = 0
    while coro is not None and depth < 80:
        depth += 1
        frame = getattr(coro, "cr_frame", None) or getattr(coro, "gi_frame", None) or getattr(coro, "ag_frame", None)
        if frame is not None:
            path = frame.f_code.co_filename.replace("\\", "/")
            if "/app/" in path and not path.endswith("/db_watch.py"):
                frames.append(f"{path.split('/app/')[-1]}:{frame.f_lineno} {frame.f_code.co_name}")
        coro = getattr(coro, "cr_await", None) or getattr(coro, "gi_yieldfrom", None) or getattr(coro, "ag_await", None)
    return " -> ".join(frames) or task.get_name()


async def _scan_forever() -> None:
    while True:
        await asyncio.sleep(SCAN_SECONDS)
        try:
            now = time.monotonic()
            slow = [(key, now - since, task) for key, (since, task, reported) in list(_holders.items()) if not reported and now - since >= HOLD_WARN_SECONDS]
            slow_writers = [(key, now - since, task) for key, (since, task, reported) in list(_writers.items()) if not reported and now - since >= WRITE_WARN_SECONDS]
            for key, held, task in slow_writers:
                where = _app_stack(task) if task is not None and not task.done() else "（任务已结束但事务没提交）"
                logger.warning("db WRITE LOCK held %.0fs (writers: %d): %s", held, len(_writers), where)
                if key in _writers:
                    since, owner, _ = _writers[key]
                    _writers[key] = (since, owner, True)
            for key, held, task in slow:
                where = _app_stack(task) if task is not None and not task.done() else "（借出的任务已结束但连接没还）"
                logger.warning("db connection held %.0fs (pool in use: %d): %s", held, len(_holders), where)
                since, owner, _ = _holders.get(key, (0.0, None, True))
                if key in _holders:
                    _holders[key] = (since, owner, True)
        except Exception:  # noqa: BLE001 —— 监控本身不能影响服务
            logger.debug("db_watch scan failed\n%s", traceback.format_exc())


def _ensure_monitor() -> None:
    global _monitor
    if _monitor is None or _monitor.done():
        try:
            _monitor = asyncio.get_running_loop().create_task(_scan_forever())
        except RuntimeError:
            pass


def install(sync_engine: Any) -> None:
    from sqlalchemy import event

    @event.listens_for(sync_engine, "checkout")
    def _on_checkout(_dbapi_connection, connection_record, _proxy) -> None:  # noqa: ANN001
        _holders[id(connection_record)] = (time.monotonic(), _current_task(), False)
        _ensure_monitor()

    from sqlalchemy.orm import Session

    @event.listens_for(Session, "after_flush")
    def _on_flush(session, _context) -> None:  # noqa: ANN001
        _writers.setdefault(id(session), (time.monotonic(), _current_task(), False))
        _ensure_monitor()

    def _clear(session, *_args) -> None:  # noqa: ANN001
        _writers.pop(id(session), None)

    event.listen(Session, "after_commit", _clear)
    event.listen(Session, "after_rollback", _clear)
    event.listen(Session, "after_soft_rollback", _clear)

    @event.listens_for(sync_engine, "checkin")
    def _on_checkin(_dbapi_connection, connection_record) -> None:  # noqa: ANN001
        _holders.pop(id(connection_record), None)
