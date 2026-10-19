"""礼物（docs/gifts.md）：花金币给别人送礼物，或者送一件商店商品。送礼是真消耗 —— 对方不得币，只涨心意值（users.charm）。

钱怎么记（表在 app/models/gift.py）：

- 每次送礼是 gift_records 的一行，amount 是花掉的币。余额公式里「送礼花掉的」直接对这张表求和
  （services/shop.py 的 _gift_out_query / available_expression），没有另外的余额列。
- 收礼人的心意值冗余在 users.charm 上（用户对象在消息等热路径上，序列化时不能查别的表），
  和记录在同一个事务里用 UPDATE ... SET charm = charm + amount 加上去。守恒：每个人的 charm = 他收到的记录的 amount 之和，
  对账（ledger_check）核对这一条。
- 换绑防刷：记录上记着送的时候绑定的 CCW 账号（ccw_oid）。同一个 CCW 账号在别的 KukeChat 账号上送礼花掉的，
  算进这个账号的已花里（shop._shared_gift_query），和商店购买、红包是同一条规则。

并发（线上两个 worker 进程，SQLite 的写事务全局串行）：和红包、商店购买一样，写事务的**第一条**语句是带条件的那条 UPDATE
（`UPDATE shop_accounts ... WHERE 余额 >= 花费`），事务从这条语句起拿着数据库的写锁直到提交，条件是对着最新的数据算的。
影响 0 行就是余额不够，整个事务回滚；影响 1 行才接着写记录、加心意值、（送商品时）给对方记拥有、（有会话时）落消息，一起提交。
用的是和购买、发红包同一个余额表达式，所以送礼与送礼、送礼与购买、送礼与发红包之间都不会双花。
写事务都先过 shop._write_lock() 再进 SQLite；向 CCW 核实余额、文本审核这些要等外部的事都在拿锁之前做完。
"""

from __future__ import annotations

import json
import logging
import re
import time
import unicodedata
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import localtime
from app.core.config import settings
from app.core.database import release_connection
from app.core.dialect import lock_for_update
from app.integrations import get_coin_ledger
from app.models.conversation import Conversation, ConversationMember, TemporaryConversationBlock
from app.models.enums import ConversationType, MemberRole, MessageType
from app.models.gift import Gift, GiftRecord
from app.models.message import Message
from app.models.shop import ShopAccount, ShopItem
from app.models.system import SystemSetting
from app.models.user import User
from app.schemas.user import UserPublic
from app.services import shop as shop_service
from app.services.penalties import mute_state
from app.services.reports import log_admin_action

logger = logging.getLogger(__name__)
UTC = timezone.utc

KINDS = ("gift", "item")
# 一次最多送多少个（送商品固定 1）
COUNT_MAX = 99
# 收到礼物涨的那个数值对外叫什么。界面文案只有这一处，以后改名改这里
CHARM_LABEL = "心意值"
KEY_RE = re.compile(r"[a-z0-9._-]{1,32}")
ICON_RE = re.compile(r"[a-z0-9_-]{1,32}")
NAME_MAX = 20
TIERS = (1, 2, 3)
STAFF_ROLES = {MemberRole.OWNER, MemberRole.ADMIN}
RECEIVED_EVENT = "gift.received"
# 心意榜一次最多给多少名
BOARD_MAX = 100

# —— 开关与限制：存在 system_settings 表里，后台可以随时改 ——
DEFAULTS: dict[str, Any] = {"enabled": False, "daily_send_limit": 200, "message_max": 60}
BOUNDS: dict[str, tuple[int, int]] = {"daily_send_limit": (1, 10000), "message_max": (1, 200)}
_KEY_PREFIX = "gifts."
# 服务跑两个 worker 进程，另一个进程改了设置后最多这么久才能看到
CACHE_SECONDS = 5.0

_cache: dict[str, Any] = {"at": 0.0, "values": None}
# 心意榜的缓存：period -> (算的时刻 time.monotonic(), 本月榜是哪个月的, [(用户 id, 心意值)] 从高到低)
_boards: dict[str, tuple[float, str, list[tuple[int, int]]]] = {}

# 首批礼物。icon 是客户端内置矢量图形的名字；另外内置但暂不上架的有 star / heart / balloon / crown / cat / icecream
DEFAULT_GIFTS: tuple[dict[str, Any], ...] = (
    {"key": "flower", "name": "小花", "price": 1, "icon": "flower", "tier": 1},
    {"key": "lollipop", "name": "棒棒糖", "price": 5, "icon": "lollipop", "tier": 1},
    {"key": "cake", "name": "蛋糕", "price": 20, "icon": "cake", "tier": 2},
    {"key": "trophy", "name": "奖杯", "price": 50, "icon": "trophy", "tier": 2},
    {"key": "rocket", "name": "火箭", "price": 100, "icon": "rocket", "tier": 3},
    {"key": "castle", "name": "城堡", "price": 300, "icon": "castle", "tier": 3},
)


def reset_cache() -> None:
    _cache["at"] = 0.0
    _cache["values"] = None


def reset_runtime_state() -> None:
    """清掉进程内的状态（测试用）。"""
    reset_cache()
    _boards.clear()


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(value: datetime | None) -> datetime | None:
    """SQLite 读出来的时间不带时区，统一当 UTC。"""
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _error(status_code: int, code: str, message: str, **extra: Any) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message, **extra})


