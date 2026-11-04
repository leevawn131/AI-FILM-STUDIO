"""
Pipeline Orchestrator Service.
Choreographs the end-to-end filmmaking production pipeline across 11 discrete stages
and 4 human approval checkpoints.

Canonical Single Source of Truth:
- PipelineStageState: Authoritative state of each production stage.
- PipelineCheckpoint: Authoritative state of each human approval gate.
- PipelineCheckpointAudit: Immutable, append-only compliance journal.
- Project.current_pipeline_stage & Project.pipeline_overall_status: Derived projections
  updated transactionally by orchestrator operations.

SECURITY & AUTHORIZATION NOTICE:
Pipeline endpoints and orchestrator methods currently use project ID scoping but do not
enforce user-level project ownership. Cross-project query filtering is not equivalent
to user authorization. This limitation must be addressed when multi-user access or public
API exposure is introduced.
"""

import json
import hashlib
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional, Tuple, Set
from sqlalchemy import update, event
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session
from fastapi import HTTPException

from backend.app.models.entities import (
    Project, Scene, Shot, Character, Location,
    PipelineStageState, PipelineCheckpoint, PipelineCheckpointAudit,
    StudioJob, gen_uuid
)
from backend.app.services.job_manager import job_manager, JobOwnershipLostError
from backend.app.schemas.pipeline import (
    sanitize_error_text,
    sanitize_stage_metadata,
    CheckpointDecisionRequest,
)

def utc_now() -> datetime:
    return datetime.now(timezone.utc)

PIPELINE_STAGES = [
    "story_development",
    "screenplay",
    "character_design",
    "location_design",
    "storyboard",
    "cinematic_prompts",
    "flow_handoff",
    "media_import",
    "film_editing",
    "audio_mixing",
    "final_rendering"
]

PIPELINE_CHECKPOINTS = [
    ("story_approval", "screenplay"),
    ("visual_approval", "visual_bible"),
    ("production_approval", "storyboard"),
    ("final_review", "assembly_cut")
]

STAGE_CHECKPOINT_MAPPING = {
    "screenplay": "story_approval",
    "location_design": "visual_approval",
    "cinematic_prompts": "production_approval",
    "audio_mixing": "final_review",
}

CHECKPOINT_PREREQUISITE_STAGE = {
    "story_approval": "screenplay",
    "visual_approval": "location_design",
    "production_approval": "cinematic_prompts",
    "final_review": "audio_mixing",
}

# ---------------------------------------------------------------------------
# State Transition Rules (M2.4)
# ---------------------------------------------------------------------------

ALLOWED_CHECKPOINT_TRANSITIONS: Dict[str, Set[str]] = {
    "LOCKED": {"WAITING_FOR_APPROVAL"},
    "WAITING_FOR_APPROVAL": {"APPROVED", "REJECTED", "STALE"},
    "APPROVED": {"STALE"},
    "REJECTED": {"STALE", "WAITING_FOR_APPROVAL"},
    "STALE": {"WAITING_FOR_APPROVAL"},
}

ALLOWED_STAGE_TRANSITIONS: Dict[str, Set[str]] = {
    "NOT_STARTED": {"IN_PROGRESS", "WAITING_FOR_APPROVAL", "COMPLETED"},
    "IN_PROGRESS": {"COMPLETED", "FAILED", "WAITING_FOR_APPROVAL", "WAITING_FOR_EXTERNAL_MEDIA", "WAITING_FOR_USER_INPUT", "STALE"},
    "WAITING_FOR_APPROVAL": {"COMPLETED", "WAITING_FOR_USER_INPUT", "STALE", "FAILED"},
    "WAITING_FOR_EXTERNAL_MEDIA": {"IN_PROGRESS", "COMPLETED", "FAILED", "STALE"},
    "WAITING_FOR_USER_INPUT": {"IN_PROGRESS", "WAITING_FOR_APPROVAL", "STALE"},
    "COMPLETED": {"STALE", "IN_PROGRESS"},
    "FAILED": {"IN_PROGRESS", "NOT_STARTED", "WAITING_FOR_APPROVAL"},
    "STALE": {"IN_PROGRESS", "WAITING_FOR_APPROVAL"},
}


def validate_checkpoint_transition(current_status: str, target_status: str) -> bool:
    """Validates if a checkpoint status transition is legally permitted."""
    if current_status == target_status:
        return True
    allowed = ALLOWED_CHECKPOINT_TRANSITIONS.get(current_status, set())
    return target_status in allowed


def validate_stage_transition(current_status: str, target_status: str) -> bool:
    """Validates if a stage status transition is legally permitted."""
    if current_status == target_status:
        return True
    allowed = ALLOWED_STAGE_TRANSITIONS.get(current_status, set())
    return target_status in allowed


@event.listens_for(Session, "after_transaction_end")
def _clear_project_locks(session, transaction):
    if transaction.nested:
        return
    session.info.pop("_active_sqlite_write_lock_tx", None)
    session.info.pop("_locked_projects", None)
    session.info.pop("_pg_locked_projects", None)


def acquire_project_write_lock(db: Session, project_id: str) -> Optional[Project]:
    """
    Acquires an exclusive write lock for the project across SQLite and PostgreSQL.
    - PostgreSQL: Executes SELECT ... FOR UPDATE on the Project row to serialize writers.
      Natively reentrant within the same transaction.
    - SQLite: Starts a BEGIN IMMEDIATE transaction before reading snapshots,
      ensuring that transactions are serialized across concurrent processes.
      Reentrant within the same active SessionTransaction.
      If a caller-owned transaction is already in progress without write lock serialization,
      raises HTTP 409 WITHOUT rolling back caller's uncommitted work.
    """
    bind = db.get_bind()
    dialect = bind.dialect.name if bind else "sqlite"

    if dialect == "postgresql":
        project = db.query(Project).filter(Project.id == project_id).with_for_update().first()
        if project:
            db.info.setdefault("_pg_locked_projects", set()).add(project_id)
            db.info.setdefault("_locked_projects", set()).add(project_id)
        return project
    else:
        current_tx = db.get_transaction()
        conn = db.connection()
        raw_conn = getattr(conn.connection, "dbapi_connection", None)
        if raw_conn is None:
            raw_conn = getattr(conn.connection, "connection", None)
        in_dbapi_tx = getattr(raw_conn, "in_transaction", False)

        # 1. Reentrant lock check for active SQLite write transaction
        tx_write_locked = (
            current_tx is not None
            and current_tx.is_active
            and in_dbapi_tx
            and db.info.get("_active_sqlite_write_lock_tx") is current_tx
        )
        if tx_write_locked:
            db.info.setdefault("_locked_projects", set()).add(project_id)
            return db.query(Project).filter(Project.id == project_id).first()

        # 2. Check if caller already has an active transaction or pending writes
        has_caller_pending_writes = bool(db.new or db.dirty or db.deleted)
        if in_dbapi_tx or has_caller_pending_writes:
            # Caller owns this active transaction; do NOT roll it back!
            raise HTTPException(
                status_code=409,
                detail="Cannot safely acquire SQLite write lock: caller transaction is already in progress without write lock serialization."
            )

        # 3. Fresh transaction: helper attempts BEGIN IMMEDIATE
        try:
            conn.exec_driver_sql("BEGIN IMMEDIATE")
        except Exception as lock_err:
            # Helper initiated this transaction attempt, so helper safely rolls back
            db.rollback()
            err_msg = str(lock_err).lower()
            if "locked" in err_msg or "busy" in err_msg:
                raise HTTPException(
                    status_code=409,
                    detail="Database write lock contention: database is locked by concurrent transaction. Please retry."
                ) from lock_err
            raise HTTPException(
                status_code=500,
                detail=f"Failed to acquire SQLite write lock: {lock_err}"
            ) from lock_err

        # Write lock acquired: record transaction identity
        db.info["_active_sqlite_write_lock_tx"] = db.get_transaction()
        db.info.setdefault("_locked_projects", set()).add(project_id)
        try:
            project = db.query(Project).filter(Project.id == project_id).first()
        except Exception:
            db.rollback()
            raise
        return project


