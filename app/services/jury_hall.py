"""议事厅（docs/jury-v2.md 3.5）：风纪委员公开讨论规则的地方。实名，所有登录用户可读。

三类帖子：公告（管理员在后台发）、提案（在任委员表决若干天，通过后由管理员决定是否采纳）、讨论。
发帖要正式或资深委员，评论和表决是所有在任委员；受处罚期间都不行。
和观点区不同，这里是实名的公开发言，所以发帖和评论都过一遍同步的文本审核。
"""

from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timedelta, timezone
from fractions import Fraction
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import and_, exists, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.core.config import settings
from app.models.case import JuryMember
from app.models.enums import PlatformRole
from app.models.jury_hall import JuryHallComment, JuryHallPost, JuryHallReport, JuryHallVote
from app.models.report import AdminAuditLog
from app.models.user import User
from app.services import jury as jury_service
from app.services import penalties as penalty_service

logger = logging.getLogger(__name__)
UTC = timezone.utc

# 三期里这个模块和 jury_precedents 带来的能力，/jury/config 的 capabilities 里追加
CAPABILITIES = ["precedents", "hall"]

TITLE_MAX = settings.jury_hall_title_max
CONTENT_MAX = settings.jury_hall_content_max
COMMENT_MAX = settings.jury_hall_comment_max
# 列表里的正文只给前这么多字
LIST_EXCERPT = settings.jury_hall_list_excerpt
POSTS_PER_DAY = settings.jury_hall_posts_per_day
COMMENTS_PER_DAY = settings.jury_hall_comments_per_day
VOTE_DAYS = settings.jury_hall_vote_days
QUORUM_RATIO = settings.jury_hall_quorum_ratio
QUORUM_MIN = settings.jury_hall_quorum_min
PASS_NUM = settings.jury_hall_pass_num
PASS_DEN = settings.jury_hall_pass_den
# 帖子或评论被这么多个不同的人举报后自动折叠
AUTO_HIDE_REPORTS = settings.jury_hall_auto_hide_reports

KINDS = ("announcement", "proposal", "discussion")
# 委员能发的两种；公告只能在后台发
MEMBER_KINDS = ("proposal", "discussion")
POST_TIERS = ("regular", "senior")
STATUSES = ("open", "passed", "rejected", "adopted", "declined", "closed")
VOTE_VALUES = ("yes", "no")
OTHER_REASON = "其他"
REPORT_REASONS = ["人身攻击", "讨论未结案件", "泄露身份或隐私", "与风纪无关", OTHER_REASON]
OTHER_REASON_MIN_DETAILS = 5
REPORT_DETAILS_MAX_LENGTH = 500
# 被折叠的帖子对外不给标题和正文
HIDDEN_TITLE = "该帖已被折叠，等待管理员处理"
ROLLING_WINDOW = timedelta(hours=24)


def guidelines() -> str:
    """要在议事厅里展示的守则。"""
    return "\n".join(
        [
            "议事厅是风纪委员公开讨论规则和评审方式的地方。这里实名发言，全站可见。",
            "",
            "1. 不要讨论具体的未结案件。还在评审、合议或申诉中的案件，不在这里透露内容、不拉票、不打听别人怎么投；已经公示的案件和判例可以引用。",
            "2. 对事不对人。可以反驳观点，不要评价人；不点名指责其他委员、举报人或当事人。",
            "3. 不泄露身份和隐私。不贴没有匿名的聊天记录，不猜测、不公开当事人或评审委员是谁。",
            "4. 提案要说清楚：现在的规则是什么、想改成什么、为什么。一个提案只说一件事。",
            f"5. 规则由管理员修改，提案通过后由管理员决定是否采纳。提案由在任委员表决 {VOTE_DAYS} 天，参与人数和赞成比例都达标即为通过，管理员会说明采纳或不采纳的理由。",
            "6. 违反守则的帖子和评论可以举报，被多人举报后会先折叠，由管理员处理。",
        ]
    )


def config_view() -> dict[str, Any]:
    """GET /jury/config 里的 hall 块。"""
    return {
        "title_max": TITLE_MAX,
        "content_max": CONTENT_MAX,
        "comment_max": COMMENT_MAX,
        "posts_per_day": POSTS_PER_DAY,
        "comments_per_day": COMMENTS_PER_DAY,
        "vote_days": VOTE_DAYS,
        "quorum_ratio": QUORUM_RATIO,
        "quorum_min": QUORUM_MIN,
        "pass_num": PASS_NUM,
        "pass_den": PASS_DEN,
        # 只是给展示用的近似值，服务端按整数比较
        "pass_ratio": round(PASS_NUM / PASS_DEN, 4),
        "guidelines": guidelines(),
        "report_reasons": list(REPORT_REASONS),
        "auto_hide_reports": AUTO_HIDE_REPORTS,
    }


def _error(status_code: int, code: str, message: str, **extra: Any) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message, **extra})


def _post_not_found() -> HTTPException:
    return _error(status.HTTP_404_NOT_FOUND, "post_not_found", "帖子不存在或已删除")


def _comment_not_found() -> HTTPException:
    return _error(status.HTTP_404_NOT_FOUND, "comment_not_found", "评论不存在或已删除")


# ---------------------------------------------------------------- 表决规则


def quorum_for(jurors: int) -> int:
    """一个提案需要的最少表决人数：在任委员数 × 比例向上取整，不低于下限。发帖时算一次，之后不变。"""
    # 比例是小数，直接乘会有浮点误差（10 × 0.3 向上取整会变成 4），按分数算
    return max(QUORUM_MIN, math.ceil(Fraction(str(QUORUM_RATIO)) * jurors))


def passes(yes: int, no: int, quorum: int, pass_num: int, pass_den: int) -> bool:
    """整数比较：表决人数够，并且 yes × pass_den >= (yes + no) × pass_num（恰好等于也算通过）。"""
    return yes + no >= quorum and yes * pass_den >= (yes + no) * pass_num


