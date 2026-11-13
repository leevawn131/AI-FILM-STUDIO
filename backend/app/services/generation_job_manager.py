"""
Asynchronous Cloud and Local Generation Job Manager.
Implements:
- Background asynchronous job execution via ThreadPoolExecutor (non-blocking)
- Polling and status tracking for Google Cloud operations:
  * Gemini 3.8 Flash (script generation & storyboard breakdown)
  * Nano Banana 2 (image generation)
  * Gemini Omni Flash 1.1 (video generation via Interactions API)
  * Gemini TTS (speech synthesis)
  * Lyria 3.5 (music generation via Interactions API)
- Support for pending, processing, completed, failed, and cancelled statuses
- Honest cancellation semantics (never claims remote cancellation if unsupported)
- Safe idempotent retries without duplicating completed outputs
- Generation history preservation across projects, scenes, and shots
- Resumable tracking across studio restarts
"""

import json
import time
import uuid
import logging
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, Any, Optional, List
from sqlalchemy.orm import Session

from backend.app.config import settings
from backend.app.database import SessionLocal
from backend.app.models.entities import GenerationJob, Project, Shot, MediaAsset
from backend.app.services.google_image_adapter import (
    google_image_adapter,
    get_supported_image_sizes_for_model,
)
from backend.app.services.google_video_adapter import (
    google_video_adapter,
)
from backend.app.services.google_tts_adapter import google_tts_adapter, SUPPORTED_VOICES
from backend.app.services.google_music_adapter import google_music_adapter
from backend.app.services.google_model_catalog import (
    estimate_video_cost_usd,
    estimate_music_cost_usd,
)
from backend.app.services.provider_error_classifier import classify_provider_exception
from backend.app.services.cost_controller import cost_controller, CostController
from backend.app.services.ai_provider import get_ai_provider

logger = logging.getLogger("generation_job_manager")

_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="gen_job_worker")


class DuplicateGenerationRiskError(ValueError):
    """Raised when retrying an unknown-outcome job without an operation ID requires explicit acknowledgement."""
    pass


