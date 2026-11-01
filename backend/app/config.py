import os
import shutil
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("STUDIO_DATA_DIR", BASE_DIR / "data"))

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="allow")

    app_name: str = "AI Film Studio - Hybrid Edition"
    version: str = "1.0.0"
    debug: bool = False
    
    # Storage Paths
    base_dir: Path = BASE_DIR
    data_dir: Path = DATA_DIR
    projects_dir: Path = DATA_DIR / "projects"
    watched_dir: Path = Path(os.getenv("IMPORT_WATCH_DIR", DATA_DIR / "watched_import"))
    exports_dir: Path = DATA_DIR / "exports"
    db_path: Path = DATA_DIR / "studio.db"
    
    # Database
    database_url: str = f"sqlite:///{DATA_DIR / 'studio.db'}"
    
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
for folder in [settings.data_dir, settings.projects_dir, settings.watched_dir, settings.exports_dir]:
    folder.mkdir(parents=True, exist_ok=True)
