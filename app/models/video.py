from sqlalchemy import Column, Integer, String, Text, DateTime, func
from sqlalchemy.orm import relationship
from app.db import Base


class Video(Base):
    __tablename__ = "videos"

    id = Column(Integer, primary_key=True, autoincrement=True)
    slug = Column(String(255), unique=True, nullable=False, index=True)
    title = Column(String(255), nullable=False)
    transcript = Column(Text, nullable=True)
    status = Column(String(50), nullable=False, default="draft", index=True)
    
    deleted_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    mistakes = relationship("Mistake", back_populates="video", cascade="all, delete-orphan")
    final_assets = relationship("FinalAsset", back_populates="video", cascade="all, delete-orphan")
