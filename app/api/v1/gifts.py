"""礼物的用户侧接口（docs/gifts.md），挂在 /gifts 下；礼物墙那两条用 users_router 挂在 /users 下。都要求登录。

只有 POST /gifts 看总开关（关着是 403 disabled）；目录、礼物墙、记录、心意榜这些只读接口开关关着也照常返回数据。
"""

from __future__ import annotations

import logging
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.conversations import online_conversation_member_ids, schedule_realtime_task, send_admin_message_signal, send_message_updates
from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.models.user import User
from app.schemas.gift import CharmBoard, GiftCatalog, GiftCreate, GiftMinePage, GiftRecordPage, GiftSendResult, GiftWall
from app.services import gifts as gift_service
from app.services import intimacy as intimacy_service
from app.services import sparks as spark_service
from app.services import messages as message_service
from app.services import moderation as moderation_service
from app.services import response_cache
from app.websocket.manager import manager

logger = logging.getLogger(__name__)
router = APIRouter()
users_router = APIRouter()


@router.get("/catalog", response_model=GiftCatalog)
async def gift_catalog(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> GiftCatalog:
    """开关、上架中的礼物、限制和我今天已经送了几次。功能关着也能调（enabled 为 false）。"""
    return GiftCatalog(**await gift_service.catalog_view(db, current_user))


# 带不带结尾的斜杠都接：契约写的是 POST /gifts，不能让客户端吃一次 307
@router.post("", response_model=GiftSendResult)
@router.post("/", response_model=GiftSendResult, include_in_schema=False)
async def send_gift(
    payload: GiftCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> GiftSendResult:
    """送礼物 / 送商品：立刻扣币。在会话里送的落一条 type = gift 的消息，广播和发普通消息一样。"""
    user_id, whitelisted = current_user.id, current_user.content_moderation_whitelisted
    result, message = await gift_service.send_gift(db, current_user, payload.model_dump())
    # 好友之间送礼涨亲密度（docs/intimacy.md）：后台用自己的会话记，失败只记日志
    intimacy_service.schedule_gift(result["record"]["id"], bind=db.bind)
    if message is None:
        return GiftSendResult(record=result["record"], message=None, balance=result["balance"])
    conversation_id = message.conversation_id
    # 好友火花（docs/sparks.md）：在私聊里送礼也算发言
    spark_service.schedule_message(conversation_id, user_id, message.type, message.created_at, bind=db.bind)
    message_data = message_service.build_new_message_read(message, user_id)
    member_ids = await online_conversation_member_ids(db, conversation_id)
    response_cache.invalidate_user_conversations(set(member_ids) | {user_id})
    try:
        await send_message_updates(db, "message.created", message, member_ids)
    except Exception:
        logger.exception("Committed gift message %s realtime delivery failed", message.id)
    schedule_realtime_task(send_admin_message_signal("admin.message.changed", message, "created"))
    previous_level = getattr(message, "_level_previous", None)
    current_level = getattr(message, "_level_current", None)
    if isinstance(previous_level, int) and isinstance(current_level, int) and current_level > previous_level:
        schedule_realtime_task(
            manager.send_to_users(
                member_ids,
                "group.member.level_updated",
                {"conversation_id": conversation_id, "user_id": user_id, "level": current_level, "level_exp": getattr(message, "_sender_level_exp", 0)},
            )
        )
    if not whitelisted and result["record"]["message"]:
        # 留言送之前已经审过一遍；这里和别的消息一样再走一次异步审核（审核服务当时不可用的话靠它兜底）。没写留言的没有可审的
        moderation_service.schedule_message_moderation(message.id)
    return GiftSendResult(record=result["record"], message=message_data, balance=result["balance"])


@router.get("/mine", response_model=GiftMinePage)
async def my_gifts(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    direction: Literal["sent", "received"] = "received",
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> GiftMinePage:
    return GiftMinePage(**await gift_service.list_mine(db, current_user, direction, limit, offset))


@router.get("/leaderboard", response_model=CharmBoard)
async def charm_leaderboard(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    period: Literal["all", "month"] = "all",
    limit: int = Query(default=50, ge=1, le=gift_service.BOARD_MAX),
) -> CharmBoard:
    """心意榜：总榜 / 本月榜（UTC+8 的自然月）。结果在进程内缓存，不是每次请求都算。"""
    return CharmBoard(**await gift_service.leaderboard(db, current_user, period, limit))


@users_router.get("/{user_id}/gifts", response_model=GiftWall)
async def user_gift_wall(
    user_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> GiftWall:
    """礼物墙：这个人收到的礼物按种类聚合，公开。"""
    return GiftWall(**await gift_service.wall(db, user_id))


@users_router.get("/{user_id}/gifts/records", response_model=GiftRecordPage)
async def user_gift_records(
    user_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    gift_key: str | None = Query(default=None, max_length=64),
    item_sku: str | None = Query(default=None, max_length=80),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> GiftRecordPage:
    """礼物墙上的送礼记录，公开。留言只有送的人和收的人自己看得到，别人拿到的是 null。"""
    return GiftRecordPage(**await gift_service.wall_records(db, current_user, user_id, gift_key=gift_key, item_sku=item_sku, limit=limit, offset=offset))
