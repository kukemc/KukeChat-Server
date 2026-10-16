"""发版服务（docs/releases.md）。

公开接口挂在 /releases 下：不需要登录（检查更新在登录前也要能用），但和其它 /api/v1 一样要带客户端证明头。
后台接口挂在 /admin/releases 下：管理员登录令牌或发版令牌（X-Kuke-Release-Token）。
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Query, Request, Response, UploadFile, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import get_current_user_from_token
from app.models.enums import PlatformRole
from app.models.user import User
from app.schemas.release import (
    ReleaseAdmin,
    ReleaseAdminPage,
    ReleaseArtifactUpdate,
    ReleaseConfig,
    ReleaseCreate,
    ReleaseExternalCreate,
    ReleaseHistory,
    ReleaseLatest,
    ReleasePublish,
    ReleaseTokenCreate,
    ReleaseTokenCreated,
    ReleaseTokenList,
    ReleaseUpdate,
    ReleaseUploadCreate,
    ReleaseUploadCreated,
)
from app.services import releases as release_service
from app.services.releases import ReleaseActor

public_router = APIRouter()
admin_router = APIRouter()

_optional_bearer = HTTPBearer(auto_error=False)


async def _admin_from_bearer(db: AsyncSession, credentials: HTTPAuthorizationCredentials | None) -> User:
    if credentials is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated", headers={"WWW-Authenticate": "Bearer"})
    user = await get_current_user_from_token(db, credentials.credentials)
    if user.platform_role != PlatformRole.ADMIN:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin privileges required")
    return user


async def get_release_actor(
    db: Annotated[AsyncSession, Depends(get_db)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_optional_bearer)],
    release_token: Annotated[str | None, Header(alias="X-Kuke-Release-Token")] = None,
) -> ReleaseActor:
    """管理员登录令牌或发版令牌，二选一。"""
    if release_token:
        return await release_service.authenticate_token(db, release_token)
    return ReleaseActor(user=await _admin_from_bearer(db, credentials))


async def get_token_admin(
    db: Annotated[AsyncSession, Depends(get_db)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_optional_bearer)],
    release_token: Annotated[str | None, Header(alias="X-Kuke-Release-Token")] = None,
) -> User:
    """令牌管理只认管理员登录令牌：发版令牌不能给自己续命或签新令牌。"""
    if release_token:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail={"code": "token_not_allowed", "message": "发版令牌不能管理令牌，请登录后台操作"})
    return await _admin_from_bearer(db, credentials)


Actor = Annotated[ReleaseActor, Depends(get_release_actor)]
Db = Annotated[AsyncSession, Depends(get_db)]


# ------------------------------------------------------------------ 公开


@public_router.get("/latest", response_model=ReleaseLatest)
async def latest_release(
    request: Request,
    db: Db,
    platform: str = Query(max_length=16),
    channel: str = Query(default="stable", max_length=16),
    current: str | None = Query(default=None, max_length=64),
    arch: str | None = Query(default=None, max_length=16),
) -> ReleaseLatest:
    return ReleaseLatest(**await release_service.check_latest(db, request, platform, channel, current, arch))


@public_router.get("", response_model=ReleaseHistory)
async def release_history(
    request: Request,
    db: Db,
    platform: str = Query(max_length=16),
    channel: str = Query(default="stable", max_length=16),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> ReleaseHistory:
    return ReleaseHistory(**await release_service.history(db, request, platform, channel, limit, offset))


# ------------------------------------------------------------------ 后台：版本


@admin_router.get("/releases/config", response_model=ReleaseConfig)
async def admin_release_config(_actor: Actor) -> ReleaseConfig:
    return ReleaseConfig(**release_service.config_view())


@admin_router.get("/releases/tokens", response_model=ReleaseTokenList)
async def admin_release_tokens(_admin: Annotated[User, Depends(get_token_admin)], db: Db) -> ReleaseTokenList:
    return ReleaseTokenList(**await release_service.list_tokens(db))


@admin_router.post("/releases/tokens", response_model=ReleaseTokenCreated, status_code=201)
async def admin_create_release_token(payload: ReleaseTokenCreate, admin: Annotated[User, Depends(get_token_admin)], db: Db) -> ReleaseTokenCreated:
    return ReleaseTokenCreated(**await release_service.create_token(db, admin, payload.label, payload.expires_in_days))


@admin_router.delete("/releases/tokens/{token_id}", status_code=204)
async def admin_revoke_release_token(token_id: int, admin: Annotated[User, Depends(get_token_admin)], db: Db) -> Response:
    await release_service.revoke_token(db, admin, token_id)
    return Response(status_code=204)


@admin_router.get("/releases", response_model=ReleaseAdminPage)
async def admin_list_releases(
    request: Request,
    _actor: Actor,
    db: Db,
    platform: str | None = Query(default=None, max_length=16),
    channel: str | None = Query(default=None, max_length=16),
    state: Literal["draft", "published"] | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> ReleaseAdminPage:
    return ReleaseAdminPage(**await release_service.admin_list(db, request, platform, channel, state, limit, offset))


@admin_router.post("/releases", response_model=ReleaseAdmin, status_code=201)
async def admin_create_release(payload: ReleaseCreate, request: Request, actor: Actor, db: Db) -> ReleaseAdmin:
    return ReleaseAdmin(**await release_service.create_release(db, request, actor, payload.model_dump()))


@admin_router.get("/releases/{release_id}", response_model=ReleaseAdmin)
async def admin_get_release(release_id: int, request: Request, _actor: Actor, db: Db) -> ReleaseAdmin:
    return ReleaseAdmin(**await release_service.admin_get(db, request, release_id))


@admin_router.patch("/releases/{release_id}", response_model=ReleaseAdmin)
async def admin_update_release(release_id: int, payload: ReleaseUpdate, request: Request, actor: Actor, db: Db) -> ReleaseAdmin:
    return ReleaseAdmin(**await release_service.update_release(db, request, actor, release_id, payload.model_dump(exclude_unset=True)))


@admin_router.delete("/releases/{release_id}", status_code=204)
async def admin_delete_release(release_id: int, actor: Actor, db: Db) -> Response:
    await release_service.delete_release(db, actor, release_id)
    return Response(status_code=204)


@admin_router.post("/releases/{release_id}/publish", response_model=ReleaseAdmin)
async def admin_publish_release(release_id: int, request: Request, actor: Actor, db: Db, payload: ReleasePublish | None = None) -> ReleaseAdmin:
    return ReleaseAdmin(**await release_service.publish_release(db, request, actor, release_id, force=bool(payload and payload.force)))


@admin_router.post("/releases/{release_id}/unpublish", response_model=ReleaseAdmin)
async def admin_unpublish_release(release_id: int, request: Request, actor: Actor, db: Db) -> ReleaseAdmin:
    return ReleaseAdmin(**await release_service.unpublish_release(db, request, actor, release_id))


# ------------------------------------------------------------------ 后台：安装包


@admin_router.post("/releases/{release_id}/artifacts/uploads", response_model=ReleaseUploadCreated, status_code=201)
async def admin_create_artifact_upload(release_id: int, payload: ReleaseUploadCreate, request: Request, actor: Actor, db: Db) -> ReleaseUploadCreated:
    return ReleaseUploadCreated(**await release_service.create_upload(db, request, actor, release_id, payload.model_dump()))


@admin_router.post("/releases/{release_id}/artifacts/{artifact_id}/complete", response_model=ReleaseAdmin)
async def admin_complete_artifact_upload(release_id: int, artifact_id: int, request: Request, actor: Actor, db: Db) -> ReleaseAdmin:
    return ReleaseAdmin(**await release_service.complete_upload(db, request, actor, release_id, artifact_id))


@admin_router.post("/releases/{release_id}/artifacts", response_model=ReleaseAdmin, status_code=201)
async def admin_upload_artifact(
    release_id: int,
    request: Request,
    actor: Actor,
    db: Db,
    file: Annotated[UploadFile, File()],
    sha256: Annotated[str | None, Form()] = None,
    arch: Annotated[str | None, Form()] = None,
    signature: Annotated[str | None, Form()] = None,
) -> ReleaseAdmin:
    return ReleaseAdmin(**await release_service.upload_through_server(db, request, actor, release_id, file, sha256, arch, signature))


@admin_router.post("/releases/{release_id}/artifacts/external", response_model=ReleaseAdmin, status_code=201)
async def admin_add_external_artifact(release_id: int, payload: ReleaseExternalCreate, request: Request, actor: Actor, db: Db) -> ReleaseAdmin:
    return ReleaseAdmin(**await release_service.add_external(db, request, actor, release_id, payload.model_dump()))


@admin_router.patch("/releases/{release_id}/artifacts/{artifact_id}", response_model=ReleaseAdmin)
async def admin_update_artifact(release_id: int, artifact_id: int, payload: ReleaseArtifactUpdate, request: Request, actor: Actor, db: Db) -> ReleaseAdmin:
    return ReleaseAdmin(**await release_service.update_artifact(db, request, actor, release_id, artifact_id, payload.model_dump(exclude_unset=True)))


@admin_router.delete("/releases/{release_id}/artifacts/{artifact_id}", response_model=ReleaseAdmin)
async def admin_delete_artifact(release_id: int, artifact_id: int, request: Request, actor: Actor, db: Db) -> ReleaseAdmin:
    return ReleaseAdmin(**await release_service.delete_artifact(db, request, actor, release_id, artifact_id))
