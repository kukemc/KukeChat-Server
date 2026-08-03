from __future__ import annotations

from pydantic import BaseModel


class MessageResponse(BaseModel):
    detail: str
