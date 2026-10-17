from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import PurePosixPath

from fastapi import HTTPException, status
from sqlalchemy import case, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.core.config import settings
from app.models.enums import ModerationStatus, TeamupProfileStatus, TeamupSkill, TeamupSkillLevel
from app.models.friend import Friendship
from app.models.teamup import TeamupComment, TeamupCommentLike, TeamupEvent, TeamupProfile
from app.models.user import User
from app.schemas.teamup import (
    TeamupCommentRead,
    TeamupCommentToggleRead,
    TeamupEventRead,
    TeamupEventUpsert,
    TeamupProfileRead,
    TeamupProfileSave,
    TeamupSkillItem,
    TeamupSkillStat,
    TeamupStatsRead,
)
from app.schemas.user import UserPublic
from app.services.penalties import ensure_can_post
from app.services.ccw import previews_from_text
from app.services.storage import delete_objects, storage_key_from_url, thumbnail_key_for_key

UTC = timezone.utc
TEAMUP_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}


def _status_value(value: object) -> str:
    return str(getattr(value, "value", value)).lower()


def _profile_is_approved():
    return func.lower(TeamupProfile.moderation_status) == ModerationStatus.APPROVED.value


def _comment_is_approved():
    return func.lower(TeamupComment.moderation_status) == ModerationStatus.APPROVED.value


def _teamup_upload_path(value: str) -> str | None:
    relative_path = storage_key_from_url(value)
    if relative_path is None:
        return None
    path_parts = PurePosixPath(relative_path).parts
    if len(path_parts) != 2 or path_parts[0] != "teamup" or path_parts[1] == "thumbs" or ".." in path_parts:
        return None
    if PurePosixPath(path_parts[1]).suffix.lower() not in TEAMUP_IMAGE_SUFFIXES:
        return None
    return relative_path


def _image_urls_to_json(image_urls: list[str]) -> str:
    return json.dumps(image_urls, ensure_ascii=False, separators=(",", ":"))


def _image_urls_from_json(value: str | None) -> list[str]:
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(parsed, list):
        return []
    return [item for item in parsed if isinstance(item, str) and item.strip()]


def _ccw_creations_to_json(value: list[dict[str, object]]) -> str | None:
    if not value:
        return None
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _ccw_creations_from_json(value: str | None) -> list[dict[str, object]]:
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(parsed, list):
        return []
    return [item for item in parsed if isinstance(item, dict)]


def _normalize_skill_value(value: object) -> str | None:
    if value in {"art_2d", "art_3d"}:
        return TeamupSkill.ART.value
    if isinstance(value, TeamupSkill):
        return value.value
    if isinstance(value, str):
        return value
    return None


