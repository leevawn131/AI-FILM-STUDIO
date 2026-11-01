"""
Persistent Staged Upload Operation Lifecycle and Recovery Manager.

Implements:
- STAGED -> DB_COMMITTED -> FINALIZED / FAILED lifecycle.
- Persistent tracking of temporary and destination paths with entity linkage.
- Idempotent finalization with atomic filesystem moves.
- Preservation of previous asset until new asset is finalized on disk.
- Startup recovery:
  - Finalizes DB_COMMITTED uploads when files are staged or already moved.
  - Detects lost/missing files and safely reverts entity references to prevent broken pointers.
  - Cleans expired STAGED operations and orphaned .tmp files.
- Storage root traversal protection.
"""

import os
import shutil
import logging
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, List

from sqlalchemy.orm import Session
from sqlalchemy import update, or_
from fastapi import HTTPException

from backend.app.config import settings
from backend.app.models.entities import UploadOperation, Character, Location, AudioTrack, MediaAsset
from backend.app.services.security import validate_safe_path

logger = logging.getLogger("upload_manager")

STAGED_EXPIRY_MINUTES = 60  # Staged uploads older than 60m without DB commit are considered abandoned


class UploadManager:
    def __init__(self, storage_root: Optional[Path] = None, temp_root: Optional[Path] = None):
        self.storage_root = storage_root or settings.data_dir
        self.temp_root = temp_root or settings.temp_dir

    def start_staged_upload(
        self,
        db: Session,
        project_id: Optional[str],
        entity_type: str,
        entity_id: str,
        temp_path: Path,
        destination_path: Path,
        old_asset_path: Optional[Path] = None,
        checksum: Optional[str] = None,
        file_size_bytes: int = 0
    ) -> UploadOperation:
        """
        Registers a new upload in STAGED status before database entity modification.
        Validates paths stay within approved storage roots.
        """
        safe_temp = validate_safe_path(temp_path, self.storage_root)
        safe_dest = validate_safe_path(destination_path, self.storage_root)
        safe_old = validate_safe_path(old_asset_path, self.storage_root) if old_asset_path else None

        op = UploadOperation(
            project_id=project_id,
            entity_type=entity_type,
            entity_id=entity_id,
            temp_path=str(safe_temp),
            destination_path=str(safe_dest),
            old_asset_path=str(safe_old) if safe_old else None,
            status="STAGED",
            checksum=checksum,
            file_size_bytes=file_size_bytes,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc)
        )
        db.add(op)
        db.commit()
        db.refresh(op)
        return op

    def mark_db_committed(self, db: Session, op_id: str) -> UploadOperation:
        """
        Transitions status from STAGED to DB_COMMITTED.
        Called within the transaction that modifies the entity reference.
        """
        now = datetime.now(timezone.utc)
        stmt = (
            update(UploadOperation)
            .where(UploadOperation.id == op_id)
            .where(UploadOperation.status == "STAGED")
            .values(status="DB_COMMITTED", updated_at=now)
        )
        db.execute(stmt)
        # Flush or let caller commit
        op = db.query(UploadOperation).filter(UploadOperation.id == op_id).first()
        if not op:
            raise ValueError(f"Upload operation {op_id} not found")
        return op

    def finalize_upload(self, db: Session, op_id: str) -> Path:
        """
        Idempotently moves the staged file to its destination and cleans previous asset.
        Transitions status to FINALIZED.
        """
        op = db.query(UploadOperation).filter(UploadOperation.id == op_id).first()
        if not op:
            raise ValueError(f"Upload operation {op_id} not found")

        temp_p = Path(op.temp_path)
        dest_p = Path(op.destination_path)
        old_p = Path(op.old_asset_path) if op.old_asset_path else None

        validate_safe_path(dest_p, self.storage_root)

        # Idempotency check: if destination already exists
        if dest_p.exists():
            # If temp still exists, clean it up
            if temp_p.exists() and temp_p != dest_p:
                temp_p.unlink(missing_ok=True)
        else:
            if not temp_p.exists():
                op.status = "FAILED"
                op.error_message = f"Staged file missing at {temp_p} before finalization"
                op.updated_at = datetime.now(timezone.utc)
                db.commit()
                raise FileNotFoundError(f"Staged file missing at {temp_p}")

            dest_p.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(temp_p), str(dest_p))

        # Only delete old asset after destination is firmly in place on disk
        if old_p and old_p != dest_p and old_p.exists():
            try:
                old_p.unlink(missing_ok=True)
            except OSError as e:
                logger.warning(f"Could not remove old asset {old_p}: {e}")

        op.status = "FINALIZED"
        op.completed_at = datetime.now(timezone.utc)
        op.updated_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(op)
        return dest_p

    def fail_upload(self, db: Session, op_id: str, error: Any) -> UploadOperation:
        """
        Marks upload as FAILED and unlinks staged temp file. Preserves previous asset.
        """
        op = db.query(UploadOperation).filter(UploadOperation.id == op_id).first()
        if not op:
            return None

        temp_p = Path(op.temp_path)
        if temp_p.exists():
            temp_p.unlink(missing_ok=True)

        op.status = "FAILED"
        op.error_message = str(error)[:512]
        op.updated_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(op)
        return op

    def recover_staged_uploads(self, db: Session) -> Dict[str, int]:
        """
        Startup recovery coordinator:
        - DB_COMMITTED uploads: completed if file exists, or entity reverted if file was lost.
        - Expired STAGED uploads: cleaned up and marked FAILED.
        - Orphaned .tmp files: removed from temp directory.
        """
        now = datetime.now(timezone.utc)
        stats = {
            "finalized": 0,
            "reverted_missing": 0,
            "staged_cleaned": 0
        }

        # 1. Recover DB_COMMITTED uploads
        committed_ops = db.query(UploadOperation).filter(UploadOperation.status == "DB_COMMITTED").all()
        for op in committed_ops:
            temp_p = Path(op.temp_path)
            dest_p = Path(op.destination_path)
            old_p = Path(op.old_asset_path) if op.old_asset_path else None

            try:
                if dest_p.exists():
                    # Move already finished before crash
                    if temp_p.exists() and temp_p != dest_p:
                        temp_p.unlink(missing_ok=True)
                    if old_p and old_p != dest_p and old_p.exists():
                        old_p.unlink(missing_ok=True)
                    op.status = "FINALIZED"
                    op.completed_at = now
                    op.updated_at = now
                    stats["finalized"] += 1
                elif temp_p.exists():
                    # Complete the pending move
                    dest_p.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(temp_p), str(dest_p))
                    if old_p and old_p != dest_p and old_p.exists():
                        old_p.unlink(missing_ok=True)
                    op.status = "FINALIZED"
                    op.completed_at = now
                    op.updated_at = now
                    stats["finalized"] += 1
                else:
                    # File is missing! Revert entity pointer in DB to prevent broken links
                    self._revert_entity_reference(db, op)
                    op.status = "FAILED"
                    op.error_message = f"Staged file missing at {temp_p} during recovery. Entity reverted."
                    op.updated_at = now
                    stats["reverted_missing"] += 1
            except Exception as e:
                logger.error(f"Error recovering upload operation {op.id}: {e}")

        # 2. Clean expired STAGED uploads (crashed before DB commit)
        expiry_threshold = now - timedelta(minutes=STAGED_EXPIRY_MINUTES)
        expired_staged = db.query(UploadOperation).filter(
            UploadOperation.status == "STAGED",
            UploadOperation.created_at <= expiry_threshold
        ).all()

        for op in expired_staged:
            temp_p = Path(op.temp_path)
            if temp_p.exists():
                temp_p.unlink(missing_ok=True)
            op.status = "FAILED"
            op.error_message = "Staged upload expired without database commit"
            op.updated_at = now
            stats["staged_cleaned"] += 1

        db.commit()

        # 3. Clean orphaned .tmp files in settings.temp_dir
        if self.temp_root.exists():
            for tmp_file in self.temp_root.glob("*.tmp"):
                # If file not tracked by any active STAGED or DB_COMMITTED op, remove it
                tracked = db.query(UploadOperation).filter(
                    UploadOperation.temp_path == str(tmp_file),
                    UploadOperation.status.in_(["STAGED", "DB_COMMITTED"])
                ).first()
                if not tracked:
                    try:
                        tmp_file.unlink(missing_ok=True)
                    except OSError:
                        pass

        return stats

    def _revert_entity_reference(self, db: Session, op: UploadOperation):
        """Restores old asset path or clears field when a staged file is completely lost."""
        fallback_path = ""
        if op.old_asset_path:
            old_p = Path(op.old_asset_path)
            if old_p.exists():
                try:
                    fallback_path = str(old_p.relative_to(settings.data_dir))
                except ValueError:
                    fallback_path = op.old_asset_path

        if op.entity_type == "character_reference":
            char = db.query(Character).filter(Character.id == op.entity_id).first()
            if char:
                char.reference_image_path = fallback_path
        elif op.entity_type == "location_reference":
            loc = db.query(Location).filter(Location.id == op.entity_id).first()
            if loc:
                loc.reference_image_path = fallback_path
        elif op.entity_type == "audio_track":
            track = db.query(AudioTrack).filter(AudioTrack.id == op.entity_id).first()
            if track:
                db.delete(track)
        elif op.entity_type == "media_asset":
            asset = db.query(MediaAsset).filter(MediaAsset.id == op.entity_id).first()
            if asset:
                asset.is_validated = False
                asset.validation_notes = "Missing upload file recovered on startup"


upload_manager = UploadManager()
