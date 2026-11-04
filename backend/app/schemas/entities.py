import uuid
from typing import Optional, List, Dict
from datetime import datetime
from pydantic import BaseModel, Field, ConfigDict, field_validator

# Character Schemas
class CharacterBase(BaseModel):
    name: str = Field(..., min_length=1)
    role: Optional[str] = "Protagonist"
    age: Optional[str] = "30s"
    facial_characteristics: Optional[str] = ""
    clothing: Optional[str] = ""
    voice_profile: Optional[str] = ""
    personality: Optional[str] = ""
    visual_prompt_tags: Optional[str] = ""
    reference_image_path: Optional[str] = ""

class CharacterCreate(CharacterBase):
    project_id: str

class CharacterUpdate(BaseModel):
    name: Optional[str] = None
    role: Optional[str] = None
    age: Optional[str] = None
    facial_characteristics: Optional[str] = None
    clothing: Optional[str] = None
    voice_profile: Optional[str] = None
    personality: Optional[str] = None
    visual_prompt_tags: Optional[str] = None
    reference_image_path: Optional[str] = None

class CharacterResponse(CharacterBase):
    id: str
    project_id: str
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


# Location Schemas
class LocationBase(BaseModel):
    name: str = Field(..., min_length=1)
    description: Optional[str] = ""
    time_of_day: Optional[str] = "Night"
    weather: Optional[str] = "Clear"
    lighting_setup: Optional[str] = "High contrast cinematic lighting with rim highlights"
    visual_prompt_tags: Optional[str] = ""
    reference_image_path: Optional[str] = ""

class LocationCreate(LocationBase):
    project_id: str

class LocationUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    time_of_day: Optional[str] = None
    weather: Optional[str] = None
    lighting_setup: Optional[str] = None
    visual_prompt_tags: Optional[str] = None
    reference_image_path: Optional[str] = None

class LocationResponse(LocationBase):
    id: str
    project_id: str
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


# Scene Schemas
class SceneBase(BaseModel):
    scene_number: int = Field(1, ge=1)
    slugline: str = "INT. STUDIO - DAY"
    title: Optional[str] = ""
    summary: Optional[str] = ""
    script_content: Optional[str] = ""
    location_id: Optional[str] = None
    estimated_duration: Optional[int] = 15
    budget_limit: Optional[float] = 20.0
    order_index: Optional[int] = 0

class SceneCreate(SceneBase):
    project_id: str

class SceneUpdate(BaseModel):
    scene_number: Optional[int] = Field(None, ge=1)
    slugline: Optional[str] = None
    title: Optional[str] = None
    summary: Optional[str] = None
    script_content: Optional[str] = None
    location_id: Optional[str] = None
    estimated_duration: Optional[int] = None
    budget_limit: Optional[float] = None
    order_index: Optional[int] = None

class SceneResponse(SceneBase):
    id: str
    project_id: str
    budget_spent: Optional[float] = 0.0
    created_at: datetime
    shots_count: Optional[int] = 0

    model_config = ConfigDict(from_attributes=True)


# Shot Schemas
class ShotBase(BaseModel):
    shot_number: int = 1
    shot_code: Optional[str] = "SC01_SH01"
    duration_seconds: Optional[float] = 4.0
    camera_angle: Optional[str] = "Eye Level"
    camera_movement: Optional[str] = "Slow Push In"
    visual_description: Optional[str] = ""
    lighting: Optional[str] = "Cinematic volumetric lighting"
    character_references: Optional[str] = "[]"
    video_prompt: Optional[str] = ""
    negative_prompt: Optional[str] = "blurry, distorted, amateur, low resolution, watermark, bad anatomy"
    seed: Optional[int] = None
    generation_status: Optional[str] = "pending"
    video_asset_id: Optional[str] = None
    thumbnail_path: Optional[str] = ""
    flow_credits_spent: Optional[float] = 0.0
    generation_attempts: Optional[int] = 0
    order_index: Optional[int] = 0

class ShotCreate(ShotBase):
    scene_id: str
    project_id: str

class ShotUpdate(BaseModel):
    shot_number: Optional[int] = None
    shot_code: Optional[str] = None
    duration_seconds: Optional[float] = None
    camera_angle: Optional[str] = None
    camera_movement: Optional[str] = None
    visual_description: Optional[str] = None
    lighting: Optional[str] = None
    character_references: Optional[str] = None
    video_prompt: Optional[str] = None
    negative_prompt: Optional[str] = None
    seed: Optional[int] = None
    generation_status: Optional[str] = None
    video_asset_id: Optional[str] = None
    thumbnail_path: Optional[str] = None
    order_index: Optional[int] = None

class ShotResponse(ShotBase):
    id: str
    scene_id: str
    project_id: str
    created_at: datetime
    media_url: Optional[str] = None
    versions_count: Optional[int] = 0
    selected_version: Optional[int] = None

    model_config = ConfigDict(from_attributes=True)


class SelectShotVersionRequest(BaseModel):
    media_asset_id: str


# Media Schemas
class MediaAssetResponse(BaseModel):
    id: str
    project_id: str
    shot_id: Optional[str] = None
    version: int = 1
    status: str = "ready"
    filename: str
    file_path: str
    media_type: str
    width: int
    height: int
    duration_seconds: float
    fps: float
    codec: str
    file_size_bytes: int
    thumbnail_path: Optional[str] = ""
    checksum: Optional[str] = None
    auto_matched_shot_id: Optional[str] = None
    is_validated: bool
    validation_notes: Optional[str] = ""
    created_at: datetime
    url: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


