import os
import uuid
from pathlib import Path
from typing import Optional, List
from fastapi import APIRouter, HTTPException, Depends, UploadFile, File, Form, Query
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from backend.app.config import settings
from backend.app.database import get_db, backup_database
from backend.app.services.security import validate_safe_path
from backend.app.services.ai_provider import OllamaProvider, GeminiProvider, OfflineTemplateProvider
from backend.app.services.project_backup import project_backup_service
from backend.app.services.google_drive_service import google_drive_service
from pydantic import BaseModel

router = APIRouter(prefix="/api/system", tags=["system"])

class AIConfigRequest(BaseModel):
    provider: Optional[str] = None
    gemini_api_key: Optional[str] = None
    gemini_model: Optional[str] = None
    ollama_enabled: Optional[bool] = None
    ollama_host: Optional[str] = None
    ollama_model: Optional[str] = None
    google_image_model: Optional[str] = None
    google_image_pro_model: Optional[str] = None
    google_image_lite_model: Optional[str] = None
    google_video_model: Optional[str] = None
    google_tts_model: Optional[str] = None
    google_tts_lite_model: Optional[str] = None
    google_music_model: Optional[str] = None
    google_music_clip_model: Optional[str] = None


@router.get("/ai-status")
def get_ai_status():
    """Returns the operational status and model availability of all AI providers."""
    ollama = OllamaProvider()
    gemini = GeminiProvider()
    offline = OfflineTemplateProvider()

    ollama_status = ollama.health_check()
    gemini_status = gemini.health_check()
    offline_status = offline.health_check()

    # Never expose raw API key to the client
    raw_key = settings.gemini_api_key or ""
    masked_key = f"{raw_key[:4]}...{raw_key[-4:]}" if len(raw_key) >= 8 else ("(configured)" if raw_key else "not_configured")

    return {
        "primary_provider": "gemini",
        "active_provider": settings.ai_provider,
        "gemini": {
            **gemini_status,
            "api_key_configured": bool(raw_key),
            "masked_key": masked_key
        },
        "ollama": {
            **ollama_status,
            "enabled": getattr(settings, "ollama_enabled", False)
        },
        "offline_template": offline_status,
        "google_drive": google_drive_service.get_status(),
        "google_models": {
            "text": settings.gemini_model,
            "image": getattr(settings, "google_image_model", "gemini-3.1-flash-image"),
            "image_pro": getattr(settings, "google_image_pro_model", "gemini-3-pro-image"),
            "image_lite": getattr(settings, "google_image_lite_model", "gemini-3.1-flash-lite-image"),
            "video": getattr(settings, "google_video_model", "gemini-omni-1.1-flash"),
            "tts": getattr(settings, "google_tts_model", "gemini-3.8-flash-tts"),
            "tts_lite": getattr(settings, "google_tts_lite_model", "gemini-3.8-flash-lite-tts"),
            "music": getattr(settings, "google_music_model", "lyria-3.5"),
            "music_clip": getattr(settings, "google_music_clip_model", "lyria-3-clip-preview"),
            "image_sizes": {
                "gemini-3.1-flash-image": ["512", "1K", "2K", "4K"],
                "gemini-3.1-flash-lite-image": ["512", "1K"],
                "gemini-3-pro-image": ["1K", "2K", "4K"]
            }
        }
    }


@router.post("/ai-config")
def update_ai_config(req: AIConfigRequest):
    """Dynamically updates active AI provider, Gemini API key, or optional Ollama settings."""
    if req.provider:
        prov = req.provider.lower().strip()
        if prov not in ("ollama", "gemini", "offline_template", "offline"):
            raise HTTPException(status_code=400, detail=f"Invalid AI provider '{req.provider}'. Must be 'gemini', 'ollama', or 'offline_template'.")
        settings.ai_provider = prov

    if req.gemini_api_key is not None:
        settings.gemini_api_key = req.gemini_api_key.strip()
        os.environ["GEMINI_API_KEY"] = settings.gemini_api_key

    if req.gemini_model:
        settings.gemini_model = req.gemini_model.strip()

    if req.ollama_enabled is not None:
        settings.ollama_enabled = req.ollama_enabled

    if req.ollama_host:
        settings.ollama_host = req.ollama_host.strip()

    if req.ollama_model:
        settings.ollama_model = req.ollama_model.strip()

    if req.google_image_model:
        settings.google_image_model = req.google_image_model.strip()

    if req.google_image_pro_model:
        settings.google_image_pro_model = req.google_image_pro_model.strip()

    if req.google_image_lite_model:
        settings.google_image_lite_model = req.google_image_lite_model.strip()

    if req.google_video_model:
        settings.google_video_model = req.google_video_model.strip()

    if req.google_tts_model:
        settings.google_tts_model = req.google_tts_model.strip()

    if req.google_tts_lite_model:
        settings.google_tts_lite_model = req.google_tts_lite_model.strip()

    if req.google_music_model:
        settings.google_music_model = req.google_music_model.strip()

    if req.google_music_clip_model:
        settings.google_music_clip_model = req.google_music_clip_model.strip()

    raw_key = settings.gemini_api_key or ""
    masked_key = f"{raw_key[:4]}...{raw_key[-4:]}" if len(raw_key) >= 8 else ("(configured)" if raw_key else "not_configured")

    return {
        "status": "success",
        "active_provider": settings.ai_provider,
        "gemini_configured": bool(settings.gemini_api_key),
        "masked_key": masked_key,
        "gemini_model": settings.gemini_model,
        "ollama_enabled": getattr(settings, "ollama_enabled", False),
        "ollama_host": settings.ollama_host,
        "ollama_model": settings.ollama_model
    }


