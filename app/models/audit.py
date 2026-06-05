from sqlalchemy import Column, Integer, String, Text, DateTime, func
from sqlalchemy.dialects.postgresql import JSONB
from app.db import Base


class BlockedDomain(Base):
    __tablename__ = "blocked_domains"

    id = Column(Integer, primary_key=True, autoincrement=True)
    domain = Column(String(255), nullable=False, unique=True, index=True)
    reason = Column(String(255), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    actor = Column(String(100), nullable=False, index=True)
    entity_type = Column(String(50), nullable=False, index=True)
    entity_id = Column(Integer, nullable=False, index=True)
    
    action = Column(String(50), nullable=False)
    
    before = Column(JSONB, nullable=True)
    after = Column(JSONB, nullable=True)
    
    comment = Column(Text, nullable=True)
    
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