def _skills_to_json(skills: list[TeamupSkillItem]) -> str:
    return json.dumps(
        [{"skill": item.skill.value, "level": item.level.value} for item in skills],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _skills_from_json(value: str | None) -> list[TeamupSkillItem]:
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(parsed, list):
        return []
    items: list[TeamupSkillItem] = []
    seen: set[TeamupSkill] = set()
    for entry in parsed:
        if not isinstance(entry, dict):
            continue
        try:
            skill_value = _normalize_skill_value(entry.get("skill"))
            if skill_value is None:
                continue
            skill = TeamupSkill(skill_value)
            level = TeamupSkillLevel(entry.get("level", TeamupSkillLevel.SKILLED.value))
        except ValueError:
            continue
        if skill in seen:
            continue
        seen.add(skill)
        items.append(TeamupSkillItem(skill=skill, level=level))
    return items


def _looking_for_to_json(values: list[TeamupSkill]) -> str:
    return json.dumps([item.value for item in values], ensure_ascii=False, separators=(",", ":"))


def _looking_for_from_json(value: str | None) -> list[TeamupSkill]:
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(parsed, list):
        return []
    result: list[TeamupSkill] = []
    seen: set[TeamupSkill] = set()
    for entry in parsed:
        try:
            skill_value = _normalize_skill_value(entry)
            if skill_value is None:
                continue
            skill = TeamupSkill(skill_value)
        except ValueError:
            continue
        if skill in seen:
            continue
        seen.add(skill)
        result.append(skill)
    return result


def schedule_delete_upload_files(paths: list[str]) -> None:
    if paths:
        asyncio.create_task(asyncio.to_thread(delete_objects, paths))


async def _friend_ids(db: AsyncSession, user_id: int) -> list[int]:
    result = await db.execute(select(Friendship.friend_id).where(Friendship.user_id == user_id))
    return list(result.scalars().all())


async def is_friend(db: AsyncSession, viewer_id: int, author_id: int) -> bool:
    if viewer_id == author_id:
        return False
    result = await db.execute(
        select(Friendship.id).where(Friendship.user_id == viewer_id, Friendship.friend_id == author_id).limit(1)
    )
    return result.scalar_one_or_none() is not None


# ----- Events -----


async def get_active_event(db: AsyncSession) -> TeamupEvent | None:
    result = await db.execute(
        select(TeamupEvent).where(TeamupEvent.is_active.is_(True)).order_by(TeamupEvent.created_at.desc()).limit(1)
    )
    return result.scalar_one_or_none()


async def _event_counts(db: AsyncSession, event_id: int | None) -> tuple[int, int]:
    base = select(func.count(TeamupProfile.id)).where(TeamupProfile.deleted_at.is_(None), _profile_is_approved())
    if event_id is None:
        base = base.where(TeamupProfile.event_id.is_(None))
    else:
        base = base.where(TeamupProfile.event_id == event_id)
    total_result = await db.execute(base)
    recruiting_result = await db.execute(base.where(TeamupProfile.status == TeamupProfileStatus.RECRUITING.value))
    return int(total_result.scalar_one() or 0), int(recruiting_result.scalar_one() or 0)


async def _all_profile_counts(db: AsyncSession) -> tuple[int, int]:
    base = select(func.count(TeamupProfile.id)).where(TeamupProfile.deleted_at.is_(None), _profile_is_approved())
    total_result = await db.execute(base)
    recruiting_result = await db.execute(base.where(TeamupProfile.status == TeamupProfileStatus.RECRUITING.value))
    return int(total_result.scalar_one() or 0), int(recruiting_result.scalar_one() or 0)


async def overall_stats(db: AsyncSession, event_id: int | None) -> TeamupStatsRead:
    """组队中心概览。total_* 和 _all_profile_counts 同一个口径（全部赛季，未删除、已通过审核），
    评论只数已通过且未删除的；profile_count / recruiting_count 是广场当前在展示的那一批。"""
    profile_count, recruiting_count = await _event_counts(db, event_id)
    visible_profile = (TeamupProfile.deleted_at.is_(None), _profile_is_approved())
    row = (
        await db.execute(
            select(
                func.count(TeamupProfile.id),
                func.coalesce(
                    func.sum(case((TeamupProfile.status == TeamupProfileStatus.RECRUITING.value, 1), else_=0)), 0
                ),
                func.coalesce(func.sum(TeamupProfile.view_count), 0),
            ).where(*visible_profile)
        )
    ).one()
    comment_count = (
        await db.execute(
            select(func.count(TeamupComment.id))
            .join(TeamupProfile, TeamupProfile.id == TeamupComment.profile_id)
            .where(TeamupComment.deleted_at.is_(None), _comment_is_approved(), *visible_profile)
        )
    ).scalar_one()
    return TeamupStatsRead(
        profile_count=profile_count,
        recruiting_count=recruiting_count,
        total_profile_count=int(row[0] or 0),
        total_recruiting_count=int(row[1] or 0),
        total_view_count=int(row[2] or 0),
        total_comment_count=int(comment_count or 0),
    )


def _seconds_remaining(event: TeamupEvent) -> int | None:
    if event.ends_at is None:
        return None
    ends_at = event.ends_at if event.ends_at.tzinfo is not None else event.ends_at.replace(tzinfo=UTC)
    delta = (ends_at - datetime.now(UTC)).total_seconds()
    return max(0, int(delta))


async def build_event_read(db: AsyncSession, event: TeamupEvent) -> TeamupEventRead:
    profile_count, recruiting_count = await _event_counts(db, event.id)
    total_profile_count, total_recruiting_count = await _all_profile_counts(db)
    return TeamupEventRead(
        id=event.id,
        title=event.title,
        theme=event.theme,
        cover_url=event.cover_url,
        starts_at=event.starts_at,
        ends_at=event.ends_at,
        is_active=event.is_active,
        seconds_remaining=_seconds_remaining(event),
        profile_count=profile_count,
        recruiting_count=recruiting_count,
        total_profile_count=total_profile_count,
        total_recruiting_count=total_recruiting_count,
    )


async def list_events(db: AsyncSession) -> list[TeamupEvent]:
    result = await db.execute(select(TeamupEvent).order_by(TeamupEvent.created_at.desc()))
    return list(result.scalars().all())


async def upsert_event(db: AsyncSession, actor: User, payload: TeamupEventUpsert, event_id: int | None = None) -> TeamupEvent:
    if event_id is not None:
        event = await db.get(TeamupEvent, event_id)
        if event is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Event not found")
        event.title = payload.title
        event.theme = payload.theme
        event.cover_url = payload.cover_url
        event.starts_at = payload.starts_at
        event.ends_at = payload.ends_at
        event.is_active = payload.is_active
        event.updated_at = datetime.now(UTC)
    else:
        event = TeamupEvent(
            title=payload.title,
            theme=payload.theme,
            cover_url=payload.cover_url,
            starts_at=payload.starts_at,
            ends_at=payload.ends_at,
            is_active=payload.is_active,
            created_by_id=actor.id,
        )
        db.add(event)
    await db.flush()
    if payload.is_active:
        await db.execute(
            update(TeamupEvent).where(TeamupEvent.id != event.id, TeamupEvent.is_active.is_(True)).values(is_active=False)
        )
    await db.commit()
    await db.refresh(event)
    return event


# ----- Profiles -----


async def get_active_event_id(db: AsyncSession) -> int | None:
    event = await get_active_event(db)
    return event.id if event is not None else None


async def get_my_profile(db: AsyncSession, user_id: int, event_id: int | None, include_deleted: bool = False) -> TeamupProfile | None:
    query = select(TeamupProfile).options(joinedload(TeamupProfile.user)).where(TeamupProfile.user_id == user_id)
    if event_id is None:
        query = query.where(TeamupProfile.event_id.is_(None))
    else:
        query = query.where(TeamupProfile.event_id == event_id)
    if not include_deleted:
        query = query.where(TeamupProfile.deleted_at.is_(None))
    result = await db.execute(query.limit(1))
    return result.scalar_one_or_none()


async def upsert_profile(db: AsyncSession, author: User, payload: TeamupProfileSave) -> TeamupProfile:
    ensure_can_post(author)
    if any(_teamup_upload_path(image_url) is None for image_url in payload.image_urls):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="名片配图必须是已上传的组队图片")
    if payload.background_url is not None and _teamup_upload_path(payload.background_url) is None:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="背景图必须是已上传的组队图片")

    event_id = await get_active_event_id(db)
    profile = await get_my_profile(db, author.id, event_id, include_deleted=True)
    previous_paths = _profile_upload_paths(profile) if profile is not None else set()
    initial_status = (
        ModerationStatus.PENDING.value
        if settings.content_moderation_enabled and not author.content_moderation_whitelisted
        else ModerationStatus.APPROVED.value
    )
    # 在写任何东西之前取：要去 CCW 时会先把请求的读事务结束、连接还回去
    ccw_creations = await previews_from_text(payload.intro, db=db)
    ccw_creations_json = _ccw_creations_to_json(ccw_creations)

    if profile is None:
        profile = TeamupProfile(user_id=author.id, event_id=event_id)
        db.add(profile)
    profile.headline = payload.headline
    profile.intro = payload.intro
    profile.skills_json = _skills_to_json(payload.skills)
    profile.looking_for_json = _looking_for_to_json(payload.looking_for)
    profile.image_urls_json = _image_urls_to_json(payload.image_urls)
    profile.background_url = payload.background_url
    profile.ccw_creations_json = ccw_creations_json
    profile.contact_note = payload.contact_note
    profile.status = payload.status.value
    profile.moderation_status = initial_status
    profile.deleted_at = None
    profile.updated_at = datetime.now(UTC)
    profile.user = author
    await db.commit()
    await db.refresh(profile)

    new_paths = _profile_upload_paths(profile)
    removed_paths = previous_paths - new_paths
    if removed_paths:
        keys_to_delete: list[str] = []
        for relative_path in removed_paths:
            if await _upload_path_still_referenced(db, relative_path):
                continue
            keys_to_delete.append(relative_path)
            keys_to_delete.append(thumbnail_key_for_key(relative_path))
        schedule_delete_upload_files(keys_to_delete)
    return profile


