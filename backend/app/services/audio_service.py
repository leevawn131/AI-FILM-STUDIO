"""
Audio Service.
Implements:
- Local procedural audio synthesis (cinematic ambient BGM, drones, pads)
- Scratch voiceover audio track generation
- Multi-track audio mixing with volume control and timing offsets
- True sidechain audio ducking (ducking BGM beneath voiceover dialogue)
- Strict error propagation: NEVER silently copy raw video on mix failure
"""

import subprocess
import shutil
import logging
from pathlib import Path
from typing import Dict, Any, Optional, List
from sqlalchemy.orm import Session
from backend.app.config import settings
from backend.app.models.entities import Project, AudioTrack
from backend.app.services.ffmpeg_service import ffmpeg_service

logger = logging.getLogger("audio_service")


class AudioService:
    @staticmethod
    def generate_cinematic_bgm(
        project_id: str,
        title: str = "Cinematic Ambient Theme",
        duration: float = 30.0,
        mood: str = "atmospheric"
    ) -> Path:
        """Synthesizes rich atmospheric cinematic ambient background score using FFmpeg lavfi filters."""
        dest_dir = settings.projects_dir / project_id / "audio"
        dest_dir.mkdir(parents=True, exist_ok=True)
        out_path = dest_dir / f"bgm_{int(duration)}s_{mood}.mp3"

        # Generate harmonic drone chord with low-frequency rumble, flanger, and reverb
        lavfi_expr = (
            f"aevalsrc='0.3*sin(2*PI*55*t)+0.25*sin(2*PI*82.4*t)+0.2*sin(2*PI*110*t)+0.15*sin(2*PI*130.8*t)'"
            f":duration={duration}:sample_rate=44100"
        )

        cmd = [
            settings.ffmpeg_path,
            "-y",
            "-f", "lavfi",
            "-i", lavfi_expr,
            "-af", "flanger=delay=15:speed=0.2:regen=50,lowpass=f=450,volume=0.8",
            "-c:a", "libmp3lame",
            "-b:a", "192k",
            str(out_path)
        ]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if res.returncode != 0:
            # Simple fallback tone
            cmd_fallback = [
                settings.ffmpeg_path,
                "-y",
                "-f", "lavfi",
                "-i", f"sine=frequency=110:duration={duration}",
                "-c:a", "libmp3lame",
                str(out_path)
            ]
            subprocess.run(cmd_fallback, check=True)

        return out_path

    @staticmethod
    def generate_scratch_voiceover(
        project_id: str,
        text_content: str,
        character_name: str = "KAI",
        duration: float = 5.0
    ) -> Path:
        """Generates a scratch voiceover dialogue audio track using modulated audio tone and comm filter."""
        dest_dir = settings.projects_dir / project_id / "audio"
        dest_dir.mkdir(parents=True, exist_ok=True)
        safe_name = character_name.replace(" ", "_")
        out_path = dest_dir / f"vo_{safe_name}_{int(duration)}s.mp3"

        # Modulated radio comm voiceover pulse
        lavfi_expr = (
            f"aevalsrc='0.4*sin(2*PI*220*t)*sin(2*PI*3*t)'"
            f":duration={duration}:sample_rate=44100"
        )
        cmd = [
            settings.ffmpeg_path,
            "-y",
            "-f", "lavfi",
            "-i", lavfi_expr,
            "-af", "highpass=f=300,lowpass=f=3400,volume=1.0",
            "-c:a", "libmp3lame",
            "-b:a", "128k",
            str(out_path)
        ]
        subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        return out_path

    @staticmethod
    def mix_audio_tracks(
        video_input: Path,
        audio_tracks: List[Dict[str, Any]],
        output_path: Path
    ) -> Path:
        """
        Mixes background music, voiceover, and SFX with real sidechain ducking into the target video.
        
        Strict Contract:
        - If audio_tracks is empty, copies video as-is.
        - If mixing fails, raises RuntimeError with exact FFmpeg stderr.
        - NEVER silently copies unmixed video and pretends success.
        """
        if not video_input.exists():
            raise FileNotFoundError(f"Source video file not found for audio mixing: {video_input}")

        output_path.parent.mkdir(parents=True, exist_ok=True)

        # Filter valid existing tracks
        valid_tracks = []
        for t in audio_tracks:
            tp = Path(t["file_path"])
            if tp.exists() and tp.stat().st_size > 0:
                valid_tracks.append(t)

        if not valid_tracks:
            shutil.copy2(video_input, output_path)
            return output_path

        # Identify tracks for ducking
        bgm_track = next((t for t in valid_tracks if t.get("track_type") in ("music", "ambient")), None)
        vo_track = next((t for t in valid_tracks if t.get("track_type") == "voiceover"), None)
        other_tracks = [t for t in valid_tracks if t is not bgm_track and t is not vo_track]

        inputs = ["-i", str(video_input)]
        input_idx = 1
        filter_parts = []

        # Process BGM
        bgm_label = None
        if bgm_track:
            inputs.extend(["-i", str(bgm_track["file_path"])])
            vol = bgm_track.get("volume", 0.6)
            fade_in = bgm_track.get("fade_in_seconds", 0.0)
            fade_out = bgm_track.get("fade_out_seconds", 0.0)
            filters = [f"volume={vol}"]
            if fade_in > 0:
                filters.append(f"afade=t=in:ss=0:d={fade_in}")
            filter_parts.append(f"[{input_idx}:a]{','.join(filters)}[bgm_raw]")
            bgm_label = "bgm_raw"
            input_idx += 1

        # Process Voiceover
        vo_label = None
        if vo_track:
            inputs.extend(["-i", str(vo_track["file_path"])])
            vol = vo_track.get("volume", 1.2)
            delay_ms = int(float(vo_track.get("start_time_seconds", 0.0)) * 1000)
            filters = [f"volume={vol}"]
            if delay_ms > 0:
                filters.append(f"adelay={delay_ms}|{delay_ms}")
            filter_parts.append(f"[{input_idx}:a]{','.join(filters)}[vo_raw]")
            vo_label = "vo_raw"
            input_idx += 1

        # Process Other Tracks (SFX, Ambient)
        other_labels = []
        for idx, ot in enumerate(other_tracks):
            inputs.extend(["-i", str(ot["file_path"])])
            vol = ot.get("volume", 1.0)
            delay_ms = int(float(ot.get("start_time_seconds", 0.0)) * 1000)
            filters = [f"volume={vol}"]
            if delay_ms > 0:
                filters.append(f"adelay={delay_ms}|{delay_ms}")
            label = f"other_{idx}"
            filter_parts.append(f"[{input_idx}:a]{','.join(filters)}[{label}]")
            other_labels.append(label)
            input_idx += 1

        # Build Ducking & Mix Graph
        if bgm_label and vo_label:
            # True sidechain ducking: compress BGM when VO audio is active
            filter_parts.append(
                f"[{bgm_label}][{vo_label}]sidechaincompress=threshold=0.08:ratio=4:attack=20:release=300:makeup=1[ducked_bgm]"
            )
            tracks_to_mix = ["[ducked_bgm]", f"[{vo_label}]"] + [f"[{l}]" for l in other_labels]
        elif bgm_label:
            tracks_to_mix = [f"[{bgm_label}]"] + [f"[{l}]" for l in other_labels]
        elif vo_label:
            tracks_to_mix = [f"[{vo_label}]"] + [f"[{l}]" for l in other_labels]
        else:
            tracks_to_mix = [f"[{l}]" for l in other_labels]

        if len(tracks_to_mix) > 1:
            mix_inputs_str = "".join(tracks_to_mix)
            filter_parts.append(f"{mix_inputs_str}amix=inputs={len(tracks_to_mix)}:duration=first:dropout_transition=2[aout]")
            map_audio = "[aout]"
        elif len(tracks_to_mix) == 1:
            map_audio = tracks_to_mix[0]
        else:
            map_audio = "0:a"

        cmd = [
            settings.ffmpeg_path,
            "-y",
            *inputs,
            "-filter_complex", ";".join(filter_parts),
            "-map", "0:v",
            "-map", map_audio,
            "-c:v", "copy",
            "-c:a", "aac",
            "-b:a", "192k",
            str(output_path)
        ]

        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if res.returncode != 0:
            err_msg = res.stderr.decode("utf-8", errors="replace")
            # Enforce strict policy: fail clearly, do NOT silently copy raw video
            if output_path.exists():
                output_path.unlink(missing_ok=True)
            raise RuntimeError(f"FFmpeg audio mixing failed with exit code {res.returncode}: {err_msg}")

        # Validate resulting output
        is_valid, val_msg = ffmpeg_service.validate_video(output_path)
        if not is_valid:
            if output_path.exists():
                output_path.unlink(missing_ok=True)
            raise RuntimeError(f"Mixed video output failed validation: {val_msg}")

        return output_path


audio_service = AudioService()
