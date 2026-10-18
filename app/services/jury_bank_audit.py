"""题库体检（docs/jury-v2.md 2.1.1）：让二审把题库里的每道题看一遍，标出标准答案可能有误的题。

管理员在后台「风纪 › 题库」点一下开始，由接到请求的那个进程在后台一道一道地问二审（最低一档 PRIORITY_AUDIT，
只在没有实时的活时才问，和空闲复查同一个判断）。证据就是委员看到的那份：案件快照里的匿名消息，被举报的那条之前的
上文、之后的下文，被举报人 = 那条消息的作者（moderation_judge.view_context）。

二审概率和标准答案对不上的标「疑似答案有误」（suspect）：标准答案「不违规」但 p ≥ 0.8，或标准答案「违规」但 p ≤ 0.1。
**从不自动改答案**，只给管理员一个筛选和标记，改不改由管理员决定。

进度存在 system_settings 的一行里（几个 worker 进程看到的是同一份）：{status, run_id, total, done, flagged, …}。
跑的进程每做完一道更新一次；它中途退出（发版重启）的话十分钟没动静就显示为「已中断」，可以重新开始。
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import AsyncSessionLocal, release_connection
from app.models.case import ReportCase
from app.models.report import AdminAuditLog
from app.models.system import SystemSetting
from app.services import jury_bank as bank_service
from app.services import jury_settings
from app.services import moderation_judge

logger = logging.getLogger(__name__)
UTC = timezone.utc

STATE_KEY = "jury.bank_audit"
# 跑的进程这么久没更新进度就算中断了
STALE_SECONDS = 600
# 疑似答案有误：标准答案不违规但概率这么高，或者标准答案违规但概率这么低
SUSPECT_CLEAN_P = 0.8
SUSPECT_VIOLATION_P = 0.1
FLAG_OK = "ok"
FLAG_SUSPECT = "suspect"
FLAG_SKIPPED = "skipped"
# 二审连续这么多次没问成就放弃这一轮（每次之间等 RETRY_SECONDS）
MAX_FAILURES = 5
RETRY_SECONDS = 30.0

_tasks: set[asyncio.Task] = set()


def audit_flag(upheld: bool, p: float) -> str:
    if upheld and p <= SUSPECT_VIOLATION_P:
        return FLAG_SUSPECT
    if not upheld and p >= SUSPECT_CLEAN_P:
        return FLAG_SUSPECT
    return FLAG_OK


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


async def _load(db: AsyncSession) -> tuple[dict[str, Any], datetime | None]:
    row = await db.get(SystemSetting, STATE_KEY)
    if row is None or not row.value:
        return {}, None
    try:
        value = json.loads(row.value)
    except ValueError:
        return {}, None
    return (value if isinstance(value, dict) else {}), _aware(row.updated_at)


async def _save(db: AsyncSession, state: dict[str, Any], *, admin_id: int | None = None) -> None:
    now = datetime.now(UTC)
    row = await db.get(SystemSetting, STATE_KEY)
    value = json.dumps(state, ensure_ascii=False, sort_keys=True)
    if row is None:
        db.add(SystemSetting(key=STATE_KEY, value=value, updated_by_id=admin_id, created_at=now, updated_at=now))
    else:
        row.value = value
        row.updated_at = now
        if admin_id is not None:
            row.updated_by_id = admin_id


async def audit_status(db: AsyncSession) -> dict[str, Any]:
    """体检进度：status = idle（没跑过）/ running / done / failed / interrupted，以及 total / done / flagged / skipped。"""
    state, updated_at = await _load(db)
    current = state.get("status") or "idle"
    if current == "running" and (updated_at is None or datetime.now(UTC) - updated_at > timedelta(seconds=STALE_SECONDS)):
        current = "interrupted"
    suspect = await db.execute(select(func.count(ReportCase.id)).where(ReportCase.is_golden.is_(True), ReportCase.bank_audit_flag == FLAG_SUSPECT))
    return {
        "status": current,
        "total": int(state.get("total") or 0),
        "done": int(state.get("done") or 0),
        "flagged": int(state.get("flagged") or 0),
        "skipped": int(state.get("skipped") or 0),
        # 题库里现在标着「疑似答案有误」的（含以前几轮的）
        "suspect_total": int(suspect.scalar_one() or 0),
        "started_at": state.get("started_at"),
        "finished_at": state.get("finished_at"),
        "message": state.get("message"),
    }


async def start_audit(db: AsyncSession, admin_id: int, session_factory: Any = None) -> dict[str, Any]:
    """开始一轮体检（后台跑）。已经在跑返回 409 audit_running；二审不可用返回 409 judge_unavailable。"""
    current = await audit_status(db)
    if current["status"] == "running":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={"code": "audit_running", "message": "题库体检正在进行中"})
    if not moderation_judge.is_active() or not await jury_settings.is_enabled(db, "machine_judge"):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={"code": "judge_unavailable", "message": "机器二审没有开启，不能体检"})
    total = int((await db.execute(select(func.count(ReportCase.id)).where(ReportCase.is_golden.is_(True)))).scalar_one() or 0)
    run_id = uuid.uuid4().hex
    now = datetime.now(UTC)
    await _save(
        db,
        {"status": "running", "run_id": run_id, "total": total, "done": 0, "flagged": 0, "skipped": 0, "started_at": now.isoformat(), "by": admin_id},
        admin_id=admin_id,
    )
    db.add(
        AdminAuditLog(
            actor_id=admin_id,
            action="jury.question.audit",
            target_type="system_setting",
            target_id=None,
            details=json.dumps({"total": total, "run_id": run_id}, ensure_ascii=False, sort_keys=True),
        )
    )
    await db.commit()
    task = asyncio.create_task(run_audit(run_id, session_factory))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return await audit_status(db)


async def _question_input(db: AsyncSession, case: ReportCase) -> tuple[str | None, list[str], list[str]]:
    """一道题送给二审的 (正文, 上文, 下文)：就是委员看到的那份匿名证据。没法判（被举报的不是文字、内容没了）正文为 None。"""
    from app.services import jury as jury_service

    view = await jury_service.juror_case_view(db, case)
    if case.target_type == "message":
        return moderation_judge.view_context(view.get("messages") or [])
    if case.target_type == "post" and isinstance(view.get("post"), dict):
        text = moderation_judge.clean_text(view["post"].get("content"), moderation_judge.TEXT_CHARS)
        return (text or None), [], []
    return None, [], []


async def _still_mine(db: AsyncSession, run_id: str) -> dict[str, Any] | None:
    state, _updated_at = await _load(db)
    return state if state.get("run_id") == run_id and state.get("status") == "running" else None


async def run_audit(run_id: str, session_factory: Any = None) -> None:
    """跑一轮体检。每道题一个短会话；问二审之前把连接还回池子。"""
    from app.services import moderation_idle

    factory = session_factory or AsyncSessionLocal
    try:
        async with factory() as db:
            ids = list((await db.execute(select(ReportCase.id).where(ReportCase.is_golden.is_(True)).order_by(ReportCase.id.asc()))).scalars().all())
        failures = 0
        index = 0
        while index < len(ids):
            # 只在二审闲着时问；等不到就接着等（体检不急）
            if not await moderation_idle._wait_until_idle(factory):
                async with factory() as db:
                    state = await _still_mine(db, run_id)
                    if state is None:
                        return
                    # 心跳：一直在等实时的活让出来，不是中断
                    await _save(db, state)
                    await db.commit()
                continue
            async with factory() as db:
                state = await _still_mine(db, run_id)
                if state is None:
                    return
                case = await db.get(ReportCase, ids[index])
                if case is None or not case.is_golden:
                    index += 1
                    continue
                text, context, after = await _question_input(db, case)
                now = datetime.now(UTC)
                if text is None:
                    case.bank_audit_flag = FLAG_SKIPPED
                    case.bank_audit_at = now
                    state["done"] = int(state.get("done") or 0) + 1
                    state["skipped"] = int(state.get("skipped") or 0) + 1
                    await _save(db, state)
                    await db.commit()
                    index += 1
                    continue
                await release_connection(db)
                verdict = await moderation_judge.judge(text, context, after, priority=moderation_judge.PRIORITY_AUDIT)
                if verdict is None:
                    failures += 1
                    if failures >= MAX_FAILURES:
                        state["status"] = "failed"
                        state["message"] = "二审连续多次不可用，体检已停止，可以稍后重新开始"
                        state["finished_at"] = datetime.now(UTC).isoformat()
                        await _save(db, state)
                        await db.commit()
                        return
                    # 心跳：别让后台把还在等二审的这一轮当成中断了
                    await _save(db, state)
                    await db.commit()
                    await asyncio.sleep(RETRY_SECONDS)
                    continue
                failures = 0
                await db.refresh(case)
                p = round(verdict.p, 4)
                flag = audit_flag(bank_service.question_upheld(case), p)
                case.judge_p = p
                case.bank_audit_flag = flag
                case.bank_audit_at = datetime.now(UTC)
                state = await _still_mine(db, run_id) or state
                state["done"] = int(state.get("done") or 0) + 1
                state["flagged"] = int(state.get("flagged") or 0) + (flag == FLAG_SUSPECT)
                await _save(db, state)
                await db.commit()
                index += 1
        async with factory() as db:
            state = await _still_mine(db, run_id)
            if state is not None:
                state["status"] = "done"
                state["finished_at"] = datetime.now(UTC).isoformat()
                await _save(db, state)
                await db.commit()
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Jury question bank audit failed", extra={"run_id": run_id})
        try:
            async with factory() as db:
                state = await _still_mine(db, run_id)
                if state is not None:
                    state["status"] = "failed"
                    state["message"] = "体检出错停止了，可以重新开始"
                    state["finished_at"] = datetime.now(UTC).isoformat()
                    await _save(db, state)
                    await db.commit()
        except Exception:
            logger.exception("Failed to record the bank audit failure")
