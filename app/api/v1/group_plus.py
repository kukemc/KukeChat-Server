"""群升级、群文件、群待办、聊天记录搜索的用户侧接口（docs/group-plus.md）。

四个 router：plans_router（/group-plus）、conversation_router（/conversations/{cid}/plus、/files、/todo、精华的按 id 取消）、
search_router（/search）。后台接口在 admin_group_plus.py。
"""

from __future__ import annotations

import logging
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, File, Form, Query, Response, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.conversations import online_conversation_member_ids, schedule_realtime_task, send_admin_message_signal, send_message_updates
from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.models.conversation import Conversation
from app.models.message import Message
from app.models.user import User
from app.schemas.group_plus import (
    FileQuota,
    GroupFilePage,
    GroupFilePatch,
    GroupFileLink,
    GroupFileRead,
    GroupFileUploadCompleteRequest,
    GroupFileUploadRequest,
    GroupFileUploadResult,
    GroupFileUploadTicket,
    GroupTodoEnvelope,
    GroupTodoSetRequest,
    GroupTodoSetResult,
    PlansRead,
    PlusPurchaseRequest,
    PlusPurchaseResult,
    PlusQuoteRead,
    PlusQuoteRequest,
    PlusRead,
    PlusRecordPage,
    SearchPage,
    SearchSummary,
)
from app.schemas.message import MessageFeatureToggleRead
from app.services import group_files as file_service
from app.services import group_plus as plus_service
from app.services import group_todos as todo_service
from app.services import message_search as search_service
from app.services import messages as message_service
from app.services import moderation as moderation_service
from app.services import response_cache
from app.services.conversations import require_group_conversation, require_membership
from app.websocket.manager import manager

logger = logging.getLogger(__name__)
plans_router = APIRouter()
conversation_router = APIRouter()
search_router = APIRouter()


async def _broadcast(db: AsyncSession, conversation_id: int, event: str, data: dict) -> None:
    try:
        member_ids = await online_conversation_member_ids(db, conversation_id)
        await manager.send_to_users(member_ids, event, data)
    except Exception:
        # 推送是尽力而为：客户端下次拉取就能看到
        logger.exception("Failed to broadcast %s", event)


async def _publish_message(db: AsyncSession, message: Message, sender: User, whitelisted: bool) -> None:
    """和普通发消息一样：广播、会话列表缓存、后台信号、群等级事件、异步审核。"""
    conversation_id, user_id = message.conversation_id, sender.id
    member_ids = await online_conversation_member_ids(db, conversation_id)
    response_cache.invalidate_user_conversations(set(member_ids) | {user_id})
    try:
        await send_message_updates(db, "message.created", message, member_ids)
    except Exception:
        logger.exception("Committed message %s realtime delivery failed", message.id)
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
    if not whitelisted:
        moderation_service.schedule_message_moderation(message.id)


# ---------------------------------------------------------------- 档位与购买


