from typing import List, Optional
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, Query, Body
from sqlalchemy.orm import Session
from pydantic import BaseModel

from backend.app.database import get_db
from backend.app.models.entities import StudioJob, Project, GenerationJob
from backend.app.schemas.entities import (
    StudioJobResponse,
    GenerationJobResponse,
    GenerateImageRequest,
    GenerateVideoRequest,
    GenerateTTSRequest,
    GenerateMusicRequest
)
from backend.app.services.generation_job_manager import (
    generation_job_manager,
    DuplicateGenerationRiskError,
)
from backend.app.services.google_image_adapter import get_supported_image_sizes_for_model
from backend.app.services.google_video_adapter import SUPPORTED_VIDEO_MODELS
from backend.app.config import settings

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


class RetryJobRequest(BaseModel):
    acknowledge_new_generation: bool = False


class ReconcileJobRequest(BaseModel):
    remote_status: Optional[str] = None
    notes: Optional[str] = None


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
    """Submits an asynchronous image generation request (Google Nano Banana)."""
    project = db.query(Project).filter(Project.id == req.project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    tier = (req.tier or "standard").strip().lower()
    if req.model:
        target_model = req.model.strip()
    elif tier == "pro":
        target_model = settings.google_image_pro_model
    elif tier == "lite":
        target_model = settings.google_image_lite_model
    else:
        target_model = settings.google_image_model

    try:
        supported_sizes = get_supported_image_sizes_for_model(target_model)
        req_size = (req.image_size or "1K").strip()
        if req_size not in supported_sizes:
            raise ValueError(
                f"Image size '{req_size}' is not supported by '{target_model}'. Supported: {supported_sizes}"
            )
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))

    job = generation_job_manager.create_job(
        db=db,
        project_id=req.project_id,
        generation_type="image_generation",
        input_parameters=req.model_dump(),
        provider="google_nano_banana",
        model=target_model,
        supports_cancellation=False
    )
    generation_job_manager.launch_job_async(job.id)
    return job


@router.post("/generation/video", response_model=GenerationJobResponse)
def submit_video_generation_job(req: GenerateVideoRequest, db: Session = Depends(get_db)):
    """Submits an asynchronous video generation request (Google Gemini Omni 1.2 Flash)."""
    project = db.query(Project).filter(Project.id == req.project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    target_model = (req.model or settings.google_video_model).strip()
    if target_model not in SUPPORTED_VIDEO_MODELS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported video model '{target_model}'. Supported: {list(SUPPORTED_VIDEO_MODELS)}"
        )

    job = generation_job_manager.create_job(
        db=db,
        project_id=req.project_id,
        shot_id=req.shot_id,
        generation_type="video_generation",
        input_parameters=req.model_dump(),
        provider="google_omni",
        model=target_model,
        supports_cancellation=True
    )
    generation_job_manager.launch_job_async(job.id)
    return job


@router.post("/generation/tts", response_model=GenerationJobResponse)
def submit_tts_generation_job(req: GenerateTTSRequest, db: Session = Depends(get_db)):
    """Submits an asynchronous dialogue speech generation request (Google Gemini TTS)."""
    project = db.query(Project).filter(Project.id == req.project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    job = generation_job_manager.create_job(
        db=db,
        project_id=req.project_id,
        scene_id=req.scene_id,
        shot_id=req.shot_id,
        generation_type="tts_generation",
        input_parameters=req.model_dump(),
        provider="google_tts",
        model=req.model or settings.google_tts_model,
        supports_cancellation=False
    )
    generation_job_manager.launch_job_async(job.id)
    return job


@router.post("/generation/music", response_model=GenerationJobResponse)
def submit_music_generation_job(req: GenerateMusicRequest, db: Session = Depends(get_db)):
    """Submits an asynchronous music score generation request (Google Lyria 3.5)."""
    project = db.query(Project).filter(Project.id == req.project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    job = generation_job_manager.create_job(
        db=db,
        project_id=req.project_id,
        scene_id=req.scene_id,
        generation_type="music_generation",
        input_parameters=req.model_dump(),
        provider="google_lyria",
        model=req.model or settings.google_music_model,
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
def retry_generation_job(
    job_id: str,
    req: Optional[RetryJobRequest] = Body(default=None),
    acknowledge_new_generation: bool = Query(default=False),
    db: Session = Depends(get_db),
):
    """
    Retries a failed or recoverable generation job without duplicating completed work.
    Blocks automatic retry of `provider_outcome_unknown` jobs lacking a `provider_operation_id`
    with HTTP 409 unless `acknowledge_new_generation=True`.
    """
    ack = acknowledge_new_generation or (req.acknowledge_new_generation if req else False)
    try:
        job = generation_job_manager.retry_job(db, job_id, acknowledge_new_generation=ack)
        return job
    except DuplicateGenerationRiskError as conflict:
        raise HTTPException(status_code=409, detail=str(conflict))
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/generation/{job_id}/reconcile", response_model=GenerationJobResponse)
def reconcile_generation_job(
    job_id: str,
    req: Optional[ReconcileJobRequest] = Body(default=None),
    db: Session = Depends(get_db),
):
    """
    Reconciles a job with `provider_outcome_unknown` (via remote polling if `provider_operation_id`
    is known, or explicit operator verification override when `provider_operation_id` is absent).
    """
    try:
        return generation_job_manager.reconcile_job(
            db=db,
            job_id=job_id,
            remote_status=req.remote_status if req else None,
            notes=req.notes if req else None,
        )
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