def _unprocessable(code: str, message: str, **extra: Any) -> HTTPException:
    return _error(status.HTTP_422_UNPROCESSABLE_ENTITY, code, message, **extra)


def _not_member() -> HTTPException:
    return _error(status.HTTP_403_FORBIDDEN, "not_member", "你或对方不在这个会话里")


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


async def admin_settings(db: AsyncSession) -> dict[str, Any]:
    """GET /admin/gifts/settings。"""
    return await get_settings(db, fresh=True)


async def admin_update_settings(db: AsyncSession, admin: User, changes: dict[str, Any]) -> dict[str, Any]:
    """PUT /admin/gifts/settings：只处理传了的项，有变化才写审计日志。返回改完之后的全部设置。"""
    admin_id = admin.id
    before = await get_settings(db, fresh=True)
    cleaned: dict[str, Any] = {}
    for name, value in changes.items():
        if name not in DEFAULTS or value is None:
            continue
        if name == "enabled":
            if not isinstance(value, bool):
                raise _unprocessable("invalid_settings", "enabled 只能是 true / false", field=name)
            cleaned[name] = value
            continue
        low, high = BOUNDS[name]
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise _unprocessable("invalid_settings", f"{name} 要在 {low}–{high} 之间", field=name)
        cleaned[name] = value
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
            db, admin_id, "gift.settings.update", "system_setting", None, {name: {"old": before[name], "new": value} for name, value in changed.items()}
        )
        async with shop_service._write_lock():
            await db.commit()
    reset_cache()
    return await get_settings(db, fresh=True)


async def _sent_today(db: AsyncSession, user_id: int, now: datetime) -> int:
    """今天（UTC+8）送了几次。礼物和商品合在一起，按次数算（一次送 99 个也是一次）。"""
    count = await db.scalar(
        select(func.count()).select_from(GiftRecord).where(GiftRecord.from_user_id == user_id, GiftRecord.created_at >= localtime.day_start(now))
    )
    return int(count or 0)


# ---------------------------------------------------------------- 目录


def _gift_view(gift: Gift) -> dict[str, Any]:
    return {"key": gift.key, "name": gift.name, "price": gift.price, "icon": gift.icon, "tier": gift.tier, "is_active": bool(gift.is_active), "sort": gift.sort}


async def _all_gifts(db: AsyncSession) -> list[Gift]:
    return list((await db.execute(select(Gift).order_by(Gift.sort.asc(), Gift.id.asc()))).scalars().all())


async def catalog_view(db: AsyncSession, user: User) -> dict[str, Any]:
    """GET /gifts/catalog。开关关着也能调（enabled: false）；gifts 只有上架中的。"""
    config = await get_settings(db)
    return {
        "enabled": config["enabled"],
        "charm_label": CHARM_LABEL,
        "gifts": [_gift_view(gift) for gift in await _all_gifts(db) if gift.is_active],
        "limits": {"message_max": config["message_max"], "count_max": COUNT_MAX, "daily_send_limit": config["daily_send_limit"]},
        "sent_today": await _sent_today(db, user.id, _now()),
    }


async def ensure_default_gifts(db: AsyncSession) -> int:
    """启动时播种首批礼物，返回新建了几个。幂等：按 key，已经有的（哪怕被后台改过、下架了）一概不动。出错只记日志。"""
    try:
        keys = [spec["key"] for spec in DEFAULT_GIFTS]
        existing = set((await db.execute(select(Gift.key).where(Gift.key.in_(keys)))).scalars().all())
        now = _now()
        created = 0
        for index, spec in enumerate(DEFAULT_GIFTS):
            if spec["key"] in existing:
                continue
            db.add(Gift(**spec, sort=(index + 1) * 10, created_at=now, updated_at=now))
            created += 1
        if created:
            await db.commit()
        return created
    except IntegrityError:
        # 多个 worker 同时启动，别的进程刚播种过
        await db.rollback()
        return 0
    except Exception:
        logger.exception("Failed to seed default gifts")
        await db.rollback()
        return 0


def _invalid_gift_field(field: str, message: str) -> HTTPException:
    return _unprocessable("invalid_gift", message, field=field)


