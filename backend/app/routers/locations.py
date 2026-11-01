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

    dest_dir = settings.projects_dir / loc.project_id / "references"
    dest_dir.mkdir(parents=True, exist_ok=True)

    file_ext = Path(file.filename or "ref.jpg").suffix or ".jpg"
    dest_path = dest_dir / f"loc_{loc.id}{file_ext}"

    with open(dest_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    rel_path = f"projects/{loc.project_id}/references/loc_{loc.id}{file_ext}"
    loc.reference_image_path = rel_path
    db.commit()

    return {"status": "uploaded", "reference_url": f"/media/{rel_path}"}
