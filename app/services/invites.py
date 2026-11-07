from __future__ import annotations

import hashlib
import hmac
import secrets

from fastapi import HTTPException, status
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.conversation import Conversation, ConversationMember
from app.models.enums import ConversationType, InviteTargetType, MemberRole
from app.models.friend import FriendRequest
from app.models.invite import InviteLink
from app.models.user import User
from app.schemas.conversation import GroupJoinRequestCreate
from app.schemas.invite import InviteAcceptRead, InviteAcceptRequest, InviteLinkRead, InviteResolveRead
from app.core.config import settings
from app.services import conversations as conversation_service
from app.services import friends as friend_service
from app.services.conversations import STAFF_ROLES


def invite_url(token: str) -> str:
    return f"{settings.invite_base_url.rstrip('/')}?invite={token}"


# 群邀请链接 = 群的公共 token + 「谁邀请的」：<token>.<邀请人 id>.<签名>
# 每个群只有一行 invite_links（老链接都是这个公共 token），邀请人身份放在签名后缀里，不用改表。
# 邀请人是群主 / 管理员 → 直接进群；普通成员 → 按群的入群方式走（docs/chat-features.md「邀请链接」）。
# 没有后缀的老链接，邀请人按这一行的创建者算。
_SIG_LENGTH = 12


def _signature(base_token: str, inviter_id: int) -> str:
    key = (settings.jwt_secret_key or "kukechat-invite").encode("utf-8")
    digest = hmac.new(key, f"invite:{base_token}:{inviter_id}".encode("utf-8"), hashlib.sha256).hexdigest()
    return digest[:_SIG_LENGTH]


def signed_group_token(base_token: str, inviter_id: int) -> str:
    return f"{base_token}.{inviter_id}.{_signature(base_token, inviter_id)}"


def _split_token(token: str) -> tuple[str, int | None]:
    """返回（公共 token，签名里的邀请人 id）。签名不对的当链接不存在。"""
    parts = token.split(".")
    if len(parts) == 1:
        return token, None
    if len(parts) != 3 or not parts[1].isdigit():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invite link not found")
    base, inviter, signature = parts
    inviter_id = int(inviter)
    if not hmac.compare_digest(signature, _signature(base, inviter_id)):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invite link not found")
    return base, inviter_id


async def _inviter_is_staff(db: AsyncSession, conversation_id: int, inviter_id: int | None) -> bool:
    if inviter_id is None:
        return False
    role = (
        await db.execute(
            select(ConversationMember.role).where(
                ConversationMember.conversation_id == conversation_id, ConversationMember.user_id == inviter_id
            )
        )
    ).scalar_one_or_none()
    return role in STAFF_ROLES


def group_invite_action(conversation: Conversation, inviter_is_staff: bool) -> str:
    """点群邀请的「加入」会怎样：join / request / answer / forbidden（看邀请人此刻的身份，不看发链接时的）。"""
    if inviter_is_staff:
        return "join"
    if conversation.join_mode == "invite_only":
        return "forbidden"
    if conversation.join_mode == "open" or conversation.auto_approve:
        return "join"
    if conversation.join_mode == "question":
        return "answer"
    return "request"


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
        membership = await conversation_service.require_membership(db, target_id, current_user.id)
        if conversation.join_mode == "invite_only" and membership.role not in STAFF_ROLES:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only group owners or admins can invite to an invite-only group")
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
    token = signed_group_token(link.token, current_user.id) if target_type == InviteTargetType.GROUP else link.token
    return InviteLinkRead(token=token, url=invite_url(token), target_type=link.target_type, target_id=link.target_id, created_at=link.created_at)


async def _get_invite_link(db: AsyncSession, token: str) -> tuple[InviteLink, int | None]:
    """返回（链接那一行，邀请人 id）。群链接没有签名后缀时邀请人按创建者算。"""
    base, inviter_id = _split_token(token)
    result = await db.execute(select(InviteLink).where(InviteLink.token == base))
    link = result.scalar_one_or_none()
    if link is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invite link not found")
    if link.target_type != InviteTargetType.GROUP:
        if inviter_id is not None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invite link not found")
        return link, None
    return link, inviter_id if inviter_id is not None else link.created_by_id


async def resolve_invite(db: AsyncSession, token: str, current_user: User) -> InviteResolveRead:
    link, inviter_id = await _get_invite_link(db, token)
    if link.target_type == InviteTargetType.GROUP:
        conversation = await db.get(Conversation, link.target_id)
        if conversation is None or conversation.type != ConversationType.GROUP:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Group not found")
        group = await conversation_service._build_group_discovery_read(db, conversation, current_user.id)
        action = group_invite_action(conversation, await _inviter_is_staff(db, conversation.id, inviter_id))
        return InviteResolveRead(
            token=token,
            target_type=link.target_type,
            target_id=link.target_id,
            group=group,
            already_joined=group.joined,
            join_action=action,
            inviter_id=inviter_id,
        )

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


async def accept_invite(db: AsyncSession, token: str, current_user: User, payload: InviteAcceptRequest | None = None) -> InviteAcceptRead:
    """群邀请：邀请人是群主 / 管理员直接进群；否则按入群方式 —— 要审核的提交入群申请（status=pending），
    要答题的必须带 answer（不带返回 422，和自己申请时一样），仅邀请的群 403。
    提交申请之后的通知由接口层发（和 POST /conversations/{id}/join-requests 同一套）。"""
    link, inviter_id = await _get_invite_link(db, token)
    if link.target_type == InviteTargetType.GROUP:
        conversation = await db.get(Conversation, link.target_id)
        if conversation is None or conversation.type != ConversationType.GROUP:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Group not found")
        member_result = await db.execute(select(ConversationMember.id).where(ConversationMember.conversation_id == conversation.id, ConversationMember.user_id == current_user.id))
        if member_result.scalar_one_or_none() is not None:
            return InviteAcceptRead(target_type=link.target_type, target_id=link.target_id, conversation_id=conversation.id, status="joined", joined_now=False)
        action = group_invite_action(conversation, await _inviter_is_staff(db, conversation.id, inviter_id))
        if action == "forbidden":
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Group is invite only")
        if action in {"request", "answer"}:
            join_request = await conversation_service.create_group_join_request(
                db,
                conversation.id,
                current_user,
                GroupJoinRequestCreate(
                    message=payload.message if payload is not None else None,
                    answer=payload.answer if payload is not None else None,
                ),
            )
            return InviteAcceptRead(
                target_type=link.target_type,
                target_id=link.target_id,
                conversation_id=conversation.id,
                status="pending",
                join_request_id=join_request.id,
            )
        from app.services.conversations import ensure_capacity

        await ensure_capacity(db, conversation, 1)
        db.add(ConversationMember(conversation_id=conversation.id, user_id=current_user.id, role=MemberRole.MEMBER))
        await db.commit()
        return InviteAcceptRead(target_type=link.target_type, target_id=link.target_id, conversation_id=conversation.id, status="joined", joined_now=True)

    if link.target_id == current_user.id:
        return InviteAcceptRead(target_type=link.target_type, target_id=link.target_id, status="self")
    request = await friend_service.create_friend_request(db, current_user, link.target_id)
    return InviteAcceptRead(target_type=link.target_type, target_id=link.target_id, friend_request_id=request.id, status="pending")
