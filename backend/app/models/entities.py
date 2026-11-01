import uuid
from datetime import datetime, timezone
from sqlalchemy import (
    Column, String, Integer, Float, Boolean, Text, ForeignKey, DateTime
)
from sqlalchemy.orm import relationship
from backend.app.database import Base

def gen_uuid() -> str:
    return str(uuid.uuid4())

def utc_now():
    return datetime.now(timezone.utc)

class Project(Base):
    __tablename__ = "projects"

    id = Column(String(36), primary_key=True, default=gen_uuid)
    name = Column(String(128), nullable=False)
    logline = Column(Text, default="")
    genre = Column(String(64), default="Sci-Fi")
    aspect_ratio = Column(String(16), default="16:9")
    fps = Column(Integer, default=24)
    target_duration_sec = Column(Integer, default=120)
    visual_style = Column(Text, default="Cinematic 35mm film look, anamorphic lens flares, rich color grading, natural depth of field")
    budget_total = Column(Float, default=100.0)
    budget_spent = Column(Float, default=0.0)
    created_at = Column(DateTime, default=utc_now)
    updated_at = Column(DateTime, default=utc_now, onupdate=utc_now)

    # Relationships
    scenes = relationship("Scene", back_populates="project", cascade="all, delete-orphan", order_by="Scene.order_index")
    characters = relationship("Character", back_populates="project", cascade="all, delete-orphan")
    locations = relationship("Location", back_populates="project", cascade="all, delete-orphan")
    shots = relationship("Shot", back_populates="project", cascade="all, delete-orphan", order_by="Shot.order_index")
    media_assets = relationship("MediaAsset", back_populates="project", cascade="all, delete-orphan")
    audio_tracks = relationship("AudioTrack", back_populates="project", cascade="all, delete-orphan")
    cost_records = relationship("CostRecord", back_populates="project", cascade="all, delete-orphan")
    export_jobs = relationship("ExportJob", back_populates="project", cascade="all, delete-orphan")


