import json
import logging
import hashlib
from typing import List, Optional, Dict, Any
from fastapi import APIRouter, Depends, HTTPException, Body
from sqlalchemy.orm import Session
from pydantic import BaseModel
from backend.app.database import get_db
from backend.app.models.entities import Project, Scene, Character, Location, Shot
from backend.app.schemas.entities import SceneCreate, SceneUpdate, SceneResponse
from backend.app.agents.screenwriter_agent import screenwriter_agent
from backend.app.services.cost_controller import cost_controller
from backend.app.services.job_manager import job_manager, sanitize_error_message, JobOwnershipLostError
from backend.app.services.pipeline_orchestrator import PipelineOrchestrator, acquire_project_write_lock

logger = logging.getLogger(__name__)

CANONICAL_SCREENPLAY_FIELDS = {
    "slugline", "title", "summary", "script_content",
    "scene_number", "order_index", "estimated_duration"
}

router = APIRouter(prefix="/api/scenes", tags=["screenplay"])


class GenerateSceneRequest(BaseModel):
    project_id: str
    prompt: Optional[str] = ""
    scene_number: Optional[int] = None
    location_id: Optional[str] = None
    character_ids: Optional[List[str]] = None

class RefineSceneRequest(BaseModel):
    direction: str

@router.get("/{project_id}", response_model=List[SceneResponse])
def list_scenes(project_id: str, db: Session = Depends(get_db)):
    scenes = db.query(Scene).filter(Scene.project_id == project_id).order_by(Scene.order_index).all()
    res = []
    for s in scenes:
        shots_count = db.query(Shot).filter(Shot.scene_id == s.id).count()
        s_dict = {
            "id": s.id,
            "project_id": s.project_id,
            "scene_number": s.scene_number,
            "slugline": s.slugline,
            "title": s.title,
            "summary": s.summary,
            "script_content": s.script_content,
            "location_id": s.location_id,
            "estimated_duration": s.estimated_duration,
            "budget_limit": s.budget_limit,
            "order_index": s.order_index,
            "created_at": s.created_at,
            "shots_count": shots_count
        }
        res.append(s_dict)
    return res

@router.get("/detail/{scene_id}", response_model=SceneResponse)
def get_scene(scene_id: str, db: Session = Depends(get_db)):
    s = db.query(Scene).filter(Scene.id == scene_id).first()
    if not s:
        raise HTTPException(status_code=404, detail="Scene not found")
    shots_count = db.query(Shot).filter(Shot.scene_id == s.id).count()
    return {
        "id": s.id,
        "project_id": s.project_id,
        "scene_number": s.scene_number,
        "slugline": s.slugline,
        "title": s.title,
        "summary": s.summary,
        "script_content": s.script_content,
        "location_id": s.location_id,
        "estimated_duration": s.estimated_duration,
        "budget_limit": s.budget_limit,
        "order_index": s.order_index,
        "created_at": s.created_at,
        "shots_count": shots_count
    }

@router.post("", response_model=SceneResponse, status_code=201)
def create_scene(data: SceneCreate, db: Session = Depends(get_db)):
    project = acquire_project_write_lock(db, data.project_id)
    if not project or project.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Project not found")

    committed = False
    try:
        last_scene = db.query(Scene).filter(Scene.project_id == data.project_id).order_by(Scene.order_index.desc()).first()
        next_order = (last_scene.order_index + 1) if last_scene else 1
        next_num = (last_scene.scene_number + 1) if last_scene else 1

        scene = Scene(
            project_id=data.project_id,
            scene_number=data.scene_number or next_num,
            slugline=data.slugline or "INT. NEW SET - DAY",
            title=data.title or f"Scene {next_num}",
            summary=data.summary or "",
            script_content=data.script_content or "",
            location_id=data.location_id,
            estimated_duration=data.estimated_duration or 15,
            budget_limit=data.budget_limit or 20.0,
            order_index=next_order
        )
        db.add(scene)
        # Atomically invalidate downstream approvals/stages in the same transaction
        PipelineOrchestrator.invalidate_downstream_stages(
            db=db, project_id=data.project_id, changed_stage="screenplay", commit=False
        )
        db.commit()
        db.refresh(scene)
    except Exception:
        db.rollback()
        raise

    return {
        "id": scene.id,
        "project_id": scene.project_id,
        "scene_number": scene.scene_number,
        "slugline": scene.slugline,
        "title": scene.title,
        "summary": scene.summary,
        "script_content": scene.script_content,
        "location_id": scene.location_id,
        "estimated_duration": scene.estimated_duration,
        "budget_limit": scene.budget_limit,
        "order_index": scene.order_index,
        "created_at": scene.created_at,
        "shots_count": 0
    }

