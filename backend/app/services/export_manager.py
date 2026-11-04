"""
Export Manager Module.
Implements:
- Resolution presets (1080p, 4K, 720p, 9:16)
- Asynchronous local background execution via ThreadPoolExecutor
- Live progress reporting and error capture
- Strict separation between Preview and Final Master renders
- Output verification with FFprobe
- Cancellation and crash recovery on application restart
"""

import time
import shutil
import logging
import uuid
from pathlib import Path
from typing import Dict, Any, Optional
from concurrent.futures import ThreadPoolExecutor
from sqlalchemy.orm import Session
from backend.app.config import settings
from backend.app.models.entities import Project, ExportJob, AudioTrack
from backend.app.services.timeline_editor import timeline_editor_service
from backend.app.services.audio_service import audio_service
from backend.app.services.ffmpeg_service import ffmpeg_service
from backend.app.services.security import sanitize_filename

logger = logging.getLogger("export_manager")

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
    def __init__(self):
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="ExportWorker")
        self._cancelled_jobs = set()

    def submit_export_job_async(self, job_id: str) -> None:
        """Submits an export job for asynchronous background rendering."""
        self._executor.submit(self._run_export_task, job_id)

    def cancel_export_job(self, db: Session, job_id: str) -> bool:
        """Marks an export job as cancelled."""
        job = db.query(ExportJob).filter(ExportJob.id == job_id).first()
        if not job:
            return False
        if job.status in ("completed", "failed", "cancelled"):
            return False

        job.status = "cancelled"
        job.error_message = "Cancelled by user."
        db.commit()
        self._cancelled_jobs.add(job_id)
        return True

    def recover_interrupted_jobs(self, db: Session) -> int:
        """Recovers any export jobs interrupted by server restart."""
        interrupted = db.query(ExportJob).filter(
            ExportJob.status.in_(["rendering", "processing", "queued"])
        ).all()
        for j in interrupted:
            j.status = "failed"
            j.error_message = "Render interrupted by server restart. Ready to retry."
        if interrupted:
            db.commit()
            logger.info(f"Recovered {len(interrupted)} interrupted export jobs.")
        return len(interrupted)

    def _run_export_task(self, job_id: str) -> None:
        """Worker method running in background thread pool."""
        from backend.app.database import SessionLocal
        with SessionLocal() as db:
            job = db.query(ExportJob).filter(ExportJob.id == job_id).first()
            if not job or job.status == "cancelled" or job_id in self._cancelled_jobs:
                return

            project = db.query(Project).filter(Project.id == job.project_id).first()
            if not project:
                job.status = "failed"
                job.error_message = "Project not found"
                db.commit()
                return

            final_export_path = None
            try:
                # 1. Update status to processing / rendering
                job.status = "rendering"
                job.progress_pct = 15
                db.commit()

                # Check cancellation
                if job_id in self._cancelled_jobs:
                    return

                # 2. Render sequence
                is_preview_mode = (job.render_mode == "preview")
                allow_placeholders = bool(job.allow_placeholders)

                res_str = job.resolution or "1280x720"
                fps_val = job.fps or 24
                render_res = timeline_editor_service.render_timeline_sequence(
                    db=db,
                    project_id=project.id,
                    resolution=res_str,
                    fps=fps_val,
                    burn_subtitles=job.include_subtitles,
                    is_preview=is_preview_mode,
                    allow_placeholders=allow_placeholders
                )

                job.progress_pct = 60
                db.commit()

                if job_id in self._cancelled_jobs:
                    return

                # 3. Audio mixing & ducking
                base_video_path = Path(render_res["output_path"])
                audio_tracks = db.query(AudioTrack).filter(AudioTrack.project_id == project.id).all()

                audio_data = []
                for trk in audio_tracks:
                    trk_path = settings.data_dir / trk.file_path
                    if trk_path.exists() and trk_path.stat().st_size > 0:
                        audio_data.append({
                            "file_path": str(trk_path),
                            "track_type": trk.track_type,
                            "volume": trk.volume_level,
                            "fade_in_seconds": trk.fade_in_seconds,
                            "fade_out_seconds": trk.fade_out_seconds,
                            "start_time_seconds": trk.start_time_seconds,
                            "ducking": trk.ducking_enabled
                        })

                job.progress_pct = 80
                db.commit()

                if job_id in self._cancelled_jobs:
                    return

                # 4. Generate final export path in exports directory
                settings.exports_dir.mkdir(parents=True, exist_ok=True)
                safe_name = sanitize_filename(project.name.replace(" ", "_"))
                timestamp = int(time.time())
                unique_tag = uuid.uuid4().hex[:6]
                export_filename = f"{safe_name}_{job.preset_name}_{timestamp}_{unique_tag}.mp4"
                final_export_path = settings.exports_dir / export_filename

                if audio_data:
                    audio_service.mix_audio_tracks(
                        video_input=base_video_path,
                        audio_tracks=audio_data,
                        output_path=final_export_path
                    )
                else:
                    shutil.copy2(base_video_path, final_export_path)

                # 5. Inspect & validate final export with FFprobe
                if not final_export_path.exists() or final_export_path.stat().st_size == 0:
                    raise RuntimeError("Export output file was not generated or is empty.")

                is_valid, val_msg = ffmpeg_service.validate_video(final_export_path)
                if not is_valid:
                    raise RuntimeError(f"Exported video failed validation: {val_msg}")

                meta = ffmpeg_service.inspect_media(final_export_path)

                # 6. Mark completed
                rel_path = f"exports/{export_filename}"
                job.status = "completed"
                job.progress_pct = 100
                job.output_path = rel_path
                job.file_size_bytes = meta["size_bytes"]
                from datetime import datetime, timezone
                job.completed_at = datetime.now(timezone.utc).replace(tzinfo=None)
                db.commit()

            except Exception as e:
                logger.error(f"Export job {job_id} failed: {e}", exc_info=True)
                # Dọn dẹp file output hỏng
                if final_export_path and final_export_path.exists():
                    try:
                        final_export_path.unlink()
                    except OSError:
                        pass

                job.status = "failed"
                job.error_message = str(e)
                db.commit()

    def execute_export_job(self, db: Session, job_id: str) -> ExportJob:
        """Synchronous wrapper for tests and direct callers."""
        self._run_export_task(job_id)
        db.expire_all()
        return db.query(ExportJob).filter(ExportJob.id == job_id).first()


export_manager = ExportManager()