def _clean_gift(data: dict[str, Any], gift: Gift | None) -> dict[str, Any]:
    """校验后台提交的礼物字段。gift 为 None 是新建（全部必填，is_active / sort 除外），否则是修改（只处理传了的、不为 null 的）。"""
    creating = gift is None
    values: dict[str, Any] = {}
    key = data.get("key")
    if creating:
        if not isinstance(key, str) or KEY_RE.fullmatch(key) is None:
            raise _invalid_gift_field("key", "key 只能用小写字母、数字和 . _ -，最长 32 个字符")
        values["key"] = key
    elif key is not None and key != gift.key:
        raise _unprocessable("key_immutable", "key 建好之后不能改", field="key")
    if data.get("name") is not None or creating:
        name = data.get("name")
        name = name.strip() if isinstance(name, str) else ""
        if not name or len(name) > NAME_MAX:
            raise _invalid_gift_field("name", f"名字不能为空，最多 {NAME_MAX} 个字")
        values["name"] = name
    if data.get("price") is not None or creating:
        price = data.get("price")
        if isinstance(price, bool) or not isinstance(price, int) or not 1 <= price <= shop_service.PRICE_MAX:
            raise _invalid_gift_field("price", f"价格要在 1–{shop_service.PRICE_MAX} 之间")
        values["price"] = price
    if data.get("icon") is not None or creating:
        icon = data.get("icon")
        if not isinstance(icon, str) or ICON_RE.fullmatch(icon) is None:
            raise _invalid_gift_field("icon", "图标是客户端内置图形的名字：小写字母、数字、_ -，最长 32 个字符")
        values["icon"] = icon
    if data.get("tier") is not None or creating:
        tier = data.get("tier")
        if isinstance(tier, bool) or tier not in TIERS:
            raise _invalid_gift_field("tier", "动效档位只能是 1 / 2 / 3")
        values["tier"] = tier
    if data.get("is_active") is not None:
        if not isinstance(data["is_active"], bool):
            raise _invalid_gift_field("is_active", "is_active 只能是 true / false")
        values["is_active"] = data["is_active"]
    if data.get("sort") is not None:
        sort = data["sort"]
        if isinstance(sort, bool) or not isinstance(sort, int) or not -shop_service.SORT_MAX <= sort <= shop_service.SORT_MAX:
            raise _invalid_gift_field("sort", f"排序要在 {-shop_service.SORT_MAX}–{shop_service.SORT_MAX} 之间")
        values["sort"] = sort
    return values


async def admin_catalog(db: AsyncSession) -> dict[str, Any]:
    """GET /admin/gifts/catalog：全部礼物，含已下架的。"""
    return {"gifts": [_gift_view(gift) for gift in await _all_gifts(db)]}


async def admin_create_gift(db: AsyncSession, admin: User, data: dict[str, Any]) -> dict[str, Any]:
    admin_id = admin.id
    values = _clean_gift(data, None)
    now = _now()
    gift = Gift(**values, created_at=now, updated_at=now)
    db.add(gift)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise _error(status.HTTP_409_CONFLICT, "key_exists", "这个 key 已经有了", field="key") from None
    await log_admin_action(db, admin_id, "gift.catalog.create", "gift", gift.id, {"key": gift.key, "name": gift.name, "price": gift.price})
    await db.commit()
    return _gift_view(gift)


async def admin_update_gift(db: AsyncSession, admin: User, key: str, data: dict[str, Any]) -> dict[str, Any]:
    """PUT /admin/gifts/catalog/{key}：只改传了的字段；key 不能改，礼物只能下架不能删。改名、改价不影响已经送出的记录。"""
    admin_id = admin.id
    gift = (await db.execute(select(Gift).where(Gift.key == key))).scalar_one_or_none()
    if gift is None:
        raise _error(status.HTTP_404_NOT_FOUND, "gift_not_found", "没有这个礼物")
    values = _clean_gift(data, gift)
    changed = {name: {"old": getattr(gift, name), "new": value} for name, value in values.items() if getattr(gift, name) != value}
    if changed:
        for name, change in changed.items():
            setattr(gift, name, change["new"])
        gift.updated_at = _now()
        await log_admin_action(db, admin_id, "gift.catalog.update", "gift", gift.id, {"key": gift.key, "changed": changed})
        await db.commit()
    return _gift_view(gift)


# ---------------------------------------------------------------- 展示


def _gift_ref(record: GiftRecord) -> dict[str, Any] | None:
    """记录上的礼物：送的时候它的样子。"""
    if record.kind != "gift" or not record.gift_key:
        return None
    return {"key": record.gift_key, "name": record.gift_name or "礼物", "icon": record.gift_icon or "", "price": record.gift_price or 0, "tier": record.gift_tier or 1}


def _item_ref(record: GiftRecord) -> dict[str, Any] | None:
    if record.kind != "item" or not record.item_sku:
        return None
    try:
        style = json.loads(record.item_style or "{}")
    except ValueError:
        style = {}
    return {
        "sku": record.item_sku,
        "name": record.item_name or "",
        "category": record.item_category or "",
        "style": style if isinstance(style, dict) else {},
        "duration_days": record.item_duration_days,
    }


async def _users(db: AsyncSession, user_ids: set[int], *, fresh: bool = False) -> dict[int, User]:
    if not user_ids:
        return {}
    query = select(User).where(User.id.in_(user_ids))
    if fresh:
        # 刚用 UPDATE 改过心意值：这个会话里缓存着的对象还是旧的，重新读出来
        query = query.execution_options(populate_existing=True)
    return {user.id: user for user in (await db.execute(query)).scalars().all()}


def _public(user: User | None) -> UserPublic | None:
    return UserPublic.model_validate(user) if user is not None else None


def record_view(record: GiftRecord, users: dict[int, User], viewer_id: int | None) -> dict[str, Any]:
    """契约里的 GiftRecord。留言只有送的人和收的人能看到：viewer_id 不是这两个人（或者是 None）时 message 一律为 null。

    每一个把 GiftRecord 给出去的地方都从这里过，留言的可见性只在这一处判断。
    """
    party = viewer_id is not None and viewer_id in (record.from_user_id, record.to_user_id)
    return {
        "id": record.id,
        "kind": record.kind,
        "gift": _gift_ref(record),
        "item": _item_ref(record),
        "count": record.count,
        "amount": record.amount,
        "from_user": _public(users.get(record.from_user_id)),
        "to_user": _public(users.get(record.to_user_id)),
        "conversation_id": record.conversation_id,
        "message_id": record.message_id,
        "message": (record.message or None) if party else None,
        "on_birthday": bool(record.on_birthday),
        "created_at": _aware(record.created_at),
    }


