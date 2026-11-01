"""
Media Watcher & Auto-Import Module.
Monitors watched directories for downloaded Google Flow videos.
Automatically:
- Detects video files (.mp4, .mov, .webm, .mkv)
- Validates media integrity with FFprobe
- Matches filename to Scene / Shot IDs (e.g. SC01_SH01)
- Extracts high-resolution thumbnail frames
- Organizes assets into project storage
- Links media assets directly to storyboard slots
"""

import os
import re
import shutil
import logging
from pathlib import Path
from typing import List, Dict, Any, Optional
from sqlalchemy.orm import Session
from backend.app.config import settings
from backend.app.models.entities import Project, Scene, Shot, MediaAsset
from backend.app.services.ffmpeg_service import ffmpeg_service

logger = logging.getLogger("media_watcher")

VIDEO_EXTENSIONS = {".mp4", ".mov", ".webm", ".mkv", ".avi"}

class MediaWatcherService:
    @staticmethod
    def match_shot_code(filename: str, shots: List[Shot]) -> Optional[Shot]:
        """Tries to match a filename to a storyboard shot."""
        clean_name = filename.upper()

        # 1. Direct shot_code match (e.g. SC01_SH01)
        for s in shots:
            if s.shot_code and s.shot_code.upper() in clean_name:
                return s

        # 2. Regex matching SC<num>_SH<num> or SCENE<num>_SHOT<num>
        match = re.search(r"SC(?:ENE)?_?(\d+)[_\-\s]*SH(?:OT)?_?(\d+)", clean_name)
        if match:
            sc_num = int(match.group(1))
            sh_num = int(match.group(2))
            for s in shots:
                if s.scene and s.scene.scene_number == sc_num and s.shot_number == sh_num:
                    return s
                if s.shot_number == sh_num:
                    return s

        # 3. Simple SH<num> match
        match_sh = re.search(r"SH(?:OT)?[_\-\s]*(\d+)", clean_name)
        if match_sh:
            sh_num = int(match_sh.group(1))
            for s in shots:
                if s.shot_number == sh_num:
                    return s

        return None

    @staticmethod
    def process_and_ingest_video(
        db: Session,
        project_id: str,
        source_file_path: Path,
        auto_link: bool = True
    ) -> MediaAsset:
        """Validates, extracts metadata & thumbnail, stores in project, and links to shot."""
        project = db.query(Project).filter(Project.id == project_id).first()
        if not project:
            raise ValueError("Project not found")

        # 1. Validate with FFprobe
        is_valid, validation_msg = ffmpeg_service.validate_video(source_file_path)
        if not is_valid:
            raise ValueError(f"Invalid video file '{source_file_path.name}': {validation_msg}")

        meta = ffmpeg_service.inspect_media(source_file_path)

        # 2. Destination directory in project
        dest_dir = settings.projects_dir / project_id / "imported_videos"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_file_path = dest_dir / source_file_path.name

        if source_file_path.resolve() != dest_file_path.resolve():
            shutil.copy2(source_file_path, dest_file_path)

        # 3. Generate thumbnail
        thumb_name = f"thumb_{Path(dest_file_path).stem}.jpg"
        thumb_path = dest_dir / thumb_name
        ffmpeg_service.extract_thumbnail(dest_file_path, thumb_path, seek_sec=0.5)

        rel_file_path = f"projects/{project_id}/imported_videos/{dest_file_path.name}"
        rel_thumb_path = f"projects/{project_id}/imported_videos/{thumb_name}"

        # 4. Check for auto-match with storyboard shot
        shots = db.query(Shot).filter(Shot.project_id == project_id).all()
        matched_shot = MediaWatcherService.match_shot_code(source_file_path.name, shots) if auto_link else None

        # 5. Create MediaAsset in DB
        media_asset = MediaAsset(
            project_id=project_id,
            filename=source_file_path.name,
            file_path=rel_file_path,
            media_type="video",
            width=meta["width"],
            height=meta["height"],
            duration_seconds=meta["duration"],
            fps=meta["fps"],
            codec=meta["codec"],
            file_size_bytes=meta["size_bytes"],
            thumbnail_path=rel_thumb_path,
            auto_matched_shot_id=matched_shot.id if matched_shot else None,
            is_validated=True,
            validation_notes=validation_msg
        )
        db.add(media_asset)
        db.commit()
        db.refresh(media_asset)

        # 6. Link to storyboard shot if matched
        if matched_shot:
            matched_shot.video_asset_id = media_asset.id
            matched_shot.generation_status = "imported"
            matched_shot.thumbnail_path = rel_thumb_path
            db.commit()

        return media_asset

    @staticmethod
    def scan_watched_directory(db: Session, project_id: str) -> Dict[str, Any]:
        """Scans the designated watch folder and ingests all new video files."""
        watch_dir = settings.watched_dir
        watch_dir.mkdir(parents=True, exist_ok=True)

        files = [p for p in watch_dir.iterdir() if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS]
        ingested = []
        errors = []

        for f in files:
            try:
                asset = MediaWatcherService.process_and_ingest_video(
                    db=db,
                    project_id=project_id,
                    source_file_path=f,
                    auto_link=True
                )
                ingested.append({
                    "id": asset.id,
                    "filename": asset.filename,
                    "matched_shot_id": asset.auto_matched_shot_id,
                    "duration": asset.duration_seconds
                })
            except Exception as e:
                errors.append({"file": f.name, "error": str(e)})

        return {
            "scanned_files_count": len(files),
            "ingested_count": len(ingested),
            "ingested": ingested,
            "errors": errors
        }

media_watcher_service = MediaWatcherService()
