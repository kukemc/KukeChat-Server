"""KukeAuth 的后台接口（docs/kukeauth.md）：OAuth client 管理和账号级封禁，挂在 /admin 下。"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import get_current_admin
from app.models.kukeauth import OAuthClient
from app.models.report import AdminAuditLog
from app.models.user import User
from app.services import kukeauth as service

router = APIRouter()
Admin = Annotated[User, Depends(get_current_admin)]
Db = Annotated[AsyncSession, Depends(get_db)]

_CLIENT_ID_RE = re.compile(r"[a-z0-9][a-z0-9-]{1,62}")


def _check_uri(uri: str) -> None:
    parts = urlsplit(uri)
    loopback = parts.scheme == "http" and parts.hostname in ("127.0.0.1", "::1")
    if parts.fragment or not parts.netloc or not (parts.scheme == "https" or loopback):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"回调地址必须是 https 或本机回环地址：{uri}")


def _check_scopes(scopes: list[str]) -> None:
    unknown = set(scopes) - set(service.SCOPES)
    if unknown:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"未知的 scope：{sorted(unknown)}")


class ClientCreate(BaseModel):
    client_id: str
    name: str = Field(min_length=1, max_length=80)
    icon_url: str | None = Field(default=None, max_length=500)
    redirect_uris: list[str] = Field(min_length=1)
    post_logout_redirect_uris: list[str] = []
    allowed_scopes: list[str] = ["openid", "profile"]
    # 桌面端、纯前端没法保管 secret，只靠 PKCE
    public: bool = False


class ClientUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    icon_url: str | None = Field(default=None, max_length=500)
    redirect_uris: list[str] | None = Field(default=None, min_length=1)
    post_logout_redirect_uris: list[str] | None = None
    allowed_scopes: list[str] | None = None
    disabled: bool | None = None


class AccountBan(BaseModel):
    reason: str = Field(min_length=1, max_length=1000)
    # 空 = 永久
    until: datetime | None = None


def _read(client: OAuthClient) -> dict:
    return {
        "client_id": client.client_id,
        "name": client.name,
        "icon_url": client.icon_url,
        "redirect_uris": service.json_list(client.redirect_uris),
        "post_logout_redirect_uris": service.json_list(client.post_logout_redirect_uris),
        "allowed_scopes": service.json_list(client.allowed_scopes),
        "public": client.secret_hash is None,
        "disabled": client.disabled,
        "created_at": client.created_at,
    }


async def _get(db: AsyncSession, client_id: str) -> OAuthClient:
    client = (await db.execute(select(OAuthClient).where(OAuthClient.client_id == client_id))).scalar_one_or_none()
    if client is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Client not found")
    return client


@router.get("/kukeauth/clients")
async def list_clients(_admin: Admin, db: Db) -> dict:
    rows = (await db.execute(select(OAuthClient).order_by(OAuthClient.id))).scalars().all()
    return {"items": [_read(client) for client in rows]}


@router.post("/kukeauth/clients", status_code=201)
async def create_client(body: ClientCreate, admin: Admin, db: Db) -> dict:
    if _CLIENT_ID_RE.fullmatch(body.client_id) is None:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="client_id 只能用小写字母、数字和连字符")
    for uri in [*body.redirect_uris, *body.post_logout_redirect_uris]:
        _check_uri(uri)
    _check_scopes(body.allowed_scopes)
    if (await db.execute(select(OAuthClient.id).where(OAuthClient.client_id == body.client_id))).first():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="client_id 已存在")
    secret, secret_hash = (None, None) if body.public else service.new_client_secret()
    client = OAuthClient(
        client_id=body.client_id,
        secret_hash=secret_hash,
        name=body.name,
        icon_url=body.icon_url,
        redirect_uris=json.dumps(body.redirect_uris),
        post_logout_redirect_uris=json.dumps(body.post_logout_redirect_uris),
        allowed_scopes=json.dumps(body.allowed_scopes),
    )
    db.add(client)
    db.add(AdminAuditLog(actor_id=admin.id, action="kukeauth.client_create", target_type="oauth_client", details=body.client_id))
    await db.commit()
    await db.refresh(client)
    # secret 只在这里出现一次，库里只有哈希
    return {**_read(client), "client_secret": secret}


@router.patch("/kukeauth/clients/{client_id}")
async def patch_client(client_id: str, body: ClientUpdate, admin: Admin, db: Db) -> dict:
    client = await _get(db, client_id)
    updates = body.model_dump(exclude_unset=True)
    for uri in [*(updates.get("redirect_uris") or []), *(updates.get("post_logout_redirect_uris") or [])]:
        _check_uri(uri)
    if updates.get("allowed_scopes") is not None:
        _check_scopes(updates["allowed_scopes"])
    for field in ("redirect_uris", "post_logout_redirect_uris", "allowed_scopes"):
        if updates.get(field) is not None:
            updates[field] = json.dumps(updates[field])
    for field, value in updates.items():
        if value is not None or field == "icon_url":
            setattr(client, field, value)
    db.add(AdminAuditLog(actor_id=admin.id, action="kukeauth.client_update", target_type="oauth_client", details=client_id))
    await db.commit()
    await db.refresh(client)
    return _read(client)


@router.post("/kukeauth/clients/{client_id}/rotate-secret")
async def rotate_secret(client_id: str, admin: Admin, db: Db) -> dict:
    client = await _get(db, client_id)
    if client.secret_hash is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="公开 client 没有 secret")
    secret, client.secret_hash = service.new_client_secret()
    db.add(AdminAuditLog(actor_id=admin.id, action="kukeauth.client_rotate_secret", target_type="oauth_client", details=client_id))
    await db.commit()
    return {"client_secret": secret}


@router.put("/kukeauth/users/{user_id}/ban")
async def ban_user(user_id: int, body: AccountBan, admin: Admin, db: Db) -> dict:
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    await service.ban_account(db, admin, user, body.until, body.reason)
    return {"ok": True}


@router.delete("/kukeauth/users/{user_id}/ban")
async def unban_user(user_id: int, admin: Admin, db: Db) -> dict:
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    await service.unban_account(db, admin, user)
    return {"ok": True}
