from __future__ import annotations

import os
import base64
from html import escape
from contextlib import asynccontextmanager
from collections.abc import AsyncGenerator
from pathlib import Path, PurePosixPath

from fastapi import Depends, FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles

from app import integrations
from app.api.v1.router import api_router
from app.api.public_showcase import router as public_showcase_router
from app.core.config import settings
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import AsyncSessionLocal, get_db, init_db
from app.core.request_guard import ClientProofMiddleware
from app.core.transport_metrics import TransportMetrics, TransportMetricsMiddleware
from app.services.legacy_uploads import migrate_legacy_upload_response
from app.services.account_levels import start_account_level_worker, stop_account_level_worker
from app.services.intimacy import start_intimacy_worker, stop_intimacy_worker
from app.services.sparks import start_spark_worker, stop_spark_worker
from app.services.read_coalescer import start_read_coalescer, stop_read_coalescer
from app.core import loop_watch
from app.services.cases import start_jury_sweep_worker, stop_jury_sweep_worker
from app.services.ccw import start_ccw_binding_worker, stop_ccw_binding_worker
from app.services.moderation import start_moderation_retry_worker, stop_moderation_retry_worker
from app.services.group_files import start_group_file_worker, stop_group_file_worker
from app.services.red_packets import start_red_packet_worker, stop_red_packet_worker
from app.services.gifts import ensure_default_gifts
from app.services.shop import ensure_default_items as ensure_default_shop_items
from app.services.shop import start_expiry_worker as start_shop_expiry_worker
from app.services.shop import stop_expiry_worker as stop_shop_expiry_worker
from app.services.task import start_task_reminder_worker, stop_task_reminder_worker
from app.services.task_assistant import ensure_task_assistant_bot
from app.services.system_settings import export_public_download_metadata, get_public_download_settings
from app.websocket.manager import admin_manager, bot_manager, game_manager, manager, shared_realtime_subscriber
from app.websocket.routes import router as websocket_router


DOWNLOAD_ICON_URL = "https://m.ccw.site/gandi_application/user_assets/640168abc245d7bf4c6a6a23b03cd29c.png"
transport_metrics = TransportMetrics() if settings.transport_metrics_enabled else None
# 集成点（CCW 网关、金币账本）：有私有实现就用，没有就用占位实现。放在导入时做，私有包有问题启动时就报错
integrations.load()
# The sole switch and 60-second task cover HTTP ASGI and all local WS/Redis publishers.
for _realtime_manager in (manager, admin_manager, bot_manager, game_manager):
    _realtime_manager.transport_metrics = transport_metrics


def _html_text(value: str | None) -> str:
    return escape(value or "", quote=False)


def _html_attr(value: str | None) -> str:
    return escape(value or "", quote=True)


def _svg_windows() -> str:
    return """<svg width=\"25\" height=\"25\" viewBox=\"0 0 24 24\" fill=\"none\" aria-hidden=\"true\"><path d=\"M3 5.6 10.8 4.5v7.1H3V5.6Zm9.2-1.3L21 3v8.6h-8.8V4.3ZM3 12.9h7.8V20L3 18.9v-6Zm9.2 0H21V21l-8.8-1.3v-6.8Z\" fill=\"currentColor\"/></svg>"""


def _svg_apple() -> str:
    # 线条苹果：和其他平台图标一样用 currentColor，主按钮反色时跟着变
    return """<svg width=\"25\" height=\"25\" viewBox=\"0 0 24 24\" fill=\"none\" aria-hidden=\"true\"><path d=\"M12 7.6c-1-1-2.3-1.5-3.6-1.4C5.9 6.4 4.3 8.6 4.3 11.6c0 3.7 2.5 8.9 4.9 8.9.9 0 1.6-.6 2.8-.6s1.9.6 2.8.6c1.6 0 3.2-2.4 4.1-4.8-1.6-.8-2.5-2.2-2.5-3.9 0-1.5.7-2.7 1.9-3.5-.8-1.2-2.1-1.9-3.6-2-1.3-.1-2.6.4-3.7 1.3Z\" stroke=\"currentColor\" stroke-width=\"1.6\" stroke-linejoin=\"round\"/><path d=\"M12.2 6.9c-.1-2.2 1.3-3.9 3.3-4.1.1 2.1-1.4 3.9-3.3 4.1Z\" stroke=\"currentColor\" stroke-width=\"1.6\" stroke-linejoin=\"round\"/></svg>"""


