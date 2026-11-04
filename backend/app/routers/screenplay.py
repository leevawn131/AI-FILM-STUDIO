from datetime import datetime, timezone
import json
import logging
import hashlib
import threading
from typing import List, Optional, Dict, Any
from fastapi import APIRouter, Depends, HTTPException, Body
from sqlalchemy.orm import Session
from pydantic import BaseModel
from backend.app.database import get_db
from backend.app.models.entities import Project, Scene, Character, Location, Shot, StudioJob
from backend.app.schemas.entities import SceneCreate, SceneUpdate, SceneResponse
from backend.app.agents.screenwriter_agent import screenwriter_agent
from backend.app.services.gemini_service import gemini_service
from backend.app.services.cost_controller import cost_controller
from backend.app.services.job_manager import job_manager, sanitize_error_message, JobOwnershipLostError
from backend.app.services.pipeline_orchestrator import PipelineOrchestrator, acquire_project_write_lock

logger = logging.getLogger(__name__)

CANONICAL_SCREENPLAY_FIELDS = {
    "slugline", "title", "summary", "script_content",
    "scene_number", "order_index", "estimated_duration"
}

router = APIRouter(prefix="/api/scenes", tags=["screenplay"])

_reservation_lock = threading.RLock()


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

def resolve_canonical_characters(
    db: Session,
    project_id: str,
    character_ids: Optional[List[str]] = None
) -> List[Dict[str, Any]]:
    """
    Deterministically resolves character data for canonical generation input.
    - Stable ordering: order_by(Character.created_at.asc(), Character.id.asc()).
    - If character_ids given: verifies all IDs belong to project_id.
    - If not given: takes first 2 characters in deterministic order.
    - Canonical representation includes stable ID to disambiguate identical names/roles.
    """
    if character_ids:
        chars = (
            db.query(Character)
            .filter(
                Character.id.in_(character_ids),
                Character.project_id == project_id
            )
            .order_by(Character.created_at.asc(), Character.id.asc())
            .all()
        )
        if len(chars) != len(set(character_ids)):
            raise HTTPException(
                status_code=400,
                detail="One or more character IDs are invalid or do not belong to this project."
            )
    else:
        chars = (
            db.query(Character)
            .filter(Character.project_id == project_id)
            .order_by(Character.created_at.asc(), Character.id.asc())
            .limit(2)
            .all()
        )

    return [
        {
            "id": c.id,
            "name": c.name,
            "role": c.role,
            "clothing": c.clothing,
            "facial_characteristics": c.facial_characteristics
        }
        for c in chars
    ]