class GenerationJobManager:
    """
    Durable asynchronous generation job manager enforcing billing safety:
    - Persists `dispatch_state='dispatch_started'` BEFORE calling remote Google endpoints.
    - `video_generation` tracks `provider_operation_id` (`provider_operation_known`) for crash recovery.
    - Missing operation IDs or mid-dispatch crashes transition to `provider_outcome_unknown` and
      preserve budget reservations without automatic retries.
    """

    @staticmethod
    def create_job(
        db: Session,
        project_id: str,
        generation_type: str,
        input_parameters: Dict[str, Any],
        provider: str = "gemini",
        model: Optional[str] = None,
        scene_id: Optional[str] = None,
        shot_id: Optional[str] = None,
        supports_cancellation: Optional[bool] = None
    ) -> GenerationJob:
        """Creates a new GenerationJob in pending/not_dispatched status."""
        resolved_model = model
        if not resolved_model:
            if generation_type == "image_generation":
                resolved_model = settings.google_image_model
            elif generation_type == "video_generation":
                resolved_model = settings.google_video_model
            elif generation_type == "tts_generation":
                resolved_model = settings.google_tts_model
            elif generation_type == "music_generation":
                resolved_model = settings.google_music_model
            else:
                resolved_model = settings.gemini_model

        resolved_cancel = supports_cancellation
        if resolved_cancel is None:
            resolved_cancel = generation_type == "video_generation"

        job = GenerationJob(
            id=str(uuid.uuid4()),
            project_id=project_id,
            scene_id=scene_id,
            shot_id=shot_id,
            provider=provider,
            model=resolved_model,
            generation_type=generation_type,
            input_parameters=json.dumps(input_parameters),
            status="pending",
            dispatch_state="not_dispatched",
            supports_cancellation=resolved_cancel,
            result_metadata="{}",
            error_details=""
        )
        db.add(job)
        if shot_id and generation_type == "video_generation":
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
            elif job.generation_type == "tts_generation":
                self._execute_tts_job(job, params, db)
            elif job.generation_type == "music_generation":
                self._execute_music_job(job, params, db)
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
                    active_res = cost_controller.get_active_job_reservation(db, job.id, job.project_id)
                    if active_res and active_res.action_type == "provider_outcome_unknown":
                        job.status = "provider_outcome_unknown"
                        job.dispatch_state = "provider_outcome_unknown"
                    elif active_res and active_res.action_type not in ("budget_reservation", "refund"):
                        # Reservation finalized operationally despite downstream exception -> local failure
                        job.status = "failed"
                        job.dispatch_state = "local_processing_failed"
                    elif job.status != "provider_outcome_unknown":
                        job.status = "failed"
                        if job.dispatch_state == "dispatch_started":
                            classified = classify_provider_exception(e)
                            if classified.is_outcome_unknown:
                                if active_res:
                                    cost_controller.finalize_reservation(
                                        db=db,
                                        project_id=job.project_id,
                                        reservation_record=active_res,
                                        local_processing_status="provider_outcome_unknown",
                                        error_details=classified.reason,
                                    )
                                job.status = "provider_outcome_unknown"
                                job.dispatch_state = "provider_outcome_unknown"
                            else:
                                if active_res:
                                    cost_controller.release_reservation(
                                        db=db,
                                        project_id=job.project_id,
                                        reservation_record=active_res,
                                        reason=f"provider_rejected: {classified.reason}",
                                    )
                                job.dispatch_state = "provider_failed"
                    job.error_details = str(e)
                    job.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
                    if job.shot_id and job.generation_type == "video_generation":
                        shot = db.query(Shot).filter(Shot.id == job.shot_id).first()
                        if shot and shot.generation_status == "generating":
                            shot.generation_status = "pending"
                    db.commit()
            except Exception as commit_err:
                logger.error(f"Failed to record failure for job {job_id}: {commit_err}")
        finally:
            db.close()

    def _execute_image_job(self, job: GenerationJob, params: Dict[str, Any], db: Session) -> None:
        prompt = params.get("prompt", "")
        aspect_ratio = params.get("aspect_ratio", "16:9")
        image_size = params.get("image_size", "1K")
        entity_type = params.get("entity_type")
        entity_id = params.get("entity_id")
        tier = params.get("tier", "default")

        reservation = None
        if google_image_adapter.is_available:
            target_model, est_cost = google_image_adapter._resolve_model(
                model=job.model,
                tier=tier,
                image_size=image_size,
            )
            supported_sizes = get_supported_image_sizes_for_model(target_model)
            if image_size not in supported_sizes:
                raise ValueError(
                    f"Image size '{image_size}' is not supported by model '{target_model}'. "
                    f"Supported sizes: {supported_sizes}"
                )
            reservation = cost_controller.reserve_budget(
                db=db,
                project_id=job.project_id,
                estimated_cost=est_cost,
                operation="image_generation",
                provider="google_nano_banana",
                model=target_model,
                scene_id=job.scene_id,
                shot_id=job.shot_id,
                job_id=job.id,
                details="Pre-flight budget reservation for image generation"
            )
            job.dispatch_state = "dispatch_started"
            db.commit()

        result = google_image_adapter.generate_image(
            prompt=prompt,
            project_id=job.project_id,
            db=db,
            aspect_ratio=aspect_ratio,
            image_size=image_size,
            entity_type=entity_type,
            entity_id=entity_id,
            negative_prompt=params.get("negative_prompt"),
            model=job.model,
            tier=tier,
            reservation_record=reservation,
            job_id=job.id
        )

        job.status = "completed"
        job.dispatch_state = "not_dispatched" if result.get("is_fallback") else "provider_completed"
        job.result_asset_id = result.get("asset_id")
        job.provider_operation_id = result.get("provider_operation_id") or result.get("interaction_id")
        job.result_metadata = json.dumps(result)
        job.completed_at = datetime.now(timezone.utc).replace(tzinfo=None)
        db.commit()

    def _execute_video_job(self, job: GenerationJob, params: Dict[str, Any], db: Session) -> None:
        """
        Executes Gemini Omni Flash video generation cleanly split into:
        1. Recovery mode (when `job.provider_operation_id` already exists):
           Reuses any existing reservation without duplicating it, skips `submit_video_generation`,
           and resumes polling the existing remote operation.
        2. New generation mode:
           Reserves pre-flight budget, durably persists `dispatch_state='dispatch_started'` BEFORE
           calling Google, submits once to provider, persists `provider_operation_id` +
           `provider_operation_known`, then polls and finalizes or releases strictly per provider outcome.
        """
        prompt = params.get("prompt", "")
        duration_seconds = params.get("duration_seconds", 5)
        aspect_ratio = params.get("aspect_ratio", "16:9")
        task = params.get("task", "text_to_video")
        resolution = params.get("resolution", "720p")
        reference_image_paths = params.get("reference_image_paths")

        if job.provider_operation_id:
            # Recovery mode: do NOT submit a second paid request and do NOT create a duplicate reservation
            op_id = job.provider_operation_id
            job.dispatch_state = "provider_operation_known"
            reservation = cost_controller.get_active_job_reservation(
                db=db,
                job_id=job.id,
                project_id=job.project_id
            )
            db.commit()
            logger.info(
                f"Recovering video job {job.id} using existing provider_operation_id={op_id} "
                f"(reservation={'found' if reservation else 'none'})"
            )
        else:
            if getattr(job, "dispatch_state", "not_dispatched") in ("dispatch_started", "provider_outcome_unknown"):
                raise DuplicateGenerationRiskError(
                    f"Refusing automatic remote video submission for job '{job.id}' in "
                    f"dispatch_state='{job.dispatch_state}' without a provider_operation_id."
                )

            # New generation mode: reserve budget & commit dispatch_started BEFORE calling provider
            est_cost = estimate_video_cost_usd(job.model, duration_seconds=duration_seconds, resolution=resolution)
            reservation = cost_controller.reserve_budget(
                db=db,
                project_id=job.project_id,
                estimated_cost=est_cost,
                operation="video_generation",
                provider="google_omni",
                model=job.model,
                scene_id=job.scene_id,
                shot_id=job.shot_id,
                job_id=job.id,
                details="Pre-flight budget reservation for video generation"
            )
            job.dispatch_state = "dispatch_started"
            job.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
            db.commit()

            try:
                submission = google_video_adapter.submit_video_generation(
                    prompt=prompt,
                    project_id=job.project_id,
                    shot_id=job.shot_id,
                    db=db,
                    duration_seconds=duration_seconds,
                    aspect_ratio=aspect_ratio,
                    resolution=resolution,
                    task=task,
                    input_image_path=params.get("input_image_path"),
                    input_video_path=params.get("input_video_path"),
                    reference_image_paths=reference_image_paths,
                    model=job.model,
                    negative_prompt=params.get("negative_prompt")
                )
                op_id = submission["provider_operation_id"]
                job.provider_operation_id = op_id
                job.dispatch_state = "provider_operation_known"
                job.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
                db.commit()
            except Exception as sub_err:
                classified = classify_provider_exception(sub_err)
                if classified.is_outcome_unknown:
                    cost_controller.finalize_reservation(
                        db=db,
                        project_id=job.project_id,
                        reservation_record=reservation,
                        local_processing_status="provider_outcome_unknown",
                        error_details=f"Dispatch outcome unknown: {classified.reason}"
                    )
                    job.status = "provider_outcome_unknown"
                    job.dispatch_state = "provider_outcome_unknown"
                    job.error_details = f"Video submission outcome unknown (reservation preserved): {sub_err}"
                else:
                    cost_controller.release_reservation(
                        db=db,
                        project_id=job.project_id,
                        reservation_record=reservation,
                        reason=f"provider_submission_rejected: {classified.reason}"
                    )
                    job.status = "failed"
                    job.dispatch_state = "provider_failed"
                    job.error_details = f"Video submission rejected: {sub_err}"
                job.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
                if job.shot_id:
                    shot = db.query(Shot).filter(Shot.id == job.shot_id).first()
                    if shot and shot.generation_status == "generating":
                        shot.generation_status = "pending"
                db.commit()
                return

        # Shared polling loop (both new submission and recovery converge here)
        poll_interval = 4
        max_polls = 60  # Up to 5-8 minutes

        for _ in range(max_polls):
            time.sleep(poll_interval)
            db.refresh(job)
            if job.status == "cancelled":
                logger.info(f"Job {job.id} was marked cancelled while polling.")
                if reservation is not None:
                    cost_controller.release_reservation(
                        db=db,
                        project_id=job.project_id,
                        reservation_record=reservation,
                        reason="cancelled_by_user",
                        allow_unknown_release=True
                    )
                    db.commit()
                return

            try:
                poll_res = google_video_adapter.poll_operation_status(op_id)
            except Exception as poll_err:
                logger.warning(f"Polling error for operation '{op_id}' on job {job.id}: {poll_err}")
                poll_interval = min(10, poll_interval + 1)
                continue

            if poll_res["status"] == "completed":
                # Provider generation outcome: SUCCEEDED
                try:
                    media_asset = google_video_adapter.import_completed_video(
                        project_id=job.project_id,
                        shot_id=job.shot_id,
                        video_bytes=poll_res.get("video_bytes"),
                        video_uri=poll_res.get("video_uri"),
                        db=db,
                        audio_bytes=poll_res.get("audio_bytes"),
                        audio_uri=poll_res.get("audio_uri"),
                        reservation_record=reservation,
                        raise_on_processing_error=True
                    )
                    # Local media processing outcome: SUCCEEDED
                    job.status = "completed"
                    job.dispatch_state = "provider_completed"
                    job.result_asset_id = media_asset.id
                    job.result_metadata = json.dumps({
                        "provider": "google_omni",
                        "model": job.model,
                        "task": task,
                        "asset_id": media_asset.id,
                        "filename": media_asset.filename,
                        "duration_seconds": media_asset.duration_seconds
                    })
                    job.completed_at = datetime.now(timezone.utc).replace(tzinfo=None)
                    db.commit()
                    return
                except Exception as local_err:
                    # Local media processing outcome: FAILED
                    # Incurred provider expense is preserved and NOT refunded
                    logger.error(f"Provider generation succeeded, but local media processing failed: {local_err}")
                    if reservation is not None:
                        cost_controller.finalize_reservation(
                            db=db,
                            project_id=job.project_id,
                            reservation_record=reservation,
                            actual_cost=None,
                            actual_cost_known=False,
                            local_processing_status="failed",
                            error_details=str(local_err)
                        )
                    job.status = "failed"
                    job.dispatch_state = "local_processing_failed"
                    job.error_details = f"Google Omni Flash generated video successfully, but local media processing failed: {local_err}"
                    job.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
                    if job.shot_id:
                        shot = db.query(Shot).filter(Shot.id == job.shot_id).first()
                        if shot:
                            shot.generation_status = "pending"
                    db.commit()
                    return

            elif poll_res["status"] == "output_missing":
                # Provider marked operation completed without retrievable output: NEVER refund!
                if reservation is not None:
                    cost_controller.finalize_reservation(
                        db=db,
                        project_id=job.project_id,
                        reservation_record=reservation,
                        actual_cost=None,
                        actual_cost_known=False,
                        local_processing_status="output_unretrieved",
                        error_details=poll_res.get("error", "Completed operation returned no output video")
                    )
                job.status = "failed"
                job.dispatch_state = "local_processing_failed"
                job.error_details = poll_res.get("error", "Completed operation returned no video bytes or URI.")
                job.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
                if job.shot_id:
                    shot = db.query(Shot).filter(Shot.id == job.shot_id).first()
                    if shot:
                        shot.generation_status = "pending"
                db.commit()
                return

            elif poll_res["status"] == "failed":
                if reservation is not None:
                    cost_controller.release_reservation(
                        db=db,
                        project_id=job.project_id,
                        reservation_record=reservation,
                        reason=f"provider_failed: {poll_res.get('error', '')}",
                        allow_unknown_release=True
                    )
                job.status = "failed"
                job.dispatch_state = "provider_failed"
                job.error_details = f"{poll_res.get('error', 'Omni Flash video generation failed remotely.')} [failed_op:{op_id}]"
                # Clear terminal failed provider operation ID so manual retry creates a new generation
                job.provider_operation_id = None
                job.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
                if job.shot_id:
                    shot = db.query(Shot).filter(Shot.id == job.shot_id).first()
                    if shot:
                        shot.generation_status = "pending"
                db.commit()
                return

            elif poll_res["status"] == "cancelled":
                if reservation is not None:
                    cost_controller.release_reservation(
                        db=db,
                        project_id=job.project_id,
                        reservation_record=reservation,
                        reason="provider_cancelled",
                        allow_unknown_release=True
                    )
                job.status = "cancelled"
                job.dispatch_state = "provider_failed"
                job.provider_operation_id = None
                job.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
                if job.shot_id:
                    shot = db.query(Shot).filter(Shot.id == job.shot_id).first()
                    if shot:
                        shot.generation_status = "pending"
                db.commit()
                return

            poll_interval = min(10, poll_interval + 1)

        # Polling timeout: provider status remains in-flight or unknown.
        # NEVER release reservation on polling timeout! Keep provider_operation_id for recovery/retry.
        if reservation is not None:
            cost_controller.finalize_reservation(
                db=db,
                project_id=job.project_id,
                reservation_record=reservation,
                local_processing_status="provider_outcome_unknown",
                error_details=f"Local polling timed out while operation '{op_id}' remained in-flight or unknown"
            )
        job.status = "provider_outcome_unknown"
        job.dispatch_state = "provider_outcome_unknown"
        job.error_details = (
            f"Gemini Omni Flash polling timed out for operation '{op_id}'; provider outcome is unknown "
            f"and budget reservation is preserved. Retry to resume polling."
        )
        job.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
        if job.shot_id:
            shot = db.query(Shot).filter(Shot.id == job.shot_id).first()
            if shot and shot.generation_status == "generating":
                shot.generation_status = "pending"
        db.commit()
        return

    def _execute_tts_job(self, job: GenerationJob, params: Dict[str, Any], db: Session) -> None:
        """Executes single-speaker or multi-speaker Gemini TTS with pre-flight budget reservation."""
        dialogue_turns = params.get("dialogue_turns")
        target_model = job.model or google_tts_adapter.model
        per_speaker = 0.005 if "lite" in target_model.lower() else 0.01

        if dialogue_turns and not params.get("split_turns", False):
            valid_turns = [t for t in dialogue_turns if str(t.get("text", "")).strip()]
            unique_speakers = []
            seen = set()
            for t in valid_turns:
                spk = str(t.get("speaker") or "Speaker").strip() or "Speaker"
                if spk not in seen:
                    seen.add(spk)
                    unique_speakers.append(spk)
            if len(unique_speakers) > 2:
                raise ValueError(
                    f"Google Gemini multi-speaker TTS supports at most 2 speakers per request "
                    f"(got {len(unique_speakers)})."
                )
            reservation = None
            if google_tts_adapter.is_available and valid_turns:
                est_cost = per_speaker * len(unique_speakers)
                reservation = cost_controller.reserve_budget(
                    db=db,
                    project_id=job.project_id,
                    estimated_cost=est_cost,
                    operation="tts_generation",
                    provider="google_tts",
                    model=target_model,
                    scene_id=job.scene_id,
                    shot_id=job.shot_id,
                    job_id=job.id,
                    details="Pre-flight budget reservation for multi-speaker TTS generation"
                )
                job.dispatch_state = "dispatch_started"
                db.commit()

            result = google_tts_adapter.generate_multi_speaker_dialogue(
                dialogue_turns=dialogue_turns,
                project_id=job.project_id,
                db=db,
                scene_id=job.scene_id,
                shot_id=job.shot_id,
                model=job.model,
                split_turns=False,
                reservation_record=reservation,
                job_id=job.id
            )
        else:
            text = params.get("text") or params.get("dialogue_text", "")
            voice_name = params.get("voice_name", "Puck")
            character_name = params.get("character_name")
            character_id = params.get("character_id")
            emotion = params.get("emotion", "neutral")
            speaking_pace = float(params.get("speaking_pace", 1.0))
            language_code = params.get("language_code", "en-US")
            track_type = params.get("track_type", "dialogue")

            reservation = None
            if google_tts_adapter.is_available:
                reservation = cost_controller.reserve_budget(
                    db=db,
                    project_id=job.project_id,
                    estimated_cost=per_speaker,
                    operation="tts_generation",
                    provider="google_tts",
                    model=target_model,
                    scene_id=job.scene_id,
                    shot_id=job.shot_id,
                    job_id=job.id,
                    details="Pre-flight budget reservation for TTS generation"
                )
                job.dispatch_state = "dispatch_started"
                db.commit()

            result = google_tts_adapter.generate_dialogue_speech(
                text=text,
                project_id=job.project_id,
                db=db,
                voice_name=voice_name,
                character_name=character_name,
                character_id=character_id,
                language_code=language_code,
                emotion=emotion,
                speaking_pace=speaking_pace,
                scene_id=job.scene_id,
                shot_id=job.shot_id,
                track_type=track_type,
                model=job.model,
                reservation_record=reservation,
                job_id=job.id
            )

        job.status = "completed"
        job.dispatch_state = "not_dispatched" if result.get("is_fallback") else "provider_completed"
        job.result_asset_id = result.get("asset_id")
        job.provider_operation_id = result.get("provider_operation_id") or result.get("interaction_id")
        job.result_metadata = json.dumps(result)
        job.completed_at = datetime.now(timezone.utc).replace(tzinfo=None)
        db.commit()

    def _execute_music_job(self, job: GenerationJob, params: Dict[str, Any], db: Session) -> None:
        """Executes single-request Lyria music score generation with pre-flight budget reservation."""
        mood = params.get("mood", "atmospheric")
        genre = params.get("genre", "cinematic synthwave")
        bpm = int(params.get("bpm", 110))
        instruments = params.get("instruments")
        energy = params.get("energy", "medium")
        duration_seconds = int(params.get("duration_seconds", 30))
        instrumental = params.get("instrumental", True)
        prompt = params.get("prompt")
        target_model = job.model or google_music_adapter.model

        reservation = None
        if google_music_adapter.is_available:
            reservation = cost_controller.reserve_budget(
                db=db,
                project_id=job.project_id,
                estimated_cost=estimate_music_cost_usd(target_model),
                operation="music_generation",
                provider="google_lyria",
                model=target_model,
                scene_id=job.scene_id,
                shot_id=job.shot_id,
                job_id=job.id,
                details="Pre-flight budget reservation for music generation"
            )
            job.dispatch_state = "dispatch_started"
            db.commit()

        result = google_music_adapter.generate_music_score(
            project_id=job.project_id,
            db=db,
            mood=mood,
            genre=genre,
            bpm=bpm,
            instruments=instruments,
            energy=energy,
            duration_seconds=duration_seconds,
            instrumental=instrumental,
            scene_id=job.scene_id,
            prompt=prompt,
            model=job.model,
            reservation_record=reservation,
            job_id=job.id
        )

        job.status = "completed"
        job.dispatch_state = "not_dispatched" if result.get("is_fallback") else "provider_completed"
        job.result_asset_id = result.get("asset_id")
        job.provider_operation_id = result.get("provider_operation_id") or result.get("interaction_id")
        job.result_metadata = json.dumps(result)
        job.completed_at = datetime.now(timezone.utc).replace(tzinfo=None)
        db.commit()

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
        Honest cancellation semantics:
        - If 'pending': safely cancels local queue entry.
        - If 'processing' and remote provider supports cancellation:
          calls provider cancellation API.
        - If remote provider does NOT support cancellation:
          returns unsupported explanation without faking.
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
            if job.shot_id and job.generation_type == "video_generation":
                shot = db.query(Shot).filter(Shot.id == job.shot_id).first()
                if shot and shot.generation_status == "generating":
                    shot.generation_status = "pending"
            active_res = cost_controller.get_active_job_reservation(db, job.id, job.project_id)
            if active_res and active_res.action_type == "budget_reservation":
                cost_controller.release_reservation(
                    db=db,
                    project_id=job.project_id,
                    reservation_record=active_res,
                    reason="pending_job_cancelled",
                )
            db.commit()
            return {"status": "success", "message": "Pending job cancelled successfully."}

        # If job is actively processing:
        if job.generation_type == "video_generation" and job.provider_operation_id:
            cancel_res = google_video_adapter.cancel_operation(job.provider_operation_id)
            if cancel_res.get("status") == "cancelled":
                job.status = "cancelled"
                job.dispatch_state = "provider_failed"
                job.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
                if job.shot_id:
                    shot = db.query(Shot).filter(Shot.id == job.shot_id).first()
                    if shot and shot.generation_status == "generating":
                        shot.generation_status = "pending"
                active_res = cost_controller.get_active_job_reservation(db, job.id, job.project_id)
                if active_res:
                    cost_controller.release_reservation(
                        db=db,
                        project_id=job.project_id,
                        reservation_record=active_res,
                        reason="remote_operation_cancelled",
                        allow_unknown_release=True,
                    )
                db.commit()
                return {"status": "success", "message": "Remote operation cancelled successfully."}

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

    def retry_job(
        self,
        db: Session,
        job_id: str,
        acknowledge_new_generation: bool = False
    ) -> GenerationJob:
        """
        Retries a failed, timed-out (`provider_outcome_unknown`), or cancelled job safely:
        - If already completed or currently in-flight (`pending` / `processing`), returns existing job.
        - If `provider_operation_id` IS known, resumes polling ONLY without creating duplicate remote calls.
        - CRITICAL BILLING SAFETY: If `dispatch_state` / `status` indicates `provider_outcome_unknown` or
          `dispatch_started` WITHOUT a `provider_operation_id`, automatic retry is BLOCKED
          (`DuplicateGenerationRiskError`) unless `acknowledge_new_generation=True` is explicitly supplied!
        """
        job = db.query(GenerationJob).filter(GenerationJob.id == job_id).first()
        if not job:
            raise ValueError(f"Generation job '{job_id}' not found.")

        if job.status in ("completed", "pending", "processing"):
            return job

        dispatch_state = getattr(job, "dispatch_state", "not_dispatched") or "not_dispatched"
        unknown_without_op_id = (
            (job.status == "provider_outcome_unknown" or dispatch_state in ("dispatch_started", "provider_outcome_unknown"))
            and not job.provider_operation_id
        )
        if unknown_without_op_id and not acknowledge_new_generation:
            raise DuplicateGenerationRiskError(
                f"Job '{job.id}' is in '{job.status}' ({dispatch_state}) without a provider_operation_id. "
                "Automatic retry is blocked to prevent duplicate paid generation. Reconcile outcome via "
                "/api/jobs/generation/{job_id}/reconcile, or set acknowledge_new_generation=true."
            )

        if job.provider_operation_id:
            job.dispatch_state = "provider_operation_known"
        else:
            job.dispatch_state = "not_dispatched"

        job.status = "pending"
        job.error_details = ""
        job.retry_count = (job.retry_count or 0) + 1
        job.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
        db.commit()

        self.launch_job_async(job.id)
        return job

    def reconcile_job(
        self,
        db: Session,
        job_id: str,
        remote_status: Optional[str] = None,
        notes: Optional[str] = None
    ) -> GenerationJob:
        """
        Explicit reconciliation workflow for jobs with known `provider_operation_id` or `provider_outcome_unknown`.
        - When `provider_operation_id` exists (and no manual override is given), polls Google directly to settle state.
        - When no `provider_operation_id` exists, accepts explicit operator confirmation
          (`confirmed_not_billed`, `confirmed_failed`, `confirmed_cancelled`, `confirmed_completed`) before
          releasing or settling a preserved budget reservation.
        """
        job = db.query(GenerationJob).filter(GenerationJob.id == job_id).first()
        if not job:
            raise ValueError(f"Generation job '{job_id}' not found.")

        reservation = cost_controller.get_active_job_reservation(db, job.id, job.project_id)
        normalized_override = (remote_status or "").strip().lower()

        # 1. Manual/explicit operator reconciliation override
        if normalized_override in ("confirmed_not_billed", "confirmed_failed", "confirmed_cancelled"):
            if reservation is not None:
                cost_controller.release_reservation(
                    db=db,
                    project_id=job.project_id,
                    reservation_record=reservation,
                    reason=f"reconciled_{normalized_override}: {notes or 'manual verification'}",
                    allow_unknown_release=True,
                )
            job.dispatch_state = "provider_failed"
            job.status = "cancelled" if normalized_override == "confirmed_cancelled" else "failed"
            job.error_details = notes or f"Reconciled as {normalized_override}."
            job.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
            db.commit()
            return job

        if normalized_override == "confirmed_completed":
            if reservation is not None:
                cost_controller.finalize_reservation(
                    db=db,
                    project_id=job.project_id,
                    reservation_record=reservation,
                    local_processing_status="output_unretrieved" if not job.result_asset_id else "completed",
                    error_details=notes or "Reconciled as confirmed_completed remotely",
                )
            job.dispatch_state = "provider_completed" if job.result_asset_id else "local_processing_failed"
            job.status = "completed" if job.result_asset_id else "failed"
            if notes:
                job.error_details = notes
            job.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
            db.commit()
            return job

        # 2. Automatic remote reconciliation when `provider_operation_id` exists for video jobs
        if job.generation_type == "video_generation" and job.provider_operation_id:
            poll_res = google_video_adapter.poll_operation_status(job.provider_operation_id)
            p_status = poll_res.get("status")
            if p_status == "completed":
                try:
                    asset = google_video_adapter.import_completed_video(
                        project_id=job.project_id,
                        shot_id=job.shot_id,
                        video_bytes=poll_res.get("video_bytes"),
                        video_uri=poll_res.get("video_uri"),
                        db=db,
                        audio_bytes=poll_res.get("audio_bytes"),
                        audio_uri=poll_res.get("audio_uri"),
                        reservation_record=reservation,
                        raise_on_processing_error=True,
                    )
                    job.status = "completed"
                    job.dispatch_state = "provider_completed"
                    job.result_asset_id = asset.id
                    job.completed_at = datetime.now(timezone.utc).replace(tzinfo=None)
                    job.error_details = ""
                    db.commit()
                    return job
                except Exception as import_err:
                    if reservation is not None:
                        cost_controller.finalize_reservation(
                            db=db,
                            project_id=job.project_id,
                            reservation_record=reservation,
                            local_processing_status="failed",
                            error_details=str(import_err),
                        )
                    job.status = "failed"
                    job.dispatch_state = "local_processing_failed"
                    job.error_details = f"Reconciliation found remote video complete, but local import failed: {import_err}"
                    db.commit()
                    return job
            elif p_status == "output_missing":
                if reservation is not None:
                    cost_controller.finalize_reservation(
                        db=db,
                        project_id=job.project_id,
                        reservation_record=reservation,
                        local_processing_status="output_unretrieved",
                        error_details=poll_res.get("error", "Remote completed with no output"),
                    )
                job.status = "failed"
                job.dispatch_state = "local_processing_failed"
                job.error_details = poll_res.get("error", "Remote completed with no output")
                db.commit()
                return job
            elif p_status in ("failed", "cancelled"):
                if reservation is not None:
                    cost_controller.release_reservation(
                        db=db,
                        project_id=job.project_id,
                        reservation_record=reservation,
                        reason=f"reconciled_remote_{p_status}",
                        allow_unknown_release=True,
                    )
                job.dispatch_state = "provider_failed"
                job.status = "cancelled" if p_status == "cancelled" else "failed"
                job.provider_operation_id = None
                job.error_details = poll_res.get("error", f"Remote operation {p_status}.")
                db.commit()
                return job
            else:
                job.dispatch_state = "provider_operation_known"
                job.status = "processing"
                job.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
                db.commit()
                self.launch_job_async(job.id)
                return job

        # No operation ID & no confirmed override -> preserve unknown outcome & budget reservation
        if reservation is not None and reservation.action_type == "budget_reservation":
            cost_controller.finalize_reservation(
                db=db,
                project_id=job.project_id,
                reservation_record=reservation,
                local_processing_status="provider_outcome_unknown",
                error_details=notes or "Awaiting confirmed remote reconciliation outcome",
            )
        job.dispatch_state = "provider_outcome_unknown"
        job.status = "provider_outcome_unknown"
        job.error_details = (
            notes
            or "No provider_operation_id exists to auto-reconcile. Verify Google AI usage/billing, "
               "then submit remote_status='confirmed_not_billed' or retry with acknowledge_new_generation=true."
        )
        db.commit()
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

    def recover_interrupted_jobs(self, db: Session) -> int:
        """
        Scans for generation jobs left in 'processing' or 'pending' state upon local server restart.
        - Video jobs with persisted `provider_operation_id`: resumes polling ONLY (no duplicate dispatch).
        - Jobs with `dispatch_state in ('dispatch_started', 'provider_outcome_unknown')` and NO
          `provider_operation_id`: crash happened during/after remote dispatch! Marks `provider_outcome_unknown`,
          preserves budget reservation without releasing/refunding, and blocks automatic retry.
        - Pre-dispatch interrupted jobs (`dispatch_state == 'not_dispatched'`): marked `failed` and
          their pre-flight budget reservations are safely released.
        """
        interrupted = db.query(GenerationJob).filter(
            GenerationJob.status.in_(["pending", "processing"])
        ).all()
        recovered_count = 0
        for job in interrupted:
            dispatch_state = getattr(job, "dispatch_state", "not_dispatched") or "not_dispatched"
            if job.generation_type == "video_generation" and job.provider_operation_id:
                job.dispatch_state = "provider_operation_known"
                self.launch_job_async(job.id)
                recovered_count += 1
            elif dispatch_state in ("dispatch_started", "provider_outcome_unknown"):
                # CRITICAL BILLING SAFETY: Crash occurred after dispatch started without saved op ID!
                job.status = "provider_outcome_unknown"
                job.dispatch_state = "provider_outcome_unknown"
                job.error_details = (
                    "Local backend interrupted during remote dispatch before provider_operation_id was recorded; "
                    "provider outcome is unknown and budget reservation is preserved. Reconcile or acknowledge "
                    "new generation explicitly before retrying."
                )
                if job.shot_id and job.generation_type == "video_generation":
                    shot = db.query(Shot).filter(Shot.id == job.shot_id).first()
                    if shot and shot.generation_status == "generating":
                        shot.generation_status = "pending"
                cost_controller.recover_orphaned_job_reservation(db=db, job_id=job.id, project_id=job.project_id)
                recovered_count += 1
            else:
                job.status = "failed"
                if job.generation_type in ("image_generation", "tts_generation", "music_generation"):
                    job.error_details = (
                        f"Synchronous single-request {job.generation_type} job interrupted by local "
                        f"backend restart (no mid-flight polling recovery). Click retry to resume."
                    )
                else:
                    job.error_details = "Job interrupted by local backend restart. Click retry to resume."
                if job.shot_id and job.generation_type == "video_generation":
                    shot = db.query(Shot).filter(Shot.id == job.shot_id).first()
                    if shot and shot.generation_status == "generating":
                        shot.generation_status = "pending"
                cost_controller.recover_orphaned_job_reservation(db=db, job_id=job.id, project_id=job.project_id)
                recovered_count += 1

        # Reconcile any lingering orphaned budget reservations across the database
        cost_controller.recover_interrupted_reservations(db=db)

        if recovered_count > 0:
            db.commit()
        return recovered_count


generation_job_manager = GenerationJobManager()
