from __future__ import annotations

from datetime import datetime, timezone

UTC = timezone.utc

from fastapi import HTTPException, status
from sqlalchemy import and_, case, delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.models.conversation import Conversation, ConversationMember, GroupAnnouncement, GroupJoinRequest, TemporaryConversationBlock
from app.models.enums import ConversationType, GroupJoinRequestStatus, MemberRole
from app.models.friend import Friendship
from app.models.message import Message, MessageVisibility
from app.models.user import User
from app.services.friends import are_friends
from app.schemas.conversation import (
    ConversationProfileUpdate,
    ConversationRead,
    GroupJoinRequestDecision,
    GroupJoinRequestRead,
    GroupJoinRequestCreate,
    GroupDiscoveryRead,
    GroupSettingsUpdate,
    MemberTitleUpdate,
    MemberSettingsUpdate,
)
from app.schemas.message import MessageRead
from app.schemas.user import UserPublic
from app.services.upload_paths import require_avatar_upload_url


STAFF_ROLES = {MemberRole.OWNER, MemberRole.ADMIN}


def _clean_text(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    return text or None


def _user_display_name(user: User | None, fallback: str) -> str:
    if user is None:
        return fallback
    return _clean_text(user.nickname) or _clean_text(user.username) or fallback


def _member_display_name(member: ConversationMember | None, fallback_user: User | None = None, fallback: str = "成员") -> str:
    if member is not None:
        return _clean_text(member.nickname) or _user_display_name(member.user or fallback_user, fallback)
    return _user_display_name(fallback_user, fallback)


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _member_mute_expired(member: ConversationMember, now: datetime | None = None) -> bool:
    if not member.muted or member.muted_until is None:
        return False
    return _as_utc(member.muted_until) <= (now or datetime.now(UTC))


async def clear_expired_member_mute(db: AsyncSession, member: ConversationMember, now: datetime | None = None) -> bool:
    if not _member_mute_expired(member, now):
        return False
    member.muted = False
    member.muted_until = None
    await db.flush()
    return True


async def clear_expired_mutes_for_conversation(db: AsyncSession, conversation_id: int, now: datetime | None = None) -> int:
    effective_now = now or datetime.now(UTC)
    result = await db.execute(
        select(ConversationMember).where(
            ConversationMember.conversation_id == conversation_id,
            ConversationMember.muted.is_(True),
            ConversationMember.muted_until.is_not(None),
        )
    )
    expired_members = [member for member in result.scalars().all() if _member_mute_expired(member, effective_now)]
    for member in expired_members:
        member.muted = False
        member.muted_until = None
    if expired_members:
        await db.flush()
    return len(expired_members)


def _conversation_display_title(conversation: Conversation, membership: ConversationMember, direct_user: User | None = None) -> str:
    if conversation.type == ConversationType.GROUP:
        return _clean_text(membership.remark) or _clean_text(conversation.title) or "未命名群聊"
    return _user_display_name(direct_user, _clean_text(conversation.title) or "私聊")


def direct_key_for(user_a: int, user_b: int) -> str:
    first, second = sorted((user_a, user_b))
    return f"{first}:{second}"


async def require_membership(db: AsyncSession, conversation_id: int, user_id: int, *, clear_expired_mute: bool = True) -> ConversationMember:
    result = await db.execute(
        select(ConversationMember).where(
            ConversationMember.conversation_id == conversation_id,
            ConversationMember.user_id == user_id,
        )
    )
    member = result.scalar_one_or_none()
    if member is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")
    if clear_expired_mute:
        await clear_expired_member_mute(db, member)
    return member


async def get_conversation(db: AsyncSession, conversation_id: int) -> Conversation:
    conversation = await db.get(Conversation, conversation_id)
    if conversation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")
    return conversation


async def get_member(db: AsyncSession, conversation_id: int, user_id: int, *, clear_expired_mute: bool = True) -> ConversationMember:
    result = await db.execute(
        select(ConversationMember)
        .options(joinedload(ConversationMember.user))
        .where(
            ConversationMember.conversation_id == conversation_id,
            ConversationMember.user_id == user_id,
        )
    )
    member = result.scalar_one_or_none()
    if member is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Member not found")
    if clear_expired_mute:
        await clear_expired_member_mute(db, member)
    return member


async def require_group_conversation(db: AsyncSession, conversation_id: int) -> Conversation:
    conversation = await get_conversation(db, conversation_id)
    if conversation.type != ConversationType.GROUP:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="This operation is only available in group conversations")
    return conversation


async def get_or_create_direct_conversation(db: AsyncSession, current_user: User, target_user_id: int, temporary: bool = False) -> Conversation:
    if current_user.id == target_user_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot create a direct conversation with yourself")
    target = await db.get(User, target_user_id)
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Target user not found")

    is_friend = await are_friends(db, current_user.id, target_user_id)
    temporary = temporary and not is_friend
    if temporary:
        block_result = await db.execute(
            select(TemporaryConversationBlock.id).where(
                TemporaryConversationBlock.blocker_id == target_user_id,
                TemporaryConversationBlock.blocked_user_id == current_user.id,
            )
        )
        if block_result.scalar_one_or_none() is not None:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="The user does not allow temporary conversations from you")

    direct_key = direct_key_for(current_user.id, target_user_id)
    result = await db.execute(select(Conversation).where(Conversation.direct_key == direct_key))
    conversation = result.scalar_one_or_none()
    if conversation is not None:
        if is_friend and conversation.is_temporary:
            conversation.is_temporary = False
            conversation.temporary_initiator_id = None
            conversation.temporary_target_id = None
            await db.commit()
            await db.refresh(conversation)
        elif temporary and not conversation.is_temporary:
            conversation.is_temporary = True
            conversation.temporary_initiator_id = current_user.id
            conversation.temporary_target_id = target_user_id
            await db.commit()
            await db.refresh(conversation)
        return conversation

    conversation = Conversation(
        type=ConversationType.DIRECT,
        direct_key=direct_key,
        created_by_id=current_user.id,
        is_temporary=temporary,
        temporary_initiator_id=current_user.id if temporary else None,
        temporary_target_id=target_user_id if temporary else None,
    )
    db.add(conversation)
    await db.flush()
    db.add_all(
        [
            ConversationMember(conversation_id=conversation.id, user_id=current_user.id, role=MemberRole.MEMBER),
            ConversationMember(conversation_id=conversation.id, user_id=target_user_id, role=MemberRole.MEMBER),
        ]
    )
    await db.commit()
    await db.refresh(conversation)
    return conversation


