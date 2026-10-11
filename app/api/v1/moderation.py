from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import get_appeal_user
from app.models.enums import UserStatus
from app.models.user import User
from app.schemas.penalty import (
    AppealCreate,
    BanState,
    ModerationRules,
    ModerationStanding,
    RuleCategory,
    RuleLevel,
    RuleTier,
    ViolationRead,
)
from app.services import penalties as penalty_service
from app.websocket.manager import admin_manager


router = APIRouter()


@router.get("/rules", response_model=ModerationRules)
async def moderation_rules() -> ModerationRules:
    """处罚规则本身是公开的：类别、等级记分和累计阶梯，客户端据此渲染，不要写死。"""
    return ModerationRules(
        point_window_days=penalty_service.POINT_WINDOW_DAYS,
        categories=[
            RuleCategory(key=key, label=label, level=level)
            for key, (label, level) in penalty_service.CATEGORIES.items()
            if level > 0
        ],
        levels=[
            RuleLevel(level=level, points=penalty_service.LEVEL_POINTS[level], penalty=penalty_service.BASE_PENALTY[level].encode())
            for level in (1, 2, 3, 4)
        ],
        ladder=[RuleTier(threshold=threshold, penalty=tier.encode()) for threshold, tier in penalty_service.LADDER],
    )


@router.get("/me", response_model=ModerationStanding)
async def my_moderation_standing(
    current_user: Annotated[User, Depends(get_appeal_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ModerationStanding:
    standing = await penalty_service.user_standing(db, current_user)
    violations = await penalty_service.list_user_violations(db, current_user.id)
    ban = None
    if current_user.status == UserStatus.DISABLED:
        banned_until = penalty_service.aware(current_user.banned_until)
        ban = BanState(reason=current_user.ban_reason, banned_until=banned_until, permanent=banned_until is None)
    return ModerationStanding(
        **standing,
        ban=ban,
        violations=[ViolationRead(**penalty_service.violation_payload(item)) for item in violations],
    )


@router.post("/violations/{violation_id}/appeal", response_model=ViolationRead)
async def appeal_violation(
    violation_id: int,
    payload: AppealCreate,
    current_user: Annotated[User, Depends(get_appeal_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ViolationRead:
    violation = await penalty_service.submit_appeal(db, current_user.id, violation_id, payload.text)
    await admin_manager.broadcast(
        "admin.violation.changed",
        {"violation_id": violation.id, "user_id": violation.user_id, "action": "appealed"},
    )
    return ViolationRead(**penalty_service.violation_payload(violation))
