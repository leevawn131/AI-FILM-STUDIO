import shutil
import random
from pathlib import Path
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from sqlalchemy.orm import Session
from pydantic import BaseModel
from backend.app.database import get_db
from backend.app.config import settings
from backend.app.models.entities import Project, Location
from backend.app.schemas.entities import LocationCreate, LocationUpdate, LocationResponse
from backend.app.services.security import save_upload_file_securely, finalize_staged_upload, generate_secure_filename, validate_safe_path, sanitize_filename
from backend.app.services.upload_manager import upload_manager
from backend.app.services.ai_provider import get_ai_provider
from backend.app.services.structured_output import LocationOutput

router = APIRouter(prefix="/api/locations", tags=["locations"])

class GenerateLocationRequest(BaseModel):
    project_id: str
    name_hint: Optional[str] = None
    time_of_day: Optional[str] = "Night"
    atmosphere_prompt: Optional[str] = None

LOCATION_PRESETS = [
    {
        "name": "Orbital Relay Station Sol-4",
        "description": "High-altitude atmospheric orbital dock with panoramic vacuum view of Earth's terminator line.",
        "weather": "Orbital hard vacuum, micro-meteorite static discharge",
        "lighting": "Harsh unshielded solar flare backlight contrasting deep obsidian shadows",
        "tags": "orbital space station, earth terminator line in background, realistic NASA aesthetics, volumetric sunbeam"
    },
    {
        "name": "Neo-Kowloon Lower Promenade",
        "description": "Multi-tier subterranean marketplace where steam exhausts collide with towering neon holograms.",
        "weather": "Perpetual condensation drizzle, humid vapor clouds",
        "lighting": "Vibrant emerald and fuchsia neon signage reflecting off wet tile pavement",
        "tags": "cyberpunk night market, neon rain puddles, dense crowd silhouettes, blade runner 2049 aesthetic"
    },
    {
        "name": "Sub-Glacial Bunker Vault",
        "description": "Decommissioned Cold War cryogenic archive encased inside a hollowed Greenland ice shelf.",
        "weather": "Bitter arctic blizzards howling through external pressure gates",
        "lighting": "Single emergency red beacon pulsing over blue-tinted solid ice walls",
        "tags": "arctic subterranean bunker, translucent glacial ice walls, emergency red beacon, cold atmospheric frost"
    }
]

@router.get("/{project_id}", response_model=List[LocationResponse])
def list_locations(project_id: str, db: Session = Depends(get_db)):
    locs = db.query(Location).filter(Location.project_id == project_id).all()
    return locs

@router.get("/detail/{location_id}", response_model=LocationResponse)
def get_location(location_id: str, db: Session = Depends(get_db)):
    loc = db.query(Location).filter(Location.id == location_id).first()
    if not loc:
        raise HTTPException(status_code=404, detail="Location not found")
    return loc