async def close_temporary_conversation(db: AsyncSession, conversation_id: int, current_user: User) -> None:
    conversation = await get_conversation(db, conversation_id)
    if conversation.type != ConversationType.DIRECT or not conversation.is_temporary:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="This conversation is not temporary")
    await require_membership(db, conversation_id, current_user.id)
    await db.delete(conversation)
    await db.commit()


async def block_temporary_conversation(db: AsyncSession, conversation_id: int, current_user: User) -> int:
    conversation = await get_conversation(db, conversation_id)
    if conversation.type != ConversationType.DIRECT or not conversation.is_temporary:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="This conversation is not temporary")
    await require_membership(db, conversation_id, current_user.id)
    other_id = conversation.temporary_initiator_id if conversation.temporary_initiator_id != current_user.id else conversation.temporary_target_id
    if other_id is None:
        members = await conversation_member_ids(db, conversation_id)
        other_id = next((user_id for user_id in members if user_id != current_user.id), None)
    if other_id is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Temporary conversation peer not found")

    existing = await db.execute(
        select(TemporaryConversationBlock.id).where(
            TemporaryConversationBlock.blocker_id == current_user.id,
            TemporaryConversationBlock.blocked_user_id == other_id,
        )
    )
    if existing.scalar_one_or_none() is None:
        db.add(TemporaryConversationBlock(blocker_id=current_user.id, blocked_user_id=other_id))
    await db.delete(conversation)
    await db.commit()
    return other_id


async def create_group_conversation(
    db: AsyncSession,
    current_user: User,
    title: str,
    member_ids: list[int],
    avatar_url: str | None = None,
) -> Conversation:
    avatar_url = require_avatar_upload_url(avatar_url)
    unique_ids = list(dict.fromkeys([current_user.id, *member_ids]))
    existing_users = await db.execute(select(User.id).where(User.id.in_(unique_ids)))
    existing_ids = set(existing_users.scalars().all())
    missing_ids = sorted(set(unique_ids) - existing_ids)
    if missing_ids:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Users not found: {missing_ids}")

    conversation = Conversation(
        type=ConversationType.GROUP,
        title=title,
        avatar_url=avatar_url,
        created_by_id=current_user.id,
    )
    db.add(conversation)
    await db.flush()
    for user_id in unique_ids:
        db.add(
            ConversationMember(
                conversation_id=conversation.id,
                user_id=user_id,
                role=MemberRole.OWNER if user_id == current_user.id else MemberRole.MEMBER,
            )
        )
    await db.commit()
    await db.refresh(conversation)
    return conversation


async def list_user_conversations(db: AsyncSession, user_id: int) -> list[Conversation]:
    result = await db.execute(
        select(Conversation)
        .join(ConversationMember, ConversationMember.conversation_id == Conversation.id)
        .where(ConversationMember.user_id == user_id)
        .order_by(ConversationMember.pinned.desc(), Conversation.updated_at.desc())
    )
    return list(result.scalars().unique().all())


def _public_group_conditions() -> list[object]:
    return [Conversation.type == ConversationType.GROUP, Conversation.join_mode != "invite_only"]


async def _build_group_discovery_read(
    db: AsyncSession,
    conversation: Conversation,
    current_user_id: int,
    member_count: int | None = None,
    joined: bool | None = None,
) -> GroupDiscoveryRead:
    if member_count is None:
        count_result = await db.execute(
            select(func.count()).select_from(ConversationMember).where(ConversationMember.conversation_id == conversation.id)
        )
        member_count = count_result.scalar_one()
    if joined is None:
        member_result = await db.execute(
            select(ConversationMember.id).where(
                ConversationMember.conversation_id == conversation.id,
                ConversationMember.user_id == current_user_id,
            )
        )
        joined = member_result.scalar_one_or_none() is not None

    membership_result = await db.execute(
        select(ConversationMember).where(
            ConversationMember.conversation_id == conversation.id,
            ConversationMember.user_id == current_user_id,
        )
    )
    membership = membership_result.scalar_one_or_none()
    join_request_result = await db.execute(
        select(GroupJoinRequest.status)
        .where(
            GroupJoinRequest.conversation_id == conversation.id,
            GroupJoinRequest.requester_id == current_user_id,
        )
        .order_by(GroupJoinRequest.created_at.desc(), GroupJoinRequest.id.desc())
        .limit(1)
    )
    join_request_status = join_request_result.scalar_one_or_none()
    display_title = _clean_text(membership.remark) if membership is not None else None

    return GroupDiscoveryRead(
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
        is_temporary=conversation.is_temporary,
        temporary_initiator_id=conversation.temporary_initiator_id,
        temporary_target_id=conversation.temporary_target_id,
        created_at=conversation.created_at,
        updated_at=conversation.updated_at,
        member_count=member_count,
        joined=joined,
        join_request_status=join_request_status,
    )


async def search_public_groups(db: AsyncSession, q: str, current_user_id: int, limit: int = 20) -> list[GroupDiscoveryRead]:
    term = q.strip()
    if not term:
        return []

    keyword = f"%{term}%"
    search_conditions = [
        Conversation.title.ilike(keyword),
        Conversation.description.ilike(keyword),
        Conversation.category.ilike(keyword),
    ]
    if term.isdigit():
        search_conditions.append(Conversation.id == int(term))

    result = await db.execute(
        select(Conversation)
        .where(*_public_group_conditions(), or_(*search_conditions))
        .order_by(Conversation.updated_at.desc(), Conversation.id.desc())
        .limit(limit)
    )
    conversations = list(result.scalars().all())
    return [await _build_group_discovery_read(db, item, current_user_id) for item in conversations]


async def recommended_public_groups(db: AsyncSession, current_user_id: int, limit: int = 20) -> list[GroupDiscoveryRead]:
    member_counts = (
        select(ConversationMember.conversation_id, func.count(ConversationMember.id).label("member_count"))
        .group_by(ConversationMember.conversation_id)
        .subquery()
    )
    current_membership = (
        select(ConversationMember.conversation_id)
        .where(ConversationMember.user_id == current_user_id)
        .subquery()
    )
    member_count = func.coalesce(member_counts.c.member_count, 0)
    result = await db.execute(
        select(Conversation, member_count.label("member_count"))
        .outerjoin(member_counts, member_counts.c.conversation_id == Conversation.id)
        .outerjoin(current_membership, current_membership.c.conversation_id == Conversation.id)
        .where(*_public_group_conditions(), current_membership.c.conversation_id.is_(None))
        .order_by(Conversation.updated_at.desc(), member_count.desc(), Conversation.id.desc())
        .limit(limit)
    )
    return [
        await _build_group_discovery_read(db, conversation, current_user_id, int(member_count_value), False)
        for conversation, member_count_value in result.all()
    ]


