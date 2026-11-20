"""KukeAuth 的接口：/oauth/*（标准 OAuth 2 / OIDC）和 /account/api/*（账号站页面用）。契约见 docs/kukeauth.md。

不挂在 /api/v1 下：不走客户端证明头和设备令牌中间件，也不和聊天接口共用任何令牌。
只在 issuer 那个域名上响应，账号站的 cookie 因此不会落到聊天 API 的域名上。
"""

from __future__ import annotations

import base64
import json
from typing import Annotated, Any
from urllib.parse import unquote, urlsplit

from anyio import to_thread
from fastapi import APIRouter, Depends, File, HTTPException, Request, Response, UploadFile, status
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.uploads import save_image_upload
from app.core import rate_limit
from app.core.config import settings
from app.core.database import get_db, release_connection
from app.core.device import context_for_request, ip_prefix, normalize_ip
from app.core.security import verify_password
from app.integrations import CcwUnavailable
from app.models.kukeauth import AccountSession, OAuthClient
from app.models.report import AdminAuditLog
from app.models.user import User
from app.schemas.auth import UserRegister
from app.schemas.user import UserUpdate
from app.services import ccw as ccw_service
from app.services import kukeauth as service
from app.services.auth import confirm_ccw_password_challenge, create_ccw_password_challenge, create_user_account, ensure_aware_utc, get_user_by_username_or_email
from app.services.email import send_email_internal
from app.services.users import update_profile


def _cookie_name() -> str:
    # __Host- 前缀要求 Secure，本地 http 开发时用不了
    return "__Host-kuke_auth" if settings.cookie_secure else "kuke_auth"


def _client_ip(request: Request) -> str:
    """只信反代写的 X-Real-IP 和直连地址。X-Forwarded-For 的第一段客户端可以自己填，不能拿来限速和防小号。"""
    real_ip = normalize_ip(request.headers.get("x-real-ip"))
    if real_ip:
        return real_ip
    return request.client.host if request.client else "unknown"


def _device(request: Request):  # noqa: ANN202
    context = context_for_request(request)
    ip = _client_ip(request)
    context.ip = normalize_ip(ip)
    context.prefix = ip_prefix(ip)
    return context


async def require_issuer_host(request: Request) -> None:
    if not service.configured():
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="KukeAuth is not configured")
    if request.headers.get("host", "").lower() != urlsplit(service.issuer()).netloc.lower():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")


async def require_same_origin(request: Request) -> None:
    """账号站的写接口只接受账号站自己的页面。cookie 是 SameSite=Lax，这里再挡一层。"""
    if request.headers.get("origin") != service.issuer():
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail={"code": "bad_origin", "message": "请求来源不对"})


router = APIRouter(dependencies=[Depends(require_issuer_host)], tags=["kukeauth"])
Db = Annotated[AsyncSession, Depends(get_db)]
SameOrigin = Depends(require_same_origin)


async def _current(request: Request, db: Db) -> tuple[AccountSession, User]:
    found = await service.session_user(db, request.cookies.get(_cookie_name()))
    if found is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail={"code": "not_logged_in", "message": "请先登录"})
    return found


Current = Annotated[tuple[AccountSession, User], Depends(_current)]


def _set_cookie(response: Response, raw: str) -> None:
    response.set_cookie(
        _cookie_name(), raw, max_age=settings.kukeauth_session_days * 86400, httponly=True, secure=settings.cookie_secure, samesite="lax", path="/"
    )


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


# ---------------------------------------------------------------- 发现和公钥


@router.get("/.well-known/openid-configuration")
async def discovery() -> dict[str, Any]:
    base = service.issuer()
    return {
        "issuer": base,
        "authorization_endpoint": f"{base}/oauth/authorize",
        "token_endpoint": f"{base}/oauth/token",
        "userinfo_endpoint": f"{base}/oauth/userinfo",
        "revocation_endpoint": f"{base}/oauth/revoke",
        "end_session_endpoint": f"{base}/oauth/logout",
        "jwks_uri": f"{base}/oauth/jwks",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "scopes_supported": list(service.SCOPES),
        "subject_types_supported": ["public"],
        "id_token_signing_alg_values_supported": [service.ALGORITHM],
        "token_endpoint_auth_methods_supported": ["client_secret_basic", "client_secret_post", "none"],
        "authorization_response_iss_parameter_supported": True,
    }