async def _views(db: AsyncSession, records: list[GiftRecord], viewer_id: int | None) -> list[dict[str, Any]]:
    users = await _users(db, {user_id for record in records for user_id in (record.from_user_id, record.to_user_id)})
    return [record_view(record, users, viewer_id) for record in records]


def _display_name(user: User) -> str:
    return (user.nickname or "").strip() or user.username


def fallback_text(to_name: str, record: GiftRecord) -> str:
    """消息正文：给不认识 gift 类型的旧客户端、搜索和通知预览用。"""
    if record.kind == "item":
        return f"[礼物] 送给 {to_name} 「{record.item_name}」"
    return f"[礼物] 送给 {to_name} {record.gift_name}" + (f" ×{record.count}" if record.count > 1 else "")


# ---------------------------------------------------------------- 送


def _positive_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _clean_message(value: Any, maximum: int) -> str | None:
    """留言：零宽字符删掉，换行和连续空白折成一个空格。空的当成没写。"""
    if value is None:
        return None
    if not isinstance(value, str):
        raise _unprocessable("message_rejected", "留言格式不对")
    text = re.sub(r"\s+", " ", "".join(char for char in value if unicodedata.category(char) != "Cf")).strip()
    if len(text) > maximum:
        raise _unprocessable("message_rejected", f"留言最多 {maximum} 个字", message_max=maximum)
    return text or None


async def _ensure_message_allowed(db: AsyncSession, user: User, message: str | None, to_user_id: int) -> None:
    """留言先过一遍文本审核，明确违规的不让送。审核服务不可用时放行（落了消息的话，消息发出去之后还会走一次异步审核）。"""
    if not message or not settings.content_moderation_enabled or user.content_moderation_whitelisted:
        return
    from app.services import moderation as moderation_service

    # 审核服务可能要几秒：先结束请求的读事务、把数据库连接还回去，不带着连接等
    await release_connection(db)
    try:
        result = await moderation_service._call_text_moderation_for_user(message, user.id, context_id=f"gift:{to_user_id}")
    except Exception:
        return
    if result.get("decision") == "block":
        raise _unprocessable("message_rejected", "留言未通过审核，请修改后重试")


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
    return _error(status.HTTP_429_TOO_MANY_REQUESTS, "daily_limit", f"今天送礼的次数用完了（每天最多 {limit} 次）", daily_send_limit=limit)


async def _is_member(db: AsyncSession, conversation_id: int, user_id: int) -> ConversationMember | None:
    return (
        await db.execute(select(ConversationMember).where(ConversationMember.conversation_id == conversation_id, ConversationMember.user_id == user_id))
    ).scalar_one_or_none()


async def _blocked_by(db: AsyncSession, blocker_id: int, blocked_id: int) -> bool:
    """对方有没有把我拉黑。用的是现有的拉黑关系（temporary_conversation_blocks：在临时会话里点的「拉黑」，发起临时会话时查的也是它）。"""
    found = await db.scalar(
        select(TemporaryConversationBlock.id).where(
            TemporaryConversationBlock.blocker_id == blocker_id, TemporaryConversationBlock.blocked_user_id == blocked_id
        )
    )
    return found is not None


