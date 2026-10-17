"""CCW 相关的业务逻辑：账号绑定、资料同步、作品链接卡片。

这里只有 KukeChat 自己的规则 —— 绑定口令怎么生成和匹配、取到的资料怎么落到用户身上、作品卡片怎么拼。
「怎么向 CCW 要到数据」不在本仓库里：所有取数都走 app/integrations 的 CcwGateway 接口，实现由部署方提供。
没有接入（网关 available() 为 False）时：绑定显示为未开放，资料同步和作品卡片直接跳过。
"""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qs, urlparse

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.integrations import CcwUnavailable, get_ccw_gateway
from app.models.user import CcwBindingChallenge, User
from app.services import response_cache


OID_PATTERN = re.compile(r"^[0-9a-fA-F]{24}$")
CCW_URL_PATTERN = re.compile(r"https?://(?:www\.)?ccw\.site/detail/[0-9a-fA-F]{24}[^\s<>'\"]*", re.IGNORECASE)
TRAILING_URL_PUNCTUATION = "，。！？；：,.!?;:)]}）】》"
logger = logging.getLogger(__name__)
UTC = timezone.utc
CHALLENGE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_ccw_binding_task: asyncio.Task | None = None
# 取数失败时要接住的异常：网关取不到（CcwUnavailable），或者取到的内容拼不出我们要的东西（ValueError）。
# OSError 是兜底：万一某个网关实现把底层的网络异常漏了出来，也按取数失败处理。
FETCH_ERRORS = (CcwUnavailable, ValueError, OSError)


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _number(value: Any) -> int | float | None:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _cache_key(oid: str, access_key: str = "") -> str:
    return f"{oid.lower()}:{access_key.strip()}"


def ccw_profile_url(student_oid: str) -> str:
    return f"https://www.ccw.site/student/{student_oid}"


def ccw_gateway_available() -> bool:
    """本服务有没有接入 CCW（有没有网关实现）。"""
    return get_ccw_gateway().available()


def ccw_binding_configured() -> bool:
    """CCW 绑定是否可用。

    需要接入了 CCW（网关），以及运营方自己的作品 oid 与访客标识；本项目不内置任何默认值，
    缺任何一样该功能整体不可用（而不是拿别人的标识去请求 CCW）。
    """
    return bool((settings.ccw_bind_subject_oid or "").strip() and (settings.ccw_bind_guest_id or "").strip()) and ccw_gateway_available()


def ccw_bind_creation_url() -> str:
    return f"https://www.ccw.site/detail/{settings.ccw_bind_subject_oid}"


async def fetch_comment_page() -> list[dict[str, Any]]:
    """绑定用的那个评论区最新的一页评论。取不到抛 CcwUnavailable。"""
    return await get_ccw_gateway().latest_comments(settings.ccw_bind_subject_oid, settings.ccw_bind_subject_type)


