from pydantic import BaseModel, ConfigDict, Field
from typing import Optional, Dict, Any
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


class ReferenceBriefBase(BaseModel):
    side: str
    visual_problem: Optional[str] = None
    important_visual_signs: Optional[str] = None
    do_not_copy: Optional[str] = None
    clean_generation_brief: Optional[str] = None
    negative_prompt: Optional[str] = None
    status: str = "draft"


class ReferenceBriefCreate(ReferenceBriefBase):
    candidate_id: int
    mistake_id: int


class ReferenceBriefResponse(ReferenceBriefBase):
    id: int
    candidate_id: int
    mistake_id: int
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)
