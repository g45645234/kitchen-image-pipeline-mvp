from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from typing import List

from app.db import get_db
from app.models.mistake import Mistake
from app.models.video import Video
from app.models.job import Job
from app.schemas.mistake import MistakeCreate, MistakeResponse
from app.schemas.job import JobResponse

router = APIRouter(prefix="", tags=["mistakes"])


@router.get("/videos/{video_id}/mistakes", response_model=List[MistakeResponse])
async def list_mistakes_for_video(video_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Mistake).where(Mistake.video_id == video_id).order_by(Mistake.order_index))
    mistakes = result.scalars().all()
    return mistakes


@router.get("/mistakes/{mistake_id}", response_model=MistakeResponse)
async def get_mistake(mistake_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Mistake).where(Mistake.id == mistake_id))
    mistake = result.scalars().first()
    if not mistake:
        raise HTTPException(status_code=404, detail="Mistake not found")
    return mistake


@router.post("/mistakes/{mistake_id}/candidates/search", response_model=JobResponse, status_code=status.HTTP_202_ACCEPTED)
async def search_candidates_for_mistake(mistake_id: int, db: AsyncSession = Depends(get_db)):
    """
    Создает фоновую задачу (Job) на поиск картинок по всем запросам для данной ошибки.
    В MVP пока просто создаем запись в БД.
    """
    # 1. Проверяем, существует ли ошибка
    result = await db.execute(select(Mistake).where(Mistake.id == mistake_id))
    mistake = result.scalars().first()
    if not mistake:
        raise HTTPException(status_code=404, detail="Mistake not found")
        
    # 2. Создаем Job (заглушка для будущего сервиса)
    new_job = Job(
        type="search_all_queries",
        status="pending",
        payload={"mistake_id": mistake_id}
    )
    db.add(new_job)
    await db.commit()
    await db.refresh(new_job)
    
    return new_job