async def send_gift(db: AsyncSession, user: User, payload: dict[str, Any]) -> tuple[dict[str, Any], Message | None]:
    """POST /gifts：扣币、写记录、给对方加心意值、（送商品时）给对方记拥有、（有会话时）落一条消息，都在一个事务里。

    返回 (契约里的 { record, balance }, 那条消息或 None)；消息的广播和接口返回里的 message 由接口层做，和发普通消息一样。
    检查顺序：disabled → invalid_target → not_member → muted → blocked → ccw_not_bound → invalid_gift → invalid_count →
    not_giftable → message_rejected（长度）→ recipient_owns → message_rejected（审核）→ daily_limit → 503 → 402。
    除了成功，任何一种结果都不扣币、不写记录、不落消息。
    """
    user_id, oid = user.id, shop_service._bound_oid(user)
    config = await get_settings(db)
    if not config["enabled"]:
        raise _error(status.HTTP_403_FORBIDDEN, "disabled", "礼物功能还没有开放")

    # 「我的设备」会话里不能送礼（docs/self-chat.md 第 4 节）：先说清楚原因，不报「不能送给这个人」
    raw_conversation_id = _positive_int(payload.get("conversation_id"))
    if raw_conversation_id is not None and await _is_member(db, raw_conversation_id, user_id) is not None:
        target_conversation = await db.get(Conversation, raw_conversation_id)
        if target_conversation is not None and target_conversation.type == ConversationType.SELF:
            from app.services.conversations import self_chat_unsupported

            raise self_chat_unsupported("送礼物")
    to_user_id = _positive_int(payload.get("to_user_id"))
    to_user = await db.get(User, to_user_id) if to_user_id is not None and to_user_id != user_id else None
    # 机器人收不了礼物：没有人会看到，币白花
    if to_user is None or to_user.is_bot:
        raise _unprocessable("invalid_target", "不能送给这个人")
    # 后面的事务可能回滚，回滚之后 to_user 的属性不能再读：要用的先取出来
    to_name, to_avatar, on_birthday = _display_name(to_user), to_user.avatar_url, bool(to_user.is_birthday_today)

    conversation_id = payload.get("conversation_id")
    conversation_type: str | None = None
    if conversation_id is not None:
        conversation_id = _positive_int(conversation_id)
        conversation = await db.get(Conversation, conversation_id) if conversation_id is not None else None
        member = await _is_member(db, conversation_id, user_id) if conversation is not None else None
        # 双方都得在这个会话里（私聊的话，对方就是收礼人）
        if conversation is None or member is None or await _is_member(db, conversation_id, to_user_id) is None:
            raise _not_member()
        conversation_type = "group" if conversation.type == ConversationType.GROUP else "direct"
    # 被平台禁言的人不能送（留言是说话的一种）；在群里送还要看群内禁言
    until = mute_state(user)
    if until is not None:
        raise _error(status.HTTP_403_FORBIDDEN, "muted", "你正在被禁言，暂时不能送礼", muted_until=until.isoformat())
    if conversation_type == "group" and member.role not in STAFF_ROLES:
        from app.services.conversations import _member_mute_expired

        if conversation.all_muted or (member.muted and not _member_mute_expired(member)):
            raise _error(status.HTTP_403_FORBIDDEN, "muted", "你在这个群里被禁言了，或者群开了全员禁言")
    if await _blocked_by(db, to_user_id, user_id):
        raise _error(status.HTTP_403_FORBIDDEN, "blocked", "对方把你拉黑了，不能给 TA 送礼")
    if oid is None:
        raise _error(status.HTTP_403_FORBIDDEN, "ccw_not_bound", "先绑定 CCW 账号才能送礼")

    kind = payload.get("kind")
    gift: Gift | None = None
    item: ShopItem | None = None
    if kind == "gift":
        key = payload.get("gift_key")
        gift = (await db.execute(select(Gift).where(Gift.key == key))).scalar_one_or_none() if isinstance(key, str) else None
    if kind not in KINDS or (kind == "gift" and (gift is None or not gift.is_active)):
        raise _unprocessable("invalid_gift", "没有这个礼物")
    count = payload.get("count")
    if kind == "item" and count is None:
        count = 1
    count = _positive_int(count)
    if count is None or count > COUNT_MAX or (kind == "item" and count != 1):
        raise _unprocessable("invalid_count", "送商品一次只能送 1 件" if kind == "item" else f"数量要在 1–{COUNT_MAX} 之间", count_max=COUNT_MAX)
    if kind == "item":
        sku = payload.get("item_sku")
        item = (await db.execute(select(ShopItem).where(ShopItem.sku == sku))).scalar_one_or_none() if isinstance(sku, str) else None
        # 不卖的（只能等级解锁 / 赠送）、已下架的、过了限时销售期的都不能送
        if item is None or item.price <= 0 or not shop_service._available(item, _now()):
            raise _unprocessable("not_giftable", "这件商品不能送")
    message_text = _clean_message(payload.get("message"), config["message_max"])
    if item is not None and await shop_service.owns_permanently(db, to_user_id, item.id):
        raise _error(status.HTTP_409_CONFLICT, "recipient_owns", "对方已经永久拥有这件商品了")
    amount = item.price if item is not None else gift.price * count
    # 下面的事务可能回滚，回滚之后这些对象的属性不能再读：先取成普通变量
    snapshot: dict[str, Any] = {"kind": kind, "count": count, "amount": amount}
    if gift is not None:
        snapshot.update(gift_key=gift.key, gift_name=gift.name, gift_icon=gift.icon, gift_price=gift.price, gift_tier=gift.tier)
    else:
        snapshot.update(
            item_id=item.id, item_sku=item.sku, item_name=item.name, item_category=item.category, item_style=item.style, item_duration_days=item.duration_days
        )

    await _ensure_message_allowed(db, user, message_text, to_user_id)
    if await _sent_today(db, user_id, _now()) >= config["daily_send_limit"]:
        raise _daily_limit(config["daily_send_limit"])
    # 和商店购买一样：累计投币得是刚向 CCW 核实过的，核实不了就 503，不扣币
    await shop_service._fresh_account(db, user_id, oid)

    outcome = "retry"
    record_id: int | None = None
    chat: Message | None = None
    for _attempt in range(shop_service.OWNERSHIP_ATTEMPTS):
        # 上面（或者上一轮）可能回滚过这个会话，回滚会让 user 过期；下面发消息要读它的属性，先重新读出来
        await db.refresh(user)
        now = _now()
        outcome = "ok"
        chat = None
        async with shop_service._write_lock():
            try:
                # PG：先锁住自己的账户行，下面的余额检查才看得到别的进程刚提交的花币（见 lock_for_update）
                await lock_for_update(db, select(ShopAccount.user_id).where(ShopAccount.user_id == user_id))
                # 余额够才往下走。这是事务的第一条语句：从这里到提交一直拿着写锁，条件是对着最新的数据算的；
                # 花掉的币记在下面插入的那行记录里（余额公式对它求和），这条语句本身只是把关 + 占住写锁
                charged = await db.execute(
                    update(ShopAccount)
                    .where(ShopAccount.user_id == user_id, ShopAccount.ccw_oid == oid, shop_service.available_expression(user_id, oid) >= amount)
                    .values(updated_at=now)
                    .execution_options(synchronize_session=False)
                )
                if charged.rowcount != 1:
                    await db.rollback()
                    outcome = "short"
                elif await _sent_today(db, user_id, now) >= config["daily_send_limit"]:
                    # 拿到写锁之后再数一遍：同时送出来的几个请求不能一起越过每日上限
                    await db.rollback()
                    outcome = "limit"
                else:
                    record = GiftRecord(
                        **snapshot,
                        from_user_id=user_id,
                        to_user_id=to_user_id,
                        conversation_id=conversation_id,
                        conversation_type=conversation_type,
                        message=message_text,
                        on_birthday=on_birthday,
                        ccw_oid=oid,
                        created_at=now,
                    )
                    db.add(record)
                    await db.flush()
                    record_id = record.id
                    # 心意值和记录一起提交：charm 永远等于收到的记录的 amount 之和。updated_at 保持原样（它是「资料改动时间」）
                    await db.execute(
                        update(User)
                        .where(User.id == to_user_id)
                        .values(charm=User.charm + amount, updated_at=User.updated_at)
                        .execution_options(synchronize_session=False)
                    )
                    if kind == "item":
                        # 对方直接获得拥有权（不自动装备）。限时商品加 N 天，已经有的话是续期；已经永久拥有会抛 _PermanentlyOwned
                        await shop_service._grant_ownership(
                            db,
                            user_id=to_user_id,
                            item_id=snapshot["item_id"],
                            sku=snapshot["item_sku"],
                            days=snapshot["item_duration_days"],
                            now=now,
                            source="gift",
                            from_user_id=user_id,
                            gift_record_id=record_id,
                        )
                    if conversation_id is not None:
                        # 走正常的发消息路径（成员、禁言、限频、群活跃度都和普通消息一致），但不让它自己提交
                        from app.services import messages as message_service

                        chat = await message_service.send_message(
                            db,
                            conversation_id,
                            user,
                            fallback_text(to_name, record),
                            MessageType.GIFT,
                            {
                                "gift": {
                                    "id": record_id,
                                    "kind": kind,
                                    "gift": _gift_ref(record),
                                    "item": _item_ref(record),
                                    "count": count,
                                    "to_user": {"id": to_user_id, "display_name": to_name, "avatar_url": to_avatar},
                                    "message": message_text or "",
                                }
                            },
                            allow_gift=True,
                            commit=False,
                        )
                        record.message_id = chat.id
                    await db.commit()
            except shop_service._PermanentlyOwned:
                await db.rollback()
                outcome = "owns"
            except (IntegrityError, shop_service._OwnershipChanged):
                # 对方刚好自己买了 / 续了这一件（另一个进程）：整个事务回滚，按最新的状态重来
                await db.rollback()
                outcome = "retry"
            except HTTPException as exc:
                # 发消息那一步被拒了（限频、禁言）：整个事务回滚，没有扣币
                await db.rollback()
                raise _send_error(exc) from None
            except BaseException:
                await db.rollback()
                raise
        if outcome != "retry":
            break
    if outcome == "limit":
        raise _daily_limit(config["daily_send_limit"])
    if outcome in ("owns", "retry"):
        raise _error(status.HTTP_409_CONFLICT, "recipient_owns", "对方已经永久拥有这件商品了")
    if outcome == "short":
        balance = (await shop_service._balance(db, user_id, oid))["balance"]
        raise _error(
            status.HTTP_402_PAYMENT_REQUIRED,
            "insufficient_balance",
            "金币不够",
            balance=balance,
            total=amount,
            shortfall=max(amount - balance, 0),
            donate_url=get_coin_ledger().topup_url(),
        )
    assert record_id is not None
    record = await db.get(GiftRecord, record_id)
    # 记录里的 to_user.charm 是送完之后的新值
    users = await _users(db, {user_id, to_user_id}, fresh=True)
    shop_service.announce_balance(user_id, "gift.sent", gift_id=record_id)
    # gift.received 只发给收礼人（无论有没有落消息）。别人送的商品不另发 shop.item.granted，免得弹两条提示
    shop_service._spawn(_send_received(to_user_id, record_view(record, users, to_user_id)))
    _boards.clear()
    return {"record": record_view(record, users, user_id), "balance": await shop_service._balance(db, user_id, oid)}, chat


