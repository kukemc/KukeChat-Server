"""游戏模式：作品内嵌聊天。

这些端点面向 CCW 作品里的游玩者，其中会话与消息读取**允许匿名访问** ——
这是游戏模式的核心体验（没账号也能看到聊天），代价是开启游戏模式的群其近期
消息对所有游玩者公开。因此开启条件被限制在「公开群 + 群主显式开启」，
见 services/game_mode.py。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import get_current_user, get_current_user_from_token
from app.models.user import User
from app.schemas.game import GameMessageCreate, GameModeRead, GameModeUpdate, GameSessionRead
from app.schemas.message import MessageRead
from app.services import game_mode as game_service
from app.services import messages as message_service

router = APIRouter()

# auto_error=False：未带令牌时返回 None 而不是 401，游戏模式要支持匿名只读
optional_bearer = HTTPBearer(auto_error=False)


async def get_optional_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(optional_bearer)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> User | None:
    if credentials is None or not credentials.credentials.strip():
        return None
    try:
        return await get_current_user_from_token(db, credentials.credentials)
    except HTTPException:
        # 令牌过期或无效时退化为匿名只读，而不是把玩家挡在作品外面
        return None


@router.get("/session", response_model=GameSessionRead)
async def game_session(
    db: Annotated[AsyncSession, Depends(get_db)],
    viewer: Annotated[User | None, Depends(get_optional_user)],
    conversation_id: Annotated[int, Query(gt=0)],
    creation_oid: Annotated[str, Query(min_length=1, max_length=64)],
) -> GameSessionRead:
    """校验群与作品的绑定，返回聊天接入所需信息与最近的历史消息。"""
    conversation = await game_service.resolve_game_conversation(db, conversation_id, creation_oid)
    return await game_service.build_game_session(db, conversation, viewer)


@router.post("/messages", response_model=MessageRead, status_code=status.HTTP_201_CREATED)
async def game_send_message(
    payload: GameMessageCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> MessageRead:
    """在游戏模式群里发言。非成员会在此刻自动入群。"""
    conversation = await game_service.resolve_game_conversation(db, payload.conversation_id, payload.creation_oid)
    message, _joined = await game_service.send_game_message(db, conversation, current_user, payload.content)

    from app.api.v1.conversations import send_message_updates
    from app.services.conversations import conversation_member_ids

    member_ids = await conversation_member_ids(db, conversation.id)
    await send_message_updates(db, "message.created", message, member_ids)
    return await message_service.build_message_read(db, message, current_user.id)


@router.patch("/conversations/{conversation_id}", response_model=GameModeRead)
async def update_game_mode(
    conversation_id: int,
    payload: GameModeUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> GameModeRead:
    """群主开启 / 关闭游戏模式并绑定作品。"""
    return await game_service.set_game_mode(db, conversation_id, current_user, payload)


@router.get("/conversations/{conversation_id}", response_model=GameModeRead)
async def read_game_mode(
    conversation_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> GameModeRead:
    return await game_service.get_game_mode(db, conversation_id, current_user)
