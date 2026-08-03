from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.models.user import User
from app.schemas.bot import (
    BotCreate,
    BotCreateRead,
    BotDashboardRead,
    BotInstallationCreate,
    BotInstallationRead,
    BotInstallationUpdate,
    BotKeyRead,
    BotRead,
    BotReviewCreate,
    BotReviewLikeToggleRead,
    BotReviewRead,
    BotReviewReplyCreate,
    BotSquareRead,
    BotUpdate,
)
from app.schemas.common import MessageResponse
from app.services import messages as message_service
from app.services import bots as bot_service
from app.websocket.manager import bot_manager, manager

router = APIRouter()


async def serialize_bot(bot) -> BotRead:
    read = BotRead.model_validate(bot)
    read.online = await bot_manager.is_online_global(bot.id)
    return read


async def serialize_bot_with_reviews(db: AsyncSession, bot, user_id: int | None = None) -> BotRead:
    read = await serialize_bot(bot)
    average, count, my_review = await bot_service.bot_review_stats(db, bot.id, user_id)
    read.rating_average = average
    read.review_count = count
    read.install_count = await bot_service.bot_install_count(db, bot.id)
    read.my_review = (await bot_service.build_bot_review_reads(db, [my_review], user_id))[0] if my_review and user_id is not None else None
    return read


