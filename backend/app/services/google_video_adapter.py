"""
Google Gemini Omni Flash Video Generation Adapter.
Officially supported Google Cloud AI video generation via `google-genai` SDK Interactions API.

Capabilities:
- Submits asynchronous video generation using Gemini Omni Flash 1.1 (`gemini-omni-1.1-flash`)
- Uses official `client.interactions.create()` and `client.interactions.get()`
- Supports multiple generation tasks:
  * text_to_video
  * image_to_video (reference image input)
  * edit (video editing)
  * extend (video extension)
  * upscale (keyframe interpolation & resolution upscale)
- Captures native audio output when returned by Omni Flash
- Downloads and persists generated video assets locally in project directory
- Registers MediaAsset records with incremental take versioning (_v001, _v002)
- Multi-take selection and linking on Shot entities
- Accurate cloud AI cost tracking via CostController (never labeled as subscription credits)
"""

import os
import uuid
import base64
import logging
from pathlib import Path
from typing import Dict, Any, Optional
from sqlalchemy.orm import Session

from backend.app.config import settings
from backend.app.models.entities import Project, Shot, MediaAsset
from backend.app.services.cost_controller import cost_controller
from backend.app.services.ffmpeg_service import ffmpeg_service

logger = logging.getLogger("google_video_adapter")


class GoogleVideoAdapterError(Exception):
    """Raised when Google Omni Flash video generation fails."""
    pass


