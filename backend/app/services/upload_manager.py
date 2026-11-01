"""
Persistent Staged Upload Operation Lifecycle and Recovery Manager.

Implements:
- STAGED -> DB_COMMITTED -> FINALIZED / FAILED lifecycle.
- Database-enforced atomic worker claim, lease heartbeat, and fencing tokens.
- Protection against multi-instance concurrent recovery conflicts.
- Destination integrity verification (SHA-256 and file size) before finalization.
- Same-filesystem and cross-filesystem atomic replacement.
- Preservation of previous asset until new asset is firmly verified on disk.
- Startup recovery:
  - Atomically claims eligible DB_COMMITTED uploads.
  - Verifies or finalizes uploads safely.
  - Reverts entity references if files are lost.
  - Cleans expired STAGED operations and orphaned .tmp files.
- Storage root traversal protection.
"""

import os
import re
import errno
import uuid
import shutil
import logging
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, List

from sqlalchemy.orm import Session
from sqlalchemy import update, or_, and_
from fastapi import HTTPException

from backend.app.config import settings
from backend.app.models.entities import UploadOperation, Character, Location, AudioTrack, MediaAsset
from backend.app.services.security import validate_safe_path, compute_file_sha256
from backend.app.services.job_manager import CURRENT_WORKER_ID

logger = logging.getLogger("upload_manager")

DEFAULT_UPLOAD_LEASE_SECONDS = 300  # 5 minutes
STAGED_EXPIRY_MINUTES = 60  # Staged uploads older than 60m without DB commit are considered abandoned
DEFAULT_ORPHAN_TEMP_MIN_AGE_SECONDS = 1800  # 30 minutes safe age threshold for untracked temporary files


class UploadOwnershipLostError(HTTPException):
    """Raised when an operation attempts to claim, finalize, or fail an upload whose lease or token has expired or been preempted."""
    def __init__(self, detail: str = "Upload operation lease ownership lost or preempted by another worker."):
        super().__init__(status_code=409, detail=detail)


def _is_lease_expired(lease_dt: Optional[datetime], now_dt: datetime) -> bool:
    if lease_dt is None:
        return True
    if lease_dt.tzinfo is not None:
        lease_dt = lease_dt.astimezone(timezone.utc).replace(tzinfo=None)
    if now_dt.tzinfo is not None:
        now_dt = now_dt.astimezone(timezone.utc).replace(tzinfo=None)
    return lease_dt < now_dt


def _verify_file_integrity(file_path: Path, expected_checksum: Optional[str], expected_size: Optional[int]) -> bool:
    """Verifies file existence, size, and SHA-256 checksum."""
    if not file_path.exists() or not file_path.is_file():
        return False
    if expected_size is not None and expected_size > 0:
        if file_path.stat().st_size != expected_size:
            return False
    if expected_checksum:
        actual_chk = compute_file_sha256(file_path)
        if actual_chk.lower() != expected_checksum.lower():
            return False
    return True


