from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from typing import List
from pydantic import BaseModel

from app.db import get_db
from app.models.candidate import ImageCandidate
from app.models.job import Job
from app.schemas.candidate import ImageCandidateResponse
from app.schemas.job import JobResponse

router = APIRouter(prefix="", tags=["candidates"])


class ReviewRequest(BaseModel):
    action: str  # 'approve', 'reject', 'reference_only'
    reason: str = None


@router.get("/mistakes/{mistake_id}/candidates", response_model=List[ImageCandidateResponse])
async def list_candidates_for_mistake(mistake_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(ImageCandidate).where(ImageCandidate.mistake_id == mistake_id))
    candidates = result.scalars().all()
    return candidates


@router.post("/candidates/{candidate_id}/review", response_model=JobResponse, status_code=status.HTTP_202_ACCEPTED)
async def review_candidate(candidate_id: int, review_in: ReviewRequest, db: AsyncSession = Depends(get_db)):
    """
    Принимает решение по кандидату.
    В MVP создаем Job для асинхронного переноса картинки в FinalAsset.
    """
    result = await db.execute(select(ImageCandidate).where(ImageCandidate.id == candidate_id))
    candidate = result.scalars().first()
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate not found")
        
    new_job = Job(
        type="review_candidate",
        status="pending",
        payload={
            "candidate_id": candidate_id,
            "action": review_in.action,
            "reason": review_in.reason
        }
    )
    db.add(new_job)
    await db.commit()
    await db.refresh(new_job)
    
    return new_job