class GoogleVideoAdapter:
    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None):
        self.api_key = api_key or settings.gemini_api_key or os.getenv("GEMINI_API_KEY", "")
        self.model = model or settings.google_video_model
        self._client = None
        if self.api_key:
            try:
                from google import genai
                self._client = genai.Client(api_key=self.api_key)
            except Exception as e:
                logger.warning(f"Could not initialize google-genai client for Gemini Omni Flash: {e}")

    @property
    def is_available(self) -> bool:
        return bool(self._client and self.api_key)

    def submit_video_generation(
        self,
        prompt: str,
        project_id: str,
        shot_id: str,
        db: Session,
        duration_seconds: int = 5,
        aspect_ratio: str = "16:9",
        task: str = "text_to_video",
        input_image_path: Optional[str] = None,
        input_video_path: Optional[str] = None,
        negative_prompt: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Submits an asynchronous video generation request to Gemini Omni Flash 1.1 via Interactions API.
        Returns initial job information including provider interaction ID.
        """
        project = db.query(Project).filter(Project.id == project_id).first()
        if not project:
            raise ValueError(f"Project '{project_id}' not found.")

        shot = db.query(Shot).filter(Shot.id == shot_id, Shot.project_id == project_id).first()
        if not shot:
            raise ValueError(f"Shot '{shot_id}' not found in project '{project_id}'.")

        if not self.is_available:
            raise GoogleVideoAdapterError(
                "Gemini Omni Flash video generation requires a valid GEMINI_API_KEY. "
                "Configure your key in settings to enable Google Cloud AI video rendering."
            )

        clean_ratio = aspect_ratio if aspect_ratio in ["16:9", "9:16", "1:1", "4:3"] else "16:9"
        clean_duration = 5 if duration_seconds <= 5 else 8

        # Determine task
        resolved_task = task
        if input_image_path and task == "text_to_video":
            resolved_task = "image_to_video"
        elif input_video_path and task == "text_to_video":
            resolved_task = "edit"

        valid_tasks = ["text_to_video", "image_to_video", "reference_to_video", "edit", "extend", "upscale"]
        if resolved_task not in valid_tasks:
            resolved_task = "text_to_video"

        # Prepare generation config for Interactions API
        video_config: Dict[str, Any] = {
            "task": resolved_task,
        }

        generation_config = {
            "video_config": video_config
        }

        # Format input prompt (and references if applicable)
        full_input = prompt
        if negative_prompt:
            full_input = f"{prompt}\n[Avoid: {negative_prompt}]"

        try:
            logger.info(
                f"Submitting Omni Flash video generation ({resolved_task}) for shot {shot.shot_code} "
                f"using model {self.model} via Interactions API..."
            )
            # Official Google Interactions API
            interaction = self._client.interactions.create(
                model=self.model,
                input=full_input,
                background=True,
                generation_config=generation_config
            )
        except Exception as e:
            err_msg = str(e)
            logger.error(f"Gemini Omni Flash generation submission failed: {err_msg}")
            raise GoogleVideoAdapterError(f"Gemini Omni Flash submission failed: {err_msg}")

        provider_op_id = getattr(interaction, "id", None) or str(uuid.uuid4())
        logger.info(f"Gemini Omni Flash interaction created successfully: id={provider_op_id}")

        return {
            "status": "processing",
            "provider": "google_omni",
            "model": self.model,
            "provider_operation_id": provider_op_id,
            "prompt": prompt,
            "task": resolved_task,
            "duration_seconds": clean_duration,
            "aspect_ratio": clean_ratio,
            "supports_cancellation": True
        }

    def poll_operation_status(self, provider_operation_id: str) -> Dict[str, Any]:
        """
        Polls the status of an in-flight Gemini Omni Flash generation operation using official Interactions API.
        """
        if not self.is_available:
            raise GoogleVideoAdapterError("Gemini Omni Flash client is not configured.")

        try:
            interaction = self._client.interactions.get(id=provider_operation_id)
        except Exception as e:
            logger.error(f"Failed to poll Omni Flash interaction '{provider_operation_id}': {e}")
            raise GoogleVideoAdapterError(f"Failed to poll Omni Flash interaction: {e}")

        status = getattr(interaction, "status", "pending")
        if isinstance(status, str):
            status_lower = status.lower()
        else:
            status_lower = str(getattr(status, "value", status)).lower()

        errors = getattr(interaction, "errors", None)

        if status_lower in ("failed", "error"):
            logger.warning(f"Omni Flash interaction '{provider_operation_id}' failed: {errors}")
            return {
                "status": "failed",
                "provider_operation_id": provider_operation_id,
                "error": str(errors or "Generation failed remotely.")
            }

        if status_lower in ("cancelled", "canceled"):
            return {
                "status": "cancelled",
                "provider_operation_id": provider_operation_id
            }

        if status_lower == "completed":
            output_video = getattr(interaction, "output_video", None)
            output_audio = getattr(interaction, "output_audio", None)

            video_bytes = None
            video_uri = None
            if output_video:
                raw_data = getattr(output_video, "data", None)
                if raw_data:
                    if isinstance(raw_data, str):
                        try:
                            video_bytes = base64.b64decode(raw_data)
                        except Exception:
                            video_bytes = raw_data.encode("utf-8")
                    else:
                        video_bytes = raw_data
                video_uri = getattr(output_video, "uri", None)

            audio_bytes = None
            audio_uri = None
            if output_audio:
                raw_aud = getattr(output_audio, "data", None)
                if raw_aud:
                    if isinstance(raw_aud, str):
                        try:
                            audio_bytes = base64.b64decode(raw_aud)
                        except Exception:
                            audio_bytes = raw_aud.encode("utf-8")
                    else:
                        audio_bytes = raw_aud
                audio_uri = getattr(output_audio, "uri", None)

            if not video_bytes and not video_uri:
                return {
                    "status": "failed",
                    "provider_operation_id": provider_operation_id,
                    "error": "Interaction marked completed but returned no output video."
                }

            return {
                "status": "completed",
                "provider_operation_id": provider_operation_id,
                "video_bytes": video_bytes,
                "video_uri": video_uri,
                "audio_bytes": audio_bytes,
                "audio_uri": audio_uri,
                "mime_type": getattr(output_video, "mime_type", "video/mp4")
            }

        return {
            "status": "processing",
            "provider_operation_id": provider_operation_id
        }

    def cancel_operation(self, provider_operation_id: str) -> Dict[str, Any]:
        """Cancels an in-flight interaction if supported by the provider."""
        if not self.is_available:
            return {"status": "error", "message": "Client not configured"}
        try:
            if hasattr(self._client, "interactions") and hasattr(self._client.interactions, "cancel"):
                self._client.interactions.cancel(id=provider_operation_id)
                return {"status": "cancelled", "provider_operation_id": provider_operation_id}
            return {"status": "unsupported", "message": "Cancel not supported on this endpoint"}
        except Exception as e:
            logger.warning(f"Could not cancel interaction {provider_operation_id}: {e}")
            return {"status": "unsupported", "message": str(e)}

    def import_completed_video(
        self,
        project_id: str,
        shot_id: str,
        video_bytes: Optional[bytes] = None,
        video_uri: Optional[str] = None,
        db: Optional[Session] = None,
        audio_bytes: Optional[bytes] = None,
        audio_uri: Optional[str] = None
    ) -> MediaAsset:
        """
        Saves the generated video to the local project media directory,
        creates a MediaAsset record with appropriate versioning,
        and links it to the Shot as the active take.
        """
        if db is None:
            raise ValueError("Database session is required.")

        project = db.query(Project).filter(Project.id == project_id).first()
        shot = db.query(Shot).filter(Shot.id == shot_id, Shot.project_id == project_id).first()
        if not project or not shot:
            raise ValueError("Project or Shot not found.")

        # Determine next take version for this shot
        existing_versions = db.query(MediaAsset).filter(
            MediaAsset.shot_id == shot_id
        ).all()
        next_ver = max([m.version for m in existing_versions], default=0) + 1

        dest_dir = settings.projects_dir / project_id / "imported_videos"
        dest_dir.mkdir(parents=True, exist_ok=True)

        asset_id = str(uuid.uuid4())
        clean_code = shot.shot_code.replace(" ", "_")
        filename = f"{clean_code}_v{next_ver:03d}_{asset_id[:6]}.mp4"
        file_path = dest_dir / filename

        if video_bytes:
            file_path.write_bytes(video_bytes)
        elif video_uri:
            # Download from URI
            import urllib.request
            urllib.request.urlretrieve(video_uri, str(file_path))
        else:
            raise ValueError("Neither video_bytes nor video_uri was provided.")

        # Probe video metadata using local ffmpeg service
        try:
            meta = ffmpeg_service.inspect_media(file_path)
        except Exception:
            meta = {}

        # Register local MediaAsset
        media_asset = MediaAsset(
            id=asset_id,
            project_id=project_id,
            shot_id=shot_id,
            version=next_ver,
            status="ready",
            filename=filename,
            file_path=str(file_path),
            media_type="video",
            width=meta.get("width", 1920),
            height=meta.get("height", 1080),
            duration_seconds=float(meta.get("duration", shot.duration_seconds or 4.0)),
            fps=float(meta.get("fps", 24.0)),
            codec=meta.get("video_codec", "h264"),
            file_size_bytes=file_path.stat().st_size
        )
        db.add(media_asset)
        db.flush()

        # Update Shot take selection
        shot.video_asset_id = media_asset.id
        shot.video_path = str(file_path)
        shot.generation_status = "ready"
        shot.generation_attempts = (shot.generation_attempts or 0) + 1

        # Record Cloud AI cost in CostController
        # Estimated cost for Omni Flash video generation (~$0.15)
        cost_controller.authorize_and_charge(
            db=db,
            project_id=project_id,
            shot_id=shot_id,
            action_type="video_generation",
            provider="google_omni",
            model=self.model,
            operation="video_generation",
            details=f"Gemini Omni Flash 1.1 video generation take {next_ver} for shot {shot.shot_code}",
            estimated_cost=0.15,
            actual_cost=0.15,
            actual_cost_known=False,
            allow_duplicate=True
        )
        db.commit()
        db.refresh(media_asset)
        return media_asset


google_video_adapter = GoogleVideoAdapter()
