from pydantic import BaseModel, ConfigDict
from typing import Optional
from datetime import datetime


class VideoBase(BaseModel):
    title: str
    slug: str
    transcript: Optional[str] = None
    status: str = "draft"


class VideoCreate(VideoBase):
    pass


class VideoUpdate(BaseModel):
    title: Optional[str] = None
    slug: Optional[str] = None
    transcript: Optional[str] = None
    status: Optional[str] = None


class VideoResponse(VideoBase):
    id: int
    deleted_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)