def build_canonical_generation_input(
    project_title: str,
    project_genre: str,
    scene_number: int,
    prompt: str,
    characters: List[Dict[str, Any]],
    location: Optional[Dict[str, Any]],
    screenplay_hash: Optional[str],
    config: Optional[Dict[str, Any]] = None,
    schema_version: str = "v1"
) -> Dict[str, Any]:
    """
    Constructs a deterministic canonical representation of all actual inputs
    supplied to the screenwriter agent.
    """
    sorted_characters = sorted(
        [
            {
                "clothing": str(c.get("clothing") or ""),
                "facial_characteristics": str(c.get("facial_characteristics") or ""),
                "name": str(c.get("name") or ""),
                "role": str(c.get("role") or "")
            }
            for c in (characters or [])
        ],
        key=lambda x: (x["name"], x["role"])
    )

    norm_location = None
    if location:
        norm_location = {
            "lighting_setup": str(location.get("lighting_setup") or ""),
            "name": str(location.get("name") or ""),
            "time_of_day": str(location.get("time_of_day") or "")
        }

    return {
        "schema_version": schema_version,
        "project_title": str(project_title or ""),
        "project_genre": str(project_genre or ""),
        "scene_number": int(scene_number),
        "prompt": str(prompt or ""),
        "characters": sorted_characters,
        "location": norm_location,
        "screenplay_hash": str(screenplay_hash or ""),
        "config": config or {}
    }