def _profile_upload_paths(profile: TeamupProfile) -> set[str]:
    paths: set[str] = set()
    for image_url in _image_urls_from_json(profile.image_urls_json):
        path = _teamup_upload_path(image_url)
        if path is not None:
            paths.add(path)
    background_path = _teamup_upload_path(profile.background_url) if profile.background_url else None
    if background_path is not None:
        paths.add(background_path)
    return paths


async def _upload_path_still_referenced(db: AsyncSession, relative_path: str) -> bool:
    path_fragment = f"%{relative_path}%"
    result = await db.execute(
        select(TeamupProfile.id)
        .where(
            TeamupProfile.deleted_at.is_(None),
            or_(
                TeamupProfile.image_urls_json.like(path_fragment),
                TeamupProfile.background_url.like(path_fragment),
            ),
        )
        .limit(1)
    )
    return result.scalar_one_or_none() is not None


async def get_profile(db: AsyncSession, profile_id: int) -> TeamupProfile:
    result = await db.execute(
        select(TeamupProfile).options(joinedload(TeamupProfile.user)).where(TeamupProfile.id == profile_id)
    )
    profile = result.scalar_one_or_none()
    if profile is None or profile.deleted_at is not None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Profile not found")
    return profile


async def get_visible_profile(db: AsyncSession, profile_id: int, viewer_id: int) -> TeamupProfile:
    profile = await get_profile(db, profile_id)
    if profile.user_id != viewer_id and _status_value(profile.moderation_status) != ModerationStatus.APPROVED.value:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Profile not found")
    return profile