@router.get("/oauth/jwks")
async def jwks() -> dict[str, Any]:
    return service.jwks()


# ---------------------------------------------------------------- 授权


@router.get("/oauth/authorize")
async def authorize(request: Request, db: Db) -> Response:
    params = request.query_params
    client = await service.get_client(db, params.get("client_id"))
    redirect_uri = params.get("redirect_uri") or ""
    # client 和回调地址不对时绝不跳转
    if client is None:
        return JSONResponse({"error": "invalid_client"}, status_code=400)
    if not service.redirect_uri_allowed(client, redirect_uri):
        return JSONResponse({"error": "invalid_redirect_uri"}, status_code=400)

    state = params.get("state")

    def fail(error: str) -> RedirectResponse:
        return RedirectResponse(service.error_redirect(redirect_uri, state, error), status_code=302)

    if params.get("response_type") != "code":
        return fail("unsupported_response_type")
    if not state:
        return fail("invalid_request")
    challenge = params.get("code_challenge") or ""
    if params.get("code_challenge_method") != "S256" or not 43 <= len(challenge) <= 128:
        return fail("invalid_request")
    scope = service.normalize_scope(params.get("scope"))
    if not scope or not set(scope) <= set(service.json_list(client.allowed_scopes)):
        return fail("invalid_scope")

    prompt = params.get("prompt")
    if prompt not in (None, "none", "login", "consent", "select_account"):
        return fail("invalid_request")

    found = await service.session_user(db, request.cookies.get(_cookie_name()))
    ready = found is not None and service.is_verified(found[1]) and await service.grant_covers(db, found[1].id, client.client_id, scope)
    if prompt == "none" and not ready:
        if found is None:
            return fail("login_required")
        return fail("consent_required" if service.is_verified(found[1]) else "interaction_required")

    oauth_request = await service.create_request(db, client, redirect_uri, scope, state, params.get("nonce"), challenge, prompt == "login")
    if ready and prompt in (None, "none"):
        await service.consume_request(db, oauth_request.id)
        session, user = found  # type: ignore[misc]
        return RedirectResponse(await service.issue_code(db, oauth_request, user, session.created_at), status_code=302)
    return RedirectResponse(f"/continue?request={oauth_request.id}", status_code=302)


@router.get("/account/api/requests/{request_id}")
async def read_request(request_id: str, request: Request, db: Db) -> dict[str, Any]:
    item = await service.get_request(db, request_id)
    client = await service.get_client(db, item.client_id) if item else None
    if item is None or client is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"code": "request_expired", "message": "这次登录已过期，请回到原来的页面重新发起"})
    found = await service.session_user(db, request.cookies.get(_cookie_name()))
    fresh = found is not None and (not item.force_login or ensure_aware_utc(found[0].created_at) >= ensure_aware_utc(item.created_at))
    developer = await service.client_developer(db, client)
    return {
        "client": {
            "name": client.name,
            "icon_url": client.icon_url,
            "homepage_url": client.homepage_url,
            # 第三方应用标出开发者，官方应用（后台建的）没有这一项
            "official": client.owner_id is None,
            "developer": {"username": developer.username, "nickname": developer.nickname or developer.username} if developer else None,
        },
        "scopes": item.scope.split(),
        "needs_login": not fresh,
        "needs_verification": fresh and not service.is_verified(found[1]),  # type: ignore[index]
    }


@router.post("/account/api/requests/{request_id}/approve", dependencies=[SameOrigin])
async def approve_request(request_id: str, current: Current, db: Db, response: Response) -> dict[str, str]:
    _no_store(response)
    session, user = current
    item = await service.get_request(db, request_id)
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"code": "request_expired", "message": "这次登录已过期，请回到原来的页面重新发起"})
    if item.force_login and ensure_aware_utc(session.created_at) < ensure_aware_utc(item.created_at):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail={"code": "login_required", "message": "请重新登录"})
    if not service.is_verified(user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail={"code": "verification_required", "message": "请先验证邮箱或绑定 CCW"})
    scope, client_id = item.scope.split(), item.client_id
    if not await service.consume_request(db, request_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"code": "request_expired", "message": "这次登录已过期，请回到原来的页面重新发起"})
    await service.save_grant(db, user.id, client_id, scope)
    item = await service.get_request_any(db, request_id)
    return {"redirect_url": await service.issue_code(db, item, user, session.created_at)}


