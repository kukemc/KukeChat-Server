from __future__ import annotations

import base64
import hashlib
from datetime import datetime, timedelta, timezone
from importlib.metadata import PackageNotFoundError, version
from typing import Any

UTC = timezone.utc

import bcrypt
from jose import JWTError, jwt
from passlib.context import CryptContext

from app.core.config import settings


pwd_context = CryptContext(schemes=["bcrypt_sha256"], deprecated="auto")
DIRECT_BCRYPT_PREFIX = "bcrypt_sha256_direct$"


def use_direct_bcrypt() -> bool:
    try:
        return int(version("bcrypt").split(".", 1)[0]) >= 5
    except (PackageNotFoundError, ValueError):
        return False


def password_digest(password: str) -> bytes:
    digest = hashlib.sha256(password.encode("utf-8")).digest()
    return base64.b64encode(digest)


def direct_bcrypt_hash(password: str) -> str:
    hashed = bcrypt.hashpw(password_digest(password), bcrypt.gensalt()).decode("utf-8")
    return f"{DIRECT_BCRYPT_PREFIX}{hashed}"


def direct_bcrypt_verify(plain_password: str, hashed_password: str) -> bool:
    stored_hash = hashed_password.removeprefix(DIRECT_BCRYPT_PREFIX).encode("utf-8")
    return bcrypt.checkpw(password_digest(plain_password), stored_hash)


def hash_password(password: str) -> str:
    if use_direct_bcrypt():
        return direct_bcrypt_hash(password)
    return pwd_context.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    try:
        if hashed_password.startswith(DIRECT_BCRYPT_PREFIX):
            return direct_bcrypt_verify(plain_password, hashed_password)
        return pwd_context.verify(plain_password, hashed_password)
    except ValueError:
        return False


def create_access_token(subject: str | int, expires_delta: timedelta | None = None) -> str:
    expire = datetime.now(UTC) + (expires_delta or timedelta(minutes=settings.access_token_expire_minutes))
    payload: dict[str, Any] = {"sub": str(subject), "exp": expire}
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> dict[str, Any] | None:
    try:
        return jwt.decode(token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm])
    except JWTError:
        return None
