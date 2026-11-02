"""
Administrative Audit Archival Utility for AI Film Studio.

Provides safe, compliant archival and exact full-record verification
for immutable pipeline checkpoint audit records outside of database migrations.
"""

import os
import json
import uuid
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


def fetch_source_audit_records(target: Union[Engine, Connection, Session]) -> List[Dict[str, Any]]:
    """
    Fetches all records from pipeline_checkpoint_audit, ordered deterministically by created_at and id.
    Returns a list of dicts with all 11 AUDIT_COLUMNS.
    """
    col_str = ", ".join(AUDIT_COLUMNS)
    query = text(f"SELECT {col_str} FROM pipeline_checkpoint_audit ORDER BY created_at ASC, id ASC")

    if isinstance(target, Session):
        with target.no_autoflush:
            rows = target.execute(query).mappings().all()
    elif isinstance(target, Engine):
        with target.connect() as conn:
            rows = conn.execute(query).mappings().all()
    elif isinstance(target, Connection):
        rows = target.execute(query).mappings().all()
    else:
        raise TypeError(f"Unsupported target type: {type(target).__name__}. Expected Session, Connection, or Engine.")

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
    - Enforces strict JSON types:
      * String fields must be JSON strings.
      * artifact_version must be a JSON integer (not bool, not float, not str).
      * created_at must be a valid ISO 8601 string.
      * Null handling must match exactly (null vs empty string).
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

        # Check every required column with strict typing
        for col in AUDIT_COLUMNS:
            if col not in arch:
                raise AuditVerificationError(
                    f"Archived record '{rec_id}' is missing required audit column '{col}'."
                )

            src_val = src[col]
            arch_val = arch[col]

            # Null handling: exact match
            if src_val is None:
                if arch_val is not None:
                    raise AuditVerificationError(
                        f"Field mismatch for record '{rec_id}', column '{col}': "
                        f"source is None, but archive is {arch_val!r} (type {type(arch_val).__name__})."
                    )
                continue
            else:
                if arch_val is None:
                    raise AuditVerificationError(
                        f"Field mismatch for record '{rec_id}', column '{col}': "
                        f"source is {src_val!r}, but archive is None."
                    )

            # Strict typing for artifact_version: must be JSON integer (not bool, not float, not str)
            if col == "artifact_version":
                if isinstance(arch_val, bool) or not isinstance(arch_val, int):
                    raise AuditVerificationError(
                        f"Field type error for record '{rec_id}', column 'artifact_version': "
                        f"expected JSON integer, got {type(arch_val).__name__} ({arch_val!r})."
                    )
                if arch_val != int(src_val):
                    raise AuditVerificationError(
                        f"Field mismatch for record '{rec_id}', column 'artifact_version': "
                        f"source={src_val}, archive={arch_val}."
                    )

            # Strict typing for created_at: must be valid ISO 8601 string
            elif col == "created_at":
                if isinstance(arch_val, bool) or not isinstance(arch_val, str):
                    raise AuditVerificationError(
                        f"Field type error for record '{rec_id}', column 'created_at': "
                        f"expected ISO 8601 string, got {type(arch_val).__name__} ({arch_val!r})."
                    )
                try:
                    arch_dt = datetime.fromisoformat(arch_val)
                except Exception as e:
                    raise AuditVerificationError(
                        f"Field 'created_at' in record '{rec_id}' is not a valid ISO 8601 timestamp: {arch_val!r} ({e})."
                    ) from e

                src_iso = src_val.isoformat() if hasattr(src_val, "isoformat") else str(src_val)
                try:
                    src_dt = datetime.fromisoformat(src_iso)
                except Exception as e:
                    raise AuditVerificationError(
                        f"Source 'created_at' in record '{rec_id}' is not a valid ISO 8601 timestamp: {src_iso!r} ({e})."
                    ) from e

                if src_dt != arch_dt:
                    raise AuditVerificationError(
                        f"Field mismatch for record '{rec_id}', column 'created_at': "
                        f"source='{src_iso}', archive='{arch_val}'."
                    )

            # Strict typing for string columns
            else:
                if isinstance(arch_val, bool) or not isinstance(arch_val, str):
                    raise AuditVerificationError(
                        f"Field type error for record '{rec_id}', column '{col}': "
                        f"expected JSON string, got {type(arch_val).__name__} ({arch_val!r})."
                    )
                if arch_val != str(src_val):
                    raise AuditVerificationError(
                        f"Field mismatch for record '{rec_id}', column '{col}': "
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

    Guarantees:
    - Avoids concurrent writer collisions by using a unique temporary file per invocation.
    - Atomic overwrite protection: overwrite=False cannot overwrite an archive created concurrently.
    - When overwrite=True, the previous valid archive is preserved intact until the new archive
      has completely passed strict verification.
    - Never deletes a previous valid archive because a new export failed.
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

    # 3. Write to a unique temporary file in the same directory
    path.parent.mkdir(parents=True, exist_ok=True)
    unique_suffix = uuid.uuid4().hex
    temp_path = path.parent / f".{path.name}.tmp-{unique_suffix}"

    try:
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(serialized_records, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())

        # 4. Immediate round-trip full-record verification ON THE TEMP FILE
        # If verification fails, temp_path is cleaned up and path is left untouched.
        verify_result = verify_audit_archive(source_records, temp_path)

        # 5. Safe atomic file placement
        if not overwrite:
            try:
                os.link(temp_path, path)
                temp_path.unlink()
            except FileExistsError as fe:
                if temp_path.exists():
                    temp_path.unlink()
                raise AuditArchiveExistsError(
                    f"Destination archive file already exists at '{path}'. "
                    "Overwrite is disabled to avoid silently destroying an existing archive."
                ) from fe
        else:
            os.replace(temp_path, path)

    except Exception:
        if temp_path.exists():
            temp_path.unlink()
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
    force: bool = False,
    _on_lock_acquired: Optional[Any] = None
) -> Dict[str, Any]:
    """
    Safely purges audit records from the database ONLY after strict archive verification succeeds.

    Transaction Ownership & Concurrency Model:
    - Engine Target:
        Function owns the connection and dedicated transaction.
        Acquires write lock (BEGIN IMMEDIATE on SQLite, ACCESS EXCLUSIVE on Postgres),
        verifies archive, drops triggers, deletes records, restores triggers, and commits explicitly.
        Reports status='purged' and is_durable=True.
    - Connection Target:
        If connection is not in a transaction, function manages and commits a dedicated transaction
        (is_durable=True). If caller already owns an active transaction on the connection,
        the purge is staged within a SAVEPOINT and released into the caller's transaction without
        calling conn.commit(). Reports status='pending_commit' and is_durable=False.
    - Session Target:
        Caller owns the Session. Obtains conn = target.connection().
        Acquires write lock (BEGIN IMMEDIATE on SQLite before savepoint, ACCESS EXCLUSIVE on Postgres).
        Purges within a SAVEPOINT (begin_nested) and releases the savepoint into the caller's session.
        Does NOT commit caller's session. If caller calls session.rollback(), the purge is rolled back.
        If caller calls session.commit(), the purge and caller changes are committed together.
        Reports status='pending_commit' and is_durable=False.
    - Failure Guarantees & Caller-Owned Transaction Contract:
        On verification failure or any error:
        * Engine Target:
            The function rolls back the entire dedicated transaction and closes the connection.
            The write lock is released immediately. No records are deleted.
        * Session / Connection Target (Caller-Owned Transaction):
            The function rolls back only its internal SAVEPOINT (restoring any state changed
            inside the savepoint). It does NOT commit or automatically rollback the caller's outer transaction,
            preserving unrelated caller work outside the savepoint.
            IMPORTANT: Because the write lock (SQLite BEGIN IMMEDIATE or PostgreSQL ACCESS EXCLUSIVE)
            was acquired on the caller's outer transaction, that lock remains held on the database
            connection until the caller explicitly commits or rolls back their outer transaction.
            The caller must promptly call session.rollback() / conn.rollback() to release the database
            lock and terminate the transaction.
        * In all failure modes, no audit records are deleted, and audit immutability triggers are
          never left disabled.
    """
    if not force:
        raise AuditArchivalError("Administrative purge requires explicit force=True flag.")

    # 1. Determine target type and connection ownership
    owns_connection = False
    is_session = False
    caller_owns_transaction = False

    if isinstance(target, Session):
        conn = target.connection()
        is_session = True
        caller_owns_transaction = True
    elif isinstance(target, Connection):
        conn = target
        caller_owns_transaction = conn.in_transaction()
    elif isinstance(target, Engine):
        conn = target.connect()
        owns_connection = True
        caller_owns_transaction = False
    else:
        raise TypeError(f"Unsupported target type: {type(target).__name__}. Expected Session, Connection, or Engine.")

    dialect_name = conn.dialect.name
    trans = None
    savepoint = None

    try:
        # 2. Acquire database write lock BEFORE opening any savepoint and before reading source records
        if dialect_name == "sqlite":
            raw_conn = getattr(conn.connection, "dbapi_connection", None)
            if raw_conn is None:
                raw_conn = getattr(conn.connection, "connection", None)

            # Check if an existing DBAPI transaction is already active on SQLite
            if raw_conn is not None and getattr(raw_conn, "in_transaction", False):
                raise AuditArchivalError(
                    "Cannot safely acquire SQLite write lock: an existing active caller transaction "
                    "is already in progress on this connection. Please commit or rollback the active "
                    "transaction before executing an administrative purge."
                )

            try:
                conn.exec_driver_sql("BEGIN IMMEDIATE")
            except Exception as lock_err:
                raise AuditArchivalError(f"Failed to acquire SQLite BEGIN IMMEDIATE write lock: {lock_err}") from lock_err

        elif dialect_name == "postgresql":
            if not conn.in_transaction():
                trans = conn.begin()
            conn.execute(text("LOCK TABLE pipeline_checkpoint_audit IN ACCESS EXCLUSIVE MODE"))

        # Optional test synchronization hook immediately after lock is acquired
        if _on_lock_acquired is not None:
            _on_lock_acquired()

        # 3. If caller owns transaction (Session or caller Connection), isolate purge in a savepoint
        if caller_owns_transaction:
            savepoint = conn.begin_nested()

        # 4. Read source records while holding the write lock
        source_records = fetch_source_audit_records(conn)
        count = len(source_records)

        # 5. Verify archive against the locked/current source records
        verify_result = verify_audit_archive(source_records, archive_path)

        # 6. Perform deletion and trigger management atomically under the lock
        if dialect_name == "sqlite":
            conn.execute(text("DROP TRIGGER IF EXISTS trg_audit_no_delete;"))
            conn.execute(text("DELETE FROM pipeline_checkpoint_audit;"))
            conn.execute(text("""
                CREATE TRIGGER IF NOT EXISTS trg_audit_no_delete
                BEFORE DELETE ON pipeline_checkpoint_audit
                BEGIN
                    SELECT RAISE(ABORT, 'pipeline_checkpoint_audit is immutable and append-only');
                END;
            """))
        elif dialect_name == "postgresql":
            conn.execute(text("ALTER TABLE pipeline_checkpoint_audit DISABLE TRIGGER trg_audit_prevent_mod;"))
            conn.execute(text("DELETE FROM pipeline_checkpoint_audit;"))
            conn.execute(text("ALTER TABLE pipeline_checkpoint_audit ENABLE TRIGGER trg_audit_prevent_mod;"))

        # 7. Commit or release savepoint
        if savepoint is not None:
            savepoint.commit()
            is_durable = False
            status = "pending_commit"
        else:
            if trans is not None:
                trans.commit()
            else:
                conn.commit()
            is_durable = True
            status = "purged"

    except Exception:
        if savepoint is not None:
            try:
                savepoint.rollback()
            except Exception:
                pass

        # Defensive safeguard: ensure immutability triggers remain active in failure modes
        if dialect_name == "sqlite":
            try:
                conn.execute(text("""
                    CREATE TRIGGER IF NOT EXISTS trg_audit_no_delete
                    BEFORE DELETE ON pipeline_checkpoint_audit
                    BEGIN
                        SELECT RAISE(ABORT, 'pipeline_checkpoint_audit is immutable and append-only');
                    END;
                """))
            except Exception:
                pass
        elif dialect_name == "postgresql":
            try:
                conn.execute(text("ALTER TABLE pipeline_checkpoint_audit ENABLE TRIGGER trg_audit_prevent_mod;"))
            except Exception:
                pass

        if not caller_owns_transaction:
            if trans is not None:
                try:
                    trans.rollback()
                except Exception:
                    pass
            else:
                try:
                    conn.rollback()
                except Exception:
                    pass
        raise
    finally:
        if owns_connection:
            conn.close()

    logger.info("Administrative purge completed: %d records, durable=%s at %s", count, is_durable, archive_path)
    return {
        "status": status,
        "is_durable": is_durable,
        "purged_record_count": count,
        "archive_path": str(archive_path),
        "verification": verify_result,
    }
