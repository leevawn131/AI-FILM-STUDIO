"""
Google Gemini TTS (Text-to-Speech) Audio Generation Adapter.
Officially supported Google Cloud AI speech synthesis via `google-genai` SDK.

Capabilities:
- Screenplay character dialogue generation using Gemini TTS (`gemini-3.8-flash-tts`)
- Cost-efficient alternative support (`gemini-3.8-flash-lite-tts`)
- Character voice profiles using official prebuilt voices:
  * Puck: Youthful, energetic, articulate
  * Charon: Deep, resonant, authoritative
  * Kore: Warm, expressive, empathetic
  * Fenrir: Gruff, dramatic, gritty
  * Aoede: Sophisticated, lyrical, calm
- Multi-speaker dialogue generation for screenplay scene exchanges
- Emotional tone delivery, language localization, and pacing controls
- Clean separation between transcript text, performance metadata, and voice configuration
- Direct local audio persistence in studio project directories
- Automatic registration in SQLite database as AudioTrack and MediaAsset
- Accurate Cloud AI cost accounting via CostController
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
from backend.app.models.entities import Project, Scene, Shot, Character, AudioTrack, MediaAsset
from backend.app.services.cost_controller import cost_controller
from backend.app.services.ffmpeg_service import ffmpeg_service

logger = logging.getLogger("google_tts_adapter")

SUPPORTED_VOICES = {
    "Puck": {"gender": "neutral", "tone": "energetic, youthful, clear", "suggested_roles": ["Protagonist", "Sidekick", "Youth"]},
    "Charon": {"gender": "male", "tone": "deep, resonant, authoritative", "suggested_roles": ["Mentor", "Antagonist", "Narrator"]},
    "Kore": {"gender": "female", "tone": "warm, expressive, melodic", "suggested_roles": ["Heroine", "Scientist", "Guide"]},
    "Fenrir": {"gender": "male", "tone": "gritty, dramatic, intense", "suggested_roles": ["Warrior", "Villain", "Veteran"]},
    "Aoede": {"gender": "female", "tone": "sophisticated, lyrical, serene", "suggested_roles": ["Queen", "AI Core", "Philosopher"]}
}


class GoogleTTSAdapterError(Exception):
    """Raised when Google Gemini TTS audio generation fails."""
    pass


def _is_ambiguous_provider_error(err: Exception) -> bool:
    """Returns True when an error leaves remote provider execution outcome ambiguous (e.g. network timeout)."""
    if isinstance(err, (TimeoutError, socket.timeout, ConnectionError)):
        return True
    msg = str(err).lower()
    # Explicit HTTP client rejections or auth/validation errors are non-billable confirmed failures
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


class GoogleTTSAdapter:
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
        return self._model or getattr(settings, "google_tts_model", "gemini-3.8-flash-tts")

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
                logger.warning(f"Could not initialize google-genai client for Gemini TTS: {e}")
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

    @staticmethod
    def get_available_voices() -> Dict[str, Any]:
        """Returns the dictionary of documented Google Gemini TTS voices."""
        return SUPPORTED_VOICES

    def _download_uri_bytes(self, uri: str) -> bytes:
        """Downloads audio bytes from an authenticated Files API URI or HTTP URL."""
        if self.client and hasattr(self.client, "files") and hasattr(self.client.files, "download"):
            try:
                downloaded = self.client.files.download(file=uri)
                if isinstance(downloaded, (bytes, bytearray)):
                    return bytes(downloaded)
            except Exception as dl_err:
                logger.warning(f"Files API download for '{uri}' encountered issue: {dl_err}")
        import urllib.request
        return urllib.request.urlopen(uri).read()

    def _extract_interaction_audio_bytes(self, interaction: Any) -> Tuple[Optional[bytes], Optional[str]]:
        """Extracts audio bytes or remote URI from an Interaction response."""
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

    def generate_dialogue_speech(
        self,
        text: str,
        project_id: str,
        db: Session,
        voice_name: str = "Puck",
        character_name: Optional[str] = None,
        character_id: Optional[str] = None,
        language_code: str = "en-US",
        emotion: str = "neutral",
        speaking_pace: float = 1.0,
        scene_id: Optional[str] = None,
        shot_id: Optional[str] = None,
        track_type: str = "dialogue", # "dialogue" or "voiceover"
        model: Optional[str] = None,
        reservation_record: Optional[Any] = None,
        job_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Generates expressive character speech from dialogue text using Gemini TTS via Interactions API.
        Follows reserve -> call provider -> finalize/release contract, saves audio locally,
        inspects with FFprobe, and registers AudioTrack and MediaAsset in SQLite.
        """
        project = db.query(Project).filter(Project.id == project_id).first()
        if not project:
            raise ValueError(f"Project '{project_id}' not found.")

        target_model = model or self.model
        dest_dir = settings.projects_dir / project_id / "audio"
        dest_dir.mkdir(parents=True, exist_ok=True)

        chosen_voice = voice_name if voice_name in SUPPORTED_VOICES else "Puck"
        speaker_label = character_name or "Character"

        if not self.is_available:
            logger.info("Google API key not configured for TTS. Generating offline deterministic audio reference.")
            return self._generate_fallback_audio(
                text=text,
                project=project,
                dest_dir=dest_dir,
                speaker_label=speaker_label,
                voice_name=chosen_voice,
                scene_id=scene_id,
                shot_id=shot_id,
                track_type=track_type,
                db=db
            )

        # Natural language delivery instruction in speech_metadata.style
        style_parts = []
        if emotion and emotion != "neutral":
            style_parts.append(f"{emotion} delivery")
        if speaking_pace and speaking_pace != 1.0:
            style_parts.append(f"pace {speaking_pace}x")
        if language_code and language_code != "en-US":
            style_parts.append(f"in {language_code}")
        style_desc = ", ".join(style_parts) if style_parts else "natural dialogue cadence"

        input_payload = [
            {
                "type": "user_input",
                "content": [
                    {
                        "type": "text",
                        "text": text,
                        "annotations": [
                            {
                                "type": "speech_metadata",
                                "speaker": speaker_label,
                                "style": style_desc,
                            }
                        ],
                    }
                ],
            }
        ]

        generation_config = {
            "speech_config": [
                {"voice": chosen_voice}
            ]
        }

        response_format = {
            "type": "audio"
        }

        # Pre-flight budget reservation BEFORE external Google API call
        est_cost = 0.005 if "lite" in target_model.lower() else 0.01
        if reservation_record is None:
            reservation_record = cost_controller.reserve_budget(
                db=db,
                project_id=project_id,
                estimated_cost=est_cost,
                operation="tts_generation",
                scene_id=scene_id,
                shot_id=shot_id,
                provider="google_tts",
                model=target_model,
                job_id=job_id,
                details=f"Pre-flight Gemini TTS reservation for {speaker_label} ({chosen_voice})"
            )
            db.commit()

        audio_bytes = None
        audio_uri = None
        provider_op_id = None
        interaction = None
        try:
            logger.info(
                f"Generating Gemini TTS audio for {speaker_label} ({chosen_voice}, {emotion}) "
                f"using {target_model} via Interactions API..."
            )
            interaction = self.client.interactions.create(
                model=target_model,
                input=input_payload,
                generation_config=generation_config,
                response_format=response_format
            )
            provider_op_id = getattr(interaction, "id", None)
        except Exception as e:
            err_msg = str(e)
            logger.error(f"Gemini TTS generation call failed: {err_msg}")
            if _is_ambiguous_provider_error(e):
                cost_controller.finalize_reservation(
                    db=db,
                    project_id=project_id,
                    reservation_record=reservation_record,
                    local_processing_status="provider_outcome_unknown",
                    error_details=err_msg,
                )
            else:
                cost_controller.release_reservation(
                    db=db,
                    project_id=project_id,
                    reservation_record=reservation_record,
                    reason=f"tts_provider_error: {err_msg}",
                )
            db.commit()
            raise GoogleTTSAdapterError(f"Google Gemini TTS generation failed: {err_msg}") from e

        status_str = _get_interaction_status(interaction)
        if status_str in ("failed", "error", "cancelled", "canceled"):
            cost_controller.release_reservation(
                db=db,
                project_id=project_id,
                reservation_record=reservation_record,
                reason=f"tts_provider_{status_str}",
            )
            db.commit()
            raise GoogleTTSAdapterError(
                f"Google Gemini TTS interaction '{provider_op_id}' finished with status '{status_str}'."
            )
        if status_str in ("processing", "queued", "pending", "in_progress", "requires_continuation"):
            cost_controller.finalize_reservation(
                db=db,
                project_id=project_id,
                reservation_record=reservation_record,
                local_processing_status="provider_outcome_unknown",
                error_details=f"TTS interaction '{provider_op_id}' returned non-completed status '{status_str}'",
            )
            db.commit()
            raise GoogleTTSAdapterError(
                f"Gemini TTS interaction '{provider_op_id}' returned non-completed status '{status_str}'."
            )

        audio_bytes, audio_uri = self._extract_interaction_audio_bytes(interaction)
        if not audio_bytes and not audio_uri:
            cost_controller.release_reservation(
                db=db,
                project_id=project_id,
                reservation_record=reservation_record,
                reason="tts_empty_response",
            )
            db.commit()
            raise GoogleTTSAdapterError(
                f"Gemini TTS model '{target_model}' returned no audio candidates for dialogue."
            )

        # Provider outcome SUCCEEDED; perform local download, FFprobe inspection & DB persistence
        asset_id = str(uuid.uuid4())
        safe_speaker = "".join(c for c in speaker_label.lower().replace(" ", "_") if c.isalnum() or c == "_")
        filename = f"tts_{safe_speaker}_{asset_id[:8]}.wav"
        file_path = dest_dir / filename
        try:
            if not audio_bytes and audio_uri:
                audio_bytes = self._download_uri_bytes(audio_uri)
            if not audio_bytes:
                raise GoogleTTSAdapterError("Downloaded TTS audio stream is empty.")

            file_path.write_bytes(audio_bytes)
            meta = ffmpeg_service.inspect_media(file_path)
            if not meta.get("has_audio"):
                raise GoogleTTSAdapterError(f"FFprobe validated file '{filename}' contains no audio stream.")
            probed_dur = float(meta.get("duration") or 0.0)
            duration = probed_dur if probed_dur > 0.0 else max(1.0, len(text.split()) / 2.5)

            rel_path = f"projects/{project_id}/audio/{filename}"
            track_title = f"{speaker_label}: \"{text[:35]}...\"" if len(text) > 35 else f"{speaker_label}: \"{text}\""
            audio_track = AudioTrack(
                project_id=project_id,
                track_type=track_type,
                title=track_title,
                file_path=rel_path,
                duration_seconds=duration,
                start_time_seconds=0.0,
                volume_level=1.1,
                ducking_enabled=False,
                ducking_level=0.3,
                scene_id=scene_id,
                shot_id=shot_id
            )
            db.add(audio_track)

            media_asset = MediaAsset(
                id=asset_id,
                project_id=project_id,
                shot_id=shot_id,
                version=1,
                status="ready",
                filename=filename,
                file_path=str(file_path),
                media_type="audio",
                duration_seconds=duration,
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
                error_details=str(local_err),
            )
            db.commit()
            if isinstance(local_err, GoogleTTSAdapterError):
                raise
            raise GoogleTTSAdapterError(f"Local TTS audio processing failed: {local_err}") from local_err

        return {
            "status": "completed",
            "provider": "google_tts",
            "model": target_model,
            "track_id": audio_track.id,
            "asset_id": media_asset.id,
            "file_path": str(file_path),
            "filename": filename,
            "duration_seconds": duration,
            "voice_name": chosen_voice,
            "character_name": speaker_label,
            "provider_operation_id": provider_op_id,
            "interaction_id": provider_op_id,
            "reservation_id": reservation_record.id if reservation_record else None,
            "is_fallback": False
        }

    def generate_multi_speaker_dialogue(
        self,
        dialogue_turns: List[Dict[str, Any]],
        project_id: str,
        db: Session,
        scene_id: Optional[str] = None,
        shot_id: Optional[str] = None,
        model: Optional[str] = None,
        split_turns: bool = False,
        reservation_record: Optional[Any] = None,
        job_id: Optional[str] = None
    ) -> Any:
        """
        Generates dialogue speech for multi-speaker screenplay scenes.
        Uses Google's documented multi-speaker configuration:
        - One `user_input` object containing multiple text `content` items in dialogue order,
          each with a `speech_metadata` annotation (`speaker`, `style`).
        - When 2 speakers: `generation_config.speech_config = {"speakers": [{"speaker": ..., "voice": ...}, ...]}`.
        - When 1 speaker: falls back to documented single-speaker list `[{"voice": ...}]`.
        - Enforces Gemini TTS multi-speaker limit of at most 2 speakers per request.
        - `response_format = {"type": "audio"}`.
        """
        valid_turns = [t for t in dialogue_turns if t.get("text", "").strip()]
        if not valid_turns:
            return [] if split_turns else {}

        if split_turns:
            results = []
            for turn in valid_turns:
                res = self.generate_dialogue_speech(
                    text=turn["text"],
                    project_id=project_id,
                    db=db,
                    voice_name=turn.get("voice", "Puck"),
                    character_name=turn.get("speaker", "Speaker"),
                    emotion=turn.get("emotion", "neutral"),
                    speaking_pace=float(turn.get("pace", 1.0)),
                    scene_id=scene_id,
                    shot_id=turn.get("shot_id") or shot_id,
                    track_type="dialogue",
                    model=model
                )
                results.append(res)
            return results

        # Collect unique speakers in dialogue appearance order and enforce max 2 speakers
        seen_speakers: List[str] = []
        speaker_configs: List[Dict[str, str]] = []
        seen_set = set()
        for turn in valid_turns:
            spk = str(turn.get("speaker") or "Speaker").strip() or "Speaker"
            vc = turn.get("voice", "Puck")
            if spk not in seen_set:
                seen_set.add(spk)
                seen_speakers.append(spk)
                chosen_v = vc if vc in SUPPORTED_VOICES else "Puck"
                speaker_configs.append({"speaker": spk, "voice": chosen_v})

        if len(speaker_configs) > 2:
            raise ValueError(
                f"Google Gemini multi-speaker TTS supports at most 2 speakers per request "
                f"(got {len(speaker_configs)}: {', '.join(seen_speakers)}). "
                f"Set split_turns=True to synthesize dialogue turns individually."
            )

        project = db.query(Project).filter(Project.id == project_id).first()
        if not project:
            raise ValueError(f"Project '{project_id}' not found.")

        target_model = model or self.model
        dest_dir = settings.projects_dir / project_id / "audio"
        dest_dir.mkdir(parents=True, exist_ok=True)

        if not self.is_available:
            logger.info("Google API key not configured for TTS. Generating offline reference dialogue.")
            combined_text = " ".join([t["text"] for t in valid_turns])
            return self._generate_fallback_audio(
                text=combined_text,
                project=project,
                dest_dir=dest_dir,
                speaker_label="MultiSpeaker" if len(speaker_configs) > 1 else speaker_configs[0]["speaker"],
                voice_name=speaker_configs[0]["voice"],
                scene_id=scene_id,
                shot_id=shot_id,
                track_type="dialogue",
                db=db
            )

        # Build ONE user_input object with ordered text content items and speech_metadata annotations
        content_items: List[Dict[str, Any]] = []
        for turn in valid_turns:
            spk = str(turn.get("speaker") or "Speaker").strip() or "Speaker"
            emo = turn.get("emotion", "neutral")
            pace = float(turn.get("pace", 1.0))
            style_parts = []
            if emo and emo != "neutral":
                style_parts.append(f"{emo} delivery")
            if pace and pace != 1.0:
                style_parts.append(f"pace {pace}x")
            style_desc = ", ".join(style_parts) if style_parts else "natural dialogue cadence"

            content_items.append({
                "type": "text",
                "text": turn["text"],
                "annotations": [
                    {
                        "type": "speech_metadata",
                        "speaker": spk,
                        "style": style_desc,
                    }
                ],
            })

        input_payload = [
            {
                "type": "user_input",
                "content": content_items,
            }
        ]

        is_multi_speaker = len(speaker_configs) > 1
        if is_multi_speaker:
            generation_config: Dict[str, Any] = {
                "speech_config": {
                    "speakers": speaker_configs
                }
            }
        else:
            generation_config = {
                "speech_config": [
                    {"voice": speaker_configs[0]["voice"]}
                ]
            }

        response_format = {
            "type": "audio"
        }

        # Pre-flight budget reservation BEFORE external Google API call
        per_speaker = 0.005 if "lite" in target_model.lower() else 0.01
        est_cost = per_speaker * len(speaker_configs)
        if reservation_record is None:
            reservation_record = cost_controller.reserve_budget(
                db=db,
                project_id=project_id,
                estimated_cost=est_cost,
                operation="tts_generation",
                scene_id=scene_id,
                shot_id=shot_id,
                provider="google_tts",
                model=target_model,
                job_id=job_id,
                details=f"Pre-flight Gemini TTS reservation ({', '.join(seen_speakers)})"
            )
            db.commit()

        audio_bytes = None
        audio_uri = None
        provider_op_id = None
        interaction = None
        try:
            logger.info(
                f"Generating Gemini TTS dialogue ({len(speaker_configs)} speaker(s), "
                f"{len(content_items)} turns) using {target_model} via Interactions API..."
            )
            interaction = self.client.interactions.create(
                model=target_model,
                input=input_payload,
                generation_config=generation_config,
                response_format=response_format
            )
            provider_op_id = getattr(interaction, "id", None)
        except Exception as e:
            err_msg = str(e)
            logger.error(f"Gemini multi-speaker TTS call failed: {err_msg}")
            if _is_ambiguous_provider_error(e):
                cost_controller.finalize_reservation(
                    db=db,
                    project_id=project_id,
                    reservation_record=reservation_record,
                    local_processing_status="provider_outcome_unknown",
                    error_details=err_msg,
                )
            else:
                cost_controller.release_reservation(
                    db=db,
                    project_id=project_id,
                    reservation_record=reservation_record,
                    reason=f"tts_provider_error: {err_msg}",
                )
            db.commit()
            raise GoogleTTSAdapterError(f"Google Gemini multi-speaker TTS failed: {err_msg}") from e

        status_str = _get_interaction_status(interaction)
        if status_str in ("failed", "error", "cancelled", "canceled"):
            cost_controller.release_reservation(
                db=db,
                project_id=project_id,
                reservation_record=reservation_record,
                reason=f"tts_provider_{status_str}",
            )
            db.commit()
            raise GoogleTTSAdapterError(
                f"Gemini TTS interaction '{provider_op_id}' finished with status '{status_str}'."
            )
        if status_str in ("processing", "queued", "pending", "in_progress", "requires_continuation"):
            cost_controller.finalize_reservation(
                db=db,
                project_id=project_id,
                reservation_record=reservation_record,
                local_processing_status="provider_outcome_unknown",
                error_details=f"Multi-speaker TTS '{provider_op_id}' returned non-completed status '{status_str}'",
            )
            db.commit()
            raise GoogleTTSAdapterError(
                f"Gemini TTS interaction '{provider_op_id}' returned non-completed status '{status_str}'."
            )

        audio_bytes, audio_uri = self._extract_interaction_audio_bytes(interaction)
        if not audio_bytes and not audio_uri:
            cost_controller.release_reservation(
                db=db,
                project_id=project_id,
                reservation_record=reservation_record,
                reason="tts_empty_response",
            )
            db.commit()
            raise GoogleTTSAdapterError(
                f"Gemini TTS model '{target_model}' returned no audio candidates for multi-speaker dialogue."
            )

        asset_id = str(uuid.uuid4())
        filename = f"tts_multispeaker_{scene_id or 'dialogue'}_{asset_id[:8]}.wav"
        file_path = dest_dir / filename
        try:
            if not audio_bytes and audio_uri:
                audio_bytes = self._download_uri_bytes(audio_uri)
            if not audio_bytes:
                raise GoogleTTSAdapterError("Downloaded multi-speaker TTS audio stream is empty.")

            file_path.write_bytes(audio_bytes)
            meta = ffmpeg_service.inspect_media(file_path)
            if not meta.get("has_audio"):
                raise GoogleTTSAdapterError(f"FFprobe validated file '{filename}' contains no audio stream.")
            probed_dur = float(meta.get("duration") or 0.0)
            duration = probed_dur if probed_dur > 0.0 else 4.0

            rel_path = f"projects/{project_id}/audio/{filename}"
            track_title = f"Dialogue: {', '.join(seen_speakers[:3])} Scene Exchange"

            audio_track = AudioTrack(
                project_id=project_id,
                track_type="dialogue",
                title=track_title,
                file_path=rel_path,
                duration_seconds=duration,
                start_time_seconds=0.0,
                volume_level=1.1,
                ducking_enabled=False,
                scene_id=scene_id,
                shot_id=shot_id
            )
            db.add(audio_track)

            media_asset = MediaAsset(
                id=asset_id,
                project_id=project_id,
                shot_id=shot_id,
                version=1,
                status="ready",
                filename=filename,
                file_path=str(file_path),
                media_type="audio",
                duration_seconds=duration,
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
                error_details=str(local_err),
            )
            db.commit()
            if isinstance(local_err, GoogleTTSAdapterError):
                raise
            raise GoogleTTSAdapterError(f"Local multi-speaker TTS processing failed: {local_err}") from local_err

        return {
            "status": "completed",
            "provider": "google_tts",
            "model": target_model,
            "track_id": audio_track.id,
            "asset_id": media_asset.id,
            "file_path": str(file_path),
            "filename": filename,
            "duration_seconds": duration,
            "speakers": seen_speakers,
            "is_multi_speaker": is_multi_speaker,
            "provider_operation_id": provider_op_id,
            "interaction_id": provider_op_id,
            "reservation_id": reservation_record.id if reservation_record else None,
            "is_fallback": False
        }

    def _generate_fallback_audio(
        self,
        text: str,
        project: Project,
        dest_dir: Path,
        speaker_label: str,
        voice_name: str,
        scene_id: Optional[str],
        shot_id: Optional[str],
        track_type: str,
        db: Session
    ) -> Dict[str, Any]:
        """Generates deterministic local speech preview with explicit fallback attribution."""
        asset_id = str(uuid.uuid4())
        safe_speaker = "".join(c for c in speaker_label.lower().replace(" ", "_") if c.isalnum() or c == "_")
        filename = f"tts_{safe_speaker}_{asset_id[:8]}_offline.wav"
        file_path = dest_dir / filename

        # Generate a small valid silent/beeping WAV header placeholder
        # 1 second of 16-bit 44.1kHz mono silence as offline mock
        num_samples = 44100
        byte_rate = 44100 * 2
        data_size = num_samples * 2
        total_size = 36 + data_size
        wav_header = bytearray()
        wav_header.extend(b'RIFF')
        wav_header.extend(total_size.to_bytes(4, 'little'))
        wav_header.extend(b'WAVEfmt ')
        wav_header.extend((16).to_bytes(4, 'little')) # Subchunk1Size
        wav_header.extend((1).to_bytes(2, 'little'))  # PCM format
        wav_header.extend((1).to_bytes(2, 'little'))  # Mono
        wav_header.extend((44100).to_bytes(4, 'little')) # Sample rate
        wav_header.extend(byte_rate.to_bytes(4, 'little')) # Byte rate
        wav_header.extend((2).to_bytes(2, 'little'))  # Block align
        wav_header.extend((16).to_bytes(2, 'little')) # Bits per sample
        wav_header.extend(b'data')
        wav_header.extend(data_size.to_bytes(4, 'little'))
        wav_header.extend(bytes(data_size)) # Silence samples

        file_path.write_bytes(bytes(wav_header))
        duration = 1.0

        rel_path = f"projects/{project.id}/audio/{filename}"
        track_title = f"{speaker_label} (Offline Reference): \"{text[:30]}...\""
        audio_track = AudioTrack(
            project_id=project.id,
            track_type=track_type,
            title=track_title,
            file_path=rel_path,
            duration_seconds=duration,
            start_time_seconds=0.0,
            volume_level=1.0,
            ducking_enabled=False,
            scene_id=scene_id,
            shot_id=shot_id
        )
        db.add(audio_track)

        media_asset = MediaAsset(
            id=asset_id,
            project_id=project.id,
            shot_id=shot_id,
            version=1,
            status="ready",
            filename=filename,
            file_path=str(file_path),
            media_type="audio",
            duration_seconds=duration,
            file_size_bytes=len(wav_header)
        )
        db.add(media_asset)
        db.commit()
        db.refresh(audio_track)

        return {
            "status": "completed",
            "provider": "offline_template",
            "model": "offline-speech-synthesizer",
            "track_id": audio_track.id,
            "asset_id": media_asset.id,
            "file_path": str(file_path),
            "filename": filename,
            "duration_seconds": duration,
            "voice_name": voice_name,
            "character_name": speaker_label,
            "is_fallback": True
        }


google_tts_adapter = GoogleTTSAdapter()
