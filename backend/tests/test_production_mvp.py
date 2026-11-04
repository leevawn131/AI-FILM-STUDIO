import io
import json
import os
import shutil
import tempfile
import zipfile
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from backend.app.main import app
from backend.app.database import get_db, SessionLocal, init_db
from backend.app.models.entities import (
    Project, Scene, Shot, MediaAsset, ExportJob, AudioTrack
)
from backend.app.services.ai_provider import (
    OllamaProvider, OfflineTemplateProvider, GeminiProvider, get_ai_provider
)
from backend.app.services.structured_output import (
    repair_json_string, parse_and_validate_structured_json,
    ScreenplaySceneOutput, ShotBreakdownItem, SceneBreakdownOutput
)
from backend.app.services.media_watcher import MediaWatcherService
from backend.app.services.timeline_editor import TimelineEditorService
from backend.app.services.audio_service import AudioService
from backend.app.services.export_manager import ExportManager
from backend.app.services.project_backup import ProjectBackupService, RestoreSecurityError
from backend.app.config import settings

client = TestClient(app)

@pytest.fixture(scope="module", autouse=True)
def setup_db():
    init_db()

@pytest.fixture
def db_session():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

@pytest.fixture
def test_project(db_session: Session):
    p = Project(
        name="Test Production Project",
        logline="A test project for production MVP verification",
        genre="Sci-Fi",
        aspect_ratio="16:9",
        fps=24,
        target_duration_sec=60,
        visual_style="Cinematic",
        budget_total=100.0,
        budget_spent=0.0
    )
    db_session.add(p)
    db_session.commit()
    db_session.refresh(p)

    sc1 = Scene(
        project_id=p.id,
        scene_number=1,
        slugline="INT. LAB - NIGHT",
        title="The Awakening",
        summary="A machine awakens in the lab.",
        script_content="A machine turns on.",
        estimated_duration=10.0,
        budget_limit=20.0,
        order_index=1
    )
    sc2 = Scene(
        project_id=p.id,
        scene_number=2,
        slugline="EXT. ROOFTOP - DAWN",
        title="The Escape",
        summary="Looking out over the city.",
        script_content="Wind blows.",
        estimated_duration=15.0,
        budget_limit=20.0,
        order_index=2
    )
    db_session.add_all([sc1, sc2])
    db_session.commit()
    db_session.refresh(sc1)
    db_session.refresh(sc2)

    sh1 = Shot(
        project_id=p.id,
        scene_id=sc1.id,
        shot_number=1,
        shot_code="SC01_SH01",
        camera_angle="Wide Shot",
        camera_movement="Static",
        visual_description="Wide view of dark lab.",
        lighting="Dim amber",
        character_references="None",
        video_prompt="A wide view of a futuristic dimly lit lab",
        duration_seconds=4.0,
        order_index=1,
        generation_status="pending"
    )
    sh2 = Shot(
        project_id=p.id,
        scene_id=sc2.id,
        shot_number=1,
        shot_code="SC02_SH01",
        camera_angle="Close-up",
        camera_movement="Pan Right",
        visual_description="Close up of sunrise.",
        lighting="Golden hour",
        character_references="None",
        video_prompt="A close-up shot of glowing horizon at dawn",
        duration_seconds=5.0,
        order_index=1,
        generation_status="pending"
    )
    db_session.add_all([sh1, sh2])
    db_session.commit()
    db_session.refresh(sh1)
    db_session.refresh(sh2)

    yield {
        "project": p,
        "scene1": sc1,
        "scene2": sc2,
        "shot1": sh1,
        "shot2": sh2
    }

    # Cleanup
    db_session.query(ExportJob).filter(ExportJob.project_id == p.id).delete()
    db_session.query(AudioTrack).filter(AudioTrack.project_id == p.id).delete()
    db_session.query(MediaAsset).filter(MediaAsset.project_id == p.id).delete()
    db_session.query(Shot).filter(Shot.project_id == p.id).delete()
    db_session.query(Scene).filter(Scene.project_id == p.id).delete()
    db_session.query(Project).filter(Project.id == p.id).delete()
    db_session.commit()


# ============================================================================
# 1. AI PROVIDER & STRUCTURED OUTPUT TESTS
# ============================================================================

def test_structured_json_repair():
    """Verifies that markdown fences, unclosed braces, and trailing commas are repaired."""
    malformed = """```json
    {
      "slugline": "INT. OBSERVATORY - NIGHT",
      "title": "Starlight",
      "summary": "Astronomer discovers anomaly",
      "script_content": "Looking through the telescope.",
      "estimated_duration": 8,
    }
    ```"""
    data, err = parse_and_validate_structured_json(malformed, ScreenplaySceneOutput)
    assert err is None
    assert data is not None
    assert data.slugline == "INT. OBSERVATORY - NIGHT"
    assert data.title == "Starlight"
    assert data.estimated_duration == 8

def test_offline_template_provider_attribution():
    """Offline provider must return structured output explicitly marked with is_fallback."""
    offline = OfflineTemplateProvider()
    item, meta = offline.generate_structured(
        prompt="Write a scene about discovery",
        schema=ScreenplaySceneOutput
    )
    assert meta["is_fallback"] is True
    assert meta["model"] == "offline-studio-engine"
    assert item.title != ""
    assert item.estimated_duration > 0

