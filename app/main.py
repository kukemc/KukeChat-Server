from __future__ import annotations

import os
import base64
from html import escape
from contextlib import asynccontextmanager
from collections.abc import AsyncGenerator
from pathlib import Path, PurePosixPath

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles

from app import integrations
from app.api.v1.router import api_router
from app.core.config import settings
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import AsyncSessionLocal, get_db, init_db
from app.core.request_guard import ClientProofMiddleware
from app.core.transport_metrics import TransportMetrics, TransportMetricsMiddleware
from app.services.legacy_uploads import migrate_legacy_upload_response
from app.services.account_levels import start_account_level_worker, stop_account_level_worker
from app.services.intimacy import start_intimacy_worker, stop_intimacy_worker
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
    if transport_metrics is not None:
        await transport_metrics.start()
    try:
        yield
    finally:
        await stop_intimacy_worker()
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
        ):
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


@app.get("/download", include_in_schema=False)
async def download_page(db: AsyncSession = Depends(get_db)) -> Response:
    download_settings = await get_public_download_settings(db)
    pc_url = _html_attr(download_settings.pc_url)
    android_url = _html_attr(download_settings.android_url)
    scratch_url = _html_attr(download_settings.scratch_url)
    pc_version = _html_text(download_settings.pc_version or download_settings.version or "最新版")
    mobile_version = _html_text(download_settings.mobile_version or "最新版")
    pc_disabled = "" if download_settings.pc_url else " is-disabled"
    android_disabled = "" if download_settings.android_url else " is-disabled"
    scratch_disabled = "" if download_settings.scratch_url else " is-disabled"
    pc_href = pc_url or "#"
    android_href = android_url or "#"
    scratch_href = scratch_url or "#"
    icon_url = _html_attr(DOWNLOAD_ICON_URL)
    return HTMLResponse(
        """
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>KukeChat 下载中心</title>
  <link rel="icon" type="image/png" href="__ICON_URL__" />
  <link rel="apple-touch-icon" href="__ICON_URL__" />
  <style>
    :root { color-scheme: dark; --bg:#050712; --panel:rgba(255,255,255,.08); --line:rgba(255,255,255,.16); --text:#f8fbff; --muted:#a9b5c8; --blue:#4aa8ff; --cyan:#39e7ff; --purple:#8b5cf6; --pink:#ff5fd2; }
    * { box-sizing: border-box; }
    body { margin:0; min-height:100vh; overflow-x:hidden; font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", sans-serif; color:var(--text); background: radial-gradient(circle at 18% 18%, rgba(74,168,255,.34), transparent 28%), radial-gradient(circle at 78% 10%, rgba(139,92,246,.28), transparent 26%), radial-gradient(circle at 62% 86%, rgba(255,95,210,.18), transparent 30%), var(--bg); }
    body::before { content:""; position:fixed; inset:0; pointer-events:none; background-image: linear-gradient(rgba(255,255,255,.045) 1px, transparent 1px), linear-gradient(90deg, rgba(255,255,255,.045) 1px, transparent 1px); background-size: 48px 48px; mask-image: linear-gradient(to bottom, rgba(0,0,0,.9), transparent 82%); }
    .orb { position:fixed; border-radius:999px; filter: blur(14px); opacity:.7; animation: drift 9s ease-in-out infinite alternate; }
    .orb.one { width:240px; height:240px; left:7vw; top:12vh; background:rgba(57,231,255,.22); }
    .orb.two { width:320px; height:320px; right:8vw; top:18vh; background:rgba(139,92,246,.18); animation-delay:-2s; }
    .orb.three { width:220px; height:220px; left:45vw; bottom:5vh; background:rgba(255,95,210,.16); animation-delay:-4s; }
    .page { position:relative; z-index:1; width:min(1180px, calc(100vw - 36px)); min-height:100vh; margin:0 auto; display:grid; align-items:center; padding:56px 0; }
    .hero { display:grid; grid-template-columns: minmax(0, 1.08fr) minmax(360px, .92fr); gap:34px; align-items:center; }
    .badge { display:inline-flex; align-items:center; gap:10px; width:max-content; border:1px solid rgba(74,168,255,.36); border-radius:999px; padding:9px 14px; background:rgba(74,168,255,.12); color:#d9f4ff; font-weight:800; font-size:13px; letter-spacing:.04em; box-shadow:0 0 40px rgba(57,231,255,.12); }
    .dot { width:8px; height:8px; border-radius:999px; background:var(--cyan); box-shadow:0 0 18px var(--cyan); }
    h1 { margin:22px 0 18px; font-size:clamp(44px, 7vw, 86px); line-height:.95; letter-spacing:-.07em; }
    .grad { background:linear-gradient(100deg,#fff 8%,#c8ecff 38%,#b8a7ff 68%,#ffb8ec); -webkit-background-clip:text; background-clip:text; color:transparent; }
    .lead { max-width:680px; margin:0; color:var(--muted); font-size:clamp(17px, 2.2vw, 21px); line-height:1.8; }
    .actions { display:grid; grid-template-columns:repeat(2, minmax(0, 1fr)); gap:16px; width:min(760px, 100%); margin-top:34px; }
    .download-btn { position:relative; isolation:isolate; display:flex; align-items:center; gap:16px; min-width:0; min-height:116px; padding:22px 24px; border-radius:30px; border:1px solid rgba(255,255,255,.18); text-decoration:none; color:white; overflow:hidden; background:linear-gradient(135deg,rgba(74,168,255,.92),rgba(139,92,246,.9)); box-shadow:0 22px 60px rgba(74,168,255,.26); transition: transform .32s cubic-bezier(.2,.8,.2,1), box-shadow .32s ease, border-color .32s ease; }
    .download-btn.android { background:linear-gradient(135deg,rgba(42,220,156,.92),rgba(57,231,255,.82)); box-shadow:0 22px 60px rgba(42,220,156,.2); }
    .download-btn.scratch { grid-column:1 / -1; min-height:104px; background:linear-gradient(135deg,rgba(255,159,64,.94),rgba(255,95,210,.84)); box-shadow:0 22px 60px rgba(255,159,64,.2); }
    .download-btn::before { content:""; position:absolute; inset:-60% -20%; z-index:-1; background:linear-gradient(120deg,transparent 35%,rgba(255,255,255,.36),transparent 65%); transform:translateX(-60%); transition:transform .7s ease; }
    .download-btn:hover { transform:translateY(-4px) scale(1.015); border-color:rgba(255,255,255,.38); box-shadow:0 28px 82px rgba(74,168,255,.34); }
    .download-btn:hover::before { transform:translateX(60%); }
    .download-btn.is-disabled { pointer-events:none; opacity:.48; filter:saturate(.5); }
    .icon { display:grid; flex:0 0 auto; place-items:center; width:58px; height:58px; border-radius:21px; background:rgba(255,255,255,.16); box-shadow:inset 0 1px 0 rgba(255,255,255,.22); }
    .button-copy { min-width:0; }
    .button-copy strong { display:block; overflow:visible; white-space:normal; font-size:24px; line-height:1.15; letter-spacing:-.04em; }
    .button-copy span { display:block; margin-top:8px; overflow:visible; white-space:normal; color:rgba(255,255,255,.82); font-size:14px; line-height:1.35; font-weight:800; }
    .device { position:relative; min-height:610px; display:grid; place-items:center; perspective:1100px; }
    .glass-card { position:relative; width:min(430px, 100%); border:1px solid var(--line); border-radius:42px; background:linear-gradient(180deg,rgba(255,255,255,.16),rgba(255,255,255,.07)); box-shadow:0 40px 110px rgba(0,0,0,.38), inset 0 1px 0 rgba(255,255,255,.18); backdrop-filter: blur(24px); padding:22px; transform:rotateY(-8deg) rotateX(4deg); animation: float 5.6s ease-in-out infinite; }
    .screen { min-height:560px; border-radius:31px; overflow:hidden; border:1px solid rgba(255,255,255,.14); background:linear-gradient(160deg,rgba(8,13,30,.98),rgba(17,26,56,.92)); }
    .screen-top { height:170px; padding:22px; background:radial-gradient(circle at 80% 22%,rgba(57,231,255,.46),transparent 30%), linear-gradient(135deg,rgba(74,168,255,.42),rgba(139,92,246,.34)); }
    .mini-brand { display:flex; align-items:center; gap:10px; font-weight:900; }
    .app-icon { width:34px; height:34px; border-radius:12px; object-fit:cover; box-shadow:0 10px 28px rgba(0,0,0,.2); }
    .brand-icon { width:38px; height:38px; border-radius:14px; object-fit:cover; box-shadow:0 12px 30px rgba(0,0,0,.22), inset 0 1px 0 rgba(255,255,255,.2); }
    .screen-title { margin:34px 0 0; font-size:30px; line-height:1.04; letter-spacing:-.05em; }
    .screen-body { padding:20px; display:grid; gap:14px; }
    .chat-line { height:66px; border-radius:22px; background:rgba(255,255,255,.08); border:1px solid rgba(255,255,255,.1); animation: pulse 3.2s ease-in-out infinite; }
    .chat-line:nth-child(2) { width:84%; animation-delay:-.7s; }
    .chat-line:nth-child(3) { width:92%; animation-delay:-1.3s; }
    .version-card { margin-top:34px; display:inline-grid; gap:8px; grid-template-columns:auto auto; align-items:center; border:1px solid rgba(255,255,255,.14); border-radius:22px; padding:12px 16px; background:rgba(255,255,255,.08); backdrop-filter:blur(14px); color:#d7e3f5; font-weight:800; }
    .version-card span { color:var(--muted); font-size:12px; }
    .version-card strong { font-size:14px; }
    .features { display:grid; grid-template-columns:repeat(3, minmax(0, 1fr)); gap:12px; margin-top:34px; max-width:720px; }
    .feature { border:1px solid rgba(255,255,255,.12); border-radius:22px; padding:16px; background:rgba(255,255,255,.07); backdrop-filter:blur(16px); color:#d8e3f4; }
    .feature strong { display:block; margin-bottom:6px; color:white; }
    .feature span { color:var(--muted); font-size:13px; line-height:1.5; }
    @keyframes float { 0%,100% { transform:rotateY(-8deg) rotateX(4deg) translateY(0); } 50% { transform:rotateY(-4deg) rotateX(2deg) translateY(-14px); } }
    @keyframes drift { from { transform:translate3d(0,0,0) scale(1); } to { transform:translate3d(26px,-22px,0) scale(1.08); } }
    @keyframes pulse { 0%,100% { opacity:.72; transform:translateX(0); } 50% { opacity:1; transform:translateX(6px); } }
    @media (max-width: 920px) { .page { padding:34px 0; } .hero { grid-template-columns:1fr; } .actions { grid-template-columns:1fr; } .download-btn.scratch { grid-column:auto; } .device { min-height:auto; } .glass-card { transform:none; animation:none; } .features { grid-template-columns:1fr; } h1 { font-size:clamp(42px, 13vw, 68px); } }
    @media (prefers-reduced-motion: reduce) { *, *::before, *::after { animation:none !important; transition:none !important; } }
  </style>
</head>
<body>
  <div class="orb one"></div><div class="orb two"></div><div class="orb three"></div>
  <main class="page">
    <section class="hero">
      <div>
        <div class="badge"><img class="app-icon" src="__ICON_URL__" alt="" /><span class="dot"></span>KukeChat Download Center</div>
        <h1><span class="grad">高能聊天</span><br />现在就绪</h1>
        <p class="lead">下载 KukeChat 最新客户端，体验丝滑动效、群聊协作、动态空间和跨端同步。选择适合你的平台，开启更酷的聊天体验。</p>
        <div class="version-card"><span>PC 版本</span><strong>__PC_VERSION__</strong><span>移动端</span><strong>__MOBILE_VERSION__</strong></div>
        <div class="actions">
          <a class="download-btn__PC_DISABLED__" href="__PC_HREF__" target="_blank" rel="noreferrer">
            <span class="icon">__WINDOWS_ICON__</span><span class="button-copy"><strong>下载 PC 端</strong><span>Windows 桌面客户端</span></span>
          </a>
          <a class="download-btn android__ANDROID_DISABLED__" href="__ANDROID_HREF__" target="_blank" rel="noreferrer">
            <span class="icon">__ANDROID_ICON__</span><span class="button-copy"><strong>下载安卓端</strong><span>Android 移动客户端</span></span>
          </a>
          <a class="download-btn scratch__SCRATCH_DISABLED__" href="__SCRATCH_HREF__" target="_blank" rel="noreferrer">
            <span class="icon">__SCRATCH_ICON__</span><span class="button-copy"><strong>Scratch 扩展版</strong><span>CCW / Scratch 扩展链接</span></span>
          </a>
        </div>
        <div class="features"><div class="feature"><strong>QQ 风格体验</strong><span>熟悉的聊天节奏，更现代的视觉质感。</span></div><div class="feature"><strong>实时群聊</strong><span>公告、权限、禁言、动态消息实时同步。</span></div><div class="feature"><strong>高端动效</strong><span>桌面端细腻切换动画，操作反馈更丝滑。</span></div></div>
      </div>
      <div class="device" aria-hidden="true"><div class="glass-card"><div class="screen"><div class="screen-top"><div class="mini-brand"><img class="brand-icon" src="__ICON_URL__" alt="" /><span>KukeChat</span></div><h2 class="screen-title">Chat with<br />motion.</h2></div><div class="screen-body"><div class="chat-line"></div><div class="chat-line"></div><div class="chat-line"></div></div></div></div></div>
    </section>
  </main>
</body>
</html>
"""
        .replace("__PC_VERSION__", pc_version)
        .replace("__MOBILE_VERSION__", mobile_version)
        .replace("__PC_DISABLED__", pc_disabled)
        .replace("__ANDROID_DISABLED__", android_disabled)
        .replace("__SCRATCH_DISABLED__", scratch_disabled)
        .replace("__PC_HREF__", pc_href)
        .replace("__ANDROID_HREF__", android_href)
        .replace("__SCRATCH_HREF__", scratch_href)
        .replace("__ICON_URL__", icon_url)
        .replace("__WINDOWS_ICON__", _svg_windows())
        .replace("__ANDROID_ICON__", _svg_android())
        .replace("__SCRATCH_ICON__", _svg_scratch()),
    )


@app.get("/desktop-update.json", include_in_schema=False)
async def desktop_update_metadata(db: AsyncSession = Depends(get_db)) -> dict[str, str | bool | None]:
    return await export_public_download_metadata(db)


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