def compute_generation_input_hash(canonical_input: Dict[str, Any]) -> str:
    """Computes a SHA-256 hash from canonical JSON representation with UTF-8 encoding."""
    raw = json.dumps(canonical_input, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@router.post("/generate", response_model=SceneResponse)
def generate_ai_scene(data: GenerateSceneRequest, db: Session = Depends(get_db)):
    # -------------------------------------------------------------
    # PHASE A: PREPARE (No project write lock held during execution)
    # -------------------------------------------------------------
    project = db.query(Project).filter(Project.id == data.project_id).first()
    if not project or project.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Project not found")

    # Validate character_ids: must belong to this project
    characters_data = []
    if data.character_ids:
        chars = db.query(Character).filter(
            Character.id.in_(data.character_ids),
            Character.project_id == data.project_id
        ).all()
        if len(chars) != len(set(data.character_ids)):
            raise HTTPException(
                status_code=400,
                detail="One or more character IDs are invalid or do not belong to this project."
            )
        for c in chars:
            characters_data.append({
                "name": c.name, "role": c.role, "clothing": c.clothing,
                "facial_characteristics": c.facial_characteristics
            })
    else:
        chars = db.query(Character).filter(Character.project_id == data.project_id).limit(2).all()
        for c in chars:
            characters_data.append({
                "name": c.name, "role": c.role, "clothing": c.clothing,
                "facial_characteristics": c.facial_characteristics
            })

    # Validate location_id: must belong to this project
    loc_data = None
    if data.location_id:
        loc = db.query(Location).filter(
            Location.id == data.location_id,
            Location.project_id == data.project_id
        ).first()
        if not loc:
            raise HTTPException(
                status_code=400,
                detail="Location ID is invalid or does not belong to this project."
            )
        loc_data = {"name": loc.name, "time_of_day": loc.time_of_day, "lighting_setup": loc.lighting_setup}

    # Read input snapshot: canonical screenplay hash, scene numbering, and config
    input_screenplay_hash = PipelineOrchestrator.compute_artifact_hash(db, data.project_id, "screenplay")
    last_scene = db.query(Scene).filter(Scene.project_id == data.project_id).order_by(Scene.order_index.desc()).first()
    next_num = (last_scene.scene_number + 1) if last_scene else 1

    generation_config = {"agent": "screenwriter_agent", "version": "1.0"}
    canonical_input = build_canonical_generation_input(
        project_title=project.name,
        project_genre=project.genre,
        scene_number=next_num,
        prompt=data.prompt or "",
        characters=characters_data,
        location=loc_data,
        screenplay_hash=input_screenplay_hash,
        config=generation_config
    )
    generation_input_hash = compute_generation_input_hash(canonical_input)

    job_payload = {
        "project_id": data.project_id,
        "prompt": data.prompt,
        "character_ids": data.character_ids,
        "location_id": data.location_id,
        "input_screenplay_hash": input_screenplay_hash,
        "generation_input_hash": generation_input_hash,
        "canonical_input": canonical_input
    }

    job, action = job_manager.prepare_or_claim_job(
        db=db,
        project_id=data.project_id,
        job_type="screenplay_generate",
        payload=job_payload
    )

    if action == "REUSE" and job.result:
        try:
            cached_res = json.loads(job.result)
            existing_scene = db.query(Scene).filter(Scene.id == cached_res.get("id")).first()
            if existing_scene:
                return existing_scene
        except Exception:
            pass

    if action == "IN_PROGRESS":
        raise HTTPException(
            status_code=409,
            detail=f"Screenplay generation job is already in progress (job_id: {job.id}). Please wait for completion."
        )

    # -------------------------------------------------------------
    # PHASE B: AI EXECUTION (No lock held, no DB transaction open)
    # -------------------------------------------------------------
    try:
        generated = screenwriter_agent.write_scene(
            project_title=project.name,
            genre=project.genre,
            scene_number=next_num,
            prompt=data.prompt,
            characters=characters_data,
            location=loc_data
        )
    except Exception as ai_err:
        try:
            job_manager.fail_job(
                db=db, job_id=job.id, error=ai_err,
                worker_id=job.worker_id, fencing_token=job.fencing_token, commit=True
            )
        except Exception as fail_e:
            logger.error(f"Failed to record job failure: {fail_e}")
        if isinstance(ai_err, HTTPException):
            raise ai_err
        raise HTTPException(status_code=500, detail=f"AI generation failed: {sanitize_error_message(ai_err)}") from ai_err

    # Assert job ownership before charging/finalizing (check lease has not expired or been reclaimed)
    try:
        job_manager.assert_ownership(
            db=db, job_id=job.id, worker_id=job.worker_id, fencing_token=job.fencing_token
        )
    except JobOwnershipLostError as lease_lost_err:
        logger.warning(
            f"Worker '{job.worker_id}' lost ownership of job '{job.id}' during AI execution. "
            f"Aborting without charging or modifying job."
        )
        raise HTTPException(
            status_code=409,
            detail=f"Job lease expired during AI generation: {sanitize_error_message(lease_lost_err)}"
        )

    # Extract usage metadata
    gen_meta = (generated.get("_generation_metadata") if isinstance(generated, dict) else None) or {}
    provider = gen_meta.get("provider", "offline_engine")
    model = gen_meta.get("model", "offline-heuristic-v1")
    input_tokens = gen_meta.get("input_tokens", 0)
    output_tokens = gen_meta.get("output_tokens", 0)
    engine_label = "Google Gemini" if provider == "gemini" else "Offline Studio Engine"
    cost_details = f"{engine_label} Screenwriter: {data.prompt[:50] if data.prompt else 'Dramatic sequence'}"

    # Record charge idempotently in cost ledger (separately committed)
    cost_rec = None
    try:
        cost_rec = cost_controller.authorize_and_charge(
            db=db,
            project_id=data.project_id,
            action_type="script_generation",
            provider=provider,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            payload=job_payload,
            details=cost_details
        )
    except HTTPException as budget_err:
        try:
            job_manager.fail_job(
                db=db, job_id=job.id, error=budget_err,
                worker_id=job.worker_id, fencing_token=job.fencing_token, commit=True
            )
        except Exception as fail_e:
            logger.error(f"Failed to record job failure: {fail_e}")
        raise budget_err
    except Exception as charge_err:
        try:
            job_manager.fail_job(
                db=db, job_id=job.id, error=charge_err,
                worker_id=job.worker_id, fencing_token=job.fencing_token, commit=True
            )
        except Exception as fail_e:
            logger.error(f"Failed to record job failure: {fail_e}")
        raise HTTPException(status_code=500, detail=f"Cost authorization failed: {sanitize_error_message(charge_err)}") from charge_err

    # -------------------------------------------------------------
    # PHASE C: FINALIZE (Acquire project write lock, verify, commit)
    # -------------------------------------------------------------
    project = acquire_project_write_lock(db, data.project_id)
    if not project or project.deleted_at is not None:
        db.rollback()
        if cost_rec:
            cost_controller.record_compensation(db, data.project_id, cost_rec, reason="project_deleted")
        try:
            job_manager.fail_job(
                db=db, job_id=job.id, error="Project was deleted during scene generation",
                worker_id=job.worker_id, fencing_token=job.fencing_token, commit=True
            )
        except Exception as fail_e:
            logger.error(f"Failed to record job failure: {fail_e}")
        raise HTTPException(status_code=404, detail="Project not found or deleted")

    # Assert job ownership again under write lock
    try:
        job_manager.assert_ownership(
            db=db, job_id=job.id, worker_id=job.worker_id, fencing_token=job.fencing_token
        )
    except JobOwnershipLostError as lock_lease_err:
        db.rollback()
        if cost_rec:
            cost_controller.record_compensation(db, data.project_id, cost_rec, reason="ownership_lost_before_finalize")
        logger.warning(f"Worker '{job.worker_id}' lost ownership before finalize on job '{job.id}'.")
        raise HTTPException(
            status_code=409,
            detail=f"Job lease lost: {sanitize_error_message(lock_lease_err)}"
        )

    # Re-read all canonical inputs under write lock to detect any input drift
    last_scene_locked = db.query(Scene).filter(Scene.project_id == data.project_id).order_by(Scene.order_index.desc()).first()
    current_next_num = (last_scene_locked.scene_number + 1) if last_scene_locked else 1

    current_characters_data = []
    if data.character_ids:
        cur_chars = db.query(Character).filter(
            Character.id.in_(data.character_ids),
            Character.project_id == data.project_id
        ).all()
        for c in cur_chars:
            current_characters_data.append({
                "name": c.name, "role": c.role, "clothing": c.clothing,
                "facial_characteristics": c.facial_characteristics
            })
    else:
        cur_chars = db.query(Character).filter(Character.project_id == data.project_id).limit(2).all()
        for c in cur_chars:
            current_characters_data.append({
                "name": c.name, "role": c.role, "clothing": c.clothing,
                "facial_characteristics": c.facial_characteristics
            })

    current_loc_data = None
    if data.location_id:
        cur_loc = db.query(Location).filter(
            Location.id == data.location_id,
            Location.project_id == data.project_id
        ).first()
        if cur_loc:
            current_loc_data = {
                "name": cur_loc.name,
                "time_of_day": cur_loc.time_of_day,
                "lighting_setup": cur_loc.lighting_setup
            }

    current_screenplay_hash = PipelineOrchestrator.compute_artifact_hash(db, data.project_id, "screenplay")

    current_canonical_input = build_canonical_generation_input(
        project_title=project.name,
        project_genre=project.genre,
        scene_number=current_next_num,
        prompt=data.prompt or "",
        characters=current_characters_data,
        location=current_loc_data,
        screenplay_hash=current_screenplay_hash,
        config=generation_config
    )
    current_generation_input_hash = compute_generation_input_hash(current_canonical_input)

    if current_generation_input_hash != generation_input_hash:
        db.rollback()
        if cost_rec:
            cost_controller.record_compensation(
                db=db, project_id=data.project_id, original_record=cost_rec,
                reason="canonical_generation_input_drift"
            )
        try:
            job_manager.fail_job(
                db=db, job_id=job.id, error="Generation input snapshot drifted during AI execution",
                worker_id=job.worker_id, fencing_token=job.fencing_token, commit=True
            )
        except Exception as fail_e:
            logger.error(f"Failed to record job failure: {fail_e}")
        raise HTTPException(
            status_code=409,
            detail="Generation input snapshot drifted (project, characters, location, prompt or screenplay modified). Generation discarded."
        )

    committed = False
    try:
        final_order = (last_scene_locked.order_index + 1) if last_scene_locked else 1

        scene = Scene(
            project_id=data.project_id,
            scene_number=current_next_num,
            slugline=generated.get("slugline", "INT. UNKNOWN SET - NIGHT"),
            title=generated.get("title", f"Scene {current_next_num}"),
            summary=generated.get("summary", ""),
            script_content=generated.get("script_content", ""),
            location_id=data.location_id,
            estimated_duration=generated.get("estimated_duration", 20),
            budget_limit=25.0,
            order_index=final_order
        )
        db.add(scene)
        PipelineOrchestrator.invalidate_downstream_stages(
            db=db, project_id=data.project_id, changed_stage="screenplay", commit=False
        )
        job_manager.complete_job(
            db, job.id, {"id": scene.id, "scene_number": scene.scene_number},
            worker_id=job.worker_id, fencing_token=job.fencing_token, commit=False
        )
        db.commit()
        committed = True
        db.refresh(scene)
        return scene
    except Exception as fin_err:
        if not committed:
            db.rollback()
            if cost_rec:
                try:
                    cost_controller.record_compensation(
                        db=db, project_id=data.project_id, original_record=cost_rec,
                        reason=f"final_commit_failure: {sanitize_error_message(fin_err)}"
                    )
                except Exception as comp_e:
                    logger.error(f"Failed to record compensation on final commit failure: {comp_e}")
            if not isinstance(fin_err, JobOwnershipLostError):
                try:
                    job_manager.fail_job(
                        db, job.id, fin_err,
                        worker_id=job.worker_id, fencing_token=job.fencing_token, commit=True
                    )
                except Exception as fail_e:
                    logger.error(f"Failed to record job failure for job {job.id}: {fail_e}")
        if isinstance(fin_err, HTTPException):
            raise fin_err
        raise HTTPException(status_code=500, detail=sanitize_error_message(fin_err)) from fin_err

@router.patch("/{scene_id}", response_model=SceneResponse)
def update_scene(scene_id: str, data: SceneUpdate, db: Session = Depends(get_db)):
    scene = db.query(Scene).filter(Scene.id == scene_id).first()
    if not scene:
        raise HTTPException(status_code=404, detail="Scene not found")
    project = acquire_project_write_lock(db, scene.project_id)
    if not project or project.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Project not found")

    try:
        changed_canonical = False
        for k, v in data.model_dump(exclude_unset=True).items():
            if k in CANONICAL_SCREENPLAY_FIELDS and getattr(scene, k) != v:
                changed_canonical = True
            setattr(scene, k, v)

        # Invalidate downstream stages only when canonical screenplay fields genuinely change
        if changed_canonical:
            PipelineOrchestrator.invalidate_downstream_stages(
                db=db, project_id=scene.project_id, changed_stage="screenplay", commit=False
            )

        db.commit()
        db.refresh(scene)
    except Exception:
        db.rollback()
        raise

    shots_count = db.query(Shot).filter(Shot.scene_id == scene.id).count()

    return {
        "id": scene.id,
        "project_id": scene.project_id,
        "scene_number": scene.scene_number,
        "slugline": scene.slugline,
        "title": scene.title,
        "summary": scene.summary,
        "script_content": scene.script_content,
        "location_id": scene.location_id,
        "estimated_duration": scene.estimated_duration,
        "budget_limit": scene.budget_limit,
        "order_index": scene.order_index,
        "created_at": scene.created_at,
        "shots_count": shots_count
    }

@router.delete("/{scene_id}")
def delete_scene(scene_id: str, db: Session = Depends(get_db)):
    scene = db.query(Scene).filter(Scene.id == scene_id).first()
    if not scene:
        raise HTTPException(status_code=404, detail="Scene not found")
    project = acquire_project_write_lock(db, scene.project_id)
    if not project or project.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Project not found")

    try:
        project_id = scene.project_id
        db.delete(scene)
        PipelineOrchestrator.invalidate_downstream_stages(
            db=db, project_id=project_id, changed_stage="screenplay", commit=False
        )
        db.commit()
    except Exception:
        db.rollback()
        raise

    return {"status": "deleted", "scene_id": scene_id}

@router.post("/{scene_id}/refine")
def refine_scene(scene_id: str, req: RefineSceneRequest, db: Session = Depends(get_db)):
    scene = db.query(Scene).filter(Scene.id == scene_id).first()
    if not scene:
        raise HTTPException(status_code=404, detail="Scene not found")
    project = acquire_project_write_lock(db, scene.project_id)
    if not project or project.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Project not found")

    old_content = scene.script_content or ""
    refined = screenwriter_agent.refine_scene(old_content, req.direction)
    if refined != old_content:
        try:
            scene.script_content = refined
            PipelineOrchestrator.invalidate_downstream_stages(
                db=db, project_id=scene.project_id, changed_stage="screenplay", commit=False
            )
            db.commit()
        except Exception:
            db.rollback()
            raise
    return {"scene_id": scene.id, "refined_script": refined}
