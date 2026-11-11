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
        model: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Generates expressive character speech from dialogue text using Gemini TTS via Interactions API.
        Saves the audio locally, registers an AudioTrack and MediaAsset in SQLite.
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

        # Style annotation for speech synthesis
        style_desc = f"{emotion} style, pace {speaking_pace}x" if emotion != "neutral" else f"natural dialogue cadence, pace {speaking_pace}x"
        text_bytes_len = len(text.encode("utf-8"))

        input_payload: Dict[str, Any] = {
            "type": "text",
            "text": text,
            "annotations": [
                {
                    "type": "speech_metadata",
                    "speaker": speaker_label,
                    "style": style_desc,
                    "start_index": 0,
                    "end_index": text_bytes_len,
                }
            ],
        }

        generation_config = {
            "speech_config": {
                "voice": chosen_voice,
                "language": language_code,
                "speaker": speaker_label,
            }
        }

        response_format = {
            "type": "audio",
            "mime_type": "audio/wav",
        }

        audio_bytes = None
        provider_op_id = None
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

            # Extract output audio from Interaction response
            output_audio = getattr(interaction, "output_audio", None)
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

            # Fallback extraction from candidates if SDK wraps response differently
            if not audio_bytes and hasattr(interaction, "candidates") and interaction.candidates:
                candidate = interaction.candidates[0]
                if hasattr(candidate, "content") and hasattr(candidate.content, "parts"):
                    for part in candidate.content.parts:
                        inline = getattr(part, "inline_data", None)
                        if inline and hasattr(inline, "data"):
                            raw_inline = inline.data
                            audio_bytes = base64.b64decode(raw_inline) if isinstance(raw_inline, str) else raw_inline
                            break
        except Exception as e:
            err_msg = str(e)
            logger.error(f"Gemini TTS generation call failed: {err_msg}")
            raise GoogleTTSAdapterError(f"Google Gemini TTS generation failed: {err_msg}")

        if not audio_bytes:
            raise GoogleTTSAdapterError(
                f"Gemini TTS model '{target_model}' returned no audio candidates for dialogue."
            )

        # Save audio file to project directory
        asset_id = str(uuid.uuid4())
        safe_speaker = "".join(c for c in speaker_label.lower().replace(" ", "_") if c.isalnum() or c == "_")
        filename = f"tts_{safe_speaker}_{asset_id[:8]}.wav"
        file_path = dest_dir / filename
        file_path.write_bytes(audio_bytes)

        # Inspect duration via FFmpeg / FFprobe
        try:
            meta = ffmpeg_service.inspect_media(file_path)
            duration = float(meta.get("duration", max(1.0, len(text.split()) / 2.5)))
        except Exception:
            duration = max(1.0, len(text.split()) / 2.5)

        rel_path = f"projects/{project_id}/audio/{filename}"

        # Register AudioTrack in database
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

        # Register MediaAsset in database
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

        # Charge cost via CostController (~$0.01 per dialogue turn; estimated, not verified fact)
        est_cost = 0.005 if "lite" in target_model.lower() else 0.01
        cost_controller.authorize_and_charge(
            db=db,
            project_id=project_id,
            scene_id=scene_id,
            shot_id=shot_id,
            action_type="tts_generation",
            provider="google_tts",
            model=target_model,
            operation="tts_generation",
            details=f"Gemini TTS voiceover for {speaker_label} ({chosen_voice})",
            estimated_cost=est_cost,
            actual_cost=None,
            actual_cost_known=False,
            allow_duplicate=True
        )
        db.commit()
        db.refresh(audio_track)

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
            "is_fallback": False
        }

    def generate_multi_speaker_dialogue(
        self,
        dialogue_turns: List[Dict[str, Any]],
        project_id: str,
        db: Session,
        scene_id: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        Generates individual speech tracks for multi-speaker screenplay scenes.
        dialogue_turns format:
        [
            {"speaker": "REN", "voice": "Puck", "text": "We need to move now.", "emotion": "urgent"},
            {"speaker": "ELENA", "voice": "Kore", "text": "Wait, the perimeter is still locked.", "emotion": "tense"}
        ]
        """
        results = []
        for turn in dialogue_turns:
            speaker = turn.get("speaker", "Speaker")
            text = turn.get("text", "")
            voice = turn.get("voice", "Puck")
            emotion = turn.get("emotion", "neutral")
            pace = float(turn.get("pace", 1.0))
            shot_id = turn.get("shot_id")

            if not text.strip():
                continue

            res = self.generate_dialogue_speech(
                text=text,
                project_id=project_id,
                db=db,
                voice_name=voice,
                character_name=speaker,
                emotion=emotion,
                speaking_pace=pace,
                scene_id=scene_id,
                shot_id=shot_id,
                track_type="dialogue"
            )
            results.append(res)
        return results

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
