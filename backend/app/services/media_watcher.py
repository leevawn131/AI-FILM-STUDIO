"""
Media Watcher & Auto-Import Module.
Monitors watched directories for downloaded Google Flow videos.
Automatically:
- Detects video files (.mp4, .mov, .webm, .mkv)
- Detects and skips in-flight / incomplete downloads
- Validates media integrity with FFprobe and magic byte headers
- Calculates SHA-256 checksums to prevent duplicate imports
- Matches filename to Scene / Shot IDs strictly without false cross-scene fallbacks
- Extracts version numbers (e.g. SC01_SH01_v002.mp4) and manages shot video versions
- Extracts high-resolution thumbnail frames
- Organizes assets into project storage using collision-resistant unique names
- Manages source files (moves to ingested/ or duplicates/) so subsequent scans don't continuously re-process
"""

import os
import re
import time
import shutil
import logging
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple
from sqlalchemy.orm import Session
from sqlalchemy import func
from backend.app.config import settings
from backend.app.models.entities import Project, Scene, Shot, MediaAsset
from backend.app.services.ffmpeg_service import ffmpeg_service
from backend.app.services.security import (
    sanitize_filename,
    generate_secure_filename,
    compute_file_sha256,
    verify_magic_bytes,
    validate_safe_path
)

logger = logging.getLogger("media_watcher")

VIDEO_EXTENSIONS = {".mp4", ".mov", ".webm", ".mkv", ".avi"}
INCOMPLETE_EXTENSIONS = {".tmp", ".crdownload", ".part", ".download"}