async def join_group(db: AsyncSession, conversation_id: int, user_id: int) -> tuple[Conversation, bool]:
    conversation = await require_group_conversation(db, conversation_id)
    existing = await db.execute(
        select(ConversationMember.id).where(
            ConversationMember.conversation_id == conversation_id,
            ConversationMember.user_id == user_id,
        )
    )
    if existing.scalar_one_or_none() is not None:
        return conversation, False
    if conversation.join_mode == "invite_only":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Group is invite only")
    if conversation.join_mode in {"approval", "question"} and not conversation.auto_approve:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Group requires approval")

    db.add(ConversationMember(conversation_id=conversation_id, user_id=user_id, role=MemberRole.MEMBER))
    conversation.updated_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(conversation)
    return conversation, True


async def create_group_join_request(
    db: AsyncSession,
    conversation_id: int,
    requester: User,
    payload: GroupJoinRequestCreate,
) -> GroupJoinRequest:
    conversation = await require_group_conversation(db, conversation_id)
    existing_member = await db.execute(
        select(ConversationMember.id).where(
            ConversationMember.conversation_id == conversation_id,
            ConversationMember.user_id == requester.id,
        )
    )
    if existing_member.scalar_one_or_none() is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Already joined this group")
    if conversation.join_mode == "invite_only":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Group is invite only")
    if conversation.join_mode == "open" or conversation.auto_approve:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="This group does not require approval")

    request_message = _clean_text(payload.message)
    request_answer = _clean_text(payload.answer)
    if conversation.join_mode == "question" and not request_answer:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Group join answer is required")
    existing_request_result = await db.execute(
        select(GroupJoinRequest)
        .options(joinedload(GroupJoinRequest.requester), joinedload(GroupJoinRequest.reviewer))
        .where(
            GroupJoinRequest.conversation_id == conversation_id,
            GroupJoinRequest.requester_id == requester.id,
            GroupJoinRequest.status == GroupJoinRequestStatus.PENDING,
        )
        .order_by(GroupJoinRequest.created_at.desc(), GroupJoinRequest.id.desc())
        .limit(1)
    )
    existing_request = existing_request_result.scalar_one_or_none()
    if existing_request is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="A pending join request already exists")

    join_request = GroupJoinRequest(
        conversation_id=conversation_id,
        requester_id=requester.id,
        message=request_message,
        answer=request_answer,
    )
    db.add(join_request)
    conversation.updated_at = datetime.now(UTC)
    await db.commit()
    return await get_group_join_request_for_user(db, join_request.id, requester.id)


async def list_members(db: AsyncSession, conversation_id: int, user_id: int) -> list[ConversationMember]:
    await require_membership(db, conversation_id, user_id)
    await clear_expired_mutes_for_conversation(db, conversation_id)
    result = await db.execute(
        select(ConversationMember)
        .options(joinedload(ConversationMember.user))
        .where(ConversationMember.conversation_id == conversation_id)
        .order_by(ConversationMember.joined_at.asc())
    )
    return list(result.scalars().unique().all())


async def list_members_page(
    db: AsyncSession,
    conversation_id: int,
    user_id: int,
    *,
    limit: int,
    offset: int,
    search: str | None = None,
) -> tuple[list[ConversationMember], int]:
    await require_membership(db, conversation_id, user_id)
    await clear_expired_mutes_for_conversation(db, conversation_id)
    normalized_search = _clean_text(search)
    conditions = [ConversationMember.conversation_id == conversation_id]
    if normalized_search:
        pattern = f"%{normalized_search}%"
        conditions.append(
            or_(
                ConversationMember.nickname.ilike(pattern),
                ConversationMember.remark.ilike(pattern),
                ConversationMember.title.ilike(pattern),
                User.username.ilike(pattern),
                User.nickname.ilike(pattern),
                User.bio.ilike(pattern),
            )
        )

    total_result = await db.execute(
        select(func.count(ConversationMember.id))
        .join(User, User.id == ConversationMember.user_id)
        .where(*conditions)
    )
    total = total_result.scalar_one()

    result = await db.execute(
        select(ConversationMember)
        .join(User, User.id == ConversationMember.user_id)
        .options(joinedload(ConversationMember.user))
        .where(*conditions)
        .order_by(
            case(
                (ConversationMember.role == MemberRole.OWNER, 0),
                (ConversationMember.role == MemberRole.ADMIN, 1),
                else_=2,
            ),
            ConversationMember.joined_at.asc(),
        )
        .offset(offset)
        .limit(limit)
    )
    return list(result.scalars().unique().all()), total


async def search_members(
    db: AsyncSession,
    conversation_id: int,
    user_id: int,
    *,
    query: str | None = None,
    limit: int = 12,
) -> list[ConversationMember]:
    await require_membership(db, conversation_id, user_id)
    await clear_expired_mutes_for_conversation(db, conversation_id)
    normalized_query = _clean_text(query)
    conditions = [ConversationMember.conversation_id == conversation_id]
    if normalized_query:
        pattern = f"%{normalized_query}%"
        conditions.append(
            or_(
                ConversationMember.nickname.ilike(pattern),
                ConversationMember.remark.ilike(pattern),
                ConversationMember.title.ilike(pattern),
                User.username.ilike(pattern),
                User.nickname.ilike(pattern),
            )
        )

    result = await db.execute(
        select(ConversationMember)
        .join(User, User.id == ConversationMember.user_id)
        .options(joinedload(ConversationMember.user))
        .where(*conditions)
        .order_by(
            case(
                (ConversationMember.role == MemberRole.OWNER, 0),
                (ConversationMember.role == MemberRole.ADMIN, 1),
                else_=2,
            ),
            ConversationMember.joined_at.asc(),
        )
        .limit(limit)
    )
    return list(result.scalars().unique().all())


