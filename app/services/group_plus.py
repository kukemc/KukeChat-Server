"""群升级（docs/group-plus.md）：档位、报价、购买、后台赠送、对账。

钱怎么记（表在 app/models/group_plus.py）：

- 每次购买是 group_plus_records 的一行，amount 是花掉的币。余额公式里「群升级花掉的」直接对这张表求和
  （services/shop.py 的 _group_plus_out_query / available_expression），没有另外的余额列。
- 记录上记着购买时绑定的 CCW 账号（ccw_oid），同一个 CCW 账号在别的 KukeChat 账号上买升级花掉的算进它的已花里
  （shop._shared_group_plus_query），和商店、红包、礼物是同一条换绑规则。

并发（线上两个 worker 进程，SQLite 的写事务全局串行）：和礼物一样，写事务的第一条语句是带条件的
`UPDATE shop_accounts ... WHERE 余额 >= 价格`，从这条语句起拿着写锁直到提交；影响 0 行就是余额不够。
影响 1 行才在同一个事务里重新读群、写记录、改群档位。写事务先过 shop._write_lock()；
向 CCW 核实余额这些要等外部的事都在拿锁之前做完。

群的档位是 conversations.plus_tier / plus_expires_at 两列；有效档位要看到期时间（core/group_plus_tiers）。
到期不需要定时任务：读的时候现场判断。永久档位（只有后台能给）是把到期时间写成 2999-12-31 的哨兵值，
判断用 tiers.is_permanent；永久的群不能再买（409 already_permanent）。
人数上限另有一列 conversations.member_limit_override（后台单独设：空跟随档位 / 0 不限 / 正数），取值一律走 tiers.member_limit_of。
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import and_, case, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import group_plus_tiers as tiers
from app.core.dialect import lock_for_update
from app.integrations import get_coin_ledger
from app.models.conversation import Conversation, ConversationMember
from app.models.enums import ConversationType, MemberRole
from app.models.group_plus import GroupFile, GroupPlusRecord
from app.models.message import FeaturedMessage
from app.models.shop import ShopAccount
from app.models.system import SystemSetting
from app.models.user import User
from app.schemas.user import UserPublic
from app.services import shop as shop_service
from app.services import storage
from app.services.reports import log_admin_action

logger = logging.getLogger(__name__)
UTC = timezone.utc

STAFF_ROLES = {MemberRole.OWNER, MemberRole.ADMIN}
PERIODS = ("month", "year")
OFFICIAL_GROUP_ID = 1

_KEY_PREFIX = "group_plus."
DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "price_pro": tiers.DEFAULT_PRICES["pro"],
    "price_pro_plus": tiers.DEFAULT_PRICES["pro_plus"],
    "price_max": tiers.DEFAULT_PRICES["max"],
}
CACHE_SECONDS = 5.0
_cache: dict[str, Any] = {"at": 0.0, "values": None}


def reset_cache() -> None:
    _cache["at"] = 0.0
    _cache["values"] = None


def reset_runtime_state() -> None:
    reset_cache()


def _now() -> datetime:
    return tiers._now()


def _error(status_code: int, code: str, message: str, **extra: Any) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message, **extra})


def _iso(value: datetime | None) -> datetime | None:
    return tiers.aware(value)


# ---------------------------------------------------------------- 设置与价格


def _parse_setting(name: str, raw: str | None) -> Any:
    if name == "enabled":
        return raw != "0"
    try:
        value = int(raw) if raw is not None else DEFAULTS[name]
    except ValueError:
        return DEFAULTS[name]
    return value if 1 <= value <= tiers.PRICE_MAX else DEFAULTS[name]


async def get_settings(db: AsyncSession, *, fresh: bool = False) -> dict[str, Any]:
    now = time.monotonic()
    cached = _cache["values"]
    if not fresh and cached is not None and now - _cache["at"] < CACHE_SECONDS:
        return dict(cached)
    rows = await db.execute(select(SystemSetting.key, SystemSetting.value).where(SystemSetting.key.like(f"{_KEY_PREFIX}%")))
    stored = {key[len(_KEY_PREFIX) :]: value for key, value in rows.all()}
    values = {name: _parse_setting(name, stored[name]) if name in stored else DEFAULTS[name] for name in DEFAULTS}
    _cache["at"] = now
    _cache["values"] = values
    return dict(values)


def month_price(config: dict[str, Any], tier: str) -> int:
    return int(config[f"price_{tier}"])


def period_price(config: dict[str, Any], tier: str, period: str) -> int:
    base = month_price(config, tier)
    return base * tiers.YEAR_MONTHS_PAID if period == "year" else base


def period_days(period: str) -> int:
    return tiers.YEAR_DAYS if period == "year" else tiers.MONTH_DAYS


def _tier_view(key: str, config: dict[str, Any]) -> dict[str, Any]:
    spec = tiers.TIERS[key]
    paid = key != tiers.FREE
    return {
        "key": key,
        "name": spec["name"],
        "rank": spec["rank"],
        **tiers.limits_for(key),
        "price_month": month_price(config, key) if paid else 0,
        "price_year": period_price(config, key, "year") if paid else 0,
    }


async def plans_view(db: AsyncSession) -> dict[str, Any]:
    config = await get_settings(db)
    return {
        "enabled": config["enabled"],
        "year_months_paid": tiers.YEAR_MONTHS_PAID,
        "month_days": tiers.MONTH_DAYS,
        "year_days": tiers.YEAR_DAYS,
        "file_max_bytes": tiers.file_temp_ceiling(),
        "file_temp_ttl_hours": tiers.FILE_TEMP_TTL_HOURS,
        "file_ttl_options_hours": list(tiers.FILE_TTL_OPTIONS_HOURS),
        "file_grace_days": tiers.FILE_GRACE_DAYS,
        "tiers": [_tier_view(key, config) for key in tiers.ALL_TIERS],
    }


async def admin_settings(db: AsyncSession) -> dict[str, Any]:
    config = await get_settings(db, fresh=True)
    return {
        "enabled": config["enabled"],
        "prices": {key: month_price(config, key) for key in tiers.PAID_TIERS},
        "year_months_paid": tiers.YEAR_MONTHS_PAID,
        "defaults": dict(tiers.DEFAULT_PRICES),
        "tiers": [_tier_view(key, config) for key in tiers.ALL_TIERS],
        "file_max_bytes": tiers.file_temp_ceiling(),
        "file_grace_days": tiers.FILE_GRACE_DAYS,
    }


async def admin_update_settings(db: AsyncSession, admin: User, changes: dict[str, Any]) -> dict[str, Any]:
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
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= tiers.PRICE_MAX:
            raise _error(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_settings", f"{name} 要在 1–{tiers.PRICE_MAX} 之间", field=name)
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
            db, admin_id, "group_plus.settings.update", "system_setting", None, {name: {"old": before[name], "new": value} for name, value in changed.items()}
        )
        async with shop_service._write_lock():
            await db.commit()
    reset_cache()
    return await admin_settings(db)


# ---------------------------------------------------------------- 档位上限（修剪、加人、精华用）


def member_limit(conversation: Conversation, now: datetime | None = None) -> int | None:
    """现在的人数上限；None 是不限（后台设了 member_limit_override = 0）。"""
    return tiers.member_limit_of(conversation, now)


def limits_view(conversation: Conversation, tier: str, now: datetime | None = None) -> dict[str, Any]:
    """PlusLimits：档位的上限，人数上限按 member_limit_override 改过。不限时 member_limit 为 null、member_limit_unlimited 为 true。"""
    limit = tiers.member_limit_of(conversation, now)
    return {
        **tiers.limits_for(tier),
        "member_limit": limit,
        "member_limit_unlimited": limit is None,
        "member_limit_override": conversation.member_limit_override,
    }


def featured_limit(conversation: Conversation, now: datetime | None = None) -> int:
    return tiers.TIERS[tiers.conversation_tier(conversation, now)]["featured_limit"]


async def ensure_member_capacity(db: AsyncSession, conversation: Conversation, adding: int = 1) -> None:
    """加人之前：群人数 + 要加的人数不能超过当前档位的上限（已有成员不踢，满员后不能再加）。官方群不限。"""
    if adding <= 0 or conversation.type != ConversationType.GROUP or conversation.id == OFFICIAL_GROUP_ID:
        return
    limit = member_limit(conversation)
    if limit is None:
        return
    # PG：锁住群这一行再数人数，同时进来的几个加人请求排队（锁到调用方提交，成员行和它一起提交）
    await lock_for_update(db, select(Conversation.id).where(Conversation.id == conversation.id))
    count = int(
        await db.scalar(select(func.count()).select_from(ConversationMember).where(ConversationMember.conversation_id == conversation.id)) or 0
    )
    if count + adding > limit:
        tier = tiers.conversation_tier(conversation)
        raise _error(
            status.HTTP_409_CONFLICT,
            "group_full",
            f"群已满员（{count}/{limit}），升级群档位可以提高人数上限",
            limit=limit,
            member_count=count,
            tier=tier,
        )


# ---------------------------------------------------------------- 状态视图


async def usage_of(db: AsyncSession, conversation_id: int, now: datetime | None = None) -> dict[str, int]:
    now = now or _now()
    members = int(await db.scalar(select(func.count()).select_from(ConversationMember).where(ConversationMember.conversation_id == conversation_id)) or 0)
    featured = int(await db.scalar(select(func.count()).select_from(FeaturedMessage).where(FeaturedMessage.conversation_id == conversation_id)) or 0)
    live = and_(GroupFile.conversation_id == conversation_id, (GroupFile.expires_at.is_(None)) | (GroupFile.expires_at > now))
    # 文件数算全部没过期的；占用的空间不算临时文件
    space_bytes = func.sum(case((GroupFile.temporary.is_(False), GroupFile.size), else_=0))
    file_count, file_bytes = (await db.execute(select(func.count(GroupFile.id), func.coalesce(space_bytes, 0)).where(live))).one()
    return {"member_count": members, "featured_count": featured, "file_bytes_used": int(file_bytes or 0), "file_count": int(file_count or 0)}


def file_quota_view(conversation: Conversation, used_bytes: int, file_count: int, now: datetime | None = None) -> dict[str, Any]:
    """FileQuota。used_bytes 只算空间文件（不含临时文件）；max_file_bytes 是现在能传的最大文件：
    放得进剩余空间的传成空间文件，放不进的按档位上限传成 24 小时临时文件，取两者大的。宽限期 / 未解锁为 0。
    direct：能不能用预签名 URL 直传直下（S3 存储且配了私有桶）。"""
    state = tiers.file_state(conversation, now)
    quota_bytes, temp_max = state["quota_bytes"], state["temp_max_bytes"]
    free_bytes = max(0, quota_bytes - used_bytes) if quota_bytes is not None else 0
    return {
        "state": state["state"],
        "tier": state["tier"],
        "grace_until": _iso(state["grace_until"]),
        "quota_bytes": state["quota_bytes"],
        "used_bytes": used_bytes,
        "file_count": file_count,
        "default_ttl_hours": int(conversation.file_default_ttl_hours or tiers.FILE_DEFAULT_TTL_HOURS),
        "free_bytes": free_bytes,
        "temp_max_bytes": temp_max,
        "temp_ttl_hours": tiers.FILE_TEMP_TTL_HOURS,
        "max_file_bytes": max(free_bytes, temp_max or 0) if state["state"] == "active" else 0,
        "direct": storage.direct_transfer_enabled(),
        # 档位能不能用文件夹（Pro+ / MAX，5.5）；具体某个人能不能管理看列表里的 can_manage_folders
        "folders_enabled": tiers.folders_enabled(conversation, now),
    }


async def plus_view(db: AsyncSession, conversation: Conversation, role: MemberRole | None) -> dict[str, Any]:
    now = _now()
    tier = tiers.conversation_tier(conversation, now)
    usage = await usage_of(db, conversation.id, now)
    return {
        "conversation_id": conversation.id,
        "tier": tier,
        "tier_name": tiers.TIERS[tier]["name"],
        "expires_at": _iso(conversation.plus_expires_at) if tier != tiers.FREE else None,
        "permanent": tiers.conversation_permanent(conversation, now),
        "limits": limits_view(conversation, tier, now),
        "usage": {
            "member_count": usage["member_count"],
            "featured_count": usage["featured_count"],
            "file_bytes_used": usage["file_bytes_used"],
            "file_count": usage["file_count"],
        },
        "files": file_quota_view(conversation, usage["file_bytes_used"], usage["file_count"], now),
        "can_purchase": role in STAFF_ROLES and conversation.type == ConversationType.GROUP,
    }


def plus_fields(conversation: Conversation, now: datetime | None = None) -> dict[str, Any]:
    """会话对象上的 plus_tier / plus_expires_at / plus_permanent。"""
    tier = tiers.conversation_tier(conversation, now)
    return {
        "plus_tier": tier,
        "plus_expires_at": _iso(conversation.plus_expires_at) if tier != tiers.FREE else None,
        "plus_permanent": tier != tiers.FREE and tiers.is_permanent(conversation.plus_expires_at),
    }


# ---------------------------------------------------------------- 报价与购买


def compute_plan(conversation: Conversation, tier: str, period: str, price: int, now: datetime) -> dict[str, Any]:
    """按群现在的状态算这次买的是什么（新开 / 续期 / 升级）。买更低的档位在当前档位到期前不允许。"""
    current = tiers.conversation_tier(conversation, now)
    current_expires = tiers.aware(conversation.plus_expires_at) if current != tiers.FREE else None
    if current != tiers.FREE and tiers.is_permanent(current_expires):
        # 永久档位（后台给的）：续期没有意义，升级 / 降级都只会让它变成有期限的，一律不卖
        raise _error(
            status.HTTP_409_CONFLICT,
            "already_permanent",
            f"这个群已经是永久 {tiers.TIERS[current]['name']}，不用再购买",
            current_tier=current,
        )
    days = period_days(period)
    duration = timedelta(days=days)
    forfeited = timedelta(0)
    if current == tiers.FREE:
        action, new_expires = "new", now + duration
    elif tiers.rank(tier) == tiers.rank(current):
        action, new_expires = "renew", current_expires + duration
    elif tiers.rank(tier) > tiers.rank(current):
        action, new_expires = "upgrade", now + duration
        forfeited = max(current_expires - now, timedelta(0))
    else:
        raise _error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "downgrade_not_allowed",
            f"当前的 {tiers.TIERS[current]['name']} 还没到期，到期前不能买更低的档位",
            current_tier=current,
            current_expires_at=current_expires.isoformat() if current_expires else None,
        )
    forfeited_days = int(-(-forfeited.total_seconds() // 86400)) if forfeited.total_seconds() > 0 else 0
    notice = (
        f"升级后原来 {tiers.TIERS[current]['name']} 剩余的 {forfeited_days} 天将作废" if action == "upgrade" and forfeited_days > 0 else None
    )
    return {
        "action": action,
        "tier": tier,
        "period": period,
        "days": days,
        "price": price,
        "current_tier": current,
        "current_expires_at": current_expires,
        "new_expires_at": new_expires,
        "forfeited_seconds": int(forfeited.total_seconds()),
        "forfeited_days": forfeited_days,
        "forfeit_notice": notice,
    }


def _validate_choice(tier: Any, period: Any) -> None:
    if tier not in tiers.PAID_TIERS:
        raise _error(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_tier", "档位只能是 pro / pro_plus / max")
    if period not in PERIODS:
        raise _error(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_period", "period 只能是 month / year")


async def _group_for_staff(db: AsyncSession, user_id: int, conversation_id: int) -> tuple[Conversation, ConversationMember]:
    member = (
        await db.execute(
            select(ConversationMember).where(ConversationMember.conversation_id == conversation_id, ConversationMember.user_id == user_id)
        )
    ).scalar_one_or_none()
    conversation = await db.get(Conversation, conversation_id)
    if conversation is None or member is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")
    if conversation.type != ConversationType.GROUP:
        raise _error(status.HTTP_400_BAD_REQUEST, "not_group", "私聊不能升级")
    if member.role not in STAFF_ROLES:
        raise _error(status.HTTP_403_FORBIDDEN, "not_group_staff", "只有群主和管理员可以购买群升级")
    return conversation, member


async def quote(db: AsyncSession, user: User, conversation_id: int, tier: Any, period: Any) -> dict[str, Any]:
    user_id, oid = user.id, shop_service._bound_oid(user)
    config = await get_settings(db)
    conversation, _member = await _group_for_staff(db, user_id, conversation_id)
    _validate_choice(tier, period)
    now = _now()
    plan = compute_plan(conversation, tier, period, period_price(config, tier, period), now)
    balance = (await shop_service._balance(db, user_id, oid))["balance"] if oid is not None else 0
    return {"conversation_id": conversation_id, **plan, "balance": balance, "enough": oid is not None and balance >= plan["price"]}


def apply_state(conversation: Conversation, new_tier: str, new_expires: datetime | None, now: datetime) -> None:
    """把群的档位改成 (new_tier, new_expires)，同时维护群文件空间的宽限信息。"""
    previous = tiers.file_state(conversation, now)
    conversation.plus_tier = new_tier
    conversation.plus_expires_at = new_expires
    if new_tier != tiers.FREE and tiers.TIERS[new_tier]["file_quota_bytes"] is not None and new_expires is not None:
        conversation.plus_files_tier = new_tier
        conversation.plus_files_until = new_expires + timedelta(days=tiers.FILE_GRACE_DAYS)
    elif new_tier == tiers.FREE and previous["state"] == "active":
        # 取消：现有的文件进入 30 天宽限
        conversation.plus_files_tier = previous["tier"]
        conversation.plus_files_until = now + timedelta(days=tiers.FILE_GRACE_DAYS)
    conversation.updated_at = now


async def purchase(db: AsyncSession, user: User, conversation_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    """POST /conversations/{cid}/plus/purchase。检查顺序见 docs/group-plus.md 3.4。"""
    user_id, oid = user.id, shop_service._bound_oid(user)
    config = await get_settings(db)
    if not config["enabled"]:
        raise _error(status.HTTP_403_FORBIDDEN, "disabled", "群升级暂时不开放购买")
    conversation, _member = await _group_for_staff(db, user_id, conversation_id)
    tier, period = payload.get("tier"), payload.get("period")
    _validate_choice(tier, period)
    price = period_price(config, tier, period)
    now = _now()
    plan = compute_plan(conversation, tier, period, price, now)
    confirm = bool(payload.get("confirm_forfeit"))
    if plan["forfeited_seconds"] > 0 and not confirm:
        raise _error(status.HTTP_409_CONFLICT, "forfeit_not_confirmed", plan["forfeit_notice"] or "升级会作废原档位的剩余时间", **_jsonable_plan(plan))
    expected = payload.get("expected_price")
    if expected is not None and expected != price:
        raise _error(status.HTTP_409_CONFLICT, "price_changed", "价格变了，请确认新的价格", price=price)
    if oid is None:
        raise _error(status.HTTP_403_FORBIDDEN, "ccw_not_bound", "先绑定 CCW 账号才能购买群升级")
    # 累计投币得是刚向 CCW 核实过的，核实不了就 503，不扣币
    await shop_service._fresh_account(db, user_id, oid)

    outcome = "ok"
    record_id: int | None = None
    result_state: dict[str, Any] = {}
    now = _now()
    async with shop_service._write_lock():
        try:
            # PG：先锁住自己的账户行，下面的余额检查才看得到别的进程刚提交的花币（见 lock_for_update）
            await lock_for_update(db, select(ShopAccount.user_id).where(ShopAccount.user_id == user_id))
            # 余额够才往下走。事务的第一条语句：从这里到提交一直拿着写锁
            charged = await db.execute(
                update(ShopAccount)
                .where(ShopAccount.user_id == user_id, ShopAccount.ccw_oid == oid, shop_service.available_expression(user_id, oid) >= price)
                .values(updated_at=now)
                .execution_options(synchronize_session=False)
            )
            if charged.rowcount != 1:
                await db.rollback()
                outcome = "short"
            else:
                # PG：锁住群这一行再读档位和到期时间，两个人同时给同一个群买时后一个按前一个买完的状态算
                await lock_for_update(db, select(Conversation.id).where(Conversation.id == conversation_id))
                fresh = await db.get(Conversation, conversation_id, populate_existing=True)
                try:
                    latest = compute_plan(fresh, tier, period, price, now)
                except HTTPException:
                    await db.rollback()
                    raise
                if latest["action"] != plan["action"] or latest["forfeited_seconds"] > 0 and not confirm:
                    # 并发的另一次购买刚改了这个群：档位关系变了，重新报价
                    await db.rollback()
                    outcome = "changed"
                else:
                    record = GroupPlusRecord(
                        conversation_id=conversation_id,
                        source="purchase",
                        action=latest["action"],
                        user_id=user_id,
                        tier=tier,
                        period=period,
                        days=latest["days"],
                        amount=price,
                        prev_tier=latest["current_tier"],
                        prev_expires_at=latest["current_expires_at"],
                        new_tier=tier,
                        new_expires_at=latest["new_expires_at"],
                        forfeited_days=latest["forfeited_days"],
                        ccw_oid=oid,
                        created_at=now,
                    )
                    db.add(record)
                    apply_state(fresh, tier, latest["new_expires_at"], now)
                    await db.flush()
                    record_id = record.id
                    result_state = {"plus_tier": tier, "plus_expires_at": latest["new_expires_at"]}
                    await db.commit()
        except HTTPException:
            raise
        except BaseException:
            await db.rollback()
            raise
    if outcome == "changed":
        raise _error(status.HTTP_409_CONFLICT, "plan_changed", "群的档位刚刚变了，请重新确认后再买")
    if outcome == "short":
        balance = (await shop_service._balance(db, user_id, oid))["balance"]
        raise _error(
            status.HTTP_402_PAYMENT_REQUIRED,
            "insufficient_balance",
            "金币不够",
            balance=balance,
            total=price,
            shortfall=max(price - balance, 0),
            donate_url=get_coin_ledger().topup_url(),
        )
    assert record_id is not None
    shop_service.announce_balance(user_id, "group_plus.purchased", conversation_id=conversation_id)
    conversation = await db.get(Conversation, conversation_id, populate_existing=True)
    record_row = await db.get(GroupPlusRecord, record_id)
    return {
        "record": (await _record_views(db, [record_row]))[0],
        "conversation": {"conversation_id": conversation_id, **result_state},
        "plus": await plus_view(db, conversation, MemberRole.OWNER),
        "balance": await shop_service._balance(db, user_id, oid),
    }


def _jsonable_plan(plan: dict[str, Any]) -> dict[str, Any]:
    return {key: (value.isoformat() if isinstance(value, datetime) else value) for key, value in plan.items()}


# ---------------------------------------------------------------- 记录


async def _record_views(db: AsyncSession, records: list[GroupPlusRecord], *, with_title: bool = False) -> list[dict[str, Any]]:
    user_ids = {record.user_id for record in records if record.user_id is not None}
    users = {}
    if user_ids:
        users = {user.id: user for user in (await db.execute(select(User).where(User.id.in_(user_ids)))).scalars().all()}
    titles: dict[int, str | None] = {}
    if with_title and records:
        ids = {record.conversation_id for record in records}
        titles = {cid: title for cid, title in (await db.execute(select(Conversation.id, Conversation.title).where(Conversation.id.in_(ids)))).all()}
    views = []
    for record in records:
        owner = users.get(record.user_id)
        view = {
            "id": record.id,
            "conversation_id": record.conversation_id,
            "source": record.source,
            "action": record.action,
            "tier": record.tier,
            "period": record.period,
            "days": record.days,
            "amount": record.amount,
            "prev_tier": record.prev_tier,
            "prev_expires_at": _iso(record.prev_expires_at),
            "new_tier": record.new_tier,
            "new_expires_at": _iso(record.new_expires_at),
            "permanent": record.new_tier != tiers.FREE and tiers.is_permanent(record.new_expires_at),
            "forfeited_days": record.forfeited_days,
            "user": UserPublic.model_validate(owner) if owner is not None else None,
            "note": record.note,
            "created_at": _iso(record.created_at),
        }
        if with_title:
            view["conversation_title"] = titles.get(record.conversation_id)
        views.append(view)
    return views


async def list_group_records(db: AsyncSession, user: User, conversation_id: int, limit: int, offset: int) -> dict[str, Any]:
    await _group_for_staff(db, user.id, conversation_id)
    total = int(await db.scalar(select(func.count()).select_from(GroupPlusRecord).where(GroupPlusRecord.conversation_id == conversation_id)) or 0)
    rows = (
        await db.execute(
            select(GroupPlusRecord)
            .where(GroupPlusRecord.conversation_id == conversation_id)
            .order_by(GroupPlusRecord.id.desc())
            .offset(offset)
            .limit(limit)
        )
    ).scalars().all()
    return {"items": await _record_views(db, list(rows)), "total": total}


# ---------------------------------------------------------------- 后台


async def _admin_group_item(db: AsyncSession, conversation: Conversation, now: datetime) -> dict[str, Any]:
    usage = await usage_of(db, conversation.id, now)
    owner = (
        await db.execute(
            select(User)
            .join(ConversationMember, ConversationMember.user_id == User.id)
            .where(ConversationMember.conversation_id == conversation.id, ConversationMember.role == MemberRole.OWNER)
            .limit(1)
        )
    ).scalar_one_or_none()
    expires = tiers.aware(conversation.plus_expires_at)
    recorded_free = conversation.plus_tier == tiers.FREE
    expired = recorded_free or expires is None or expires <= now
    limit = tiers.member_limit_of(conversation, now)
    return {
        "conversation_id": conversation.id,
        "title": conversation.title,
        "avatar_url": conversation.avatar_url,
        "member_count": usage["member_count"],
        "owner": {"id": owner.id, "display_name": owner.nickname or owner.username} if owner is not None else None,
        "tier": conversation.plus_tier if not expired else tiers.FREE,
        "recorded_tier": conversation.plus_tier,
        "expires_at": expires if not recorded_free else None,
        "days_left": 0 if expired else max(0, (expires - now).days),
        # 免费群（从没买过 / 已取消）不算「过期」
        "expired": expired and not recorded_free,
        "permanent": not expired and tiers.is_permanent(expires),
        "member_limit": limit,
        "member_limit_unlimited": limit is None,
        "member_limit_override": conversation.member_limit_override,
        "files_state": tiers.file_state(conversation, now)["state"],
        "featured_count": usage["featured_count"],
    }


async def admin_groups(
    db: AsyncSession,
    *,
    tier: str | None,
    expiring_within_days: int | None,
    q: str | None,
    include_expired: bool,
    limit: int,
    offset: int,
    include_free: bool = False,
) -> dict[str, Any]:
    """后台群列表。默认只列有档位的群；include_free=True 列所有群（按群名 / id 找群、设人数上限用），tier / 到期筛选照样生效。"""
    now = _now()
    clauses: list[Any] = [Conversation.type == ConversationType.GROUP]
    if not include_free:
        clauses += [Conversation.plus_tier != tiers.FREE, Conversation.plus_expires_at.is_not(None)]
        if not include_expired:
            clauses.append(Conversation.plus_expires_at > now)
    if tier in tiers.PAID_TIERS:
        clauses.append(Conversation.plus_tier == tier)
        if include_free:
            clauses.append(Conversation.plus_expires_at > now)
    if expiring_within_days is not None:
        clauses.append(Conversation.plus_expires_at > now)
        clauses.append(Conversation.plus_expires_at <= now + timedelta(days=expiring_within_days))
    term = (q or "").strip()
    if term:
        like = f"%{term}%"
        match = Conversation.title.ilike(like)
        clauses.append(match | (Conversation.id == int(term)) if term.isdigit() else match)
    total = int(await db.scalar(select(func.count()).select_from(Conversation).where(*clauses)) or 0)
    order = (Conversation.id.asc(),) if include_free else (Conversation.plus_expires_at.asc(), Conversation.id.asc())
    rows = (await db.execute(select(Conversation).where(*clauses).order_by(*order).offset(offset).limit(limit))).scalars().all()
    return {"items": [await _admin_group_item(db, conversation, now) for conversation in rows], "total": total}


async def admin_group(db: AsyncSession, conversation_id: int) -> dict[str, Any]:
    """GET /admin/group-plus/groups/{cid}：任意一个群（含免费群）的档位、人数、人数上限。不含消息内容。"""
    conversation = await db.get(Conversation, conversation_id)
    if conversation is None or conversation.type != ConversationType.GROUP:
        raise _error(status.HTTP_404_NOT_FOUND, "group_not_found", "群不存在")
    return await _admin_group_item(db, conversation, _now())


async def admin_grant(db: AsyncSession, admin: User, payload: dict[str, Any]) -> dict[str, Any]:
    """POST /admin/group-plus/grant：赠送 / 取消。同档位在原到期上加；不同档位直接替换（后台可以降档）；tier=free 是取消。

    permanent=true 是送成永久（到期时间写成哨兵值，days 不用传）。给已经是永久的同档位再送天数：保持永久，不变。
    """
    admin_id = admin.id
    conversation_id = payload.get("conversation_id")
    tier = payload.get("tier")
    days = payload.get("days")
    permanent = payload.get("permanent")
    note = (payload.get("note") or "").strip() or None
    if not isinstance(conversation_id, int) or isinstance(conversation_id, bool):
        raise _error(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_group", "conversation_id 不对")
    if tier not in tiers.ALL_TIERS:
        raise _error(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_tier", "tier 只能是 free / pro / pro_plus / max")
    if permanent is not None and not isinstance(permanent, bool):
        raise _error(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_permanent", "permanent 只能是 true / false")
    permanent = bool(permanent) and tier != tiers.FREE
    if tier != tiers.FREE and not permanent and (isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 3650):
        raise _error(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_days", "days 要在 1–3650 之间（或者传 permanent: true 送成永久）")
    conversation = await db.get(Conversation, conversation_id)
    if conversation is None or conversation.type != ConversationType.GROUP:
        raise _error(status.HTTP_404_NOT_FOUND, "group_not_found", "群不存在")
    now = _now()
    async with shop_service._write_lock():
        try:
            # 先占住写锁，再读最新的群状态
            await db.execute(
                update(Conversation).where(Conversation.id == conversation_id).values(updated_at=Conversation.updated_at).execution_options(synchronize_session=False)
            )
            fresh = await db.get(Conversation, conversation_id, populate_existing=True)
            current = tiers.conversation_tier(fresh, now)
            current_expires = tiers.aware(fresh.plus_expires_at) if current != tiers.FREE else None
            if tier == tiers.FREE:
                action, source = "revoke", "revoke"
                new_tier, new_expires, grant_days = tiers.FREE, None, 0
            elif permanent:
                action, source = "grant", "grant"
                new_tier, new_expires, grant_days = tier, tiers.PERMANENT_EXPIRES_AT, 0
            else:
                action, source = "grant", "grant"
                new_tier, grant_days = tier, int(days)
                if current == tier and tiers.is_permanent(current_expires):
                    # 已经是永久的同档位：加天数没有意义，保持永久
                    new_expires = current_expires
                else:
                    base = current_expires if current == tier and current_expires is not None else now
                    new_expires = base + timedelta(days=grant_days)
            record = GroupPlusRecord(
                conversation_id=conversation_id,
                source=source,
                action=action,
                user_id=None,
                admin_id=admin_id,
                tier=tier,
                period=None,
                days=grant_days,
                amount=0,
                prev_tier=current,
                prev_expires_at=current_expires,
                new_tier=new_tier,
                new_expires_at=new_expires,
                note=note,
                created_at=now,
            )
            db.add(record)
            apply_state(fresh, new_tier, new_expires, now)
            await db.flush()
            if tier == tiers.FREE:
                await log_admin_action(
                    db, admin_id, "group_plus.revoke", "conversation", conversation_id,
                    {"prev_tier": current, "prev_expires_at": current_expires.isoformat() if current_expires else None, "note": note},
                )
            else:
                await log_admin_action(
                    db, admin_id, "group_plus.grant", "conversation", conversation_id,
                    {
                        "tier": tier,
                        "days": grant_days,
                        "permanent": tiers.is_permanent(new_expires),
                        "prev_tier": current,
                        "new_expires_at": new_expires.isoformat(),
                        "note": note,
                    },
                )
            record_id = record.id
            await db.commit()
        except BaseException:
            await db.rollback()
            raise
    conversation = await db.get(Conversation, conversation_id, populate_existing=True)
    row = await db.get(GroupPlusRecord, record_id)
    return {
        "record": (await _record_views(db, [row], with_title=True))[0],
        "conversation": {"conversation_id": conversation_id, **plus_fields(conversation)},
        "reason": "revoke" if tier == tiers.FREE else "grant",
    }


def parse_member_limit_override(value: Any) -> int | None:
    """null = 跟随档位，0 = 不限，1–100000 = 自定义上限。不合法抛 422 invalid_member_limit。"""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= tiers.MEMBER_LIMIT_OVERRIDE_MAX:
        raise _error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "invalid_member_limit",
            f"member_limit_override 只能是 null（跟随档位）、0（不限）或 1–{tiers.MEMBER_LIMIT_OVERRIDE_MAX}",
        )
    return value


async def admin_set_member_limit(db: AsyncSession, admin: User, conversation_id: int, value: Any) -> dict[str, Any]:
    """PUT /admin/group-plus/groups/{cid}/member-limit：单独设这个群的人数上限（null 跟随档位 / 0 不限 / 正数）。

    只影响之后的加人（已有成员不踢；自定义的上限比现有人数小，也只是不能再加）。有变化才写审计日志 group_plus.member_limit。
    """
    admin_id = admin.id
    override = parse_member_limit_override(value)
    conversation = await db.get(Conversation, conversation_id)
    if conversation is None or conversation.type != ConversationType.GROUP:
        raise _error(status.HTTP_404_NOT_FOUND, "group_not_found", "群不存在")
    async with shop_service._write_lock():
        try:
            await db.execute(
                update(Conversation).where(Conversation.id == conversation_id).values(updated_at=Conversation.updated_at).execution_options(synchronize_session=False)
            )
            fresh = await db.get(Conversation, conversation_id, populate_existing=True)
            previous = fresh.member_limit_override
            changed = previous != override
            if changed:
                fresh.member_limit_override = override
                await db.flush()
                await log_admin_action(db, admin_id, "group_plus.member_limit", "conversation", conversation_id, {"old": previous, "new": override})
            await db.commit()
        except BaseException:
            await db.rollback()
            raise
    conversation = await db.get(Conversation, conversation_id, populate_existing=True)
    return {
        "group": await _admin_group_item(db, conversation, _now()),
        "conversation": {"conversation_id": conversation_id, **plus_fields(conversation)},
        "changed": changed,
        "previous": previous,
    }


async def admin_records(
    db: AsyncSession, *, conversation_id: int | None, user_id: int | None, source: str | None, limit: int, offset: int
) -> dict[str, Any]:
    clauses = []
    if conversation_id is not None:
        clauses.append(GroupPlusRecord.conversation_id == conversation_id)
    if user_id is not None:
        clauses.append(GroupPlusRecord.user_id == user_id)
    if source in ("purchase", "grant", "revoke"):
        clauses.append(GroupPlusRecord.source == source)
    total = int(await db.scalar(select(func.count()).select_from(GroupPlusRecord).where(*clauses)) or 0)
    rows = (await db.execute(select(GroupPlusRecord).where(*clauses).order_by(GroupPlusRecord.id.desc()).offset(offset).limit(limit))).scalars().all()
    total_amount, total_count = (
        await db.execute(
            select(func.coalesce(func.sum(GroupPlusRecord.amount), 0), func.count(GroupPlusRecord.id)).where(GroupPlusRecord.source == "purchase")
        )
    ).one()
    return {
        "items": await _record_views(db, list(rows), with_title=True),
        "total": total,
        "summary": {"total_amount": int(total_amount or 0), "total_count": int(total_count or 0)},
    }


async def ledger_check(db: AsyncSession) -> dict[str, Any]:
    """对账：每个有档位的群，现在的档位 / 到期时间都等于它最后一条记录记下的结果。"""
    total_amount, total_count = (
        await db.execute(
            select(func.coalesce(func.sum(GroupPlusRecord.amount), 0), func.count(GroupPlusRecord.id)).where(GroupPlusRecord.source == "purchase")
        )
    ).one()
    granted = int(await db.scalar(select(func.count()).select_from(GroupPlusRecord).where(GroupPlusRecord.source == "grant")) or 0)
    revoked = int(await db.scalar(select(func.count()).select_from(GroupPlusRecord).where(GroupPlusRecord.source == "revoke")) or 0)
    latest = select(func.max(GroupPlusRecord.id)).group_by(GroupPlusRecord.conversation_id).scalar_subquery()
    last_records = {
        record.conversation_id: record for record in (await db.execute(select(GroupPlusRecord).where(GroupPlusRecord.id.in_(latest)))).scalars().all()
    }
    problems: list[dict[str, Any]] = []
    stored = (
        await db.execute(select(Conversation).where(Conversation.plus_tier != tiers.FREE, Conversation.plus_expires_at.is_not(None)))
    ).scalars().all()
    for conversation in stored:
        record = last_records.get(conversation.id)
        expires = tiers.aware(conversation.plus_expires_at)
        if record is None or record.new_tier != conversation.plus_tier or tiers.aware(record.new_expires_at) != expires:
            problems.append(
                {
                    "conversation_id": conversation.id,
                    "tier": conversation.plus_tier,
                    "expires_at": expires,
                    "record_tier": record.new_tier if record else None,
                    "record_expires_at": tiers.aware(record.new_expires_at) if record else None,
                }
            )
            if len(problems) >= 50:
                break
    return {
        "total_amount": int(total_amount or 0),
        "total_count": int(total_count or 0),
        "granted_count": granted,
        "revoked_count": revoked,
        "balanced": not problems,
        "problems": problems,
    }
