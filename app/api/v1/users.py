from __future__ import annotations

import asyncio
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.encoders import jsonable_encoder
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import AsyncSessionLocal, get_db
from app.core.dependencies import get_current_user
from app.models.bot import Bot
from app.models.user import User
from app.schemas.user import (
    CcwCreatorPage,
    OnlineCountRead,
    OnlineUsersRead,
    PokeOptionsRead,
    ProfileLikeOptions,
    ProfileLikeReceivedPage,
    ProfileLikeState,
    UserOnlineStatusRead,
    UserPresenceUpdate,
    UserPublic,
    UserRead,
    UserUpdate,
)
from app.services import pokes as poke_service
from app.services import profile_likes as like_service
from app.services import response_cache
from app.services.users import list_ccw_creators, recommended_users, resolve_stale_profile_cover_moderation, search_users, update_presence, update_profile
from app.websocket.manager import bot_manager, manager


logger = logging.getLogger(__name__)
router = APIRouter()

PROFILE_LIKED_EVENT = "user.profile.liked"
# 被赞者 -> 这一轮里最后一个点赞的人。同一个被赞者在合并窗口（settings.profile_like_event_merge_seconds）里
# 不管收到多少个赞，到点只推一次（每个进程各自合并）
_pending_profile_likes: dict[int, int] = {}
_profile_like_tasks: set[asyncio.Task[None]] = set()


async def _push_profile_liked(target_id: int) -> None:
    from_user_id: int | None = None
    try:
        await asyncio.sleep(settings.profile_like_event_merge_seconds)
    finally:
        # 先摘掉标记再读库：读库之后才到的赞会另排一次，不会丢
        from_user_id = _pending_profile_likes.pop(target_id, None)
    if from_user_id is None:
        return
    try:
        async with AsyncSessionLocal() as db:
            payload = await like_service.liked_event_payload(db, target_id, from_user_id)
        if payload is not None:
            await manager.send_to_user(target_id, PROFILE_LIKED_EVENT, payload)
    except Exception:
        # 推送是尽力而为：赞已经落库，接口也早就返回了，这里只记日志
        logger.exception("Profile liked event for user %s failed", target_id)


def _schedule_profile_liked(target_id: int, from_user_id: int) -> None:
    waiting = target_id in _pending_profile_likes
    # 窗口里后到的赞只把「最后一个点赞人」换掉，不另起任务；数字到点再读，自然是最新的
    _pending_profile_likes[target_id] = from_user_id
    if waiting:
        return
    # 用自己的会话读库，不占用请求的；任务要留一个强引用，否则可能在跑完之前被回收
    task = asyncio.create_task(_push_profile_liked(target_id))
    _profile_like_tasks.add(task)
    task.add_done_callback(_profile_like_tasks.discard)


def cached_json_response(payload: response_cache.CachedJson, request: Request, max_age: int) -> Response:
    headers = {"Cache-Control": f"private, max-age={max_age}", "Vary": "Accept-Encoding"}
    if "gzip" in request.headers.get("accept-encoding", "").lower():
        headers["Content-Encoding"] = "gzip"
        return Response(payload.gzip, media_type="application/json", headers=headers)
    return Response(payload.plain, media_type="application/json", headers=headers)


