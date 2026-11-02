import shutil
from pathlib import Path
from typing import List, Optional
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from backend.app.database import get_db
from backend.app.config import settings
from backend.app.models.entities import Project, Scene, Shot, MediaAsset, CostRecord
from backend.app.schemas.project import (
    ProjectCreate, ProjectUpdate, ProjectResponse, DashboardStats
)
from backend.app.services.security import validate_safe_path, sanitize_filename
from backend.app.services.pipeline_orchestrator import (
    initialize_project_pipeline, is_pipeline_complete, PIPELINE_STAGES
)

router = APIRouter(prefix="/api/projects", tags=["projects"])

def get_project_stats(db: Session, project: Project) -> dict:
    scenes_count = db.query(Scene).filter(Scene.project_id == project.id).count()
    shots_count = db.query(Shot).filter(Shot.project_id == project.id).count()
    ready_shots = db.query(Shot).filter(
        Shot.project_id == project.id,
        Shot.generation_status.in_(["imported", "approved"])
    ).count()
    media_count = db.query(MediaAsset).filter(MediaAsset.project_id == project.id).count()

    return {
        "scenes_count": scenes_count,
        "shots_count": shots_count,
        "ready_shots_count": ready_shots,
        "media_count": media_count
    }

@router.get("", response_model=List[ProjectResponse])
def list_projects(db: Session = Depends(get_db)):
    projects = db.query(Project).filter(Project.deleted_at.is_(None)).order_by(Project.created_at.desc()).all()
    result = []
    for p in projects:
        stats = get_project_stats(db, p)
        p_dict = {
            "id": p.id,
            "name": p.name,
            "logline": p.logline,
            "genre": p.genre,
            "aspect_ratio": p.aspect_ratio,
            "fps": p.fps,
            "target_duration_sec": p.target_duration_sec,
            "visual_style": p.visual_style,
            "budget_total": p.budget_total,
            "budget_spent": p.budget_spent,
            "current_pipeline_stage": p.current_pipeline_stage or "story_development",
            "pipeline_overall_status": p.pipeline_overall_status or "in_progress",
            "created_at": p.created_at,
            "updated_at": p.updated_at,
            **stats
        }
        result.append(p_dict)
    return result

@router.post("", response_model=ProjectResponse, status_code=201)
def create_project(data: ProjectCreate, db: Session = Depends(get_db)):
    project = Project(
        name=data.name,
        logline=data.logline or "",
        genre=data.genre or "Sci-Fi",
        aspect_ratio=data.aspect_ratio or "16:9",
        fps=data.fps or 24,
        target_duration_sec=data.target_duration_sec or 120,
        visual_style=data.visual_style or "Cinematic 35mm film look, anamorphic lens flares, rich color grading, natural depth of field",
        budget_total=data.budget_total if data.budget_total is not None else 100.0,
        budget_spent=0.0,
        current_pipeline_stage="story_development",
        pipeline_overall_status="in_progress"
    )
    try:
        db.add(project)
        db.flush()
        # Atomically initialize pipeline stages and checkpoints
        initialize_project_pipeline(project.id, db, auto_commit=False)
        db.commit()
        db.refresh(project)
    except Exception:
        db.rollback()
        raise

    # Scaffold local project folders
    project_dir = settings.projects_dir / project.id
    for sub in ["flow_packages", "imported_videos", "renders", "audio", "references"]:
        (project_dir / sub).mkdir(parents=True, exist_ok=True)

    return {
        "id": project.id,
        "name": project.name,
        "logline": project.logline,
        "genre": project.genre,
        "aspect_ratio": project.aspect_ratio,
        "fps": project.fps,
        "target_duration_sec": project.target_duration_sec,
        "visual_style": project.visual_style,
        "budget_total": project.budget_total,
        "budget_spent": project.budget_spent,
        "current_pipeline_stage": project.current_pipeline_stage,
        "pipeline_overall_status": project.pipeline_overall_status,
        "created_at": project.created_at,
        "updated_at": project.updated_at,
        "scenes_count": 0,
        "shots_count": 0,
        "ready_shots_count": 0,
        "media_count": 0
    }

@router.get("/{project_id}", response_model=ProjectResponse)
def get_project(project_id: str, db: Session = Depends(get_db)):
    project = db.query(Project).filter(Project.id == project_id, Project.deleted_at.is_(None)).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    # Safely self-heal legacy projects predating S4 pipeline or missing checkpoints/stages
    if not is_pipeline_complete(db, project_id):
        initialize_project_pipeline(project_id, db, auto_commit=True)
        db.refresh(project)

    stats = get_project_stats(db, project)
    return {
        "id": project.id,
        "name": project.name,
        "logline": project.logline,
        "genre": project.genre,
        "aspect_ratio": project.aspect_ratio,
        "fps": project.fps,
        "target_duration_sec": project.target_duration_sec,
        "visual_style": project.visual_style,
        "budget_total": project.budget_total,
        "budget_spent": project.budget_spent,
        "current_pipeline_stage": project.current_pipeline_stage or "story_development",
        "pipeline_overall_status": project.pipeline_overall_status or "in_progress",
        "created_at": project.created_at,
        "updated_at": project.updated_at,
        **stats
    }

@router.patch("/{project_id}", response_model=ProjectResponse)
def update_project(project_id: str, data: ProjectUpdate, db: Session = Depends(get_db)):
    project = db.query(Project).filter(Project.id == project_id, Project.deleted_at.is_(None)).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(project, field, value)

    db.commit()
    db.refresh(project)
    stats = get_project_stats(db, project)
    return {
        "id": project.id,
        "name": project.name,
        "logline": project.logline,
        "genre": project.genre,
        "aspect_ratio": project.aspect_ratio,
        "fps": project.fps,
        "target_duration_sec": project.target_duration_sec,
        "visual_style": project.visual_style,
        "budget_total": project.budget_total,
        "budget_spent": project.budget_spent,
        "current_pipeline_stage": project.current_pipeline_stage or "story_development",
        "pipeline_overall_status": project.pipeline_overall_status or "in_progress",
        "created_at": project.created_at,
        "updated_at": project.updated_at,
        **stats
    }

@router.delete("/{project_id}")
def delete_project(project_id: str, db: Session = Depends(get_db)):
    project = db.query(Project).filter(Project.id == project_id, Project.deleted_at.is_(None)).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    # Soft delete project to preserve immutable audit trail
    project.deleted_at = datetime.now(timezone.utc)
    db.commit()
    return {"status": "deleted", "project_id": project_id}