@plans_router.get("/plans", response_model=PlansRead)
async def plans(
    _current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PlansRead:
    return PlansRead(**await plus_service.plans_view(db))


@conversation_router.get("/{conversation_id}/plus", response_model=PlusRead)
async def group_plus_status(
    conversation_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PlusRead:
    member = await require_membership(db, conversation_id, current_user.id)
    conversation = await db.get(Conversation, conversation_id)
    return PlusRead(**await plus_service.plus_view(db, conversation, member.role))


@conversation_router.post("/{conversation_id}/plus/quote", response_model=PlusQuoteRead)
async def group_plus_quote(
    conversation_id: int,
    payload: PlusQuoteRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PlusQuoteRead:
    return PlusQuoteRead(**await plus_service.quote(db, current_user, conversation_id, payload.tier, payload.period))


@conversation_router.post("/{conversation_id}/plus/purchase", response_model=PlusPurchaseResult)
async def group_plus_purchase(
    conversation_id: int,
    payload: PlusPurchaseRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PlusPurchaseResult:
    result = await plus_service.purchase(db, current_user, conversation_id, payload.model_dump())
    response_cache.invalidate_user_conversations(set(await online_conversation_member_ids(db, conversation_id)) | {current_user.id})
    await _broadcast(
        db,
        conversation_id,
        "group.plus.updated",
        {
            "conversation_id": conversation_id,
            "plus_tier": result["conversation"]["plus_tier"],
            "plus_expires_at": result["conversation"]["plus_expires_at"],
            "reason": "purchase",
        },
    )
    return PlusPurchaseResult(**result)


@conversation_router.get("/{conversation_id}/plus/records", response_model=PlusRecordPage)
async def group_plus_records(
    conversation_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> PlusRecordPage:
    return PlusRecordPage(**await plus_service.list_group_records(db, current_user, conversation_id, limit, offset))


# ---------------------------------------------------------------- 群文件


@conversation_router.post("/{conversation_id}/files", response_model=GroupFileUploadResult, status_code=201)
async def upload_group_file(
    conversation_id: int,
    file: Annotated[UploadFile, File()],
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    announce: Annotated[bool, Form()] = True,
    permanent: Annotated[bool, Form()] = False,
) -> GroupFileUploadResult:
    user_id, whitelisted = current_user.id, current_user.content_moderation_whitelisted
    view, message = await file_service.upload_file(db, current_user, conversation_id, file, announce=announce, permanent=permanent)
    return await _file_uploaded(db, conversation_id, current_user, user_id, whitelisted, view, message)


async def _file_uploaded(
    db: AsyncSession, conversation_id: int, current_user: User, user_id: int, whitelisted: bool, view: dict, message: Message | None
) -> GroupFileUploadResult:
    """multipart 上传和直传 complete 共用：广播文件消息和 group.file.updated。"""
    message_data = None
    if message is not None:
        message_data = message_service.build_new_message_read(message, user_id)
        await _publish_message(db, message, current_user, whitelisted)
    await _broadcast(db, conversation_id, "group.file.updated", {"conversation_id": conversation_id, "action": "uploaded", "file_id": view["id"]})
    return GroupFileUploadResult(file=view, message=message_data)


@conversation_router.post("/{conversation_id}/files/uploads", response_model=GroupFileUploadTicket, status_code=201)
async def create_group_file_upload(
    conversation_id: int,
    payload: GroupFileUploadRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> GroupFileUploadTicket:
    ticket = await file_service.create_upload(
        db, current_user, conversation_id, name=payload.name, size=payload.size, content_type=payload.content_type, permanent=payload.permanent
    )
    return GroupFileUploadTicket(**ticket)


@conversation_router.post("/{conversation_id}/files/uploads/{upload_id}/complete", response_model=GroupFileUploadResult, status_code=201)
async def complete_group_file_upload(
    conversation_id: int,
    upload_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    payload: GroupFileUploadCompleteRequest | None = None,
) -> GroupFileUploadResult:
    user_id, whitelisted = current_user.id, current_user.content_moderation_whitelisted
    announce = payload.announce if payload is not None else True
    view, message = await file_service.complete_upload(db, current_user, conversation_id, upload_id, announce=announce)
    return await _file_uploaded(db, conversation_id, current_user, user_id, whitelisted, view, message)


@conversation_router.get("/{conversation_id}/files", response_model=GroupFilePage)
async def list_group_files(
    conversation_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    q: str | None = Query(default=None, max_length=100),
    uploader_id: int | None = Query(default=None, ge=1),
    limit: int = Query(default=30, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> GroupFilePage:
    return GroupFilePage(**await file_service.list_files(db, current_user.id, conversation_id, q=q, uploader_id=uploader_id, limit=limit, offset=offset))


@conversation_router.get("/{conversation_id}/files/usage", response_model=FileQuota)
async def group_file_usage(
    conversation_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> FileQuota:
    return FileQuota(**await file_service.usage(db, current_user.id, conversation_id))


@conversation_router.get("/{conversation_id}/files/{file_id}/download")
async def download_group_file(
    conversation_id: int,
    file_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    return await file_service.download(db, current_user.id, conversation_id, file_id)


@conversation_router.get("/{conversation_id}/files/{file_id}/link", response_model=GroupFileLink)
async def group_file_link(
    conversation_id: int,
    file_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> GroupFileLink:
    return GroupFileLink(**await file_service.download_link(db, current_user.id, conversation_id, file_id))


@conversation_router.delete("/{conversation_id}/files/{file_id}", status_code=204)
async def delete_group_file(
    conversation_id: int,
    file_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    await file_service.delete_file(db, current_user.id, conversation_id, file_id)
    await _broadcast(db, conversation_id, "group.file.updated", {"conversation_id": conversation_id, "action": "deleted", "file_id": file_id})
    return Response(status_code=204)


@conversation_router.patch("/{conversation_id}/files/{file_id}", response_model=GroupFileRead)
async def patch_group_file(
    conversation_id: int,
    file_id: int,
    payload: GroupFilePatch,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> GroupFileRead:
    view = await file_service.set_permanent(db, current_user.id, conversation_id, file_id, payload.permanent)
    await _broadcast(db, conversation_id, "group.file.updated", {"conversation_id": conversation_id, "action": "updated", "file_id": file_id})
    return GroupFileRead(**view)


# ---------------------------------------------------------------- 精华（按 id 取消）


@conversation_router.delete("/{conversation_id}/featured-messages/{featured_id}", response_model=MessageFeatureToggleRead)
async def remove_featured_message(
    conversation_id: int,
    featured_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> MessageFeatureToggleRead:
    message_id = await message_service.remove_featured_by_id(db, conversation_id, current_user.id, featured_id)
    await _broadcast(
        db, conversation_id, "message.featured.updated", {"conversation_id": conversation_id, "message_id": message_id, "featured": False}
    )
    return MessageFeatureToggleRead(featured=False)


# ---------------------------------------------------------------- 群待办


@conversation_router.get("/{conversation_id}/todo", response_model=GroupTodoEnvelope)
async def get_group_todo(
    conversation_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> GroupTodoEnvelope:
    await require_membership(db, conversation_id, current_user.id)
    await require_group_conversation(db, conversation_id)
    return GroupTodoEnvelope(todo=await todo_service.get_todo(db, conversation_id, current_user.id))


@conversation_router.put("/{conversation_id}/todo", response_model=GroupTodoSetResult)
async def set_group_todo(
    conversation_id: int,
    payload: GroupTodoSetRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> GroupTodoSetResult:
    user_id, whitelisted = current_user.id, current_user.content_moderation_whitelisted
    todo, reminder = await todo_service.set_todo(db, current_user, conversation_id, payload.message_id)
    message_data = message_service.build_new_message_read(reminder, user_id)
    await _publish_message(db, reminder, current_user, whitelisted)
    await _broadcast(db, conversation_id, "group.todo.updated", {"conversation_id": conversation_id, "todo": todo})
    return GroupTodoSetResult(todo=todo, message=message_data)


@conversation_router.delete("/{conversation_id}/todo", status_code=204)
async def clear_group_todo(
    conversation_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    await todo_service.clear_todo(db, current_user.id, conversation_id)
    await _broadcast(db, conversation_id, "group.todo.updated", {"conversation_id": conversation_id, "todo": None})
    return Response(status_code=204)


# ---------------------------------------------------------------- 聊天记录搜索


@search_router.get("/messages", response_model=SearchPage)
async def search_my_messages(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    q: str | None = Query(default=None, max_length=100),
    conversation_id: int | None = Query(default=None, ge=1),
    sender_id: int | None = Query(default=None, ge=1),
    category: Literal["all", "media", "sticker", "file", "link"] = "all",
    date_from: str | None = Query(default=None, max_length=40),
    date_to: str | None = Query(default=None, max_length=40),
    limit: int = Query(default=20, ge=1, le=50),
    offset: int = Query(default=0, ge=0, le=100000),
    before_id: int | None = Query(default=None, ge=1),
) -> SearchPage:
    """搜自己的聊天记录。只搜自己所在会话里自己可见的消息，没有任何管理员 / 全站入口。"""
    return SearchPage(
        **await search_service.search(
            db,
            current_user.id,
            q=q,
            conversation_id=conversation_id,
            sender_id=sender_id,
            category=category,
            date_from=date_from,
            date_to=date_to,
            limit=limit,
            offset=offset,
            before_id=before_id,
        )
    )


@search_router.get("/messages/summary", response_model=SearchSummary)
async def search_my_messages_summary(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    q: str = Query(min_length=1, max_length=100),
    category: Literal["all", "media", "sticker", "file", "link"] = "all",
    sender_id: int | None = Query(default=None, ge=1),
    date_from: str | None = Query(default=None, max_length=40),
    date_to: str | None = Query(default=None, max_length=40),
    limit: int = Query(default=50, ge=1, le=100),
) -> SearchSummary:
    return SearchSummary(
        **await search_service.summary(
            db, current_user.id, q=q, category=category, sender_id=sender_id, date_from=date_from, date_to=date_to, limit=limit
        )
    )

