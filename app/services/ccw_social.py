"""从 CCW 用户查 KukeChat 名片（docs/ccw-social.md）。

油猴脚本在别人的 CCW 主页、评论区里调这些接口，显示「这个人有没有 KukeChat、在不在线、正在玩什么」。
对方关掉了「能从 CCW 主页找到我」、被封禁、或者根本没绑定 CCW 时，一律当作没有账号返回，
不区分这几种情况，免得被拿来探测谁关了开关。看自己永远能看到。

在线状态由调用方查好传进来（服务层不碰 WebSocket 管理器，测试里也就不用起它）。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from sqlalchemy import and_, distinct, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.models.conversation import Conversation, ConversationMember
from app.models.enums import ConversationType, FriendRequestStatus, UserStatus
from app.models.friend import FriendRequest, Friendship
from app.models.user import User
from app.schemas.ccw_social import CcwUserBrief, CcwUserCard, Friendship as FriendshipKind
from app.schemas.user import UserPublic
from app.services import ccw_activity

OnlineLookup = Callable[[list[int]], Awaitable[set[int]]]


async def _visible_users(db: AsyncSession, viewer_id: int, oids: list[str]) -> dict[str, User]:
    if not oids:
        return {}
    result = await db.execute(
        select(User).where(
            User.ccw_student_oid.in_(oids),
            User.status == UserStatus.ACTIVE,
            or_(User.ccw_discoverable.is_(True), User.id == viewer_id),
        )
    )
    return {user.ccw_student_oid: user for user in result.scalars() if user.ccw_student_oid}


async def _friendship(db: AsyncSession, viewer_id: int, target_id: int) -> FriendshipKind:
    if viewer_id == target_id:
        return "self"
    friend = await db.execute(select(Friendship.id).where(Friendship.user_id == viewer_id, Friendship.friend_id == target_id))
    if friend.scalar_one_or_none() is not None:
        return "friend"
    pending = await db.execute(
        select(FriendRequest.requester_id).where(
            FriendRequest.status == FriendRequestStatus.PENDING,
            or_(
                and_(FriendRequest.requester_id == viewer_id, FriendRequest.receiver_id == target_id),
                and_(FriendRequest.requester_id == target_id, FriendRequest.receiver_id == viewer_id),
            ),
        )
    )
    requester = pending.scalars().first()
    if requester is None:
        return "none"
    return "outgoing" if requester == viewer_id else "incoming"


async def _mutual_friend_count(db: AsyncSession, a: int, b: int) -> int:
    mine, theirs = aliased(Friendship), aliased(Friendship)
    result = await db.execute(
        select(func.count())
        .select_from(mine)
        .join(theirs, theirs.friend_id == mine.friend_id)
        .where(mine.user_id == a, theirs.user_id == b)
    )
    return int(result.scalar_one() or 0)


async def _mutual_group_count(db: AsyncSession, a: int, b: int) -> int:
    mine, theirs = aliased(ConversationMember), aliased(ConversationMember)
    result = await db.execute(
        select(func.count(distinct(mine.conversation_id)))
        .select_from(mine)
        .join(theirs, theirs.conversation_id == mine.conversation_id)
        .join(Conversation, Conversation.id == mine.conversation_id)
        .where(mine.user_id == a, theirs.user_id == b, Conversation.type == ConversationType.GROUP)
    )
    return int(result.scalar_one() or 0)


async def _activities_for(users: list[User], viewer_id: int) -> dict:
    # 关掉公开的人，别人看不到他的活动；自己仍能看到自己的（设置页要显示「当前状态」）
    ids = [u.id for u in users if u.share_ccw_activity or u.id == viewer_id]
    return await ccw_activity.get_activities(ids)


async def get_card(db: AsyncSession, viewer: User, oid: str, online: OnlineLookup) -> CcwUserCard:
    target = (await _visible_users(db, viewer.id, [oid])).get(oid)
    if target is None:
        return CcwUserCard(ccw_oid=oid, has_account=False)
    activity = (await _activities_for([target], viewer.id)).get(target.id)
    is_self = target.id == viewer.id
    return CcwUserCard(
        ccw_oid=oid,
        has_account=True,
        user=UserPublic.model_validate(target),
        online=target.id in await online([target.id]),
        activity=activity,
        friendship=await _friendship(db, viewer.id, target.id),
        mutual_friend_count=0 if is_self else await _mutual_friend_count(db, viewer.id, target.id),
        mutual_group_count=0 if is_self else await _mutual_group_count(db, viewer.id, target.id),
    )


async def get_briefs(db: AsyncSession, viewer: User, oids: list[str], online: OnlineLookup) -> list[CcwUserBrief]:
    users = await _visible_users(db, viewer.id, oids)
    if not users:
        return []
    found = list(users.values())
    online_ids = await online([u.id for u in found])
    activities = await _activities_for(found, viewer.id)
    return [
        CcwUserBrief(
            ccw_oid=oid,
            user_id=users[oid].id,
            nickname=users[oid].nickname or users[oid].username,
            avatar_url=users[oid].avatar_url,
            account_level=users[oid].account_level or 0,
            online=users[oid].id in online_ids,
            activity=activities.get(users[oid].id),
        )
        for oid in oids
        if oid in users
    ]


async def friend_ids(db: AsyncSession, user_id: int) -> list[int]:
    result = await db.execute(select(Friendship.friend_id).where(Friendship.user_id == user_id))
    return [row for row in result.scalars()]


async def friend_activities(db: AsyncSession, user_id: int) -> dict:
    """我的好友里谁正在玩什么（客户端启动、重连时拉一次，之后靠推送）。"""
    ids = await friend_ids(db, user_id)
    if not ids:
        return {}
    result = await db.execute(select(User.id).where(User.id.in_(ids), User.share_ccw_activity.is_(True)))
    return await ccw_activity.get_activities(list(result.scalars()))
