"""
Pipeline Read API Router.
Provides read-only access to project pipeline states, stages, approval checkpoints,
and immutable compliance audit journals.

SECURITY & AUTHORIZATION NOTICE:
Pipeline endpoints currently use project ID scoping but do not enforce user-level project ownership.
Cross-project query filtering is not equivalent to user authorization.
This must be addressed when the application introduces multi-user access or exposes the API
beyond its trusted deployment boundary.
"""

from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from backend.app.database import get_db
from backend.app.services.pipeline_orchestrator import pipeline_orchestrator
from backend.app.schemas.pipeline import (
    PipelineStatusResponse,
    PipelineStageDetailResponse,
    PipelineCheckpointDetailResponse,
    PipelineAuditHistoryResponse,
    StoryReviewResponse,
    StorySubmitResponse,
    CheckpointDecisionRequest,
    CheckpointDecisionResponse,
)

router = APIRouter(prefix="/api/pipeline", tags=["pipeline"])


# ---------------------------------------------------------------------------
# S4 M2.2 Story Approval Endpoints
# ---------------------------------------------------------------------------

@router.get(
    "/{project_id}/checkpoints/story_approval/review",
    response_model=StoryReviewResponse,
    summary="Retrieve screenplay data for approval review",
    description="Returns ordered scenes, canonical content hash, current approval status, and token metadata for human review."
)
def get_story_review(
    project_id: str,
    db: Session = Depends(get_db)
):
    return pipeline_orchestrator.get_story_review_data(db, project_id)


@router.post(
    "/{project_id}/checkpoints/story_approval/submit",
    response_model=StorySubmitResponse,
    summary="Submit screenplay for human director approval",
    description="Transitions screenplay stage and checkpoint to WAITING_FOR_APPROVAL and records compliance audit."
)
def submit_story_for_review(
    project_id: str,
    db: Session = Depends(get_db)
):
    return pipeline_orchestrator.submit_story_for_review(db, project_id)


@router.post(
    "/{project_id}/checkpoints/story_approval/decision",
    response_model=CheckpointDecisionResponse,
    summary="Record human approval or rejection decision",
    description="Applies director decision (APPROVED, REJECTED, REQUEST_CHANGES) with optimistic concurrency checks."
)
def decide_story_approval(
    project_id: str,
    request: CheckpointDecisionRequest,
    db: Session = Depends(get_db)
):
    return pipeline_orchestrator.record_story_decision(db, project_id, request)



@router.get(
    "/{project_id}",
    response_model=PipelineStatusResponse,
    summary="Retrieve complete pipeline state for a project",
    description="Returns overall pipeline status, active stage, all 11 stages in canonical order, and all 4 checkpoints."
)
def get_pipeline_state(
    project_id: str,
    db: Session = Depends(get_db)
):
    return pipeline_orchestrator.get_pipeline_status(db, project_id)


@router.get(
    "/{project_id}/stages/{stage_name}",
    response_model=PipelineStageDetailResponse,
    summary="Retrieve individual pipeline stage details",
    description="Returns detailed stage state, content hashes, stage metadata, active background job, and associated checkpoint."
)
def get_stage_detail(
    project_id: str,
    stage_name: str,
    db: Session = Depends(get_db)
):
    return pipeline_orchestrator.get_stage_detail(db, project_id, stage_name)


@router.get(
    "/{project_id}/checkpoints/{checkpoint_type}",
    response_model=PipelineCheckpointDetailResponse,
    summary="Retrieve approval checkpoint details",
    description="Returns checkpoint status, bound artifact version/hash, fresh hash comparison, reviewer info, and latest audit decision."
)
def get_checkpoint_detail(
    project_id: str,
    checkpoint_type: str,
    db: Session = Depends(get_db)
):
    return pipeline_orchestrator.get_checkpoint_detail(db, project_id, checkpoint_type)


@router.get(
    "/{project_id}/checkpoints/{checkpoint_type}/audit",
    response_model=PipelineAuditHistoryResponse,
    summary="Retrieve audit history for a checkpoint",
    description="Returns chronological, paginated audit entries for decisions, rejections, and invalidations on a specific checkpoint."
)
def get_checkpoint_audit_history(
    project_id: str,
    checkpoint_type: str,
    limit: int = Query(50, ge=1, le=100, description="Maximum records to return (1-100)"),
    offset: int = Query(0, ge=0, description="Offset index for pagination"),
    db: Session = Depends(get_db)
):
    return pipeline_orchestrator.get_checkpoint_audit_history(
        db=db,
        project_id=project_id,
        checkpoint_type=checkpoint_type,
        limit=limit,
        offset=offset
    )


@router.get(
    "/{project_id}/audit",
    response_model=PipelineAuditHistoryResponse,
    summary="Retrieve project-wide pipeline audit history",
    description="Returns chronological, paginated audit entries across all checkpoints for a given project."
)
def get_project_audit_history(
    project_id: str,
    checkpoint_type: Optional[str] = Query(None, description="Optional filter by checkpoint type"),
    limit: int = Query(50, ge=1, le=100, description="Maximum records to return (1-100)"),
    offset: int = Query(0, ge=0, description="Offset index for pagination"),
    db: Session = Depends(get_db)
):
    return pipeline_orchestrator.get_project_audit_history(
        db=db,
        project_id=project_id,
        checkpoint_type=checkpoint_type,
        limit=limit,
        offset=offset
    )