@router.get("/google-drive/status")
def get_google_drive_status():
    """Returns optional Google Drive cloud backup availability status."""
    return google_drive_service.get_status()


@router.get("/google-drive/backups")
def list_google_drive_backups():
    """Lists remote project backups stored in Google Drive."""
    return google_drive_service.list_remote_backups()


@router.post("/google-drive/backup/{project_id}")
def backup_to_google_drive(project_id: str, db: Session = Depends(get_db)):
    """Exports project ZIP package and uploads it to Google Drive."""
    # 1. Create local zip package
    res = project_backup_service.export_project_archive(db, project_id)
    zip_path = Path(res["archive_path"])

    # 2. Upload to Google Drive if configured
    drive_res = google_drive_service.upload_project_backup(project_id, zip_path)
    return {
        "local_backup": res,
        "drive_sync": drive_res
    }

@router.post("/backup")
def trigger_backup():
    """
    Creates an atomic online snapshot of the SQLite database.
    """
    try:
        backup_path = backup_database()
        return {
            "status": "success",
            "message": "Database backup completed successfully",
            "backup_file": backup_path.name,
            "size_bytes": backup_path.stat().st_size,
            "created_at": backup_path.stat().st_mtime
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database backup failed: {str(e)}")

@router.get("/backups")
def list_backups():
    """
    Lists available database backups.
    """
    if not settings.backup_dir.exists():
        return []
    
    backups = []
    for file in sorted(settings.backup_dir.glob("studio_backup_*.db"), reverse=True):
        stat = file.stat()
        backups.append({
            "filename": file.name,
            "size_bytes": stat.st_size,
            "modified_at": stat.st_mtime
        })
    return backups

@router.post("/seed-demo")
def seed_demo(force: bool = True):
    """
    Manually triggers demo project seeding.
    """
    from backend.app.main import seed_initial_demo_if_empty
    try:
        seed_initial_demo_if_empty(force=force)
        return {"status": "success", "message": "Demo data checked/seeded"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to seed demo data: {str(e)}")


# --- Project-Level Full Backup & Disaster Recovery ---

@router.post("/projects/{project_id}/backup")
def export_project_package(project_id: str, db: Session = Depends(get_db)):
    """Exports a complete project (database entities + physical media assets) as a verified ZIP."""
    try:
        zip_path = project_backup_service.create_project_backup(db=db, project_id=project_id)
        return {
            "status": "success",
            "message": "Project backup created successfully",
            "backup_filename": zip_path.name,
            "download_url": f"/api/system/projects/{project_id}/download-backup/{zip_path.name}",
            "size_bytes": zip_path.stat().st_size
        }
    except ValueError as ve:
        raise HTTPException(status_code=404, detail=str(ve))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Project backup failed: {str(e)}")


@router.get("/projects/{project_id}/download-backup/{filename}")
def download_project_backup(project_id: str, filename: str):
    """Downloads a previously generated project backup archive."""
    safe_path = validate_safe_path(settings.backup_dir / filename, settings.backup_dir)
    if not safe_path.exists():
        raise HTTPException(status_code=404, detail="Backup file not found")
    return FileResponse(
        path=str(safe_path),
        filename=filename,
        media_type="application/zip"
    )


@router.post("/projects/restore")
async def restore_project_package(
    file: UploadFile = File(...),
    new_project_name: Optional[str] = Form(None),
    overwrite: bool = Form(False),
    db: Session = Depends(get_db)
):
    """
    Restores a project from an uploaded ZIP package.
    Validates manifest and SHA-256 checksums, and protects against path traversal.
    """
    temp_zip = settings.temp_dir / f"upload_restore_{uuid.uuid4().hex[:8]}.zip"
    try:
        with open(temp_zip, "wb") as f:
            content = await file.read()
            f.write(content)

        result = project_backup_service.restore_project_backup(
            db=db,
            zip_path=temp_zip,
            new_project_name=new_project_name,
            overwrite=overwrite
        )
        return result
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Project restore failed: {str(e)}")
    finally:
        if temp_zip.exists():
            temp_zip.unlink(missing_ok=True)

