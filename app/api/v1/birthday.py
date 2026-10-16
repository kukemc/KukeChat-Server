"""生日的接口（docs/chat-features.md 第 8 节），挂在 /users 下，都要求登录，都只动自己的。

别人的生日在公开资料（UserPublic.birthday / is_birthday_today）里，不在这里。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.models.user import User
from app.schemas.gift import BirthdayWrite, MyBirthday
from app.services import birthdays as birthday_service
from app.services import response_cache

router = APIRouter()


@router.get("/me/birthday", response_model=MyBirthday)
async def my_birthday(current_user: Annotated[User, Depends(get_current_user)]) -> MyBirthday:
    return MyBirthday(**birthday_service.view(current_user))


@router.put("/me/birthday", response_model=MyBirthday)
async def set_my_birthday(
    payload: BirthdayWrite,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> MyBirthday:
    """设置或修改，随时可以、不限次数。show_year 不传就保持原样。返回和 GET 同样的形状。"""
    result = await birthday_service.set_birthday(db, current_user, payload.model_dump())
    # 在线名单里缓存着序列化好的用户（带 birthday）
    response_cache.invalidate_online_users()
    return MyBirthday(**result)


@router.delete("/me/birthday", response_model=MyBirthday)
async def clear_my_birthday(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> MyBirthday:
    """清空，随时可以。"""
    result = await birthday_service.clear_birthday(db, current_user)
    response_cache.invalidate_online_users()
    return MyBirthday(**result)


@router.post("/me/birthday/celebrated", response_model=MyBirthday)
async def mark_my_birthday_celebrated(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> MyBirthday:
    """标记今年的庆生动画已经播过（celebrate_today 之后是 false）。"""
    return MyBirthday(**await birthday_service.mark_celebrated(db, current_user))
