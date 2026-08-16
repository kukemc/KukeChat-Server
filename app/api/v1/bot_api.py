from __future__ import annotations

import html
import json
import re
import shlex
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.api.v1.conversations import display_name, emit_system_message, mute_duration_label, online_conversation_member_ids, schedule_realtime_task, send_admin_message_signal, send_bot_conversation_event, send_conversation_updates, send_message_updates, send_reaction_updates
from app.api.v1.uploads import UploadRead, save_audio_upload, save_image_upload
from app.core.database import get_db
from app.core.dependencies import get_current_bot
from app.models.bot import Bot, BotInstallation
from app.models.conversation import ConversationMember
from app.models.enums import GroupJoinRequestStatus, MemberRole, MessageType
from app.models.message import Message
from app.schemas.bot import (
    BotConversationInfoRead,
    BotConversationMembersRead,
    BotDirectMessageCreate,
    BotJoinRequestReview,
    BotMemberMuteUpdate,
    BotMemberRoleUpdate,
    BotMessageComponentUpdate,
    BotRuntimeMeRead,
    BotSendMessageCreate,
    BotUserPublicInfoRead,
)
from app.schemas.common import MessageResponse
from app.schemas.conversation import ConversationMemberRead, ConversationRead, GroupJoinRequestDecision, GroupJoinRequestRead
from app.schemas.message import MessageReactionCreate, MessageReactionSummary, MessageRead
from app.schemas.user import OnlineUsersRead, UserPublic
from app.services import bots as bot_service
from app.services import conversations as conversation_service
from app.services import messages as message_service
from app.services import moderation as moderation_service
from app.services import response_cache
from app.services.bots import BotContext
from app.models.user import User
from app.websocket.manager import bot_manager, manager

router = APIRouter()
BOT_MARKDOWN_MAX_LENGTH = 8000
BOT_MAX_ELEMENTS = 50
BOT_ELEMENT_MESSAGE_MAX_LENGTH = 8000
BOT_INTERACTIVE_TEXT_MAX_LENGTH = 500
_ELEMENT_TAG_RE = re.compile(r"<\s*([a-zA-Z][\w-]*)([^<>]*?)(/?)\s*>")
_CONTENT_ELEMENT_RE = re.compile(r"<\s*(markdown|md|button|btn|link)\s*([^<>]*?)>(.*?)<\s*/\s*\1\s*>", re.IGNORECASE | re.DOTALL)
_MARKDOWN_IMAGE_RE = re.compile(r"!\[[^\]\n]{0,80}\]\(([^\s)<>]{1,500})\)")
_MARKDOWN_IMAGE_TAG_RE = re.compile(r"<\s*(img|image)\b([^<>]*?)(?:/\s*>|>(.*?)<\s*/\s*\1\s*>)", re.IGNORECASE | re.DOTALL)
_MARKDOWN_INTERACTION_TAG_RE = re.compile(r"<\s*(button|btn|link)\b([^<>]*?)(?:/\s*>|>(.*?)<\s*/\s*\1\s*>)", re.IGNORECASE | re.DOTALL)
_MARKDOWN_MENTION_TAG_RE = re.compile(r"<\s*(at|mention|at_all|mention_all)\b([^<>]*?)/\s*>", re.IGNORECASE)


def _element_attrs(element: Any) -> dict[str, Any]:
    if isinstance(element, dict):
        attrs = element.get("attrs") if isinstance(element.get("attrs"), dict) else {}
        extra = {key: value for key, value in element.items() if key not in {"type", "name", "attrs", "content"}}
        return {**extra, **attrs}
    attrs = element.attrs if isinstance(getattr(element, "attrs", None), dict) else {}
    extra = getattr(element, "model_extra", None)
    if isinstance(extra, dict):
        attrs = {**extra, **attrs}
    return attrs


def _element_name(element: Any) -> str:
    if isinstance(element, dict):
        raw = element.get("type") or element.get("name") or "text"
        return str(raw).strip().lower().replace("-", "_")
    raw = getattr(element, "type", None) or getattr(element, "name", None) or "text"
    return str(raw).strip().lower().replace("-", "_")


def _element_content(element: Any) -> str:
    if isinstance(element, dict):
        value = element.get("content")
    else:
        value = getattr(element, "content", None)
    return str(value or "").strip()


def _string_attr(attrs: dict[str, Any], *names: str) -> str | None:
    for name in names:
        value = attrs.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, (int, float)):
            return str(value)
    return None


def _int_attr(attrs: dict[str, Any], *names: str) -> int | None:
    for name in names:
        value = attrs.get(name)
        if isinstance(value, str):
            value = value.strip()
            if value.startswith("{") and value.endswith("}"):
                value = value[1:-1].strip()
        try:
            integer = int(value)
        except (TypeError, ValueError):
            continue
        if integer > 0:
            return integer
    return None


def _quote_preview(content: str) -> str:
    return " ".join(content.split())[:120]


def _validate_markdown_image_urls(content: str) -> None:
    for match in _MARKDOWN_IMAGE_RE.finditer(content):
        _validate_markdown_image_url(match.group(1))
    for match in _MARKDOWN_IMAGE_TAG_RE.finditer(content):
        attrs = _parse_element_attrs(match.group(2).strip())
        _validate_markdown_image_url(_string_attr(attrs, "src", "url", "href") or html.unescape(match.group(3) or ""))


