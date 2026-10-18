from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.models.user import User
from app.schemas.common import MessageResponse
from app.schemas.sticker import FavoriteStickerCreate, FavoriteStickerIds, FavoriteStickerRead
from app.services import stickers as sticker_service


router = APIRouter()


@router.get("/favorites", response_model=list[FavoriteStickerRead])
async def favorite_stickers(
    request: Request,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[FavoriteStickerRead]:
    items = await sticker_service.list_favorite_stickers(db, current_user.id)
    return [sticker_service.build_favorite_sticker_read(item, request) for item in items]


@router.post("/favorites", response_model=FavoriteStickerRead, status_code=status.HTTP_201_CREATED)
async def add_favorite_sticker(
    payload: FavoriteStickerCreate,
    request: Request,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> FavoriteStickerRead:
    favorite = await sticker_service.add_favorite_sticker(db, current_user.id, payload.source_url)
    return sticker_service.build_favorite_sticker_read(favorite, request)


@router.put("/favorites/order", response_model=list[FavoriteStickerRead])
async def reorder_favorite_stickers(
    payload: FavoriteStickerIds,
    request: Request,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[FavoriteStickerRead]:
    items = await sticker_service.reorder_favorite_stickers(db, current_user.id, payload.asset_ids)
    return [sticker_service.build_favorite_sticker_read(item, request) for item in items]


@router.post("/favorites/batch-delete")
async def remove_favorite_stickers(
    payload: FavoriteStickerIds,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, int]:
    return {"removed": await sticker_service.remove_favorite_stickers(db, current_user.id, payload.asset_ids)}


@router.delete("/favorites/{asset_id}", response_model=MessageResponse)
async def remove_favorite_sticker(
    asset_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> MessageResponse:
    await sticker_service.remove_favorite_sticker(db, current_user.id, asset_id)
    return MessageResponse(detail="Favorite sticker removed")
