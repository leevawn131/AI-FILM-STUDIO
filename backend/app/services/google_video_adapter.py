"""
Google Gemini Omni Flash Video Generation Adapter.
Officially supported Google Cloud AI video generation via `google-genai` SDK Interactions API.

Capabilities:
- Submits asynchronous video generation explicitly targeting Gemini Omni 1.2 Flash (`gemini-omni-1.2-flash`)
  with explicit fallback support for `gemini-omni-1.1-flash`
- Uses official `client.interactions.create()` and `client.interactions.get()`
- Supports documented tasks:
  * text_to_video
  * image_to_video (reference image input)
  * reference_to_video (up to 6 reference images on Omni 1.2 Flash)
  * edit (video editing)
  * extend (video extension)
  * upscale (video upscaling when video input is provided)
- Supports documented aspect ratios (`16:9`, `9:16`), resolutions (`360p`, `720p`, `1080p`, `4k`),
  duration strings (`Xs`), and URI delivery for high-resolution outputs
- Never fabricates fake UUIDs when provider omits `interaction.id` (raises `SubmissionResponseAnomalyError`)
- Distinguishes provider `"completed"` without retrievable output (`output_missing`) from confirmed `"failed"`
"""

import os
import time
import uuid
import base64
import logging
from pathlib import Path
from typing import Dict, Any, Optional, List
from sqlalchemy.orm import Session

from backend.app.config import settings
from backend.app.models.entities import Project, Shot, MediaAsset
from backend.app.services.cost_controller import cost_controller
from backend.app.services.ffmpeg_service import ffmpeg_service
from backend.app.services.google_model_catalog import (
    estimate_video_cost_usd,
    MODEL_CATALOG,
)
from backend.app.services.provider_error_classifier import (
    SubmissionResponseAnomalyError,
    classify_provider_exception,
    decode_media_bytes,
    safe_str,
)

logger = logging.getLogger("google_video_adapter")


SUPPORTED_VIDEO_MODELS = ("gemini-omni-1.2-flash", "gemini-omni-1.1-flash")


class GoogleVideoAdapterError(Exception):
    """Raised when Google Omni Flash video generation fails."""
    pass


class LocalMediaProcessingError(GoogleVideoAdapterError):
    """Raised when provider video generation succeeded, but local media processing/muxing failed."""
    pass


def is_ambiguous_provider_error(err: Exception) -> bool:
    """Returns True when a provider call error leaves remote creation/execution outcome ambiguous."""
    return classify_provider_exception(err).is_outcome_unknown