async def increment_view(db: AsyncSession, profile: TeamupProfile, viewer_id: int) -> None:
    if profile.user_id == viewer_id:
        return
    await db.execute(
        update(TeamupProfile).where(TeamupProfile.id == profile.id).values(view_count=TeamupProfile.view_count + 1)
    )
    await db.commit()


def _skill_match_condition(skill: TeamupSkill):
    if skill == TeamupSkill.ART:
        return or_(
            TeamupProfile.skills_json.like('%"art"%'),
            TeamupProfile.looking_for_json.like('%"art"%'),
            TeamupProfile.skills_json.like('%"art_2d"%'),
            TeamupProfile.looking_for_json.like('%"art_2d"%'),
            TeamupProfile.skills_json.like('%"art_3d"%'),
            TeamupProfile.looking_for_json.like('%"art_3d"%'),
        )
    fragment = f'%"{skill.value}"%'
    return or_(TeamupProfile.skills_json.like(fragment), TeamupProfile.looking_for_json.like(fragment))


async def list_profiles(
    db: AsyncSession,
    viewer_id: int,
    scope: str,
    event_id: int | None,
    skill: TeamupSkill | None,
    status_filter: TeamupProfileStatus | None,
    sort: str,
    before_id: int | None,
    limit: int,
    search: str | None,
) -> list[TeamupProfile]:
    query = select(TeamupProfile).options(joinedload(TeamupProfile.user)).where(TeamupProfile.deleted_at.is_(None))
    if scope == "mine":
        query = query.where(TeamupProfile.user_id == viewer_id)
    else:
        query = query.where(or_(TeamupProfile.user_id == viewer_id, _profile_is_approved()))
        if event_id is None:
            query = query.where(TeamupProfile.event_id.is_(None))
        else:
            query = query.where(TeamupProfile.event_id == event_id)
    if skill is not None:
        query = query.where(_skill_match_condition(skill))
    if status_filter is not None:
        query = query.where(TeamupProfile.status == status_filter.value)
    if search:
        like = f"%{search.strip()}%"
        query = query.where(or_(TeamupProfile.headline.ilike(like), TeamupProfile.intro.ilike(like)))
    if before_id is not None:
        query = query.where(TeamupProfile.id < before_id, TeamupProfile.pinned_at.is_(None))
    if sort == "hot":
        order_by = (TeamupProfile.pinned_at.desc().nullslast(), TeamupProfile.view_count.desc(), TeamupProfile.id.desc())
    else:
        order_by = (TeamupProfile.pinned_at.desc().nullslast(), TeamupProfile.id.desc())
    result = await db.execute(query.order_by(*order_by).limit(limit))
    return list(result.scalars().all())


