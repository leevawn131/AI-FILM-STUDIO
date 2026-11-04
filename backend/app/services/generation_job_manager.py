"""
Asynchronous Cloud and Local Generation Job Manager.
Implements:
- Background asynchronous job execution via ThreadPoolExecutor (non-blocking)
- Polling and status tracking for Google Cloud operations (Veo, Imagen, Gemini)
- Support for pending, processing, completed, failed, and cancelled statuses
- Honest cancellation semantics (never claims remote cancellation if unsupported)
- Safe idempotent retries without duplicating completed outputs
- Generation history preservation across projects, scenes, and shots
"""

import json
import time
import uuid
import logging
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, Any, Optional, List
from sqlalchemy.orm import Session

from backend.app.database import SessionLocal
from backend.app.models.entities import GenerationJob, Project, Shot, MediaAsset
from backend.app.services.google_image_adapter import google_image_adapter
from backend.app.services.google_video_adapter import google_video_adapter
from backend.app.services.ai_provider import get_ai_provider

logger = logging.getLogger("generation_job_manager")

_executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix="gen_job_worker")


class GenerationJobManager:
    @staticmethod
    def create_job(
        db: Session,
        project_id: str,
        generation_type: str,
        input_parameters: Dict[str, Any],
        provider: str = "gemini",
        model: str = "gemini-2.5-flash",
        scene_id: Optional[str] = None,
        shot_id: Optional[str] = None,
        supports_cancellation: bool = False
    ) -> GenerationJob:
        """Creates a new GenerationJob in pending status."""
        job = GenerationJob(
            id=str(uuid.uuid4()),
            project_id=project_id,
            scene_id=scene_id,
            shot_id=shot_id,
            provider=provider,
            model=model,
            generation_type=generation_type,
            input_parameters=json.dumps(input_parameters),
            status="pending",
            supports_cancellation=supports_cancellation,
            result_metadata="{}",
            error_details=""
        )
        db.add(job)
        if shot_id:
            shot = db.query(Shot).filter(Shot.id == shot_id).first()
            if shot:
                shot.generation_job_id = job.id
                shot.generation_status = "generating"
        db.commit()
        db.refresh(job)
        return job

    def launch_job_async(self, job_id: str) -> None:
        """Submits the job to the background thread pool."""
        _executor.submit(self._run_job_worker, job_id)

    def _run_job_worker(self, job_id: str) -> None:
        """Worker executing in background thread."""
        db: Session = SessionLocal()
        try:
            job = db.query(GenerationJob).filter(GenerationJob.id == job_id).first()
            if not job or job.status in ("completed", "cancelled"):
                return

            job.status = "processing"
            job.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
            db.commit()

            params = json.loads(job.input_parameters or "{}")

            if job.generation_type == "image_generation":
                self._execute_image_job(job, params, db)
            elif job.generation_type == "video_generation":
                self._execute_video_job(job, params, db)
            elif job.generation_type == "script_generation":
                self._execute_script_job(job, params, db)
            elif job.generation_type == "storyboard_breakdown":
                self._execute_storyboard_job(job, params, db)
            else:
                raise ValueError(f"Unsupported generation_type: {job.generation_type}")

        except Exception as e:
            logger.error(f"Job {job_id} failed with error: {e}", exc_info=True)
            try:
                job = db.query(GenerationJob).filter(GenerationJob.id == job_id).first()
                if job:
                    job.status = "failed"
                    job.error_details = str(e)
                    job.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
                    if job.shot_id:
                        shot = db.query(Shot).filter(Shot.id == job.shot_id).first()
                        if shot:
                            shot.generation_status = "pending"
                    db.commit()
            except Exception as commit_err:
                logger.error(f"Failed to record failure for job {job_id}: {commit_err}")
        finally:
            db.close()

    def _execute_image_job(self, job: GenerationJob, params: Dict[str, Any], db: Session) -> None:
        prompt = params.get("prompt", "")
        aspect_ratio = params.get("aspect_ratio", "16:9")
        entity_type = params.get("entity_type")
        entity_id = params.get("entity_id")

        result = google_image_adapter.generate_image(
            prompt=prompt,
            project_id=job.project_id,
            db=db,
            aspect_ratio=aspect_ratio,
            entity_type=entity_type,
            entity_id=entity_id,
            negative_prompt=params.get("negative_prompt")
        )

        job.status = "completed"
        job.result_asset_id = result.get("asset_id")
        job.result_metadata = json.dumps(result)
        job.completed_at = datetime.now(timezone.utc).replace(tzinfo=None)
        db.commit()

    def _execute_video_job(self, job: GenerationJob, params: Dict[str, Any], db: Session) -> None:
        prompt = params.get("prompt", "")
        duration_seconds = params.get("duration_seconds", 5)
        aspect_ratio = params.get("aspect_ratio", "16:9")

        # 1. Submit to Google Veo
        submission = google_video_adapter.submit_video_generation(
            prompt=prompt,
            project_id=job.project_id,
            shot_id=job.shot_id,
            db=db,
            duration_seconds=duration_seconds,
            aspect_ratio=aspect_ratio,
            negative_prompt=params.get("negative_prompt")
        )

        job.provider_operation_id = submission["provider_operation_id"]
        job.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
        db.commit()

        # 2. Poll until complete
        op_id = job.provider_operation_id
        poll_interval = 4
        max_polls = 60 # Up to 5-8 minutes

        for _ in range(max_polls):
            time.sleep(poll_interval)
            # Re-read status to verify if user tried to cancel
            db.refresh(job)
            if job.status == "cancelled":
                logger.info(f"Job {job.id} was marked cancelled while polling.")
                return

            poll_res = google_video_adapter.poll_operation_status(op_id)
            if poll_res["status"] == "completed":
                # Import completed video locally
                media_asset = google_video_adapter.import_completed_video(
                    project_id=job.project_id,
                    shot_id=job.shot_id,
                    video_bytes=poll_res.get("video_bytes"),
                    video_uri=poll_res.get("video_uri"),
                    db=db
                )
                job.status = "completed"
                job.result_asset_id = media_asset.id
                job.result_metadata = json.dumps({
                    "provider": "google_veo",
                    "model": job.model,
                    "asset_id": media_asset.id,
                    "filename": media_asset.filename,
                    "duration_seconds": media_asset.duration_seconds
                })
                job.completed_at = datetime.now(timezone.utc).replace(tzinfo=None)
                db.commit()
                return

            elif poll_res["status"] == "failed":
                job.status = "failed"
                job.error_details = poll_res.get("error", "Veo video generation failed.")
                job.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
                if job.shot_id:
                    shot = db.query(Shot).filter(Shot.id == job.shot_id).first()
                    if shot:
                        shot.generation_status = "pending"
                db.commit()
                return

            # Gradually increase poll interval up to 10 seconds
            poll_interval = min(10, poll_interval + 1)

        raise TimeoutError("Veo video generation timed out while waiting for operation completion.")

    def _execute_script_job(self, job: GenerationJob, params: Dict[str, Any], db: Session) -> None:
        from backend.app.agents.screenwriter_agent import screenwriter_agent
        res = screenwriter_agent.write_scene(
            project_title=params.get("project_title", "Untitled"),
            genre=params.get("genre", "Sci-Fi"),
            scene_number=params.get("scene_number", 1),
            prompt=params.get("prompt"),
            characters=params.get("characters"),
            location=params.get("location"),
            provider_preference=job.provider
        )
        job.status = "completed"
        job.result_metadata = json.dumps(res.get("_generation_metadata", {}))
        job.completed_at = datetime.now(timezone.utc).replace(tzinfo=None)
        db.commit()

    def _execute_storyboard_job(self, job: GenerationJob, params: Dict[str, Any], db: Session) -> None:
        from backend.app.agents.director_agent import director_agent
        shots = director_agent.breakdown_scene_to_shots(
            scene_slugline=params.get("scene_slugline", "INT. STUDIO - DAY"),
            scene_summary=params.get("scene_summary", ""),
            genre=params.get("genre", "Sci-Fi"),
            characters=params.get("characters"),
            visual_style=params.get("visual_style"),
            shot_count=params.get("shot_count", 4)
        )
        job.status = "completed"
        job.result_metadata = json.dumps({"shots_generated": len(shots)})
        job.completed_at = datetime.now(timezone.utc).replace(tzinfo=None)
        db.commit()

    @staticmethod
    def cancel_job(db: Session, job_id: str) -> Dict[str, Any]:
        """
        Attempts to cancel a generation job.
        Honest cancellation:
        - If 'pending': safely cancels local queue entry.
        - If 'processing' and remote provider does NOT support cancellation:
          Raises HTTP 400 with explanation rather than faking cancellation.
        """
        job = db.query(GenerationJob).filter(GenerationJob.id == job_id).first()
        if not job:
            raise ValueError(f"Generation job '{job_id}' not found.")

        if job.status == "completed":
            return {"status": "error", "message": "Cannot cancel a completed job."}

        if job.status == "cancelled":
            return {"status": "success", "message": "Job is already cancelled."}

        if job.status == "pending":
            job.status = "cancelled"
            job.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
            if job.shot_id:
                shot = db.query(Shot).filter(Shot.id == job.shot_id).first()
                if shot and shot.generation_status == "generating":
                    shot.generation_status = "pending"
            db.commit()
            return {"status": "success", "message": "Pending job cancelled successfully."}

        # If job is actively processing:
        if not job.supports_cancellation:
            return {
                "status": "unsupported",
                "message": (
                    f"Remote provider '{job.provider}' does not support in-flight cancellation for "
                    f"operation '{job.provider_operation_id or job.id}'. The operation must complete or fail remotely."
                )
            }

        job.status = "cancelled"
        job.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
        db.commit()
        return {"status": "success", "message": "Job marked cancelled."}

    def retry_job(self, db: Session, job_id: str) -> GenerationJob:
        """
        Retries a failed or cancelled job.
        If already completed, returns the existing job without creating duplicates.
        """
        job = db.query(GenerationJob).filter(GenerationJob.id == job_id).first()
        if not job:
            raise ValueError(f"Generation job '{job_id}' not found.")

        if job.status == "completed":
            # Idempotently return completed job
            return job

        job.status = "pending"
        job.error_details = ""
        job.retry_count = (job.retry_count or 0) + 1
        job.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
        db.commit()

        self.launch_job_async(job.id)
        return job

    @staticmethod
    def list_jobs(
        db: Session,
        project_id: str,
        scene_id: Optional[str] = None,
        shot_id: Optional[str] = None,
        generation_type: Optional[str] = None,
        limit: int = 50
    ) -> List[GenerationJob]:
        """Returns generation jobs history."""
        query = db.query(GenerationJob).filter(GenerationJob.project_id == project_id)
        if scene_id:
            query = query.filter(GenerationJob.scene_id == scene_id)
        if shot_id:
            query = query.filter(GenerationJob.shot_id == shot_id)
        if generation_type:
            query = query.filter(GenerationJob.generation_type == generation_type)
        return query.order_by(GenerationJob.created_at.desc()).limit(limit).all()


generation_job_manager = GenerationJobManager()
