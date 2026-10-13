"""商店的后台接口（docs/shop.md 第 5 节）。

和 admin.py 一样挂在 /admin 下，每个接口都要求管理员身份。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import get_current_admin
from app.models.user import User
from app.schemas.shop import (
    AdminShopAccountPage,
    AdminShopGrant,
    AdminShopItemPage,
    AdminShopItemRead,
    AdminShopItemWrite,
    AdminShopPurchasePage,
    AdminShopPurchaseRead,
    AdminShopReconcileResult,
    AdminShopRevoke,
    AdminShopSettings,
)
from app.services import shop as shop_service

router = APIRouter()


# ---------------------------------------------------------------- 商品


@router.get("/shop/items", response_model=AdminShopItemPage)
async def admin_list_shop_items(
    _admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AdminShopItemPage:
    return AdminShopItemPage(**await shop_service.admin_list_items(db))


@router.post("/shop/items", response_model=AdminShopItemRead)
async def admin_create_shop_item(
    payload: AdminShopItemWrite,
    admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AdminShopItemRead:
    return AdminShopItemRead(**await shop_service.admin_create_item(db, admin, payload.model_dump(exclude_unset=True)))


@router.put("/shop/items/{item_id}", response_model=AdminShopItemRead)
async def admin_update_shop_item(
    item_id: int,
    payload: AdminShopItemWrite,
    admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AdminShopItemRead:
    """只改传了的字段；sku 不能改。"""
    return AdminShopItemRead(**await shop_service.admin_update_item(db, admin, item_id, payload.model_dump(exclude_unset=True)))


# ---------------------------------------------------------------- 记录、账户与对账


@router.get("/shop/purchases", response_model=AdminShopPurchasePage)
async def admin_list_shop_purchases(
    _admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
    user_id: int | None = Query(default=None, ge=1),
    item_id: int | None = Query(default=None, ge=1),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> AdminShopPurchasePage:
    return AdminShopPurchasePage(**await shop_service.admin_list_purchases(db, user_id=user_id, item_id=item_id, limit=limit, offset=offset))


@router.get("/shop/accounts", response_model=AdminShopAccountPage)
async def admin_list_shop_accounts(
    _admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
    q: str | None = Query(default=None, max_length=100),
    # true 只看「花的比投的多」的；不传或 false 是全部
    mismatch: bool | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> AdminShopAccountPage:
    return AdminShopAccountPage(**await shop_service.admin_list_accounts(db, q=q, mismatch=mismatch, limit=limit, offset=offset))


@router.post("/shop/reconcile", response_model=AdminShopReconcileResult)
async def admin_reconcile_shop(
    admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AdminShopReconcileResult:
    return AdminShopReconcileResult(**await shop_service.admin_reconcile(db, admin))


@router.get("/shop/settings", response_model=AdminShopSettings)
async def admin_shop_settings(_admin: Annotated[User, Depends(get_current_admin)]) -> AdminShopSettings:
    return AdminShopSettings(**shop_service.admin_settings())


# ---------------------------------------------------------------- 赠送与收回


@router.post("/shop/users/{user_id}/grant", response_model=AdminShopPurchaseRead)
async def admin_grant_shop_item(
    user_id: int,
    payload: AdminShopGrant,
    admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AdminShopPurchaseRead:
    return AdminShopPurchaseRead(**await shop_service.admin_grant(db, admin, user_id, payload.item_id, payload.note))


@router.post("/shop/users/{user_id}/revoke", response_model=AdminShopPurchaseRead)
async def admin_revoke_shop_item(
    user_id: int,
    payload: AdminShopRevoke,
    admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AdminShopPurchaseRead:
    """收回并卸下。默认不退金币，refund 为 true 时把花掉的这一笔退回去。"""
    return AdminShopPurchaseRead(**await shop_service.admin_revoke(db, admin, user_id, payload.item_id, payload.note, payload.refund))