def is_pipeline_complete(db: Session, project_id: str) -> bool:
    """
    Checks if all 11 discrete stages and all 4 checkpoints exist for the project.
    Verifies exact equality of stage names and checkpoint types (rejects unexpected or missing records).
    """
    stage_names = {
        name for (name,) in db.query(PipelineStageState.stage_name)
        .filter(PipelineStageState.project_id == project_id).all()
    }
    if stage_names != set(PIPELINE_STAGES):
        return False

    cp_types = {
        cp_type for (cp_type,) in db.query(PipelineCheckpoint.checkpoint_type)
        .filter(PipelineCheckpoint.project_id == project_id).all()
    }
    expected_cps = {cp[0] for cp in PIPELINE_CHECKPOINTS}
    if cp_types != expected_cps:
        return False

    return True


def update_project_pipeline_projections(db: Session, project_id: str) -> None:
    """
    Updates Project.current_pipeline_stage and Project.pipeline_overall_status
    as derived projections of PipelineStageState (canonical source of truth).
    """
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        return
    stages = db.query(PipelineStageState).filter(PipelineStageState.project_id == project_id).all()
    stage_map = {s.stage_name: s for s in stages}

    current_stage = PIPELINE_STAGES[0]
    overall_status = "in_progress"

    all_completed = True
    for stage_name in PIPELINE_STAGES:
        st = stage_map.get(stage_name)
        if st and st.status != "COMPLETED":
            current_stage = stage_name
            all_completed = False
            if st.status == "FAILED":
                overall_status = "failed"
            elif st.status in ("WAITING_FOR_APPROVAL", "WAITING_FOR_USER_INPUT", "WAITING_FOR_EXTERNAL_MEDIA"):
                overall_status = st.status.lower()
            elif st.status == "IN_PROGRESS":
                overall_status = "in_progress"
            else:
                overall_status = "in_progress"
            break

    if all_completed and len(stage_map) >= len(PIPELINE_STAGES):
        current_stage = PIPELINE_STAGES[-1]
        overall_status = "completed"

    project.current_pipeline_stage = current_stage
    project.pipeline_overall_status = overall_status


def initialize_project_pipeline(project_id: Any, db: Any = None, auto_commit: bool = True) -> None:
    """
    Atomically and idempotently initializes all 11 production stages and 4 approval checkpoints.
    Safe for concurrent initialization across SQLite and PostgreSQL using dialect-specific
    INSERT ON CONFLICT DO NOTHING and PostgreSQL row locking.
    Preserves all existing stages, checkpoints, and associated metadata.
    Supports initialize_project_pipeline(project_id, db) and initialize_project_pipeline(db, project_id).
    """
    if isinstance(project_id, Session) or hasattr(project_id, "query"):
        session: Session = project_id
        pid: str = db
    else:
        pid: str = project_id
        session: Session = db

    if not pid or not session:
        raise ValueError("Both project_id and db session are required for pipeline initialization")

    bind = session.get_bind()
    dialect_name = bind.dialect.name if bind else "sqlite"

    try:
        # Row-level lock on PostgreSQL to serialize concurrent initializations
        if dialect_name == "postgresql":
            project = session.query(Project).filter(Project.id == pid).with_for_update().first()
        else:
            project = session.query(Project).filter(Project.id == pid).first()

        if not project or project.deleted_at is not None:
            raise ValueError(f"Project '{pid}' not found or has been soft-deleted")

        now = utc_now()

        # 1. Atomic insertion for 11 Stages
        for stage_name in PIPELINE_STAGES:
            if dialect_name == "postgresql":
                stmt = (
                    pg_insert(PipelineStageState)
                    .values(
                        id=gen_uuid(),
                        project_id=pid,
                        stage_name=stage_name,
                        status="NOT_STARTED",
                        retry_count=0,
                        created_at=now,
                        updated_at=now
                    )
                    .on_conflict_do_nothing(index_elements=["project_id", "stage_name"])
                )
                session.execute(stmt)
            elif dialect_name == "sqlite":
                stmt = (
                    sqlite_insert(PipelineStageState)
                    .values(
                        id=gen_uuid(),
                        project_id=pid,
                        stage_name=stage_name,
                        status="NOT_STARTED",
                        retry_count=0,
                        created_at=now,
                        updated_at=now
                    )
                    .on_conflict_do_nothing(index_elements=["project_id", "stage_name"])
                )
                session.execute(stmt)
            else:
                exists = session.query(PipelineStageState).filter(
                    PipelineStageState.project_id == pid,
                    PipelineStageState.stage_name == stage_name
                ).first()
                if not exists:
                    session.add(PipelineStageState(
                        id=gen_uuid(),
                        project_id=pid,
                        stage_name=stage_name,
                        status="NOT_STARTED",
                        created_at=now,
                        updated_at=now
                    ))

        # 2. Atomic insertion for 4 Checkpoints
        for cp_type, artifact_type in PIPELINE_CHECKPOINTS:
            if dialect_name == "postgresql":
                stmt = (
                    pg_insert(PipelineCheckpoint)
                    .values(
                        id=gen_uuid(),
                        project_id=pid,
                        checkpoint_type=cp_type,
                        status="LOCKED",
                        artifact_type=artifact_type,
                        artifact_version=1,
                        artifact_content_hash=None,
                        reviewer_name="Director / User",
                        created_at=now,
                        updated_at=now
                    )
                    .on_conflict_do_nothing(index_elements=["project_id", "checkpoint_type"])
                )
                session.execute(stmt)
            elif dialect_name == "sqlite":
                stmt = (
                    sqlite_insert(PipelineCheckpoint)
                    .values(
                        id=gen_uuid(),
                        project_id=pid,
                        checkpoint_type=cp_type,
                        status="LOCKED",
                        artifact_type=artifact_type,
                        artifact_version=1,
                        artifact_content_hash=None,
                        reviewer_name="Director / User",
                        created_at=now,
                        updated_at=now
                    )
                    .on_conflict_do_nothing(index_elements=["project_id", "checkpoint_type"])
                )
                session.execute(stmt)
            else:
                exists = session.query(PipelineCheckpoint).filter(
                    PipelineCheckpoint.project_id == pid,
                    PipelineCheckpoint.checkpoint_type == cp_type
                ).first()
                if not exists:
                    session.add(PipelineCheckpoint(
                        id=gen_uuid(),
                        project_id=pid,
                        checkpoint_type=cp_type,
                        status="LOCKED",
                        artifact_type=artifact_type,
                        artifact_version=1,
                        artifact_content_hash=None,
                        reviewer_name="Director / User",
                        created_at=now,
                        updated_at=now
                    ))

        # 3. Synchronize projections if not set
        if not project.current_pipeline_stage:
            project.current_pipeline_stage = "story_development"
        if not project.pipeline_overall_status:
            project.pipeline_overall_status = "in_progress"

        # 4. Strict verification of exact stages and checkpoints
        stages = session.query(PipelineStageState).filter(PipelineStageState.project_id == pid).all()
        stage_names = {s.stage_name for s in stages}
        missing_stages = set(PIPELINE_STAGES) - stage_names
        if missing_stages:
            raise RuntimeError(f"Pipeline initialization failed to establish all stages: missing {missing_stages}")

        cps = session.query(PipelineCheckpoint).filter(PipelineCheckpoint.project_id == pid).all()
        cp_types = {cp.checkpoint_type for cp in cps}
        missing_cps = {c[0] for c in PIPELINE_CHECKPOINTS} - cp_types
        if missing_cps:
            raise RuntimeError(f"Pipeline initialization failed to establish all checkpoints: missing {missing_cps}")

        session.flush()
        if auto_commit:
            session.commit()
    except Exception:
        session.rollback()
        # If another worker concurrently initialized the pipeline, verify completeness
        if is_pipeline_complete(session, pid):
            return
        raise


