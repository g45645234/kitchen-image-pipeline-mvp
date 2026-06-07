from pydantic import BaseModel, ConfigDict
from typing import Optional
from datetime import datetime


class FinalAssetBase(BaseModel):
    side: str
    source_type: str
    source_url: Optional[str] = None
    
    license_label: Optional[str] = None
    author_name: Optional[str] = None
    rights_status: str
    may_use_directly: bool = False
    license_note: Optional[str] = None
    license_document_ref: Optional[str] = None
    
    rights_confirmed_by: Optional[str] = None
    rights_confirmed_at: Optional[datetime] = None
    
    storage_key_original: Optional[str] = None
    storage_key_thumbnail: Optional[str] = None
    storage_key_processed: Optional[str] = None
    metadata_storage_key: Optional[str] = None
    
    storage_status: str = "ok"
    original_exif_preserved: bool = False
    processed_exif_stripped: bool = False
    
    caption: Optional[str] = None
    sort_order: int = 0
    status: str = "approved"


class FinalAssetCreate(FinalAssetBase):
    video_id: int
    mistake_id: int
    candidate_id: Optional[int] = None


class FinalAssetResponse(FinalAssetBase):
    id: int
    video_id: int
    mistake_id: int
    candidate_id: Optional[int] = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)

class FinalAssetRightsConfirmRequest(BaseModel):
    rights_status: str = "manual_licensed"
    source_url: Optional[str] = None
    license_note: Optional[str] = None
    license_document_ref: Optional[str] = None
    author_name: Optional[str] = None
    comment: str
    actor: str = "admin-ui"
