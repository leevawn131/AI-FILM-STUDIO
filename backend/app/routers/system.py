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
from pydantic import BaseModel

router = APIRouter(prefix="/api/system", tags=["system"])

class AIConfigRequest(BaseModel):
    provider: Optional[str] = None
    ollama_host: Optional[str] = None
    ollama_model: Optional[str] = None


@router.get("/ai-status")
def get_ai_status():
    """Returns the operational status and model availability of all AI providers."""
    ollama = OllamaProvider()
    gemini = GeminiProvider()
    offline = OfflineTemplateProvider()

    ollama_status = ollama.health_check()
    gemini_status = gemini.health_check()
    offline_status = offline.health_check()

    return {
        "active_provider": settings.ai_provider,
        "ollama": ollama_status,
        "gemini": gemini_status,
        "offline_template": offline_status
    }


@router.post("/ai-config")
def update_ai_config(req: AIConfigRequest):
    """Dynamically updates active AI provider or Ollama settings."""
    if req.provider:
        prov = req.provider.lower().strip()
        if prov not in ("ollama", "gemini", "offline_template", "offline"):
            raise HTTPException(status_code=400, detail=f"Invalid AI provider '{req.provider}'. Must be 'ollama', 'gemini', or 'offline_template'.")
        settings.ai_provider = prov
    if req.ollama_host:
        settings.ollama_host = req.ollama_host.strip()
    if req.ollama_model:
        settings.ollama_model = req.ollama_model.strip()

    return {
        "status": "success",
        "active_provider": settings.ai_provider,
        "ollama_host": settings.ollama_host,
        "ollama_model": settings.ollama_model
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