@router.post("/account/api/requests/{request_id}/deny", dependencies=[SameOrigin])
async def deny_request(request_id: str, db: Db) -> dict[str, str]:
    item = await service.get_request(db, request_id)
    if item is None or not await service.consume_request(db, request_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"code": "request_expired", "message": "这次登录已过期"})
    item = await service.get_request_any(db, request_id)
    return {"redirect_url": service.error_redirect(item.redirect_uri, item.state, "access_denied")}


# ---------------------------------------------------------------- 令牌


def _oauth_error(error: service.OAuthError) -> JSONResponse:
    body = {"error": error.error}
    if error.description:
        body["error_description"] = error.description
    headers = {"Cache-Control": "no-store"}
    if error.status_code == 401:
        headers["WWW-Authenticate"] = 'Basic realm="oauth"'
    return JSONResponse(body, status_code=error.status_code, headers=headers)


async def _authenticate_client(request: Request, db: AsyncSession, form) -> Any:  # noqa: ANN001
    client_id, secret = form.get("client_id"), form.get("client_secret")
    header = request.headers.get("authorization", "")
    if header.lower().startswith("basic "):
        try:
            decoded = base64.b64decode(header[6:]).decode("utf-8")
            left, _, right = decoded.partition(":")
            client_id, secret = unquote(left), unquote(right)
        except ValueError as exc:
            raise service.OAuthError("invalid_client", status_code=401) from exc
    client = await service.get_client(db, client_id)
    if client is None or not service.client_secret_ok(client, secret):
        raise service.OAuthError("invalid_client", status_code=401)
    return client


@router.post("/oauth/token")
async def token(request: Request, db: Db) -> Response:
    await rate_limit.hit(f"oauth-token:{_client_ip(request)}", 120, 60)
    form = await request.form()
    try:
        client = await _authenticate_client(request, db, form)
        grant_type = form.get("grant_type")
        if grant_type == "authorization_code":
            body = await service.exchange_code(db, client, form.get("code"), form.get("redirect_uri"), form.get("code_verifier"))
        elif grant_type == "refresh_token":
            body = await service.refresh_tokens(db, client, form.get("refresh_token"), form.get("scope"))
        else:
            raise service.OAuthError("unsupported_grant_type")
    except service.OAuthError as error:
        return _oauth_error(error)
    return JSONResponse(body, headers={"Cache-Control": "no-store", "Pragma": "no-cache"})


@router.post("/oauth/revoke")
async def revoke(request: Request, db: Db) -> Response:
    form = await request.form()
    try:
        client = await _authenticate_client(request, db, form)
    except service.OAuthError as error:
        return _oauth_error(error)
    await service.revoke_refresh_token(db, client, form.get("token"))
    return Response(status_code=200)


@router.post("/oauth/userinfo", include_in_schema=False)
@router.get("/oauth/userinfo")
async def userinfo(request: Request, db: Db) -> Response:
    unauthorized = JSONResponse({"error": "invalid_token"}, status_code=401, headers={"WWW-Authenticate": 'Bearer error="invalid_token"'})
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        return unauthorized
    claims = service.verify_access_token(header[7:].strip())
    if claims is None:
        return unauthorized
    # 应用被删或被停用后，它手里还没过期的 access token 也不再能读资料
    if await service.get_client(db, claims.get("aud")) is None:
        return unauthorized
    user = await service.user_by_public_id(db, claims["sub"])
    if user is None or service.account_ban_state(user) is not None:
        return unauthorized
    return JSONResponse(service.userinfo_claims(user, str(claims.get("scope", "")).split()), headers={"Cache-Control": "no-store"})