def resolve_canonical_location(
    db: Session,
    project_id: str,
    location_id: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """
    Deterministically resolves location data for canonical generation input.
    - Verifies location belongs to project_id.
    - Canonical representation includes stable location ID.
    """
    if not location_id:
        return None
    loc = (
        db.query(Location)
        .filter(
            Location.id == location_id,
            Location.project_id == project_id
        )
        .first()
    )
    if not loc:
        raise HTTPException(
            status_code=400,
            detail="Location ID is invalid or does not belong to this project."
        )
    return {
        "id": loc.id,
        "name": loc.name,
        "time_of_day": loc.time_of_day,
        "lighting_setup": loc.lighting_setup
    }


def reserve_scene_number(
    db: Session,
    project_id: str,
    requested_scene_number: Optional[int] = None,
    job_id: Optional[str] = None
) -> int:
    """
    Policy A: Deterministic scene number reservation with ownership and expiry.
    - Serializes scene number reservations prior to AI execution.
    - Unexpired running/pending StudioJobs hold reservations.
    - Expired/abandoned job leases release reservations automatically.
    - Retries of the same job reuse their existing valid reservation.
    """
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    # 1. Existing committed scene numbers
    committed_nums = {
        s.scene_number
        for s in db.query(Scene.scene_number).filter(Scene.project_id == project_id).all()
    }

    # 2. Active reserved scene numbers from unexpired running/pending jobs
    active_jobs = (
        db.query(StudioJob)
        .filter(
            StudioJob.project_id == project_id,
            StudioJob.job_type == "screenplay_generate",
            StudioJob.status.in_(["RUNNING", "PENDING"])
        )
        .all()
    )

    active_reserved: Dict[int, str] = {}
    reusable_scene_num = None

    for j in active_jobs:
        if j.lease_expires_at and j.lease_expires_at <= now:
            # Abandoned reservation: lease expired
            continue
        try:
            payload = json.loads(j.payload) if isinstance(j.payload, str) else (j.payload or {})
            res_num = payload.get("reserved_scene_number")
            if res_num is not None:
                res_int = int(res_num)
                if job_id and j.id == job_id:
                    reusable_scene_num = res_int
                else:
                    active_reserved[res_int] = j.id
        except Exception:
            pass

    # If this specific job already holds a reservation, reuse it (retry safety)
    if reusable_scene_num is not None:
        return reusable_scene_num

    if requested_scene_number is not None:
        req_int = int(requested_scene_number)
        if req_int in committed_nums or req_int in active_reserved:
            raise HTTPException(
                status_code=409,
                detail=f"Scene number {req_int} is already taken or reserved by an active job."
            )
        return req_int

    # Allocate the smallest positive integer not in committed or actively reserved
    candidate = 1
    unavailable = committed_nums.union(active_reserved.keys())
    while candidate in unavailable:
        candidate += 1
    return candidate


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
    supplied to the screenwriter agent, including stable IDs and runtime configuration.
    """
    sorted_characters = sorted(
        [
            {
                "id": str(c.get("id") or ""),
                "clothing": str(c.get("clothing") or ""),
                "facial_characteristics": str(c.get("facial_characteristics") or ""),
                "name": str(c.get("name") or ""),
                "role": str(c.get("role") or "")
            }
            for c in (characters or [])
        ],
        key=lambda x: (x["id"], x["name"])
    )

    norm_location = None
    if location:
        norm_location = {
            "id": str(location.get("id") or ""),
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

    # 1. Resolve canonical characters and location deterministically
    characters_data = resolve_canonical_characters(db, data.project_id, data.character_ids)
    loc_data = resolve_canonical_location(db, data.project_id, data.location_id)

    # 2. Capture actual runtime provider/model/config snapshot
    actual_provider = "gemini" if gemini_service.has_gemini else "offline_engine"
    actual_model = gemini_service.model if gemini_service.has_gemini else "offline-studio-engine"
    generation_config = {
        "agent": "screenwriter_agent",
        "provider": actual_provider,
        "model": actual_model,
        "version": "1.0"
    }

    # Policy A: Atomically reserve scene number and prepare job under reservation lock
    with _reservation_lock:
        # 3. Base screenplay snapshot (scenes that exist at preparation time)
        base_scenes = db.query(Scene).filter(Scene.project_id == data.project_id).order_by(Scene.order_index).all()
        base_scene_ids = [s.id for s in base_scenes]
        input_screenplay_hash = PipelineOrchestrator.compute_artifact_hash(db, data.project_id, "screenplay")

        # 4. Policy A: Deterministic scene number reservation
        reserved_scene_num = reserve_scene_number(
            db=db,
            project_id=data.project_id,
            requested_scene_number=data.scene_number
        )

        canonical_input = build_canonical_generation_input(
            project_title=project.name,
            project_genre=project.genre,
            scene_number=reserved_scene_num,
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
            "reserved_scene_number": reserved_scene_num,
            "base_scene_ids": base_scene_ids,
            "input_screenplay_hash": input_screenplay_hash,
            "generation_config": generation_config,
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
            scene_number=reserved_scene_num,
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
    current_characters_data = resolve_canonical_characters(db, data.project_id, data.character_ids)
    current_loc_data = resolve_canonical_location(db, data.project_id, data.location_id)

    cur_provider = "gemini" if gemini_service.has_gemini else "offline_engine"
    cur_model = gemini_service.model if gemini_service.has_gemini else "offline-studio-engine"
    cur_config = {
        "agent": "screenwriter_agent",
        "provider": cur_provider,
        "model": cur_model,
        "version": "1.0"
    }

    reserved_num = job_payload["reserved_scene_number"]
    collision = db.query(Scene).filter(
        Scene.project_id == data.project_id,
        Scene.scene_number == reserved_num
    ).first()

    screenplay_drifted = False
    if collision is not None:
        screenplay_drifted = True
    else:
        base_ids = job_payload.get("base_scene_ids", [])
        if base_ids:
            cur_base_scenes = db.query(Scene).filter(Scene.id.in_(base_ids)).order_by(Scene.order_index).all()
            if len(cur_base_scenes) != len(base_ids):
                screenplay_drifted = True
            else:
                canonical_base = [
                    {
                        "scene_number": s.scene_number,
                        "order_index": s.order_index,
                        "slugline": s.slugline or "",
                        "title": s.title or "",
                        "summary": s.summary or "",
                        "script_content": s.script_content or "",
                        "estimated_duration": s.estimated_duration or 0
                    }
                    for s in cur_base_scenes
                ]
                cur_base_hash = hashlib.sha256(
                    json.dumps(canonical_base, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode('utf-8')
                ).hexdigest()
                if cur_base_hash != job_payload.get("input_screenplay_hash"):
                    screenplay_drifted = True

    current_canonical_input = build_canonical_generation_input(
        project_title=project.name,
        project_genre=project.genre,
        scene_number=reserved_num,
        prompt=data.prompt or "",
        characters=current_characters_data,
        location=current_loc_data,
        screenplay_hash=job_payload.get("input_screenplay_hash"),
        config=cur_config
    )
    current_generation_input_hash = compute_generation_input_hash(current_canonical_input)

    if screenplay_drifted or current_generation_input_hash != generation_input_hash:
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
        last_scene_locked = db.query(Scene).filter(Scene.project_id == data.project_id).order_by(Scene.order_index.desc()).first()
        final_order = (last_scene_locked.order_index + 1) if last_scene_locked else 1

        scene = Scene(
            project_id=data.project_id,
            scene_number=reserved_num,
            slugline=generated.get("slugline", "INT. UNKNOWN SET - NIGHT"),
            title=generated.get("title", f"Scene {reserved_num}"),
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