@router.post("", response_model=BotCreateRead, status_code=status.HTTP_201_CREATED)
async def create_bot(
    payload: BotCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> BotCreateRead:
    bot, key = await bot_service.create_bot(db, current_user, payload)
    return BotCreateRead(bot=await serialize_bot(bot), key=key)


@router.get("/mine", response_model=list[BotRead])
async def my_bots(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[BotRead]:
    return [await serialize_bot_with_reviews(db, bot, current_user.id) for bot in await bot_service.list_owner_bots(db, current_user.id)]


@router.get("/square", response_model=BotSquareRead)
async def public_bots(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    q: Annotated[str | None, Query(max_length=80)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> BotSquareRead:
    online_bot_ids = await bot_manager.online_bot_ids_global()
    bots, total = await bot_service.list_public_bots(db, q, limit, offset, online_bot_ids)
    items = [await serialize_bot_with_reviews(db, bot, current_user.id) for bot in bots]
    return BotSquareRead(
        items=items,
        total=total,
        limit=limit,
        offset=offset,
        has_more=offset + len(items) < total,
        stats=await bot_service.bot_square_stats(db, online_bot_ids),
    )


@router.get("/conversations/{conversation_id}/bots", response_model=list[BotInstallationRead])
async def conversation_bots(
    conversation_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[BotInstallationRead]:
    return [BotInstallationRead.model_validate(item) for item in await bot_service.list_conversation_bots(db, conversation_id, current_user.id)]


@router.get("/{bot_id}/dashboard", response_model=BotDashboardRead)
async def bot_dashboard(
    bot_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> BotDashboardRead:
    bot = await bot_service.get_bot_for_owner(db, bot_id, current_user.id)
    bot_read = await serialize_bot_with_reviews(db, bot, current_user.id)
    return await bot_service.build_bot_dashboard(db, bot_id, current_user.id, bot_read)


@router.get("/{bot_id}", response_model=BotRead)
async def bot_detail(
    bot_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> BotRead:
    bot = await bot_service.get_bot(db, bot_id)
    if not bot.is_public and bot.owner_id != current_user.id:
        await bot_service.get_bot_for_owner(db, bot_id, current_user.id)
    return await serialize_bot_with_reviews(db, bot, current_user.id)


@router.patch("/{bot_id}", response_model=BotRead)
async def update_bot(
    bot_id: int,
    payload: BotUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> BotRead:
    return await serialize_bot(await bot_service.update_bot(db, bot_id, current_user, payload))


@router.post("/{bot_id}/rotate-key", response_model=BotKeyRead)
async def rotate_bot_key(
    bot_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> BotKeyRead:
    bot, key = await bot_service.rotate_bot_key(db, bot_id, current_user.id)
    return BotKeyRead(bot_id=bot.id, key=key, key_prefix=bot.key_prefix)


@router.delete("/{bot_id}", response_model=MessageResponse)
async def delete_bot(
    bot_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> MessageResponse:
    bot, conversation_ids = await bot_service.delete_bot(db, bot_id, current_user)
    await bot_manager.send_to_bot(bot.id, "bot.deleted", {"bot_id": bot.id, "owner_id": current_user.id})
    for conversation_id in conversation_ids:
        member_ids = await _conversation_member_ids(db, conversation_id)
        if member_ids:
            await manager.send_to_users(member_ids, "conversation.updated", {"conversation_id": conversation_id})
        await bot_manager.send_to_bot(bot.id, "bot.removed", {"bot_id": bot.id, "conversation_id": conversation_id, "removed_by_id": current_user.id})
    return MessageResponse(detail="Bot deleted")


@router.get("/{bot_id}/reviews", response_model=list[BotReviewRead])
async def bot_reviews(
    bot_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[BotReviewRead]:
    return await bot_service.list_bot_reviews(db, bot_id, current_user.id)


@router.put("/{bot_id}/reviews/me", response_model=BotReviewRead)
async def upsert_bot_review(
    bot_id: int,
    payload: BotReviewCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> BotReviewRead:
    review = await bot_service.upsert_bot_review(db, bot_id, current_user, payload)
    return (await bot_service.build_bot_review_reads(db, [review], current_user.id))[0]


@router.post("/{bot_id}/reviews/{review_id}/replies", response_model=BotReviewRead, status_code=status.HTTP_201_CREATED)
async def create_bot_review_reply(
    bot_id: int,
    review_id: int,
    payload: BotReviewReplyCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> BotReviewRead:
    return await bot_service.create_bot_review_reply(db, bot_id, review_id, current_user, payload)


@router.post("/{bot_id}/reviews/{review_id}/like", response_model=BotReviewLikeToggleRead)
async def toggle_bot_review_like(
    bot_id: int,
    review_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> BotReviewLikeToggleRead:
    liked, like_count = await bot_service.toggle_bot_review_like(db, bot_id, review_id, current_user.id)
    return BotReviewLikeToggleRead(liked=liked, like_count=like_count)


@router.post("/{bot_id}/reviews/{review_id}/replies/{reply_id}/like", response_model=BotReviewLikeToggleRead)
async def toggle_bot_review_reply_like(
    bot_id: int,
    review_id: int,
    reply_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> BotReviewLikeToggleRead:
    liked, like_count = await bot_service.toggle_bot_review_reply_like(db, bot_id, review_id, reply_id, current_user.id)
    return BotReviewLikeToggleRead(liked=liked, like_count=like_count)


@router.post("/{bot_id}/install", response_model=BotInstallationRead, status_code=status.HTTP_201_CREATED)
async def install_bot(
    bot_id: int,
    payload: BotInstallationCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> BotInstallationRead:
    installation = await bot_service.install_bot(db, bot_id, current_user, payload)
    member_ids = await _conversation_member_ids(db, installation.conversation_id)
    bot_name = installation.bot.name if installation.bot else f"机器人 {installation.bot_id}"
    await _emit_bot_system_message(
        db,
        installation.conversation_id,
        current_user,
        f"{display_name(current_user, '管理员')} 添加了机器人 {bot_name}",
        member_ids,
        {"system_event": "group.bot.installed", "bot_id": installation.bot_id, "actor_id": current_user.id},
    )
    await manager.send_to_users(member_ids, "conversation.updated", {"conversation_id": installation.conversation_id})
    await bot_manager.send_to_bot(installation.bot_id, "bot.installed", {"bot_id": installation.bot_id, "conversation_id": installation.conversation_id, "installed_by_id": current_user.id})
    return BotInstallationRead.model_validate(installation)


@router.patch("/{bot_id}/installations/{conversation_id}", response_model=BotInstallationRead)
async def update_bot_installation(
    bot_id: int,
    conversation_id: int,
    payload: BotInstallationUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> BotInstallationRead:
    return BotInstallationRead.model_validate(await bot_service.update_installation(db, bot_id, conversation_id, current_user, payload))


@router.delete("/{bot_id}/installations/{conversation_id}", response_model=MessageResponse)
async def remove_bot_installation(
    bot_id: int,
    conversation_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> MessageResponse:
    installation = await bot_service.remove_installation(db, bot_id, conversation_id, current_user)
    member_ids = await _conversation_member_ids(db, conversation_id)
    bot_name = installation.bot.name if installation.bot else f"机器人 {bot_id}"
    await _emit_bot_system_message(
        db,
        conversation_id,
        current_user,
        f"{display_name(current_user, '管理员')} 移出了机器人 {bot_name}",
        member_ids,
        {"system_event": "group.bot.removed", "bot_id": bot_id, "actor_id": current_user.id},
    )
    await manager.send_to_users(member_ids, "conversation.updated", {"conversation_id": conversation_id})
    await bot_manager.send_to_bot(bot_id, "bot.removed", {"bot_id": bot_id, "conversation_id": conversation_id, "removed_by_id": current_user.id})
    return MessageResponse(detail=f"Bot {installation.bot_id} removed")


@router.get("/{bot_id}/installations", response_model=list[BotInstallationRead])
async def bot_installations(
    bot_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[BotInstallationRead]:
    return [BotInstallationRead.model_validate(item) for item in await bot_service.list_bot_installations(db, bot_id, current_user.id)]


async def _conversation_member_ids(db: AsyncSession, conversation_id: int) -> list[int]:
    from app.services.conversations import conversation_member_ids

    return await conversation_member_ids(db, conversation_id)


def display_name(user: User | None, fallback: str) -> str:
    if user is None:
        return fallback
    return user.nickname or user.username or fallback


async def _emit_bot_system_message(
    db: AsyncSession,
    conversation_id: int,
    sender: User,
    content: str,
    user_ids: list[int] | set[int],
    metadata: dict | None = None,
) -> None:
    message = await message_service.create_system_message(db, conversation_id, sender, content, metadata)
    recipients = list(dict.fromkeys(user_ids))
    if recipients:
        from app.api.v1.conversations import send_message_updates

        await send_message_updates(db, "message.created", message, recipients)
