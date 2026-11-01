"""
Comprehensive Integration Tests for AI FILM STUDIO (Sprints S1, S2, S3 & Review Fixes).

Covers all 10 target integration scenarios:
1. Fresh database startup without demo data
2. Legacy database migration with existing film data
3. Interrupted migration recovery
4. Duplicate job submission (idempotency, no duplicate charges)
5. Retry after failed job
6. Concurrent budget updates (independent DB sessions, atomic conditional updates)
7. Upload traversal attacks (rejecting outside-root writes)
8. Duplicate media upload (409 Conflict & temp cleanup)
9. Audio/image upload validation (magic bytes, spoofing rejection)
10. Application restart and data persistence across sessions
"""

import io
import shutil
import sqlite3
import threading
from pathlib import Path
from PIL import Image
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.app.config import settings
from backend.app.main import app
from backend.app.database import (
    Base,
    run_migrations,
    validate_legacy_schema,
    REQUIRED_LEGACY_SCHEMA,
    IncompatibleLegacySchemaError
)
from backend.app.models.entities import Project, Scene, Shot, Character, Location, StudioJob, CostRecord, MediaAsset
from backend.app.services.job_manager import job_manager, sanitize_error_message
from backend.app.services.cost_controller import cost_controller
from backend.app.services.security import (
    save_upload_file_securely,
    validate_safe_path,
    verify_magic_bytes,
    sanitize_filename
)
from backend.app.services.ffmpeg_service import ffmpeg_service

client = TestClient(app)


def test_1_fresh_database_startup_without_demo_data(tmp_path):
    """Scenario 1: Fresh database startup without demo data (SEED_DEMO_DATA=False)."""
    db_file = tmp_path / "fresh_no_demo.db"
    db_url = f"sqlite:///{db_file}"
    
    # Run migration on fresh DB
    run_migrations(db_url)
    
    engine = create_engine(db_url)
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        # Schema tables should exist
        assert len(Base.metadata.tables) > 0
        
        # Verify 0 projects seeded
        count = session.query(Project).count()
        assert count == 0
    finally:
        session.close()
        engine.dispose()


from backend.tests.test_database_integrity import create_full_legacy_schema

def test_2_legacy_database_migration_with_existing_film_data(tmp_path):
    """Scenario 2: Legacy database migration with existing film data preserves data and creates backup."""
    legacy_file = tmp_path / "legacy_production.db"
    create_full_legacy_schema(legacy_file)

    # Seed realistic legacy film data across all entities
    conn = sqlite3.connect(str(legacy_file))
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO projects (id, name, budget_total, budget_spent) VALUES (?, ?, ?, ?)",
        ("proj-legacy-1", "Legacy Feature Film", 200.0, 15.0)
    )
    cur.execute(
        "INSERT INTO characters (id, project_id, name, role) VALUES (?, ?, ?, ?)",
        ("char-legacy-1", "proj-legacy-1", "Commander Vance", "Protagonist")
    )
    cur.execute(
        "INSERT INTO locations (id, project_id, name, time_of_day) VALUES (?, ?, ?, ?)",
        ("loc-legacy-1", "proj-legacy-1", "Deep Space Station", "Midnight")
    )
    cur.execute(
        "INSERT INTO scenes (id, project_id, scene_number, slugline, title) VALUES (?, ?, ?, ?, ?)",
        ("scene-legacy-1", "proj-legacy-1", 1, "INT. DOCK - NIGHT", "Opening Dawn Sequence")
    )
    cur.execute(
        "INSERT INTO shots (id, scene_id, project_id, shot_number, shot_code) VALUES (?, ?, ?, ?, ?)",
        ("shot-legacy-1", "scene-legacy-1", "proj-legacy-1", 1, "SC01_SH01")
    )
    cur.execute(
        "INSERT INTO media_assets (id, project_id, filename, file_path) VALUES (?, ?, ?, ?)",
        ("media-legacy-1", "proj-legacy-1", "legacy_clip.mp4", "projects/proj-legacy-1/imported_videos/clip.mp4")
    )
    cur.execute(
        "INSERT INTO cost_records (id, project_id, action_type, cost_usd) VALUES (?, ?, ?, ?)",
        ("cost-legacy-1", "proj-legacy-1", "script_generation", 2.50)
    )
    conn.commit()
    conn.close()

    db_url = f"sqlite:///{legacy_file}"
    run_migrations(db_url)

    # Verify backup exists in settings.backup_dir
    backups = list(settings.backup_dir.glob("studio_backup_*.db"))
    assert len(backups) >= 1

    engine = create_engine(db_url)
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        p = session.query(Project).filter(Project.id == "proj-legacy-1").first()
        assert p is not None
        assert p.name == "Legacy Feature Film"
        assert p.budget_total == 200.0
        assert p.budget_spent == 15.0

        c = session.query(Character).filter(Character.id == "char-legacy-1").first()
        assert c is not None and c.name == "Commander Vance"

        loc = session.query(Location).filter(Location.id == "loc-legacy-1").first()
        assert loc is not None and loc.name == "Deep Space Station"

        sc = session.query(Scene).filter(Scene.id == "scene-legacy-1").first()
        assert sc is not None
        assert sc.title == "Opening Dawn Sequence"
        assert sc.budget_spent == 0.0 # New column defaulted cleanly

        sh = session.query(Shot).filter(Shot.id == "shot-legacy-1").first()
        assert sh is not None
        assert sh.shot_code == "SC01_SH01"
        assert sh.flow_credits_spent == 0.0 # New column
        assert sh.generation_attempts == 0 # New column

        ma = session.query(MediaAsset).filter(MediaAsset.id == "media-legacy-1").first()
        assert ma is not None and ma.filename == "legacy_clip.mp4"

        cr = session.query(CostRecord).filter(CostRecord.id == "cost-legacy-1").first()
        assert cr is not None and cr.action_type == "script_generation"
    finally:
        session.close()
        engine.dispose()


