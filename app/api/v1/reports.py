from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.models.user import User
from app.schemas.report import ReportCreate, ReportRead
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
    await admin_manager.broadcast("admin.report.changed", {"report_id": report.id, "action": "created"})
    return await report_service.build_report_read(db, report, current_user.id)


@router.get("/me", response_model=list[ReportRead])
async def list_my_reports(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[ReportRead]:
    reports = await report_service.list_my_reports(db, current_user.id)
    return await report_service.build_report_reads(db, reports, current_user.id)
