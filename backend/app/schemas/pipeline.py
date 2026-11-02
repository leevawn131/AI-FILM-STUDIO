from typing import Optional, List, Dict, Any, Union
from datetime import datetime
import json
import re
from pydantic import BaseModel, Field, ConfigDict, field_validator


# ---------------------------------------------------------------------------
# Strict Public Metadata Allowlist
# ---------------------------------------------------------------------------
# Only explicitly approved top-level and nested fields are exposed to public clients.
# Unknown keys are dropped by default to prevent leakage of internal state,
# provider responses, raw prompts, headers, or private keys.
# ---------------------------------------------------------------------------

ALLOWED_METADATA_FIELDS = {
    "model",
    "resolution",
    "aspect_ratio",
    "duration_seconds",
    "frame_rate",
    "fps",
    "act_count",
    "scene_count",
    "estimated_pages",
    "generation_summary",
    "summary",
    "quality",
    "style",
    "format",
    "output_format",
    "version",
    "script_version",
    "progress_percent",
    "safe_notes",
    "note",
}

# Regex patterns for redacting any secrets that might be embedded in allowed text values
PATH_PATTERN = re.compile(
    r"(/(?:home|tmp|var|etc|opt|srv|mnt|workspace|Users|root)/[^\s:\"']+|[A-Za-z]:\\[^\s:\"']+)",
    re.IGNORECASE
)
KEY_PATTERNS = [
    (re.compile(r"sk-[a-zA-Z0-9_\-]{10,}", re.IGNORECASE), "[REDACTED_API_KEY]"),
    (re.compile(r"AIza[0-9A-Za-z_\-]{15,}", re.IGNORECASE), "[REDACTED_API_KEY]"),
    (re.compile(r"ghp_[a-zA-Z0-9]{15,}|github_pat_[a-zA-Z0-9_]{15,}", re.IGNORECASE), "[REDACTED_TOKEN]"),
    (re.compile(r"AKIA[0-9A-Za-z]{8,}", re.IGNORECASE), "[REDACTED_CREDENTIAL]"),
    (re.compile(r"(?i)bearer\s+[A-Za-z0-9_\-\.]{10,}", re.IGNORECASE), "Bearer [REDACTED_TOKEN]"),
    (re.compile(r"(?i)(?:password|passwd|pwd|secret|token|api_key|access_key|private_key|aws_secret)\s*[:=]\s*['\"]?[^\s'\"]+['\"]?"), "[REDACTED_SECRET]"),
    (re.compile(r"(?i)(?:postgres(?:ql)?|mysql|sqlite|mongodb|redis)://[^:\s]+:[^@\s]+@"), "[REDACTED_CONNECTION]@"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL), "[REDACTED_PRIVATE_KEY]"),
]


def sanitize_text_value(text: str) -> str:
    """Redacts filesystem paths and credential patterns from text values."""
    if not text:
        return text
    text = PATH_PATTERN.sub("[REDACTED_PATH]", text)
    for pat, repl in KEY_PATTERNS:
        text = pat.sub(repl, text)
    return text


def sanitize_stage_metadata(v: Any) -> Optional[Dict[str, Any]]:
    """
    Projects stage metadata through a strict public allowlist.
    Unknown fields, internal responses, raw prompts, headers, private keys,
    and filesystem paths are strictly stripped.
    """
    if v is None:
        return None
    if isinstance(v, str):
        try:
            parsed = json.loads(v)
            if isinstance(parsed, dict):
                v = parsed
            else:
                return {"summary": sanitize_text_value(str(v))}
        except Exception:
            return {"summary": sanitize_text_value(v)}

    if not isinstance(v, dict):
        return None

    out: Dict[str, Any] = {}
    for k, val in v.items():
        # Strict allowlist check: drop all unknown keys
        if k not in ALLOWED_METADATA_FIELDS:
            continue

        if isinstance(val, (int, float, bool)):
            out[k] = val
        elif isinstance(val, str):
            out[k] = sanitize_text_value(val)
        elif isinstance(val, list):
            # Only allow lists of primitive types
            clean_list = [
                sanitize_text_value(item) if isinstance(item, str) else item
                for item in val
                if isinstance(item, (int, float, bool, str))
            ]
            if clean_list:
                out[k] = clean_list
        elif isinstance(val, dict):
            # For nested dictionaries within allowed fields, enforce allowlist recursively
            sub_dict: Dict[str, Any] = {}
            for sub_k, sub_val in val.items():
                if sub_k in ALLOWED_METADATA_FIELDS:
                    if isinstance(sub_val, (int, float, bool)):
                        sub_dict[sub_k] = sub_val
                    elif isinstance(sub_val, str):
                        sub_dict[sub_k] = sanitize_text_value(sub_val)
            if sub_dict:
                out[k] = sub_dict

    return out


# ---------------------------------------------------------------------------
# Controlled User-Safe Error Sanitization
# ---------------------------------------------------------------------------
# Rather than relying solely on regex redaction, error messages are classified
# into controlled, user-safe failure states.
# Raw exception names, internal tracebacks, and technical indicators are never exposed.
# ---------------------------------------------------------------------------

