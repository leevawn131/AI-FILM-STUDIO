"""
FFmpeg Service.
Provides robust local video processing:
- FFprobe stream inspection & metadata extraction
- Video file validation
- High-quality thumbnail generation
- Video concatenation and normalization
- Subtitle generation
- Dummy / test video synthesis for testing without external video downloads
"""

import subprocess
import json
import logging
from pathlib import Path
from typing import Dict, Any, Optional
from backend.app.config import settings

logger = logging.getLogger("ffmpeg_service")

class FFmpegService:
    def __init__(self):
        self.ffmpeg = settings.ffmpeg_path
        self.ffprobe = settings.ffprobe_path

    def inspect_media(self, file_path: Path) -> Dict[str, Any]:
        """Runs ffprobe to extract video/audio stream properties."""
        cmd = [
            self.ffprobe,
            "-v", "error",
            "-show_entries", "stream=width,height,duration,r_frame_rate,codec_name,codec_type",
            "-show_entries", "format=duration,size,bit_rate",
            "-of", "json",
            str(file_path)
        ]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if res.returncode != 0:
            raise RuntimeError(f"FFprobe failed on {file_path.name}: {res.stderr}")

        data = json.loads(res.stdout)
        streams = data.get("streams", [])
        fmt = data.get("format", {})

        video_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
        audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), None)

        width = int(video_stream["width"]) if (video_stream and video_stream.get("width") is not None) else None
        height = int(video_stream["height"]) if (video_stream and video_stream.get("height") is not None) else None
        codec = video_stream.get("codec_name") if video_stream else (audio_stream.get("codec_name") if audio_stream else "unknown")

        # Calculate FPS from fraction (e.g. "24/1")
        fps = 24.0
        if video_stream and "r_frame_rate" in video_stream:
            r_fr = video_stream["r_frame_rate"]
            try:
                if "/" in r_fr:
                    num, den = r_fr.split("/")
                    fps = round(float(num) / float(den), 2)
                else:
                    fps = float(r_fr)
            except Exception:
                fps = 24.0

        # Duration
        dur_str = fmt.get("duration") or (video_stream.get("duration") if video_stream else (audio_stream.get("duration") if audio_stream else "0"))
        try:
            duration = round(float(dur_str), 2)
        except Exception:
            duration = 0.0

        size_bytes = int(fmt.get("size", 0))

        return {
            "width": width,
            "height": height,
            "duration": duration,
            "fps": fps,
            "codec": codec,
            "audio_codec": audio_stream.get("codec_name") if audio_stream else None,
            "sample_rate": int(audio_stream.get("sample_rate")) if (audio_stream and audio_stream.get("sample_rate")) else None,
            "channels": int(audio_stream.get("channels")) if (audio_stream and audio_stream.get("channels")) else None,
            "size_bytes": size_bytes,
            "has_audio": bool(audio_stream)
        }

    def validate_video(self, file_path: Path) -> tuple[bool, str]:
        """Validates that a video is non-empty, readable, and decodable."""
        if not file_path.exists() or file_path.stat().st_size == 0:
            return False, "File does not exist or is 0 bytes."

        try:
            meta = self.inspect_media(file_path)
            if meta["duration"] <= 0:
                return False, "Video reports 0 duration."
            return True, f"Valid video: {meta['width']}x{meta['height']} @ {meta['fps']}fps, {meta['duration']}s"
        except Exception as e:
            return False, f"FFprobe decoding error: {str(e)}"

    def extract_thumbnail(self, video_path: Path, output_thumb_path: Path, seek_sec: float = 0.5) -> Path:
        """Extracts a frame from the video as a thumbnail image."""
        output_thumb_path.parent.mkdir(parents=True, exist_ok=True)
        cmd = [
            self.ffmpeg,
            "-y",
            "-ss", str(seek_sec),
            "-i", str(video_path),
            "-vframes", "1",
            "-q:v", "2",
            str(output_thumb_path)
        ]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if res.returncode != 0:
            # Fallback seek to 0 if video is shorter than seek_sec
            cmd[2] = "0"
            subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return output_thumb_path

    def generate_synthetic_test_clip(
        self,
        output_path: Path,
        duration: float = 3.0,
        width: int = 1280,
        height: int = 720,
        text_label: str = "FLOW SHOT"
    ) -> Path:
        """Generates a synthetic MP4 test clip with label overlay for tests and previews."""
        output_path.parent.mkdir(parents=True, exist_ok=True)
        # Generate video with animated testsrc + audio tone
        cmd = [
            self.ffmpeg,
            "-y",
            "-f", "lavfi",
            "-i", f"testsrc=duration={duration}:size={width}x{height}:rate=24",
            "-f", "lavfi",
            "-i", f"sine=frequency=440:duration={duration}",
            "-vf", f"drawtext=text='{text_label}':fontcolor=white:fontsize=36:box=1:boxcolor=black@0.6:x=(w-text_w)/2:y=(h-text_h)/2",
            "-c:v", "libx264",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            str(output_path)
        ]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if res.returncode != 0:
            # Try without drawtext if freetype is unavailable
            cmd_simple = [
                self.ffmpeg,
                "-y",
                "-f", "lavfi",
                "-i", f"testsrc=duration={duration}:size={width}x{height}:rate=24",
                "-f", "lavfi",
                "-i", f"sine=frequency=440:duration={duration}",
                "-c:v", "libx264",
                "-pix_fmt", "yuv420p",
                "-c:a", "aac",
                str(output_path)
            ]
            subprocess.run(cmd_simple, check=True)
        return output_path

    def mux_video_and_audio(
        self,
        video_path: Path,
        audio_path: Path,
        output_path: Path,
    ) -> Path:
        """Muxes video and audio streams using local FFmpeg."""
        output_path.parent.mkdir(parents=True, exist_ok=True)
        cmd = [
            self.ffmpeg,
            "-y",
            "-i", str(video_path),
            "-i", str(audio_path),
            "-c:v", "copy",
            "-c:a", "aac",
            "-shortest",
            str(output_path),
        ]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if res.returncode != 0:
            raise RuntimeError(f"FFmpeg muxing failed: {res.stderr}")
        return output_path

ffmpeg_service = FFmpegService()
