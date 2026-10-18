"""机器拿不准的内容作为案源（docs/jury-v2.md 3.3）。

开关 machine_review 打开后，机器判为「拿不准」（review）、又没有被采取任何动作的内容里，群聊的纯文本消息和公开动态的文字
会建成一个没有举报人的案件，直接进委员队列。图片、私聊、色情类不进；每天有上限；排在所有真实举报的后面。
之后有人举报同一条内容，举报会并进这个案件（cases.attach_report 按 target_key 找到它）。

二审（docs/moderation-v2.md 1.10）之后：二审认为没问题的（cleared）不进；一审出错或拿不准、二审认为值得看的（flagged）照常进，
按违规概率排先后；一审判 block 但二审没把握、因此没处罚的（downgraded）也进，排在机器案件的最前面，而且不占每天的名额 ——
它们原本是要直接处罚的。

由 moderation.py 在审核流程的最后调用，调用方保证这里出任何问题都不影响审核本身。
"""

from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.case import JuryMember, ReportCase
from app.models.conversation import Conversation
from app.models.enums import ConversationType, MessageType, ModerationAction, ModerationSource, PostVisibility
from app.models.message import Message
from app.models.post import Post
from app.models.report import ContentModerationRecord
from app.services import cases as case_service
from app.services import jury_settings
from app.services import penalties as penalty_service

logger = logging.getLogger(__name__)
UTC = timezone.utc

# 每天（最近 24 小时）最多进多少件 = max(下限, 在任委员数 × 系数)。旧管线每天约 170 条「拿不准」，全收远超委员容量。
DAILY_PER_JUROR = settings.jury_machine_daily_per_juror
DAILY_MIN = settings.jury_machine_daily_min
# 真实举报的优先级恒为正数（举报人权重 × 理由权重），所以负数一定排在它们后面。
MACHINE_PRIORITY = -1.0
# 二审之后还要交给人看的两种结果
JUDGED_FOR_JURY = {"downgraded", "flagged"}
# 色情类不给委员看（委员池里未成年人占多数）。和投票时可选的类别、分案时的敏感判断是同一个口径。
BLOCKED_CATEGORIES = {"sexual", "minor_sexual", "vulgar", "suggestive"}


def uncertain_text_record(records: list[ContentModerationRecord], *, allow_images: bool) -> ContentModerationRecord | None:
    """这次审核里那条「拿不准」的文本记录；不是这种情况返回 None。

    只认文本：消息必须是纯文本（allow_images=False）；动态可以带图，但图片必须是机器明确放行的。
    """
    texts = [record for record in records if record.content_type == "text"]
    others = [record for record in records if record.content_type != "text"]
    if len(texts) != 1:
        return None
    # 二审过的按二审：值得看的照收（包括一审出错的），认为没问题的不收；没二审过的照旧只收真正的「拿不准」
    if texts[0].judge_outcome not in JUDGED_FOR_JURY and (
        texts[0].judge_outcome == "cleared" or texts[0].decision != "review" or is_moderation_error(texts[0])
    ):
        return None
    if others and (not allow_images or any(record.decision != "allow" for record in others)):
        return None
    return texts[0]


def is_moderation_error(record: ContentModerationRecord) -> bool:
    """审核服务出错（上游模型挂了、输出解析失败）时也记成 review、置信度 0，但那不是「机器拿不准」，只是没审成。
    这种不能交给委员：内容本身多半没问题（2026-10-16 一天 90 条 review 里 39 条是这种，进队列的全是正常聊天）。"""
    from app.services import moderation as moderation_service

    raw = moderation_service._raw_result(record)
    listed = moderation_service._json_list(record.categories)
    return raw.get("decision") == "error" or bool(raw.get("error_code")) or "moderation_error" in listed


def _machine_categories(record: ContentModerationRecord) -> tuple[str, set[str]]:
    """(给案件记的类别, 机器提到过的全部类别)。「拿不准」的记录没有违规结论，类别只是线索：委员定案时可以改。"""
    from app.services import moderation as moderation_service

    raw = moderation_service._raw_result(record).get("category")
    listed = {item for item in moderation_service._json_list(record.categories) if isinstance(item, str)}
    mentioned = listed | ({raw} if isinstance(raw, str) else set())
    if isinstance(raw, str) and raw in penalty_service.CATEGORIES:
        return raw, mentioned
    # 旧版审核服务的类别名和处罚类别不是一套，按同一张表翻译
    matched = [(name, level) for legacy, name, level in moderation_service._LEGACY_TEXT_CATEGORIES if legacy in listed]
    if matched:
        name = max(matched, key=lambda item: item[1])[0]
        return name, mentioned | {name}
    return next((item for item in sorted(listed) if item in penalty_service.CATEGORIES), "other"), mentioned


def machine_priority(record: ContentModerationRecord) -> float:
    """机器案件的排队优先级，恒为负（排在所有真实举报后面）。

    没有二审的都是 MACHINE_PRIORITY；有二审的按违规概率排，一审 block 被降级的整体排在其余机器案件前面。"""
    p = record.judge_p
    if p is None:
        return MACHINE_PRIORITY
    score = (1 + p) / 2 if record.judge_outcome == "downgraded" else p / 2
    return round(MACHINE_PRIORITY + 0.99 * score, 4)