async def invite_members(db: AsyncSession, conversation_id: int, inviter_id: int, user_ids: list[int]) -> list[ConversationMember]:
    inviter = await require_membership(db, conversation_id, inviter_id)
    conversation = await require_group_conversation(db, conversation_id)
    if inviter.role not in STAFF_ROLES:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only group owners or admins can invite members")

    unique_ids = list(dict.fromkeys(user_ids))
    existing_users = await db.execute(select(User.id).where(User.id.in_(unique_ids)))
    existing_ids = set(existing_users.scalars().all())
    missing_ids = sorted(set(unique_ids) - existing_ids)
    if missing_ids:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Users not found: {missing_ids}")

    existing_members = await db.execute(
        select(ConversationMember.user_id).where(
            ConversationMember.conversation_id == conversation_id,
            ConversationMember.user_id.in_(unique_ids),
        )
    )
    already_member_ids = set(existing_members.scalars().all())
    new_ids = [user_id for user_id in unique_ids if user_id not in already_member_ids]
    for user_id in new_ids:
        db.add(ConversationMember(conversation_id=conversation_id, user_id=user_id, role=MemberRole.MEMBER))
    conversation.updated_at = datetime.now(UTC)
    await db.commit()
    return await list_members(db, conversation_id, inviter_id)


async def update_group_announcement(
    db: AsyncSession,
    conversation_id: int,
    actor_id: int,
    announcement: str | None,
) -> Conversation:
    actor = await require_membership(db, conversation_id, actor_id)
    conversation = await require_group_conversation(db, conversation_id)
    if actor.role not in STAFF_ROLES:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only group owners or admins can update announcements")

    conversation.announcement = announcement
    conversation.updated_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(conversation)
    return conversation


async def update_group_profile(
    db: AsyncSession,
    conversation_id: int,
    actor_id: int,
    payload: ConversationProfileUpdate,
) -> Conversation:
    actor = await require_membership(db, conversation_id, actor_id)
    conversation = await require_group_conversation(db, conversation_id)
    if actor.role not in STAFF_ROLES:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only group owners or admins can update group profile")

    updates = payload.model_dump(exclude_unset=True)
    if "avatar_url" in updates:
        updates["avatar_url"] = require_avatar_upload_url(updates["avatar_url"])
    target_join_mode = updates.get("join_mode", conversation.join_mode)
    if "join_mode" in updates and target_join_mode != "question":
        updates["join_question"] = None
    if target_join_mode == "question" and ("join_mode" in updates or "join_question" in updates):
        question = _clean_text(updates.get("join_question") if "join_question" in updates else conversation.join_question)
        if not question:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Join question is required")
        updates["join_question"] = question
    for field, value in updates.items():
        setattr(conversation, field, value)
    if updates:
        conversation.updated_at = datetime.now(UTC)
        await db.commit()
        await db.refresh(conversation)
    return conversation


async def update_group_settings(
    db: AsyncSession,
    conversation_id: int,
    actor_id: int,
    payload: GroupSettingsUpdate,
) -> Conversation:
    actor = await require_membership(db, conversation_id, actor_id)
    conversation = await require_group_conversation(db, conversation_id)
    if actor.role not in STAFF_ROLES:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only group owners or admins can update group settings")

    updates = payload.model_dump(exclude_unset=True)
    target_join_mode = updates.get("join_mode", conversation.join_mode)
    if "join_mode" in updates and target_join_mode != "question":
        updates["join_question"] = None
    if target_join_mode == "question" and ("join_mode" in updates or "join_question" in updates):
        question = _clean_text(updates.get("join_question") if "join_question" in updates else conversation.join_question)
        if not question:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Join question is required")
        updates["join_question"] = question
        updates["auto_approve"] = False
    for field, value in updates.items():
        setattr(conversation, field, value)
    if updates:
        conversation.updated_at = datetime.now(UTC)
        await db.commit()
        await db.refresh(conversation)
    return conversation


async def update_my_member_settings(
    db: AsyncSession,
    conversation_id: int,
    actor_id: int,
    payload: MemberSettingsUpdate,
) -> ConversationMember:
    member = await require_membership(db, conversation_id, actor_id)
    updates = payload.model_dump(exclude_unset=True)
    for field, value in updates.items():
        setattr(member, field, value)
    if updates:
        await db.commit()
    return await get_member(db, conversation_id, actor_id)


async def update_member_title(
    db: AsyncSession,
    conversation_id: int,
    actor_id: int,
    target_user_id: int,
    payload: MemberTitleUpdate,
) -> ConversationMember:
    actor = await require_membership(db, conversation_id, actor_id)
    conversation = await require_group_conversation(db, conversation_id)
    if actor.role != MemberRole.OWNER:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only group owners can set member titles")
    target = await get_member(db, conversation_id, target_user_id)
    target.title = _clean_text(payload.title)
    conversation.updated_at = datetime.now(UTC)
    await db.commit()
    return await get_member(db, conversation_id, target_user_id)


async def list_announcements(db: AsyncSession, conversation_id: int, actor_id: int) -> list[GroupAnnouncement]:
    await require_membership(db, conversation_id, actor_id)
    await require_group_conversation(db, conversation_id)
    result = await db.execute(
        select(GroupAnnouncement)
        .options(joinedload(GroupAnnouncement.author))
        .where(GroupAnnouncement.conversation_id == conversation_id)
        .order_by(GroupAnnouncement.pinned.desc(), GroupAnnouncement.created_at.desc(), GroupAnnouncement.id.desc())
    )
    return list(result.scalars().all())


async def create_announcement(
    db: AsyncSession,
    conversation_id: int,
    actor: User,
    content: str,
    pinned: bool,
) -> GroupAnnouncement:
    member = await require_membership(db, conversation_id, actor.id)
    conversation = await require_group_conversation(db, conversation_id)
    if member.role not in STAFF_ROLES:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only group owners or admins can publish announcements")

    if pinned:
        await db.execute(
            update(GroupAnnouncement)
            .where(GroupAnnouncement.conversation_id == conversation_id, GroupAnnouncement.pinned.is_(True))
            .values(pinned=False, updated_at=datetime.now(UTC))
        )

    announcement = GroupAnnouncement(
        conversation_id=conversation_id,
        author_id=actor.id,
        content=content,
        pinned=pinned,
    )
    db.add(announcement)
    conversation.announcement = content
    conversation.updated_at = datetime.now(UTC)
    await db.commit()
    result = await db.execute(
        select(GroupAnnouncement)
        .options(joinedload(GroupAnnouncement.author))
        .where(GroupAnnouncement.id == announcement.id)
    )
    return result.scalar_one()