@router.get("/oauth/logout")
async def oauth_logout(request: Request, db: Db) -> Response:
    """服务发起的登出。必须带我们签的 id_token_hint，回跳地址必须登记过，否则别人放个链接就能把人踢下线。"""
    params = request.query_params
    claims = service.verify_id_token_hint(params.get("id_token_hint") or "")
    client = await service.get_client(db, claims.get("aud")) if claims else None
    target = params.get("post_logout_redirect_uri")
    if client is None or (target and target not in service.json_list(client.post_logout_redirect_uris)):
        return JSONResponse({"error": "invalid_request"}, status_code=400)
    raw = request.cookies.get(_cookie_name())
    found = await service.session_user(db, raw)
    if found is not None and found[1].public_id == claims["sub"]:
        await service.delete_session(db, raw)
    response = RedirectResponse(service.with_query(target, state=params.get("state")) if target else "/login", status_code=302)
    response.delete_cookie(_cookie_name(), path="/", secure=settings.cookie_secure, samesite="lax", httponly=True)
    return response


# ---------------------------------------------------------------- 注册、登录


class RegisterBody(BaseModel):
    username: str = Field(min_length=3, max_length=50, pattern=r"^[A-Za-z0-9_.-]+$")
    nickname: str | None = Field(default=None, max_length=80)
    password: str = Field(min_length=8, max_length=128)


class LoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=1, max_length=128)


def _me(user: User) -> dict[str, Any]:
    return {
        "id": user.public_id,
        "username": user.username,
        "nickname": user.nickname or user.username,
        "avatar_url": service.display_avatar(user),
        "bio": user.bio,
        "email": user.email if user.email_verified_at else None,
        "ccw": {"name": user.ccw_name, "avatar_url": user.ccw_avatar_url} if user.ccw_student_oid else None,
        "verified": service.is_verified(user),
    }


@router.get("/account/api/device")
async def device(request: Request) -> dict[str, str]:
    """账号站页面拿设备令牌（docs/device-bans.md）。注册时要带上，防小号的设备上限才数得到它。"""
    context = _device(request)
    token_value = request.headers.get("x-kuke-device", "").strip()
    return {"device": token_value if context.has_device else context.ensure_issued()}


@router.post("/account/api/register", status_code=201, dependencies=[SameOrigin])
async def register(body: RegisterBody, request: Request, response: Response, db: Db) -> dict[str, Any]:
    ip = _client_ip(request)
    await rate_limit.hit(f"account-register:{ip}", 20, 3600)
    payload = UserRegister(username=body.username, nickname=body.nickname, password=body.password, email=None)
    # 同一 IP 一个号、设备上限、网段上限都在这里面，和 KukeChat 客户端注册同一套
    user = await create_user_account(db, payload, ip, _device(request), official_group_pending=True)
    await service.ensure_public_id(db, user)
    _set_cookie(response, await service.create_session(db, user, ip, request.headers.get("user-agent")))
    _no_store(response)
    return _me(user)


@router.post("/account/api/login", dependencies=[SameOrigin])
async def login(body: LoginBody, request: Request, response: Response, db: Db) -> dict[str, Any]:
    ip = _client_ip(request)
    await rate_limit.hit(f"account-login-ip:{ip}", 60, 600)
    await rate_limit.hit(f"account-login-user:{body.username.lower()}:{ip}", 10, 600)
    user = await get_user_by_username_or_email(db, body.username)
    if user is None or user.is_bot or not verify_password(body.password, user.hashed_password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail={"code": "bad_credentials", "message": "用户名或密码不对"})
    ban = service.account_ban_state(user)
    if ban is not None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=ban)
    await service.record_login_signals(db, user, _device(request))
    await service.ensure_public_id(db, user)
    _set_cookie(response, await service.create_session(db, user, ip, request.headers.get("user-agent")))
    _no_store(response)
    return _me(user)


@router.post("/account/api/logout", dependencies=[SameOrigin])
async def logout(request: Request, response: Response, db: Db) -> dict[str, bool]:
    await service.delete_session(db, request.cookies.get(_cookie_name()))
    response.delete_cookie(_cookie_name(), path="/", secure=settings.cookie_secure, samesite="lax", httponly=True)
    return {"ok": True}


# ---------------------------------------------------------------- 我的账号