def _svg_android() -> str:
    return """<svg width=\"25\" height=\"25\" viewBox=\"0 0 24 24\" fill=\"none\" aria-hidden=\"true\"><path d=\"M8.2 4.2 6.9 2.1a.7.7 0 0 1 1.2-.7l1.4 2.2a8 8 0 0 1 5 0l1.4-2.2a.7.7 0 0 1 1.2.7l-1.3 2.1A6.9 6.9 0 0 1 19 9.9H5a6.9 6.9 0 0 1 3.2-5.7ZM8.5 7.5a1 1 0 1 0 0-2 1 1 0 0 0 0 2Zm7 0a1 1 0 1 0 0-2 1 1 0 0 0 0 2ZM5 11.2h14v6.3A3.5 3.5 0 0 1 15.5 21h-7A3.5 3.5 0 0 1 5 17.5v-6.3Zm-2 1.1a1.1 1.1 0 0 1 2.2 0v4.8a1.1 1.1 0 1 1-2.2 0v-4.8Zm15.8 0a1.1 1.1 0 0 1 2.2 0v4.8a1.1 1.1 0 1 1-2.2 0v-4.8Z\" fill=\"currentColor\"/></svg>"""


def _svg_scratch() -> str:
    return """<svg width=\"25\" height=\"25\" viewBox=\"0 0 24 24\" fill=\"none\" aria-hidden=\"true\"><path d=\"M12 2.5 20.2 7v10L12 21.5 3.8 17V7L12 2.5Zm0 2.3L6 8.1v6.8l6 3.3 6-3.3V8.1l-6-3.3Zm0 3 3.9 2.1v4.2L12 16.2l-3.9-2.1V9.9L12 7.8Z\" fill=\"currentColor\"/></svg>"""


def _download_icon_data_uri() -> str:
    candidates = [
        Path(__file__).resolve().parents[3] / "KukeChat" / "src-tauri" / "icons" / "icon.png",
        Path(__file__).resolve().parents[2] / "KukeChat" / "src-tauri" / "icons" / "icon.png",
    ]
    for icon_path in candidates:
        if icon_path.exists():
            encoded = base64.b64encode(icon_path.read_bytes()).decode("ascii")
            return f"data:image/png;base64,{encoded}"
    return "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E%3Crect width='64' height='64' rx='18' fill='%234aa8ff'/%3E%3Ctext x='32' y='41' text-anchor='middle' font-size='28' font-family='Arial' fill='white' font-weight='800'%3EK%3C/text%3E%3C/svg%3E"


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[None, None]:
    # 事件循环卡顿监控 + SIGUSR2 现场转储（见 core/loop_watch.py），最先起，启动阶段卡住也能看到
    loop_watch.start()
    await init_db()
    async with AsyncSessionLocal() as db:
        await ensure_task_assistant_bot(db)
        # 商店的初始商品（幂等，已有的不动）
        await ensure_default_shop_items(db)
        # 礼物目录的首批礼物（幂等，已有的不动）
        await ensure_default_gifts(db)
    await manager.start()
    await admin_manager.start()
    await bot_manager.start()
    await game_manager.start()
    await shared_realtime_subscriber.start()
    start_ccw_binding_worker()
    start_moderation_retry_worker()
    start_task_reminder_worker()
    start_jury_sweep_worker()
    # 账号等级：记在线时长、结算活跃天；老用户的折算也在这个循环启动时做一次
    start_account_level_worker()
    # 红包：把过期没领完的、消息已被撤回的退回发送者
    start_red_packet_worker()
    # 限时商品：到期的卸下并通知本人，快到期的提醒一次
    start_shop_expiry_worker()
    # 群文件：每小时删过期的、以及群升级到期超过宽限期的文件
    start_group_file_worker()
    # 好友亲密度：上线后第一次启动给老好友回填一次；之后每小时补「成为好友满 N 天」的徽章
    start_intimacy_worker()
    # 好友火花：每 10 分钟把连续两天没续上的火花清零（记下原来的天数，3 天内可重燃）并通知双方
    start_spark_worker()
    # 已读位置：几秒内的连续推进合并成一次写库
    start_read_coalescer()
    if transport_metrics is not None:
        await transport_metrics.start()
    try:
        yield
    finally:
        await loop_watch.stop()
        await stop_read_coalescer()
        await stop_intimacy_worker()
        await stop_spark_worker()
        await stop_group_file_worker()
        await stop_shop_expiry_worker()
        await stop_red_packet_worker()
        await stop_account_level_worker()
        await stop_jury_sweep_worker()
        await stop_task_reminder_worker()
        await stop_ccw_binding_worker()
        await stop_moderation_retry_worker()
        await shared_realtime_subscriber.stop()
        await game_manager.stop()
        await bot_manager.stop()
        await admin_manager.stop()
        await manager.stop()
        if transport_metrics is not None:
            await transport_metrics.stop()


