"""
Idempotent Job Lifecycle & Execution State Manager.
Implements:
- Persistent job lifecycle: PENDING, RUNNING, COMPLETED, FAILED, CANCELLED
- Separation of job state from the financial ledger
- Result reuse for completed requests
- Database-enforced race-safe job claiming with IntegrityError catch and re-query
- Retryability for failed requests without creating duplicate rows
- Worker lease mechanism preventing conflicting worker overwrites
- Application restart recovery / reconciliation
- Sanitization of error messages to protect sensitive provider details
"""

import os
import re
import json
import uuid
import hashlib
import traceback
from typing import Any, Tuple, Optional, Dict
from datetime import datetime, timezone, timedelta
from sqlalchemy import update, or_
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from fastapi import HTTPException

from backend.app.models.entities import StudioJob

# Process-level worker identity for local execution & worker leases
CURRENT_WORKER_ID = f"worker-{os.getpid()}-{uuid.uuid4().hex[:6]}"
DEFAULT_LEASE_SECONDS = 300  # 5 minutes

class JobOwnershipLostError(HTTPException):
    """Raised when an operation attempts to renew, complete, or fail a job whose lease or token has expired or been reclaimed."""
    def __init__(self, detail: str = "Worker lease ownership lost or preempted by another worker."):
        super().__init__(status_code=409, detail=detail)

# Regex patterns for stripping sensitive tokens from error outputs
SENSITIVE_PATTERNS = [
    re.compile(r"AIza[0-9A-Za-z-_]{25,45}"), # Google API Keys
    re.compile(r"bearer\s+[A-Za-z0-9._-]+", re.IGNORECASE),
    re.compile(r"sk-[A-Za-z0-9]{20,}", re.IGNORECASE),
    re.compile(r"/home/[a-zA-Z0-9_-]+/[^\s:\"']+"), # Server file paths
]

def sanitize_error_message(error: Any) -> str:
    """
    Sanitizes raw errors to protect sensitive keys, paths, and internal endpoints
    from being exposed to frontend users or API responses.
    """
    msg = str(error)
    for pattern in SENSITIVE_PATTERNS:
        msg = pattern.sub("[REDACTED]", msg)
        
    # Remove raw traceback details from user-facing message if present
    if "Traceback (most recent call last)" in msg:
        lines = [line.strip() for line in msg.split("\n") if line.strip()]
        msg = lines[-1] if lines else "Operation encountered an internal error"

    return msg[:256]

def _is_lease_expired(lease_dt: Optional[datetime], now_dt: datetime) -> bool:
    if lease_dt is None:
        return True
    if lease_dt.tzinfo is not None:
        lease_dt = lease_dt.astimezone(timezone.utc).replace(tzinfo=None)
    if now_dt.tzinfo is not None:
        now_dt = now_dt.astimezone(timezone.utc).replace(tzinfo=None)
    return lease_dt < now_dt