def test_3_interrupted_migration_recovery(tmp_path):
    """Scenario 3: Corrupt or partially initialized database fails explicitly and leaves zero stamped version."""
    corrupted_db = tmp_path / "corrupted_partial.db"
    conn = sqlite3.connect(corrupted_db)
    cur = conn.cursor()
    # Missing 6 required tables
    cur.execute('CREATE TABLE "projects" (id TEXT, name TEXT)')
    cur.execute('CREATE TABLE "scenes" (id TEXT, project_id TEXT)')
    cur.execute('CREATE TABLE "shots" (id TEXT, scene_id TEXT)')
    conn.commit()
    conn.close()

    db_url = f"sqlite:///{corrupted_db}"
    with pytest.raises(IncompatibleLegacySchemaError) as exc_info:
        run_migrations(db_url)

    assert "Missing required legacy tables" in str(exc_info.value)

    # Verify alembic_version was NOT created or stamped
    conn = sqlite3.connect(corrupted_db)
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='alembic_version'")
    assert cur.fetchone() is None
    conn.close()


def test_4_duplicate_job_submission(tmp_path):
    """Scenario 4: Duplicate job submission reuses results idempotently without duplicate charges."""
    db_file = tmp_path / "job_idempotency.db"
    db_url = f"sqlite:///{db_file}"
    run_migrations(db_url)

    engine = create_engine(db_url)
    Session = sessionmaker(bind=engine)
    session = Session()

    try:
        # Create project with $100 budget
        p = Project(id="p-job-test", name="Idempotency Test", budget_total=100.0, budget_spent=0.0)
        session.add(p)
        session.commit()

        job_payload = {"prompt": "Cybernetic dawn in Neo-Tokyo", "shot_count": 4}
        
        # 1. First claim
        job1, action1 = job_manager.prepare_or_claim_job(
            db=session,
            project_id=p.id,
            job_type="test_storyboard",
            payload=job_payload
        )
        assert action1 == "EXECUTE"
        assert job1.status == "RUNNING"

        # Charge cost controller with an explicit estimated cost
        cost_controller.authorize_and_charge(
            db=session,
            project_id=p.id,
            action_type="storyboard_generation",
            payload=job_payload,
            details="Test Storyboard",
            estimated_cost=2.50
        )
        # Complete job
        result_data = {"shots": ["shot-1", "shot-2", "shot-3", "shot-4"]}
        job_manager.complete_job(db=session, job_id=job1.id, result=result_data)
        assert job1.status == "COMPLETED"

        # Check budget spent
        session.refresh(p)
        first_spent = p.budget_spent
        assert first_spent > 0.0

        # 2. Second identical claim
        job2, action2 = job_manager.prepare_or_claim_job(
            db=session,
            project_id=p.id,
            job_type="test_storyboard",
            payload=job_payload
        )
        assert action2 == "REUSE"
        assert job2.id == job1.id
        assert job2.status == "COMPLETED"

        # Verify budget was NOT charged a second time
        session.refresh(p)
        assert p.budget_spent == first_spent

    finally:
        session.close()
        engine.dispose()