SAFE_PATTERNS = [
    # Match user-intended business messages without internal technical details
    re.compile(r"^[A-Za-z0-9\s.,'!?:;()_\-]+$")
]

TECHNICAL_INDICATORS = [
    "traceback", "exception", "error:", "errno", "failed with", "raise ",
    "file \"", ".py", ".json", ".bin", ".env", ".pem", ".key",
    "/", "\\", "c:\\", "http://", "https://", "postgresql://", "sqlite://",
    "token", "secret", "password", "key", "auth", "bearer", "credential",
    "header", "socket", "connection", "query", "syntax", "nullpointer",
    "segfault", "panic", "select ", "insert ", "update ", "delete ",
    "private_key", "raw_prompt", "provider_response", "request_headers",
    "source_path", "artifact_uri", "ghp_", "akia"
]


def classify_controlled_error(lower: str) -> str:
    """Returns a controlled, user-safe failure state without exposing internal details."""
    if "timeout" in lower or "timed out" in lower:
        return "Operation timed out. Please retry the stage."
    elif "rate limit" in lower or "quota" in lower or "429" in lower:
        return "Provider quota or rate limit exceeded. Please retry later."
    elif "reject" in lower or "denied" in lower or "policy" in lower or "safety" in lower:
        return "Content or request was rejected by review policy."
    elif "connection" in lower or "network" in lower or "unreachable" in lower or "dns" in lower:
        return "External service connection failure. Please retry."
    elif "memory" in lower or "oom" in lower or "killed" in lower:
        return "Resource limit exceeded during processing."
    elif "validation" in lower or "invalid input" in lower or "schema" in lower:
        return "Stage input validation failed."
    else:
        return "Stage execution failed. Please retry the operation."


def sanitize_error_text(text: Optional[str]) -> Optional[str]:
    """
    Returns controlled, user-safe error messages.
    Never exposes arbitrary raw exceptions or traceback excerpts.
    """
    if not text:
        return text

    stripped = text.strip()
    lower = stripped.lower()

    # If it is multiline or contains traceback or code syntax
    if "\n" in stripped or "traceback" in lower or "raise " in lower:
        return classify_controlled_error(lower)

    # Check if any technical indicator, path, or credential indicator exists
    if any(ind in lower for ind in TECHNICAL_INDICATORS):
        return classify_controlled_error(lower)

    # If it's a short, clean single-line business message (e.g. director review note)
    if len(stripped) <= 200 and SAFE_PATTERNS[0].match(stripped):
        return stripped

    return classify_controlled_error(lower)


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


# ---------------------------------------------------------------------------
# S4 M2.2 Story Approval & Gatekeeper Schemas
# ---------------------------------------------------------------------------

class StoryReviewSceneItem(BaseModel):
    """Clean, public projection of a screenplay scene for human review."""
    scene_number: int
    order_index: int
    slugline: str
    title: str
    summary: str
    script_content: str
    estimated_duration: int

    model_config = ConfigDict(from_attributes=True)


class StoryReviewResponse(BaseModel):
    """Public read-only response for screenplay review and verification."""
    project_id: str
    project_name: str
    checkpoint_status: str
    artifact_version: int
    current_artifact_hash: Optional[str] = None
    submitted_artifact_hash: Optional[str] = None
    is_hash_fresh: bool = False
    scenes: List[StoryReviewSceneItem]


class StorySubmitResponse(BaseModel):
    """Confirmation of screenplay submission for human review."""
    project_id: str
    checkpoint_id: str
    checkpoint_type: str
    status: str
    artifact_version: int
    artifact_content_hash: str
    submitted_at: datetime
    message: str

    model_config = ConfigDict(from_attributes=True)


class CheckpointDecisionRequest(BaseModel):
    """Request payload for recording a director or human approval decision."""
    decision: str = Field(..., pattern="^(APPROVED|REJECTED|REQUEST_CHANGES)$", description="Approval decision: APPROVED, REJECTED, or REQUEST_CHANGES")
    expected_artifact_version: int = Field(..., gt=0, description="Artifact version present when reviewer inspected the submission")
    expected_artifact_hash: str = Field(..., pattern="^[a-fA-F0-9]{64}$", description="Canonical SHA-256 hash expected by reviewer")
    reviewer_name: str = Field("Director", min_length=1, max_length=128, description="Name or role of the reviewer")
    feedback_notes: Optional[str] = Field(None, max_length=2000, description="Optional director feedback or revision instructions")


class CheckpointDecisionResponse(BaseModel):
    """Response confirming recorded approval decision."""
    project_id: str
    checkpoint_id: str
    checkpoint_type: str
    status: str
    decision: str
    artifact_version: int
    artifact_content_hash: str
    reviewer_name: str
    feedback_notes: Optional[str] = None
    decided_at: datetime

    model_config = ConfigDict(from_attributes=True)

