"""KukeAuth 的业务逻辑：OAuth 2 / OIDC（授权码 + PKCE）、账号站会话、邮箱验证、账号级封禁。契约见 docs/kukeauth.md。

和聊天登录的边界：
- OAuth 令牌用独立的 ES256 密钥签名，聊天接口只认 HS256，反过来 userinfo 只认 ES256，互相用不了；
- 聊天处罚（user.status）不影响这里，只有 account_banned_* 才拦。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import HTTPException, status
from jose import JWTError, jwt
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.device import DeviceContext
from app.core.security import hash_password
from app.models.kukeauth import AccountSession, EmailVerification, OAuthClient, OAuthCode, OAuthGrant, OAuthRefreshToken, OAuthRequest
from app.models.report import AdminAuditLog
from app.models.user import AuthSession, User
from app.services import ccw as ccw_service
from app.services import device_signals
from app.services.auth import ensure_aware_utc

UTC = timezone.utc

SCOPES = ("openid", "profile", "email", "ccw", "offline_access")
REQUEST_TTL = timedelta(minutes=10)
CODE_TTL = timedelta(seconds=60)
ACCESS_TTL = timedelta(hours=1)
ID_TOKEN_TTL = timedelta(hours=1)
REFRESH_TTL = timedelta(days=30)
EMAIL_CODE_TTL = timedelta(minutes=10)
EMAIL_CODE_ATTEMPTS = 5
ALGORITHM = "ES256"


def _now() -> datetime:
    return datetime.now(UTC)


def hash_secret(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def configured() -> bool:
    return bool(settings.kukeauth_issuer and settings.kukeauth_signing_key)


def issuer() -> str:
    return (settings.kukeauth_issuer or "").rstrip("/")


# ---------------------------------------------------------------- 签名密钥


def _pem(value: str) -> bytes:
    return value.replace("\\n", "\n").strip().encode("utf-8")


def _private_key() -> ec.EllipticCurvePrivateKey:
    key = serialization.load_pem_private_key(_pem(settings.kukeauth_signing_key or ""), password=None)
    if not isinstance(key, ec.EllipticCurvePrivateKey) or key.curve.name != "secp256r1":
        raise RuntimeError("KUKEAUTH_SIGNING_KEY must be a P-256 private key")
    return key


def _jwk(public_key: ec.EllipticCurvePublicKey) -> dict[str, str]:
    numbers = public_key.public_numbers()
    x, y = _b64(numbers.x.to_bytes(32, "big")), _b64(numbers.y.to_bytes(32, "big"))
    # RFC 7638 指纹：成员按字典序、没有空白
    thumbprint = json.dumps({"crv": "P-256", "kty": "EC", "x": x, "y": y}, separators=(",", ":"), sort_keys=True)
    return {"kty": "EC", "crv": "P-256", "use": "sig", "alg": ALGORITHM, "kid": _b64(hashlib.sha256(thumbprint.encode()).digest())[:16], "x": x, "y": y}


def jwks() -> dict[str, list[dict[str, str]]]:
    keys = [_jwk(_private_key().public_key())]
    for pem in settings.kukeauth_retired_public_keys:
        keys.append(_jwk(serialization.load_pem_public_key(_pem(pem))))
    return {"keys": keys}


def _sign(claims: dict[str, Any]) -> str:
    key = _private_key()
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    return jwt.encode(claims, pem, algorithm=ALGORITHM, headers={"kid": _jwk(key.public_key())["kid"]})


def _verify(token: str, *, verify_exp: bool = True) -> dict[str, Any] | None:
    try:
        kid = jwt.get_unverified_header(token).get("kid")
        key = next((item for item in jwks()["keys"] if item["kid"] == kid), None)
        if key is None:
            return None
        return jwt.decode(token, key, algorithms=[ALGORITHM], issuer=issuer(), options={"verify_aud": False, "verify_exp": verify_exp, "verify_at_hash": False})
    except JWTError:
        return None


# ---------------------------------------------------------------- 账号


def account_ban_state(user: User, now: datetime | None = None) -> dict[str, Any] | None:
    """账号级封禁还在生效就返回给页面看的内容。到期不用清理，读的时候比较时间。"""
    if user.account_banned_at is None:
        return None
    if user.account_banned_until is not None and ensure_aware_utc(user.account_banned_until) <= (now or _now()):
        return None
    return {
        "code": "account_banned",
        "message": "账号已被封禁",
        "reason": user.account_ban_reason,
        "banned_until": ensure_aware_utc(user.account_banned_until).isoformat() if user.account_banned_until else None,
    }


def is_verified(user: User) -> bool:
    return user.email_verified_at is not None or bool(user.ccw_student_oid)


def connections(user: User) -> list[dict[str, Any]]:
    """账号绑定页列出的外部账号。以后接 GitHub 就在这里加一项，页面按这份列表画。"""
    bound = bool(user.ccw_student_oid)
    return [
        {
            "provider": "ccw",
            "label": "CCW",
            "connected": bound,
            "available": ccw_service.ccw_binding_configured(),
            "account": {"name": user.ccw_name, "avatar_url": user.ccw_avatar_url, "url": ccw_service.ccw_profile_url(user.ccw_student_oid)} if bound else None,
            # 解绑后账号不能一个验证方式都不剩，否则登录不了其他服务，密码也找不回来
            "can_unbind": bound and user.email_verified_at is not None,
        }
    ]


async def ensure_public_id(db: AsyncSession, user: User) -> str:
    if user.public_id:
        return user.public_id
    user.public_id = f"acc_{secrets.token_urlsafe(18)}"
    await db.commit()
    return user.public_id


async def revoke_user_sessions(db: AsyncSession, user_id: int, *, keep_session_hash: str | None = None) -> None:
    """密码变了或账号被封：账号站会话、refresh token、聊天的 cookie 会话全部作废。聊天的 JWT 是无状态的，到期前收不回来。"""
    query = delete(AccountSession).where(AccountSession.user_id == user_id)
    if keep_session_hash:
        query = query.where(AccountSession.session_hash != keep_session_hash)
    await db.execute(query)
    await db.execute(update(OAuthRefreshToken).where(OAuthRefreshToken.user_id == user_id, OAuthRefreshToken.revoked_at.is_(None)).values(revoked_at=_now()))
    await db.execute(delete(AuthSession).where(AuthSession.user_id == user_id))


async def set_password(db: AsyncSession, user: User, new_password: str, *, keep_session_hash: str | None = None) -> None:
    user.hashed_password = hash_password(new_password)
    await revoke_user_sessions(db, user.id, keep_session_hash=keep_session_hash)
    await db.commit()


async def record_login_signals(db: AsyncSession, user: User, device: DeviceContext | None) -> None:
    """只记信号。不走 device_bans.on_login：聊天被封的人登录账号站，不该把这台设备也一起封了。"""
    if device is not None and not user.is_bot:
        await device_signals.record_now(db, user.id, device)
        await db.commit()


# ---------------------------------------------------------------- 账号站会话


async def create_session(db: AsyncSession, user: User, client_ip: str | None, user_agent: str | None) -> str:
    raw = secrets.token_urlsafe(32)
    db.add(
        AccountSession(
            session_hash=hash_secret(raw),
            user_id=user.id,
            client_ip=client_ip,
            user_agent=user_agent[:500] if user_agent else None,
            expires_at=_now() + timedelta(days=settings.kukeauth_session_days),
        )
    )
    await db.commit()
    return raw


async def session_user(db: AsyncSession, raw: str | None) -> tuple[AccountSession, User] | None:
    """cookie 对应的会话和用户。账号级封禁的人算没登录。"""
    if not raw:
        return None
    session = await db.get(AccountSession, hash_secret(raw))
    if session is None or ensure_aware_utc(session.expires_at) <= _now():
        return None
    user = await db.get(User, session.user_id)
    if user is None or user.is_bot or account_ban_state(user) is not None:
        return None
    return session, user


async def delete_session(db: AsyncSession, raw: str | None) -> None:
    if raw:
        await db.execute(delete(AccountSession).where(AccountSession.session_hash == hash_secret(raw)))
        await db.commit()


# ---------------------------------------------------------------- 邮箱验证


def _email_code_hash(email: str, purpose: str, code: str) -> str:
    return hmac.new(settings.jwt_secret_key.encode("utf-8"), f"kukeauth-email|{purpose}|{email}|{code}".encode("utf-8"), hashlib.sha256).hexdigest()


async def create_email_code(db: AsyncSession, user: User, email: str, purpose: str) -> str:
    await db.execute(
        update(EmailVerification)
        .where(EmailVerification.user_id == user.id, EmailVerification.purpose == purpose, EmailVerification.used_at.is_(None))
        .values(used_at=_now())
    )
    code = f"{secrets.randbelow(1_000_000):06d}"
    db.add(EmailVerification(user_id=user.id, email=email, purpose=purpose, code_hash=_email_code_hash(email, purpose, code), expires_at=_now() + EMAIL_CODE_TTL))
    await db.commit()
    return code


async def consume_email_code(db: AsyncSession, email: str, purpose: str, code: str, user_id: int | None = None) -> EmailVerification:
    query = select(EmailVerification).where(EmailVerification.email == email, EmailVerification.purpose == purpose, EmailVerification.used_at.is_(None))
    if user_id is not None:
        query = query.where(EmailVerification.user_id == user_id)
    record = (await db.execute(query.order_by(EmailVerification.id.desc()).limit(1))).scalar_one_or_none()
    invalid = HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail={"code": "invalid_code", "message": "验证码不对或已过期"})
    if record is None or ensure_aware_utc(record.expires_at) <= _now() or record.attempts >= EMAIL_CODE_ATTEMPTS:
        raise invalid
    if not hmac.compare_digest(record.code_hash, _email_code_hash(email, purpose, code.strip())):
        record.attempts += 1
        await db.commit()
        raise invalid
    record.used_at = _now()
    return record


async def apply_verified_email(db: AsyncSession, user: User, email: str) -> None:
    """验证过邮箱的人优先。别的账号只是未经验证地填了这个邮箱，就清掉它的（它还能用用户名登录）。"""
    holder = (await db.execute(select(User).where(User.email == email, User.id != user.id))).scalar_one_or_none()
    if holder is not None:
        if holder.email_verified_at is not None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={"code": "email_taken", "message": "这个邮箱已经被别的账号验证过了"})
        holder.email = None
        db.add(AdminAuditLog(actor_id=user.id, action="kukeauth.email_reclaimed", target_type="user", target_id=holder.id))
        await db.flush()
    user.email = email
    user.email_verified_at = _now()
    await db.commit()


async def verified_user_by_email(db: AsyncSession, email: str) -> User | None:
    return (await db.execute(select(User).where(User.email == email, User.email_verified_at.is_not(None)))).scalar_one_or_none()


# ---------------------------------------------------------------- client


def json_list(value: str | None) -> list[str]:
    return list(json.loads(value or "[]"))


async def get_client(db: AsyncSession, client_id: str | None) -> OAuthClient | None:
    if not client_id:
        return None
    client = (await db.execute(select(OAuthClient).where(OAuthClient.client_id == client_id))).scalar_one_or_none()
    return None if client is None or client.disabled else client


def redirect_uri_allowed(client: OAuthClient, redirect_uri: str) -> bool:
    registered = json_list(client.redirect_uris)
    if redirect_uri in registered:
        return True
    # 本机回环地址的端口是临时分的（RFC 8252 7.3），只有公开 client 才放宽端口
    if client.secret_hash is not None:
        return False
    try:
        asked = urlsplit(redirect_uri)
        asked_host = asked.hostname
    except ValueError:
        return False
    if asked.scheme != "http" or asked_host not in ("127.0.0.1", "::1"):
        return False
    for item in registered:
        known = urlsplit(item)
        if known.scheme == "http" and known.hostname == asked_host and known.path == asked.path and known.query == asked.query:
            return True
    return False


def client_secret_ok(client: OAuthClient, secret: str | None) -> bool:
    if client.secret_hash is None:
        return True
    return bool(secret) and hmac.compare_digest(client.secret_hash, hash_secret(secret or ""))


def with_query(uri: str, **params: str | None) -> str:
    parts = urlsplit(uri)
    query = parse_qsl(parts.query, keep_blank_values=True) + [(key, value) for key, value in params.items() if value is not None]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def error_redirect(redirect_uri: str, state: str | None, error: str) -> str:
    return with_query(redirect_uri, error=error, state=state, iss=issuer())


# ---------------------------------------------------------------- 授权请求和授权码


def normalize_scope(raw: str | None) -> list[str]:
    seen: list[str] = []
    for item in (raw or "").split():
        if item not in seen:
            seen.append(item)
    return seen


async def create_request(
    db: AsyncSession, client: OAuthClient, redirect_uri: str, scope: list[str], state: str, nonce: str | None, code_challenge: str, force_login: bool
) -> OAuthRequest:
    now = _now()
    await db.execute(delete(OAuthRequest).where(OAuthRequest.expires_at <= now))
    request = OAuthRequest(
        id=secrets.token_urlsafe(24),
        client_id=client.client_id,
        redirect_uri=redirect_uri,
        scope=" ".join(scope),
        state=state,
        nonce=nonce,
        code_challenge=code_challenge,
        force_login=force_login,
        expires_at=now + REQUEST_TTL,
    )
    db.add(request)
    await db.commit()
    return request


async def get_request(db: AsyncSession, request_id: str) -> OAuthRequest | None:
    request = await db.get(OAuthRequest, request_id)
    if request is None or request.consumed_at is not None or ensure_aware_utc(request.expires_at) <= _now():
        return None
    return request


async def get_request_any(db: AsyncSession, request_id: str) -> OAuthRequest:
    """已经用掉的请求也取：consume 之后还要读它的回调地址。commit 后属性过期，这里重新查。"""
    request = await db.get(OAuthRequest, request_id, populate_existing=True)
    assert request is not None
    return request


async def consume_request(db: AsyncSession, request_id: str) -> bool:
    now = _now()
    result = await db.execute(
        update(OAuthRequest)
        .where(OAuthRequest.id == request_id, OAuthRequest.consumed_at.is_(None), OAuthRequest.expires_at > now)
        .values(consumed_at=now)
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    return result.rowcount == 1


async def grant_covers(db: AsyncSession, user_id: int, client_id: str, scope: list[str]) -> bool:
    grant = (await db.execute(select(OAuthGrant).where(OAuthGrant.user_id == user_id, OAuthGrant.client_id == client_id))).scalar_one_or_none()
    return grant is not None and set(scope) <= set(grant.scope.split())


async def save_grant(db: AsyncSession, user_id: int, client_id: str, scope: list[str]) -> None:
    grant = (await db.execute(select(OAuthGrant).where(OAuthGrant.user_id == user_id, OAuthGrant.client_id == client_id))).scalar_one_or_none()
    if grant is not None:
        grant.scope = " ".join(sorted(set(grant.scope.split()) | set(scope)))
        await db.commit()
        return
    db.add(OAuthGrant(user_id=user_id, client_id=client_id, scope=" ".join(sorted(set(scope)))))
    try:
        await db.commit()
    except IntegrityError:
        # 同一个人两个标签页同时点同意
        await db.rollback()


async def list_grants(db: AsyncSession, user_id: int) -> list[tuple[OAuthGrant, OAuthClient | None]]:
    grants = (await db.execute(select(OAuthGrant).where(OAuthGrant.user_id == user_id).order_by(OAuthGrant.id))).scalars().all()
    return [(grant, (await db.execute(select(OAuthClient).where(OAuthClient.client_id == grant.client_id))).scalar_one_or_none()) for grant in grants]


async def revoke_grant(db: AsyncSession, user_id: int, client_id: str) -> None:
    await db.execute(delete(OAuthGrant).where(OAuthGrant.user_id == user_id, OAuthGrant.client_id == client_id))
    await db.execute(
        update(OAuthRefreshToken).where(OAuthRefreshToken.user_id == user_id, OAuthRefreshToken.client_id == client_id, OAuthRefreshToken.revoked_at.is_(None)).values(revoked_at=_now())
    )
    await db.commit()


async def issue_code(db: AsyncSession, request: OAuthRequest, user: User, auth_time: datetime) -> str:
    """建授权码，返回带着它的回调地址。"""
    code = secrets.token_urlsafe(32)
    db.add(
        OAuthCode(
            code_hash=hash_secret(code),
            client_id=request.client_id,
            user_id=user.id,
            redirect_uri=request.redirect_uri,
            scope=request.scope,
            nonce=request.nonce,
            code_challenge=request.code_challenge,
            auth_time=auth_time,
            expires_at=_now() + CODE_TTL,
        )
    )
    await db.commit()
    return with_query(request.redirect_uri, code=code, state=request.state, iss=issuer())


# ---------------------------------------------------------------- 换令牌


class OAuthError(Exception):
    def __init__(self, error: str, description: str | None = None, status_code: int = 400) -> None:
        super().__init__(error)
        self.error = error
        self.description = description
        self.status_code = status_code


def pkce_ok(verifier: str | None, challenge: str) -> bool:
    if not verifier or not 43 <= len(verifier) <= 128:
        return False
    return hmac.compare_digest(_b64(hashlib.sha256(verifier.encode("ascii", "ignore")).digest()), challenge)


async def _token_response(db: AsyncSession, user: User, client_id: str, scope: list[str], auth_time: datetime, nonce: str | None, refresh: str | None) -> dict[str, Any]:
    now = _now()
    subject = await ensure_public_id(db, user)
    body: dict[str, Any] = {
        "access_token": _sign(
            {
                "iss": issuer(),
                "sub": subject,
                "aud": client_id,
                "scope": " ".join(scope),
                "token_use": "access",
                "jti": secrets.token_urlsafe(12),
                "iat": int(now.timestamp()),
                "exp": int((now + ACCESS_TTL).timestamp()),
            }
        ),
        "token_type": "Bearer",
        "expires_in": int(ACCESS_TTL.total_seconds()),
        "scope": " ".join(scope),
    }
    if "openid" in scope:
        claims: dict[str, Any] = {
            "iss": issuer(),
            "sub": subject,
            "aud": client_id,
            "token_use": "id",
            "iat": int(now.timestamp()),
            "exp": int((now + ID_TOKEN_TTL).timestamp()),
            "auth_time": int(ensure_aware_utc(auth_time).timestamp()),
        }
        if nonce:
            claims["nonce"] = nonce
        body["id_token"] = _sign(claims)
    if refresh:
        body["refresh_token"] = refresh
    return body


def _new_refresh(family_id: str, code_hash: str | None, client_id: str, user_id: int, scope: list[str], auth_time: datetime) -> tuple[str, OAuthRefreshToken]:
    raw = secrets.token_urlsafe(32)
    return raw, OAuthRefreshToken(
        token_hash=hash_secret(raw),
        family_id=family_id,
        code_hash=code_hash,
        client_id=client_id,
        user_id=user_id,
        scope=" ".join(scope),
        auth_time=auth_time,
        expires_at=_now() + REFRESH_TTL,
    )


async def _active_user(db: AsyncSession, user_id: int) -> User:
    user = await db.get(User, user_id)
    if user is None or user.is_bot or account_ban_state(user) is not None:
        raise OAuthError("invalid_grant", "account unavailable")
    return user


async def exchange_code(db: AsyncSession, client: OAuthClient, code: str | None, redirect_uri: str | None, verifier: str | None) -> dict[str, Any]:
    code_hash = hash_secret(code or "")
    now = _now()
    # 先原子地用掉再校验：并发换同一个码只有一个能过，校验失败的码也作废
    result = await db.execute(
        update(OAuthCode)
        .where(OAuthCode.code_hash == code_hash, OAuthCode.used_at.is_(None), OAuthCode.expires_at > now)
        .values(used_at=now)
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    record = await db.get(OAuthCode, code_hash, populate_existing=True)
    if result.rowcount != 1:
        if record is not None and record.used_at is not None and record.client_id == client.client_id:
            # 重放：这个码可能泄露了，吊销用它换出来的令牌
            await db.execute(update(OAuthRefreshToken).where(OAuthRefreshToken.code_hash == code_hash, OAuthRefreshToken.revoked_at.is_(None)).values(revoked_at=now))
            await db.commit()
        raise OAuthError("invalid_grant", "code invalid or expired")
    if record is None or record.client_id != client.client_id or record.redirect_uri != redirect_uri or not pkce_ok(verifier, record.code_challenge):
        raise OAuthError("invalid_grant", "code does not match this request")
    user = await _active_user(db, record.user_id)
    scope = record.scope.split()
    refresh: str | None = None
    if "offline_access" in scope:
        refresh, row = _new_refresh(secrets.token_urlsafe(16), code_hash, client.client_id, user.id, scope, record.auth_time)
        db.add(row)
        await db.commit()
    return await _token_response(db, user, client.client_id, scope, record.auth_time, record.nonce, refresh)


async def refresh_tokens(db: AsyncSession, client: OAuthClient, raw: str | None, requested_scope: str | None) -> dict[str, Any]:
    token_hash = hash_secret(raw or "")
    record = await db.get(OAuthRefreshToken, token_hash)
    now = _now()
    invalid = OAuthError("invalid_grant", "refresh token invalid or expired")
    if record is None or record.client_id != client.client_id:
        raise invalid
    family_id = record.family_id
    if record.replaced_at is not None:
        await db.execute(update(OAuthRefreshToken).where(OAuthRefreshToken.family_id == family_id, OAuthRefreshToken.revoked_at.is_(None)).values(revoked_at=now))
        await db.commit()
        raise invalid
    if record.revoked_at is not None or ensure_aware_utc(record.expires_at) <= now:
        raise invalid
    user = await _active_user(db, record.user_id)
    granted = record.scope.split()
    scope = normalize_scope(requested_scope) or granted
    if not set(scope) <= set(granted):
        raise OAuthError("invalid_scope")
    auth_time, code_hash = record.auth_time, record.code_hash
    result = await db.execute(update(OAuthRefreshToken).where(OAuthRefreshToken.token_hash == token_hash, OAuthRefreshToken.replaced_at.is_(None)).values(replaced_at=now))
    if result.rowcount != 1:
        # 并发刷新，等同于重放
        await db.execute(update(OAuthRefreshToken).where(OAuthRefreshToken.family_id == family_id, OAuthRefreshToken.revoked_at.is_(None)).values(revoked_at=now))
        await db.commit()
        raise invalid
    new_raw, row = _new_refresh(family_id, code_hash, client.client_id, user.id, scope, auth_time)
    db.add(row)
    await db.commit()
    return await _token_response(db, user, client.client_id, scope, auth_time, None, new_raw)


async def revoke_refresh_token(db: AsyncSession, client: OAuthClient, raw: str | None) -> None:
    record = await db.get(OAuthRefreshToken, hash_secret(raw or ""))
    if record is not None and record.client_id == client.client_id:
        await db.execute(update(OAuthRefreshToken).where(OAuthRefreshToken.family_id == record.family_id, OAuthRefreshToken.revoked_at.is_(None)).values(revoked_at=_now()))
        await db.commit()


# ---------------------------------------------------------------- userinfo、登出


def verify_access_token(token: str) -> dict[str, Any] | None:
    claims = _verify(token)
    return claims if claims and claims.get("token_use") == "access" and claims.get("sub") else None


def verify_id_token_hint(token: str) -> dict[str, Any] | None:
    """登出用：过期的 id_token 也认，只看是不是我们签的。"""
    claims = _verify(token, verify_exp=False)
    return claims if claims and claims.get("token_use") == "id" else None


async def user_by_public_id(db: AsyncSession, public_id: str) -> User | None:
    return (await db.execute(select(User).where(User.public_id == public_id))).scalar_one_or_none()


def display_avatar(user: User) -> str | None:
    return user.avatar_url or user.ccw_avatar_url


def userinfo_claims(user: User, scope: list[str]) -> dict[str, Any]:
    claims: dict[str, Any] = {"sub": user.public_id}
    if "profile" in scope:
        claims.update(preferred_username=user.username, name=user.nickname or user.username, picture=display_avatar(user), bio=user.bio)
    if "email" in scope and user.email_verified_at is not None and user.email:
        claims.update(email=user.email, email_verified=True)
    if "ccw" in scope:
        claims["ccw_verified"] = bool(user.ccw_student_oid)
        if user.ccw_student_oid:
            claims.update(ccw_student_oid=user.ccw_student_oid, ccw_name=user.ccw_name)
    return claims


# ---------------------------------------------------------------- 应用（client）管理：后台和开发者共用


def new_client_secret() -> tuple[str, str]:
    raw = f"kcs_{secrets.token_urlsafe(32)}"
    return raw, hash_secret(raw)


def new_developer_client_id() -> str:
    """开发者建的应用用随机 ID，不让人抢占像 gandi-feedback 这样的名字。"""
    return f"app-{secrets.token_hex(8)}"


MAX_DEVELOPER_CLIENTS = 10
MAX_REDIRECT_URIS = 10
# 第三方应用不能叫这些名字，免得在授权页上冒充官方
RESERVED_NAME_WORDS = ("kukeauth", "kukechat", "kuke auth", "kuke chat")


def redirect_uri_problem(uri: str) -> str | None:
    """回调地址不合规时返回原因。只收 https 和本机回环 http（RFC 8252），不收片段。"""
    try:
        parts = urlsplit(uri)
        host = parts.hostname
    except ValueError:
        return f"不是有效的地址：{uri}"
    loopback = parts.scheme == "http" and host in ("127.0.0.1", "::1")
    if parts.fragment or not parts.netloc or not (parts.scheme == "https" or loopback):
        return f"回调地址必须是 https 或本机回环地址（http://127.0.0.1），且不能带 #：{uri}"
    if parts.username or parts.password:
        return f"回调地址不能带用户名和密码：{uri}"
    return None


def https_url_problem(url: str, label: str) -> str | None:
    try:
        parts = urlsplit(url)
    except ValueError:
        return f"{label}不是有效的地址"
    if parts.scheme != "https" or not parts.netloc or parts.username or parts.password:
        return f"{label}必须是 https 地址"
    return None


def scope_problem(scopes: list[str]) -> str | None:
    unknown = sorted(set(scopes) - set(SCOPES))
    if unknown:
        return f"未知的 scope：{', '.join(unknown)}"
    if not scopes:
        return "至少要选一个 scope"
    return None


async def developer_clients(db: AsyncSession, user_id: int) -> list[OAuthClient]:
    return list((await db.execute(select(OAuthClient).where(OAuthClient.owner_id == user_id).order_by(OAuthClient.id))).scalars().all())


async def owned_client(db: AsyncSession, user_id: int, client_id: str) -> OAuthClient | None:
    return (await db.execute(select(OAuthClient).where(OAuthClient.client_id == client_id, OAuthClient.owner_id == user_id))).scalar_one_or_none()


async def delete_client(db: AsyncSession, client: OAuthClient) -> None:
    """删应用：授权记录、授权码、等待中的请求一起删，refresh token 吊销。已签出的 access token 由 userinfo 拦（client 不在了就 401）。"""
    client_id = client.client_id
    await db.execute(delete(OAuthGrant).where(OAuthGrant.client_id == client_id))
    await db.execute(delete(OAuthCode).where(OAuthCode.client_id == client_id))
    await db.execute(delete(OAuthRequest).where(OAuthRequest.client_id == client_id))
    await db.execute(update(OAuthRefreshToken).where(OAuthRefreshToken.client_id == client_id, OAuthRefreshToken.revoked_at.is_(None)).values(revoked_at=_now()))
    await db.delete(client)
    await db.commit()


async def client_developer(db: AsyncSession, client: OAuthClient) -> User | None:
    return await db.get(User, client.owner_id) if client.owner_id is not None else None


# ---------------------------------------------------------------- 后台


async def ban_account(db: AsyncSession, admin: User, user: User, until: datetime | None, reason: str) -> None:
    user.account_banned_at = _now()
    user.account_banned_until = until
    user.account_ban_reason = reason
    await revoke_user_sessions(db, user.id)
    db.add(
        AdminAuditLog(
            actor_id=admin.id,
            action="kukeauth.account_ban",
            target_type="user",
            target_id=user.id,
            details=json.dumps({"until": until.isoformat() if until else None, "reason": reason}, ensure_ascii=False),
        )
    )
    await db.commit()


async def unban_account(db: AsyncSession, admin: User, user: User) -> None:
    user.account_banned_at = None
    user.account_banned_until = None
    user.account_ban_reason = None
    db.add(AdminAuditLog(actor_id=admin.id, action="kukeauth.account_unban", target_type="user", target_id=user.id))
    await db.commit()
