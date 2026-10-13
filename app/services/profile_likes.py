"""资料点赞：给别人的个人空间 / 资料卡点赞，参照 QQ 的名片赞（docs/chat-features.md 第 6 节）。

- 每人每天给同一个人点 1 个，第二天可以再点；赞只增不减，没有取消。「天」是 UTC+8 的自然日，和账号等级同一个口径。
- 明细在 profile_likes，一行就是某人某天给某人的那一个赞；累计数冗余在 users.profile_like_count 上，
  和明细在同一个事务里写。序列化用户只读那一列，这里的查询不会出现在消息发送等热路径上。
- 同一个赞被并发提交两次：靠 (liker_id, target_id, day) 的唯一约束挡住，后到的整个事务回滚，计数不会多加。
  每日人数上限是先数后写、不加锁，并发下可能多出一两个。
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import case, func, insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.profile_like import ProfileLike
from app.models.user import User
from app.schemas.user import UserPublic
from app.services.account_levels import day_bounds, local_day
from app.services.penalties import ensure_can_post, mute_state

UTC = timezone.utc

_user_t = User.__table__


def _error(code: str, message: str, status_code: int, **extra: Any) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message, **extra})


def _now() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _iso(moment: datetime) -> str:
    """错误详情里的时间：和响应模型输出的格式保持一致。"""
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def options() -> dict[str, Any]:
    return {"targets_per_day": settings.profile_like_targets_per_day}


async def _target(db: AsyncSession, user_id: int) -> Any:
    """被看 / 被赞的那个人：id、是不是机器人、累计赞数。

    直接读列而不是 db.get：计数是用 UPDATE 语句加的，会话里缓存着的 User 对象上那个数可能是旧的。
    """
    row = (
        await db.execute(
            select(_user_t.c.id, _user_t.c.is_bot, _user_t.c.profile_like_count).where(_user_t.c.id == user_id)
        )
    ).first()
    if row is None:
        raise _error("user_not_found", "用户不存在", status.HTTP_404_NOT_FOUND)
    return row


async def _given_today(db: AsyncSession, liker_id: int, target_id: int, today: date) -> tuple[int, bool]:
    """这个人今天已经给多少个不同的人点过赞、其中有没有 target_id。

    每人每天给同一个人只有一行，所以数行就是数人。走 (liker_id, day) 索引，最多扫每日上限那么多行。
    """
    used, hit = (
        await db.execute(
            select(
                func.count(ProfileLike.id),
                func.coalesce(func.max(case((ProfileLike.target_id == target_id, 1), else_=0)), 0),
            ).where(ProfileLike.liker_id == liker_id, ProfileLike.day == today)
        )
    ).one()
    return int(used or 0), bool(hit)


async def _received_today(db: AsyncSession, target_id: int, today: date) -> int:
    """这个人今天收到了几个赞。按时间范围数，走 (target_id, created_at) 索引，不用扫他收到过的全部赞。"""
    start, end = day_bounds(today)
    count = await db.scalar(
        select(func.count(ProfileLike.id)).where(
            ProfileLike.target_id == target_id, ProfileLike.created_at >= start, ProfileLike.created_at < end
        )
    )
    return int(count or 0)


def _left(used: int) -> int:
    return max(0, settings.profile_like_targets_per_day - used)


def _blocked_by(*, is_self: bool, is_bot: bool, muted: bool, liked: bool, left: int) -> str | None:
    """现在不能点的原因，能点返回 None。几条同时成立时按这个顺序取第一个，和 like() 里报错的顺序一致。"""
    if is_self:
        return "self"
    if is_bot:
        return "bot"
    if muted:
        return "muted"
    if liked:
        return "liked_today"
    if left <= 0:
        return "limit"
    return None


def _already_liked() -> HTTPException:
    return _error("already_liked_today", "今天已经给 TA 点过赞了，明天再来吧", status.HTTP_409_CONFLICT)


async def get_state(db: AsyncSession, viewer: User, user_id: int) -> dict[str, Any]:
    """viewer 和 user_id 之间的点赞状态。看自己也可以：liked_today / can_like 恒为 false，其余（包括 left_today）照常。"""
    today = local_day(_now())
    target = await _target(db, user_id)
    is_self = target.id == viewer.id
    used, liked = await _given_today(db, viewer.id, target.id, today)
    liked = liked and not is_self
    reason = _blocked_by(
        is_self=is_self, is_bot=bool(target.is_bot), muted=mute_state(viewer) is not None, liked=liked, left=_left(used)
    )
    return {
        "user_id": target.id,
        "like_count": int(target.profile_like_count or 0),
        "today_count": await _received_today(db, target.id, today),
        "liked_today": liked,
        "can_like": reason is None,
        "reason": reason,
        "left_today": _left(used),
    }


async def like(db: AsyncSession, liker: User, user_id: int) -> dict[str, Any]:
    """点一个赞，返回点完之后的状态。不能点的情况按这个顺序报：不存在、自己 / 机器人、禁言、今天点过、到每日上限。"""
    # 先把 id 取出来：下面回滚之后 liker 这个对象会过期
    liker_id = liker.id
    target = await _target(db, user_id)
    target_id = target.id
    if target_id == liker_id:
        raise _error("cannot_like_self", "不能给自己点赞", status.HTTP_400_BAD_REQUEST)
    if target.is_bot:
        raise _error("cannot_like_bot", "不能给机器人点赞", status.HTTP_400_BAD_REQUEST)
    ensure_can_post(liker)

    now = _now()
    today = local_day(now)
    used, liked = await _given_today(db, liker_id, target_id, today)
    if liked:
        raise _already_liked()
    limit = settings.profile_like_targets_per_day
    if used >= limit:
        raise _error(
            "like_limit",
            f"今天已经给 {limit} 个人点过赞了，明天再来吧",
            status.HTTP_429_TOO_MANY_REQUESTS,
            resets_at=_iso(day_bounds(today)[1]),
        )

    try:
        # 先插明细、再加计数：撞了唯一约束就停在加计数之前，整个事务回滚
        await db.execute(insert(ProfileLike).values(liker_id=liker_id, target_id=target_id, day=today, created_at=now))
        # 在库里加一，不读出来再写回去；不动 updated_at —— 这不是资料修改
        await db.execute(
            _user_t.update()
            .where(_user_t.c.id == target_id)
            .values(profile_like_count=_user_t.c.profile_like_count + 1, updated_at=_user_t.c.updated_at)
        )
        await db.commit()
    except IntegrityError:
        # 并发：同一个赞被重复提交（唯一约束），或者对方刚好被删（外键）
        await db.rollback()
        await _target(db, target_id)
        raise _already_liked() from None

    like_count = await db.scalar(select(_user_t.c.profile_like_count).where(_user_t.c.id == target_id))
    return {
        "user_id": target_id,
        "like_count": int(like_count or 0),
        "today_count": await _received_today(db, target_id, today),
        "liked_today": True,
        "can_like": False,
        # 刚点成功的人不是自己、不是机器人、也没被禁言，剩下的原因就是「今天点过了」
        "reason": "liked_today",
        "left_today": _left(used + 1),
    }


async def received(db: AsyncSession, user: User, limit: int, offset: int) -> dict[str, Any]:
    """谁赞了我，按时间倒序。同一个人每天一条（表里本来就是每天一行）。

    total 数的是明细行，用来翻页；点赞人注销后他的行会跟着删掉，所以它可能比累计赞数（只增不减）小。
    """
    today = local_day(_now())
    total = await db.scalar(select(func.count(ProfileLike.id)).where(ProfileLike.target_id == user.id))
    rows = await db.execute(
        select(ProfileLike.created_at, ProfileLike.day, User)
        .join(User, User.id == ProfileLike.liker_id)
        .where(ProfileLike.target_id == user.id)
        .order_by(ProfileLike.created_at.desc(), ProfileLike.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return {
        "items": [
            {"user": liker, "created_at": _as_utc(created_at), "today": day == today}
            for created_at, day, liker in rows.all()
        ],
        "total": int(total or 0),
        "today_count": await _received_today(db, user.id, today),
    }


async def liked_event_payload(db: AsyncSession, target_id: int, from_user_id: int) -> dict[str, Any] | None:
    """实时事件 user.profile.liked 的负载：最后一个点赞人，加上此刻的累计数和今天收到的数。

    点赞人或被赞的人已经不在了就返回 None，不用再推。
    """
    from_user = await db.get(User, from_user_id)
    like_count = await db.scalar(select(_user_t.c.profile_like_count).where(_user_t.c.id == target_id))
    if from_user is None or like_count is None:
        return None
    return {
        "from_user": UserPublic.model_validate(from_user).model_dump(mode="json"),
        "like_count": int(like_count),
        "today_count": await _received_today(db, target_id, local_day(_now())),
    }
