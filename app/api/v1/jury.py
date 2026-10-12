from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.models.user import User
from app.schemas.case import (
    JuryApply,
    JuryCaseDetail,
    JuryCaseView,
    JuryHistoryItem,
    JuryMeSettings,
    JuryNextCase,
    JuryStatus,
    JuryVoteCreate,
    JuryVoteResult,
)
from app.schemas.jury import (
    ExamResult,
    ExamStart,
    ExamState,
    ExamSubmit,
    JuryAchievementPage,
    JuryConfig,
    JuryMyStats,
    JuryOpinion,
    JuryOpinionCreate,
    JuryOpinionLikeResult,
    JuryOpinionPage,
    JuryOpinionReportCreate,
    JuryOpinionState,
    JuryOverview,
    JuryPointPage,
    JuryVoteItem,
    JuryVotePage,
    Leaderboard,
    PracticeAnswerCreate,
    PracticeAnswerResult,
    PracticeNext,
    PublicCase,
    PublicCaseDetail,
    PublicCasePage,
)
from app.services import jury as jury_service
from app.services import jury_exam as exam_service
from app.services import jury_opinions as opinion_service
from app.services import jury_overview as overview_service
from app.services import jury_points as points_service
from app.websocket.manager import admin_manager


router = APIRouter()