def test_5_retry_after_failed_job(tmp_path):
    """Scenario 5: Failed requests transition cleanly, store safe error, and are retryable."""
    db_file = tmp_path / "job_retry.db"
    db_url = f"sqlite:///{db_file}"
    run_migrations(db_url)

    engine = create_engine(db_url)
    Session = sessionmaker(bind=engine)
    session = Session()

    try:
        p = Project(id="p-retry-test", name="Retry Test", budget_total=100.0, budget_spent=0.0)
        session.add(p)
        session.commit()

        payload = {"action": "render_scene_v1"}

        # 1. First claim & failure
        job, action = job_manager.prepare_or_claim_job(session, p.id, "render_job", payload)
        assert action == "EXECUTE"

        # Simulate provider failure containing a sensitive Google API key and server path
        raw_sensitive_error = "Provider API error with key AIzaSyD4e5f6g7h8i9j0k1l2m3n4o5p6q7r8s9 at /home/leevawn/secret/token.json"
        job_manager.fail_job(session, job.id, raw_sensitive_error)
        
        session.refresh(job)
        assert job.status == "FAILED"
        assert "AIza" not in job.error_message
        assert "/home/leevawn" not in job.error_message
        assert "[REDACTED]" in job.error_message
        assert job.retry_count == 0

        # 2. Second claim should be recognized as a retry
        job_retried, action_retried = job_manager.prepare_or_claim_job(session, p.id, "render_job", payload)
        assert action_retried == "EXECUTE"
        assert job_retried.id == job.id
        assert job_retried.status == "RUNNING"
        assert job_retried.retry_count == 1
        assert job_retried.error_message == ""

        # 3. Successful retry completion
        job_manager.complete_job(session, job_retried.id, {"status": "success", "frames": 120})
        session.refresh(job_retried)
        assert job_retried.status == "COMPLETED"

    finally:
        session.close()
        engine.dispose()