async def _send_received(user_id: int, record: dict[str, Any]) -> None:
    from app.websocket.manager import manager

    try:
        await manager.send_to_user(user_id, RECEIVED_EVENT, {"record": record})
    except Exception:
        # 推送是尽力而为：礼物已经落库，对方下次拉记录就能看到
        logger.exception("Failed to send gift received event", extra={"user_id": user_id})


# ---------------------------------------------------------------- 礼物墙、记录


async def _target(db: AsyncSession, user_id: int) -> User:
    user = await db.get(User, user_id)
    if user is None:
        raise _error(status.HTTP_404_NOT_FOUND, "user_not_found", "用户不存在")
    return user


async def wall(db: AsyncSession, user_id: int) -> dict[str, Any]:
    """GET /users/{id}/gifts：这个人收到的礼物，按种类聚合，公开。不含留言。

    summary 按单价从高到低排；每一种展示的是最近一次收到时它的样子。total_count 是收到的礼物件数（各种的 count 之和）。
    """
    user = await _target(db, user_id)
    rows = (
        await db.execute(
            select(func.sum(GiftRecord.count), func.max(GiftRecord.created_at), func.max(GiftRecord.id))
            .where(GiftRecord.to_user_id == user_id)
            .group_by(GiftRecord.kind, GiftRecord.gift_key, GiftRecord.item_sku)
        )
    ).all()
    latest_ids = [row[2] for row in rows]
    latest = {record.id: record for record in (await db.execute(select(GiftRecord).where(GiftRecord.id.in_(latest_ids)))).scalars().all()} if latest_ids else {}
    summary = []
    for count, last_at, latest_id in rows:
        record = latest.get(latest_id)
        if record is None:
            continue
        unit = record.amount / record.count if record.count else record.amount
        summary.append((unit, {"kind": record.kind, "gift": _gift_ref(record), "item": _item_ref(record), "count": int(count or 0), "last_at": _aware(last_at)}))
    summary.sort(key=lambda entry: (-entry[0], -(entry[1]["last_at"].timestamp() if entry[1]["last_at"] else 0)))
    return {"charm": int(user.charm or 0), "total_count": sum(entry["count"] for _unit, entry in summary), "summary": [entry for _unit, entry in summary]}