class ProfileBody(BaseModel):
    nickname: str | None = Field(default=None, min_length=1, max_length=80)
    bio: str | None = Field(default=None, max_length=1000)
    avatar_url: str | None = Field(default=None, max_length=500)


class PasswordBody(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=8, max_length=128)


@router.get("/account/api/me")
async def me(current: Current, response: Response) -> dict[str, Any]:
    _no_store(response)
    return _me(current[1])


@router.patch("/account/api/me", dependencies=[SameOrigin])
async def patch_me(body: ProfileBody, current: Current, db: Db) -> dict[str, Any]:
    # 和 KukeChat 客户端改资料走同一个函数，头像地址校验等规则都一样
    user = await update_profile(db, current[1], UserUpdate(**body.model_dump(exclude_unset=True)))
    return _me(user)


@router.post("/account/api/me/avatar", status_code=201, dependencies=[SameOrigin])
async def upload_avatar(request: Request, current: Current, db: Db, file: Annotated[UploadFile, File()]) -> dict[str, Any]:
    await rate_limit.hit(f"account-avatar:{current[1].id}", 20, 3600)
    await release_connection(db)
    uploaded = await save_image_upload(file, "avatars", request)
    return {"url": uploaded.url}


@router.post("/account/api/me/password", dependencies=[SameOrigin])
async def change_password(body: PasswordBody, current: Current, db: Db) -> dict[str, bool]:
    session, user = current
    await rate_limit.hit(f"account-password:{user.id}", 10, 600)
    if not verify_password(body.current_password, user.hashed_password):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail={"code": "bad_password", "message": "当前密码不对"})
    await service.set_password(db, user, body.new_password, keep_session_hash=session.session_hash)
    return {"ok": True}


@router.get("/account/api/me/grants")
async def my_grants(current: Current, db: Db) -> dict[str, Any]:
    rows = await service.list_grants(db, current[1].id)
    return {
        "items": [
            {
                "client_id": grant.client_id,
                "name": client.name if client else grant.client_id,
                "icon_url": client.icon_url if client else None,
                "official": client is not None and client.owner_id is None,
                "scopes": grant.scope.split(),
                "created_at": grant.created_at,
            }
            for grant, client in rows
        ]
    }


@router.delete("/account/api/me/grants/{client_id}", dependencies=[SameOrigin])
async def delete_grant(client_id: str, current: Current, db: Db) -> dict[str, bool]:
    await service.revoke_grant(db, current[1].id, client_id)
    return {"ok": True}


# ---------------------------------------------------------------- 邮箱验证和找回密码


class EmailBody(BaseModel):
    email: EmailStr


class EmailVerifyBody(EmailBody):
    code: str = Field(min_length=6, max_length=6)


class ResetEmailConfirm(EmailVerifyBody):
    new_password: str = Field(min_length=8, max_length=128)


class ResetCcwStart(BaseModel):
    username: str = Field(min_length=1, max_length=255)


class ResetCcwConfirm(ResetCcwStart):
    code: str = Field(min_length=1, max_length=32)
    new_password: str = Field(min_length=8, max_length=128)


async def _send_code(email: str, code: str, title: str) -> bool:
    body = f"你的 KukeAuth {title}验证码是 {code}，10 分钟内有效。\n如果不是你本人操作，请忽略这封邮件。"
    ok, _error = await to_thread.run_sync(send_email_internal, email, f"KukeAuth {title}验证码", body)
    return ok


async def _email_limits(request: Request, email: str) -> None:
    await rate_limit.hit(f"account-email-to:{email}", 1, 60)
    await rate_limit.hit(f"account-email-ip:{_client_ip(request)}", 10, 3600)


@router.post("/account/api/email/send", dependencies=[SameOrigin])
async def email_send(body: EmailBody, request: Request, current: Current, db: Db) -> dict[str, bool]:
    email = str(body.email).lower()
    await _email_limits(request, email)
    code = await service.create_email_code(db, current[1], email, "bind")
    await release_connection(db)
    if not await _send_code(email, code, "邮箱"):
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail={"code": "email_failed", "message": "验证码发送失败，请稍后再试"})
    return {"ok": True}


