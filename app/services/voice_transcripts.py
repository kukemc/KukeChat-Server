"""语音消息转文字（docs/chat-features.md「语音转文字」）。

POST /conversations/{cid}/messages/{mid}/transcribe：会话成员点一条语音消息的「转文字」，第一次交给识别服务
（voice_asr.transcribe → 审核机上 SenseVoice 的 /asr/transcribe）识别，结果存进 voice_transcripts，之后谁来点都直接读库。

- 能看到这条消息才能转（是成员、没清空到它之前、没对自己隐藏、没撤回），否则一律 404，不暴露消息存不存在；
- 只认 VOICE 消息（语音红包领取时发出的那条语音也是 VOICE），别的 422 not_voice；
- 录音超过 60 秒 422 voice_too_long（metadata 里有时长先按它挡，没有的由识别服务判）；
- 识别服务没配、连不上、出错 503 transcribe_unavailable；
- 每人每小时最多真正识别 TRANSCRIBE_HOURLY_LIMIT 次（命中缓存的不算），超了 429 transcribe_rate_limited。
  按 voice_transcripts.requested_by_id 数，几个 worker 共用一个口径；没识别成的不记、不算次数；
- 识别出空串（静音、没听到话）也存：不然每次点都再识别一遍。客户端拿到 "" 显示「未识别到文字」。

等对象存储和识别服务之前先 release_connection，识别一条 60 秒的语音要好几秒。
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import PurePosixPath
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import release_connection
from app.models.enums import MessageType
from app.models.message import Message, MessageVisibility, VoiceTranscript
from app.services.conversations import require_membership

logger = logging.getLogger(__name__)
UTC = timezone.utc

TRANSCRIBE_MAX_SECONDS = 60
TRANSCRIBE_HOURLY_LIMIT = 60
# 存进库里的文字最多这么长（60 秒的话一般两三百字）
TRANSCRIPT_MAX_CHARS = 2000


def _error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


def _not_found() -> HTTPException:
    return _error(status.HTTP_404_NOT_FOUND, "message_not_found", "消息不存在或已撤回")


def _too_long() -> HTTPException:
    return _error(status.HTTP_422_UNPROCESSABLE_ENTITY, "voice_too_long", f"超过 {TRANSCRIBE_MAX_SECONDS} 秒的语音不能转文字")


def _unavailable() -> HTTPException:
    return _error(status.HTTP_503_SERVICE_UNAVAILABLE, "transcribe_unavailable", "语音转文字暂时不可用，稍后再试")


def voice_duration_seconds(metadata_json: str | None) -> float | None:
    """语音消息 metadata 里记的时长（{ voice: { duration_ms } }，老客户端放在顶层 duration_ms）。没有或不像样返回 None。"""
    try:
        metadata = json.loads(metadata_json) if metadata_json else None
    except (TypeError, ValueError):
        return None
    if not isinstance(metadata, dict):
        return None
    voice = metadata.get("voice") if isinstance(metadata.get("voice"), dict) else {}
    for value in (voice.get("duration_ms"), metadata.get("duration_ms")):
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            return float(value) / 1000
    return None


async def _visible_message(db: AsyncSession, conversation_id: int, user_id: int, message_id: int) -> Message:
    try:
        # 不顺手清过期禁言：那是一次写，后面还要把连接还回去等识别服务
        membership = await require_membership(db, conversation_id, user_id, clear_expired_mute=False)
    except HTTPException:
        raise _not_found() from None
    message = (
        await db.execute(select(Message).where(Message.id == message_id, Message.conversation_id == conversation_id))
    ).scalar_one_or_none()
    if message is None or message.recalled_at is not None:
        raise _not_found()
    if membership.cleared_before_message_id is not None and message.id <= membership.cleared_before_message_id:
        raise _not_found()
    hidden = await db.scalar(
        select(MessageVisibility.id).where(MessageVisibility.message_id == message.id, MessageVisibility.user_id == user_id)
    )
    if hidden is not None:
        raise _not_found()
    return message


async def _cached(db: AsyncSession, message_id: int) -> str | None:
    return await db.scalar(select(VoiceTranscript.text).where(VoiceTranscript.message_id == message_id))


async def transcribe_message(db: AsyncSession, conversation_id: int, user_id: int, message_id: int) -> dict[str, Any]:
    """返回 { text, cached }。错误见模块说明。"""
    from app.services import storage, voice_asr
    from app.services.upload_paths import require_message_upload_url

    message = await _visible_message(db, conversation_id, user_id, message_id)
    if message.type != MessageType.VOICE:
        raise _error(status.HTTP_422_UNPROCESSABLE_ENTITY, "not_voice", "只有语音消息能转文字")
    cached = await _cached(db, message.id)
    if cached is not None:
        return {"text": cached, "cached": True}
    duration = voice_duration_seconds(message.metadata_json)
    if duration is not None and duration > TRANSCRIBE_MAX_SECONDS + 1:
        raise _too_long()
    if not voice_asr.enabled():
        raise _unavailable()
    since = datetime.now(UTC) - timedelta(hours=1)
    used = await db.scalar(
        select(func.count(VoiceTranscript.id)).where(VoiceTranscript.requested_by_id == user_id, VoiceTranscript.created_at >= since)
    )
    if int(used or 0) >= TRANSCRIBE_HOURLY_LIMIT:
        raise _error(status.HTTP_429_TOO_MANY_REQUESTS, "transcribe_rate_limited", "转文字太频繁了，过一会儿再试")
    path = require_message_upload_url(message.content)
    if path is None:
        # 不是我们上传的录音（比如机器人发的外链音频）：拿不到文件
        raise _unavailable()
    message_id = message.id
    # 下面要等对象存储和识别服务：先把数据库连接还回去
    await release_connection(db)
    try:
        stored = await asyncio.to_thread(storage.get_object, path, storage.MAX_AUDIO_UPLOAD_BYTES)
    except HTTPException as exc:
        if exc.status_code == 413:
            raise _too_long() from None
        logger.warning("Voice file for transcription is missing", extra={"message_id": message_id})
        raise _unavailable() from None
    try:
        result = await voice_asr.transcribe(stored.data, PurePosixPath(path).name, stored.content_type or "audio/webm")
    except voice_asr.VoiceTooLong:
        raise _too_long() from None
    except voice_asr.VoiceAsrUnavailable:
        logger.exception("Voice transcription unavailable", extra={"message_id": message_id})
        raise _unavailable() from None
    if result.duration > TRANSCRIBE_MAX_SECONDS + 1:
        raise _too_long()
    text = result.text[:TRANSCRIPT_MAX_CHARS]
    db.add(VoiceTranscript(message_id=message_id, text=text, requested_by_id=user_id, created_at=datetime.now(UTC)))
    try:
        await db.commit()
    except IntegrityError:
        # 别人同时点了同一条，已经存进去了：用那一份
        await db.rollback()
        existing = await _cached(db, message_id)
        if existing is not None:
            return {"text": existing, "cached": True}
        # 消息刚好被修剪删掉了（外键失败）
        raise _not_found() from None
    return {"text": text, "cached": False}


async def transcripts_for(db: AsyncSession, message_ids: list[int]) -> dict[int, str]:
    """一批语音消息已经存着的转写（一条查询，走 message_id 唯一索引）。"""
    if not message_ids:
        return {}
    rows = await db.execute(select(VoiceTranscript.message_id, VoiceTranscript.text).where(VoiceTranscript.message_id.in_(message_ids)))
    return {message_id: text for message_id, text in rows.all()}