def _validate_markdown_image_url(raw_url: str) -> str:
    url = raw_url.strip()
    if not url:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Markdown image URL is required")
    lower = url.lower()
    if lower.startswith(("javascript:", "data:", "vbscript:", "file:")):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Markdown image URL is not allowed")
    path = lower.split("?", 1)[0].split("#", 1)[0]
    if not path.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif")):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Markdown image URL must point to an image")
    if lower.startswith("http://") or lower.startswith("https://") or lower.startswith("/uploads/"):
        return url
    raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Markdown image URL is not allowed")


def _validate_interaction_text(value: str, detail: str) -> str:
    text = value.strip()
    if not text:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=detail)
    if len(text) > BOT_INTERACTIVE_TEXT_MAX_LENGTH:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Bot interactive value must not exceed 500 characters")
    return text


def _validate_interaction_href(value: str) -> str:
    href = value.strip()
    if not href:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Bot link href is required")
    if len(href) > BOT_INTERACTIVE_TEXT_MAX_LENGTH or re.search(r"[\x00-\x1f<>\"'`]", href):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Bot link href is not allowed")
    parsed = urlparse(href)
    if parsed.scheme not in {"http", "https"}:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Bot link href must be HTTP or HTTPS")
    return href


def _validate_component_id(value: str | None) -> str | None:
    if value is None:
        return None
    component_id = value.strip()
    if not component_id:
        return None
    if len(component_id) > 64 or not re.fullmatch(r"[A-Za-z0-9_-]+", component_id):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Bot button id must use 1-64 letters, numbers, _ or -")
    return component_id


def _message_metadata(message: Message) -> dict[str, Any]:
    if not message.metadata_json:
        return {}
    try:
        metadata = json.loads(message.metadata_json)
    except (TypeError, json.JSONDecodeError):
        return {}
    return metadata if isinstance(metadata, dict) else {}


def _component_state_payload(payload: BotMessageComponentUpdate) -> dict[str, Any]:
    data: dict[str, Any] = {}
    for key in ("label", "variant", "border_color", "text_color", "background_color", "disabled"):
        value = getattr(payload, key)
        if value is not None:
            data[key] = value
    if not data:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="No component state fields provided")
    return data


def _component_id_from_attrs(attrs: dict[str, Any]) -> str | None:
    return _validate_component_id(_string_attr(attrs, "component_id", "componentId", "button_id", "buttonId", "id"))


def _metadata_has_button_component(metadata: dict[str, Any], component_id: str) -> bool:
    raw_elements = metadata.get("elements")
    if not isinstance(raw_elements, list):
        return False
    for element in raw_elements:
        if not isinstance(element, dict):
            continue
        element_type = _element_name(element)
        attrs = _element_attrs(element)
        if element_type in {"button", "btn"} and _component_id_from_attrs(attrs) == component_id:
            return True
        if element_type in {"markdown", "md"}:
            content = element.get("content")
            if not isinstance(content, str):
                continue
            for match in _MARKDOWN_INTERACTION_TAG_RE.finditer(content):
                tag = match.group(1).strip().lower()
                if tag not in {"button", "btn"}:
                    continue
                if _component_id_from_attrs(_parse_element_attrs(match.group(2).strip())) == component_id:
                    return True
    return False


def _validate_markdown_interactions(content: str) -> None:
    for match in _MARKDOWN_INTERACTION_TAG_RE.finditer(content):
        tag = match.group(1).strip().lower()
        attrs = _parse_element_attrs(match.group(2).strip())
        label = html.unescape(match.group(3) or "").strip() or _string_attr(attrs, "label", "text", "title") or ("链接" if tag == "link" else "按钮")
        if len(label) > 80:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Bot interactive label must not exceed 80 characters")
        _validate_component_id(_string_attr(attrs, "component_id", "componentId", "button_id", "buttonId", "id"))
        action = (_string_attr(attrs, "action", "type", "mode") or ("open" if tag == "link" else "callback")).lower().replace("-", "_")
        action = "input" if action in {"insert", "fill", "preset", "text"} else "callback" if action == "callback" else "open" if tag == "link" else "callback"
        if action == "input":
            _validate_interaction_text(_string_attr(attrs, "value", "text", "input", "command") or label, "Bot input value is required")
        elif action == "callback":
            _validate_interaction_text(_string_attr(attrs, "id", "action_id", "actionId", "key", "value") or label, "Bot callback action_id is required")
        elif action == "open":
            _validate_interaction_href(_string_attr(attrs, "href", "url") or "")


def _markdown_mentions(content: str) -> tuple[list[dict[str, Any]], bool]:
    mentions: list[dict[str, Any]] = []
    mention_all = False
    for match in _MARKDOWN_MENTION_TAG_RE.finditer(content):
        tag = match.group(1).strip().lower().replace("-", "_")
        attrs = _parse_element_attrs(match.group(2).strip())
        if tag in {"at_all", "mention_all"}:
            mention_all = True
            continue
        target_id = _int_attr(attrs, "id", "user_id", "userId")
        if target_id:
            name = _string_attr(attrs, "name", "nickname", "username") or f"用户 {target_id}"
            mentions.append({"user_id": target_id, "name": name})
    return mentions, mention_all


