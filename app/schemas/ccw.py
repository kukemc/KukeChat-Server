from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class CcwBindingChallengeRead(BaseModel):
    id: int
    code: str
    status: str
    subject_oid: str
    creation_url: str
    expires_at: datetime
    verified_at: datetime | None = None
    last_checked_at: datetime | None = None
    matched_student_oid: str | None = None
    matched_name: str | None = None
    matched_avatar_url: str | None = None


class CcwSyncProfilePayload(BaseModel):
    sync_profile: bool = False
