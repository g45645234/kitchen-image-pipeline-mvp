import os
import json
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from app.models.asset import FinalAsset
from app.models.video import Video
from app.models.mistake import Mistake

async def export_video_manifest(video_id: int, db: AsyncSession):
    """
    Собирает финальные ассеты и генерирует JSON манифест для сборки видео.
    """
    # 1. Получаем видео
    result_video = await db.execute(select(Video).where(Video.id == video_id))
    video = result_video.scalars().first()
    if not video:
        raise ValueError(f"Video {video_id} not found")

    # 2. Получаем ассеты
    result_assets = await db.execute(select(FinalAsset).where(FinalAsset.video_id == video_id))
    assets = result_assets.scalars().all()
    
    # 3. Собираем структуру
    manifest = {
        "video_id": video.id,
        "title": video.title,
        "assets": []
    }
    
    for asset in assets:
        # Получаем информацию об ошибке
        result_mistake = await db.execute(select(Mistake).where(Mistake.id == asset.mistake_id))
        mistake = result_mistake.scalars().first()
        
        manifest["assets"].append({
            "asset_id": asset.id,
            "mistake_index": mistake.order_index if mistake else 0,
            "side": asset.side,
            "storage_path": asset.storage_key_original,
            "rights_status": asset.rights_status
        })
        
    # 4. Сохраняем файл
    export_dir = "exports"
    os.makedirs(export_dir, exist_ok=True)
    manifest_path = os.path.join(export_dir, f"video_{video_id}_manifest.json")
    
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
        
    return manifest_path