class Character(Base):
    __tablename__ = "characters"

    id = Column(String(36), primary_key=True, default=gen_uuid)
    project_id = Column(String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    name = Column(String(128), nullable=False)
    role = Column(String(64), default="Protagonist")
    age = Column(String(32), default="30s")
    facial_characteristics = Column(Text, default="")
    clothing = Column(Text, default="")
    voice_profile = Column(Text, default="")
    personality = Column(Text, default="")
    visual_prompt_tags = Column(Text, default="")
    reference_image_path = Column(String(512), default="")
    created_at = Column(DateTime, default=utc_now)

    project = relationship("Project", back_populates="characters")


class Location(Base):
    __tablename__ = "locations"

    id = Column(String(36), primary_key=True, default=gen_uuid)
    project_id = Column(String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    name = Column(String(128), nullable=False)
    description = Column(Text, default="")
    time_of_day = Column(String(64), default="Night")
    weather = Column(String(64), default="Clear")
    lighting_setup = Column(Text, default="High contrast cinematic lighting with rim highlights")
    visual_prompt_tags = Column(Text, default="")
    reference_image_path = Column(String(512), default="")
    created_at = Column(DateTime, default=utc_now)

    project = relationship("Project", back_populates="locations")


class Scene(Base):
    __tablename__ = "scenes"

    id = Column(String(36), primary_key=True, default=gen_uuid)
    project_id = Column(String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    scene_number = Column(Integer, nullable=False, default=1)
    slugline = Column(String(256), nullable=False, default="INT. STUDIO - DAY")
    title = Column(String(256), default="")
    summary = Column(Text, default="")
    script_content = Column(Text, default="")
    location_id = Column(String(36), ForeignKey("locations.id", ondelete="SET NULL"), nullable=True)
    estimated_duration = Column(Integer, default=15) # seconds
    budget_limit = Column(Float, default=20.0) # per-scene budget limit
    order_index = Column(Integer, default=0)
    created_at = Column(DateTime, default=utc_now)

    project = relationship("Project", back_populates="scenes")
    shots = relationship("Shot", back_populates="scene", cascade="all, delete-orphan", order_by="Shot.order_index")


class Shot(Base):
    __tablename__ = "shots"

    id = Column(String(36), primary_key=True, default=gen_uuid)
    scene_id = Column(String(36), ForeignKey("scenes.id", ondelete="CASCADE"), nullable=False)
    project_id = Column(String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    shot_number = Column(Integer, nullable=False, default=1)
    shot_code = Column(String(32), default="SC01_SH01")
    duration_seconds = Column(Float, default=4.0)
    camera_angle = Column(String(64), default="Eye Level")
    camera_movement = Column(String(64), default="Slow Push In")
    visual_description = Column(Text, default="")
    lighting = Column(String(256), default="Cinematic volumetric lighting")
    character_references = Column(Text, default="[]")  # JSON encoded list of character names
    video_prompt = Column(Text, default="")  # Google Flow optimized prompt
    negative_prompt = Column(Text, default="blurry, distorted, amateur, low resolution, watermark, bad anatomy")
    seed = Column(Integer, nullable=True)
    generation_status = Column(String(32), default="pending") # pending, ready_for_flow, generating, imported, approved
    video_asset_id = Column(String(36), ForeignKey("media_assets.id", ondelete="SET NULL"), nullable=True)
    thumbnail_path = Column(String(512), default="")
    order_index = Column(Integer, default=0)
    created_at = Column(DateTime, default=utc_now)

    project = relationship("Project", back_populates="shots")
    scene = relationship("Scene", back_populates="shots")
    video_asset = relationship("MediaAsset", foreign_keys=[video_asset_id])


class MediaAsset(Base):
    __tablename__ = "media_assets"

    id = Column(String(36), primary_key=True, default=gen_uuid)
    project_id = Column(String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    filename = Column(String(256), nullable=False)
    file_path = Column(String(512), nullable=False)
    media_type = Column(String(32), default="video") # video, image, audio
    width = Column(Integer, default=1920)
    height = Column(Integer, default=1080)
    duration_seconds = Column(Float, default=0.0)
    fps = Column(Float, default=24.0)
    codec = Column(String(64), default="h264")
    file_size_bytes = Column(Integer, default=0)
    thumbnail_path = Column(String(512), default="")
    auto_matched_shot_id = Column(String(36), nullable=True)
    is_validated = Column(Boolean, default=True)
    validation_notes = Column(Text, default="Validated via FFprobe")
    created_at = Column(DateTime, default=utc_now)

    project = relationship("Project", back_populates="media_assets")


class AudioTrack(Base):
    __tablename__ = "audio_tracks"

    id = Column(String(36), primary_key=True, default=gen_uuid)
    project_id = Column(String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    track_type = Column(String(32), default="voiceover") # voiceover, music, sfx, ambient
    title = Column(String(256), nullable=False)
    file_path = Column(String(512), nullable=False)
    duration_seconds = Column(Float, default=0.0)
    start_time_seconds = Column(Float, default=0.0)
    volume_level = Column(Float, default=1.0) # 0.0 to 2.0
    ducking_enabled = Column(Boolean, default=False)
    ducking_level = Column(Float, default=0.25)
    scene_id = Column(String(36), nullable=True)
    shot_id = Column(String(36), nullable=True)
    created_at = Column(DateTime, default=utc_now)

    project = relationship("Project", back_populates="audio_tracks")


class CostRecord(Base):
    __tablename__ = "cost_records"

    id = Column(String(36), primary_key=True, default=gen_uuid)
    project_id = Column(String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    scene_id = Column(String(36), nullable=True)
    action_type = Column(String(64), nullable=False)
    credits_used = Column(Float, default=1.0)
    cost_usd = Column(Float, default=0.0)
    prompt_hash = Column(String(64), default="") # for duplicate detection
    details = Column(Text, default="")
    created_at = Column(DateTime, default=utc_now)

    project = relationship("Project", back_populates="cost_records")


class ExportJob(Base):
    __tablename__ = "export_jobs"

    id = Column(String(36), primary_key=True, default=gen_uuid)
    project_id = Column(String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    preset_name = Column(String(64), default="1080p_cinematic")
    resolution = Column(String(32), default="1920x1080")
    aspect_ratio = Column(String(16), default="16:9")
    fps = Column(Integer, default=24)
    include_subtitles = Column(Boolean, default=False)
    status = Column(String(32), default="queued") # queued, rendering, completed, failed
    progress_pct = Column(Integer, default=0)
    output_path = Column(String(512), default="")
    file_size_bytes = Column(Integer, default=0)
    error_message = Column(Text, default="")
    created_at = Column(DateTime, default=utc_now)
    completed_at = Column(DateTime, nullable=True)

    project = relationship("Project", back_populates="export_jobs")