class UploadAwareGZipMiddleware(GZipMiddleware):
    """Keep local, already-encoded upload media byte-identical (including Range)."""

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if (
            scope["type"] == "http"
            and settings.storage_backend == "local"
            and path.startswith("/uploads/")
            and CachedUploadFiles.is_public_media_path(path[len("/uploads/"):])
        ) or (scope["type"] == "http" and path.endswith("/download")):
            # 文件下载（群文件等）是二进制大文件：apk、视频压不小，gzip 只会在事件循环里白烧 CPU、多占一份内存
            await self.app(scope, receive, send)
            return
        await super().__call__(scope, receive, send)


app = FastAPI(title=settings.app_name, lifespan=lifespan)
Path(settings.upload_dir).mkdir(parents=True, exist_ok=True)
app.add_middleware(UploadAwareGZipMiddleware, minimum_size=1024, compresslevel=4)
app.add_middleware(ClientProofMiddleware)


app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_origin_regex=settings.cors_origin_regex,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
# Last registered means outermost: records ASGI body bytes *after* gzip/CORS.
# With the default disabled, no per-request observer or background task exists.
if transport_metrics is not None:
    app.add_middleware(TransportMetricsMiddleware, metrics=transport_metrics)


@app.get("/health", tags=["health"])
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health/storage", tags=["health"])
async def storage_health() -> dict[str, str | None]:
    return {
        "storage_backend": settings.storage_backend,
        "s3_endpoint_url": settings.s3_endpoint_url,
        "s3_bucket": settings.s3_bucket,
        "s3_public_url": settings.s3_public_url,
    }


@app.get("/health/realtime", tags=["health"])
async def realtime_health() -> dict[str, int | str | bool]:
    return {
        "worker_pid": os.getpid(),
        "realtime_instance_id": os.environ.get("REALTIME_INSTANCE_ID", "local"),
        "online_users": await manager.online_count_global(),
        "local_online_users": manager.online_count(),
        "websocket_connections": manager.connection_count(),
        "admin_websocket_connections": admin_manager.connection_count(),
        "redis_realtime_configured": bool(settings.redis_url),
        "redis_realtime_enabled": manager.redis_enabled(),
        "admin_redis_realtime_enabled": admin_manager.redis_enabled(),
        "realtime_redis_channel": settings.realtime_redis_channel,
    }


_DOWNLOAD_TEMPLATE_PATH = Path(__file__).resolve().parent / "templates" / "download.html"
_download_template_cache: str | None = None


def _download_template() -> str:
    global _download_template_cache
    if _download_template_cache is None:
        _download_template_cache = _DOWNLOAD_TEMPLATE_PATH.read_text(encoding="utf-8")
    return _download_template_cache


def _detect_platform(user_agent: str) -> str:
    """按 UA 猜访问者的平台：android / ios / mac / desktop。

    iPadOS 13 起 Safari 默认发桌面版 UA（和 Mac 一模一样，带 Macintosh），服务端分不出来，
    会被当成 mac；页面脚本里再按触屏把它纠正回 ios（见 download.html 的 data-platform）。"""
    ua = user_agent.lower()
    if "android" in ua:
        return "android"
    if any(token in ua for token in ("iphone", "ipad", "ipod")):
        return "ios"
    if "macintosh" in ua or "mac os x" in ua:
        return "mac"
    return "desktop"


