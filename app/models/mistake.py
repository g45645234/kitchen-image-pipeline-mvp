from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship
from app.db import Base


class Mistake(Base):
    __tablename__ = "mistakes"
    __table_args__ = (
        UniqueConstraint("video_id", "order_index", name="uq_mistakes_video_order"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    video_id = Column(Integer, ForeignKey("videos.id", ondelete="CASCADE"), nullable=False, index=True)
    order_index = Column(Integer, nullable=False)
    
    title = Column(String(255), nullable=False)
    short_title = Column(String(100), nullable=True)
    time_start = Column(String(20), nullable=True)
    time_end = Column(String(20), nullable=True)
    explanation = Column(Text, nullable=True)
    
    wrong_visual_prompt = Column(Text, nullable=True)
    right_visual_prompt = Column(Text, nullable=True)
    negative_criteria = Column(JSONB, nullable=False, server_default='[]')
    
    deleted_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    video = relationship("Video", back_populates="mistakes")
    search_queries = relationship("SearchQuery", back_populates="mistake", cascade="all, delete-orphan")
    image_candidates = relationship("ImageCandidate", back_populates="mistake", cascade="all, delete-orphan")
    reference_briefs = relationship("ReferenceBrief", back_populates="mistake", cascade="all, delete-orphan")
    final_assets = relationship("FinalAsset", back_populates="mistake", cascade="all, delete-orphan")