class GoogleVideoAdapter:
    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None):
        self._api_key = api_key
        self._model = model
        self._client = None
        self._cached_api_key = None
        self._injected_client = False

    @property
    def api_key(self) -> str:
        return self._api_key or settings.gemini_api_key or os.getenv("GEMINI_API_KEY", "")

    @property
    def model(self) -> str:
        return self._model or settings.google_video_model

    @property
    def client(self):
        if self._client is not None and getattr(self, "_injected_client", False):
            return self._client
        curr_key = self.api_key
        if not curr_key:
            return None
        if self._client is not None and self._cached_api_key is None:
            self._cached_api_key = curr_key
            return self._client
        if self._client is None or self._cached_api_key != curr_key:
            try:
                from google import genai
                self._client = genai.Client(api_key=curr_key)
                self._cached_api_key = curr_key
                self._injected_client = False
            except Exception as e:
                logger.warning(f"Could not initialize google-genai client for Gemini Omni Flash: {e}")
                self._client = None
        return self._client

    @client.setter
    def client(self, value):
        self._client = value
        self._cached_api_key = self.api_key
        self._injected_client = True

    @property
    def is_available(self) -> bool:
        return bool(self.client and self.api_key)

    def _download_uri_bytes(self, uri: str) -> bytes:
        """Downloads video/audio output bytes preferring authenticated Google Files API download."""
        if self.client and hasattr(self.client, "files") and hasattr(self.client.files, "download"):
            try:
                downloaded = self.client.files.download(file=uri)
                if isinstance(downloaded, (bytes, bytearray)):
                    return bytes(downloaded)
            except Exception as dl_err:
                logger.warning(f"Google Files API download failed for '{uri}': {dl_err}")
                if not str(uri).startswith(("http://", "https://")):
                    raise LocalMediaProcessingError(
                        f"Google Files API download failed for '{uri}': {dl_err}"
                    ) from dl_err
        import urllib.request
        return urllib.request.urlopen(uri).read()

    def submit_video_generation(
        self,
        prompt: str,
        project_id: str,
        shot_id: str,
        db: Session,
        duration_seconds: int = 5,
        aspect_ratio: str = "16:9",
        resolution: str = "720p",
        task: str = "text_to_video",
        input_image_path: Optional[str] = None,
        input_video_path: Optional[str] = None,
        reference_image_paths: Optional[List[str]] = None,
        model: Optional[str] = None,
        negative_prompt: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Submits an asynchronous video generation request explicitly to Gemini Omni 1.2 Flash
        (`gemini-omni-1.2-flash`) or explicit fallback (`gemini-omni-1.1-flash`) via Interactions API.
        Never synthesizes a local UUID if `interaction.id` is omitted (`SubmissionResponseAnomalyError`).
        """
        project = db.query(Project).filter(Project.id == project_id).first()
        if not project:
            raise ValueError(f"Project '{project_id}' not found.")

        shot = db.query(Shot).filter(Shot.id == shot_id, Shot.project_id == project_id).first()
        if not shot:
            raise ValueError(f"Shot '{shot_id}' not found in project '{project_id}'.")

        target_model = (model or self.model or getattr(settings, "google_video_model", "gemini-omni-1.2-flash")).strip()
        if target_model not in SUPPORTED_VIDEO_MODELS:
            model_meta = MODEL_CATALOG.get(target_model)
            status_note = model_meta.get("status") if model_meta else "unknown"
            raise ValueError(
                f"Unsupported video model '{target_model}' (status={status_note}). "
                f"Supported models: {list(SUPPORTED_VIDEO_MODELS)}"
            )

        if not self.is_available:
            raise GoogleVideoAdapterError(
                "Gemini Omni Flash video generation requires a valid GEMINI_API_KEY. "
                "Configure your key in settings to enable Google Cloud AI video rendering."
            )

        is_omni_12 = target_model == "gemini-omni-1.2-flash"

        # Documented aspect ratios: strictly 16:9 and 9:16
        clean_ratio = aspect_ratio if aspect_ratio in ("16:9", "9:16") else "16:9"

        # Normalize resolution: Omni 1.2 supports 360p, 720p, 1080p, 4k; Omni 1.1 supports 360p, 720p, 1080p
        req_res = (resolution or "720p").strip().lower()
        supported_res = ("360p", "720p", "1080p", "4k") if is_omni_12 else ("360p", "720p", "1080p")
        if req_res not in supported_res:
            if req_res == "4k" and not is_omni_12:
                raise ValueError(
                    f"Resolution '4k' requires 'gemini-omni-1.2-flash' (not supported by '{target_model}')."
                )
            req_res = "720p"
        clean_resolution = req_res

        # Documented tasks: text_to_video, image_to_video, reference_to_video, edit, extend (+ upscale for video inputs on 1.2)
        base_valid_tasks = ("text_to_video", "image_to_video", "reference_to_video", "edit", "extend")
        video_valid_tasks = ("edit", "extend", "upscale") if is_omni_12 else ("edit", "extend")

        if reference_image_paths:
            if len(reference_image_paths) > 6:
                raise ValueError(
                    f"Omni Flash supports at most 6 reference images for reference_to_video (got {len(reference_image_paths)})."
                )
            resolved_task = "reference_to_video"
        elif input_video_path:
            if task in video_valid_tasks:
                resolved_task = task
            else:
                resolved_task = "edit"
        elif input_image_path:
            # Image input supports image_to_video / reference_to_video (never video-only 'upscale')
            resolved_task = "image_to_video" if (task == "text_to_video" or task not in ("image_to_video", "reference_to_video")) else task
        elif task in base_valid_tasks:
            resolved_task = task
        else:
            resolved_task = "text_to_video"

        # Duration clamping: Omni 1.2 supports 3-15s standard/edit and 3-10s extend
        raw_dur = int(duration_seconds or 5)
        if resolved_task == "extend":
            clean_duration = max(3, min(10, raw_dur))
        elif is_omni_12:
            clean_duration = max(3, min(15, raw_dur))
        else:
            clean_duration = 5 if raw_dur <= 5 else 8

        # Prepare generation config and response format for Interactions API
        video_config: Dict[str, Any] = {
            "task": resolved_task,
        }
        generation_config = {
            "video_config": video_config
        }

        response_format: Dict[str, Any] = {
            "type": "video",
            "aspect_ratio": clean_ratio,
            "resolution": clean_resolution,
            "duration": f"{clean_duration}s",
        }
        if is_omni_12 and clean_resolution in ("1080p", "4k"):
            response_format["delivery"] = "uri"

        full_text = f"{prompt.strip()}\n[Target duration: approximately {clean_duration}s]"
        if negative_prompt:
            full_text = f"{full_text}\n[Avoid: {negative_prompt}]"

        # Build multimodal input parts when reference files are attached
        input_payload: Any = full_text
        if reference_image_paths:
            parts: List[Dict[str, Any]] = [{"type": "text", "text": full_text}]
            for ref_item in reference_image_paths:
                ref_p = Path(ref_item)
                if not ref_p.exists():
                    raise ValueError(f"Reference image file not found: '{ref_item}'")
                suffix = ref_p.suffix.lower()
                mime = "image/png" if suffix == ".png" else ("image/webp" if suffix == ".webp" else "image/jpeg")
                parts.append({
                    "type": "image",
                    "data": base64.b64encode(ref_p.read_bytes()).decode("utf-8"),
                    "mime_type": mime,
                })
            input_payload = parts
        elif input_image_path:
            img_p = Path(input_image_path)
            if img_p.exists():
                suffix = img_p.suffix.lower()
                mime = "image/png" if suffix == ".png" else ("image/webp" if suffix == ".webp" else "image/jpeg")
                img_data = base64.b64encode(img_p.read_bytes()).decode("utf-8")
                input_payload = [
                    {"type": "text", "text": full_text},
                    {"type": "image", "data": img_data, "mime_type": mime}
                ]
        elif input_video_path:
            vid_p = Path(input_video_path)
            if not vid_p.exists():
                raise GoogleVideoAdapterError(f"Input video file not found: '{input_video_path}'")

            if hasattr(self.client, "files") and hasattr(self.client.files, "upload"):
                logger.info(f"Uploading input video '{input_video_path}' via Google Files API...")
                try:
                    uploaded_file = self.client.files.upload(file=str(vid_p))
                except Exception as upload_err:
                    raise GoogleVideoAdapterError(f"Files API upload failed: {upload_err}") from upload_err

                max_upload_polls = 30
                is_active = False
                for poll_idx in range(max_upload_polls):
                    state = getattr(uploaded_file, "state", None)
                    state_name = str(getattr(state, "name", getattr(state, "value", state))).upper()
                    if "ACTIVE" in state_name:
                        is_active = True
                        break
                    if "FAILED" in state_name or "ERROR" in state_name:
                        raise GoogleVideoAdapterError(f"Files API video processing failed: {state_name}")
                    if "PROCESSING" in state_name or "STATE_UNSPECIFIED" in state_name:
                        if poll_idx < max_upload_polls - 1:
                            time.sleep(1)
                            if hasattr(self.client.files, "get") and getattr(uploaded_file, "name", None):
                                uploaded_file = self.client.files.get(name=uploaded_file.name)
                    else:
                        raise GoogleVideoAdapterError(
                            f"Unexpected Files API file state '{state_name}' for '{input_video_path}'"
                        )

                if not is_active:
                    raise GoogleVideoAdapterError(
                        f"Google Files API upload for '{input_video_path}' remained in PROCESSING state "
                        f"after {max_upload_polls} polls; aborting video generation request."
                    )

                file_uri = getattr(uploaded_file, "uri", None)
                if not file_uri or not isinstance(file_uri, str):
                    raise GoogleVideoAdapterError("Google Files API upload became ACTIVE but returned no file URI.")

                input_payload = [
                    {"type": "text", "text": full_text},
                    {"type": "video", "uri": file_uri, "mime_type": "video/mp4"}
                ]
            else:
                vid_data = base64.b64encode(vid_p.read_bytes()).decode("utf-8")
                input_payload = [
                    {"type": "text", "text": full_text},
                    {"type": "video", "data": vid_data, "mime_type": "video/mp4"}
                ]

        try:
            logger.info(
                f"Submitting Omni Flash video generation ({resolved_task}, {clean_resolution}, {clean_duration}s) "
                f"for shot {shot.shot_code} using model {target_model} via Interactions API..."
            )
            interaction = self.client.interactions.create(
                model=target_model,
                input=input_payload,
                background=True,
                generation_config=generation_config,
                response_format=response_format
            )
        except Exception as e:
            err_msg = str(e)
            logger.error(f"Gemini Omni Flash generation submission failed: {err_msg}")
            raise GoogleVideoAdapterError(f"Gemini Omni Flash submission failed: {err_msg}") from e

        provider_op_id = safe_str(getattr(interaction, "id", None))
        if not provider_op_id:
            # CRITICAL BILLING SAFETY: NEVER synthesize a fake UUID if provider omitted `id`.
            logger.error("Google Omni Flash interactions.create returned without an interaction ID.")
            raise SubmissionResponseAnomalyError(
                "Provider response from interactions.create lacked a valid interaction.id; "
                "cannot safely assume request did not execute remotely."
            )

        logger.info(f"Gemini Omni Flash interaction created successfully: id={provider_op_id}")

        return {
            "status": "processing",
            "provider": "google_omni",
            "model": target_model,
            "provider_operation_id": provider_op_id,
            "prompt": prompt,
            "task": resolved_task,
            "target_duration_seconds": clean_duration,
            "aspect_ratio": clean_ratio,
            "resolution": clean_resolution,
            "delivery": response_format.get("delivery", "inline"),
            "supports_cancellation": True
        }

    def poll_operation_status(self, provider_operation_id: str) -> Dict[str, Any]:
        """
        Polls the status of an in-flight Gemini Omni Flash generation operation using official Interactions API.
        """
        if not self.is_available:
            raise GoogleVideoAdapterError("Gemini Omni Flash client is not configured.")

        try:
            interaction = self.client.interactions.get(id=provider_operation_id)
        except Exception as e:
            logger.error(f"Failed to poll Omni Flash interaction '{provider_operation_id}': {e}")
            raise GoogleVideoAdapterError(f"Failed to poll Omni Flash interaction: {e}")

        status = getattr(interaction, "status", "pending")
        if isinstance(status, str):
            status_lower = status.lower()
        else:
            status_lower = str(getattr(status, "value", status)).lower()

        errors = getattr(interaction, "errors", None)

        if status_lower in ("failed", "error", "budget_exceeded"):
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

            video_bytes = decode_media_bytes(getattr(output_video, "data", None)) if output_video else None
            video_uri = safe_str(getattr(output_video, "uri", None)) if output_video else None

            if not video_bytes and not video_uri:
                outputs = getattr(interaction, "outputs", None)
                if isinstance(outputs, (list, tuple)):
                    for out in outputs:
                        raw_b = decode_media_bytes(getattr(out, "data", None))
                        raw_u = safe_str(getattr(out, "uri", None))
                        mime = (safe_str(getattr(out, "mime_type", None)) or "").lower()
                        if (raw_b or raw_u) and (not mime or mime.startswith("video/")):
                            video_bytes = raw_b
                            video_uri = raw_u
                            break

            audio_bytes = decode_media_bytes(getattr(output_audio, "data", None)) if output_audio else None
            audio_uri = safe_str(getattr(output_audio, "uri", None)) if output_audio else None

            if not video_bytes and not video_uri:
                # CRITICAL BILLING SAFETY: Provider reported `completed`, so do NOT report `"failed"`
                # and do NOT release reservation! Return `"output_missing"` so budget stays preserved.
                return {
                    "status": "output_missing",
                    "provider_operation_id": provider_operation_id,
                    "error": "Interaction marked completed remotely but returned no video bytes or URI; charge preserved."
                }

            return {
                "status": "completed",
                "provider_operation_id": provider_operation_id,
                "video_bytes": video_bytes,
                "video_uri": video_uri,
                "audio_bytes": audio_bytes,
                "audio_uri": audio_uri,
                "mime_type": safe_str(getattr(output_video, "mime_type", None)) or "video/mp4"
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
            if hasattr(self.client, "interactions") and hasattr(self.client.interactions, "cancel"):
                self.client.interactions.cancel(id=provider_operation_id)
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
        audio_uri: Optional[str] = None,
        reservation_record: Optional[Any] = None,
        raise_on_processing_error: bool = False
    ) -> MediaAsset:
        """
        Saves the generated video to the local project media directory (preferring authenticated
        Google Files API download for URI outputs), multiplexes native audio output via FFmpeg
        if present, strictly inspects stream dimensions/duration/fps/codec with FFprobe (never
        inventing metadata on failure), creates a MediaAsset record with take versioning,
        finalizes the budget reservation (or charges cost), and links it to the Shot.
        """
        if db is None:
            raise ValueError("Database session is required.")

        project = db.query(Project).filter(Project.id == project_id).first()
        shot = db.query(Shot).filter(Shot.id == shot_id, Shot.project_id == project_id).first()
        if not project or not shot:
            raise ValueError("Project or Shot not found.")

        if not video_bytes and not video_uri:
            raise ValueError("Neither video_bytes nor video_uri was provided.")

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
        temp_audio_path = dest_dir / f"{clean_code}_v{next_ver:03d}_{asset_id[:6]}_temp_audio.wav"
        muxed_path = dest_dir / f"{clean_code}_v{next_ver:03d}_{asset_id[:6]}_muxed.mp4"

        def _record_local_failure_if_reserved(err_msg: str) -> None:
            if reservation_record is not None:
                cost_controller.finalize_reservation(
                    db=db,
                    project_id=project_id,
                    reservation_record=reservation_record,
                    actual_cost=None,
                    actual_cost_known=False,
                    local_processing_status="failed",
                    error_details=err_msg,
                )
                db.commit()

        try:
            if video_bytes:
                file_path.write_bytes(video_bytes)
            else:
                downloaded_video = self._download_uri_bytes(video_uri)
                if not downloaded_video:
                    raise LocalMediaProcessingError(f"Downloaded video from '{video_uri}' is empty.")
                file_path.write_bytes(downloaded_video)

            # Multiplex native audio output if returned by Gemini Omni Flash
            raw_audio = audio_bytes
            if not raw_audio and audio_uri:
                try:
                    raw_audio = self._download_uri_bytes(audio_uri)
                except Exception as audio_dl_err:
                    logger.warning(f"Could not download Omni Flash native audio from {audio_uri}: {audio_dl_err}")
                    if raise_on_processing_error:
                        raise LocalMediaProcessingError(
                            f"Failed downloading Omni Flash native audio '{audio_uri}': {audio_dl_err}"
                        ) from audio_dl_err

            if raw_audio:
                try:
                    temp_audio_path.write_bytes(raw_audio)
                    ffmpeg_service.mux_video_and_audio(file_path, temp_audio_path, muxed_path)
                    muxed_path.replace(file_path)
                    logger.info(f"Successfully multiplexed Omni Flash native audio into {filename}")
                except Exception as mux_err:
                    logger.warning(f"FFmpeg audio muxing failed: {mux_err}")
                    if raise_on_processing_error:
                        raise LocalMediaProcessingError(f"Local FFmpeg audio muxing failed: {mux_err}") from mux_err
                finally:
                    for p in (temp_audio_path, muxed_path):
                        if p.exists():
                            try:
                                p.unlink()
                            except Exception:
                                pass

            # Strictly probe video metadata using local FFprobe (never invent metadata on failure)
            try:
                meta = ffmpeg_service.inspect_media(file_path)
            except Exception as probe_err:
                raise LocalMediaProcessingError(
                    f"FFprobe could not inspect video file '{filename}': {probe_err}"
                ) from probe_err

            width = meta.get("width")
            height = meta.get("height")
            duration = meta.get("duration")
            fps = meta.get("fps")
            codec = meta.get("codec") or meta.get("video_codec")
            if (
                not width or int(width) <= 0
                or not height or int(height) <= 0
                or duration is None or float(duration) <= 0.0
                or not fps or float(fps) <= 0.0
                or not codec or codec == "unknown"
            ):
                raise LocalMediaProcessingError(
                    f"FFprobe returned incomplete or invalid video stream metadata for '{filename}': {meta}"
                )

            media_asset = MediaAsset(
                id=asset_id,
                project_id=project_id,
                shot_id=shot_id,
                version=next_ver,
                status="ready",
                filename=filename,
                file_path=str(file_path),
                media_type="video",
                width=int(width),
                height=int(height),
                duration_seconds=float(duration),
                fps=float(fps),
                codec=str(codec),
                file_size_bytes=file_path.stat().st_size
            )
            db.add(media_asset)
            db.flush()

            shot.video_asset_id = media_asset.id
            shot.video_path = str(file_path)
            shot.generation_status = "ready"
            shot.generation_attempts = (shot.generation_attempts or 0) + 1

            if reservation_record is not None:
                cost_controller.finalize_reservation(
                    db=db,
                    project_id=project_id,
                    reservation_record=reservation_record,
                    actual_cost=None,
                    actual_cost_known=False,
                    local_processing_status="completed"
                )
            else:
                cost_controller.authorize_and_charge(
                    db=db,
                    project_id=project_id,
                    shot_id=shot_id,
                    action_type="video_generation",
                    provider="google_omni",
                    model=self.model,
                    operation="video_generation",
                    details=f"{self.model} video generation take {next_ver} for shot {shot.shot_code}",
                    estimated_cost=estimate_video_cost_usd(self.model),
                    actual_cost=None,
                    actual_cost_known=False,
                    allow_duplicate=True
                )
            db.commit()
            db.refresh(media_asset)
            return media_asset
        except LocalMediaProcessingError as local_err:
            db.rollback()
            for p in (file_path, temp_audio_path, muxed_path):
                if p.exists():
                    try:
                        p.unlink()
                    except Exception:
                        pass
            _record_local_failure_if_reserved(str(local_err))
            raise
        except Exception as err:
            db.rollback()
            for p in (file_path, temp_audio_path, muxed_path):
                if p.exists():
                    try:
                        p.unlink()
                    except Exception:
                        pass
            _record_local_failure_if_reserved(str(err))
            raise LocalMediaProcessingError(f"Local video import processing failed: {err}") from err


google_video_adapter = GoogleVideoAdapter()