async def daily_cap(db: AsyncSession, now: datetime) -> int:
    """今天最多收多少件。"""
    jurors = await db.execute(
        select(func.count(JuryMember.id)).where(JuryMember.status == "active", or_(JuryMember.term_ends_at.is_(None), JuryMember.term_ends_at > now))
    )
    return max(DAILY_MIN, math.floor(int(jurors.scalar_one() or 0) * DAILY_PER_JUROR))


async def _open_case(
    db: AsyncSession,
    *,
    record: ContentModerationRecord,
    target_type: str,
    target_id: int,
    reported_user_id: int,
    conversation_id: int | None,
    message_id: int | None,
    content_publishable: bool,
    snapshot: Any,
) -> ReportCase | None:
    """建一个机器来源的案件并放进委员队列。snapshot 是一个返回快照的协程函数：前面的条件都过了才去取。"""
    category, mentioned = _machine_categories(record)
    if mentioned & (BLOCKED_CATEGORIES | case_service.SENSITIVE_MACHINE_CATEGORIES):
        return None
    key = f"{target_type}:{target_id}"
    # 这条内容已经有案件了（未结的会接着审，结了的已经有过结论）：不重复建
    existing = await db.execute(select(ReportCase.id).where(ReportCase.target_key == key).limit(1))
    if existing.scalar_one_or_none() is not None:
        return None
    now = datetime.now(UTC)
    # 一审 block、二审降级的不占名额：它们本来是要直接处罚的，不能因为名额满了就谁也不看
    if record.judge_outcome != "downgraded":
        used = await db.execute(
            select(func.count(ReportCase.id)).where(ReportCase.case_origin == "machine", ReportCase.created_at >= now - timedelta(hours=24))
        )
        if int(used.scalar_one() or 0) >= await daily_cap(db, now):
            return None
    case = ReportCase(
        target_key=key,
        target_type=target_type,
        target_id=target_id,
        conversation_id=conversation_id,
        message_id=message_id,
        reported_user_id=reported_user_id,
        first_report_id=None,
        status="open",
        queue="jury",
        jury_eligible=True,
        report_count=0,
        reporter_weight=0.0,
        primary_reason=f"{case_service.MACHINE_REASON_PREFIX} · {penalty_service.category_label(category)}",
        priority=machine_priority(record),
        machine_decision="review",
        machine_category=category,
        machine_confidence=record.confidence,
        judge_p=record.judge_p,
        case_origin="machine",
        # 和举报时固化的快照同一个结构：被判的内容和前后各 10 条上下文
        snapshot=json.dumps(await snapshot(), ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        content_publishable=content_publishable,
        created_at=now,
        updated_at=now,
        queued_at=now,
    )
    db.add(case)
    await db.commit()
    await case_service._broadcast_admin(case, "queued")
    await case_service.notify_jury_queue_changed(db)
    return case


async def offer_message(
    db: AsyncSession, message: Message, records: list[ContentModerationRecord], action: ModerationAction, source: ModerationSource
) -> ReportCase | None:
    """一条消息的机器审核做完之后：符合条件的建成案件。message 要带着 sender（审核任务里已经预加载了）。"""
    # 先看不用查库的条件：绝大多数消息在这里就返回了。举报触发的复审（REPORT_REVIEW）本来就有案件，不走这里。
    if action != ModerationAction.NONE or source != ModerationSource.MESSAGE_AUTO or message.type != MessageType.TEXT or message.recalled_at is not None:
        return None
    record = uncertain_text_record(records, allow_images=False)
    if record is None or not await jury_settings.is_enabled(db, "machine_review"):
        return None
    conversation = await db.get(Conversation, message.conversation_id)
    if conversation is None or conversation.type != ConversationType.GROUP or conversation.is_temporary:
        return None

    async def snapshot() -> dict[str, Any]:
        from app.services import reports as report_service

        # 没有举报人：上下文取会话里实际的前后文
        return await report_service._message_report_snapshot(db, message, conversation, None)

    return await _open_case(
        db,
        record=record,
        target_type="message",
        target_id=message.id,
        reported_user_id=message.sender_id,
        conversation_id=message.conversation_id,
        message_id=message.id,
        content_publishable=False,
        snapshot=snapshot,
    )


async def offer_post(db: AsyncSession, post: Post, records: list[ContentModerationRecord], action: ModerationAction) -> ReportCase | None:
    """一篇动态的机器审核做完之后：公开动态、文字「拿不准」的建成案件。post 要带着 author。"""
    if action != ModerationAction.NONE or post.deleted_at is not None or post.visibility != PostVisibility.PUBLIC:
        return None
    record = uncertain_text_record(records, allow_images=True)
    if record is None or not await jury_settings.is_enabled(db, "machine_review"):
        return None

    async def snapshot() -> dict[str, Any]:
        from app.services import reports as report_service

        return report_service._post_report_snapshot(post)

    return await _open_case(
        db,
        record=record,
        target_type="post",
        target_id=post.id,
        reported_user_id=post.author_id,
        conversation_id=None,
        message_id=None,
        # 公开动态的内容可以公示（和举报建案时一样）
        content_publishable=True,
        snapshot=snapshot,
    )
