import json
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from pydantic import BaseModel
from backend.app.database import get_db
from backend.app.models.entities import Project, Scene, Shot, Character, MediaAsset
from backend.app.schemas.entities import ShotCreate, ShotUpdate, ShotResponse, MediaAssetResponse
from backend.app.agents.director_agent import director_agent
from backend.app.agents.cinematographer_agent import cinematographer_agent
from backend.app.agents.continuity_agent import continuity_agent
from backend.app.services.cost_controller import cost_controller
from backend.app.services.job_manager import job_manager, sanitize_error_message
from backend.app.services.gatekeeper import gatekeeper

router = APIRouter(prefix="/api/storyboard", tags=["storyboard"])

class GenerateShotsRequest(BaseModel):
    shot_count: Optional[int] = 4

class OptimizePromptRequest(BaseModel):
    character_anchors: Optional[str] = None

@router.get("/{project_id}", response_model=List[ShotResponse])
def list_shots(project_id: str, scene_id: Optional[str] = Query(None), db: Session = Depends(get_db)):
    query = db.query(Shot).filter(Shot.project_id == project_id)
    if scene_id:
        query = query.filter(Shot.scene_id == scene_id)
    shots = query.order_by(Shot.order_index).all()
    
    res = []
    for s in shots:
        media_url = None
        selected_version = None
        if s.video_asset_id:
            asset = db.query(MediaAsset).filter(MediaAsset.id == s.video_asset_id).first()
            if asset:
                media_url = f"/media/{asset.file_path}"
                selected_version = asset.version

        # Count total media versions available for this shot
        v_count = db.query(MediaAsset).filter(
            (MediaAsset.shot_id == s.id) | (MediaAsset.auto_matched_shot_id == s.id)
        ).count()

        res.append({
            "id": s.id,
            "scene_id": s.scene_id,
            "project_id": s.project_id,
            "shot_number": s.shot_number,
            "shot_code": s.shot_code,
            "duration_seconds": s.duration_seconds,
            "camera_angle": s.camera_angle,
            "camera_movement": s.camera_movement,
            "visual_description": s.visual_description,
            "lighting": s.lighting,
            "character_references": s.character_references,
            "video_prompt": s.video_prompt,
            "negative_prompt": s.negative_prompt,
            "seed": s.seed,
            "generation_status": s.generation_status,
            "video_asset_id": s.video_asset_id,
            "thumbnail_path": s.thumbnail_path,
            "order_index": s.order_index,
            "created_at": s.created_at,
            "media_url": media_url,
            "versions_count": v_count,
            "selected_version": selected_version
        })
    return res

@router.post("", response_model=ShotResponse, status_code=201)
def create_shot(data: ShotCreate, db: Session = Depends(get_db)):
    # Gatekeeper check: validates scene existence, cross-project consistency, and story approval
    scene = gatekeeper.verify_scene_approval(db=db, scene_id=data.scene_id, expected_project_id=data.project_id)

    last_shot = db.query(Shot).filter(Shot.scene_id == data.scene_id).order_by(Shot.order_index.desc()).first()
    next_num = (last_shot.shot_number + 1) if last_shot else 1
    next_order = (last_shot.order_index + 1) if last_shot else 1
    code = f"SC{scene.scene_number:02d}_SH{next_num:02d}"

    shot = Shot(
        scene_id=data.scene_id,
        project_id=data.project_id,
        shot_number=data.shot_number or next_num,
        shot_code=code,
        duration_seconds=data.duration_seconds or 4.0,
        camera_angle=data.camera_angle or "Eye Level",
        camera_movement=data.camera_movement or "Slow Push In",
        visual_description=data.visual_description or "",
        lighting=data.lighting or "Cinematic volumetric lighting",
        character_references=data.character_references or "[]",
        video_prompt=data.video_prompt or "",
        negative_prompt=data.negative_prompt or "blurry, low quality, distorted",
        seed=data.seed,
        generation_status=data.generation_status or "pending",
        video_asset_id=data.video_asset_id,
        thumbnail_path=data.thumbnail_path or "",
        order_index=next_order
    )
    db.add(shot)
    db.commit()
    db.refresh(shot)

    return shot

