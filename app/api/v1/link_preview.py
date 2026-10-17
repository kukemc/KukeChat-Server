from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query

from app.core.dependencies import get_current_user_then_release
from app.models.user import User
from app.services.link_preview import bilibili_preview

router = APIRouter()


@router.get("/bilibili")
async def bilibili_link_preview(
    url: Annotated[str, Query(min_length=6, max_length=300)],
    current_user: Annotated[User, Depends(get_current_user_then_release)],
) -> dict[str, Any]:
    """B 站视频卡片：{bvid, aid, title, cover, owner_name, duration(秒), views, danmaku, url}。
    不是视频链接 422 unsupported_link；B 站那边拿不到 502 preview_unavailable（结果缓存 1 小时，失败缓存 5 分钟）。"""
    del current_user
    return await bilibili_preview(url)
