from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from typing import List

from app.db import get_db
from app.models.asset import FinalAsset
from app.models.video import Video
from app.models.job import Job
from app.schemas.asset import FinalAssetResponse
from app.schemas.job import JobResponse

router = APIRouter(prefix="", tags=["assets"])


@router.get("/videos/{video_id}/assets", response_model=List[FinalAssetResponse])
async def list_assets_for_video(video_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(FinalAsset).where(FinalAsset.video_id == video_id).order_by(FinalAsset.sort_order))
    assets = result.scalars().all()
    return assets


@router.post("/videos/{video_id}/export", response_model=JobResponse, status_code=status.HTTP_202_ACCEPTED)
async def export_video_assets(video_id: int, db: AsyncSession = Depends(get_db)):
    """
    Запускает сборку финального манифеста и копирование файлов в папку exports.
    """
    result = await db.execute(select(Video).where(Video.id == video_id))
    video = result.scalars().first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
        
    new_job = Job(
        type="export_final_assets",
        status="pending",
        payload={"video_id": video_id}
    )
    db.add(new_job)
    await db.commit()
    await db.refresh(new_job)
    
    return new_job
