import os
import shutil
from pathlib import Path
from typing import Optional
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
    
    # AI Engine & Provider Configuration (Google Cloud AI First)
    ai_provider: str = os.getenv("AI_PROVIDER", "gemini") # gemini (default), ollama (optional local fallback), offline_template
    gemini_api_key: str = os.getenv("GEMINI_API_KEY", "")
    gemini_model: str = os.getenv("GEMINI_MODEL", "gemini-4-hydrogen")
    gemini_reasoning_model: str = os.getenv("GEMINI_REASONING_MODEL", "gemini-4-carbon")
    gemini_fallback_model: str = os.getenv("GEMINI_FALLBACK_MODEL", "gemini-3.8-flash")
    gemini_timeout_seconds: int = int(os.getenv("GEMINI_TIMEOUT_SECONDS", "60"))
    gemini_max_retries: int = int(os.getenv("GEMINI_MAX_RETRIES", "3"))
    
    # Google Generative Media Services
    # Image Generation (Nano Banana 2.1 primary via Interactions API)
    google_image_model: str = os.getenv("GOOGLE_IMAGE_MODEL", "gemini-nano-banana-2.1")
    google_image_pro_model: str = os.getenv("GOOGLE_IMAGE_PRO_MODEL", "gemini-3-pro-image")
    google_image_lite_model: str = os.getenv("GOOGLE_IMAGE_LITE_MODEL", "gemini-3.1-flash-lite-image")

    # Video Generation (Gemini Omni 1.2 Flash primary via Interactions API)
    google_video_model: str = os.getenv("GOOGLE_VIDEO_MODEL", "gemini-omni-1.2-flash")
    google_video_fallback_model: str = os.getenv("GOOGLE_VIDEO_FALLBACK_MODEL", "gemini-omni-1.1-flash")

    # Text-to-Speech (Gemini TTS)
    google_tts_model: str = os.getenv("GOOGLE_TTS_MODEL", "gemini-3.8-flash-tts")
    google_tts_lite_model: str = os.getenv("GOOGLE_TTS_LITE_MODEL", "gemini-3.8-flash-lite-tts")

    # Music Generation (Lyria 3.5 via Interactions API)
    google_music_model: str = os.getenv("GOOGLE_MUSIC_MODEL", "lyria-3.5")
    google_music_clip_model: str = os.getenv("GOOGLE_MUSIC_CLIP_MODEL", "lyria-3-clip-preview")
    
    # Optional Google Drive Integration
    google_drive_enabled: bool = os.getenv("GOOGLE_DRIVE_ENABLED", "false").lower() in ("true", "1")
    google_drive_credentials_file: Optional[str] = os.getenv("GOOGLE_DRIVE_CREDENTIALS_FILE", None)
    
    # Optional Local Fallback Providers
    ollama_enabled: bool = os.getenv("OLLAMA_ENABLED", "false").lower() in ("true", "1")
    ollama_host: str = os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434")
    ollama_model: str = os.getenv("OLLAMA_MODEL", "llama3.2")
    ollama_timeout_seconds: int = int(os.getenv("OLLAMA_TIMEOUT_SECONDS", "90"))
    
    # Filmmaking Defaults & Budget
    default_fps: int = 24
    default_aspect_ratio: str = "16:9"
    default_resolution: str = "1920x1080"
    default_project_budget: float = 100.0
    budget_warning_threshold: float = float(os.getenv("BUDGET_WARNING_THRESHOLD", "0.8"))
    
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