async def update_announcement(
    db: AsyncSession,
    conversation_id: int,
    announcement_id: int,
    actor: User,
    content: str,
    pinned: bool,
) -> GroupAnnouncement:
    conversation = await require_group_conversation(db, conversation_id)
    membership = await get_member(db, conversation_id, actor.id)
    if membership.role not in STAFF_ROLES:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only group owners or admins can update announcements")

    result = await db.execute(
        select(GroupAnnouncement)
        .options(joinedload(GroupAnnouncement.author))
        .where(GroupAnnouncement.id == announcement_id, GroupAnnouncement.conversation_id == conversation_id)
    )
    announcement = result.scalar_one_or_none()
    if announcement is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Announcement not found")

    now = datetime.now(UTC)
    if pinned:
        await db.execute(
            update(GroupAnnouncement)
            .where(
                GroupAnnouncement.conversation_id == conversation_id,
                GroupAnnouncement.id != announcement_id,
                GroupAnnouncement.pinned.is_(True),
            )
            .values(pinned=False, updated_at=now)
        )

    announcement.content = content
    announcement.pinned = pinned
    announcement.updated_at = now
    conversation.announcement = content
    conversation.updated_at = now
    await db.commit()

    result = await db.execute(
        select(GroupAnnouncement)
        .options(joinedload(GroupAnnouncement.author))
        .where(GroupAnnouncement.id == announcement_id)
    )
    return result.scalar_one()


async def update_member_role(
    db: AsyncSession,
    conversation_id: int,
    actor_id: int,
    target_user_id: int,
    role: MemberRole,
) -> ConversationMember:
    actor = await require_membership(db, conversation_id, actor_id)
    conversation = await require_group_conversation(db, conversation_id)
    if actor.role != MemberRole.OWNER:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only group owners can change member roles")
    if role == MemberRole.OWNER:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Owner role cannot be assigned through this endpoint")
    if target_user_id == actor_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Owner cannot demote self")

    target = await get_member(db, conversation_id, target_user_id)
    target.role = role
    conversation.updated_at = datetime.now(UTC)
    await db.commit()
    return await get_member(db, conversation_id, target_user_id)


async def update_member_mute(
    db: AsyncSession,
    conversation_id: int,
    actor_id: int,
    target_user_id: int,
    muted: bool,
    muted_until: datetime | None = None,
) -> ConversationMember:
    actor = await require_membership(db, conversation_id, actor_id)
    conversation = await require_group_conversation(db, conversation_id)
    if actor.role not in STAFF_ROLES:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only group owners or admins can mute members")

    target = await get_member(db, conversation_id, target_user_id)
    if actor.role == MemberRole.ADMIN and target.role in STAFF_ROLES:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admins cannot mute owners or admins")

    target.muted = muted
    target.muted_until = muted_until if muted else None
    conversation.updated_at = datetime.now(UTC)
    await db.commit()
    return await get_member(db, conversation_id, target_user_id)


async def remove_member(db: AsyncSession, conversation_id: int, actor_id: int, target_user_id: int) -> None:
    actor = await require_membership(db, conversation_id, actor_id)
    conversation = await require_group_conversation(db, conversation_id)
    if actor.role not in STAFF_ROLES:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only group owners or admins can remove members")
    if target_user_id == actor_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Use the leave endpoint to remove yourself")

    target = await get_member(db, conversation_id, target_user_id)
    if actor.role == MemberRole.OWNER:
        if target.role == MemberRole.OWNER:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Owners cannot remove another owner")
    elif target.role in STAFF_ROLES:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admins can only remove normal members")

    await db.delete(target)
    conversation.updated_at = datetime.now(UTC)
    await db.commit()


async def leave_group(db: AsyncSession, conversation_id: int, user_id: int) -> None:
    membership = await require_membership(db, conversation_id, user_id)
    conversation = await get_conversation(db, conversation_id)
    if conversation.type != ConversationType.GROUP:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Direct conversations cannot be left")
    if membership.role == MemberRole.OWNER:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Group owners must dissolve the group instead of leaving")

    await db.delete(membership)
    await db.flush()
    remaining_members = await db.execute(
        select(ConversationMember)
        .where(ConversationMember.conversation_id == conversation_id)
        .order_by(ConversationMember.joined_at.asc())
    )
    remaining = list(remaining_members.scalars().all())
    if not remaining:
        await db.delete(conversation)
    else:
        conversation.updated_at = datetime.now(UTC)
    await db.commit()


async def dissolve_group(db: AsyncSession, conversation_id: int, actor_id: int) -> None:
    actor = await require_membership(db, conversation_id, actor_id)
    conversation = await require_group_conversation(db, conversation_id)
    if actor.role != MemberRole.OWNER:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only the group owner can dissolve this group")

    await db.delete(conversation)
    await db.commit()


async def clear_conversation_history(db: AsyncSession, conversation_id: int, user_id: int) -> int | None:
    membership = await require_membership(db, conversation_id, user_id)
    latest_result = await db.execute(
        select(Message.id)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.id.desc())
        .limit(1)
    )
    latest_message_id = latest_result.scalar_one_or_none()
    now = datetime.now(UTC)
    membership.cleared_before_message_id = latest_message_id
    membership.last_read_message_id = latest_message_id
    membership.last_read_at = now
    await db.commit()
    return latest_message_id


async def conversation_member_ids(db: AsyncSession, conversation_id: int) -> list[int]:
    result = await db.execute(select(ConversationMember.user_id).where(ConversationMember.conversation_id == conversation_id))
    return list(result.scalars().all())


async def conversation_member_ids_in(db: AsyncSession, conversation_id: int, user_ids: set[int]) -> list[int]:
    if not user_ids:
        return []
    result = await db.execute(
        select(ConversationMember.user_id).where(
            ConversationMember.conversation_id == conversation_id,
            ConversationMember.user_id.in_(user_ids),
        )
    )
    return list(result.scalars().all())


async def pending_join_request_reviewer_ids(db: AsyncSession, conversation_id: int) -> list[int]:
    result = await db.execute(
        select(ConversationMember.user_id).where(
            ConversationMember.conversation_id == conversation_id,
            ConversationMember.role.in_(STAFF_ROLES),
        )
    )
    return list(result.scalars().all())