async def wall_records(
    db: AsyncSession, viewer: User, user_id: int, *, gift_key: str | None, item_sku: str | None, limit: int, offset: int
) -> dict[str, Any]:
    """GET /users/{id}/gifts/records：谁送的、送了什么、什么时候，公开，新的在前。留言只有送的人和收的人自己看得到。"""
    viewer_id = viewer.id
    await _target(db, user_id)
    clauses = [GiftRecord.to_user_id == user_id]
    if gift_key:
        clauses.append(GiftRecord.gift_key == gift_key)
    if item_sku:
        clauses.append(GiftRecord.item_sku == item_sku)
    total = await db.scalar(select(func.count()).select_from(GiftRecord).where(*clauses))
    records = list(
        (await db.execute(select(GiftRecord).where(*clauses).order_by(GiftRecord.created_at.desc(), GiftRecord.id.desc()).offset(offset).limit(limit)))
        .scalars()
        .all()
    )
    return {"items": await _views(db, records, viewer_id), "total": int(total or 0)}


async def list_mine(db: AsyncSession, user: User, direction: str, limit: int, offset: int) -> dict[str, Any]:
    """GET /gifts/mine：我送出的 / 我收到的，新的在前。sum 是这个方向的 amount 之和（送出 = 花掉的币，收到 = 涨的心意值）。"""
    user_id = user.id
    clause = GiftRecord.from_user_id == user_id if direction == "sent" else GiftRecord.to_user_id == user_id
    total, amount = (await db.execute(select(func.count(), func.coalesce(func.sum(GiftRecord.amount), 0)).where(clause))).one()
    records = list(
        (await db.execute(select(GiftRecord).where(clause).order_by(GiftRecord.created_at.desc(), GiftRecord.id.desc()).offset(offset).limit(limit)))
        .scalars()
        .all()
    )
    return {"items": await _views(db, records, user_id), "total": int(total or 0), "sum": int(amount or 0)}


# ---------------------------------------------------------------- 心意榜


async def _board(db: AsyncSession, period: str, now: datetime) -> list[tuple[int, int]]:
    """榜单的原始数据：[(用户 id, 心意值)]，从高到低，一样多时 id 小的在前。每个进程缓存 gift_leaderboard_cache_seconds 秒。

    总榜直接读 users.charm（有索引），只取前 BOARD_MAX 名。本月榜（UTC+8 的自然月）要对这个月的记录聚合一次，
    所以整份结果（这个月收到过礼物的所有人）都缓存着，「我排第几」也从这份缓存里查，不再碰库。
    本进程里有人送了礼会作废缓存；别的进程里送的最多晚这么多秒。
    """
    start = localtime.month_start(now)
    bucket = start.isoformat() if period == "month" else ""
    cached = _boards.get(period)
    if cached is not None and cached[1] == bucket and time.monotonic() - cached[0] < settings.gift_leaderboard_cache_seconds:
        return cached[2]
    if period == "month":
        total = func.sum(GiftRecord.amount)
        query = (
            select(GiftRecord.to_user_id, total)
            .join(User, User.id == GiftRecord.to_user_id)
            .where(GiftRecord.created_at >= start)
            .group_by(GiftRecord.to_user_id)
            .order_by(total.desc(), GiftRecord.to_user_id.asc())
        )
    else:
        query = select(User.id, User.charm).where(User.charm > 0).order_by(User.charm.desc(), User.id.asc()).limit(BOARD_MAX)
    rows = [(int(user_id), int(charm or 0)) for user_id, charm in (await db.execute(query)).all()]
    _boards[period] = (time.monotonic(), bucket, rows)
    return rows


