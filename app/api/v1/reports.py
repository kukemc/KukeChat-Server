from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.models.user import User
from app.schemas.case import ReporterCredit, ReportOptions, ReportReasonRead
from app.schemas.report import ReportCreate, ReportRead, ReportReconsider
from app.services import cases as case_service
from app.services import jury_reviews as review_service
from app.services import reports as report_service
from app.websocket.manager import admin_manager


router = APIRouter()


@router.post("", response_model=ReportRead, status_code=status.HTTP_201_CREATED)
async def create_report(
    payload: ReportCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ReportRead:
    report = await report_service.create_report(db, current_user.id, payload)
    new_case_id = getattr(report, "_new_case_id", None)
    if new_case_id is not None:
        case_service.schedule_triage(new_case_id)
    judge_case_id = getattr(report, "_judge_case_id", None)
    if judge_case_id is not None:
        case_service.schedule_case_judge(judge_case_id)
    await admin_manager.broadcast("admin.report.changed", {"report_id": report.id, "action": "created"})
    return await report_service.build_report_read(db, report, current_user.id)


@router.get("/me", response_model=list[ReportRead])
async def list_my_reports(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[ReportRead]:
    reports = await report_service.list_my_reports(db, current_user.id)
    return await report_service.build_report_reads(db, reports, current_user.id)


@router.post("/{report_id}/reconsider", response_model=ReportRead)
async def reconsider_report(
    report_id: int,
    payload: ReportReconsider,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ReportRead:
    """举报人对「委员 / 合议认定不违规」的结论申请复议（docs/jury-v2.md 3.7）。一个案件只能复议一次。"""
    report = await review_service.request_reconsider(db, current_user.id, report_id, payload.text)
    return await report_service.build_report_read(db, await report_service.get_report(db, report.id), current_user.id)


@router.get("/options", response_model=ReportOptions)
async def report_options() -> ReportOptions:
    """举报表单的可选理由和填写要求。客户端据此渲染，不要写死。"""
    return ReportOptions(
        reasons=[
            ReportReasonRead(reason=reason, details_required=reason == case_service.OTHER_REASON)
            for reason in case_service.REPORT_REASONS
        ],
        details_min_length=case_service.OTHER_REASON_MIN_DETAILS,
    )


@router.get("/quota", response_model=ReporterCredit)
async def my_report_quota(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ReporterCredit:
    return ReporterCredit(**await case_service.reporter_credit(db, current_user.id))
