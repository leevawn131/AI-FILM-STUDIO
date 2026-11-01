import shutil
from pathlib import Path
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Query
from sqlalchemy.orm import Session
from pydantic import BaseModel
from backend.app.database import get_db
from backend.app.config import settings
from backend.app.models.entities import Project, AudioTrack
from backend.app.schemas.entities import AudioTrackCreate, AudioTrackUpdate, AudioTrackResponse
from backend.app.services.audio_service import audio_service
from backend.app.services.ffmpeg_service import ffmpeg_service
from backend.app.services.security import save_upload_file_securely, validate_safe_path, sanitize_filename

router = APIRouter(prefix="/api/audio", tags=["audio"])

class GenerateBgmRequest(BaseModel):
    title: Optional[str] = "Atmospheric Synth Score"
    duration: Optional[float] = 20.0
    mood: Optional[str] = "cyberpunk"

class GenerateVoiceoverRequest(BaseModel):
    character_name: Optional[str] = "REN SHINODA"
    dialogue_text: str
    duration: Optional[float] = 4.0

@router.get("/{project_id}", response_model=List[AudioTrackResponse])
def list_audio_tracks(project_id: str, db: Session = Depends(get_db)):
    tracks = db.query(AudioTrack).filter(AudioTrack.project_id == project_id).all()
    res = []
    for t in tracks:
        res.append({
            "id": t.id,
            "project_id": t.project_id,
            "track_type": t.track_type,
            "title": t.title,
            "file_path": t.file_path,
            "duration_seconds": t.duration_seconds,
            "start_time_seconds": t.start_time_seconds,
            "volume_level": t.volume_level,
            "ducking_enabled": t.ducking_enabled,
            "ducking_level": t.ducking_level,
            "scene_id": t.scene_id,
            "shot_id": t.shot_id,
            "created_at": t.created_at,
            "url": f"/media/{t.file_path}"
        })
    return res