async def set_profile_status(db: AsyncSession, user_id: int, event_id: int | None, new_status: TeamupProfileStatus) -> TeamupProfile:
    profile = await get_my_profile(db, user_id, event_id)
    if profile is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Profile not found")
    profile.status = new_status.value
    profile.updated_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(profile)
    return profile


async def delete_profile(db: AsyncSession, user_id: int, event_id: int | None) -> None:
    profile = await get_my_profile(db, user_id, event_id)
    if profile is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Profile not found")
    candidate_paths = _profile_upload_paths(profile)
    profile.deleted_at = datetime.now(UTC)
    await db.commit()
    keys_to_delete: list[str] = []
    for relative_path in candidate_paths:
        if await _upload_path_still_referenced(db, relative_path):
            continue
        keys_to_delete.append(relative_path)
        keys_to_delete.append(thumbnail_key_for_key(relative_path))
    schedule_delete_upload_files(keys_to_delete)


async def skill_stats(db: AsyncSession, event_id: int | None) -> list[TeamupSkillStat]:
    query = select(TeamupProfile.skills_json).where(
        TeamupProfile.deleted_at.is_(None),
        _profile_is_approved(),
        TeamupProfile.status == TeamupProfileStatus.RECRUITING.value,
    )
    if event_id is None:
        query = query.where(TeamupProfile.event_id.is_(None))
    else:
        query = query.where(TeamupProfile.event_id == event_id)
    result = await db.execute(query)
    counts: dict[TeamupSkill, int] = {skill: 0 for skill in TeamupSkill}
    for (skills_json,) in result.all():
        for item in _skills_from_json(skills_json):
            counts[item.skill] += 1
    return [TeamupSkillStat(skill=skill, count=count) for skill, count in counts.items()]


# ----- Comments -----


async def _comment_like_counts(db: AsyncSession, comment_ids: list[int]) -> dict[int, int]:
    if not comment_ids:
        return {}
    result = await db.execute(
        select(TeamupCommentLike.comment_id, func.count(TeamupCommentLike.id))
        .where(TeamupCommentLike.comment_id.in_(comment_ids))
        .group_by(TeamupCommentLike.comment_id)
    )
    return {comment_id: int(count or 0) for comment_id, count in result.all()}


async def _liked_comment_ids(db: AsyncSession, comment_ids: list[int], viewer_id: int) -> set[int]:
    if not comment_ids:
        return set()
    result = await db.execute(
        select(TeamupCommentLike.comment_id).where(
            TeamupCommentLike.comment_id.in_(comment_ids), TeamupCommentLike.user_id == viewer_id
        )
    )
    return set(result.scalars().all())


async def _comment_parent_authors(db: AsyncSession, comments: list[TeamupComment]) -> dict[int, UserPublic]:
    parent_ids = {comment.parent_id for comment in comments if comment.parent_id is not None}
    if not parent_ids:
        return {}
    result = await db.execute(
        select(TeamupComment).options(joinedload(TeamupComment.author)).where(TeamupComment.id.in_(parent_ids))
    )
    parent_authors: dict[int, UserPublic] = {}
    for parent in result.scalars().all():
        if parent.author is not None:
            parent_authors[parent.id] = UserPublic.model_validate(parent.author)
    return parent_authors


async def _build_comment_reads(db: AsyncSession, comments: list[TeamupComment], viewer_id: int) -> list[TeamupCommentRead]:
    ids = [comment.id for comment in comments]
    like_counts = await _comment_like_counts(db, ids)
    liked_ids = await _liked_comment_ids(db, ids, viewer_id)
    parent_authors = await _comment_parent_authors(db, comments)
    return [
        TeamupCommentRead(
            id=comment.id,
            profile_id=comment.profile_id,
            author_id=comment.author_id,
            author=UserPublic.model_validate(comment.author) if comment.author is not None else None,
            parent_id=comment.parent_id,
            parent_author=parent_authors.get(comment.parent_id) if comment.parent_id is not None else None,
            content=comment.content,
            moderation_status=ModerationStatus(comment.moderation_status),
            created_at=comment.created_at,
            like_count=like_counts.get(comment.id, 0),
            liked_by_me=comment.id in liked_ids,
        )
        for comment in comments
    ]


