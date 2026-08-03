from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import decode_access_token
from app.models.enums import PlatformRole
from app.models.user import User
from app.services.auth import ensure_user_not_disabled, get_user_by_id
from app.services.bots import BotContext, authenticate_bot_key


bearer_scheme = HTTPBearer(auto_error=True)
async def get_current_user_from_token(db: AsyncSession, token: str) -> User:
    credentials_error = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired token",
        headers={"WWW-Authenticate": "Bearer"},
    )
    payload = decode_access_token(token)
    if not payload or not payload.get("sub"):
        raise credentials_error
    try:
        user_id = int(payload["sub"])
    except (TypeError, ValueError) as exc:
        raise credentials_error from exc

    user = await get_user_by_id(db, user_id)
    if user is None:
        raise credentials_error
    await ensure_user_not_disabled(db, user)
    return user


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(bearer_scheme)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> User:
    return await get_current_user_from_token(db, credentials.credentials)


async def get_current_admin(
    current_user: Annotated[User, Depends(get_current_user)],
) -> User:
    if current_user.platform_role != PlatformRole.ADMIN:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin privileges required")
    return current_user


async def get_current_bot(
    db: Annotated[AsyncSession, Depends(get_db)],
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    x_kuke_bot_key: Annotated[str | None, Header(alias="X-Kuke-Bot-Key")] = None,
) -> BotContext:
    key = (x_kuke_bot_key or "").strip()
    if key:
        return await authenticate_bot_key(db, key)

    scheme = ""
    credentials = ""
    if authorization:
        scheme, _, credentials = authorization.strip().partition(" ")
    if not credentials.strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Bot key is required. Use Authorization: Bot <key> or X-Kuke-Bot-Key.",
            headers={"WWW-Authenticate": "Bot"},
        )
    if scheme.lower() != "bot":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Bot authorization required. Use Authorization: Bot <key>.",
            headers={"WWW-Authenticate": "Bot"},
        )
    return await authenticate_bot_key(db, credentials.strip())
