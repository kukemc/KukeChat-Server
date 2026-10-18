from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator


class FavoriteStickerCreate(BaseModel):
    source_url: str = Field(min_length=1, max_length=2000)

    @field_validator("source_url")
    @classmethod
    def normalize_source_url(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Source image URL is required")
        return normalized


class FavoriteStickerIds(BaseModel):
    """排序（必须正好是当前全部收藏表情）和批量删除共用。"""

    asset_ids: list[int] = Field(max_length=2000)


class FavoriteStickerRead(BaseModel):
    asset_id: int
    url: str
    content_type: str
    created_at: datetime