async def _recent_comments(db: AsyncSession, profile_ids: list[int], viewer_id: int, limit_per_profile: int = 3) -> dict[int, list[TeamupCommentRead]]:
    if not profile_ids:
        return {}
    result = await db.execute(
        select(TeamupComment)
        .options(joinedload(TeamupComment.author))
        .where(
            TeamupComment.profile_id.in_(profile_ids),
            TeamupComment.deleted_at.is_(None),
            or_(TeamupComment.author_id == viewer_id, _comment_is_approved()),
        )
        .order_by(TeamupComment.profile_id.asc(), TeamupComment.created_at.desc(), TeamupComment.id.desc())
    )
    grouped_raw: dict[int, list[TeamupComment]] = {profile_id: [] for profile_id in profile_ids}
    for comment in result.scalars().all():
        bucket = grouped_raw.setdefault(comment.profile_id, [])
        if len(bucket) < limit_per_profile:
            bucket.append(comment)
    flattened = [comment for bucket in grouped_raw.values() for comment in bucket]
    reads = {read.id: read for read in await _build_comment_reads(db, flattened, viewer_id)}
    grouped: dict[int, list[TeamupCommentRead]] = {}
    for profile_id, bucket in grouped_raw.items():
        ordered = [reads[comment.id] for comment in reversed(bucket) if comment.id in reads]
        grouped[profile_id] = ordered
    return grouped


async def _comment_counts(db: AsyncSession, profile_ids: list[int], viewer_id: int) -> dict[int, int]:
    if not profile_ids:
        return {}
    result = await db.execute(
        select(TeamupComment.profile_id, func.count(TeamupComment.id))
        .where(
            TeamupComment.profile_id.in_(profile_ids),
            TeamupComment.deleted_at.is_(None),
            or_(TeamupComment.author_id == viewer_id, _comment_is_approved()),
        )
        .group_by(TeamupComment.profile_id)
    )
    return {profile_id: int(count or 0) for profile_id, count in result.all()}


async def build_profile_reads(db: AsyncSession, profiles: list[TeamupProfile], viewer_id: int) -> list[TeamupProfileRead]:
    profile_ids = [profile.id for profile in profiles]
    comment_counts = await _comment_counts(db, profile_ids, viewer_id)
    recent_comments = await _recent_comments(db, profile_ids, viewer_id)
    friend_ids = set(await _friend_ids(db, viewer_id))
    return [
        TeamupProfileRead(
            id=profile.id,
            user_id=profile.user_id,
            author=UserPublic.model_validate(profile.user) if profile.user is not None else None,
            event_id=profile.event_id,
            headline=profile.headline,
            intro=profile.intro,
            skills=_skills_from_json(profile.skills_json),
            looking_for=_looking_for_from_json(profile.looking_for_json),
            image_urls=_image_urls_from_json(profile.image_urls_json),
            background_url=profile.background_url,
            ccw_creations=_ccw_creations_from_json(profile.ccw_creations_json),
            contact_note=profile.contact_note,
            status=TeamupProfileStatus(profile.status),
            moderation_status=ModerationStatus(profile.moderation_status),
            view_count=profile.view_count,
            pinned_at=profile.pinned_at,
            created_at=profile.created_at,
            updated_at=profile.updated_at,
            comment_count=comment_counts.get(profile.id, 0),
            recent_comments=recent_comments.get(profile.id, []),
            is_friend=profile.user_id in friend_ids,
            is_self=profile.user_id == viewer_id,
        )
        for profile in profiles
    ]


async def build_profile_read(db: AsyncSession, profile: TeamupProfile, viewer_id: int) -> TeamupProfileRead:
    return (await build_profile_reads(db, [profile], viewer_id))[0]


