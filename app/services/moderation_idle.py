"""空闲复查（docs/moderation-v2.md 1.11）：二审闲着的时候，把一审放行过的可疑群聊文字再交给二审看一遍。

二审一小时能做 600–1400 次，实时的活（一审标出来的、被举报的）一小时只有二十来次，大部分时间审核机是闲的。
这里在后台慢慢地挑最近几小时里一审放行、但「作者有风险信号」或「一审风险分偏高」的群聊纯文本消息，带上下文问二审；
概率 ≥ moderation_idle_scan_threshold 的建成机器案件交给委员（jury_machine.offer_idle_scan）。

- **从不处罚、从不撤回**：最多只是多一件没有举报人的机器案件，名额用 jury_machine_idle_share 那一份。
- **只在空闲时问**：本进程的二审队列里有实时的活、或者库里有别的进程正在等二审的记录（pending）时先等着；
  放进队列时用最低一档（PRIORITY_IDLE），一次只放一条，实时的活来了排在它前面。
- **只有一个进程在扫**：几个 gunicorn worker 抢 system_settings 里的一行租约（LEASE_KEY），抢到的扫，
  每问一条续一次；那个进程退出后租约过期，别的进程接手。SQLite / PG 都是同一条带条件的 UPDATE。
- **限速**：两次之间至少隔 3600 / moderation_idle_scan_hourly_cap 秒；二审不可用（排不上、超时、关了）时整体暂停
  moderation_idle_scan_backoff_seconds。
- **不重复**：问之前先在审核记录上认领（judge_outcome 从空改成 idle_scan），概率写回同一行；没问成的退回空值，下一轮再来。
- **私聊不碰**：私聊永远不是案源（docs/jury-v2.md 3.3），连二审都不送。

选人的风险信号都只查带索引的列、批量查：新账号（注册不满 7 天）、和被封的设备 / 被封的账号有强 / 中关联（同一设备令牌、
机器码、浏览器指纹）、近 30 天有处罚、近 7 天被举报过、所在的群近 7 天被举报 ≥ 3 次。另外一审风险分 ≥
moderation_idle_scan_min_risk 的不看作者也收。候选先按一审风险分从高到低、再按作者身上的风险信号多少排。
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import socket
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased, joinedload

from app.core.config import settings
from app.core.database import AsyncSessionLocal, release_connection
from app.models.case import ReportCase
from app.models.conversation import Conversation
from app.models.device import DeviceBan, DeviceSignal
from app.models.enums import ConversationType, MessageType, ModerationAction, ModerationSource, UserStatus
from app.models.message import Message
from app.models.penalty import UserViolation
from app.models.report import ContentModerationRecord, UserReport
from app.models.system import SystemSetting
from app.models.user import User
from app.services import jury_machine
from app.services import jury_settings
from app.services import moderation_judge

logger = logging.getLogger(__name__)
UTC = timezone.utc

JUDGE_IDLE_SCAN = "idle_scan"
LEASE_KEY = "moderation.idle_scan.lease"
# 租约多久没续就算过期（比两轮之间的间隔长；一条二审最多 45 秒，每条都会续）
LEASE_SECONDS = 180
# 风险名单每个进程缓存这么久（只有拿到租约的进程会算）
RISK_CACHE_SECONDS = 600
# 每份名单最多取这么多人：再多就是名单本身出了问题，不能让一条查询带上万个参数
RISK_LIST_MAX = 5000
# 风险信号的口径
NEW_ACCOUNT_DAYS = 7
PENALTY_DAYS = 30
REPORT_DAYS = 7
HOT_CONVERSATION_REPORTS = 3
BANNED_LOOKBACK_DAYS = 180
# 强 / 中关联的信号（只靠 IP 的弱关联不算，见 device_signals 的说明）
LINK_KINDS = ("device", "machine", "fp")
# 等实时的活让出来最多等这么久，等不到这一轮先收工
IDLE_WAIT_SECONDS = 60.0
IDLE_POLL_SECONDS = 2.0
# 二审忙的时候下一轮多久后再来
BUSY_RETRY_SECONDS = 15.0

REASON_NEW_ACCOUNT = "新账号"
REASON_LINKED = "关联被封设备 / 账号"
REASON_PENALIZED = "近 30 天有处罚"
REASON_REPORTED = "近 7 天被举报"
REASON_HOT_CONVERSATION = "所在群近 7 天被举报 ≥3 次"

_worker_id = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
_state: dict[str, Any] = {"task": None, "backoff_until": 0.0, "last_call": 0.0, "risk": None, "risk_at": 0.0}


@dataclass
class RiskSets:
    penalized: set[int] = field(default_factory=set)
    reported: set[int] = field(default_factory=set)
    linked: set[int] = field(default_factory=set)
    hot_conversations: set[int] = field(default_factory=set)

    @property
    def users(self) -> set[int]:
        return self.penalized | self.reported | self.linked


@dataclass
class Candidate:
    record_id: int
    message_id: int
    user_id: int
    risk_score: float | None
    reasons: list[str]
    # 作者 / 会话身上的风险信号有几条（排序用；一审风险分不算在内）
    author_risk: int


@dataclass
class RoundResult:
    # disabled / backoff / not_leader / cap_full / empty / busy / unavailable / done
    status: str
    judged: int = 0
    cases: int = 0
    delay: float = 0.0


def reset_state() -> None:
    """测试用：清掉暂停、限速和风险名单缓存。"""
    _state.update(backoff_until=0.0, last_call=0.0, risk=None, risk_at=0.0)


# ---------------------------------------------------------------- 后台循环


def start_idle_scan_worker() -> None:
    if not settings.content_moderation_enabled or not settings.moderation_idle_scan_enabled:
        return
    task = _state["task"]
    if task is not None and not task.done():
        return
    _state["task"] = asyncio.create_task(_loop())


async def stop_idle_scan_worker() -> None:
    task = _state["task"]
    _state["task"] = None
    if task is None or task.done():
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    try:
        async with AsyncSessionLocal() as db:
            await release_lease(db)
    except Exception:
        logger.warning("Failed to release the idle scan lease", exc_info=True)


async def _loop() -> None:
    # 几个 worker 同时启动：错开一点再去抢租约
    await asyncio.sleep(random.uniform(10, 30))
    while True:
        try:
            result = await run_round()
            delay = result.delay
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Idle moderation scan round failed")
            delay = float(settings.moderation_idle_scan_interval_seconds)
        await asyncio.sleep(max(1.0, delay))


# ---------------------------------------------------------------- 租约：同一时刻只有一个进程在扫


async def acquire_lease(db: AsyncSession, owner: str | None = None) -> bool:
    """抢 / 续租约，自己提交。拿到（或本来就是自己的）返回 True。"""
    owner = owner or _worker_id
    now = datetime.now(UTC)
    claimed = await db.execute(
        update(SystemSetting)
        .where(
            SystemSetting.key == LEASE_KEY,
            or_(SystemSetting.value == owner, SystemSetting.value.is_(None), SystemSetting.updated_at < now - timedelta(seconds=LEASE_SECONDS)),
        )
        .values(value=owner, updated_at=now)
    )
    if claimed.rowcount == 1:
        await db.commit()
        return True
    await db.rollback()
    if await db.get(SystemSetting, LEASE_KEY) is not None:
        await db.rollback()
        return False
    db.add(SystemSetting(key=LEASE_KEY, value=owner, created_at=now, updated_at=now))
    try:
        await db.commit()
    except IntegrityError:
        # 另一个进程刚插进去
        await db.rollback()
        return False
    return True


async def release_lease(db: AsyncSession, owner: str | None = None) -> None:
    owner = owner or _worker_id
    await db.execute(update(SystemSetting).where(SystemSetting.key == LEASE_KEY, SystemSetting.value == owner).values(value=None))
    await db.commit()


# ---------------------------------------------------------------- 一轮


def _active() -> bool:
    return settings.moderation_idle_scan_enabled and moderation_judge.is_active()


async def _switches_on(db: AsyncSession) -> bool:
    switches = await jury_settings.get_switches(db)
    # 二审关了不问；machine_review 关着时建不了案件，问了也白问
    return all(switches.get(name) for name in ("machine_idle_scan", "machine_judge", "machine_review"))


async def run_round(session_factory: Any = None) -> RoundResult:
    """扫一轮：最多问 moderation_idle_scan_batch 条。返回这一轮的结果和建议多久之后再来。"""
    factory = session_factory or AsyncSessionLocal
    interval = float(settings.moderation_idle_scan_interval_seconds)
    if not _active():
        return RoundResult("disabled", delay=interval)
    wait = _state["backoff_until"] - time.monotonic()
    if wait > 0:
        return RoundResult("backoff", delay=max(5.0, wait))
    async with factory() as db:
        if not await _switches_on(db):
            return RoundResult("disabled", delay=interval)
        if not await acquire_lease(db):
            return RoundResult("not_leader", delay=interval)
        if not await jury_machine.has_room(db, idle_scan=True):
            return RoundResult("cap_full", delay=interval)
        candidates = await select_candidates(db, datetime.now(UTC))
        await db.commit()
    if not candidates:
        return RoundResult("empty", delay=interval)
    result = RoundResult("done")
    for candidate in candidates:
        if not await _wait_until_idle(factory):
            result.status, result.delay = "busy", BUSY_RETRY_SECONDS
            return result
        await _pace()
        outcome = await _judge_candidate(factory, candidate)
        if outcome == "unavailable":
            _state["backoff_until"] = time.monotonic() + settings.moderation_idle_scan_backoff_seconds
            result.status, result.delay = "unavailable", float(settings.moderation_idle_scan_backoff_seconds)
            return result
        if outcome == "lost_lease":
            result.status, result.delay = "not_leader", interval
            return result
        if outcome in ("judged", "case", "cap_full"):
            result.judged += 1
        if outcome == "case":
            result.cases += 1
        if outcome == "cap_full":
            result.status, result.delay = "cap_full", interval
            return result
    # 一批做满了说明可能还有，马上接着做；不满就歇一会
    result.delay = 1.0 if len(candidates) >= settings.moderation_idle_scan_batch else interval
    return result


async def live_pending_elsewhere(db: AsyncSession) -> bool:
    """库里有没有正在等二审的实时记录（任何进程的）。走部分索引 ix_moderation_records_judge_pending，平时几乎是空的。"""
    since = datetime.now(UTC) - timedelta(seconds=settings.moderation_judge_timeout_seconds + 30)
    row = await db.execute(
        select(ContentModerationRecord.id)
        .where(ContentModerationRecord.judge_outcome == "pending", ContentModerationRecord.created_at >= since)
        .limit(1)
    )
    return row.scalar_one_or_none() is not None


async def _wait_until_idle(factory: Any) -> bool:
    """等到本进程没有实时的二审、库里也没有别的进程在等二审。等太久返回 False。"""
    deadline = time.monotonic() + IDLE_WAIT_SECONDS
    while True:
        if moderation_judge.background_idle():
            async with factory() as db:
                busy = await live_pending_elsewhere(db)
            if not busy:
                return True
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(IDLE_POLL_SECONDS)


async def _pace() -> None:
    """限速：两次之间至少隔 3600 / hourly_cap 秒。"""
    gap = 3600.0 / max(1, settings.moderation_idle_scan_hourly_cap)
    wait = _state["last_call"] + gap - time.monotonic()
    if wait > 0:
        await asyncio.sleep(wait)
    _state["last_call"] = time.monotonic()


async def _judge_candidate(factory: Any, candidate: Candidate) -> str:
    """问一条。返回 judged / case / cap_full / skipped / unavailable / lost_lease。"""
    async with factory() as db:
        if not await acquire_lease(db):
            return "lost_lease"
        claimed = await db.execute(
            update(ContentModerationRecord)
            .where(ContentModerationRecord.id == candidate.record_id, ContentModerationRecord.judge_outcome.is_(None))
            .values(judge_outcome=JUDGE_IDLE_SCAN)
        )
        if claimed.rowcount != 1:
            await db.rollback()
            return "skipped"
        await db.commit()
        record = await db.get(ContentModerationRecord, candidate.record_id)
        message = (
            await db.execute(select(Message).options(joinedload(Message.sender)).where(Message.id == candidate.message_id))
        ).scalar_one_or_none()
        # 选出来之后又撤回了：认领留着（不再看），不问
        if record is None or message is None or message.recalled_at is not None or not (record.content_text or "").strip():
            await db.commit()
            return "skipped"
        context, after = await moderation_judge.message_context(db, message, after=moderation_judge.REPORT_AFTER_MESSAGES)
        await release_connection(db)
        verdict = await moderation_judge.judge(
            moderation_judge.clean_text(record.content_text, moderation_judge.TEXT_CHARS),
            context,
            after,
            priority=moderation_judge.PRIORITY_IDLE,
            risk=record.risk_score,
        )
        if verdict is None:
            # 没问成：退回去，下一轮再来
            await db.execute(
                update(ContentModerationRecord)
                .where(
                    ContentModerationRecord.id == record.id,
                    ContentModerationRecord.judge_outcome == JUDGE_IDLE_SCAN,
                    ContentModerationRecord.judge_p.is_(None),
                )
                .values(judge_outcome=None)
            )
            await db.commit()
            return "unavailable"
        record.judge_p = round(verdict.p, 4)
        record.judge_ms = verdict.latency_ms
        await db.commit()
        if verdict.p < settings.moderation_idle_scan_threshold:
            return "judged"
        await db.refresh(message, attribute_names=["recalled_at"])
        if not await jury_machine.has_room(db, idle_scan=True):
            return "cap_full"
        try:
            case = await jury_machine.offer_idle_scan(db, message, record, candidate.reasons)
        except Exception:
            await db.rollback()
            logger.exception("Failed to open an idle scan case", extra={"message_id": message.id})
            return "judged"
        return "case" if case is not None else "judged"


# ---------------------------------------------------------------- 选消息


async def risk_sets(db: AsyncSession, now: datetime, *, fresh: bool = False) -> RiskSets:
    """各种风险名单（用户 id / 会话 id）。缓存 RISK_CACHE_SECONDS。"""
    cached = _state["risk"]
    if not fresh and cached is not None and time.monotonic() - _state["risk_at"] < RISK_CACHE_SECONDS:
        return cached
    sets = RiskSets()
    # 近 30 天的处罚（撤销的不算）：user_violations.created_at 有索引
    rows = await db.execute(
        select(UserViolation.user_id)
        .where(UserViolation.created_at >= now - timedelta(days=PENALTY_DAYS), UserViolation.status != "revoked")
        .distinct()
        .limit(RISK_LIST_MAX)
    )
    sets.penalized = set(rows.scalars().all())
    # 近 7 天被举报过（不论结果）：user_reports.created_at 有索引
    rows = await db.execute(
        select(UserReport.reported_user_id)
        .where(UserReport.created_at >= now - timedelta(days=REPORT_DAYS), UserReport.reported_user_id.is_not(None))
        .distinct()
        .limit(RISK_LIST_MAX)
    )
    sets.reported = set(rows.scalars().all())
    rows = await db.execute(
        select(UserReport.conversation_id)
        .where(UserReport.created_at >= now - timedelta(days=REPORT_DAYS), UserReport.conversation_id.is_not(None))
        .group_by(UserReport.conversation_id)
        .having(func.count(UserReport.id) >= HOT_CONVERSATION_REPORTS)
        .limit(RISK_LIST_MAX)
    )
    sets.hot_conversations = set(rows.scalars().all())
    sets.linked = await _linked_users(db, now)
    _state["risk"] = sets
    _state["risk_at"] = time.monotonic()
    return sets


async def _linked_users(db: AsyncSession, now: datetime) -> set[int]:
    """和被封的设备、被封的账号有强 / 中关联的账号（口径同 device_signals：同一设备令牌 / 机器码 = 强，同一浏览器指纹 = 中）。"""
    linked: set[int] = set()
    # 用过一个正在生效的设备封禁里的信号的人（ix_device_bans_active + ix_device_signals_kind_value）
    rows = await db.execute(
        select(DeviceSignal.user_id)
        .join(DeviceBan, and_(DeviceBan.kind == DeviceSignal.kind, DeviceBan.value_hash == DeviceSignal.value_hash))
        .where(
            DeviceSignal.kind.in_(LINK_KINDS),
            DeviceBan.lifted_at.is_(None),
            or_(DeviceBan.expires_at.is_(None), DeviceBan.expires_at > now),
        )
        .distinct()
        .limit(RISK_LIST_MAX)
    )
    linked.update(rows.scalars().all())
    # 和最近被封、现在还封着的账号共用过设备 / 机器码 / 指纹的人（users.banned_at 有索引）
    banned = (
        select(User.id)
        .where(
            User.banned_at >= now - timedelta(days=BANNED_LOOKBACK_DAYS),
            User.status == UserStatus.DISABLED,
            or_(User.banned_until.is_(None), User.banned_until > now),
        )
        .scalar_subquery()
    )
    mine, other = aliased(DeviceSignal), aliased(DeviceSignal)
    rows = await db.execute(
        select(other.user_id)
        .select_from(mine)
        .join(other, and_(other.kind == mine.kind, other.value_hash == mine.value_hash, other.user_id != mine.user_id))
        .where(mine.user_id.in_(banned), mine.kind.in_(LINK_KINDS))
        .distinct()
        .limit(RISK_LIST_MAX)
    )
    linked.update(rows.scalars().all())
    return linked


def _base_clauses(now: datetime) -> list[Any]:
    """一审放行、没处理过、没送过二审的最近几小时的群聊纯文本消息。"""
    return [
        ContentModerationRecord.created_at >= now - timedelta(hours=settings.moderation_idle_scan_lookback_hours),
        ContentModerationRecord.created_at <= now - timedelta(seconds=settings.moderation_idle_scan_min_age_seconds),
        ContentModerationRecord.source == ModerationSource.MESSAGE_AUTO,
        ContentModerationRecord.content_type == "text",
        ContentModerationRecord.decision == "allow",
        ContentModerationRecord.action_taken == ModerationAction.NONE,
        ContentModerationRecord.judge_outcome.is_(None),
        ContentModerationRecord.content_text.is_not(None),
        Message.type == MessageType.TEXT,
        Message.recalled_at.is_(None),
        Conversation.type == ConversationType.GROUP,
        Conversation.is_temporary.is_(False),
    ]


def _candidate_query(now: datetime, *extra: Any) -> Any:
    return (
        select(
            ContentModerationRecord.id,
            ContentModerationRecord.message_id,
            Message.sender_id,
            Message.conversation_id,
            ContentModerationRecord.risk_score,
            User.created_at,
        )
        .join(Message, Message.id == ContentModerationRecord.message_id)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .join(User, User.id == Message.sender_id)
        .where(*_base_clauses(now), *extra)
    )


async def select_candidates(db: AsyncSession, now: datetime) -> list[Candidate]:
    """这一轮要复查的消息，最多 moderation_idle_scan_batch 条，按一审风险分、作者风险排好。"""
    batch = settings.moderation_idle_scan_batch
    risk = await risk_sets(db, now)
    new_since = now - timedelta(days=NEW_ACCOUNT_DAYS)
    rows: dict[int, Any] = {}
    # 一审风险分够高的：走 ix_moderation_records_created_risk
    scored = await db.execute(
        _candidate_query(now, ContentModerationRecord.risk_score >= settings.moderation_idle_scan_min_risk)
        .order_by(ContentModerationRecord.risk_score.desc(), ContentModerationRecord.id.asc())
        .limit(batch * 2)
    )
    for row in scored.all():
        rows[row[0]] = row
    # 作者 / 会话有风险信号的
    signals: list[Any] = [User.created_at >= new_since]
    if risk.users:
        signals.append(Message.sender_id.in_(sorted(risk.users)[:RISK_LIST_MAX]))
    if risk.hot_conversations:
        signals.append(Message.conversation_id.in_(sorted(risk.hot_conversations)[:RISK_LIST_MAX]))
    risky = await db.execute(_candidate_query(now, or_(*signals)).order_by(ContentModerationRecord.id.asc()).limit(batch * 4))
    for row in risky.all():
        rows.setdefault(row[0], row)
    if not rows:
        return []
    # 已经在案件里的消息不再看
    message_ids = [row[1] for row in rows.values()]
    in_case: set[int] = set()
    for start in range(0, len(message_ids), 200):
        found = await db.execute(select(ReportCase.message_id).where(ReportCase.message_id.in_(message_ids[start : start + 200])))
        in_case.update(found.scalars().all())
    candidates: list[Candidate] = []
    for record_id, message_id, user_id, conversation_id, risk_score, created_at in rows.values():
        if message_id in in_case:
            continue
        reasons: list[str] = []
        created = created_at.replace(tzinfo=UTC) if created_at is not None and created_at.tzinfo is None else created_at
        if created is not None and created >= new_since:
            reasons.append(REASON_NEW_ACCOUNT)
        if user_id in risk.linked:
            reasons.append(REASON_LINKED)
        if user_id in risk.penalized:
            reasons.append(REASON_PENALIZED)
        if user_id in risk.reported:
            reasons.append(REASON_REPORTED)
        if conversation_id in risk.hot_conversations:
            reasons.append(REASON_HOT_CONVERSATION)
        author_risk = len(reasons)
        if risk_score is not None and risk_score >= settings.moderation_idle_scan_min_risk:
            reasons.append(f"一审风险分 {risk_score:.1f}")
        if not reasons:
            continue
        candidates.append(
            Candidate(record_id=record_id, message_id=message_id, user_id=user_id, risk_score=risk_score, reasons=reasons, author_risk=author_risk)
        )
    candidates.sort(key=lambda item: (-(item.risk_score or 0.0), -item.author_risk, item.record_id))
    return candidates[:batch]
