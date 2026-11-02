from typing import Optional, List, Dict, Any
from datetime import datetime
import json
from pydantic import BaseModel, Field, ConfigDict, field_validator


class JobSummaryResponse(BaseModel):
    """Safe public projection of a StudioJob without sensitive worker details."""
    id: str
    job_type: str
    status: str
    error_message: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class PipelineStageResponse(BaseModel):
    """Summary of a single discrete pipeline stage."""
    id: str
    project_id: str
    stage_name: str
    status: str
    order_index: int
    active_job_id: Optional[str] = None
    retry_count: int = 0
    content_hash: Optional[str] = None
    consumed_input_hash: Optional[str] = None
    error_message: Optional[str] = None
    stage_metadata: Optional[Dict[str, Any]] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime

    @field_validator("stage_metadata", mode="before")
    @classmethod
    def parse_stage_metadata(cls, v: Any) -> Optional[Dict[str, Any]]:
        if v is None:
            return None
        if isinstance(v, dict):
            return v
        if isinstance(v, str):
            try:
                return json.loads(v)
            except Exception:
                return {"raw": v}
        return None

    model_config = ConfigDict(from_attributes=True)


class PipelineCheckpointResponse(BaseModel):
    """Summary of a human approval checkpoint."""
    id: str
    project_id: str
    checkpoint_type: str
    status: str
    artifact_type: str
    artifact_version: int = 1
    artifact_content_hash: Optional[str] = None
    reviewer_name: str = "Director / User"
    feedback_notes: Optional[str] = None
    decided_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class PipelineCheckpointAuditItem(BaseModel):
    """Single immutable compliance audit entry."""
    id: str
    project_id: str
    project_name_snapshot: str
    checkpoint_id: str
    checkpoint_type: str
    decision: str
    artifact_content_hash: str
    artifact_version: int
    reviewer_name: str
    feedback_notes: Optional[str] = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class PipelineStageDetailResponse(PipelineStageResponse):
    """Detailed view of a pipeline stage with job details and associated checkpoint."""
    active_job: Optional[JobSummaryResponse] = None
    associated_checkpoint: Optional[PipelineCheckpointResponse] = None


class PipelineCheckpointDetailResponse(PipelineCheckpointResponse):
    """Detailed view of an approval checkpoint with hash freshness and latest audit."""
    current_artifact_hash: Optional[str] = None
    is_hash_fresh: Optional[bool] = None
    latest_audit: Optional[PipelineCheckpointAuditItem] = None


class PipelineAuditHistoryResponse(BaseModel):
    """Paginated collection of checkpoint audit history entries."""
    project_id: str
    checkpoint_type: Optional[str] = None
    checkpoint_id: Optional[str] = None
    total: int
    limit: int
    offset: int
    items: List[PipelineCheckpointAuditItem]


class PipelineStatusResponse(BaseModel):
    """Complete pipeline state for a project."""
    project_id: str
    project_name: str
    current_pipeline_stage: str
    pipeline_overall_status: str
    stages: List[PipelineStageResponse]
    checkpoints: List[PipelineCheckpointResponse]
