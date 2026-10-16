"""礼物的后台接口（docs/gifts.md「后台」一节），挂在 /admin/gifts 下，每个接口都要求管理员身份。

隐私边界：后台不能借礼物看私聊内容。私聊里送的（以及从个人空间 / 资料卡送的）礼物，留言在这里一律是 null；这些接口也不返回任何消息内容。
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import get_current_admin
from app.models.user import User
from app.schemas.gift import AdminGiftCatalog, AdminGiftRecordPage, AdminGiftSettings, AdminGiftSettingsWrite, AdminGiftWrite, GiftRead
from app.services import gifts as gift_service

router = APIRouter()


@router.get("/gifts/settings", response_model=AdminGiftSettings)
async def admin_gift_settings(
    _admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AdminGiftSettings:
    return AdminGiftSettings(**await gift_service.admin_settings(db))


@router.put("/gifts/settings", response_model=AdminGiftSettings)
async def admin_update_gift_settings(
    payload: AdminGiftSettingsWrite,
    admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AdminGiftSettings:
    """只改传了的项，改动写审计日志。另一个 worker 进程最多 5 秒后看到新的设置。"""
    return AdminGiftSettings(**await gift_service.admin_update_settings(db, admin, payload.model_dump(exclude_unset=True)))


@router.get("/gifts/catalog", response_model=AdminGiftCatalog)
async def admin_gift_catalog(
    _admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AdminGiftCatalog:
    """全部礼物，含已下架的。"""
    return AdminGiftCatalog(**await gift_service.admin_catalog(db))


@router.post("/gifts/catalog", response_model=GiftRead)
async def admin_create_gift(
    payload: AdminGiftWrite,
    admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> GiftRead:
    return GiftRead(**await gift_service.admin_create_gift(db, admin, payload.model_dump(exclude_unset=True)))


@router.put("/gifts/catalog/{key}", response_model=GiftRead)
async def admin_update_gift(
    key: str,
    payload: AdminGiftWrite,
    admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> GiftRead:
    """只改传了的字段；key 不能改。礼物只能下架（is_active: false），不能删。"""
    return GiftRead(**await gift_service.admin_update_gift(db, admin, key, payload.model_dump(exclude_unset=True)))


@router.get("/gifts/records", response_model=AdminGiftRecordPage)
async def admin_gift_records(
    _admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
    kind: Literal["gift", "item"] | None = None,
    from_user_id: int | None = Query(default=None, ge=1),
    to_user_id: int | None = Query(default=None, ge=1),
    # 送出时间的范围：created_from <= created_at < created_to
    created_from: datetime | None = None,
    created_to: datetime | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> AdminGiftRecordPage:
    return AdminGiftRecordPage(
        **await gift_service.admin_records(
            db, kind=kind, from_user_id=from_user_id, to_user_id=to_user_id, created_from=created_from, created_to=created_to, limit=limit, offset=offset
        )
    )
