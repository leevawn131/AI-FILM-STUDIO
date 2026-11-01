import os
from pathlib import Path
from fastapi import APIRouter, HTTPException, Depends
from sqlalchemy.orm import Session
from backend.app.config import settings
from backend.app.database import get_db, backup_database
from backend.app.services.security import validate_safe_path

router = APIRouter(prefix="/api/system", tags=["system"])

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