@router.post("/generate/{scene_id}", response_model=List[ShotResponse])
def generate_shots_for_scene(scene_id: str, req: GenerateShotsRequest, db: Session = Depends(get_db)):
    # Gatekeeper check: validates scene existence and active story approval with exact hash match
    scene = gatekeeper.verify_scene_approval(db=db, scene_id=scene_id)

    project = db.query(Project).filter(Project.id == scene.project_id).first()
    if not project or project.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Project not found")

    job_payload = {"scene_id": scene.id, "count": req.shot_count}

    job, action = job_manager.prepare_or_claim_job(
        db=db,
        project_id=project.id,
        job_type="storyboard_generate",
        payload=job_payload
    )

    if action == "REUSE" and job.result:
        try:
            cached = json.loads(job.result)
            cached_ids = cached.get("shot_ids", [])
            existing_shots = db.query(Shot).filter(Shot.id.in_(cached_ids)).order_by(Shot.order_index).all()
            if existing_shots and len(existing_shots) == len(cached_ids):
                return existing_shots
        except Exception:
            pass

    if action == "IN_PROGRESS":
        raise HTTPException(
            status_code=409,
            detail=f"Storyboard generation job is already in progress (job_id: {job.id}). Please wait for completion."
        )

    try:
        characters = db.query(Character).filter(Character.project_id == project.id).all()
        char_list = [{"name": c.name, "clothing": c.clothing, "facial_characteristics": c.facial_characteristics} for c in characters]

        shots_data = director_agent.breakdown_scene_to_shots(
            scene_slugline=scene.slugline,
            scene_summary=scene.summary or scene.script_content[:200],
            genre=project.genre,
            characters=char_list,
            visual_style=project.visual_style,
            shot_count=req.shot_count or 4
        )

        # Extract genuine usage metadata (Gemini token counts or offline engine attribution)
        first_shot_meta = (shots_data[0].get("_generation_metadata") if (shots_data and isinstance(shots_data[0], dict)) else None) or {}
        provider = first_shot_meta.get("provider", "offline_engine")
        model = first_shot_meta.get("model", "offline-heuristic-v1")
        input_tokens = first_shot_meta.get("input_tokens", 0)
        output_tokens = first_shot_meta.get("output_tokens", 0)
        engine_label = "Google Gemini" if provider == "gemini" else "Offline Studio Engine"
        cost_details = f"{engine_label} Director: {req.shot_count} shots breakdown for Scene {scene.scene_number}"

        # Authorize & charge with duplicate detection and accurate provider/model attribution
        cost_controller.authorize_and_charge(
            db=db,
            project_id=project.id,
            scene_id=scene.id,
            action_type="storyboard_generation",
            provider=provider,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            payload=job_payload,
            details=cost_details
        )

        created_shots = []
        base_order = db.query(Shot).filter(Shot.scene_id == scene.id).count()

        for idx, s in enumerate(shots_data):
            shot_num = idx + 1
            shot_code = f"SC{scene.scene_number:02d}_SH{shot_num:02d}"
            shot = Shot(
                scene_id=scene.id,
                project_id=project.id,
                shot_number=shot_num,
                shot_code=shot_code,
                duration_seconds=s.get("duration_seconds", 4.0),
                camera_angle=s.get("camera_angle", "Eye Level"),
                camera_movement=s.get("camera_movement", "Slow Dolly In"),
                visual_description=s.get("visual_description", ""),
                lighting=s.get("lighting", "Volumetric cinema lighting"),
                character_references=str(s.get("character_references", "[]")),
                video_prompt=s.get("video_prompt", ""),
                negative_prompt=s.get("negative_prompt", "blurry, distorted, low quality"),
                seed=s.get("seed"),
                generation_status="ready_for_flow",
                order_index=base_order + idx + 1
            )
            db.add(shot)
            created_shots.append(shot)

        db.commit()
        for s in created_shots:
            db.refresh(s)

        job_manager.complete_job(db, job.id, {"shot_ids": [s.id for s in created_shots]}, worker_id=job.worker_id, fencing_token=job.fencing_token)
        return created_shots
    except Exception as e:
        job_manager.fail_job(db, job.id, e, worker_id=job.worker_id, fencing_token=job.fencing_token)
        if isinstance(e, HTTPException):
            raise e
        raise HTTPException(status_code=500, detail=sanitize_error_message(e))

@router.patch("/{shot_id}", response_model=ShotResponse)
def update_shot(shot_id: str, data: ShotUpdate, db: Session = Depends(get_db)):
    shot = db.query(Shot).filter(Shot.id == shot_id).first()
    if not shot:
        raise HTTPException(status_code=404, detail="Shot not found")

    for k, v in data.model_dump(exclude_unset=True).items():
        setattr(shot, k, v)

    db.commit()
    db.refresh(shot)
    return shot