# Audio Schemas
class AudioTrackBase(BaseModel):
    track_type: str = "voiceover"
    title: str = "Audio Track"
    file_path: str
    duration_seconds: Optional[float] = 0.0
    start_time_seconds: Optional[float] = 0.0
    fade_in_seconds: Optional[float] = 0.0
    fade_out_seconds: Optional[float] = 0.0
    volume_level: Optional[float] = 1.0
    ducking_enabled: Optional[bool] = False
    ducking_level: Optional[float] = 0.25
    scene_id: Optional[str] = None
    shot_id: Optional[str] = None

class AudioTrackCreate(AudioTrackBase):
    project_id: str

class AudioTrackUpdate(BaseModel):
    title: Optional[str] = None
    volume_level: Optional[float] = None
    start_time_seconds: Optional[float] = None
    ducking_enabled: Optional[bool] = None
    ducking_level: Optional[float] = None

class AudioTrackResponse(AudioTrackBase):
    id: str
    project_id: str
    created_at: datetime
    url: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


# Cost & Budget Schemas
class CostRecordResponse(BaseModel):
    id: str
    project_id: str
    scene_id: Optional[str] = None
    shot_id: Optional[str] = None
    provider: Optional[str] = "offline_engine"
    model: Optional[str] = None
    operation: Optional[str] = "unknown"
    input_tokens: Optional[int] = 0
    output_tokens: Optional[int] = 0
    flow_credits: Optional[float] = 0.0
    actual_cost: Optional[float] = None
    currency: Optional[str] = "USD"
    estimated_cost: Optional[float] = 0.0
    actual_cost_known: Optional[bool] = False
    action_type: str
    credits_used: float
    cost_usd: float
    prompt_hash: Optional[str] = ""
    details: Optional[str] = ""
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)

class BudgetSummary(BaseModel):
    project_id: str
    budget_total: float
    budget_spent: float
    budget_remaining: float
    percent_used: float
    budget_warning_threshold: Optional[float] = 0.8
    warning_triggered: Optional[bool] = False
    total_actions: int
    total_flow_credits: Optional[float] = 0.0
    records: List[CostRecordResponse]
    provider_breakdown: Optional[Dict[str, int]] = None


# Export Job Schemas
class ExportJobCreate(BaseModel):
    project_id: str
    preset_name: Optional[str] = "1080p_cinematic"
    render_mode: Optional[str] = None
    allow_placeholders: Optional[bool] = None
    resolution: Optional[str] = "1920x1080"
    aspect_ratio: Optional[str] = "16:9"
    fps: Optional[int] = 24
    include_subtitles: Optional[bool] = False

class ExportJobResponse(BaseModel):
    id: str
    project_id: str
    preset_name: str
    render_mode: Optional[str] = "final"
    allow_placeholders: Optional[bool] = False
    resolution: str
    aspect_ratio: str
    fps: int
    include_subtitles: bool
    status: str
    progress_pct: int
    output_path: Optional[str] = ""
    file_size_bytes: Optional[int] = 0
    error_message: Optional[str] = ""
    created_at: datetime
    completed_at: Optional[datetime] = None
    download_url: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


# Persistent Studio Job Lifecycle Schemas
class StudioJobResponse(BaseModel):
    id: str
    project_id: str
    job_type: str
    idempotency_key: str
    status: str
    worker_id: Optional[str] = None
    fencing_token: Optional[int] = 1
    lease_token: Optional[int] = 1
    payload: Optional[str] = "{}"
    result: Optional[str] = "{}"
    error_message: Optional[str] = ""
    retry_count: int = 0
    created_at: datetime
    updated_at: datetime
    completed_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


class GenerationJobResponse(BaseModel):
    id: str
    project_id: str
    scene_id: Optional[str] = None
    shot_id: Optional[str] = None
    provider: str
    model: str
    generation_type: str
    input_parameters: Optional[str] = "{}"
    status: str
    provider_operation_id: Optional[str] = None
    supports_cancellation: bool = False
    result_metadata: Optional[str] = "{}"
    result_asset_id: Optional[str] = None
    error_details: Optional[str] = ""
    retry_count: int = 0
    created_at: datetime
    updated_at: datetime
    completed_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


class GenerateImageRequest(BaseModel):
    project_id: str
    prompt: str
    aspect_ratio: Optional[str] = "16:9"
    entity_type: Optional[str] = None
    entity_id: Optional[str] = None
    negative_prompt: Optional[str] = None


class GenerateVideoRequest(BaseModel):
    project_id: str
    shot_id: str
    prompt: str
    duration_seconds: Optional[int] = 5
    aspect_ratio: Optional[str] = "16:9"
    negative_prompt: Optional[str] = None


class UploadOperationResponse(BaseModel):
    id: str
    project_id: Optional[str] = None
    entity_type: str
    entity_id: str
    temp_path: str
    destination_path: str
    old_asset_path: Optional[str] = None
    status: str
    checksum: Optional[str] = None
    file_size_bytes: int = 0
    worker_id: Optional[str] = None
    lease_expires_at: Optional[datetime] = None
    fencing_token: int = 1
    error_message: Optional[str] = ""
    created_at: datetime
    updated_at: datetime
    completed_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


# Explicit Flow Credit Accounting Schema
class RecordFlowUsageRequest(BaseModel):
    shot_id: str
    flow_credits: float = 1.0
    is_regeneration: bool = False
    generation_attempt_id: str
    notes: Optional[str] = ""

    @field_validator("generation_attempt_id")
    @classmethod
    def validate_uuid_format(cls, v: str) -> str:
        if not v or not isinstance(v, str):
            raise ValueError("generation_attempt_id is mandatory and must be a valid UUID string.")
        try:
            val = uuid.UUID(v.strip())
            return str(val)
        except (ValueError, AttributeError):
            raise ValueError("generation_attempt_id must be a valid UUID string.")

