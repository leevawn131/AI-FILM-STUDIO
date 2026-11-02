"""
Pipeline Orchestrator Service.
Choreographs the end-to-end filmmaking production pipeline across 11 discrete stages
and 4 human approval checkpoints.
"""

import json
import hashlib
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional, Tuple
from sqlalchemy import update
from sqlalchemy.orm import Session
from fastapi import HTTPException

from backend.app.models.entities import (
    Project, Scene, Shot, Character, Location,
    PipelineStageState, PipelineCheckpoint, PipelineCheckpointAudit,
    StudioJob
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


def initialize_project_pipeline(project_id: Any, db: Any = None, auto_commit: bool = True) -> None:
    """
    Idempotently initializes all 11 production stages and 4 approval checkpoints for a project.
    Safe for both new project creation and existing legacy projects.
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

    project = session.query(Project).filter(Project.id == pid).first()
    if not project or project.deleted_at is not None:
        raise ValueError(f"Project '{pid}' not found or has been soft-deleted")

    try:
        # 1. Initialize 11 Stage States in order
        existing_stages = {
            s.stage_name: s
            for s in session.query(PipelineStageState).filter(PipelineStageState.project_id == pid).all()
        }

        for stage_name in PIPELINE_STAGES:
            if stage_name not in existing_stages:
                stage = PipelineStageState(
                    project_id=pid,
                    stage_name=stage_name,
                    status="NOT_STARTED"
                )
                session.add(stage)

        # 2. Initialize 4 Checkpoints
        existing_cps = {
            cp.checkpoint_type: cp
            for cp in session.query(PipelineCheckpoint).filter(PipelineCheckpoint.project_id == pid).all()
        }

        for cp_type, artifact_type in PIPELINE_CHECKPOINTS:
            if cp_type not in existing_cps:
                checkpoint = PipelineCheckpoint(
                    project_id=pid,
                    checkpoint_type=cp_type,
                    status="LOCKED",
                    artifact_type=artifact_type,
                    artifact_version=1,
                    artifact_content_hash=None
                )
                session.add(checkpoint)

        # 3. Synchronize project projections if default
        if not project.current_pipeline_stage:
            project.current_pipeline_stage = "story_development"
        if not project.pipeline_overall_status:
            project.pipeline_overall_status = "in_progress"

        session.flush()
        if auto_commit:
            session.commit()
    except Exception as e:
        from sqlalchemy.exc import IntegrityError
        if isinstance(e, IntegrityError):
            session.rollback()
            # Another concurrent worker initialized stages; confirm existence
            stage_cnt = session.query(PipelineStageState).filter(PipelineStageState.project_id == pid).count()
            cp_cnt = session.query(PipelineCheckpoint).filter(PipelineCheckpoint.project_id == pid).count()
            if stage_cnt >= len(PIPELINE_STAGES) and cp_cnt >= len(PIPELINE_CHECKPOINTS):
                return
        raise


class PipelineOrchestrator:
    @staticmethod
    def initialize_project_pipeline(project_id: Any, db: Any = None, auto_commit: bool = True) -> None:
        return initialize_project_pipeline(project_id, db, auto_commit=auto_commit)

    @staticmethod
    def get_pipeline_status(db: Session, project_id: str) -> Dict[str, Any]:
        """
        Retrieves complete pipeline status, active stage, and checkpoints.
        Self-healing: initializes pipeline records if missing.
        """
        project = db.query(Project).filter(Project.id == project_id).first()
        if not project or project.deleted_at is not None:
            raise HTTPException(status_code=404, detail="Project not found")

        # Self-healing for existing legacy projects
        stage_count = db.query(PipelineStageState).filter(PipelineStageState.project_id == project_id).count()
        if stage_count < len(PIPELINE_STAGES):
            PipelineOrchestrator.initialize_project_pipeline(db, project_id)

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

        return None

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
            else: # REJECTED
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
        Atomically completes a pipeline stage verified by worker fencing token.
        Guarantees that stale workers cannot update stage status.
        """
        now = utc_now()

        # 1. Complete underlying StudioJob with mandatory fencing check
        job_manager.complete_job(
            db=db,
            job_id=active_job_id,
            result={"status": "completed", "content_hash": result_hash},
            worker_id=worker_id,
            fencing_token=fencing_token
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
        )
        res = db.execute(stmt)
        if res.rowcount == 0:
            db.rollback()
            raise JobOwnershipLostError("Pipeline stage is no longer owned by the specified active job")

        db.commit()

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
        Fails a pipeline stage verified by worker fencing token.
        """
        now = utc_now()
        err_msg = str(error)

        job_manager.fail_job(
            db=db,
            job_id=active_job_id,
            error=error,
            worker_id=worker_id,
            fencing_token=fencing_token
        )

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
        )
        db.execute(stmt)
        db.commit()

        stage = db.query(PipelineStageState).filter(
            PipelineStageState.project_id == project_id,
            PipelineStageState.stage_name == stage_name
        ).first()
        return stage


pipeline_orchestrator = PipelineOrchestrator()
