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
"""

import json
import hashlib
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional, Tuple, Set
from sqlalchemy import update
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


def is_pipeline_complete(db: Session, project_id: str) -> bool:
    """
    Checks if all 11 discrete stages and all 4 checkpoints exist for the project.
    Verifies exact stage names and checkpoint types, not just row count.
    """
    stage_names = {
        name for (name,) in db.query(PipelineStageState.stage_name)
        .filter(PipelineStageState.project_id == project_id).all()
    }
    if not set(PIPELINE_STAGES).issubset(stage_names):
        return False

    cp_types = {
        cp_type for (cp_type,) in db.query(PipelineCheckpoint.checkpoint_type)
        .filter(PipelineCheckpoint.project_id == project_id).all()
    }
    expected_cps = {cp[0] for cp in PIPELINE_CHECKPOINTS}
    if not expected_cps.issubset(cp_types):
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
        Self-healing: initializes any missing stage or checkpoint records for legacy projects.
        """
        project = db.query(Project).filter(Project.id == project_id).first()
        if not project or project.deleted_at is not None:
            raise HTTPException(status_code=404, detail="Project not found")

        # Self-healing for legacy or partially initialized projects
        if not is_pipeline_complete(db, project_id):
            initialize_project_pipeline(project_id, db, auto_commit=True)
            db.refresh(project)

        stages = db.query(PipelineStageState).filter(PipelineStageState.project_id == project_id).all()
        checkpoints = db.query(PipelineCheckpoint).filter(PipelineCheckpoint.project_id == project_id).all()

        stage_order = {name: idx for idx, name in enumerate(PIPELINE_STAGES)}
        sorted_stages = sorted(stages, key=lambda s: stage_order.get(s.stage_name, 99))

        return {
            "project_id": project.id,
            "project_name": project.name,
            "current_pipeline_stage": project.current_pipeline_stage,
            "pipeline_overall_status": project.pipeline_overall_status,
            "stages": [
                {
                    "id": s.id,
                    "stage_name": s.stage_name,
                    "status": s.status,
                    "active_job_id": s.active_job_id,
                    "retry_count": s.retry_count,
                    "content_hash": s.content_hash,
                    "consumed_input_hash": s.consumed_input_hash,
                    "error_message": s.error_message,
                    "stage_metadata": s.stage_metadata,
                    "started_at": s.started_at.isoformat() if s.started_at else None,
                    "completed_at": s.completed_at.isoformat() if s.completed_at else None,
                }
                for s in sorted_stages
            ],
            "checkpoints": [
                {
                    "id": cp.id,
                    "checkpoint_type": cp.checkpoint_type,
                    "status": cp.status,
                    "artifact_type": cp.artifact_type,
                    "artifact_version": cp.artifact_version,
                    "artifact_content_hash": cp.artifact_content_hash,
                    "reviewer_name": cp.reviewer_name,
                    "feedback_notes": cp.feedback_notes,
                    "decided_at": cp.decided_at.isoformat() if cp.decided_at else None,
                }
                for cp in checkpoints
            ]
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
    def record_checkpoint_decision(
        db: Session,
        project_id: str,
        checkpoint_type: str,
        decision: str,
        notes: Optional[str] = None,
        reviewer_name: str = "Director / User"
    ) -> PipelineCheckpoint:
        """
        Records human approval or rejection bound to current artifact content hash.
        Creates an immutable audit record in pipeline_checkpoint_audit.
        """
        if decision not in ("APPROVED", "REJECTED"):
            raise HTTPException(status_code=400, detail="Decision must be 'APPROVED' or 'REJECTED'")

        project = db.query(Project).filter(Project.id == project_id).first()
        if not project or project.deleted_at is not None:
            raise HTTPException(status_code=404, detail="Project not found")

        cp = db.query(PipelineCheckpoint).filter(
            PipelineCheckpoint.project_id == project_id,
            PipelineCheckpoint.checkpoint_type == checkpoint_type
        ).first()

        if not cp:
            raise HTTPException(status_code=404, detail=f"Checkpoint '{checkpoint_type}' not found")

        current_hash = PipelineOrchestrator.compute_artifact_hash(db, project_id, cp.artifact_type)
        if not current_hash:
            raise HTTPException(status_code=400, detail="Cannot approve checkpoint: artifact outputs have not been generated.")

        # Idempotent re-approval with identical hash
        if decision == "APPROVED" and cp.status == "APPROVED" and cp.artifact_content_hash == current_hash:
            return cp

        now = utc_now()
        cp.status = decision
        cp.artifact_content_hash = current_hash
        cp.decided_at = now
        cp.reviewer_name = reviewer_name
        cp.feedback_notes = notes or ""
        cp.updated_at = now

        # Append-only immutable audit record
        audit = PipelineCheckpointAudit(
            project_id=project.id,
            project_name_snapshot=project.name,
            checkpoint_id=cp.id,
            checkpoint_type=checkpoint_type,
            decision=decision,
            artifact_content_hash=current_hash,
            artifact_version=cp.artifact_version,
            reviewer_name=reviewer_name,
            feedback_notes=notes or ""
        )
        db.add(audit)

        # Stage state transitions based on checkpoint type
        if checkpoint_type == "story_approval":
            screenplay_stage = db.query(PipelineStageState).filter(
                PipelineStageState.project_id == project_id,
                PipelineStageState.stage_name == "screenplay"
            ).first()

            if decision == "APPROVED":
                if screenplay_stage:
                    screenplay_stage.status = "COMPLETED"
                    screenplay_stage.content_hash = current_hash
                    screenplay_stage.completed_at = now
                project.current_pipeline_stage = "character_design"
                project.pipeline_overall_status = "in_progress"
            else:  # REJECTED
                if screenplay_stage:
                    screenplay_stage.status = "WAITING_FOR_USER_INPUT"
                    screenplay_stage.error_message = notes or "Screenplay rejected during review."
                project.current_pipeline_stage = "screenplay"

        db.commit()
        db.refresh(cp)
        return cp

    @staticmethod
    def invalidate_downstream_stages(db: Session, project_id: str, changed_stage: str) -> None:
        """
        Invalidates dependent approvals and downstream stages when upstream artifacts change.
        """
        project = db.query(Project).filter(Project.id == project_id).first()
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
                cp.status = "STALE"
                cp.artifact_version += 1
                cp.updated_at = now

                # Audit invalidation
                audit = PipelineCheckpointAudit(
                    project_id=project.id,
                    project_name_snapshot=project.name,
                    checkpoint_id=cp.id,
                    checkpoint_type=cp.checkpoint_type,
                    decision="INVALIDATED",
                    artifact_content_hash=cp.artifact_content_hash or "none",
                    artifact_version=cp.artifact_version,
                    reviewer_name="System / Invalidator",
                    feedback_notes="Screenplay modified upstream. Approval invalidated."
                )
                db.add(audit)

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
            db.commit()

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

        now = utc_now()
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

            # 2. Conditional atomic update on PipelineStageState
            stmt = (
                update(PipelineStageState)
                .where(PipelineStageState.project_id == project_id)
                .where(PipelineStageState.stage_name == stage_name)
                .where(PipelineStageState.active_job_id == active_job_id)
                .values(
                    status="COMPLETED",
                    content_hash=result_hash,
                    consumed_input_hash=consumed_input_hash,
                    stage_metadata=stage_metadata,
                    completed_at=now,
                    updated_at=now
                )
                .execution_options(synchronize_session=False)
            )
            res = db.execute(stmt)
            if res.rowcount == 0:
                raise JobOwnershipLostError(
                    f"Pipeline stage '{stage_name}' for project '{project_id}' is no longer associated with active job '{active_job_id}'"
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

        now = utc_now()
        err_msg = str(error)

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
                    f"Pipeline stage '{stage_name}' for project '{project_id}' is no longer associated with active job '{active_job_id}'"
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