@app.get("/download", include_in_schema=False)
async def download_page(request: Request, db: AsyncSession = Depends(get_db)) -> Response:
    download_settings = await get_public_download_settings(db, request)
    pc_url = _html_attr(download_settings.pc_url)
    android_url = _html_attr(download_settings.android_url)
    mac_url = _html_attr(download_settings.mac_url)
    scratch_url = _html_attr(download_settings.scratch_url)
    pc_version = _html_text(download_settings.pc_version or download_settings.version or "最新版")
    mobile_version = _html_text(download_settings.mobile_version or "最新版")
    mac_version = _html_text(download_settings.mac_version or "最新版")
    # 纯数字版本号前面补个 v，「最新版」这类文字原样显示
    pc_version = f"v{pc_version}" if pc_version[:1].isdigit() else pc_version
    mobile_version = f"v{mobile_version}" if mobile_version[:1].isdigit() else mobile_version
    mac_version = f"v{mac_version}" if mac_version[:1].isdigit() else mac_version
    available = {
        "pc": bool(download_settings.pc_url),
        "mac": bool(download_settings.mac_url),
        "android": bool(download_settings.android_url),
        "scratch": bool(download_settings.scratch_url),
    }

    # 安卓访问把 Android 设为主按钮，Mac 推荐 Mac 版，iOS 没有客户端就推荐社区扩展，其余（电脑）推荐 Windows；
    # 推荐的那一端没有链接时，顺延到第一个能下的
    platform = _detect_platform(request.headers.get("user-agent", ""))
    preferred = {"android": "android", "ios": "scratch", "mac": "mac"}.get(platform, "pc")
    primary = next((key for key in (preferred, "pc", "mac", "android", "scratch") if available[key]), None)
    names = {"pc": "Windows 版", "mac": "Mac 版", "android": "Android 版", "scratch": "CCW 社区扩展"}
    if primary is None:
        hint = "下载链接正在准备中，请稍后再来看看。"
    elif platform == "ios":
        hint = (
            "iOS 版还在路上，先用 CCW 社区扩展在浏览器里聊吧。"
            if primary == "scratch"
            else "iOS 版还在路上，可以先在电脑或安卓手机上使用。"
        )
    else:
        hint = f"已按你的设备推荐 {names[primary]}，其他平台也能随时下载。"

    def button_class(key: str) -> str:
        return ("" if available[key] else " is-disabled") + (" is-primary" if key == primary else "")

    def button_aria(key: str) -> str:
        return "" if available[key] else ' aria-disabled="true" tabindex="-1"'

    replacements = {
        "__PLATFORM__": platform,
        "__PC_CLASS__": button_class("pc"),
        "__MAC_CLASS__": button_class("mac"),
        "__ANDROID_CLASS__": button_class("android"),
        "__SCRATCH_CLASS__": button_class("scratch"),
        "__PC_ARIA__": button_aria("pc"),
        "__MAC_ARIA__": button_aria("mac"),
        "__ANDROID_ARIA__": button_aria("android"),
        "__SCRATCH_ARIA__": button_aria("scratch"),
        "__PC_META__": pc_version if available["pc"] else "暂未开放",
        "__MAC_META__": mac_version if available["mac"] else "暂未开放",
        "__MOBILE_META__": mobile_version if available["android"] else "暂未开放",
        "__SCRATCH_META__": "在 CCW 里直接用" if available["scratch"] else "暂未开放",
        "__PC_HREF__": pc_url or "#",
        "__MAC_HREF__": mac_url or "#",
        "__ANDROID_HREF__": android_url or "#",
        "__SCRATCH_HREF__": scratch_url or "#",
        "__HINT__": _html_text(hint),
        "__ICON_URL__": _html_attr(DOWNLOAD_ICON_URL),
        "__WINDOWS_ICON__": _svg_windows(),
        "__APPLE_ICON__": _svg_apple(),
        "__ANDROID_ICON__": _svg_android(),
        "__SCRATCH_ICON__": _svg_scratch(),
    }
    html = _download_template()
    for placeholder, value in replacements.items():
        html = html.replace(placeholder, value)
    return HTMLResponse(html)


# 下载页用的静态图（产品截图）。只认白名单里的文件名，路径参数不会拼进文件系统路径
_DOWNLOAD_ASSET_DIR = Path(__file__).resolve().parent / "static" / "download"
_DOWNLOAD_ASSETS = {
    "app-light.webp": "image/webp",
    "app-dark.webp": "image/webp",
}


