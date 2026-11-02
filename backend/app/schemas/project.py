from typing import Optional, List
from datetime import datetime
from pydantic import BaseModel, Field, ConfigDict

# Project schemas
class ProjectBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    logline: Optional[str] = ""
    genre: Optional[str] = "Sci-Fi"
    aspect_ratio: Optional[str] = "16:9"
    fps: Optional[int] = 24
    target_duration_sec: Optional[int] = 120
    visual_style: Optional[str] = "Cinematic 35mm film look, anamorphic lens flares, rich color grading, natural depth of field"
    budget_total: Optional[float] = 100.0

class ProjectCreate(ProjectBase):
    pass

class ProjectUpdate(BaseModel):
    name: Optional[str] = None
    logline: Optional[str] = None
    genre: Optional[str] = None
    aspect_ratio: Optional[str] = None
    fps: Optional[int] = None
    target_duration_sec: Optional[int] = None
    visual_style: Optional[str] = None
    budget_total: Optional[float] = None

class ProjectResponse(ProjectBase):
    id: str
    budget_spent: float
    current_pipeline_stage: Optional[str] = "story_development"
    pipeline_overall_status: Optional[str] = "in_progress"
    created_at: datetime
    updated_at: datetime
    scenes_count: Optional[int] = 0
    shots_count: Optional[int] = 0
    media_count: Optional[int] = 0
    ready_shots_count: Optional[int] = 0

    model_config = ConfigDict(from_attributes=True)

# Dashboard summary schema
class DashboardStats(BaseModel):
    total_projects: int
    active_project_id: Optional[str]
    total_scenes: int
    total_shots: int
    total_ready_shots: int
    total_media_imported: int
    budget_allocated: float
    budget_spent: float
    recent_activity: List[dict]