def _serving_clause(now: datetime) -> Any:
    return and_(JuryMember.status == "active", or_(JuryMember.term_ends_at.is_(None), JuryMember.term_ends_at > now))


async def _serving_ids(db: AsyncSession, now: datetime | None = None) -> list[int]:
    rows = await db.execute(select(JuryMember.user_id).where(_serving_clause(now or datetime.now(UTC))))
    return list(rows.scalars().all())


async def _tallies(db: AsyncSession, post_ids: list[int]) -> dict[int, dict[str, int]]:
    tallies = {post_id: {"yes": 0, "no": 0} for post_id in post_ids}
    if not post_ids:
        return tallies
    rows = await db.execute(
        select(JuryHallVote.post_id, JuryHallVote.vote, func.count()).where(JuryHallVote.post_id.in_(post_ids)).group_by(JuryHallVote.post_id, JuryHallVote.vote)
    )
    for post_id, vote, count in rows.all():
        if vote in tallies[post_id]:
            tallies[post_id][vote] = int(count)
    return tallies


def _due(post: JuryHallPost, now: datetime) -> bool:
    ends_at = penalty_service.aware(post.vote_ends_at)
    return post.kind == "proposal" and post.status == "open" and post.state != "deleted" and ends_at is not None and ends_at <= now


def _settle(post: JuryHallPost, counts: dict[str, int], now: datetime) -> None:
    passed = passes(counts["yes"], counts["no"], post.quorum or QUORUM_MIN, post.pass_num or PASS_NUM, post.pass_den or PASS_DEN)
    post.status = "passed" if passed else "rejected"
    post.settled_at = now
    post.updated_at = now


async def settle_due_proposals(db: AsyncSession, now: datetime | None = None) -> int:
    """给表决到期的提案出结果：通过（待管理员采纳）或未通过。自己提交事务，返回结算了多少个。

    设计成由后台的定时循环调用；读帖子列表时也会调一次，所以即使定时循环没接上，读到的状态也是对的。
    """
    now = now or datetime.now(UTC)
    posts = list(
        (
            await db.execute(
                select(JuryHallPost).where(
                    JuryHallPost.kind == "proposal", JuryHallPost.status == "open", JuryHallPost.state != "deleted", JuryHallPost.vote_ends_at <= now
                )
            )
        ).scalars()
    )
    if not posts:
        return 0
    tallies = await _tallies(db, [post.id for post in posts])
    for post in posts:
        _settle(post, tallies[post.id], now)
    await db.commit()
    for post in posts:
        await _notify(db, post, "closed")
    return len(posts)


async def _settle_if_due(db: AsyncSession, post: JuryHallPost) -> None:
    """按 id 操作单个帖子之前：它是到期的提案就先结算。"""
    now = datetime.now(UTC)
    if not _due(post, now):
        return
    _settle(post, (await _tallies(db, [post.id]))[post.id], now)
    await db.commit()
    await _notify(db, post, "closed")


# ---------------------------------------------------------------- 权限与通用检查


async def _viewer(db: AsyncSession, user: User, now: datetime) -> dict[str, bool]:
    """看的人现在能不能参与：active = 在任委员且没有受处罚。"""
    serving = jury_service._is_serving(await jury_service.get_member(db, user.id), now)
    return {"active": serving and jury_service.restriction(user, now) is None, "admin": user.platform_role == PlatformRole.ADMIN}


async def _post(db: AsyncSession, post_id: int) -> JuryHallPost:
    post = await db.get(JuryHallPost, post_id)
    if post is None or post.state == "deleted":
        raise _post_not_found()
    return post


def _clean_post(title: str | None, content: str | None) -> tuple[str, str]:
    title, content = (title or "").strip(), (content or "").strip()
    if not title or len(title) > TITLE_MAX or not content or len(content) > CONTENT_MAX:
        raise _error(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_post", f"标题和正文都不能为空；标题最多 {TITLE_MAX} 个字，正文最多 {CONTENT_MAX} 个字"
        )
    return title, content


async def ensure_text_allowed(user: User, text: str, context_id: str) -> None:
    """发表前过一遍同步的文本审核，明确违规的才拒绝。

    审核没启用、发言人在白名单里、或者审核服务不可用时都放行 —— 不能因为它挂了议事厅就发不了言。
    """
    if not text or not settings.content_moderation_enabled or user.content_moderation_whitelisted:
        return
    from app.services import moderation as moderation_service

    try:
        result = await moderation_service._call_text_moderation_for_user(text, user.id, context_id=context_id)
    except Exception:
        return
    if result.get("decision") == "block":
        raise _error(status.HTTP_422_UNPROCESSABLE_ENTITY, "content_rejected", "内容未通过审核，请修改后重试")


async def _check_rate(db: AsyncSession, model: Any, user_id: int, limit: int, noun: str, now: datetime, *clauses: Any) -> None:
    """每人每天的上限，和评审上限一样按最近 24 小时滚动。删掉的也算，否则可以靠删了重发绕过去。"""
    scope = (model.author_id == user_id, *clauses)
    count = await db.execute(select(func.count()).select_from(model).where(*scope, model.created_at >= now - ROLLING_WINDOW))
    if int(count.scalar_one() or 0) < limit:
        return
    resets_at = await jury_service.rolling_resets_at(db, model.created_at, *scope, now=now)
    raise _error(
        status.HTTP_429_TOO_MANY_REQUESTS,
        "hall_rate_limited",
        f"今天的{noun}已达上限（{limit}），明天再来",
        resets_at=jury_service._iso(resets_at) if resets_at is not None else None,
    )


def _lock_reason(post: JuryHallPost, votes: int) -> str | None:
    """作者现在为什么不能改这个帖子；None 表示可以。"""
    if post.state != "visible":
        return "这个帖子已被折叠，不能修改"
    if post.status != "open":
        return "这个帖子已经结束，不能修改"
    if post.kind == "proposal" and votes > 0:
        return "已经有人表决了，提案不能再修改"
    return None