async def fetch_student_payloads(student_oid: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """某个学生的（资料, 主页统计）。取不到抛 CcwUnavailable。"""
    payloads = await get_ccw_gateway().student_payloads(student_oid)
    return payloads.profile, payloads.detail


def normalize_student_profile(profile_payload: dict[str, Any] | None, detail_payload: dict[str, Any] | None, fallback: dict[str, Any] | None = None) -> dict[str, Any]:
    profile_body = profile_payload.get("body") if isinstance(profile_payload, dict) else None
    detail_body = detail_payload.get("body") if isinstance(detail_payload, dict) else None
    if not isinstance(profile_body, dict):
        profile_body = {}
    if not isinstance(detail_body, dict):
        detail_body = {}
    if not isinstance(fallback, dict):
        fallback = {}
    member_archive = profile_body.get("memberArchive") if isinstance(profile_body.get("memberArchive"), dict) else {}
    extra_info = profile_body.get("extraInfo") if isinstance(profile_body.get("extraInfo"), dict) else {}
    oid = _text(profile_body.get("studentOid")) or _text(detail_body.get("oid")) or _text(fallback.get("accountOid"))
    if not oid or not OID_PATTERN.fullmatch(oid):
        raise ValueError("Missing CCW student oid")
    return {
        "student_oid": oid.lower(),
        "name": _text(profile_body.get("name")) or _text(detail_body.get("name")) or _text(fallback.get("name")),
        "avatar_url": _text(profile_body.get("avatar")) or _text(detail_body.get("avatar")) or _text(fallback.get("avatar")) or _text(profile_body.get("picUrl")) or _text(detail_body.get("picUrl")),
        "bio": _text(profile_body.get("bio")) or _text(detail_body.get("bio")) or _text(extra_info.get("selfIntroduction")),
        "homepage_cover_url": _text(member_archive.get("homepageCover")),
        "following_count": _int(detail_body.get("followingCount")),
        "follower_count": _int(detail_body.get("followerCount")),
        "like_count": _int(detail_body.get("likeCount")),
        "favorite_count": _int(detail_body.get("favoriteCount")),
        "comment_count": _int(profile_body.get("commentCount")) or _int(detail_body.get("commentCount")),
        "creation_count": _int(detail_body.get("creationCount")),
        "view_count": _int(detail_body.get("viewCount")),
    }


def apply_student_profile(user: User, data: dict[str, Any], sync_profile: bool = False) -> None:
    user.ccw_student_oid = data["student_oid"]
    user.ccw_name = data.get("name")
    user.ccw_avatar_url = data.get("avatar_url")
    user.ccw_bio = data.get("bio")
    user.ccw_homepage_cover_url = data.get("homepage_cover_url")
    user.ccw_following_count = data.get("following_count")
    user.ccw_follower_count = data.get("follower_count")
    user.ccw_like_count = data.get("like_count")
    user.ccw_favorite_count = data.get("favorite_count")
    user.ccw_comment_count = data.get("comment_count")
    user.ccw_creation_count = data.get("creation_count")
    user.ccw_view_count = data.get("view_count")
    user.ccw_synced_at = datetime.now(UTC)
    if sync_profile:
        if data.get("name"):
            user.nickname = data["name"][:80]
        if data.get("avatar_url"):
            user.avatar_url = data["avatar_url"][:500]
        if data.get("bio"):
            user.bio = data["bio"][:1000]
        if data.get("homepage_cover_url"):
            user.profile_cover_url = data["homepage_cover_url"][:500]
            user.profile_cover_pending_url = None
            user.profile_cover_moderation_status = "approved"


async def sync_bound_student(db: AsyncSession, user: User, sync_profile: bool = False) -> User:
    if not user.ccw_student_oid:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="还没有绑定 CCW 账号")
    if not ccw_gateway_available():
        # 没有接入 CCW：没东西可同步，资料原样留着，不报错
        return user
    try:
        profile_payload, detail_payload = await fetch_student_payloads(user.ccw_student_oid)
        data = normalize_student_profile(profile_payload, detail_payload, {"accountOid": user.ccw_student_oid, "name": user.ccw_name, "avatar": user.ccw_avatar_url})
    except FETCH_ERRORS as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="同步 CCW 账号信息失败") from exc
    apply_student_profile(user, data, sync_profile=sync_profile)
    await db.commit()
    await db.refresh(user)
    return user


async def unbind_student(db: AsyncSession, user: User) -> User:
    if not user.ccw_student_oid:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="还没有绑定 CCW 账号")

    user.ccw_student_oid = None
    user.ccw_name = None
    user.ccw_avatar_url = None
    user.ccw_bio = None
    user.ccw_homepage_cover_url = None
    user.ccw_following_count = None
    user.ccw_follower_count = None
    user.ccw_like_count = None
    user.ccw_favorite_count = None
    user.ccw_comment_count = None
    user.ccw_creation_count = None
    user.ccw_view_count = None
    user.ccw_synced_at = None
    await db.commit()
    await db.refresh(user)
    return user


async def create_binding_challenge(db: AsyncSession, user: User) -> CcwBindingChallenge:
    if not ccw_gateway_available():
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="本服务未开放 CCW 账号绑定。")
    if not ccw_binding_configured():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="本服务未配置 CCW 账号绑定（需要 CCW_BIND_SUBJECT_OID 与 CCW_BIND_GUEST_ID）。",
        )
    now = datetime.now(UTC)
    await _expire_old_challenges(db, now)
    existing_result = await db.execute(
        select(CcwBindingChallenge)
        .where(CcwBindingChallenge.user_id == user.id, CcwBindingChallenge.status == "pending", CcwBindingChallenge.expires_at > now)
        .order_by(CcwBindingChallenge.created_at.desc())
        .limit(1)
    )
    existing = existing_result.scalar_one_or_none()
    if existing is not None:
        return existing

    for _ in range(10):
        code = f"{''.join(secrets.choice(CHALLENGE_ALPHABET) for _ in range(4))}-{''.join(secrets.choice(CHALLENGE_ALPHABET) for _ in range(4))}"
        duplicate = await db.execute(select(CcwBindingChallenge.id).where(CcwBindingChallenge.code == code).limit(1))
        if duplicate.scalar_one_or_none() is None:
            challenge = CcwBindingChallenge(
                user_id=user.id,
                code=code,
                subject_oid=settings.ccw_bind_subject_oid,
                expires_at=now + timedelta(minutes=settings.ccw_bind_challenge_minutes),
            )
            db.add(challenge)
            await db.commit()
            await db.refresh(challenge)
            return challenge
    raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="验证码生成失败，请重试")


