import json
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from pydantic import BaseModel
from backend.app.database import get_db
from backend.app.models.entities import Project, Scene, Shot, MediaAsset
from backend.app.agents.editor_agent import editor_agent
from backend.app.services.timeline_editor import timeline_editor_service
from backend.app.services.cost_controller import cost_controller
from backend.app.services.job_manager import job_manager, sanitize_error_message

router = APIRouter(prefix="/api/editor", tags=["editor"])

class ReorderRequest(BaseModel):
    shot_ids: List[str]

class RenderPreviewRequest(BaseModel):
    resolution: Optional[str] = "1280x720"
    fps: Optional[int] = 24
    burn_subtitles: Optional[bool] = True

@router.get("/timeline/{project_id}")
def get_timeline(project_id: str, db: Session = Depends(get_db)):
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    shots = db.query(Shot).filter(Shot.project_id == project_id).order_by(Shot.order_index).all()
    timeline_items = []
    current_time = 0.0

    for s in shots:
        dur = s.duration_seconds or 4.0
        media_url = None
        if s.video_asset_id:
            asset = db.query(MediaAsset).filter(MediaAsset.id == s.video_asset_id).first()
            if asset:
                media_url = f"/media/{asset.file_path}"

        thumb_url = f"/media/{s.thumbnail_path}" if s.thumbnail_path else None

        timeline_items.append({
            "shot_id": s.id,
            "scene_id": s.scene_id,
            "shot_number": s.shot_number,
            "shot_code": s.shot_code,
            "camera_angle": s.camera_angle,
            "camera_movement": s.camera_movement,
            "visual_description": s.visual_description,
            "duration_seconds": dur,
            "start_time_seconds": round(current_time, 2),
            "end_time_seconds": round(current_time + dur, 2),
            "generation_status": s.generation_status,
            "media_url": media_url,
            "thumbnail_url": thumb_url
        })
        current_time += dur

    return {
        "project_id": project.id,
        "project_name": project.name,
        "aspect_ratio": project.aspect_ratio,
        "total_duration_seconds": round(current_time, 2),
        "total_shots_count": len(shots),
        "timeline_items": timeline_items
    }

@router.post("/ai-assembly-cut/{project_id}")
def run_ai_assembly_cut(project_id: str, db: Session = Depends(get_db)):
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    shots = db.query(Shot).filter(Shot.project_id == project_id).order_by(Shot.order_index).all()
    shots_data = [
        {
            "id": s.id,
            "shot_code": s.shot_code,
            "duration_seconds": s.duration_seconds,
            "camera_angle": s.camera_angle,
            "video_asset_id": s.video_asset_id
        }
        for s in shots
    ]

    job_payload = {"shot_ids": [s.id for s in shots]}
    job, action = job_manager.prepare_or_claim_job(
        db=db,
        project_id=project_id,
        job_type="editor_assembly_cut",
        payload=job_payload
    )

    if action == "REUSE" and job.result:
        try:
            return json.loads(job.result)
        except Exception:
            pass

    if action == "IN_PROGRESS":
        raise HTTPException(
            status_code=409,
            detail=f"Assembly cut job is already in progress (job_id: {job.id}). Please wait for completion."
        )

    try:
        assembly = editor_agent.create_assembly_cut(shots_data)
        job_manager.complete_job(db, job.id, assembly, worker_id=job.worker_id, lease_token=job.lease_token)
        return assembly
    except Exception as e:
        job_manager.fail_job(db, job.id, e, worker_id=job.worker_id, lease_token=job.lease_token)
        if isinstance(e, HTTPException):
            raise e
        raise HTTPException(status_code=500, detail=sanitize_error_message(e))

@router.post("/reorder/{project_id}")
def reorder_timeline_shots(project_id: str, req: ReorderRequest, db: Session = Depends(get_db)):
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    for idx, sid in enumerate(req.shot_ids):
        shot = db.query(Shot).filter(Shot.id == sid, Shot.project_id == project_id).first()
        if shot:
            shot.order_index = idx + 1

    db.commit()
    return {"status": "reordered", "count": len(req.shot_ids)}

@router.post("/render-preview/{project_id}")
def render_timeline_preview(project_id: str, req: RenderPreviewRequest, db: Session = Depends(get_db)):
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    # Authorize & charge with duplicate detection
    cost_controller.authorize_and_charge(
        db=db,
        project_id=project.id,
        action_type="video_render",
        payload={"project_id": project.id, "res": req.resolution, "fps": req.fps},
        details=f"FFmpeg Render: {req.resolution} @ {req.fps}fps",
        allow_duplicate=True
    )

    result = timeline_editor_service.render_timeline_sequence(
        db=db,
        project_id=project_id,
        resolution=req.resolution or "1280x720",
        fps=req.fps or 24,
        burn_subtitles=req.burn_subtitles or True
    )

    return result