@router.delete("/{shot_id}")
def delete_shot(shot_id: str, db: Session = Depends(get_db)):
    shot = db.query(Shot).filter(Shot.id == shot_id).first()
    if not shot:
        raise HTTPException(status_code=404, detail="Shot not found")

    # Unlink any media assets referencing this shot
    media_assets = db.query(MediaAsset).filter(
        (MediaAsset.shot_id == shot.id) | (MediaAsset.auto_matched_shot_id == shot.id)
    ).all()
    for ma in media_assets:
        ma.shot_id = None
        ma.auto_matched_shot_id = None

    db.delete(shot)
    db.commit()
    return {"status": "deleted", "shot_id": shot_id}


@router.get("/shot/{shot_id}/versions", response_model=List[MediaAssetResponse])
def list_shot_versions(shot_id: str, db: Session = Depends(get_db)):
    """Lists all video asset versions imported for this storyboard shot."""
    shot = db.query(Shot).filter(Shot.id == shot_id).first()
    if not shot:
        raise HTTPException(status_code=404, detail="Shot not found")

    versions = db.query(MediaAsset).filter(
        (MediaAsset.shot_id == shot.id) | (MediaAsset.auto_matched_shot_id == shot.id)
    ).order_by(MediaAsset.version.asc(), MediaAsset.created_at.asc()).all()

    res = []
    for a in versions:
        res.append({
            "id": a.id,
            "project_id": a.project_id,
            "shot_id": a.shot_id,
            "version": a.version or 1,
            "status": a.status or "ready",
            "filename": a.filename,
            "file_path": a.file_path,
            "media_type": a.media_type,
            "width": a.width,
            "height": a.height,
            "duration_seconds": a.duration_seconds,
            "fps": a.fps,
            "codec": a.codec,
            "file_size_bytes": a.file_size_bytes,
            "thumbnail_path": a.thumbnail_path,
            "checksum": a.checksum,
            "auto_matched_shot_id": a.auto_matched_shot_id,
            "is_validated": a.is_validated,
            "validation_notes": a.validation_notes,
            "created_at": a.created_at,
            "url": f"/media/{a.file_path}"
        })
    return res


@router.post("/shot/{shot_id}/select-version/{media_asset_id}")
def select_shot_version(shot_id: str, media_asset_id: str, db: Session = Depends(get_db)):
    """Selects which video version is active in the shot and timeline editor."""
    shot = db.query(Shot).filter(Shot.id == shot_id).first()
    if not shot:
        raise HTTPException(status_code=404, detail="Shot not found")

    asset = db.query(MediaAsset).filter(MediaAsset.id == media_asset_id).first()
    if not asset:
        raise HTTPException(status_code=404, detail="Media asset not found")
    if asset.project_id != shot.project_id:
        raise HTTPException(status_code=400, detail="Media asset belongs to another project")

    asset.shot_id = shot.id
    shot.video_asset_id = asset.id
    shot.thumbnail_path = asset.thumbnail_path
    shot.generation_status = "imported"
    db.commit()

    return {
        "status": "success",
        "shot_id": shot.id,
        "selected_version": asset.version,
        "media_asset_id": asset.id,
        "video_url": f"/media/{asset.file_path}"
    }

@router.post("/continuity-check/{project_id}")
def run_continuity_check(project_id: str, db: Session = Depends(get_db)):
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    shots = db.query(Shot).filter(Shot.project_id == project_id).order_by(Shot.order_index).all()
    chars = db.query(Character).filter(Character.project_id == project_id).all()

    shots_data = [
        {
            "shot_code": s.shot_code,
            "lighting": s.lighting,
            "camera_angle": s.camera_angle,
            "character_references": s.character_references
        }
        for s in shots
    ]
    chars_data = [
        {"name": c.name, "clothing": c.clothing, "facial_characteristics": c.facial_characteristics}
        for c in chars
    ]

    analysis = continuity_agent.verify_project_continuity(shots=shots_data, characters=chars_data)
    return analysis

@router.post("/{shot_id}/optimize-prompt", response_model=ShotResponse)
def optimize_shot_prompt(shot_id: str, req: OptimizePromptRequest, db: Session = Depends(get_db)):
    shot = db.query(Shot).filter(Shot.id == shot_id).first()
    if not shot:
        raise HTTPException(status_code=404, detail="Shot not found")

    project = db.query(Project).filter(Project.id == shot.project_id).first()
    optimized = cinematographer_agent.optimize_flow_prompt(
        camera_angle=shot.camera_angle,
        camera_movement=shot.camera_movement,
        visual_description=shot.visual_description,
        lighting=shot.lighting,
        visual_style=project.visual_style if project else None,
        character_anchors=req.character_anchors
    )

    shot.video_prompt = optimized["video_prompt"]
    shot.negative_prompt = optimized["negative_prompt"]
    shot.generation_status = "ready_for_flow"
    db.commit()
    db.refresh(shot)
    return shot
