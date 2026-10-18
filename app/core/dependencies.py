from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import bot_rate_limit
from app.core.database import get_db, release_connection
from app.core.device import context_for_request
from app.core.security import decode_access_token
from app.models.enums import PlatformRole
from app.models.user import User
from app.services import device_bans
from app.services.auth import APPEAL_TOKEN_SCOPE, ensure_user_not_disabled, get_user_by_id
from app.services.bots import BotContext, authenticate_bot_key


bearer_scheme = HTTPBearer(auto_error=True)
async def get_current_user_from_token(db: AsyncSession, token: str) -> User:
    credentials_error = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired token",
        headers={"WWW-Authenticate": "Bearer"},
    )
    payload = decode_access_token(token)
    if not payload or not payload.get("sub") or payload.get("scope"):
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
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(bearer_scheme)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> User:
    user = await get_current_user_from_token(db, credentials.credentials)
    # 设备封禁（docs/device-bans.md）：查的是进程内缓存；信号只在内存里记一笔，后台循环合并写库
    await device_bans.guard_request(db, context_for_request(request), user)
    return user


async def get_current_user_then_release(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> User:
    """和 get_current_user 一样认人，认完就结束这个请求的读事务、把数据库连接还回池子。

    给「认完人就去等网络 / 慢 IO」的接口用（上传图片和语音、CCW 作品卡片、B 站链接卡片）：请求的会话在认人时借走了
    一条连接，不还的话会一直占到请求结束，几秒到几十秒；这类请求一多就能把连接池占满。之后要是还查库，会话会再借一条。
    db 和 get_current_user 里的是同一个会话（FastAPI 在一个请求里只解析一次 get_db）。
    """
    await release_connection(db)
    return user


async def get_appeal_user(
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(bearer_scheme)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> User:
    """处罚查看 / 申诉接口专用：正常令牌和封禁期间签发的申诉令牌都接受。"""
    payload = decode_access_token(credentials.credentials)
    if payload and payload.get("sub") and payload.get("scope") == APPEAL_TOKEN_SCOPE:
        try:
            user = await get_user_by_id(db, int(payload["sub"]))
        except (TypeError, ValueError):
            user = None
        if user is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token")
        return user
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
        return await _authenticate_bot_limited(db, key)

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
    return await _authenticate_bot_limited(db, credentials.strip())


async def _authenticate_bot_limited(db: AsyncSession, raw_key: str) -> BotContext:
    # 频率限制和认证失败缓存都在查库之前（app/core/bot_rate_limit.py）
    digest = bot_rate_limit.key_digest(raw_key)
    bot_rate_limit.check(digest)
    try:
        return await authenticate_bot_key(db, raw_key)
    except HTTPException as error:
        bot_rate_limit.remember_failure(digest, error)
        raise
