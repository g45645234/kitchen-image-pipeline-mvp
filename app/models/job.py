from sqlalchemy import Column, Integer, String, Text, DateTime, func
from sqlalchemy.dialects.postgresql import JSONB
from app.db import Base


class Job(Base):
    __tablename__ = "jobs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    type = Column(String(50), nullable=False, index=True)
    status = Column(String(50), nullable=False, default="pending", index=True)
    
    idempotency_key = Column(String(255), nullable=True, unique=True)
    
    payload = Column(JSONB, nullable=False, server_default='{}')
    result = Column(JSONB, nullable=True)
    error_message = Column(Text, nullable=True)
    
    attempts = Column(Integer, nullable=False, default=0)
    max_attempts = Column(Integer, nullable=False, default=3)
    
    locked_by = Column(String(100), nullable=True)
    locked_at = Column(DateTime(timezone=True), nullable=True)
    
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    started_at = Column(DateTime(timezone=True), nullable=True)
    finished_at = Column(DateTime(timezone=True), nullable=True)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
