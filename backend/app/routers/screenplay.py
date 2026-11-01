from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Body
from sqlalchemy.orm import Session
from pydantic import BaseModel
from backend.app.database import get_db
from backend.app.models.entities import Project, Scene, Character, Location, Shot
from backend.app.schemas.entities import SceneCreate, SceneUpdate, SceneResponse
from backend.app.agents.screenwriter_agent import screenwriter_agent
from backend.app.services.cost_controller import cost_controller

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
    project = db.query(Project).filter(Project.id == data.project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

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
    db.commit()
    db.refresh(scene)

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

@router.post("/generate", response_model=SceneResponse)
def generate_ai_scene(data: GenerateSceneRequest, db: Session = Depends(get_db)):
    project = db.query(Project).filter(Project.id == data.project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    # Authorize & charge with duplicate detection
    cost_controller.authorize_and_charge(
        db=db,
        project_id=data.project_id,
        action_type="script_generation",
        payload={"project_id": data.project_id, "prompt": data.prompt},
        details=f"AI Screenwriter: {data.prompt[:50] if data.prompt else 'Dramatic sequence'}"
    )

    last_scene = db.query(Scene).filter(Scene.project_id == data.project_id).order_by(Scene.order_index.desc()).first()
    next_num = (last_scene.scene_number + 1) if last_scene else 1
    next_order = (last_scene.order_index + 1) if last_scene else 1

    # Gather character details
    characters_data = []
    if data.character_ids:
        chars = db.query(Character).filter(Character.id.in_(data.character_ids)).all()
        for c in chars:
            characters_data.append({"name": c.name, "role": c.role, "clothing": c.clothing, "facial_characteristics": c.facial_characteristics})
    else:
        chars = db.query(Character).filter(Character.project_id == data.project_id).limit(2).all()
        for c in chars:
            characters_data.append({"name": c.name, "role": c.role, "clothing": c.clothing, "facial_characteristics": c.facial_characteristics})

    # Location details
    loc_data = None
    if data.location_id:
        loc = db.query(Location).filter(Location.id == data.location_id).first()
        if loc:
            loc_data = {"name": loc.name, "time_of_day": loc.time_of_day, "lighting_setup": loc.lighting_setup}

    generated = screenwriter_agent.write_scene(
        project_title=project.name,
        genre=project.genre,
        scene_number=next_num,
        prompt=data.prompt,
        characters=characters_data,
        location=loc_data
    )

    scene = Scene(
        project_id=data.project_id,
        scene_number=next_num,
        slugline=generated.get("slugline", "INT. UNKNOWN SET - NIGHT"),
        title=generated.get("title", f"Scene {next_num}"),
        summary=generated.get("summary", ""),
        script_content=generated.get("script_content", ""),
        location_id=data.location_id,
        estimated_duration=generated.get("estimated_duration", 20),
        budget_limit=25.0,
        order_index=next_order
    )
    db.add(scene)
    db.commit()
    db.refresh(scene)

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

@router.patch("/{scene_id}", response_model=SceneResponse)
def update_scene(scene_id: str, data: SceneUpdate, db: Session = Depends(get_db)):
    scene = db.query(Scene).filter(Scene.id == scene_id).first()
    if not scene:
        raise HTTPException(status_code=404, detail="Scene not found")

    for k, v in data.model_dump(exclude_unset=True).items():
        setattr(scene, k, v)

    db.commit()
    db.refresh(scene)
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

    db.delete(scene)
    db.commit()
    return {"status": "deleted", "scene_id": scene_id}

@router.post("/{scene_id}/refine")
def refine_scene(scene_id: str, req: RefineSceneRequest, db: Session = Depends(get_db)):
    scene = db.query(Scene).filter(Scene.id == scene_id).first()
    if not scene:
        raise HTTPException(status_code=404, detail="Scene not found")

    refined = screenwriter_agent.refine_scene(scene.script_content or "", req.direction)
    scene.script_content = refined
    db.commit()
    return {"scene_id": scene.id, "refined_script": refined}
