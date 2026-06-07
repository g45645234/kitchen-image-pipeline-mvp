import re
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, field_validator


SLUG_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
EDITABLE_VIDEO_STATUSES = {"draft", "ready_to_export"}


class VideoBase(BaseModel):
    title: str
    slug: str
    transcript: Optional[str] = None
    status: str = "draft"

    @field_validator("slug")
    @classmethod
    def validate_slug(cls, value: str) -> str:
        if not SLUG_PATTERN.fullmatch(value):
            raise ValueError("slug must contain lowercase letters, digits, and single hyphens only")
        return value


class VideoCreate(VideoBase):
    @field_validator("status")
    @classmethod
    def validate_status(cls, value: str) -> str:
        if value not in EDITABLE_VIDEO_STATUSES:
            raise ValueError("status must be draft or ready_to_export")
        return value


class VideoUpdate(BaseModel):
    title: Optional[str] = None
    slug: Optional[str] = None
    transcript: Optional[str] = None
    status: Optional[str] = None

    @field_validator("slug")
    @classmethod
    def validate_slug(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and not SLUG_PATTERN.fullmatch(value):
            raise ValueError("slug must contain lowercase letters, digits, and single hyphens only")
        return value

    @field_validator("status")
    @classmethod
    def validate_status(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and value not in EDITABLE_VIDEO_STATUSES:
            raise ValueError("status must be draft or ready_to_export")
        return value


class VideoResponse(VideoBase):
    id: int
    deleted_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class VideoExportReadiness(BaseModel):
    video_id: int
    can_export: bool
    complete: bool
    active_mistake_count: int
    ready_mistake_count: int
    exportable_asset_count: int
    ready_asset_count: int
    warnings: list[dict[str, Any]]
