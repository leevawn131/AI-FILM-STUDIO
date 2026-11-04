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
from backend.app.services.security import validate_safe_path

router = APIRouter(prefix="/api/export", tags=["export"])


def _format_export_response(j: ExportJob) -> dict:
    return {
        "id": j.id,
        "project_id": j.project_id,
        "preset_name": j.preset_name,
        "render_mode": j.render_mode or "final",
        "allow_placeholders": bool(j.allow_placeholders),
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
    }


@router.get("/presets")
def list_presets():
    return PRESET_CONFIGS


@router.get("/jobs/{project_id}", response_model=List[ExportJobResponse])
def list_export_jobs(project_id: str, db: Session = Depends(get_db)):
    jobs = db.query(ExportJob).filter(ExportJob.project_id == project_id).order_by(ExportJob.created_at.desc()).all()
    return [_format_export_response(j) for j in jobs]


@router.get("/job/{job_id}", response_model=ExportJobResponse)
def get_export_job(job_id: str, db: Session = Depends(get_db)):
    job = db.query(ExportJob).filter(ExportJob.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Export job not found")
    return _format_export_response(job)


@router.post("/render/{project_id}", response_model=ExportJobResponse)
def launch_export_render(
    project_id: str,
    data: ExportJobCreate,
    async_mode: bool = Query(False, alias="async", description="Whether to run in background thread pool"),
    sync: Optional[bool] = Query(None, description="Legacy sync flag"),
    db: Session = Depends(get_db)
):
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    # Authorize & charge ledger
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

    preset_is_preview = (data.preset_name == "720p_fast")
    resolved_render_mode = data.render_mode or ("preview" if preset_is_preview else "final")
    allow_placeholders_val = bool(data.allow_placeholders) or (resolved_render_mode == "preview")

    job = ExportJob(
        project_id=project.id,
        preset_name=data.preset_name or "1080p_cinematic",
        render_mode=resolved_render_mode,
        allow_placeholders=allow_placeholders_val,
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

    run_async = async_mode or (sync is False)
    if not run_async:
        completed_job = export_manager.execute_export_job(db, job.id)
        return _format_export_response(completed_job)

    # Launch in local background thread pool and return immediately
    export_manager.submit_export_job_async(job.id)
    return _format_export_response(job)


@router.post("/job/{job_id}/cancel")
def cancel_export(job_id: str, db: Session = Depends(get_db)):
    success = export_manager.cancel_export_job(db, job_id)
    if not success:
        raise HTTPException(status_code=400, detail="Cannot cancel job in current status.")
    return {"status": "cancelled", "job_id": job_id}


@router.post("/job/{job_id}/retry", response_model=ExportJobResponse)
def retry_export(job_id: str, db: Session = Depends(get_db)):
    job = db.query(ExportJob).filter(ExportJob.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Export job not found")

    job.status = "queued"
    job.progress_pct = 10
    job.error_message = ""
    db.commit()
    db.refresh(job)

    export_manager.submit_export_job_async(job.id)
    return _format_export_response(job)


@router.get("/download/{job_id}")
def download_export_film(job_id: str, db: Session = Depends(get_db)):
    job = db.query(ExportJob).filter(ExportJob.id == job_id).first()
    if not job or not job.output_path:
        raise HTTPException(status_code=404, detail="Export job or output file not found")

    file_path = validate_safe_path(settings.data_dir / job.output_path, settings.data_dir)
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found on disk")

    return FileResponse(
        path=str(file_path),
        filename=file_path.name,
        media_type="video/mp4"
    )