def _deletable(post: JuryHallPost) -> bool:
    """作者能不能删：出了结果的提案是表决的记录，留着。"""
    return not (post.kind == "proposal" and post.status != "open")


# ---------------------------------------------------------------- 实时事件


async def _notify(db: AsyncSession, post: JuryHallPost, action: str) -> None:
    """jury.hall.changed 发给在任委员。action：created / updated / closed。发不出去只记日志，不影响主流程。"""
    from app.websocket.manager import manager

    try:
        juror_ids = await _serving_ids(db)
        if juror_ids:
            await manager.send_to_users(set(juror_ids), "jury.hall.changed", {"post_id": post.id, "kind": post.kind, "action": action})
    except Exception:
        logger.exception("Failed to notify hall change", extra={"post_id": post.id, "action": action})


async def _broadcast_admin(post_id: int, action: str) -> None:
    from app.websocket.manager import admin_manager

    try:
        await admin_manager.broadcast("admin.jury.hall.changed", {"post_id": post_id, "action": action})
    except Exception:
        logger.exception("Failed to broadcast hall change", extra={"post_id": post_id})


# ---------------------------------------------------------------- 帖子：展示


async def _users(db: AsyncSession, user_ids: set[int]) -> dict[int, User]:
    if not user_ids:
        return {}
    return {user.id: user for user in (await db.execute(select(User).where(User.id.in_(user_ids)))).scalars()}


async def _tiers(db: AsyncSession, user_ids: set[int], now: datetime) -> dict[int, str]:
    """这些人里现在在任的委员的层级。"""
    if not user_ids:
        return {}
    rows = await db.execute(select(JuryMember.user_id, JuryMember.tier).where(JuryMember.user_id.in_(user_ids), _serving_clause(now)))
    return dict(rows.all())


async def _comment_counts(db: AsyncSession, post_ids: list[int]) -> dict[int, int]:
    """每个帖子还在的评论数（含回复、含被折叠的，不含已删除的）。"""
    rows = await db.execute(
        select(JuryHallComment.post_id, func.count())
        .where(JuryHallComment.post_id.in_(post_ids), JuryHallComment.state != "deleted")
        .group_by(JuryHallComment.post_id)
    )
    return {post_id: int(count) for post_id, count in rows.all()}


async def _reported_ids(db: AsyncSession, user_id: int, target_type: str, target_ids: list[int]) -> set[int]:
    rows = await db.execute(
        select(JuryHallReport.target_id).where(
            JuryHallReport.reporter_id == user_id, JuryHallReport.target_type == target_type, JuryHallReport.target_id.in_(target_ids)
        )
    )
    return set(rows.scalars().all())


async def _post_views(db: AsyncSession, posts: list[JuryHallPost], user: User, *, excerpt: bool) -> list[dict[str, Any]]:
    if not posts:
        return []
    now = datetime.now(UTC)
    post_ids = [post.id for post in posts]
    author_ids = {post.author_id for post in posts}
    authors = await _users(db, author_ids)
    tiers = await _tiers(db, author_ids, now)
    tallies = await _tallies(db, [post.id for post in posts if post.kind == "proposal"])
    my_votes = dict(
        (await db.execute(select(JuryHallVote.post_id, JuryHallVote.vote).where(JuryHallVote.user_id == user.id, JuryHallVote.post_id.in_(post_ids)))).all()
    )
    comments = await _comment_counts(db, post_ids)
    reported = await _reported_ids(db, user.id, "post", post_ids)
    viewer = await _viewer(db, user, now)
    views: list[dict[str, Any]] = []
    for post in posts:
        mine = post.author_id == user.id
        visible = post.state == "visible"
        announcement = post.kind == "announcement"
        counts = tallies.get(post.id, {"yes": 0, "no": 0})
        vote: dict[str, Any] | None = None
        if post.kind == "proposal":
            ends_at = penalty_service.aware(post.vote_ends_at)
            vote = {
                "yes": counts["yes"],
                "no": counts["no"],
                "ends_at": ends_at,
                "my_vote": my_votes.get(post.id),
                "can_vote": viewer["active"] and visible and post.status == "open" and ends_at is not None and now < ends_at,
                "quorum": post.quorum or QUORUM_MIN,
                "pass_num": post.pass_num or PASS_NUM,
                "pass_den": post.pass_den or PASS_DEN,
            }
        content = post.content if visible else None
        truncated = excerpt and content is not None and len(content) > LIST_EXCERPT
        views.append(
            {
                "id": post.id,
                "kind": post.kind,
                "title": post.title if visible else HIDDEN_TITLE,
                "content": content[:LIST_EXCERPT] if truncated else content,
                "content_truncated": truncated,
                "author": authors.get(post.author_id),
                # 公告是管理员以管理员身份发的，不带层级
                "author_tier": None if announcement else tiers.get(post.author_id),
                "status": post.status,
                "state": post.state,
                "pinned": bool(post.pinned),
                "vote": vote,
                "admin_note": post.admin_note,
                "comment_count": comments.get(post.id, 0),
                "can_comment": viewer["active"] and visible and post.status != "closed",
                "mine": mine,
                # 公告的作者是管理员，不要求他是委员；其余的要求作者现在仍能发言
                "can_edit": mine
                and (viewer["admin"] if announcement else viewer["active"])
                and _lock_reason(post, counts["yes"] + counts["no"]) is None,
                "can_delete": mine and _deletable(post),
                "reported": post.id in reported,
                "created_at": penalty_service.aware(post.created_at),
                "updated_at": penalty_service.aware(post.updated_at),
            }
        )
    return views


async def post_view(db: AsyncSession, user: User, post: JuryHallPost) -> dict[str, Any]:
    return (await _post_views(db, [post], user, excerpt=False))[0]