def _atomic_replace(src: Path, dst: Path) -> None:
    """
    Safely and atomically moves src to dst.
    Uses shutil.move (which performs atomic rename via os.rename on POSIX same filesystem).
    If cross-device link (EXDEV), stages into dst's directory before atomic os.replace.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.move(str(src), str(dst))
    except OSError as e:
        if getattr(e, "errno", None) == errno.EXDEV:
            # Cross-device move: copy to temp file on the destination filesystem, then os.replace
            tmp_staging = dst.with_name(f"{dst.name}.staging_{os.getpid()}_{uuid.uuid4().hex[:6]}")
            try:
                shutil.copy2(str(src), str(tmp_staging))
                os.replace(str(tmp_staging), str(dst))
                src.unlink(missing_ok=True)
            finally:
                if tmp_staging.exists():
                    tmp_staging.unlink(missing_ok=True)
        else:
            raise


class UploadManager:
    def __init__(
        self,
        storage_root: Optional[Path] = None,
        temp_root: Optional[Path] = None,
        worker_id: str = CURRENT_WORKER_ID,
        orphan_min_age_seconds: int = DEFAULT_ORPHAN_TEMP_MIN_AGE_SECONDS
    ):
        self.storage_root = storage_root or settings.data_dir
        self.temp_root = temp_root or settings.temp_dir
        self.worker_id = worker_id
        self.orphan_min_age_seconds = orphan_min_age_seconds

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

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        lease_expires = now + timedelta(seconds=DEFAULT_UPLOAD_LEASE_SECONDS)

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
            worker_id=self.worker_id,
            lease_expires_at=lease_expires,
            fencing_token=1,
            created_at=now,
            updated_at=now
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
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        lease_expires = now + timedelta(seconds=DEFAULT_UPLOAD_LEASE_SECONDS)
        stmt = (
            update(UploadOperation)
            .where(UploadOperation.id == op_id)
            .where(UploadOperation.status == "STAGED")
            .values(
                status="DB_COMMITTED",
                worker_id=self.worker_id,
                lease_expires_at=lease_expires,
                updated_at=now
            )
            .execution_options(synchronize_session=False)
        )
        db.execute(stmt)
        op = db.query(UploadOperation).filter(UploadOperation.id == op_id).first()
        if not op:
            raise ValueError(f"Upload operation {op_id} not found")
        return op

    def claim_upload_operation(
        self,
        db: Session,
        op_id: str,
        worker_id: Optional[str] = None,
        lease_seconds: int = DEFAULT_UPLOAD_LEASE_SECONDS
    ) -> Optional[UploadOperation]:
        """
        Atomically claims an eligible DB_COMMITTED upload operation using a conditional SQL UPDATE.
        Eligible operations:
        - status == 'DB_COMMITTED'
        - worker_id is NULL or lease_expires_at is NULL or lease_expires_at < now or worker_id == target_worker
        Monotonically increments fencing_token.
        Returns the claimed UploadOperation or None if already claimed by another active worker.
        """
        target_worker = worker_id or self.worker_id
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        new_expires = now + timedelta(seconds=lease_seconds)

        op = db.query(UploadOperation).filter(UploadOperation.id == op_id).first()
        if not op or op.status != "DB_COMMITTED":
            return None

        # Check if currently claimed by another active worker
        if op.worker_id and op.worker_id != target_worker and not _is_lease_expired(op.lease_expires_at, now):
            return None

        current_token = op.fencing_token or 1
        stmt = (
            update(UploadOperation)
            .where(UploadOperation.id == op_id)
            .where(UploadOperation.status == "DB_COMMITTED")
            .where(
                or_(
                    UploadOperation.worker_id == None,
                    UploadOperation.lease_expires_at == None,
                    UploadOperation.lease_expires_at < now,
                    UploadOperation.worker_id == target_worker
                )
            )
            .where(UploadOperation.fencing_token == current_token)
            .values(
                worker_id=target_worker,
                lease_expires_at=new_expires,
                fencing_token=current_token + 1,
                updated_at=now
            )
            .execution_options(synchronize_session=False)
        )
        res = db.execute(stmt)
        db.commit()

        if res.rowcount > 0:
            db.refresh(op)
            return op
        return None

    def renew_upload_lease(
        self,
        db: Session,
        op_id: str,
        worker_id: Optional[str] = None,
        fencing_token: Optional[int] = None,
        extend_seconds: int = DEFAULT_UPLOAD_LEASE_SECONDS
    ) -> bool:
        """
        Heartbeat renewal during long filesystem or transfer tasks.
        Requires explicit fencing_token and worker_id matching the active lease owner.
        """
        if fencing_token is None:
            raise UploadOwnershipLostError("Explicit fencing_token is required for renew_upload_lease.")
        target_worker = worker_id or self.worker_id
        if not target_worker:
            raise UploadOwnershipLostError("Worker ID is required for renew_upload_lease.")

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        new_expires = now + timedelta(seconds=extend_seconds)

        stmt = (
            update(UploadOperation)
            .where(UploadOperation.id == op_id)
            .where(UploadOperation.status == "DB_COMMITTED")
            .where(UploadOperation.worker_id == target_worker)
            .where(UploadOperation.fencing_token == fencing_token)
            .where(UploadOperation.lease_expires_at >= now)
            .values(
                lease_expires_at=new_expires,
                updated_at=now
            )
            .execution_options(synchronize_session=False)
        )
        res = db.execute(stmt)
        db.commit()
        return res.rowcount > 0

    def finalize_upload(
        self,
        db: Session,
        op_id: str,
        worker_id: Optional[str] = None,
        fencing_token: Optional[int] = None
    ) -> Path:
        """
        Idempotently and atomically moves the staged file to its destination and cleans previous asset.
        Verifies destination file against checksum and file_size_bytes before marking FINALIZED.
        Enforces atomic worker ownership and strict fencing token.
        Preserves old asset file until database state transition to FINALIZED has fully committed.
        """
        if fencing_token is None:
            raise UploadOwnershipLostError("Explicit fencing_token is required for finalize_upload.")
        target_worker = worker_id or self.worker_id
        if not target_worker:
            raise UploadOwnershipLostError("Worker ID is required for finalize_upload.")

        op = db.query(UploadOperation).filter(UploadOperation.id == op_id).first()
        if not op:
            raise ValueError(f"Upload operation {op_id} not found")

        dest_p = Path(op.destination_path)
        temp_p = Path(op.temp_path)
        old_p = Path(op.old_asset_path) if op.old_asset_path else None

        validate_safe_path(dest_p, self.storage_root)

        # Idempotency check: already marked FINALIZED
        if op.status == "FINALIZED":
            if _verify_file_integrity(dest_p, op.checksum, op.file_size_bytes):
                return dest_p
            raise ValueError(f"Upload operation '{op_id}' is marked FINALIZED but destination file failed integrity check")

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        if op.status != "DB_COMMITTED":
            raise UploadOwnershipLostError(
                f"Upload operation '{op_id}' is not in DB_COMMITTED status (current: '{op.status}')."
            )

        if op.worker_id != target_worker or op.fencing_token != fencing_token or _is_lease_expired(op.lease_expires_at, now):
            raise UploadOwnershipLostError(
                f"Worker '{target_worker}' (token {fencing_token}) cannot finalize upload '{op_id}': "
                f"lease expired or preempted by another worker. Current state: worker='{op.worker_id}', "
                f"token={op.fencing_token}, status='{op.status}'."
            )

        # Phase 1: Destination file placement & integrity verification
        dest_is_valid = _verify_file_integrity(dest_p, op.checksum, op.file_size_bytes)
        temp_is_valid = _verify_file_integrity(temp_p, op.checksum, op.file_size_bytes)

        # In-place replacement safety: if destination matches old_asset_path, preserve backup until DB commit
        in_place_backup: Optional[Path] = None
        if old_p and old_p == dest_p and old_p.exists():
            in_place_backup = dest_p.with_name(f"{dest_p.name}.bak_{uuid.uuid4().hex[:8]}")
            shutil.copy2(str(dest_p), str(in_place_backup))

        try:
            if dest_p.exists():
                if dest_is_valid:
                    # Destination already in place with valid checksum and size
                    pass
                else:
                    # Destination exists but checksum or size mismatch!
                    if temp_is_valid:
                        try:
                            _atomic_replace(temp_p, dest_p)
                        except Exception as replace_err:
                            self._fail_operation_with_revert(
                                db, op, target_worker, fencing_token,
                                f"Atomic replacement of corrupt destination failed: {replace_err}"
                            )
                            raise
                        if not _verify_file_integrity(dest_p, op.checksum, op.file_size_bytes):
                            self._fail_operation_with_revert(
                                db, op, target_worker, fencing_token,
                                "Destination file failed integrity verification immediately after atomic replacement."
                            )
                            raise ValueError("Destination file failed integrity check after replacement")
                    else:
                        self._fail_operation_with_revert(
                            db, op, target_worker, fencing_token,
                            f"Destination file at {dest_p} exists but checksum/size mismatch; staged file is missing or invalid. Preserving old asset."
                        )
                        raise ValueError(f"Destination file at {dest_p} does not match expected upload checksum {op.checksum}")
            else:
                # Destination does not exist yet
                if not temp_p.exists():
                    self._fail_operation_with_revert(
                        db, op, target_worker, fencing_token,
                        f"Staged file missing at {temp_p} before finalization. Entity reference reverted."
                    )
                    raise FileNotFoundError(f"Staged file missing at {temp_p}")

                if not temp_is_valid:
                    self._fail_operation_with_revert(
                        db, op, target_worker, fencing_token,
                        f"Staged file at {temp_p} failed integrity verification before move"
                    )
                    raise ValueError(f"Staged file failed integrity verification (expected {op.checksum})")

                try:
                    _atomic_replace(temp_p, dest_p)
                except Exception as move_err:
                    self._fail_operation_with_revert(
                        db, op, target_worker, fencing_token,
                        f"Filesystem move failed: {move_err}"
                    )
                    raise

                if not _verify_file_integrity(dest_p, op.checksum, op.file_size_bytes):
                    self._fail_operation_with_revert(
                        db, op, target_worker, fencing_token,
                        "Destination file failed integrity check after filesystem move"
                    )
                    raise ValueError("Destination file failed integrity check after filesystem move")

            # Phase 2: Database state transition to FINALIZED (ENFORCES FENCING AND OWNERSHIP)
            # NOTE: old_asset_path is STILL 100% PRESERVED on disk at this point!
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            stmt = (
                update(UploadOperation)
                .where(UploadOperation.id == op_id)
                .where(UploadOperation.status == "DB_COMMITTED")
                .where(UploadOperation.worker_id == target_worker)
                .where(UploadOperation.fencing_token == fencing_token)
                .where(UploadOperation.lease_expires_at >= now)
                .values(
                    status="FINALIZED",
                    completed_at=now,
                    updated_at=now
                )
                .execution_options(synchronize_session=False)
            )
            res = db.execute(stmt)
            db.commit()

            if res.rowcount == 0:
                # Ownership was lost before or during finalization commit!
                if in_place_backup and in_place_backup.exists() and old_p:
                    try:
                        shutil.move(str(in_place_backup), str(old_p))
                    except Exception:
                        pass
                current = db.query(UploadOperation).filter(UploadOperation.id == op_id).first()
                raise UploadOwnershipLostError(
                    f"Worker '{target_worker}' (token {fencing_token}) cannot finalize upload '{op_id}': "
                    f"lease expired or preempted by another worker. Current state: worker='{current.worker_id if current else 'N/A'}', "
                    f"token={current.fencing_token if current else 'N/A'}, status='{current.status if current else 'N/A'}'."
                )

            # Phase 3: DB transition succeeded! NOW AND ONLY NOW clean up old asset and temporary files.
            if in_place_backup and in_place_backup.exists():
                try:
                    in_place_backup.unlink(missing_ok=True)
                except OSError:
                    pass

            if old_p and old_p != dest_p and old_p.exists():
                try:
                    old_p.unlink(missing_ok=True)
                except OSError as e:
                    logger.warning(f"Could not remove old asset {old_p}: {e}")

            if temp_p.exists() and temp_p != dest_p:
                try:
                    temp_p.unlink(missing_ok=True)
                except OSError:
                    pass

            db.refresh(op)
            return dest_p

        except Exception:
            if in_place_backup and in_place_backup.exists() and old_p and not old_p.exists():
                try:
                    shutil.move(str(in_place_backup), str(old_p))
                except Exception:
                    pass
            raise

    def _fail_operation_with_revert(
        self,
        db: Session,
        op: UploadOperation,
        worker_id: str,
        fencing_token: int,
        error_message: str
    ) -> None:
        """Conditionally marks upload FAILED and reverts entity pointer."""
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        self._revert_entity_reference(db, op)

        stmt = (
            update(UploadOperation)
            .where(UploadOperation.id == op.id)
            .where(UploadOperation.status == "DB_COMMITTED")
            .where(UploadOperation.worker_id == worker_id)
            .where(UploadOperation.fencing_token == fencing_token)
            .where(UploadOperation.lease_expires_at >= now)
            .values(
                status="FAILED",
                error_message=error_message[:512],
                updated_at=now
            )
            .execution_options(synchronize_session=False)
        )
        res = db.execute(stmt)
        db.commit()
        if res.rowcount == 0:
            db.rollback()
            raise UploadOwnershipLostError(
                f"Worker '{worker_id}' cannot fail upload '{op.id}': ownership lost or preempted."
            )

    def fail_upload(
        self,
        db: Session,
        op_id: str,
        error: Any,
        worker_id: Optional[str] = None,
        fencing_token: Optional[int] = None
    ) -> Optional[UploadOperation]:
        """
        Marks upload as FAILED and unlinks staged temp file.
        Requires explicit fencing_token and worker_id.
        If reached DB_COMMITTED status, conditionally updates with worker fencing and reverts entity reference.
        Preserves previous asset file on disk.
        """
        if fencing_token is None:
            raise UploadOwnershipLostError("Explicit fencing_token is required for fail_upload.")
        target_worker = worker_id or self.worker_id
        if not target_worker:
            raise UploadOwnershipLostError("Worker ID is required for fail_upload.")

        op = db.query(UploadOperation).filter(UploadOperation.id == op_id).first()
        if not op:
            return None

        now = datetime.now(timezone.utc).replace(tzinfo=None)

        if op.status == "DB_COMMITTED":
            self._revert_entity_reference(db, op)
            stmt = (
                update(UploadOperation)
                .where(UploadOperation.id == op_id)
                .where(UploadOperation.status == "DB_COMMITTED")
                .where(UploadOperation.worker_id == target_worker)
                .where(UploadOperation.fencing_token == fencing_token)
                .where(UploadOperation.lease_expires_at >= now)
                .values(
                    status="FAILED",
                    error_message=str(error)[:512],
                    updated_at=now
                )
                .execution_options(synchronize_session=False)
            )
            res = db.execute(stmt)
            if res.rowcount == 0:
                db.rollback()
                raise UploadOwnershipLostError(
                    f"Worker '{target_worker}' cannot fail upload '{op_id}': ownership lost or preempted."
                )
            db.commit()
        elif op.status == "STAGED":
            stmt = (
                update(UploadOperation)
                .where(UploadOperation.id == op_id)
                .where(UploadOperation.status == "STAGED")
                .where(UploadOperation.worker_id == target_worker)
                .where(UploadOperation.fencing_token == fencing_token)
                .where(UploadOperation.lease_expires_at >= now)
                .values(
                    status="FAILED",
                    error_message=str(error)[:512],
                    updated_at=now
                )
                .execution_options(synchronize_session=False)
            )
            res = db.execute(stmt)
            if res.rowcount == 0:
                db.rollback()
                raise UploadOwnershipLostError(
                    f"Worker '{target_worker}' cannot fail upload '{op_id}': ownership lost or preempted."
                )
            db.commit()
        else:
            return op

        temp_p = Path(op.temp_path)
        if temp_p.exists():
            try:
                temp_p.unlink(missing_ok=True)
            except OSError:
                pass

        db.refresh(op)
        return op

    def claim_and_clean_staged_upload(
        self,
        db: Session,
        op_id: str,
        worker_id: Optional[str] = None
    ) -> bool:
        """
        Safely and atomically claims an expired STAGED upload for cleanup.
        Eligible ONLY if status == 'STAGED' AND (lease_expires_at < now OR (lease_expires_at IS NULL AND created_at <= threshold)).
        Never cleans active uploads (lease_expires_at >= now).
        Uses conditional atomic UPDATE so multiple instances cannot clean concurrently.
        Unlinks temporary file only after DB state transition to FAILED succeeds.
        """
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        expiry_threshold = now - timedelta(minutes=STAGED_EXPIRY_MINUTES)

        op = db.query(UploadOperation).filter(UploadOperation.id == op_id).first()
        if not op or op.status != "STAGED":
            return False

        # Active check: lease still valid
        if op.lease_expires_at and op.lease_expires_at >= now:
            return False

        stmt = (
            update(UploadOperation)
            .where(UploadOperation.id == op_id)
            .where(UploadOperation.status == "STAGED")
            .where(
                or_(
                    and_(UploadOperation.lease_expires_at != None, UploadOperation.lease_expires_at < now),
                    and_(UploadOperation.lease_expires_at == None, UploadOperation.created_at <= expiry_threshold)
                )
            )
            .values(
                status="FAILED",
                error_message="Staged upload expired without database commit",
                updated_at=now
            )
            .execution_options(synchronize_session=False)
        )
        res = db.execute(stmt)
        db.commit()

        if res.rowcount > 0:
            temp_p = Path(op.temp_path)
            if temp_p.exists():
                try:
                    temp_p.unlink(missing_ok=True)
                except OSError:
                    pass
            return True
        return False

    def recover_staged_uploads(
        self,
        db: Session,
        orphan_min_age_seconds: Optional[int] = None
    ) -> Dict[str, int]:
        """
        Startup and background recovery coordinator:
        - Atomically claims each eligible DB_COMMITTED upload.
        - Skips uploads actively claimed by another worker with a valid lease.
        - Verifies or finalizes uploads safely.
        - Reverts entity reference if file is missing or corrupt.
        - Cleans expired STAGED uploads safely without deleting active uploads.
        - Safely cleans orphaned .tmp files respecting the safe age threshold so in-flight
          uploads are never deleted before database staging.
        - Multiple instances protected against concurrent cleanup.
        """
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        stats = {
            "finalized": 0,
            "reverted_missing": 0,
            "staged_cleaned": 0
        }

        # 1. Recover DB_COMMITTED uploads using atomic conditional claim
        committed_ops = db.query(UploadOperation).filter(UploadOperation.status == "DB_COMMITTED").all()
        for candidate in committed_ops:
            claimed = self.claim_upload_operation(db, candidate.id, worker_id=self.worker_id)
            if not claimed:
                # Active lease held by another worker or claimed concurrently; skip
                continue

            try:
                self.finalize_upload(
                    db=db,
                    op_id=claimed.id,
                    worker_id=claimed.worker_id,
                    fencing_token=claimed.fencing_token
                )
                stats["finalized"] += 1
            except Exception as e:
                logger.info(f"Recovery reverted or failed upload {claimed.id}: {e}")
                stats["reverted_missing"] += 1

        # 2. Clean expired STAGED uploads (abandoned before DB commit) atomically
        expiry_threshold = now - timedelta(minutes=STAGED_EXPIRY_MINUTES)
        expired_staged = db.query(UploadOperation).filter(
            UploadOperation.status == "STAGED",
            or_(
                and_(UploadOperation.lease_expires_at != None, UploadOperation.lease_expires_at < now),
                and_(UploadOperation.lease_expires_at == None, UploadOperation.created_at <= expiry_threshold)
            )
        ).all()

        for op in expired_staged:
            if self.claim_and_clean_staged_upload(db, op.id, worker_id=self.worker_id):
                stats["staged_cleaned"] += 1

        # 3. Clean orphaned .tmp files in settings.temp_dir respecting safe age threshold
        orphan_threshold = (
            orphan_min_age_seconds
            if orphan_min_age_seconds is not None
            else self.orphan_min_age_seconds
        )
        if self.temp_root.exists():
            for tmp_file in self.temp_root.glob("*.tmp"):
                try:
                    st = tmp_file.stat()
                    mtime = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).replace(tzinfo=None)
                except OSError:
                    continue

                # Check if tracked by an operation
                tracked = db.query(UploadOperation).filter(
                    UploadOperation.temp_path == str(tmp_file)
                ).first()

                if tracked:
                    # If tracked by an already finished/terminal operation (FAILED or FINALIZED),
                    # the staged temp file was left over from an interrupted cleanup or crash.
                    if tracked.status in ("FAILED", "FINALIZED"):
                        try:
                            tmp_file.unlink(missing_ok=True)
                            stats["staged_cleaned"] += 1
                        except OSError:
                            pass
                    # If status is STAGED or DB_COMMITTED, it is handled in steps 1 or 2; skip.
                else:
                    # Untracked temporary file:
                    # Safe age threshold: Never delete recently created or modified temporary files.
                    # In-progress uploads by any application instance may be streaming before DB commit.
                    if (now - mtime) < timedelta(seconds=orphan_threshold):
                        continue

                    # Untracked and older than safe threshold: genuinely abandoned
                    try:
                        tmp_file.unlink(missing_ok=True)
                        stats["staged_cleaned"] += 1
                    except OSError:
                        pass

        return stats

    def _revert_entity_reference(self, db: Session, op: UploadOperation):
        """Restores old asset path or clears field when a staged file is completely lost or invalid."""
        fallback_path = ""
        if op.old_asset_path:
            old_p = Path(op.old_asset_path)
            if old_p.exists():
                try:
                    fallback_path = str(old_p.relative_to(self.storage_root))
                except ValueError:
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
