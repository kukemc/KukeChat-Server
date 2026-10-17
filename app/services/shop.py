"""商店（docs/shop.md）：用 CCW 金币换聊天装饰，目前有气泡皮肤和头像框。

KukeChat 不发币也不存币。余额 = 用户给官方作品累计投的币 − 在这里花掉的 − 送礼花掉的 − 群升级花掉的 − 发出的红包 + 抢到的红包 + 退回的红包
+ 后台赠送的（coin_grants，可正可负，见 admin_grant_coins）
（红包见 docs/red-packets.md、services/red_packets.py，礼物见 docs/gifts.md、services/gifts.py；
红包和礼物那几项直接对它们自己的表求和，这里没有另外的列）。
累计投了多少由可插拔的余额来源核实（app/integrations/coin_ledger.py 的 CoinLedger 接口，实现不在本仓库里）；
没有接入余额来源时商店能逛、能领等级解锁的商品，不能花币买。
几条贯穿全文件的约定：

- 向 CCW 核实不了时一律按「不知道」处理：展示上次的数并标 stale，购买直接 503；不会因此多给或少扣。
- 扣款是一条带条件的 UPDATE（余额够才加 spent），和写购买记录在同一个事务里；重复拥有由 shop_purchases 上的唯一索引挡住。
- 装备着什么冗余在 users.cosmetics 上，序列化用户时不查这里的表；商品样式被改、商品被收回时由这里负责把那一列改对，
  这一列每次变化都发 user.cosmetics.updated。
- 事务里一旦 rollback，会话里的 ORM 对象全部过期，异步下再读属性会出错 —— 所以先把要用的值取成普通变量。
- 用户侧的写事务都先过一把进程内的锁再进 SQLite，原因见 _write_lock。
- 限时商品（duration_days）：拥有记录带到期时间，「是否拥有 / 是否装备着」一律读的时候现场和现在比，不依赖清扫；
  续期是写一行新记录接替原来那一行（见 _grant_ownership）。清扫只负责卸下和发事件（见 sweep_expiry）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time
import weakref
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import and_, case, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import AsyncSessionLocal, release_connection
from app.integrations import CoinsUnavailable, get_coin_ledger
from app.models.gift import GiftRecord
from app.models.group_plus import GroupPlusRecord
from app.models.red_packet import RedPacket, RedPacketShare
from app.models.shop import CoinGrant, ShopAccount, ShopItem, ShopPurchase
from app.models.user import User
from app.schemas.user import UserPublic, load_cosmetics, parse_cosmetics
from app.services.reports import log_admin_action
from app.services.shop_style import (
    ANIMATIONS,
    FRAME_ANIMATIONS,
    FRAME_ORNAMENTS,
    FRAME_POSITIONS,
    ORNAMENTS,
    PATTERNS,
    StyleError,
    validate_bubble_style,
    validate_frame_style,
)

logger = logging.getLogger(__name__)
UTC = timezone.utc


@dataclass(frozen=True)
class Category:
    key: str
    label: str
    # 装备槽：一个槽同时只能装备一件
    slot: str
    # 校验并规范化这类商品的 style，不合法抛 StyleError
    validate: Callable[[Any], dict[str, Any]]


# 以后加昵称颜色之类的，在这里登记一行（再写一个对应的 style 校验函数）。
# 装备、卸下、收回、样式同步、user.cosmetics.updated 都是按槽通用写的，登记完就能用
CATEGORIES: dict[str, Category] = {
    "bubble": Category(key="bubble", label="气泡", slot="bubble", validate=validate_bubble_style),
    "frame": Category(key="frame", label="头像框", slot="frame", validate=validate_frame_style),
}
SLOTS = frozenset(category.slot for category in CATEGORIES.values())
TAGS = ("new", "hot", "limited")
SOURCES = ("purchase", "level", "grant", "gift")
SKU_RE = re.compile(r"[a-z0-9]+(?:[._-][a-z0-9]+)*")
SKU_MAX = 64
NAME_MAX = 40
DESCRIPTION_MAX = 200
PREVIEW_TEXT_MAX = 80
NOTE_MAX = 500
PRICE_MAX = 1_000_000
UNLOCK_LEVEL_MAX = 256
# 限时商品一次最多给多少天
DURATION_MAX = 3650
# 到期前多久提醒一次（shop.item.expiring）
EXPIRING_BEFORE = timedelta(days=3)
# 到期清扫一轮最多处理多少行，剩下的下一轮接着来
EXPIRY_SWEEP_BATCH = 500
# 续期和别的写事务撞车（另一个进程刚好也在给同一个人的同一件商品写记录）时重来几次
OWNERSHIP_ATTEMPTS = 3
SORT_MAX = 1_000_000
# 对账结果里最多列出多少个「花的比投的多」的账户，其余的去账户列表里看
RECONCILE_MISMATCH_LIMIT = 200

COSMETICS_EVENT = "user.cosmetics.updated"
CHANGED_EVENT = "shop.changed"
GRANTED_EVENT = "shop.item.granted"
BALANCE_EVENT = "shop.balance.changed"
EXPIRING_EVENT = "shop.item.expiring"
EXPIRED_EVENT = "shop.item.expired"

# —— 进程内的状态 ——
# 正在向 CCW 核实的人：(用户 id, CCW oid) -> 那次请求。同一个人同时来的第二个请求等同一个结果
_verifying: dict[tuple[int, str], asyncio.Future[str]] = {}
# 上次发 shop.changed 的时刻（time.monotonic()），以及已经排好、还没发出去的那一次
_changed_sent_at: float | None = None
_changed_task: asyncio.Task[None] | None = None
# user.cosmetics.updated 按用户合并：上次发的时刻、装扮变了还没发出去的人、每个人负责发送的那个任务（同时最多一个）
_cosmetics_sent_at: dict[int, float] = {}
_cosmetics_dirty: set[int] = set()
_cosmetics_pending: dict[int, asyncio.Task[Any]] = {}
_COSMETICS_SWEEP_SIZE = 2048
# 后台任务要留一个强引用，否则可能在跑完之前被回收
_background: set[asyncio.Task[Any]] = set()
# 见 _write_lock。锁是绑在事件循环上的，按循环各存一把
_write_locks: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock] = weakref.WeakKeyDictionary()
_expiry_task: asyncio.Task[None] | None = None
EXPIRY_WORKER_STARTUP_DELAY_SECONDS = 9.0


def reset_runtime_state() -> None:
    """清掉进程内的状态（测试用）。"""
    global _changed_sent_at, _changed_task, _expiry_task
    _expiry_task = None
    _verifying.clear()
    _changed_sent_at = None
    _changed_task = None
    _cosmetics_sent_at.clear()
    _cosmetics_dirty.clear()
    _cosmetics_pending.clear()
    _background.clear()


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(value: datetime | None) -> datetime | None:
    """SQLite 读出来的时间不带时区，统一当 UTC。"""
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _error(status_code: int, code: str, message: str, **extra: Any) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message, **extra})


def _item_not_found() -> HTTPException:
    return _error(status.HTTP_404_NOT_FOUND, "item_not_found", "商品不存在")


def _already_owned() -> HTTPException:
    return _error(status.HTTP_409_CONFLICT, "already_owned", "已经拥有这件商品了")


def _not_bound() -> HTTPException:
    return _error(status.HTTP_409_CONFLICT, "ccw_not_bound", "先绑定 CCW 账号才能使用金币")


def _unavailable() -> HTTPException:
    return _error(status.HTTP_503_SERVICE_UNAVAILABLE, "verification_unavailable", "现在核实不了金币余额，稍后再试。没有扣币")


def user_level(user: Any) -> int:
    """用户的账号等级（docs/account-level.md）。等级功能还没合进来时没有这个属性，按 0 级算。"""
    return int(getattr(user, "account_level", 0) or 0)


def _bound_oid(user: User) -> str | None:
    return (user.ccw_student_oid or "").strip().lower() or None


def _write_lock() -> asyncio.Lock:
    """本进程里商店的短写事务排队用的锁：先在这里排队，再进 SQLite。锁里只放「写几条语句然后提交」，不放任何别的等待。

    SQLite 的写事务本来就是串行的，但让同一个进程里的两条连接在 SQLite 里面互相等有个坑（并发购买的测试能复现）：
    等写锁的那条连接在自己的线程里 busy-wait，这时事件循环线程上如果正好触发垃圾回收、要析构那条连接上的游标，
    就会卡在那条连接的互斥锁上；而拿着写锁的那个请求得回到事件循环才能提交 —— 两边互相等，直到 busy_timeout（10 秒）
    才散，期间整个 worker 不响应，最后等锁的一方报 database is locked。
    先用 asyncio 的锁排队，同一个进程里的商店写事务就不会在 SQLite 里互相等了。别的进程照旧靠 SQLite 自己的锁。
    """
    loop = asyncio.get_running_loop()
    lock = _write_locks.get(loop)
    if lock is None:
        lock = _write_locks[loop] = asyncio.Lock()
    return lock


def _spawn(coroutine: Coroutine[Any, Any, Any]) -> asyncio.Task[Any]:
    task = asyncio.create_task(coroutine)
    _background.add(task)
    task.add_done_callback(_background.discard)
    return task


async def drain() -> None:
    """等后台的推送任务都跑完（测试用）。"""
    while _background:
        await asyncio.gather(*list(_background), return_exceptions=True)


# ---------------------------------------------------------------- 类别、样式与展示


def categories_view() -> list[dict[str, str]]:
    return [{"key": category.key, "label": category.label, "slot": category.slot} for category in CATEGORIES.values()]


def validate_style(category: str, style: Any) -> dict[str, Any]:
    """按类别校验 style，返回规范化之后的副本；不合法时是 422 invalid_style，detail.field 指出哪个字段。"""
    try:
        return CATEGORIES[category].validate(style)
    except StyleError as exc:
        raise _error(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_style", f"样式不合法（{exc.field}）：{exc.message}", field=exc.field) from exc


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _style(item: ShopItem) -> dict[str, Any]:
    try:
        style = json.loads(item.style or "{}")
    except ValueError:
        return {}
    return style if isinstance(style, dict) else {}


def _slot(item: ShopItem) -> str | None:
    category = CATEGORIES.get(item.category)
    return category.slot if category is not None else None


def _brief(item: ShopItem) -> dict[str, str]:
    return {"sku": item.sku, "name": item.name, "category": item.category}


def _available(item: ShopItem, now: datetime) -> bool:
    """现在还在卖：上架中，而且没过限时。"""
    until = _aware(item.available_until)
    return bool(item.is_active) and (until is None or until > now)


def _on_sale_clause(now: datetime) -> Any:
    return and_(ShopItem.is_active.is_(True), or_(ShopItem.available_until.is_(None), ShopItem.available_until > now))


def _obtainable(item: ShopItem, owned: bool, level: int, *, timed: bool = False, now: datetime | None = None) -> str:
    """我现在能怎么得到它。timed：我拥有的这一件是有到期时间的。"""
    free = item.unlock_level is not None and level >= item.unlock_level
    if owned:
        # 拥有的是限时的，而且现在还能买（或者等级够了能领）：可以再来一次，续期
        if timed and _available(item, now or _now()) and (free or item.price > 0):
            return "renew"
        return "owned"
    if free:
        return "level"
    return "buy" if item.price > 0 else "locked"


def _item_base(item: ShopItem) -> dict[str, Any]:
    return {
        "id": item.id,
        "sku": item.sku,
        "category": item.category,
        "name": item.name,
        "description": item.description or "",
        "price": item.price,
        "unlock_level": item.unlock_level,
        "style": _style(item),
        "preview_text": item.preview_text,
        "tag": item.tag,
        "available_until": _aware(item.available_until),
        "duration_days": item.duration_days,
        "is_active": bool(item.is_active),
    }


def _item_view(
    item: ShopItem, *, owned: bool, equipped: dict[str, str], level: int, expires_at: datetime | None = None, now: datetime | None = None
) -> dict[str, Any]:
    """expires_at：我拥有的这一件什么时候到期（永久或者没拥有为 None）。"""
    slot = _slot(item)
    expires_at = _aware(expires_at) if owned else None
    return {
        **_item_base(item),
        "owned": owned,
        "equipped": owned and slot is not None and equipped.get(slot) == item.sku,
        "obtainable": _obtainable(item, owned, level, timed=expires_at is not None, now=now),
        "expires_at": expires_at,
    }


def equipped_map(user: User) -> dict[str, str]:
    """每个槽装备的 sku，从 users.cosmetics 读。已经到期的限时商品不算装备着（parse_cosmetics 现场判断）。"""
    return {slot: entry["sku"] for slot, entry in load_cosmetics(user.cosmetics, _now()).items()}


def _set_cosmetics(user: User, cosmetics: dict[str, Any]) -> None:
    user.cosmetics = _dump(cosmetics) if cosmetics else None


# ---------------------------------------------------------------- 金币账户


def _interval() -> timedelta:
    return timedelta(seconds=settings.shop_refresh_interval_seconds)


def _shared_spent_query(user_id: Any, oid: Any) -> Any:
    """这个 CCW 账号的币被「别的」KukeChat 账号花掉了多少。

    一个 CCW 账号的累计投币只有一份：它解绑后再绑到另一个 KukeChat 账号上，不能把同一笔币再花一遍。
    """
    return select(func.coalesce(func.sum(ShopPurchase.price), 0)).where(
        ShopPurchase.ccw_oid == oid,
        ShopPurchase.user_id != user_id,
        ShopPurchase.source == "purchase",
        ShopPurchase.refunded.is_(False),
    )


def _packet_out_query(user_id: Any) -> Any:
    """这个人发红包净流出了多少：发出的整包金额 − 已经退回的。"""
    return select(func.coalesce(func.sum(RedPacket.total - RedPacket.refunded_amount), 0)).where(RedPacket.sender_id == user_id)


def _packet_in_query(user_id: Any) -> Any:
    """这个人抢到了多少：记在他名下的那些份的金额之和。"""
    return select(func.coalesce(func.sum(RedPacketShare.amount), 0)).where(RedPacketShare.user_id == user_id)


def _shared_packet_query(user_id: Any, oid: Any) -> Any:
    """这个 CCW 账号的币被「别的」KukeChat 账号用红包发出去了多少（净额）。和 _shared_spent_query 是同一条规则：

    不然可以绑定、把币发给小号、解绑、换一个 KukeChat 账号再绑同一个 CCW 账号，把同一笔投币再发一遍。
    那个账号发红包用的是不是它自己抢来的币，这里不区分，一律算在这个 CCW 账号头上 —— 只会少算余额，不会多算。
    """
    return select(func.coalesce(func.sum(RedPacket.total - RedPacket.refunded_amount), 0)).where(
        RedPacket.ccw_oid == oid, RedPacket.sender_id != user_id
    )


def _gift_out_query(user_id: Any) -> Any:
    """这个人送礼（礼物、商店商品）花掉了多少：他名下送礼记录的 amount 之和。送礼是真消耗，不会退。"""
    return select(func.coalesce(func.sum(GiftRecord.amount), 0)).where(GiftRecord.from_user_id == user_id)


def _shared_gift_query(user_id: Any, oid: Any) -> Any:
    """这个 CCW 账号的币被「别的」KukeChat 账号拿去送礼花掉了多少。和 _shared_spent_query / _shared_packet_query 是同一条规则：

    不然可以绑定、把币全送成礼物（刷心意值）、解绑、换一个 KukeChat 账号再绑同一个 CCW 账号，把同一笔投币再花一遍。
    送礼记录上记着送的时候绑定的 CCW 账号（gift_records.ccw_oid）。
    """
    return select(func.coalesce(func.sum(GiftRecord.amount), 0)).where(GiftRecord.ccw_oid == oid, GiftRecord.from_user_id != user_id)


def _group_plus_out_query(user_id: Any) -> Any:
    """这个人买群升级花掉了多少：他名下 group_plus_records 的 amount 之和（后台赠送 / 取消的 amount 是 0）。不退款。"""
    return select(func.coalesce(func.sum(GroupPlusRecord.amount), 0)).where(GroupPlusRecord.user_id == user_id)


def _shared_group_plus_query(user_id: Any, oid: Any) -> Any:
    """这个 CCW 账号的币被「别的」KukeChat 账号拿去买群升级花掉了多少。和 _shared_gift_query 是同一条规则（换绑防二次花）。"""
    return select(func.coalesce(func.sum(GroupPlusRecord.amount), 0)).where(
        GroupPlusRecord.ccw_oid == oid, GroupPlusRecord.user_id != user_id, GroupPlusRecord.amount > 0
    )


def _granted_query(user_id: Any) -> Any:
    """后台给这个人加 / 扣的金币净额（coin_grants.amount 之和，扣除是负数）。记在 KukeChat 账号上，不跟 CCW 账号走。"""
    return select(func.coalesce(func.sum(CoinGrant.amount), 0)).where(CoinGrant.user_id == user_id)


def available_expression(user_id: int, oid: str) -> Any:
    """余额的 SQL 表达式（对着 shop_accounts 里这个人的那一行算），扣款语句的条件用它：余额够才扣。

    口径和 _balance 一致；调用方另外要带上 ShopAccount.ccw_oid == oid（累计投币得是按现在绑定的账号核实的）。
    购买、发红包、送礼用的是同一个表达式，所以这几种花法之间也不会双花。
    """
    return (
        ShopAccount.donated
        - ShopAccount.spent
        - _shared_spent_query(user_id, oid).scalar_subquery()
        - _shared_packet_query(user_id, oid).scalar_subquery()
        - _shared_gift_query(user_id, oid).scalar_subquery()
        - _shared_group_plus_query(user_id, oid).scalar_subquery()
        - _packet_out_query(user_id).scalar_subquery()
        - _gift_out_query(user_id).scalar_subquery()
        - _group_plus_out_query(user_id).scalar_subquery()
        + _packet_in_query(user_id).scalar_subquery()
        + _granted_query(user_id).scalar_subquery()
    )


async def _ensure_account(db: AsyncSession, user_id: int) -> ShopAccount:
    account = await db.get(ShopAccount, user_id, populate_existing=True)
    if account is not None:
        return account
    async with _write_lock():
        db.add(ShopAccount(user_id=user_id))
        try:
            await db.commit()
        except IntegrityError:
            # 另一个请求刚好也建了这一行
            await db.rollback()
    return await db.get(ShopAccount, user_id, populate_existing=True)


async def _balance(db: AsyncSession, user_id: int, oid: str | None) -> dict[str, Any]:
    """契约里的 balance 对象。只读库，不向 CCW 发请求。"""
    account = await db.get(ShopAccount, user_id, populate_existing=True)
    spent = account.spent if account is not None else 0
    if oid is not None:
        spent += int(await db.scalar(_shared_spent_query(user_id, oid)) or 0)
        # 同一个 CCW 账号在别的 KukeChat 账号上用红包发出去的，也算在已花里
        spent += int(await db.scalar(_shared_packet_query(user_id, oid)) or 0)
        # 送礼同理
        spent += int(await db.scalar(_shared_gift_query(user_id, oid)) or 0)
        # 买群升级同理
        spent += int(await db.scalar(_shared_group_plus_query(user_id, oid)) or 0)
    gift_spent = int(await db.scalar(_gift_out_query(user_id)) or 0)
    group_plus_spent = int(await db.scalar(_group_plus_out_query(user_id)) or 0)
    sent, refunded = (
        await db.execute(
            select(func.coalesce(func.sum(RedPacket.total), 0), func.coalesce(func.sum(RedPacket.refunded_amount), 0)).where(RedPacket.sender_id == user_id)
        )
    ).one()
    sent, refunded = int(sent or 0), int(refunded or 0)
    received = int(await db.scalar(_packet_in_query(user_id)) or 0)
    granted = int(await db.scalar(_granted_query(user_id)) or 0)
    # 账户里的累计投币是按另一个 CCW 账号核实的（换绑了）：作废，等重新核实
    current = account is not None and oid is not None and account.ccw_oid == oid
    donated = account.donated if current else 0
    if oid is None:
        stale = False
    elif not get_coin_ledger().configured():
        stale = True
    else:
        stale = account is not None and not (current and account.verify_ok)
    return {
        "bound": oid is not None,
        "donated": donated,
        "spent": spent,
        "balance": donated - spent - gift_spent - group_plus_spent - sent + received + refunded + granted,
        "verified_at": _aware(account.verified_at) if current else None,
        "stale": stale,
        "red_packet_sent": sent,
        "red_packet_received": received,
        "red_packet_refunded": refunded,
        "gift_spent": gift_spent,
        "group_plus_spent": group_plus_spent,
        "granted": granted,
    }


async def balance_of(db: AsyncSession, user_id: int) -> dict[str, Any]:
    """按用户 id 取 balance 对象（没有请求里的 User 对象时用，比如红包到期退回之后给发送者发事件）。"""
    raw = await db.scalar(select(User.ccw_student_oid).where(User.id == user_id))
    return await _balance(db, user_id, (raw or "").strip().lower() or None)


async def _claim(db: AsyncSession, user_id: int, now: datetime) -> bool:
    """占一次向 CCW 核实的名额：每人每 shop_refresh_interval_seconds 秒一次。

    是一条带条件的 UPDATE，多个 worker 进程之间也只有一个能占到。自己提交，之后去请求 CCW 时不占着写事务。
    """
    async with _write_lock():
        claimed = await db.execute(
            update(ShopAccount)
            .where(ShopAccount.user_id == user_id, or_(ShopAccount.checked_at.is_(None), ShopAccount.checked_at <= now - _interval()))
            .values(checked_at=now)
            .execution_options(synchronize_session=False)
        )
        await db.commit()
    return claimed.rowcount == 1


async def _verify_once(user_id: int, oid: str) -> str:
    """向 CCW 核实一次并把结果写进账户。返回 "ok" / "failed"（核实不了）/ "limited"（这个人刚核实过，没轮到）。

    用自己的会话：它可能被好几个请求同时等着，不能绑在其中某一个请求的会话上。
    """
    async with AsyncSessionLocal() as db:
        if not await _claim(db, user_id, _now()):
            return "limited"
        try:
            donated = await get_coin_ledger().credited_to(oid)
        except CoinsUnavailable as exc:
            # 失败只记状态，上次核实到的数原样留着
            values: dict[str, Any] = {"verify_ok": False, "last_error": str(exc)[:200], "updated_at": _now()}
        else:
            now = _now()
            previous = (await db.execute(select(ShopAccount.ccw_oid, ShopAccount.donated).where(ShopAccount.user_id == user_id))).first()
            if previous is not None and previous[0] == oid and donated < previous[1]:
                # 累计投币按理只增不减。仍以余额来源为准，但留个痕迹方便排查是不是那边变了
                logger.warning("CCW donated amount decreased", extra={"user_id": user_id, "before": previous[1], "after": donated})
            values = {"ccw_oid": oid, "donated": donated, "verified_at": now, "verify_ok": True, "last_error": None, "updated_at": now}
        async with _write_lock():
            await db.execute(update(ShopAccount).where(ShopAccount.user_id == user_id).values(**values).execution_options(synchronize_session=False))
            await db.commit()
        return "ok" if values["verify_ok"] else "failed"


def _verification(user_id: int, oid: str) -> asyncio.Future[str]:
    """这个人正在进行的那次核实；没有就开始一次。

    「查有没有」和「开始」之间没有 await，所以同一个进程里同时来的请求（连点两下购买）一定落在同一次核实上，
    不会有一个被限频挡回去。别的进程里的请求靠 _claim 的那条 UPDATE 挡。
    """
    key = (user_id, oid)
    flight = _verifying.get(key)
    if flight is None or flight.done() or flight.get_loop() is not asyncio.get_running_loop():
        flight = asyncio.ensure_future(_verify_once(user_id, oid))
        _verifying[key] = flight

        def finished(done: asyncio.Future[str]) -> None:
            if _verifying.get(key) is done:
                _verifying.pop(key, None)
            # 等它的请求都被取消了也把异常取走
            if not done.cancelled():
                done.exception()

        flight.add_done_callback(finished)
    return flight


def _recently_verified(account: ShopAccount | None, oid: str, now: datetime) -> bool:
    if account is None:
        return False
    verified_at = _aware(account.verified_at)
    return bool(account.verify_ok) and account.ccw_oid == oid and verified_at is not None and now - verified_at < _interval()


async def _fresh_account(db: AsyncSession, user_id: int, oid: str) -> None:
    """购买前：保证账户里的累计投币是刚向 CCW 核实过的。做不到就 503，不扣币。"""
    if not get_coin_ledger().configured():
        raise _unavailable()
    if _recently_verified(await _ensure_account(db, user_id), oid, _now()):
        return
    # 等 CCW 可能要好几秒：先结束这个会话的事务把数据库连接还回去，别占着连接池干等
    await db.commit()
    # shield：这个请求被取消（客户端断开）不该连带取消别的请求也在等的那次核实
    await asyncio.shield(_verification(user_id, oid))
    # 不管这次是自己问的、搭了别人的车，还是被限频挡了（刚有别的请求问过）：只认账户里「刚刚核实成功」这个结果。
    # 刚核实失败过的话不追着 CCW 再问，直接 503
    if not _recently_verified(await db.get(ShopAccount, user_id, populate_existing=True), oid, _now()):
        raise _unavailable()


async def refresh_balance(db: AsyncSession, user: User) -> dict[str, Any]:
    """POST /shop/balance/refresh：向 CCW 重新核实一次累计投币。核实不了不报错，返回上次的数并标 stale。"""
    user_id, oid = user.id, _bound_oid(user)
    if oid is None:
        raise _not_bound()
    if not get_coin_ledger().configured():
        return await _balance(db, user_id, oid)
    await _ensure_account(db, user_id)
    now = _now()
    # 等 CCW 可能要好几秒：先结束这个会话的事务把数据库连接还回去，别占着连接池干等
    await db.commit()
    if await asyncio.shield(_verification(user_id, oid)) == "limited":
        checked_at = _aware(await db.scalar(select(ShopAccount.checked_at).where(ShopAccount.user_id == user_id)))
        elapsed = (now - checked_at).total_seconds() if checked_at is not None else 0.0
        retry_after = max(1, math.ceil(settings.shop_refresh_interval_seconds - elapsed))
        raise _error(status.HTTP_429_TOO_MANY_REQUESTS, "refresh_too_fast", f"刷新得太快了，{retry_after} 秒后再试", retry_after=retry_after)
    return await _balance(db, user_id, oid)


# ---------------------------------------------------------------- 拥有（含限时）


class _PermanentlyOwned(Exception):
    """这个人已经永久拥有这件商品了，不能再得到一份。"""


class _OwnershipChanged(Exception):
    """要接替的那一行刚被别的事务动过（并发的续期 / 收回）：整个事务回滚后重来。"""


def _live_clause() -> Any:
    """「当前的那一行」：没被收回，也没被后来的记录接替。每个人每件商品最多一行（唯一索引）。"""
    return and_(ShopPurchase.revoked_at.is_(None), ShopPurchase.superseded_at.is_(None))


def _owned_clause(now: datetime) -> Any:
    """现在拥有：当前的那一行，而且没到期。"""
    return and_(_live_clause(), or_(ShopPurchase.expires_at.is_(None), ShopPurchase.expires_at > now))


async def _owned_map(db: AsyncSession, user_id: int, now: datetime) -> dict[int, datetime | None]:
    """这个人现在拥有的商品：{ 商品 id: 到期时间（永久为 None） }。到期是现场判断的。"""
    rows = await db.execute(select(ShopPurchase.item_id, ShopPurchase.expires_at).where(ShopPurchase.user_id == user_id, _owned_clause(now)))
    return {item_id: _aware(expires_at) for item_id, expires_at in rows.all()}


async def owns_permanently(db: AsyncSession, user_id: int, item_id: int) -> bool:
    """这个人是不是已经**永久**拥有这件商品（送礼时「对方已拥有」只挡这一种，限时的再送是续期）。"""
    found = await db.scalar(
        select(ShopPurchase.id)
        .where(ShopPurchase.user_id == user_id, ShopPurchase.item_id == item_id, _live_clause(), ShopPurchase.expires_at.is_(None))
        .limit(1)
    )
    return found is not None


def _iso(value: datetime | None) -> str | None:
    value = _aware(value)
    return value.isoformat() if value is not None else None


async def _refresh_worn_expiry(db: AsyncSession, user_id: int, sku: str, expires_at: datetime | None, now: datetime) -> None:
    """这个人这件商品的到期时间变了（续期、换成永久）：正装备着它的话，把 users.cosmetics 里记的到期时间改成新的。调用方提交。"""
    raw = await db.scalar(select(User.cosmetics).where(User.id == user_id))
    cosmetics = load_cosmetics(raw, now)
    changed = False
    for entry in cosmetics.values():
        if entry["sku"] != sku or entry.get("expires_at") == _iso(expires_at):
            continue
        if expires_at is None:
            entry.pop("expires_at", None)
        else:
            entry["expires_at"] = _iso(expires_at)
        changed = True
    if changed:
        # updated_at 保持原样：它是「资料改动时间」，不该因为续期而变
        await db.execute(
            update(User).where(User.id == user_id).values(cosmetics=_dump(cosmetics), updated_at=User.updated_at).execution_options(synchronize_session=False)
        )


async def _grant_ownership(
    db: AsyncSession,
    *,
    user_id: int,
    item_id: int,
    sku: str,
    days: int | None,
    now: datetime,
    source: str,
    price: int = 0,
    ccw_oid: str | None = None,
    actor_id: int | None = None,
    note: str | None = None,
    from_user_id: int | None = None,
    gift_record_id: int | None = None,
) -> ShopPurchase:
    """在调用方的事务里给这个人记一份拥有（买的、等级解锁、后台赠送、别人送的都走这里），不提交。days 为 None 是永久。

    - 没有当前记录：写一行，限时的到期时间 = 现在 + days。
    - 当前记录是限时的：把它标成被接替，写一行新的。原来那一行还没到期、这次也是限时的，就在**原到期时间**上加 days（续期）；
      已经到期了从现在起算；这次是永久的，新的一行就是永久。
    - 当前记录是永久的：抛 _PermanentlyOwned。

    并发：接替是一条带条件的 UPDATE（那一行必须还是当前记录），抢不到抛 _OwnershipChanged；两个事务同时给一个原本没有的人写第一行，
    后提交的那个撞唯一索引（IntegrityError）。调用方两种都是整个事务回滚后重来 —— 重来时读到的是对方提交之后的到期时间，
    所以同时续期的两次都会加上，不会丢天数。花币的路径上扣款那条 UPDATE 在前，事务从那里起就拿着写锁，根本走不到重来。
    """
    live = (
        await db.execute(
            select(ShopPurchase.id, ShopPurchase.expires_at).where(ShopPurchase.user_id == user_id, ShopPurchase.item_id == item_id, _live_clause())
        )
    ).first()
    base = now
    if live is not None:
        until = _aware(live[1])
        if until is None:
            raise _PermanentlyOwned()
        if days is not None and until > now:
            base = until
        replaced = await db.execute(
            update(ShopPurchase).where(ShopPurchase.id == live[0], _live_clause()).values(superseded_at=now).execution_options(synchronize_session=False)
        )
        if replaced.rowcount != 1:
            raise _OwnershipChanged()
    expires_at = base + timedelta(days=days) if days is not None else None
    row = ShopPurchase(
        user_id=user_id,
        item_id=item_id,
        price=price,
        source=source,
        ccw_oid=ccw_oid,
        actor_id=actor_id,
        note=note,
        from_user_id=from_user_id,
        gift_record_id=gift_record_id,
        created_at=now,
        expires_at=expires_at,
        duration_days=days,
        # 一得到就已经在「到期前 3 天」以内的（比如只有 1 天的体验装）不再另发快到期的提醒
        expiring_notified_at=now if expires_at is not None and expires_at - now <= EXPIRING_BEFORE else None,
    )
    db.add(row)
    await db.flush()
    await _refresh_worn_expiry(db, user_id, sku, expires_at, now)
    return row


# ---------------------------------------------------------------- 逛、买、装备


async def overview(db: AsyncSession, user: User) -> dict[str, Any]:
    """GET /shop。items 是上架中的商品，加上我已经拥有的（哪怕已下架）。到期了的限时商品不算拥有。"""
    user_id, oid, level, equipped = user.id, _bound_oid(user), user_level(user), equipped_map(user)
    now = _now()
    owned = await _owned_map(db, user_id, now)
    clause = _on_sale_clause(now)
    if owned:
        clause = or_(clause, ShopItem.id.in_(owned))
    items = (await db.execute(select(ShopItem).where(clause).order_by(ShopItem.sort.asc(), ShopItem.id.asc()))).scalars().all()
    return {
        "categories": categories_view(),
        "items": [_item_view(item, owned=item.id in owned, equipped=equipped, level=level, expires_at=owned.get(item.id), now=now) for item in items],
        "balance": await _balance(db, user_id, oid),
        "equipped": equipped,
        "donate_url": get_coin_ledger().topup_url(),
        "level": level,
    }


async def purchase(db: AsyncSession, user: User, item_id: int) -> dict[str, Any]:
    """POST /shop/items/{id}/purchase：买一件，或者免费领一件等级已经够了的。

    限时商品（duration_days）：没拥有（或者到期了）是从现在起算 N 天；已拥有没到期是在原到期时间上再加 N 天（续期）。
    永久拥有着的重复购买是 409 already_owned。
    """
    user_id, oid, level, equipped = user.id, _bound_oid(user), user_level(user), equipped_map(user)
    item = await db.get(ShopItem, item_id)
    if item is None:
        raise _item_not_found()
    if await owns_permanently(db, user_id, item_id):
        raise _already_owned()
    if not _available(item, _now()):
        raise _error(status.HTTP_409_CONFLICT, "item_unavailable", "这件商品已下架或已过期")
    if item.unlock_level is not None and level >= item.unlock_level:
        # 等级够了：免费领，不看金币，也不要求绑定 CCW
        source, price = "level", 0
    else:
        if item.price <= 0:
            raise _error(status.HTTP_409_CONFLICT, "item_locked", "这件商品不出售，账号等级达到要求后解锁")
        if oid is None:
            raise _not_bound()
        await _fresh_account(db, user_id, oid)
        # 核实可能花了几秒，这期间商品可能被改价或下架：按最新的来
        await db.refresh(item)
        if not _available(item, _now()) or item.price <= 0:
            raise _error(status.HTTP_409_CONFLICT, "item_unavailable", "这件商品已下架或已过期")
        source, price = "purchase", item.price
    # 下面的事务可能回滚，回滚之后 item 的属性不能再读：先取成普通变量
    sku, days = item.sku, item.duration_days

    outcome = "retry"
    expires_at: datetime | None = None
    for _attempt in range(OWNERSHIP_ATTEMPTS):
        now = _now()
        outcome = "ok"
        async with _write_lock():
            try:
                if source == "purchase":
                    # 余额够才扣。单条语句在数据库里是原子的，写事务又是串行的：并发的两次购买，后一个看到的是前一个扣完之后的余额
                    charged = await db.execute(
                        update(ShopAccount)
                        .where(
                            ShopAccount.user_id == user_id,
                            ShopAccount.ccw_oid == oid,
                            available_expression(user_id, oid) >= price,
                        )
                        .values(spent=ShopAccount.spent + price, updated_at=now)
                        .execution_options(synchronize_session=False)
                    )
                    if charged.rowcount != 1:
                        await db.rollback()
                        outcome = "short"
                if outcome == "ok":
                    row = await _grant_ownership(
                        db, user_id=user_id, item_id=item_id, sku=sku, days=days, now=now, source=source, price=price, ccw_oid=oid if source == "purchase" else None
                    )
                    expires_at = row.expires_at
                    # 发货和上面的扣款一起提交
                    await db.commit()
            except _PermanentlyOwned:
                # 同一件永久商品的另一次购买抢先了。回滚会把这次的扣款一起撤掉
                await db.rollback()
                outcome = "owned"
            except (IntegrityError, _OwnershipChanged):
                # 同一件商品的另一次购买 / 续期抢先了（唯一索引，或者要接替的那一行已经被接替）：回滚，按最新的状态重来
                await db.rollback()
                outcome = "retry"
            except BaseException:
                await db.rollback()
                raise
        if outcome != "retry":
            break
    if outcome in ("owned", "retry"):
        raise _already_owned()
    if outcome == "short":
        balance = (await _balance(db, user_id, oid))["balance"]
        raise _error(
            status.HTTP_402_PAYMENT_REQUIRED,
            "insufficient_coins",
            "金币不够",
            balance=balance,
            price=price,
            shortfall=max(price - balance, 0),
            donate_url=get_coin_ledger().topup_url(),
        )
    # 中途回滚过的话 item 已经过期，重新读出来
    await db.refresh(item)
    return {
        "item": _item_view(item, owned=True, equipped=equipped, level=level, expires_at=expires_at),
        "balance": await _balance(db, user_id, oid),
        "equipped": equipped,
    }


async def set_equipped(db: AsyncSession, user: User, slot: str, sku: str | None) -> dict[str, Any]:
    """PUT /shop/equipped：装备自己拥有的一件，sku 为 None 表示卸下。装备有变化时发 user.cosmetics.updated。"""
    if slot not in SLOTS:
        raise _error(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_slot", "没有这个装备槽")
    now = _now()
    # 到期了的限时商品在这里就已经不在了；下面只要有改动，写回去的也是去掉它们之后的样子
    cosmetics = load_cosmetics(user.cosmetics, now)
    if sku is None:
        changed = cosmetics.pop(slot, None) is not None
    else:
        item = (await db.execute(select(ShopItem).where(ShopItem.sku == sku))).scalar_one_or_none()
        if item is None:
            raise _item_not_found()
        if _slot(item) != slot:
            raise _error(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_slot", "这件商品不能装在这个槽里")
        # 下架不影响已经拥有的人，所以这里不看商品是否还在卖。到期了的限时商品不算拥有
        owned = await _owned_map(db, user.id, now)
        if item.id not in owned:
            raise _error(status.HTTP_403_FORBIDDEN, "not_owned", "只能装备自己拥有的商品")
        entry: dict[str, Any] = {"sku": item.sku, "style": _style(item)}
        if owned[item.id] is not None:
            # 限时商品：到期时间和样式一起记在 users.cosmetics 里，序列化用户时不查库也知道它到没到期
            entry["expires_at"] = _iso(owned[item.id])
        changed = cosmetics.get(slot) != entry
        cosmetics[slot] = entry
    if changed:
        _set_cosmetics(user, cosmetics)
        async with _write_lock():
            await db.commit()
        announce_cosmetics(user.id)
    return {"equipped": {key: entry["sku"] for key, entry in cosmetics.items()}}


def _shown_expiry(row: ShopPurchase) -> datetime | None:
    """这一行记录对外展示的到期时间。被接替的行（续期之前的那几行）到接替的那一刻为止：

    它剩下的时间已经并进接替它的那一行了，再把原来的到期时间给出去，看起来就像同一件商品同时有好几份没到期的。
    """
    expires_at, superseded_at = _aware(row.expires_at), _aware(row.superseded_at)
    if expires_at is not None and superseded_at is not None:
        return min(expires_at, superseded_at)
    return expires_at if superseded_at is None else superseded_at


def _purchase_view(row: ShopPurchase, item: ShopItem, from_user: User | None = None) -> dict[str, Any]:
    return {
        "id": row.id,
        "item": _brief(item),
        "price": row.price,
        "source": row.source,
        "created_at": _aware(row.created_at),
        "revoked_at": _aware(row.revoked_at),
        "refunded": bool(row.refunded),
        "expires_at": _shown_expiry(row),
        "duration_days": row.duration_days,
        "from_user": UserPublic.model_validate(from_user) if from_user is not None else None,
    }


async def _givers(db: AsyncSession, rows: list[ShopPurchase]) -> dict[int, User]:
    """别人送的那几行（source = gift）是谁送的。"""
    giver_ids = {row.from_user_id for row in rows if row.from_user_id is not None}
    if not giver_ids:
        return {}
    users = await db.execute(select(User).where(User.id.in_(giver_ids)))
    return {user.id: user for user in users.scalars().all()}


async def list_purchases(db: AsyncSession, user_id: int, limit: int, offset: int) -> dict[str, Any]:
    """GET /shop/purchases：我的获得记录，新的在前。被收回的也在里面（带 revoked_at）。"""
    total = await db.scalar(select(func.count()).select_from(ShopPurchase).where(ShopPurchase.user_id == user_id))
    rows = (
        await db.execute(
            select(ShopPurchase, ShopItem)
            .join(ShopItem, ShopItem.id == ShopPurchase.item_id)
            .where(ShopPurchase.user_id == user_id)
            .order_by(ShopPurchase.created_at.desc(), ShopPurchase.id.desc())
            .offset(offset)
            .limit(limit)
        )
    ).all()
    givers = await _givers(db, [row for row, _item in rows])
    return {"items": [_purchase_view(row, item, givers.get(row.from_user_id)) for row, item in rows], "total": int(total or 0)}


# ---------------------------------------------------------------- 等级解锁


async def on_level_up(db: AsyncSession, user_id: int, level: int) -> list[dict[str, str]]:
    """账号升到 level 级之后调用：把到这一级为止解锁、还没得到过的商品发给用户，并发 shop.item.granted。

    返回这次发了哪些（[{sku, name, category}]）。要在调用方提交了自己的事务之后调：这里会自己提交，出错时会回滚这个会话。
    发不出去只记日志、返回已经发成的那些 —— 不能因为它让升级失败；漏发的商品用户还能在商店里手动领（obtainable = level）。
    被后台收回过的不会再自动发；限时的（按商品的 duration_days 计期）到期之后也不会再自动发，要用户自己去商店再领。
    """
    granted: list[dict[str, str]] = []
    try:
        held = select(ShopPurchase.item_id).where(ShopPurchase.user_id == user_id)
        rows = await db.execute(
            select(ShopItem.id, ShopItem.sku, ShopItem.name, ShopItem.category, ShopItem.duration_days)
            .where(ShopItem.unlock_level.is_not(None), ShopItem.unlock_level <= level, _on_sale_clause(_now()), ShopItem.id.not_in(held))
            .order_by(ShopItem.unlock_level.asc(), ShopItem.sort.asc(), ShopItem.id.asc())
        )
        for item_id, sku, name, category, days in rows.all():
            async with _write_lock():
                try:
                    await _grant_ownership(db, user_id=user_id, item_id=item_id, sku=sku, days=days, now=_now(), source="level")
                    await db.commit()
                except (IntegrityError, _PermanentlyOwned, _OwnershipChanged):
                    # 用户刚好自己领了这一件
                    await db.rollback()
                    continue
            granted.append({"sku": sku, "name": name, "category": category})
    except Exception:
        logger.exception("Failed to grant level unlocks", extra={"user_id": user_id, "level": level})
        try:
            await db.rollback()
        except Exception:
            pass
    for item in granted:
        await _send_granted(user_id, item, "level")
    return granted


# ---------------------------------------------------------------- 实时事件


async def _send_granted(user_id: int, item: dict[str, str], source: str) -> None:
    """shop.item.granted 发给本人。发不出去只记日志。"""
    from app.websocket.manager import manager

    try:
        await manager.send_to_user(user_id, GRANTED_EVENT, {"item": item, "source": source})
    except Exception:
        logger.exception("Failed to send shop grant event", extra={"user_id": user_id, "sku": item.get("sku")})


async def _send_balance(user_id: int, change: str, extra: dict[str, Any]) -> None:
    reason = change.split(".", 1)[0]
    from app.websocket.manager import manager

    try:
        # 用自己的会话读最新的余额，不占用请求的；发的是这一刻库里的样子
        async with AsyncSessionLocal() as db:
            balance = await balance_of(db, user_id)
        await manager.send_to_user(user_id, BALANCE_EVENT, {"reason": reason, "change": change, "balance": balance, **extra})
    except Exception:
        # 推送是尽力而为：钱已经落库，接口也返回了最新的余额，这里只记日志
        logger.exception("Failed to send balance change", extra={"user_id": user_id, "change": change})


def announce_balance(user_id: int, change: str, **extra: Any) -> None:
    """shop.balance.changed：这个人的余额变了，只发给他本人（他别的设备也能跟着变）。要在改动提交之后调。

    data 是 { reason, change, balance, ... }：reason 是来源（change 里第一个点之前的部分），change 是具体哪一种，
    balance 是完整的 balance 对象。红包：reason = "red_packet"，change = red_packet.sent / red_packet.claimed / red_packet.refunded，
    带 red_packet_id。送礼：reason = "gift"，change = gift.sent，带 gift_id。
    """
    _spawn(_send_balance(user_id, change, extra))


async def _broadcast_cosmetics(user_id: int) -> None:
    from app.websocket.manager import manager

    try:
        while user_id in _cosmetics_dirty:
            sent_at = _cosmetics_sent_at.get(user_id)
            if sent_at is not None:
                wait = sent_at + settings.shop_cosmetics_event_interval_seconds - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
            # 先摘掉标记再读库：读库之后才到的变化会让这个循环再转一圈，不会丢
            _cosmetics_dirty.discard(user_id)
            now = time.monotonic()
            _cosmetics_sent_at[user_id] = now
            if len(_cosmetics_sent_at) > _COSMETICS_SWEEP_SIZE:
                cutoff = now - settings.shop_cosmetics_event_interval_seconds
                for stale_id in [key for key, stamp in _cosmetics_sent_at.items() if stamp < cutoff]:
                    _cosmetics_sent_at.pop(stale_id, None)
            try:
                # 用自己的会话读最新的状态，不占用请求的；发的永远是这一刻库里的样子
                async with AsyncSessionLocal() as db:
                    raw = await db.scalar(select(User.cosmetics).where(User.id == user_id))
                await manager.broadcast(COSMETICS_EVENT, {"user_id": user_id, "cosmetics": parse_cosmetics(raw)})
            except Exception:
                # 推送是尽力而为：装备已经落库，接口也早就返回了，这里只记日志
                logger.exception("Failed to broadcast cosmetics change", extra={"user_id": user_id})
    finally:
        if _cosmetics_pending.get(user_id) is asyncio.current_task():
            _cosmetics_pending.pop(user_id, None)


def announce_cosmetics(user_id: int) -> None:
    """user.cosmetics.updated（契约 2.3）：这个人的装扮变了，发给所有在线用户。要在改动提交之后调。

    同一个用户 shop_cosmetics_event_interval_seconds 秒内最多发一次：距上次不到这么久就等到间隔结束再发，
    期间更多的变化并进这一次，发的是那时库里最后的状态。平时（距上次够久）是立刻发的，不会让人等。
    每个用户同时只有一个任务在发，所以先后两次变化的事件不会前后颠倒。每个进程各自合并。
    """
    _cosmetics_dirty.add(user_id)
    pending = _cosmetics_pending.get(user_id)
    if pending is not None and not pending.done() and pending.get_loop() is asyncio.get_running_loop():
        return
    _cosmetics_pending[user_id] = _spawn(_broadcast_cosmetics(user_id))


async def _broadcast_changed() -> None:
    global _changed_sent_at
    from app.websocket.manager import manager

    if _changed_sent_at is not None:
        wait = _changed_sent_at + settings.shop_changed_interval_seconds - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
    _changed_sent_at = time.monotonic()
    try:
        await manager.broadcast(CHANGED_EVENT, {})
    except Exception:
        logger.exception("Failed to broadcast shop change")


def announce_catalog_change() -> None:
    """shop.changed 发给所有在线用户，同一进程 shop_changed_interval_seconds 秒内最多一次。

    间隔内再有变化不会丢：排一次到间隔结束时补发，期间更多的变化都并进这一次。
    """
    global _changed_task
    pending = _changed_task
    if pending is not None and not pending.done() and pending.get_loop() is asyncio.get_running_loop():
        return
    _changed_task = _spawn(_broadcast_changed())


# ---------------------------------------------------------------- 后台：商品


def _invalid_item(field: str, message: str) -> HTTPException:
    return _error(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_item", message, field=field)


def _clean_text(value: Any, field: str, label: str, maximum: int, *, required: bool) -> str | None:
    text = (value or "").strip() if isinstance(value, str) or value is None else None
    if text is None or len(text) > maximum or (required and not text):
        raise _invalid_item(field, f"{label}{'不能为空，' if required else ''}最多 {maximum} 个字")
    return text or None


def _clean_int(value: Any, field: str, label: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise _invalid_item(field, f"{label}要在 {low}–{high} 之间")
    return value


def _clean_item(data: dict[str, Any], item: ShopItem | None) -> dict[str, Any]:
    """校验后台提交的商品字段，返回要写进库里的值。item 为 None 是新建，否则是修改（只处理传了的字段）。

    name / category / price / style / is_active / sort 不可为空，传 null 等于没传；
    unlock_level / preview_text / tag / available_until / description / duration_days 传 null 表示清空（duration_days 清空 = 永久）。
    """
    creating = item is None
    values: dict[str, Any] = {}
    sku = data.get("sku")
    if creating:
        if not isinstance(sku, str) or len(sku) > SKU_MAX or SKU_RE.fullmatch(sku) is None:
            raise _invalid_item("sku", f"sku 只能用小写字母、数字和 . _ -（不能开头结尾、不能连着用），最多 {SKU_MAX} 个字符")
        values["sku"] = sku
    elif sku is not None and sku != item.sku:
        raise _error(status.HTTP_422_UNPROCESSABLE_ENTITY, "sku_immutable", "sku 建好之后不能改", field="sku")

    category = data.get("category")
    if category is not None or creating:
        if category not in CATEGORIES:
            raise _invalid_item("category", f"类别只能是 {' / '.join(CATEGORIES)}")
        values["category"] = category
    if data.get("name") is not None or creating:
        values["name"] = _clean_text(data.get("name"), "name", "名称", NAME_MAX, required=True)
    if "description" in data:
        values["description"] = _clean_text(data["description"], "description", "描述", DESCRIPTION_MAX, required=False) or ""
    if data.get("price") is not None:
        values["price"] = _clean_int(data["price"], "price", "价格", 0, PRICE_MAX)
    if "unlock_level" in data:
        level = data["unlock_level"]
        values["unlock_level"] = None if level is None else _clean_int(level, "unlock_level", "解锁等级", 1, UNLOCK_LEVEL_MAX)
    if "preview_text" in data:
        values["preview_text"] = _clean_text(data["preview_text"], "preview_text", "预览文字", PREVIEW_TEXT_MAX, required=False)
    if "tag" in data:
        tag = data["tag"] or None
        if tag is not None and tag not in TAGS:
            raise _invalid_item("tag", f"标签只能是 {' / '.join(TAGS)}，或者留空")
        values["tag"] = tag
    if data.get("is_active") is not None:
        values["is_active"] = bool(data["is_active"])
    if data.get("sort") is not None:
        values["sort"] = _clean_int(data["sort"], "sort", "排序", -SORT_MAX, SORT_MAX)
    if "available_until" in data:
        values["available_until"] = _aware(data["available_until"])
    if "duration_days" in data:
        # null 是永久。只影响之后的购买 / 赠送，已有的拥有记录不动
        days = data["duration_days"]
        values["duration_days"] = None if days is None else _clean_int(days, "duration_days", "有效期（天）", 1, DURATION_MAX)

    # style 跟着类别校验；只改类别不改样式时，旧样式也得符合新类别的规则
    style = data.get("style")
    target_category = values.get("category") or (item.category if item is not None else None)
    if style is not None or creating:
        if style is None:
            raise _error(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_style", "样式不能为空", field="style")
        values["style"] = _dump(validate_style(target_category, style))
    elif "category" in values and values["category"] != item.category:
        values["style"] = _dump(validate_style(target_category, _style(item)))
    return values


async def _sync_equipped(db: AsyncSession, item: ShopItem) -> list[int]:
    """商品的样式（或类别）变了：把装备着它的人 users.cosmetics 里的那份改成最新的。返回受影响的用户 id，调用方提交。"""
    owners = await db.execute(
        select(User)
        .join(ShopPurchase, ShopPurchase.user_id == User.id)
        .where(ShopPurchase.item_id == item.id, _live_clause(), User.cosmetics.is_not(None))
    )
    slot, style = _slot(item), _style(item)
    affected: list[int] = []
    now = _now()
    for user in owners.scalars().all():
        # load_cosmetics 保留限时商品的到期时间；已经到期的那几件在这里就去掉了
        cosmetics = load_cosmetics(user.cosmetics, now)
        changed = False
        for key in list(cosmetics):
            if cosmetics[key]["sku"] != item.sku:
                continue
            if key != slot:
                # 类别换了，原来的槽里不能再放它
                del cosmetics[key]
                changed = True
            elif cosmetics[key]["style"] != style:
                cosmetics[key] = {**cosmetics[key], "style": style}
                changed = True
        if changed:
            _set_cosmetics(user, cosmetics)
            affected.append(user.id)
    return affected


def _admin_item_view(item: ShopItem, sold: int = 0, owners: int = 0) -> dict[str, Any]:
    return {
        **_item_base(item),
        "sort": item.sort,
        "sold": sold,
        "owners": owners,
        "created_at": _aware(item.created_at),
        "updated_at": _aware(item.updated_at),
    }


async def _item_counts(db: AsyncSession, item_ids: list[int] | None = None) -> tuple[dict[int, int], dict[int, int]]:
    """(每件商品卖出去多少份, 每件商品现在有多少人拥有)。卖出只算花金币买的，退款的不算（限时商品每续一次算一份）；拥有不算已经到期的。"""
    sold_query = select(ShopPurchase.item_id, func.count()).where(ShopPurchase.source == "purchase", ShopPurchase.refunded.is_(False))
    owners_query = select(ShopPurchase.item_id, func.count()).where(_owned_clause(_now()))
    if item_ids is not None:
        sold_query = sold_query.where(ShopPurchase.item_id.in_(item_ids))
        owners_query = owners_query.where(ShopPurchase.item_id.in_(item_ids))
    sold = {item_id: int(count) for item_id, count in (await db.execute(sold_query.group_by(ShopPurchase.item_id))).all()}
    owners = {item_id: int(count) for item_id, count in (await db.execute(owners_query.group_by(ShopPurchase.item_id))).all()}
    return sold, owners


async def admin_list_items(db: AsyncSession) -> dict[str, Any]:
    """GET /admin/shop/items：全部商品（含下架的），每项带已售数量。"""
    items = (await db.execute(select(ShopItem).order_by(ShopItem.sort.asc(), ShopItem.id.asc()))).scalars().all()
    sold, owners = await _item_counts(db)
    return {"items": [_admin_item_view(item, sold.get(item.id, 0), owners.get(item.id, 0)) for item in items], "total": len(items)}


async def admin_create_item(db: AsyncSession, admin: User, data: dict[str, Any]) -> dict[str, Any]:
    admin_id = admin.id
    values = _clean_item(data, None)
    now = _now()
    item = ShopItem(**values, created_at=now, updated_at=now)
    db.add(item)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise _error(status.HTTP_409_CONFLICT, "sku_exists", "这个 sku 已经有了", field="sku") from None
    await log_admin_action(db, admin_id, "shop.item.create", "shop_item", item.id, {"sku": item.sku, "name": item.name, "price": item.price})
    await db.commit()
    announce_catalog_change()
    return _admin_item_view(item)


async def admin_update_item(db: AsyncSession, admin: User, item_id: int, data: dict[str, Any]) -> dict[str, Any]:
    admin_id = admin.id
    item = await db.get(ShopItem, item_id)
    if item is None:
        raise _item_not_found()
    values = _clean_item(data, item)
    changed = sorted(key for key, value in values.items() if _comparable(getattr(item, key)) != _comparable(value))
    affected: list[int] = []
    if changed:
        for key in changed:
            setattr(item, key, values[key])
        item.updated_at = _now()
        if "style" in changed or "category" in changed:
            affected = await _sync_equipped(db, item)
        await log_admin_action(db, admin_id, "shop.item.update", "shop_item", item.id, {"sku": item.sku, "changed": changed, "equipped_users": len(affected)})
        await db.commit()
        announce_catalog_change()
        for user_id in affected:
            announce_cosmetics(user_id)
    sold, owners = await _item_counts(db, [item.id])
    return _admin_item_view(item, sold.get(item.id, 0), owners.get(item.id, 0))


def _comparable(value: Any) -> Any:
    return _aware(value) if isinstance(value, datetime) else value


# ---------------------------------------------------------------- 后台：记录、赠送与收回


def _admin_user_view(user: User | None) -> dict[str, Any] | None:
    if user is None:
        return None
    return {"id": user.id, "username": user.username, "nickname": user.nickname, "avatar_url": user.avatar_url}


def _admin_purchase_view(row: ShopPurchase, item: ShopItem, user: User | None, from_user: User | None = None) -> dict[str, Any]:
    return {
        **_purchase_view(row, item),
        "user": _admin_user_view(user),
        "item_id": row.item_id,
        "note": row.note,
        "actor_id": row.actor_id,
        "revoked_by_id": row.revoked_by_id,
        "revoke_note": row.revoke_note,
        # 别的用户送的（source = gift）：谁送的
        "from_user": _admin_user_view(from_user),
        "superseded_at": _aware(row.superseded_at),
    }


async def admin_list_purchases(db: AsyncSession, *, user_id: int | None, item_id: int | None, limit: int, offset: int) -> dict[str, Any]:
    """GET /admin/shop/purchases：全站的获得记录（含被收回的、续期前的），新的在前。"""
    clauses = []
    if user_id is not None:
        clauses.append(ShopPurchase.user_id == user_id)
    if item_id is not None:
        clauses.append(ShopPurchase.item_id == item_id)
    total = await db.scalar(select(func.count()).select_from(ShopPurchase).where(*clauses))
    rows = (
        await db.execute(
            select(ShopPurchase, ShopItem, User)
            .join(ShopItem, ShopItem.id == ShopPurchase.item_id)
            .outerjoin(User, User.id == ShopPurchase.user_id)
            .where(*clauses)
            .order_by(ShopPurchase.created_at.desc(), ShopPurchase.id.desc())
            .offset(offset)
            .limit(limit)
        )
    ).all()
    givers = await _givers(db, [row for row, _item, _user in rows])
    return {"items": [_admin_purchase_view(row, item, user, givers.get(row.from_user_id)) for row, item, user in rows], "total": int(total or 0)}


def _clean_note(note: str | None) -> str | None:
    return (note or "").strip()[:NOTE_MAX] or None


async def _grant_target(db: AsyncSession, user_id: int, item_id: int) -> tuple[User, ShopItem]:
    user = await db.get(User, user_id)
    if user is None:
        raise _error(status.HTTP_404_NOT_FOUND, "user_not_found", "用户不存在")
    item = await db.get(ShopItem, item_id)
    if item is None:
        raise _item_not_found()
    return user, item


async def admin_grant(db: AsyncSession, admin: User, user_id: int, item_id: int, note: str | None, duration_days: int | None = None) -> dict[str, Any]:
    """POST /admin/shop/users/{id}/grant：送一件。下架的、不卖的也能送。

    有效期：duration_days 不传按商品的设置；传正整数是这一份只给这么多天；传 0 是送成永久。
    对方已经有这件限时商品时是续期（在原到期时间上加）；已经永久拥有是 409 already_owned。
    """
    admin_id = admin.id
    user, item = await _grant_target(db, user_id, item_id)
    if duration_days is None:
        days = item.duration_days
    elif isinstance(duration_days, bool) or not isinstance(duration_days, int) or not 0 <= duration_days <= DURATION_MAX:
        raise _error(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_duration", f"有效期要在 0–{DURATION_MAX} 天之间（0 是永久）", field="duration_days")
    else:
        days = duration_days or None
    brief = _brief(item)
    clean_note = _clean_note(note)
    row: ShopPurchase | None = None
    for _attempt in range(OWNERSHIP_ATTEMPTS):
        async with _write_lock():
            try:
                row = await _grant_ownership(
                    db, user_id=user_id, item_id=item_id, sku=brief["sku"], days=days, now=_now(), source="grant", actor_id=admin_id, note=clean_note
                )
                await log_admin_action(
                    db, admin_id, "shop.grant", "user", user_id, {"item_id": item_id, "sku": brief["sku"], "note": clean_note, "duration_days": days}
                )
                await db.commit()
                break
            except _PermanentlyOwned:
                await db.rollback()
                raise _already_owned() from None
            except (IntegrityError, _OwnershipChanged):
                # 这个人刚好自己买了 / 续了这一件：按最新的状态重来
                await db.rollback()
                row = None
            except BaseException:
                await db.rollback()
                raise
    if row is None:
        raise _already_owned()
    # 中途回滚过的话这两个对象已经过期，重新读出来
    await db.refresh(item)
    await db.refresh(user)
    await _send_granted(user_id, brief, "grant")
    return _admin_purchase_view(row, item, user)


async def admin_revoke(db: AsyncSession, admin: User, user_id: int, item_id: int, note: str | None, refund: bool = False) -> dict[str, Any]:
    """POST /admin/shop/users/{id}/revoke：收回一件，装备着的话同时卸下。

    默认不退金币；refund=True 而且这件是花金币买的，才把这一笔从已花里减掉。收回的是当前的那一行：
    续过期的限时商品只退最后一次的钱，之前几次的不退。
    """
    admin_id = admin.id
    user, item = await _grant_target(db, user_id, item_id)
    row = (
        await db.execute(select(ShopPurchase).where(ShopPurchase.user_id == user_id, ShopPurchase.item_id == item_id, _live_clause()))
    ).scalar_one_or_none()
    if row is None:
        raise _error(status.HTTP_404_NOT_FOUND, "ownership_not_found", "这个用户没有这件商品")
    now = _now()
    row.revoked_at = now
    row.revoked_by_id = admin_id
    row.revoke_note = _clean_note(note)
    refunded = bool(refund) and row.source == "purchase" and row.price > 0 and not row.refunded
    if refunded:
        row.refunded = True
        await db.execute(
            update(ShopAccount)
            .where(ShopAccount.user_id == user_id)
            .values(spent=case((ShopAccount.spent >= row.price, ShopAccount.spent - row.price), else_=0), updated_at=now)
            .execution_options(synchronize_session=False)
        )
    worn = _strip_sku(user, item.sku, now)
    await log_admin_action(
        db, admin_id, "shop.revoke", "user", user_id, {"item_id": item_id, "sku": item.sku, "note": row.revoke_note, "refunded": refunded, "price": row.price}
    )
    await db.commit()
    if worn:
        announce_cosmetics(user_id)
    return _admin_purchase_view(row, item, user)


def _strip_sku(user: User, sku: str, now: datetime) -> bool:
    """把这个人装备着的这件商品卸下（收回、到期时用）。返回 users.cosmetics 里原来有没有它；调用方提交。

    看的是这一列里原样存着的内容：到期了的那一件 load_cosmetics 已经不认，但它还占在这一列里，这里把它真的删掉。
    """
    try:
        stored = json.loads(user.cosmetics) if user.cosmetics else {}
    except ValueError:
        stored = {}
    worn = isinstance(stored, dict) and any(isinstance(entry, dict) and entry.get("sku") == sku for entry in stored.values())
    if worn:
        _set_cosmetics(user, {slot: entry for slot, entry in load_cosmetics(user.cosmetics, now).items() if entry["sku"] != sku})
    return worn


# ---------------------------------------------------------------- 限时商品到期


async def _send_item_event(user_id: int, event_type: str, data: dict[str, Any]) -> None:
    """shop.item.expiring / shop.item.expired 只发给本人。发不出去只记日志。"""
    from app.websocket.manager import manager

    try:
        await manager.send_to_user(user_id, event_type, data)
    except Exception:
        logger.exception("Failed to send shop expiry event", extra={"user_id": user_id, "event": event_type})


async def _mark_expiry(db: AsyncSession, purchase_id: int, user_id: int, sku: str, now: datetime, *, expired: bool) -> tuple[bool, bool]:
    """占住「处理这一行的到期 / 快到期」：一条带条件的 UPDATE，多个 worker 同时来也只有一个影响到行。返回 (占到了没有, 是否卸下了装备)。

    条件里带着「还是当前的那一行」和时间：刚好被续期（那一行被接替了）、被收回的，这里影响 0 行，什么都不做。
    到期的那一种在同一个事务里把装备着的卸下。
    """
    column = ShopPurchase.expired_notified_at if expired else ShopPurchase.expiring_notified_at
    due = ShopPurchase.expires_at <= now if expired else and_(ShopPurchase.expires_at > now, ShopPurchase.expires_at <= now + EXPIRING_BEFORE)
    async with _write_lock():
        try:
            marked = await db.execute(
                update(ShopPurchase)
                .where(ShopPurchase.id == purchase_id, _live_clause(), due, column.is_(None))
                .values({column.key: now})
                .execution_options(synchronize_session=False)
            )
            if marked.rowcount != 1:
                await db.rollback()
                return False, False
            worn = False
            if expired:
                user = await db.get(User, user_id, populate_existing=True)
                worn = user is not None and _strip_sku(user, sku, now)
            await db.commit()
            return True, worn
        except BaseException:
            await db.rollback()
            raise


async def sweep_expiry(db: AsyncSession, *, now: datetime | None = None) -> dict[str, int]:
    """定时清扫限时商品：到期的卸下、发 user.cosmetics.updated 和 shop.item.expired；三天内到期的发一次 shop.item.expiring。

    「是否拥有 / 是否装备着」不靠这里：读的时候都是现场和现在比的。这里只做读路径做不了的两件事 —— 把 users.cosmetics 里那一件真的删掉、
    通知本人。每个 worker 都跑，靠 _mark_expiry 的条件更新去重：同一行的同一种事件只有一个进程发，各只发一次。
    续期会写一行新的，所以续了之后快到期时还会再提醒一次。
    """
    now = now or _now()
    done = {"expired": 0, "expiring": 0}
    for expired in (True, False):
        column = ShopPurchase.expired_notified_at if expired else ShopPurchase.expiring_notified_at
        due = ShopPurchase.expires_at <= now if expired else and_(ShopPurchase.expires_at > now, ShopPurchase.expires_at <= now + EXPIRING_BEFORE)
        rows = (
            await db.execute(
                select(ShopPurchase.id, ShopPurchase.user_id, ShopPurchase.expires_at, ShopItem.sku, ShopItem.name, ShopItem.category)
                .join(ShopItem, ShopItem.id == ShopPurchase.item_id)
                .where(_live_clause(), due, column.is_(None))
                .order_by(ShopPurchase.expires_at.asc(), ShopPurchase.id.asc())
                .limit(EXPIRY_SWEEP_BATCH)
            )
        ).all()
        for purchase_id, user_id, expires_at, sku, name, category in rows:
            marked, worn = await _mark_expiry(db, purchase_id, user_id, sku, now, expired=expired)
            if not marked:
                continue
            brief = {"sku": sku, "name": name, "category": category}
            if expired:
                done["expired"] += 1
                if worn:
                    announce_cosmetics(user_id)
                await _send_item_event(user_id, EXPIRED_EVENT, {"item": brief})
            else:
                done["expiring"] += 1
                await _send_item_event(user_id, EXPIRING_EVENT, {"item": brief, "expires_at": _aware(expires_at)})
    return done


async def _expiry_worker() -> None:
    delay = EXPIRY_WORKER_STARTUP_DELAY_SECONDS
    while True:
        await asyncio.sleep(delay)
        try:
            async with AsyncSessionLocal() as db:
                await sweep_expiry(db)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Shop expiry sweep failed")
        delay = float(settings.shop_expiry_sweep_interval_seconds)


def start_expiry_worker() -> None:
    global _expiry_task
    if _expiry_task is not None and not _expiry_task.done():
        return
    _expiry_task = asyncio.create_task(_expiry_worker())


async def stop_expiry_worker() -> None:
    global _expiry_task
    task = _expiry_task
    _expiry_task = None
    if task is None or task.done():
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


# ---------------------------------------------------------------- 后台：账户与对账


def _account_columns() -> tuple[Any, Any, Any, Any, Any, Any, Any, Any, Any, Any]:
    """账户列表用的几个 SQL 表达式，口径和 _balance 一致：

    (是否按现在绑定的账号核实的, 有效累计投币, 被别的账号花掉的（含用红包发出去的、送礼花掉的）, 余额, 红包发出的, 红包抢到的, 红包退回的, 送礼花掉的,
    群升级花掉的, 后台赠送的净额)。
    """
    current = and_(User.ccw_student_oid.is_not(None), User.ccw_student_oid == ShopAccount.ccw_oid)
    donated = case((current, ShopAccount.donated), else_=0)
    shared = (
        _shared_spent_query(User.id, User.ccw_student_oid).correlate(User).scalar_subquery()
        + _shared_packet_query(User.id, User.ccw_student_oid).correlate(User).scalar_subquery()
        + _shared_gift_query(User.id, User.ccw_student_oid).correlate(User).scalar_subquery()
        + _shared_group_plus_query(User.id, User.ccw_student_oid).correlate(User).scalar_subquery()
    )
    gifted = _gift_out_query(User.id).correlate(User).scalar_subquery()
    plus = _group_plus_out_query(User.id).correlate(User).scalar_subquery()
    sent = select(func.coalesce(func.sum(RedPacket.total), 0)).where(RedPacket.sender_id == User.id).correlate(User).scalar_subquery()
    refunded = select(func.coalesce(func.sum(RedPacket.refunded_amount), 0)).where(RedPacket.sender_id == User.id).correlate(User).scalar_subquery()
    received = _packet_in_query(User.id).correlate(User).scalar_subquery()
    granted = _granted_query(User.id).correlate(User).scalar_subquery()
    balance = donated - ShopAccount.spent - shared - gifted - plus - sent + received + refunded + granted
    return current, donated, shared, balance, sent, received, refunded, gifted, plus, granted


def _account_view(
    account: ShopAccount,
    user: User,
    donated: int,
    shared: int,
    sent: int = 0,
    received: int = 0,
    refunded: int = 0,
    gifted: int = 0,
    plus: int = 0,
    granted: int = 0,
) -> dict[str, Any]:
    oid = _bound_oid(user)
    current = oid is not None and account.ccw_oid == oid
    balance = donated - account.spent - shared - gifted - plus - sent + received + refunded + granted
    return {
        "user": _admin_user_view(user),
        "ccw_oid": oid,
        "verified_oid": account.ccw_oid,
        "donated": donated,
        "spent": account.spent,
        "shared_spent": shared,
        "balance": balance,
        "verified_at": _aware(account.verified_at),
        "checked_at": _aware(account.checked_at),
        "stale": not (current and account.verify_ok),
        "last_error": account.last_error,
        "mismatch": balance < 0,
        "red_packet_sent": sent,
        "red_packet_received": received,
        "red_packet_refunded": refunded,
        "gift_spent": gifted,
        "group_plus_spent": plus,
        "granted": granted,
    }


async def admin_list_accounts(db: AsyncSession, *, q: str | None, mismatch: bool | None, limit: int, offset: int) -> dict[str, Any]:
    """GET /admin/shop/accounts：每个用户的累计投币、已花、送礼花掉的、红包收支、余额、上次核实时间。mismatch=True 只看余额为负的。

    只有核实过余额、对账时对上了投币记录、或者抢到过红包的用户才有账户，没出现在这里的人就是没投过、没花过也没抢到过。
    """
    _current, donated, shared, balance, sent, received, refunded, gifted, plus, granted = _account_columns()
    clauses = []
    term = (q or "").strip()
    if term:
        keyword = f"%{term}%"
        matches = [User.username.ilike(keyword), User.nickname.ilike(keyword), User.ccw_name.ilike(keyword), User.ccw_student_oid.ilike(keyword)]
        if term.isdigit():
            matches.append(User.id == int(term))
        clauses.append(or_(*matches))
    if mismatch:
        clauses.append(balance < 0)
    base = select(ShopAccount).join(User, User.id == ShopAccount.user_id).where(*clauses)
    total = await db.scalar(select(func.count()).select_from(base.subquery()))
    rows = await db.execute(
        select(ShopAccount, User, donated, shared, sent, received, refunded, gifted, plus, granted)
        .join(User, User.id == ShopAccount.user_id)
        .where(*clauses)
        .order_by(balance.asc(), ShopAccount.user_id.asc())
        .offset(offset)
        .limit(limit)
    )
    return {
        "items": [
            _account_view(
                account,
                user,
                int(row_donated or 0),
                int(row_shared or 0),
                int(row_sent or 0),
                int(row_received or 0),
                int(row_refunded or 0),
                int(row_gifted or 0),
                int(row_plus or 0),
                int(row_granted or 0),
            )
            for account, user, row_donated, row_shared, row_sent, row_received, row_refunded, row_gifted, row_plus, row_granted in rows.all()
        ],
        "total": int(total or 0),
    }


# ---------------------------------------------------------------- 后台：赠送 / 扣除金币（docs/shop.md 5.2）

COIN_GRANT_MAX = 1_000_000
COIN_GRANT_REASON_MAX = 200
COIN_GRANT_EVENT_PREFIX = "coin_grant"


def _coin_grant_view(grant: CoinGrant, user: User | None, admin: User | None) -> dict[str, Any]:
    return {
        "id": grant.id,
        "user_id": grant.user_id,
        "user": _admin_user_view(user),
        "amount": grant.amount,
        "reason": grant.reason,
        "admin_id": grant.admin_id,
        "admin": _admin_user_view(admin),
        "created_at": _aware(grant.created_at),
    }


def validate_coin_grant(amount: Any, reason: Any) -> tuple[int, str]:
    """金额是非 0 整数、绝对值不超过 COIN_GRANT_MAX；原因必填、不超过 200 字。不合法抛 422。"""
    if isinstance(amount, bool) or not isinstance(amount, int) or amount == 0 or abs(amount) > COIN_GRANT_MAX:
        raise _error(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_amount", f"金额要是非 0 整数，绝对值不超过 {COIN_GRANT_MAX:,}", field="amount"
        )
    text_reason = reason.strip() if isinstance(reason, str) else ""
    if not text_reason:
        raise _error(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_reason", "要写明原因", field="reason")
    if len(text_reason) > COIN_GRANT_REASON_MAX:
        raise _error(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_reason", f"原因最多 {COIN_GRANT_REASON_MAX} 字", field="reason")
    return amount, text_reason


async def admin_grant_coins(db: AsyncSession, admin: User, user_id: int, amount: Any, reason: Any) -> dict[str, Any]:
    """POST /admin/shop/grants：给一个用户加（amount > 0）或扣（amount < 0）金币。

    写 coin_grants 一行 + 审计日志 shop.coins.grant，提交之后给本人发 shop.balance.changed（reason = coin_grant）。
    扣除时按完整公式算的余额扣完不能为负，否则 409 insufficient_balance（带 balance），什么都不写。
    和购买 / 发红包一样先过 _write_lock，事务的第一条语句就拿写锁，余额检查和写入之间不会被别的花币插进来。
    """
    admin_id = admin.id
    amount, reason = validate_coin_grant(amount, reason)
    target = await db.get(User, user_id)
    if target is None:
        raise _error(status.HTTP_404_NOT_FOUND, "user_not_found", "用户不存在")
    oid = _bound_oid(target)
    now = _now()
    outcome: dict[str, Any] = {}
    async with _write_lock():
        try:
            # 事务的第一条语句：从这里到提交一直拿着写锁。没有账户就建一行，这样他会出现在后台的账户列表里
            touched = await db.execute(
                update(ShopAccount).where(ShopAccount.user_id == user_id).values(updated_at=now).execution_options(synchronize_session=False)
            )
            if touched.rowcount != 1:
                db.add(ShopAccount(user_id=user_id, created_at=now, updated_at=now))
                await db.flush()
            before = (await _balance(db, user_id, oid))["balance"]
            if amount < 0 and before + amount < 0:
                await db.rollback()
                raise _error(
                    status.HTTP_409_CONFLICT,
                    "insufficient_balance",
                    f"他现在只有 {before} 金币，扣 {-amount} 之后会变成负数",
                    balance=before,
                    amount=amount,
                )
            grant = CoinGrant(user_id=user_id, amount=amount, reason=reason, admin_id=admin_id, created_at=now)
            db.add(grant)
            await db.flush()
            await log_admin_action(
                db,
                admin_id,
                "shop.coins.grant",
                "user",
                user_id,
                {"grant_id": grant.id, "amount": amount, "reason": reason, "balance_before": before, "balance_after": before + amount},
            )
            outcome = {"id": grant.id, "before": before}
            await db.commit()
        except HTTPException:
            raise
        except BaseException:
            await db.rollback()
            raise
    change = f"{COIN_GRANT_EVENT_PREFIX}.{'granted' if amount > 0 else 'deducted'}"
    announce_balance(user_id, change, grant_id=outcome["id"], amount=amount)
    row = await db.get(CoinGrant, outcome["id"])
    admin_row = await db.get(User, admin_id)
    target = await db.get(User, user_id)
    return {
        "grant": _coin_grant_view(row, target, admin_row),
        "balance_before": outcome["before"],
        "balance": await _balance(db, user_id, oid),
    }


async def admin_list_coin_grants(
    db: AsyncSession, *, user_id: int | None, admin_id: int | None, direction: str | None, limit: int, offset: int
) -> dict[str, Any]:
    """GET /admin/shop/grants：赠送 / 扣除记录，新的在前。summary 是筛选范围内的合计。"""
    clauses = []
    if user_id is not None:
        clauses.append(CoinGrant.user_id == user_id)
    if admin_id is not None:
        clauses.append(CoinGrant.admin_id == admin_id)
    if direction == "grant":
        clauses.append(CoinGrant.amount > 0)
    elif direction == "deduct":
        clauses.append(CoinGrant.amount < 0)
    total = int(await db.scalar(select(func.count()).select_from(CoinGrant).where(*clauses)) or 0)
    granted_total, deducted_total = (
        await db.execute(
            select(
                func.coalesce(func.sum(case((CoinGrant.amount > 0, CoinGrant.amount), else_=0)), 0),
                func.coalesce(func.sum(case((CoinGrant.amount < 0, -CoinGrant.amount), else_=0)), 0),
            ).where(*clauses)
        )
    ).one()
    rows = (await db.execute(select(CoinGrant).where(*clauses).order_by(CoinGrant.id.desc()).offset(offset).limit(limit))).scalars().all()
    ids = {row.user_id for row in rows} | {row.admin_id for row in rows if row.admin_id is not None}
    users = {user.id: user for user in (await db.execute(select(User).where(User.id.in_(ids)))).scalars().all()} if ids else {}
    return {
        "items": [_coin_grant_view(row, users.get(row.user_id), users.get(row.admin_id) if row.admin_id is not None else None) for row in rows],
        "total": total,
        "summary": {"granted": int(granted_total or 0), "deducted": int(deducted_total or 0), "net": int(granted_total or 0) - int(deducted_total or 0)},
    }


async def admin_reconcile(db: AsyncSession, admin: User) -> dict[str, Any]:
    """POST /admin/shop/reconcile：向余额来源拉一次完整的名单，更新所有已绑定用户的累计投币。

    只对已有账户的人和名单上有名字的人动手；绑定了但没投过币、也没有账户的人不用建一行。拉不到完整名单时 503，什么都不改。

    有了红包之后「花的比投的多」不再必然是异常（抢到的币也能花），所以对账查的是三件事：
    每个人按完整公式算出来的余额不为负（mismatched），全站的红包账守恒（red_packets，见 red_packets.ledger_check），
    以及送礼的账守恒（gifts，见 gifts.ledger_check：每个人的心意值 = 他收到的礼物花掉的币之和）。
    """
    admin_id = admin.id
    # 拉完整名单要等 CCW（可能好几秒）：先结束请求的读事务、把连接还回去；下面的读写都在拿到名单之后
    await release_connection(db)
    try:
        donations = await get_coin_ledger().all_credits(fresh=True)
    except CoinsUnavailable:
        raise _error(status.HTTP_503_SERVICE_UNAVAILABLE, "verification_unavailable", "现在拉不到 CCW 的投币排行，稍后再试。没有改动任何账户") from None
    now = _now()
    bound = (await db.execute(select(User.id, User.ccw_student_oid).where(User.ccw_student_oid.is_not(None), User.ccw_student_oid != ""))).all()
    accounts = {account.user_id: account for account in (await db.execute(select(ShopAccount))).scalars().all()}
    matched = updated = 0
    for user_id, raw_oid in bound:
        oid = raw_oid.strip().lower()
        donated = donations.get(oid)
        if donated is not None:
            matched += 1
        account = accounts.get(user_id)
        if account is None:
            if not donated:
                continue
            db.add(ShopAccount(user_id=user_id, ccw_oid=oid, donated=donated, verified_at=now, verify_ok=True, created_at=now, updated_at=now))
            updated += 1
            continue
        if account.ccw_oid != oid or account.donated != (donated or 0):
            updated += 1
        account.ccw_oid = oid
        account.donated = donated or 0
        account.verified_at = now
        account.verify_ok = True
        account.last_error = None
        account.updated_at = now
    await log_admin_action(db, admin_id, "shop.reconcile", "shop", None, {"donors": len(donations), "matched": matched, "updated": updated})
    await db.commit()
    mismatched = await admin_list_accounts(db, q=None, mismatch=True, limit=RECONCILE_MISMATCH_LIMIT, offset=0)
    from app.services import gifts as gift_service
    from app.services import group_plus as group_plus_service
    from app.services import red_packets as red_packet_service

    return {
        "donors": len(donations),
        "matched": matched,
        "updated": updated,
        "mismatched": mismatched["items"],
        "red_packets": await red_packet_service.ledger_check(db),
        "gifts": await gift_service.ledger_check(db),
        "group_plus": await group_plus_service.ledger_check(db),
    }


def admin_settings() -> dict[str, Any]:
    """GET /admin/shop/settings：官方作品和各项阈值，只读（都来自配置文件）。"""
    ledger = get_coin_ledger()
    source = ledger.source()
    return {
        "configured": ledger.configured(),
        "creation_oid": source.id,
        "creation_oid_source": source.setting,
        "donate_url": ledger.topup_url(),
        "refresh_interval_seconds": settings.shop_refresh_interval_seconds,
        "ranking_recent_limit": settings.shop_ranking_recent_limit,
        "ranking_cache_seconds": settings.shop_ranking_cache_seconds,
        "changed_interval_seconds": settings.shop_changed_interval_seconds,
        "cosmetics_event_interval_seconds": settings.shop_cosmetics_event_interval_seconds,
        "categories": categories_view(),
        "tags": list(TAGS),
        "animations": list(ANIMATIONS),
        "patterns": list(PATTERNS),
        "ornaments": list(ORNAMENTS),
        "frame_animations": list(FRAME_ANIMATIONS),
        "frame_ornaments": list(FRAME_ORNAMENTS),
        "frame_positions": list(FRAME_POSITIONS),
        "price_max": PRICE_MAX,
        "unlock_level_max": UNLOCK_LEVEL_MAX,
        "duration_max": DURATION_MAX,
    }


# ---------------------------------------------------------------- 初始商品


def _gradient(angle: int, *stops: str) -> str:
    return f"linear-gradient({angle}deg, {', '.join(stops)})"


def _conic(start: int, *stops: str) -> str:
    """锥形渐变（只有头像框的环能用）。首尾写同一个颜色，转起来才没有接缝。"""
    return f"conic-gradient(from {start}deg, {', '.join(stops)})"


# 8 件气泡皮肤，按档次递进：便宜的两件是干净的配色，中档的带纹样 / 一个角饰，
# 最贵的两件是「渐变 + 纹样 + 角饰 + 动效」的整套；两件等级奖励分别用月亮和太阳，对应等级图标。
# 每件都有深色模式的覆盖。正文颜色对底色每一段（包括叠上纹样之后）的对比度都不低于 4.5，测试里有检查。
# 链接色也一样（不低于 4.3）；浅色、深色底色明暗相反的几件在深色覆盖里单独配了链接色。
# 角饰没有深色覆盖，又会探出气泡一点，所以都用了在浅色和深色聊天背景上都看得见的中间调。
DEFAULT_ITEMS: tuple[dict[str, Any], ...] = (
    {
        "sku": "bubble.mint",
        "name": "薄荷汽水",
        "description": "清清爽爽的薄荷绿，像夏天的第一口汽水",
        "price": 10,
        "preview_text": "来一口冰的！",
        "style": {
            "version": 1,
            "background": _gradient(135, "#D8F8EA 0%", "#B4EDD6 100%"),
            "text": "#0F4A3A",
            "link": "#0A6249",
            "border": {"color": "#8FDDBE", "width": 1},
            "radius": 16,
            "dark": {
                "background": _gradient(135, "#145445 0%", "#0D3D33 100%"),
                "text": "#D9FBEF",
                "link": "#7EE8C6",
                "border": {"color": "#2A7A65", "width": 1},
            },
        },
    },
    {
        "sku": "bubble.peach",
        "name": "蜜桃乌龙",
        "description": "软乎乎的蜜桃色，说什么都显得温柔",
        "price": 30,
        "preview_text": "抱抱～",
        "style": {
            "version": 1,
            "background": _gradient(135, "#FFE1D6 0%", "#FFC4B8 100%"),
            "text": "#6B2A1F",
            "link": "#8F2D1D",
            "radius": 18,
            "shadow": "0 2px 8px #FF9E8A40",
            "dark": {
                "background": _gradient(135, "#6A2E28 0%", "#4A1F1F 100%"),
                "text": "#FFE8E0",
                "link": "#FFC2B0",
                "shadow": "0 2px 8px #00000040",
            },
        },
    },
    {
        "sku": "bubble.ocean",
        "name": "深海",
        "description": "从浅滩一路沉到深海的蓝，水面下有一层层暗涌的波纹",
        "price": 60,
        "preview_text": "潜水中，勿扰",
        "style": {
            "version": 1,
            "background": _gradient(160, "#2B6BDD 0%", "#1B4FD6 55%", "#1A2F9E 100%"),
            "text": "#FFFFFF",
            "link": "#E6F1FF",
            "radius": 16,
            "shadow": "0 3px 10px #1B4FD655",
            "pattern": {"key": "waves", "color": "#0A1F6E", "opacity": 0.22, "scale": 1},
            "dark": {
                "background": _gradient(160, "#1D53B0 0%", "#163A9C 55%", "#121F6B 100%"),
                "text": "#EAF2FF",
                "shadow": "0 3px 10px #0B143F66",
                "pattern": {"key": "waves", "color": "#7FB0FF", "opacity": 0.14, "scale": 1},
            },
        },
    },
    {
        "sku": "bubble.pixel",
        "name": "像素心跳",
        "description": "方方正正的复古游戏机风格，角上挂着一颗像素爱心",
        "price": 60,
        "preview_text": "PRESS START",
        "style": {
            "version": 1,
            "background": "#2D2A4A",
            "text": "#F4F1FF",
            "link": "#FFB3C9",
            "border": {"color": "#FF5C8A", "width": 2},
            "radius": 6,
            "shadow": "3px 3px 0 #FF5C8A59",
            "pattern": {"key": "pixels", "color": "#8B7BFF", "opacity": 0.22, "scale": 1},
            "ornaments": [{"key": "pixel_heart", "corner": "tr", "color": "#FF5C8A", "size": 16}],
            "dark": {
                "background": "#1E1C36",
                "text": "#F4F1FF",
                "border": {"color": "#FF5C8A", "width": 2},
                "shadow": "3px 3px 0 #FF5C8A40",
                "pattern": {"key": "pixels", "color": "#8B7BFF", "opacity": 0.18, "scale": 1},
            },
        },
    },
    {
        "sku": "bubble.galaxy",
        "name": "星河",
        "description": "洒满星点的深紫色夜空，时不时有一道光扫过，角上的星星会闪",
        "price": 100,
        "preview_text": "抬头，有流星",
        "tag": "new",
        "style": {
            "version": 1,
            "background": _gradient(135, "#1B1F4B 0%", "#3A2C7A 55%", "#643A98 100%"),
            "text": "#F7F3FF",
            "link": "#E4DAFF",
            "border": {"color": "#B9A6FF55", "width": 1},
            "radius": 16,
            "shadow": "0 3px 12px #3A2C7A66",
            "animation": "shimmer",
            "pattern": {"key": "stars", "color": "#FFFFFF", "opacity": 0.16, "scale": 1},
            "ornaments": [
                {"key": "sparkle", "corner": "tr", "color": "#FFC93C", "size": 18, "animated": True},
                {"key": "star", "corner": "bl", "color": "#9B7BFF", "size": 12},
            ],
            "dark": {
                "background": _gradient(135, "#14173A 0%", "#2D2263 55%", "#55318A 100%"),
                "text": "#F3EEFF",
                "border": {"color": "#B9A6FF44", "width": 1},
                "shadow": "0 3px 12px #0A0B2080",
                "pattern": {"key": "stars", "color": "#FFFFFF", "opacity": 0.16, "scale": 1},
            },
        },
    },
    {
        "sku": "bubble.aurora",
        "name": "极光",
        "description": "紫、蓝、青缓缓流动的极光，蒙着一层细细的霜，角上落着雪花和闪光",
        "price": 200,
        "preview_text": "今天也要开心呀",
        "tag": "hot",
        "style": {
            "version": 1,
            "background": _gradient(135, "#5A50CF 0%", "#2864CC 50%", "#097870 100%"),
            "text": "#FFFFFF",
            "link": "#F0F8FF",
            "border": {"color": "#FFFFFF33", "width": 1},
            "radius": 14,
            "shadow": "0 2px 10px #5A50CF66",
            "animation": "flow",
            "pattern": {"key": "noise", "color": "#FFFFFF", "opacity": 0.08, "scale": 1},
            "ornaments": [
                {"key": "sparkle", "corner": "tr", "color": "#3DD6C4", "size": 18, "animated": True},
                {"key": "snow", "corner": "bl", "color": "#6FA0F5", "size": 14, "animated": True},
            ],
            "dark": {
                "background": _gradient(135, "#463EA6 0%", "#2456AA 50%", "#0A6B63 100%"),
                "text": "#F4F7FF",
                "border": {"color": "#FFFFFF26", "width": 1},
                "shadow": "0 2px 10px #0A6B6355",
                "pattern": {"key": "noise", "color": "#FFFFFF", "opacity": 0.08, "scale": 1},
            },
        },
    },
    {
        "sku": "bubble.moonlight",
        "name": "月光",
        "description": "等级奖励：账号升到 4 级、拿到第一个月亮时解锁。银蓝色的夜，角上挂着一弯月亮",
        "price": 0,
        "unlock_level": 4,
        "preview_text": "晚安，好梦",
        "style": {
            "version": 1,
            "background": "radial-gradient(#F7F9FF 0%, #DCE4FB 100%)",
            "text": "#27324F",
            "link": "#2F47B5",
            "border": {"color": "#C3CEF2", "width": 1},
            "radius": 16,
            "shadow": "0 2px 10px #8FA3E840",
            "pattern": {"key": "stars", "color": "#7F92D6", "opacity": 0.3, "scale": 1},
            "ornaments": [
                {"key": "moon", "corner": "tr", "color": "#F2B233", "size": 20},
                {"key": "star", "corner": "bl", "color": "#F2B233", "size": 12, "animated": True},
            ],
            "dark": {
                "background": "radial-gradient(#2F3A61 0%, #1D2540 100%)",
                "text": "#EEF2FF",
                "link": "#BCCBFF",
                "border": {"color": "#46527F", "width": 1},
                "shadow": "0 2px 10px #0B102566",
                "pattern": {"key": "stars", "color": "#FFFFFF", "opacity": 0.14, "scale": 1},
            },
        },
    },
    {
        "sku": "bubble.corona",
        "name": "日冕",
        "description": "等级奖励：账号升到 16 级、拿到第一个太阳时解锁。金色的光一呼一吸，角上是一轮太阳",
        "price": 0,
        "unlock_level": 16,
        "preview_text": "早上好！",
        "style": {
            "version": 1,
            "background": _gradient(135, "#FFD76A 0%", "#FFB23E 55%", "#FF8A3C 100%"),
            "text": "#4A2500",
            "link": "#6B2A00",
            "border": {"color": "#FFE9A8", "width": 1},
            "radius": 16,
            "shadow": "0 3px 14px #FFB23E73",
            "animation": "breathe",
            "pattern": {"key": "stripes", "color": "#FFFFFF", "opacity": 0.2, "scale": 1},
            "ornaments": [
                {"key": "sun", "corner": "tr", "color": "#FF9A1F", "size": 22, "animated": True},
                {"key": "sparkle", "corner": "bl", "color": "#FFC93C", "size": 12},
            ],
            "dark": {
                "background": _gradient(135, "#8C590D 0%", "#804110 55%", "#6E2E10 100%"),
                "text": "#FFF4DC",
                "link": "#FFE2A6",
                "border": {"color": "#E0A84A55", "width": 1},
                "shadow": "0 3px 14px #FF8A3C40",
                "pattern": {"key": "stripes", "color": "#FFD98A", "opacity": 0.1, "scale": 1},
            },
        },
    },
    # —— 11 件头像框。前 8 件和上面的气泡成套（同样的档次和价位，等级奖励也是 4 级、16 级各一件），
    # 后 3 件各用一种头像框专用的装饰（蝴蝶结、小恶魔角、翅膀），其中「小恶魔」「天使」是限时的（买一次 30 天，见 duration_days）。
    # 样式是客户端那边先拟好、在浅色和深色聊天背景上都截图看过的那一套（客户端仓库 dev/mocks/shop.ts 的 FRAMES），
    # 这里原样照搬；要调配色先在客户端看效果，再两边一起改。
    {
        "sku": "frame.mint",
        "category": "frame",
        "name": "薄荷",
        "description": "清爽的薄荷绿，角上一片小叶子",
        "price": 10,
        "style": {
            "version": 1,
            "ring": {"background": _gradient(140, "#a8f5d8 0%", "#3ecfa0 55%", "#8ff0cf 100%"), "width": 3, "gap": 1},
            "ornaments": [{"key": "leaf", "position": "br", "color": "#37c488", "size": 14, "animated": True}],
            "dark": {"ring": {"background": _gradient(140, "#7fe6c2 0%", "#2bb389 55%", "#6fdcb8 100%")}},
        },
    },
    {
        "sku": "frame.sakura",
        "category": "frame",
        "name": "樱花",
        "description": "粉色的环带着淡淡的光晕，飘着两片花瓣",
        "price": 30,
        "style": {
            "version": 1,
            "ring": {"background": _gradient(160, "#ffd9e6 0%", "#ff8fb3 55%", "#ffc4d8 100%"), "width": 3, "gap": 1},
            "glow": {"color": "#ff8fb355", "blur": 6},
            "ornaments": [
                {"key": "petal", "position": "tr", "color": "#ff8fb3", "size": 15, "animated": True},
                {"key": "petal", "position": "bl", "color": "#ffb7cd", "size": 11, "animated": True},
            ],
        },
    },
    {
        "sku": "frame.ocean",
        "category": "frame",
        "name": "海盐",
        "description": "浅浅的海蓝，偶尔一道光扫过去，角上一点闪光",
        "price": 60,
        "style": {
            "version": 1,
            "ring": {"background": _gradient(180, "#d6f1ff 0%", "#6bbcf7 60%", "#a9dcff 100%"), "width": 3, "gap": 1},
            "animation": "shimmer",
            "ornaments": [{"key": "sparkle", "position": "tr", "color": "#ffffff", "size": 12, "animated": True}],
            "dark": {"ring": {"background": _gradient(180, "#a9dcff 0%", "#3f96dc 60%", "#7cc4f5 100%")}},
        },
    },
    {
        "sku": "frame.neko",
        "category": "frame",
        "name": "猫耳",
        "description": "头顶一对猫耳，偶尔会抖一下，角上还有个小爪印",
        "price": 60,
        "style": {
            "version": 1,
            "ring": {"background": _gradient(180, "#ffd9a0 0%", "#f2a954 100%"), "width": 3, "gap": 0},
            "ornaments": [
                {"key": "cat_ears", "position": "top", "color": "#f5b160", "size": 20, "animated": True},
                {"key": "paw", "position": "br", "color": "#e8944a", "size": 13},
            ],
        },
    },
    {
        "sku": "frame.galaxy",
        "category": "frame",
        "name": "星环",
        "description": "缓缓旋转的渐变星环，挂着一颗会闪的星星",
        "price": 100,
        "style": {
            "version": 1,
            "ring": {"background": _conic(0, "#7f7fd5", "#91eae4", "#ffe9a8", "#b08cf0", "#7f7fd5"), "width": 3, "gap": 1},
            "glow": {"color": "#7f7fd566", "blur": 8},
            "animation": "spin",
            "ornaments": [
                {"key": "star", "position": "tr", "color": "#ffd76a", "size": 15, "animated": True},
                {"key": "sparkle", "position": "bl", "color": "#fff6c4", "size": 11, "animated": True},
            ],
        },
    },
    {
        "sku": "frame.aurora",
        "category": "frame",
        "name": "极光",
        "description": "极光的颜色绕着头像慢慢转，带着一圈青绿色的光晕",
        "price": 200,
        "style": {
            "version": 1,
            "ring": {"background": _conic(90, "#4c3fd1", "#2fb6dd", "#3ddc97", "#b05cff", "#4c3fd1"), "width": 4, "gap": 1},
            "glow": {"color": "#3ddc9750", "blur": 10},
            "animation": "spin",
        },
    },
    {
        "sku": "frame.halo",
        "category": "frame",
        "name": "光环",
        "description": "等级奖励：账号升到 4 级、拿到第一个月亮时解锁。淡金色的柔光一呼一吸，头顶悬着一圈光环",
        "price": 0,
        "unlock_level": 4,
        "style": {
            "version": 1,
            "ring": {"background": _gradient(180, "#fff6d0 0%", "#ffd76a 100%"), "width": 2, "gap": 1},
            "glow": {"color": "#ffd76a88", "blur": 9},
            "animation": "pulse",
            "ornaments": [{"key": "halo", "position": "top", "color": "#ffdf7a", "size": 22, "animated": True}],
        },
    },
    {
        "sku": "frame.crown",
        "category": "frame",
        "name": "加冕",
        "description": "等级奖励：账号升到 16 级、拿到第一个太阳时解锁。金环上不时扫过一道光，头戴皇冠，脚下是月桂",
        "price": 0,
        "unlock_level": 16,
        "style": {
            "version": 1,
            "ring": {"background": _conic(0, "#f7d774", "#b8862b", "#fff1b8", "#c9962f", "#f7d774"), "width": 4, "gap": 1},
            "glow": {"color": "#f2c14e66", "blur": 8},
            "animation": "shimmer",
            "ornaments": [
                {"key": "crown", "position": "top", "color": "#ffd24a", "size": 18},
                {"key": "laurel", "position": "bottom", "color": "#e0ae3e", "size": 15},
            ],
        },
    },
    {
        "sku": "frame.bow",
        "category": "frame",
        "name": "蝴蝶结",
        "description": "粉粉的一圈，角上系着蝴蝶结，还有一颗小爱心",
        "price": 30,
        "style": {
            "version": 1,
            "ring": {"background": "#ff9ec0", "width": 3, "gap": 1},
            "ornaments": [
                {"key": "bow", "position": "tr", "color": "#ff5c8a", "size": 18, "animated": True},
                {"key": "heart", "position": "bl", "color": "#ff7aa5", "size": 11, "animated": True},
            ],
        },
    },
    {
        "sku": "frame.devil",
        "category": "frame",
        "name": "小恶魔",
        "description": "暗红色的环一明一暗，头顶一对小恶魔角，角上一道闪电",
        "price": 50,
        "duration_days": 30,
        "style": {
            "version": 1,
            "ring": {"background": _gradient(160, "#ff6b7d 0%", "#8a1f3d 100%"), "width": 3, "gap": 0},
            "glow": {"color": "#ff3b5c55", "blur": 8},
            "animation": "pulse",
            "ornaments": [
                {"key": "horns", "position": "top", "color": "#d8314f", "size": 18, "animated": True},
                {"key": "bolt", "position": "br", "color": "#ffd34a", "size": 13},
            ],
        },
    },
    {
        "sku": "frame.angel",
        "category": "frame",
        "name": "天使",
        "description": "洁白的环泛着淡蓝的光，身侧一对翅膀，头顶一圈光环",
        "price": 120,
        "duration_days": 30,
        "style": {
            "version": 1,
            "ring": {"background": _gradient(180, "#ffffff 0%", "#cfe4ff 100%"), "width": 2, "gap": 1},
            "glow": {"color": "#a9cfff77", "blur": 8},
            "ornaments": [
                {"key": "wings", "position": "right", "color": "#f4f8ff", "size": 24, "animated": True},
                {"key": "halo", "position": "top", "color": "#ffe38a", "size": 18, "animated": True},
            ],
        },
    },
)


async def ensure_default_items(db: AsyncSession) -> int:
    """启动时播种初始商品，返回新建了几件。幂等：按 sku，已经有的（哪怕被后台改过、下架了）一概不动。

    出错只记日志：商店的初始数据不该拦住服务启动。
    """
    try:
        skus = [spec["sku"] for spec in DEFAULT_ITEMS]
        existing = set((await db.execute(select(ShopItem.sku).where(ShopItem.sku.in_(skus)))).scalars().all())
        now = _now()
        created = 0
        for index, spec in enumerate(DEFAULT_ITEMS):
            if spec["sku"] in existing:
                continue
            category = spec.get("category", "bubble")
            db.add(
                ShopItem(
                    sku=spec["sku"],
                    category=category,
                    name=spec["name"],
                    description=spec.get("description", ""),
                    price=spec.get("price", 0),
                    unlock_level=spec.get("unlock_level"),
                    style=_dump(CATEGORIES[category].validate(spec["style"])),
                    preview_text=spec.get("preview_text"),
                    tag=spec.get("tag"),
                    duration_days=spec.get("duration_days"),
                    sort=(index + 1) * 10,
                    created_at=now,
                    updated_at=now,
                )
            )
            created += 1
        if created:
            await db.commit()
        return created
    except IntegrityError:
        # 多个 worker 同时启动，别的进程刚播种过
        await db.rollback()
        return 0
    except Exception:
        logger.exception("Failed to seed default shop items")
        await db.rollback()
        return 0