@router.post("", response_model=LocationResponse, status_code=201)
def create_location(data: LocationCreate, db: Session = Depends(get_db)):
    project = db.query(Project).filter(Project.id == data.project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    loc = Location(
        project_id=data.project_id,
        name=data.name,
        description=data.description or "",
        time_of_day=data.time_of_day or "Night",
        weather=data.weather or "Clear",
        lighting_setup=data.lighting_setup or "High contrast cinematic lighting with rim highlights",
        visual_prompt_tags=data.visual_prompt_tags or f"{data.name}, {data.time_of_day}",
        reference_image_path=data.reference_image_path or ""
    )
    db.add(loc)
    db.commit()
    db.refresh(loc)
    return loc

@router.post("/ai-generate", response_model=LocationResponse)
def generate_ai_location(data: GenerateLocationRequest, db: Session = Depends(get_db)):
    project = db.query(Project).filter(Project.id == data.project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    provider = get_ai_provider()
    user_prompt = (
        f"Project Title: {project.name}\n"
        f"Genre: {project.genre}\n"
        f"Visual Style: {project.visual_style}\n"
        f"Location Name Hint: {data.name_hint or 'Atmospheric Cinematic Set'}\n"
        f"Time of Day: {data.time_of_day or 'Night'}\n"
        f"Atmospheric Direction: {data.atmosphere_prompt or 'Deep atmospheric visual depth and evocative lighting.'}\n"
        "Design a cinematic film location profile with detailed description, weather, lighting setup, and visual prompt tags."
    )

    try:
        loc_data, meta = provider.generate_structured(
            prompt=user_prompt,
            schema=LocationOutput,
            system_prompt="You are a master Hollywood Production Designer and Art Director. Create visually rich, atmospheric location concepts."
        )
        loc = Location(
            project_id=data.project_id,
            name=loc_data.name,
            description=loc_data.description,
            time_of_day=loc_data.time_of_day,
            weather=loc_data.weather,
            lighting_setup=loc_data.lighting_setup,
            visual_prompt_tags=loc_data.visual_prompt_tags,
            reference_image_path=""
        )
    except Exception:
        # Graceful procedural fallback
        tmpl = random.choice(LOCATION_PRESETS)
        name = data.name_hint or tmpl["name"]
        time_of_day = data.time_of_day or "Night"
        desc = tmpl["description"]
        if data.atmosphere_prompt:
            desc += f" Context: {data.atmosphere_prompt}"

        loc = Location(
            project_id=data.project_id,
            name=name,
            description=desc,
            time_of_day=time_of_day,
            weather=tmpl["weather"],
            lighting_setup=tmpl["lighting"],
            visual_prompt_tags=f"{name}, {time_of_day}, {tmpl['tags']}",
            reference_image_path=""
        )

    db.add(loc)
    db.commit()
    db.refresh(loc)
    return loc

@router.patch("/{location_id}", response_model=LocationResponse)
def update_location(location_id: str, data: LocationUpdate, db: Session = Depends(get_db)):
    loc = db.query(Location).filter(Location.id == location_id).first()
    if not loc:
        raise HTTPException(status_code=404, detail="Location not found")

    for k, v in data.model_dump(exclude_unset=True).items():
        setattr(loc, k, v)

    db.commit()
    db.refresh(loc)
    return loc

@router.delete("/{location_id}")
def delete_location(location_id: str, db: Session = Depends(get_db)):
    loc = db.query(Location).filter(Location.id == location_id).first()
    if not loc:
        raise HTTPException(status_code=404, detail="Location not found")

    db.delete(loc)
    db.commit()
    return {"status": "deleted", "location_id": location_id}

@router.post("/{location_id}/upload-ref")
async def upload_location_ref(location_id: str, file: UploadFile = File(...), db: Session = Depends(get_db)):
    loc = db.query(Location).filter(Location.id == location_id).first()
    if not loc:
        raise HTTPException(status_code=404, detail="Location not found")

    safe_pid = sanitize_filename(loc.project_id)
    dest_dir = validate_safe_path(settings.projects_dir / safe_pid / "references", settings.projects_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    max_bytes = settings.max_image_upload_size_mb * 1024 * 1024
    temp_path, checksum, size_bytes = await save_upload_file_securely(
        upload_file=file,
        dest_dir=dest_dir,
        allowed_extensions=settings.allowed_image_extensions,
        max_size_bytes=max_bytes,
        prefix=f"loc_{loc.id[:8]}",
        expected_type="image",
        validate_content=True,
        stage_only=True
    )

    secure_filename = generate_secure_filename(file.filename, prefix=f"loc_{loc.id[:8]}")
    rel_path = f"projects/{safe_pid}/references/{secure_filename}"

    old_path = None
    if loc.reference_image_path:
        try:
            old_path = validate_safe_path(settings.data_dir / loc.reference_image_path, settings.data_dir)
        except Exception:
            pass

    final_dest_path = dest_dir / secure_filename
    upload_op = upload_manager.start_staged_upload(
        db=db,
        project_id=loc.project_id,
        entity_type="location_reference",
        entity_id=loc.id,
        temp_path=temp_path,
        destination_path=final_dest_path,
        old_asset_path=old_path,
        checksum=checksum,
        file_size_bytes=size_bytes
    )

    try:
        loc.reference_image_path = rel_path
        upload_op = upload_manager.mark_db_committed(db, upload_op.id)
        db.commit()
    except Exception as e:
        db.rollback()
        upload_manager.fail_upload(
            db=db,
            op_id=upload_op.id,
            error=e,
            worker_id=upload_op.worker_id,
            fencing_token=upload_op.fencing_token
        )
        raise

    try:
        upload_manager.finalize_upload(
            db=db,
            op_id=upload_op.id,
            worker_id=upload_op.worker_id,
            fencing_token=upload_op.fencing_token
        )
    except Exception as e:
        upload_manager.fail_upload(
            db=db,
            op_id=upload_op.id,
            error=e,
            worker_id=upload_op.worker_id,
            fencing_token=upload_op.fencing_token
        )
        raise

    return {"status": "uploaded", "reference_url": f"/media/{rel_path}"}
