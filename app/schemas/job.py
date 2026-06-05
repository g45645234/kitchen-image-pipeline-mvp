from pydantic import BaseModel, ConfigDict
from typing import Optional, Any, Dict
from datetime import datetime


class JobBase(BaseModel):
    type: str
    status: str = "pending"
    idempotency_key: Optional[str] = None
    payload: Dict[str, Any]
    result: Optional[Dict[str, Any]] = None
    error_message: Optional[str] = None
    attempts: int = 0
    max_attempts: int = 3
    locked_by: Optional[str] = None
    locked_at: Optional[datetime] = None


class JobCreate(JobBase):
    pass


class JobResponse(JobBase):
    id: int
    created_at: datetime
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


# --- Typed Payloads for background workers ---

class BaseJobPayload(BaseModel):
    """Base class for strict typing of job payloads."""
    pass


class FetchImagesJobPayload(BaseJobPayload):
    query_id: int
    provider: str


class ProcessImageJobPayload(BaseJobPayload):
    candidate_id: int


class AnalyzeQualityJobPayload(BaseJobPayload):
    candidate_id: int


class VerifyRightsJobPayload(BaseJobPayload):
    candidate_id: int


class ReviewCandidateJobPayload(BaseJobPayload):
    candidate_id: int
    action: str  # e.g., 'approve', 'reject'


class ExportFinalAssetsJobPayload(BaseJobPayload):
    video_id: int


class CleanupStorageJobPayload(BaseJobPayload):
    dry_run: bool = True
