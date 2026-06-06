import os
import uuid
import logging
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

async def download_candidate_image(candidate_id: int, image_url: str, db: AsyncSession):
    """
    Симулирует скачивание картинки в локальное хранилище.
    """
    # 1. Создаем директорию, если нет
    storage_dir = "storage/candidates"
    os.makedirs(storage_dir, exist_ok=True)
    
    # 2. Генерируем ключ
    ext = image_url.split('.')[-1] if '.' in image_url else 'jpg'
    if len(ext) > 4: ext = 'jpg'
    
    filename = f"{uuid.uuid4().hex}.{ext}"
    filepath = os.path.join(storage_dir, filename)
    
    # 3. Симулируем файл (пустой или dummy)
    with open(filepath, "wb") as f:
        f.write(b"MOCK_IMAGE_DATA")
        
    logger.info(f"Mock downloaded image for candidate {candidate_id} to {filepath}")
    
    return filepath

async def cleanup_storage(dry_run: bool, db: AsyncSession):
    """
    Симуляция очистки мусора.
    """
    logger.info(f"Running storage cleanup (dry_run={dry_run})")
    # В реальности здесь: обход папки storage, 
    # поиск путей в БД, удаление файлов, которых нет в БД.
    return {"deleted_files_count": 0, "freed_bytes": 0}
