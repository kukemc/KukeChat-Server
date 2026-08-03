from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import re
import secrets
import ssl
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.models.user import CcwBindingChallenge, User
from app.services import response_cache


DETAIL_URL = "https://community-web.ccw.site/creation/detail"
CCW_BASE_URL = "https://community-web.ccw.site"
COMMENT_PAGE_URL = f"{CCW_BASE_URL}/comment/page_by_topic?offset=0&page=1&perPage=20&sortField=createdAt&sortType=DESC"
STUDENT_PROFILE_URL = f"{CCW_BASE_URL}/students/profile"
STUDENT_DETAIL_URL = f"{CCW_BASE_URL}/creation/student/detail"
OID_PATTERN = re.compile(r"^[0-9a-fA-F]{24}$")
CCW_URL_PATTERN = re.compile(r"https?://(?:www\.)?ccw\.site/detail/[0-9a-fA-F]{24}[^\s<>'\"]*", re.IGNORECASE)
TRAILING_URL_PUNCTUATION = "，。！？；：,.!?;:)]}）】》"
logger = logging.getLogger(__name__)
UTC = timezone.utc
CHALLENGE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_ccw_binding_task: asyncio.Task | None = None
CA_FILE_CANDIDATES = (
    "/etc/ssl/certs/ca-certificates.crt",
    "/etc/pki/tls/certs/ca-bundle.crt",
    "/etc/ssl/ca-bundle.pem",
)


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _number(value: Any) -> int | float | None:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _cache_key(oid: str, access_key: str = "") -> str:
    return f"{oid.lower()}:{access_key.strip()}"


def _ssl_context() -> ssl.SSLContext | None:
    for cafile in CA_FILE_CANDIDATES:
        try:
            return ssl.create_default_context(cafile=cafile)
        except FileNotFoundError:
            continue
    return None


def ccw_profile_url(student_oid: str) -> str:
    return f"https://www.ccw.site/student/{student_oid}"


def ccw_binding_configured() -> bool:
    """CCW 绑定是否已配置。

    需要运营方自己的作品 oid 与访客标识，本项目不内置任何默认值，
    未配置时该功能整体不可用（而不是拿别人的标识去请求 CCW）。
    """
    return bool((settings.ccw_bind_subject_oid or "").strip() and (settings.ccw_bind_guest_id or "").strip())


def ccw_bind_creation_url() -> str:
    return f"https://www.ccw.site/detail/{settings.ccw_bind_subject_oid}"


def _post_json_sync(url: str, payload: dict[str, Any] | None, headers: dict[str, str] | None = None) -> dict[str, Any]:
    data = None if payload is None else json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    request = Request(
        url,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json;charset=UTF-8",
            "Accept": "application/json, text/plain, */*",
            "Guest-Id": settings.ccw_bind_guest_id,
            "Origin": "https://www.ccw.site",
            "Referer": ccw_bind_creation_url(),
            "User-Agent": "Mozilla/5.0 KukeChat/1.0 ccw-binding",
            **(headers or {}),
        },
    )
    with urlopen(request, timeout=10, context=_ssl_context()) as response:
        raw = response.read()
    parsed = json.loads(raw.decode("utf-8"))
    if not isinstance(parsed, dict):
        raise ValueError("Invalid CCW response")
    return parsed


def _derive_token(health: dict[str, Any]) -> str:
    body = health.get("body")
    if not isinstance(body, list):
        raise ValueError("Invalid CCW health response")
    chars: list[str] = []
    for item in body:
        if not isinstance(item, dict) or not isinstance(item.get("traceId"), str):
            continue
        trace_id = item["traceId"]
        if not trace_id:
            continue
        index = int(trace_id[0], 16) + 1
        if index < len(trace_id):
            chars.append(trace_id[index])
    return "".join(reversed(chars))


