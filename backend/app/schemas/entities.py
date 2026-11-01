from typing import Optional, List, Dict
from datetime import datetime
from pydantic import BaseModel, Field, ConfigDict

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
    scene_number: int = 1
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
    scene_number: Optional[int] = None
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

    model_config = ConfigDict(from_attributes=True)


# Media Schemas
class MediaAssetResponse(BaseModel):
    id: str
    project_id: str
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
    total_actions: int
    total_flow_credits: Optional[float] = 0.0
    records: List[CostRecordResponse]
    provider_breakdown: Optional[Dict[str, int]] = None


# Export Job Schemas
class ExportJobCreate(BaseModel):
    project_id: str
    preset_name: Optional[str] = "1080p_cinematic"
    resolution: Optional[str] = "1920x1080"
    aspect_ratio: Optional[str] = "16:9"
    fps: Optional[int] = 24
    include_subtitles: Optional[bool] = False

class ExportJobResponse(BaseModel):
    id: str
    project_id: str
    preset_name: str
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
