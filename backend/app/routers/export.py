from pathlib import Path
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from backend.app.database import get_db
from backend.app.config import settings
from backend.app.models.entities import Project, ExportJob
from backend.app.schemas.entities import ExportJobCreate, ExportJobResponse
from backend.app.services.export_manager import export_manager, PRESET_CONFIGS
from backend.app.services.cost_controller import cost_controller

router = APIRouter(prefix="/api/export", tags=["export"])

@router.get("/presets")
def list_presets():
    return PRESET_CONFIGS

@router.get("/jobs/{project_id}", response_model=List[ExportJobResponse])
def list_export_jobs(project_id: str, db: Session = Depends(get_db)):
    jobs = db.query(ExportJob).filter(ExportJob.project_id == project_id).order_by(ExportJob.created_at.desc()).all()
    res = []
    for j in jobs:
        res.append({
            "id": j.id,
            "project_id": j.project_id,
            "preset_name": j.preset_name,
            "resolution": j.resolution,
            "aspect_ratio": j.aspect_ratio,
            "fps": j.fps,
            "include_subtitles": j.include_subtitles,
            "status": j.status,
            "progress_pct": j.progress_pct,
            "output_path": j.output_path,
            "file_size_bytes": j.file_size_bytes,
            "error_message": j.error_message,
            "created_at": j.created_at,
            "completed_at": j.completed_at,
            "download_url": f"/api/export/download/{j.id}" if j.status == "completed" else None
        })
    return res

@router.post("/render/{project_id}", response_model=ExportJobResponse)
def launch_export_render(project_id: str, data: ExportJobCreate, db: Session = Depends(get_db)):
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    # Authorize & charge
    cost_controller.authorize_and_charge(
        db=db,
        project_id=project.id,
        action_type="video_render",
        payload={"project_id": project.id, "preset": data.preset_name, "res": data.resolution},
        details=f"Master Export: {data.preset_name} ({data.resolution})",
        allow_duplicate=True
    )

    preset_cfg = PRESET_CONFIGS.get(data.preset_name or "1080p_cinematic", {})
    res_str = data.resolution or preset_cfg.get("resolution", "1920x1080")
    aspect_str = data.aspect_ratio or preset_cfg.get("aspect_ratio", "16:9")
    fps_val = data.fps or preset_cfg.get("fps", 24)

    job = ExportJob(
        project_id=project.id,
        preset_name=data.preset_name or "1080p_cinematic",
        resolution=res_str,
        aspect_ratio=aspect_str,
        fps=fps_val,
        include_subtitles=data.include_subtitles if data.include_subtitles is not None else True,
        status="queued",
        progress_pct=10
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    # Execute export pipeline
    completed_job = export_manager.execute_export_job(db, job.id)

    return {
        "id": completed_job.id,
        "project_id": completed_job.project_id,
        "preset_name": completed_job.preset_name,
        "resolution": completed_job.resolution,
        "aspect_ratio": completed_job.aspect_ratio,
        "fps": completed_job.fps,
        "include_subtitles": completed_job.include_subtitles,
        "status": completed_job.status,
        "progress_pct": completed_job.progress_pct,
        "output_path": completed_job.output_path,
        "file_size_bytes": completed_job.file_size_bytes,
        "error_message": completed_job.error_message,
        "created_at": completed_job.created_at,
        "completed_at": completed_job.completed_at,
        "download_url": f"/api/export/download/{completed_job.id}" if completed_job.status == "completed" else None
    }

@router.get("/download/{job_id}")
def download_export_film(job_id: str, db: Session = Depends(get_db)):
    job = db.query(ExportJob).filter(ExportJob.id == job_id).first()
    if not job or not job.output_path:
        raise HTTPException(status_code=404, detail="Export job or output file not found")

    file_path = settings.data_dir / job.output_path
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found on disk")

    return FileResponse(
        path=str(file_path),
        filename=file_path.name,
        media_type="video/mp4"
    )