def _fetch_comment_page_sync() -> list[dict[str, Any]]:
    body = {
        "subjectOid": settings.ccw_bind_subject_oid,
        "subjectType": settings.ccw_bind_subject_type,
        "sectionType": "COMMENT",
    }
    health = _post_json_sync(f"{CCW_BASE_URL}/health/check", None)
    token = _derive_token(health)
    date_time = _post_json_sync(f"{CCW_BASE_URL}/base/dateTime", None)
    b = str(date_time.get("body") or int(datetime.now(UTC).timestamp() * 1000))
    raw_body = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
    a = hmac.new(token.encode("utf-8"), f"ccw{raw_body}{b}".encode("utf-8"), hashlib.md5).hexdigest()
    payload = _post_json_sync(COMMENT_PAGE_URL, body, {"a": a, "b": b})
    data = payload.get("body", {}).get("data") if isinstance(payload.get("body"), dict) else None
    return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []


def _fetch_student_payloads_sync(student_oid: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    payload = {"studentOid": student_oid}
    profile = _post_json_sync(STUDENT_PROFILE_URL, payload)
    detail = _post_json_sync(STUDENT_DETAIL_URL, payload)
    return profile, detail


async def fetch_comment_page() -> list[dict[str, Any]]:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _fetch_comment_page_sync)


async def fetch_student_payloads(student_oid: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _fetch_student_payloads_sync, student_oid)


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
    try:
        profile_payload, detail_payload = await fetch_student_payloads(user.ccw_student_oid)
        data = normalize_student_profile(profile_payload, detail_payload, {"accountOid": user.ccw_student_oid, "name": user.ccw_name, "avatar": user.ccw_avatar_url})
    except (HTTPError, URLError, TimeoutError, ValueError, json.JSONDecodeError, OSError) as exc:
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
    query = select(CcwBindingChallenge).where(CcwBindingChallenge.status == "pending", CcwBindingChallenge.expires_at > now)
    if user_id is not None:
        query = query.where(CcwBindingChallenge.user_id == user_id)
    result = await db.execute(query.order_by(CcwBindingChallenge.created_at.asc()))
    challenges = list(result.scalars().all())
    if not challenges:
        return 0
    comments = await fetch_comment_page()
    matched = 0
    for challenge in challenges:
        if await _match_challenge(db, challenge, comments):
            matched += 1
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
    challenge.last_checked_at = now
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
        try:
            profile_payload, detail_payload = await fetch_student_payloads(student_oid.lower())
            data = normalize_student_profile(profile_payload, detail_payload, commenter)
        except (HTTPError, URLError, TimeoutError, ValueError, json.JSONDecodeError, OSError):
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


def _fetch_creation_detail_sync(oid: str, access_key: str = "") -> dict[str, Any]:
    payload = json.dumps({"oid": oid, "accessKey": access_key}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    request = Request(
        DETAIL_URL,
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json;charset=UTF-8",
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 KukeChat/1.0 link-preview",
            "Origin": "https://www.ccw.site",
            "Referer": f"https://www.ccw.site/detail/{oid}",
        },
    )
    with urlopen(request, timeout=8, context=_ssl_context()) as response:
        raw = response.read()
    parsed = json.loads(raw.decode("utf-8"))
    if not isinstance(parsed, dict):
        raise ValueError("Invalid CCW response")
    return parsed


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
    loop = asyncio.get_running_loop()
    payload = await loop.run_in_executor(None, _fetch_creation_detail_sync, normalized_oid, normalized_access_key)
    preview = build_preview(normalized_oid, payload, normalized_access_key)
    response_cache.set_ccw_creation(_cache_key(normalized_oid, normalized_access_key), preview)
    return preview


async def previews_from_text(text: str, limit: int = 3) -> list[dict[str, Any]]:
    previews: list[dict[str, Any]] = []
    for ref in extract_creation_refs(text, limit=limit):
        try:
            previews.append(await fetch_creation_preview(ref["oid"], ref.get("access_key", "")))
        except (HTTPError, URLError, TimeoutError, ValueError, json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to resolve CCW creation preview oid=%s access_key=%s: %s", ref["oid"], bool(ref.get("access_key")), exc)
            continue
    return previews


async def enrich_metadata_with_previews(content: str, metadata: dict[str, Any] | None, limit: int = 3) -> dict[str, Any] | None:
    previews = await previews_from_text(content, limit=limit)
    if not previews:
        return metadata
    merged = dict(metadata or {})
    merged["ccw_creations"] = previews
    return merged
