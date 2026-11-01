from typing import List
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from backend.app.database import get_db
from backend.app.models.entities import StudioJob, Project
from backend.app.schemas.entities import StudioJobResponse

router = APIRouter(prefix="/api/jobs", tags=["jobs"])

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