@router.get("/config", response_model=JuryConfig)
async def jury_config(
    _current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> JuryConfig:
    return JuryConfig(**await overview_service.config(db))


@router.get("/overview", response_model=JuryOverview)
async def jury_overview(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> JuryOverview:
    return JuryOverview(**await overview_service.overview(db, current_user))


# 固定路径要声明在带参数的路径前面：/me/stats、/cases/next、/public/cases 都是。
@router.get("/me/stats", response_model=JuryMyStats)
async def my_jury_stats(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> JuryMyStats:
    return JuryMyStats(**await jury_service.my_stats(db, current_user.id))


@router.get("/me/points", response_model=JuryPointPage)
async def my_jury_points(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: int = Query(default=20, ge=1, le=50),
    offset: int = Query(default=0, ge=0),
) -> JuryPointPage:
    return JuryPointPage(**await points_service.list_points(db, current_user.id, limit=limit, offset=offset))


@router.get("/me/achievements", response_model=JuryAchievementPage)
async def my_jury_achievements(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> JuryAchievementPage:
    return JuryAchievementPage(items=await points_service.list_achievements(db, current_user.id))


@router.put("/me/settings", response_model=JuryStatus)
async def update_my_jury_settings(
    payload: JuryMeSettings,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> JuryStatus:
    await jury_service.update_settings(db, current_user, badge_visible=payload.badge_visible)
    return JuryStatus(**await jury_service.my_status(db, current_user))


@router.get("/me", response_model=JuryStatus)
async def my_jury_status(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> JuryStatus:
    return JuryStatus(**await jury_service.my_status(db, current_user))


@router.post("/apply", response_model=JuryStatus)
async def apply_for_jury(
    payload: JuryApply,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> JuryStatus:
    await jury_service.apply(db, current_user, payload.statement)
    await admin_manager.broadcast("admin.jury.changed", {"user_id": current_user.id})
    return JuryStatus(**await jury_service.my_status(db, current_user))


@router.get("/leaderboard", response_model=Leaderboard)
async def jury_leaderboard(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    period: Literal["week", "month", "all"] = "week",
    limit: int = Query(default=50, ge=1, le=100),
) -> Leaderboard:
    return Leaderboard(**await points_service.leaderboard(db, current_user.id, period=period, limit=limit))


@router.get("/exam", response_model=ExamState)
async def jury_exam_state(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ExamState:
    return ExamState(**await exam_service.exam_state(db, current_user))


@router.post("/exam/start", response_model=ExamStart)
async def start_jury_exam(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ExamStart:
    return ExamStart(**await exam_service.start_exam(db, current_user))


@router.post("/exam/{attempt_id}/submit", response_model=ExamResult)
async def submit_jury_exam(
    attempt_id: int,
    payload: ExamSubmit,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ExamResult:
    return ExamResult(**await exam_service.submit_exam(db, current_user, attempt_id, payload.answers))


@router.get("/practice/next", response_model=PracticeNext)
async def next_practice_question(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PracticeNext:
    return PracticeNext(**await exam_service.practice_next(db, current_user))


@router.post("/practice/{question_id}/answer", response_model=PracticeAnswerResult)
async def answer_practice_question(
    question_id: int,
    payload: PracticeAnswerCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PracticeAnswerResult:
    return PracticeAnswerResult(**await exam_service.practice_answer(db, current_user, question_id, payload.vote))


@router.get("/public/cases", response_model=PublicCasePage)
async def public_jury_cases(
    _current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: int = Query(default=20, ge=1, le=50),
    offset: int = Query(default=0, ge=0),
    kind: Literal["message", "post", ""] | None = None,
    outcome: Literal["violation", "clean", ""] | None = None,
    category: str | None = Query(default=None, max_length=40),
) -> PublicCasePage:
    items, total = await overview_service.public_cases(
        db, limit=limit, offset=offset, kind=kind or None, outcome=outcome or None, category=category or None
    )
    return PublicCasePage(items=[PublicCase(**item) for item in items], total=total)


@router.get("/public/cases/{case_id}", response_model=PublicCaseDetail)
async def public_jury_case(
    case_id: int,
    _current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PublicCaseDetail:
    return PublicCaseDetail(**await overview_service.public_case(db, case_id))


@router.get("/cases/next", response_model=JuryNextCase)
async def next_jury_case(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> JuryNextCase:
    view = await jury_service.next_case(db, current_user)
    return JuryNextCase(case=JuryCaseView(**view) if view is not None else None)


@router.get("/cases/{case_id}", response_model=JuryCaseDetail)
async def jury_case_detail(
    case_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> JuryCaseDetail:
    return JuryCaseDetail(**await jury_service.case_detail(db, current_user.id, case_id))


@router.post("/cases/{case_id}/vote", response_model=JuryVoteResult)
async def vote_on_case(
    case_id: int,
    payload: JuryVoteCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> JuryVoteResult:
    return JuryVoteResult(**await jury_service.cast_vote(db, current_user, case_id, payload.vote, payload.note, payload.category))


@router.get("/cases/{case_id}/opinions", response_model=JuryOpinionPage)
async def list_case_opinions(
    case_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> JuryOpinionPage:
    return JuryOpinionPage(**await opinion_service.list_opinions(db, current_user, case_id))


@router.post("/cases/{case_id}/opinions", response_model=JuryOpinion)
async def create_case_opinion(
    case_id: int,
    payload: JuryOpinionCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> JuryOpinion:
    return JuryOpinion(**await opinion_service.create_opinion(db, current_user, case_id, payload.content, payload.parent_id))


@router.delete("/opinions/{opinion_id}", response_model=JuryOpinionState)
async def delete_opinion(
    opinion_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> JuryOpinionState:
    return JuryOpinionState(**await opinion_service.delete_opinion(db, current_user, opinion_id))


@router.post("/opinions/{opinion_id}/like", response_model=JuryOpinionLikeResult)
async def like_opinion(
    opinion_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> JuryOpinionLikeResult:
    return JuryOpinionLikeResult(**await opinion_service.set_like(db, current_user, opinion_id, True))


@router.delete("/opinions/{opinion_id}/like", response_model=JuryOpinionLikeResult)
async def unlike_opinion(
    opinion_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> JuryOpinionLikeResult:
    return JuryOpinionLikeResult(**await opinion_service.set_like(db, current_user, opinion_id, False))


@router.post("/opinions/{opinion_id}/report", response_model=JuryOpinionState)
async def report_opinion(
    opinion_id: int,
    payload: JuryOpinionReportCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> JuryOpinionState:
    return JuryOpinionState(**await opinion_service.report_opinion(db, current_user, opinion_id, payload.reason, payload.details))


@router.get("/votes", response_model=JuryVotePage)
async def my_jury_votes(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: int = Query(default=20, ge=1, le=50),
    offset: int = Query(default=0, ge=0),
    state: Literal["open", "closed", ""] | None = None,
    outcome: Literal["violation", "clean", ""] | None = None,
    # 留空表示不筛选，所以不直接声明成 bool
    agreed: Literal["true", "false", ""] | None = None,
    vote: str | None = Query(default=None, max_length=80),
) -> JuryVotePage:
    votes = [item.strip() for item in (vote or "").split(",") if item.strip()]
    if any(item not in jury_service.VOTES for item in votes):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "invalid_vote_filter", "message": "vote 只能是 none / l1 / l2 / l3 / abstain / recuse / need_context，多个用逗号分隔"},
        )
    items, total = await jury_service.list_votes(
        db,
        current_user.id,
        limit=limit,
        offset=offset,
        state=state or None,
        outcome=outcome or None,
        agreed={"true": True, "false": False}.get(agreed or ""),
        votes=votes,
    )
    return JuryVotePage(items=[JuryVoteItem(**item) for item in items], total=total)


@router.get("/history", response_model=list[JuryHistoryItem])
async def my_jury_history(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[JuryHistoryItem]:
    return [JuryHistoryItem(**item) for item in await jury_service.history(db, current_user.id)]