def test_ai_status_endpoint():
    """GET /api/system/ai-status returns provider status and local health."""
    res = client.get("/api/system/ai-status")
    assert res.status_code == 200
    data = res.json()
    assert "active_provider" in data
    assert "ollama" in data
    assert "gemini" in data
    assert "offline_template" in data

def test_ai_config_switch():
    """POST /api/system/ai-config switches provider dynamically."""
    res = client.post("/api/system/ai-config", json={"provider": "offline"})
    assert res.status_code == 200
    assert res.json()["active_provider"] == "offline"


# ============================================================================
# 2. MEDIA WATCHER STRICT MATCHING & IN-FLIGHT DETECTION & VERSIONING
# ============================================================================

def test_media_watcher_in_flight_detection(tmp_path):
    """Temporary or downloading files (.crdownload, .part) must be skipped."""
    watcher = MediaWatcherService()
    p_cr = tmp_path / "SC01_SH01.mp4.crdownload"
    p_cr.write_bytes(b"downloading...")
    assert watcher.is_file_in_flight(p_cr) is True

    p_part = tmp_path / "SC01_SH01.mp4.part"
    p_part.write_bytes(b"partial...")
    assert watcher.is_file_in_flight(p_part) is True

    p_tmp = tmp_path / "SC01_SH01.mp4.tmp"
    p_tmp.write_bytes(b"tmp...")
    assert watcher.is_file_in_flight(p_tmp) is True

def test_strict_scene_shot_matching(db_session: Session, test_project):
    """
    SC02_SH01 must match Scene 2 Shot 1, NEVER Scene 1 Shot 1.
    """
    watcher = MediaWatcherService()
    p_id = test_project["project"].id
    shots = db_session.query(Shot).filter(Shot.project_id == p_id).all()

    # Test SC02_SH01
    shot_match, is_ambig, reason = watcher.match_shot_detailed("SC02_SH01_take1.mp4", shots)
    assert not is_ambig
    assert shot_match is not None
    assert shot_match.id == test_project["shot2"].id
    assert shot_match.shot_code == "SC02_SH01"

    # Test SC01_SH01
    shot_match1, is_ambig1, reason1 = watcher.match_shot_detailed("SC01_SH01.mp4", shots)
    assert not is_ambig1
    assert shot_match1 is not None
    assert shot_match1.id == test_project["shot1"].id
    assert shot_match1.shot_code == "SC01_SH01"

def test_ambiguous_shot_matching(db_session: Session, test_project):
    """
    A filename with only SH01 when multiple scenes exist must be rejected as ambiguous.
    """
    watcher = MediaWatcherService()
    p_id = test_project["project"].id
    shots = db_session.query(Shot).filter(Shot.project_id == p_id).all()
    # Both scene 1 and scene 2 have shot_number = 1
    match, is_ambig, reason = watcher.match_shot_detailed("SH01_test.mp4", shots)
    assert is_ambig is True
    assert match is None, "Ambiguous single shot number across multiple scenes must NOT silently match scene 1!"

def test_version_extraction(db_session: Session):
    """Regex extracts version numbers correctly."""
    watcher = MediaWatcherService()
    assert watcher.extract_version_number("SC01_SH01_v001.mp4", None, db_session) == 1
    assert watcher.extract_version_number("SC01_SH01_v2.mp4", None, db_session) == 2

def test_shot_version_selection(db_session: Session, test_project):
    """Switching active version changes shot.video_asset_id."""
    p_id = test_project["project"].id
    sh_id = test_project["shot1"].id

    v1 = MediaAsset(
        project_id=p_id,
        shot_id=sh_id,
        version=1,
        filename="SC01_SH01_v001.mp4",
        file_path="mock/v1.mp4",
        media_type="video",
        width=1920,
        height=1080,
        duration_seconds=4.0,
        fps=24,
        codec="h264",
        file_size_bytes=1000
    )
    v2 = MediaAsset(
        project_id=p_id,
        shot_id=sh_id,
        version=2,
        filename="SC01_SH01_v002.mp4",
        file_path="mock/v2.mp4",
        media_type="video",
        width=1920,
        height=1080,
        duration_seconds=4.0,
        fps=24,
        codec="h264",
        file_size_bytes=1200
    )
    db_session.add_all([v1, v2])
    db_session.commit()
    db_session.refresh(v1)
    db_session.refresh(v2)

    # Initial selection endpoint test
    res = client.post(f"/api/storyboard/shot/{sh_id}/select-version/{v2.id}")
    assert res.status_code == 200
    assert res.json()["selected_version"] == 2

    # Check list versions endpoint
    vers_res = client.get(f"/api/storyboard/shot/{sh_id}/versions")
    assert vers_res.status_code == 200
    versions = vers_res.json()
    assert len(versions) >= 2


# ============================================================================
# 3. TIMELINE RENDERING MODES (PREVIEW VS FINAL)
# ============================================================================

