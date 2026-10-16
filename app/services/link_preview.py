"""外站链接的卡片预览（目前只有 B 站视频）。

客户端在消息里认出 B 站视频链接（bilibili.com/video/BV… / av…、m.bilibili.com、b23.tv 短链）后调
GET /link-preview/bilibili?url=…，拿到标题、封面、UP 主、时长画成卡片；点卡片打开原链接（手机上交给 B 站 App）。
B 站的公开接口不需要登录；结果在进程内缓存 1 小时，失败的缓存 5 分钟，免得同一个链接被一群人同时刷。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from fastapi import HTTPException, status

_BVID = re.compile(r"(BV[0-9A-Za-z]{10})")
_AVID = re.compile(r"(?:^|/)av(\d{1,12})(?:[/?#]|$)", re.IGNORECASE)
_SHORT_HOSTS = {"b23.tv", "www.b23.tv"}
_VIDEO_HOSTS = {"bilibili.com", "www.bilibili.com", "m.bilibili.com"}
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
_OK_TTL = 3600.0
_FAIL_TTL = 300.0
_MAX_CACHE = 2000
_cache: dict[str, tuple[float, dict[str, Any] | None]] = {}


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        return None


def _bad(message: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail={"code": "unsupported_link", "message": message})


def _video_id(url: str) -> tuple[str, str]:
    """('bvid', 'BV…') 或 ('aid', '123')；不是视频链接就抛 422"""
    parsed = urlparse(url if "://" in url else f"https://{url}")
    host = (parsed.hostname or "").lower()
    if host not in _VIDEO_HOSTS:
        raise _bad("不是 B 站视频链接")
    path = parsed.path or ""
    if match := _BVID.search(path):
        return "bvid", match.group(1)
    if match := _AVID.search(path):
        return "aid", match.group(1)
    raise _bad("不是 B 站视频链接")


def _resolve_short(url: str) -> str:
    """b23.tv 短链只跟一跳：取 Location，不去访问它指向的地方"""
    parsed = urlparse(url if "://" in url else f"https://{url}")
    request = Request(f"https://b23.tv{parsed.path}", headers={"User-Agent": _UA}, method="GET")
    opener = build_opener(_NoRedirect)
    try:
        opener.open(request, timeout=6)
    except Exception as exc:  # noqa: BLE001 —— 302 在 _NoRedirect 下以 HTTPError 抛出
        location = getattr(exc, "headers", None) and exc.headers.get("Location")  # type: ignore[union-attr]
        if location:
            return location
    raise _bad("短链接解析失败")


def _fetch_view(kind: str, value: str) -> dict[str, Any]:
    query = f"bvid={value}" if kind == "bvid" else f"aid={value}"
    request = Request(f"https://api.bilibili.com/x/web-interface/view?{query}", headers={"User-Agent": _UA, "Referer": "https://www.bilibili.com/"})
    with build_opener().open(request, timeout=6) as response:
        body = json.loads(response.read().decode("utf-8"))
    if not isinstance(body, dict) or body.get("code") != 0 or not isinstance(body.get("data"), dict):
        raise ValueError(f"bilibili code {body.get('code') if isinstance(body, dict) else '?'}")
    data = body["data"]
    owner = data.get("owner") if isinstance(data.get("owner"), dict) else {}
    stat = data.get("stat") if isinstance(data.get("stat"), dict) else {}
    pic = str(data.get("pic") or "")
    bvid = str(data.get("bvid") or value)
    return {
        "bvid": bvid,
        "aid": data.get("aid"),
        "title": str(data.get("title") or "")[:200],
        "cover": pic.replace("http://", "https://", 1) if pic else None,
        "owner_name": str(owner.get("name") or "")[:60] or None,
        "duration": int(data.get("duration") or 0),
        "views": int(stat.get("view") or 0),
        "danmaku": int(stat.get("danmaku") or 0),
        "url": f"https://www.bilibili.com/video/{bvid}",
    }


def _lookup_blocking(url: str) -> dict[str, Any]:
    raw = url.strip()
    host = (urlparse(raw if "://" in raw else f"https://{raw}").hostname or "").lower()
    if host in _SHORT_HOSTS:
        raw = _resolve_short(raw)
    kind, value = _video_id(raw)
    key = f"{kind}:{value}"
    hit = _cache.get(key)
    if hit and time.monotonic() < hit[0]:
        if hit[1] is None:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail={"code": "preview_unavailable", "message": "暂时拿不到这个视频的信息"})
        return hit[1]
    if len(_cache) > _MAX_CACHE:
        _cache.clear()
    try:
        result = _fetch_view(kind, value)
    except Exception as exc:  # noqa: BLE001
        _cache[key] = (time.monotonic() + _FAIL_TTL, None)
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail={"code": "preview_unavailable", "message": "暂时拿不到这个视频的信息"}) from exc
    _cache[key] = (time.monotonic() + _OK_TTL, result)
    return result


async def bilibili_preview(url: str) -> dict[str, Any]:
    return await asyncio.to_thread(_lookup_blocking, url)
