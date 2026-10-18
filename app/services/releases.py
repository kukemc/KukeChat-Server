"""发版服务（docs/releases.md）：版本、安装包（上传到我们的存储或外部链接）、更新检查、发版令牌。

上传的安装包放在主桶 releases/ 前缀下（公开读），下载直接走桶 / CDN，不经过应用服务器。
直传流程和群文件一样：开预签名 PUT → 客户端自己传 → complete 时 head_object 核对大小。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import secrets
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath
from typing import Any

from fastapi import HTTPException, Request, UploadFile
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import release_connection
from app.models.enums import PlatformRole, UserStatus
from app.models.release import AppRelease, AppReleaseArtifact, ReleaseApiToken
from app.models.user import User
from app.services import storage
from app.services.reports import log_admin_action

logger = logging.getLogger(__name__)

PLATFORMS = ("android", "windows", "macos", "linux")
CHANNELS = ("stable", "beta")
UPLOAD_URL_TTL_SECONDS = 2 * 60 * 60
PENDING_TTL = timedelta(hours=24)
LATEST_CACHE_SECONDS = 30
TOKEN_PREFIX = "kcr_"
KEY_PREFIX = "releases"
# 发布后可改的字段；其它字段只有草稿能改
PUBLISHED_EDITABLE = frozenset({"changelog", "min_supported_version"})

_STRICT_VERSION = re.compile(r"^(\d{1,6})\.(\d{1,6})\.(\d{1,6})(?:-([0-9A-Za-z][0-9A-Za-z.-]{0,40}))?$")
_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")
_ARCH = re.compile(r"^[a-z0-9_]{1,16}$")
_FILENAME_BAD = re.compile(r"[\x00-\x1f\x7f​-‏ -‮﻿]")


def _now() -> datetime:
    return datetime.now(UTC)


def _error(status_code: int, code: str, message: str, **extra: Any) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message, **extra})


# ------------------------------------------------------------------ 版本号


VersionKey = tuple[int, int, int, int, tuple[tuple[int, Any], ...]]


def _pre_key(pre: str | None) -> tuple[int, tuple[tuple[int, Any], ...]]:
    if not pre:
        return 1, ()  # 正式版排在同号的预发布之后
    parts: list[tuple[int, Any]] = []
    for part in pre.split("."):
        parts.append((0, int(part)) if part.isdigit() else (1, part))
    return 0, tuple(parts)


def normalize_version(value: str | None) -> str:
    """发版用的严格版本号 x.y.z(-pre)，不合法 422。"""
    text = (value or "").strip()
    if text[:1] in {"v", "V"}:
        text = text[1:]
    if not _STRICT_VERSION.match(text):
        raise _error(422, "invalid_version", "版本号要写成 x.y.z（可带 -beta.1 这样的后缀）")
    return text


def version_key(value: str) -> VersionKey:
    """严格或宽松版本号的排序键。宽松：允许 v 前缀、1~4 段数字、+build 元数据（客户端上报的 current 用）。"""
    text = value.strip()
    if text[:1] in {"v", "V"}:
        text = text[1:]
    text = text.split("+", 1)[0]
    core, _, pre = text.partition("-")
    numbers = core.split(".")
    if not 1 <= len(numbers) <= 4 or not all(part.isdigit() and len(part) <= 9 for part in numbers):
        raise _error(422, "invalid_version", "无法识别的版本号")
    if pre and not re.fullmatch(r"[0-9A-Za-z.-]{1,64}", pre):
        raise _error(422, "invalid_version", "无法识别的版本号")
    padded = [int(part) for part in numbers[:3]] + [0] * (3 - min(3, len(numbers)))
    released, pre_parts = _pre_key(pre or None)
    return padded[0], padded[1], padded[2], released, pre_parts


def compare_versions(left: str, right: str) -> int:
    a, b = version_key(left), version_key(right)
    return (a > b) - (a < b)


def _release_key(release: AppRelease) -> tuple[VersionKey, int]:
    return version_key(release.version), release.build or 0


# ------------------------------------------------------------------ 校验 / 清洗


def _platform(value: str | None) -> str:
    text = (value or "").strip().lower()
    if text not in PLATFORMS:
        raise _error(422, "invalid_platform", f"平台只能是 {' / '.join(PLATFORMS)}")
    return text


def _channel(value: str | None) -> str:
    text = (value or "stable").strip().lower()
    if text not in CHANNELS:
        raise _error(422, "invalid_channel", f"渠道只能是 {' / '.join(CHANNELS)}")
    return text


def _channels_for(channel: str) -> tuple[str, ...]:
    return ("stable", "beta") if channel == "beta" else ("stable",)


def _sha256(value: str | None, *, required: bool) -> str | None:
    text = (value or "").strip()
    if not text:
        if required:
            raise _error(422, "invalid_sha256", "需要安装包的 sha256")
        return None
    if not _SHA256.match(text):
        raise _error(422, "invalid_sha256", "sha256 要是 64 位十六进制")
    return text.lower()


def _arch(value: str | None) -> str | None:
    text = (value or "").strip().lower()
    if not text:
        return None
    if not _ARCH.match(text):
        raise _error(422, "invalid_arch", "架构只能是小写字母、数字和下划线，例如 x64、arm64")
    return text


def _url(value: str | None) -> str:
    text = (value or "").strip()
    if not re.match(r"^https?://[^\s/]+", text, re.IGNORECASE) or len(text) > 1000 or any(ch.isspace() for ch in text):
        raise _error(422, "invalid_url", "链接要以 http:// 或 https:// 开头")
    return text


def clean_filename(raw: str | None) -> str:
    name = (raw or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = _FILENAME_BAD.sub("", name).strip().strip(".").strip()
    name = re.sub(r"\s+", " ", name)
    if not name:
        return "package"
    if len(name) > 120:
        suffix = PurePosixPath(name).suffix[:20]
        name = name[: 120 - len(suffix)] + suffix
    return name


def _size(value: Any, *, required: bool) -> int | None:
    if value is None or value == "":
        if required:
            raise _error(422, "empty_file", "需要安装包大小")
        return None
    try:
        size = int(value)
    except (TypeError, ValueError) as exc:
        raise _error(422, "empty_file", "安装包大小不对") from exc
    if size <= 0:
        raise _error(422, "empty_file", "安装包是空的")
    if size > settings.release_max_artifact_bytes:
        raise _error(413, "file_too_large", "安装包太大了", max_bytes=settings.release_max_artifact_bytes)
    return size


def direct_upload_enabled() -> bool:
    return settings.storage_backend == "s3"


def _changelog(value: str | None) -> str:
    text = (value or "").replace("\r\n", "\n").strip()
    if len(text) > 20000:
        raise _error(422, "changelog_too_long", "更新日志最多 20000 字")
    return text


def _min_supported(value: str | None) -> str | None:
    text = (value or "").strip()
    return normalize_version(text) if text else None


# ------------------------------------------------------------------ 视图


def artifact_url(artifact: AppReleaseArtifact, request: Request | None = None) -> str | None:
    if artifact.source == "external":
        return artifact.external_url
    if not artifact.storage_key:
        return None
    try:
        return storage.public_url_for_key(artifact.storage_key, request)
    except HTTPException:
        return None


def _public_artifact(artifact: AppReleaseArtifact, request: Request | None) -> dict[str, Any]:
    return {
        "id": artifact.id,
        "source": artifact.source,
        "file_name": artifact.file_name,
        "url": artifact_url(artifact, request),
        "size": artifact.size,
        "sha256": artifact.sha256,
        "arch": artifact.arch,
        "signature": artifact.signature,
    }


def _ready(release: AppRelease) -> list[AppReleaseArtifact]:
    return [artifact for artifact in release.artifacts if artifact.status == "ready"]


def pick_artifact(artifacts: list[AppReleaseArtifact], arch: str | None) -> AppReleaseArtifact | None:
    if not artifacts:
        return None
    if arch:
        for artifact in artifacts:
            if artifact.arch == arch:
                return artifact
    for artifact in artifacts:
        if not artifact.arch:
            return artifact
    return artifacts[0]


def public_view(release: AppRelease, request: Request | None = None, arch: str | None = None) -> dict[str, Any]:
    ready = _ready(release)
    chosen = pick_artifact(ready, arch)
    return {
        "id": release.id,
        "platform": release.platform,
        "channel": release.channel,
        "version": release.version,
        "build": release.build,
        "changelog": release.changelog,
        "min_supported_version": release.min_supported_version,
        "published_at": release.published_at,
        "artifacts": [_public_artifact(artifact, request) for artifact in ready],
        "artifact": _public_artifact(chosen, request) if chosen else None,
    }


def _user_brief(user: User | None) -> dict[str, Any] | None:
    if user is None:
        return None
    return {"id": user.id, "username": user.username, "nickname": user.nickname}


def admin_view(release: AppRelease, request: Request | None = None) -> dict[str, Any]:
    return {
        "id": release.id,
        "platform": release.platform,
        "channel": release.channel,
        "version": release.version,
        "build": release.build,
        "changelog": release.changelog,
        "min_supported_version": release.min_supported_version,
        "published": release.published,
        "published_at": release.published_at,
        "created_at": release.created_at,
        "updated_at": release.updated_at,
        "created_by": _user_brief(release.created_by),
        "artifacts": [
            {
                **_public_artifact(artifact, request),
                "status": artifact.status,
                "storage_key": artifact.storage_key,
                "created_at": artifact.created_at,
            }
            for artifact in release.artifacts
        ],
    }


# ------------------------------------------------------------------ 公开查询


_latest_cache: dict[tuple[str, str, str], tuple[float, int | None]] = {}


def invalidate_cache() -> None:
    _latest_cache.clear()


async def _published(db: AsyncSession, platform: str, channel: str) -> list[AppRelease]:
    rows = (
        await db.execute(
            select(AppRelease).where(
                AppRelease.platform == platform,
                AppRelease.channel.in_(_channels_for(channel)),
                AppRelease.published.is_(True),
            )
        )
    ).scalars().all()
    return sorted(rows, key=_release_key, reverse=True)


async def latest_published(db: AsyncSession, platform: str, channel: str = "stable", *, require_artifact: bool = True) -> AppRelease | None:
    """最新的已发布版本（按版本号，不按发布时间）。缓存的是 id，对象每次重新读，这样 URL 里的域名跟着请求走。"""
    cache_key = (platform, channel, "a" if require_artifact else "-")
    cached = _latest_cache.get(cache_key)
    if cached and cached[0] > time.monotonic():
        return await db.get(AppRelease, cached[1]) if cached[1] is not None else None
    found = None
    for release in await _published(db, platform, channel):
        if not require_artifact or _ready(release):
            found = release
            break
    _latest_cache[cache_key] = (time.monotonic() + LATEST_CACHE_SECONDS, found.id if found else None)
    return found


async def check_latest(
    db: AsyncSession, request: Request | None, platform: str, channel: str | None, current: str | None, arch: str | None
) -> dict[str, Any]:
    platform = _platform(platform)
    channel = _channel(channel)
    current_text = (current or "").strip() or None
    if current_text:
        version_key(current_text)  # 不合法直接 422
    arch_text = _arch(arch)
    release = await latest_published(db, platform, channel)
    update_available = False
    forced = False
    if release is not None:
        if current_text is None:
            update_available = True
        else:
            update_available = compare_versions(release.version, current_text) > 0
            forced = bool(
                update_available and release.min_supported_version and compare_versions(current_text, release.min_supported_version) < 0
            )
    return {
        "platform": platform,
        "channel": channel,
        "current": current_text,
        "update_available": update_available,
        "forced": forced,
        "latest": public_view(release, request, arch_text) if release else None,
    }


async def history(db: AsyncSession, request: Request | None, platform: str, channel: str | None, limit: int, offset: int) -> dict[str, Any]:
    rows = await _published(db, _platform(platform), _channel(channel))
    return {"items": [public_view(row, request) for row in rows[offset : offset + limit]], "total": len(rows)}


async def legacy_overlay(db: AsyncSession, request: Request | None = None) -> dict[str, tuple[str, str]]:
    """给 /desktop-update.json 和 /download 用：各平台最新的 stable 版本 → (版本号, 安装包地址)。没有就不出现在结果里。"""
    found: dict[str, tuple[str, str]] = {}
    # macOS 不带 arch：挑到的是 dmg（带 universal 的是给自动更新用的 .app.tar.gz）
    for platform, arch in (("windows", "x64"), ("android", None), ("macos", None)):
        try:
            release = await latest_published(db, platform, "stable")
        except Exception:  # 发版表出问题不能拖垮旧接口
            logger.exception("Release lookup failed for %s", platform)
            continue
        if release is None:
            continue
        ready = _ready(release)
        if platform == "macos":
            # 下载页只给 dmg；没有 dmg 时不拿自动更新包顶上
            ready = [item for item in ready if item.file_name.lower().endswith(".dmg")]
        artifact = pick_artifact(ready, arch)
        url = artifact_url(artifact, request) if artifact else None
        if url:
            found[platform] = (release.version, url)
    return found


# ------------------------------------------------------------------ 后台：版本


async def _audit(db: AsyncSession, actor: "ReleaseActor", action: str, release_id: int | None, details: dict[str, Any]) -> None:
    if actor.token_id is not None:
        details = {**details, "via_token": actor.token_id}
    await log_admin_action(db, actor.user.id, action, "app_release", release_id, details)


async def _get(db: AsyncSession, release_id: int) -> AppRelease:
    release = await db.get(AppRelease, release_id)
    if release is None:
        raise _error(404, "release_not_found", "版本不存在")
    await _sweep_pending(db, release)
    return release


async def _reload(db: AsyncSession, release_id: int) -> AppRelease:
    db.expire_all()
    release = (await db.execute(select(AppRelease).where(AppRelease.id == release_id))).scalar_one()
    return release


async def _sweep_pending(db: AsyncSession, release: AppRelease) -> None:
    cutoff = _now() - PENDING_TTL
    stale = [a for a in release.artifacts if a.status == "pending" and _aware(a.created_at) < cutoff]
    if not stale:
        return
    keys = [a.storage_key for a in stale if a.storage_key]
    for artifact in stale:
        release.artifacts.remove(artifact)
    await db.commit()
    if keys:
        await asyncio.to_thread(storage.delete_objects, keys)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


async def _exists(db: AsyncSession, platform: str, channel: str, version: str, exclude_id: int | None = None) -> bool:
    query = select(AppRelease.id).where(AppRelease.platform == platform, AppRelease.channel == channel, AppRelease.version == version)
    if exclude_id is not None:
        query = query.where(AppRelease.id != exclude_id)
    return (await db.execute(query)).first() is not None


async def admin_list(
    db: AsyncSession, request: Request | None, platform: str | None, channel: str | None, state: str | None, limit: int, offset: int
) -> dict[str, Any]:
    query = select(AppRelease)
    if platform:
        query = query.where(AppRelease.platform == _platform(platform))
    if channel:
        query = query.where(AppRelease.channel == _channel(channel))
    if state == "draft":
        query = query.where(AppRelease.published.is_(False))
    elif state == "published":
        query = query.where(AppRelease.published.is_(True))
    rows = sorted((await db.execute(query)).scalars().all(), key=lambda r: (r.platform, _release_key(r)), reverse=True)
    if not platform:
        rows.sort(key=lambda r: _aware(r.created_at), reverse=True)
    return {"items": [admin_view(row, request) for row in rows[offset : offset + limit]], "total": len(rows)}


async def admin_get(db: AsyncSession, request: Request | None, release_id: int) -> dict[str, Any]:
    return admin_view(await _get(db, release_id), request)


def _build(value: Any) -> int:
    try:
        build = int(value or 0)
    except (TypeError, ValueError) as exc:
        raise _error(422, "invalid_build", "构建号要是整数") from exc
    if not 0 <= build <= 2_100_000_000:
        raise _error(422, "invalid_build", "构建号要在 0 到 2100000000 之间")
    return build


async def create_release(db: AsyncSession, request: Request | None, actor: "ReleaseActor", payload: dict[str, Any]) -> dict[str, Any]:
    platform = _platform(payload.get("platform"))
    channel = _channel(payload.get("channel"))
    version = normalize_version(payload.get("version"))
    if await _exists(db, platform, channel, version):
        raise _error(409, "release_exists", f"{platform} {channel} 已经有 {version} 了")
    release = AppRelease(
        platform=platform,
        channel=channel,
        version=version,
        build=_build(payload.get("build")),
        changelog=_changelog(payload.get("changelog")),
        min_supported_version=_min_supported(payload.get("min_supported_version")),
        published=False,
        created_by_id=actor.user.id,
    )
    db.add(release)
    await db.flush()
    await _audit(db, actor, "release.create", release.id, {"platform": platform, "channel": channel, "version": version, "build": release.build})
    await db.commit()
    return admin_view(await _reload(db, release.id), request)


async def update_release(db: AsyncSession, request: Request | None, actor: "ReleaseActor", release_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    release = await _get(db, release_id)
    if release.published:
        locked = [key for key in payload if key not in PUBLISHED_EDITABLE]
        if locked:
            raise _error(409, "release_published", "已发布的版本只能改更新日志和最低支持版本，其它要先撤回", fields=locked)
    changes: dict[str, Any] = {}
    if "platform" in payload:
        changes["platform"] = _platform(payload["platform"])
    if "channel" in payload:
        changes["channel"] = _channel(payload["channel"])
    if "version" in payload:
        changes["version"] = normalize_version(payload["version"])
    if "build" in payload:
        changes["build"] = _build(payload["build"])
    if "changelog" in payload:
        changes["changelog"] = _changelog(payload["changelog"])
    if "min_supported_version" in payload:
        changes["min_supported_version"] = _min_supported(payload["min_supported_version"])
    target = (changes.get("platform", release.platform), changes.get("channel", release.channel), changes.get("version", release.version))
    if target != (release.platform, release.channel, release.version) and await _exists(db, *target, exclude_id=release.id):
        raise _error(409, "release_exists", f"{target[0]} {target[1]} 已经有 {target[2]} 了")
    before = {key: getattr(release, key) for key in changes}
    for key, value in changes.items():
        setattr(release, key, value)
    await _audit(
        db, actor, "release.update", release.id,
        {"version": release.version, "changed": sorted(changes), "before": {k: v for k, v in before.items() if k != "changelog"}},
    )
    await db.commit()
    if release.published:
        invalidate_cache()
    return admin_view(await _reload(db, release.id), request)


async def delete_release(db: AsyncSession, actor: "ReleaseActor", release_id: int) -> None:
    release = await _get(db, release_id)
    if release.published:
        raise _error(409, "release_published", "已发布的版本要先撤回才能删")
    keys = [a.storage_key for a in release.artifacts if a.storage_key]
    details = {"platform": release.platform, "channel": release.channel, "version": release.version}
    await db.delete(release)
    await _audit(db, actor, "release.delete", release_id, details)
    await db.commit()
    if keys:
        await asyncio.to_thread(storage.delete_objects, keys)


async def publish_release(db: AsyncSession, request: Request | None, actor: "ReleaseActor", release_id: int, force: bool = False) -> dict[str, Any]:
    release = await _get(db, release_id)
    if not _ready(release):
        raise _error(409, "no_artifacts", "这个版本还没有可用的安装包")
    if not release.published:
        newest = next((row for row in await _published(db, release.platform, release.channel) if row.channel == release.channel), None)
        if newest is not None and _release_key(release) <= _release_key(newest) and not force:
            raise _error(409, "version_not_newer", f"已发布的最新版本是 {newest.version}（构建 {newest.build}），这个版本不比它新", latest_version=newest.version)
        release.published = True
        release.published_at = _now()
        await _audit(db, actor, "release.publish", release.id, {"platform": release.platform, "channel": release.channel, "version": release.version, "force": force})
        await db.commit()
        invalidate_cache()
        await _notify_published(release)
    return admin_view(await _reload(db, release.id), request)


def published_event(release: AppRelease) -> dict[str, Any]:
    """`release.published` 实时事件的内容。forced 只表示这个版本设了最低支持版本；
    具体某个客户端是不是被强制更新，要它自己带 current 去问 /releases/latest。"""
    return {
        "platform": release.platform,
        "channel": release.channel,
        "version": release.version,
        "build": release.build,
        "min_supported_version": release.min_supported_version,
        "forced": bool(release.min_supported_version),
    }


async def _notify_published(release: AppRelease) -> None:
    """发布成功（已提交）后通知所有在线用户去查一次更新。推送失败只记日志，不影响发布本身。"""
    try:
        from app.websocket.manager import manager

        await manager.broadcast("release.published", published_event(release))
    except Exception:
        logger.exception("Release publish broadcast failed for release %s", release.id)


async def unpublish_release(db: AsyncSession, request: Request | None, actor: "ReleaseActor", release_id: int) -> dict[str, Any]:
    release = await _get(db, release_id)
    if release.published:
        release.published = False
        release.published_at = None
        await _audit(db, actor, "release.unpublish", release.id, {"platform": release.platform, "channel": release.channel, "version": release.version})
        await db.commit()
        invalidate_cache()
    return admin_view(await _reload(db, release.id), request)


# ------------------------------------------------------------------ 后台：安装包


def _storage_key(release: AppRelease, file_name: str) -> str:
    return "/".join((KEY_PREFIX, release.platform, release.channel, release.version, secrets.token_hex(5), file_name))


def _artifact(release: AppRelease, artifact_id: int) -> AppReleaseArtifact:
    for artifact in release.artifacts:
        if artifact.id == artifact_id:
            return artifact
    raise _error(404, "artifact_not_found", "安装包不存在")


def _ensure_name_free(release: AppRelease, file_name: str) -> None:
    """同名的 ready 包 → 409。只读，不改 release.artifacts。"""
    for artifact in release.artifacts:
        if artifact.file_name == file_name and artifact.status == "ready":
            raise _error(409, "artifact_exists", f"这个版本里已经有 {file_name} 了，先删掉旧的")


async def _claim_name(db: AsyncSession, release: AppRelease, file_name: str) -> list[str]:
    """同名的 ready 包 → 409；同名的 pending 直传单 → 让位给新的。返回要删的对象 key。"""
    doomed: list[str] = []
    for artifact in list(release.artifacts):
        if artifact.file_name != file_name:
            continue
        if artifact.status == "ready":
            raise _error(409, "artifact_exists", f"这个版本里已经有 {file_name} 了，先删掉旧的")
        if artifact.storage_key:
            doomed.append(artifact.storage_key)
        release.artifacts.remove(artifact)
    if doomed:
        await db.flush()
    return doomed


async def create_upload(db: AsyncSession, request: Request | None, actor: "ReleaseActor", release_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    release = await _get(db, release_id)
    if not direct_upload_enabled():
        raise _error(409, "direct_upload_unavailable", "服务器没开直传，请用中转上传")
    file_name = clean_filename(payload.get("file_name"))
    size = _size(payload.get("size"), required=True)
    sha = _sha256(payload.get("sha256"), required=True)
    arch = _arch(payload.get("arch"))
    signature = (payload.get("signature") or "").strip() or None
    doomed = await _claim_name(db, release, file_name)
    key = _storage_key(release, file_name)
    url, headers = storage.presign_put(key, size, UPLOAD_URL_TTL_SECONDS)
    artifact = AppReleaseArtifact(
        source="upload", status="pending", file_name=file_name, storage_key=key, size=size, sha256=sha, arch=arch, signature=signature
    )
    release.artifacts.append(artifact)
    await db.flush()
    await _audit(db, actor, "release.artifact.upload_ticket", release.id, {"version": release.version, "file_name": file_name, "size": size})
    await db.commit()
    if doomed:
        await asyncio.to_thread(storage.delete_objects, doomed)
    view = admin_view(await _reload(db, release.id), request)
    return {
        "artifact": next(item for item in view["artifacts"] if item["id"] == artifact.id),
        "upload": {"method": "PUT", "url": url, "headers": headers, "expires_at": _now() + timedelta(seconds=UPLOAD_URL_TTL_SECONDS)},
    }


async def complete_upload(db: AsyncSession, request: Request | None, actor: "ReleaseActor", release_id: int, artifact_id: int) -> dict[str, Any]:
    release = await _get(db, release_id)
    artifact = _artifact(release, artifact_id)
    if artifact.status == "ready":
        return admin_view(release, request)
    key = artifact.storage_key or ""
    # 去对象存储核对（网络）之前先结束读事务、把连接还回去
    await release_connection(db)
    stored = await asyncio.to_thread(storage.object_size, key)
    if stored is None:
        raise _error(409, "upload_incomplete", "还没在存储里找到这个文件，传完再点完成")
    if stored != artifact.size:
        release.artifacts.remove(artifact)
        await db.commit()
        await asyncio.to_thread(storage.delete_objects, [key])
        raise _error(422, "size_mismatch", f"传上去的文件大小（{stored}）和声明的（{artifact.size}）不一样，已删除，请重传")
    artifact.status = "ready"
    await _audit(db, actor, "release.artifact.add", release.id, {"version": release.version, "file_name": artifact.file_name, "size": stored, "source": "upload"})
    await db.commit()
    if release.published:
        invalidate_cache()
    return admin_view(await _reload(db, release.id), request)


async def _spool(upload: UploadFile) -> tuple[Any, int, str]:
    spool = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024)
    digest = hashlib.sha256()
    size = 0
    try:
        while True:
            chunk = await upload.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > settings.release_max_artifact_bytes:
                raise _error(413, "file_too_large", "安装包太大了", max_bytes=settings.release_max_artifact_bytes)
            digest.update(chunk)
            spool.write(chunk)
    except BaseException:
        spool.close()
        raise
    spool.seek(0)
    return spool, size, digest.hexdigest()


async def upload_through_server(
    db: AsyncSession, request: Request | None, actor: "ReleaseActor", release_id: int, upload: UploadFile,
    sha256: str | None, arch: str | None, signature: str | None,
) -> dict[str, Any]:
    release = await _get(db, release_id)
    expected = _sha256(sha256, required=False)
    arch_value = _arch(arch)
    file_name = clean_filename(upload.filename)
    # 同名 ready 包先挡掉（只读，不动 release.artifacts）；真正的占名在传完之后的写事务里做
    _ensure_name_free(release, file_name)
    key = _storage_key(release, file_name)
    # 安装包可能上百 MB，传对象存储要很久：传之前先结束读事务、把数据库连接还回去。
    # 以前是先 _claim_name（让位同名的 pending 单会 flush 一笔删除）再上传，整个上传期间都拿着 SQLite 的写锁
    await release_connection(db)
    spool, size, actual = await _spool(upload)
    try:
        if size == 0:
            raise _error(422, "empty_file", "安装包是空的")
        if expected and expected != actual:
            raise _error(422, "sha256_mismatch", "服务器算出来的 sha256 和填的不一样，文件可能传坏了")
        await asyncio.to_thread(storage.put_object, key, spool, storage.OCTET_STREAM)
    finally:
        spool.close()
    try:
        # 上传期间版本可能被改过（别人传了同名包、删了版本）：重新读一遍再占名、记账，一个短事务提交
        release = await db.get(AppRelease, release_id, populate_existing=True)
        if release is None:
            raise _error(404, "release_not_found", "版本不存在")
        doomed = await _claim_name(db, release, file_name)
        release.artifacts.append(
            AppReleaseArtifact(
                source="upload", status="ready", file_name=file_name, storage_key=key, size=size, sha256=actual,
                arch=arch_value, signature=(signature or "").strip() or None,
            )
        )
        await _audit(db, actor, "release.artifact.add", release.id, {"version": release.version, "file_name": file_name, "size": size, "source": "upload"})
        await db.commit()
    except BaseException:
        # 没记上账：刚传上去的对象删掉（key 带随机段，不会误删别人的）
        await db.rollback()
        await asyncio.to_thread(storage.delete_objects, [key])
        raise
    if doomed:
        await asyncio.to_thread(storage.delete_objects, doomed)
    if release.published:
        invalidate_cache()
    return admin_view(await _reload(db, release.id), request)


async def add_external(db: AsyncSession, request: Request | None, actor: "ReleaseActor", release_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    release = await _get(db, release_id)
    url = _url(payload.get("url"))
    file_name = clean_filename(payload.get("file_name") or url.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1])
    size = _size(payload.get("size"), required=False)
    sha = _sha256(payload.get("sha256"), required=False)
    arch = _arch(payload.get("arch"))
    doomed = await _claim_name(db, release, file_name)
    release.artifacts.append(
        AppReleaseArtifact(
            source="external", status="ready", file_name=file_name, external_url=url, size=size, sha256=sha, arch=arch,
            signature=(payload.get("signature") or "").strip() or None,
        )
    )
    await _audit(db, actor, "release.artifact.add", release.id, {"version": release.version, "file_name": file_name, "url": url, "source": "external"})
    await db.commit()
    if doomed:
        await asyncio.to_thread(storage.delete_objects, doomed)
    if release.published:
        invalidate_cache()
    return admin_view(await _reload(db, release.id), request)


async def update_artifact(
    db: AsyncSession, request: Request | None, actor: "ReleaseActor", release_id: int, artifact_id: int, payload: dict[str, Any]
) -> dict[str, Any]:
    release = await _get(db, release_id)
    artifact = _artifact(release, artifact_id)
    if "arch" in payload:
        artifact.arch = _arch(payload["arch"])
    if "signature" in payload:
        artifact.signature = (payload["signature"] or "").strip() or None
    external_fields = {"url", "size", "sha256"} & set(payload)
    if external_fields and artifact.source != "external":
        raise _error(409, "artifact_not_external", "只有外部链接能改地址、大小和 sha256")
    if "url" in payload:
        artifact.external_url = _url(payload["url"])
    if "size" in payload:
        artifact.size = _size(payload["size"], required=False)
    if "sha256" in payload:
        artifact.sha256 = _sha256(payload["sha256"], required=False)
    await _audit(db, actor, "release.artifact.update", release.id, {"version": release.version, "artifact_id": artifact.id, "changed": sorted(payload)})
    await db.commit()
    if release.published:
        invalidate_cache()
    return admin_view(await _reload(db, release.id), request)


async def delete_artifact(db: AsyncSession, request: Request | None, actor: "ReleaseActor", release_id: int, artifact_id: int) -> dict[str, Any]:
    release = await _get(db, release_id)
    artifact = _artifact(release, artifact_id)
    if release.published and artifact.status == "ready" and len(_ready(release)) <= 1:
        raise _error(409, "last_artifact", "这是已发布版本最后一个安装包，要删先撤回版本")
    key = artifact.storage_key
    release.artifacts.remove(artifact)
    await _audit(db, actor, "release.artifact.delete", release.id, {"version": release.version, "file_name": artifact.file_name})
    await db.commit()
    if key:
        await asyncio.to_thread(storage.delete_objects, [key])
    if release.published:
        invalidate_cache()
    return admin_view(await _reload(db, release.id), request)


# ------------------------------------------------------------------ 发版令牌


@dataclass
class ReleaseActor:
    user: User
    token_id: int | None = None


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def authenticate_token(db: AsyncSession, token: str) -> ReleaseActor:
    invalid = _error(401, "invalid_release_token", "发版令牌无效、已过期或已撤销")
    token = token.strip()
    if not token.startswith(TOKEN_PREFIX) or len(token) > 200:
        raise invalid
    row = (await db.execute(select(ReleaseApiToken).where(ReleaseApiToken.token_hash == _hash_token(token)))).scalar_one_or_none()
    now = _now()
    if row is None or row.revoked_at is not None or (row.expires_at is not None and _aware(row.expires_at) <= now):
        raise invalid
    user = row.created_by
    if user is None or user.platform_role != PlatformRole.ADMIN or user.status == UserStatus.DISABLED:
        raise invalid
    if row.last_used_at is None or now - _aware(row.last_used_at) > timedelta(minutes=1):
        row.last_used_at = now
        await db.commit()
    return ReleaseActor(user=user, token_id=row.id)


def _token_view(row: ReleaseApiToken) -> dict[str, Any]:
    return {
        "id": row.id,
        "label": row.label,
        "prefix": row.prefix,
        "created_at": row.created_at,
        "last_used_at": row.last_used_at,
        "expires_at": row.expires_at,
        "revoked_at": row.revoked_at,
        "created_by": _user_brief(row.created_by),
    }


async def list_tokens(db: AsyncSession) -> dict[str, Any]:
    rows = (await db.execute(select(ReleaseApiToken).order_by(ReleaseApiToken.id.desc()))).scalars().all()
    return {"items": [_token_view(row) for row in rows]}


async def create_token(db: AsyncSession, admin: User, label: str | None, expires_in_days: int | None) -> dict[str, Any]:
    text = (label or "").strip()[:80] or "发版脚本"
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    row = ReleaseApiToken(
        label=text,
        token_hash=_hash_token(token),
        prefix=token[:10],
        created_by_id=admin.id,
        expires_at=_now() + timedelta(days=expires_in_days) if expires_in_days else None,
    )
    db.add(row)
    await db.flush()
    await log_admin_action(db, admin.id, "release.token.create", "release_token", row.id, {"label": text, "expires_in_days": expires_in_days})
    await db.commit()
    row = (await db.execute(select(ReleaseApiToken).where(ReleaseApiToken.id == row.id))).scalar_one()
    return {"token": token, "item": _token_view(row)}


async def revoke_token(db: AsyncSession, admin: User, token_id: int) -> None:
    row = await db.get(ReleaseApiToken, token_id)
    if row is None:
        raise _error(404, "token_not_found", "令牌不存在")
    if row.revoked_at is None:
        row.revoked_at = _now()
        await log_admin_action(db, admin.id, "release.token.revoke", "release_token", row.id, {"label": row.label})
        await db.commit()


async def count_published(db: AsyncSession) -> int:
    return int(await db.scalar(select(func.count()).select_from(AppRelease).where(AppRelease.published.is_(True))) or 0)


def config_view() -> dict[str, Any]:
    return {
        "direct_upload": direct_upload_enabled(),
        "max_artifact_bytes": settings.release_max_artifact_bytes,
        "platforms": list(PLATFORMS),
        "channels": list(CHANNELS),
    }

