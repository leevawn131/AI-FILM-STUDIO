import shutil
from pathlib import Path
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Query
from sqlalchemy.orm import Session
from pydantic import BaseModel
from backend.app.database import get_db
from backend.app.config import settings
from backend.app.models.entities import Project, MediaAsset, Shot
from backend.app.schemas.entities import MediaAssetResponse
from backend.app.services.media_watcher import media_watcher_service, VIDEO_EXTENSIONS
from backend.app.services.ffmpeg_service import ffmpeg_service

router = APIRouter(prefix="/api/media", tags=["media"])

class LinkShotRequest(BaseModel):
    shot_id: str

@router.get("/assets/{project_id}", response_model=List[MediaAssetResponse])
def list_project_media(project_id: str, db: Session = Depends(get_db)):
    assets = db.query(MediaAsset).filter(MediaAsset.project_id == project_id).order_by(MediaAsset.created_at.desc()).all()
    res = []
    for a in assets:
        res.append({
            "id": a.id,
            "project_id": a.project_id,
            "filename": a.filename,
            "file_path": a.file_path,
            "media_type": a.media_type,
            "width": a.width,
            "height": a.height,
            "duration_seconds": a.duration_seconds,
            "fps": a.fps,
            "codec": a.codec,
            "file_size_bytes": a.file_size_bytes,
            "thumbnail_path": a.thumbnail_path,
            "auto_matched_shot_id": a.auto_matched_shot_id,
            "is_validated": a.is_validated,
            "validation_notes": a.validation_notes,
            "created_at": a.created_at,
            "url": f"/media/{a.file_path}"
        })
    return res

@router.post("/upload/{project_id}", response_model=MediaAssetResponse)
async def upload_media_file(project_id: str, file: UploadFile = File(...), db: Session = Depends(get_db)):
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    temp_path = settings.watched_dir / file.filename
    with open(temp_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    try:
        asset = media_watcher_service.process_and_ingest_video(
            db=db,
            project_id=project_id,
            source_file_path=temp_path,
            auto_link=True
        )
    finally:
        if temp_path.exists():
            temp_path.unlink()

    return {
        "id": asset.id,
        "project_id": asset.project_id,
        "filename": asset.filename,
        "file_path": asset.file_path,
        "media_type": asset.media_type,
        "width": asset.width,
        "height": asset.height,
        "duration_seconds": asset.duration_seconds,
        "fps": asset.fps,
        "codec": asset.codec,
        "file_size_bytes": asset.file_size_bytes,
        "thumbnail_path": asset.thumbnail_path,
        "auto_matched_shot_id": asset.auto_matched_shot_id,
        "is_validated": asset.is_validated,
        "validation_notes": asset.validation_notes,
        "created_at": asset.created_at,
        "url": f"/media/{asset.file_path}"
    }

@router.post("/scan-watched/{project_id}")
def scan_watched_folder(project_id: str, db: Session = Depends(get_db)):
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    result = media_watcher_service.scan_watched_directory(db, project_id)
    return result

@router.get("/watcher-status")
def get_watcher_status():
    watch_dir = settings.watched_dir
    watch_dir.mkdir(parents=True, exist_ok=True)
    pending_files = [p.name for p in watch_dir.iterdir() if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS]

    return {
        "watch_directory_path": str(watch_dir.resolve()),
        "pending_files_count": len(pending_files),
        "pending_files": pending_files,
        "supported_extensions": list(VIDEO_EXTENSIONS)
    }

@router.post("/link-shot/{asset_id}")
def link_asset_to_shot(asset_id: str, req: LinkShotRequest, db: Session = Depends(get_db)):
    asset = db.query(MediaAsset).filter(MediaAsset.id == asset_id).first()
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")

    shot = db.query(Shot).filter(Shot.id == req.shot_id).first()
    if not shot:
        raise HTTPException(status_code=404, detail="Shot not found")

    asset.auto_matched_shot_id = shot.id
    shot.video_asset_id = asset.id
    shot.generation_status = "imported"
    shot.thumbnail_path = asset.thumbnail_path
    db.commit()

    return {"status": "linked", "asset_id": asset.id, "shot_id": shot.id}

@router.delete("/{asset_id}")
def delete_media_asset(asset_id: str, db: Session = Depends(get_db)):
    asset = db.query(MediaAsset).filter(MediaAsset.id == asset_id).first()
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")

    # Unlink any shot referencing it
    shots = db.query(Shot).filter(Shot.video_asset_id == asset.id).all()
    for s in shots:
        s.video_asset_id = None
        s.generation_status = "pending"

    # Delete physical files
    real_file = settings.data_dir / asset.file_path
    if real_file.exists():
        real_file.unlink()

    if asset.thumbnail_path:
        real_thumb = settings.data_dir / asset.thumbnail_path
        if real_thumb.exists():
            real_thumb.unlink()

    db.delete(asset)
    db.commit()
    return {"status": "deleted", "asset_id": asset_id}

@router.post("/generate-test-clip/{project_id}")
def generate_synthetic_test_clip(
    project_id: str,
    shot_code: str = Query("SC01_SH01"),
    duration: float = Query(3.0),
    db: Session = Depends(get_db)
):
    """Creates a synthetic test clip with label directly matching a shot for local testing."""
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    test_file = settings.watched_dir / f"{shot_code}_synthetic.mp4"
    ffmpeg_service.generate_synthetic_test_clip(
        output_path=test_file,
        duration=duration,
        text_label=f"FLOW VIDEO: {shot_code}"
    )

    # Ingest it immediately
    asset = media_watcher_service.process_and_ingest_video(
        db=db,
        project_id=project_id,
        source_file_path=test_file,
        auto_link=True
    )

    if test_file.exists():
        test_file.unlink()

    return {
        "status": "success",
        "asset_id": asset.id,
        "matched_shot_id": asset.auto_matched_shot_id,
        "filename": asset.filename,
        "url": f"/media/{asset.file_path}"
    }
