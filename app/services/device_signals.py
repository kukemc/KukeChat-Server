"""设备信号的记录与账号关联（docs/device-bans.md 第 2、3 节）。

记录：
- 注册、登录当场写（注册数限制要立刻数得到；被封的号来登录也要记下它这次用的设备）。
- 已登录的请求只在内存里记一笔（note），同一账号的同一个信号每 SEEN_INTERVAL 秒最多记一次，
  后台循环每 FLUSH_INTERVAL 秒用一个事务把攒下的一起写掉。两个 worker 各记各的，最坏每小时各写一次。
  没起后台循环（测试）时攒着不写，测试里手动 flush。

关联强度：同一个设备令牌 / 机器码 = 强（基本就是同一台机器）；同一个浏览器指纹 = 中（同型号手机、
同一批机房电脑可能一样）；同一个 IP = 弱（学校、家庭、移动网络的运营商 NAT 都共用 IP）。
只靠 IP 连起来的账号永远是「弱」，不进关联圈，也不会被自动处理。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.device import DeviceContext, mask
from app.models.device import DeviceSignal
from app.models.user import AuthSession, User

logger = logging.getLogger(__name__)
UTC = timezone.utc

SEEN_INTERVAL = 3600.0
FLUSH_INTERVAL = 30.0
# 内存里最多攒多少条没写的 / 节流表最多记多少个键，超过就丢新的、清旧的（内存紧，宁可少记）
PENDING_MAX = 20000
THROTTLE_MAX = 100000
# 同一个 IP 两边用的时间相差不到这么久，算「前后脚」
CLOSE_IN_TIME = timedelta(minutes=30)
# 关联查询的上限：一个信号被太多账号用过（同一批机房电脑、运营商大 NAT）时只看这么多
MATCH_LIMIT = 2000
CLUSTER_MAX_ACCOUNTS = 50
CLUSTER_MAX_DEPTH = 3

STRENGTH_RANK = {"weak": 1, "medium": 2, "strong": 3}
_KIND_STRENGTH = {"device": "strong", "machine": "strong", "bot_owner": "strong", "fp": "medium", "ip": "weak"}
_KIND_SCORE = {"device": 10, "machine": 10, "bot_owner": 10, "fp": 5, "ip": 1}
_IP_SCORE_CAP = 4

# (user_id, kind, value) -> time.monotonic()
_last_noted: dict[tuple[int, str, str], float] = {}
# (user_id, kind, value) -> (prefix, platform, 时刻, 次数)
_pending: dict[tuple[int, str, str], tuple[str | None, str | None, datetime, int]] = {}
_worker_task: asyncio.Task | None = None


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def reset_runtime_state() -> None:
    """清掉进程内的状态（测试用）。"""
    _last_noted.clear()
    _pending.clear()


# ---------------------------------------------------------------- 记录


def note(user_id: int, context: DeviceContext | None) -> None:
    """已登录请求上记一笔。纯内存操作，不碰数据库。"""
    if context is None:
        return
    now_mono = time.monotonic()
    wall = None
    for kind, value in context.signals():
        key = (user_id, kind, value)
        last = _last_noted.get(key)
        if last is not None and now_mono - last < SEEN_INTERVAL:
            continue
        if len(_pending) >= PENDING_MAX:
            return
        _last_noted[key] = now_mono
        wall = wall or _now()
        previous = _pending.get(key)
        count = previous[3] + 1 if previous else 1
        _pending[key] = (context.prefix if kind == "ip" else None, context.platform, wall, count)
    if len(_last_noted) > THROTTLE_MAX:
        cutoff = now_mono - SEEN_INTERVAL
        for key in [key for key, at in _last_noted.items() if at < cutoff]:
            del _last_noted[key]


async def _upsert(
    db: AsyncSession,
    user_id: int,
    kind: str,
    value: str,
    prefix: str | None,
    platform: str | None,
    at: datetime,
    count: int = 1,
    registered: bool = False,
) -> None:
    values: dict[str, Any] = {"last_seen_at": at, "seen_count": DeviceSignal.seen_count + count}
    if platform:
        values["platform"] = platform
    if registered:
        values["registered_here"] = True
    result = await db.execute(
        update(DeviceSignal)
        .where(DeviceSignal.user_id == user_id, DeviceSignal.kind == kind, DeviceSignal.value_hash == value)
        .values(**values)
    )
    if result.rowcount:
        return
    try:
        async with db.begin_nested():
            db.add(
                DeviceSignal(
                    user_id=user_id,
                    kind=kind,
                    value_hash=value,
                    prefix=prefix,
                    platform=platform,
                    registered_here=registered,
                    first_seen_at=at,
                    last_seen_at=at,
                    seen_count=count,
                )
            )
    except IntegrityError:
        # 另一个 worker 同时插了同一行：当成它已经记下了
        pass


async def record_now(db: AsyncSession, user_id: int, context: DeviceContext | None, *, registered: bool = False) -> None:
    """注册 / 登录当场写（不提交，由调用方 commit）。顺带刷新节流表，接下来一小时内的请求不再重复记。"""
    if context is None:
        return
    at = _now()
    now_mono = time.monotonic()
    for kind, value in context.signals():
        await _upsert(db, user_id, kind, value, context.prefix if kind == "ip" else None, context.platform, at, registered=registered)
        _last_noted[(user_id, kind, value)] = now_mono
        _pending.pop((user_id, kind, value), None)


async def flush(session_factory: async_sessionmaker | None = None) -> int:
    """把内存里攒下的写进库，返回写了几条。失败的放回去下次再试。"""
    if not _pending:
        return 0
    if session_factory is None:
        from app.core.database import AsyncSessionLocal

        session_factory = AsyncSessionLocal
    batch = dict(_pending)
    _pending.clear()
    try:
        async with session_factory() as db:
            # 按键排序：两个 worker 同时写同几行时加锁顺序一致
            for (user_id, kind, value), (prefix, platform, at, count) in sorted(batch.items(), key=lambda item: item[0]):
                await _upsert(db, user_id, kind, value, prefix, platform, at, count)
            await db.commit()
    except Exception:
        logger.exception("device signals: flush of %d rows failed, will retry", len(batch))
        for key, item in batch.items():
            if len(_pending) < PENDING_MAX:
                _pending.setdefault(key, item)
        return 0
    return len(batch)


async def _worker() -> None:
    while True:
        await asyncio.sleep(FLUSH_INTERVAL)
        try:
            await flush()
        except Exception:  # pragma: no cover - flush 自己已经兜住
            logger.exception("device signals worker iteration failed")


def start_device_signal_worker() -> None:
    global _worker_task
    if _worker_task is None or _worker_task.done():
        _worker_task = asyncio.create_task(_worker())


async def stop_device_signal_worker() -> None:
    global _worker_task
    task, _worker_task = _worker_task, None
    if task is not None:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    await flush()


# ---------------------------------------------------------------- 关联


@dataclass
class Link:
    user_id: int
    # [{kind, masked, detail}]；ip 的 detail 非空表示两边是前后脚用的
    reasons: list[dict[str, Any]] = field(default_factory=list)

    def add(self, kind: str, masked: str | None, detail: str | None = None) -> None:
        for reason in self.reasons:
            if reason["kind"] == kind and reason["masked"] == masked:
                if detail and not reason["detail"]:
                    reason["detail"] = detail
                return
        self.reasons.append({"kind": kind, "masked": masked, "detail": detail})

    @property
    def kinds(self) -> set[str]:
        return {reason["kind"] for reason in self.reasons}

    @property
    def strength(self) -> str:
        best = max((STRENGTH_RANK[_KIND_STRENGTH[kind]] for kind in self.kinds), default=1)
        return next(name for name, rank in STRENGTH_RANK.items() if rank == best)

    @property
    def score(self) -> int:
        ip_score = sum(3 if reason["detail"] else 1 for reason in self.reasons if reason["kind"] == "ip")
        return sum(_KIND_SCORE[kind] for kind in self.kinds if kind != "ip") + min(_IP_SCORE_CAP, ip_score)


@dataclass
class UserSignals:
    rows: list[DeviceSignal]
    # (kind, value) -> 还有几个别的账号用过
    shared: dict[tuple[str, str], int]
    links: dict[int, Link]


def _close(left: list[datetime], right: list[datetime]) -> bool:
    return any(abs(a - b) <= CLOSE_IN_TIME for a in left for b in right)


async def _root_ips(db: AsyncSession, user: User, rows: list[DeviceSignal]) -> dict[str, list[datetime]]:
    """这个账号用过的 IP -> 用的时刻。除了信号表，也看 users 表上的老字段和会话表（上线前的老数据只有这些）。"""
    ips: dict[str, list[datetime]] = {}

    def put(ip: str | None, at: datetime | None) -> None:
        if not ip or ip == "unknown":
            return
        times = ips.setdefault(ip, [])
        if at is not None:
            times.append(_aware(at))

    for row in rows:
        if row.kind == "ip":
            put(row.value_hash, row.last_seen_at)
            put(row.value_hash, row.first_seen_at)
    put(user.registration_ip, user.created_at)
    put(user.last_login_ip, user.last_login_at)
    put(user.last_seen_ip, user.last_seen_at)
    sessions = await db.execute(
        select(AuthSession.client_ip, AuthSession.created_at, AuthSession.last_used_at).where(AuthSession.user_id == user.id).limit(50)
    )
    for ip, created_at, last_used_at in sessions.all():
        put(ip, created_at)
        put(ip, last_used_at)
    return ips


async def _ip_matches(db: AsyncSession, user_id: int, ips: dict[str, list[datetime]]) -> dict[int, dict[str, list[datetime]]]:
    """别的账号 -> {共用的 IP -> 它用的时刻}。"""
    if not ips:
        return {}
    values = list(ips)[:100]
    found: dict[int, dict[str, list[datetime]]] = {}

    def put(other: int, ip: str | None, at: datetime | None) -> None:
        if other == user_id or ip not in ips:
            return
        times = found.setdefault(other, {}).setdefault(ip, [])
        if at is not None:
            times.append(_aware(at))

    rows = await db.execute(
        select(DeviceSignal.user_id, DeviceSignal.value_hash, DeviceSignal.first_seen_at, DeviceSignal.last_seen_at)
        .where(DeviceSignal.kind == "ip", DeviceSignal.value_hash.in_(values), DeviceSignal.user_id != user_id)
        .limit(MATCH_LIMIT)
    )
    for other, ip, first_at, last_at in rows.all():
        put(other, ip, first_at)
        put(other, ip, last_at)
    users = await db.execute(
        select(User.id, User.registration_ip, User.created_at, User.last_login_ip, User.last_login_at, User.last_seen_ip, User.last_seen_at)
        .where(
            User.id != user_id,
            or_(User.registration_ip.in_(values), User.last_login_ip.in_(values), User.last_seen_ip.in_(values)),
        )
        .limit(MATCH_LIMIT)
    )
    for other, reg_ip, created_at, login_ip, login_at, seen_ip, seen_at in users.all():
        put(other, reg_ip, created_at)
        put(other, login_ip, login_at)
        put(other, seen_ip, seen_at)
    sessions = await db.execute(
        select(AuthSession.user_id, AuthSession.client_ip, AuthSession.created_at, AuthSession.last_used_at)
        .where(AuthSession.client_ip.in_(values), AuthSession.user_id != user_id)
        .limit(MATCH_LIMIT)
    )
    for other, ip, created_at, last_used_at in sessions.all():
        put(other, ip, created_at)
        put(other, ip, last_used_at)
    return found


async def user_signals(db: AsyncSession, user: User, *, include_weak: bool = True) -> UserSignals:
    """一个账号的全部信号，以及和它直接相连的账号（include_weak=False 时不查 IP）。"""
    rows = list(
        (
            await db.execute(
                select(DeviceSignal).where(DeviceSignal.user_id == user.id).order_by(DeviceSignal.last_seen_at.desc()).limit(500)
            )
        ).scalars()
    )
    shared: dict[tuple[str, str], set[int]] = {}
    links: dict[int, Link] = {}

    def link(other: int) -> Link:
        return links.setdefault(other, Link(other))

    strong_values = [(row.kind, row.value_hash) for row in rows if row.kind != "ip"]
    if strong_values:
        conditions = []
        for kind in ("device", "machine", "fp"):
            values = [value for row_kind, value in strong_values if row_kind == kind]
            if values:
                conditions.append(and_(DeviceSignal.kind == kind, DeviceSignal.value_hash.in_(values)))
        matches = await db.execute(
            select(DeviceSignal.user_id, DeviceSignal.kind, DeviceSignal.value_hash)
            .where(or_(*conditions), DeviceSignal.user_id != user.id)
            .limit(MATCH_LIMIT)
        )
        for other, kind, value in matches.all():
            shared.setdefault((kind, value), set()).add(other)
            link(other).add(kind, mask(kind, value))

    # 机器人和它的创建者：本来就是同一个人管着的
    if user.bot_owner_id:
        link(user.bot_owner_id).add("bot_owner", None, "这个机器人的创建者")
    bots = await db.execute(select(User.id).where(User.bot_owner_id == user.id).limit(100))
    for (bot_id,) in bots.all():
        link(bot_id).add("bot_owner", None, "TA 创建的机器人")

    if include_weak:
        ips = await _root_ips(db, user, rows)
        for other, used in (await _ip_matches(db, user.id, ips)).items():
            for ip, times in used.items():
                shared.setdefault(("ip", ip), set()).add(other)
                close = _close(ips.get(ip, []), times)
                link(other).add("ip", ip, "前后 30 分钟内都用过这个 IP" if close else None)

    return UserSignals(rows=rows, shared={key: len(value) for key, value in shared.items()}, links=links)


def sorted_links(links: dict[int, Link]) -> list[Link]:
    return sorted(links.values(), key=lambda item: (-STRENGTH_RANK[item.strength], -item.score, item.user_id))


async def cluster(db: AsyncSession, root: User) -> tuple[dict[int, tuple[str, Link]], bool]:
    """从 root 出发，只走强 / 中关联能连到的账号。返回 {账号: (相对 root 的强度 = 路径上最弱的一段, 发现它的那条关联)} 和是否截断。"""
    found: dict[int, tuple[str, Link]] = {}
    frontier: list[tuple[User, str]] = [(root, "strong")]
    visited = {root.id}
    truncated = False
    for _depth in range(CLUSTER_MAX_DEPTH):
        next_frontier: list[tuple[User, str]] = []
        for current, path_strength in frontier:
            signals = await user_signals(db, current, include_weak=False)
            for item in sorted_links(signals.links):
                if item.user_id in visited:
                    continue
                strength = min(path_strength, item.strength, key=lambda name: STRENGTH_RANK[name])
                if len(found) >= CLUSTER_MAX_ACCOUNTS:
                    truncated = True
                    break
                visited.add(item.user_id)
                found[item.user_id] = (strength, item)
                other = await db.get(User, item.user_id)
                if other is not None:
                    next_frontier.append((other, strength))
        frontier = next_frontier
        if not frontier or truncated:
            break
    else:
        # 走满了层数，最外一圈还没展开
        truncated = bool(frontier)
    return found, truncated


async def shared_account_counts(db: AsyncSession, keys: list[tuple[str, str]]) -> dict[tuple[str, str], int]:
    """(kind, value) -> 用过它的账号数。ip 的 value 可以是网段（112.39.99.0/24），按网段数。"""
    counts: dict[tuple[str, str], int] = {}
    for kind, value in keys:
        if kind == "ip" and "/" in value:
            query = select(DeviceSignal.user_id).where(DeviceSignal.kind == "ip", DeviceSignal.prefix == value)
        else:
            query = select(DeviceSignal.user_id).where(DeviceSignal.kind == kind, DeviceSignal.value_hash == value)
        rows = await db.execute(query.distinct().limit(MATCH_LIMIT))
        counts[(kind, value)] = len(rows.all())
    return counts


async def users_on_signals(db: AsyncSession, keys: list[tuple[str, str]], *, since: datetime | None = None) -> set[int]:
    """最近用过这些信号的账号（新封禁生效时把他们的实时连接断掉）。"""
    user_ids: set[int] = set()
    for kind, value in keys:
        if kind == "ip" and "/" in value:
            query = select(DeviceSignal.user_id).where(DeviceSignal.kind == "ip", DeviceSignal.prefix == value)
        else:
            query = select(DeviceSignal.user_id).where(DeviceSignal.kind == kind, DeviceSignal.value_hash == value)
        if since is not None:
            query = query.where(DeviceSignal.last_seen_at >= since)
        rows = await db.execute(query.distinct().limit(MATCH_LIMIT))
        user_ids.update(user_id for (user_id,) in rows.all())
    return user_ids
