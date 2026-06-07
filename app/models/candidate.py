from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey, Boolean, Numeric, CheckConstraint, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship
from app.db import Base


class SearchQuery(Base):
    __tablename__ = "search_queries"

    id = Column(Integer, primary_key=True, autoincrement=True)
    mistake_id = Column(Integer, ForeignKey("mistakes.id", ondelete="CASCADE"), nullable=False, index=True)
    side = Column(String(20), nullable=False) # 'wrong' or 'right'
    source_provider = Column(String(50), nullable=False)
    
    query_text = Column(Text, nullable=False)
    language = Column(String(10), nullable=False, default="ru")
    
    status = Column(String(50), nullable=False, default="pending", index=True)
    results_count = Column(Integer, nullable=True)
    error_message = Column(Text, nullable=True)
    
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    mistake = relationship("Mistake", back_populates="search_queries")
    image_candidates = relationship("ImageCandidate", back_populates="search_query")


class ImageCandidate(Base):
    __tablename__ = "image_candidates"
    __table_args__ = (
        UniqueConstraint("mistake_id", "side", "image_url_hash", name="uq_candidate_mistake_side_hash"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    mistake_id = Column(Integer, ForeignKey("mistakes.id", ondelete="CASCADE"), nullable=False, index=True)
    query_id = Column(Integer, ForeignKey("search_queries.id", ondelete="SET NULL"), nullable=True)
    
    side = Column(String(20), nullable=False) # 'wrong' or 'right'
    source_type = Column(String(50), nullable=False) # 'search', 'generation', 'manual'
    source_provider = Column(String(50), nullable=True)
    
    source_page_url = Column(Text, nullable=True)
    image_url = Column(Text, nullable=False)
    image_url_hash = Column(String(64), nullable=False, index=True)
    thumbnail_url = Column(Text, nullable=True)
    
    original_width = Column(Integer, nullable=True)
    original_height = Column(Integer, nullable=True)
    domain = Column(String(255), nullable=True, index=True)
    
    author_name = Column(String(255), nullable=True)
    license_label = Column(String(100), nullable=True)
    rights_status = Column(String(50), nullable=False, default="unknown")
    usage_role = Column(String(50), nullable=False, default="candidate")
    may_use_directly = Column(Boolean, nullable=False, default=False)
    
    storage_key_thumbnail = Column(String(255), nullable=True)
    storage_key_original = Column(String(255), nullable=True)
    storage_key_processed = Column(String(255), nullable=True)
    
    phash = Column(String(64), nullable=True)
    score_quality = Column(Numeric, nullable=True)
    score_visual = Column(Numeric, nullable=True)
    reference_priority_score = Column(Numeric, nullable=True)
    review_score = Column(Numeric, nullable=True, index=True)
    
    quality_flags = Column(JSONB, nullable=False, server_default='{}')
    is_low_quality = Column(Boolean, nullable=False, default=False)
    
    storage_status = Column(String(50), nullable=False, default="pending")
    status = Column(String(50), nullable=False, default="new", index=True)
    reject_reason = Column(String(100), nullable=True)
    
    reviewed_by = Column(String(100), nullable=True)
    reviewed_at = Column(DateTime(timezone=True), nullable=True)
    
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    mistake = relationship("Mistake", back_populates="image_candidates")
    search_query = relationship("SearchQuery", back_populates="image_candidates")
    reviews = relationship("CandidateReview", back_populates="candidate", cascade="all, delete-orphan")
    reference_brief = relationship("ReferenceBrief", back_populates="candidate", uselist=False, cascade="all, delete-orphan")
    final_assets = relationship("FinalAsset", back_populates="candidate")


class CandidateReview(Base):
    __tablename__ = "candidate_reviews"
    __table_args__ = (
        UniqueConstraint("candidate_id", "reviewer_name", name="uq_candidate_review_reviewer"),
        CheckConstraint("score >= 0 AND score <= 1", name="ck_candidate_reviews_score_range"),
        CheckConstraint("verdict IN ('pass', 'maybe', 'fail')", name="ck_candidate_reviews_verdict"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    candidate_id = Column(Integer, ForeignKey("image_candidates.id", ondelete="CASCADE"), nullable=False, index=True)
    mistake_id = Column(Integer, ForeignKey("mistakes.id", ondelete="CASCADE"), nullable=False, index=True)

    side = Column(String(20), nullable=False)
    reviewer_name = Column(String(100), nullable=False, index=True)
    reviewer_version = Column(String(100), nullable=True)

    score = Column(Numeric, nullable=False)
    verdict = Column(String(20), nullable=False)
    reason = Column(Text, nullable=True)
    flags = Column(JSONB, nullable=False, server_default='{}')
    response_time_ms = Column(Integer, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    candidate = relationship("ImageCandidate", back_populates="reviews")
    mistake = relationship("Mistake")


class ReferenceBrief(Base):
    __tablename__ = "reference_briefs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    candidate_id = Column(Integer, ForeignKey("image_candidates.id", ondelete="CASCADE"), nullable=False, unique=True)
    mistake_id = Column(Integer, ForeignKey("mistakes.id", ondelete="CASCADE"), nullable=False)
    side = Column(String(20), nullable=False)
    
    visual_problem = Column(Text, nullable=True)
    important_visual_signs = Column(JSONB, nullable=False, server_default='[]')
    do_not_copy = Column(JSONB, nullable=False, server_default='[]')
    clean_generation_brief = Column(Text, nullable=True)
    negative_prompt = Column(Text, nullable=True)
    error_message = Column(Text, nullable=True)
    
    status = Column(String(50), nullable=False, default="draft")
    
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    candidate = relationship("ImageCandidate", back_populates="reference_brief")
    mistake = relationship("Mistake", back_populates="reference_briefs")
