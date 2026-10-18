"""设备封禁的后台接口（docs/device-bans.md 第 6 节），挂在 /admin 下，每个接口都要求管理员身份。

隐私边界：这里只给脱敏后的信号（哈希只给前 8 位）和账号之间的关联，不返回任何消息内容。
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import get_current_admin
from app.models.user import User
from app.schemas.device import (
    DeviceBanCreate,
    DeviceBanLift,
    DeviceBanPage,
    DeviceBanRead,
    DeviceBanSettings,
    DeviceBanSettingsWrite,
    PersonBanPreview,
    PersonBanPreviewRequest,
    PersonBanRequest,
    PersonBanResult,
    UserClusterRead,
    UserDevicesRead,
)
from app.services import device_bans as device_ban_service
from app.websocket.manager import admin_manager

router = APIRouter()


@router.get("/users/{user_id}/devices", response_model=UserDevicesRead)
async def admin_user_devices(
    user_id: int,
    _admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> UserDevicesRead:
    """这个账号用过的设备令牌、机器码、浏览器指纹、IP（脱敏），和直接关联的账号（强的在前；只靠 IP 连上的一律是弱）。"""
    return UserDevicesRead(**await device_ban_service.user_devices(db, user_id))


@router.get("/users/{user_id}/cluster", response_model=UserClusterRead)
async def admin_user_cluster(
    user_id: int,
    _admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> UserClusterRead:
    """关联圈：只沿强 / 中关联往外走（最多 3 层、50 个账号）。"""
    return UserClusterRead(**await device_ban_service.user_cluster(db, user_id))


@router.get("/device-bans", response_model=DeviceBanPage)
async def admin_list_device_bans(
    _admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
    status: Annotated[Literal["active", "expired", "lifted", "all"], Query()] = "active",
    kind: Annotated[Literal["device", "machine", "fp", "ip"] | None, Query()] = None,
    q: Annotated[str | None, Query(max_length=100)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> DeviceBanPage:
    """q：账号 id（`#123` / `123`，从它身上封的或封的是它用过的信号）、IP / 网段、哈希前缀。"""
    return DeviceBanPage(**await device_ban_service.list_bans(db, state=status, kind=kind, q=q, limit=limit, offset=offset))


@router.post("/device-bans", response_model=DeviceBanRead, status_code=201)
async def admin_create_device_ban(
    payload: DeviceBanCreate,
    admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> DeviceBanRead:
    item = await device_ban_service.create_ban(db, admin, payload.model_dump())
    await admin_manager.broadcast("admin.device_ban.changed", {"ban_id": item["id"], "action": "created"})
    return DeviceBanRead(**item)


@router.post("/device-bans/person/preview", response_model=PersonBanPreview)
async def admin_preview_person_ban(
    payload: PersonBanPreviewRequest,
    _admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PersonBanPreview:
    """「封禁此人」会影响哪些账号、哪些信号。不写任何东西。"""
    return PersonBanPreview(**await device_ban_service.preview_person(db, payload.model_dump()))


@router.post("/device-bans/person", response_model=PersonBanResult)
async def admin_ban_person(
    payload: PersonBanRequest,
    admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PersonBanResult:
    result = await device_ban_service.ban_person(db, admin, payload.model_dump())
    await admin_manager.broadcast("admin.device_ban.changed", {"user_id": payload.user_id, "action": "person"})
    for user_id in result["banned_user_ids"]:
        await admin_manager.broadcast("admin.user.changed", {"user_id": user_id, "action": "status_updated"})
    return PersonBanResult(**result)


@router.post("/device-bans/{ban_id}/lift", response_model=DeviceBanRead)
async def admin_lift_device_ban(
    ban_id: int,
    payload: DeviceBanLift,
    admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> DeviceBanRead:
    item = await device_ban_service.lift_ban(db, admin, ban_id, payload.note)
    await admin_manager.broadcast("admin.device_ban.changed", {"ban_id": ban_id, "action": "lifted"})
    return DeviceBanRead(**item)


@router.get("/device-bans/settings", response_model=DeviceBanSettings)
async def admin_device_ban_settings(
    _admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> DeviceBanSettings:
    return DeviceBanSettings(**await device_ban_service.get_settings(db, fresh=True))


@router.put("/device-bans/settings", response_model=DeviceBanSettings)
async def admin_update_device_ban_settings(
    payload: DeviceBanSettingsWrite,
    admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> DeviceBanSettings:
    """只改传了的项，改动写审计日志。另一个 worker 进程最多 5 秒后看到新的设置。"""
    return DeviceBanSettings(**await device_ban_service.admin_update_settings(db, admin, payload.model_dump(exclude_unset=True)))
