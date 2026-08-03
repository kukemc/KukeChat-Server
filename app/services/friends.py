from __future__ import annotations

from datetime import datetime

from fastapi import HTTPException, status
from sqlalchemy import and_, delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.models.enums import FriendRequestStatus
from app.models.conversation import TemporaryConversationBlock
from app.models.friend import FriendRequest, Friendship
from app.models.user import User


async def are_friends(db: AsyncSession, user_id: int, friend_id: int) -> bool:
    result = await db.execute(select(Friendship.id).where(Friendship.user_id == user_id, Friendship.friend_id == friend_id))
    return result.scalar_one_or_none() is not None


async def create_friend_request(db: AsyncSession, requester: User, receiver_id: int) -> FriendRequest:
    if requester.id == receiver_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot add yourself as a friend")
    receiver = await db.get(User, receiver_id)
    if receiver is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Receiver not found")
    if await are_friends(db, requester.id, receiver_id):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="你们已经是好友了")

    existing = await db.execute(
        select(FriendRequest)
        .options(joinedload(FriendRequest.requester), joinedload(FriendRequest.receiver))
        .where(
            or_(
                and_(FriendRequest.requester_id == requester.id, FriendRequest.receiver_id == receiver_id),
                and_(FriendRequest.requester_id == receiver_id, FriendRequest.receiver_id == requester.id),
            ),
            FriendRequest.status == FriendRequestStatus.PENDING,
        )
    )
    existing_request = existing.scalar_one_or_none()
    if existing_request is not None:
        # The other user already sent us a pending request — clicking "add/组队" is
        # mutual consent, so accept it and become friends instead of erroring.
        if existing_request.requester_id == receiver_id and existing_request.receiver_id == requester.id:
            return await accept_friend_request(db, existing_request.id, requester.id)
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="你已发送过好友申请，正在等待对方通过")

    request = FriendRequest(requester_id=requester.id, receiver_id=receiver_id)
    db.add(request)
    await db.commit()
    return await get_friend_request_for_user(db, request.id, requester.id)


async def list_incoming_requests(db: AsyncSession, user_id: int) -> list[FriendRequest]:
    result = await db.execute(
        select(FriendRequest)
        .options(joinedload(FriendRequest.requester), joinedload(FriendRequest.receiver))
        .where(FriendRequest.receiver_id == user_id)
        .order_by(FriendRequest.created_at.desc())
    )
    return list(result.scalars().all())


async def list_outgoing_requests(db: AsyncSession, user_id: int) -> list[FriendRequest]:
    result = await db.execute(
        select(FriendRequest)
        .options(joinedload(FriendRequest.requester), joinedload(FriendRequest.receiver))
        .where(FriendRequest.requester_id == user_id)
        .order_by(FriendRequest.created_at.desc())
    )
    return list(result.scalars().all())


async def get_friend_request_for_user(db: AsyncSession, request_id: int, user_id: int) -> FriendRequest:
    result = await db.execute(
        select(FriendRequest)
        .options(joinedload(FriendRequest.requester), joinedload(FriendRequest.receiver))
        .where(
            FriendRequest.id == request_id,
            or_(FriendRequest.requester_id == user_id, FriendRequest.receiver_id == user_id),
        )
    )
    request = result.scalar_one_or_none()
    if request is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Friend request not found")
    return request


async def accept_friend_request(db: AsyncSession, request_id: int, user_id: int) -> FriendRequest:
    request = await get_friend_request_for_user(db, request_id, user_id)
    if request.receiver_id != user_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only the receiver can accept this request")
    if request.status != FriendRequestStatus.PENDING:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Friend request has already been processed")

    request.status = FriendRequestStatus.ACCEPTED
    if not await are_friends(db, request.requester_id, request.receiver_id):
        db.add(Friendship(user_id=request.requester_id, friend_id=request.receiver_id))
        db.add(Friendship(user_id=request.receiver_id, friend_id=request.requester_id))
    from app.models.conversation import Conversation, ConversationMember
    from app.models.enums import ConversationType, MemberRole
    from app.services.conversations import direct_key_for

    direct_key = direct_key_for(request.requester_id, request.receiver_id)
    conversation_result = await db.execute(select(Conversation).where(Conversation.direct_key == direct_key))
    conversation = conversation_result.scalar_one_or_none()
    if conversation is None:
        conversation = Conversation(type=ConversationType.DIRECT, direct_key=direct_key, created_by_id=request.requester_id)
        db.add(conversation)
        await db.flush()
        db.add_all(
            [
                ConversationMember(conversation_id=conversation.id, user_id=request.requester_id, role=MemberRole.MEMBER),
                ConversationMember(conversation_id=conversation.id, user_id=request.receiver_id, role=MemberRole.MEMBER),
            ]
        )
    elif conversation.is_temporary:
        conversation.is_temporary = False
        conversation.temporary_initiator_id = None
        conversation.temporary_target_id = None
    await db.commit()
    if conversation is not None:
        await db.refresh(conversation)
    return await get_friend_request_for_user(db, request.id, user_id)


async def reject_friend_request(db: AsyncSession, request_id: int, user_id: int) -> FriendRequest:
    request = await get_friend_request_for_user(db, request_id, user_id)
    if request.receiver_id != user_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only the receiver can reject this request")
    if request.status != FriendRequestStatus.PENDING:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Friend request has already been processed")

    request.status = FriendRequestStatus.REJECTED
    await db.commit()
    return await get_friend_request_for_user(db, request.id, user_id)


async def list_friends(db: AsyncSession, user_id: int) -> list[tuple[User, datetime]]:
    result = await db.execute(
        select(User, Friendship.created_at)
        .join(Friendship, Friendship.friend_id == User.id)
        .where(Friendship.user_id == user_id)
        .order_by(User.username.asc())
    )
    return list(result.all())


async def delete_friend(db: AsyncSession, user_id: int, friend_id: int) -> None:
    if user_id == friend_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid friend id")
    result = await db.execute(select(Friendship.id).where(Friendship.user_id == user_id, Friendship.friend_id == friend_id))
    if result.scalar_one_or_none() is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Friendship not found")

    await db.execute(
        delete(Friendship).where(
            or_(
                and_(Friendship.user_id == user_id, Friendship.friend_id == friend_id),
                and_(Friendship.user_id == friend_id, Friendship.friend_id == user_id),
            )
        )
    )
    await db.commit()


async def list_temporary_blocks(db: AsyncSession, user_id: int) -> list[TemporaryConversationBlock]:
    result = await db.execute(
        select(TemporaryConversationBlock)
        .options(joinedload(TemporaryConversationBlock.blocked_user))
        .where(TemporaryConversationBlock.blocker_id == user_id)
        .order_by(TemporaryConversationBlock.created_at.desc())
    )
    return list(result.scalars().all())


async def delete_temporary_block(db: AsyncSession, user_id: int, block_id: int) -> TemporaryConversationBlock:
    result = await db.execute(
        select(TemporaryConversationBlock)
        .options(joinedload(TemporaryConversationBlock.blocked_user))
        .where(TemporaryConversationBlock.id == block_id, TemporaryConversationBlock.blocker_id == user_id)
    )
    block = result.scalar_one_or_none()
    if block is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Temporary conversation block not found")
    await db.delete(block)
    await db.commit()
    return block
