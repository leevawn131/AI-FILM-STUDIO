"""
Timeline Editor & FFmpeg Sequence Renderer.
Implements:
- Video sequence concatenation
- Uniform aspect ratio and framerate normalization
- Preview Mode (watermarked placeholders allowed) vs Final Mode (strict rejection of missing clips)
- Isolated UUID temporary working directories with guaranteed cleanup
- Verification of rendered video integrity via FFprobe
"""

import uuid
import shutil
import logging
import subprocess
import time
from pathlib import Path
from typing import List, Dict, Any, Optional
from sqlalchemy.orm import Session
from backend.app.config import settings
from backend.app.models.entities import Project, Scene, Shot, MediaAsset
from backend.app.services.ffmpeg_service import ffmpeg_service

logger = logging.getLogger("timeline_editor")


class TimelineEditorService:
    @staticmethod
    def render_timeline_sequence(
        db: Session,
        project_id: str,
        resolution: str = "1280x720",
        fps: int = 24,
        burn_subtitles: bool = True,
        is_preview: bool = False,
        allow_placeholders: bool = False
    ) -> Dict[str, Any]:
        """
        Stitches the project's timeline sequence into a single MP4 video.
        
        Strict Render Modes:
        - Preview Mode (is_preview=True or allow_placeholders=True): Placeholders are generated for missing clips
          and explicitly watermarked with '[PREVIEW PLACEHOLDER] Shot SCxx_SHyy'.
        - Final Render Mode (is_preview=False and not allow_placeholders): Fails immediately if ANY shot
          lacks an imported media video asset on disk, listing all missing shots.
        """
        project = db.query(Project).filter(Project.id == project_id).first()
        if not project:
            raise ValueError(f"Project '{project_id}' not found.")

        shots = db.query(Shot).filter(Shot.project_id == project_id).order_by(Shot.order_index).all()
        if not shots:
            raise ValueError("No shots in project timeline to render.")

        renders_dir = settings.projects_dir / project_id / "renders"
        renders_dir.mkdir(parents=True, exist_ok=True)

        w_str, h_str = resolution.split("x")
        target_w, target_h = int(w_str), int(h_str)

        # Isolated temporary working directory for this exact render job
        render_temp_dir = renders_dir / f"tmp_render_{uuid.uuid4().hex[:12]}"
        render_temp_dir.mkdir(parents=True, exist_ok=True)

        try:
            # 1. Check for missing video assets
            missing_shots = []
            valid_clips: List[Tuple[Shot, Optional[Path]]] = []

            for s in shots:
                clip_path: Optional[Path] = None
                if s.video_asset_id:
                    asset = db.query(MediaAsset).filter(MediaAsset.id == s.video_asset_id).first()
                    if asset and asset.file_path:
                        real_p = settings.data_dir / asset.file_path
                        if real_p.exists() and real_p.stat().st_size > 0:
                            clip_path = real_p

                if clip_path is None:
                    missing_shots.append(s.shot_code or f"Shot {s.shot_number}")
                valid_clips.append((s, clip_path))

            # In Final Render Mode, reject if any clips are missing
            if missing_shots and not is_preview and not allow_placeholders:
                raise ValueError(
                    f"Final Render Rejected: {len(missing_shots)} shot(s) lack imported video assets: "
                    f"{', '.join(missing_shots)}. Please import media in the Media Library or select Preview Mode."
                )

            # 2. Prepare normalized clips in temp directory
            normalized_clips: List[Path] = []
            placeholder_shots = []

            for idx, (shot, clip_p) in enumerate(valid_clips):
                norm_out_path = render_temp_dir / f"norm_{idx:03d}.mp4"

                if clip_p is not None:
                    # Normalize existing video
                    source_to_process = clip_p
                else:
                    # Generate explicitly watermarked preview placeholder clip
                    placeholder_shots.append(shot.shot_code)
                    placeholder_p = render_temp_dir / f"placeholder_{idx:03d}.mp4"
                    label_text = f"[PREVIEW PLACEHOLDER] {shot.shot_code} ({shot.camera_angle})"
                    ffmpeg_service.generate_synthetic_test_clip(
                        output_path=placeholder_p,
                        duration=shot.duration_seconds or 4.0,
                        width=target_w,
                        height=target_h,
                        text_label=label_text
                    )
                    source_to_process = placeholder_p

                # Normalization filter: scale, pad to uniform aspect ratio, fps
                vf = (
                    f"scale={target_w}:{target_h}:force_original_aspect_ratio=decrease,"
                    f"pad={target_w}:{target_h}:(ow-iw)/2:(oh-ih)/2,fps={fps}"
                )

                norm_cmd = [
                    settings.ffmpeg_path,
                    "-y",
                    "-i", str(source_to_process),
                    "-vf", vf,
                    "-c:v", "libx264",
                    "-preset", "veryfast",
                    "-crf", "22",
                    "-pix_fmt", "yuv420p",
                    "-c:a", "aac",
                    "-ar", "44100",
                    "-ac", "2",
                    str(norm_out_path)
                ]
                res_norm = subprocess.run(norm_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                if res_norm.returncode != 0:
                    err_msg = res_norm.stderr.decode("utf-8", errors="replace")
                    raise RuntimeError(f"FFmpeg normalization failed for clip {idx} ({shot.shot_code}): {err_msg}")

                normalized_clips.append(norm_out_path)

            # 3. Write concat demuxer file inside temp directory
            concat_txt = render_temp_dir / "concat_list.txt"
            with open(concat_txt, "w") as f:
                for p in normalized_clips:
                    f.write(f"file '{p.resolve()}'\n")

            # 4. Concatenate normalized clips into output video
            timestamp = int(time.time())
            prefix = "preview" if is_preview else "master"
            output_filename = f"{prefix}_{project_id[:8]}_{timestamp}.mp4"
            output_path = renders_dir / output_filename

            concat_cmd = [
                settings.ffmpeg_path,
                "-y",
                "-f", "concat",
                "-safe", "0",
                "-i", str(concat_txt),
                "-c:v", "libx264",
                "-preset", "fast",
                "-pix_fmt", "yuv420p",
                "-c:a", "aac",
                "-ar", "44100",
                "-ac", "2",
                str(output_path)
            ]
            res_concat = subprocess.run(concat_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if res_concat.returncode != 0:
                err_msg = res_concat.stderr.decode("utf-8", errors="replace")
                raise RuntimeError(f"FFmpeg concatenation failed: {err_msg}")

            # 5. Validate output video
            if not output_path.exists() or output_path.stat().st_size == 0:
                raise RuntimeError(f"Render failed: Output video file is empty or missing at {output_path}")

            is_valid, val_msg = ffmpeg_service.validate_video(output_path)
            if not is_valid:
                raise RuntimeError(f"Rendered video failed FFprobe validation: {val_msg}")

            meta = ffmpeg_service.inspect_media(output_path)
            rel_path = f"projects/{project_id}/renders/{output_filename}"

            return {
                "output_path": str(output_path),
                "filename": output_filename,
                "duration_seconds": meta["duration"],
                "resolution": f"{meta['width']}x{meta['height']}",
                "fps": meta["fps"],
                "file_size_bytes": meta["size_bytes"],
                "video_url": f"/media/{rel_path}",
                "is_preview": is_preview,
                "placeholder_count": len(placeholder_shots),
                "placeholder_shots": placeholder_shots
            }

        finally:
            # Guarantee thorough cleanup of the isolated temporary directory
            if render_temp_dir.exists():
                try:
                    shutil.rmtree(render_temp_dir, ignore_errors=True)
                except Exception as clean_err:
                    logger.warning(f"Failed to cleanup temp render dir {render_temp_dir}: {clean_err}")


timeline_editor_service = TimelineEditorService()
