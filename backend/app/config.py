import os
import shutil
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent
ROOT_DIR = BASE_DIR.parent
DATA_DIR = Path(os.getenv("STUDIO_DATA_DIR", BASE_DIR / "data"))

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="allow")

    app_name: str = "AI Film Studio - Hybrid Edition"
    version: str = "1.0.0"
    debug: bool = False
    
    # Storage Paths
    root_dir: Path = ROOT_DIR
    base_dir: Path = BASE_DIR
    data_dir: Path = DATA_DIR
    projects_dir: Path = DATA_DIR / "projects"
    watched_dir: Path = Path(os.getenv("IMPORT_WATCH_DIR", DATA_DIR / "watched_import"))
    exports_dir: Path = DATA_DIR / "exports"
    backup_dir: Path = DATA_DIR / "backups"
    temp_dir: Path = DATA_DIR / "temp"
    db_path: Path = DATA_DIR / "studio.db"
    
    # Database & Seeding
    database_url: str = f"sqlite:///{DATA_DIR / 'studio.db'}"
    seed_demo_data: bool = False
    
    # Security & CORS
    cors_origins: list[str] = [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ]
    max_upload_size_mb: int = 500
    max_image_upload_size_mb: int = 25
    allowed_video_extensions: list[str] = [".mp4", ".mov", ".webm", ".mkv"]
    allowed_image_extensions: list[str] = [".jpg", ".jpeg", ".png", ".webp"]
    allowed_audio_extensions: list[str] = [".mp3", ".wav", ".aac", ".m4a", ".ogg"]
    
    # Gemini / AI Integration
    gemini_api_key: str = os.getenv("GEMINI_API_KEY", "")
    gemini_model: str = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
    
    # Filmmaking Defaults
    default_fps: int = 24
    default_aspect_ratio: str = "16:9"
    default_resolution: str = "1920x1080"
    default_project_budget: float = 100.0
    
    # FFmpeg tool discovery
    ffmpeg_path: str = shutil.which("ffmpeg") or "ffmpeg"
    ffprobe_path: str = shutil.which("ffprobe") or "ffprobe"

settings = Settings()

# Ensure directories exist
for folder in [
    settings.data_dir,
    settings.projects_dir,
    settings.watched_dir,
    settings.exports_dir,
    settings.backup_dir,
    settings.temp_dir,
]:
    folder.mkdir(parents=True, exist_ok=True)
