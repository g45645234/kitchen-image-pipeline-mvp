from pydantic import BaseModel, ConfigDict, Field
from typing import Optional, List
from datetime import datetime


class MistakeBase(BaseModel):
    order_index: int
    title: str
    short_title: Optional[str] = None
    time_start: Optional[str] = None
    time_end: Optional[str] = None
    explanation: Optional[str] = None
    wrong_visual_prompt: Optional[str] = None
    right_visual_prompt: Optional[str] = None
    negative_criteria: List[str] = Field(default_factory=list)


class MistakeCreate(MistakeBase):
    video_id: int


class MistakeCreateForVideo(MistakeBase):
    pass


class MistakeUpdate(BaseModel):
    order_index: Optional[int] = None
    title: Optional[str] = None
    short_title: Optional[str] = None
    time_start: Optional[str] = None
    time_end: Optional[str] = None
    explanation: Optional[str] = None
    wrong_visual_prompt: Optional[str] = None
    right_visual_prompt: Optional[str] = None
    negative_criteria: Optional[List[str]] = None


class MistakeResponse(MistakeBase):
    id: int
    video_id: int
    deleted_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class MistakeSideFeedbackUpdate(BaseModel):
    feedback_text: str = ""
    actor: str = "admin-ui"


class MistakeSideFeedbackResponse(BaseModel):
    id: int
    mistake_id: int
    side: str
    feedback_text: str
    actor: str
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)

