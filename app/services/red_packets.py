"""红包（docs/red-packets.md）：用户拿自己的金币余额发红包，会话里的人来抢。币只在用户之间流动，总量不变。

钱怎么记（表在 app/models/red_packet.py）：

- 发的时候把整包金额一次分成 count 份、存成 red_packet_shares 的行；领取 = 把其中一份记到某个人名下。
- 余额公式里红包的三项都是对这两张表求和（services/shop.py 的 _balance / available_expression），没有另外的余额列，
  所以不存在「余额改了、流水没写」这种对不上的中间状态。
- 结束（到期、消息被撤回、后台撤销）时把没被领走的那些份的金额之和写进 red_packets.refunded_amount，这就是退回。

并发（线上两个 worker 进程，SQLite 的写事务全局串行）：每个写事务的**第一条**语句就是带条件的那条 UPDATE，
事务从这条语句起拿着数据库的写锁直到提交，条件是在拿到写锁之后、对着最新的数据算的。

- 发：`UPDATE shop_accounts ... WHERE 余额 >= 整包金额`。影响 0 行就是余额不够，整个事务回滚；
  影响 1 行才接着写红包、每一份、会话里的那条消息，一起提交。和商店购买用的是同一个余额表达式、同一种扣法，
  所以同一个人同时发两个红包、或者一边发一边买，后一个事务看到的一定是前一个提交之后的余额。
- 抢：`UPDATE red_packet_shares SET user_id = 我 WHERE id = (这个红包里还没人领的第一份) AND user_id IS NULL AND 红包还在进行中 AND 消息没被撤回`。
  一份只可能被一条这样的语句拿到；(packet_id, user_id) 的唯一约束保证一个人拿不到第二份。
- 结束：`UPDATE red_packets SET status = ..., refunded_amount = (没领的那些份之和) WHERE id = ? AND status = 'active'`。
  只有一个事务能把它从 active 改走，退回的数是在同一条语句里算的；之后「红包还在进行中」不成立，再也领不走。
  定时清扫、领取时的现场判断、撤回消息、后台撤销都走这一条，谁先到谁做，其余的影响 0 行什么都不发生。

和商店一样，写事务都先过 shop._write_lock() 再进 SQLite（原因见那里的说明）；向 CCW 核实余额、文本审核这些要等外部的事都在拿锁之前做完。
"""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import and_, case, exists, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import AsyncSessionLocal, release_connection
from app.core.dialect import lock_for_update
from app.integrations import get_coin_ledger
from app.models.conversation import Conversation, ConversationMember
from app.models.enums import ConversationType, MemberRole, MessageType
from app.models.message import Message
from app.models.red_packet import RedPacket, RedPacketShare
from app.models.shop import ShopAccount
from app.models.system import SystemSetting
from app.models.user import User
from app.schemas.user import UserPublic
from app.services import shop as shop_service
from app.services.penalties import mute_state
from app.services.reports import log_admin_action

logger = logging.getLogger(__name__)
UTC = timezone.utc
LOCAL_TZ = timezone(timedelta(hours=8))

KINDS = ("normal", "lucky")
STATUSES = ("active", "finished", "expired", "revoked")
DEFAULT_GREETING = "恭喜发财"
GREETING_MAX = 30
# 接龙口令最长几个字
CHAIN_TEXT_MAX = 30
# GET /red-packets/config 的 features：服务端支持的玩法，客户端据此决定显示不显示对应的选项
FEATURES = ("chain", "exclusive")
REASON_MAX = 500
STAFF_ROLES = {MemberRole.OWNER, MemberRole.ADMIN}

UPDATED_EVENT = "red_packet.updated"
# 同一个红包的 red_packet.updated 合并推送：这段时间里不管被领了几份只推一次最新的状态。结束（到期、撤销）立刻推
UPDATE_EVENT_DELAY_SECONDS = 0.3
# 定时清扫一轮最多处理多少个，剩下的下一轮接着来
SWEEP_BATCH = 500
# 领取那条语句抢不到行、但红包明明还有剩时重试几次（SQLite 的写事务串行，走不到这里；留给以后换数据库）
CLAIM_ATTEMPTS = 3

# —— 开关与限制：存在 system_settings 表里，后台可以随时改 ——
DEFAULTS: dict[str, Any] = {"enabled": False, "max_total": 500, "max_count": 100, "min_each": 1, "daily_send_limit": 20, "expire_hours": 24}
LIMITS: tuple[str, ...] = ("max_total", "max_count", "min_each", "daily_send_limit", "expire_hours")
# 每一项允许设成多少。max_count 封顶 500：每一份是一行，详情里也会整个列出来
BOUNDS: dict[str, tuple[int, int]] = {
    "max_total": (1, 1_000_000),
    "max_count": (1, 500),
    "min_each": (1, 1_000_000),
    "daily_send_limit": (1, 1000),
    "expire_hours": (1, 168),
}
_KEY_PREFIX = "red_packet."
# 服务跑两个 worker 进程，另一个进程改了设置后最多这么久才能看到
CACHE_SECONDS = 5.0

_cache: dict[str, Any] = {"at": 0.0, "values": None}
_pending_updates: set[int] = set()
# 见 pending_conversation_ids：「全站现在有没有进行中的红包」在进程里记几秒 —— (查的时刻 time.monotonic(), 有没有)
LIVE_CACHE_SECONDS = 3.0
_live: tuple[float, bool] | None = None
_worker_task: asyncio.Task[None] | None = None
WORKER_STARTUP_DELAY_SECONDS = 7.0


def reset_cache() -> None:
    _cache["at"] = 0.0
    _cache["values"] = None


def reset_runtime_state() -> None:
    """清掉进程内的状态（测试用）。"""
    global _live
    reset_cache()
    _pending_updates.clear()
    _live = None


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(value: datetime | None) -> datetime | None:
    """SQLite 读出来的时间不带时区，统一当 UTC。"""
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _error(status_code: int, code: str, message: str, **extra: Any) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message, **extra})


def _disabled() -> HTTPException:
    return _error(status.HTTP_403_FORBIDDEN, "disabled", "红包功能还没有开放")


def _not_member() -> HTTPException:
    return _error(status.HTTP_403_FORBIDDEN, "not_member", "你不在这个会话里")


def _not_found() -> HTTPException:
    return _error(status.HTTP_404_NOT_FOUND, "not_found", "红包不存在")


def _gone(code: str) -> HTTPException:
    messages = {"finished": "手慢了，红包已经被抢完", "expired": "红包已经过期", "revoked": "红包已经被撤回"}
    return _error(status.HTTP_410_GONE, code, messages[code])


def _day_start(now: datetime) -> datetime:
    """今天（UTC+8 的自然日）零点，换成 UTC。库里的时间都是按 UTC 存的。"""
    local = now.astimezone(LOCAL_TZ)
    return datetime(local.year, local.month, local.day, tzinfo=LOCAL_TZ).astimezone(UTC)


# ---------------------------------------------------------------- 开关与限制


def _parse_setting(name: str, raw: str | None) -> Any:
    if name == "enabled":
        return raw == "1"
    try:
        value = int(raw) if raw is not None else DEFAULTS[name]
    except ValueError:
        return DEFAULTS[name]
    low, high = BOUNDS[name]
    return value if low <= value <= high else DEFAULTS[name]


async def get_settings(db: AsyncSession, *, fresh: bool = False) -> dict[str, Any]:
    """当前的开关与限制，没设置过的用默认值（enabled 默认关）。"""
    now = time.monotonic()
    cached = _cache["values"]
    if not fresh and cached is not None and now - _cache["at"] < CACHE_SECONDS:
        return dict(cached)
    rows = await db.execute(select(SystemSetting.key, SystemSetting.value).where(SystemSetting.key.like(f"{_KEY_PREFIX}%")))
    stored = {key[len(_KEY_PREFIX) :]: value for key, value in rows.all()}
    values = {name: _parse_setting(name, stored.get(name)) if name in stored else DEFAULTS[name] for name in DEFAULTS}
    _cache["at"] = now
    _cache["values"] = values
    return dict(values)


