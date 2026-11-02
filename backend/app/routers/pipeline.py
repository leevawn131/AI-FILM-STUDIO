"""
Pipeline Read API Router.
Provides read-only access to project pipeline states, stages, approval checkpoints,
and immutable compliance audit journals.
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
    PipelineAuditHistoryResponse
)

router = APIRouter(prefix="/api/pipeline", tags=["pipeline"])


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