@router.post("/account/api/email/verify", dependencies=[SameOrigin])
async def email_verify(body: EmailVerifyBody, current: Current, db: Db) -> dict[str, Any]:
    email = str(body.email).lower()
    await service.consume_email_code(db, email, "bind", body.code, current[1].id)
    await service.apply_verified_email(db, current[1], email)
    return _me(current[1])


@router.post("/account/api/password-reset/email/send", dependencies=[SameOrigin])
async def reset_email_send(body: EmailBody, request: Request, db: Db) -> dict[str, bool]:
    """邮箱有没有注册都回一样的结果，不能拿来探测账号。"""
    email = str(body.email).lower()
    await _email_limits(request, email)
    user = await service.verified_user_by_email(db, email)
    if user is not None:
        code = await service.create_email_code(db, user, email, "reset")
        await release_connection(db)
        await _send_code(email, code, "找回密码")
    return {"ok": True}


@router.post("/account/api/password-reset/email/confirm", dependencies=[SameOrigin])
async def reset_email_confirm(body: ResetEmailConfirm, db: Db) -> dict[str, bool]:
    email = str(body.email).lower()
    record = await service.consume_email_code(db, email, "reset", body.code)
    user = await db.get(User, record.user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail={"code": "invalid_code", "message": "验证码不对或已过期"})
    await service.set_password(db, user, body.new_password)
    return {"ok": True}


@router.post("/account/api/password-reset/ccw/challenge", status_code=201, dependencies=[SameOrigin])
async def reset_ccw_challenge(body: ResetCcwStart, request: Request, db: Db) -> dict[str, Any]:
    await rate_limit.hit(f"account-ccw-reset:{_client_ip(request)}", 10, 3600)
    user = await get_user_by_username_or_email(db, body.username)
    if user is None or not user.ccw_student_oid:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail={"code": "ccw_not_bound", "message": "这个账号没有绑定 CCW"})
    challenge = await create_ccw_password_challenge(db, user, "reset")
    return {"code": challenge.code, "comment_url": ccw_service.ccw_bind_creation_url()}


@router.post("/account/api/password-reset/ccw/confirm", dependencies=[SameOrigin])
async def reset_ccw_confirm(body: ResetCcwConfirm, request: Request, db: Db) -> dict[str, bool]:
    await rate_limit.hit(f"account-ccw-confirm:{_client_ip(request)}", 20, 600)
    user = await get_user_by_username_or_email(db, body.username)
    if user is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail={"code": "invalid_code", "message": "验证码不存在或已过期，请重新获取"})
    await confirm_ccw_password_challenge(db, user, body.code, "reset", body.new_password)
    await service.revoke_user_sessions(db, user.id)
    await db.commit()
    return {"ok": True}


# ---------------------------------------------------------------- CCW 绑定（复用 services/ccw.py）


def _challenge(challenge) -> dict[str, Any] | None:  # noqa: ANN001
    if challenge is None:
        return None
    return {"code": challenge.code, "status": challenge.status, "comment_url": ccw_service.ccw_bind_creation_url(), "expires_at": challenge.expires_at, "matched_name": challenge.matched_name}


@router.post("/account/api/ccw/challenge", dependencies=[SameOrigin])
async def ccw_challenge(current: Current, db: Db) -> dict[str, Any] | None:
    return _challenge(await ccw_service.create_binding_challenge(db, current[1]))


@router.post("/account/api/ccw/check", dependencies=[SameOrigin])
async def ccw_check(current: Current, db: Db) -> dict[str, Any]:
    user = current[1]
    await rate_limit.hit(f"account-ccw-check:{user.id}", 30, 600)
    try:
        await ccw_service.check_binding_challenges_once(db, user.id)
    except (*ccw_service.FETCH_ERRORS, CcwUnavailable) as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail={"code": "ccw_failed", "message": "检查 CCW 评论失败，请稍后重试"}) from exc
    await db.refresh(user)
    return {"challenge": _challenge(await ccw_service.latest_binding_challenge(db, user.id)), "me": _me(user)}


# ---------------------------------------------------------------- 账号绑定


class UnbindBody(BaseModel):
    password: str = Field(min_length=1, max_length=128)


