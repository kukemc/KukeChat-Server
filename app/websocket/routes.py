from __future__ import annotations

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect, status

from app.core.database import AsyncSessionLocal
from app.core.dependencies import get_current_user_from_token
from app.models.enums import PlatformRole
from app.models.user import User
from app.services.auth import mark_user_seen
from app.services import response_cache
from app.services.bots import authenticate_bot_key
from app.websocket.manager import admin_manager, bot_manager, manager


router = APIRouter()


def get_websocket_ip(websocket: WebSocket) -> str:
    forwarded_for = websocket.headers.get("x-forwarded-for")
    if forwarded_for:
        first_ip = forwarded_for.split(",", 1)[0].strip()
        if first_ip:
            return first_ip
    real_ip = websocket.headers.get("x-real-ip")
    if real_ip:
        return real_ip.strip()
    if websocket.client is not None:
        return websocket.client.host
    return "unknown"


async def build_presence_payload(user: User, online: bool) -> dict:
    return {
        "user_id": user.id,
        "online": online,
        "online_count": await manager.online_count_global(),
        "presence_status": user.presence_status if online else "offline",
        "presence_text": user.presence_text if online else None,
        "presence_updated_at": user.presence_updated_at.isoformat() if user.presence_updated_at else None,
    }


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket, token: str = Query(...)) -> None:
    async with AsyncSessionLocal() as db:
        try:
            user = await get_current_user_from_token(db, token)
        except Exception:
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return
        mark_user_seen(user, get_websocket_ip(websocket))
        await db.commit()

    became_online = await manager.connect(user.id, websocket)
    await manager.send_to_user(user.id, "connection.ready", {"user_id": user.id})
    if became_online:
        response_cache.invalidate_online_users()
        await manager.broadcast("presence.updated", await build_presence_payload(user, True))
    try:
        while True:
            message = await websocket.receive_text()
            if message.strip().lower() == "ping":
                await websocket.send_text("pong")
    except (WebSocketDisconnect, RuntimeError):
        became_offline = await manager.disconnect(user.id, websocket)
        if became_offline:
            response_cache.invalidate_online_users()
            await manager.broadcast("presence.updated", await build_presence_payload(user, False))


@router.websocket("/admin/ws")
async def admin_websocket_endpoint(websocket: WebSocket, token: str = Query(...)) -> None:
    async with AsyncSessionLocal() as db:
        try:
            user = await get_current_user_from_token(db, token)
        except Exception:
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return
        if user.platform_role != PlatformRole.ADMIN:
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return

    await admin_manager.connect(user.id, websocket)
    await admin_manager.broadcast("admin.connection.ready", {"admin_id": user.id})
    try:
        while True:
            message = await websocket.receive_text()
            if message.strip().lower() == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        await admin_manager.disconnect(user.id, websocket)


@router.websocket("/bot/ws")
async def bot_websocket_endpoint(websocket: WebSocket, key: str = Query(...)) -> None:
    async with AsyncSessionLocal() as db:
        try:
            context = await authenticate_bot_key(db, key)
        except Exception:
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return

    await bot_manager.connect(context.bot.id, websocket)
    await bot_manager.send_to_bot(context.bot.id, "bot.connection.ready", {"bot_id": context.bot.id, "user_id": context.user.id})
    try:
        while True:
            message = await websocket.receive_text()
            if message.strip().lower() == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        await bot_manager.disconnect(context.bot.id, websocket)
