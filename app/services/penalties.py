"""统一处罚引擎。

自动审核、举报、风纪委员、管理员手动处置，全部只通过 record_violation 进来：
它负责记分、算阶梯、落地禁言 / 封禁。处罚的轻重由「违规多严重 + 累计了多少」
决定，和是谁判的无关 —— 来源只影响上限（机器和风纪委员不能单独给出超过 7 天的封禁）。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import PlatformRole, UserStatus
from app.models.penalty import UserViolation
from app.models.report import AdminAuditLog
from app.models.user import AuthSession, User

logger = logging.getLogger(__name__)
UTC = timezone.utc

# 记分在 90 天滚动窗口内累计，过期自动不计。
POINT_WINDOW_DAYS = 90
LEVEL_POINTS: dict[int, int] = {0: 0, 1: 1, 2: 3, 3: 6, 4: 12}

# 类别 -> (展示名, 默认等级)。等级 0 表示只处理内容、不记分。
CATEGORIES: dict[str, tuple[str, int]] = {
    "profanity": ("脏话", 1),
    "flooding": ("刷屏", 1),
    "suggestive": ("擦边", 1),
    "harassment": ("辱骂骚扰", 2),
    "vulgar": ("低俗", 2),
    "advertising": ("垃圾广告", 2),
    "political": ("敏感话题", 2),
    "sexual": ("色情", 3),
    "fraud": ("诈骗", 3),
    "gambling": ("赌博", 3),
    "drugs": ("涉毒", 3),
    "privacy": ("泄露隐私", 3),
    "threat": ("暴力威胁", 3),
    "hate": ("仇恨攻击", 3),
    "illegal": ("违法违规", 3),
    "minor_sexual": ("涉未成年人色情", 4),
    "unverified_media": ("无法审核的外链内容", 0),
    "admin_action": ("管理员处置", 0),
    "other": ("其他违规", 1),
}

SOURCES = {"auto_rule", "auto_ai", "auto_image", "report", "jury", "admin"}
# 这些来源给出的封禁不能超过 CAPPED_BAN_HOURS；阶梯算出更重时进管理员复核。
CAPPED_SOURCES = {"auto_rule", "auto_ai", "auto_image", "jury"}
CAPPED_BAN_HOURS = 24 * 7

_KIND_RANK = {"none": 0, "notice": 1, "warning": 2, "mute": 3, "ban": 4}


@dataclass(frozen=True)
class Penalty:
    """hours 为 None 表示永久，只对 ban 有意义；notice / warning / none 的 hours 恒为 0。"""

    kind: str
    hours: int | None = 0

    @property
    def permanent(self) -> bool:
        return self.kind == "ban" and self.hours is None

    def _order(self) -> tuple[int, float]:
        return (_KIND_RANK[self.kind], float("inf") if self.hours is None else float(self.hours))

    def heavier_than(self, other: Penalty) -> bool:
        return self._order() > other._order()

    def encode(self) -> str:
        if self.kind in {"none", "notice", "warning"}:
            return self.kind
        return f"{self.kind}:{'permanent' if self.hours is None else self.hours}"

    @staticmethod
    def decode(value: str) -> Penalty:
        kind, _, amount = value.partition(":")
        if kind not in _KIND_RANK:
            raise ValueError(f"Unknown penalty kind: {value}")
        if kind in {"none", "notice", "warning"}:
            return Penalty(kind, 0)
        if amount == "permanent":
            if kind != "ban":
                raise ValueError("Only bans can be permanent")
            return Penalty(kind, None)
        return Penalty(kind, int(amount))


# 单次违规的基础处置。
BASE_PENALTY: dict[int, Penalty] = {
    0: Penalty("none"),
    1: Penalty("notice"),
    2: Penalty("warning"),
    3: Penalty("mute", 24),
    4: Penalty("ban", 24 * 7),
}

# 累计分数跨过门槛时触发的处置，按门槛升序。
LADDER: tuple[tuple[int, Penalty], ...] = (
    (6, Penalty("mute", 24)),
    (9, Penalty("mute", 72)),
    (12, Penalty("ban", 24 * 7)),
    (18, Penalty("ban", 24 * 30)),
    (24, Penalty("ban", None)),
)


@dataclass
class ViolationOutcome:
    violation: UserViolation
    penalty: Penalty
    user_id: int
    banned: bool = False
    muted: bool = False


def category_label(category: str) -> str:
    return CATEGORIES.get(category, (category, 1))[0]


def default_level(category: str) -> int:
    return CATEGORIES.get(category, (category, 1))[1]


def aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def compute_penalty(level: int, previous_total: int, new_total: int) -> Penalty:
    """本次违规的处置 = 基础处置与本次跨过的最高阶梯里更重的那个。

    只在「跨过」门槛时触发阶梯：否则一个已经 12 分的人每说一句脏话都会再封 7 天。
    """
    penalty = BASE_PENALTY.get(level, BASE_PENALTY[1])
    for threshold, tier in LADDER:
        if previous_total < threshold <= new_total and tier.heavier_than(penalty):
            penalty = tier
    return penalty


def cap_penalty(penalty: Penalty, source: str, level: int) -> tuple[Penalty, Penalty | None]:
    """返回 (实际执行的处置, 待管理员确认的原处置)。不需要复核时第二项为 None。"""
    if source not in CAPPED_SOURCES:
        return penalty, None
    if penalty.kind == "ban" and (penalty.hours is None or penalty.hours > CAPPED_BAN_HOURS):
        return Penalty("ban", CAPPED_BAN_HOURS), penalty
    # 红线内容即使阶梯只算到 7 天，也要有人看一眼决定是否永封。
    if level >= 4 and penalty.kind == "ban":
        return penalty, Penalty("ban", None)
    return penalty, None


async def active_points(db: AsyncSession, user_id: int, now: datetime | None = None) -> int:
    now = now or datetime.now(UTC)
    result = await db.execute(
        select(func.coalesce(func.sum(UserViolation.points), 0)).where(
            UserViolation.user_id == user_id,
            UserViolation.status == "active",
            UserViolation.expires_at > now,
        )
    )
    return int(result.scalar_one() or 0)


def _penalty_reason(category: str, level: int, total: int, summary: str | None) -> str:
    text = f"{category_label(category)}"
    if level > 0:
        text += f"（L{level}，90 天内累计 {total} 分）"
    if summary:
        text += f"：{summary[:120]}"
    return text


def _apply_mute(user: User, hours: int, reason: str, now: datetime) -> datetime:
    ends_at = now + timedelta(hours=hours)
    current = aware(user.muted_until)
    if current is not None and current > ends_at:
        ends_at = current
    user.muted_until = ends_at
    user.mute_reason = reason
    return ends_at


async def _apply_ban(db: AsyncSession, user: User, hours: int | None, reason: str, now: datetime) -> datetime | None:
    """落地封禁，返回生效后的到期时间（None = 永久）。已有更重的封禁时不缩短。"""
    ends_at = None if hours is None else now + timedelta(hours=hours)
    if user.status == UserStatus.DISABLED:
        current = aware(user.banned_until)
        if current is None:
            ends_at = None
        elif ends_at is not None and current > ends_at:
            ends_at = current
    user.status = UserStatus.DISABLED
    user.banned_until = ends_at
    user.banned_at = now
    user.ban_reason = reason
    await db.execute(
        update(AuthSession)
        .where(AuthSession.user_id == user.id, AuthSession.revoked_at.is_(None))
        .values(revoked_at=now)
    )
    return ends_at


async def _existing_for_target(db: AsyncSession, user_id: int, target_type: str | None, target_id: int | None) -> UserViolation | None:
    if target_type is None or target_id is None:
        return None
    result = await db.execute(
        select(UserViolation)
        .where(
            UserViolation.user_id == user_id,
            UserViolation.target_type == target_type,
            UserViolation.target_id == target_id,
            UserViolation.status == "active",
        )
        .order_by(UserViolation.id.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def record_violation(
    db: AsyncSession,
    *,
    user_id: int,
    category: str,
    level: int | None = None,
    source: str,
    target_type: str | None = None,
    target_id: int | None = None,
    conversation_id: int | None = None,
    moderation_record_id: int | None = None,
    report_id: int | None = None,
    case_id: int | None = None,
    actor_id: int | None = None,
    confidence: float | None = None,
    summary: str | None = None,
    reason: str | None = None,
    override: Penalty | None = None,
) -> ViolationOutcome | None:
    """记一次违规并落地处罚。不提交事务，由调用方 commit 后再 flush_notifications。

    同一条内容只罚一次：已有生效记录时直接返回 None；只有人工来源给出更高等级时，
    才撤销旧记录按新等级重算。
    """
    if source not in SOURCES:
        raise ValueError(f"Unknown violation source: {source}")
    user = await db.get(User, user_id)
    if user is None:
        return None
    level = default_level(category) if level is None else max(0, min(4, int(level)))
    now = datetime.now(UTC)

    existing = await _existing_for_target(db, user_id, target_type, target_id)
    if existing is not None:
        human = source not in CAPPED_SOURCES or source == "jury"
        if not (human and level > existing.level):
            return None
        existing.status = "revoked"
        existing.revoked_at = now
        existing.revoked_by_id = actor_id
        existing.revoke_reason = "superseded"
        await db.flush()

    points = LEVEL_POINTS.get(level, 0)
    previous_total = await active_points(db, user_id, now)
    new_total = previous_total + points

    recommended: Penalty | None = None
    if override is not None:
        penalty = override
    else:
        penalty = compute_penalty(level, previous_total, new_total)
        penalty, recommended = cap_penalty(penalty, source, level)
    # 平台管理员不被禁言 / 封禁，最多记一次警告，避免后台被自己锁在外面。
    if user.platform_role == PlatformRole.ADMIN and penalty.kind in {"mute", "ban"}:
        penalty, recommended = Penalty("warning"), None

    text_reason = _penalty_reason(category, level, new_total, summary)
    ends_at: datetime | None = None
    banned = muted = False
    if penalty.kind == "mute":
        ends_at = _apply_mute(user, int(penalty.hours or 0), text_reason, now)
        muted = True
    elif penalty.kind == "ban":
        ends_at = await _apply_ban(db, user, penalty.hours, text_reason, now)
        banned = True
        # 设置打开时把这个账号的设备令牌、机器码一起封（docs/device-bans.md 第 5 节，默认关）
        from app.services import device_bans

        await device_bans.propagate_account_ban(db, user)

    violation = UserViolation(
        user_id=user_id,
        category=category,
        level=level,
        points=points,
        source=source,
        confidence=confidence,
        target_type=target_type,
        target_id=target_id,
        conversation_id=conversation_id,
        moderation_record_id=moderation_record_id,
        report_id=report_id,
        case_id=case_id,
        actor_id=actor_id,
        summary=summary[:500] if summary else None,
        reason=reason[:1000] if reason else None,
        created_at=now,
        expires_at=now + timedelta(days=POINT_WINDOW_DAYS),
        penalty_kind=penalty.kind,
        penalty_ends_at=ends_at,
        penalty_permanent=penalty.kind == "ban" and ends_at is None,
        points_total_after=new_total,
        review_status="pending" if recommended is not None else "none",
        recommended_penalty=recommended.encode() if recommended is not None else None,
    )
    db.add(violation)
    await db.flush()
    db.add(
        AdminAuditLog(
            actor_id=actor_id,
            action="violation.recorded",
            target_type="user",
            target_id=user_id,
            details=_audit_json(
                {
                    "violation_id": violation.id,
                    "category": category,
                    "level": level,
                    "points": points,
                    "total": new_total,
                    "source": source,
                    "penalty": penalty.encode(),
                    "recommended": violation.recommended_penalty,
                    "target_type": target_type,
                    "target_id": target_id,
                }
            ),
        )
    )
    outcome = ViolationOutcome(violation=violation, penalty=penalty, user_id=user_id, banned=banned, muted=muted)
    _pending(db).append(outcome)
    return outcome


def _audit_json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _pending(db: AsyncSession) -> list[ViolationOutcome]:
    return db.info.setdefault("penalty_outcomes", [])


def violation_payload(violation: UserViolation) -> dict[str, Any]:
    """给当事人看的处罚说明。不含举报人、判定人等信息。"""
    ends_at = aware(violation.penalty_ends_at)
    expires_at = aware(violation.expires_at)
    created_at = aware(violation.created_at)
    return {
        "id": violation.id,
        "category": violation.category,
        "category_label": category_label(violation.category),
        "level": violation.level,
        "points": violation.points,
        "points_total": violation.points_total_after,
        "source": "auto" if violation.source.startswith("auto_") else violation.source,
        "target_type": violation.target_type,
        "target_id": violation.target_id,
        "conversation_id": violation.conversation_id,
        "summary": violation.summary,
        "status": violation.status,
        "penalty_kind": violation.penalty_kind,
        "penalty_ends_at": ends_at.isoformat() if ends_at else None,
        "penalty_permanent": violation.penalty_permanent,
        "under_review": violation.review_status == "pending",
        "appeal_status": violation.appeal_status,
        "can_appeal": violation.status == "active" and violation.appeal_status == "none",
        # 申诉现在在谁手里（senior 资深复审中 / admin 等管理员），只在待处理时有值；结果和结果是谁给的
        "appeal_stage": (violation.appeal_stage or "admin") if violation.appeal_status == "pending" else None,
        "appeal_result": violation.appeal_result,
        "appeal_decided_by": violation.appeal_decided_by,
        "created_at": created_at.isoformat() if created_at else None,
        "expires_at": expires_at.isoformat() if expires_at else None,
    }


def discard_notifications(db: AsyncSession) -> None:
    """事务回滚后调用：这些处罚没有落库，不能再推出去。"""
    db.info.pop("penalty_outcomes", None)


_TARGET_LABELS = {
    "message": "消息",
    "post": "动态",
    "post_comment": "评论",
    "post_topic": "话题",
    "teamup_profile": "组队名片",
    "teamup_comment": "组队留言",
    "profile_cover": "主页封面",
}


def _duration_text(hours: float) -> str:
    if hours >= 24:
        days = hours / 24
        return f"{int(days)} 天" if days == int(days) else f"{days:.1f} 天"
    return f"{max(1, int(round(hours)))} 小时"


def describe_violation(violation: UserViolation) -> tuple[str, str]:
    """给通知中心用的 (标题, 正文)。"""
    label = category_label(violation.category)
    target = _TARGET_LABELS.get(violation.target_type or "")
    title = f"你的{target}因「{label}」被处理" if target else f"你因「{label}」被处罚"
    parts: list[str] = []
    if violation.level > 0:
        parts.append(f"违规等级 L{violation.level}，记 {violation.points} 分，{POINT_WINDOW_DAYS} 天内累计 {violation.points_total_after} 分。")
    created_at, ends_at = aware(violation.created_at), aware(violation.penalty_ends_at)
    if violation.penalty_kind == "warning":
        parts.append("本次给予警告，再次违规将被禁言或封禁。")
    elif violation.penalty_kind == "mute" and created_at and ends_at:
        parts.append(f"禁言 {_duration_text((ends_at - created_at).total_seconds() / 3600)}。")
    elif violation.penalty_kind == "ban":
        if violation.penalty_permanent:
            parts.append("账号已被永久封禁。")
        elif created_at and ends_at:
            parts.append(f"账号封禁 {_duration_text((ends_at - created_at).total_seconds() / 3600)}。")
    if violation.status == "revoked":
        parts.append("该处罚已撤销。")
    elif violation.appeal_status == "none":
        parts.append("如有异议可以申诉。")
    elif violation.appeal_status == "pending":
        parts.append("申诉处理中。")
    elif violation.appeal_status == "rejected":
        parts.append("申诉未通过。")
    elif violation.appeal_result == "reduced":
        parts.append("申诉后处罚已减轻：禁言 / 封禁已解除，违规记录保留。")
    return title, "".join(parts)


async def flush_notifications(db: AsyncSession) -> None:
    """事务提交后调用：把本次会话里产生的处罚推给当事人和后台。"""
    outcomes: list[ViolationOutcome] = db.info.pop("penalty_outcomes", [])
    if not outcomes:
        return
    from app.websocket.manager import admin_manager, manager

    for outcome in outcomes:
        try:
            payload = violation_payload(outcome.violation)
            if outcome.penalty.kind != "none":
                await manager.send_to_user(outcome.user_id, "moderation.penalty", payload)
            if outcome.banned:
                user = await db.get(User, outcome.user_id)
                if user is not None and user.status == UserStatus.DISABLED:
                    banned_until = aware(user.banned_until)
                    await manager.disconnect_user(
                        user.id,
                        "account.suspended",
                        {
                            "code": "account_suspended",
                            "reason": user.ban_reason,
                            "banned_until": banned_until.isoformat() if banned_until else None,
                            "permanent": banned_until is None,
                            "violation_id": outcome.violation.id,
                        },
                    )
            await admin_manager.broadcast(
                "admin.violation.changed",
                {"violation_id": outcome.violation.id, "user_id": outcome.user_id, "action": "created"},
            )
        except Exception:
            logger.exception("Failed to deliver penalty notification", extra={"violation_id": outcome.violation.id})


# ---------------------------------------------------------------- 禁言


def mute_state(user: User, now: datetime | None = None) -> datetime | None:
    """仍在禁言中则返回到期时间，否则 None。"""
    until = aware(user.muted_until)
    if until is None:
        return None
    return until if until > (now or datetime.now(UTC)) else None


def _local_time_text(value: datetime) -> str:
    # 旧客户端只会把 message 原样弹出来，所以这里直接写成用户能读的北京时间。
    local = value.astimezone(timezone(timedelta(hours=8)))
    return f"{local.month}月{local.day}日 {local:%H:%M}"


def ensure_can_post(user: User) -> None:
    until = mute_state(user)
    if until is None:
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={
            "code": "account_muted",
            "message": f"你已被禁言至 {_local_time_text(until)}，暂时不能发布内容",
            "reason": user.mute_reason,
            "muted_until": until.isoformat(),
        },
    )


# ---------------------------------------------------------------- 撤销 / 复核 / 申诉


async def _get_violation(db: AsyncSession, violation_id: int) -> UserViolation:
    violation = await db.get(UserViolation, violation_id)
    if violation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Violation not found")
    return violation


async def _resync_restrictions(db: AsyncSession, user: User, changed: UserViolation, now: datetime) -> None:
    """一条记录被撤销或改判后，按台账里剩下的生效记录重算禁言 / 封禁。"""
    result = await db.execute(
        select(UserViolation).where(
            UserViolation.user_id == user.id,
            UserViolation.status == "active",
            UserViolation.penalty_kind.in_(("mute", "ban")),
        )
    )
    rows = list(result.scalars().all())

    mute_ends = [aware(row.penalty_ends_at) for row in rows if row.penalty_kind == "mute"]
    mute_ends = [value for value in mute_ends if value is not None and value > now]
    if mute_ends:
        user.muted_until = max(mute_ends)
    else:
        user.muted_until = None
        user.mute_reason = None

    bans = [row for row in rows if row.penalty_kind == "ban" and (row.penalty_permanent or (aware(row.penalty_ends_at) or now) > now)]
    if bans:
        permanent = any(row.penalty_permanent for row in bans)
        user.status = UserStatus.DISABLED
        user.banned_until = None if permanent else max(aware(row.penalty_ends_at) for row in bans)  # type: ignore[type-var]
        return
    # 台账里已经没有生效的封禁。只有当前封禁确实来自这条记录时才解封 ——
    # 台账上线前的旧封禁没有对应记录，不能被顺手放出来。
    banned_at = aware(user.banned_at)
    created_at = aware(changed.created_at)
    if (
        user.status == UserStatus.DISABLED
        and banned_at is not None
        and created_at is not None
        and banned_at >= created_at - timedelta(seconds=5)
    ):
        user.status = UserStatus.ACTIVE
        user.banned_until = None
        user.banned_at = None
        user.ban_reason = None


async def revoke_violation(
    db: AsyncSession, violation_id: int, *, actor_id: int | None, reason: str | None, sync_case: bool = True
) -> UserViolation:
    """撤销一条处罚并回滚它触发的禁言 / 封禁。不提交事务。

    处罚来自举报案件时，案件会跟着改判成不违规（sync_case=False 是案件改判自己撤销处罚时用的，避免来回调用）。
    """
    violation = await _get_violation(db, violation_id)
    if violation.status != "active":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Violation is already revoked")
    now = datetime.now(UTC)
    violation.status = "revoked"
    violation.revoked_at = now
    violation.revoked_by_id = actor_id
    violation.revoke_reason = reason.strip()[:1000] if reason else None
    if violation.review_status == "pending":
        violation.review_status = "adjusted"
        violation.reviewed_by_id = actor_id
        violation.reviewed_at = now
    await db.flush()
    user = await db.get(User, violation.user_id)
    if user is not None:
        await _resync_restrictions(db, user, violation, now)
    db.add(
        AdminAuditLog(
            actor_id=actor_id,
            action="violation.revoked",
            target_type="user",
            target_id=violation.user_id,
            details=_audit_json({"violation_id": violation.id, "reason": violation.revoke_reason}),
        )
    )
    if violation.appeal_status == "pending":
        # 申诉还没有结果（可能正在资深复审里）：处罚都没了，复审到此为止。
        from app.services import jury_reviews

        await jury_reviews.on_violation_revoked(db, violation, now)
    if sync_case and violation.case_id is not None:
        # 依赖方向是 cases -> penalties，这里只能在函数里引。
        from app.services import cases as case_service

        await case_service.sync_case_after_revocation(db, violation, actor_id=actor_id)
    return violation


async def adjust_penalty(db: AsyncSession, violation_id: int, *, actor_id: int, penalty: Penalty | None, note: str | None = None) -> UserViolation:
    """管理员复核被上限压住的处罚。penalty 为 None 表示维持现状，否则改成指定处置。"""
    violation = await _get_violation(db, violation_id)
    if violation.status != "active":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Violation is revoked")
    user = await db.get(User, violation.user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    now = datetime.now(UTC)
    if penalty is None:
        violation.review_status = "confirmed"
    else:
        if user.platform_role == PlatformRole.ADMIN and penalty.kind in {"mute", "ban"}:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cannot mute or ban an admin")
        text_reason = _penalty_reason(violation.category, violation.level, violation.points_total_after, violation.summary)
        violation.penalty_kind = penalty.kind
        violation.penalty_ends_at = None
        violation.penalty_permanent = False
        await db.flush()
        # 先按台账里其它记录重算，再叠加这次改判，避免改轻时旧的禁言 / 封禁残留。
        await _resync_restrictions(db, user, violation, now)
        if penalty.kind == "mute":
            violation.penalty_ends_at = _apply_mute(user, int(penalty.hours or 0), text_reason, now)
        elif penalty.kind == "ban":
            violation.penalty_ends_at = await _apply_ban(db, user, penalty.hours, text_reason, now)
            violation.penalty_permanent = violation.penalty_ends_at is None
        violation.review_status = "adjusted"
    violation.reviewed_by_id = actor_id
    violation.reviewed_at = now
    db.add(
        AdminAuditLog(
            actor_id=actor_id,
            action="violation.reviewed",
            target_type="user",
            target_id=violation.user_id,
            details=_audit_json(
                {
                    "violation_id": violation.id,
                    "review_status": violation.review_status,
                    "penalty": penalty.encode() if penalty else None,
                    "note": note,
                }
            ),
        )
    )
    return violation


async def lift_ban(db: AsyncSession, user: User, *, actor_id: int | None) -> None:
    """管理员直接解封：记分保留，只结束台账里仍生效的封禁。"""
    now = datetime.now(UTC)
    result = await db.execute(
        select(UserViolation).where(
            UserViolation.user_id == user.id,
            UserViolation.status == "active",
            UserViolation.penalty_kind == "ban",
        )
    )
    for row in result.scalars().all():
        ends_at = aware(row.penalty_ends_at)
        if row.penalty_permanent or (ends_at is not None and ends_at > now):
            row.penalty_ends_at = now
            row.penalty_permanent = False
            if row.review_status == "pending":
                row.review_status = "adjusted"
                row.reviewed_by_id = actor_id
                row.reviewed_at = now
    user.status = UserStatus.ACTIVE
    user.banned_until = None
    user.banned_at = None
    user.ban_reason = None
    # 当初随账号封禁自动带上的设备封禁一起解除
    from app.services import device_bans

    await device_bans.lift_propagated(db, user.id, actor_id)


async def lift_mute(db: AsyncSession, user: User) -> None:
    now = datetime.now(UTC)
    result = await db.execute(
        select(UserViolation).where(
            UserViolation.user_id == user.id,
            UserViolation.status == "active",
            UserViolation.penalty_kind == "mute",
        )
    )
    for row in result.scalars().all():
        ends_at = aware(row.penalty_ends_at)
        if ends_at is not None and ends_at > now:
            row.penalty_ends_at = now
    user.muted_until = None
    user.mute_reason = None


async def submit_appeal(db: AsyncSession, user_id: int, violation_id: int, text: str) -> UserViolation:
    violation = await _get_violation(db, violation_id)
    if violation.user_id != user_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Violation not found")
    if violation.status != "active":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="该处罚已撤销，无需申诉")
    if violation.appeal_status != "none":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="每条处罚只能申诉一次")
    content = text.strip()
    if len(content) < 5:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="请写明申诉理由（至少 5 个字）")
    now = datetime.now(UTC)
    violation.appeal_status = "pending"
    violation.appeal_text = content[:2000]
    violation.appealed_at = now
    # 默认到管理员（和三期之前一样）；开关 appeals 打开且条件都满足时先进资深复审，见 jury_reviews.appeal_block_reason。
    violation.appeal_stage = "admin"
    violation.appeal_result = None
    violation.appeal_decided_by = None
    from app.services import jury_reviews

    try:
        review = await jury_reviews.open_appeal_review(db, violation, now)
    except Exception:
        # 资深复审这一步出任何问题都不能让申诉交不上去：照旧到管理员。
        logger.exception("Failed to route appeal to senior review", extra={"violation_id": violation.id})
        violation.appeal_stage = "admin"
        review = None
    await db.commit()
    if review is not None:
        from app.services import cases as case_service

        await case_service.notify_jury_queue_changed(db)
    return violation


async def _restore_penalty(db: AsyncSession, violation: UserViolation, before: dict[str, Any] | None, now: datetime) -> None:
    """管理员推翻了资深复审的「减轻」：把这条处罚的禁言 / 封禁恢复成原来的样子。原本就已经到期的只改回记录。"""
    if before is None or before["kind"] not in {"mute", "ban"}:
        return
    kind, ends_at, permanent = before["kind"], aware(before["ends_at"]), bool(before["permanent"])
    violation.penalty_kind = kind
    violation.penalty_ends_at = ends_at
    violation.penalty_permanent = permanent
    user = await db.get(User, violation.user_id)
    if user is None or (not permanent and (ends_at is None or ends_at <= now)):
        return
    reason = _penalty_reason(violation.category, violation.level, violation.points_total_after, violation.summary)
    if kind == "mute":
        current = aware(user.muted_until)
        user.muted_until = max(current, ends_at) if current is not None else ends_at
        user.mute_reason = reason
        return
    if user.status == UserStatus.DISABLED:
        # 期间又被别的处罚封了：不缩短已有的封禁
        current = aware(user.banned_until)
        if current is None:
            permanent = True
        elif not permanent and current > ends_at:
            ends_at = current
    user.status = UserStatus.DISABLED
    user.banned_until = None if permanent else ends_at
    user.banned_at = now
    user.ban_reason = reason
    await db.execute(update(AuthSession).where(AuthSession.user_id == user.id, AuthSession.revoked_at.is_(None)).values(revoked_at=now))


async def review_appeal(db: AsyncSession, violation_id: int, *, reviewer_id: int, accept: bool, note: str | None) -> UserViolation:
    """管理员处理申诉。待处理的申诉（无论在资深复审里还是已经到管理员），以及资深复审已经给了结果的，都可以处理：
    管理员的结论覆盖复审的结果，进行中的复审就此关闭。"""
    violation = await _get_violation(db, violation_id)
    overriding = violation.appeal_status != "pending" and violation.appeal_decided_by == "senior"
    if violation.appeal_status != "pending" and not overriding:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No pending appeal for this violation")
    if overriding and not accept and violation.status != "active":
        # 复审已经把处罚撤销了（案件也跟着改判了）：没法原样恢复，要重新处罚得另记一条或改判案件。
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "appeal_already_revoked", "message": "资深复审已经撤销了这条处罚，不能再驳回申诉；需要的话请重新记一条处罚或改判案件"},
        )
    now = datetime.now(UTC)
    from app.services import jury_reviews

    review = await jury_reviews.on_admin_appeal_decision(db, violation, accept=accept, now=now)
    previous = violation.appeal_result if overriding else None
    violation.appeal_status = "accepted" if accept else "rejected"
    violation.appeal_reviewer_id = reviewer_id
    violation.appeal_note = note.strip()[:1000] if note else None
    violation.appeal_reviewed_at = now
    violation.appeal_stage = None
    violation.appeal_result = "revoked" if accept else "upheld"
    violation.appeal_decided_by = "admin"
    if accept and violation.status == "active":
        await revoke_violation(db, violation.id, actor_id=reviewer_id, reason=f"申诉通过：{violation.appeal_note or ''}".rstrip("："))
    elif not accept and previous == "reduced" and review is not None:
        await _restore_penalty(db, violation, jury_reviews._parse_penalty_before(review.penalty_before), now)
    details: dict[str, Any] = {"violation_id": violation.id, "accepted": accept, "note": violation.appeal_note}
    if review is not None:
        # 有过资深复审：记下管理员覆盖的是什么（进行中的为 null）
        details["senior_review"] = {"review_id": review.id, "result": previous}
    db.add(
        AdminAuditLog(
            actor_id=reviewer_id,
            action="violation.appeal.reviewed",
            target_type="user",
            target_id=violation.user_id,
            details=_audit_json(details),
        )
    )
    await db.commit()
    if review is not None:
        from app.services import cases as case_service

        await case_service.notify_jury_queue_changed(db)
    return violation


# ---------------------------------------------------------------- 查询


async def list_user_violations(db: AsyncSession, user_id: int, limit: int = 50) -> list[UserViolation]:
    result = await db.execute(
        select(UserViolation)
        .where(UserViolation.user_id == user_id)
        .order_by(UserViolation.id.desc())
        .limit(limit)
    )
    return list(result.scalars().all())


async def user_standing(db: AsyncSession, user: User) -> dict[str, Any]:
    now = datetime.now(UTC)
    points = await active_points(db, user.id, now)
    muted_until = mute_state(user, now)
    next_threshold = next(((threshold, tier) for threshold, tier in LADDER if threshold > points), None)
    return {
        "points": points,
        "point_window_days": POINT_WINDOW_DAYS,
        "muted_until": muted_until.isoformat() if muted_until else None,
        "mute_reason": user.mute_reason if muted_until else None,
        "next_threshold": next_threshold[0] if next_threshold else None,
        "next_penalty": next_threshold[1].encode() if next_threshold else None,
    }


async def list_violations(
    db: AsyncSession,
    *,
    user_id: int | None = None,
    status_filter: str | None = None,
    appeal_status: str | None = None,
    review_status: str | None = None,
    source: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[UserViolation], int]:
    clauses = []
    if user_id is not None:
        clauses.append(UserViolation.user_id == user_id)
    if status_filter:
        clauses.append(UserViolation.status == status_filter)
    if appeal_status:
        clauses.append(UserViolation.appeal_status == appeal_status)
    if review_status:
        clauses.append(UserViolation.review_status == review_status)
    if source:
        clauses.append(UserViolation.source.like("auto_%") if source == "auto" else UserViolation.source == source)
    total = int((await db.execute(select(func.count(UserViolation.id)).where(*clauses))).scalar_one() or 0)
    result = await db.execute(
        select(UserViolation)
        .where(*clauses)
        .order_by(UserViolation.id.desc())
        .offset(offset)
        .limit(limit)
    )
    return list(result.scalars().all()), total


async def build_admin_reads(db: AsyncSession, violations: list[UserViolation]) -> list[dict[str, Any]]:
    """后台视图：在当事人可见字段之上补判定依据、复核与申诉详情。"""
    user_ids = {item.user_id for item in violations} | {item.actor_id for item in violations if item.actor_id is not None}
    users: dict[int, User] = {}
    if user_ids:
        result = await db.execute(select(User).where(User.id.in_(user_ids)))
        users = {user.id: user for user in result.scalars().all()}
    items: list[dict[str, Any]] = []
    for violation in violations:
        items.append(
            {
                **violation_payload(violation),
                "user_id": violation.user_id,
                "user": users.get(violation.user_id),
                "raw_source": violation.source,
                "confidence": violation.confidence,
                "reason": violation.reason,
                "moderation_record_id": violation.moderation_record_id,
                "report_id": violation.report_id,
                "case_id": violation.case_id,
                "actor": users.get(violation.actor_id) if violation.actor_id is not None else None,
                "review_status": violation.review_status,
                "recommended_penalty": violation.recommended_penalty,
                "reviewed_at": aware(violation.reviewed_at),
                "appeal_text": violation.appeal_text,
                "appealed_at": aware(violation.appealed_at),
                "appeal_note": violation.appeal_note,
                "appeal_reviewed_at": aware(violation.appeal_reviewed_at),
                "revoked_at": aware(violation.revoked_at),
                "revoke_reason": violation.revoke_reason,
            }
        )
    return items


async def notify_violation_updated(db: AsyncSession, violation: UserViolation, action: str) -> None:
    """撤销 / 改判 / 申诉有结果后，让当事人和后台刷新。"""
    from app.websocket.manager import admin_manager, manager

    try:
        user = await db.get(User, violation.user_id)
        standing = await user_standing(db, user) if user is not None else {}
        await manager.send_to_user(
            violation.user_id,
            "moderation.violation.updated",
            {"action": action, "violation": violation_payload(violation), "standing": standing},
        )
        await admin_manager.broadcast(
            "admin.violation.changed",
            {"violation_id": violation.id, "user_id": violation.user_id, "action": action},
        )
    except Exception:
        logger.exception("Failed to deliver violation update", extra={"violation_id": violation.id})
    if db.info.get("overturned_case_ids"):
        # 撤销处罚连带改判了案件：举报人和投过票的委员也要知道。
        from app.services import cases as case_service

        await case_service.flush_overturn_notifications(db)
