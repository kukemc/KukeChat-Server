"""在共创世界上（docs/ccw-social.md）：油猴脚本 KukeChat for CCW 调的接口。

- PUT    /ccw/activity           上报「正在玩 / 正在创作」，同时也是心跳
- DELETE /ccw/activity           离开作品页时清除
- GET    /ccw/activity/friends   我的好友里谁正在玩什么
- GET    /ccw/users/{oid}        CCW 主页上的 KukeChat 名片
- POST   /ccw/users/batch        评论区等批量场景：有没有账号、在不在线、正在玩什么
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status
from fastapi.encoders import jsonable_encoder
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import rate_limit
from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.models.user import User
from app.schemas.ccw_social import (
    CCW_OID,
    ActivityRead,
    ActivityUpdate,
    CcwUserBatchRequest,
    CcwUserBrief,
    CcwUserCard,
    UserActivityRead,
)
from app.services import ccw_activity, ccw_social
from app.websocket.manager import manager

router = APIRouter()

ACTIVITY_EVENT = "ccw.activity.updated"


async def _online(user_ids: list[int]) -> set[int]:
    return await manager.online_user_ids_among(user_ids)


async def _push(db: AsyncSession, user: User, activity: ActivityRead | None) -> None:
    # 推给好友，也推给自己的其他设备（桌面端、安卓上的「我的状态」跟着变）
    recipients = set(await ccw_social.friend_ids(db, user.id)) | {user.id}
    payload = UserActivityRead(user_id=user.id, activity=activity)
    await manager.send_to_users(recipients, ACTIVITY_EVENT, jsonable_encoder(payload))


@router.put("/activity", response_model=ActivityRead | None)
async def put_activity(
    payload: ActivityUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ActivityRead | None:
    # 心跳一分钟一次；留足余量给多开的标签页
    await rate_limit.hit(f"ccw-activity:{current_user.id}", 30, 60)
    if not current_user.share_ccw_activity:
        return None
    activity, push = await ccw_activity.set_activity(current_user.id, payload)
    if push:
        await _push(db, current_user, activity)
    return activity


@router.delete("/activity", status_code=status.HTTP_204_NO_CONTENT)
async def delete_activity(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    if await ccw_activity.clear_activity(current_user.id):
        await _push(db, current_user, None)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/activity/friends", response_model=list[UserActivityRead])
async def friends_activity(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[UserActivityRead]:
    activities = await ccw_social.friend_activities(db, current_user.id)
    return [UserActivityRead(user_id=uid, activity=activity) for uid, activity in activities.items()]


@router.get("/users/{oid}", response_model=CcwUserCard)
async def user_card(
    oid: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CcwUserCard:
    oid = oid.strip().lower()
    if not CCW_OID.match(oid):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid CCW user id")
    await rate_limit.hit(f"ccw-card:{current_user.id}", 120, 60)
    return await ccw_social.get_card(db, current_user, oid, _online)


@router.post("/users/batch", response_model=list[CcwUserBrief])
async def user_batch(
    payload: CcwUserBatchRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[CcwUserBrief]:
    await rate_limit.hit(f"ccw-batch:{current_user.id}", 60, 60)
    return await ccw_social.get_briefs(db, current_user, payload.oids, _online)
