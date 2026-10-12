"""账号等级的后台接口（docs/account-level.md 第 5 节）。

和 admin.py 一样挂在 /admin 下，每个接口都要求管理员身份。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import get_current_admin
from app.models.user import User
from app.schemas.account_level import AdminLevelAdjust, AdminLevelSettings, AdminLevelUser, AdminLevelUserPage
from app.services import account_levels as level_service


router = APIRouter()


@router.get("/levels/users", response_model=AdminLevelUserPage)
async def admin_list_level_users(
    _admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
    # 匹配用户名 / 昵称；纯数字时也匹配用户 id。留空按累计活跃天从高到低列出所有人
    q: str | None = Query(default=None, max_length=80),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> AdminLevelUserPage:
    items, total = await level_service.admin_list_users(db, q=q, limit=limit, offset=offset)
    return AdminLevelUserPage(items=[AdminLevelUser(**item) for item in items], total=total)


@router.post("/levels/users/{user_id}/adjust", response_model=AdminLevelUser)
async def admin_adjust_level(
    user_id: int,
    payload: AdminLevelAdjust,
    admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AdminLevelUser:
    """手动增减活跃天，写审计日志。返回调整之后这个用户的那一行。"""
    return AdminLevelUser(**await level_service.admin_adjust(db, admin, user_id, days=payload.days, note=payload.note))


@router.get("/levels/settings", response_model=AdminLevelSettings)
async def admin_level_settings(
    _admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AdminLevelSettings:
    return AdminLevelSettings(**await level_service.admin_settings(db))
