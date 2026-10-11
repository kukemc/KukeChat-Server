from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import get_current_admin, get_current_user
from app.models.enums import ModerationStatus, TeamupProfileStatus, TeamupSkill
from app.models.user import User
from app.schemas.common import MessageResponse
from app.schemas.teamup import (
    TeamupCommentCreate,
    TeamupCommentRead,
    TeamupCommentToggleRead,
    TeamupEventRead,
    TeamupEventUpsert,
    TeamupProfileRead,
    TeamupProfileSave,
    TeamupSkillStat,
    TeamupStatsRead,
)
from app.services import moderation as moderation_service
from app.services import teamup as teamup_service
from app.websocket.manager import manager

router = APIRouter()


async def _broadcast_profile(db: AsyncSession, event_type: str, profile_id: int, actor_id: int) -> None:
    await manager.broadcast(event_type, {"profile_id": profile_id, "actor_id": actor_id})


@router.get("/event/current", response_model=TeamupEventRead | None)
async def current_event(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> TeamupEventRead | None:
    del current_user
    event = await teamup_service.get_active_event(db)
    return await teamup_service.build_event_read(db, event) if event is not None else None


@router.get("/stats", response_model=TeamupStatsRead)
async def stats(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> TeamupStatsRead:
    del current_user
    return await teamup_service.overall_stats(db, await teamup_service.get_active_event_id(db))


@router.get("/skills", response_model=list[TeamupSkillStat])
async def skills(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[TeamupSkillStat]:
    del current_user
    event_id = await teamup_service.get_active_event_id(db)
    return await teamup_service.skill_stats(db, event_id)


@router.get("/profiles", response_model=list[TeamupProfileRead])
async def list_profiles(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    scope: Annotated[Literal["square", "mine"], Query()] = "square",
    skill: Annotated[TeamupSkill | None, Query()] = None,
    status_filter: Annotated[TeamupProfileStatus | None, Query(alias="status")] = None,
    sort: Annotated[Literal["latest", "hot"], Query()] = "latest",
    before_id: Annotated[int | None, Query(gt=0)] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
    search: Annotated[str | None, Query(max_length=80)] = None,
) -> list[TeamupProfileRead]:
    event_id = await teamup_service.get_active_event_id(db)
    profiles = await teamup_service.list_profiles(
        db, current_user.id, scope, event_id, skill, status_filter, sort, before_id, limit, search
    )
    return await teamup_service.build_profile_reads(db, profiles, current_user.id)


@router.get("/profiles/me", response_model=TeamupProfileRead | None)
async def my_profile(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> TeamupProfileRead | None:
    event_id = await teamup_service.get_active_event_id(db)
    profile = await teamup_service.get_my_profile(db, current_user.id, event_id)
    return await teamup_service.build_profile_read(db, profile, current_user.id) if profile is not None else None


@router.put("/profiles/me", response_model=TeamupProfileRead)
async def save_my_profile(
    payload: TeamupProfileSave,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> TeamupProfileRead:
    profile = await teamup_service.upsert_profile(db, current_user, payload)
    read = await teamup_service.build_profile_read(db, profile, current_user.id)
    if profile.moderation_status == ModerationStatus.APPROVED.value:
        await _broadcast_profile(db, "teamup.profile.updated", profile.id, current_user.id)
    else:
        await manager.send_to_user(current_user.id, "teamup.profile.updated", {"profile_id": profile.id, "actor_id": current_user.id, "moderation_status": profile.moderation_status})
        moderation_service.schedule_teamup_profile_moderation(profile.id)
    return read


@router.post("/profiles/me/close", response_model=TeamupProfileRead)
async def close_my_profile(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> TeamupProfileRead:
    event_id = await teamup_service.get_active_event_id(db)
    profile = await teamup_service.set_profile_status(db, current_user.id, event_id, TeamupProfileStatus.CLOSED)
    await _broadcast_profile(db, "teamup.profile.updated", profile.id, current_user.id)
    return await teamup_service.build_profile_read(db, profile, current_user.id)


@router.post("/profiles/me/reopen", response_model=TeamupProfileRead)
async def reopen_my_profile(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> TeamupProfileRead:
    event_id = await teamup_service.get_active_event_id(db)
    profile = await teamup_service.set_profile_status(db, current_user.id, event_id, TeamupProfileStatus.RECRUITING)
    await _broadcast_profile(db, "teamup.profile.updated", profile.id, current_user.id)
    return await teamup_service.build_profile_read(db, profile, current_user.id)


@router.delete("/profiles/me", response_model=MessageResponse)
async def delete_my_profile(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> MessageResponse:
    event_id = await teamup_service.get_active_event_id(db)
    profile = await teamup_service.get_my_profile(db, current_user.id, event_id)
    profile_id = profile.id if profile is not None else None
    await teamup_service.delete_profile(db, current_user.id, event_id)
    if profile_id is not None:
        await _broadcast_profile(db, "teamup.profile.deleted", profile_id, current_user.id)
    return MessageResponse(detail="Profile deleted")


@router.get("/profiles/{profile_id}", response_model=TeamupProfileRead)
async def get_profile(
    profile_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> TeamupProfileRead:
    profile = await teamup_service.get_visible_profile(db, profile_id, current_user.id)
    await teamup_service.increment_view(db, profile, current_user.id)
    return await teamup_service.build_profile_read(db, profile, current_user.id)


@router.get("/profiles/{profile_id}/comments", response_model=list[TeamupCommentRead])
async def list_comments(
    profile_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    before_id: Annotated[int | None, Query(gt=0)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> list[TeamupCommentRead]:
    return await teamup_service.list_comments(db, profile_id, current_user.id, before_id, limit)


@router.post("/profiles/{profile_id}/comments", response_model=TeamupCommentRead, status_code=status.HTTP_201_CREATED)
async def create_comment(
    profile_id: int,
    payload: TeamupCommentCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> TeamupCommentRead:
    comment = await teamup_service.create_comment(db, profile_id, current_user, payload.content, payload.parent_id)
    if comment.moderation_status == ModerationStatus.APPROVED.value:
        await _broadcast_profile(db, "teamup.comment.created", profile_id, current_user.id)
    else:
        await manager.send_to_user(current_user.id, "teamup.comment.created", {"profile_id": profile_id, "comment_id": comment.id, "actor_id": current_user.id, "moderation_status": comment.moderation_status})
    if not current_user.content_moderation_whitelisted:
        moderation_service.schedule_teamup_comment_moderation(comment.id)
    return (await teamup_service._build_comment_reads(db, [comment], current_user.id))[0]


@router.post("/profiles/{profile_id}/comments/{comment_id}/like", response_model=TeamupCommentToggleRead)
async def toggle_comment_like(
    profile_id: int,
    comment_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> TeamupCommentToggleRead:
    result = await teamup_service.toggle_comment_like(db, profile_id, comment_id, current_user.id)
    await _broadcast_profile(db, "teamup.comment.like.updated", profile_id, current_user.id)
    return result


@router.delete("/profiles/{profile_id}/comments/{comment_id}", response_model=MessageResponse)
async def delete_comment(
    profile_id: int,
    comment_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> MessageResponse:
    await teamup_service.delete_comment(db, profile_id, comment_id, current_user.id)
    await _broadcast_profile(db, "teamup.comment.deleted", profile_id, current_user.id)
    return MessageResponse(detail="Comment deleted")


@router.get("/admin/events", response_model=list[TeamupEventRead])
async def admin_list_events(
    current_admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[TeamupEventRead]:
    del current_admin
    events = await teamup_service.list_events(db)
    return [await teamup_service.build_event_read(db, event) for event in events]


@router.post("/admin/events", response_model=TeamupEventRead, status_code=status.HTTP_201_CREATED)
async def admin_create_event(
    payload: TeamupEventUpsert,
    current_admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> TeamupEventRead:
    event = await teamup_service.upsert_event(db, current_admin, payload)
    return await teamup_service.build_event_read(db, event)


@router.put("/admin/events/{event_id}", response_model=TeamupEventRead)
async def admin_update_event(
    event_id: int,
    payload: TeamupEventUpsert,
    current_admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> TeamupEventRead:
    event = await teamup_service.upsert_event(db, current_admin, payload, event_id=event_id)
    return await teamup_service.build_event_read(db, event)
