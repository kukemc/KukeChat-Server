from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.encoders import jsonable_encoder
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.models.bot import Bot
from app.models.user import User
from app.schemas.user import CcwCreatorPage, OnlineCountRead, OnlineUsersRead, UserOnlineStatusRead, UserPresenceUpdate, UserPublic, UserRead, UserUpdate
from app.services import response_cache
from app.services.users import list_ccw_creators, recommended_users, resolve_stale_profile_cover_moderation, search_users, update_presence, update_profile
from app.websocket.manager import bot_manager, manager


router = APIRouter()


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
