"""
Project Backup & Disaster Recovery Service.
Handles full project export and restore:
- Complete relational metadata (Project, Scenes, Shots, Characters, Locations, Media, Audio, Exports, Pipeline States)
- Physical assets (Imported video clips, audio tracks, exported masters, thumbnails)
- Cryptographic SHA-256 manifest validation
- Strict path traversal defense on archive inspection
- Collision management: auto-remap to new project or explicit overwrite
"""

import os
import json
import time
import uuid
import shutil
import zipfile
import logging
import hashlib
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, Any, Optional, List, Tuple
from sqlalchemy.orm import Session
from sqlalchemy import text

from backend.app.config import settings
from backend.app.models.entities import (
    Project, Scene, Shot, Character, Location,
    MediaAsset, AudioTrack, ExportJob,
    PipelineStageState, PipelineCheckpoint, PipelineCheckpointAudit
)
from backend.app.services.security import compute_file_sha256, sanitize_filename, validate_safe_path

logger = logging.getLogger("project_backup")


class BackupError(Exception):
    """Base exception for backup operations."""
    pass


class RestoreSecurityError(BackupError):
    """Raised when an archive attempts path traversal or malicious extraction."""
    pass


class ProjectBackupService:
    @staticmethod
    def create_project_backup(db: Session, project_id: str) -> Path:
        """
        Exports a complete project into a verified ZIP archive containing:
        - manifest.json (project metadata & file checksums)
        - project_data.json (full database entity tree)
        - files/ (video assets, thumbnails, audio tracks, exports)
        """
        project = db.query(Project).filter(Project.id == project_id).first()
        if not project:
            raise ValueError(f"Project '{project_id}' not found.")

        settings.backup_dir.mkdir(parents=True, exist_ok=True)
        timestamp = int(time.time())
        safe_name = sanitize_filename(project.name.replace(" ", "_"))
        zip_filename = f"backup_{safe_name}_{project_id[:8]}_{timestamp}.zip"
        zip_path = settings.backup_dir / zip_filename

        # Query all related entities
        scenes = db.query(Scene).filter(Scene.project_id == project_id).all()
        shots = db.query(Shot).filter(Shot.project_id == project_id).all()
        characters = db.query(Character).filter(Character.project_id == project_id).all()
        locations = db.query(Location).filter(Location.project_id == project_id).all()
        media_assets = db.query(MediaAsset).filter(MediaAsset.project_id == project_id).all()
        audio_tracks = db.query(AudioTrack).filter(AudioTrack.project_id == project_id).all()
        export_jobs = db.query(ExportJob).filter(ExportJob.project_id == project_id).all()
        pipeline_stages = db.query(PipelineStageState).filter(PipelineStageState.project_id == project_id).all()
        pipeline_checkpoints = db.query(PipelineCheckpoint).filter(PipelineCheckpoint.project_id == project_id).all()
        pipeline_audits = db.query(PipelineCheckpointAudit).filter(PipelineCheckpointAudit.project_id == project_id).all()

        # Build relational data payload
        def dt_str(dt: Optional[datetime]) -> Optional[str]:
            return dt.isoformat() if dt else None

        project_data = {
            "project": {
                "id": project.id,
                "name": project.name,
                "logline": project.logline,
                "genre": project.genre,
                "aspect_ratio": project.aspect_ratio,
                "fps": project.fps,
                "target_duration_sec": project.target_duration_sec,
                "visual_style": project.visual_style,
                "budget_total": project.budget_total,
                "budget_spent": project.budget_spent,
                "created_at": dt_str(project.created_at),
                "updated_at": dt_str(project.updated_at)
            },
            "scenes": [
                {
                    "id": sc.id, "scene_number": sc.scene_number, "slugline": sc.slugline,
                    "title": sc.title, "summary": sc.summary, "script_content": sc.script_content,
                    "location_id": sc.location_id, "estimated_duration": sc.estimated_duration,
                    "budget_limit": sc.budget_limit, "budget_spent": sc.budget_spent,
                    "order_index": sc.order_index, "created_at": dt_str(sc.created_at)
                }
                for sc in scenes
            ],
            "shots": [
                {
                    "id": sh.id, "scene_id": sh.scene_id, "shot_number": sh.shot_number,
                    "shot_code": sh.shot_code, "duration_seconds": sh.duration_seconds,
                    "camera_angle": sh.camera_angle, "camera_movement": sh.camera_movement,
                    "visual_description": sh.visual_description, "lighting": sh.lighting,
                    "character_references": sh.character_references, "video_prompt": sh.video_prompt,
                    "negative_prompt": sh.negative_prompt, "seed": sh.seed,
                    "generation_status": sh.generation_status, "video_asset_id": sh.video_asset_id,
                    "thumbnail_path": sh.thumbnail_path, "flow_credits_spent": sh.flow_credits_spent,
                    "generation_attempts": sh.generation_attempts, "order_index": sh.order_index,
                    "created_at": dt_str(sh.created_at)
                }
                for sh in shots
            ],
            "characters": [
                {
                    "id": c.id, "name": c.name, "role": c.role, "age": c.age,
                    "facial_characteristics": c.facial_characteristics, "clothing": c.clothing,
                    "voice_profile": c.voice_profile, "personality": c.personality,
                    "visual_prompt_tags": c.visual_prompt_tags, "reference_image_path": c.reference_image_path,
                    "created_at": dt_str(c.created_at)
                }
                for c in characters
            ],
            "locations": [
                {
                    "id": l.id, "name": l.name, "description": l.description,
                    "time_of_day": l.time_of_day, "weather": l.weather,
                    "lighting_setup": l.lighting_setup, "visual_prompt_tags": l.visual_prompt_tags,
                    "reference_image_path": l.reference_image_path, "created_at": dt_str(l.created_at)
                }
                for l in locations
            ],
            "media_assets": [
                {
                    "id": m.id, "shot_id": m.shot_id, "version": m.version, "status": m.status,
                    "filename": m.filename, "file_path": m.file_path, "media_type": m.media_type,
                    "width": m.width, "height": m.height, "duration_seconds": m.duration_seconds,
                    "fps": m.fps, "codec": m.codec, "file_size_bytes": m.file_size_bytes,
                    "thumbnail_path": m.thumbnail_path, "checksum": m.checksum,
                    "auto_matched_shot_id": m.auto_matched_shot_id, "is_validated": m.is_validated,
                    "validation_notes": m.validation_notes, "created_at": dt_str(m.created_at)
                }
                for m in media_assets
            ],
            "audio_tracks": [
                {
                    "id": a.id, "track_type": a.track_type, "title": a.title,
                    "file_path": a.file_path, "duration_seconds": a.duration_seconds,
                    "start_time_seconds": a.start_time_seconds, "fade_in_seconds": a.fade_in_seconds,
                    "fade_out_seconds": a.fade_out_seconds, "volume_level": a.volume_level,
                    "ducking_enabled": a.ducking_enabled, "ducking_level": a.ducking_level,
                    "scene_id": a.scene_id, "shot_id": a.shot_id, "created_at": dt_str(a.created_at)
                }
                for a in audio_tracks
            ],
            "export_jobs": [
                {
                    "id": ej.id, "preset_name": ej.preset_name, "render_mode": ej.render_mode,
                    "allow_placeholders": ej.allow_placeholders, "resolution": ej.resolution,
                    "aspect_ratio": ej.aspect_ratio, "fps": ej.fps,
                    "include_subtitles": ej.include_subtitles, "status": ej.status,
                    "progress_pct": ej.progress_pct, "output_path": ej.output_path,
                    "file_size_bytes": ej.file_size_bytes, "error_message": ej.error_message,
                    "created_at": dt_str(ej.created_at), "completed_at": dt_str(ej.completed_at)
                }
                for ej in export_jobs
            ],
            "pipeline_stages": [
                {
                    "id": st.id, "stage_name": st.stage_name, "status": st.status,
                    "content_hash": st.content_hash, "consumed_input_hash": st.consumed_input_hash,
                    "error_message": st.error_message, "stage_metadata": st.stage_metadata,
                    "started_at": dt_str(st.started_at), "completed_at": dt_str(st.completed_at)
                }
                for st in pipeline_stages
            ],
            "pipeline_checkpoints": [
                {
                    "id": cp.id, "checkpoint_type": cp.checkpoint_type, "status": cp.status,
                    "artifact_type": cp.artifact_type, "artifact_version": cp.artifact_version,
                    "artifact_content_hash": cp.artifact_content_hash, "reviewer_name": cp.reviewer_name,
                    "feedback_notes": cp.feedback_notes, "decided_at": dt_str(cp.decided_at)
                }
                for cp in pipeline_checkpoints
            ],
            "pipeline_audits": [
                {
                    "id": au.id, "checkpoint_id": au.checkpoint_id, "checkpoint_type": au.checkpoint_type,
                    "decision": au.decision, "artifact_content_hash": au.artifact_content_hash,
                    "artifact_version": au.artifact_version, "reviewer_name": au.reviewer_name,
                    "feedback_notes": au.feedback_notes, "decided_at": dt_str(au.decided_at)
                }
                for au in pipeline_audits
            ]
        }

        # Collect physical files to archive
        files_to_pack: Dict[str, Path] = {}

        # Media assets
        for ma in media_assets:
            if ma.file_path:
                rp = settings.data_dir / ma.file_path
                if rp.exists() and rp.is_file():
                    files_to_pack[f"files/{ma.file_path}"] = rp
            if ma.thumbnail_path:
                tp = settings.data_dir / ma.thumbnail_path
                if tp.exists() and tp.is_file():
                    files_to_pack[f"files/{ma.thumbnail_path}"] = tp

        # Audio tracks
        for trk in audio_tracks:
            if trk.file_path:
                ap = settings.data_dir / trk.file_path
                if ap.exists() and ap.is_file():
                    files_to_pack[f"files/{trk.file_path}"] = ap

        # Exports
        for ej in export_jobs:
            if ej.output_path:
                ep = settings.data_dir / ej.output_path
                if ep.exists() and ep.is_file():
                    files_to_pack[f"files/{ej.output_path}"] = ep

        # Build Manifest with checksums
        manifest_files: Dict[str, Dict[str, Any]] = {}
        for arc_name, f_path in files_to_pack.items():
            checksum = compute_file_sha256(f_path)
            manifest_files[arc_name] = {
                "size_bytes": f_path.stat().st_size,
                "sha256": checksum
            }

        manifest = {
            "version": settings.version,
            "format": "ai_film_studio_project_backup_v1",
            "project_id": project.id,
            "project_name": project.name,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "entity_counts": {
                "scenes": len(scenes),
                "shots": len(shots),
                "characters": len(characters),
                "locations": len(locations),
                "media_assets": len(media_assets),
                "audio_tracks": len(audio_tracks),
                "export_jobs": len(export_jobs)
            },
            "file_count": len(files_to_pack),
            "files": manifest_files
        }

        # Write ZIP Archive
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zipf:
            zipf.writestr("manifest.json", json.dumps(manifest, indent=2))
            zipf.writestr("project_data.json", json.dumps(project_data, indent=2))
            for arc_name, f_path in files_to_pack.items():
                zipf.write(f_path, arcname=arc_name)

        logger.info(f"Created project backup at {zip_path} ({len(files_to_pack)} files)")
        return zip_path

    @staticmethod
    def restore_project_backup(
        db: Session,
        zip_path: Path,
        new_project_name: Optional[str] = None,
        overwrite: bool = False
    ) -> Dict[str, Any]:
        """
        Restores a project from a verified ZIP archive:
        1. Validates zip structure & path traversal defense.
        2. Validates manifest & file checksums.
        3. Manages project ID collision:
           - overwrite=False: Generates fresh UUIDs for project and related entities.
           - overwrite=True: Overwrites existing project with confirmation.
        4. Atomic database restoration & asset extraction.
        """
        if not zip_path.exists():
            raise FileNotFoundError(f"Backup archive not found at {zip_path}")

        temp_extract_dir = settings.temp_dir / f"restore_{uuid.uuid4().hex[:12]}"
        temp_extract_dir.mkdir(parents=True, exist_ok=True)

        try:
            with zipfile.ZipFile(zip_path, "r") as zipf:
                # 1. Path Traversal Defense: Ensure all archive paths are safe
                for name in zipf.namelist():
                    norm_p = os.path.normpath(name)
                    if norm_p.startswith("..") or os.path.isabs(norm_p) or "\\\\" in name:
                        raise RestoreSecurityError(f"Malicious path traversal detected in archive: '{name}'")
                    # Target path check
                    target_p = (temp_extract_dir / norm_p).resolve()
                    if not str(target_p).startswith(str(temp_extract_dir.resolve())):
                        raise RestoreSecurityError(f"Path escapes extraction directory: '{name}'")

                # Extract manifest & project data
                if "manifest.json" not in zipf.namelist() or "project_data.json" not in zipf.namelist():
                    raise BackupError("Invalid backup archive: missing manifest.json or project_data.json")

                manifest_raw = zipf.read("manifest.json").decode("utf-8")
                project_data_raw = zipf.read("project_data.json").decode("utf-8")
                manifest = json.loads(manifest_raw)
                project_data = json.loads(project_data_raw)

                # Extract all files into temp directory
                zipf.extractall(temp_extract_dir)

            # 2. Checksum Verification
            manifest_files = manifest.get("files", {})
            for arc_name, meta in manifest_files.items():
                extracted_file = temp_extract_dir / arc_name
                if not extracted_file.exists():
                    raise BackupError(f"Corrupt backup archive: expected file '{arc_name}' was not extracted.")
                actual_sha = compute_file_sha256(extracted_file)
                if actual_sha != meta["sha256"]:
                    raise BackupError(
                        f"Checksum mismatch on '{arc_name}'. Expected {meta['sha256']} but computed {actual_sha}."
                    )

            # 3. Handle Project ID Collision
            original_proj_id = project_data["project"]["id"]
            existing_proj = db.query(Project).filter(Project.id == original_proj_id).first()

            if existing_proj and not overwrite:
                # Remap project to new unique ID
                target_project_id = str(uuid.uuid4())
                target_project_name = new_project_name or f"{project_data['project']['name']} (Restored)"
                remap_mode = True
            else:
                target_project_id = original_proj_id
                target_project_name = new_project_name or project_data["project"]["name"]
                remap_mode = False
                if existing_proj and overwrite:
                    # Clear previous children for clean overwrite
                    db.query(Shot).filter(Shot.project_id == target_project_id).delete()
                    db.query(Scene).filter(Scene.project_id == target_project_id).delete()
                    db.query(Character).filter(Character.project_id == target_project_id).delete()
                    db.query(Location).filter(Location.project_id == target_project_id).delete()
                    db.query(MediaAsset).filter(MediaAsset.project_id == target_project_id).delete()
                    db.query(AudioTrack).filter(AudioTrack.project_id == target_project_id).delete()
                    db.query(ExportJob).filter(ExportJob.project_id == target_project_id).delete()
                    db.flush()

            # ID Mapping tables for remap mode
            scene_id_map: Dict[str, str] = {}
            shot_id_map: Dict[str, str] = {}
            loc_id_map: Dict[str, str] = {}
            media_id_map: Dict[str, str] = {}

            # Parse timestamps
            def parse_dt(s: Optional[str]) -> Optional[datetime]:
                return datetime.fromisoformat(s) if s else None

            # 4. Insert or Update Project Entity
            proj_data = project_data["project"]
            if existing_proj and overwrite:
                project = existing_proj
                project.name = target_project_name
                project.logline = proj_data["logline"]
                project.genre = proj_data["genre"]
                project.aspect_ratio = proj_data["aspect_ratio"]
                project.fps = proj_data["fps"]
                project.target_duration_sec = proj_data["target_duration_sec"]
                project.visual_style = proj_data["visual_style"]
                project.budget_total = proj_data["budget_total"]
                project.budget_spent = proj_data["budget_spent"]
            else:
                project = Project(
                    id=target_project_id,
                    name=target_project_name,
                    logline=proj_data["logline"],
                    genre=proj_data["genre"],
                    aspect_ratio=proj_data["aspect_ratio"],
                    fps=proj_data["fps"],
                    target_duration_sec=proj_data["target_duration_sec"],
                    visual_style=proj_data["visual_style"],
                    budget_total=proj_data["budget_total"],
                    budget_spent=proj_data["budget_spent"],
                    created_at=parse_dt(proj_data["created_at"]) or datetime.now(timezone.utc).replace(tzinfo=None)
                )
                db.add(project)
            db.flush()

            # Locations
            for ld in project_data.get("locations", []):
                new_loc_id = str(uuid.uuid4()) if remap_mode else ld["id"]
                loc_id_map[ld["id"]] = new_loc_id
                loc = Location(
                    id=new_loc_id,
                    project_id=target_project_id,
                    name=ld["name"],
                    description=ld["description"],
                    time_of_day=ld["time_of_day"],
                    weather=ld["weather"],
                    lighting_setup=ld["lighting_setup"],
                    visual_prompt_tags=ld["visual_prompt_tags"],
                    reference_image_path=ld["reference_image_path"] or "",
                    created_at=parse_dt(ld["created_at"]) or datetime.now(timezone.utc).replace(tzinfo=None)
                )
                db.add(loc)
            db.flush()

            # Characters
            for cd in project_data.get("characters", []):
                new_char_id = str(uuid.uuid4()) if remap_mode else cd["id"]
                ch = Character(
                    id=new_char_id,
                    project_id=target_project_id,
                    name=cd["name"],
                    role=cd["role"],
                    age=cd["age"],
                    facial_characteristics=cd["facial_characteristics"],
                    clothing=cd["clothing"],
                    voice_profile=cd["voice_profile"],
                    personality=cd["personality"],
                    visual_prompt_tags=cd["visual_prompt_tags"],
                    reference_image_path=cd["reference_image_path"] or "",
                    created_at=parse_dt(cd["created_at"]) or datetime.now(timezone.utc).replace(tzinfo=None)
                )
                db.add(ch)
            db.flush()

            # Scenes
            for sd in project_data.get("scenes", []):
                new_scene_id = str(uuid.uuid4()) if remap_mode else sd["id"]
                scene_id_map[sd["id"]] = new_scene_id
                sc = Scene(
                    id=new_scene_id,
                    project_id=target_project_id,
                    scene_number=sd["scene_number"],
                    slugline=sd["slugline"],
                    title=sd["title"],
                    summary=sd["summary"],
                    script_content=sd["script_content"],
                    location_id=loc_id_map.get(sd["location_id"]) if sd["location_id"] else None,
                    estimated_duration=sd["estimated_duration"],
                    budget_limit=sd["budget_limit"],
                    budget_spent=sd["budget_spent"],
                    order_index=sd["order_index"],
                    created_at=parse_dt(sd["created_at"]) or datetime.now(timezone.utc).replace(tzinfo=None)
                )
                db.add(sc)
            db.flush()

            # Media Assets (pre-create ID map for shot references)
            for md in project_data.get("media_assets", []):
                new_media_id = str(uuid.uuid4()) if remap_mode else md["id"]
                media_id_map[md["id"]] = new_media_id

            # Shots
            for shd in project_data.get("shots", []):
                new_shot_id = str(uuid.uuid4()) if remap_mode else shd["id"]
                shot_id_map[shd["id"]] = new_shot_id
                remapped_video_asset = media_id_map.get(shd["video_asset_id"]) if shd["video_asset_id"] else None
                sh = Shot(
                    id=new_shot_id,
                    scene_id=scene_id_map.get(shd["scene_id"], shd["scene_id"]),
                    project_id=target_project_id,
                    shot_number=shd["shot_number"],
                    shot_code=shd["shot_code"],
                    duration_seconds=shd["duration_seconds"],
                    camera_angle=shd["camera_angle"],
                    camera_movement=shd["camera_movement"],
                    visual_description=shd["visual_description"],
                    lighting=shd["lighting"],
                    character_references=shd["character_references"],
                    video_prompt=shd["video_prompt"],
                    negative_prompt=shd["negative_prompt"],
                    seed=shd["seed"],
                    generation_status=shd["generation_status"],
                    video_asset_id=remapped_video_asset,
                    thumbnail_path=shd["thumbnail_path"] or "",
                    flow_credits_spent=shd["flow_credits_spent"],
                    generation_attempts=shd["generation_attempts"],
                    order_index=shd["order_index"],
                    created_at=parse_dt(shd["created_at"]) or datetime.now(timezone.utc).replace(tzinfo=None)
                )
                db.add(sh)
            db.flush()

            # Media Assets entity insertion
            for md in project_data.get("media_assets", []):
                new_media_id = media_id_map[md["id"]]
                remapped_shot = shot_id_map.get(md["shot_id"]) if md.get("shot_id") else None
                ma = MediaAsset(
                    id=new_media_id,
                    project_id=target_project_id,
                    shot_id=remapped_shot,
                    version=md.get("version", 1),
                    status=md.get("status", "ready"),
                    filename=md["filename"],
                    file_path=md["file_path"],
                    media_type=md["media_type"],
                    width=md["width"],
                    height=md["height"],
                    duration_seconds=md["duration_seconds"],
                    fps=md["fps"],
                    codec=md["codec"],
                    file_size_bytes=md["file_size_bytes"],
                    thumbnail_path=md["thumbnail_path"] or "",
                    checksum=md["checksum"],
                    auto_matched_shot_id=shot_id_map.get(md.get("auto_matched_shot_id")),
                    is_validated=md.get("is_validated", True),
                    validation_notes=md.get("validation_notes", ""),
                    created_at=parse_dt(md["created_at"]) or datetime.now(timezone.utc).replace(tzinfo=None)
                )
                db.add(ma)

            # Audio Tracks
            for ad in project_data.get("audio_tracks", []):
                new_audio_id = str(uuid.uuid4()) if remap_mode else ad["id"]
                at = AudioTrack(
                    id=new_audio_id,
                    project_id=target_project_id,
                    track_type=ad["track_type"],
                    title=ad["title"],
                    file_path=ad["file_path"],
                    duration_seconds=ad["duration_seconds"],
                    start_time_seconds=ad["start_time_seconds"],
                    fade_in_seconds=ad.get("fade_in_seconds", 0.0),
                    fade_out_seconds=ad.get("fade_out_seconds", 0.0),
                    volume_level=ad["volume_level"],
                    ducking_enabled=ad["ducking_enabled"],
                    ducking_level=ad["ducking_level"],
                    scene_id=scene_id_map.get(ad.get("scene_id")),
                    shot_id=shot_id_map.get(ad.get("shot_id")),
                    created_at=parse_dt(ad["created_at"]) or datetime.now(timezone.utc).replace(tzinfo=None)
                )
                db.add(at)

            # Export Jobs
            for ejd in project_data.get("export_jobs", []):
                new_ej_id = str(uuid.uuid4()) if remap_mode else ejd["id"]
                ej = ExportJob(
                    id=new_ej_id,
                    project_id=target_project_id,
                    preset_name=ejd["preset_name"],
                    render_mode=ejd.get("render_mode", "final"),
                    allow_placeholders=ejd.get("allow_placeholders", False),
                    resolution=ejd["resolution"],
                    aspect_ratio=ejd["aspect_ratio"],
                    fps=ejd["fps"],
                    include_subtitles=ejd["include_subtitles"],
                    status=ejd["status"],
                    progress_pct=ejd["progress_pct"],
                    output_path=ejd["output_path"] or "",
                    file_size_bytes=ejd["file_size_bytes"],
                    error_message=ejd["error_message"] or "",
                    created_at=parse_dt(ejd["created_at"]) or datetime.now(timezone.utc).replace(tzinfo=None),
                    completed_at=parse_dt(ejd.get("completed_at"))
                )
                db.add(ej)

            # Commit all DB changes atomically
            db.commit()

            # 5. Copy extracted files to destination locations in settings.data_dir
            extracted_files_root = temp_extract_dir / "files"
            if extracted_files_root.exists():
                for root_dir, _, filenames in os.walk(extracted_files_root):
                    for fn in filenames:
                        src_f = Path(root_dir) / fn
                        rel_to_files = src_f.relative_to(extracted_files_root)
                        dest_f = settings.data_dir / rel_to_files
                        dest_f.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(src_f, dest_f)

            logger.info(f"Successfully restored project '{project.name}' (ID: {project.id})")
            return {
                "status": "success",
                "project_id": project.id,
                "project_name": project.name,
                "remapped": remap_mode,
                "restored_scenes_count": len(project_data.get("scenes", [])),
                "restored_shots_count": len(project_data.get("shots", [])),
                "restored_media_count": len(project_data.get("media_assets", []))
            }

        finally:
            if temp_extract_dir.exists():
                try:
                    shutil.rmtree(temp_extract_dir, ignore_errors=True)
                except Exception:
                    pass


project_backup_service = ProjectBackupService()
