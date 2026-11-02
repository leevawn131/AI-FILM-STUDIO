"""
Administrative Audit Archival Utility for AI Film Studio.

Provides safe, compliant archival and exact full-record verification
for immutable pipeline checkpoint audit records outside of database migrations.
"""

import os
import json
import logging
from pathlib import Path
from typing import List, Dict, Any, Union, Optional
from datetime import datetime
from sqlalchemy import text
from sqlalchemy.engine import Engine, Connection
from sqlalchemy.orm import Session

logger = logging.getLogger("audit_archiver")

AUDIT_COLUMNS: List[str] = [
    "id",
    "project_id",
    "project_name_snapshot",
    "checkpoint_id",
    "checkpoint_type",
    "decision",
    "artifact_content_hash",
    "artifact_version",
    "reviewer_name",
    "feedback_notes",
    "created_at",
]


class AuditArchivalError(Exception):
    """Base exception for audit archival and verification operations."""
    pass


class AuditArchiveExistsError(AuditArchivalError, FileExistsError):
    """Raised when the archive destination file already exists and overwrite is False."""
    pass


class AuditVerificationError(AuditArchivalError):
    """Raised when an audit archive fails strict record-level verification."""
    pass


def _get_connection(target: Union[Engine, Connection, Session]):
    """Helper to obtain an active connection or execute context."""
    if isinstance(target, Session):
        return target.connection()
    return target


def fetch_source_audit_records(target: Union[Engine, Connection, Session]) -> List[Dict[str, Any]]:
    """
    Fetches all records from pipeline_checkpoint_audit, ordered deterministically by created_at and id.
    Returns a list of dicts with all 11 AUDIT_COLUMNS.
    """
    col_str = ", ".join(AUDIT_COLUMNS)
    query = text(f"SELECT {col_str} FROM pipeline_checkpoint_audit ORDER BY created_at ASC, id ASC")
    
    if isinstance(target, Session):
        rows = target.execute(query).mappings().all()
    elif isinstance(target, Engine):
        with target.connect() as conn:
            rows = conn.execute(query).mappings().all()
    else:
        # Connection
        rows = target.execute(query).mappings().all()

    records = []
    for r in rows:
        row_dict = {col: r[col] for col in AUDIT_COLUMNS}
        records.append(row_dict)
    return records


