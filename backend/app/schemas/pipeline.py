from typing import Optional, List, Dict, Any
from datetime import datetime
import json
import re
from pydantic import BaseModel, Field, ConfigDict, field_validator


SENSITIVE_KEY_SUBSTRINGS = {
    "worker", "fencing", "lease", "payload", "internal",
    "token", "secret", "password", "api_key", "apikey",
    "auth", "credential", "traceback", "stack_trace",
    "filepath", "file_path", "local_path", "disk_path",
    "storage_path", "db_url", "database_url", "connection_string",
    "root_dir", "absolute_path"
}


def is_sensitive_key(k: Any) -> bool:
    kl = str(k).lower().replace("-", "_")
    return any(sub in kl for sub in SENSITIVE_KEY_SUBSTRINGS)


def sanitize_error_text(text: Optional[str]) -> Optional[str]:
    """Redacts internal stack traces, API keys, credentials, and local filesystem paths."""
    if not text:
        return text

    # If Python traceback, extract clean summary to avoid exposing internal frame logic
    if "Traceback (most recent call last):" in text:
        lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
        last_line = lines[-1] if lines else "Internal processing error"
        text = f"Pipeline execution error: {last_line}"

    # Redact API keys and authorization tokens
    text = re.sub(r"sk-[a-zA-Z0-9_\-]{15,}", "[REDACTED_API_KEY]", text)
    text = re.sub(r"AIza[0-9A-Za-z_\-]{20,}", "[REDACTED_API_KEY]", text)
    text = re.sub(r"(?i)bearer\s+[A-Za-z0-9_\-\.]{15,}", "Bearer [REDACTED_TOKEN]", text)
    text = re.sub(r"(?i)(password|passwd|pwd|secret|token|api_key)=['\"][^'\"]+['\"]", r"\1='[REDACTED]'", text)
    text = re.sub(r"(?i)(postgres(?:ql)?|mysql|sqlite)://[^:\s]+:[^@\s]+@", r"\1://[REDACTED_USER]:[REDACTED_PASS]@", text)

    # Redact local filesystem paths
    text = re.sub(r"(/home/[a-zA-Z0-9_.\-]+/[^\s:\"']+)", "[REDACTED_PATH]", text)
    text = re.sub(r"(/tmp/[^\s:\"']+)", "[REDACTED_PATH]", text)
    text = re.sub(r"(/var/[^\s:\"']+)", "[REDACTED_PATH]", text)
    text = re.sub(r"(/etc/[^\s:\"']+)", "[REDACTED_PATH]", text)
    text = re.sub(r"([A-Za-z]:\\[^\s:\"']+)", "[REDACTED_PATH]", text)

    return text


def sanitize_stage_metadata(v: Any) -> Optional[Dict[str, Any]]:
    """Sanitizes stage metadata dictionary, removing sensitive internal keys and redacting text."""
    if v is None:
        return None
    if isinstance(v, str):
        try:
            parsed = json.loads(v)
            if isinstance(parsed, dict):
                v = parsed
            elif isinstance(parsed, list):
                v = {"items": parsed}
            else:
                return {"note": sanitize_error_text(str(v))}
        except Exception:
            return {"note": sanitize_error_text(v)}

    if not isinstance(v, dict):
        return None

    def _sanitize(val: Any) -> Any:
        if isinstance(val, dict):
            return {
                k: _sanitize(item)
                for k, item in val.items()
                if not is_sensitive_key(k)
            }
        elif isinstance(val, list):
            return [_sanitize(item) for item in val]
        elif isinstance(val, str):
            return sanitize_error_text(val)
        else:
            return val

    return _sanitize(v)


class JobSummaryResponse(BaseModel):
    """Safe public projection of a StudioJob without sensitive worker details."""
    id: str
    job_type: str
    status: str
    error_message: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    @field_validator("error_message", mode="before")
    @classmethod
    def clean_error_message(cls, v: Optional[str]) -> Optional[str]:
        return sanitize_error_text(v)

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
    def parse_and_sanitize_metadata(cls, v: Any) -> Optional[Dict[str, Any]]:
        return sanitize_stage_metadata(v)

    @field_validator("error_message", mode="before")
    @classmethod
    def clean_error_message(cls, v: Optional[str]) -> Optional[str]:
        return sanitize_error_text(v)

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
    is_initialized: bool = Field(False, description="Whether the pipeline has been fully initialized with all 11 stages and 4 checkpoints")
    stages: List[PipelineStageResponse]
    checkpoints: List[PipelineCheckpointResponse]