def compute_idempotency_key(project_id: str, job_type: str, payload: Any) -> str:
    """Computes a deterministic SHA-256 hash identifying identical jobs."""
    if job_type == "screenplay_generate" and isinstance(payload, dict):
        # Requirement 1: Extract stable request identity, excluding server-assigned reservation and hashes
        stable_payload = {
            "project_id": str(project_id),
            "prompt": str(payload.get("prompt") or ""),
            "character_ids": sorted(payload.get("character_ids") or []),
            "location_id": str(payload.get("location_id") or ""),
            "requested_scene_number": payload.get("requested_scene_number"),
            "request_token": payload.get("request_token")
        }
        raw = json.dumps({
            "project_id": str(project_id),
            "job_type": str(job_type),
            "payload": stable_payload
        }, sort_keys=True, default=str)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    raw = json.dumps({
        "project_id": str(project_id),
        "job_type": str(job_type),
        "payload": payload or {}
    }, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()

class JobManager:
    def __init__(self, worker_id: str = CURRENT_WORKER_ID):
        self.worker_id = worker_id

    def prepare_or_claim_job(
        self,
        db: Session,
        project_id: str,
        job_type: str,
        payload: Any,
        lease_seconds: int = DEFAULT_LEASE_SECONDS
    ) -> Tuple[StudioJob, str]:
        """
        Manages job execution state and idempotency with database-enforced race safety.
        Returns (job, action):
        - (job, "REUSE"): Job is already COMPLETED. Reuse existing result without running or charging.
        - (job, "IN_PROGRESS"): Job is currently RUNNING or PENDING. Do not execute twice.
        - (job, "EXECUTE"): Job is newly claimed or a previously FAILED/CANCELLED job is being retried.
        """
        key = compute_idempotency_key(project_id, job_type, payload)
        payload_str = json.dumps(payload, default=str) if payload else "{}"
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        lease_expires = now + timedelta(seconds=lease_seconds)

        job = db.query(StudioJob).filter(
            StudioJob.project_id == project_id,
            StudioJob.job_type == job_type,
            StudioJob.idempotency_key == key
        ).first()

        if job:
            if job.status == "COMPLETED":
                return job, "REUSE"
            elif job.status in ("RUNNING", "PENDING"):
                # Check if lease expired (dead worker recovery)
                if job.lease_expires_at is None or _is_lease_expired(job.lease_expires_at, now):
                    current_token = job.fencing_token or 1
                    next_token = current_token + 1
                    # Conditional atomic UPDATE:
                    # 1. Job must still be RUNNING or PENDING
                    # 2. Lease must be expired (lease_expires_at < now or lease_expires_at is NULL)
                    # 3. fencing_token must match current_token
                    stmt = (
                        update(StudioJob)
                        .where(StudioJob.id == job.id)
                        .where(StudioJob.status.in_(["RUNNING", "PENDING"]))
                        .where(
                            or_(
                                StudioJob.lease_expires_at == None,
                                StudioJob.lease_expires_at < now
                            )
                        )
                        .where(StudioJob.fencing_token == current_token)
                        .values(
                            worker_id=self.worker_id,
                            fencing_token=next_token,
                            lease_token=next_token,
                            lease_expires_at=lease_expires,
                            updated_at=now
                        )
                        .execution_options(synchronize_session=False)
                    )
                    res = db.execute(stmt)
                    db.commit()
                    if res.rowcount > 0:
                        db.refresh(job)
                        return job, "EXECUTE"
                return job, "IN_PROGRESS"
            elif job.status in ("FAILED", "CANCELLED"):
                # Atomic conditional retry transition without inserting a second row
                current_token = job.fencing_token or 1
                next_token = current_token + 1
                stmt = (
                    update(StudioJob)
                    .where(StudioJob.id == job.id)
                    .where(StudioJob.status.in_(["FAILED", "CANCELLED"]))
                    .where(StudioJob.fencing_token == current_token)
                    .values(
                        status="RUNNING",
                        retry_count=StudioJob.retry_count + 1,
                        worker_id=self.worker_id,
                        fencing_token=next_token,
                        lease_token=next_token,
                        lease_expires_at=lease_expires,
                        error_message="",
                        internal_error="",
                        updated_at=now
                    )
                    .execution_options(synchronize_session=False)
                )
                res = db.execute(stmt)
                db.commit()
                db.refresh(job)
                if res.rowcount > 0:
                    return job, "EXECUTE"
                else:
                    # Another concurrent request already claimed the retry
                    if job.status == "COMPLETED":
                        return job, "REUSE"
                    return job, "IN_PROGRESS"

        # No existing job found: attempt insertion. Database unique constraint prevents race conditions.
        new_job = StudioJob(
            project_id=project_id,
            job_type=job_type,
            idempotency_key=key,
            status="RUNNING",
            worker_id=self.worker_id,
            fencing_token=1,
            lease_token=1,
            lease_expires_at=lease_expires,
            payload=payload_str,
            retry_count=0,
            created_at=now,
            updated_at=now
        )
        db.add(new_job)
        try:
            db.commit()
            db.refresh(new_job)
            return new_job, "EXECUTE"
        except IntegrityError:
            # Race condition: concurrent request committed first.
            db.rollback()
            existing = db.query(StudioJob).filter(
                StudioJob.project_id == project_id,
                StudioJob.job_type == job_type,
                StudioJob.idempotency_key == key
            ).first()
            if not existing:
                raise HTTPException(status_code=500, detail="Job claim race condition recovery failed")

            if existing.status == "COMPLETED":
                return existing, "REUSE"
            elif existing.status in ("RUNNING", "PENDING"):
                return existing, "IN_PROGRESS"
            elif existing.status in ("FAILED", "CANCELLED"):
                return existing, "IN_PROGRESS"

            return existing, "IN_PROGRESS"

    def renew_lease(
        self,
        db: Session,
        job_id: str,
        worker_id: Optional[str] = None,
        fencing_token: Optional[int] = None,
        lease_token: Optional[int] = None,
        extend_seconds: int = DEFAULT_LEASE_SECONDS
    ) -> bool:
        """
        Heartbeat renewal during long-running tasks.
        Extends lease_expires_at ONLY IF worker_id, fencing_token match the current owner,
        status is RUNNING, and lease has not expired.
        Returns True if renewed, False if ownership was lost or lease already expired.
        Raises JobOwnershipLostError if fencing token is missing.
        """
        expected_token = fencing_token if fencing_token is not None else lease_token
        if expected_token is None:
            raise JobOwnershipLostError("Fencing token is required for lease renewal.")

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        target_worker = worker_id or self.worker_id
        new_expires = now + timedelta(seconds=extend_seconds)

        stmt = (
            update(StudioJob)
            .where(StudioJob.id == job_id)
            .where(StudioJob.status == "RUNNING")
            .where(StudioJob.worker_id == target_worker)
            .where(StudioJob.fencing_token == expected_token)
            .where(StudioJob.lease_expires_at >= now)
            .values(
                lease_expires_at=new_expires,
                updated_at=now
            )
            .execution_options(synchronize_session=False)
        )
        res = db.execute(stmt)
        db.commit()
        return res.rowcount > 0

    def assert_ownership(
        self,
        db: Session,
        job_id: str,
        worker_id: Optional[str] = None,
        fencing_token: Optional[int] = None,
        lease_token: Optional[int] = None
    ) -> None:
        """
        Verifies that current worker still owns the lease.
        Raises JobOwnershipLostError (HTTP 409) immediately if lease was lost, expired, or token is missing.
        """
        expected_token = fencing_token if fencing_token is not None else lease_token
        if expected_token is None:
            raise JobOwnershipLostError("Fencing token is required to assert job ownership.")

        target_worker = worker_id or self.worker_id
        now = datetime.now(timezone.utc).replace(tzinfo=None)

        job = db.query(StudioJob).filter(StudioJob.id == job_id).first()
        if not job:
            raise JobOwnershipLostError(f"Job '{job_id}' no longer exists.")

        try:
            db.refresh(job)
        except Exception:
            pass

        if job.status not in ("RUNNING", "PENDING"):
            raise JobOwnershipLostError(
                f"Job '{job_id}' is no longer active (status: {job.status}). Worker '{target_worker}' must abort."
            )
        if job.worker_id != target_worker:
            raise JobOwnershipLostError(
                f"Worker '{target_worker}' lost ownership of job '{job_id}' to worker '{job.worker_id}'."
            )
        if job.fencing_token != expected_token:
            raise JobOwnershipLostError(
                f"Fencing token mismatch for job '{job_id}': expected {expected_token}, found {job.fencing_token}."
            )
        if _is_lease_expired(job.lease_expires_at, now):
            raise JobOwnershipLostError(
                f"Lease for job '{job_id}' expired at {job.lease_expires_at} (current time: {now})."
            )

    def complete_job(
        self,
        db: Session,
        job_id: str,
        result: Any,
        worker_id: Optional[str] = None,
        fencing_token: Optional[int] = None,
        lease_token: Optional[int] = None,
        commit: bool = True
    ) -> StudioJob:
        """
        Marks a job as COMPLETED and stores its serialized output.
        Enforces worker fencing: conditional SQL update checks worker_id, canonical fencing_token,
        RUNNING status, and valid unexpired lease.
        Raises JobOwnershipLostError (HTTP 409) if token is missing, ownership was lost, or lease expired.
        """
        expected_token = fencing_token if fencing_token is not None else lease_token
        if expected_token is None:
            raise JobOwnershipLostError("Fencing token is required to complete a job.")

        target_worker = worker_id or self.worker_id
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        result_str = json.dumps(result, default=str) if result is not None else "{}"

        stmt = (
            update(StudioJob)
            .where(StudioJob.id == job_id)
            .where(StudioJob.status == "RUNNING")
            .where(StudioJob.worker_id == target_worker)
            .where(StudioJob.fencing_token == expected_token)
            .where(StudioJob.lease_expires_at >= now)
            .values(
                status="COMPLETED",
                result=result_str,
                completed_at=now,
                updated_at=now
            )
            .execution_options(synchronize_session=False)
        )
        res = db.execute(stmt)
        if commit:
            db.commit()

        if res.rowcount == 0:
            current = db.query(StudioJob).filter(StudioJob.id == job_id).first()
            if not current:
                raise ValueError(f"Job {job_id} not found")
            raise JobOwnershipLostError(
                f"Worker '{target_worker}' (token {expected_token}) cannot complete job '{job_id}': "
                f"ownership was lost or superseded (lease expired or token superseded). "
                f"Current state: worker='{current.worker_id}', token={current.fencing_token}, "
                f"status='{current.status}'."
            )

        job = db.query(StudioJob).filter(StudioJob.id == job_id).first()
        return job

    def fail_job(
        self,
        db: Session,
        job_id: str,
        error: Any,
        user_facing_error: Optional[str] = None,
        worker_id: Optional[str] = None,
        fencing_token: Optional[int] = None,
        lease_token: Optional[int] = None,
        commit: bool = True
    ) -> StudioJob:
        """
        Marks a job as FAILED, storing safe user message while preserving internal diagnostics.
        Enforces worker fencing: conditional SQL update checks worker_id, canonical fencing_token,
        RUNNING status, and valid unexpired lease.
        Raises JobOwnershipLostError (HTTP 409) if token is missing, ownership was lost, or lease expired.
        """
        expected_token = fencing_token if fencing_token is not None else lease_token
        if expected_token is None:
            raise JobOwnershipLostError("Fencing token is required to fail a job.")

        target_worker = worker_id or self.worker_id
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        raw_internal = traceback.format_exc() if isinstance(error, Exception) else str(error)
        safe_msg = user_facing_error or sanitize_error_message(error)

        stmt = (
            update(StudioJob)
            .where(StudioJob.id == job_id)
            .where(StudioJob.status == "RUNNING")
            .where(StudioJob.worker_id == target_worker)
            .where(StudioJob.fencing_token == expected_token)
            .where(StudioJob.lease_expires_at >= now)
            .values(
                status="FAILED",
                error_message=safe_msg,
                internal_error=raw_internal[:2048],
                updated_at=now
            )
            .execution_options(synchronize_session=False)
        )
        res = db.execute(stmt)
        if commit:
            db.commit()

        if res.rowcount == 0:
            current = db.query(StudioJob).filter(StudioJob.id == job_id).first()
            if not current:
                raise ValueError(f"Job {job_id} not found")
            raise JobOwnershipLostError(
                f"Worker '{target_worker}' (token {expected_token}) cannot fail job '{job_id}': "
                f"ownership was lost or superseded (lease expired or token superseded). "
                f"Current state: worker='{current.worker_id}', token={current.fencing_token}, "
                f"status='{current.status}'."
            )

        job = db.query(StudioJob).filter(StudioJob.id == job_id).first()
        return job

    def reconcile_interrupted_jobs(
        self,
        db: Session,
        current_worker_id: Optional[str] = None
    ) -> int:
        """
        Recovers jobs left in RUNNING or PENDING after unexpected termination.
        Only reconciles:
        - Jobs owned by this worker (from prior run), OR
        - Jobs with expired leases (dead workers).
        Active jobs with valid leases on other workers are preserved.
        Increments fencing_token and lease_token to fence off any late zombie workers.
        """
        now = datetime.now(timezone.utc)
        target_worker = current_worker_id or self.worker_id

        candidates = db.query(StudioJob).filter(
            StudioJob.status.in_(["RUNNING", "PENDING"])
        ).all()

        interrupted = []
        for job in candidates:
            if job.worker_id == target_worker:
                interrupted.append(job)
            elif job.lease_expires_at is None or _is_lease_expired(job.lease_expires_at, now):
                interrupted.append(job)

        count = len(interrupted)
        for job in interrupted:
            job.status = "FAILED"
            new_token = (job.fencing_token or job.lease_token or 1) + 1
            job.fencing_token = new_token
            job.lease_token = new_token
            job.error_message = "Job execution was interrupted by server restart or worker lease expiration. Ready to retry."
            job.internal_error = "Server terminated or lease expired while job was in active state."
            job.updated_at = now

        if count > 0:
            db.commit()

        return count

job_manager = JobManager()