def serialize_audit_records(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Serializes audit record values for JSON archiving:
    - Consistent ISO 8601 string formatting for datetimes.
    - Preserves exact types for strings, integers, and nulls.
    """
    serialized = []
    for r in records:
        rec_copy = dict(r)
        for col in AUDIT_COLUMNS:
            val = rec_copy.get(col)
            if isinstance(val, (datetime, )):
                rec_copy[col] = val.isoformat()
            elif val is not None and col in ("id", "project_id", "project_name_snapshot", "checkpoint_id",
                                             "checkpoint_type", "decision", "artifact_content_hash",
                                             "reviewer_name", "feedback_notes"):
                rec_copy[col] = str(val)
            elif val is not None and col == "artifact_version":
                rec_copy[col] = int(val)
        serialized.append(rec_copy)
    return serialized


def verify_audit_archive(
    source_records_or_target: Union[List[Dict[str, Any]], Engine, Connection, Session],
    archive_path: Union[str, Path]
) -> Dict[str, Any]:
    """
    Performs strict full-record verification of an audit archive against source records:
    - Verifies archive file existence and valid JSON array.
    - Verifies exact record count.
    - Verifies uniqueness of archived IDs (rejects duplicates).
    - Rejects missing, duplicated, modified, or extra records.
    - Verifies EVERY column across all records:
      id, project_id, project_name_snapshot, checkpoint_id, checkpoint_type,
      decision, artifact_content_hash, artifact_version, reviewer_name,
      feedback_notes, created_at.
    - Verifies no unexpected extra columns exist in archived records.

    Raises:
        AuditVerificationError: If any discrepancy is found.
    """
    path = Path(archive_path)
    if not path.exists():
        raise AuditVerificationError(f"Audit archive file does not exist at {path}")
    if path.stat().st_size == 0:
        raise AuditVerificationError(f"Audit archive file is empty: {path}")

    try:
        with open(path, "r", encoding="utf-8") as f:
            archived_records = json.load(f)
    except Exception as e:
        raise AuditVerificationError(f"Failed to parse audit archive as JSON from {path}: {e}") from e

    if not isinstance(archived_records, list):
        raise AuditVerificationError(f"Audit archive content must be a JSON array, got {type(archived_records).__name__}")

    # Fetch source records if engine/session/connection passed
    if isinstance(source_records_or_target, (Engine, Connection, Session)):
        source_records = fetch_source_audit_records(source_records_or_target)
    else:
        source_records = source_records_or_target

    # 1. Verify record count
    if len(archived_records) != len(source_records):
        raise AuditVerificationError(
            f"Audit archive record count mismatch: source has {len(source_records)} records, "
            f"archive contains {len(archived_records)} records."
        )

    # 2. Verify uniqueness of archived IDs
    archived_by_id: Dict[str, Dict[str, Any]] = {}
    for idx, item in enumerate(archived_records):
        if not isinstance(item, dict):
            raise AuditVerificationError(f"Archived record at index {idx} is not an object/dict: {item!r}")
        rec_id = item.get("id")
        if not rec_id:
            raise AuditVerificationError(f"Archived record at index {idx} is missing required primary key 'id'.")
        if rec_id in archived_by_id:
            raise AuditVerificationError(
                f"Duplicate record id '{rec_id}' found in audit archive at index {idx}. "
                "All archived IDs must be strictly unique."
            )
        archived_by_id[rec_id] = item

    # 3. Check for missing or extra records
    source_ids = {r["id"] for r in source_records}
    archived_ids = set(archived_by_id.keys())
    missing_ids = source_ids - archived_ids
    extra_ids = archived_ids - source_ids

    if missing_ids:
        raise AuditVerificationError(f"Audit archive is missing {len(missing_ids)} source record(s): {sorted(missing_ids)}")
    if extra_ids:
        raise AuditVerificationError(f"Audit archive contains {len(extra_ids)} unexpected extra record(s): {sorted(extra_ids)}")

    # 4. Compare every source record against its archived representation across ALL columns
    for src in source_records:
        rec_id = src["id"]
        arch = archived_by_id[rec_id]

        # Check for unexpected extra columns
        extra_columns = set(arch.keys()) - set(AUDIT_COLUMNS)
        if extra_columns:
            raise AuditVerificationError(
                f"Archived record '{rec_id}' contains unexpected extra column(s): {sorted(extra_columns)}"
            )

        # Check every required column
        for col in AUDIT_COLUMNS:
            if col not in arch:
                raise AuditVerificationError(
                    f"Archived record '{rec_id}' is missing required audit column '{col}'."
                )

            src_val = src[col]
            arch_val = arch[col]

            # Null handling
            if src_val is None and arch_val is None:
                continue
            if (src_val is None) != (arch_val is None):
                raise AuditVerificationError(
                    f"Audit verification mismatch for record '{rec_id}', column '{col}': "
                    f"source is {src_val!r}, archive is {arch_val!r}."
                )

            # Timestamp comparison
            if col == "created_at":
                src_iso = src_val.isoformat() if hasattr(src_val, "isoformat") else str(src_val)
                arch_iso = arch_val.isoformat() if hasattr(arch_val, "isoformat") else str(arch_val)
                if src_iso != arch_iso:
                    raise AuditVerificationError(
                        f"Audit verification mismatch for record '{rec_id}', column 'created_at': "
                        f"source='{src_iso}', archive='{arch_iso}'."
                    )
            # Integer comparison
            elif col == "artifact_version":
                try:
                    if int(src_val) != int(arch_val):
                        raise AuditVerificationError(
                            f"Audit verification mismatch for record '{rec_id}', column 'artifact_version': "
                            f"source={src_val}, archive={arch_val}."
                        )
                except (ValueError, TypeError) as e:
                    raise AuditVerificationError(
                        f"Audit verification invalid integer for record '{rec_id}', column 'artifact_version': {e}"
                    ) from e
            # String comparison
            else:
                if str(src_val) != str(arch_val):
                    raise AuditVerificationError(
                        f"Audit verification mismatch for record '{rec_id}', column '{col}': "
                        f"source={src_val!r}, archive={arch_val!r}."
                    )

    logger.info("Successfully verified audit archive at %s (%d records)", path, len(source_records))
    return {
        "status": "verified",
        "verified_record_count": len(source_records),
        "archive_path": str(path),
        "verified_columns": list(AUDIT_COLUMNS),
    }


def archive_audit_records(
    target: Union[Engine, Connection, Session],
    archive_path: Union[str, Path],
    overwrite: bool = False
) -> Dict[str, Any]:
    """
    Exports all audit records to a JSON archive, serializes timestamps consistently,
    prevents silent overwrite of existing archives, and immediately verifies the written file.

    Returns:
        Dict reporting status, record count, and archive path.

    Raises:
        AuditArchiveExistsError: If archive_path already exists and overwrite is False.
        AuditVerificationError: If verification of the created archive fails.
    """
    path = Path(archive_path)
    if path.exists() and not overwrite:
        raise AuditArchiveExistsError(
            f"Destination archive file already exists at '{path}'. "
            "Overwrite is disabled to avoid silently destroying an existing archive."
        )

    # 1. Fetch source records
    source_records = fetch_source_audit_records(target)

    # 2. Serialize for JSON
    serialized_records = serialize_audit_records(source_records)

    # 3. Write to destination atomically
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(".tmp")
    try:
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(serialized_records, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())

        # Atomic replacement
        temp_path.replace(path)
    except Exception as e:
        if temp_path.exists():
            temp_path.unlink()
        raise AuditArchivalError(f"Failed to write audit archive to {path}: {e}") from e

    # 4. Immediate round-trip full-record verification
    try:
        verify_result = verify_audit_archive(source_records, path)
    except Exception as err:
        # If verification fails, remove the unverified archive so no bad archive remains
        if path.exists():
            path.unlink()
        raise

    logger.info("Audit archive created and verified at %s (%d records)", path, len(source_records))
    return {
        "status": "success",
        "records_exported": len(source_records),
        "archive_path": str(path),
        "verification": verify_result,
    }


def safe_administrative_purge(
    target: Union[Engine, Connection, Session],
    archive_path: Union[str, Path],
    force: bool = False
) -> Dict[str, Any]:
    """
    Safely purges audit records from the database ONLY after strict archive verification succeeds.
    If archive verification fails for ANY reason, the purge operation is strictly aborted.
    """
    if not force:
        raise AuditArchivalError("Administrative purge requires explicit force=True flag.")

    # 1. Verify archive first against current database
    verify_result = verify_audit_archive(target, archive_path)
    count = verify_result["verified_record_count"]

    # 2. Perform purge within a transaction
    # On SQLite/Postgres triggers block normal DELETE, so administrative purge
    # temporarily drops trigger or relies on migration downgrade with FORCE_AUDIT_PURGE=1
    if isinstance(target, Session):
        bind = target.get_bind()
    elif isinstance(target, Connection):
        bind = target
    else:
        bind = target.connect()

    dialect_name = bind.dialect.name
    try:
        if dialect_name == "sqlite":
            bind.execute(text("DROP TRIGGER IF EXISTS trg_audit_no_delete;"))
            bind.execute(text("DELETE FROM pipeline_checkpoint_audit;"))
            bind.execute(text("""
                CREATE TRIGGER IF NOT EXISTS trg_audit_no_delete
                BEFORE DELETE ON pipeline_checkpoint_audit
                BEGIN
                    SELECT RAISE(ABORT, 'pipeline_checkpoint_audit is immutable and append-only');
                END;
            """))
        elif dialect_name == "postgresql":
            bind.execute(text("ALTER TABLE pipeline_checkpoint_audit DISABLE TRIGGER trg_audit_prevent_mod;"))
            bind.execute(text("DELETE FROM pipeline_checkpoint_audit;"))
            bind.execute(text("ALTER TABLE pipeline_checkpoint_audit ENABLE TRIGGER trg_audit_prevent_mod;"))

        if hasattr(bind, "commit"):
            bind.commit()
    finally:
        if isinstance(target, Engine) and isinstance(bind, Connection):
            bind.close()

    logger.info("Safely purged %d audit records after verified archive at %s", count, archive_path)
    return {
        "status": "purged",
        "purged_record_count": count,
        "archive_path": str(archive_path),
    }
