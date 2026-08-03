from __future__ import annotations

import json
from typing import Annotated, Any
from urllib.error import HTTPError, URLError

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.models.user import User
from app.schemas.ccw import CcwBindingChallengeRead, CcwSyncProfilePayload
from app.schemas.user import UserRead
from app.services.ccw import OID_PATTERN, ccw_bind_creation_url, check_binding_challenges_once, create_binding_challenge, fetch_creation_preview, latest_binding_challenge, sync_bound_student, unbind_student


router = APIRouter()


def _challenge_read(challenge) -> CcwBindingChallengeRead:
    return CcwBindingChallengeRead(
        id=challenge.id,
        code=challenge.code,
        status=challenge.status,
        subject_oid=challenge.subject_oid,
        creation_url=ccw_bind_creation_url(),
        expires_at=challenge.expires_at,
        verified_at=challenge.verified_at,
        last_checked_at=challenge.last_checked_at,
        matched_student_oid=challenge.matched_student_oid,
        matched_name=challenge.matched_name,
        matched_avatar_url=challenge.matched_avatar_url,
    )


@router.get("/creations/{oid}")
async def ccw_creation_preview(
    oid: str,
    current_user: Annotated[User, Depends(get_current_user)],
    access_key: Annotated[str, Query(alias="accessKey", max_length=64)] = "",
) -> dict[str, Any]:
    del current_user
    if not OID_PATTERN.fullmatch(oid):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="无效的作品 ID")

    try:
        return await fetch_creation_preview(oid.lower(), access_key)
    except HTTPError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="CCW 作品接口请求失败") from exc
    except (URLError, TimeoutError, ValueError, json.JSONDecodeError, OSError) as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="无法解析 CCW 作品详情") from exc


@router.get("/binding/me", response_model=CcwBindingChallengeRead | None)
async def my_ccw_binding_challenge(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CcwBindingChallengeRead | None:
    challenge = await latest_binding_challenge(db, current_user.id)
    return _challenge_read(challenge) if challenge is not None else None


@router.post("/binding/challenge", response_model=CcwBindingChallengeRead)
async def start_my_ccw_binding(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CcwBindingChallengeRead:
    challenge = await create_binding_challenge(db, current_user)
    return _challenge_read(challenge)


@router.post("/binding/check", response_model=CcwBindingChallengeRead | None)
async def check_my_ccw_binding(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CcwBindingChallengeRead | None:
    try:
        await check_binding_challenges_once(db, current_user.id)
    except (HTTPError, URLError, TimeoutError, ValueError, json.JSONDecodeError, OSError) as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="检查 CCW 评论失败，请稍后重试") from exc
    challenge = await latest_binding_challenge(db, current_user.id)
    return _challenge_read(challenge) if challenge is not None else None


@router.post("/me/sync", response_model=UserRead)
async def sync_my_ccw_profile(
    payload: CcwSyncProfilePayload,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> User:
    return await sync_bound_student(db, current_user, sync_profile=payload.sync_profile)


@router.delete("/me", response_model=UserRead)
async def unbind_my_ccw_profile(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> User:
    return await unbind_student(db, current_user)