@router.get("/account/api/connections")
async def connections(current: Current) -> dict[str, Any]:
    return {"items": service.connections(current[1])}


@router.post("/account/api/connections/{provider}/unbind", dependencies=[SameOrigin])
async def unbind_connection(provider: str, body: UnbindBody, current: Current, db: Db) -> dict[str, Any]:
    user = current[1]
    await rate_limit.hit(f"account-unbind:{user.id}", 10, 600)
    item = next((item for item in service.connections(user) if item["provider"] == provider), None)
    if item is None or not item["connected"]:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"code": "not_connected", "message": "没有绑定这个账号"})
    if not verify_password(body.password, user.hashed_password):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail={"code": "bad_password", "message": "当前密码不对"})
    if not item["can_unbind"]:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={"code": "last_method", "message": "请先验证邮箱，再解绑这个账号"})
    user = await ccw_service.unbind_student(db, user)
    return {"items": service.connections(user), "me": _me(user)}


# ---------------------------------------------------------------- 开发者：自助创建和管理应用
#
# 验证过的账号都能建，每人最多 MAX_DEVELOPER_CLIENTS 个。建出来的是第三方应用（owner_id 有值），
# 授权页会标出开发者；后台随时可以停用（disabled），开发者自己不能重新启用。

RedirectUri = Annotated[str, Field(max_length=500)]


class AppBody(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    homepage_url: str | None = Field(default=None, max_length=500)
    icon_url: str | None = Field(default=None, max_length=500)
    redirect_uris: list[RedirectUri] = Field(min_length=1, max_length=service.MAX_REDIRECT_URIS)
    post_logout_redirect_uris: list[RedirectUri] = Field(default_factory=list, max_length=service.MAX_REDIRECT_URIS)
    allowed_scopes: list[str] = Field(default_factory=lambda: ["openid", "profile"], max_length=len(service.SCOPES))


class AppCreate(AppBody):
    # 桌面端、纯前端没法保管 secret，只靠 PKCE。建好后不能改
    public: bool = False


class AppPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    homepage_url: str | None = Field(default=None, max_length=500)
    icon_url: str | None = Field(default=None, max_length=500)
    redirect_uris: list[RedirectUri] | None = Field(default=None, min_length=1, max_length=service.MAX_REDIRECT_URIS)
    post_logout_redirect_uris: list[RedirectUri] | None = Field(default=None, max_length=service.MAX_REDIRECT_URIS)
    allowed_scopes: list[str] | None = Field(default=None, max_length=len(service.SCOPES))


def _invalid(message: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail={"code": "invalid_app", "message": message})


def _clean_app(values: dict[str, Any]) -> dict[str, Any]:
    """校验并整理开发者填的应用信息，返回可以直接写进 OAuthClient 的字段。"""
    out: dict[str, Any] = {}
    if values.get("name") is not None:
        name = values["name"].strip()
        if not name:
            raise _invalid("应用名称不能为空")
        lowered = name.lower()
        if any(word in lowered for word in service.RESERVED_NAME_WORDS):
            raise _invalid("应用名称不能包含 KukeAuth、KukeChat")
        out["name"] = name
    for field, label in (("homepage_url", "应用主页"), ("icon_url", "图标地址")):
        if field in values:
            url = (values[field] or "").strip() or None
            if url:
                problem = service.https_url_problem(url, label)
                if problem:
                    raise _invalid(problem)
            out[field] = url
    for field in ("redirect_uris", "post_logout_redirect_uris"):
        if values.get(field) is not None:
            uris = list(dict.fromkeys(uri.strip() for uri in values[field] if uri.strip()))
            if field == "redirect_uris" and not uris:
                raise _invalid("至少要填一个回调地址")
            for uri in uris:
                problem = service.redirect_uri_problem(uri)
                if problem:
                    raise _invalid(problem)
            out[field] = json.dumps(uris)
    if values.get("allowed_scopes") is not None:
        scopes = list(dict.fromkeys(values["allowed_scopes"]))
        problem = service.scope_problem(scopes)
        if problem:
            raise _invalid(problem)
        out["allowed_scopes"] = json.dumps(scopes)
    return out


def _app(client: OAuthClient) -> dict[str, Any]:
    return {
        "client_id": client.client_id,
        "name": client.name,
        "homepage_url": client.homepage_url,
        "icon_url": client.icon_url,
        "redirect_uris": service.json_list(client.redirect_uris),
        "post_logout_redirect_uris": service.json_list(client.post_logout_redirect_uris),
        "allowed_scopes": service.json_list(client.allowed_scopes),
        "public": client.secret_hash is None,
        "disabled": client.disabled,
        "created_at": client.created_at,
    }


async def _developer(current: Current) -> User:
    user = current[1]
    if not service.is_verified(user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail={"code": "verification_required", "message": "请先验证邮箱或绑定 CCW，再创建应用"})
    return user


Developer = Annotated[User, Depends(_developer)]


async def _owned(db: AsyncSession, user: User, client_id: str) -> OAuthClient:
    client = await service.owned_client(db, user.id, client_id)
    if client is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"code": "app_not_found", "message": "没有这个应用"})
    return client