async def latest_binding_challenge(db: AsyncSession, user_id: int) -> CcwBindingChallenge | None:
    now = datetime.now(UTC)
    await _expire_old_challenges(db, now)
    result = await db.execute(
        select(CcwBindingChallenge)
        .where(CcwBindingChallenge.user_id == user_id)
        .order_by(CcwBindingChallenge.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def check_binding_challenges_once(db: AsyncSession, user_id: int | None = None) -> int:
    now = datetime.now(UTC)
    await _expire_old_challenges(db, now)
    # 下面要去 CCW 拉评论（慢的时候几十秒）。SQLite 是整库一把写锁：带着没提交的写（上面的过期标记）去等网络，
    # 全站的写都会卡在 database is locked，连接池随之被占满（2026-10-17 12:42、12:58 两次）。所以先提交，空手去等
    await db.commit()
    query = select(CcwBindingChallenge).where(CcwBindingChallenge.status == "pending", CcwBindingChallenge.expires_at > now)
    if user_id is not None:
        query = query.where(CcwBindingChallenge.user_id == user_id)
    result = await db.execute(query.order_by(CcwBindingChallenge.created_at.asc()))
    challenges = list(result.scalars().all())
    await db.commit()
    if not challenges:
        return 0
    if not ccw_gateway_available():
        # 没有接入 CCW：没有评论可查，待验证的口令留着等它自己过期
        return 0
    comments = await fetch_comment_page()
    matched = 0
    for challenge in challenges:
        if await _match_challenge(db, challenge, comments):
            matched += 1
    checked_at = datetime.now(UTC)
    for challenge in challenges:
        challenge.last_checked_at = checked_at
    await db.commit()
    return matched


async def _expire_old_challenges(db: AsyncSession, now: datetime) -> None:
    result = await db.execute(select(CcwBindingChallenge).where(CcwBindingChallenge.status == "pending", CcwBindingChallenge.expires_at <= now))
    expired = list(result.scalars().all())
    for challenge in expired:
        challenge.status = "expired"
    if expired:
        await db.flush()


async def _match_challenge(db: AsyncSession, challenge: CcwBindingChallenge, comments: list[dict[str, Any]]) -> bool:
    now = datetime.now(UTC)
    code = challenge.code.upper()
    for comment in comments:
        content = _text(comment.get("content")) or ""
        if code not in content.upper():
            continue
        commenter = comment.get("commenter")
        if not isinstance(commenter, dict):
            continue
        student_oid = _text(commenter.get("accountOid"))
        if not student_oid or not OID_PATTERN.fullmatch(student_oid):
            continue
        duplicate_result = await db.execute(select(User.id).where(User.ccw_student_oid == student_oid.lower(), User.id != challenge.user_id).limit(1))
        if duplicate_result.scalar_one_or_none() is not None:
            challenge.status = "conflict"
            return False
        user = await db.get(User, challenge.user_id)
        if user is None:
            challenge.status = "expired"
            return False
        # 又要去 CCW 拉资料：同样先把前面的写提交掉，别带着写锁等网络
        await db.commit()
        try:
            profile_payload, detail_payload = await fetch_student_payloads(student_oid.lower())
            data = normalize_student_profile(profile_payload, detail_payload, commenter)
        except FETCH_ERRORS:
            data = normalize_student_profile(None, None, commenter)
        apply_student_profile(user, data, sync_profile=False)
        challenge.status = "verified"
        challenge.matched_student_oid = data["student_oid"]
        challenge.matched_comment_id = str(comment.get("id")) if comment.get("id") is not None else None
        challenge.matched_name = data.get("name")
        challenge.matched_avatar_url = data.get("avatar_url")
        challenge.verified_at = now
        return True
    return False


def start_ccw_binding_worker() -> None:
    global _ccw_binding_task
    if not ccw_binding_configured():
        return
    if _ccw_binding_task is not None and not _ccw_binding_task.done():
        return
    _ccw_binding_task = asyncio.create_task(_ccw_binding_worker())


async def stop_ccw_binding_worker() -> None:
    global _ccw_binding_task
    task = _ccw_binding_task
    _ccw_binding_task = None
    if task is None or task.done():
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def _ccw_binding_worker() -> None:
    while True:
        try:
            async with AsyncSessionLocal() as db:
                await check_binding_challenges_once(db)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("CCW binding poll failed")
        await asyncio.sleep(settings.ccw_bind_poll_interval_seconds)


def extract_creation_refs(text: str, limit: int = 3) -> list[dict[str, str]]:
    refs: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for match in CCW_URL_PATTERN.finditer(text or ""):
        raw_url = match.group(0).rstrip(TRAILING_URL_PUNCTUATION)
        parsed = urlparse(raw_url)
        oid = parsed.path.rstrip("/").split("/")[-1].lower()
        if not OID_PATTERN.fullmatch(oid):
            continue
        access_key = (parse_qs(parsed.query).get("accessKey") or [""])[0].strip()
        key = (oid, access_key)
        if key in seen:
            continue
        seen.add(key)
        refs.append({"oid": oid, "access_key": access_key})
        if len(refs) >= limit:
            break
    return refs


def build_preview(oid: str, payload: dict[str, Any], access_key: str = "") -> dict[str, Any]:
    body = payload.get("body")
    if not isinstance(body, dict):
        raise ValueError("Missing CCW creation body")
    release = body.get("creationRelease")
    if not isinstance(release, dict):
        release = {}
    student = body.get("student")
    if not isinstance(student, dict):
        student = {}

    cover_url = _text(body.get("featuredCoverLink")) or _text(body.get("latestCoverLink")) or _text(release.get("coverLink")) or _text(release.get("coverGifLink"))
    normalized_oid = (_text(body.get("oid")) or oid).lower()
    url = f"https://www.ccw.site/detail/{normalized_oid}"
    if access_key:
        url = f"{url}?accessKey={access_key}"
    return {
        "oid": normalized_oid,
        "access_key": access_key or None,
        "url": url,
        "title": _text(body.get("title")) or "未命名作品",
        "description": _text(body.get("description")) or _text(release.get("description")),
        "cover_url": cover_url,
        "author_name": _text(student.get("name")) or "CCW 用户",
        "author_avatar_url": _text(student.get("avatar")) or _text(student.get("picUrl")),
        "author_oid": _text(body.get("studentOid")) or _text(student.get("oid")),
        "view_count": _number(body.get("viewCount")),
        "like_count": _number(body.get("likeCount")),
        "favorite_count": _number(body.get("favoriteCount")),
        "comment_count": _number(body.get("commentCount")),
        "version": _text(release.get("customVersion")) or _text(release.get("version")),
        "status": _text(body.get("status")),
        "screen_mode": _text(body.get("screenMode")),
        "keyboard_layout": _text(body.get("keyboardLayout")) or _text(release.get("keyboardLayout")),
        "updated_at": _number(body.get("updatedAt")),
        "tags": [tag for tag in body.get("tags", []) if isinstance(tag, str)][:8],
    }


async def fetch_creation_preview(oid: str, access_key: str = "") -> dict[str, Any]:
    normalized_oid = oid.lower()
    normalized_access_key = access_key.strip()
    cached = response_cache.get_ccw_creation(_cache_key(normalized_oid, normalized_access_key))
    if cached is not None:
        return cached
    payload = await get_ccw_gateway().creation_detail(normalized_oid, normalized_access_key)
    preview = build_preview(normalized_oid, payload, normalized_access_key)
    response_cache.set_ccw_creation(_cache_key(normalized_oid, normalized_access_key), preview)
    return preview


async def previews_from_text(text: str, limit: int = 3) -> list[dict[str, Any]]:
    # At most three independent eight-second lookups; gather preserves URL order.
    refs = extract_creation_refs(text, limit=min(3, limit)) if limit > 0 else []
    if not refs:
        return []
    if not ccw_gateway_available():
        # 没有接入 CCW：不做卡片，也不为每条带链接的消息刷一遍日志
        return []

    async def resolve(ref: dict[str, str]) -> dict[str, Any] | None:
        try:
            return await fetch_creation_preview(ref["oid"], ref.get("access_key", ""))
        except FETCH_ERRORS as exc:
            logger.warning("Failed to resolve CCW creation preview oid=%s access_key=%s: %s", ref["oid"], bool(ref.get("access_key")), exc)
            return None

    results = await asyncio.gather(*(resolve(ref) for ref in refs))
    return [preview for preview in results if preview is not None]


async def enrich_metadata_with_previews(content: str, metadata: dict[str, Any] | None, limit: int = 3) -> dict[str, Any] | None:
    previews = await previews_from_text(content, limit=limit)
    if not previews:
        return metadata
    merged = dict(metadata or {})
    merged["ccw_creations"] = previews
    return merged
