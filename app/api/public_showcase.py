"""作品页用的公开数据：GET /public/showcase（docs/releases.md 第 6 节）。

给 CCW 作品里的介绍页（WebDev 扩展的舞台 HTML 图层，跑在 sandbox iframe 里，Origin 是 null）读：
主页统计、在线人数、热门公开群、入驻创作者、各平台最新版本与更新日志。
- 不在 /api/v1 下：不走客户端证明头；任何来源都能跨域读（只读、不带凭据，Access-Control-Allow-Origin: *）。
- 只给公开字段（群名 / 头像 / 人数 / 简介，创作者昵称 / 头像 / CCW 名），和主页、群发现页能看到的一样。
- 进程内缓存 60 秒，作品被很多人同时打开也只查一次库。
"""

from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.services import home as home_service
from app.services import releases as release_service
from app.websocket.manager import manager

logger = logging.getLogger(__name__)
router = APIRouter()

CACHE_SECONDS = 60
_cache: dict[str, Any] = {"at": 0.0, "body": None}


def _absolute(url: str | None, request: Request) -> str | None:
    """上传目录里的相对地址（/uploads/...）补成完整地址：作品页不在我们的域名下"""
    if not url:
        return None
    if url.startswith(("http://", "https://", "data:")):
        return url
    return str(request.base_url).rstrip("/") + "/" + url.lstrip("/")


def _short(text: str | None, limit: int) -> str | None:
    if not text:
        return None
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


async def _releases(db: AsyncSession, request: Request) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for platform in ("windows", "android", "macos"):
        try:
            page = await release_service.history(db, request, platform, "stable", 3, 0)
        except Exception:  # 发版表出问题不能拖垮整个接口
            logger.exception("showcase: release history failed for %s", platform)
            continue
        items = []
        for item in page.get("items", []):
            artifact = item.get("artifact") or {}
            published = item.get("published_at")
            items.append({
                "version": item.get("version"),
                "published_at": published.isoformat() if hasattr(published, "isoformat") else published,
                "changelog": item.get("changelog") or "",
                "download_url": artifact.get("url"),
                "size": artifact.get("size"),
            })
        result[platform] = {"latest": items[0] if items else None, "history": items}
    return result


async def _build(db: AsyncSession, request: Request) -> dict[str, Any]:
    # 创作者全部给出（几十位），作品页默认只露前 8 位、可以展开
    home = await home_service.get_home(db, 0, 0, group_limit=6, creator_limit=200)
    try:
        online = await manager.online_count_global()
    except Exception:
        logger.exception("showcase: online count failed")
        online = None
    return {
        "stats": {**home.stats.model_dump(), "online": online},
        "groups": [
            {
                "id": group.id,
                "title": group.display_title or group.title,
                "avatar_url": _absolute(group.avatar_url, request),
                "member_count": group.member_count,
                "description": _short(group.description, 60),
                "category": group.category,
            }
            for group in home.groups
        ],
        # 大佬用 CCW 上的资料（头像、名字、简介、粉丝 / 获赞 / 作品），按粉丝数从高到低
        "creators": [
            {
                "nickname": creator.ccw_name or creator.nickname or creator.username,
                "avatar_url": _absolute(creator.ccw_avatar_url or creator.avatar_url, request),
                "bio": _short(creator.ccw_bio, 40),
                "followers": creator.ccw_follower_count or 0,
                "likes": creator.ccw_like_count or 0,
                "works": creator.ccw_creation_count or 0,
                "views": creator.ccw_view_count or 0,
                "ccw_url": f"https://www.ccw.site/student/{creator.ccw_student_oid}" if creator.ccw_student_oid else None,
            }
            for creator in sorted(home.creators, key=lambda item: item.ccw_follower_count or 0, reverse=True)
        ],
        "creator_total": home.creator_total,
        "releases": await _releases(db, request),
        "download_page": str(request.base_url).rstrip("/") + "/download",
        "generated_at": int(time.time()),
    }


@router.get("/public/showcase", include_in_schema=False)
async def showcase(request: Request, db: AsyncSession = Depends(get_db)) -> JSONResponse:
    now = time.monotonic()
    body = _cache["body"]
    if body is None or now - _cache["at"] > CACHE_SECONDS:
        body = await _build(db, request)
        _cache.update(at=now, body=body)
    return JSONResponse(
        body,
        headers={
            "Access-Control-Allow-Origin": "*",
            "Cache-Control": f"public, max-age={CACHE_SECONDS}",
        },
    )
