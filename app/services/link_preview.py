"""外站链接的卡片预览（目前只有 B 站视频）。

客户端在消息里认出 B 站视频链接（bilibili.com/video/BV… / av…、m.bilibili.com、b23.tv 短链）后调
GET /link-preview/bilibili?url=…，拿到标题、封面、UP 主、时长画成卡片；点卡片打开原链接（手机上交给 B 站 App）。
B 站的公开接口不需要登录；结果在进程内缓存 1 小时，失败的缓存 5 分钟，免得同一个链接被一群人同时刷。
"""

from __future__ import annotations

import asyncio
import json
import re
import ssl
from html import unescape
import time
from typing import Any
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

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
# 线上的 Python（宝塔装的，自带 openssl111）找不到能校验 b23.tv 证书链的根证书，握手直接失败；
# 和 CCW 网关一样显式用系统的 CA 文件，找不到再退回默认
_CA_FILES = ("/etc/ssl/certs/ca-certificates.crt", "/etc/pki/tls/certs/ca-bundle.crt", "/etc/ssl/cert.pem")


def _ssl_context() -> ssl.SSLContext:
    for cafile in _CA_FILES:
        try:
            return ssl.create_default_context(cafile=cafile)
        except (FileNotFoundError, ssl.SSLError):
            continue
    return ssl.create_default_context()


_SSL = _ssl_context()


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
    opener = build_opener(_NoRedirect, HTTPSHandler(context=_SSL))
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
    with build_opener(HTTPSHandler(context=_SSL)).open(request, timeout=6) as response:
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


_MOBILE_UA = "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Mobile Safari/537.36"


def _meta(html: str, key: str) -> str | None:
    match = re.search(r'<meta[^>]*(?:property|name)="' + re.escape(key) + r'"[^>]*content="([^"]*)"', html)
    return unescape(match.group(1)).strip() if match else None


def _fetch_mobile_page(kind: str, value: str) -> dict[str, Any]:
    """接口被风控（线上服务器的 IP 调 api.bilibili.com/x/web-interface/view 一律 412）时的退路：
    手机版视频页没拦，从它的 og 标签和页面数据里取标题、封面、UP 主、时长、播放量。"""
    path = value if kind == "bvid" else f"av{value}"
    request = Request(f"https://m.bilibili.com/video/{path}", headers={"User-Agent": _MOBILE_UA})
    with build_opener(HTTPSHandler(context=_SSL)).open(request, timeout=6) as response:
        html = response.read(600_000).decode("utf-8", errors="replace")
    title = _meta(html, "og:title")
    if not title:
        raise ValueError("no og:title")
    title = re.sub(r"_哔哩哔哩_bilibili$", "", title)
    page_url = _meta(html, "og:url") or ""
    bvid = (_BVID.search(page_url) or _BVID.search(html))
    bvid_text = bvid.group(1) if bvid else (value if kind == "bvid" else "")
    cover = (_meta(html, "og:image") or "").split("@")[0] or None
    description = _meta(html, "og:description") or ""
    views = re.search(r"播放量\s*(\d+)", description)
    danmaku = re.search(r"弹幕量\s*(\d+)", description)
    duration = re.search(r'"duration":(\d+)', html)
    aid = re.search(r'"aid":(\d+)', html)
    return {
        "bvid": bvid_text,
        "aid": int(aid.group(1)) if aid else (int(value) if kind == "aid" else None),
        "title": title[:200],
        "cover": cover.replace("http://", "https://", 1) if cover else None,
        "owner_name": (_meta(html, "author") or "")[:60] or None,
        "duration": int(duration.group(1)) if duration else 0,
        "views": int(views.group(1)) if views else 0,
        "danmaku": int(danmaku.group(1)) if danmaku else 0,
        "url": f"https://www.bilibili.com/video/{bvid_text}" if bvid_text else f"https://www.bilibili.com/video/av{value}",
    }


def _fetch(kind: str, value: str) -> dict[str, Any]:
    try:
        return _fetch_view(kind, value)
    except Exception:  # noqa: BLE001 —— 412 风控、接口改版都走网页退路
        return _fetch_mobile_page(kind, value)


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
        result = _fetch(kind, value)
    except Exception as exc:  # noqa: BLE001
        _cache[key] = (time.monotonic() + _FAIL_TTL, None)
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail={"code": "preview_unavailable", "message": "暂时拿不到这个视频的信息"}) from exc
    _cache[key] = (time.monotonic() + _OK_TTL, result)
    return result


async def bilibili_preview(url: str) -> dict[str, Any]:
    return await asyncio.to_thread(_lookup_blocking, url)