@router.patch("/me", response_model=UserRead)
async def update_me(
    payload: UserUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> User:
    return await update_profile(db, current_user, payload)


@router.patch("/me/presence", response_model=UserRead)
async def update_my_presence(
    payload: UserPresenceUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> User:
    user = await update_presence(db, current_user, payload)
    response_cache.invalidate_online_users()
    await manager.broadcast(
        "presence.updated",
        {
            "user_id": user.id,
            "online": await manager.is_online_global(user.id),
            "online_count": await manager.online_count_global(),
            "presence_status": user.presence_status,
            "presence_text": user.presence_text,
            "presence_updated_at": user.presence_updated_at,
        },
    )
    return user


@router.get("/poke-options", response_model=PokeOptionsRead)
async def poke_options(current_user: Annotated[User, Depends(get_current_user)]) -> PokeOptionsRead:
    return PokeOptionsRead(**poke_service.options())


# 资料点赞（docs/chat-features.md 第 6 节）。这两条固定路径放在带 {user_id} 的路径前面
@router.get("/likes/options", response_model=ProfileLikeOptions)
async def profile_like_options(current_user: Annotated[User, Depends(get_current_user)]) -> ProfileLikeOptions:
    return ProfileLikeOptions(**like_service.options())


@router.get("/me/likes/received", response_model=ProfileLikeReceivedPage)
async def my_received_profile_likes(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ProfileLikeReceivedPage:
    return ProfileLikeReceivedPage(**await like_service.received(db, current_user, limit, offset))


@router.get("/online-count", response_model=OnlineCountRead)
async def online_count(current_user: Annotated[User, Depends(get_current_user)]) -> OnlineCountRead:
    return OnlineCountRead(online_count=await manager.online_count_global())


@router.get("/online", response_model=OnlineUsersRead)
async def online_users(
    request: Request,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    cached = response_cache.get_online_users()
    if cached is not None:
        return cached_json_response(cached, request, 3)

    online_ids = await manager.online_user_ids_global()
    users: list[User] = []
    if online_ids:
        result = await db.execute(select(User).where(User.id.in_(online_ids)))
        users = sorted(result.scalars().all(), key=lambda user: user.id)
    data = jsonable_encoder(OnlineUsersRead(online_count=len(users), users=users))
    payload = response_cache.build_json(data)
    response_cache.set_online_users(payload)
    return cached_json_response(payload, request, 3)


@router.get("/{user_id}/online", response_model=UserOnlineStatusRead)
async def user_online_status(
    user_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> UserOnlineStatusRead:
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    if user.is_bot:
        bot_result = await db.execute(select(Bot).where(Bot.user_id == user_id))
        bot = bot_result.scalar_one_or_none()
        gateway_online = bool(bot and await bot_manager.is_online_global(bot.id))
        user_online = await manager.is_online_global(user_id)
        online = gateway_online or user_online
        return UserOnlineStatusRead(
            user_id=user_id,
            online=online,
            presence_status="online" if online else "offline",
            presence_text="机器人网关已连接" if gateway_online else ("机器人客户端在线" if user_online else None),
            presence_updated_at=user.presence_updated_at,
        )
    online = await manager.is_online_global(user_id)
    return UserOnlineStatusRead(
        user_id=user_id,
        online=online,
        presence_status=user.presence_status if online else "offline",
        presence_text=user.presence_text if online else None,
        presence_updated_at=user.presence_updated_at,
    )


@router.get("/search", response_model=list[UserPublic])
async def search(
    q: Annotated[str, Query(min_length=1, max_length=100)],
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[User]:
    return await search_users(db, q, current_user.id)


@router.get("/recommended", response_model=list[UserPublic])
async def recommended(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[User]:
    return await recommended_users(db, current_user.id)


@router.get("/ccw-creators", response_model=CcwCreatorPage)
async def ccw_creators(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> CcwCreatorPage:
    items, total = await list_ccw_creators(db, limit, offset)
    return CcwCreatorPage(items=items, total=total, limit=limit, offset=offset, has_more=offset + len(items) < total)


@router.get("/{user_id}/likes", response_model=ProfileLikeState)
async def profile_like_state(
    user_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ProfileLikeState:
    return ProfileLikeState(**await like_service.get_state(db, current_user, user_id))


@router.post("/{user_id}/likes", response_model=ProfileLikeState)
async def like_profile(
    user_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ProfileLikeState:
    liker_id = current_user.id
    state = await like_service.like(db, current_user, user_id)
    # 只推给被赞的人；不写通知中心（太频繁）
    _schedule_profile_liked(user_id, liker_id)
    return ProfileLikeState(**state)


@router.get("/{user_id}", response_model=UserPublic)
async def user_profile(
    user_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> User:
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    return await resolve_stale_profile_cover_moderation(db, user)
