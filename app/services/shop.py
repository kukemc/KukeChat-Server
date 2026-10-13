"""商店（docs/shop.md）：用 CCW 金币换聊天装饰，一期只有气泡皮肤。

KukeChat 不发币也不存币。余额 = 用户给官方作品累计投的币 − 在这里花掉的。
累计投了多少由可插拔的余额来源核实（app/integrations/coin_ledger.py 的 CoinLedger 接口，实现不在本仓库里）；
没有接入余额来源时商店能逛、能领等级解锁的商品，不能花币买。
几条贯穿全文件的约定：

- 向 CCW 核实不了时一律按「不知道」处理：展示上次的数并标 stale，购买直接 503；不会因此多给或少扣。
- 扣款是一条带条件的 UPDATE（余额够才加 spent），和写购买记录在同一个事务里；重复拥有由 shop_purchases 上的唯一索引挡住。
- 装备着什么冗余在 users.cosmetics 上，序列化用户时不查这里的表；商品样式被改、商品被收回时由这里负责把那一列改对，
  这一列每次变化都发 user.cosmetics.updated。
- 事务里一旦 rollback，会话里的 ORM 对象全部过期，异步下再读属性会出错 —— 所以先把要用的值取成普通变量。
- 用户侧的写事务都先过一把进程内的锁再进 SQLite，原因见 _write_lock。
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
from app.core.database import AsyncSessionLocal
from app.integrations import CoinsUnavailable, get_coin_ledger
from app.models.shop import ShopAccount, ShopItem, ShopPurchase
from app.models.user import User
from app.schemas.user import parse_cosmetics
from app.services.reports import log_admin_action
from app.services.shop_style import ANIMATIONS, ORNAMENTS, PATTERNS, StyleError, validate_bubble_style

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


# 以后加头像框、昵称颜色之类的，在这里登记一行（再写一个对应的 style 校验函数）
CATEGORIES: dict[str, Category] = {
    "bubble": Category(key="bubble", label="气泡", slot="bubble", validate=validate_bubble_style),
}
SLOTS = frozenset(category.slot for category in CATEGORIES.values())
TAGS = ("new", "hot", "limited")
SOURCES = ("purchase", "level", "grant")
SKU_RE = re.compile(r"[a-z0-9]+(?:[._-][a-z0-9]+)*")
SKU_MAX = 64
NAME_MAX = 40
DESCRIPTION_MAX = 200
PREVIEW_TEXT_MAX = 80
NOTE_MAX = 500
PRICE_MAX = 1_000_000
UNLOCK_LEVEL_MAX = 256
SORT_MAX = 1_000_000
# 对账结果里最多列出多少个「花的比投的多」的账户，其余的去账户列表里看
RECONCILE_MISMATCH_LIMIT = 200

COSMETICS_EVENT = "user.cosmetics.updated"
CHANGED_EVENT = "shop.changed"
GRANTED_EVENT = "shop.item.granted"

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


def reset_runtime_state() -> None:
    """清掉进程内的状态（测试用）。"""
    global _changed_sent_at, _changed_task
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


def _obtainable(item: ShopItem, owned: bool, level: int) -> str:
    if owned:
        return "owned"
    if item.unlock_level is not None and level >= item.unlock_level:
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
        "is_active": bool(item.is_active),
    }


def _item_view(item: ShopItem, *, owned: bool, equipped: dict[str, str], level: int) -> dict[str, Any]:
    slot = _slot(item)
    return {
        **_item_base(item),
        "owned": owned,
        "equipped": slot is not None and equipped.get(slot) == item.sku,
        "obtainable": _obtainable(item, owned, level),
    }


def equipped_map(user: User) -> dict[str, str]:
    """每个槽装备的 sku，从 users.cosmetics 读。"""
    return {slot: entry["sku"] for slot, entry in parse_cosmetics(user.cosmetics).items()}


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
        "balance": donated - spent,
        "verified_at": _aware(account.verified_at) if current else None,
        "stale": stale,
    }


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


# ---------------------------------------------------------------- 逛、买、装备


async def _owned_ids(db: AsyncSession, user_id: int) -> set[int]:
    rows = await db.execute(select(ShopPurchase.item_id).where(ShopPurchase.user_id == user_id, ShopPurchase.revoked_at.is_(None)))
    return set(rows.scalars().all())


async def _owns(db: AsyncSession, user_id: int, item_id: int) -> bool:
    found = await db.scalar(
        select(ShopPurchase.id).where(ShopPurchase.user_id == user_id, ShopPurchase.item_id == item_id, ShopPurchase.revoked_at.is_(None)).limit(1)
    )
    return found is not None


async def overview(db: AsyncSession, user: User) -> dict[str, Any]:
    """GET /shop。items 是上架中的商品，加上我已经拥有的（哪怕已下架）。"""
    user_id, oid, level, equipped = user.id, _bound_oid(user), user_level(user), equipped_map(user)
    owned = await _owned_ids(db, user_id)
    clause = _on_sale_clause(_now())
    if owned:
        clause = or_(clause, ShopItem.id.in_(owned))
    items = (await db.execute(select(ShopItem).where(clause).order_by(ShopItem.sort.asc(), ShopItem.id.asc()))).scalars().all()
    return {
        "categories": categories_view(),
        "items": [_item_view(item, owned=item.id in owned, equipped=equipped, level=level) for item in items],
        "balance": await _balance(db, user_id, oid),
        "equipped": equipped,
        "donate_url": get_coin_ledger().topup_url(),
        "level": level,
    }


async def purchase(db: AsyncSession, user: User, item_id: int) -> dict[str, Any]:
    """POST /shop/items/{id}/purchase：买一件，或者免费领一件等级已经够了的。"""
    user_id, oid, level, equipped = user.id, _bound_oid(user), user_level(user), equipped_map(user)
    item = await db.get(ShopItem, item_id)
    if item is None:
        raise _item_not_found()
    if await _owns(db, user_id, item_id):
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

    now = _now()
    outcome = "ok"
    async with _write_lock():
        if source == "purchase":
            # 余额够才扣。单条语句在数据库里是原子的，写事务又是串行的：并发的两次购买，后一个看到的是前一个扣完之后的余额
            charged = await db.execute(
                update(ShopAccount)
                .where(
                    ShopAccount.user_id == user_id,
                    ShopAccount.ccw_oid == oid,
                    ShopAccount.donated - ShopAccount.spent - _shared_spent_query(user_id, oid).scalar_subquery() >= price,
                )
                .values(spent=ShopAccount.spent + price, updated_at=now)
                .execution_options(synchronize_session=False)
            )
            if charged.rowcount != 1:
                await db.rollback()
                outcome = "short"
        if outcome == "ok":
            db.add(
                ShopPurchase(
                    user_id=user_id, item_id=item_id, price=price, source=source, ccw_oid=oid if source == "purchase" else None, created_at=now
                )
            )
            try:
                # 发货和上面的扣款一起提交
                await db.commit()
            except IntegrityError:
                # 同一件商品的另一次购买抢先了（唯一索引）。回滚会把这次的扣款一起撤掉
                await db.rollback()
                outcome = "owned"
    if outcome == "owned":
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
    return {
        "item": _item_view(item, owned=True, equipped=equipped, level=level),
        "balance": await _balance(db, user_id, oid),
        "equipped": equipped,
    }


async def set_equipped(db: AsyncSession, user: User, slot: str, sku: str | None) -> dict[str, Any]:
    """PUT /shop/equipped：装备自己拥有的一件，sku 为 None 表示卸下。装备有变化时发 user.cosmetics.updated。"""
    if slot not in SLOTS:
        raise _error(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_slot", "没有这个装备槽")
    cosmetics = parse_cosmetics(user.cosmetics)
    if sku is None:
        changed = cosmetics.pop(slot, None) is not None
    else:
        item = (await db.execute(select(ShopItem).where(ShopItem.sku == sku))).scalar_one_or_none()
        if item is None:
            raise _item_not_found()
        if _slot(item) != slot:
            raise _error(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_slot", "这件商品不能装在这个槽里")
        # 下架不影响已经拥有的人，所以这里不看商品是否还在卖
        if not await _owns(db, user.id, item.id):
            raise _error(status.HTTP_403_FORBIDDEN, "not_owned", "只能装备自己拥有的商品")
        entry = {"sku": item.sku, "style": _style(item)}
        changed = cosmetics.get(slot) != entry
        cosmetics[slot] = entry
    if changed:
        _set_cosmetics(user, cosmetics)
        async with _write_lock():
            await db.commit()
        announce_cosmetics(user.id)
    return {"equipped": {key: entry["sku"] for key, entry in cosmetics.items()}}


def _purchase_view(row: ShopPurchase, item: ShopItem) -> dict[str, Any]:
    return {
        "id": row.id,
        "item": _brief(item),
        "price": row.price,
        "source": row.source,
        "created_at": _aware(row.created_at),
        "revoked_at": _aware(row.revoked_at),
        "refunded": bool(row.refunded),
    }


async def list_purchases(db: AsyncSession, user_id: int, limit: int, offset: int) -> dict[str, Any]:
    """GET /shop/purchases：我的获得记录，新的在前。被收回的也在里面（带 revoked_at）。"""
    total = await db.scalar(select(func.count()).select_from(ShopPurchase).where(ShopPurchase.user_id == user_id))
    rows = await db.execute(
        select(ShopPurchase, ShopItem)
        .join(ShopItem, ShopItem.id == ShopPurchase.item_id)
        .where(ShopPurchase.user_id == user_id)
        .order_by(ShopPurchase.created_at.desc(), ShopPurchase.id.desc())
        .offset(offset)
        .limit(limit)
    )
    return {"items": [_purchase_view(row, item) for row, item in rows.all()], "total": int(total or 0)}


# ---------------------------------------------------------------- 等级解锁


async def on_level_up(db: AsyncSession, user_id: int, level: int) -> list[dict[str, str]]:
    """账号升到 level 级之后调用：把到这一级为止解锁、还没得到过的商品发给用户，并发 shop.item.granted。

    返回这次发了哪些（[{sku, name, category}]）。要在调用方提交了自己的事务之后调：这里会自己提交，出错时会回滚这个会话。
    发不出去只记日志、返回已经发成的那些 —— 不能因为它让升级失败；漏发的商品用户还能在商店里手动领（obtainable = level）。
    被后台收回过的不会再自动发。
    """
    granted: list[dict[str, str]] = []
    try:
        held = select(ShopPurchase.item_id).where(ShopPurchase.user_id == user_id)
        rows = await db.execute(
            select(ShopItem.id, ShopItem.sku, ShopItem.name, ShopItem.category)
            .where(ShopItem.unlock_level.is_not(None), ShopItem.unlock_level <= level, _on_sale_clause(_now()), ShopItem.id.not_in(held))
            .order_by(ShopItem.unlock_level.asc(), ShopItem.sort.asc(), ShopItem.id.asc())
        )
        for item_id, sku, name, category in rows.all():
            async with _write_lock():
                db.add(ShopPurchase(user_id=user_id, item_id=item_id, price=0, source="level", created_at=_now()))
                try:
                    await db.commit()
                except IntegrityError:
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
    unlock_level / preview_text / tag / available_until / description 传 null 表示清空。
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
        .where(ShopPurchase.item_id == item.id, ShopPurchase.revoked_at.is_(None), User.cosmetics.is_not(None))
    )
    slot, style = _slot(item), _style(item)
    affected: list[int] = []
    for user in owners.scalars().all():
        cosmetics = parse_cosmetics(user.cosmetics)
        changed = False
        for key in list(cosmetics):
            if cosmetics[key]["sku"] != item.sku:
                continue
            if key != slot:
                # 类别换了，原来的槽里不能再放它
                del cosmetics[key]
                changed = True
            elif cosmetics[key]["style"] != style:
                cosmetics[key] = {"sku": item.sku, "style": style}
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
    """(每件商品卖出去多少份, 每件商品现在有多少人拥有)。卖出只算花金币买的，退款的不算。"""
    sold_query = select(ShopPurchase.item_id, func.count()).where(ShopPurchase.source == "purchase", ShopPurchase.refunded.is_(False))
    owners_query = select(ShopPurchase.item_id, func.count()).where(ShopPurchase.revoked_at.is_(None))
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


def _admin_purchase_view(row: ShopPurchase, item: ShopItem, user: User | None) -> dict[str, Any]:
    return {
        **_purchase_view(row, item),
        "user": _admin_user_view(user),
        "item_id": row.item_id,
        "note": row.note,
        "actor_id": row.actor_id,
        "revoked_by_id": row.revoked_by_id,
        "revoke_note": row.revoke_note,
    }


async def admin_list_purchases(db: AsyncSession, *, user_id: int | None, item_id: int | None, limit: int, offset: int) -> dict[str, Any]:
    """GET /admin/shop/purchases：全站的获得记录（含被收回的），新的在前。"""
    clauses = []
    if user_id is not None:
        clauses.append(ShopPurchase.user_id == user_id)
    if item_id is not None:
        clauses.append(ShopPurchase.item_id == item_id)
    total = await db.scalar(select(func.count()).select_from(ShopPurchase).where(*clauses))
    rows = await db.execute(
        select(ShopPurchase, ShopItem, User)
        .join(ShopItem, ShopItem.id == ShopPurchase.item_id)
        .outerjoin(User, User.id == ShopPurchase.user_id)
        .where(*clauses)
        .order_by(ShopPurchase.created_at.desc(), ShopPurchase.id.desc())
        .offset(offset)
        .limit(limit)
    )
    return {"items": [_admin_purchase_view(row, item, user) for row, item, user in rows.all()], "total": int(total or 0)}


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


async def admin_grant(db: AsyncSession, admin: User, user_id: int, item_id: int, note: str | None) -> dict[str, Any]:
    """POST /admin/shop/users/{id}/grant：送一件。下架的、不卖的也能送。"""
    admin_id = admin.id
    user, item = await _grant_target(db, user_id, item_id)
    if await _owns(db, user_id, item_id):
        raise _already_owned()
    brief = _brief(item)
    row = ShopPurchase(user_id=user_id, item_id=item_id, price=0, source="grant", actor_id=admin_id, note=_clean_note(note), created_at=_now())
    db.add(row)
    await log_admin_action(db, admin_id, "shop.grant", "user", user_id, {"item_id": item_id, "sku": brief["sku"], "note": row.note})
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise _already_owned() from None
    await _send_granted(user_id, brief, "grant")
    return _admin_purchase_view(row, item, user)


async def admin_revoke(db: AsyncSession, admin: User, user_id: int, item_id: int, note: str | None, refund: bool = False) -> dict[str, Any]:
    """POST /admin/shop/users/{id}/revoke：收回一件，装备着的话同时卸下。

    默认不退金币；refund=True 而且这件是花金币买的，才把这一笔从已花里减掉。
    """
    admin_id = admin.id
    user, item = await _grant_target(db, user_id, item_id)
    row = (
        await db.execute(
            select(ShopPurchase).where(ShopPurchase.user_id == user_id, ShopPurchase.item_id == item_id, ShopPurchase.revoked_at.is_(None))
        )
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
    cosmetics = parse_cosmetics(user.cosmetics)
    worn = [slot for slot, entry in cosmetics.items() if entry["sku"] == item.sku]
    for slot in worn:
        del cosmetics[slot]
    if worn:
        _set_cosmetics(user, cosmetics)
    await log_admin_action(
        db, admin_id, "shop.revoke", "user", user_id, {"item_id": item_id, "sku": item.sku, "note": row.revoke_note, "refunded": refunded, "price": row.price}
    )
    await db.commit()
    if worn:
        announce_cosmetics(user_id)
    return _admin_purchase_view(row, item, user)


# ---------------------------------------------------------------- 后台：账户与对账


def _account_columns() -> tuple[Any, Any, Any, Any]:
    """账户列表用的几个 SQL 表达式，口径和 _balance 一致：(是否按现在绑定的账号核实的, 有效累计投币, 被别的账号花掉的, 余额)。"""
    current = and_(User.ccw_student_oid.is_not(None), User.ccw_student_oid == ShopAccount.ccw_oid)
    donated = case((current, ShopAccount.donated), else_=0)
    shared = _shared_spent_query(User.id, User.ccw_student_oid).correlate(User).scalar_subquery()
    return current, donated, shared, donated - ShopAccount.spent - shared


def _account_view(account: ShopAccount, user: User, donated: int, shared: int) -> dict[str, Any]:
    oid = _bound_oid(user)
    current = oid is not None and account.ccw_oid == oid
    balance = donated - account.spent - shared
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
    }


async def admin_list_accounts(db: AsyncSession, *, q: str | None, mismatch: bool | None, limit: int, offset: int) -> dict[str, Any]:
    """GET /admin/shop/accounts：每个用户的累计投币、已花、余额、上次核实时间。mismatch=True 只看花的比投的多的。

    只有核实过余额（或者对账时对上了投币记录）的用户才有账户，没出现在这里的人就是没投过也没花过。
    """
    _current, donated, shared, balance = _account_columns()
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
        select(ShopAccount, User, donated, shared)
        .join(User, User.id == ShopAccount.user_id)
        .where(*clauses)
        .order_by(balance.asc(), ShopAccount.user_id.asc())
        .offset(offset)
        .limit(limit)
    )
    return {
        "items": [_account_view(account, user, int(row_donated or 0), int(row_shared or 0)) for account, user, row_donated, row_shared in rows.all()],
        "total": int(total or 0),
    }


async def admin_reconcile(db: AsyncSession, admin: User) -> dict[str, Any]:
    """POST /admin/shop/reconcile：向余额来源拉一次完整的名单，更新所有已绑定用户的累计投币。

    只对已有账户的人和名单上有名字的人动手；绑定了但没投过币、也没有账户的人不用建一行。拉不到完整名单时 503，什么都不改。
    """
    admin_id = admin.id
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
    return {"donors": len(donations), "matched": matched, "updated": updated, "mismatched": mismatched["items"]}


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
        "price_max": PRICE_MAX,
        "unlock_level_max": UNLOCK_LEVEL_MAX,
    }


# ---------------------------------------------------------------- 初始商品


def _gradient(angle: int, *stops: str) -> str:
    return f"linear-gradient({angle}deg, {', '.join(stops)})"


# 一期的 8 件气泡皮肤，按档次递进：便宜的两件是干净的配色，中档的带纹样 / 一个角饰，
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
