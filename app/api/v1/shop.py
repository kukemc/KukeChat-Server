"""商店的用户侧接口（docs/shop.md 第 3 节），挂在 /shop 下，都要求登录。"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.models.user import User
from app.schemas.shop import ShopBalance, ShopEquipRequest, ShopEquipResult, ShopOverview, ShopPurchasePage, ShopPurchaseResult
from app.services import shop as shop_service

router = APIRouter()


@router.get("", response_model=ShopOverview)
async def shop_overview(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ShopOverview:
    """商品、我的余额和装备。只读库，不会向 CCW 发请求；要最新的余额调 /shop/balance/refresh。"""
    return ShopOverview(**await shop_service.overview(db, current_user))


@router.post("/balance/refresh", response_model=ShopBalance)
async def refresh_shop_balance(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ShopBalance:
    return ShopBalance(**await shop_service.refresh_balance(db, current_user))


@router.post("/items/{item_id}/purchase", response_model=ShopPurchaseResult)
async def purchase_shop_item(
    item_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ShopPurchaseResult:
    return ShopPurchaseResult(**await shop_service.purchase(db, current_user, item_id))


@router.put("/equipped", response_model=ShopEquipResult)
async def set_shop_equipped(
    payload: ShopEquipRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ShopEquipResult:
    return ShopEquipResult(**await shop_service.set_equipped(db, current_user, payload.slot, payload.sku))


@router.get("/purchases", response_model=ShopPurchasePage)
async def list_shop_purchases(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> ShopPurchasePage:
    return ShopPurchasePage(**await shop_service.list_purchases(db, current_user.id, limit, offset))