def test_final_render_mode_strict_missing_clips_failure(db_session: Session, test_project):
    """
    Final Master render mode must strictly raise ValueError when clips are missing,
    never silently generating a blank movie.
    """
    editor = TimelineEditorService()
    p_id = test_project["project"].id
    with pytest.raises(ValueError) as exc:
        editor.render_timeline_sequence(
            db=db_session,
            project_id=p_id,
            is_preview=False,
            allow_placeholders=False
        )
    assert "Final Render Rejected" in str(exc.value)
    assert "SC01_SH01" in str(exc.value) or "SC02_SH01" in str(exc.value)


# ============================================================================
# 4. AUDIO DUCKING & STRICT ERROR HANDLING
# ============================================================================

def test_audio_strict_error_handling(tmp_path):
    """Audio mixing on non-existent video input must raise FileNotFoundError, not fake success."""
    audio_srv = AudioService()
    fake_video = tmp_path / "non_existent.mp4"
    fake_out = tmp_path / "out.mp4"
    with pytest.raises(FileNotFoundError):
        audio_srv.mix_audio_tracks(
            video_input=fake_video,
            audio_tracks=[{"file_path": "fake.mp3", "track_type": "music"}],
            output_path=fake_out
        )


# ============================================================================
# 5. ASYNC EXPORT MANAGER LIFECYCLE (CANCEL, RETRY, RECOVERY)
# ============================================================================

def test_export_job_lifecycle(db_session: Session, test_project):
    """Test full async job lifecycle: cancel and startup recovery."""
    mgr = ExportManager()
    p_id = test_project["project"].id

    job = ExportJob(
        project_id=p_id,
        preset_name="1080p_cinematic",
        resolution="1920x1080",
        aspect_ratio="16:9",
        fps=24,
        include_subtitles=False,
        render_mode="preview",
        allow_placeholders=True,
        status="rendering",
        progress_pct=40
    )
    db_session.add(job)
    db_session.commit()
    db_session.refresh(job)

    # Test Cancel
    cancel_success = mgr.cancel_export_job(db_session, job.id)
    assert cancel_success is True
    db_session.refresh(job)
    assert job.status == "cancelled"

    # Test Startup Recovery
    job.status = "rendering"
    db_session.commit()
    recovered = mgr.recover_interrupted_jobs(db_session)
    assert recovered >= 1
    db_session.refresh(job)
    assert job.status == "failed"
    assert "interrupted" in (job.error_message or "").lower()


# ============================================================================
# 6. FULL PROJECT BACKUP & RESTORE WITH PATH TRAVERSAL DEFENSE
# ============================================================================

def test_project_backup_and_restore(db_session: Session, test_project):
    """Test ZIP creation, manifest SHA-256 verification, and restoration."""
    backup_srv = ProjectBackupService()
    p_id = test_project["project"].id

    # Create backup
    backup_file = backup_srv.create_project_backup(db_session, p_id)
    assert backup_file.exists()
    assert backup_file.suffix == ".zip"

    try:
        # Verify ZIP contains manifest.json and project_data.json
        with zipfile.ZipFile(backup_file, "r") as zf:
            namelist = zf.namelist()
            assert "manifest.json" in namelist
            assert "project_data.json" in namelist
            manifest_data = json.loads(zf.read("manifest.json").decode("utf-8"))
            assert manifest_data["project_id"] == p_id
            assert manifest_data["entity_counts"]["scenes"] == 2
            assert manifest_data["entity_counts"]["shots"] == 2

        # Test Restore with new project name
        res = backup_srv.restore_project_backup(
            db=db_session,
            zip_path=backup_file,
            new_project_name="Restored Copy",
            overwrite=False
        )
        assert res["status"] == "success"
        restored_id = res["project_id"]
        assert restored_id != p_id

        # Verify restored entities
        restored_scenes = db_session.query(Scene).filter(Scene.project_id == restored_id).all()
        assert len(restored_scenes) == 2
        restored_shots = db_session.query(Shot).filter(Shot.project_id == restored_id).all()
        assert len(restored_shots) == 2

        # Cleanup restored project
        db_session.query(Shot).filter(Shot.project_id == restored_id).delete()
        db_session.query(Scene).filter(Scene.project_id == restored_id).delete()
        db_session.query(Project).filter(Project.id == restored_id).delete()
        db_session.commit()

    finally:
        if backup_file.exists():
            backup_file.unlink()

def test_project_restore_path_traversal_defense(db_session: Session, tmp_path):
    """Malicious ZIP archives with ../ must be rejected to prevent path traversal."""
    malicious_zip = tmp_path / "malicious.zip"
    with zipfile.ZipFile(malicious_zip, "w") as zf:
        zf.writestr("../../evil.txt", "exploit")
        manifest = {
            "version": "1.0",
            "project_id": "evil",
            "files": {}
        }
        zf.writestr("manifest.json", json.dumps(manifest))
        zf.writestr("project_data.json", json.dumps({"project": {}}))

    backup_srv = ProjectBackupService()
    with pytest.raises(RestoreSecurityError) as exc:
        backup_srv.restore_project_backup(db_session, malicious_zip)
    assert "Malicious path traversal detected" in str(exc.value)
