"""二审：一审（审核服务的规则 + 分类器）标出来的文本，交给审核机上的判定小模型结合上下文再看一遍（docs/moderation-v2.md 1.10）。

审核机只有 CPU，一次 4–6 秒，所以：

- 只有一审标出来的（block / review / 出错，约 2–3%）和被举报的文本消息才会来这里；
- 每个进程一个有界优先队列、一个 worker，同一时刻最多一个请求在路上（审核机那边还有一道全局并发 1）；
- 排队加调用一共最多等 moderation_judge_timeout_seconds。等不到、队列满、服务不可用一律返回 None，
  调用方照一审结论办 —— 二审只会让结论更稳，不能让任何一条审核结论因为它丢掉。

这里只管「问一次、拿到概率」和给待审消息拼上下文；拿到概率之后怎么办在 moderation.py。
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.enums import MessageType
from app.models.message import Message

logger = logging.getLogger(__name__)

# 队列优先级，小的先做：一审 block 的在等着决定罚不罚，最急；其次是一审拿不准的；举报只是调排队先后，最后
PRIORITY_BLOCK = 0
PRIORITY_FLAGGED = 1
PRIORITY_REPORT = 2

# 待审消息作者的称呼，其他人是 成员A / 成员B …。审核服务的提示词里写的就是【被举报人】，改了要两边一起改
AUTHOR_LABEL = "被举报人"
# 被举报的消息额外带的下文条数（实时审核的消息刚发出去，没有下文）
REPORT_AFTER_MESSAGES = 3
# 每条上下文、待审消息最多带多少字（审核服务那边还会把总字数压到 1200 以内）
LINE_CHARS = 120
TEXT_CHARS = 500
# 审核服务明确说二审没开、或者根本连不上时，这么久之内不再去问，直接照一审办（省得每条都白等一次）
UNAVAILABLE_COOLDOWN_SECONDS = 60.0

_PLACEHOLDERS = {
    MessageType.IMAGE: "[图片]",
    MessageType.VOICE: "[语音]",
    MessageType.FORWARD_BUNDLE: "[聊天记录]",
    MessageType.POLL: "[投票]",
    MessageType.RED_PACKET: "[红包]",
    MessageType.GIFT: "[礼物]",
}
# @ 后面跟着的多半是昵称：不能送出去
_MENTION = re.compile(r"@[^\s@]{1,32}")


@dataclass(frozen=True)
class JudgeVerdict:
    p: float
    latency_ms: int | None
    model: str | None


@dataclass(order=True)
class _Job:
    priority: int
    seq: int
    payload: dict[str, Any] = field(compare=False)
    deadline: float = field(compare=False)
    future: asyncio.Future = field(compare=False)


_seq = itertools.count()
_state: dict[str, Any] = {"loop": None, "queue": None, "task": None}
_unavailable_until = 0.0


def is_active() -> bool:
    return settings.moderation_judge_active


def _queue() -> asyncio.PriorityQueue:
    """本进程的队列和 worker。按事件循环懒创建：测试里每个用例一个循环，旧循环上的队列不能接着用。"""
    loop = asyncio.get_running_loop()
    task = _state["task"]
    if _state["loop"] is not loop or task is None or task.done():
        queue: asyncio.PriorityQueue = asyncio.PriorityQueue(maxsize=settings.moderation_judge_queue_size)
        _state.update(loop=loop, queue=queue, task=loop.create_task(_worker(queue)))
    return _state["queue"]


async def stop_worker() -> None:
    task = _state["task"]
    _state.update(loop=None, queue=None, task=None)
    if task is None or task.done():
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def judge(text: str, context: list[str] | None = None, after: list[str] | None = None, *, priority: int) -> JudgeVerdict | None:
    """问一次二审，返回违规概率；不可用（没开、排不上、超时、出错）返回 None。"""
    if not is_active() or time.monotonic() < _unavailable_until:
        return None
    timeout = float(settings.moderation_judge_timeout_seconds)
    loop = asyncio.get_running_loop()
    job = _Job(
        priority=priority,
        seq=next(_seq),
        payload={"text": text, "context": list(context or []), "after": list(after or [])},
        deadline=time.monotonic() + timeout,
        future=loop.create_future(),
    )
    try:
        _queue().put_nowait(job)
    except asyncio.QueueFull:
        logger.warning("Moderation judge queue full; falling back to the first-stage verdict")
        return None
    try:
        # 超时会取消 future，worker 看到已经取消的就跳过，不再白跑一次
        return await asyncio.wait_for(job.future, timeout)
    except asyncio.TimeoutError:
        logger.warning("Moderation judge timed out after %.0fs (including queue wait)", timeout)
        return None


async def _worker(queue: asyncio.PriorityQueue) -> None:
    while True:
        job: _Job = await queue.get()
        try:
            if job.future.done():
                continue
            remaining = job.deadline - time.monotonic()
            verdict: JudgeVerdict | None = None
            if remaining >= 1:
                try:
                    verdict = await asyncio.to_thread(_post_judge, job.payload, remaining)
                except Exception as exc:
                    logger.warning("Moderation judge unavailable: %s", exc)
            if not job.future.done():
                job.future.set_result(verdict)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Moderation judge worker failed on a job")
            if not job.future.done():
                job.future.set_result(None)
        finally:
            queue.task_done()


def _post_judge(payload: dict[str, Any], timeout: float) -> JudgeVerdict:
    """同步调审核服务的 /judge/text（在线程里跑）。和 moderation._post_json 同一个地址和密钥，但出错不发告警邮件：
    二审挂了只是退回一审，不影响审核本身。"""
    global _unavailable_until
    base_url = (settings.content_moderation_url or "").rstrip("/")
    headers = {"Content-Type": "application/json"}
    if settings.content_moderation_token:
        headers["X-Moderation-Token"] = settings.content_moderation_token
    request = Request(f"{base_url}/judge/text", data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), headers=headers, method="POST")
    try:
        with urlopen(request, timeout=max(1.0, timeout)) as response:
            body = response.read().decode("utf-8")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        # 404：审核服务还是没有二审的旧版本；judge_disabled：审核机上关了二审
        if exc.code == 404 or "judge_disabled" in detail:
            _unavailable_until = time.monotonic() + UNAVAILABLE_COOLDOWN_SECONDS
        raise RuntimeError(f"judge HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        if not isinstance(exc.reason, TimeoutError):
            _unavailable_until = time.monotonic() + UNAVAILABLE_COOLDOWN_SECONDS
        raise RuntimeError(f"judge unreachable: {exc.reason}") from exc
    value = json.loads(body)
    p = value.get("p_violation") if isinstance(value, dict) else None
    if isinstance(p, bool) or not isinstance(p, (int, float)) or not 0.0 <= float(p) <= 1.0:
        raise RuntimeError(f"judge returned an invalid probability: {body[:300]}")
    latency = value.get("latency_ms")
    model = value.get("model")
    return JudgeVerdict(
        p=float(p),
        latency_ms=int(latency) if isinstance(latency, (int, float)) and not isinstance(latency, bool) else None,
        model=model if isinstance(model, str) else None,
    )


# ---------------------------------------------------------------- 上下文


def clean_text(text: str | None, limit: int = LINE_CHARS) -> str:
    """一行要送出去的文字：抹掉 @昵称、压掉空白、截断。"""
    value = _MENTION.sub("@某人", text or "")
    value = re.sub(r"\s+", " ", value).strip()
    return value if len(value) <= limit else value[: limit - 1] + "…"


async def message_context(db: AsyncSession, message: Message, *, after: int = 0) -> tuple[list[str], list[str]]:
    """待审消息前 moderation_judge_context_messages 条、后 after 条同会话消息，打好匿名标签（「成员A：……」）。

    只查 (conversation_id, id) 索引上的几行、正文在库里就截断。不带用户名、昵称、ID；撤回的和系统消息不带。
    """
    before = settings.moderation_judge_context_messages
    if before <= 0 and after <= 0:
        return [], []
    columns = (Message.sender_id, Message.type, func.substr(Message.content, 1, LINE_CHARS * 2))

    def scope():
        return select(*columns).where(
            Message.conversation_id == message.conversation_id,
            Message.recalled_at.is_(None),
            Message.type != MessageType.SYSTEM,
        )

    earlier: list[Any] = []
    if before > 0:
        rows = await db.execute(scope().where(Message.id < message.id).order_by(Message.id.desc()).limit(before))
        earlier = list(reversed(rows.all()))
    later: list[Any] = []
    if after > 0:
        rows = await db.execute(scope().where(Message.id > message.id).order_by(Message.id.asc()).limit(after))
        later = list(rows.all())

    labels: dict[int, str] = {message.sender_id: AUTHOR_LABEL}

    def label(sender_id: int) -> str:
        if sender_id not in labels:
            index = len(labels) - 1
            labels[sender_id] = f"成员{chr(ord('A') + index)}" if index < 26 else f"成员{index + 1}"
        return labels[sender_id]

    def line(row: Any) -> str | None:
        sender_id, message_type, content = row
        text = clean_text(content) if message_type == MessageType.TEXT else _PLACEHOLDERS.get(message_type, "[消息]")
        return f"{label(sender_id)}：{text}" if text else None

    return [item for item in map(line, earlier) if item], [item for item in map(line, later) if item]
