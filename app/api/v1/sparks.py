"""好友火花（docs/sparks.md）。都要求登录，只能看 / 操作自己和自己好友之间的。"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.models.user import User
from app.schemas.spark import SparkDetail, SparkList, SparkRelightResult, SparkRules
from app.services import sparks as spark_service

router = APIRouter()


@router.get("/sparks/rules", response_model=SparkRules)
async def spark_rules(current_user: Annotated[User, Depends(get_current_user)]) -> SparkRules:
    return SparkRules(**spark_service.rules_view())


@router.get("/sparks", response_model=SparkList)
async def my_sparks(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SparkList:
    return SparkList(**await spark_service.list_mine(db, current_user))


@router.get("/friends/{friend_id}/spark", response_model=SparkDetail)
async def friend_spark(
    friend_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SparkDetail:
    return SparkDetail(**await spark_service.detail(db, current_user, friend_id))


@router.post("/friends/{friend_id}/spark/relight", response_model=SparkRelightResult)
async def relight_spark(
    friend_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SparkRelightResult:
    return SparkRelightResult(**await spark_service.relight(db, current_user, friend_id))
