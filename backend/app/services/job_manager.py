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
        return False
    if lease_dt.tzinfo is None:
        return lease_dt.replace(tzinfo=timezone.utc) < now_dt
    return lease_dt < now_dt

def compute_idempotency_key(project_id: str, job_type: str, payload: Any) -> str:
    """Computes a deterministic SHA-256 hash identifying identical jobs."""
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
        now = datetime.now(timezone.utc)
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
                if job.lease_expires_at and _is_lease_expired(job.lease_expires_at, now):
                    stmt = (
                        update(StudioJob)
                        .where(StudioJob.id == job.id)
                        .where(StudioJob.status.in_(["RUNNING", "PENDING"]))
                        .values(
                            worker_id=self.worker_id,
                            lease_expires_at=lease_expires,
                            updated_at=now
                        )
                    )
                    res = db.execute(stmt)
                    db.commit()
                    if res.rowcount > 0:
                        db.refresh(job)
                        return job, "EXECUTE"
                return job, "IN_PROGRESS"
            elif job.status in ("FAILED", "CANCELLED"):
                # Atomic conditional retry transition without inserting a second row
                stmt = (
                    update(StudioJob)
                    .where(StudioJob.id == job.id)
                    .where(StudioJob.status.in_(["FAILED", "CANCELLED"]))
                    .values(
                        status="RUNNING",
                        retry_count=StudioJob.retry_count + 1,
                        worker_id=self.worker_id,
                        lease_expires_at=lease_expires,
                        error_message="",
                        internal_error="",
                        updated_at=now
                    )
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

    def complete_job(self, db: Session, job_id: str, result: Any) -> StudioJob:
        """Marks a job as COMPLETED and stores its serialized output."""
        job = db.query(StudioJob).filter(StudioJob.id == job_id).first()
        if not job:
            raise ValueError(f"Job {job_id} not found")

        job.status = "COMPLETED"
        job.result = json.dumps(result, default=str) if result is not None else "{}"
        job.completed_at = datetime.now(timezone.utc)
        job.updated_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(job)
        return job

    def fail_job(
        self,
        db: Session,
        job_id: str,
        error: Any,
        user_facing_error: Optional[str] = None
    ) -> StudioJob:
        """
        Marks a job as FAILED, storing safe user message while preserving internal diagnostics.
        """
        job = db.query(StudioJob).filter(StudioJob.id == job_id).first()
        if not job:
            raise ValueError(f"Job {job_id} not found")

        raw_internal = traceback.format_exc() if isinstance(error, Exception) else str(error)
        safe_msg = user_facing_error or sanitize_error_message(error)

        job.status = "FAILED"
        job.error_message = safe_msg
        job.internal_error = raw_internal[:2048]
        job.updated_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(job)
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
        """
        now = datetime.now(timezone.utc)
        target_worker = current_worker_id or self.worker_id

        # Target expired jobs or jobs owned by current worker
        interrupted = db.query(StudioJob).filter(
            StudioJob.status.in_(["RUNNING", "PENDING"]),
            or_(
                StudioJob.worker_id == target_worker,
                StudioJob.lease_expires_at == None,
                StudioJob.lease_expires_at <= now
            )
        ).all()

        count = len(interrupted)
        for job in interrupted:
            job.status = "FAILED"
            job.error_message = "Job execution was interrupted by server restart or worker lease expiration. Ready to retry."
            job.internal_error = "Server terminated or lease expired while job was in active state."
            job.updated_at = now

        if count > 0:
            db.commit()

        return count

job_manager = JobManager()