async def get_group_join_request_for_user(db: AsyncSession, request_id: int, user_id: int) -> GroupJoinRequest:
    result = await db.execute(
        select(GroupJoinRequest)
        .options(
            joinedload(GroupJoinRequest.requester),
            joinedload(GroupJoinRequest.reviewer),
            joinedload(GroupJoinRequest.conversation),
        )
        .where(
            GroupJoinRequest.id == request_id,
            or_(GroupJoinRequest.requester_id == user_id, GroupJoinRequest.reviewer_id == user_id),
        )
    )
    join_request = result.scalar_one_or_none()
    if join_request is None:
        membership_result = await db.execute(
            select(ConversationMember.id)
            .join(GroupJoinRequest, GroupJoinRequest.conversation_id == ConversationMember.conversation_id)
            .where(
                GroupJoinRequest.id == request_id,
                ConversationMember.user_id == user_id,
                ConversationMember.role.in_(STAFF_ROLES),
            )
            .limit(1)
        )
        if membership_result.scalar_one_or_none() is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Group join request not found")
        result = await db.execute(
            select(GroupJoinRequest)
            .options(
                joinedload(GroupJoinRequest.requester),
                joinedload(GroupJoinRequest.reviewer),
                joinedload(GroupJoinRequest.conversation),
            )
            .where(GroupJoinRequest.id == request_id)
        )
        join_request = result.scalar_one_or_none()
    if join_request is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Group join request not found")
    return join_request


async def list_incoming_group_join_requests(db: AsyncSession, user_id: int) -> list[GroupJoinRequest]:
    result = await db.execute(
        select(GroupJoinRequest)
        .options(
            joinedload(GroupJoinRequest.requester),
            joinedload(GroupJoinRequest.reviewer),
            joinedload(GroupJoinRequest.conversation),
        )
        .join(ConversationMember, ConversationMember.conversation_id == GroupJoinRequest.conversation_id)
        .where(
            ConversationMember.user_id == user_id,
            ConversationMember.role.in_(STAFF_ROLES),
        )
        .order_by(
            GroupJoinRequest.status == GroupJoinRequestStatus.PENDING,
            GroupJoinRequest.created_at.desc(),
            GroupJoinRequest.id.desc(),
        )
    )
    return list(result.scalars().unique().all())


async def list_outgoing_group_join_requests(db: AsyncSession, user_id: int) -> list[GroupJoinRequest]:
    result = await db.execute(
        select(GroupJoinRequest)
        .options(
            joinedload(GroupJoinRequest.requester),
            joinedload(GroupJoinRequest.reviewer),
            joinedload(GroupJoinRequest.conversation),
        )
        .where(GroupJoinRequest.requester_id == user_id)
        .order_by(GroupJoinRequest.created_at.desc(), GroupJoinRequest.id.desc())
    )
    return list(result.scalars().all())


async def review_group_join_request(
    db: AsyncSession,
    request_id: int,
    reviewer_id: int,
    approved: bool,
    payload: GroupJoinRequestDecision,
) -> tuple[GroupJoinRequest, bool]:
    join_request = await get_group_join_request_for_user(db, request_id, reviewer_id)
    reviewer_member = await require_membership(db, join_request.conversation_id, reviewer_id)
    if reviewer_member.role not in STAFF_ROLES:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only group owners or admins can review join requests")
    if join_request.status != GroupJoinRequestStatus.PENDING:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Group join request has already been processed")

    requester_member_result = await db.execute(
        select(ConversationMember.id).where(
            ConversationMember.conversation_id == join_request.conversation_id,
            ConversationMember.user_id == join_request.requester_id,
        )
    )
    already_joined = requester_member_result.scalar_one_or_none() is not None
    join_request.reviewer_id = reviewer_id
    join_request.decision_note = _clean_text(payload.decision_note)
    join_request.status = GroupJoinRequestStatus.ACCEPTED if approved else GroupJoinRequestStatus.REJECTED

    added = False
    if approved and not already_joined:
        db.add(
            ConversationMember(
                conversation_id=join_request.conversation_id,
                user_id=join_request.requester_id,
                role=MemberRole.MEMBER,
            )
        )
        added = True

    conversation = await require_group_conversation(db, join_request.conversation_id)
    conversation.updated_at = datetime.now(UTC)
    await db.commit()
    return await get_group_join_request_for_user(db, join_request.id, reviewer_id), added


async def build_group_join_request_read(
    db: AsyncSession,
    join_request: GroupJoinRequest,
    current_user_id: int,
) -> GroupJoinRequestRead:
    conversation = join_request.conversation
    member_count = 0
    display_title = None
    if conversation is not None:
        member_count_result = await db.execute(
            select(func.count()).select_from(ConversationMember).where(ConversationMember.conversation_id == conversation.id)
        )
        member_count = member_count_result.scalar_one()
        membership_result = await db.execute(
            select(ConversationMember).where(
                ConversationMember.conversation_id == conversation.id,
                ConversationMember.user_id == current_user_id,
            )
        )
        membership = membership_result.scalar_one_or_none()
        if membership is not None:
            display_title = _conversation_display_title(conversation, membership)

    return GroupJoinRequestRead(
        id=join_request.id,
        conversation_id=join_request.conversation_id,
        conversation_title=conversation.title if conversation is not None else None,
        conversation_display_title=display_title or (conversation.title if conversation is not None else None),
        conversation_avatar_url=conversation.avatar_url if conversation is not None else None,
        conversation_member_count=member_count,
        requester_id=join_request.requester_id,
        reviewer_id=join_request.reviewer_id,
        status=join_request.status,
        message=join_request.message,
        answer=join_request.answer,
        decision_note=join_request.decision_note,
        created_at=join_request.created_at,
        updated_at=join_request.updated_at,
        requester=UserPublic.model_validate(join_request.requester) if join_request.requester is not None else None,
        reviewer=UserPublic.model_validate(join_request.reviewer) if join_request.reviewer is not None else None,
    )


