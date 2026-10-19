from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, HTTPException, Query, WebSocket, WebSocketDisconnect, status

from app.core.database import AsyncSessionLocal
from app.core.dependencies import get_current_user_from_token
from app.models.enums import PlatformRole
from app.models.user import User
from app.services.auth import mark_user_seen
from app.services import response_cache
from app.services import typing as typing_service
from app.services.bots import authenticate_bot_key
from app.websocket.manager import admin_manager, bot_manager, game_manager, manager, parse_features


router = APIRouter()
logger = logging.getLogger(__name__)


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


def _consume_task_error(task: asyncio.Task) -> None:
    try:
        task.result()
    except Exception:
        logger.exception("Client websocket event failed")


def _handle_client_event(user_id: int, raw: str) -> None:
    """客户端上行的 JSON 事件。目前只有「正在输入」；格式不对或不认识的一律忽略。"""
    if len(raw) > 512:
        return
    try:
        event = json.loads(raw)
    except json.JSONDecodeError:
        return
    if not isinstance(event, dict) or event.get("type") != "typing":
        return
    conversation_id = event.get("conversation_id")
    if not isinstance(conversation_id, int) or isinstance(conversation_id, bool) or conversation_id <= 0:
        return
    # 不在接收循环里等数据库，避免挡住心跳
    asyncio.create_task(typing_service.handle_typing(user_id, conversation_id)).add_done_callback(_consume_task_error)


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket, token: str = Query(...), features: str = Query(default="", max_length=200)) -> None:
    # features：客户端认识的新东西，逗号分隔。目前只有 self（「我的设备」会话的事件，docs/self-chat.md 第 6 节）
    async with AsyncSessionLocal() as db:
        try:
            user = await get_current_user_from_token(db, token)
        except Exception:
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return
        mark_user_seen(user, get_websocket_ip(websocket))
        await db.commit()

    became_online = await manager.connect(user.id, websocket, parse_features(features))
    await manager.send_to_user(user.id, "connection.ready", {"user_id": user.id})
    if became_online:
        response_cache.invalidate_online_users()
        await manager.broadcast("presence.updated", await build_presence_payload(user, True))
    try:
        while True:
            message = await websocket.receive_text()
            if message.strip().lower() == "ping":
                await manager.send_text(user.id, websocket, "pong")
            elif message.startswith("{"):
                _handle_client_event(user.id, message)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
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
                await admin_manager.send_text(user.id, websocket, "pong")
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
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
                await bot_manager.send_text(context.bot.id, websocket, "pong")
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        await bot_manager.disconnect(context.bot.id, websocket)


@router.websocket("/game/ws")
async def game_websocket_endpoint(
    websocket: WebSocket,
    conversation_id: int = Query(..., gt=0),
    creation_oid: str = Query(..., min_length=1, max_length=64),
) -> None:
    """游戏模式的匿名订阅通道。

    作品里的游玩者不需要账号就能接收消息，因此这里不校验令牌，
    只校验「该群确实开启了游戏模式且绑定的正是这个作品」。
    """
    from app.core.database import AsyncSessionLocal
    from app.services import game_mode as game_service

    async with AsyncSessionLocal() as db:
        try:
            await game_service.resolve_game_conversation(db, conversation_id, creation_oid)
        except HTTPException as exc:
            await websocket.close(code=4403, reason=str(exc.detail)[:120])
            return

    await game_manager.connect(conversation_id, websocket)
    try:
        while True:
            # 客户端只收不发，这里仅用于保持连接并感知断开
            await websocket.receive_text()
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        await game_manager.disconnect(conversation_id, websocket)
