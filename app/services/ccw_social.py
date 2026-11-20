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
from fastapi import HTTPException
from sqlalchemy.orm import aliased, joinedload

from app.models.conversation import Conversation, ConversationMember
from app.models.message import Message
from app.models.enums import ConversationType, FriendRequestStatus, MessageType, UserStatus
from app.models.friend import FriendRequest, Friendship
from app.models.user import User
from app.schemas.ccw_social import (
    CcwChatMessage,
    CcwMutualsRead,
    CcwPlayingFriend,
    CcwUserBrief,
    CcwUserCard,
    CcwWorkGroup,
    CcwWorkChat,
    CcwWorkRead,
    Friendship as FriendshipKind,
)
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


# ------------------------------------------------------------------ 第二期：动态、互关邀请；第三期：作品页


async def _discoverable_ids(db: AsyncSession, viewer_id: int, oids: list[str]) -> list[int]:
    return [u.id for u in (await _visible_users(db, viewer_id, oids)).values() if u.id != viewer_id]


async def list_ccw_feed(db: AsyncSession, viewer: User, ccw_oids: list[str], before_id: int | None, limit: int):
    """CCW 动态页里并进来的 KukeChat 动态：我的好友、我在 CCW 上关注的人里有 KukeChat 的，以及我自己。

    可见性和 KukeChat 自己的动态流一样（公开的、好友可见且我们是好友的、我自己的）。
    CCW 关注列表由脚本从页面上读来，只用来圈定作者，不改变谁能看到什么。
    """
    from app.models.post import Post
    from app.services import posts as post_service

    friends = await friend_ids(db, viewer.id)
    authors = {viewer.id, *friends, *(await _discoverable_ids(db, viewer.id, ccw_oids))}
    query = (
        select(Post)
        .options(joinedload(Post.author))
        .where(Post.deleted_at.is_(None), Post.author_id.in_(authors), post_service._visible_post_condition(viewer.id, friends))
    )
    if before_id is not None:
        query = query.where(Post.id < before_id)
    posts = list((await db.execute(query.order_by(Post.id.desc()).limit(limit))).scalars().all())
    return await post_service.build_post_reads(db, posts, viewer.id)


async def mutuals_to_invite(db: AsyncSession, viewer: User, ccw_oids: list[str], online: OnlineLookup, send: bool) -> CcwMutualsRead:
    """CCW 互关里有 KukeChat、还不是好友的人；send=True 时顺手给他们发好友申请。"""
    from app.services import friends as friend_service

    users = await _visible_users(db, viewer.id, ccw_oids)
    already = set(await friend_ids(db, viewer.id))
    pending = await db.execute(
        select(FriendRequest.requester_id, FriendRequest.receiver_id).where(
            FriendRequest.status == FriendRequestStatus.PENDING,
            or_(FriendRequest.requester_id == viewer.id, FriendRequest.receiver_id == viewer.id),
        )
    )
    waiting = {a if a != viewer.id else b for a, b in pending.all()}
    candidates = [oid for oid, u in users.items() if u.id != viewer.id and u.id not in already]
    sent = 0
    if send:
        for oid in candidates:
            target = users[oid]
            if target.id in waiting:
                continue
            try:
                await friend_service.create_friend_request(db, viewer, target.id)
                sent += 1
            except HTTPException:
                continue
    briefs = await get_briefs(db, viewer, [oid for oid in ccw_oids if oid in candidates], online)
    return CcwMutualsRead(users=briefs, sent=sent)


async def _work_group_row(db: AsyncSession, oid: str) -> Conversation | None:
    """作品群：群主开了游戏模式、绑定了这个作品的公开群。有多个时取最早建的。"""
    return (
        await db.execute(
            select(Conversation)
            .where(
                Conversation.type == ConversationType.GROUP,
                Conversation.game_mode_enabled.is_(True),
                Conversation.game_creation_oid == oid,
                Conversation.join_mode == "open",
            )
            .order_by(Conversation.id)
            .limit(1)
        )
    ).scalar_one_or_none()


async def _work_group(db: AsyncSession, row: Conversation, viewer_id: int | None) -> CcwWorkGroup:
    count = (await db.execute(select(func.count()).select_from(ConversationMember).where(ConversationMember.conversation_id == row.id))).scalar_one()
    member = None
    if viewer_id is not None:
        member = (
            await db.execute(select(ConversationMember.id).where(ConversationMember.conversation_id == row.id, ConversationMember.user_id == viewer_id))
        ).scalar_one_or_none()
    return CcwWorkGroup(conversation_id=row.id, title=row.title or "作品群", avatar_url=row.avatar_url, member_count=int(count or 0), is_member=member is not None)


async def work_info(db: AsyncSession, viewer: User, oid: str) -> CcwWorkRead:
    group = None
    row = await _work_group_row(db, oid)
    if row is not None:
        group = await _work_group(db, row, viewer.id)
    playing: list[CcwPlayingFriend] = []
    activities = {uid: a for uid, a in (await friend_activities(db, viewer.id)).items() if a.ccw_oid == oid and a.kind == "play"}
    if activities:
        users = (await db.execute(select(User).where(User.id.in_(list(activities))))).scalars().all()
        playing = [
            CcwPlayingFriend(user_id=u.id, nickname=u.nickname or u.username, avatar_url=u.avatar_url, activity=activities[u.id])
            for u in sorted(users, key=lambda u: activities[u.id].started_at)
        ]
    return CcwWorkRead(ccw_oid=oid, group=group, playing=playing)


_SUMMARY = {
    MessageType.VOICE: "[语音]",
    MessageType.FORWARD_BUNDLE: "[聊天记录]",
    MessageType.POLL: "[投票]",
    MessageType.RED_PACKET: "[红包]",
    MessageType.GIFT: "[礼物]",
}


def _chat_message(m: Message) -> CcwChatMessage:
    u = m.sender
    content = m.content if m.type in (MessageType.TEXT, MessageType.IMAGE, MessageType.SYSTEM) else _SUMMARY.get(m.type, "[消息]")
    return CcwChatMessage(
        id=m.id,
        sender_id=m.sender_id,
        nickname=(u.nickname or u.username) if u else "KukeChat 用户",
        avatar_url=u.avatar_url if u else None,
        ccw_oid=u.ccw_student_oid if u else None,
        account_level=int(getattr(u, "account_level", 0) or 0) if u else 0,
        type=m.type.value if hasattr(m.type, "value") else str(m.type),
        content=content,
        created_at=m.created_at,
    )


async def work_chat(db: AsyncSession, oid: str, viewer_id: int | None, after_id: int | None, limit: int) -> CcwWorkChat:
    """作品页「聊天」标签：作品群最近的消息，任何人都能看（群是群主公开绑定到作品上的）。

    after_id 为空时取最近 limit 条；否则取比它新的（轮询用）。按 id 正序返回。撤回的不返回。
    """
    row = await _work_group_row(db, oid)
    if row is None:
        return CcwWorkChat()
    query = (
        select(Message)
        .options(joinedload(Message.sender))
        .where(Message.conversation_id == row.id, Message.recalled_at.is_(None))
    )
    if after_id:
        query = query.where(Message.id > after_id).order_by(Message.id).limit(limit)
        messages = list((await db.execute(query)).scalars().all())
    else:
        messages = list((await db.execute(query.order_by(Message.id.desc()).limit(limit))).scalars().all())
        messages.reverse()
    return CcwWorkChat(group=await _work_group(db, row, viewer_id), messages=[_chat_message(m) for m in messages])
