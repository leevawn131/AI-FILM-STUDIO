from typing import List, Optional
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from backend.app.database import get_db
from backend.app.models.entities import StudioJob, Project, GenerationJob
from backend.app.schemas.entities import (
    StudioJobResponse,
    GenerationJobResponse,
    GenerateImageRequest,
    GenerateVideoRequest
)
from backend.app.services.generation_job_manager import generation_job_manager
from backend.app.config import settings

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


# ==============================================================================
# Cloud & Local AI Generation Jobs (Asynchronous Google Cloud AI)
# ==============================================================================

@router.get("/generation/list/{project_id}", response_model=List[GenerationJobResponse])
def list_generation_jobs(
    project_id: str,
    scene_id: Optional[str] = Query(None),
    shot_id: Optional[str] = Query(None),
    generation_type: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db)
):
    """Lists asynchronous generation jobs for a given project with filtering."""
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    return generation_job_manager.list_jobs(
        db=db,
        project_id=project_id,
        scene_id=scene_id,
        shot_id=shot_id,
        generation_type=generation_type,
        limit=limit
    )


@router.get("/generation/detail/{job_id}", response_model=GenerationJobResponse)
def get_generation_job_detail(job_id: str, db: Session = Depends(get_db)):
    """Gets status and output details of an asynchronous AI generation job."""
    job = db.query(GenerationJob).filter(GenerationJob.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Generation job not found")
    return job


@router.post("/generation/image", response_model=GenerationJobResponse)
def submit_image_generation_job(req: GenerateImageRequest, db: Session = Depends(get_db)):
    """Submits an asynchronous image generation request (Google Imagen 3)."""
    project = db.query(Project).filter(Project.id == req.project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    job = generation_job_manager.create_job(
        db=db,
        project_id=req.project_id,
        generation_type="image_generation",
        input_parameters=req.model_dump(),
        provider="google_imagen",
        model=settings.google_image_model,
        supports_cancellation=False
    )
    generation_job_manager.launch_job_async(job.id)
    return job


@router.post("/generation/video", response_model=GenerationJobResponse)
def submit_video_generation_job(req: GenerateVideoRequest, db: Session = Depends(get_db)):
    """Submits an asynchronous video generation request (Google Veo)."""
    project = db.query(Project).filter(Project.id == req.project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    job = generation_job_manager.create_job(
        db=db,
        project_id=req.project_id,
        shot_id=req.shot_id,
        generation_type="video_generation",
        input_parameters=req.model_dump(),
        provider="google_veo",
        model=settings.google_video_model,
        supports_cancellation=False
    )
    generation_job_manager.launch_job_async(job.id)
    return job


@router.post("/generation/{job_id}/cancel")
def cancel_generation_job(job_id: str, db: Session = Depends(get_db)):
    """Attempts honest cancellation of a generation job."""
    try:
        res = generation_job_manager.cancel_job(db, job_id)
        if res.get("status") == "unsupported":
            raise HTTPException(status_code=400, detail=res.get("message"))
        return res
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/generation/{job_id}/retry", response_model=GenerationJobResponse)
def retry_generation_job(job_id: str, db: Session = Depends(get_db)):
    """Retries a failed generation job without duplicating completed work."""
    try:
        job = generation_job_manager.retry_job(db, job_id)
        return job
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


# ==============================================================================
# Persistent Studio Pipeline Jobs
# ==============================================================================

@router.get("/{project_id}", response_model=List[StudioJobResponse])
def list_project_jobs(project_id: str, db: Session = Depends(get_db)):
    """Lists persistent lifecycle jobs for a given project."""
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    jobs = db.query(StudioJob).filter(
        StudioJob.project_id == project_id
    ).order_by(StudioJob.created_at.desc()).all()
    return jobs

@router.get("/detail/{job_id}", response_model=StudioJobResponse)
def get_job_detail(job_id: str, db: Session = Depends(get_db)):
    """Gets details for a specific studio job."""
    job = db.query(StudioJob).filter(StudioJob.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job

@router.post("/{job_id}/cancel", response_model=StudioJobResponse)
def cancel_job(job_id: str, db: Session = Depends(get_db)):
    """Cancels a job if it is PENDING or RUNNING."""
    job = db.query(StudioJob).filter(StudioJob.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    if job.status in ("COMPLETED", "CANCELLED"):
        raise HTTPException(status_code=400, detail=f"Cannot cancel job in {job.status} state")

    job.status = "CANCELLED"
    job.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(job)
    return job

@router.post("/{job_id}/retry", response_model=StudioJobResponse)
def mark_job_for_retry(job_id: str, db: Session = Depends(get_db)):
    """Resets a FAILED or CANCELLED job to PENDING so it can be retried."""
    job = db.query(StudioJob).filter(StudioJob.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    if job.status not in ("FAILED", "CANCELLED"):
        raise HTTPException(status_code=400, detail=f"Cannot retry job in {job.status} state")

    job.status = "PENDING"
    job.error_message = ""
    job.internal_error = ""
    job.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(job)
    return job
