"""聊天记录搜索（QQ 式，docs/group-plus.md 第 8 节）。

搜的是用户**自己的**聊天记录：只在他是成员的会话里，且是他可见的消息（清空聊天记录之前的、「仅自己删除」的、
已撤回的、已被修剪的都搜不到）。这里没有任何管理员 / 全站 / 按用户翻阅别人消息的入口，也不能有（CLAUDE.md 第一条）。

索引：message_search_index（每条可搜消息一行，触发器随 messages 同步）+ message_search_fts（FTS5 外部内容表，
trigram 分词，对中文是「任意连续 3 个字」的子串匹配）。建表和触发器在 services/schema_extras.py。
关键词里少于 3 个字的词 trigram 搜不到，退回 LIKE（仍然限定在用户自己的会话里）。FTS5 不可用时整体退回 LIKE。
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.core import group_plus_tiers as tiers
from app.core import localtime
from app.models.conversation import Conversation, ConversationMember
from app.models.enums import ConversationType
from app.models.message import Message
from app.models.user import User
from app.schemas.user import UserPublic

logger = logging.getLogger(__name__)
UTC = timezone.utc

CATEGORIES = ("all", "media", "sticker", "file", "link")
TOTAL_CAP = 10000
SNIPPET_RADIUS = 36
MAX_TOKENS = 8
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _error(status_code: int, code: str, message: str, **extra: Any) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message, **extra})


def _db_time(value: datetime) -> str:
    """和 SQLAlchemy 往 SQLite 里存 DateTime 的格式一致（UTC，无时区后缀）。"""
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")


def parse_range(date_from: str | None, date_to: str | None) -> tuple[datetime | None, datetime | None]:
    """日期（2026-10-01，UTC+8 的整天，date_to 含当天）或完整 ISO 时间（[from, to)）。"""

    def one(raw: str | None, *, end: bool) -> datetime | None:
        value = (raw or "").strip()
        if not value:
            return None
        try:
            if _DATE_RE.match(value):
                day = date.fromisoformat(value)
                if end:
                    day = day + timedelta(days=1)
                return datetime.combine(day, time.min, tzinfo=localtime.LOCAL_TZ).astimezone(UTC)
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise _error(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_date", "日期格式不对，用 2026-10-01 或 ISO 时间") from None
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)

    return one(date_from, end=False), one(date_to, end=True)


def split_tokens(query: str | None) -> list[str]:
    seen: list[str] = []
    for token in (query or "").split():
        token = token.strip()
        if token and token.lower() not in [item.lower() for item in seen]:
            seen.append(token)
    return seen[:MAX_TOKENS]


def _like_pattern(token: str) -> str:
    return "%" + token.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _fts_phrase(token: str) -> str:
    return '"' + token.replace('"', '""') + '"'


class _Query:
    """拼一条搜索 SQL 的 FROM / WHERE 和参数。"""

    def __init__(
        self,
        user_id: int,
        *,
        tokens: list[str],
        conversation_id: int | None,
        sender_id: int | None,
        category: str,
        start: datetime | None,
        end: datetime | None,
        before_id: int | None,
        use_fts: bool,
    ) -> None:
        self.params: dict[str, Any] = {"uid": user_id}
        where = [
            "(cm.cleared_before_message_id IS NULL OR i.message_id > cm.cleared_before_message_id)",
            "NOT EXISTS (SELECT 1 FROM message_visibilities v WHERE v.message_id = i.message_id AND v.user_id = :uid)",
        ]
        if conversation_id is not None:
            where.append("i.conversation_id = :cid")
            self.params["cid"] = conversation_id
        if sender_id is not None:
            where.append("i.sender_id = :sid")
            self.params["sid"] = sender_id
        if category != "all":
            where.append("i.category = :cat")
            self.params["cat"] = category
        if start is not None:
            where.append("i.created_at >= :df")
            self.params["df"] = _db_time(start)
        if end is not None:
            where.append("i.created_at < :dt")
            self.params["dt"] = _db_time(end)
        if before_id is not None:
            where.append("i.message_id < :before")
            self.params["before"] = before_id
        long_tokens = [token for token in tokens if len(token) >= 3] if use_fts else []
        like_tokens = [token for token in tokens if token not in long_tokens]
        for index, token in enumerate(like_tokens):
            where.append(f"i.text LIKE :like{index} ESCAPE '\\'")
            self.params[f"like{index}"] = _like_pattern(token)
        self.cte = ""
        if long_tokens:
            self.params["match"] = " AND ".join(_fts_phrase(token) for token in long_tokens)
            if conversation_id is not None:
                # 单会话：从这个会话的索引行往回扫（有 LIMIT 就早停），用命中集合做探测
                where.append("i.message_id IN (SELECT rowid FROM message_search_fts WHERE message_search_fts MATCH :match)")
                self.source = (
                    "message_search_index i CROSS JOIN conversation_members cm "
                    "ON cm.conversation_id = i.conversation_id AND cm.user_id = :uid"
                )
            else:
                # 全部会话：先取 FTS 命中，再按「是不是我的会话」过滤
                self.cte = "WITH hits(id) AS MATERIALIZED (SELECT rowid FROM message_search_fts WHERE message_search_fts MATCH :match) "
                self.source = (
                    "hits CROSS JOIN message_search_index i ON i.message_id = hits.id "
                    "CROSS JOIN conversation_members cm ON cm.conversation_id = i.conversation_id AND cm.user_id = :uid"
                )
        else:
            self.source = (
                "conversation_members cm CROSS JOIN message_search_index i ON i.conversation_id = cm.conversation_id"
            )
            where.append("cm.user_id = :uid")
        self.where = " AND ".join(where)


async def _fts_available(db: AsyncSession) -> bool:
    row = await db.execute(text("SELECT 1 FROM sqlite_master WHERE name = 'message_search_fts'"))
    return row.first() is not None


async def _require_my_conversation(db: AsyncSession, user_id: int, conversation_id: int) -> None:
    exists = await db.scalar(
        select(ConversationMember.id).where(ConversationMember.conversation_id == conversation_id, ConversationMember.user_id == user_id)
    )
    if exists is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")


def _utf16_index(value: str, index: int) -> int:
    return index + sum(1 for ch in value[:index] if ord(ch) > 0xFFFF)


def find_highlights(value: str, tokens: list[str]) -> list[list[int]]:
    """value 里每个关键词（不区分大小写）每次出现的 [start, end)，合并重叠，按 UTF-16 码元计（和 JS 字符串下标一致）。"""
    if not value or not tokens:
        return []
    lowered = value.lower()
    spans: list[tuple[int, int]] = []
    for token in tokens:
        needle = token.lower()
        if not needle:
            continue
        start = lowered.find(needle)
        while start >= 0 and len(spans) < 200:
            spans.append((start, start + len(needle)))
            start = lowered.find(needle, start + len(needle))
    spans.sort()
    merged: list[list[int]] = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [[_utf16_index(value, start), _utf16_index(value, end)] for start, end in merged]


def build_snippet(value: str, tokens: list[str]) -> tuple[str, list[list[int]]]:
    """命中处前后各 SNIPPET_RADIUS 个字；返回 (snippet, snippet 内的命中区间)。"""
    flat = value.replace("\r", " ").replace("\n", " ")
    first = -1
    lowered = flat.lower()
    for token in tokens:
        position = lowered.find(token.lower()) if token else -1
        if position >= 0 and (first < 0 or position < first):
            first = position
    if first < 0:
        head = flat[: SNIPPET_RADIUS * 2]
        return head + ("..." if len(flat) > len(head) else ""), []
    start = max(0, first - SNIPPET_RADIUS)
    end = min(len(flat), first + SNIPPET_RADIUS * 2)
    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(flat) else ""
    snippet = f"{prefix}{flat[start:end]}{suffix}"
    return snippet, find_highlights(snippet, tokens)


async def _conversation_summaries(db: AsyncSession, user_id: int, conversation_ids: list[int]) -> dict[int, dict[str, Any]]:
    from app.services.conversations import _conversation_display_title

    if not conversation_ids:
        return {}
    conversations = {
        conversation.id: conversation
        for conversation in (await db.execute(select(Conversation).where(Conversation.id.in_(conversation_ids)))).scalars().all()
    }
    memberships = {
        member.conversation_id: member
        for member in (
            await db.execute(
                select(ConversationMember).where(ConversationMember.conversation_id.in_(conversation_ids), ConversationMember.user_id == user_id)
            )
        ).scalars().all()
    }
    direct_ids = [cid for cid, conversation in conversations.items() if conversation.type == ConversationType.DIRECT]
    peers: dict[int, User] = {}
    if direct_ids:
        rows = await db.execute(
            select(ConversationMember.conversation_id, User)
            .join(User, User.id == ConversationMember.user_id)
            .where(ConversationMember.conversation_id.in_(direct_ids), ConversationMember.user_id != user_id)
        )
        for cid, peer in rows.all():
            peers.setdefault(cid, peer)
    now = tiers._now()
    summaries: dict[int, dict[str, Any]] = {}
    for cid, conversation in conversations.items():
        membership = memberships.get(cid)
        peer = peers.get(cid)
        title = _conversation_display_title(conversation, membership, peer) if membership is not None else conversation.title
        is_group = conversation.type == ConversationType.GROUP
        tier = tiers.conversation_tier(conversation, now)
        summaries[cid] = {
            "id": cid,
            "type": conversation.type.value,
            "title": conversation.title if is_group else None,
            "display_title": title,
            "avatar_url": conversation.avatar_url if is_group else (peer.avatar_url if peer is not None else None),
            "plus_tier": tier,
            "plus_expires_at": tiers.aware(conversation.plus_expires_at) if tier != tiers.FREE else None,
            "direct_user": UserPublic.model_validate(peer) if peer is not None else None,
        }
    return summaries


def _validate(category: str, tokens: list[str], conversation_id: int | None, sender_id: int | None, start: Any, end: Any) -> None:
    if category not in CATEGORIES:
        raise _error(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_category", "category 只能是 all / media / sticker / file / link")
    if not tokens and conversation_id is None and sender_id is None and category == "all" and start is None and end is None:
        raise _error(status.HTTP_422_UNPROCESSABLE_ENTITY, "query_required", "至少要给关键词、会话、发送人、分类或日期中的一项")


async def search(
    db: AsyncSession,
    user_id: int,
    *,
    q: str | None,
    conversation_id: int | None,
    sender_id: int | None,
    category: str,
    date_from: str | None,
    date_to: str | None,
    limit: int,
    offset: int,
    before_id: int | None,
) -> dict[str, Any]:
    from app.services import messages as message_service

    tokens = split_tokens(q)
    start, end = parse_range(date_from, date_to)
    _validate(category, tokens, conversation_id, sender_id, start, end)
    if conversation_id is not None:
        await _require_my_conversation(db, user_id, conversation_id)
    use_fts = await _fts_available(db)
    query = _Query(
        user_id,
        tokens=tokens,
        conversation_id=conversation_id,
        sender_id=sender_id,
        category=category,
        start=start,
        end=end,
        before_id=before_id,
        use_fts=use_fts,
    )
    params = dict(query.params)
    params["limit"] = limit + 1
    params["offset"] = offset
    try:
        rows = (
            await db.execute(
                text(
                    f"{query.cte}SELECT i.message_id, i.conversation_id, i.category, i.text FROM {query.source} "
                    f"WHERE {query.where} ORDER BY i.message_id DESC LIMIT :limit OFFSET :offset"
                ),
                params,
            )
        ).all()
        count_params = {key: value for key, value in query.params.items()}
        counted = int(
            await db.scalar(
                text(f"{query.cte}SELECT count(*) FROM (SELECT 1 FROM {query.source} WHERE {query.where} LIMIT {TOTAL_CAP + 1})"),
                count_params,
            )
            or 0
        )
    except OperationalError as exc:
        logger.exception("Message search failed")
        raise _error(status.HTTP_503_SERVICE_UNAVAILABLE, "search_unavailable", "搜索暂时不可用，稍后再试") from exc
    has_more = len(rows) > limit
    rows = rows[:limit]
    ids = [row[0] for row in rows]
    messages: dict[int, Message] = {}
    if ids:
        loaded = (await db.execute(select(Message).options(joinedload(Message.sender)).where(Message.id.in_(ids)))).scalars().all()
        messages = {message.id: message for message in loaded if message.recalled_at is None}
    reads = {read.id: read for read in await message_service.build_message_reads(db, list(messages.values()), user_id)}
    summaries = await _conversation_summaries(db, user_id, sorted({row[1] for row in rows}))
    items = []
    for message_id, cid, category_value, index_text in rows:
        read = reads.get(message_id)
        if read is None or cid not in summaries:
            continue
        snippet_source = index_text or read.content
        snippet, snippet_highlights = build_snippet(snippet_source, tokens)
        items.append(
            {
                "message": read,
                "conversation": summaries[cid],
                "category": category_value,
                "snippet": snippet,
                "snippet_highlights": snippet_highlights,
                "highlights": find_highlights(read.content, tokens),
            }
        )
    return {
        "items": items,
        "total": min(counted, TOTAL_CAP),
        "total_capped": counted > TOTAL_CAP,
        "limit": limit,
        "offset": offset,
        "has_more": has_more,
        "next_before_id": ids[-1] if has_more and ids else None,
    }


async def summary(
    db: AsyncSession,
    user_id: int,
    *,
    q: str,
    category: str,
    sender_id: int | None,
    date_from: str | None,
    date_to: str | None,
    limit: int,
) -> dict[str, Any]:
    tokens = split_tokens(q)
    if not tokens:
        raise _error(status.HTTP_422_UNPROCESSABLE_ENTITY, "query_required", "关键词不能为空")
    start, end = parse_range(date_from, date_to)
    _validate(category, tokens, None, sender_id, start, end)
    use_fts = await _fts_available(db)
    query = _Query(
        user_id, tokens=tokens, conversation_id=None, sender_id=sender_id, category=category, start=start, end=end, before_id=None, use_fts=use_fts
    )
    try:
        rows = (
            await db.execute(
                text(
                    f"{query.cte}SELECT i.conversation_id, count(*) AS c, max(i.message_id) AS latest FROM {query.source} "
                    f"WHERE {query.where} GROUP BY i.conversation_id ORDER BY latest DESC"
                ),
                query.params,
            )
        ).all()
    except OperationalError as exc:
        logger.exception("Message search summary failed")
        raise _error(status.HTTP_503_SERVICE_UNAVAILABLE, "search_unavailable", "搜索暂时不可用，稍后再试") from exc
    total = sum(int(row[1]) for row in rows)
    page = rows[:limit]
    latest_at: dict[int, Any] = {}
    if page:
        latest_ids = [int(row[2]) for row in page]
        stamps = (
            await db.execute(select(Message.id, Message.created_at).where(Message.id.in_(latest_ids)))
        ).all()
        latest_at = {message_id: created_at for message_id, created_at in stamps}
    summaries = await _conversation_summaries(db, user_id, [int(row[0]) for row in page])
    items = []
    for cid, count, latest in page:
        if int(cid) not in summaries:
            continue
        created = latest_at.get(int(latest))
        items.append(
            {
                "conversation": summaries[int(cid)],
                "count": int(count),
                "latest_message_id": int(latest),
                "latest_at": tiers.aware(created) if created is not None else None,
            }
        )
    return {"total": total, "items": items}