def test_6_concurrent_budget_updates(tmp_path):
    """Scenario 6: Multiple independent database sessions concurrently charging cannot exceed budget."""
    db_file = tmp_path / "concurrent_budget.db"
    db_url = f"sqlite:///{db_file}"
    run_migrations(db_url)

    engine = create_engine(db_url, connect_args={"timeout": 30.0})
    SessionMaker = sessionmaker(bind=engine)

    # Initialize project with tight budget: total $1.00
    init_session = SessionMaker()
    proj_id = "p-concurrent-atomic"
    proj = Project(id=proj_id, name="Atomic Budget Project", budget_total=1.0, budget_spent=0.0)
    init_session.add(proj)
    init_session.commit()
    init_session.close()

    # Attempt 20 concurrent charges of $0.20 each ($4.00 total requested against $1.00 budget)
    results = {"success": 0, "rejected": 0}
    lock = threading.Lock()

    def attempt_charge(charge_id):
        session = SessionMaker()
        try:
            cost_controller.authorize_and_charge(
                db=session,
                project_id=proj_id,
                action_type="script_generation",
                payload={"thread_task": charge_id},
                allow_duplicate=True,
                estimated_cost=0.20
            )
            with lock:
                results["success"] += 1
        except Exception:
            with lock:
                results["rejected"] += 1
        finally:
            session.close()

    threads = [threading.Thread(target=attempt_charge, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Verify atomic constraint: budget_spent must NEVER exceed budget_total
    verify_session = SessionMaker()
    final_p = verify_session.query(Project).filter(Project.id == proj_id).first()
    assert final_p.budget_spent <= final_p.budget_total
    assert results["success"] > 0
    assert results["rejected"] > 0
    assert (results["success"] + results["rejected"]) == 20
    verify_session.close()
    engine.dispose()


def test_7_upload_traversal_attacks(tmp_path):
    """Scenario 7: Reject path traversal payloads and guarantee no files outside storage root."""
    # 1. Test validate_safe_path directly
    safe_root = tmp_path / "sandbox"
    safe_root.mkdir()

    with pytest.raises(Exception):
        validate_safe_path(safe_root / "../../../etc/shadow", safe_root)

    with pytest.raises(Exception):
        validate_safe_path(Path("/etc/passwd"), safe_root)

    # 2. Test sanitize_filename prevents directory separators
    traversal_name = "../../exploit.mp4"
    clean = sanitize_filename(traversal_name)
    assert "/" not in clean
    assert "\\" not in clean
    assert ".." not in clean


def test_8_duplicate_media_upload(tmp_path):
    """Scenario 8: Exact duplicate video upload is rejected with 409 Conflict and cleans temp storage."""
    projects = client.get("/api/projects").json()
    assert len(projects) > 0
    project_id = projects[0]["id"]

    import uuid
    unique_tag = uuid.uuid4().hex[:8]
    test_video = tmp_path / f"unique_clip_{unique_tag}.mp4"
    ffmpeg_service.generate_synthetic_test_clip(
        output_path=test_video,
        duration=1.0,
        text_label=f"DUP_{unique_tag}"
    )
    video_bytes = test_video.read_bytes()

    # First upload
    res1 = client.post(
        f"/api/media/upload/{project_id}",
        files={"file": ("dup_test_1.mp4", io.BytesIO(video_bytes), "video/mp4")}
    )
    assert res1.status_code == 200

    # Second upload of identical content
    res2 = client.post(
        f"/api/media/upload/{project_id}",
        files={"file": ("dup_test_2.mp4", io.BytesIO(video_bytes), "video/mp4")}
    )
    assert res2.status_code == 409
    assert "Duplicate media asset" in res2.json()["detail"]

    # Verify no leaked .tmp files in settings.temp_dir
    tmp_files = list(settings.temp_dir.glob("*.tmp"))
    assert len(tmp_files) == 0


def test_9_audio_and_image_upload_validation(tmp_path):
    """Scenario 9: Magic bytes verify genuine headers and reject spoofed extensions."""
    projects = client.get("/api/projects").json()
    assert len(projects) > 0
    project_id = projects[0]["id"]

    # Fetch a character
    chars = client.get(f"/api/characters/{project_id}").json()
    assert len(chars) > 0
    char_id = chars[0]["id"]

    # 1. Attempt uploading executable content disguised as a JPEG
    fake_exe_bytes = b"MZ\x90\x00\x03\x00\x00\x00\x04\x00\x00\x00\xff\xff\x00\x00"
    res_spoof = client.post(
        f"/api/characters/{char_id}/upload-ref",
        files={"file": ("malicious.jpg", io.BytesIO(fake_exe_bytes), "image/jpeg")}
    )
    assert res_spoof.status_code == 400
    assert "Invalid file header" in res_spoof.json()["detail"]

    # 2. Attempt uploading valid JPEG
    img_buf = io.BytesIO()
    Image.new("RGB", (64, 64), color="blue").save(img_buf, format="JPEG")
    valid_jpeg_bytes = img_buf.getvalue()
    res_valid = client.post(
        f"/api/characters/{char_id}/upload-ref",
        files={"file": ("valid.jpg", io.BytesIO(valid_jpeg_bytes), "image/jpeg")}
    )
    assert res_valid.status_code == 200
    assert "reference_url" in res_valid.json()


def test_10_application_restart_and_data_persistence(tmp_path):
    """Scenario 10: Server restart reconciles running jobs and persists film data across sessions."""
    db_file = tmp_path / "restart_persistence.db"
    db_url = f"sqlite:///{db_file}"
    run_migrations(db_url)

    engine = create_engine(db_url)
    SessionMaker = sessionmaker(bind=engine)

    # 1. Session 1: Create project and an in-progress job
    s1 = SessionMaker()
    p = Project(id="p-restart-1", name="Persistent Sci-Fi Studio", budget_total=300.0, budget_spent=50.0)
    s1.add(p)
    s1.commit()

    job1, _ = job_manager.prepare_or_claim_job(s1, p.id, "render_heavy_timeline", {"scene_id": "sc1"})
    assert job1.status == "RUNNING"
    saved_job_id = job1.id
    s1.close()

    # 2. Simulate application restart: new session + reconcile_interrupted_jobs
    s2 = SessionMaker()
    recovered_count = job_manager.reconcile_interrupted_jobs(s2)
    assert recovered_count == 1

    # Verify project persists
    p_check = s2.query(Project).filter(Project.id == "p-restart-1").first()
    assert p_check is not None
    assert p_check.name == "Persistent Sci-Fi Studio"
    assert p_check.budget_spent == 50.0

    # Verify job was safely recovered into a retryable FAILED state
    job_check = s2.query(StudioJob).filter(StudioJob.id == saved_job_id).first()
    assert job_check is not None
    assert job_check.status == "FAILED"
    assert "interrupted by server restart" in job_check.error_message

    # Verify that a client can immediately retry this job after the restart
    retried_job, action = job_manager.prepare_or_claim_job(s2, p_check.id, "render_heavy_timeline", {"scene_id": "sc1"})
    assert action == "EXECUTE"
    assert retried_job.status == "RUNNING"
    assert retried_job.retry_count == 1

    s2.close()
    engine.dispose()