async def build_conversation_read(db: AsyncSession, conversation: Conversation, current_user_id: int) -> ConversationRead:
    member_count_result = await db.execute(
        select(func.count()).select_from(ConversationMember).where(ConversationMember.conversation_id == conversation.id)
    )
    member_count = member_count_result.scalar_one()

    membership = await require_membership(db, conversation.id, current_user_id)

    last_message_conditions = [Message.conversation_id == conversation.id]
    hidden_message_ids = select(MessageVisibility.message_id).where(MessageVisibility.user_id == current_user_id)
    last_message_conditions.append(Message.id.not_in(hidden_message_ids))
    if membership.cleared_before_message_id is not None:
        last_message_conditions.append(Message.id > membership.cleared_before_message_id)

    last_message_result = await db.execute(
        select(Message)
        .options(joinedload(Message.sender))
        .where(and_(*last_message_conditions))
        .order_by(Message.id.desc())
        .limit(1)
    )
    last_message = last_message_result.scalar_one_or_none()

    unread_conditions = [Message.conversation_id == conversation.id, Message.sender_id != current_user_id]
    unread_conditions.append(Message.id.not_in(hidden_message_ids))
    if membership.last_read_message_id is not None:
        unread_conditions.append(Message.id > membership.last_read_message_id)
    if membership.cleared_before_message_id is not None:
        unread_conditions.append(Message.id > membership.cleared_before_message_id)
    unread_result = await db.execute(select(func.count()).select_from(Message).where(and_(*unread_conditions)))
    unread_count = unread_result.scalar_one()

    direct_user: UserPublic | None = None
    direct_user_model: User | None = None
    direct_is_friend = False
    if conversation.type == ConversationType.DIRECT:
        direct_user_result = await db.execute(
            select(User)
            .join(ConversationMember, ConversationMember.user_id == User.id)
            .where(ConversationMember.conversation_id == conversation.id, User.id != current_user_id)
            .limit(1)
        )
        direct_user_model = direct_user_result.scalar_one_or_none()
        if direct_user_model is not None:
            direct_user = UserPublic.model_validate(direct_user_model)
            direct_is_friend = await are_friends(db, current_user_id, direct_user_model.id)
            if not direct_is_friend and not conversation.is_temporary:
                conversation.is_temporary = True
                conversation.temporary_initiator_id = conversation.created_by_id or current_user_id
                conversation.temporary_target_id = direct_user_model.id if conversation.temporary_initiator_id == current_user_id else current_user_id
                await db.commit()
                await db.refresh(conversation)

    pending_join_request_count = 0
    if conversation.type == ConversationType.GROUP and membership.role in STAFF_ROLES:
        pending_result = await db.execute(
            select(func.count()).select_from(GroupJoinRequest).where(
                GroupJoinRequest.conversation_id == conversation.id,
                GroupJoinRequest.status == GroupJoinRequestStatus.PENDING,
            )
        )
        pending_join_request_count = pending_result.scalar_one()

    display_title = _conversation_display_title(conversation, membership, direct_user_model)

    last_message_read: MessageRead | None = None
    if last_message is not None:
        from app.services import messages as message_service

        last_message_read = await message_service.build_message_read(db, last_message, current_user_id)

    return ConversationRead(
        id=conversation.id,
        type=conversation.type,
        title=conversation.title,
        display_title=display_title,
        avatar_url=conversation.avatar_url,
        description=conversation.description,
        category=conversation.category,
        announcement=conversation.announcement,
        all_muted=conversation.all_muted,
        slow_mode_seconds=conversation.slow_mode_seconds,
        message_rate_limit_per_minute=conversation.message_rate_limit_per_minute,
        join_mode=conversation.join_mode,
        auto_approve=conversation.auto_approve,
        join_question=conversation.join_question,
        tasks_enabled=conversation.tasks_enabled,
        task_creation_permission=conversation.task_creation_permission,
        is_temporary=conversation.is_temporary,
        temporary_initiator_id=conversation.temporary_initiator_id,
        temporary_target_id=conversation.temporary_target_id,
        is_friend=direct_is_friend,
        created_at=conversation.created_at,
        updated_at=conversation.updated_at,
        member_count=member_count,
        unread_count=unread_count,
        last_message=last_message_read,
        direct_user=direct_user,
        my_role=membership.role,
        my_nickname=membership.nickname,
        my_remark=membership.remark,
        my_pinned=membership.pinned,
        my_do_not_disturb=membership.do_not_disturb,
        my_message_setting=membership.message_setting,
        pending_join_request_count=pending_join_request_count,
    )