@app.get("/download/assets/{name}", include_in_schema=False)
async def download_asset(name: str) -> Response:
    media_type = _DOWNLOAD_ASSETS.get(name)
    if media_type is None:
        return Response(status_code=404)
    return FileResponse(
        _DOWNLOAD_ASSET_DIR / name,
        media_type=media_type,
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.get("/desktop-update.json", include_in_schema=False)
async def desktop_update_metadata(request: Request, db: AsyncSession = Depends(get_db)) -> dict[str, str | bool | None]:
    # 旧版桌面端 / 安卓检查更新用的元数据。发版服务里有已发布版本时优先用它（docs/releases.md 1.3）
    return await export_public_download_metadata(db, request)


@app.get("/login", response_class=HTMLResponse, include_in_schema=False)
async def login_page() -> str:
    return """
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>KukeChat 登录</title>
  <style>
    :root { --primary-400: #60a5fa; --primary-500: #3b82f6; --primary-600: #2563eb; --primary-700: #1d4ed8; --gray-50: #f9fafb; --gray-100: #f3f4f6; --gray-200: #e5e7eb; --gray-300: #d1d5db; --gray-400: #9ca3af; --gray-500: #6b7280; --gray-600: #4b5563; --gray-700: #374151; --gray-800: #1f2937; --gray-900: #111827; --gray-950: #030712; }
    * { box-sizing: border-box; }
    body { margin: 0; min-height: 100vh; font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, "Microsoft YaHei", sans-serif; color: var(--gray-900); background: #fff; }
    .shell { display: flex; min-height: 100vh; width: 100%; overflow: hidden; background: #fff; }
    .brand-side { position: relative; display: none; width: 0; flex: 1 1 0%; color: #fff; }
    .visual-bg { position: absolute; inset: 0; height: 100%; width: 100%; overflow: hidden; background: var(--gray-900); }
    .visual-gradient { position: absolute; inset: 0; background: linear-gradient(135deg, var(--gray-900), var(--gray-800), #000); opacity: .9; }
    .visual-image { position: absolute; inset: 0; background-image: url('https://images.unsplash.com/photo-1605218427306-022ba8c6f661?q=80&w=2070&auto=format&fit=crop'); background-size: cover; background-position: center; mix-blend-mode: overlay; opacity: .2; }
    .blob-one { position: absolute; left: -80px; top: -80px; height: 384px; width: 384px; border-radius: 9999px; background: rgba(59, 130, 246, .2); filter: blur(64px); }
    .blob-two { position: absolute; bottom: 0; right: 0; height: 384px; width: 384px; border-radius: 9999px; background: rgba(168, 85, 247, .2); filter: blur(64px); }
    .brand-content { position: relative; z-index: 1; display: flex; height: 100%; flex-direction: column; justify-content: space-between; padding: 48px; }
    .brand { display: flex; align-items: center; gap: 12px; }
    .logo-wrap { display: flex; height: 40px; width: 40px; align-items: center; justify-content: center; overflow: hidden; border-radius: 8px; background: rgba(255, 255, 255, .1); backdrop-filter: blur(12px); }
    .logo { height: 100%; width: 100%; object-fit: cover; }
    .brand-name { font-size: 20px; font-weight: 700; letter-spacing: .025em; }
    .hero { display: flex; flex-direction: column; gap: 24px; }
    h1 { margin: 0; color: #fff; font-size: 36px; line-height: 1.25; font-weight: 700; letter-spacing: -.025em; }
    .accent { color: var(--primary-400); }
    .lead { max-width: 448px; margin: 0; color: var(--gray-400); font-size: 18px; line-height: 1.75; }
    .copyright { display: flex; align-items: center; justify-content: space-between; color: var(--gray-500); font-size: 14px; }
    .login-side { display: flex; flex: 1 1 0%; flex-direction: column; justify-content: center; padding: 48px 16px; background: #fff; }
    .card { width: 100%; max-width: 384px; margin: 0 auto; animation: fade-in .3s ease-out; }
    .form-heading { text-align: center; }
    .card h2 { margin: 0; color: var(--gray-900); font-size: 30px; line-height: 1.2; font-weight: 700; letter-spacing: -.025em; }
    .subtitle { margin: 8px 0 0; color: var(--gray-600); font-size: 14px; line-height: 1.5; }
    .form-stack { margin-top: 40px; display: flex; flex-direction: column; gap: 24px; }
    .field { display: block; margin: 0; color: var(--gray-700); font-size: 14px; font-weight: 500; }
    .control-wrap { margin-top: 8px; }
    .field input, .field textarea { display: block; width: 100%; border: 0; border-radius: 8px; background: var(--gray-50); color: var(--gray-900); font-size: 14px; line-height: 24px; box-shadow: inset 0 0 0 1px var(--gray-200), 0 1px 2px rgba(0, 0, 0, .05); outline: none; transition: background .2s ease, box-shadow .2s ease; }
    .field input { padding: 12px 16px; }
    .field textarea { min-height: 110px; resize: vertical; padding: 12px 16px; }
    .field input::placeholder, .field textarea::placeholder { color: var(--gray-400); }
    .field input:focus, .field textarea:focus { background: #fff; box-shadow: inset 0 0 0 2px var(--primary-600), 0 1px 2px rgba(0, 0, 0, .05); }
    .hint { margin: 8px 0 0; color: var(--gray-400); font-size: 12px; font-weight: 400; line-height: 1.5; }
    .row { display: flex; align-items: center; justify-content: space-between; gap: 16px; }
    .remember { display: flex; align-items: center; color: var(--gray-900); font-size: 14px; font-weight: 400; cursor: pointer; user-select: none; }
    .remember input { height: 16px; width: 16px; margin: 0 8px 0 0; border-radius: 4px; accent-color: var(--primary-600); cursor: pointer; }
    .link { border: 0; background: transparent; padding: 0; color: var(--primary-600); text-decoration: none; font: inherit; font-size: 14px; font-weight: 500; cursor: pointer; }
    .link:hover { color: var(--primary-500); }
    .primary-button { position: relative; display: flex; width: 100%; justify-content: center; border: 0; border-radius: 8px; background: var(--primary-600); padding: 12px 16px; color: #fff; font-size: 14px; font-weight: 600; line-height: 20px; cursor: pointer; box-shadow: 0 10px 15px -3px rgba(37, 99, 235, .18), 0 4px 6px -4px rgba(37, 99, 235, .22); transition: background .2s ease, box-shadow .2s ease, transform .2s ease, opacity .2s ease; }
    .primary-button:hover { background: var(--primary-700); box-shadow: 0 10px 15px -3px rgba(59, 130, 246, .3), 0 4px 6px -4px rgba(59, 130, 246, .3); }
    .primary-button:active { transform: scale(.98); }
    .primary-button:disabled { cursor: not-allowed; opacity: .7; transform: none; }
    .spinner { position: absolute; left: 0; inset-block: 0; display: none; align-items: center; padding-left: 12px; }
    .spinner::before { content: ""; height: 20px; width: 20px; border-radius: 9999px; border: 3px solid rgba(255, 255, 255, .28); border-top-color: #fff; animation: spin .8s linear infinite; }
    .primary-button.loading .spinner { display: flex; }
    .primary-button.loading .button-label { opacity: 0; }
    .error, .ok { display: none; border-radius: 6px; padding: 16px; font-size: 14px; line-height: 1.5; animation: fade-in .3s ease-out; }
    .error { background: #fef2f2; color: #b91c1c; }
    .ok { background: #ecfdf5; color: #047857; }
    .hidden { display: none !important; }
    .secondary { display: flex; width: 100%; justify-content: center; border: 0; border-radius: 8px; background: var(--gray-100); padding: 12px 16px; color: var(--gray-700); font-size: 14px; font-weight: 600; line-height: 20px; cursor: pointer; transition: background .2s ease, color .2s ease; }
    .secondary:hover { background: var(--gray-200); color: var(--gray-900); }
    @keyframes fade-in { from { opacity: 0; } to { opacity: 1; } }
    @keyframes spin { to { transform: rotate(360deg); } }
    @media (min-width: 640px) { .login-side { padding-inline: 24px; } }
    @media (min-width: 1024px) { .brand-side { display: block; } .login-side { flex: none; padding-inline: 80px; } .card { width: 384px; } .form-heading { text-align: left; } h1 { font-size: 48px; } }
    @media (min-width: 1280px) { .login-side { padding-inline: 96px; } }
    @media (prefers-reduced-motion: reduce) { *, *::before, *::after { animation-duration: .001ms !important; transition-duration: .001ms !important; } }
  </style>
</head>
<body>
  <main class="shell">
    <section class="brand-side" aria-label="KukeChat 介绍">
      <div class="visual-bg" aria-hidden="true">
        <div class="visual-gradient"></div>
        <div class="visual-image"></div>
        <div class="blob-one"></div>
        <div class="blob-two"></div>
      </div>
      <div class="brand-content">
        <div class="brand">
          <div class="logo-wrap">
            <img class="logo" src="https://m.ccw.site/gandi_application/user_assets/2a6bb37880317d2bb5525ab560618e04.png" alt="KukeChat" />
          </div>
          <span class="brand-name">KukeChat</span>
        </div>
        <div class="hero">
          <h1>连接每一次 <br /><span class="accent">KukeChat</span> 对话</h1>
          <p class="lead">轻量、实时的聊天工具，让好友、群聊、图片消息与收藏表情自然连接。</p>
        </div>
        <div class="copyright">
          <p>© 2026 KukeChat Network</p>
        </div>
      </div>
    </section>
    <section class="login-side" aria-label="登录表单">
      <form class="card" id="form">
        <div id="loginView">
          <div class="form-heading">
            <h2>欢迎回来</h2>
            <p class="subtitle">请登录您的 KukeChat 账号以继续</p>
          </div>
          <div class="form-stack">
            <label class="field" for="username">
              用户名
              <div class="control-wrap">
                <input id="username" name="username" type="text" autocomplete="username" placeholder="请输入您的用户名" required />
              </div>
            </label>
            <label class="field" for="password">
              密码
              <div class="control-wrap">
                <input id="password" name="password" type="password" autocomplete="current-password" placeholder="请输入您的密码" required />
              </div>
            </label>
            <div class="row">
              <label class="remember"><input id="remember" name="remember-me" type="checkbox" checked />记住我</label>
              <button class="link" id="forgotLink" type="button">忘记密码?</button>
            </div>
            <button class="primary-button" id="submit" type="submit"><span class="spinner"></span><span class="button-label">立即登录</span></button>
            <div class="error" id="error"></div>
            <div class="ok" id="ok">登录成功，正在返回 KukeChat...</div>
          </div>
        </div>
        <div id="forgotView" class="hidden">
          <div class="form-heading">
            <h2>忘记密码申诉</h2>
            <p class="subtitle">提交后由管理员人工审核，不会自动重置密码。</p>
          </div>
          <div class="form-stack">
            <label class="field" for="resetUsername">
              用户名
              <div class="control-wrap">
                <input id="resetUsername" autocomplete="username" placeholder="请输入 KukeChat 用户名" />
              </div>
              <p class="hint">账号仅可使用英文字母、数字、下划线、点和短横线。</p>
            </label>
            <label class="field" for="resetProfile">
              CCW 主页链接
              <div class="control-wrap">
                <input id="resetProfile" placeholder="https://www.ccw.site/student/xxx" />
              </div>
            </label>
            <label class="field" for="resetPassword">
              请填写新密码
              <div class="control-wrap">
                <input id="resetPassword" type="text" autocomplete="new-password" placeholder="至少 6 位，审核通过后将使用此密码" />
              </div>
            </label>
            <label class="field" for="resetReason">
              申诉理由
              <div class="control-wrap">
                <textarea id="resetReason" placeholder="说明为什么可以证明这个账号属于你，至少 10 个字。"></textarea>
              </div>
            </label>
            <button class="primary-button" id="resetSubmit" type="button"><span class="spinner"></span><span class="button-label">提交申请</span></button>
            <button class="secondary" id="backLogin" type="button">返回登录</button>
            <div class="error" id="resetError"></div>
            <div class="ok" id="resetOk">申请已提交，管理员审核后会处理。</div>
          </div>
        </div>
      </form>
    </section>
  </main>
  <script>
    const allowedTargets = new Set(['https://www.ccw.site', 'https://ccw.site']);
    const params = new URLSearchParams(location.search);
    const targetOrigin = params.get('origin') || 'https://www.ccw.site';
    const form = document.getElementById('form');
    const errorBox = document.getElementById('error');
    const okBox = document.getElementById('ok');
    const submit = document.getElementById('submit');
    const submitLabel = submit.querySelector('.button-label');
    const loginView = document.getElementById('loginView');
    const forgotView = document.getElementById('forgotView');
    const forgotLink = document.getElementById('forgotLink');
    const backLogin = document.getElementById('backLogin');
    const resetSubmit = document.getElementById('resetSubmit');
    const resetSubmitLabel = resetSubmit.querySelector('.button-label');
    const resetError = document.getElementById('resetError');
    const resetOk = document.getElementById('resetOk');
    const usernameInput = document.getElementById('username');
    const resetUsername = document.getElementById('resetUsername');
    const resetProfile = document.getElementById('resetProfile');
    const resetPassword = document.getElementById('resetPassword');
    const resetReason = document.getElementById('resetReason');
    function showLogin() {
      loginView.classList.remove('hidden');
      forgotView.classList.add('hidden');
      resetError.style.display = 'none';
      resetOk.style.display = 'none';
    }
    function showForgot() {
      resetUsername.value = usernameInput.value.trim();
      loginView.classList.add('hidden');
      forgotView.classList.remove('hidden');
      errorBox.style.display = 'none';
      okBox.style.display = 'none';
    }
    function randomHex(length) {
      const bytes = new Uint8Array(Math.ceil(length / 2));
      crypto.getRandomValues(bytes);
      return Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0')).join('').slice(0, length);
    }
    async function sha256Hex(input) {
      const bytes = new TextEncoder().encode(input);
      const digest = await crypto.subtle.digest('SHA-256', bytes);
      return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, '0')).join('');
    }
    async function proofHeaders(method, path) {
      const timestamp = Date.now().toString();
      const nonce = randomHex(16);
      return {
        'X-Kuke-Client': 'kukechat-web/0.2.0',
        'X-Kuke-Timestamp': timestamp,
        'X-Kuke-Nonce': nonce,
        'X-Kuke-Proof': await sha256Hex(`${method}|${path}|${timestamp}|${nonce}|KukeChat`)
      };
    }
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      if (loginView.classList.contains('hidden')) return;
      errorBox.style.display = 'none';
      okBox.style.display = 'none';
      submit.disabled = true;
      submit.classList.add('loading');
      submitLabel.textContent = '正在登录...';
      try {
        const response = await fetch('/api/v1/auth/cookie-login', {
          method: 'POST',
          credentials: 'include',
          headers: { 'Content-Type': 'application/json', ...(await proofHeaders('POST', '/auth/cookie-login')) },
          body: JSON.stringify({ username_or_email: document.getElementById('username').value.trim(), password: document.getElementById('password').value, remember_me: document.getElementById('remember').checked })
        });
        const payload = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(payload.detail || '登录失败');
        okBox.style.display = 'block';
        submit.classList.remove('loading');
        submitLabel.textContent = '登录成功';
        if (window.opener && allowedTargets.has(targetOrigin)) {
          window.opener.postMessage({ type: 'kukechat.cookie-login.success' }, targetOrigin);
          setTimeout(() => window.close(), 450);
        }
      } catch (error) {
        errorBox.textContent = error instanceof Error ? error.message : '登录失败';
        errorBox.style.display = 'block';
        submit.disabled = false;
        submit.classList.remove('loading');
        submitLabel.textContent = '立即登录';
      }
    });
    forgotLink.addEventListener('click', showForgot);
    backLogin.addEventListener('click', showLogin);
    resetSubmit.addEventListener('click', async () => {
      resetError.style.display = 'none';
      resetOk.style.display = 'none';
      const username = resetUsername.value.trim();
      const ccwProfileUrl = resetProfile.value.trim();
      const requestedPassword = resetPassword.value;
      const reason = resetReason.value.trim();
      if (!/^[A-Za-z0-9_.-]+$/.test(username) || username.length < 3) {
        resetError.textContent = '请输入正确的 KukeChat 用户名。';
        resetError.style.display = 'block';
        return;
      }
      if (!/^https:\/\/www\.ccw\.site\/student\/[^\s/]+\/?$/.test(ccwProfileUrl)) {
        resetError.textContent = '请输入正确的 CCW 学生主页链接，例如 https://www.ccw.site/student/xxx';
        resetError.style.display = 'block';
        return;
      }
      try {
        const lookupResponse = await fetch(`/api/v1/auth/account-lookup?username=${encodeURIComponent(username)}`);
        const lookup = await lookupResponse.json().catch(() => ({}));
        if (!lookup.exists) {
          resetError.textContent = '账号不存在，请检查用户名是否输入正确。';
          resetError.style.display = 'block';
          return;
        }
      } catch (error) {
        resetError.textContent = '无法校验账号，请检查网络后重试。';
        resetError.style.display = 'block';
        return;
      }
      if (requestedPassword.length < 6) {
        resetError.textContent = '请填写新密码，至少需要 6 位。';
        resetError.style.display = 'block';
        return;
      }
      if (reason.length < 10) {
        resetError.textContent = '申诉理由至少需要 10 个字。';
        resetError.style.display = 'block';
        return;
      }
      resetSubmit.disabled = true;
      resetSubmit.classList.add('loading');
      resetSubmitLabel.textContent = '提交中...';
      try {
        const response = await fetch('/api/v1/auth/password-reset-requests', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ username, ccw_profile_url: ccwProfileUrl, requested_password: requestedPassword, reason })
        });
        const payload = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(payload.detail || '提交失败');
        resetOk.style.display = 'block';
        resetSubmit.classList.remove('loading');
        resetSubmitLabel.textContent = '已提交';
      } catch (error) {
        resetError.textContent = error instanceof Error ? error.message : '提交失败';
        resetError.style.display = 'block';
        resetSubmit.disabled = false;
        resetSubmit.classList.remove('loading');
        resetSubmitLabel.textContent = '提交申请';
      }
    });
  </script>
</body>
</html>
"""


class CachedUploadFiles(StaticFiles):
    """Cache only public image/voice files, without altering Starlette's 304/Range handling."""

    _image_folders = {"avatars", "profile-covers", "messages", "posts", "teamup", "announcements", "stickers"}
    _image_suffixes = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
    _audio_suffixes = {".webm", ".ogg", ".mp3", ".m4a", ".aac", ".wav"}

    @classmethod
    def is_public_media_path(cls, path: str) -> bool:
        # StaticFiles.get_path uses the OS separator (backslash on Windows).
        parts = PurePosixPath(path.replace("\\", "/")).parts
        if len(parts) == 2:
            folder, filename = parts
        elif len(parts) == 3 and parts[1] == "thumbs":
            folder, _, filename = parts
        else:
            return False
        suffix = PurePosixPath(filename).suffix.lower()
        return (folder in cls._image_folders and suffix in cls._image_suffixes) or (
            folder == "messages" and len(parts) == 2 and suffix in cls._audio_suffixes
        )

    # 群文件（docs/group-plus.md 第 5 节）只能走带鉴权的下载接口，不能通过 /uploads 直链拿
    _private_folders = {"group-files"}

    async def get_response(self, path: str, scope: dict) -> Response:
        first = PurePosixPath(path.replace("\\", "/")).parts[:1]
        if first and first[0] in self._private_folders:
            return Response(status_code=404)
        response = await super().get_response(path, scope)
        if self.is_public_media_path(path):
            # Historic names and deleted/replaced uploads may still resolve at the same URL.
            # A short TTL is safer than claiming immutable, including for UUID-named media.
            response.headers["Cache-Control"] = "public, max-age=3600"
        return response


app.include_router(api_router)
# 作品页用的公开数据（不在 /api/v1 下：不走客户端证明头，任何来源可读）
app.include_router(public_showcase_router)
app.include_router(websocket_router)
if settings.storage_backend == "local":
    app.mount("/uploads", CachedUploadFiles(directory=settings.upload_dir), name="uploads")
else:
    @app.get("/uploads/{path:path}", include_in_schema=False)
    async def legacy_upload(path: str, request: Request, db: AsyncSession = Depends(get_db)) -> Response:
        if path.split("/", 1)[0] in CachedUploadFiles._private_folders:
            return Response(status_code=404)
        response = await migrate_legacy_upload_response(path, db, request)
        if response is None:
            return Response(status_code=404)
        return response
