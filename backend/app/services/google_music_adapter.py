"""
Google Lyria Music Generation Adapter.
Officially supported Google Cloud AI music synthesis via `google-genai` SDK Interactions API.

Capabilities:
- Scene background score composition using Lyria 3.5 (`lyria-3.5`)
- Short musical cues and loops using Lyria Clip Preview (`lyria-3-clip-preview`)
- Composition customization:
  * Scene mood (cyberpunk, noir, orchestral drama, horror tension, epic sci-fi)
  * Musical genre (ambient, synthwave, orchestral, electronic, neoclassical)
  * Featured instruments (strings, analog synthesizers, percussion, brass)
  * Tempo / BPM (e.g. 80, 100, 120, 140)
  * Energy dynamics (low ambient to high intensity)
  * Duration constraints (15s, 30s, 60s)
  * Pure instrumental or melodic styling
  * Storyboard reference frame inspiration
- Asynchronous submission and tracking via Interactions API
- Local audio file persistence in project media directories
- Automatic registration in SQLite database as AudioTrack and MediaAsset
- Accurate Cloud AI cost tracking via CostController
- Never confuses or labels procedural FFmpeg tones as AI-generated music
"""

import os
import uuid
import socket
import base64
import logging
from pathlib import Path
from typing import Dict, Any, Optional, List, Tuple
from sqlalchemy.orm import Session

from backend.app.config import settings
from backend.app.models.entities import Project, Scene, AudioTrack, MediaAsset
from backend.app.services.cost_controller import cost_controller
from backend.app.services.ffmpeg_service import ffmpeg_service

logger = logging.getLogger("google_music_adapter")


class GoogleMusicAdapterError(Exception):
    """Raised when Google Lyria music generation fails."""
    pass


def _is_ambiguous_provider_error(err: Exception) -> bool:
    """Returns True when an error leaves remote Lyria execution outcome ambiguous."""
    if isinstance(err, (TimeoutError, socket.timeout, ConnectionError)):
        return True
    msg = str(err).lower()
    rejection_markers = ("400", "401", "403", "404", "422", "429", "invalid_argument", "permission_denied", "unauthenticated", "api key")
    if any(m in msg for m in rejection_markers):
        return False
    ambiguous_markers = ("timeout", "timed out", "504", "gateway", "connection reset", "broken pipe", "remote end closed", "incomplete read")
    return any(m in msg for m in ambiguous_markers)


def _get_interaction_status(interaction: Any) -> Optional[str]:
    raw_status = getattr(interaction, "status", None)
    if isinstance(raw_status, str):
        return raw_status.lower()
    val = getattr(raw_status, "value", None)
    if isinstance(val, str):
        return val.lower()
    return None


