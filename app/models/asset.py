from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey, Boolean, UniqueConstraint, func
from sqlalchemy.orm import relationship
from app.db import Base


class FinalAsset(Base):
    __tablename__ = "final_assets"
    __table_args__ = (
        UniqueConstraint("mistake_id", "side", name="uq_final_asset_mistake_side"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    video_id = Column(Integer, ForeignKey("videos.id", ondelete="CASCADE"), nullable=False, index=True)
    mistake_id = Column(Integer, ForeignKey("mistakes.id", ondelete="CASCADE"), nullable=False, index=True)
    side = Column(String(20), nullable=False)
    
    candidate_id = Column(Integer, ForeignKey("image_candidates.id", ondelete="SET NULL"), nullable=True, index=True)
    
    source_type = Column(String(50), nullable=False)
    source_url = Column(Text, nullable=True)
    
    license_label = Column(String(100), nullable=True)
    author_name = Column(String(255), nullable=True)
    rights_status = Column(String(50), nullable=False)
    may_use_directly = Column(Boolean, nullable=False, default=False)
    license_note = Column(Text, nullable=True)
    license_document_ref = Column(String(255), nullable=True)
    
    rights_confirmed_by = Column(String(100), nullable=True)
    rights_confirmed_at = Column(DateTime(timezone=True), nullable=True)
    
    storage_key_original = Column(String(255), nullable=True)
    storage_key_thumbnail = Column(String(255), nullable=True)
    storage_key_processed = Column(String(255), nullable=True)
    metadata_storage_key = Column(String(255), nullable=True)
    
    storage_status = Column(String(50), nullable=False, default="ok")
    original_exif_preserved = Column(Boolean, nullable=False, default=False)
    processed_exif_stripped = Column(Boolean, nullable=False, default=False)
    
    caption = Column(Text, nullable=True)
    sort_order = Column(Integer, nullable=False, default=0)
    
    status = Column(String(50), nullable=False, default="approved")
    
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    video = relationship("Video", back_populates="final_assets")
    mistake = relationship("Mistake", back_populates="final_assets")
    candidate = relationship("ImageCandidate", back_populates="final_assets")
