"""
Server-side Gatekeeper Service.
Enforces pipeline approval checkpoints before permitting downstream artifact generation
or downstream production execution.

Gatekeeper operates fail-closed and strictly read-only:
No state mutations, no audit creation, and no commits occur during validation.

NOTE: Single-Tenant Authorization Limitation Notice:
Currently, the studio application operates in a single-tenant environment without multi-user
RBAC or per-user permission partitions. Gatekeeper enforces data integrity and stage progression
gates across the project lifecycle, but user authentication/tenant isolation is managed at the
infrastructure/network boundary.
"""

from typing import Optional, Any
from fastapi import HTTPException, Depends
from sqlalchemy.orm import Session

from backend.app.database import get_db
from backend.app.models.entities import Project, Scene, PipelineCheckpoint
from backend.app.services.pipeline_orchestrator import pipeline_orchestrator, PipelineOrchestrator


class GatekeeperService:
    """
    Central server-side gatekeeper service for validating pipeline approval checkpoints.
    """

    SUPPORTED_CHECKPOINTS = {"story_approval"}

    def verify_checkpoint(
        self,
        db: Session,
        project_id: str,
        checkpoint_type: str = "story_approval"
    ) -> PipelineCheckpoint:
        """
        Validates that a project has an active, valid approval for the requested checkpoint type.
        Operates strictly read-only.
        
        Fail-closed rules:
        - Project must exist and not be soft-deleted (404).
        - Checkpoint must exist (404).
        - Checkpoint type must have an implemented canonical artifact hash (403).
        - Checkpoint status must be APPROVED (403, or 409 if STALE).
        - Approved artifact hash must exist and be 64-char hex (403).
        - Current artifact hash computed from DB must match approved hash exactly (409 if changed).
        """
        if not project_id:
            raise HTTPException(status_code=400, detail="project_id is required")

        # 1. Verify project exists and is not soft-deleted
        project = db.query(Project).filter(Project.id == project_id).first()
        if not project or project.deleted_at is not None:
            raise HTTPException(status_code=404, detail="Project not found")

        # 2. Verify checkpoint exists
        cp = db.query(PipelineCheckpoint).filter(
            PipelineCheckpoint.project_id == project_id,
            PipelineCheckpoint.checkpoint_type == checkpoint_type
        ).first()
        if not cp:
            raise HTTPException(
                status_code=404,
                detail=f"Checkpoint '{checkpoint_type}' not found for project"
            )

        # 3. Fail-closed on unsupported checkpoints
        if checkpoint_type not in self.SUPPORTED_CHECKPOINTS:
            raise HTTPException(
                status_code=403,
                detail=(
                    f"Gatekeeper enforcement for checkpoint '{checkpoint_type}' is unavailable "
                    "(canonical artifact hashing not implemented). Access denied (fail-closed)."
                )
            )

        # 4. Check checkpoint status
        if cp.status == "STALE":
            raise HTTPException(
                status_code=409,
                detail="Checkpoint 'story_approval' is STALE: upstream screenplay was modified after approval. Re-approval required."
            )
        elif cp.status != "APPROVED":
            raise HTTPException(
                status_code=403,
                detail=f"Checkpoint '{checkpoint_type}' is in state '{cp.status}'. Approval is required before proceeding."
            )

        # 5. Check approved artifact hash exists
        if not cp.artifact_content_hash:
            raise HTTPException(
                status_code=403,
                detail=f"Checkpoint '{checkpoint_type}' has status APPROVED but no recorded artifact content hash."
            )

        # 6. Compute current canonical hash from actual database entities
        try:
            current_hash = PipelineOrchestrator.compute_artifact_hash(db, project_id, cp.artifact_type)
        except NotImplementedError:
            raise HTTPException(
                status_code=403,
                detail=f"Canonical artifact hashing for '{cp.artifact_type}' is not implemented. Access denied."
            )

        if not current_hash:
            raise HTTPException(
                status_code=403,
                detail=f"Current artifact output for '{cp.artifact_type}' is empty or missing."
            )

        # 7. Exact hash match
        if current_hash != cp.artifact_content_hash:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Screenplay content has changed since approval (hash mismatch). "
                    "Approval is no longer valid. Re-approval required."
                )
            )

        return cp

    def verify_scene_approval(
        self,
        db: Session,
        scene_id: str,
        expected_project_id: Optional[str] = None
    ) -> Scene:
        """
        Validates scene existence, ownership consistency, and verifies that story_approval
        is APPROVED with exact canonical hash match before permitting shot creation or generation.
        """
        if not scene_id:
            raise HTTPException(status_code=400, detail="scene_id is required")

        scene = db.query(Scene).filter(Scene.id == scene_id).first()
        if not scene:
            raise HTTPException(status_code=404, detail="Scene not found")

        if expected_project_id and scene.project_id != expected_project_id:
            raise HTTPException(
                status_code=400,
                detail=f"Scene '{scene_id}' belongs to project '{scene.project_id}', not '{expected_project_id}'"
            )

        self.verify_checkpoint(db=db, project_id=scene.project_id, checkpoint_type="story_approval")
        return scene


gatekeeper = GatekeeperService()


def require_checkpoint(checkpoint_type: str):
    """
    Factory creating a FastAPI dependency to enforce a specific pipeline checkpoint.
    Expects 'project_id' in query or path params.
    """
    def _dependency(
        project_id: Optional[str] = None,
        db: Session = Depends(get_db)
    ) -> PipelineCheckpoint:
        if not project_id:
            raise HTTPException(status_code=400, detail="project_id is required to verify checkpoint")
        return gatekeeper.verify_checkpoint(db=db, project_id=project_id, checkpoint_type=checkpoint_type)

    return _dependency
