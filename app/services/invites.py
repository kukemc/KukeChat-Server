from __future__ import annotations

import secrets

from fastapi import HTTPException, status
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.conversation import Conversation, ConversationMember
from app.models.enums import ConversationType, InviteTargetType, MemberRole
from app.models.friend import FriendRequest
from app.models.invite import InviteLink
from app.models.user import User
from app.schemas.invite import InviteAcceptRead, InviteLinkRead, InviteResolveRead
from app.core.config import settings
from app.services import conversations as conversation_service
from app.services import friends as friend_service


def invite_url(token: str) -> str:
    return f"{settings.invite_base_url.rstrip('/')}?invite={token}"


async def _new_token(db: AsyncSession) -> str:
    while True:
        token = secrets.token_urlsafe(18).replace("-", "").replace("_", "")[:22]
        existing = await db.execute(select(InviteLink.id).where(InviteLink.token == token))
        if existing.scalar_one_or_none() is None:
            return token


async def get_or_create_invite_link(db: AsyncSession, target_type: InviteTargetType, target_id: int, current_user: User) -> InviteLinkRead:
    if target_type == InviteTargetType.GROUP:
        conversation = await db.get(Conversation, target_id)
        if conversation is None or conversation.type != ConversationType.GROUP:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Group not found")
        await conversation_service.require_membership(db, target_id, current_user.id)
    else:
        if current_user.id != target_id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You can only create your own profile invite link")

    result = await db.execute(select(InviteLink).where(InviteLink.target_type == target_type, InviteLink.target_id == target_id))
    link = result.scalar_one_or_none()
    if link is None:
        link = InviteLink(target_type=target_type, target_id=target_id, token=await _new_token(db), created_by_id=current_user.id)
        db.add(link)
        await db.commit()
        await db.refresh(link)
    return InviteLinkRead(token=link.token, url=invite_url(link.token), target_type=link.target_type, target_id=link.target_id, created_at=link.created_at)


async def _get_invite_link(db: AsyncSession, token: str) -> InviteLink:
    result = await db.execute(select(InviteLink).where(InviteLink.token == token))
    link = result.scalar_one_or_none()
    if link is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invite link not found")
    return link


async def resolve_invite(db: AsyncSession, token: str, current_user: User) -> InviteResolveRead:
    link = await _get_invite_link(db, token)
    if link.target_type == InviteTargetType.GROUP:
        conversation = await db.get(Conversation, link.target_id)
        if conversation is None or conversation.type != ConversationType.GROUP:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Group not found")
        group = await conversation_service._build_group_discovery_read(db, conversation, current_user.id)
        return InviteResolveRead(token=link.token, target_type=link.target_type, target_id=link.target_id, group=group, already_joined=group.joined)

    user = await db.get(User, link.target_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    return InviteResolveRead(
        token=link.token,
        target_type=link.target_type,
        target_id=link.target_id,
        user=user,
        already_friends=await friend_service.are_friends(db, current_user.id, user.id),
    )


async def accept_invite(db: AsyncSession, token: str, current_user: User) -> InviteAcceptRead:
    link = await _get_invite_link(db, token)
    if link.target_type == InviteTargetType.GROUP:
        conversation = await db.get(Conversation, link.target_id)
        if conversation is None or conversation.type != ConversationType.GROUP:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Group not found")
        member_result = await db.execute(select(ConversationMember.id).where(ConversationMember.conversation_id == conversation.id, ConversationMember.user_id == current_user.id))
        if member_result.scalar_one_or_none() is not None:
            return InviteAcceptRead(target_type=link.target_type, target_id=link.target_id, conversation_id=conversation.id, status="joined", joined_now=False)
        from app.services.conversations import ensure_capacity

        await ensure_capacity(db, conversation, 1)
        db.add(ConversationMember(conversation_id=conversation.id, user_id=current_user.id, role=MemberRole.MEMBER))
        await db.commit()
        return InviteAcceptRead(target_type=link.target_type, target_id=link.target_id, conversation_id=conversation.id, status="joined", joined_now=True)

    if link.target_id == current_user.id:
        return InviteAcceptRead(target_type=link.target_type, target_id=link.target_id, status="self")
    request = await friend_service.create_friend_request(db, current_user, link.target_id)
    return InviteAcceptRead(target_type=link.target_type, target_id=link.target_id, friend_request_id=request.id, status="pending")
