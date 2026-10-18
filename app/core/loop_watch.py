"""事件循环卡顿监控与现场转储。

2026-10-18 早上服务两次整体卡死（08:40、10:05）：同一时刻 7 个请求停在事务中途，30 秒后被 PG 的
idle_in_transaction 超时断开；db_watch 一条都没报（它的扫描本身跑在事件循环里），说明整个循环被卡住了，
或者所有请求都在等同一个东西。这里补两样：

1. 卡顿监控：一个普通线程每秒看一眼循环的心跳，超过 STALL_WARN_SECONDS 没跳，就把主线程此刻的完整
   Python 调用栈写进日志 —— 循环被哪段同步代码卡住，一眼就能看到。恢复后再记一行。
2. 现场转储：收到 SIGUSR2 时把所有 asyncio 任务按「停在 app 代码的哪一行」分组计数写进日志，外加连接池状态。
   看门狗重启服务前先给每个 worker 发一次（只发给 worker，别发给 gunicorn 主进程：主进程的 USR2 是重新 exec）。
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import threading
import time
import traceback
from collections import Counter

logger = logging.getLogger("app.loop_watch")

STALL_WARN_SECONDS = 5.0

_last_tick = time.monotonic()
_main_ident: int | None = None
_loop: asyncio.AbstractEventLoop | None = None
_heartbeat_task: asyncio.Task[None] | None = None
_thread: threading.Thread | None = None


async def _heartbeat() -> None:
    global _last_tick
    while True:
        _last_tick = time.monotonic()
        await asyncio.sleep(0.5)


def _main_stack() -> str:
    frame = sys._current_frames().get(_main_ident) if _main_ident is not None else None
    return "".join(traceback.format_stack(frame)) if frame is not None else "(main thread frame unavailable)"


def _watch() -> None:
    reported_at: float | None = None
    while True:
        time.sleep(1.0)
        stalled = time.monotonic() - _last_tick
        if stalled >= STALL_WARN_SECONDS:
            if reported_at is None:
                reported_at = stalled
                logger.warning("event loop STALLED %.0fs (pid %d); main thread stack:\n%s", stalled, os.getpid(), _main_stack())
            elif stalled - reported_at >= 30:
                # 卡得久就每 30 秒再记一次，看是不是一直卡在同一处
                reported_at = stalled
                logger.warning("event loop still STALLED %.0fs (pid %d); main thread stack:\n%s", stalled, os.getpid(), _main_stack())
        elif reported_at is not None:
            logger.warning("event loop resumed after stall of at least %.0fs (pid %d)", reported_at, os.getpid())
            reported_at = None


def dump_tasks(reason: str = "SIGUSR2") -> None:
    from app.core.db_watch import _app_stack

    try:
        tasks = list(asyncio.all_tasks(_loop))
    except RuntimeError:
        tasks = []
    counts = Counter(_app_stack(task) for task in tasks)
    pool = ""
    try:
        from app.core.database import engine

        pool = engine.pool.status()
    except Exception:  # noqa: BLE001
        pass
    lines = "\n".join(f"{count:5d}  {stack}" for stack, count in counts.most_common(40))
    logger.warning("asyncio task dump (%s, pid %d, %d tasks, pool: %s):\n%s", reason, os.getpid(), len(tasks), pool, lines)


def start() -> None:
    """在 lifespan 里（事件循环所在的主线程上）调用。"""
    global _main_ident, _loop, _heartbeat_task, _thread, _last_tick
    _main_ident = threading.get_ident()
    _loop = asyncio.get_running_loop()
    _last_tick = time.monotonic()
    if _heartbeat_task is None or _heartbeat_task.done():
        _heartbeat_task = _loop.create_task(_heartbeat())
    if _thread is None:
        _thread = threading.Thread(target=_watch, name="loop-watch", daemon=True)
        _thread.start()
    try:
        _loop.add_signal_handler(signal.SIGUSR2, dump_tasks)
    except (NotImplementedError, RuntimeError, AttributeError, ValueError):
        # Windows 或非主线程：没有信号，卡顿监控照常
        pass


async def stop() -> None:
    global _heartbeat_task
    task = _heartbeat_task
    _heartbeat_task = None
    if task is not None and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
