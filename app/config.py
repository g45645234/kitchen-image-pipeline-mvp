from pydantic_settings import BaseSettings, SettingsConfigDict
from pathlib import Path
from typing import Optional


class Settings(BaseSettings):
    app_env: str = "local"
    database_url: str
    database_echo: bool = False
    anthropic_api_key: Optional[str] = None
    unsplash_access_key: Optional[str] = None
    yandex_xml_user: Optional[str] = None
    yandex_xml_key: Optional[str] = None
    yandex_api_key: Optional[str] = None
    yandex_folder_id: Optional[str] = None
    yandex_relay_url: Optional[str] = None
    yandex_relay_secret: Optional[str] = None
    pixabay_api_key: Optional[str] = None
    google_api_key: Optional[str] = None
    google_cse_id: Optional[str] = None
    storage_root: Path = Path("./storage")
    export_root: Path = Path("./exports")
    dev_sync_jobs: bool = False
    run_background_worker: bool = True
    admin_api_token: Optional[str] = None
    worker_poll_interval_seconds: float = 5.0
    job_lock_timeout_minutes: int = 30
    worker_job_types: Optional[str] = None
    reviewer_prompt_version: str = "candidate-review-v1"
    mistake_extraction_provider: str = "mock"
    reviewer_timeout_seconds: int = 120
    host_reviewer_status_path: Optional[Path] = Path("./storage/host_reviewer_bridge_status.json")
    host_reviewer_status_ttl_seconds: int = 30
    codex_cli_command: Optional[str] = None
    antigravity_cli_command: Optional[str] = None
    claude_cli_command: Optional[str] = None
    
    default_target_width: int = 1920
    default_target_height: int = 1080
    default_min_width: int = 500
    default_min_height: int = 300
    
    default_search_limit_per_query: int = 20
    max_search_limit_per_query: int = 50
    
    max_upload_mb: int = 20
    max_download_mb: int = 20
    max_image_pixels: int = 25000000
    allowed_image_domains: Optional[str] = None
    
    max_running_jobs: int = 5
    max_image_processing_jobs: int = 2
    
    keep_original_exif: bool = True
    strip_exif_processed: bool = True
    storage_cleanup_dry_run_by_default: bool = True
    require_rights_for_final_approval: bool = True

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


settings = Settings()