def _parse_element_attrs(raw: str) -> dict[str, Any]:
    attrs: dict[str, Any] = {}
    lexer = shlex.shlex(raw, posix=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    for token in lexer:
        if "=" not in token:
            attrs[token] = True
            continue
        key, value = token.split("=", 1)
        key = key.strip()
        if not key:
            continue
        value = html.unescape(value.strip().strip('"').strip("'"))
        attrs[key] = value
    return attrs


def _append_text_element(elements: list[dict[str, Any]], raw_text: str) -> None:
    text = html.unescape(raw_text)
    if not text:
        return
    if elements and elements[-1].get("type") == "text":
        elements[-1]["content"] = f"{elements[-1].get('content', '')}{text}"
        return
    elements.append({"type": "text", "content": text})


def parse_element_message(message: str) -> list[dict[str, Any]]:
    if len(message) > BOT_ELEMENT_MESSAGE_MAX_LENGTH:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Bot element message must not exceed 8000 characters")
    elements: list[dict[str, Any]] = []
    position = 0
    while position < len(message):
        content_match = _CONTENT_ELEMENT_RE.search(message, position)
        tag_match = _ELEMENT_TAG_RE.search(message, position)
        if content_match and (not tag_match or content_match.start() <= tag_match.start()):
            _append_text_element(elements, message[position:content_match.start()])
            tag = content_match.group(1).strip().lower().replace("-", "_")
            attrs = _parse_element_attrs(content_match.group(2).strip())
            content = html.unescape(content_match.group(3))
            elements.append({"type": {"md": "markdown", "btn": "button"}.get(tag, tag), "attrs": attrs, "content": content})
            position = content_match.end()
            continue
        if not tag_match:
            break
        match = tag_match
        _append_text_element(elements, message[position:match.start()])
        tag = match.group(1).strip().lower().replace("-", "_")
        raw_attrs = match.group(2).strip()
        attrs = _parse_element_attrs(raw_attrs)
        if tag in {"br", "at_all"}:
            elements.append({"type": tag})
        elif tag in {"at", "quote", "img", "image", "audio", "voice", "sticker", "face", "button", "btn", "link"}:
            normalized = {"image": "img", "voice": "audio", "face": "sticker", "btn": "button"}.get(tag, tag)
            elements.append({"type": normalized, "attrs": attrs})
        else:
            _append_text_element(elements, match.group(0))
        position = match.end()
    _append_text_element(elements, message[position:])
    if len(elements) > BOT_MAX_ELEMENTS:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Bot message elements must not exceed 50 items")
    return elements


def normalize_bot_message_payload(payload: BotSendMessageCreate, bot_id: int, owner_id: int) -> tuple[str, Any, dict[str, Any]]:
    metadata: dict[str, Any] = dict(payload.metadata or {})
    metadata["bot"] = {"id": bot_id, "owner_id": owner_id}
    raw_elements = (payload.elements or parse_element_message(payload.message)) if payload.message else payload.elements
    if not raw_elements:
        if metadata.get("markdown") is True and len(payload.content) > BOT_MARKDOWN_MAX_LENGTH:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Bot markdown content must not exceed 8000 characters")
        if metadata.get("markdown") is True:
            _validate_markdown_interactions(payload.content)
        return payload.content, payload.type, metadata

    if len(raw_elements) > BOT_MAX_ELEMENTS:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Bot message elements must not exceed 50 items")

    text_parts: list[str] = []
    images: list[str] = []
    mentions: list[dict[str, Any]] = []
    normalized_elements: list[dict[str, Any]] = []
    message_type = payload.type
    primary_media_url: str | None = None

    for index, element in enumerate(raw_elements):
        element_type = _element_name(element)
        attrs = _element_attrs(element)
        content = _element_content(element)

        if element_type in {"text", "plain", "p"}:
            if content:
                text_parts.append(content)
                normalized_elements.append({"type": "text", "content": content})
            continue

        if element_type in {"markdown", "md"}:
            if content:
                if len(content) > BOT_MARKDOWN_MAX_LENGTH:
                    raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Bot markdown content must not exceed 8000 characters")
                _validate_markdown_image_urls(content)
                _validate_markdown_interactions(content)
                markdown_mentions, markdown_mention_all = _markdown_mentions(content)
                mentions.extend(markdown_mentions)
                if markdown_mention_all:
                    metadata["mention_all"] = True
                text_parts.append(content)
                metadata["markdown"] = True
                normalized_elements.append({"type": "markdown", "content": content})
            continue

        if element_type in {"button", "btn"}:
            label = content or _string_attr(attrs, "label", "text", "title") or "按钮"
            action = (_string_attr(attrs, "action", "type", "mode") or "callback").lower().replace("-", "_")
            action = "input" if action in {"insert", "fill", "preset", "text"} else "callback"
            action_id = _string_attr(attrs, "id", "action_id", "actionId", "key", "value")
            value = _string_attr(attrs, "value", "text", "input", "command")
            component_id = _validate_component_id(_string_attr(attrs, "component_id", "componentId", "button_id", "buttonId", "id"))
            normalized: dict[str, Any] = {"type": "button", "attrs": {"label": label[:80], "action": action}}
            if component_id:
                normalized["attrs"]["id"] = component_id
            if action == "input":
                normalized["attrs"]["value"] = _validate_interaction_text(value or label, "Bot input button value is required")
            else:
                normalized["attrs"]["action_id"] = _validate_interaction_text(action_id or label, "Bot callback button action_id is required")
                if value:
                    normalized["attrs"]["value"] = _validate_interaction_text(value, "Bot callback button value is invalid")
            text_parts.append(label)
            normalized_elements.append(normalized)
            continue

        if element_type == "link":
            label = content or _string_attr(attrs, "label", "text", "title") or "链接"
            href = _string_attr(attrs, "href", "url")
            action = (_string_attr(attrs, "action", "type", "mode") or "open").lower().replace("-", "_")
            action = "input" if action in {"insert", "fill", "preset", "text"} else "callback" if action == "callback" else "open"
            component_id = _validate_component_id(_string_attr(attrs, "component_id", "componentId", "button_id", "buttonId", "id"))
            normalized = {"type": "link", "attrs": {"label": label[:80], "action": action}}
            if component_id:
                normalized["attrs"]["id"] = component_id
            if action == "input":
                normalized["attrs"]["value"] = _validate_interaction_text(_string_attr(attrs, "value", "text", "input", "command") or label, "Bot input link value is required")
            elif action == "callback":
                normalized["attrs"]["action_id"] = _validate_interaction_text(_string_attr(attrs, "id", "action_id", "actionId", "key", "value") or label, "Bot callback link action_id is required")
                if href:
                    normalized["attrs"]["href"] = _validate_interaction_href(href)
            else:
                normalized["attrs"]["href"] = _validate_interaction_href(href or "")
            text_parts.append(label)
            normalized_elements.append(normalized)
            continue

        if element_type in {"at", "mention"}:
            target_id = _int_attr(attrs, "id", "user_id", "userId")
            name = _string_attr(attrs, "name", "nickname", "username") or (f"用户 {target_id}" if target_id else "")
            if target_id:
                mentions.append({"user_id": target_id, "name": name})
                text_parts.append(f"@{name}")
                normalized_elements.append({"type": "at", "attrs": {"id": target_id, "name": name}})
            continue

        if element_type in {"at_all", "mention_all"}:
            metadata["mention_all"] = True
            text_parts.append("@全体")
            normalized_elements.append({"type": "at_all"})
            continue

        if element_type == "quote":
            message_id = _int_attr(attrs, "id", "message_id", "messageId")
            if message_id:
                preview = _string_attr(attrs, "preview", "content") or _quote_preview(payload.content)
                sender_name = _string_attr(attrs, "sender_name", "senderName", "name") or ""
                metadata["quote"] = {"message_id": message_id, "sender_name": sender_name, "preview": preview}
                normalized_elements.append({"type": "quote", "attrs": {"id": message_id, "preview": preview, "sender_name": sender_name}})
            continue

        if element_type in {"img", "image"}:
            src = _string_attr(attrs, "src", "url", "href") or content
            if src:
                if primary_media_url is None and not text_parts and not images and index == 0:
                    primary_media_url = src
                    message_type = MessageType.IMAGE
                else:
                    images.append(src)
                normalized_elements.append({"type": "img", "attrs": {"src": src}})
            continue

        if element_type in {"sticker", "face"}:
            src = _string_attr(attrs, "src", "url", "href") or content
            if src:
                primary_media_url = primary_media_url or src
                message_type = MessageType.IMAGE
                metadata["sticker"] = True
                normalized_elements.append({"type": "sticker", "attrs": {"src": src}})
            continue

        if element_type in {"audio", "voice"}:
            src = _string_attr(attrs, "src", "url", "href") or content
            if src:
                primary_media_url = primary_media_url or src
                message_type = MessageType.VOICE
                duration_ms = _int_attr(attrs, "duration_ms", "durationMs", "duration")
                if duration_ms:
                    metadata["voice"] = {**(metadata.get("voice") if isinstance(metadata.get("voice"), dict) else {}), "duration_ms": duration_ms}
                normalized_elements.append({"type": "audio", "attrs": {"src": src, **({"duration_ms": duration_ms} if duration_ms else {})}})
            continue

        if element_type == "br":
            text_parts.append("\n")
            normalized_elements.append({"type": "br"})

    if mentions:
        metadata["mentions"] = mentions
    metadata["elements"] = normalized_elements

    text_content = " ".join(part for part in text_parts if part != "\n").strip()
    if any(part == "\n" for part in text_parts):
        text_content = "".join(part if part == "\n" else f"{part} " for part in text_parts).strip()
    if primary_media_url and message_type == MessageType.IMAGE and (text_content or images):
        images.insert(0, primary_media_url)
        primary_media_url = None
        message_type = MessageType.TEXT
    if images:
        metadata["images"] = [*metadata.get("images", [])] if isinstance(metadata.get("images"), list) else []
        metadata["images"].extend(images)
    if primary_media_url and message_type in {MessageType.IMAGE, MessageType.VOICE} and not text_content and not images:
        return primary_media_url, message_type, metadata
    return text_content or payload.content, message_type, metadata


async def resolve_mention_names(db: AsyncSession, conversation_id: int, content: str, metadata: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    mentions = metadata.get("mentions")
    if not isinstance(mentions, list) or not mentions:
        return content, metadata
    ids: list[int] = []
    for item in mentions:
        if isinstance(item, dict):
            try:
                user_id = int(item.get("user_id") or item.get("userId") or item.get("id") or 0)
            except (TypeError, ValueError):
                continue
            if user_id > 0:
                ids.append(user_id)
    ids = list(dict.fromkeys(ids))
    if not ids:
        return content, metadata

    result = await db.execute(
        select(ConversationMember)
        .options(joinedload(ConversationMember.user))
        .where(ConversationMember.conversation_id == conversation_id, ConversationMember.user_id.in_(ids))
    )
    names: dict[int, str] = {}
    for member in result.scalars().unique().all():
        user = member.user
        name = (member.nickname or "").strip() or ((user.nickname or user.username or "").strip() if user else "")
        if name:
            names[member.user_id] = name

    replacements: list[tuple[str, str]] = []
    next_mentions: list[dict[str, Any]] = []
    for item in mentions:
        if not isinstance(item, dict):
            continue
        try:
            user_id = int(item.get("user_id") or item.get("userId") or item.get("id") or 0)
        except (TypeError, ValueError):
            continue
        if user_id <= 0:
            continue
        old_name = str(item.get("name") or item.get("nickname") or item.get("username") or f"用户 {user_id}").strip()
        name = names.get(user_id) or old_name or f"用户 {user_id}"
        next_mentions.append({"user_id": user_id, "name": name})
        if old_name and old_name != name:
            replacements.append((f"@{old_name}", f"@{name}"))

    if next_mentions:
        metadata["mentions"] = next_mentions
    elements = metadata.get("elements")
    if isinstance(elements, list):
        for element in elements:
            if not isinstance(element, dict):
                continue
            element_type = str(element.get("type") or "").lower().replace("-", "_")
            if element_type not in {"at", "mention"}:
                continue
            attrs = element.get("attrs")
            if not isinstance(attrs, dict):
                continue
            user_id = _int_attr(attrs, "id", "user_id", "userId")
            if user_id and user_id in names:
                attrs["name"] = names[user_id]
    for old, new in replacements:
        content = content.replace(old, new)
    return content, metadata


@router.get("/me", response_model=BotRuntimeMeRead)
async def bot_me(
    context: Annotated[BotContext, Depends(get_current_bot)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> BotRuntimeMeRead:
    conversations = await conversation_service.list_user_conversations(db, context.user.id)
    reads = [await conversation_service.build_conversation_read(db, conversation, context.user.id) for conversation in conversations]
    from app.schemas.bot import BotRead

    return BotRuntimeMeRead(bot=BotRead.model_validate(context.bot), conversations=reads)


@router.get("/conversations", response_model=list[ConversationRead])
async def bot_conversations(
    context: Annotated[BotContext, Depends(get_current_bot)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    conversations = await conversation_service.list_user_conversations(db, context.user.id)
    return [await conversation_service.build_conversation_read(db, conversation, context.user.id) for conversation in conversations]


@router.get("/users/online", response_model=OnlineUsersRead)
async def bot_online_users(
    _context: Annotated[BotContext, Depends(get_current_bot)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> OnlineUsersRead:
    online_ids = await manager.online_user_ids_global()
    users: list[User] = []
    if online_ids:
        result = await db.execute(select(User).where(User.id.in_(online_ids)))
        users = sorted(result.scalars().all(), key=lambda user: user.id)
    return OnlineUsersRead(online_count=len(users), users=users)


@router.get("/users/{user_id}", response_model=BotUserPublicInfoRead)
async def bot_user_public_info(
    user_id: int,
    _context: Annotated[BotContext, Depends(get_current_bot)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> BotUserPublicInfoRead:
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    online = await manager.is_online_global(user_id)
    if user.is_bot:
        bot_result = await db.execute(select(Bot).where(Bot.user_id == user_id))
        bot = bot_result.scalar_one_or_none()
        online = online or bool(bot and await bot_manager.is_online_global(bot.id))
    return BotUserPublicInfoRead(user=UserPublic.model_validate(user), online=online)


@router.get("/conversations/{conversation_id}", response_model=BotConversationInfoRead)
async def bot_conversation_info(
    conversation_id: int,
    context: Annotated[BotContext, Depends(get_current_bot)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> BotConversationInfoRead:
    installation = await bot_service.validate_bot_can_use_conversation(db, context, conversation_id)
    conversation = await conversation_service.require_group_conversation(db, conversation_id)
    member_count_result = await db.execute(
        select(func.count()).select_from(ConversationMember).where(ConversationMember.conversation_id == conversation_id)
    )
    member_count = int(member_count_result.scalar_one())
    return BotConversationInfoRead(
        id=conversation.id,
        title=conversation.title,
        display_title=conversation.title,
        avatar_url=conversation.avatar_url,
        announcement=conversation.announcement,
        member_count=member_count,
        bot_enabled=installation.enabled,
    )


@router.get("/conversations/{conversation_id}/members", response_model=BotConversationMembersRead)
async def bot_conversation_members(
    conversation_id: int,
    context: Annotated[BotContext, Depends(get_current_bot)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> BotConversationMembersRead:
    await bot_service.validate_bot_can_use_conversation(db, context, conversation_id)
    members = await conversation_service.list_members(db, conversation_id, context.user.id)
    return BotConversationMembersRead(items=[ConversationMemberRead.model_validate(item) for item in members])


@router.get("/conversations/{conversation_id}/messages", response_model=list[MessageRead])
async def bot_messages(
    conversation_id: int,
    context: Annotated[BotContext, Depends(get_current_bot)],
    db: Annotated[AsyncSession, Depends(get_db)],
    before_id: int | None = None,
    after_id: int | None = None,
    limit: int = Query(default=50, ge=1, le=100),
) -> list[MessageRead]:
    await bot_service.validate_bot_can_use_message_conversation(db, context, conversation_id)
    if after_id is not None:
        items = await message_service.list_messages_after(db, conversation_id, context.user.id, after_id, limit)
    else:
        items = await message_service.list_messages(db, conversation_id, context.user.id, before_id, limit)
    return await message_service.build_message_reads(db, items, context.user.id)


@router.post("/conversations/{conversation_id}/messages", response_model=MessageRead, status_code=status.HTTP_201_CREATED)
async def bot_send_message(
    conversation_id: int,
    payload: BotSendMessageCreate,
    context: Annotated[BotContext, Depends(get_current_bot)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> MessageRead:
    await bot_service.validate_bot_can_use_message_conversation(db, context, conversation_id)
    content, message_type, metadata = normalize_bot_message_payload(payload, context.bot.id, context.bot.owner_id)
    content, metadata = await resolve_mention_names(db, conversation_id, content, metadata)
    message = await message_service.send_message(db, conversation_id, context.user, content, message_type, metadata)
    message_data = message_service.build_new_message_read(message, context.user.id)
    member_ids = await online_conversation_member_ids(db, conversation_id)
    schedule_realtime_task(send_message_updates(db, "message.created", message, member_ids))
    schedule_realtime_task(send_admin_message_signal("admin.message.changed", message, "created"))
    if not context.user.content_moderation_whitelisted:
        moderation_service.schedule_message_moderation(message.id)
    return message_data


@router.post("/users/{user_id}/messages", response_model=MessageRead, status_code=status.HTTP_201_CREATED)
async def bot_send_direct_message(
    user_id: int,
    payload: BotSendMessageCreate,
    context: Annotated[BotContext, Depends(get_current_bot)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> MessageRead:
    if user_id == context.user.id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot create a direct conversation with yourself")
    conversation = await conversation_service.get_or_create_direct_conversation(db, context.user, user_id, True)
    response_recipients = await conversation_service.conversation_member_ids(db, conversation.id)
    schedule_realtime_task(send_conversation_updates(db, conversation.id, response_recipients))
    content, message_type, metadata = normalize_bot_message_payload(payload, context.bot.id, context.bot.owner_id)
    message = await message_service.send_message(db, conversation.id, context.user, content, message_type, metadata)
    message_data = message_service.build_new_message_read(message, context.user.id)
    member_ids = await online_conversation_member_ids(db, conversation.id)
    schedule_realtime_task(send_message_updates(db, "message.created", message, member_ids))
    schedule_realtime_task(send_admin_message_signal("admin.message.changed", message, "created"))
    if not context.user.content_moderation_whitelisted:
        moderation_service.schedule_message_moderation(message.id)
    return message_data


@router.post("/direct/messages", response_model=MessageRead, status_code=status.HTTP_201_CREATED)
async def bot_send_direct_message_by_payload(
    payload: BotDirectMessageCreate,
    context: Annotated[BotContext, Depends(get_current_bot)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> MessageRead:
    message_payload = BotSendMessageCreate(
        type=payload.type,
        content=payload.content,
        message=payload.message,
        metadata=payload.metadata,
        elements=payload.elements,
    )
    return await bot_send_direct_message(payload.user_id, message_payload, context, db)


@router.post("/conversations/{conversation_id}/messages/{message_id}/recall", response_model=MessageRead)
async def bot_recall_message(
    conversation_id: int,
    message_id: int,
    context: Annotated[BotContext, Depends(get_current_bot)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> MessageRead:
    await bot_service.validate_bot_can_use_message_conversation(db, context, conversation_id)
    message = await message_service.recall_message(db, conversation_id, context.user.id, message_id)
    message_data = await message_service.build_message_read(db, message, context.user.id)
    member_ids = await online_conversation_member_ids(db, conversation_id)
    await send_message_updates(db, "message.recalled", message, member_ids)
    return message_data


@router.patch("/conversations/{conversation_id}/messages/{message_id}/components/{component_id}")
async def bot_update_message_component(
    conversation_id: int,
    message_id: int,
    component_id: str,
    payload: BotMessageComponentUpdate,
    context: Annotated[BotContext, Depends(get_current_bot)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    await bot_service.validate_bot_can_use_message_conversation(db, context, conversation_id)
    clean_component_id = _validate_component_id(component_id)
    if clean_component_id is None:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Bot button id is required")
    result = await db.execute(
        select(Message)
        .where(
            Message.id == message_id,
            Message.conversation_id == conversation_id,
            Message.sender_id == context.user.id,
            Message.recalled_at.is_(None),
        )
    )
    message = result.scalar_one_or_none()
    if message is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Bot message not found")
    metadata = _message_metadata(message)
    if not _metadata_has_button_component(metadata, clean_component_id):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Button id is not defined on this message")
    state = _component_state_payload(payload)
    data = {
        "conversation_id": conversation_id,
        "message_id": message_id,
        "component_id": clean_component_id,
        "state": state,
        "scope": payload.scope,
    }
    if payload.scope == "user":
        target_user_id = payload.user_id
        if target_user_id is None:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="user_id is required for user-scoped component updates")
        await conversation_service.require_membership(db, conversation_id, target_user_id)
        data["user_id"] = target_user_id
        await manager.send_to_user(target_user_id, "message.component.updated", data)
        return data
    component_state = metadata.get("component_state") if isinstance(metadata.get("component_state"), dict) else {}
    component_state[clean_component_id] = {**(component_state.get(clean_component_id) if isinstance(component_state.get(clean_component_id), dict) else {}), **state}
    metadata["component_state"] = component_state
    message.metadata_json = json.dumps(metadata, ensure_ascii=False, separators=(",", ":"))
    await db.commit()
    await db.refresh(message)
    member_ids = await online_conversation_member_ids(db, conversation_id)
    await manager.send_to_users(member_ids, "message.component.updated", data)
    return data


@router.post("/conversations/{conversation_id}/messages/{message_id}/reactions", response_model=list[MessageReactionSummary])
async def bot_toggle_reaction(
    conversation_id: int,
    message_id: int,
    payload: MessageReactionCreate,
    context: Annotated[BotContext, Depends(get_current_bot)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[MessageReactionSummary]:
    await bot_service.validate_bot_can_use_message_conversation(db, context, conversation_id)
    reactions = await message_service.toggle_message_reaction(db, conversation_id, context.user.id, message_id, payload.emoji)
    member_ids = await online_conversation_member_ids(db, conversation_id)
    await send_reaction_updates(db, conversation_id, message_id, member_ids)
    return reactions


@router.delete("/conversations/{conversation_id}/messages/{message_id}/reactions/{emoji}", response_model=list[MessageReactionSummary])
async def bot_delete_reaction(
    conversation_id: int,
    message_id: int,
    emoji: str,
    context: Annotated[BotContext, Depends(get_current_bot)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[MessageReactionSummary]:
    await bot_service.validate_bot_can_use_message_conversation(db, context, conversation_id)
    reactions = await message_service.delete_message_reaction(db, conversation_id, context.user.id, message_id, emoji)
    member_ids = await online_conversation_member_ids(db, conversation_id)
    await send_reaction_updates(db, conversation_id, message_id, member_ids)
    return reactions


@router.post("/uploads/image", response_model=UploadRead, status_code=status.HTTP_201_CREATED)
async def bot_upload_image(
    request: Request,
    file: Annotated[UploadFile, File()],
    _context: Annotated[BotContext, Depends(get_current_bot)],
) -> UploadRead:
    return await save_image_upload(file, "messages", request)


@router.post("/uploads/voice", response_model=UploadRead, status_code=status.HTTP_201_CREATED)
async def bot_upload_voice(
    request: Request,
    file: Annotated[UploadFile, File()],
    _context: Annotated[BotContext, Depends(get_current_bot)],
) -> UploadRead:
    return await save_audio_upload(file, "messages", request)


# ---------------------------------------------------------------------------
# 群管理
#
# 这里不额外定义权限：机器人在群里就是一个普通成员，能不能踢人 / 禁言完全取决于
# 群主有没有把它设成管理员。下面直接复用 conversation_service 里那套人类用的
# 逻辑，角色校验、越权保护（管理员不能动管理员）全部原样生效。
# ---------------------------------------------------------------------------


async def _require_bot_staff(db: AsyncSession, context: BotContext, conversation_id: int) -> ConversationMember:
    """机器人必须已安装在该群，且自身是管理员或群主。"""
    await bot_service.validate_bot_can_use_conversation(db, context, conversation_id)
    member = await conversation_service.require_membership(db, conversation_id, context.user.id)
    if member.role not in conversation_service.STAFF_ROLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Bot must be a group admin to perform this action",
        )
    return member


@router.patch("/conversations/{conversation_id}/members/{user_id}/mute", response_model=ConversationMemberRead)
async def bot_update_member_mute(
    conversation_id: int,
    user_id: int,
    payload: BotMemberMuteUpdate,
    context: Annotated[BotContext, Depends(get_current_bot)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ConversationMember:
    await _require_bot_staff(db, context, conversation_id)
    previous = await conversation_service.get_member(db, conversation_id, user_id)
    previous_muted = bool(previous.muted)
    muted_until = None
    if payload.muted and payload.minutes:
        muted_until = datetime.now(UTC) + timedelta(minutes=payload.minutes)
    member = await conversation_service.update_member_mute(
        db, conversation_id, context.user.id, user_id, payload.muted, muted_until
    )
    response_cache.invalidate_conversation_members(conversation_id)
    member_ids = await online_conversation_member_ids(db, conversation_id)
    if bool(member.muted) != previous_muted:
        actor_name = display_name(context.user, f"机器人 {context.user.id}")
        target_name = display_name(member.user, f"用户 {user_id}")
        duration_label = mute_duration_label(member.muted_until) if member.muted else ""
        await emit_system_message(
            db,
            conversation_id,
            context.user,
            f"{actor_name}{'禁言了' if member.muted else '解除了'} {target_name}{duration_label if member.muted else ' 的禁言'}",
            member_ids,
            {
                "system_event": "group.member.mute_updated",
                "actor_id": context.user.id,
                "user_id": user_id,
                "muted": member.muted,
                "muted_until": member.muted_until.isoformat() if member.muted_until else None,
            },
        )
    event_data = {
        "conversation_id": conversation_id,
        "user_id": user_id,
        "muted": member.muted,
        "muted_until": member.muted_until.isoformat() if member.muted_until else None,
    }
    await manager.send_to_users(member_ids, "group.member.mute_updated", event_data)
    await send_bot_conversation_event(db, "group.member.mute_updated", conversation_id, event_data)
    await send_conversation_updates(db, conversation_id, member_ids)
    return member


@router.patch("/conversations/{conversation_id}/members/{user_id}/role", response_model=ConversationMemberRead)
async def bot_update_member_role(
    conversation_id: int,
    user_id: int,
    payload: BotMemberRoleUpdate,
    context: Annotated[BotContext, Depends(get_current_bot)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ConversationMember:
    # 改角色是群主专属，_require_bot_staff 只挡住非管理员；真正的群主校验在 service 里
    await _require_bot_staff(db, context, conversation_id)
    previous = await conversation_service.get_member(db, conversation_id, user_id)
    previous_role = previous.role
    member = await conversation_service.update_member_role(db, conversation_id, context.user.id, user_id, payload.role)
    response_cache.invalidate_conversation_members(conversation_id)
    member_ids = await online_conversation_member_ids(db, conversation_id)
    if member.role != previous_role:
        actor_name = display_name(context.user, f"机器人 {context.user.id}")
        target_name = display_name(member.user, f"用户 {user_id}")
        role_text = "设为管理员" if member.role == MemberRole.ADMIN else "取消管理员"
        await emit_system_message(
            db,
            conversation_id,
            context.user,
            f"{actor_name} 将 {target_name} {role_text}",
            member_ids,
            {
                "system_event": "group.member.role_updated",
                "actor_id": context.user.id,
                "user_id": user_id,
                "role": member.role.value,
            },
        )
    event_data = {"conversation_id": conversation_id, "user_id": user_id, "role": member.role}
    await manager.send_to_users(member_ids, "group.member.role_updated", event_data)
    await send_bot_conversation_event(db, "group.member.role_updated", conversation_id, event_data)
    await send_conversation_updates(db, conversation_id, member_ids)
    return member


@router.delete("/conversations/{conversation_id}/members/{user_id}", response_model=MessageResponse)
async def bot_remove_member(
    conversation_id: int,
    user_id: int,
    context: Annotated[BotContext, Depends(get_current_bot)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> MessageResponse:
    await _require_bot_staff(db, context, conversation_id)
    before_ids = set(await online_conversation_member_ids(db, conversation_id))
    target_member = await conversation_service.get_member(db, conversation_id, user_id)
    target_name = display_name(target_member.user, f"用户 {user_id}")
    await conversation_service.remove_member(db, conversation_id, context.user.id, user_id)
    response_cache.invalidate_conversation_members(conversation_id)
    remaining_ids = before_ids - {user_id}
    if remaining_ids:
        await emit_system_message(
            db,
            conversation_id,
            context.user,
            f"{display_name(context.user, f'机器人 {context.user.id}')} 将 {target_name} 移出群聊",
            remaining_ids,
            {"system_event": "group.member.removed", "actor_id": context.user.id, "user_id": user_id},
        )
    event_data = {"conversation_id": conversation_id, "user_id": user_id, "actor_id": context.user.id}
    await manager.send_to_users(remaining_ids, "group.member.removed", event_data)
    await send_bot_conversation_event(db, "group.member.removed", conversation_id, event_data)
    await manager.send_to_user(
        user_id,
        "conversation.deleted",
        {"conversation_id": conversation_id, "actor_id": context.user.id, "reason": "removed"},
    )
    if remaining_ids:
        await send_conversation_updates(db, conversation_id, remaining_ids)
    return MessageResponse(detail="Removed member")


@router.get("/conversations/{conversation_id}/join-requests", response_model=list[GroupJoinRequestRead])
async def bot_join_requests(
    conversation_id: int,
    context: Annotated[BotContext, Depends(get_current_bot)],
    db: Annotated[AsyncSession, Depends(get_db)],
    pending_only: bool = Query(default=True),
) -> list[GroupJoinRequestRead]:
    await _require_bot_staff(db, context, conversation_id)
    # 复用的是「我能审的所有申请」，这里再按群过滤，避免机器人看到别的群的申请
    items = await conversation_service.list_incoming_group_join_requests(db, context.user.id)
    items = [item for item in items if item.conversation_id == conversation_id]
    if pending_only:
        items = [item for item in items if item.status == GroupJoinRequestStatus.PENDING]
    return [await conversation_service.build_group_join_request_read(db, item, context.user.id) for item in items]


@router.post("/join-requests/{request_id}/review", response_model=GroupJoinRequestRead)
async def bot_review_join_request(
    request_id: int,
    payload: BotJoinRequestReview,
    context: Annotated[BotContext, Depends(get_current_bot)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> GroupJoinRequestRead:
    join_request = await conversation_service.get_group_join_request_for_user(db, request_id, context.user.id)
    await _require_bot_staff(db, context, join_request.conversation_id)
    conversation_id = join_request.conversation_id
    reviewed, added = await conversation_service.review_group_join_request(
        db,
        request_id,
        context.user.id,
        payload.approved,
        GroupJoinRequestDecision(decision_note=payload.decision_note),
    )
    response = await conversation_service.build_group_join_request_read(db, reviewed, context.user.id)
    reviewers = await conversation_service.pending_join_request_reviewer_ids(db, conversation_id)
    recipients = set(reviewers) | {reviewed.requester_id, context.user.id}
    await manager.send_to_users(
        recipients,
        "group.join_request.accepted" if payload.approved else "group.join_request.rejected",
        response,
    )
    if added:
        response_cache.invalidate_conversation_members(conversation_id)
        member_ids = await online_conversation_member_ids(db, conversation_id)
        requester_member = await conversation_service.get_member(db, conversation_id, reviewed.requester_id)
        requester_name = display_name(requester_member.user, f"用户 {reviewed.requester_id}")
        await emit_system_message(
            db,
            conversation_id,
            context.user,
            f"{requester_name} 加入了群聊",
            member_ids,
            {"system_event": "group.member.joined", "user_id": reviewed.requester_id},
        )
        event_data = {"conversation_id": conversation_id, "user_id": reviewed.requester_id}
        await manager.send_to_users(member_ids, "group.member.joined", event_data)
        await send_bot_conversation_event(db, "group.member.joined", conversation_id, event_data)
        await send_conversation_updates(db, conversation_id, member_ids)
    else:
        await send_conversation_updates(db, conversation_id, reviewers)
    return response