async def leaderboard(db: AsyncSession, user: User, period: str, limit: int) -> dict[str, Any]:
    """GET /gifts/leaderboard：总榜 / 本月榜。me 是我自己的名次和心意值（没上榜 rank 为 null）。"""
    user_id, my_charm = user.id, int(user.charm or 0)
    rows = await _board(db, period, _now())
    top = rows[:limit]
    users = await _users(db, {row[0] for row in top})
    items = [{"rank": index + 1, "user": _public(users[row[0]]), "charm": row[1]} for index, row in enumerate(top) if row[0] in users]
    position = next((index for index, row in enumerate(rows) if row[0] == user_id), None)
    if period == "month":
        me = {"rank": position + 1 if position is not None else None, "charm": rows[position][1] if position is not None else 0}
    elif position is not None:
        me = {"rank": position + 1, "charm": rows[position][1]}
    elif my_charm > 0:
        # 不在缓存的前 BOARD_MAX 名里：数一下排在我前面的有多少人（走 users.charm 的索引）
        ahead = await db.scalar(
            select(func.count()).select_from(User).where((User.charm > my_charm) | ((User.charm == my_charm) & (User.id < user_id)))
        )
        me = {"rank": int(ahead or 0) + 1, "charm": my_charm}
    else:
        me = {"rank": None, "charm": 0}
    return {"items": items, "me": me}


# ---------------------------------------------------------------- 后台


async def admin_records(
    db: AsyncSession,
    *,
    kind: str | None,
    from_user_id: int | None,
    to_user_id: int | None,
    created_from: datetime | None,
    created_to: datetime | None,
    limit: int,
    offset: int,
) -> dict[str, Any]:
    """GET /admin/gifts/records：全站的送礼流水，新的在前。summary 是全站累计，不随筛选变。

    隐私边界：只有在**群聊**里送的礼物才给留言（那张卡片群成员本来就都看得到）。私聊里送的、从个人空间 / 资料卡送的，
    留言只有双方看得到，在这里一律是 null。这个接口不返回任何聊天消息内容。
    """
    clauses = []
    if kind is not None:
        clauses.append(GiftRecord.kind == kind)
    if from_user_id is not None:
        clauses.append(GiftRecord.from_user_id == from_user_id)
    if to_user_id is not None:
        clauses.append(GiftRecord.to_user_id == to_user_id)
    if created_from is not None:
        clauses.append(GiftRecord.created_at >= _aware(created_from).astimezone(UTC))
    if created_to is not None:
        clauses.append(GiftRecord.created_at < _aware(created_to).astimezone(UTC))
    total = await db.scalar(select(func.count()).select_from(GiftRecord).where(*clauses))
    total_amount, total_count = (await db.execute(select(func.coalesce(func.sum(GiftRecord.amount), 0), func.count()).select_from(GiftRecord))).one()
    records = list(
        (await db.execute(select(GiftRecord).where(*clauses).order_by(GiftRecord.created_at.desc(), GiftRecord.id.desc()).offset(offset).limit(limit)))
        .scalars()
        .all()
    )
    users = await _users(db, {user_id for record in records for user_id in (record.from_user_id, record.to_user_id)})
    group_ids = {record.conversation_id for record in records if record.conversation_type == "group" and record.conversation_id is not None}
    titles: dict[int, str | None] = {}
    if group_ids:
        rows = await db.execute(select(Conversation.id, Conversation.title).where(Conversation.id.in_(group_ids)))
        titles = {conversation_id: title for conversation_id, title in rows.all()}
    items = []
    for record in records:
        in_group = record.conversation_type == "group"
        items.append(
            {
                # viewer_id=None：先按「谁都不是」出一份（留言为 null），群聊里送的再把留言填回去
                **record_view(record, users, None),
                "message": (record.message or None) if in_group else None,
                "conversation_type": record.conversation_type,
                "conversation_title": titles.get(record.conversation_id) if in_group else None,
            }
        )
    return {"items": items, "total": int(total or 0), "summary": {"total_amount": int(total_amount or 0), "total_count": int(total_count or 0)}}


async def ledger_check(db: AsyncSession) -> dict[str, Any]:
    """送礼的账的守恒检查（对账用）：每个人的心意值 = 他收到的礼物花掉的币之和。

    两边各从各的地方数：total_charm 是 users.charm 之和，total_received 是 gift_records 里收礼人还在的那些记录的 amount 之和
    （total_amount 是全部记录的，收礼人的账号被删了的也算，所以可能比 total_received 大）。
    balanced 为 false 时 problems 列出哪些人对不上（最多 50 个）。
    """
    total_amount, total_count = (await db.execute(select(func.coalesce(func.sum(GiftRecord.amount), 0), func.count()).select_from(GiftRecord))).one()
    total_charm = await db.scalar(select(func.coalesce(func.sum(User.charm), 0)))
    received = select(GiftRecord.to_user_id.label("user_id"), func.sum(GiftRecord.amount).label("amount")).group_by(GiftRecord.to_user_id).subquery()
    total_received = await db.scalar(select(func.coalesce(func.sum(received.c.amount), 0)).select_from(received).join(User, User.id == received.c.user_id))
    rows = await db.execute(
        select(User.id, User.charm, func.coalesce(received.c.amount, 0))
        .outerjoin(received, received.c.user_id == User.id)
        .where(User.charm != func.coalesce(received.c.amount, 0))
        .order_by(User.id)
        .limit(50)
    )
    problems = [{"user_id": user_id, "charm": int(charm or 0), "received": int(amount or 0)} for user_id, charm, amount in rows.all()]
    return {
        "total_amount": int(total_amount or 0),
        "total_count": int(total_count or 0),
        "total_charm": int(total_charm or 0),
        "total_received": int(total_received or 0),
        "balanced": int(total_charm or 0) == int(total_received or 0) and not problems,
        "problems": problems,
    }
