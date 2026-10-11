from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.models.user import User
from app.schemas.case import ReporterCredit, ReportOptions, ReportReasonRead
from app.schemas.report import ReportCreate, ReportRead
from app.services import cases as case_service
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
    await admin_manager.broadcast("admin.report.changed", {"report_id": report.id, "action": "created"})
    return await report_service.build_report_read(db, report, current_user.id)


@router.get("/me", response_model=list[ReportRead])
async def list_my_reports(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[ReportRead]:
    reports = await report_service.list_my_reports(db, current_user.id)
    return await report_service.build_report_reads(db, reports, current_user.id)


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