@router.post("/upload/{project_id}", response_model=AudioTrackResponse)
async def upload_audio_track(
    project_id: str,
    track_type: str = Query("music"),
    title: str = Query("Uploaded Track"),
    file: UploadFile = File(...),
    db: Session = Depends(get_db)
):
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    safe_pid = sanitize_filename(project_id)
    dest_dir = validate_safe_path(settings.projects_dir / safe_pid / "audio", settings.projects_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    max_bytes = settings.max_upload_size_mb * 1024 * 1024
    saved_path, checksum, size_bytes = await save_upload_file_securely(
        upload_file=file,
        dest_dir=dest_dir,
        allowed_extensions=settings.allowed_audio_extensions,
        max_size_bytes=max_bytes,
        prefix="audio",
        expected_type="audio",
        validate_content=True
    )

    try:
        meta = ffmpeg_service.inspect_media(saved_path)
    except Exception as e:
        if saved_path.exists():
            try:
                saved_path.unlink()
            except OSError:
                pass
        raise HTTPException(status_code=400, detail=f"Invalid or unreadable audio media: {str(e)}")

    rel_path = f"projects/{safe_pid}/audio/{saved_path.name}"
    display_title = sanitize_filename(title or file.filename)

    track = AudioTrack(
        project_id=project_id,
        track_type=track_type,
        title=display_title,
        file_path=rel_path,
        duration_seconds=meta["duration"],
        start_time_seconds=0.0,
        volume_level=0.8 if track_type == "music" else 1.2,
        ducking_enabled=True if track_type == "music" else False,
        ducking_level=0.3
    )
    db.add(track)
    db.commit()
    db.refresh(track)

    return {
        "id": track.id,
        "project_id": track.project_id,
        "track_type": track.track_type,
        "title": track.title,
        "file_path": track.file_path,
        "duration_seconds": track.duration_seconds,
        "start_time_seconds": track.start_time_seconds,
        "volume_level": track.volume_level,
        "ducking_enabled": track.ducking_enabled,
        "ducking_level": track.ducking_level,
        "scene_id": track.scene_id,
        "shot_id": track.shot_id,
        "created_at": track.created_at,
        "url": f"/media/{track.file_path}"
    }

@router.post("/generate-bgm/{project_id}", response_model=AudioTrackResponse)
def generate_cinematic_bgm(project_id: str, req: GenerateBgmRequest, db: Session = Depends(get_db)):
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    out_path = audio_service.generate_cinematic_bgm(
        project_id=project_id,
        title=req.title or "Ambient Score",
        duration=req.duration or 20.0,
        mood=req.mood or "cyberpunk"
    )

    rel_path = f"projects/{project_id}/audio/{out_path.name}"
    track = AudioTrack(
        project_id=project_id,
        track_type="music",
        title=req.title or "Ambient Score",
        file_path=rel_path,
        duration_seconds=req.duration or 20.0,
        start_time_seconds=0.0,
        volume_level=0.5,
        ducking_enabled=True,
        ducking_level=0.25
    )
    db.add(track)
    db.commit()
    db.refresh(track)

    return {
        "id": track.id,
        "project_id": track.project_id,
        "track_type": track.track_type,
        "title": track.title,
        "file_path": track.file_path,
        "duration_seconds": track.duration_seconds,
        "start_time_seconds": track.start_time_seconds,
        "volume_level": track.volume_level,
        "ducking_enabled": track.ducking_enabled,
        "ducking_level": track.ducking_level,
        "scene_id": track.scene_id,
        "shot_id": track.shot_id,
        "created_at": track.created_at,
        "url": f"/media/{track.file_path}"
    }

@router.post("/generate-voiceover/{project_id}", response_model=AudioTrackResponse)
def generate_voiceover(project_id: str, req: GenerateVoiceoverRequest, db: Session = Depends(get_db)):
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    out_path = audio_service.generate_scratch_voiceover(
        project_id=project_id,
        text_content=req.dialogue_text,
        character_name=req.character_name or "Character",
        duration=req.duration or 4.0
    )

    rel_path = f"projects/{project_id}/audio/{out_path.name}"
    track = AudioTrack(
        project_id=project_id,
        track_type="voiceover",
        title=f"VO: {req.character_name}",
        file_path=rel_path,
        duration_seconds=req.duration or 4.0,
        start_time_seconds=0.0,
        volume_level=1.2,
        ducking_enabled=False
    )
    db.add(track)
    db.commit()
    db.refresh(track)

    return {
        "id": track.id,
        "project_id": track.project_id,
        "track_type": track.track_type,
        "title": track.title,
        "file_path": track.file_path,
        "duration_seconds": track.duration_seconds,
        "start_time_seconds": track.start_time_seconds,
        "volume_level": track.volume_level,
        "ducking_enabled": track.ducking_enabled,
        "ducking_level": track.ducking_level,
        "scene_id": track.scene_id,
        "shot_id": track.shot_id,
        "created_at": track.created_at,
        "url": f"/media/{track.file_path}"
    }

@router.patch("/{track_id}", response_model=AudioTrackResponse)
def update_audio_track(track_id: str, data: AudioTrackUpdate, db: Session = Depends(get_db)):
    track = db.query(AudioTrack).filter(AudioTrack.id == track_id).first()
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")

    for k, v in data.model_dump(exclude_unset=True).items():
        setattr(track, k, v)

    db.commit()
    db.refresh(track)
    return {
        "id": track.id,
        "project_id": track.project_id,
        "track_type": track.track_type,
        "title": track.title,
        "file_path": track.file_path,
        "duration_seconds": track.duration_seconds,
        "start_time_seconds": track.start_time_seconds,
        "volume_level": track.volume_level,
        "ducking_enabled": track.ducking_enabled,
        "ducking_level": track.ducking_level,
        "scene_id": track.scene_id,
        "shot_id": track.shot_id,
        "created_at": track.created_at,
        "url": f"/media/{track.file_path}"
    }

@router.delete("/{track_id}")
def delete_audio_track(track_id: str, db: Session = Depends(get_db)):
    track = db.query(AudioTrack).filter(AudioTrack.id == track_id).first()
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")

    if track.file_path:
        try:
            real_file = validate_safe_path(settings.data_dir / track.file_path, settings.data_dir)
            if real_file.exists():
                real_file.unlink()
        except Exception:
            pass

    db.delete(track)
    db.commit()
    return {"status": "deleted", "track_id": track_id}
