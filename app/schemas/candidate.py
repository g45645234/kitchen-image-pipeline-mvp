from pydantic import BaseModel, ConfigDict, Field
from typing import Optional, Dict, Any, List
from datetime import datetime


class SearchQueryBase(BaseModel):
    side: str
    source_provider: str
    query_text: str
    language: str = "ru"
    status: str = "pending"
    results_count: Optional[int] = None
    error_message: Optional[str] = None


class SearchQueryCreate(SearchQueryBase):
    mistake_id: int


class SearchQueryGenerateRequest(BaseModel):
    sides: list[str] = Field(default_factory=lambda: ["wrong", "right"])
    providers: Optional[list[str]] = None
    limit_per_query: int = Field(default=20, ge=1, le=50)


class SearchQueryManualCreateRequest(BaseModel):
    side: str = Field(pattern="^(wrong|right)$")
    source_provider: str = "mock_search"
    query_text: str = Field(min_length=1)
    language: str = "ru"
    results_count: int = Field(default=20, ge=1, le=50)


class SearchQueryUpdateRequest(BaseModel):
    side: Optional[str] = Field(default=None, pattern="^(wrong|right)$")
    source_provider: Optional[str] = None
    query_text: Optional[str] = Field(default=None, min_length=1)
    language: Optional[str] = None
    results_count: Optional[int] = Field(default=None, ge=1, le=50)
    status: Optional[str] = Field(default=None, pattern="^(pending|running|completed|failed)$")


class SearchQueryResponse(SearchQueryBase):
    id: int
    mistake_id: int
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ImageCandidateBase(BaseModel):
    side: str
    source_type: str
    source_provider: Optional[str] = None
    source_page_url: Optional[str] = None
    image_url: str
    image_url_hash: str
    thumbnail_url: Optional[str] = None
    original_width: Optional[int] = None
    original_height: Optional[int] = None
    domain: Optional[str] = None
    
    author_name: Optional[str] = None
    license_label: Optional[str] = None
    rights_status: str = "unknown"
    usage_role: str = "candidate"
    may_use_directly: bool = False
    
    storage_key_thumbnail: Optional[str] = None
    storage_key_original: Optional[str] = None
    storage_key_processed: Optional[str] = None
    
    phash: Optional[str] = None
    score_quality: Optional[float] = None
    score_visual: Optional[float] = None
    reference_priority_score: Optional[float] = None
    review_score: Optional[float] = None
    
    quality_flags: Dict[str, Any] = Field(default_factory=dict)
    is_low_quality: bool = False
    
    storage_status: str = "pending"
    status: str = "new"
    reject_reason: Optional[str] = None
    
    reviewed_by: Optional[str] = None
    reviewed_at: Optional[datetime] = None


class ImageCandidateCreate(ImageCandidateBase):
    mistake_id: int
    query_id: Optional[int] = None


class ImageCandidateResponse(ImageCandidateBase):
    id: int
    mistake_id: int
    query_id: Optional[int] = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class CandidateReferenceRequest(BaseModel):
    mark_high_value: bool = False
    comment: Optional[str] = None
    actor: str = "admin-ui"


class CandidateBlockDomainRequest(BaseModel):
    reason: Optional[str] = None
    actor: str = "admin-ui"


class CandidateRightsConfirmRequest(BaseModel):
    rights_status: str = "manual_licensed"
    source_url: Optional[str] = None
    license_note: Optional[str] = None
    license_document_ref: Optional[str] = None
    author_name: Optional[str] = None
    comment: str
    actor: str = "admin-ui"






class ReviewerCliStatus(BaseModel):
    reviewer_name: str
    setting_name: str
    configured: bool
    executable: bool
    executable_path: Optional[str] = None
    ready: bool = False
    execution_environment: str = "host_bridge"
    message: Optional[str] = None
    web_process_executable: bool = False
    web_process_executable_path: Optional[str] = None
    host_bridge_seen_at: Optional[datetime] = None
    host_bridge_age_seconds: Optional[float] = None
    host_bridge_state: Optional[str] = None
    host_bridge_pid: Optional[int] = None
    host_bridge_locked_by: Optional[str] = None
    error: Optional[str] = None


class CandidateReviewRunRequest(BaseModel):
    reviewers: list[str] = Field(default_factory=lambda: ["codex", "antigravity", "claude_cli"])
    prompt_version: Optional[str] = None
    force: bool = False


class CandidateReviewBase(BaseModel):
    reviewer_name: str
    reviewer_version: Optional[str] = None
    score: float = Field(ge=0.0, le=1.0)
    verdict: str
    reason: Optional[str] = None
    flags: Dict[str, Any] = Field(default_factory=dict)
    response_time_ms: Optional[int] = Field(default=None, ge=0)


class CandidateReviewCreate(CandidateReviewBase):
    pass


class CandidateReviewResponse(CandidateReviewBase):
    id: int
    candidate_id: int
    mistake_id: int
    side: str
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class CandidateReviewAggregate(BaseModel):
    candidate_id: int
    review_score: Optional[float] = None
    review_count: int
    pass_count: int
    approved_by_consensus: bool
    reviewers: list[str]


class ReferenceBriefBase(BaseModel):
    side: str
    visual_problem: Optional[str] = None
    important_visual_signs: List[str] = Field(default_factory=list)
    do_not_copy: List[str] = Field(default_factory=list)
    clean_generation_brief: Optional[str] = None
    negative_prompt: Optional[str] = None
    error_message: Optional[str] = None
    status: str = "draft"


class ReferenceBriefCreate(ReferenceBriefBase):
    candidate_id: int
    mistake_id: int


class ReferenceBriefUpdate(BaseModel):
    visual_problem: Optional[str] = None
    important_visual_signs: Optional[List[str]] = None
    do_not_copy: Optional[List[str]] = None
    clean_generation_brief: Optional[str] = None
    negative_prompt: Optional[str] = None
    error_message: Optional[str] = None
    status: Optional[str] = None


class ReferenceBriefResponse(ReferenceBriefBase):
    id: int
    candidate_id: int
    mistake_id: int
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)