class MediaWatcherService:
    @staticmethod
    def match_shot_code(filename: str, shots: List[Shot]) -> Optional[Shot]:
        """
        Legacy compatibility wrapper around match_shot_detailed.
        Returns the matched shot if an unambiguous exact match was found, else None.
        """
        shot, is_ambiguous, _ = MediaWatcherService.match_shot_detailed(filename, shots)
        if is_ambiguous:
            return None
        return shot

    @staticmethod
    def match_shot_detailed(filename: str, shots: List[Shot]) -> Tuple[Optional[Shot], bool, str]:
        """
        Strictly matches a filename to a storyboard shot.
        Returns: (matched_shot, is_ambiguous, reason)

        Strict Matching Rules:
        1. If both Scene and Shot are specified (e.g. SC02_SH01), MUST match both.
           NEVER fall back to matching SC01_SH01 just because the shot number is 1.
        2. If multiple shots match (e.g. SH01 with multiple scenes), mark as ambiguous.
        3. Exact shot_code match (e.g. SC01_SH01) takes precedence if uniquely matched.
        """
        clean_name = filename.upper()

        # 1. Check for explicit Scene AND Shot pattern: SC<num>_SH<num> or SCENE<num>_SHOT<num>
        sc_sh_match = re.search(r"SC(?:ENE)?_?(\d+)[_\-\s]*SH(?:OT)?_?(\d+)", clean_name)
        if sc_sh_match:
            sc_num = int(sc_sh_match.group(1))
            sh_num = int(sc_sh_match.group(2))
            
            matching_shots = [
                s for s in shots
                if s.scene and s.scene.scene_number == sc_num and s.shot_number == sh_num
            ]
            if len(matching_shots) == 1:
                return matching_shots[0], False, "exact_scene_and_shot"
            elif len(matching_shots) > 1:
                return None, True, f"multiple_shots_matched_sc{sc_num}_sh{sh_num}"
            else:
                # CRITICAL: Both were specified, but no shot matched both.
                # Strictly DO NOT fallback to matching by shot_number alone!
                return None, False, f"scene_{sc_num}_shot_{sh_num}_not_found"

        # 2. Check for exact shot_code string match (e.g., SC01_SH01)
        direct_matches = [
            s for s in shots
            if s.shot_code and s.shot_code.upper() in clean_name
        ]
        if len(direct_matches) == 1:
            return direct_matches[0], False, "exact_shot_code"
        elif len(direct_matches) > 1:
            return None, True, "multiple_shot_codes_found_in_filename"

        # 3. Check for standalone Shot pattern (e.g., SH01 or SHOT_1)
        sh_only_match = re.search(r"(?:^|[^A-Z0-9])SH(?:OT)?[_\-\s]*(\d+)(?:[^A-Z0-9]|$)", clean_name)
        if sh_only_match:
            sh_num = int(sh_only_match.group(1))
            matching_by_shot = [s for s in shots if s.shot_number == sh_num]
            if len(matching_by_shot) == 1:
                return matching_by_shot[0], False, "single_shot_matched"
            elif len(matching_by_shot) > 1:
                # Multiple scenes have shot_number = sh_num; ambiguous!
                return None, True, f"ambiguous_shot_{sh_num}_in_multiple_scenes"

        return None, False, "no_match"

    @staticmethod
    def extract_version_number(filename: str, shot_id: Optional[str], db: Session) -> int:
        """
        Extracts version number from filename (e.g., _v002 -> 2).
        If not found in filename and shot_id is known, increments highest existing version for that shot.
        """
        # Look for _v<num> or _version<num>
        match = re.search(r"[_\-\s]V(?:ER)?(?:SION)?[_\-\s]*(\d+)", filename, re.IGNORECASE)
        if match:
            return int(match.group(1))

        if shot_id:
            max_v = db.query(func.max(MediaAsset.version)).filter(MediaAsset.shot_id == shot_id).scalar()
            if max_v is not None:
                return int(max_v) + 1

        return 1

    @staticmethod
    def is_file_in_flight(file_path: Path) -> bool:
        """
        Checks if a file is currently being copied, downloaded, or written to.
        Returns True if in-flight (should be skipped for now).
        """
        if any(file_path.name.lower().endswith(ext) for ext in INCOMPLETE_EXTENSIONS):
            return True

        if not file_path.exists():
            return True

        try:
            size1 = file_path.stat().st_size
            if size1 == 0:
                return True
            # Short sleep to detect active write streams
            time.sleep(0.05)
            size2 = file_path.stat().st_size
            if size1 != size2:
                return True
            # Attempt to open file in append mode to test lock
            with open(file_path, "a+b"):
                pass
            return False
        except (OSError, PermissionError):
            return True

    @staticmethod
    def process_and_ingest_video(
        db: Session,
        project_id: str,
        source_file_path: Path,
        auto_link: bool = True,
        checksum: Optional[str] = None,
        original_filename: Optional[str] = None
    ) -> MediaAsset:
        """
        Validates media integrity, prevents duplicate checksums, writes to project
        storage using secure unique names, extracts thumbnails, assigns versions, and links to shots.
        """
        project = db.query(Project).filter(Project.id == project_id).first()
        if not project:
            raise ValueError("Project not found")

        if not source_file_path.exists():
            raise ValueError(f"Source media file does not exist: {source_file_path}")

        # In-flight check
        if MediaWatcherService.is_file_in_flight(source_file_path):
            raise ValueError(f"File '{source_file_path.name}' is currently being written or locked by another process.")

        # 1. Enforce size limit
        file_size = source_file_path.stat().st_size
        max_size_bytes = settings.max_upload_size_mb * 1024 * 1024
        if file_size > max_size_bytes:
            raise ValueError(f"File size ({file_size // (1024*1024)}MB) exceeds limit of {settings.max_upload_size_mb}MB")

        # 2. Magic byte verification
        if not verify_magic_bytes(source_file_path, expected_type="video"):
            raise ValueError("Invalid media header: file content does not match video specification")

        # 3. SHA-256 Checksum and Duplicate Check
        file_checksum = checksum or compute_file_sha256(source_file_path)
        existing_dup = db.query(MediaAsset).filter(
            MediaAsset.project_id == project_id,
            MediaAsset.checksum == file_checksum
        ).first()
        if existing_dup:
            raise ValueError(
                f"Duplicate media asset detected: this file was already imported as '{existing_dup.filename}'"
            )

        # 4. Validate with FFprobe
        is_valid, validation_msg = ffmpeg_service.validate_video(source_file_path)
        if not is_valid:
            raise ValueError(f"Corrupt or unplayable video '{source_file_path.name}': {validation_msg}")

        meta = ffmpeg_service.inspect_media(source_file_path)

        # 5. Destination directory in project
        safe_project_id = sanitize_filename(project_id)
        dest_dir = validate_safe_path(settings.projects_dir / safe_project_id / "imported_videos", settings.projects_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)

        raw_display_name = original_filename or source_file_path.name
        display_name = sanitize_filename(raw_display_name)
        unique_video_name = generate_secure_filename(display_name, prefix="vid")
        dest_file_path = dest_dir / unique_video_name
        thumb_name = f"thumb_{dest_file_path.stem}.jpg"
        thumb_path = dest_dir / thumb_name

        try:
            # Atomic copy to project storage
            shutil.copy2(str(source_file_path), str(dest_file_path))

            # 6. Generate thumbnail
            ffmpeg_service.extract_thumbnail(dest_file_path, thumb_path, seek_sec=0.5)

            rel_file_path = f"projects/{safe_project_id}/imported_videos/{dest_file_path.name}"
            rel_thumb_path = f"projects/{safe_project_id}/imported_videos/{thumb_name}"

            # 7. Check for strict auto-match with storyboard shot
            matched_shot = None
            match_status_note = ""
            if auto_link:
                shots = db.query(Shot).filter(Shot.project_id == project_id).all()
                matched_shot, is_ambiguous, reason = MediaWatcherService.match_shot_detailed(display_name, shots)
                if is_ambiguous:
                    match_status_note = f"Ambiguous shot match ({reason}). Manual linking required."
                elif matched_shot:
                    match_status_note = f"Auto-matched to shot {matched_shot.shot_code} ({reason})."

            # 8. Determine version
            version_num = MediaWatcherService.extract_version_number(
                filename=display_name,
                shot_id=matched_shot.id if matched_shot else None,
                db=db
            )

            # 9. Create MediaAsset in DB
            media_asset = MediaAsset(
                project_id=project_id,
                shot_id=matched_shot.id if matched_shot else None,
                version=version_num,
                status="ready",
                filename=display_name,
                file_path=rel_file_path,
                media_type="video",
                width=meta["width"],
                height=meta["height"],
                duration_seconds=meta["duration"],
                fps=meta["fps"],
                codec=meta["codec"],
                file_size_bytes=meta["size_bytes"],
                thumbnail_path=rel_thumb_path,
                checksum=file_checksum,
                auto_matched_shot_id=matched_shot.id if matched_shot else None,
                is_validated=True,
                validation_notes=f"{validation_msg}. {match_status_note}".strip()
            )
            db.add(media_asset)
            db.commit()
            db.refresh(media_asset)

            # 10. Link to storyboard shot if matched
            if matched_shot:
                matched_shot.video_asset_id = media_asset.id
                matched_shot.generation_status = "imported"
                matched_shot.thumbnail_path = rel_thumb_path
                db.commit()

            return media_asset

        except Exception:
            # Clean up destination files on failure
            if dest_file_path.exists():
                try:
                    dest_file_path.unlink()
                except OSError:
                    pass
            if thumb_path.exists():
                try:
                    thumb_path.unlink()
                except OSError:
                    pass
            raise

    @staticmethod
    def _safely_move_source_file(file_path: Path, target_dir: Path) -> Path:
        """Moves source file to destination folder, renaming if filename already exists."""
        target_dir.mkdir(parents=True, exist_ok=True)
        dest = target_dir / file_path.name
        if dest.exists():
            stem = file_path.stem
            suffix = file_path.suffix
            dest = target_dir / f"{stem}_{int(time.time())}{suffix}"
        shutil.move(str(file_path), str(dest))
        return dest

    @staticmethod
    def scan_watched_directory(db: Session, project_id: str) -> Dict[str, Any]:
        """
        Scans the designated watch folder and ingests all new video files.
        Moves successfully ingested files to 'ingested/' and duplicate files to 'duplicates/'.
        """
        watch_dir = settings.watched_dir
        watch_dir.mkdir(parents=True, exist_ok=True)
        ingested_dir = watch_dir / "ingested"
        duplicates_dir = watch_dir / "duplicates"

        files = [
            p for p in watch_dir.iterdir()
            if p.is_file()
            and p.suffix.lower() in VIDEO_EXTENSIONS
            and p.parent == watch_dir
        ]

        ingested = []
        errors = []
        duplicates = []
        in_flight_skipped = []

        for f in sorted(files, key=lambda p: p.stat().st_mtime):
            # Skip files in-flight
            if MediaWatcherService.is_file_in_flight(f):
                in_flight_skipped.append(f.name)
                continue

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
                    "version": asset.version,
                    "duration": asset.duration_seconds
                })
                # Move to ingested directory so it won't be reprocessed
                MediaWatcherService._safely_move_source_file(f, ingested_dir)

            except Exception as e:
                err_msg = str(e)
                if "Duplicate media asset" in err_msg:
                    duplicates.append({"file": f.name, "reason": err_msg})
                    # Move duplicate out of watch root
                    try:
                        MediaWatcherService._safely_move_source_file(f, duplicates_dir)
                    except Exception as move_err:
                        logger.warning(f"Could not move duplicate {f.name}: {move_err}")
                else:
                    errors.append({"file": f.name, "error": err_msg})

        return {
            "scanned_files_count": len(files),
            "ingested_count": len(ingested),
            "duplicates_count": len(duplicates),
            "in_flight_count": len(in_flight_skipped),
            "ingested": ingested,
            "duplicates": duplicates,
            "errors": errors
        }


media_watcher_service = MediaWatcherService()
