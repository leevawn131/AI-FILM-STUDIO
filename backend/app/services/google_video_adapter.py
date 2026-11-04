"""
Google Veo Video Generation Adapter.
Officially supported Google Generative AI video generation via `google-genai` SDK.

Capabilities:
- Submits asynchronous video generation requests using Google Veo (veo-2.0-generate-001)
- Stores and tracks Google Cloud operation resource IDs
- Polls asynchronous operation status
- Downloads completed video assets directly to local project media directories
- Automatically registers MediaAsset with versioning (_v001, _v002)
- Supports multi-take selection on Shot entities
- Preserves full generation metadata and token/cost accounting
"""

import os
import uuid
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
    """Raised when Google Veo video generation fails."""
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
                logger.warning(f"Could not initialize google-genai client for Veo: {e}")

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
        negative_prompt: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Submits an asynchronous video generation request to Google Veo.
        Returns initial job information including provider operation ID.
        """
        project = db.query(Project).filter(Project.id == project_id).first()
        if not project:
            raise ValueError(f"Project '{project_id}' not found.")

        shot = db.query(Shot).filter(Shot.id == shot_id, Shot.project_id == project_id).first()
        if not shot:
            raise ValueError(f"Shot '{shot_id}' not found in project '{project_id}'.")

        if not self.is_available:
            raise GoogleVideoAdapterError(
                "Google Veo video generation requires a valid GEMINI_API_KEY. "
                "Configure your key in settings or use the Flow Handoff workflow."
            )

        from google.genai import types

        clean_ratio = aspect_ratio if aspect_ratio in ["16:9", "9:16"] else "16:9"
        clean_duration = 5 if duration_seconds <= 5 else 8

        config_kwargs: Dict[str, Any] = {
            "aspect_ratio": clean_ratio,
            "duration_seconds": clean_duration,
            "person_generation": "allow_adult",
        }
        if negative_prompt:
            config_kwargs["negative_prompt"] = negative_prompt

        config = types.GenerateVideosConfig(**config_kwargs)

        try:
            logger.info(f"Submitting Veo video generation for shot {shot.shot_code} ({self.model})...")
            operation = self._client.models.generate_videos(
                model=self.model,
                prompt=prompt,
                config=config
            )
        except Exception as e:
            err_msg = str(e)
            logger.error(f"Google Veo generation submission failed: {err_msg}")
            raise GoogleVideoAdapterError(f"Google Veo submission failed: {err_msg}")

        provider_op_id = getattr(operation, "name", None) or str(uuid.uuid4())
        logger.info(f"Veo generation submitted successfully: operation_id={provider_op_id}")

        return {
            "status": "processing",
            "provider": "google_veo",
            "model": self.model,
            "provider_operation_id": provider_op_id,
            "prompt": prompt,
            "duration_seconds": clean_duration,
            "aspect_ratio": clean_ratio,
            "supports_cancellation": False
        }

    def poll_operation_status(self, provider_operation_id: str) -> Dict[str, Any]:
        """
        Polls the status of an in-flight Google Veo generation operation using official API.
        """
        if not self.is_available:
            raise GoogleVideoAdapterError("Google Veo client is not configured.")

        try:
            operation = self._client.operations.get(operation=provider_operation_id)
        except Exception as e:
            logger.error(f"Failed to poll Veo operation '{provider_operation_id}': {e}")
            raise GoogleVideoAdapterError(f"Failed to poll Veo operation: {e}")

        is_done = getattr(operation, "done", False)
        error = getattr(operation, "error", None)

        if is_done:
            if error:
                logger.warning(f"Veo operation '{provider_operation_id}' completed with error: {error}")
                return {
                    "status": "failed",
                    "provider_operation_id": provider_operation_id,
                    "error": str(error)
                }

            # Operation succeeded, retrieve video
            response = getattr(operation, "response", None)
            generated_videos = getattr(response, "generated_videos", []) if response else []

            if not generated_videos:
                return {
                    "status": "failed",
                    "provider_operation_id": provider_operation_id,
                    "error": "Operation marked done but no generated video was returned."
                }

            vid_item = generated_videos[0].video
            video_bytes = getattr(vid_item, "video_bytes", None)
            uri = getattr(vid_item, "uri", None)

            return {
                "status": "completed",
                "provider_operation_id": provider_operation_id,
                "video_bytes": video_bytes,
                "video_uri": uri,
                "mime_type": getattr(vid_item, "mime_type", "video/mp4")
            }

        return {
            "status": "processing",
            "provider_operation_id": provider_operation_id
        }

    def import_completed_video(
        self,
        project_id: str,
        shot_id: str,
        video_bytes: Optional[bytes],
        video_uri: Optional[str],
        db: Session
    ) -> MediaAsset:
        """
        Saves the generated video to the local project media directory,
        creates a MediaAsset record with appropriate versioning,
        and links it to the Shot as the active take.
        """
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

        # Update Shot
        shot.video_asset_id = media_asset.id
        shot.generation_status = "imported"
        shot.generation_attempts = (shot.generation_attempts or 0) + 1

        # Record Cost Record
        cost_controller.authorize_and_charge(
            db=db,
            project_id=project_id,
            shot_id=shot_id,
            action_type="flow_video_generation",
            provider="google_veo",
            model=self.model,
            operation="flow_video_generation",
            details=f"Google Veo video generation take {next_ver} for shot {shot.shot_code}",
            flow_credits=1.0,
            estimated_cost=0.20,
            actual_cost=0.20,
            actual_cost_known=True,
            allow_duplicate=True
        )
        db.commit()
        db.refresh(media_asset)
        return media_asset


google_video_adapter = GoogleVideoAdapter()
