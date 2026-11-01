from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from backend.app.database import get_db
from backend.app.config import settings
from backend.app.models.entities import Project
from backend.app.schemas.entities import RecordFlowUsageRequest
from backend.app.services.flow_handoff import flow_handoff_service
from backend.app.services.cost_controller import cost_controller
from backend.app.services.security import validate_safe_path

router = APIRouter(prefix="/api/flow-handoff", tags=["flow_handoff"])

@router.get("/package-info/{project_id}")
def get_package_info(project_id: str, db: Session = Depends(get_db)):
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    pkg = flow_handoff_service.generate_package(db, project_id)
    return {
        "project_name": project.name,
        "total_shots": pkg["total_shots"],
        "total_characters": pkg["total_characters"],
        "estimated_minutes": pkg["estimated_minutes"],
        "instructions_markdown": pkg["instructions_markdown"],
        "quick_copy_txt": pkg["quick_copy_txt"],
        "zip_available": Path(pkg["zip_path"]).exists()
    }

@router.post("/generate/{project_id}")
def generate_handoff_package(project_id: str, db: Session = Depends(get_db)):
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    pkg = flow_handoff_service.generate_package(db, project_id)
    return {
        "status": "ready",
        "zip_filename": pkg["zip_filename"],
        "total_shots": pkg["total_shots"],
        "total_characters": pkg["total_characters"]
    }

@router.get("/download/{project_id}")
def download_flow_package(project_id: str, db: Session = Depends(get_db)):
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    pkg = flow_handoff_service.generate_package(db, project_id)
    zip_path = validate_safe_path(Path(pkg["zip_path"]), settings.data_dir)
    if not zip_path.exists():
        raise HTTPException(status_code=404, detail="Package zip not generated")

    return FileResponse(
        path=str(zip_path),
        filename=pkg["zip_filename"],
        media_type="application/zip"
    )

@router.post("/record-usage/{project_id}")
def record_actual_flow_usage(project_id: str, data: RecordFlowUsageRequest, db: Session = Depends(get_db)):
    """
    Explicitly records actual Google Flow credits spent on video generation or regeneration for a shot.
    Does NOT invent prices or conflate estimated package prep with actual platform video generation.
    """
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    record, shot = cost_controller.record_flow_credit_usage(
        db=db,
        project_id=project_id,
        shot_id=data.shot_id,
        flow_credits=data.flow_credits,
        is_regeneration=data.is_regeneration,
        notes=data.notes
    )

    return {
        "status": "success",
        "shot_id": shot.id,
        "shot_code": shot.shot_code,
        "flow_credits_charged": data.flow_credits,
        "total_shot_flow_credits": shot.flow_credits_spent,
        "generation_attempts": shot.generation_attempts,
        "ledger_record_id": record.id
    }