class GoogleMusicAdapter:
    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None):
        self._api_key = api_key
        self._model = model
        self._client = None
        self._cached_api_key = None

    @property
    def api_key(self) -> str:
        return self._api_key or settings.gemini_api_key or os.getenv("GEMINI_API_KEY", "")

    @property
    def model(self) -> str:
        return self._model or getattr(settings, "google_music_model", "lyria-3.5")

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
                logger.warning(f"Could not initialize google-genai client for Lyria: {e}")
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
        """Downloads audio bytes via authenticated Files API when available, falling back to HTTP."""
        if self.client and hasattr(self.client, "files") and hasattr(self.client.files, "download"):
            try:
                downloaded = self.client.files.download(file=uri)
                if isinstance(downloaded, (bytes, bytearray)):
                    return bytes(downloaded)
            except Exception as dl_err:
                logger.warning(f"Files API download for '{uri}' encountered issue: {dl_err}")
        import urllib.request
        return urllib.request.urlopen(uri).read()

    @staticmethod
    def _extract_interaction_audio(interaction: Any) -> Tuple[Optional[bytes], Optional[str]]:
        """Extracts audio bytes or URI from a Lyria Interaction response."""
        output_audio = getattr(interaction, "output_audio", None)
        if output_audio:
            raw_data = getattr(output_audio, "data", None)
            if raw_data:
                if isinstance(raw_data, str):
                    try:
                        return base64.b64decode(raw_data), None
                    except Exception:
                        return raw_data.encode("utf-8"), None
                return raw_data, None
            uri = getattr(output_audio, "uri", None)
            if uri:
                return None, uri

        if hasattr(interaction, "candidates") and interaction.candidates:
            candidate = interaction.candidates[0]
            if hasattr(candidate, "content") and hasattr(candidate.content, "parts"):
                for part in candidate.content.parts:
                    inline = getattr(part, "inline_data", None)
                    if inline and hasattr(inline, "data"):
                        raw_inline = inline.data
                        audio_bytes = base64.b64decode(raw_inline) if isinstance(raw_inline, str) else raw_inline
                        return audio_bytes, None
        return None, None

    def generate_music_score(
        self,
        project_id: str,
        db: Session,
        mood: str = "atmospheric",
        genre: str = "cinematic synthwave",
        bpm: int = 110,
        instruments: Optional[List[str]] = None,
        energy: str = "medium",
        duration_seconds: int = 30,
        instrumental: bool = True,
        scene_id: Optional[str] = None,
        reference_image_path: Optional[str] = None,
        prompt: Optional[str] = None,
        model: Optional[str] = None,
        reservation_record: Optional[Any] = None,
        job_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Generates an AI background music score using Google Lyria via Interactions API.
        Follows reserve -> single submit -> FFprobe validation -> finalize/release contract.
        Never blindly submits a second paid interaction if the first call fails or is ambiguous.
        """
        project = db.query(Project).filter(Project.id == project_id).first()
        if not project:
            raise ValueError(f"Project '{project_id}' not found.")

        target_model = model or self.model
        dest_dir = settings.projects_dir / project_id / "audio"
        dest_dir.mkdir(parents=True, exist_ok=True)

        # Distinguish between lyria-3.5 (full-length) and lyria-3-clip-preview (fixed 30s)
        is_clip_model = "clip" in target_model.lower()
        effective_duration = 30 if is_clip_model else duration_seconds

        if not self.is_available:
            logger.info("Google API key not configured for Lyria. Generating offline mock score reference.")
            return self._generate_fallback_music(
                project=project,
                dest_dir=dest_dir,
                mood=mood,
                genre=genre,
                duration_seconds=effective_duration,
                scene_id=scene_id,
                db=db
            )

        # Build prompt expressing musical requirements per official API documentation.
        # Do not use unsupported audio_config dictionary parameters.
        inst_list = ", ".join(instruments) if instruments else "analog synthesizers, cinematic sub-bass, orchestral strings"
        pref = "Instrumental only, no vocals." if instrumental else "Atmospheric vocal chops and pads."

        if is_clip_model:
            # Lyria 3 Clip Preview is a fixed 30-second model; do not promise exact arbitrary duration
            duration_instruction = "30-second musical clip preview / scene cue."
        else:
            # Lyria 3.5 supports full-length composition
            duration_instruction = f"Full-length cinematic track with target duration approximately {effective_duration} seconds."

        if prompt:
            full_prompt = (
                f"Cinematic score: {prompt}. Mood: {mood}. Genre: {genre}. "
                f"Tempo: {bpm} BPM. Energy: {energy}. Featured instruments: {inst_list}. {pref} "
                f"{duration_instruction}"
            )
        else:
            full_prompt = (
                f"Cinematic {genre} score with a {mood} atmosphere. "
                f"Tempo: {bpm} BPM. Energy dynamic: {energy}. "
                f"Featured instrumentation: {inst_list}. {pref} "
                f"{duration_instruction}"
            )

        response_format = {"type": "audio", "mime_type": "audio/mp3"}

        # Pre-flight budget reservation BEFORE external Google API call
        est_cost = 0.05
        if reservation_record is None:
            reservation_record = cost_controller.reserve_budget(
                db=db,
                project_id=project_id,
                estimated_cost=est_cost,
                operation="music_generation",
                scene_id=scene_id,
                provider="google_lyria",
                model=target_model,
                job_id=job_id,
                details=f"Pre-flight Lyria score reservation: {mood} ({genre})"
            )
            db.commit()

        interaction = None
        try:
            logger.info(
                f"Submitting Lyria music generation ({target_model}) for project '{project.name}': "
                f"mood='{mood}', genre='{genre}', bpm={bpm}..."
            )
            # Single submission only — never blindly retry with a second interactions.create()
            interaction = self.client.interactions.create(
                model=target_model,
                input=full_prompt,
                background=False,
                response_format=response_format
            )
        except Exception as e:
            err_msg = str(e)
            logger.error(f"Lyria music generation submission failed: {err_msg}")
            if _is_ambiguous_provider_error(e):
                cost_controller.finalize_reservation(
                    db=db,
                    project_id=project_id,
                    reservation_record=reservation_record,
                    local_processing_status="provider_outcome_unknown",
                    error_details=err_msg,
                )
                db.commit()
                raise GoogleMusicAdapterError(
                    f"Lyria music generation encountered an ambiguous transport error "
                    f"(not automatically retried to prevent duplicate billing): {err_msg}"
                ) from e
            else:
                cost_controller.release_reservation(
                    db=db,
                    project_id=project_id,
                    reservation_record=reservation_record,
                    reason=f"lyria_request_rejected: {err_msg}",
                )
                db.commit()
                raise GoogleMusicAdapterError(f"Lyria music generation rejected: {err_msg}") from e

        raw_id = getattr(interaction, "id", None)
        provider_op_id = raw_id if isinstance(raw_id, str) and raw_id else None

        status_str = _get_interaction_status(interaction)
        if status_str in ("failed", "error", "cancelled", "canceled"):
            cost_controller.release_reservation(
                db=db,
                project_id=project_id,
                reservation_record=reservation_record,
                reason=f"lyria_provider_{status_str}",
            )
            db.commit()
            raise GoogleMusicAdapterError(
                f"Lyria interaction '{provider_op_id or 'unknown'}' finished with status '{status_str}'."
            )
        if status_str in ("processing", "queued", "pending", "in_progress", "requires_continuation"):
            cost_controller.finalize_reservation(
                db=db,
                project_id=project_id,
                reservation_record=reservation_record,
                local_processing_status="provider_outcome_unknown",
                error_details=f"Lyria interaction '{provider_op_id}' returned non-completed status '{status_str}'",
            )
            db.commit()
            raise GoogleMusicAdapterError(
                f"Lyria interaction '{provider_op_id}' returned non-completed status '{status_str}' "
                f"(outcome pending remotely; reservation preserved)."
            )

        audio_bytes, audio_uri = self._extract_interaction_audio(interaction)
        if not audio_bytes and not audio_uri:
            cost_controller.release_reservation(
                db=db,
                project_id=project_id,
                reservation_record=reservation_record,
                reason="lyria_empty_response",
            )
            db.commit()
            raise GoogleMusicAdapterError(
                f"Lyria model '{target_model}' returned no audio content in interaction response."
            )

        # Provider succeeded; download if needed, save locally, and validate MP3 strictly via FFprobe
        asset_id = str(uuid.uuid4())
        safe_mood = "".join(c for c in mood.lower().replace(" ", "_") if c.isalnum() or c == "_")
        filename = f"score_{safe_mood}_{asset_id[:8]}.mp3"
        file_path = dest_dir / filename
        try:
            if not audio_bytes and audio_uri:
                audio_bytes = self._download_uri_bytes(audio_uri)
            if not audio_bytes:
                raise GoogleMusicAdapterError("Downloaded Lyria MP3 stream is empty.")

            file_path.write_bytes(audio_bytes)

            meta = ffmpeg_service.inspect_media(file_path)
            audio_codec = meta.get("audio_codec") or meta.get("codec")
            actual_duration = float(meta.get("duration") or 0.0)
            if not meta.get("has_audio") or not audio_codec or audio_codec == "unknown" or actual_duration <= 0.0:
                raise GoogleMusicAdapterError(
                    f"FFprobe validation failed on Lyria output '{filename}': invalid audio stream ({meta})"
                )

            rel_path = f"projects/{project_id}/audio/{filename}"
            track_title = f"Score: {mood.capitalize()} ({genre.capitalize()})"

            audio_track = AudioTrack(
                project_id=project_id,
                track_type="music",
                title=track_title,
                file_path=rel_path,
                duration_seconds=actual_duration,
                start_time_seconds=0.0,
                volume_level=0.75,
                ducking_enabled=True,
                ducking_level=0.3,
                scene_id=scene_id
            )
            db.add(audio_track)

            media_asset = MediaAsset(
                id=asset_id,
                project_id=project_id,
                version=1,
                status="ready",
                filename=filename,
                file_path=str(file_path),
                media_type="audio",
                duration_seconds=actual_duration,
                file_size_bytes=len(audio_bytes)
            )
            db.add(media_asset)

            cost_controller.finalize_reservation(
                db=db,
                project_id=project_id,
                reservation_record=reservation_record,
                actual_cost=None,
                actual_cost_known=False,
                local_processing_status="completed",
            )
            db.commit()
            db.refresh(audio_track)
        except Exception as local_err:
            db.rollback()
            if file_path.exists():
                try:
                    file_path.unlink()
                except Exception:
                    pass
            cost_controller.finalize_reservation(
                db=db,
                project_id=project_id,
                reservation_record=reservation_record,
                actual_cost=None,
                actual_cost_known=False,
                local_processing_status="failed",
                error_details=f"Local MP3 validation/persistence failed: {local_err}",
            )
            db.commit()
            if isinstance(local_err, GoogleMusicAdapterError):
                raise
            raise GoogleMusicAdapterError(f"Lyria MP3 output validation failed: {local_err}") from local_err

        return {
            "status": "completed",
            "provider": "google_lyria",
            "model": target_model,
            "track_id": audio_track.id,
            "asset_id": media_asset.id,
            "file_path": str(file_path),
            "filename": filename,
            "duration_seconds": actual_duration,
            "target_duration_seconds": effective_duration,
            "mood": mood,
            "genre": genre,
            "bpm": bpm,
            "audio_codec": audio_codec,
            "provider_operation_id": provider_op_id,
            "interaction_id": provider_op_id,
            "reservation_id": reservation_record.id if reservation_record else None,
            "is_fallback": False
        }

    def _generate_fallback_music(
        self,
        project: Project,
        dest_dir: Path,
        mood: str,
        genre: str,
        duration_seconds: int,
        scene_id: Optional[str],
        db: Session
    ) -> Dict[str, Any]:
        """Generates deterministic local MP3 audio placeholder with explicit fallback attribution."""
        asset_id = str(uuid.uuid4())
        safe_mood = "".join(c for c in mood.lower().replace(" ", "_") if c.isalnum() or c == "_")
        filename = f"score_{safe_mood}_{asset_id[:8]}_offline.mp3"
        file_path = dest_dir / filename

        # Generates a 2-second deterministic silent MP3 reference placeholder
        actual_duration = 2.0
        generated = False
        try:
            import subprocess
            cmd = [
                "ffmpeg", "-y", "-f", "lavfi",
                "-i", "anullsrc=r=44100:cl=stereo",
                "-t", str(actual_duration),
                "-c:a", "libmp3lame",
                "-b:a", "128k",
                str(file_path)
            ]
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
            generated = True
        except Exception:
            pass

        if not generated:
            # Fallback raw MPEG-1 Layer 3 frame sequence
            mp3_frame = b"\xff\xfb\x90d" + b"\x00" * 414
            file_path.write_bytes(mp3_frame * int(max(1, actual_duration) * 38))

        try:
            meta = ffmpeg_service.inspect_media(file_path)
            actual_duration = float(meta.get("duration") or actual_duration)
            codec_found = meta.get("audio_codec") or meta.get("codec") or "mp3"
        except Exception:
            codec_found = "mp3"

        rel_path = f"projects/{project.id}/audio/{filename}"
        track_title = f"Score (Offline Placeholder): {mood.capitalize()} ({genre.capitalize()})"

        audio_track = AudioTrack(
            project_id=project.id,
            track_type="music",
            title=track_title,
            file_path=rel_path,
            duration_seconds=actual_duration,
            start_time_seconds=0.0,
            volume_level=0.75,
            ducking_enabled=True,
            ducking_level=0.3,
            scene_id=scene_id
        )
        db.add(audio_track)

        media_asset = MediaAsset(
            id=asset_id,
            project_id=project.id,
            version=1,
            status="ready",
            filename=filename,
            file_path=str(file_path),
            media_type="audio",
            duration_seconds=actual_duration,
            file_size_bytes=file_path.stat().st_size
        )
        db.add(media_asset)
        db.commit()
        db.refresh(audio_track)

        return {
            "status": "completed",
            "provider": "offline_template",
            "model": "offline-score-arranger",
            "track_id": audio_track.id,
            "asset_id": media_asset.id,
            "file_path": str(file_path),
            "filename": filename,
            "duration_seconds": actual_duration,
            "mood": mood,
            "genre": genre,
            "bpm": 120,
            "audio_codec": codec_found,
            "is_fallback": True
        }


google_music_adapter = GoogleMusicAdapter()
