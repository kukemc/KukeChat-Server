from __future__ import annotations

import math
import random
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.conversation import Conversation, ConversationMember, GroupJoinRequest
from app.models.enums import ConversationType, ModerationStatus, PostVisibility
from app.models.friend import Friendship
from app.models.message import Message
from app.models.post import Post, PostLike
from app.models.user import User
from app.schemas.home import HomeGroupRead, HomeRead, HomeStatsRead
from app.schemas.user import CcwCreatorRead
from app.services import conversations as conversation_service
from app.services.users import list_ccw_creators


def _pick_weighted_groups(rows: list[tuple[Conversation, int, int]], limit: int, seed: int) -> list[tuple[Conversation, int, int, int]]:
    now = datetime.now(UTC)
    pool: list[tuple[Conversation, int, int, int]] = []
    for conversation, member_count, recent_message_count in rows:
        updated_at = conversation.updated_at
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=UTC)
        freshness = max(0, 14 - (now - updated_at).days)
        heat_score = int(member_count * 4 + recent_message_count * 7 + freshness * 2)
        pool.append((conversation, member_count, recent_message_count, heat_score))

    rng = random.Random(seed)
    selected: list[tuple[Conversation, int, int, int]] = []
    while pool and len(selected) < limit:
        weights = [max(1.0, math.pow(max(item[3], 1), 0.72)) for item in pool]
        index = rng.choices(range(len(pool)), weights=weights, k=1)[0]
        selected.append(pool.pop(index))
    return selected


async def get_home(db: AsyncSession, current_user_id: int, seed: int, group_limit: int = 6, creator_limit: int = 6) -> HomeRead:
    since = datetime.now(UTC) - timedelta(days=7)
    stat_result = await db.execute(select(
        select(func.count()).select_from(User).where(User.is_bot.is_(False)).scalar_subquery(),
        select(func.count()).select_from(Message).where(Message.recalled_at.is_(None)).scalar_subquery(),
        select(func.count()).select_from(Message).where(Message.recalled_at.is_(None), Message.created_at >= since).scalar_subquery(),
        select(func.count()).select_from(Conversation).where(Conversation.type == ConversationType.GROUP).scalar_subquery(),
        select(func.count()).select_from(Conversation).where(Conversation.type == ConversationType.GROUP, Conversation.join_mode != "invite_only").scalar_subquery(),
        select(func.count()).select_from(Post).where(
            Post.deleted_at.is_(None),
            Post.visibility == PostVisibility.PUBLIC,
            Post.moderation_status == ModerationStatus.APPROVED.value,
        ).scalar_subquery(),
        select(func.count()).select_from(Friendship).scalar_subquery(),
        select(func.count()).select_from(PostLike).scalar_subquery(),
    ))
    registered_users, messages, messages_last_7_days, groups, public_groups, public_posts, friendships, post_likes = stat_result.one()
    stats = HomeStatsRead(
        registered_users=int(registered_users),
        messages=int(messages),
        messages_last_7_days=int(messages_last_7_days),
        groups=int(groups),
        public_groups=int(public_groups),
        public_posts=int(public_posts),
        friendships=int(friendships) // 2,
        post_likes=int(post_likes),
    )

    member_counts = (
        select(ConversationMember.conversation_id, func.count(ConversationMember.id).label("member_count"))
        .group_by(ConversationMember.conversation_id)
        .subquery()
    )
    recent_messages = (
        select(Message.conversation_id, func.count(Message.id).label("recent_message_count"))
        .where(Message.recalled_at.is_(None), Message.created_at >= since)
        .group_by(Message.conversation_id)
        .subquery()
    )
    member_count = func.coalesce(member_counts.c.member_count, 0)
    recent_message_count = func.coalesce(recent_messages.c.recent_message_count, 0)
    group_result = await db.execute(
        select(Conversation, member_count.label("member_count"), recent_message_count.label("recent_message_count"))
        .outerjoin(member_counts, member_counts.c.conversation_id == Conversation.id)
        .outerjoin(recent_messages, recent_messages.c.conversation_id == Conversation.id)
        .where(Conversation.type == ConversationType.GROUP, Conversation.join_mode != "invite_only")
        .order_by(recent_message_count.desc(), member_count.desc(), Conversation.updated_at.desc(), Conversation.id.desc())
        .limit(max(group_limit * 6, 24))
    )
    candidates = [(conversation, int(members), int(messages)) for conversation, members, messages in group_result.all()]
    selected = _pick_weighted_groups(candidates, group_limit, seed)
    selected_ids = [conversation.id for conversation, *_rest in selected]
    memberships: dict[int, ConversationMember] = {}
    join_requests: dict[int, object] = {}
    if selected_ids:
        membership_result = await db.execute(
            select(ConversationMember).where(
                ConversationMember.user_id == current_user_id,
                ConversationMember.conversation_id.in_(selected_ids),
            )
        )
        memberships = {membership.conversation_id: membership for membership in membership_result.scalars().all()}
        request_result = await db.execute(
            select(GroupJoinRequest.conversation_id, GroupJoinRequest.status)
            .where(GroupJoinRequest.requester_id == current_user_id, GroupJoinRequest.conversation_id.in_(selected_ids))
            .order_by(GroupJoinRequest.created_at.desc(), GroupJoinRequest.id.desc())
        )
        for conversation_id, request_status in request_result.all():
            join_requests.setdefault(conversation_id, request_status)

    groups: list[HomeGroupRead] = []
    for conversation, members, messages, heat_score in selected:
        membership = memberships.get(conversation.id)
        display_title = conversation_service._clean_text(membership.remark) if membership is not None else None
        groups.append(HomeGroupRead(
            id=conversation.id,
            type=conversation.type,
            title=conversation.title,
            display_title=display_title or conversation.title,
            avatar_url=conversation.avatar_url,
            description=conversation.description,
            category=conversation.category,
            announcement=conversation.announcement,
            join_mode=conversation.join_mode,
            auto_approve=conversation.auto_approve,
            join_question=conversation.join_question,
            created_at=conversation.created_at,
            updated_at=conversation.updated_at,
            member_count=members,
            joined=membership is not None,
            join_request_status=join_requests.get(conversation.id),
            recent_message_count=messages,
            heat_score=heat_score,
        ))

    creators, creator_total = await list_ccw_creators(db, creator_limit, 0)
    return HomeRead(
        stats=stats,
        groups=groups,
        creators=[CcwCreatorRead.model_validate(creator) for creator in creators],
        creator_total=creator_total,
        recommendation_seed=seed,
    )