async def list_posts(
    db: AsyncSession,
    user: User,
    *,
    kind: str | None = None,
    status_filter: str | None = None,
    sort: str = "active",
    limit: int = 20,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    """GET /jury/hall/posts。置顶始终在前；其余按最后活动时间（active）或发帖时间（new）倒序。正文只给前 LIST_EXCERPT 个字。"""
    await settle_due_proposals(db)
    clauses: list[Any] = [JuryHallPost.state != "deleted"]
    if kind:
        clauses.append(JuryHallPost.kind == kind)
    if status_filter:
        clauses.append(JuryHallPost.status == status_filter)
    total = int((await db.execute(select(func.count(JuryHallPost.id)).where(*clauses))).scalar_one() or 0)
    moment = JuryHallPost.created_at if sort == "new" else JuryHallPost.last_activity_at
    rows = await db.execute(
        select(JuryHallPost).where(*clauses).order_by(JuryHallPost.pinned.desc(), moment.desc(), JuryHallPost.id.desc()).offset(offset).limit(limit)
    )
    return await _post_views(db, list(rows.scalars()), user, excerpt=True), total


async def get_post(db: AsyncSession, user: User, post_id: int) -> dict[str, Any]:
    """GET /jury/hall/posts/{id}，正文是全文。"""
    post = await _post(db, post_id)
    await _settle_if_due(db, post)
    return await post_view(db, user, post)


# ---------------------------------------------------------------- 帖子：发表 / 修改 / 删除 / 表决


async def create_post(db: AsyncSession, user: User, kind: str | None, title: str | None, content: str | None) -> dict[str, Any]:
    """POST /jury/hall/posts：提案或讨论，正式、资深委员才能发。"""
    member = await jury_service._require_serving(db, user)
    if member.tier not in POST_TIERS:
        raise _error(status.HTTP_403_FORBIDDEN, "tier_required", "正式委员和资深委员才能在议事厅发帖，见习委员可以评论和表决")
    if kind not in MEMBER_KINDS:
        raise _error(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_post", "帖子类型只能是提案（proposal）或讨论（discussion）")
    title, content = _clean_post(title, content)
    now = datetime.now(UTC)
    # 公告不占发帖的额度（管理员本人如果也是委员）
    await _check_rate(db, JuryHallPost, user.id, POSTS_PER_DAY, "发帖数", now, JuryHallPost.kind != "announcement")
    # 标题也审，和正文一起送一次
    await ensure_text_allowed(user, f"{title}\n{content}", f"jury_hall_post:{user.id}")
    post = JuryHallPost(kind=kind, title=title, content=content, author_id=user.id, created_at=now, updated_at=now, last_activity_at=now)
    if kind == "proposal":
        post.vote_ends_at = now + timedelta(days=VOTE_DAYS)
        # 最少表决人数和通过线按发帖这一刻固定下来，之后委员人数或配置变了也不影响这个提案
        post.quorum = quorum_for(len(await _serving_ids(db, now)))
        post.pass_num = PASS_NUM
        post.pass_den = PASS_DEN
    db.add(post)
    await db.commit()
    await _notify(db, post, "created")
    await _broadcast_admin(post.id, "created")
    return await post_view(db, user, post)


def _require_author(post: JuryHallPost, user: User) -> None:
    if post.author_id != user.id:
        raise _error(status.HTTP_403_FORBIDDEN, "not_post_author", "只能修改或删除自己发的帖子")


async def _vote_count(db: AsyncSession, post_id: int) -> int:
    result = await db.execute(select(func.count()).select_from(JuryHallVote).where(JuryHallVote.post_id == post_id))
    return int(result.scalar_one() or 0)


async def update_post(db: AsyncSession, user: User, post_id: int, title: str | None, content: str | None) -> dict[str, Any]:
    """PUT /jury/hall/posts/{id}：作者本人；提案有人表决后不能再改。只改传进来的那一项。"""
    post = await _post(db, post_id)
    _require_author(post, user)
    announcement = post.kind == "announcement"
    if announcement:
        if user.platform_role != PlatformRole.ADMIN:
            raise _error(status.HTTP_403_FORBIDDEN, "not_post_author", "公告只能由管理员修改")
    else:
        # 修改等于重新发言：要求现在仍是在任委员、没有受处罚
        await jury_service._require_serving(db, user)
    await _settle_if_due(db, post)
    reason = _lock_reason(post, await _vote_count(db, post.id))
    if reason is not None:
        raise _error(status.HTTP_409_CONFLICT, "post_locked", reason)
    new_title, new_content = _clean_post(title if title is not None else post.title, content if content is not None else post.content)
    if (new_title, new_content) != (post.title, post.content):
        if not announcement:
            await ensure_text_allowed(user, f"{new_title}\n{new_content}", f"jury_hall_post:{user.id}")
        post.title = new_title
        post.content = new_content
        post.updated_at = datetime.now(UTC)
        await db.commit()
        await _notify(db, post, "updated")
    return await post_view(db, user, post)


def _mark_deleted(target: JuryHallPost | JuryHallComment, actor_id: int, now: datetime, note: str | None = None) -> None:
    target.state = "deleted"
    target.deleted_at = now
    target.deleted_by_id = actor_id
    if note is not None:
        target.moderation_note = note


async def delete_post(db: AsyncSession, user: User, post_id: int) -> dict[str, Any]:
    """DELETE /jury/hall/posts/{id}：作者本人。被停职 / 卸任的前委员也可以删自己以前发的。"""
    post = await _post(db, post_id)
    _require_author(post, user)
    await _settle_if_due(db, post)
    if not _deletable(post):
        raise _error(status.HTTP_409_CONFLICT, "post_locked", "提案已经出了表决结果，不能删除")
    _mark_deleted(post, user.id, datetime.now(UTC))
    await db.commit()
    await _notify(db, post, "closed")
    await _broadcast_admin(post.id, "deleted")
    return {"id": post.id, "state": post.state}


async def cast_vote(db: AsyncSession, user: User, post_id: int, vote: str | None) -> dict[str, Any]:
    """POST /jury/hall/posts/{id}/vote：所有在任委员，结束前可以改票。"""
    await jury_service._require_serving(db, user)
    if vote not in VOTE_VALUES:
        raise _error(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_vote", "表决只能是 yes 或 no")
    post = await _post(db, post_id)
    await _settle_if_due(db, post)
    now = datetime.now(UTC)
    ends_at = penalty_service.aware(post.vote_ends_at)
    if post.kind != "proposal":
        raise _error(status.HTTP_409_CONFLICT, "vote_closed", "只有提案可以表决")
    if post.state != "visible" or post.status != "open" or ends_at is None or ends_at <= now:
        raise _error(status.HTTP_409_CONFLICT, "vote_closed", "这个提案的表决已经结束")
    existing = (
        await db.execute(select(JuryHallVote).where(JuryHallVote.post_id == post.id, JuryHallVote.user_id == user.id))
    ).scalar_one_or_none()
    if existing is None:
        db.add(JuryHallVote(post_id=post.id, user_id=user.id, vote=vote, created_at=now, updated_at=now))
    elif existing.vote != vote:
        existing.vote = vote
        existing.updated_at = now
    try:
        await db.commit()
    except IntegrityError:
        # 同一个人的两次表决同时到达：另一次已经记上了，按改票处理。
        await db.rollback()
        await db.execute(update(JuryHallVote).where(JuryHallVote.post_id == post_id, JuryHallVote.user_id == user.id).values(vote=vote, updated_at=now))
        await db.commit()
        post = await _post(db, post_id)
    await _notify(db, post, "updated")
    return await post_view(db, user, post)


# ---------------------------------------------------------------- 评论


async def _comment_views(db: AsyncSession, comments: list[JuryHallComment], user: User) -> list[dict[str, Any]]:
    if not comments:
        return []
    now = datetime.now(UTC)
    comment_ids = [item.id for item in comments]
    # 被回复的那条可能不在这一页里
    reply_authors = {item.id: item.author_id for item in comments}
    missing = {item.reply_to_id for item in comments if item.reply_to_id is not None} - set(reply_authors)
    if missing:
        rows = await db.execute(select(JuryHallComment.id, JuryHallComment.author_id).where(JuryHallComment.id.in_(missing)))
        reply_authors.update(dict(rows.all()))
    author_ids = {item.author_id for item in comments}
    users = await _users(db, author_ids | {reply_authors[item.reply_to_id] for item in comments if item.reply_to_id in reply_authors})
    tiers = await _tiers(db, author_ids, now)
    reported = await _reported_ids(db, user.id, "comment", comment_ids)
    return [
        {
            "id": item.id,
            "parent_id": item.parent_id,
            "author": users.get(item.author_id),
            "author_tier": tiers.get(item.author_id),
            "content": item.content if item.state == "visible" else None,
            "state": item.state,
            "mine": item.author_id == user.id,
            "reported": item.id in reported,
            "created_at": penalty_service.aware(item.created_at),
            "reply_to": users.get(reply_authors.get(item.reply_to_id)) if item.reply_to_id is not None else None,
        }
        for item in comments
    ]


async def list_comments(db: AsyncSession, user: User, post_id: int, *, limit: int = 50, offset: int = 0) -> tuple[list[dict[str, Any]], int]:
    """GET /jury/hall/posts/{id}/comments：平铺、时间正序，回复用 parent_id 归到根评论下。

    被折叠的留占位（content 为 null）。已删除的回复不出现；已删除的根评论只在下面还有回复时留一个占位。
    """
    post = await _post(db, post_id)
    reply = aliased(JuryHallComment)
    # 带上 post_id 是为了让子查询走 (post_id, created_at) 索引，不去扫全表
    has_reply = exists().where(reply.post_id == JuryHallComment.post_id, reply.parent_id == JuryHallComment.id, reply.state != "deleted")
    clauses = [
        JuryHallComment.post_id == post.id,
        or_(JuryHallComment.state != "deleted", and_(JuryHallComment.parent_id.is_(None), has_reply)),
    ]
    total = int((await db.execute(select(func.count(JuryHallComment.id)).where(*clauses))).scalar_one() or 0)
    rows = await db.execute(
        select(JuryHallComment).where(*clauses).order_by(JuryHallComment.created_at.asc(), JuryHallComment.id.asc()).offset(offset).limit(limit)
    )
    return await _comment_views(db, list(rows.scalars()), user), total


async def create_comment(db: AsyncSession, user: User, post_id: int, content: str | None, parent_id: int | None) -> dict[str, Any]:
    """POST /jury/hall/posts/{id}/comments：所有在任委员（含见习）。"""
    await jury_service._require_serving(db, user)
    post = await _post(db, post_id)
    if post.state != "visible" or post.status == "closed":
        raise _error(status.HTTP_409_CONFLICT, "post_closed", "这个帖子已经关闭或被折叠，不能再评论")
    text = (content or "").strip()
    if not text or len(text) > COMMENT_MAX:
        raise _error(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_comment", f"评论不能为空，也不能超过 {COMMENT_MAX} 个字")
    root_id: int | None = None
    reply_to_id: int | None = None
    if parent_id is not None:
        parent = await db.get(JuryHallComment, parent_id)
        if parent is None or parent.post_id != post.id or parent.state == "deleted":
            raise _comment_not_found()
        # 回复只有一层：回复一条回复时挂到同一条根评论下面，另外记下回复的是谁。
        root_id = parent.parent_id or parent.id
        reply_to_id = parent.id if parent.parent_id is not None else None
    now = datetime.now(UTC)
    await _check_rate(db, JuryHallComment, user.id, COMMENTS_PER_DAY, "评论数", now)
    await ensure_text_allowed(user, text, f"jury_hall_comment:{post.id}")
    comment = JuryHallComment(post_id=post.id, author_id=user.id, parent_id=root_id, reply_to_id=reply_to_id, content=text, created_at=now)
    db.add(comment)
    post.last_activity_at = now
    await db.commit()
    await _notify(db, post, "updated")
    return (await _comment_views(db, [comment], user))[0]


async def delete_comment(db: AsyncSession, user: User, comment_id: int) -> dict[str, Any]:
    """DELETE /jury/hall/comments/{id}：作者本人。被停职 / 卸任的前委员也可以删自己以前发的。"""
    comment = await db.get(JuryHallComment, comment_id)
    post = await db.get(JuryHallPost, comment.post_id) if comment is not None else None
    if comment is None or post is None or post.state == "deleted":
        raise _comment_not_found()
    if comment.author_id != user.id:
        raise _error(status.HTTP_403_FORBIDDEN, "not_comment_author", "只能删除自己发的评论")
    if comment.state != "deleted":
        _mark_deleted(comment, user.id, datetime.now(UTC))
        await db.commit()
        await _notify(db, post, "updated")
    return {"id": comment.id, "state": comment.state}


# ---------------------------------------------------------------- 举报


async def _report_target(db: AsyncSession, target_type: str, target_id: int) -> tuple[JuryHallPost | JuryHallComment, JuryHallPost]:
    """(被举报的帖子或评论, 它所在的帖子)。已删除的当不存在。"""
    if target_type == "post":
        post = await _post(db, target_id)
        return post, post
    comment = await db.get(JuryHallComment, target_id)
    post = await db.get(JuryHallPost, comment.post_id) if comment is not None else None
    if comment is None or comment.state == "deleted" or post is None or post.state == "deleted":
        raise _comment_not_found()
    return comment, post


async def _pending_reports(db: AsyncSession, target_type: str, target_id: int) -> int:
    # 一个人对一个目标只能举报一次（唯一约束），所以条数就是人数
    result = await db.execute(
        select(func.count())
        .select_from(JuryHallReport)
        .where(JuryHallReport.target_type == target_type, JuryHallReport.target_id == target_id, JuryHallReport.status == "pending")
    )
    return int(result.scalar_one() or 0)


async def create_report(db: AsyncSession, user: User, target_type: str, target_id: int, reason: str | None, details: str | None) -> dict[str, Any]:
    """POST /jury/hall/reports：任何登录用户都可以举报帖子或评论。被够多的人举报后先折叠，等管理员处理。"""
    if target_type not in {"post", "comment"}:
        raise _error(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_target", "只能举报帖子或评论")
    target, post = await _report_target(db, target_type, target_id)
    if target.author_id == user.id:
        raise _error(status.HTTP_400_BAD_REQUEST, "cannot_report_own", "不能举报自己发的内容")
    reason = (reason or "").strip()
    text = (details or "").strip()
    if reason not in REPORT_REASONS:
        raise _error(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_reason", "请选择一个举报理由", reasons=REPORT_REASONS)
    if (reason == OTHER_REASON and len(text) < OTHER_REASON_MIN_DETAILS) or len(text) > REPORT_DETAILS_MAX_LENGTH:
        raise _error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "invalid_reason",
            f"选择「其他」时请写明具体情况（至少 {OTHER_REASON_MIN_DETAILS} 个字）；说明最多 {REPORT_DETAILS_MAX_LENGTH} 个字",
        )
    already = _error(status.HTTP_409_CONFLICT, "already_reported", "你已经举报过了")
    existing = await db.execute(
        select(JuryHallReport.id)
        .where(JuryHallReport.target_type == target_type, JuryHallReport.target_id == target_id, JuryHallReport.reporter_id == user.id)
        .limit(1)
    )
    if existing.scalar_one_or_none() is not None:
        raise already
    db.add(JuryHallReport(target_type=target_type, target_id=target_id, post_id=post.id, reporter_id=user.id, reason=reason, details=text or None))
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise already from None
    # 公告不自动折叠：否则随便几个人就能把管理员的公告藏起来。举报照样记下来给管理员看。
    foldable = not (target_type == "post" and post.kind == "announcement")
    hidden = foldable and target.state == "visible" and await _pending_reports(db, target_type, target_id) >= AUTO_HIDE_REPORTS
    if hidden:
        target.state = "hidden"
    await db.commit()
    if hidden:
        await _notify(db, post, "updated")
    await _broadcast_admin(post.id, "reported")
    return {"target_type": target_type, "target_id": target_id, "state": target.state}


# ---------------------------------------------------------------- 后台


def _audit(db: AsyncSession, actor_id: int, action: str, target_type: str, target_id: int, details: dict[str, Any]) -> None:
    db.add(
        AdminAuditLog(
            actor_id=actor_id, action=f"jury.hall.{action}", target_type=target_type, target_id=target_id, details=json.dumps(details, ensure_ascii=False)
        )
    )


async def admin_post_reads(db: AsyncSession, posts: list[JuryHallPost]) -> list[dict[str, Any]]:
    """后台视图：含已删除 / 被折叠的，标题和正文始终是原文。"""
    if not posts:
        return []
    now = datetime.now(UTC)
    post_ids = [post.id for post in posts]
    author_ids = {post.author_id for post in posts}
    authors = await _users(db, author_ids)
    tiers = await _tiers(db, author_ids, now)
    tallies = await _tallies(db, [post.id for post in posts if post.kind == "proposal"])
    comments = await _comment_counts(db, post_ids)
    pending = dict(
        (
            await db.execute(
                select(JuryHallReport.post_id, func.count())
                .where(JuryHallReport.post_id.in_(post_ids), JuryHallReport.status == "pending")
                .group_by(JuryHallReport.post_id)
            )
        ).all()
    )
    items: list[dict[str, Any]] = []
    for post in posts:
        vote: dict[str, Any] | None = None
        if post.kind == "proposal":
            vote = {
                **tallies[post.id],
                "ends_at": penalty_service.aware(post.vote_ends_at),
                "quorum": post.quorum or QUORUM_MIN,
                "pass_num": post.pass_num or PASS_NUM,
                "pass_den": post.pass_den or PASS_DEN,
            }
        items.append(
            {
                "id": post.id,
                "kind": post.kind,
                "title": post.title,
                "content": post.content,
                "author_id": post.author_id,
                "author": authors.get(post.author_id),
                "author_tier": None if post.kind == "announcement" else tiers.get(post.author_id),
                "status": post.status,
                "state": post.state,
                "pinned": bool(post.pinned),
                "vote": vote,
                "settled_at": penalty_service.aware(post.settled_at),
                "admin_note": post.admin_note,
                "decided_by_id": post.decided_by_id,
                "decided_at": penalty_service.aware(post.decided_at),
                "comment_count": comments.get(post.id, 0),
                # 这个帖子和它下面的评论一共还有多少条待处理的举报
                "pending_reports": int(pending.get(post.id, 0)),
                "created_at": penalty_service.aware(post.created_at),
                "updated_at": penalty_service.aware(post.updated_at),
                "last_activity_at": penalty_service.aware(post.last_activity_at),
                "deleted_at": penalty_service.aware(post.deleted_at),
                "deleted_by_id": post.deleted_by_id,
                "moderation_note": post.moderation_note,
            }
        )
    return items


async def admin_list_posts(
    db: AsyncSession,
    *,
    kind: str | None = None,
    status_filter: str | None = None,
    state: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    """后台的帖子列表：默认含已删除的，按发帖时间倒序。"""
    await settle_due_proposals(db)
    clauses: list[Any] = []
    if kind:
        clauses.append(JuryHallPost.kind == kind)
    if status_filter:
        clauses.append(JuryHallPost.status == status_filter)
    if state:
        clauses.append(JuryHallPost.state == state)
    total = int((await db.execute(select(func.count(JuryHallPost.id)).where(*clauses))).scalar_one() or 0)
    rows = await db.execute(select(JuryHallPost).where(*clauses).order_by(JuryHallPost.id.desc()).offset(offset).limit(limit))
    return await admin_post_reads(db, list(rows.scalars())), total


async def _admin_post(db: AsyncSession, post_id: int) -> JuryHallPost:
    post = await db.get(JuryHallPost, post_id)
    if post is None:
        raise _post_not_found()
    return post


async def admin_post_detail(db: AsyncSession, post_id: int) -> dict[str, Any]:
    """后台的帖子详情：带全部评论的原文（含已删除 / 被折叠的）。"""
    post = await _admin_post(db, post_id)
    await _settle_if_due(db, post)
    comments = list(
        (
            await db.execute(
                select(JuryHallComment).where(JuryHallComment.post_id == post.id).order_by(JuryHallComment.created_at.asc(), JuryHallComment.id.asc())
            )
        ).scalars()
    )
    users = await _users(db, {comment.author_id for comment in comments})
    return {
        **(await admin_post_reads(db, [post]))[0],
        "comments": [
            {
                "id": comment.id,
                "post_id": comment.post_id,
                "parent_id": comment.parent_id,
                "reply_to_id": comment.reply_to_id,
                "author_id": comment.author_id,
                "author": users.get(comment.author_id),
                "content": comment.content,
                "state": comment.state,
                "created_at": penalty_service.aware(comment.created_at),
                "deleted_at": penalty_service.aware(comment.deleted_at),
                "deleted_by_id": comment.deleted_by_id,
                "moderation_note": comment.moderation_note,
            }
            for comment in comments
        ],
    }


async def admin_create_announcement(db: AsyncSession, admin: User, title: str | None, content: str | None, pinned: bool = False) -> dict[str, Any]:
    """后台发公告。管理员不需要是委员；不过文本审核，不占发帖额度。"""
    title, content = _clean_post(title, content)
    now = datetime.now(UTC)
    post = JuryHallPost(
        kind="announcement", title=title, content=content, author_id=admin.id, pinned=bool(pinned), created_at=now, updated_at=now, last_activity_at=now
    )
    db.add(post)
    await db.flush()
    _audit(db, admin.id, "post.announce", "jury_hall_post", post.id, {"title": title, "pinned": bool(pinned)})
    await db.commit()
    await _notify(db, post, "created")
    await _broadcast_admin(post.id, "created")
    return (await admin_post_reads(db, [post]))[0]


async def _close_reports(db: AsyncSession, admin_id: int, now: datetime, report_status: str, note: str | None, *clauses: Any) -> None:
    await db.execute(
        update(JuryHallReport)
        .where(JuryHallReport.status == "pending", *clauses)
        .values(status=report_status, handled_by_id=admin_id, handled_at=now, handle_note=note)
    )


async def admin_decide(db: AsyncSession, admin: User, post_id: int, action: str, note: str | None) -> dict[str, Any]:
    """POST /admin/jury/hall/posts/{id}/decision。

    adopt / decline：把通过的提案标为已采纳 / 不采纳（也可以在两者之间改）；close：关闭还开着的帖子，之后不能评论和表决；
    delete：删除；pin / unpin：置顶。note 在 adopt / decline / close 时对所有人展示，delete 时只留在后台。
    """
    post = await _admin_post(db, post_id)
    if post.state == "deleted" and action != "delete":
        raise _post_not_found()
    await _settle_if_due(db, post)
    now = datetime.now(UTC)
    text = note.strip()[:2000] if note and note.strip() else None
    before = {"status": post.status, "state": post.state, "pinned": bool(post.pinned)}
    event = "updated"
    if action in {"adopt", "decline"}:
        if post.kind != "proposal" or post.status not in {"passed", "adopted", "declined"}:
            raise _error(status.HTTP_409_CONFLICT, "invalid_post_status", "只有表决通过的提案可以标为采纳或不采纳")
        post.status = "adopted" if action == "adopt" else "declined"
        post.admin_note = text
        post.decided_by_id = admin.id
        post.decided_at = now
    elif action == "close":
        if post.status != "open":
            raise _error(status.HTTP_409_CONFLICT, "invalid_post_status", "这个帖子已经结束了")
        post.status = "closed"
        post.admin_note = text
        post.decided_by_id = admin.id
        post.decided_at = now
        event = "closed"
    elif action == "delete":
        if post.state != "deleted":
            _mark_deleted(post, admin.id, now, text)
            # 帖子和它下面评论的举报一起了结
            await _close_reports(db, admin.id, now, "handled", text, JuryHallReport.post_id == post.id)
        event = "closed"
    elif action in {"pin", "unpin"}:
        post.pinned = action == "pin"
    else:
        raise _error(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_action", "未知的处置动作")
    after = {"status": post.status, "state": post.state, "pinned": bool(post.pinned)}
    if after != before or action in {"adopt", "decline"}:
        post.updated_at = now
        _audit(db, admin.id, f"post.{action}", "jury_hall_post", post.id, {"kind": post.kind, "title": post.title, "from": before, "to": after, "note": text})
    await db.commit()
    if after != before or action in {"adopt", "decline"}:
        await _notify(db, post, event)
        await _broadcast_admin(post.id, action)
    return (await admin_post_reads(db, [post]))[0]


async def admin_report_reads(db: AsyncSession, reports: list[JuryHallReport]) -> list[dict[str, Any]]:
    if not reports:
        return []
    post_ids = {report.post_id for report in reports}
    comment_ids = {report.target_id for report in reports if report.target_type == "comment"}
    posts = {post.id: post for post in (await db.execute(select(JuryHallPost).where(JuryHallPost.id.in_(post_ids)))).scalars()}
    comments = (
        {comment.id: comment for comment in (await db.execute(select(JuryHallComment).where(JuryHallComment.id.in_(comment_ids)))).scalars()}
        if comment_ids
        else {}
    )
    pending: dict[tuple[str, int], int] = {}
    rows = await db.execute(
        select(JuryHallReport.target_type, JuryHallReport.target_id, func.count())
        .where(JuryHallReport.post_id.in_(post_ids), JuryHallReport.status == "pending")
        .group_by(JuryHallReport.target_type, JuryHallReport.target_id)
    )
    for target_type, target_id, count in rows.all():
        pending[(target_type, target_id)] = int(count)
    user_ids = {report.reporter_id for report in reports} | {post.author_id for post in posts.values()} | {comment.author_id for comment in comments.values()}
    users = await _users(db, user_ids)
    items: list[dict[str, Any]] = []
    for report in reports:
        post = posts.get(report.post_id)
        subject = post if report.target_type == "post" else comments.get(report.target_id)
        target: dict[str, Any] | None = None
        if post is not None and subject is not None:
            target = {
                "type": report.target_type,
                "id": report.target_id,
                "post_id": post.id,
                "post_title": post.title,
                "kind": post.kind,
                "content": subject.content,
                "state": subject.state,
                "author_id": subject.author_id,
                "author": users.get(subject.author_id),
                "pending_reports": pending.get((report.target_type, report.target_id), 0),
            }
        items.append(
            {
                "id": report.id,
                "target_type": report.target_type,
                "target_id": report.target_id,
                "target": target,
                "reporter_id": report.reporter_id,
                "reporter": users.get(report.reporter_id),
                "reason": report.reason,
                "details": report.details,
                "status": report.status,
                "created_at": penalty_service.aware(report.created_at),
                "handled_by_id": report.handled_by_id,
                "handled_at": penalty_service.aware(report.handled_at),
                "handle_note": report.handle_note,
            }
        )
    return items


async def admin_list_reports(
    db: AsyncSession, *, status_filter: str | None = None, limit: int = 50, offset: int = 0
) -> tuple[list[dict[str, Any]], int]:
    """GET /admin/jury/hall/reports?status=：逐条举报，按时间倒序。"""
    clauses: list[Any] = []
    if status_filter:
        clauses.append(JuryHallReport.status == status_filter)
    total = int((await db.execute(select(func.count(JuryHallReport.id)).where(*clauses))).scalar_one() or 0)
    rows = await db.execute(select(JuryHallReport).where(*clauses).order_by(JuryHallReport.id.desc()).offset(offset).limit(limit))
    return await admin_report_reads(db, list(rows.scalars())), total


async def admin_handle_report(db: AsyncSession, admin: User, report_id: int, action: str, note: str | None) -> dict[str, Any]:
    """POST /admin/jury/hall/reports/{id}/handle。

    delete：删除被举报的帖子 / 评论；dismiss：举报不成立，被折叠的恢复显示。
    管理员判断的是那条内容，所以同一个目标上其余待处理的举报一起了结。
    """
    report = await db.get(JuryHallReport, report_id)
    if report is None:
        raise _error(status.HTTP_404_NOT_FOUND, "report_not_found", "举报不存在")
    if report.status != "pending":
        raise _error(status.HTTP_409_CONFLICT, "report_handled", "这条举报已经处理过了")
    if action not in {"delete", "dismiss"}:
        raise _error(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_action", "未知的处置动作")
    post = await db.get(JuryHallPost, report.post_id)
    target: JuryHallPost | JuryHallComment | None = post if report.target_type == "post" else await db.get(JuryHallComment, report.target_id)
    now = datetime.now(UTC)
    text = note.strip()[:500] if note and note.strip() else None
    before = target.state if target is not None else None
    same_target = (JuryHallReport.target_type == report.target_type, JuryHallReport.target_id == report.target_id)
    if action == "delete":
        if target is not None and target.state != "deleted":
            _mark_deleted(target, admin.id, now, text)
        await _close_reports(db, admin.id, now, "handled", text, *same_target)
        if report.target_type == "post":
            # 帖子没了，它下面评论的举报也一起了结
            await _close_reports(db, admin.id, now, "handled", text, JuryHallReport.post_id == report.post_id)
    else:
        if target is not None and target.state == "hidden":
            target.state = "visible"
        await _close_reports(db, admin.id, now, "dismissed", text, *same_target)
    after = target.state if target is not None else None
    _audit(
        db,
        admin.id,
        f"report.{action}",
        "jury_hall_report",
        report.id,
        {"target_type": report.target_type, "target_id": report.target_id, "post_id": report.post_id, "from": before, "to": after, "note": text},
    )
    await db.commit()
    await db.refresh(report)
    if post is not None and after != before:
        await _notify(db, post, "closed" if report.target_type == "post" and after == "deleted" else "updated")
    await _broadcast_admin(report.post_id, f"report_{action}")
    return (await admin_report_reads(db, [report]))[0]
