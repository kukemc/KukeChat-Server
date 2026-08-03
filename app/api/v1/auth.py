from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.config import settings
from app.core.dependencies import get_current_user
from app.models.user import User
from app.schemas.auth import AccountLookupRead, CcwPasswordChallengeCreate, CcwPasswordChallengeRead, CcwPasswordChangeConfirm, CcwPasswordResetConfirm, CookieSessionRead, IpLoginStatus, PasswordResetRequestCreate, PasswordResetRequestRead, Token, UserLogin, UserRegister
from app.schemas.user import UserRead
from app.services.auth import CCW_PASSWORD_CHALLENGE_MINUTES, confirm_ccw_password_challenge, cookie_login_user, create_ccw_password_challenge, create_password_reset_request, ensure_aware_utc, get_cookie_session_user, get_user_by_username_or_email, login_user, lookup_account, register_user, revoke_cookie_session
from app.websocket.manager import admin_manager

UTC = timezone.utc


router = APIRouter()


def get_client_ip(request: Request) -> str:
    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for:
        first_ip = forwarded_for.split(",", 1)[0].strip()
        if first_ip:
            return first_ip
    real_ip = request.headers.get("x-real-ip")
    if real_ip:
        return real_ip.strip()
    if request.client is not None:
        return request.client.host
    return "unknown"


@router.post("/register", response_model=Token, status_code=201)
async def register(payload: UserRegister, request: Request, db: Annotated[AsyncSession, Depends(get_db)]) -> Token:
    return await register_user(db, payload, get_client_ip(request))


@router.post("/login", response_model=Token)
async def login(payload: UserLogin, request: Request, db: Annotated[AsyncSession, Depends(get_db)]) -> Token:
    return await login_user(db, payload, get_client_ip(request))


def set_session_cookie(response: Response, token: str, max_age: int | None) -> None:
    response.set_cookie(
        settings.cookie_session_name,
        token,
        max_age=max_age,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="none",
        domain=settings.cookie_session_domain,
        path="/",
    )


@router.post("/cookie-login", response_model=CookieSessionRead)
async def cookie_login(payload: UserLogin, request: Request, response: Response, db: Annotated[AsyncSession, Depends(get_db)]) -> CookieSessionRead:
    raw_token, session = await cookie_login_user(db, payload, get_client_ip(request), request.headers.get("user-agent"), settings.cookie_session_days)
    max_age = settings.cookie_session_days * 24 * 60 * 60 if payload.remember_me else None
    set_session_cookie(response, raw_token, max_age)
    return session


@router.get("/session", response_model=CookieSessionRead)
async def session(request: Request, db: Annotated[AsyncSession, Depends(get_db)]) -> CookieSessionRead:
    return await get_cookie_session_user(db, request.cookies.get(settings.cookie_session_name), get_client_ip(request), request.headers.get("user-agent"))


@router.post("/session/logout")
async def session_logout(request: Request, response: Response, db: Annotated[AsyncSession, Depends(get_db)]) -> dict[str, bool]:
    await revoke_cookie_session(db, request.cookies.get(settings.cookie_session_name))
    response.delete_cookie(settings.cookie_session_name, domain=settings.cookie_session_domain, path="/", secure=settings.cookie_secure, samesite="none")
    return {"ok": True}


@router.post("/ip-login", response_model=Token)
async def ip_login(request: Request, db: Annotated[AsyncSession, Depends(get_db)]) -> Token:
    raise HTTPException(status_code=status.HTTP_410_GONE, detail="IP auto-login has been disabled")


@router.post("/password-reset-requests", response_model=PasswordResetRequestRead, status_code=201)
async def password_reset_request(payload: PasswordResetRequestCreate, request: Request, db: Annotated[AsyncSession, Depends(get_db)]):
    item = await create_password_reset_request(db, payload, get_client_ip(request))
    await admin_manager.broadcast("admin.password_reset.changed", {"request_id": item.id, "action": "created"})
    return item


@router.get("/account-lookup", response_model=AccountLookupRead)
async def account_lookup(username: str, db: Annotated[AsyncSession, Depends(get_db)]) -> AccountLookupRead:
    username = username.strip()
    if not username:
        return AccountLookupRead(exists=False, ccw_bound=False)
    return await lookup_account(db, username)


def _challenge_read(challenge) -> CcwPasswordChallengeRead:
    return CcwPasswordChallengeRead(
        code=challenge.code,
        comment_url=f"https://www.ccw.site/detail/{settings.ccw_bind_subject_oid}",
        expires_in=CCW_PASSWORD_CHALLENGE_MINUTES * 60,
    )


@router.post("/password-reset/ccw/challenge", response_model=CcwPasswordChallengeRead, status_code=201)
async def password_reset_ccw_challenge(payload: CcwPasswordChallengeCreate, db: Annotated[AsyncSession, Depends(get_db)]) -> CcwPasswordChallengeRead:
    user = await get_user_by_username_or_email(db, payload.username)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="账号不存在")
    if not user.ccw_student_oid:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="该账号未绑定 CCW，无法使用验证码重置，请使用人工申诉")
    challenge = await create_ccw_password_challenge(db, user, "reset")
    return _challenge_read(challenge)


@router.post("/password-reset/ccw/confirm")
async def password_reset_ccw_confirm(payload: CcwPasswordResetConfirm, db: Annotated[AsyncSession, Depends(get_db)]) -> dict[str, bool]:
    user = await get_user_by_username_or_email(db, payload.username)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="账号不存在")
    await confirm_ccw_password_challenge(db, user, payload.code, "reset", payload.new_password)
    return {"ok": True}


@router.post("/password/change/challenge", response_model=CcwPasswordChallengeRead, status_code=201)
async def password_change_challenge(current_user: Annotated[User, Depends(get_current_user)], db: Annotated[AsyncSession, Depends(get_db)]) -> CcwPasswordChallengeRead:
    if not current_user.ccw_student_oid:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="绑定 CCW 账号后才能使用验证码修改密码")
    challenge = await create_ccw_password_challenge(db, current_user, "change")
    return _challenge_read(challenge)


@router.post("/password/change/confirm")
async def password_change_confirm(payload: CcwPasswordChangeConfirm, current_user: Annotated[User, Depends(get_current_user)], db: Annotated[AsyncSession, Depends(get_db)]) -> dict[str, bool]:
    await confirm_ccw_password_challenge(db, current_user, payload.code, "change", payload.new_password)
    return {"ok": True}


@router.get("/me", response_model=UserRead)
async def me(current_user: Annotated[User, Depends(get_current_user)]) -> User:
    return current_user


@router.get("/ip-login/status", response_model=IpLoginStatus)
async def ip_login_status(current_user: Annotated[User, Depends(get_current_user)], request: Request) -> IpLoginStatus:
    client_ip = get_client_ip(request)
    expires_at = current_user.ip_login_expires_at
    available = False
    if current_user.registration_ip == client_ip and expires_at is not None:
        available = ensure_aware_utc(expires_at) > datetime.now(UTC)
    return IpLoginStatus(
        client_ip=client_ip,
        registration_ip=current_user.registration_ip,
        ip_login_expires_at=expires_at,
        ip_login_available=available,
    )
