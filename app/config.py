from pydantic_settings import BaseSettings, SettingsConfigDict
from pathlib import Path


class Settings(BaseSettings):
    app_env: str = "local"
    database_url: str
    storage_root: Path = Path("./storage")
    export_root: Path = Path("./exports")
    dev_sync_jobs: bool = False
    
    default_target_width: int = 1920
    default_target_height: int = 1080
    default_min_width: int = 500
    default_min_height: int = 300
    
    default_search_limit_per_query: int = 20
    max_search_limit_per_query: int = 50
    
    max_upload_mb: int = 20
    max_download_mb: int = 20
    max_image_pixels: int = 25000000
    
    max_running_jobs: int = 5
    max_image_processing_jobs: int = 2
    
    keep_original_exif: bool = True
    strip_exif_processed: bool = True
    storage_cleanup_dry_run_by_default: bool = True
    require_rights_for_final_approval: bool = True

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


settings = Settings()
