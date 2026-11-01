"""
Idempotent Job Lifecycle & Execution State Manager.
Implements:
- Persistent job lifecycle: PENDING, RUNNING, COMPLETED, FAILED, CANCELLED
- Separation of job state from the financial ledger
- Result reuse for completed requests
- Conflict prevention for currently running requests
- Retryability for failed requests
- Application restart recovery / reconciliation
- Sanitization of error messages to protect sensitive provider details
"""

import re
import json
import hashlib
import traceback
from typing import Any, Tuple, Optional, Dict
from datetime import datetime, timezone
from sqlalchemy.orm import Session
from fastapi import HTTPException

from backend.app.models.entities import StudioJob

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

def compute_idempotency_key(project_id: str, job_type: str, payload: Any) -> str:
    """Computes a deterministic SHA-256 hash identifying identical jobs."""
    raw = json.dumps({
        "project_id": str(project_id),
        "job_type": str(job_type),
        "payload": payload or {}
    }, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()

class JobManager:
    @staticmethod
    def prepare_or_claim_job(
        db: Session,
        project_id: str,
        job_type: str,
        payload: Any
    ) -> Tuple[StudioJob, str]:
        """
        Manages job execution state and idempotency.
        Returns (job, action):
        - ("REUSE", job): Job is already COMPLETED. Reuse existing result without running or charging.
        - ("IN_PROGRESS", job): Job is currently RUNNING or PENDING. Do not execute twice.
        - ("EXECUTE", job): Job is newly claimed or a previously FAILED job is being retried.
        """
        key = compute_idempotency_key(project_id, job_type, payload)
        payload_str = json.dumps(payload, default=str) if payload else "{}"

        job = db.query(StudioJob).filter(
            StudioJob.project_id == project_id,
            StudioJob.job_type == job_type,
            StudioJob.idempotency_key == key
        ).first()

        now = datetime.now(timezone.utc)

        if job:
            if job.status == "COMPLETED":
                return job, "REUSE"
            elif job.status in ("RUNNING", "PENDING"):
                return job, "IN_PROGRESS"
            elif job.status in ("FAILED", "CANCELLED"):
                # Retryable: transition back to RUNNING
                job.status = "RUNNING"
                job.retry_count += 1
                job.error_message = ""
                job.internal_error = ""
                job.updated_at = now
                db.commit()
                db.refresh(job)
                return job, "EXECUTE"

        # New job
        new_job = StudioJob(
            project_id=project_id,
            job_type=job_type,
            idempotency_key=key,
            status="RUNNING",
            payload=payload_str,
            retry_count=0,
            created_at=now,
            updated_at=now
        )
        db.add(new_job)
        db.commit()
        db.refresh(new_job)
        return new_job, "EXECUTE"

    @staticmethod
    def complete_job(db: Session, job_id: str, result: Any) -> StudioJob:
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

    @staticmethod
    def fail_job(
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

    @staticmethod
    def reconcile_interrupted_jobs(db: Session) -> int:
        """
        Recovers jobs left in RUNNING or PENDING after an unexpected application crash or restart.
        Marks them as FAILED with a clear retryable message.
        """
        interrupted = db.query(StudioJob).filter(
            StudioJob.status.in_(["RUNNING", "PENDING"])
        ).all()

        now = datetime.now(timezone.utc)
        count = len(interrupted)
        for job in interrupted:
            job.status = "FAILED"
            job.error_message = "Job execution was interrupted by server restart. Ready to retry."
            job.internal_error = "Server terminated while job was in active state."
            job.updated_at = now

        if count > 0:
            db.commit()

        return count

job_manager = JobManager()
