from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from typing import List

from app.db import get_db
from app.models.video import Video
from app.schemas.video import VideoCreate, VideoResponse, VideoUpdate

router = APIRouter(prefix="/videos", tags=["videos"])


@router.post("", response_model=VideoResponse, status_code=status.HTTP_201_CREATED)
async def create_video(video_in: VideoCreate, db: AsyncSession = Depends(get_db)):
    db_video = Video(**video_in.model_dump())
    db.add(db_video)
    try:
        await db.commit()
        await db.refresh(db_video)
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=400, detail="Video with this slug might already exist.")
    return db_video


@router.get("", response_model=List[VideoResponse])
async def list_videos(skip: int = 0, limit: int = 100, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Video).order_by(Video.created_at.desc()).offset(skip).limit(limit))
    videos = result.scalars().all()
    return videos


@router.get("/{video_id}", response_model=VideoResponse)
async def get_video(video_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Video).where(Video.id == video_id))
    video = result.scalars().first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    return video
