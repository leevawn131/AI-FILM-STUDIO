import shutil
from pathlib import Path
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from sqlalchemy.orm import Session
from pydantic import BaseModel
from backend.app.database import get_db
from backend.app.config import settings
from backend.app.models.entities import Project, Character
from backend.app.schemas.entities import CharacterCreate, CharacterUpdate, CharacterResponse
from backend.app.agents.character_agent import character_designer_agent
from backend.app.services.security import save_upload_file_securely, finalize_staged_upload, generate_secure_filename, validate_safe_path, sanitize_filename

router = APIRouter(prefix="/api/characters", tags=["characters"])

class GenerateCharacterRequest(BaseModel):
    project_id: str
    role: Optional[str] = "Protagonist"
    name: Optional[str] = None
    concept_prompt: Optional[str] = None

@router.get("/{project_id}", response_model=List[CharacterResponse])
def list_characters(project_id: str, db: Session = Depends(get_db)):
    chars = db.query(Character).filter(Character.project_id == project_id).all()
    return chars

@router.get("/detail/{character_id}", response_model=CharacterResponse)
def get_character(character_id: str, db: Session = Depends(get_db)):
    char = db.query(Character).filter(Character.id == character_id).first()
    if not char:
        raise HTTPException(status_code=404, detail="Character not found")
    return char

@router.post("", response_model=CharacterResponse, status_code=201)
def create_character(data: CharacterCreate, db: Session = Depends(get_db)):
    project = db.query(Project).filter(Project.id == data.project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    char = Character(
        project_id=data.project_id,
        name=data.name,
        role=data.role or "Protagonist",
        age=data.age or "30s",
        facial_characteristics=data.facial_characteristics or "",
        clothing=data.clothing or "",
        voice_profile=data.voice_profile or "",
        personality=data.personality or "",
        visual_prompt_tags=data.visual_prompt_tags or f"{data.name}, {data.role}",
        reference_image_path=data.reference_image_path or ""
    )
    db.add(char)
    db.commit()
    db.refresh(char)
    return char

@router.post("/ai-generate", response_model=CharacterResponse)
def generate_ai_character(data: GenerateCharacterRequest, db: Session = Depends(get_db)):
    project = db.query(Project).filter(Project.id == data.project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    profile = character_designer_agent.design_character(
        genre=project.genre,
        role_hint=data.role,
        name_hint=data.name,
        concept_prompt=data.concept_prompt
    )

    char = Character(
        project_id=data.project_id,
        name=profile["name"],
        role=profile["role"],
        age=profile["age"],
        facial_characteristics=profile["facial_characteristics"],
        clothing=profile["clothing"],
        voice_profile=profile["voice_profile"],
        personality=profile["personality"],
        visual_prompt_tags=profile["visual_prompt_tags"],
        reference_image_path=""
    )
    db.add(char)
    db.commit()
    db.refresh(char)
    return char

@router.patch("/{character_id}", response_model=CharacterResponse)
def update_character(character_id: str, data: CharacterUpdate, db: Session = Depends(get_db)):
    char = db.query(Character).filter(Character.id == character_id).first()
    if not char:
        raise HTTPException(status_code=404, detail="Character not found")

    for k, v in data.model_dump(exclude_unset=True).items():
        setattr(char, k, v)

    db.commit()
    db.refresh(char)
    return char

@router.delete("/{character_id}")
def delete_character(character_id: str, db: Session = Depends(get_db)):
    char = db.query(Character).filter(Character.id == character_id).first()
    if not char:
        raise HTTPException(status_code=404, detail="Character not found")

    db.delete(char)
    db.commit()
    return {"status": "deleted", "character_id": character_id}

@router.post("/{character_id}/upload-ref")
async def upload_character_ref(character_id: str, file: UploadFile = File(...), db: Session = Depends(get_db)):
    char = db.query(Character).filter(Character.id == character_id).first()
    if not char:
        raise HTTPException(status_code=404, detail="Character not found")

    safe_pid = sanitize_filename(char.project_id)
    dest_dir = validate_safe_path(settings.projects_dir / safe_pid / "references", settings.projects_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    max_bytes = settings.max_image_upload_size_mb * 1024 * 1024
    temp_path, checksum, size_bytes = await save_upload_file_securely(
        upload_file=file,
        dest_dir=dest_dir,
        allowed_extensions=settings.allowed_image_extensions,
        max_size_bytes=max_bytes,
        prefix=f"char_{char.id[:8]}",
        expected_type="image",
        validate_content=True,
        stage_only=True
    )

    secure_filename = generate_secure_filename(file.filename, prefix=f"char_{char.id[:8]}")
    rel_path = f"projects/{safe_pid}/references/{secure_filename}"

    old_path = None
    if char.reference_image_path:
        try:
            old_path = validate_safe_path(settings.data_dir / char.reference_image_path, settings.data_dir)
        except Exception:
            pass

    char.reference_image_path = rel_path
    try:
        db.commit()
        finalize_staged_upload(temp_path, dest_dir, secure_filename, old_path_to_replace=old_path)
    except Exception:
        db.rollback()
        if temp_path.exists():
            temp_path.unlink(missing_ok=True)
        raise

    return {"status": "uploaded", "reference_url": f"/media/{rel_path}"}
