from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import relationship

from app.db import Base


class MistakeSideFeedback(Base):
    __tablename__ = "mistake_side_feedback"
    __table_args__ = (
        UniqueConstraint("mistake_id", "side", name="uq_mistake_side_feedback"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    mistake_id = Column(Integer, ForeignKey("mistakes.id", ondelete="CASCADE"), nullable=False, index=True)
    side = Column(String(20), nullable=False)
    feedback_text = Column(Text, nullable=False, default="")
    actor = Column(String(100), nullable=False, default="admin-ui")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    mistake = relationship("Mistake")