def _limits(config: dict[str, Any]) -> dict[str, int]:
    return {name: config[name] for name in LIMITS}


async def update_settings(db: AsyncSession, admin: User, changes: dict[str, Any]) -> dict[str, Any]:
    """PUT /admin/red-packets/settings：只处理传了的项，改动写审计日志。返回改完之后的全部设置。"""
    admin_id = admin.id
    before = await get_settings(db, fresh=True)
    cleaned: dict[str, Any] = {}
    for name, value in changes.items():
        if name not in DEFAULTS or value is None:
            continue
        if name == "enabled":
            if not isinstance(value, bool):
                raise _error(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_settings", "enabled 只能是 true / false", field=name)
            cleaned[name] = value
            continue
        low, high = BOUNDS[name]
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise _error(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_settings", f"{name} 要在 {low}–{high} 之间", field=name)
        cleaned[name] = value
    after = {**before, **cleaned}
    if after["min_each"] > after["max_total"]:
        raise _error(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_settings", "min_each 不能比 max_total 大", field="min_each")
    changed = {name: value for name, value in cleaned.items() if value != before[name]}
    for name, value in changed.items():
        text = ("1" if value else "0") if name == "enabled" else str(value)
        item = await db.get(SystemSetting, f"{_KEY_PREFIX}{name}")
        if item is None:
            db.add(SystemSetting(key=f"{_KEY_PREFIX}{name}", value=text, updated_by_id=admin_id))
        else:
            item.value = text
            item.updated_by_id = admin_id
    if changed:
        await log_admin_action(
            db, admin_id, "red_packet.settings.update", "system_setting", None, {name: {"old": before[name], "new": value} for name, value in changed.items()}
        )
        async with shop_service._write_lock():
            await db.commit()
    reset_cache()
    return await get_settings(db, fresh=True)


async def _sent_today(db: AsyncSession, user_id: int, now: datetime) -> int:
    """今天（UTC+8）发了几个。被撤回、被撤销的也算，不然可以靠反复撤回刷屏。"""
    count = await db.scalar(select(func.count()).select_from(RedPacket).where(RedPacket.sender_id == user_id, RedPacket.created_at >= _day_start(now)))
    return int(count or 0)


async def config_view(db: AsyncSession, user: User) -> dict[str, Any]:
    """GET /red-packets/config。"""
    config = await get_settings(db)
    return {"enabled": config["enabled"], "limits": _limits(config), "sent_today": await _sent_today(db, user.id, _now()), "features": list(FEATURES)}


# ---------------------------------------------------------------- 金额分配


def split_lucky(total: int, count: int, min_each: int = 1) -> list[int]:
    """拼手气：把 total 分成 count 份，每份不少于 min_each，加起来正好是 total。

    二倍均值法：轮到某一份时，它的金额在 [min_each, 剩余人均的两倍 − min_each] 里均匀取，所以每一份的期望都是人均；
    最后一份拿剩下的全部。做法是先给每份留出 min_each，再对超出的部分做二倍均值，这样下限天然成立。
    随机数来自系统的密码学随机源（secrets），分完再整个打乱一次，领取顺序和金额大小无关。
    """
    if count < 1 or min_each < 1 or total < min_each * count:
        raise ValueError("total is too small for this many shares")
    surplus = total - min_each * count
    amounts: list[int] = []
    for remaining in range(count, 1, -1):
        extra = secrets.randbelow(2 * surplus // remaining + 1)
        amounts.append(min_each + extra)
        surplus -= extra
    amounts.append(min_each + surplus)
    secrets.SystemRandom().shuffle(amounts)
    return amounts


def split(kind: str, amount: int, count: int, min_each: int) -> list[int]:
    """每一份的金额。normal：amount 是单个金额；lucky：amount 是总额。"""
    return [amount] * count if kind == "normal" else split_lucky(amount, count, min_each)


# ---------------------------------------------------------------- 展示


def fallback_text(greeting: str, *, chain: bool = False, target_name: str | None = None) -> str:
    """消息正文：给不认识 red_packet 类型的旧客户端、搜索、会话列表和通知预览用。"""
    exclusive = target_name is not None
    label = "[专属接龙红包]" if chain and exclusive else "[专属红包]" if exclusive else "[接龙红包]" if chain else "[红包]"
    head = f"{label} 给 {target_name}：" if exclusive else f"{label} "
    return f"{head}{greeting}（更新到新版才能领取）"


def effective_status(packet: RedPacket, now: datetime) -> str:
    """现在的状态。过了期但清扫还没轮到的，对外已经是 expired（领不了了，钱稍后退回）。"""
    if packet.status == "active" and _aware(packet.expires_at) <= now:
        return "expired"
    return packet.status


def _luckiest(packet: RedPacket, claimed: list[RedPacketShare]) -> int | None:
    """手气最佳的那一份的 id：只有拼手气、而且抢完了才标，金额一样大时算先抢到的。"""
    if packet.kind != "lucky" or packet.status != "finished" or not claimed:
        return None
    return max(claimed, key=lambda share: (share.amount, -share.seq)).id


async def _users(db: AsyncSession, user_ids: set[int]) -> dict[int, User]:
    if not user_ids:
        return {}
    rows = await db.execute(select(User).where(User.id.in_(user_ids)))
    return {user.id: user for user in rows.scalars().all()}


def _public(user: User | None) -> UserPublic | None:
    return UserPublic.model_validate(user) if user is not None else None


async def _claimed_shares(db: AsyncSession, packet_id: int) -> list[RedPacketShare]:
    rows = await db.execute(
        select(RedPacketShare).where(RedPacketShare.packet_id == packet_id, RedPacketShare.user_id.is_not(None)).order_by(RedPacketShare.seq.asc())
    )
    return list(rows.scalars().all())


def _people(packet: RedPacket) -> set[int]:
    """展示这个红包要用到的人：发的人，专属红包还有目标。"""
    return {packet.sender_id} | ({packet.target_user_id} if packet.target_user_id is not None else set())


def _base_view(packet: RedPacket, users: dict[int, User], mine: RedPacketShare | None, viewer_id: int, now: datetime) -> dict[str, Any]:
    current = effective_status(packet, now)
    # 金额只给已经领了的人、发的人看；红包结束之后所有成员都能看
    open_book = mine is not None or packet.sender_id == viewer_id or current != "active"
    target_id = packet.target_user_id
    return {
        "id": packet.id,
        "kind": packet.kind,
        "greeting": packet.greeting,
        "sender": _public(users.get(packet.sender_id)),
        "chain_text": packet.chain_text,
        "target_user_id": target_id,
        "target": _public(users.get(target_id)) if target_id is not None else None,
        "conversation_id": packet.conversation_id,
        "message_id": packet.message_id,
        "total": packet.total if open_book else None,
        "count": packet.count,
        "claimed_count": packet.claimed_count,
        "claimed_amount": packet.claimed_amount if open_book else None,
        "refunded_amount": packet.refunded_amount if open_book else None,
        "status": current,
        "my_claim": {"amount": mine.amount, "created_at": _aware(mine.claimed_at)} if mine is not None else None,
        "expires_at": _aware(packet.expires_at),
        "created_at": _aware(packet.created_at),
        "finished_at": _aware(packet.finished_at) or (_aware(packet.expires_at) if current == "expired" else None),
        "claims": None,
    }


async def _view(db: AsyncSession, packet: RedPacket, viewer_id: int, now: datetime) -> dict[str, Any]:
    """契约里的红包对象（带领取明细）。没抢的人在红包还进行着的时候看不到任何金额。"""
    claimed = await _claimed_shares(db, packet.id)
    mine = next((share for share in claimed if share.user_id == viewer_id), None)
    users = await _users(db, _people(packet) | {share.user_id for share in claimed})
    view = _base_view(packet, users, mine, viewer_id, now)
    if view["total"] is not None:
        luckiest = _luckiest(packet, claimed)
        view["claims"] = [
            {"user": _public(users.get(share.user_id)), "amount": share.amount, "created_at": _aware(share.claimed_at), "is_luckiest": share.id == luckiest}
            for share in claimed
            if share.user_id in users
        ]
    return view


async def _load(db: AsyncSession, packet_id: int) -> RedPacket:
    packet = await db.get(RedPacket, packet_id, populate_existing=True)
    if packet is None:
        raise _not_found()
    return packet


async def _membership(db: AsyncSession, conversation_id: int, user_id: int) -> ConversationMember:
    member = (
        await db.execute(select(ConversationMember).where(ConversationMember.conversation_id == conversation_id, ConversationMember.user_id == user_id))
    ).scalar_one_or_none()
    if member is None:
        raise _not_member()
    return member


def _ensure_not_muted(user: User) -> None:
    """被平台禁言的人不能发也不能领（禁言期间不参与红包）。"""
    until = mute_state(user)
    if until is not None:
        raise _error(status.HTTP_403_FORBIDDEN, "muted", "你正在被禁言，暂时不能发红包或领红包", muted_until=until.isoformat())


# ---------------------------------------------------------------- 会话列表上的「[有红包]」


async def pending_conversation_ids(db: AsyncSession, user_id: int, *, now: datetime | None = None) -> set[int]:
    """哪些会话里有这个人还能领的红包（会话对象的 red_packet_pending）。会话列表是热路径，所以整个列表只查这一条。

    「还能领」= 状态 active、没过期（现场按 expires_at 判断，不依赖清扫）、这个人没领过、不是「私聊里自己发的」、
    不是「给别人的专属红包」。群里自己发的、自己还没抢的算（专属红包除外：目标不会是自己）。
    专属这一条只看红包行自己的 target_user_id，不多查任何东西。

    查询是从「进行中的红包」出发的，不是从这个人的会话出发：进行中的红包全站也就那么些（24 小时就过期），
    走 (status, expires_at) 索引取出来，每个再做两次唯一索引上的点查 —— 这个人在不在那个会话里
    （conversation_members 的 (conversation_id, user_id)）、领没领过（red_packet_shares 的 (packet_id, user_id)）。
    代价和这个人有多少会话、会话里有多少消息都无关。

    短路：「全站现在有没有进行中的红包」每个进程每 LIVE_CACHE_SECONDS 秒最多问一次库（一次 LIMIT 1 的索引查找），
    答案是「没有」（功能没开过，或者都结束了）的这几秒里，这个函数不碰数据库。本进程里有人发了新红包会立刻作废这个答案；
    别的进程发的最多晚这几秒（会话列表本身也有 5 秒的缓存，新红包客户端靠 message.created 当场就知道）。
    """
    global _live
    now = now or _now()
    live = (RedPacket.status == "active", RedPacket.expires_at > now)
    if _live is None or time.monotonic() - _live[0] >= LIVE_CACHE_SECONDS:
        _live = (time.monotonic(), await db.scalar(select(RedPacket.id).where(*live).limit(1)) is not None)
    if not _live[1]:
        return set()
    claimed = exists().where(RedPacketShare.packet_id == RedPacket.id, RedPacketShare.user_id == user_id)
    rows = await db.execute(
        select(RedPacket.conversation_id)
        .join(ConversationMember, and_(ConversationMember.conversation_id == RedPacket.conversation_id, ConversationMember.user_id == user_id))
        .where(*live, or_(RedPacket.conversation_type != "direct", RedPacket.sender_id != user_id), _claimable_target(user_id), ~claimed)
        .distinct()
    )
    return set(rows.scalars().all())


async def pending_in_conversation(db: AsyncSession, user_id: int, conversation_id: int, *, now: datetime | None = None) -> list[RedPacket]:
    """这个会话里我还能领的红包（聊天右上角「有红包没领」的提示），新的在前，最多 20 个。口径同 pending_conversation_ids。"""
    await _membership(db, conversation_id, user_id)
    now = now or _now()
    claimed = exists().where(RedPacketShare.packet_id == RedPacket.id, RedPacketShare.user_id == user_id)
    rows = await db.execute(
        select(RedPacket)
        .where(
            RedPacket.conversation_id == conversation_id,
            RedPacket.status == "active",
            RedPacket.expires_at > now,
            RedPacket.message_id.is_not(None),
            or_(RedPacket.conversation_type != "direct", RedPacket.sender_id != user_id),
            _claimable_target(user_id),
            ~claimed,
        )
        .order_by(RedPacket.id.desc())
        .limit(20)
    )
    return list(rows.scalars().all())


def _claimable_target(user_id: int) -> Any:
    """不是专属红包，或者专属给的就是我。"""
    return or_(RedPacket.target_user_id.is_(None), RedPacket.target_user_id == user_id)


async def pending_items(db: AsyncSession, packets: list[RedPacket]) -> list[dict[str, Any]]:
    """GET /red-packets/pending 的每一项（不带金额）。"""
    users = await _users(db, {packet.target_user_id for packet in packets if packet.target_user_id is not None})
    return [
        {
            "id": packet.id,
            "message_id": packet.message_id,
            "sender_id": packet.sender_id,
            "kind": packet.kind,
            "greeting": packet.greeting,
            "count": packet.count,
            "claimed_count": packet.claimed_count,
            "expires_at": _aware(packet.expires_at),
            "created_at": _aware(packet.created_at),
            "chain_text": packet.chain_text,
            "target_user_id": packet.target_user_id,
            "target": _public(users.get(packet.target_user_id)) if packet.target_user_id is not None else None,
        }
        for packet in packets
    ]


def _mark_live() -> None:
    """有新红包了：别再相信「全站没有进行中的红包」。"""
    global _live
    _live = None


# ---------------------------------------------------------------- 实时事件


async def _recipients(db: AsyncSession, conversation_id: int) -> list[int]:
    """会话里现在在线的成员。和发消息用的是同一个函数。"""
    from app.api.v1.conversations import online_conversation_member_ids

    return await online_conversation_member_ids(db, conversation_id)


async def _broadcast_updated(packet_id: int, delay: float) -> None:
    from app.websocket.manager import manager

    try:
        if delay > 0:
            await asyncio.sleep(delay)
    finally:
        # 先摘掉标记再读库：读库之后才到的变化会再排一次，不会丢
        _pending_updates.discard(packet_id)
    try:
        async with AsyncSessionLocal() as db:
            packet = await db.get(RedPacket, packet_id)
            if packet is None:
                return
            data = {
                "id": packet.id,
                "conversation_id": packet.conversation_id,
                "message_id": packet.message_id,
                "claimed_count": packet.claimed_count,
                "count": packet.count,
                "status": effective_status(packet, _now()),
            }
            member_ids = await _recipients(db, packet.conversation_id)
        if member_ids:
            await manager.send_to_users(member_ids, UPDATED_EVENT, data)
    except Exception:
        # 推送是尽力而为：状态已经落库，客户端下次读取就是对的
        logger.exception("Failed to broadcast red packet update", extra={"red_packet_id": packet_id})


def announce_updated(packet_id: int, *, immediate: bool = False) -> None:
    """red_packet.updated：发给会话的在线成员，只有份数和状态，不带任何金额。要在改动提交之后调。"""
    if immediate:
        shop_service._spawn(_broadcast_updated(packet_id, 0.0))
        return
    if packet_id in _pending_updates:
        return
    _pending_updates.add(packet_id)
    shop_service._spawn(_broadcast_updated(packet_id, UPDATE_EVENT_DELAY_SECONDS))


# ---------------------------------------------------------------- 发


def _clean_greeting(value: Any) -> str:
    if value is None:
        return DEFAULT_GREETING
    if not isinstance(value, str):
        raise _error(status.HTTP_422_UNPROCESSABLE_ENTITY, "greeting_rejected", "祝福语格式不对")
    # 零宽字符删掉，换行和连续空白折成一个空格
    text = re.sub(r"\s+", " ", "".join(char for char in value if unicodedata.category(char) != "Cf")).strip()
    if len(text) > GREETING_MAX:
        raise _error(status.HTTP_422_UNPROCESSABLE_ENTITY, "greeting_rejected", f"祝福语最多 {GREETING_MAX} 个字")
    return text or DEFAULT_GREETING


# 接龙口令里不许出现的换行（\n、\r 之外再算上 Unicode 的行分隔符、段分隔符）
_LINE_BREAKS = re.compile("[\r\n\v\f\x1c-\x1e\x85\u2028\u2029]")


def _clean_chain_text(value: Any) -> str | None:
    """接龙口令：不传、或者去掉首尾空白之后是空的 = 不是接龙红包。零宽字符删掉；不能有换行，最长 CHAIN_TEXT_MAX 字。"""
    if value is None:
        return None
    if not isinstance(value, str):
        raise _error(status.HTTP_422_UNPROCESSABLE_ENTITY, "chain_text_rejected", "接龙口令格式不对")
    text = "".join(char for char in value if unicodedata.category(char) != "Cf").strip()
    if not text:
        return None
    if _LINE_BREAKS.search(text):
        raise _error(status.HTTP_422_UNPROCESSABLE_ENTITY, "chain_text_rejected", "接龙口令不能换行")
    if len(text) > CHAIN_TEXT_MAX:
        raise _error(status.HTTP_422_UNPROCESSABLE_ENTITY, "chain_text_rejected", f"接龙口令最多 {CHAIN_TEXT_MAX} 个字")
    return text


async def _ensure_text_allowed(db: AsyncSession, user: User, text: str, conversation_id: int, code: str, message: str) -> None:
    """发之前先过一遍文本审核，明确违规的不让发。审核服务不可用时放行，消息发出去之后仍会走异步审核。"""
    if not settings.content_moderation_enabled or user.content_moderation_whitelisted:
        return
    from app.services import moderation as moderation_service

    # 审核服务可能要几秒：先结束请求的读事务、把数据库连接还回去，不带着连接等
    await release_connection(db)
    try:
        result = await moderation_service._call_text_moderation_for_user(text, user.id, context_id=f"red_packet:{conversation_id}")
    except Exception:
        return
    if result.get("decision") == "block":
        raise _error(status.HTTP_422_UNPROCESSABLE_ENTITY, code, message)


async def _ensure_greeting_allowed(db: AsyncSession, user: User, greeting: str, conversation_id: int) -> None:
    """祝福语过审核（默认祝福语不送审）。"""
    if greeting == DEFAULT_GREETING:
        return
    await _ensure_text_allowed(db, user, greeting, conversation_id, "greeting_rejected", "祝福语未通过审核，请修改后重试")


def _invalid_target(reason: str, message: str) -> HTTPException:
    return _error(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_target", message, reason=reason)


async def _check_target(db: AsyncSession, conversation_id: int, sender_id: int, raw: Any) -> User:
    """专属红包的目标：必须是这个群的成员、不是自己、不是机器人。"""
    target_id = _positive_int(raw)
    if target_id is None:
        raise _invalid_target("invalid", "专属红包要指定一个群成员")
    if target_id == sender_id:
        raise _invalid_target("self", "专属红包不能发给自己")
    target = await db.get(User, target_id)
    if target is None:
        raise _invalid_target("not_member", "对方不在这个群里")
    if target.is_bot:
        raise _invalid_target("bot", "不能给机器人发专属红包")
    member = await db.scalar(
        select(ConversationMember.id).where(ConversationMember.conversation_id == conversation_id, ConversationMember.user_id == target_id)
    )
    if member is None:
        raise _invalid_target("not_member", "对方不在这个群里")
    return target


async def _member_name(db: AsyncSession, conversation_id: int, user: User) -> str:
    """群昵称 > 昵称 > 用户名。"""
    nickname = await db.scalar(
        select(ConversationMember.nickname).where(ConversationMember.conversation_id == conversation_id, ConversationMember.user_id == user.id)
    )
    return (nickname or "").strip() or (user.nickname or "").strip() or user.username


def _positive_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _plan(payload: dict[str, Any], config: dict[str, Any], direct: bool, exclusive: bool = False) -> tuple[str, int, int, int]:
    """校验 kind / amount / count，返回 (kind, amount, count, 整包金额)。私聊和专属红包都只能是 normal、1 个。"""
    single = direct or exclusive
    kind = payload.get("kind")
    if kind not in KINDS or (single and kind != "normal"):
        message = "红包类型只能是 normal 或 lucky" if kind not in KINDS else "私聊只能发普通红包" if direct else "专属红包只能是普通红包"
        raise _error(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_kind", message)
    count = _positive_int(payload.get("count"))
    if count is None or count > config["max_count"] or (single and count != 1):
        message = "私聊红包只能发 1 个" if direct else "专属红包只能发 1 个" if exclusive else f"红包个数要在 1–{config['max_count']} 之间"
        raise _error(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_count", message, max_count=config["max_count"])
    amount = _positive_int(payload.get("amount"))
    extra = {"max_total": config["max_total"], "min_each": config["min_each"]}
    if amount is None:
        raise _error(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_amount", "金额必须是正整数", **extra)
    total = amount * count if kind == "normal" else amount
    if total > config["max_total"]:
        raise _error(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_amount", f"单个红包的总额最多 {config['max_total']} 金币", **extra)
    if total < config["min_each"] * count:
        raise _error(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_amount", f"每个红包至少 {config['min_each']} 金币", **extra)
    return kind, amount, count, total


def _send_error(exc: HTTPException) -> HTTPException:
    """发消息那条路径（messages.send_message）自己的拒绝，换成这里的错误格式。"""
    detail = exc.detail
    if isinstance(detail, dict) and detail.get("code") == "account_muted":
        return _error(status.HTTP_403_FORBIDDEN, "muted", detail.get("message") or "你正在被禁言", muted_until=detail.get("muted_until"))
    if isinstance(detail, dict) and detail.get("code"):
        return exc
    if exc.status_code == status.HTTP_404_NOT_FOUND:
        return _not_member()
    if exc.status_code == status.HTTP_403_FORBIDDEN:
        return _error(status.HTTP_403_FORBIDDEN, "muted", "你在这个群里被禁言了，或者群开了全员禁言")
    if exc.status_code == status.HTTP_429_TOO_MANY_REQUESTS:
        return _error(status.HTTP_429_TOO_MANY_REQUESTS, "rate_limited", "发得太快了，稍后再试")
    return _error(exc.status_code, "send_failed", str(detail))


def _daily_limit(limit: int) -> HTTPException:
    return _error(status.HTTP_429_TOO_MANY_REQUESTS, "daily_limit", f"今天发红包的次数用完了（每天最多 {limit} 个）", daily_send_limit=limit)


async def send_red_packet(db: AsyncSession, user: User, payload: dict[str, Any]) -> tuple[dict[str, Any], Message]:
    """POST /red-packets：扣整包金额、建红包、在会话里落一条消息，三件事在一个事务里。

    返回 (契约里的 { red_packet, balance }, 那条消息)；消息的广播和接口返回里的 message 由接口层做，和发普通消息一样。
    检查顺序：disabled → not_member → muted → invalid_kind / invalid_count / invalid_amount / greeting_rejected →
    invalid_target / chain_text_rejected → ccw_not_bound → 祝福语、接龙口令审核 → daily_limit → 503 → 402。
    """
    user_id, oid = user.id, shop_service._bound_oid(user)
    config = await get_settings(db)
    if not config["enabled"]:
        raise _disabled()
    conversation_id = _positive_int(payload.get("conversation_id"))
    if conversation_id is None:
        raise _not_member()
    member = await _membership(db, conversation_id, user_id)
    conversation = await db.get(Conversation, conversation_id)
    if conversation is None:
        raise _not_member()
    _ensure_not_muted(user)
    direct = conversation.type != ConversationType.GROUP
    if not direct and member.role not in STAFF_ROLES:
        from app.services.conversations import _member_mute_expired

        if conversation.all_muted or (member.muted and not _member_mute_expired(member)):
            raise _error(status.HTTP_403_FORBIDDEN, "muted", "你在这个群里被禁言了，或者群开了全员禁言")
    raw_target = payload.get("target_user_id")
    exclusive = raw_target is not None
    if exclusive and direct:
        raise _invalid_target("direct", "专属红包只能在群里发")
    kind, amount, count, total = _plan(payload, config, direct, exclusive)
    greeting = _clean_greeting(payload.get("greeting"))
    chain_text = _clean_chain_text(payload.get("chain_text"))
    target = await _check_target(db, conversation_id, user_id, raw_target) if exclusive else None
    target_id = target.id if target is not None else None
    target_name = await _member_name(db, conversation_id, target) if target is not None else None
    target_public = _public(target).model_dump(mode="json") if target is not None else None
    if oid is None:
        raise _error(status.HTTP_403_FORBIDDEN, "ccw_not_bound", "先绑定 CCW 账号才能发红包")
    conversation_type = "direct" if direct else "group"
    await _ensure_greeting_allowed(db, user, greeting, conversation_id)
    if chain_text is not None:
        await _ensure_text_allowed(db, user, chain_text, conversation_id, "chain_text_rejected", "接龙口令未通过审核，请修改后重试")
    if await _sent_today(db, user_id, _now()) >= config["daily_send_limit"]:
        raise _daily_limit(config["daily_send_limit"])
    # 和商店购买一样：累计投币得是刚向 CCW 核实过的，核实不了就 503，不扣币
    await shop_service._fresh_account(db, user_id, oid)
    # 上面可能回滚过这个会话（两个请求同时给这个人建账户），回滚会让 user 过期；下面发消息要读它的属性，先重新读出来
    await db.refresh(user)

    now = _now()
    amounts = split(kind, amount, count, config["min_each"])
    outcome = "ok"
    packet: RedPacket | None = None
    message: Message | None = None
    async with shop_service._write_lock():
        try:
            # PG：先锁住自己的账户行，下面的余额检查才看得到别的进程刚提交的花币（见 lock_for_update）
            await lock_for_update(db, select(ShopAccount.user_id).where(ShopAccount.user_id == user_id))
            # 余额够才往下走。这是事务的第一条语句：从这里到提交一直拿着写锁，条件是对着最新的数据算的；
            # 红包的金额记在下面插入的行里（余额公式对它们求和），这条语句本身只是把关 + 占住写锁
            charged = await db.execute(
                update(ShopAccount)
                .where(ShopAccount.user_id == user_id, ShopAccount.ccw_oid == oid, shop_service.available_expression(user_id, oid) >= total)
                .values(updated_at=now)
                .execution_options(synchronize_session=False)
            )
            if charged.rowcount != 1:
                await db.rollback()
                outcome = "short"
            elif await _sent_today(db, user_id, now) >= config["daily_send_limit"]:
                # 拿到写锁之后再数一遍：同时发出来的几个请求不能一起越过每日上限
                await db.rollback()
                outcome = "limit"
            else:
                packet = RedPacket(
                    sender_id=user_id,
                    conversation_id=conversation_id,
                    conversation_type=conversation_type,
                    kind=kind,
                    greeting=greeting,
                    chain_text=chain_text,
                    target_user_id=target_id,
                    total=total,
                    count=count,
                    ccw_oid=oid,
                    expires_at=now + timedelta(hours=config["expire_hours"]),
                    created_at=now,
                )
                db.add(packet)
                await db.flush()
                db.add_all([RedPacketShare(packet_id=packet.id, seq=index, amount=value) for index, value in enumerate(amounts, start=1)])
                # 走正常的发消息路径（成员、禁言、限频、群活跃度都和普通消息一致），但不让它自己提交。
                # metadata 里只放发的时候就定下的字段；状态和金额要调 GET /red-packets/{id}。
                # 接龙、专属的字段只在是的时候才有（普通红包的 metadata 和以前一模一样）
                from app.services import messages as message_service

                info: dict[str, Any] = {"id": packet.id, "kind": kind, "greeting": greeting, "count": count}
                if chain_text is not None:
                    info["chain_text"] = chain_text
                if target_id is not None:
                    info["target_user_id"] = target_id
                    info["target"] = target_public
                message = await message_service.send_message(
                    db,
                    conversation_id,
                    user,
                    fallback_text(greeting, chain=chain_text is not None, target_name=target_name),
                    MessageType.RED_PACKET,
                    {"red_packet": info},
                    allow_red_packet=True,
                    commit=False,
                )
                packet.message_id = message.id
                await db.commit()
                _mark_live()
        except HTTPException as exc:
            # 发消息那一步被拒了：整个事务回滚，没有扣币
            await db.rollback()
            raise _send_error(exc) from None
        except BaseException:
            await db.rollback()
            raise
    if outcome == "limit":
        raise _daily_limit(config["daily_send_limit"])
    if outcome == "short":
        balance = (await shop_service._balance(db, user_id, oid))["balance"]
        raise _error(
            status.HTTP_402_PAYMENT_REQUIRED,
            "insufficient_balance",
            "金币不够",
            balance=balance,
            total=total,
            shortfall=max(total - balance, 0),
            donate_url=get_coin_ledger().topup_url(),
        )
    assert packet is not None and message is not None
    shop_service.announce_balance(user_id, "red_packet.sent", red_packet_id=packet.id)
    return {"red_packet": await _view(db, packet, user_id, now), "balance": await shop_service._balance(db, user_id, oid)}, message


# ---------------------------------------------------------------- 结束（到期、撤回、撤销）


async def _end(
    db: AsyncSession,
    packet_id: int,
    new_status: str,
    now: datetime,
    *,
    only_if_expired: bool = False,
    admin_id: int | None = None,
    reason: str | None = None,
) -> bool:
    """把一个还在进行中的红包结束掉，没领的退回发送者。返回这一次调用是不是真的结束了它。

    幂等：条件是 status = 'active'，多个 worker、多条路径同时来也只有一个影响到行，其余的什么都不改。
    退回多少是在同一条语句里算的（没被领的那些份之和），所以和「最后一刻被领走一份」不会打架：
    那一份要么在这条语句之前已经记到人头上（不算进退回），要么在之后（那时红包已不是 active，领不走）。
    """
    unclaimed = (
        select(func.coalesce(func.sum(RedPacketShare.amount), 0))
        .where(RedPacketShare.packet_id == RedPacket.id, RedPacketShare.user_id.is_(None))
        .correlate(RedPacket)
        .scalar_subquery()
    )
    clauses = [RedPacket.id == packet_id, RedPacket.status == "active"]
    if only_if_expired:
        clauses.append(RedPacket.expires_at <= now)
    async with shop_service._write_lock():
        try:
            # PG：先锁住红包这一行，等正在抢的人提交之后再算「没领的」（见 _take_share）
            await lock_for_update(db, select(RedPacket.id).where(RedPacket.id == packet_id))
            result = await db.execute(
                update(RedPacket)
                .where(*clauses)
                .values(status=new_status, refunded_amount=unclaimed, finished_at=now, revoked_by_id=admin_id, revoke_reason=reason)
                .execution_options(synchronize_session=False)
            )
            ended = result.rowcount == 1
            if ended and admin_id is not None:
                refunded = int(await db.scalar(select(RedPacket.refunded_amount).where(RedPacket.id == packet_id)) or 0)
                await log_admin_action(db, admin_id, "red_packet.revoke", "red_packet", packet_id, {"reason": reason, "refunded": refunded})
            await db.commit()
        except BaseException:
            await db.rollback()
            raise
    if ended:
        await _after_end(db, packet_id)
    return ended


async def _after_end(db: AsyncSession, packet_id: int) -> None:
    row = (await db.execute(select(RedPacket.sender_id, RedPacket.refunded_amount).where(RedPacket.id == packet_id))).first()
    announce_updated(packet_id, immediate=True)
    if row is not None and row[1] > 0:
        shop_service.announce_balance(row[0], "red_packet.refunded", red_packet_id=packet_id)


async def _message_recalled(db: AsyncSession, packet: RedPacket) -> bool:
    if packet.message_id is None:
        return False
    return await db.scalar(select(Message.id).where(Message.id == packet.message_id, Message.recalled_at.is_not(None))) is not None


async def _settle_if_due(db: AsyncSession, packet: RedPacket, now: datetime) -> RedPacket:
    """现场判断：过期了、或者消息已经被撤回的红包，不等清扫，现在就结束掉。返回最新的红包。"""
    if packet.status != "active":
        return packet
    packet_id = packet.id
    if _aware(packet.expires_at) <= now:
        await _end(db, packet_id, "expired", now, only_if_expired=True)
    elif await _message_recalled(db, packet):
        await _end(db, packet_id, "revoked", now)
    else:
        return packet
    return await _load(db, packet_id)


async def end_for_recalled_message(_db: AsyncSession | None, message_id: int) -> None:
    """红包消息被撤回之后调（messages.recall_message / admin_recall_message）：视同提前结束，没领的退回。

    用自己的会话，出错只记日志：撤回已经成了，不能因为这里失败让撤回报错；漏掉的由定时清扫补上。
    """
    try:
        async with AsyncSessionLocal() as db:
            rows = await db.execute(select(RedPacket.id).where(RedPacket.message_id == message_id, RedPacket.status == "active"))
            for packet_id in rows.scalars().all():
                await _end(db, packet_id, "revoked", _now())
    except Exception:
        logger.exception("Failed to end the red packet of a recalled message", extra={"message_id": message_id})


async def sweep(db: AsyncSession, *, now: datetime | None = None) -> dict[str, int]:
    """定时清扫：过期没领完的退回并置 expired；消息已被撤回（机器审核、案件结案这些不经过撤回接口的路径）或会话已经不在了的置 revoked。

    每个 worker 都跑，靠 _end 的条件更新去重：同一个红包只有一个进程真的结束它、发事件。开关关着也照常清扫 —— 关掉功能不能把币留在半路。
    """
    now = now or _now()
    expired_ids = (
        (await db.execute(select(RedPacket.id).where(RedPacket.status == "active", RedPacket.expires_at <= now).order_by(RedPacket.id).limit(SWEEP_BATCH)))
        .scalars()
        .all()
    )
    recalled = exists().where(Message.id == RedPacket.message_id, Message.recalled_at.is_not(None))
    orphaned = ~exists().where(Conversation.id == RedPacket.conversation_id)
    revoked_ids = (
        (
            await db.execute(
                select(RedPacket.id).where(RedPacket.status == "active", RedPacket.expires_at > now, or_(recalled, orphaned)).order_by(RedPacket.id).limit(SWEEP_BATCH)
            )
        )
        .scalars()
        .all()
    )
    done = {"expired": 0, "revoked": 0}
    for packet_id in expired_ids:
        done["expired"] += await _end(db, packet_id, "expired", now, only_if_expired=True)
    for packet_id in revoked_ids:
        done["revoked"] += await _end(db, packet_id, "revoked", now)
    return done


async def _worker() -> None:
    delay = WORKER_STARTUP_DELAY_SECONDS
    while True:
        await asyncio.sleep(delay)
        try:
            async with AsyncSessionLocal() as db:
                await sweep(db)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Red packet sweep failed")
        delay = float(settings.red_packet_sweep_interval_seconds)


def start_red_packet_worker() -> None:
    global _worker_task
    if _worker_task is not None and not _worker_task.done():
        return
    _worker_task = asyncio.create_task(_worker())


async def stop_red_packet_worker() -> None:
    global _worker_task
    task = _worker_task
    _worker_task = None
    if task is None or task.done():
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


# ---------------------------------------------------------------- 看、抢


async def get_red_packet(db: AsyncSession, user: User, packet_id: int) -> dict[str, Any]:
    """GET /red-packets/{id}：只有会话成员能看。"""
    user_id = user.id
    packet = await _load(db, packet_id)
    await _membership(db, packet.conversation_id, user_id)
    now = _now()
    packet = await _settle_if_due(db, packet, now)
    return await _view(db, packet, user_id, now)


def _muted_in_group() -> HTTPException:
    return _error(status.HTTP_403_FORBIDDEN, "muted_in_group", "你在这个群里被禁言了（或者群开了全员禁言），领接龙红包要发言，暂时领不了")


def _chain_send_error(exc: HTTPException) -> HTTPException:
    """领接龙红包时自动发言被发消息那条路径拒了，换成这里的错误格式。这时整个领取已经回滚，没有领到。"""
    detail = exc.detail
    if isinstance(detail, dict) and detail.get("code") == "account_muted":
        return _error(status.HTTP_403_FORBIDDEN, "muted", detail.get("message") or "你正在被禁言", muted_until=detail.get("muted_until"))
    if isinstance(detail, dict) and detail.get("code"):
        return exc
    if exc.status_code == status.HTTP_404_NOT_FOUND:
        return _not_member()
    if exc.status_code == status.HTTP_403_FORBIDDEN:
        return _muted_in_group()
    return _error(exc.status_code, "chain_send_failed", "接龙消息发不出去，这次没有领取，稍后再试")


async def _take_share(db: AsyncSession, packet_id: int, user_id: int, now: datetime, chain: tuple[User, int, str] | None = None) -> tuple[str, Message | None]:
    """抢一份。返回 ("ok", 接龙消息或 None) / ("none", None)（没抢到：没有剩的了，或者红包已经不在进行中）/ ("already", None)（这个人已经有一份了）。

    chain：接龙红包，(领取人, 会话 id, 接龙口令)。领到之后在**同一个事务**里以领取人的身份发接龙口令，两件事一起提交：
    消息发不出去（比如这一刻刚被禁言）就整个回滚，钱没动、消息也没有，接口报错。实时推送由调用方在提交之后做。
    """
    free = (
        select(RedPacketShare.id)
        .where(RedPacketShare.packet_id == packet_id, RedPacketShare.user_id.is_(None))
        .order_by(RedPacketShare.seq.asc())
        .limit(1)
        # 子查询和外面的 UPDATE 是同一张表，不能让它被当成关联子查询
        .correlate(None)
        .scalar_subquery()
    )
    still_open = exists().where(RedPacket.id == packet_id, RedPacket.status == "active", RedPacket.expires_at > now)
    recalled = exists().where(
        Message.id == select(RedPacket.message_id).where(RedPacket.id == packet_id).scalar_subquery(), Message.recalled_at.is_not(None)
    )
    async with shop_service._write_lock():
        try:
            # PG：先锁住红包这一行。抢红包和结束红包（到期 / 撤回 / 清扫）都先锁它，彼此串行：下面的语句各自取新快照，
            # 不会出现「同一份既被领走、又被算进退款」，多人同时抢也不会挑中同一份（见 lock_for_update）
            await lock_for_update(db, select(RedPacket.id).where(RedPacket.id == packet_id))
            # 事务的第一条写语句，也是唯一决定「谁拿到哪一份」的语句：一份只会被一条这样的 UPDATE 记到人头上
            taken = await db.execute(
                update(RedPacketShare)
                .where(RedPacketShare.id == free, RedPacketShare.user_id.is_(None), still_open, ~recalled)
                .values(user_id=user_id, claimed_at=now)
                .execution_options(synchronize_session=False)
            )
            if taken.rowcount != 1:
                await db.rollback()
                return "none", None
            amount = select(RedPacketShare.amount).where(RedPacketShare.packet_id == packet_id, RedPacketShare.user_id == user_id).scalar_subquery()
            last = RedPacket.claimed_count + 1 >= RedPacket.count
            # 红包上的计数和上面那一份一起提交；领完最后一份就地置 finished
            await db.execute(
                update(RedPacket)
                .where(RedPacket.id == packet_id)
                .values(
                    claimed_count=RedPacket.claimed_count + 1,
                    claimed_amount=RedPacket.claimed_amount + amount,
                    status=case((last, "finished"), else_=RedPacket.status),
                    finished_at=case((last, now), else_=RedPacket.finished_at),
                )
                .execution_options(synchronize_session=False)
            )
        except IntegrityError:
            # (packet_id, user_id) 唯一：这个人的另一个请求已经拿到一份了
            await db.rollback()
            return "already", None
        except BaseException:
            await db.rollback()
            raise
        message: Message | None = None
        try:
            if chain is not None:
                claimer, conversation_id, chain_text = chain
                from app.services import messages as message_service

                # 前面的重试回滚过会话的话 claimer 的属性已经过期，发消息要读它们，先重新读出来（这时已经拿着写锁，读到的是最新的）
                await db.refresh(claimer)
                message = await message_service.send_message(
                    db,
                    conversation_id,
                    claimer,
                    chain_text,
                    MessageType.TEXT,
                    {"red_packet_chain": {"red_packet_id": packet_id}},
                    red_packet_chain=True,
                    commit=False,
                )
            await db.commit()
            return "ok", message
        except HTTPException as exc:
            await db.rollback()
            raise _chain_send_error(exc) from None
        except BaseException:
            await db.rollback()
            raise


async def _my_share(db: AsyncSession, packet_id: int, user_id: int) -> RedPacketShare | None:
    return (
        await db.execute(select(RedPacketShare).where(RedPacketShare.packet_id == packet_id, RedPacketShare.user_id == user_id))
    ).scalar_one_or_none()


async def _ensure_can_speak_in_group(db: AsyncSession, packet: RedPacket, member: ConversationMember) -> None:
    """接龙红包：领了就要在群里发言，群里被禁言（或者群开了全员禁言）的不能领。群主、管理员不受群内禁言限制。"""
    if packet.conversation_type != "group" or member.role in STAFF_ROLES:
        return
    conversation = await db.get(Conversation, packet.conversation_id)
    if conversation is None:
        raise _not_member()
    from app.services.conversations import _member_mute_expired

    if conversation.all_muted or (member.muted and not _member_mute_expired(member)):
        raise _muted_in_group()


async def claim(db: AsyncSession, user: User, packet_id: int) -> tuple[dict[str, Any], Message | None]:
    """POST /red-packets/{id}/claim。没绑定 CCW 的人也能领：币记在账上，花的时候才要求绑定。

    返回 (契约里的 { amount, red_packet, balance }, 接龙消息)。接龙红包领到时会以领取人的身份发一条消息（和领取一起提交），
    它的广播由接口层做，和发普通消息一样；不是接龙红包时第二项是 None。
    """
    user_id, oid = user.id, shop_service._bound_oid(user)
    # 这里不看总开关：关掉开关只是不让发新的，已经发出去的照常能领、照常过期退回（要止损用后台撤销）
    packet = await _load(db, packet_id)
    member = await _membership(db, packet.conversation_id, user_id)
    _ensure_not_muted(user)
    if packet.target_user_id is not None and packet.target_user_id != user_id:
        raise _error(status.HTTP_403_FORBIDDEN, "not_target", "这是给别人的专属红包")
    if packet.conversation_type == "direct" and packet.sender_id == user_id:
        raise _error(status.HTTP_403_FORBIDDEN, "own_packet", "私聊里不能领自己发的红包")
    if packet.chain_text:
        # 先确认能发言再去抢：被禁言的人领不了接龙红包（事务里发消息那一步还会再查一遍，挡住这之间刚被禁言的）
        await _ensure_can_speak_in_group(db, packet, member)
    now = _now()
    chain_message: Message | None = None
    for _attempt in range(CLAIM_ATTEMPTS):
        if await _my_share(db, packet_id, user_id) is not None:
            raise _error(status.HTTP_409_CONFLICT, "already_claimed", "你已经领过这个红包了")
        # 过期、消息被撤回都在这里现场判断并结束掉，不依赖清扫
        packet = await _settle_if_due(db, await _load(db, packet_id), now)
        if packet.status != "active":
            raise _gone(packet.status)
        outcome, chain_message = await _take_share(db, packet_id, user_id, now, (user, packet.conversation_id, packet.chain_text) if packet.chain_text else None)
        if outcome == "ok":
            break
        if outcome == "already":
            raise _error(status.HTTP_409_CONFLICT, "already_claimed", "你已经领过这个红包了")
    else:
        raise _gone("finished")
    mine = await _my_share(db, packet_id, user_id)
    assert mine is not None
    amount = mine.amount
    # 抢到过红包的人在后台的账户列表里要看得到，哪怕他从没核实过余额
    await shop_service._ensure_account(db, user_id)
    announce_updated(packet_id)
    shop_service.announce_balance(user_id, "red_packet.claimed", red_packet_id=packet_id)
    return {
        "amount": amount,
        "red_packet": await _view(db, await _load(db, packet_id), user_id, now),
        "balance": await shop_service._balance(db, user_id, oid),
    }, chain_message


async def list_mine(db: AsyncSession, user: User, direction: str, limit: int, offset: int) -> dict[str, Any]:
    """GET /red-packets/mine：我发出的 / 我抢到的红包，新的在前。

    sent 的每一项直接是红包对象；received 的每一项是 { amount, created_at, red_packet }（我领到多少、什么时候、哪个红包）。
    这里的红包对象不带领取明细（claims 为 null；要看明细调 GET /red-packets/{id}）。
    sum：sent 是发出的整包金额之和（不扣退回的），received 是抢到的金额之和。
    """
    user_id = user.id
    now = _now()
    if direction == "sent":
        clause = RedPacket.sender_id == user_id
        total, amount = (await db.execute(select(func.count(), func.coalesce(func.sum(RedPacket.total), 0)).where(clause))).one()
        packets = list(
            (await db.execute(select(RedPacket).where(clause).order_by(RedPacket.created_at.desc(), RedPacket.id.desc()).offset(offset).limit(limit)))
            .scalars()
            .all()
        )
    else:
        clause = RedPacketShare.user_id == user_id
        total, amount = (await db.execute(select(func.count(), func.coalesce(func.sum(RedPacketShare.amount), 0)).where(clause))).one()
        packets = list(
            (
                await db.execute(
                    select(RedPacket)
                    .join(RedPacketShare, RedPacketShare.packet_id == RedPacket.id)
                    .where(clause)
                    .order_by(RedPacketShare.claimed_at.desc(), RedPacketShare.id.desc())
                    .offset(offset)
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
    packet_ids = [packet.id for packet in packets]
    mine: dict[int, RedPacketShare] = {}
    if packet_ids:
        rows = await db.execute(select(RedPacketShare).where(RedPacketShare.packet_id.in_(packet_ids), RedPacketShare.user_id == user_id))
        mine = {share.packet_id: share for share in rows.scalars().all()}
    users = await _users(db, {person for packet in packets for person in _people(packet)})
    views = [_base_view(packet, users, mine.get(packet.id), user_id, now) for packet in packets]
    if direction != "sent":
        views = [{"amount": view["my_claim"]["amount"], "created_at": view["my_claim"]["created_at"], "red_packet": view} for view in views]
    return {"items": views, "total": int(total or 0), "sum": int(amount or 0)}


# ---------------------------------------------------------------- 后台


def _admin_view(packet: RedPacket, users: dict[int, User], now: datetime) -> dict[str, Any]:
    """后台看到的红包。隐私边界：私聊红包的祝福语、接龙口令一律不给（那是私聊内容）；任何消息内容都不在这里出现。"""
    group = packet.conversation_type == "group"
    return {
        "id": packet.id,
        "kind": packet.kind,
        "greeting": packet.greeting if group else None,
        "sender": shop_service._admin_user_view(users.get(packet.sender_id)),
        "sender_id": packet.sender_id,
        "conversation_id": packet.conversation_id,
        "conversation_type": packet.conversation_type,
        "message_id": packet.message_id,
        "chain": packet.chain_text is not None,
        "chain_text": packet.chain_text if group else None,
        "target_user_id": packet.target_user_id,
        "target": shop_service._admin_user_view(users.get(packet.target_user_id)) if packet.target_user_id is not None else None,
        "total": packet.total,
        "count": packet.count,
        "claimed_count": packet.claimed_count,
        "claimed_amount": packet.claimed_amount,
        "refunded_amount": packet.refunded_amount,
        "status": effective_status(packet, now),
        "expires_at": _aware(packet.expires_at),
        "created_at": _aware(packet.created_at),
        "finished_at": _aware(packet.finished_at),
        "revoked_by_id": packet.revoked_by_id,
        "revoke_reason": packet.revoke_reason,
    }


async def admin_settings(db: AsyncSession) -> dict[str, Any]:
    """GET /admin/red-packets/settings：开关与限制，外加每一项允许设成多少。"""
    config = await get_settings(db, fresh=True)
    return {**config, "bounds": {name: {"min": low, "max": high} for name, (low, high) in BOUNDS.items()}}


async def admin_update_settings(db: AsyncSession, admin: User, changes: dict[str, Any]) -> dict[str, Any]:
    await update_settings(db, admin, changes)
    return await admin_settings(db)


async def admin_list(
    db: AsyncSession,
    *,
    status_filter: str | None,
    kind: str | None,
    sender_id: int | None,
    conversation_id: int | None,
    created_from: datetime | None,
    created_to: datetime | None,
    limit: int,
    offset: int,
) -> dict[str, Any]:
    """GET /admin/red-packets：按状态 / 类型 / 发送者 / 会话 / 创建时间筛选，新的在前。summary 是全站累计，不随筛选变。

    status 按库里的状态筛：过了期、清扫还没轮到的（最多晚一个清扫间隔）仍算 active，但每一项展示的是 expired。
    """
    clauses = []
    if status_filter is not None:
        clauses.append(RedPacket.status == status_filter)
    if kind is not None:
        clauses.append(RedPacket.kind == kind)
    if sender_id is not None:
        clauses.append(RedPacket.sender_id == sender_id)
    if conversation_id is not None:
        clauses.append(RedPacket.conversation_id == conversation_id)
    if created_from is not None:
        clauses.append(RedPacket.created_at >= _aware(created_from).astimezone(UTC))
    if created_to is not None:
        clauses.append(RedPacket.created_at < _aware(created_to).astimezone(UTC))
    total = await db.scalar(select(func.count()).select_from(RedPacket).where(*clauses))
    sent, claimed, refunded, outstanding = (
        await db.execute(
            select(
                func.coalesce(func.sum(RedPacket.total), 0),
                func.coalesce(func.sum(RedPacket.claimed_amount), 0),
                func.coalesce(func.sum(RedPacket.refunded_amount), 0),
                func.coalesce(func.sum(case((RedPacket.status == "active", RedPacket.total - RedPacket.claimed_amount), else_=0)), 0),
            )
        )
    ).one()
    rows = await db.execute(select(RedPacket).where(*clauses).order_by(RedPacket.created_at.desc(), RedPacket.id.desc()).offset(offset).limit(limit))
    packets = list(rows.scalars().all())
    users = await _users(db, {person for packet in packets for person in _people(packet)})
    now = _now()
    return {
        "items": [_admin_view(packet, users, now) for packet in packets],
        "total": int(total or 0),
        "summary": {"total_sent": int(sent), "total_claimed": int(claimed), "total_refunded": int(refunded), "outstanding": int(outstanding)},
    }


async def admin_detail(db: AsyncSession, packet_id: int) -> dict[str, Any]:
    """GET /admin/red-packets/{id}：红包和领取明细（谁、多少、什么时候）。"""
    packet = await _load(db, packet_id)
    claimed = await _claimed_shares(db, packet_id)
    users = await _users(db, _people(packet) | {share.user_id for share in claimed})
    luckiest = _luckiest(packet, claimed)
    return {
        **_admin_view(packet, users, _now()),
        "claims": [
            {
                "user": shop_service._admin_user_view(users.get(share.user_id)),
                "user_id": share.user_id,
                "amount": share.amount,
                "created_at": _aware(share.claimed_at),
                "is_luckiest": share.id == luckiest,
            }
            for share in claimed
        ],
    }


async def admin_revoke(db: AsyncSession, admin: User, packet_id: int, reason: str | None) -> dict[str, Any]:
    """POST /admin/red-packets/{id}/revoke：提前结束，没领的退回发送者，写审计。已经结束的返回 409 not_active。"""
    admin_id = admin.id
    await _load(db, packet_id)
    text = (reason or "").strip()
    if not text or len(text) > REASON_MAX:
        raise _error(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_reason", f"撤销原因必填，最长 {REASON_MAX} 个字", field="reason")
    if not await _end(db, packet_id, "revoked", _now(), admin_id=admin_id, reason=text):
        raise _error(status.HTTP_409_CONFLICT, "not_active", "这个红包已经结束了")
    return await admin_detail(db, packet_id)


async def ledger_check(db: AsyncSession) -> dict[str, Any]:
    """全站红包账的守恒检查（对账用）：发出 = 已领 + 已退 + 未结。

    四个数各从各的地方数，不是互相推出来的：发出和已退来自 red_packets，已领和未结来自 red_packet_shares
    （未结 = 还在进行中的红包里没被领的那些份）。balanced 为 false 时 problems 列出哪些红包自己就对不上（最多 50 个）：
    每一份加起来不等于整包、红包上的计数和实际领取的行不一致、结束了的红包「已领 + 已退」不等于整包。
    """
    sent, refunded = (await db.execute(select(func.coalesce(func.sum(RedPacket.total), 0), func.coalesce(func.sum(RedPacket.refunded_amount), 0)))).one()
    claimed = await db.scalar(select(func.coalesce(func.sum(RedPacketShare.amount), 0)).where(RedPacketShare.user_id.is_not(None)))
    outstanding = await db.scalar(
        select(func.coalesce(func.sum(RedPacketShare.amount), 0))
        .join(RedPacket, RedPacket.id == RedPacketShare.packet_id)
        .where(RedPacketShare.user_id.is_(None), RedPacket.status == "active")
    )
    taken = RedPacketShare.user_id.is_not(None)
    share_count = func.count(RedPacketShare.id)
    share_sum = func.coalesce(func.sum(RedPacketShare.amount), 0)
    taken_count = func.coalesce(func.sum(case((taken, 1), else_=0)), 0)
    taken_sum = func.coalesce(func.sum(case((taken, RedPacketShare.amount), else_=0)), 0)
    ended = RedPacket.status.in_(("expired", "revoked"))
    rows = await db.execute(
        select(RedPacket.id, RedPacket.status, RedPacket.total, RedPacket.count, RedPacket.claimed_count, RedPacket.claimed_amount, RedPacket.refunded_amount, share_count, share_sum, taken_count, taken_sum)
        .outerjoin(RedPacketShare, RedPacketShare.packet_id == RedPacket.id)
        .group_by(RedPacket.id)
        .having(
            or_(
                share_count != RedPacket.count,
                share_sum != RedPacket.total,
                taken_count != RedPacket.claimed_count,
                taken_sum != RedPacket.claimed_amount,
                and_(RedPacket.status.in_(("active", "finished")), RedPacket.refunded_amount != 0),
                and_(RedPacket.status == "finished", taken_sum != RedPacket.total),
                and_(ended, RedPacket.refunded_amount != RedPacket.total - taken_sum),
                RedPacket.status.not_in(STATUSES),
            )
        )
        .order_by(RedPacket.id)
        .limit(50)
    )
    problems = [
        {
            "id": row[0],
            "status": row[1],
            "total": row[2],
            "count": row[3],
            "claimed_count": row[4],
            "claimed_amount": row[5],
            "refunded_amount": row[6],
            "share_count": int(row[7]),
            "share_sum": int(row[8]),
            "taken_count": int(row[9]),
            "taken_sum": int(row[10]),
        }
        for row in rows.all()
    ]
    sent, refunded, claimed, outstanding = int(sent), int(refunded), int(claimed or 0), int(outstanding or 0)
    return {
        "total_sent": sent,
        "total_claimed": claimed,
        "total_refunded": refunded,
        "outstanding": outstanding,
        "balanced": sent == claimed + refunded + outstanding and not problems,
        "problems": problems,
    }
