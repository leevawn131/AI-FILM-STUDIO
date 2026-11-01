"""
Export Manager Module.
Implements:
- Resolution selection (1080p, 4K, 720p, 9:16)
- Master audio mixing & ducking
- Subtitle burn-in rendering
- Export job execution and status tracking
"""

import time
import shutil
from pathlib import Path
from typing import Dict, Any, Optional
from sqlalchemy.orm import Session
from backend.app.config import settings
from backend.app.models.entities import Project, ExportJob, AudioTrack
from backend.app.services.timeline_editor import timeline_editor_service
from backend.app.services.audio_service import audio_service
from backend.app.services.ffmpeg_service import ffmpeg_service

PRESET_CONFIGS = {
    "1080p_cinematic": {
        "name": "1080p Cinematic Master",
        "resolution": "1920x1080",
        "aspect_ratio": "16:9",
        "fps": 24,
        "description": "Standard high-definition widescreen cinematic master."
    },
    "4k_master": {
        "name": "4K Ultra-HD Master",
        "resolution": "3840x2160",
        "aspect_ratio": "16:9",
        "fps": 24,
        "description": "Maximum resolution 4K master suitable for theatrical projection."
    },
    "vertical_shorts": {
        "name": "9:16 Vertical Mobile",
        "resolution": "1080x1920",
        "aspect_ratio": "9:16",
        "fps": 30,
        "description": "Vertical format optimized for Shorts, TikTok, and Reels."
    },
    "720p_fast": {
        "name": "720p Fast Preview",
        "resolution": "1280x720",
        "aspect_ratio": "16:9",
        "fps": 24,
        "description": "Fast rendering preset for rapid review cuts."
    }
}

class ExportManager:
    @staticmethod
    def execute_export_job(db: Session, job_id: str) -> ExportJob:
        job = db.query(ExportJob).filter(ExportJob.id == job_id).first()
        if not job:
            raise ValueError("Export job not found")

        project = db.query(Project).filter(Project.id == job.project_id).first()
        if not project:
            raise ValueError("Project not found")

        try:
            # 1. Update status to rendering
            job.status = "rendering"
            job.progress_pct = 25
            db.commit()

            # 2. Render base video sequence
            res_str = job.resolution or "1280x720"
            fps_val = job.fps or 24
            render_res = timeline_editor_service.render_timeline_sequence(
                db=db,
                project_id=project.id,
                resolution=res_str,
                fps=fps_val,
                burn_subtitles=job.include_subtitles
            )

            job.progress_pct = 60
            db.commit()

            # 3. Audio mixing & ducking
            base_video_path = Path(render_res["output_path"])
            audio_tracks = db.query(AudioTrack).filter(AudioTrack.project_id == project.id).all()

            audio_data = []
            for trk in audio_tracks:
                trk_path = settings.data_dir / trk.file_path
                if trk_path.exists():
                    audio_data.append({
                        "file_path": str(trk_path),
                        "track_type": trk.track_type,
                        "volume": trk.volume_level,
                        "ducking": trk.ducking_enabled
                    })

            job.progress_pct = 80
            db.commit()

            # 4. Final output file in exports directory
            settings.exports_dir.mkdir(parents=True, exist_ok=True)
            export_filename = f"{project.name.replace(' ', '_')}_{job.preset_name}_{int(time.time())}.mp4"
            final_export_path = settings.exports_dir / export_filename

            if audio_data:
                audio_service.mix_audio_tracks(
                    video_input=base_video_path,
                    audio_tracks=audio_data,
                    output_path=final_export_path
                )
            else:
                shutil.copy2(base_video_path, final_export_path)

            # 5. Inspect final exported file
            meta = ffmpeg_service.inspect_media(final_export_path)

            rel_path = f"exports/{export_filename}"
            job.status = "completed"
            job.progress_pct = 100
            job.output_path = rel_path
            job.file_size_bytes = meta["size_bytes"]
            db.commit()
            db.refresh(job)
            return job

        except Exception as e:
            job.status = "failed"
            job.error_message = str(e)
            db.commit()
            db.refresh(job)
            raise e

export_manager = ExportManager()
