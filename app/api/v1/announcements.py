from __future__ import annotations

import asyncio
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import AsyncSessionLocal, get_db
from app.core.dependencies import get_current_user
from app.models.user import User
from app.schemas.announcement import (
    AnnouncementReactionCreate,
    AnnouncementReactionOptions,
    AnnouncementReactionResult,
    AnnouncementRead,
)
from app.services import announcements as announcement_service
from app.websocket.manager import manager

logger = logging.getLogger(__name__)
router = APIRouter()

REACTION_EVENT = "announcement.reaction.updated"
# 回应变化合并推送：同一条公告在这段时间内不管变了多少次，到点只推一次最新的汇总（每个进程各自合并）
REACTION_BROADCAST_DELAY_SECONDS = 1.0
_pending_reaction_broadcasts: set[int] = set()
_reaction_broadcast_tasks: set[asyncio.Task[None]] = set()


async def _broadcast_reactions(announcement_id: int) -> None:
    try:
        await asyncio.sleep(REACTION_BROADCAST_DELAY_SECONDS)
    finally:
        # 先摘掉标记再读库：读库之后才到的变化会另排一次，不会丢
        _pending_reaction_broadcasts.discard(announcement_id)
    try:
        async with AsyncSessionLocal() as db:
            payload = await announcement_service.reaction_event_payload(db, announcement_id)
        if payload is not None:
            await manager.broadcast(REACTION_EVENT, payload)
    except Exception:
        # 推送是尽力而为：回应已经落库，接口也早就返回了，这里只记日志
        logger.exception("Announcement %s reaction broadcast failed", announcement_id)


def _schedule_reaction_broadcast(announcement_id: int) -> None:
    if announcement_id in _pending_reaction_broadcasts:
        return
    _pending_reaction_broadcasts.add(announcement_id)
    # 用自己的会话读库，不占用请求的；任务要留一个强引用，否则可能在跑完之前被回收
    task = asyncio.create_task(_broadcast_reactions(announcement_id))
    _reaction_broadcast_tasks.add(task)
    task.add_done_callback(_reaction_broadcast_tasks.discard)


@router.get("", response_model=list[AnnouncementRead])
async def list_announcements(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
) -> list[AnnouncementRead]:
    return await announcement_service.list_active(db, limit, current_user.id)


@router.get("/latest", response_model=AnnouncementRead | None)
async def latest_announcement(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AnnouncementRead | None:
    return await announcement_service.get_latest(db, current_user.id)


# 固定路径放在带 {announcement_id} 的路径前面
@router.get("/reaction-options", response_model=AnnouncementReactionOptions)
async def announcement_reaction_options(
    current_user: Annotated[User, Depends(get_current_user)],
) -> AnnouncementReactionOptions:
    del current_user
    return AnnouncementReactionOptions(**announcement_service.reaction_options())


@router.post("/{announcement_id}/reactions", response_model=AnnouncementReactionResult)
async def toggle_announcement_reaction(
    announcement_id: int,
    payload: AnnouncementReactionCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AnnouncementReactionResult:
    reactions, changed = await announcement_service.toggle_reaction(db, current_user, announcement_id, payload.emoji)
    if changed:
        _schedule_reaction_broadcast(announcement_id)
    return AnnouncementReactionResult(id=announcement_id, reactions=reactions)


@router.delete("/{announcement_id}/reactions/{emoji}", response_model=AnnouncementReactionResult)
async def delete_announcement_reaction(
    announcement_id: int,
    emoji: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AnnouncementReactionResult:
    reactions, changed = await announcement_service.remove_reaction(db, current_user, announcement_id, emoji)
    if changed:
        _schedule_reaction_broadcast(announcement_id)
    return AnnouncementReactionResult(id=announcement_id, reactions=reactions)
