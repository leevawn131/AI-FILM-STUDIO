from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from backend.app.database import get_db
from backend.app.models.entities import Project, Scene, Shot, MediaAsset, CostRecord
from backend.app.schemas.project import DashboardStats

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])

@router.get("/stats", response_model=DashboardStats)
def get_dashboard_stats(db: Session = Depends(get_db)):
    projects = db.query(Project).all()
    total_projects = len(projects)
    total_scenes = db.query(Scene).count()
    total_shots = db.query(Shot).count()
    ready_shots = db.query(Shot).filter(Shot.generation_status.in_(["imported", "approved"])).count()
    media_imported = db.query(MediaAsset).count()
    
    budget_alloc = sum([p.budget_total for p in projects]) if projects else 0.0
    budget_spent = sum([p.budget_spent for p in projects]) if projects else 0.0

    recent_projects = db.query(Project).order_by(Project.updated_at.desc()).limit(5).all()
    recent_activity = []
    for p in recent_projects:
        recent_activity.append({
            "id": p.id,
            "name": p.name,
            "genre": p.genre,
            "updated_at": p.updated_at.isoformat() if p.updated_at else p.created_at.isoformat()
        })

    active_id = projects[0].id if projects else None

    return {
        "total_projects": total_projects,
        "active_project_id": active_id,
        "total_scenes": total_scenes,
        "total_shots": total_shots,
        "total_ready_shots": ready_shots,
        "total_media_imported": media_imported,
        "budget_allocated": round(budget_alloc, 2),
        "budget_spent": round(budget_spent, 3),
        "recent_activity": recent_activity
    }