async def create_comment(db: AsyncSession, profile_id: int, actor: User, content: str, parent_id: int | None = None) -> TeamupComment:
    ensure_can_post(actor)
    profile = await get_visible_profile(db, profile_id, actor.id)
    if _status_value(profile.moderation_status) != ModerationStatus.APPROVED.value:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Profile is pending moderation")
    if parent_id is not None:
        parent_result = await db.execute(
            select(TeamupComment).where(
                TeamupComment.id == parent_id,
                TeamupComment.profile_id == profile_id,
                TeamupComment.deleted_at.is_(None),
                _comment_is_approved(),
            )
        )
        if parent_result.scalar_one_or_none() is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Parent comment not found")
    initial_status = (
        ModerationStatus.PENDING.value
        if settings.content_moderation_enabled and not actor.content_moderation_whitelisted
        else ModerationStatus.APPROVED.value
    )
    comment = TeamupComment(
        profile_id=profile_id, author_id=actor.id, parent_id=parent_id, content=content, moderation_status=initial_status
    )
    db.add(comment)
    await db.flush()
    comment.author = actor
    await db.commit()
    return await get_comment(db, profile_id, comment.id, actor.id)


async def get_comment(db: AsyncSession, profile_id: int, comment_id: int, viewer_id: int) -> TeamupComment:
    result = await db.execute(
        select(TeamupComment)
        .options(joinedload(TeamupComment.author))
        .where(
            TeamupComment.id == comment_id,
            TeamupComment.profile_id == profile_id,
            TeamupComment.deleted_at.is_(None),
            or_(TeamupComment.author_id == viewer_id, _comment_is_approved()),
        )
    )
    comment = result.scalar_one_or_none()
    if comment is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Comment not found")
    return comment


async def list_comments(db: AsyncSession, profile_id: int, viewer_id: int, before_id: int | None, limit: int) -> list[TeamupCommentRead]:
    await get_visible_profile(db, profile_id, viewer_id)
    query = (
        select(TeamupComment)
        .options(joinedload(TeamupComment.author))
        .where(
            TeamupComment.profile_id == profile_id,
            TeamupComment.deleted_at.is_(None),
            or_(TeamupComment.author_id == viewer_id, _comment_is_approved()),
        )
    )
    if before_id is not None:
        query = query.where(TeamupComment.id < before_id)
    result = await db.execute(query.order_by(TeamupComment.id.desc()).limit(limit))
    comments = list(result.scalars().all())
    comments.reverse()
    return await _build_comment_reads(db, comments, viewer_id)


async def toggle_comment_like(db: AsyncSession, profile_id: int, comment_id: int, actor_id: int) -> TeamupCommentToggleRead:
    await get_visible_profile(db, profile_id, actor_id)
    comment = await get_comment(db, profile_id, comment_id, actor_id)
    if _status_value(comment.moderation_status) != ModerationStatus.APPROVED.value:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Comment is pending moderation")
    result = await db.execute(
        select(TeamupCommentLike).where(
            TeamupCommentLike.comment_id == comment_id, TeamupCommentLike.user_id == actor_id
        )
    )
    like = result.scalar_one_or_none()
    liked = like is None
    if like is None:
        db.add(TeamupCommentLike(comment_id=comment_id, user_id=actor_id))
    else:
        await db.delete(like)
    await db.commit()
    count_result = await db.execute(
        select(func.count(TeamupCommentLike.id)).where(TeamupCommentLike.comment_id == comment_id)
    )
    return TeamupCommentToggleRead(liked=liked, like_count=int(count_result.scalar_one() or 0))


async def delete_comment(db: AsyncSession, profile_id: int, comment_id: int, actor_id: int) -> None:
    profile = await get_visible_profile(db, profile_id, actor_id)
    result = await db.execute(
        select(TeamupComment).where(
            TeamupComment.id == comment_id, TeamupComment.profile_id == profile_id, TeamupComment.deleted_at.is_(None)
        )
    )
    comment = result.scalar_one_or_none()
    if comment is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Comment not found")
    if comment.author_id != actor_id and profile.user_id != actor_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You cannot delete this comment")
    comment.deleted_at = datetime.now(UTC)
    await db.commit()


async def comment_recipient_ids(db: AsyncSession, profile: TeamupProfile, comment: TeamupComment) -> set[int]:
    return {profile.user_id, comment.author_id}