class PipelineOrchestrator:
    @staticmethod
    def initialize_project_pipeline(project_id: Any, db: Any = None, auto_commit: bool = True) -> None:
        return initialize_project_pipeline(project_id, db, auto_commit=auto_commit)

    @staticmethod
    def is_pipeline_complete(db: Session, project_id: str) -> bool:
        return is_pipeline_complete(db, project_id)

    @staticmethod
    def update_project_pipeline_projections(db: Session, project_id: str) -> None:
        return update_project_pipeline_projections(db, project_id)

    @staticmethod
    def get_pipeline_status(db: Session, project_id: str) -> Dict[str, Any]:
        """
        Retrieves complete pipeline status, active stage, and checkpoints.
        Strict read-only: never mutates or commits database state for uninitialized projects.
        """
        project = db.query(Project).filter(Project.id == project_id).first()
        if not project or project.deleted_at is not None:
            raise HTTPException(status_code=404, detail="Project not found")

        stages = db.query(PipelineStageState).filter(PipelineStageState.project_id == project_id).all()
        checkpoints = db.query(PipelineCheckpoint).filter(PipelineCheckpoint.project_id == project_id).all()

        # Validate active_job_ids strictly against this project
        candidate_job_ids = [s.active_job_id for s in stages if s.active_job_id]
        valid_job_ids = set()
        if candidate_job_ids:
            matching_jobs = db.query(StudioJob.id).filter(
                StudioJob.id.in_(candidate_job_ids),
                StudioJob.project_id == project_id
            ).all()
            valid_job_ids = {j[0] for j in matching_jobs}

        stage_order = {name: idx for idx, name in enumerate(PIPELINE_STAGES)}
        sorted_stages = sorted(stages, key=lambda s: stage_order.get(s.stage_name, 99))

        cp_order = {cp[0]: idx for idx, cp in enumerate(PIPELINE_CHECKPOINTS)}
        sorted_checkpoints = sorted(checkpoints, key=lambda c: cp_order.get(c.checkpoint_type, 99))

        is_initialized = is_pipeline_complete(db, project_id)
        overall_status = project.pipeline_overall_status or ("uninitialized" if not is_initialized else "not_started")

        return {
            "project_id": project.id,
            "project_name": project.name,
            "current_pipeline_stage": project.current_pipeline_stage,
            "pipeline_overall_status": overall_status,
            "is_initialized": is_initialized,
            "stages": [
                {
                    "id": s.id,
                    "project_id": s.project_id,
                    "stage_name": s.stage_name,
                    "status": s.status,
                    "order_index": stage_order.get(s.stage_name, 0) + 1,
                    "active_job_id": s.active_job_id if s.active_job_id in valid_job_ids else None,
                    "retry_count": s.retry_count,
                    "content_hash": s.content_hash,
                    "consumed_input_hash": s.consumed_input_hash,
                    "error_message": sanitize_error_text(s.error_message),
                    "stage_metadata": sanitize_stage_metadata(s.stage_metadata),
                    "started_at": s.started_at,
                    "completed_at": s.completed_at,
                    "created_at": s.created_at,
                    "updated_at": s.updated_at,
                }
                for s in sorted_stages
            ],
            "checkpoints": [
                {
                    "id": cp.id,
                    "project_id": cp.project_id,
                    "checkpoint_type": cp.checkpoint_type,
                    "status": cp.status,
                    "artifact_type": cp.artifact_type,
                    "artifact_version": cp.artifact_version,
                    "artifact_content_hash": cp.artifact_content_hash,
                    "reviewer_name": cp.reviewer_name,
                    "feedback_notes": cp.feedback_notes,
                    "decided_at": cp.decided_at,
                    "created_at": cp.created_at,
                    "updated_at": cp.updated_at,
                }
                for cp in sorted_checkpoints
            ]
        }

    @staticmethod
    def get_stage_detail(db: Session, project_id: str, stage_name: str) -> Dict[str, Any]:
        """
        Retrieves detailed information for an individual pipeline stage,
        including associated background job information and associated checkpoint.
        Strict read-only: never mutates database state.
        """
        project = db.query(Project).filter(Project.id == project_id).first()
        if not project or project.deleted_at is not None:
            raise HTTPException(status_code=404, detail="Project not found")

        if stage_name not in PIPELINE_STAGES:
            raise HTTPException(
                status_code=404,
                detail=f"Stage '{stage_name}' not found. Valid stages: {', '.join(PIPELINE_STAGES)}"
            )

        stage = db.query(PipelineStageState).filter(
            PipelineStageState.project_id == project_id,
            PipelineStageState.stage_name == stage_name
        ).first()

        if not stage:
            raise HTTPException(
                status_code=404,
                detail=f"Stage '{stage_name}' has not been initialized for project '{project_id}'"
            )

        stage_order = {name: idx for idx, name in enumerate(PIPELINE_STAGES)}

        active_job_dict = None
        safe_active_job_id = None
        if stage.active_job_id:
            job = db.query(StudioJob).filter(
                StudioJob.id == stage.active_job_id,
                StudioJob.project_id == project_id
            ).first()
            if job:
                safe_active_job_id = job.id
                active_job_dict = {
                    "id": job.id,
                    "job_type": job.job_type,
                    "status": job.status,
                    "error_message": sanitize_error_text(job.error_message),
                    "created_at": job.created_at,
                    "updated_at": job.updated_at,
                }

        associated_cp_dict = None
        cp_type = STAGE_CHECKPOINT_MAPPING.get(stage_name)
        if cp_type:
            cp = db.query(PipelineCheckpoint).filter(
                PipelineCheckpoint.project_id == project_id,
                PipelineCheckpoint.checkpoint_type == cp_type
            ).first()
            if cp:
                associated_cp_dict = {
                    "id": cp.id,
                    "project_id": cp.project_id,
                    "checkpoint_type": cp.checkpoint_type,
                    "status": cp.status,
                    "artifact_type": cp.artifact_type,
                    "artifact_version": cp.artifact_version,
                    "artifact_content_hash": cp.artifact_content_hash,
                    "reviewer_name": cp.reviewer_name,
                    "feedback_notes": cp.feedback_notes,
                    "decided_at": cp.decided_at,
                    "created_at": cp.created_at,
                    "updated_at": cp.updated_at,
                }

        return {
            "id": stage.id,
            "project_id": stage.project_id,
            "stage_name": stage.stage_name,
            "status": stage.status,
            "order_index": stage_order.get(stage.stage_name, 0) + 1,
            "active_job_id": safe_active_job_id,
            "retry_count": stage.retry_count,
            "content_hash": stage.content_hash,
            "consumed_input_hash": stage.consumed_input_hash,
            "error_message": sanitize_error_text(stage.error_message),
            "stage_metadata": sanitize_stage_metadata(stage.stage_metadata),
            "started_at": stage.started_at,
            "completed_at": stage.completed_at,
            "created_at": stage.created_at,
            "updated_at": stage.updated_at,
            "active_job": active_job_dict,
            "associated_checkpoint": associated_cp_dict,
        }

    @staticmethod
    def get_checkpoint_detail(db: Session, project_id: str, checkpoint_type: str) -> Dict[str, Any]:
        """
        Retrieves detailed approval checkpoint state, hash freshness, and latest audit decision.
        Strict read-only: never mutates database state.
        """
        project = db.query(Project).filter(Project.id == project_id).first()
        if not project or project.deleted_at is not None:
            raise HTTPException(status_code=404, detail="Project not found")

        valid_cp_types = [cp[0] for cp in PIPELINE_CHECKPOINTS]
        if checkpoint_type not in valid_cp_types:
            raise HTTPException(
                status_code=404,
                detail=f"Checkpoint '{checkpoint_type}' not found. Valid checkpoints: {', '.join(valid_cp_types)}"
            )

        cp = db.query(PipelineCheckpoint).filter(
            PipelineCheckpoint.project_id == project_id,
            PipelineCheckpoint.checkpoint_type == checkpoint_type
        ).first()

        if not cp:
            raise HTTPException(
                status_code=404,
                detail=f"Checkpoint '{checkpoint_type}' has not been initialized for project '{project_id}'"
            )

        # Fresh hash calculation (distinguishing unsupported hashing vs missing artifact)
        hashing_supported = True
        current_hash = None
        try:
            current_hash = PipelineOrchestrator.compute_artifact_hash(db, project_id, cp.artifact_type)
        except NotImplementedError:
            hashing_supported = False
            current_hash = None

        is_fresh: Optional[bool] = None
        if not hashing_supported:
            # Cannot verify hash freshness because hashing is not implemented for this artifact type
            is_fresh = None
        elif not cp.artifact_content_hash:
            # Checkpoint does not yet have an approved baseline hash to compare against
            is_fresh = None
        else:
            # Hashing is supported and checkpoint has an approved hash
            if current_hash is not None:
                is_fresh = (cp.artifact_content_hash == current_hash)
            else:
                # Artifact was previously approved with a hash, but current artifact content is missing
                is_fresh = False

        # Latest audit record with tie-breaker
        latest_audit = (
            db.query(PipelineCheckpointAudit)
            .filter(
                PipelineCheckpointAudit.project_id == project_id,
                PipelineCheckpointAudit.checkpoint_type == checkpoint_type
            )
            .order_by(PipelineCheckpointAudit.created_at.desc(), PipelineCheckpointAudit.id.desc())
            .first()
        )

        latest_audit_dict = None
        if latest_audit:
            latest_audit_dict = {
                "id": latest_audit.id,
                "project_id": latest_audit.project_id,
                "project_name_snapshot": latest_audit.project_name_snapshot,
                "checkpoint_id": latest_audit.checkpoint_id,
                "checkpoint_type": latest_audit.checkpoint_type,
                "decision": latest_audit.decision,
                "artifact_content_hash": latest_audit.artifact_content_hash,
                "artifact_version": latest_audit.artifact_version,
                "reviewer_name": latest_audit.reviewer_name,
                "feedback_notes": latest_audit.feedback_notes,
                "created_at": latest_audit.created_at,
            }

        return {
            "id": cp.id,
            "project_id": cp.project_id,
            "checkpoint_type": cp.checkpoint_type,
            "status": cp.status,
            "artifact_type": cp.artifact_type,
            "artifact_version": cp.artifact_version,
            "artifact_content_hash": cp.artifact_content_hash,
            "current_artifact_hash": current_hash,
            "is_hash_fresh": is_fresh,
            "reviewer_name": cp.reviewer_name,
            "feedback_notes": cp.feedback_notes,
            "decided_at": cp.decided_at,
            "created_at": cp.created_at,
            "updated_at": cp.updated_at,
            "latest_audit": latest_audit_dict,
        }

    @staticmethod
    def get_checkpoint_audit_history(
        db: Session,
        project_id: str,
        checkpoint_type: str,
        limit: int = 50,
        offset: int = 0
    ) -> Dict[str, Any]:
        """
        Retrieves paginated, chronological compliance audit records for a checkpoint.
        """
        project = db.query(Project).filter(Project.id == project_id).first()
        if not project or project.deleted_at is not None:
            raise HTTPException(status_code=404, detail="Project not found")

        valid_cp_types = [cp[0] for cp in PIPELINE_CHECKPOINTS]
        if checkpoint_type not in valid_cp_types:
            raise HTTPException(
                status_code=404,
                detail=f"Checkpoint '{checkpoint_type}' not found. Valid checkpoints: {', '.join(valid_cp_types)}"
            )

        cp = db.query(PipelineCheckpoint).filter(
            PipelineCheckpoint.project_id == project_id,
            PipelineCheckpoint.checkpoint_type == checkpoint_type
        ).first()

        checkpoint_id = cp.id if cp else None

        query = db.query(PipelineCheckpointAudit).filter(
            PipelineCheckpointAudit.project_id == project_id,
            PipelineCheckpointAudit.checkpoint_type == checkpoint_type
        )

        total = query.count()
        records = (
            query.order_by(PipelineCheckpointAudit.created_at.desc(), PipelineCheckpointAudit.id.desc())
            .offset(offset)
            .limit(limit)
            .all()
        )

        items = [
            {
                "id": r.id,
                "project_id": r.project_id,
                "project_name_snapshot": r.project_name_snapshot,
                "checkpoint_id": r.checkpoint_id,
                "checkpoint_type": r.checkpoint_type,
                "decision": r.decision,
                "artifact_content_hash": r.artifact_content_hash,
                "artifact_version": r.artifact_version,
                "reviewer_name": r.reviewer_name,
                "feedback_notes": r.feedback_notes,
                "created_at": r.created_at,
            }
            for r in records
        ]

        return {
            "project_id": project_id,
            "checkpoint_type": checkpoint_type,
            "checkpoint_id": checkpoint_id,
            "total": total,
            "limit": limit,
            "offset": offset,
            "items": items,
        }

    @staticmethod
    def get_project_audit_history(
        db: Session,
        project_id: str,
        checkpoint_type: Optional[str] = None,
        limit: int = 50,
        offset: int = 0
    ) -> Dict[str, Any]:
        """
        Retrieves paginated compliance audit history across all checkpoints for a project.
        """
        project = db.query(Project).filter(Project.id == project_id).first()
        if not project or project.deleted_at is not None:
            raise HTTPException(status_code=404, detail="Project not found")

        query = db.query(PipelineCheckpointAudit).filter(
            PipelineCheckpointAudit.project_id == project_id
        )
        if checkpoint_type:
            query = query.filter(PipelineCheckpointAudit.checkpoint_type == checkpoint_type)

        total = query.count()
        records = (
            query.order_by(PipelineCheckpointAudit.created_at.desc(), PipelineCheckpointAudit.id.desc())
            .offset(offset)
            .limit(limit)
            .all()
        )

        items = [
            {
                "id": r.id,
                "project_id": r.project_id,
                "project_name_snapshot": r.project_name_snapshot,
                "checkpoint_id": r.checkpoint_id,
                "checkpoint_type": r.checkpoint_type,
                "decision": r.decision,
                "artifact_content_hash": r.artifact_content_hash,
                "artifact_version": r.artifact_version,
                "reviewer_name": r.reviewer_name,
                "feedback_notes": r.feedback_notes,
                "created_at": r.created_at,
            }
            for r in records
        ]

        return {
            "project_id": project_id,
            "checkpoint_type": checkpoint_type,
            "checkpoint_id": None,
            "total": total,
            "limit": limit,
            "offset": offset,
            "items": items,
        }

    @staticmethod
    def compute_artifact_hash(db: Session, project_id: str, artifact_type: str) -> Optional[str]:
        """
        Computes canonical SHA-256 hash using deterministic JSON serialization.
        Currently strictly scoped to 'screenplay' in Milestone 1 foundation.
        Raises NotImplementedError for other checkpoint types to prevent unsupported approvals.
        """
        if artifact_type == "screenplay":
            scenes = db.query(Scene).filter(Scene.project_id == project_id).order_by(Scene.order_index).all()
            if not scenes:
                return None
            canonical_list = [
                {
                    "scene_number": s.scene_number,
                    "order_index": s.order_index,
                    "slugline": s.slugline or "",
                    "title": s.title or "",
                    "summary": s.summary or "",
                    "script_content": s.script_content or "",
                    "estimated_duration": s.estimated_duration or 0
                }
                for s in scenes
            ]
            raw = json.dumps(canonical_list, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode('utf-8')
            return hashlib.sha256(raw).hexdigest()

        raise NotImplementedError(
            f"Artifact hashing for '{artifact_type}' is not implemented in Milestone 1 foundation. "
            "Only 'screenplay' is supported in this milestone."
        )

    @staticmethod
    def validate_checkpoint_transition(current_status: str, target_status: str) -> bool:
        return validate_checkpoint_transition(current_status, target_status)

    @staticmethod
    def validate_stage_transition(current_status: str, target_status: str) -> bool:
        return validate_stage_transition(current_status, target_status)

    @staticmethod
    def get_story_review_data(db: Session, project_id: str) -> Dict[str, Any]:
        """
        Retrieves complete screenplay review data for human director approval.
        Strictly read-only; performs no state modifications or database writes.
        """
        project = db.query(Project).filter(Project.id == project_id).first()
        if not project or project.deleted_at is not None:
            raise HTTPException(status_code=404, detail="Project not found")

        cp = db.query(PipelineCheckpoint).filter(
            PipelineCheckpoint.project_id == project_id,
            PipelineCheckpoint.checkpoint_type == "story_approval"
        ).first()
        if not cp:
            raise HTTPException(status_code=404, detail="Checkpoint 'story_approval' not found")

        scenes = db.query(Scene).filter(Scene.project_id == project_id).order_by(Scene.order_index).all()
        current_hash = PipelineOrchestrator.compute_artifact_hash(db, project_id, "screenplay")
        is_hash_fresh = bool(
            current_hash and cp.artifact_content_hash and current_hash == cp.artifact_content_hash
        )

        return {
            "project_id": project.id,
            "project_name": project.name,
            "checkpoint_status": cp.status,
            "artifact_version": cp.artifact_version,
            "current_artifact_hash": current_hash,
            "submitted_artifact_hash": cp.artifact_content_hash,
            "is_hash_fresh": is_hash_fresh,
            "scenes": [
                {
                    "scene_number": s.scene_number,
                    "order_index": s.order_index,
                    "slugline": s.slugline or "",
                    "title": s.title or "",
                    "summary": s.summary or "",
                    "script_content": s.script_content or "",
                    "estimated_duration": s.estimated_duration or 0,
                }
                for s in scenes
            ]
        }

    @staticmethod
    def submit_story_for_review(
        db: Session,
        project_id: str,
        reviewer_name: str = "Director / User"
    ) -> Dict[str, Any]:
        """
        Submits canonical screenplay artifact for human director approval.
        Validates non-empty screenplay, computes SHA-256 hash, updates checkpoint and stage
        to WAITING_FOR_APPROVAL, and logs SUBMITTED_FOR_REVIEW in compliance audit.
        Acquires exclusive write lock before reading snapshot to serialize concurrent submissions.
        """
        # 1. Acquire project write lock before reading any snapshots
        project = acquire_project_write_lock(db, project_id)
        if not project or project.deleted_at is not None:
            raise HTTPException(status_code=404, detail="Project not found")

        # 2. Verify pipeline is fully initialized
        stages_count = db.query(PipelineStageState).filter(PipelineStageState.project_id == project_id).count()
        cps_count = db.query(PipelineCheckpoint).filter(PipelineCheckpoint.project_id == project_id).count()
        if stages_count < 11 or cps_count < 4:
            raise HTTPException(
                status_code=400,
                detail="Pipeline has not been initialized for this project."
            )

        # 3. Read checkpoint under lock
        cp_query = db.query(PipelineCheckpoint).filter(
            PipelineCheckpoint.project_id == project_id,
            PipelineCheckpoint.checkpoint_type == "story_approval"
        )
        if db.bind and db.bind.dialect.name == "postgresql":
            cp_query = cp_query.with_for_update()
        cp = cp_query.first()
        if not cp:
            raise HTTPException(status_code=404, detail="Checkpoint 'story_approval' not found")

        # 4. Read scenes under lock and compute canonical hash
        scenes = db.query(Scene).filter(Scene.project_id == project_id).order_by(Scene.order_index).all()
        if not scenes:
            raise HTTPException(status_code=400, detail="Cannot submit empty screenplay for review. Please add scenes first.")

        has_content = any((s.script_content and s.script_content.strip()) or (s.slugline and s.slugline.strip()) for s in scenes)
        if not has_content:
            raise HTTPException(status_code=400, detail="Cannot submit empty screenplay for review. Scenes contain no script content.")

        current_hash = PipelineOrchestrator.compute_artifact_hash(db, project_id, "screenplay")
        if not current_hash:
            raise HTTPException(status_code=400, detail="Cannot compute canonical hash for screenplay.")

        # 5. Idempotency checks
        if cp.status == "WAITING_FOR_APPROVAL" and cp.artifact_content_hash == current_hash:
            return {
                "project_id": project.id,
                "checkpoint_id": cp.id,
                "checkpoint_type": cp.checkpoint_type,
                "status": cp.status,
                "artifact_version": cp.artifact_version,
                "artifact_content_hash": cp.artifact_content_hash,
                "submitted_at": cp.updated_at or cp.created_at,
                "message": "Screenplay is already submitted and waiting for review."
            }

        if cp.status == "APPROVED" and cp.artifact_content_hash == current_hash:
            return {
                "project_id": project.id,
                "checkpoint_id": cp.id,
                "checkpoint_type": cp.checkpoint_type,
                "status": cp.status,
                "artifact_version": cp.artifact_version,
                "artifact_content_hash": cp.artifact_content_hash,
                "submitted_at": cp.decided_at or cp.updated_at,
                "message": "Screenplay is already approved with current content hash."
            }

        # 6. Validate legal transition
        if not validate_checkpoint_transition(cp.status, "WAITING_FOR_APPROVAL"):
            raise HTTPException(
                status_code=400,
                detail=f"Illegal transition: cannot submit checkpoint from status '{cp.status}' to 'WAITING_FOR_APPROVAL'"
            )

        now = utc_now()
        if cp.status == "LOCKED":
            new_version = 1
        elif cp.status == "WAITING_FOR_APPROVAL" and cp.artifact_content_hash and current_hash != cp.artifact_content_hash:
            new_version = cp.artifact_version + 1
        else:
            new_version = max(1, cp.artifact_version)
        cp.status = "WAITING_FOR_APPROVAL"
        cp.artifact_content_hash = current_hash
        cp.artifact_version = new_version
        cp.updated_at = now

        screenplay_stage = db.query(PipelineStageState).filter(
            PipelineStageState.project_id == project_id,
            PipelineStageState.stage_name == "screenplay"
        ).first()
        if screenplay_stage:
            screenplay_stage.status = "WAITING_FOR_APPROVAL"
            screenplay_stage.content_hash = current_hash
            screenplay_stage.updated_at = now
            screenplay_stage.error_message = None

        project.current_pipeline_stage = "screenplay"
        project.pipeline_overall_status = "in_progress"

        audit = PipelineCheckpointAudit(
            project_id=project.id,
            project_name_snapshot=project.name,
            checkpoint_id=cp.id,
            checkpoint_type="story_approval",
            decision="SUBMITTED_FOR_REVIEW",
            artifact_content_hash=current_hash,
            artifact_version=cp.artifact_version,
            reviewer_name=reviewer_name,
            feedback_notes="Screenplay submitted for human review."
        )
        db.add(audit)
        db.commit()
        db.refresh(cp)

        return {
            "project_id": project.id,
            "checkpoint_id": cp.id,
            "checkpoint_type": cp.checkpoint_type,
            "status": cp.status,
            "artifact_version": cp.artifact_version,
            "artifact_content_hash": cp.artifact_content_hash,
            "submitted_at": cp.updated_at,
            "message": "Screenplay submitted successfully for review."
        }

    @staticmethod
    def record_story_decision(
        db: Session,
        project_id: str,
        request: CheckpointDecisionRequest
    ) -> Dict[str, Any]:
        """
        Records human approval, rejection, or changes-requested decision with strict TOCTOU concurrency fencing.
        Validates that expected artifact version and canonical hash match actual database state.
        Raises 409 Conflict if artifact drifted since review was opened, invalidating checkpoint to STALE.
        """
        # 1. Acquire project write lock before reading state
        project = acquire_project_write_lock(db, project_id)
        if not project or project.deleted_at is not None:
            raise HTTPException(status_code=404, detail="Project not found")

        cp_query = db.query(PipelineCheckpoint).filter(
            PipelineCheckpoint.project_id == project_id,
            PipelineCheckpoint.checkpoint_type == "story_approval"
        )
        if db.bind and db.bind.dialect.name == "postgresql":
            cp_query = cp_query.with_for_update()
        cp = cp_query.first()
        if not cp:
            raise HTTPException(status_code=404, detail="Checkpoint 'story_approval' not found")

        # 2. Idempotent re-approval check: returns existing approval only if version and live hash are still current
        if (
            request.decision == "APPROVED"
            and cp.status == "APPROVED"
            and cp.artifact_content_hash == request.expected_artifact_hash
            and cp.artifact_version == request.expected_artifact_version
        ):
            current_hash = PipelineOrchestrator.compute_artifact_hash(db, project_id, "screenplay")
            if current_hash != cp.artifact_content_hash:
                now = utc_now()
                old_hash = cp.artifact_content_hash
                old_version = cp.artifact_version
                cp.status = "STALE"
                cp.artifact_version = old_version + 1
                cp.updated_at = now

                screenplay_stage = db.query(PipelineStageState).filter(
                    PipelineStageState.project_id == project_id,
                    PipelineStageState.stage_name == "screenplay"
                ).first()
                if screenplay_stage:
                    screenplay_stage.status = "STALE"
                    screenplay_stage.active_job_id = None
                    screenplay_stage.updated_at = now

                audit = PipelineCheckpointAudit(
                    project_id=project.id,
                    project_name_snapshot=project.name,
                    checkpoint_id=cp.id,
                    checkpoint_type="story_approval",
                    decision="INVALIDATED",
                    artifact_content_hash=old_hash,
                    artifact_version=old_version,
                    reviewer_name="System / TOCTOU Guard",
                    feedback_notes="Screenplay content drifted. Checkpoint invalidated to STALE."
                )
                db.add(audit)
                db.commit()
                raise HTTPException(
                    status_code=409,
                    detail="Screenplay content has been modified since approval (hash mismatch). Approval state invalidated to STALE."
                )

            return {
                "project_id": project.id,
                "checkpoint_id": cp.id,
                "checkpoint_type": cp.checkpoint_type,
                "status": cp.status,
                "decision": "APPROVED",
                "artifact_version": cp.artifact_version,
                "artifact_content_hash": cp.artifact_content_hash,
                "reviewer_name": cp.reviewer_name,
                "feedback_notes": cp.feedback_notes,
                "decided_at": cp.decided_at or cp.updated_at
            }

        # 3. Checkpoint must be in WAITING_FOR_APPROVAL
        if cp.status != "WAITING_FOR_APPROVAL":
            if cp.status in ("APPROVED", "REJECTED", "STALE"):
                raise HTTPException(
                    status_code=409,
                    detail=f"Checkpoint state was modified concurrently (current status: {cp.status})."
                )
            raise HTTPException(
                status_code=400,
                detail=f"Cannot record decision: checkpoint is in '{cp.status}' state, expected 'WAITING_FOR_APPROVAL'. Please submit for review first."
            )

        # 4. Recompute fresh canonical hash from actual database entities
        current_hash = PipelineOrchestrator.compute_artifact_hash(db, project_id, "screenplay")
        if not current_hash:
            raise HTTPException(status_code=400, detail="Cannot record decision: screenplay artifact is empty.")

        # 5. Optimistic concurrency: verify artifact version matches reviewer view
        if cp.artifact_version != request.expected_artifact_version:
            raise HTTPException(
                status_code=409,
                detail=f"Artifact version mismatch: expected version {request.expected_artifact_version}, but current version is {cp.artifact_version}. Screenplay was updated concurrently."
            )

        # 6. TOCTOU Drift Protection: check if content drifted
        if current_hash != request.expected_artifact_hash or current_hash != cp.artifact_content_hash:
            now = utc_now()
            old_hash = cp.artifact_content_hash or current_hash
            old_version = cp.artifact_version
            cp.status = "STALE"
            cp.artifact_version = old_version + 1
            cp.updated_at = now

            screenplay_stage = db.query(PipelineStageState).filter(
                PipelineStageState.project_id == project_id,
                PipelineStageState.stage_name == "screenplay"
            ).first()
            if screenplay_stage:
                screenplay_stage.status = "STALE"
                screenplay_stage.active_job_id = None
                screenplay_stage.updated_at = now

            audit = PipelineCheckpointAudit(
                project_id=project.id,
                project_name_snapshot=project.name,
                checkpoint_id=cp.id,
                checkpoint_type="story_approval",
                decision="INVALIDATED",
                artifact_content_hash=old_hash,
                artifact_version=old_version,
                reviewer_name="System / TOCTOU Guard",
                feedback_notes="Screenplay content drifted between review inspection and decision. Checkpoint invalidated to STALE."
            )
            db.add(audit)
            db.commit()
            raise HTTPException(
                status_code=409,
                detail="Screenplay content has been modified since review was opened (hash mismatch). Approval state invalidated to STALE. Please review the updated screenplay."
            )

        now = utc_now()
        decision = request.decision
        reviewer = request.reviewer_name
        notes = request.feedback_notes or ""
        target_status = "APPROVED" if decision == "APPROVED" else "REJECTED"

        # 7. Atomic Compare-and-Swap (CAS) update to guard against concurrent decisions
        cas_stmt = (
            update(PipelineCheckpoint)
            .where(
                PipelineCheckpoint.id == cp.id,
                PipelineCheckpoint.status == "WAITING_FOR_APPROVAL",
                PipelineCheckpoint.artifact_version == request.expected_artifact_version
            )
            .values(
                status=target_status,
                artifact_content_hash=current_hash,
                decided_at=now,
                reviewer_name=reviewer,
                feedback_notes=notes,
                updated_at=now
            )
            .execution_options(synchronize_session=False)
        )
        res = db.execute(cas_stmt)
        if res.rowcount == 0:
            db.rollback()
            refreshed = db.query(PipelineCheckpoint).filter(PipelineCheckpoint.id == cp.id).first()
            current_status = refreshed.status if refreshed else "UNKNOWN"
            raise HTTPException(
                status_code=409,
                detail=f"Concurrent modification conflict: checkpoint status is now '{current_status}' or version changed."
            )

        screenplay_stage = db.query(PipelineStageState).filter(
            PipelineStageState.project_id == project_id,
            PipelineStageState.stage_name == "screenplay"
        ).first()

        if decision == "APPROVED":
            if screenplay_stage:
                screenplay_stage.status = "COMPLETED"
                screenplay_stage.content_hash = current_hash
                screenplay_stage.completed_at = now
                screenplay_stage.error_message = None
                screenplay_stage.updated_at = now

            project.current_pipeline_stage = "character_design"
            project.pipeline_overall_status = "in_progress"

        elif decision in ("REJECTED", "REQUEST_CHANGES"):
            if screenplay_stage:
                screenplay_stage.status = "WAITING_FOR_USER_INPUT"
                screenplay_stage.error_message = notes or f"Screenplay {decision.lower().replace('_', ' ')} during review."
                screenplay_stage.updated_at = now

            project.current_pipeline_stage = "screenplay"

        audit = PipelineCheckpointAudit(
            project_id=project.id,
            project_name_snapshot=project.name,
            checkpoint_id=cp.id,
            checkpoint_type="story_approval",
            decision=decision,
            artifact_content_hash=current_hash,
            artifact_version=request.expected_artifact_version,
            reviewer_name=reviewer,
            feedback_notes=notes
        )
        db.add(audit)
        db.commit()
        db.refresh(cp)

        return {
            "project_id": project.id,
            "checkpoint_id": cp.id,
            "checkpoint_type": cp.checkpoint_type,
            "status": cp.status,
            "decision": decision,
            "artifact_version": cp.artifact_version,
            "artifact_content_hash": cp.artifact_content_hash,
            "reviewer_name": cp.reviewer_name,
            "feedback_notes": cp.feedback_notes,
            "decided_at": cp.decided_at
        }

    @staticmethod
    def record_checkpoint_decision(
        db: Session,
        project_id: str,
        checkpoint_type: str,
        decision: str,
        notes: Optional[str] = None,
        reviewer_name: str = "Director / User"
    ) -> PipelineCheckpoint:
        """
        Legacy checkpoint decision recording is disabled and fails closed.
        All checkpoint mutations must go through dedicated milestone lifecycles.
        """
        if checkpoint_type == "story_approval":
            raise HTTPException(
                status_code=400,
                detail="Direct decision on 'story_approval' via legacy record_checkpoint_decision is not permitted. The Story Approval lifecycle (submit_story_for_review and record_story_decision) is the only supported write path."
            )
        raise HTTPException(
            status_code=400,
            detail=f"Checkpoint lifecycle for '{checkpoint_type}' is not implemented. Direct decisions via legacy endpoint are disabled (fail-closed)."
        )

    @staticmethod
    def invalidate_downstream_stages(
        db: Session,
        project_id: str,
        changed_stage: str,
        commit: bool = True
    ) -> None:
        """
        Invalidates dependent approvals and downstream stages when upstream artifacts change.
        Does not commit when commit=False, allowing callers to atomically bundle invalidation
        with entity mutations in a single transaction.
        """
        project = acquire_project_write_lock(db, project_id)
        if not project or project.deleted_at is not None:
            return

        now = utc_now()

        if changed_stage == "screenplay":
            # Invalidate story_approval checkpoint
            cp = db.query(PipelineCheckpoint).filter(
                PipelineCheckpoint.project_id == project_id,
                PipelineCheckpoint.checkpoint_type == "story_approval"
            ).first()

            if cp and cp.status in ("APPROVED", "WAITING_FOR_APPROVAL"):
                old_hash = cp.artifact_content_hash or "none"
                old_version = cp.artifact_version
                cp.status = "STALE"
                cp.artifact_version = old_version + 1
                cp.updated_at = now

                # Audit invalidation with old hash and old version
                audit = PipelineCheckpointAudit(
                    project_id=project.id,
                    project_name_snapshot=project.name,
                    checkpoint_id=cp.id,
                    checkpoint_type=cp.checkpoint_type,
                    decision="INVALIDATED",
                    artifact_content_hash=old_hash,
                    artifact_version=old_version,
                    reviewer_name="System / Invalidator",
                    feedback_notes="Screenplay modified upstream. Approval invalidated."
                )
                db.add(audit)
            elif cp and cp.status == "REJECTED":
                cp.status = "STALE"
                cp.artifact_version += 1
                cp.updated_at = now

            # Invalidate screenplay stage if completed or waiting
            screenplay_stage = db.query(PipelineStageState).filter(
                PipelineStageState.project_id == project_id,
                PipelineStageState.stage_name == "screenplay"
            ).first()
            if screenplay_stage and screenplay_stage.status in ("COMPLETED", "WAITING_FOR_APPROVAL"):
                screenplay_stage.status = "STALE"
                screenplay_stage.active_job_id = None
                screenplay_stage.updated_at = now

            # Invalidate downstream stages
            downstream_names = [
                "character_design", "location_design", "storyboard",
                "cinematic_prompts", "flow_handoff", "media_import",
                "film_editing", "audio_mixing", "final_rendering"
            ]
            stages = db.query(PipelineStageState).filter(
                PipelineStageState.project_id == project_id,
                PipelineStageState.stage_name.in_(downstream_names)
            ).all()

            for s in stages:
                if s.status in ("COMPLETED", "IN_PROGRESS", "WAITING_FOR_APPROVAL", "WAITING_FOR_EXTERNAL_MEDIA"):
                    s.status = "STALE"
                    s.active_job_id = None  # Disassociate active job so stale worker execution cannot complete
                    s.updated_at = now

            # Invalidate downstream checkpoints
            downstream_cps = db.query(PipelineCheckpoint).filter(
                PipelineCheckpoint.project_id == project_id,
                PipelineCheckpoint.checkpoint_type.in_(["visual_approval", "production_approval", "final_review"])
            ).all()
            for dcp in downstream_cps:
                if dcp.status != "LOCKED":
                    dcp.status = "LOCKED"
                    dcp.updated_at = now

            project.current_pipeline_stage = "screenplay"
            if commit:
                db.commit()
            else:
                db.flush()


    @staticmethod
    def complete_pipeline_stage(
        db: Session,
        project_id: str,
        stage_name: str,
        active_job_id: str,
        worker_id: str,
        fencing_token: int,
        result_hash: Optional[str] = None,
        consumed_input_hash: Optional[str] = None,
        stage_metadata: Optional[str] = None
    ) -> PipelineStageState:
        """
        Atomically completes a pipeline stage verified by worker fencing token within a single database transaction.
        Guarantees that stale workers, expired leases, or mismatched active jobs cannot update stage or job status.
        Rolls back both StudioJob and PipelineStageState updates if any validation fails.
        """
        if not active_job_id:
            raise JobOwnershipLostError("active_job_id is required to complete pipeline stage")
        if not worker_id:
            raise JobOwnershipLostError("worker_id is required to complete pipeline stage")
        if fencing_token is None:
            raise JobOwnershipLostError("fencing_token is required to complete pipeline stage")

        project = acquire_project_write_lock(db, project_id)
        if not project or project.deleted_at is not None:
            raise JobOwnershipLostError(f"Project '{project_id}' not found")

        now = utc_now()

        stage = db.query(PipelineStageState).filter(
            PipelineStageState.project_id == project_id,
            PipelineStageState.stage_name == stage_name
        ).first()

        if not stage:
            raise JobOwnershipLostError(f"Pipeline stage '{stage_name}' for project '{project_id}' not found")

        # Worker input snapshot freshness check
        if stage_name in ("character_design", "location_design", "storyboard"):
            if not consumed_input_hash:
                raise JobOwnershipLostError(
                    f"consumed_input_hash is mandatory for stage '{stage_name}' (fail-closed)."
                )
            current_screenplay_hash = PipelineOrchestrator.compute_artifact_hash(db, project_id, "screenplay")
            if not current_screenplay_hash:
                raise JobOwnershipLostError(
                    f"Cannot complete stage '{stage_name}': upstream screenplay artifact has no content hash (fail-closed)."
                )
            if consumed_input_hash != current_screenplay_hash:
                raise JobOwnershipLostError(
                    f"Worker consumed an obsolete input snapshot (input hash '{consumed_input_hash}' != current '{current_screenplay_hash}'). Execution rejected."
                )
            if stage.consumed_input_hash and stage.consumed_input_hash != consumed_input_hash:
                raise JobOwnershipLostError(
                    f"Stage input snapshot drifted (stage hash '{stage.consumed_input_hash}' != current '{consumed_input_hash}'). Execution rejected."
                )

        # Do not allow late completion to move a STALE stage back to COMPLETED
        if stage.status == "STALE":
            raise JobOwnershipLostError(
                f"Pipeline stage '{stage_name}' is STALE due to upstream invalidation. Stale worker execution cannot complete a stale stage."
            )

        if stage.active_job_id != active_job_id:
            raise JobOwnershipLostError(
                f"Pipeline stage '{stage_name}' active job '{stage.active_job_id}' does not match worker job '{active_job_id}'"
            )

        if not validate_stage_transition(stage.status, "COMPLETED"):
            raise JobOwnershipLostError(
                f"Illegal stage transition: cannot move stage '{stage_name}' from '{stage.status}' to 'COMPLETED'."
            )

        try:
            # 1. Complete underlying StudioJob with commit=False in the active transaction
            job_manager.complete_job(
                db=db,
                job_id=active_job_id,
                result={"status": "completed", "content_hash": result_hash},
                worker_id=worker_id,
                fencing_token=fencing_token,
                commit=False
            )

            # 2. Conditional atomic update on PipelineStageState (must not be STALE)
            stmt = (
                update(PipelineStageState)
                .where(PipelineStageState.project_id == project_id)
                .where(PipelineStageState.stage_name == stage_name)
                .where(PipelineStageState.active_job_id == active_job_id)
                .where(PipelineStageState.status != "STALE")
                .values(
                    status="COMPLETED",
                    content_hash=result_hash,
                    consumed_input_hash=consumed_input_hash or stage.consumed_input_hash,
                    stage_metadata=stage_metadata,
                    completed_at=now,
                    updated_at=now
                )
                .execution_options(synchronize_session=False)
            )
            res = db.execute(stmt)
            if res.rowcount == 0:
                raise JobOwnershipLostError(
                    f"Pipeline stage '{stage_name}' for project '{project_id}' is no longer associated with active job '{active_job_id}' or is STALE"
                )

            # 3. Update derived projections
            update_project_pipeline_projections(db, project_id)

            db.commit()
        except Exception:
            db.rollback()
            raise

        stage = db.query(PipelineStageState).filter(
            PipelineStageState.project_id == project_id,
            PipelineStageState.stage_name == stage_name
        ).first()
        return stage

    @staticmethod
    def fail_pipeline_stage(
        db: Session,
        project_id: str,
        stage_name: str,
        active_job_id: str,
        error: Any,
        worker_id: str,
        fencing_token: int
    ) -> PipelineStageState:
        """
        Atomically fails a pipeline stage verified by worker fencing token within a single database transaction.
        Guarantees that stale workers, expired leases, or mismatched active jobs cannot update stage or job status.
        Rolls back both StudioJob and PipelineStageState updates if any validation fails.
        """
        if not active_job_id:
            raise JobOwnershipLostError("active_job_id is required to fail pipeline stage")
        if not worker_id:
            raise JobOwnershipLostError("worker_id is required to fail pipeline stage")
        if fencing_token is None:
            raise JobOwnershipLostError("fencing_token is required to fail pipeline stage")

        project = acquire_project_write_lock(db, project_id)
        if not project or project.deleted_at is not None:
            raise JobOwnershipLostError(f"Project '{project_id}' not found")

        now = utc_now()
        err_msg = str(error)

        stage = db.query(PipelineStageState).filter(
            PipelineStageState.project_id == project_id,
            PipelineStageState.stage_name == stage_name
        ).first()

        if not stage:
            raise JobOwnershipLostError(f"Pipeline stage '{stage_name}' for project '{project_id}' not found")

        # Do not allow late failure to move a STALE stage to FAILED
        if stage.status == "STALE":
            raise JobOwnershipLostError(
                f"Pipeline stage '{stage_name}' is STALE due to upstream invalidation. Stale worker execution cannot fail a stale stage."
            )

        if stage.active_job_id != active_job_id:
            raise JobOwnershipLostError(
                f"Pipeline stage '{stage_name}' active job '{stage.active_job_id}' does not match worker job '{active_job_id}'"
            )

        if not validate_stage_transition(stage.status, "FAILED"):
            raise JobOwnershipLostError(
                f"Illegal stage transition: cannot move stage '{stage_name}' from '{stage.status}' to 'FAILED'."
            )

        try:
            # 1. Fail underlying StudioJob with commit=False in the active transaction
            job_manager.fail_job(
                db=db,
                job_id=active_job_id,
                error=error,
                worker_id=worker_id,
                fencing_token=fencing_token,
                commit=False
            )

            # 2. Conditional atomic update on PipelineStageState
            stmt = (
                update(PipelineStageState)
                .where(PipelineStageState.project_id == project_id)
                .where(PipelineStageState.stage_name == stage_name)
                .where(PipelineStageState.active_job_id == active_job_id)
                .where(PipelineStageState.status != "STALE")
                .where(PipelineStageState.status.in_(["IN_PROGRESS", "WAITING_FOR_APPROVAL", "WAITING_FOR_EXTERNAL_MEDIA"]))
                .values(
                    status="FAILED",
                    error_message=err_msg,
                    updated_at=now
                )
                .execution_options(synchronize_session=False)
            )
            res = db.execute(stmt)
            if res.rowcount == 0:
                raise JobOwnershipLostError(
                    f"Pipeline stage '{stage_name}' for project '{project_id}' is no longer associated with active job '{active_job_id}' or is STALE"
                )

            # 3. Update derived projections
            update_project_pipeline_projections(db, project_id)

            db.commit()
        except Exception:
            db.rollback()
            raise

        stage = db.query(PipelineStageState).filter(
            PipelineStageState.project_id == project_id,
            PipelineStageState.stage_name == stage_name
        ).first()
        return stage


pipeline_orchestrator = PipelineOrchestrator()
