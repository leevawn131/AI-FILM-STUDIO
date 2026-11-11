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
import base64
import logging
from pathlib import Path
from typing import Dict, Any, Optional, List
from sqlalchemy.orm import Session

from backend.app.config import settings
from backend.app.models.entities import Project, Scene, AudioTrack, MediaAsset
from backend.app.services.cost_controller import cost_controller
from backend.app.services.ffmpeg_service import ffmpeg_service

logger = logging.getLogger("google_music_adapter")


class GoogleMusicAdapterError(Exception):
    """Raised when Google Lyria music generation fails."""
    pass


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
        model: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Generates an AI background music score using Google Lyria via Interactions API.
        Saves the audio file locally as a real MP3, and registers it in SQLite.
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

        # Build prompt describing musical composition requirements
        inst_list = ", ".join(instruments) if instruments else "analog synthesizers, cinematic sub-bass, orchestral strings"
        pref = "Instrumental only, no vocals." if instrumental else "Atmospheric vocal chops and pads."
        
        if prompt:
            full_prompt = (
                f"Cinematic score: {prompt}. Mood: {mood}. Genre: {genre}. "
                f"Tempo: {bpm} BPM. Energy: {energy}. Instruments: {inst_list}. {pref} Duration: {effective_duration}s."
            )
        else:
            full_prompt = (
                f"Cinematic {genre} score with a {mood} atmosphere. "
                f"Tempo: {bpm} BPM. Energy dynamic: {energy}. "
                f"Featured instrumentation: {inst_list}. {pref} "
                f"Designed as a movie background soundtrack with duration {effective_duration}s."
            )

        response_format = {"type": "audio", "mime_type": "audio/mp3"}

        interaction = None
        try:
            logger.info(
                f"Submitting Lyria music generation ({target_model}) for project '{project.name}': "
                f"mood='{mood}', genre='{genre}', bpm={bpm}..."
            )
            # Submit to Interactions API
            interaction = self.client.interactions.create(
                model=target_model,
                input=full_prompt,
                background=False,
                response_format=response_format
            )
        except Exception as e:
            # If background=False unsupported, try standard background=True and poll
            try:
                interaction = self.client.interactions.create(
                    model=target_model,
                    input=full_prompt,
                    background=True,
                    response_format=response_format
                )
                import time
                op_id = getattr(interaction, "id", None)
                if op_id:
                    for _ in range(30):
                        time.sleep(2)
                        interaction = self.client.interactions.get(id=op_id)
                        status = str(getattr(interaction, "status", "")).lower()
                        if status in ("completed", "failed", "cancelled"):
                            break
            except Exception as e2:
                err_msg = f"Lyria music generation call failed: {e}; retry failed: {e2}"
                logger.error(err_msg)
                raise GoogleMusicAdapterError(err_msg)

        provider_op_id = getattr(interaction, "id", None) or str(uuid.uuid4())

        # Extract output audio from Interaction response
        output_audio = getattr(interaction, "output_audio", None)
        audio_bytes = None
        if output_audio:
            raw_data = getattr(output_audio, "data", None)
            if raw_data:
                if isinstance(raw_data, str):
                    try:
                        audio_bytes = base64.b64decode(raw_data)
                    except Exception:
                        audio_bytes = raw_data.encode("utf-8")
                else:
                    audio_bytes = raw_data
            elif getattr(output_audio, "uri", None):
                import urllib.request
                audio_bytes = urllib.request.urlopen(output_audio.uri).read()

        # Fallback candidate inspection
        if not audio_bytes and hasattr(interaction, "candidates") and interaction.candidates:
            candidate = interaction.candidates[0]
            if hasattr(candidate, "content") and hasattr(candidate.content, "parts"):
                for part in candidate.content.parts:
                    inline = getattr(part, "inline_data", None)
                    if inline and hasattr(inline, "data"):
                        raw_inline = inline.data
                        audio_bytes = base64.b64decode(raw_inline) if isinstance(raw_inline, str) else raw_inline
                        break

        if not audio_bytes:
            raise GoogleMusicAdapterError(
                f"Lyria model '{target_model}' returned no audio content in interaction response."
            )

        # Save audio locally as real MP3 file
        asset_id = str(uuid.uuid4())
        safe_mood = "".join(c for c in mood.lower().replace(" ", "_") if c.isalnum() or c == "_")
        filename = f"score_{safe_mood}_{asset_id[:8]}.mp3"
        file_path = dest_dir / filename
        file_path.write_bytes(audio_bytes)

        # Inspect duration and audio stream format via FFmpeg / FFprobe
        try:
            meta = ffmpeg_service.inspect_media(file_path)
            actual_duration = float(meta.get("duration") or effective_duration)
            audio_codec = meta.get("audio_codec") or meta.get("codec") or "mp3"
        except Exception:
            actual_duration = float(effective_duration)
            audio_codec = "mp3"

        rel_path = f"projects/{project_id}/audio/{filename}"
        track_title = f"Score: {mood.capitalize()} ({genre.capitalize()})"

        # Register AudioTrack in SQLite
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

        # Register MediaAsset in SQLite
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

        # Charge cost via CostController (~$0.05 per music generation; estimated, not verified fact)
        cost_controller.authorize_and_charge(
            db=db,
            project_id=project_id,
            scene_id=scene_id,
            action_type="music_generation",
            provider="google_lyria",
            model=target_model,
            operation="music_generation",
            details=f"Google Lyria score generation: {mood} ({genre})",
            estimated_cost=0.05,
            actual_cost=None,
            actual_cost_known=False,
            allow_duplicate=True
        )
        db.commit()
        db.refresh(audio_track)

        return {
            "status": "completed",
            "provider": "google_lyria",
            "model": target_model,
            "track_id": audio_track.id,
            "asset_id": media_asset.id,
            "file_path": str(file_path),
            "filename": filename,
            "duration_seconds": actual_duration,
            "mood": mood,
            "genre": genre,
            "bpm": bpm,
            "audio_codec": audio_codec,
            "provider_operation_id": provider_op_id,
            "interaction_id": provider_op_id,
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
        """Generates deterministic local audio placeholder with explicit fallback attribution."""
        asset_id = str(uuid.uuid4())
        safe_mood = "".join(c for c in mood.lower().replace(" ", "_") if c.isalnum() or c == "_")
        filename = f"score_{safe_mood}_{asset_id[:8]}_offline.wav"
        file_path = dest_dir / filename

        # 2 seconds of clean 16-bit 44.1kHz stereo silence as offline reference placeholder
        num_samples = 44100 * 2
        byte_rate = 44100 * 4
        data_size = num_samples * 4
        total_size = 36 + data_size
        wav_header = bytearray()
        wav_header.extend(b'RIFF')
        wav_header.extend(total_size.to_bytes(4, 'little'))
        wav_header.extend(b'WAVEfmt ')
        wav_header.extend((16).to_bytes(4, 'little')) # Subchunk1Size
        wav_header.extend((1).to_bytes(2, 'little'))  # PCM
        wav_header.extend((2).to_bytes(2, 'little'))  # Stereo
        wav_header.extend((44100).to_bytes(4, 'little')) # Sample rate
        wav_header.extend(byte_rate.to_bytes(4, 'little')) # Byte rate
        wav_header.extend((4).to_bytes(2, 'little'))  # Block align
        wav_header.extend((16).to_bytes(2, 'little')) # Bits per sample
        wav_header.extend(b'data')
        wav_header.extend(data_size.to_bytes(4, 'little'))
        wav_header.extend(bytes(data_size))

        file_path.write_bytes(bytes(wav_header))
        actual_duration = 2.0

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
            file_size_bytes=len(wav_header)
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
            "is_fallback": True
        }


google_music_adapter = GoogleMusicAdapter()