async def build_conversation_reads(db: AsyncSession, conversations: list[Conversation], current_user_id: int) -> list[ConversationRead]:
    if not conversations:
        return []

    conversation_ids = [conversation.id for conversation in conversations]

    member_count_result = await db.execute(
        select(ConversationMember.conversation_id, func.count(ConversationMember.id))
        .where(ConversationMember.conversation_id.in_(conversation_ids))
        .group_by(ConversationMember.conversation_id)
    )
    member_counts = {conversation_id: int(count) for conversation_id, count in member_count_result.all()}

    membership_result = await db.execute(
        select(ConversationMember).where(
            ConversationMember.conversation_id.in_(conversation_ids),
            ConversationMember.user_id == current_user_id,
        )
    )
    memberships = {member.conversation_id: member for member in membership_result.scalars().all()}
    if len(memberships) != len(conversation_ids):
        return [await build_conversation_read(db, conversation, current_user_id) for conversation in conversations]

    hidden_message_ids = select(MessageVisibility.message_id).where(MessageVisibility.user_id == current_user_id)
    last_message_ids = (
        select(
            Message.conversation_id.label("conversation_id"),
            func.max(Message.id).label("message_id"),
        )
        .join(
            ConversationMember,
            and_(
                ConversationMember.conversation_id == Message.conversation_id,
                ConversationMember.user_id == current_user_id,
            ),
        )
        .where(
            Message.conversation_id.in_(conversation_ids),
            Message.id.not_in(hidden_message_ids),
            or_(
                ConversationMember.cleared_before_message_id.is_(None),
                Message.id > ConversationMember.cleared_before_message_id,
            ),
        )
        .group_by(Message.conversation_id)
        .subquery()
    )
    last_message_result = await db.execute(
        select(Message)
        .options(joinedload(Message.sender))
        .join(last_message_ids, last_message_ids.c.message_id == Message.id)
    )
    last_messages = {message.conversation_id: message for message in last_message_result.scalars().all()}

    unread_result = await db.execute(
        select(Message.conversation_id, func.count(Message.id))
        .join(
            ConversationMember,
            and_(
                ConversationMember.conversation_id == Message.conversation_id,
                ConversationMember.user_id == current_user_id,
            ),
        )
        .where(
            Message.conversation_id.in_(conversation_ids),
            Message.sender_id != current_user_id,
            Message.id.not_in(hidden_message_ids),
            or_(
                ConversationMember.last_read_message_id.is_(None),
                Message.id > ConversationMember.last_read_message_id,
            ),
            or_(
                ConversationMember.cleared_before_message_id.is_(None),
                Message.id > ConversationMember.cleared_before_message_id,
            ),
        )
        .group_by(Message.conversation_id)
    )
    unread_counts = {conversation_id: int(count) for conversation_id, count in unread_result.all()}

    direct_conversation_ids = [conversation.id for conversation in conversations if conversation.type == ConversationType.DIRECT]
    direct_members: dict[int, ConversationMember] = {}
    if direct_conversation_ids:
        direct_members_result = await db.execute(
            select(ConversationMember)
            .options(joinedload(ConversationMember.user))
            .where(
                ConversationMember.conversation_id.in_(direct_conversation_ids),
                ConversationMember.user_id != current_user_id,
            )
        )
        for member in direct_members_result.scalars().unique().all():
            direct_members.setdefault(member.conversation_id, member)

    direct_peer_ids = [member.user_id for member in direct_members.values()]
    friend_ids: set[int] = set()
    if direct_peer_ids:
        friend_result = await db.execute(
            select(Friendship.friend_id).where(
                Friendship.user_id == current_user_id,
                Friendship.friend_id.in_(direct_peer_ids),
            )
        )
        friend_ids = set(friend_result.scalars().all())

    pending_join_request_counts: dict[int, int] = {}
    reviewer_group_ids = [
        conversation.id
        for conversation in conversations
        if conversation.type == ConversationType.GROUP and memberships[conversation.id].role in STAFF_ROLES
    ]
    if reviewer_group_ids:
        pending_result = await db.execute(
            select(GroupJoinRequest.conversation_id, func.count(GroupJoinRequest.id))
            .where(
                GroupJoinRequest.conversation_id.in_(reviewer_group_ids),
                GroupJoinRequest.status == GroupJoinRequestStatus.PENDING,
            )
            .group_by(GroupJoinRequest.conversation_id)
        )
        pending_join_request_counts = {conversation_id: int(count) for conversation_id, count in pending_result.all()}

    changed_temporary = False
    for conversation in conversations:
        if conversation.type != ConversationType.DIRECT:
            continue
        direct_member = direct_members.get(conversation.id)
        if direct_member is None or direct_member.user_id in friend_ids or conversation.is_temporary:
            continue
        conversation.is_temporary = True
        conversation.temporary_initiator_id = conversation.created_by_id or current_user_id
        conversation.temporary_target_id = direct_member.user_id if conversation.temporary_initiator_id == current_user_id else current_user_id
        changed_temporary = True
    if changed_temporary:
        await db.commit()

    last_message_reads: dict[int, MessageRead] = {}
    messages_to_build = [last_messages[conversation.id] for conversation in conversations if conversation.id in last_messages]
    if messages_to_build:
        from app.services import messages as message_service

        for message_read in await message_service.build_message_reads(db, messages_to_build, current_user_id):
            last_message_reads[message_read.conversation_id] = message_read

    reads: list[ConversationRead] = []
    for conversation in conversations:
        membership = memberships[conversation.id]
        direct_member = direct_members.get(conversation.id)
        direct_user_model = direct_member.user if direct_member is not None else None
        direct_user = UserPublic.model_validate(direct_user_model) if direct_user_model is not None else None
        direct_is_friend = direct_member.user_id in friend_ids if direct_member is not None else False

        reads.append(
            ConversationRead(
                id=conversation.id,
                type=conversation.type,
                title=conversation.title,
                display_title=_conversation_display_title(conversation, membership, direct_user_model),
                avatar_url=conversation.avatar_url,
                description=conversation.description,
                category=conversation.category,
                announcement=conversation.announcement,
                all_muted=conversation.all_muted,
                slow_mode_seconds=conversation.slow_mode_seconds,
                message_rate_limit_per_minute=conversation.message_rate_limit_per_minute,
                join_mode=conversation.join_mode,
                auto_approve=conversation.auto_approve,
                join_question=conversation.join_question,
                tasks_enabled=conversation.tasks_enabled,
                task_creation_permission=conversation.task_creation_permission,
                is_temporary=conversation.is_temporary,
                temporary_initiator_id=conversation.temporary_initiator_id,
                temporary_target_id=conversation.temporary_target_id,
                is_friend=direct_is_friend,
                created_at=conversation.created_at,
                updated_at=conversation.updated_at,
                member_count=member_counts.get(conversation.id, 0),
                unread_count=unread_counts.get(conversation.id, 0),
                last_message=last_message_reads.get(conversation.id),
                direct_user=direct_user,
                my_role=membership.role,
                my_nickname=membership.nickname,
                my_remark=membership.remark,
                my_pinned=membership.pinned,
                my_do_not_disturb=membership.do_not_disturb,
                my_message_setting=membership.message_setting,
                pending_join_request_count=pending_join_request_counts.get(conversation.id, 0),
            )
        )

    return reads


async def delete_announcement(db: AsyncSession, conversation_id: int, announcement_id: int, actor: User) -> GroupAnnouncement:
    conversation = await require_group_conversation(db, conversation_id)
    membership = await get_member(db, conversation_id, actor.id)
    if membership.role not in STAFF_ROLES:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only group owners or admins can delete announcements")
    result = await db.execute(
        select(GroupAnnouncement)
        .where(GroupAnnouncement.id == announcement_id, GroupAnnouncement.conversation_id == conversation_id)
    )
    announcement = result.scalar_one_or_none()
    if announcement is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Announcement not found")

    deleted_content = announcement.content
    await db.delete(announcement)
    if conversation.announcement == deleted_content:
        latest_result = await db.execute(
            select(GroupAnnouncement)
            .where(GroupAnnouncement.conversation_id == conversation_id, GroupAnnouncement.id != announcement_id)
            .order_by(GroupAnnouncement.pinned.desc(), GroupAnnouncement.created_at.desc(), GroupAnnouncement.id.desc())
            .limit(1)
        )
        latest = latest_result.scalar_one_or_none()
        conversation.announcement = latest.content if latest else None
    await db.commit()
    return announcement