def _audit(db: AsyncSession, user: User, action: str, client_id: str) -> None:
    db.add(AdminAuditLog(actor_id=user.id, action=f"kukeauth.developer_{action}", target_type="oauth_client", details=client_id))


@router.get("/account/api/developer/apps")
async def list_apps(current: Current, db: Db, response: Response) -> dict[str, Any]:
    _no_store(response)
    user = current[1]
    items = await service.developer_clients(db, user.id)
    return {"items": [_app(client) for client in items], "limit": service.MAX_DEVELOPER_CLIENTS, "can_create": service.is_verified(user)}


@router.post("/account/api/developer/apps", status_code=201, dependencies=[SameOrigin])
async def create_app(body: AppCreate, user: Developer, db: Db, response: Response) -> dict[str, Any]:
    _no_store(response)
    # 先校验和数个数，填错了不占限速次数；限速只防反复建删
    fields = _clean_app(body.model_dump())
    if len(await service.developer_clients(db, user.id)) >= service.MAX_DEVELOPER_CLIENTS:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={"code": "app_limit", "message": f"每个账号最多创建 {service.MAX_DEVELOPER_CLIENTS} 个应用"})
    await rate_limit.hit(f"account-app-create:{user.id}", 20, 86400)
    secret, secret_hash = (None, None) if body.public else service.new_client_secret()
    client = OAuthClient(client_id=service.new_developer_client_id(), secret_hash=secret_hash, owner_id=user.id, **fields)
    db.add(client)
    _audit(db, user, "create", client.client_id)
    await db.commit()
    await db.refresh(client)
    # secret 只在这里出现一次，库里只有哈希
    return {**_app(client), "client_secret": secret}


@router.patch("/account/api/developer/apps/{client_id}", dependencies=[SameOrigin])
async def update_app(client_id: str, body: AppPatch, user: Developer, db: Db) -> dict[str, Any]:
    client = await _owned(db, user, client_id)
    for field, value in _clean_app(body.model_dump(exclude_unset=True)).items():
        setattr(client, field, value)
    _audit(db, user, "update", client.client_id)
    await db.commit()
    await db.refresh(client)
    return _app(client)


@router.post("/account/api/developer/apps/{client_id}/rotate-secret", dependencies=[SameOrigin])
async def rotate_app_secret(client_id: str, user: Developer, db: Db, response: Response) -> dict[str, Any]:
    _no_store(response)
    await rate_limit.hit(f"account-app-rotate:{user.id}", 10, 3600)
    client = await _owned(db, user, client_id)
    if client.secret_hash is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail={"code": "public_client", "message": "公开应用没有 client secret"})
    secret, client.secret_hash = service.new_client_secret()
    _audit(db, user, "rotate_secret", client.client_id)
    await db.commit()
    return {"client_secret": secret}


@router.delete("/account/api/developer/apps/{client_id}", dependencies=[SameOrigin])
async def delete_app(client_id: str, current: Current, db: Db) -> dict[str, bool]:
    # 删除不要求账号仍是已验证状态：解绑了验证方式的人也要能收拾自己的应用
    user = current[1]
    client = await _owned(db, user, client_id)
    _audit(db, user, "delete", client.client_id)
    await service.delete_client(db, client)
    return {"ok": True}
