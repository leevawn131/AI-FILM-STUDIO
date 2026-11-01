"""
Final Reliability Test Suite.
Validates:
- TASK 1: Worker Lease Heartbeat, Fencing Token, and Split-Brain Protection
- TASK 2: Persistent Upload Lifecycle (STAGED -> DB_COMMITTED -> FINALIZED / FAILED)
          Crash Recovery, Missing File Reversion, and Asset Preservation
- TASK 3: Gemini Usage Persistence, Missing Metadata Handling, and Provider Attribution
"""

import io
import os
import shutil
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.app.config import settings
from backend.app.main import app
from backend.app.database import (
    Base,
    run_migrations,
    SessionLocal
)
from backend.app.models.entities import (
    Project,
    Scene,
    Shot,
    Character,
    Location,
    StudioJob,
    CostRecord,
    UploadOperation
)
from backend.app.services.job_manager import (
    JobManager,
    JobOwnershipLostError
)
from backend.app.services.upload_manager import (
    UploadManager,
    upload_manager
)
from backend.app.services.cost_controller import cost_controller
from backend.app.services.gemini_service import (
    gemini_service,
    extract_gemini_usage,
    get_offline_usage
)

client = TestClient(app)


def create_test_image(format="JPEG", size=(64, 64), color="blue") -> bytes:
    """Produces genuine raster bytes for Pillow-based validation."""
    buf = io.BytesIO()
    img = Image.new("RGB", size, color=color)
    img.save(buf, format=format)
    return buf.getvalue()


# ==============================================================================
# TASK 1: Worker Lease Heartbeat & Fencing Tests
# ==============================================================================

def test_task1_worker_lease_fencing_and_split_brain_protection(tmp_path):
    """
    TASK 1:
    - Worker A claims a job (token=1).
    - Worker A lease expires.
    - Worker B reclaims the job (token=2).
    - Worker A attempts to complete -> rejected with JobOwnershipLostError.
    - Worker B completes successfully.
    """
    db_file = tmp_path / "fencing.db"
    db_url = f"sqlite:///{db_file}"
    run_migrations(db_url)

    engine = create_engine(db_url)
    SessionMaker = sessionmaker(bind=engine)
    session = SessionMaker()

    proj = Project(id="p-fence-1", name="Fencing Project", budget_total=500.0, budget_spent=0.0)
    session.add(proj)
    session.commit()

    worker_a = JobManager(worker_id="worker-A")
    worker_b = JobManager(worker_id="worker-B")

    payload = {"scene": 1, "task": "long_render"}

    # 1. Worker A claims the job with a short 2-second lease
    job_a, action_a = worker_a.prepare_or_claim_job(
        db=session,
        project_id="p-fence-1",
        job_type="ffmpeg_render",
        payload=payload,
        lease_seconds=1
    )
    assert action_a == "EXECUTE"
    assert job_a.worker_id == "worker-A"
    assert job_a.lease_token == 1

    # 2. Worker A lease expires
    past_time = datetime.now(timezone.utc) - timedelta(seconds=10)
    job_a.lease_expires_at = past_time
    session.commit()

    # 3. Worker B reclaims the expired job
    job_b, action_b = worker_b.prepare_or_claim_job(
        db=session,
        project_id="p-fence-1",
        job_type="ffmpeg_render",
        payload=payload,
        lease_seconds=300
    )
    assert action_b == "EXECUTE"
    assert job_b.worker_id == "worker-B"
    assert job_b.lease_token == 2  # Monotonically incremented fencing token!

    # 4. Zombie Worker A finishes its slow task and attempts to complete
    with pytest.raises(JobOwnershipLostError) as exc_info:
        worker_a.complete_job(
            db=session,
            job_id=job_a.id,
            result={"file": "stale_worker_a_output.mp4"},
            worker_id="worker-A",
            lease_token=1
        )
    assert "ownership was lost or superseded" in str(exc_info.value).lower()

    # Verify database still has Worker B as active owner and job is NOT completed by Worker A
    session.refresh(job_b)
    assert job_b.status == "RUNNING"
    assert job_b.worker_id == "worker-B"
    assert job_b.lease_token == 2

    # 5. Worker B completes successfully
    completed_b = worker_b.complete_job(
        db=session,
        job_id=job_b.id,
        result={"file": "valid_worker_b_output.mp4"},
        worker_id="worker-B",
        lease_token=2
    )
    assert completed_b.status == "COMPLETED"
    assert "valid_worker_b_output.mp4" in completed_b.result

    session.close()
    engine.dispose()


def test_task1_heartbeat_renewal_during_long_task(tmp_path):
    """
    TASK 1:
    - Long running job periodically renews lease via renew_lease / heartbeat.
    - Non-owners cannot renew.
    """
    db_file = tmp_path / "heartbeat.db"
    db_url = f"sqlite:///{db_file}"
    run_migrations(db_url)

    engine = create_engine(db_url)
    SessionMaker = sessionmaker(bind=engine)
    session = SessionMaker()

    proj = Project(id="p-hb-1", name="Heartbeat Project", budget_total=200.0, budget_spent=0.0)
    session.add(proj)
    session.commit()

    worker = JobManager(worker_id="worker-long-task")
    impostor = JobManager(worker_id="worker-impostor")

    job, action = worker.prepare_or_claim_job(
        db=session,
        project_id="p-hb-1",
        job_type="ffmpeg_render",
        payload={"step": "deep_render"},
        lease_seconds=5
    )
    assert action == "EXECUTE"
    initial_expiry = job.lease_expires_at

    # Impostor worker cannot renew Worker's lease
    renewed_impostor = impostor.renew_lease(
        db=session,
        job_id=job.id,
        worker_id="worker-impostor",
        lease_token=job.lease_token,
        extend_seconds=60
    )
    assert renewed_impostor is False

    # Genuine worker renews lease
    renewed = worker.renew_lease(
        db=session,
        job_id=job.id,
        worker_id="worker-long-task",
        lease_token=job.lease_token,
        extend_seconds=60
    )
    assert renewed is True

    session.refresh(job)
    assert job.lease_expires_at > initial_expiry

    session.close()
    engine.dispose()


def test_task1_reconciliation_preserves_active_jobs_on_other_workers(tmp_path):
    """
    TASK 1:
    - Reconciliation must NOT mark active jobs with valid leases on other workers as failed.
    """
    db_file = tmp_path / "recon.db"
    db_url = f"sqlite:///{db_file}"
    run_migrations(db_url)

    engine = create_engine(db_url)
    SessionMaker = sessionmaker(bind=engine)
    session = SessionMaker()

    proj = Project(id="p-recon-1", name="Recon Project", budget_total=200.0, budget_spent=0.0)
    session.add(proj)

    now = datetime.now(timezone.utc)
    future = now + timedelta(minutes=15)
    past = now - timedelta(minutes=5)

    # Job on another worker with valid lease
    job_other_active = StudioJob(
        id="job-other-alive",
        project_id="p-recon-1",
        job_type="ai_generation",
        idempotency_key="key-other-1",
        status="RUNNING",
        worker_id="remote-worker-88",
        lease_token=1,
        lease_expires_at=future,
        created_at=now,
        updated_at=now
    )

    # Job on another worker with expired lease
    job_other_dead = StudioJob(
        id="job-other-expired",
        project_id="p-recon-1",
        job_type="ai_generation",
        idempotency_key="key-other-2",
        status="RUNNING",
        worker_id="dead-worker-11",
        lease_token=1,
        lease_expires_at=past,
        created_at=now,
        updated_at=now
    )

    # Job owned by current restarting worker
    job_my_old = StudioJob(
        id="job-my-crashed",
        project_id="p-recon-1",
        job_type="ai_generation",
        idempotency_key="key-my-1",
        status="RUNNING",
        worker_id="current-restarting-worker",
        lease_token=1,
        lease_expires_at=future,
        created_at=now,
        updated_at=now
    )

    session.add_all([job_other_active, job_other_dead, job_my_old])
    session.commit()

    reconciler = JobManager(worker_id="current-restarting-worker")
    count = reconciler.reconcile_interrupted_jobs(session, current_worker_id="current-restarting-worker")

    assert count == 2  # Reconciled job_other_dead and job_my_old

    session.refresh(job_other_active)
    session.refresh(job_other_dead)
    session.refresh(job_my_old)

    assert job_other_active.status == "RUNNING"  # Active worker job PRESERVED!
    assert job_other_dead.status == "FAILED"
    assert job_other_dead.lease_token == 2       # Token incremented to fence dead worker
    assert job_my_old.status == "FAILED"

    session.close()
    engine.dispose()


# ==============================================================================
# TASK 2: Reliable Staged Upload Recovery Tests
# ==============================================================================

def test_task2_staged_upload_lifecycle_and_idempotent_finalization(tmp_path):
    """
    TASK 2:
    - STAGED -> DB_COMMITTED -> FINALIZED.
    - Idempotent finalization: repeating finalization preserves file and returns path.
    - Old asset is deleted only after new asset is firmly in place.
    """
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    um = UploadManager(storage_root=storage_root, temp_root=storage_root / "temp")

    db_file = tmp_path / "uploads.db"
    db_url = f"sqlite:///{db_file}"
    run_migrations(db_url)

    engine = create_engine(db_url)
    SessionMaker = sessionmaker(bind=engine)
    session = SessionMaker()

    proj = Project(id="p-up-1", name="Upload Project", budget_total=100.0, budget_spent=0.0)
    char = Character(id="c-up-1", project_id="p-up-1", name="Avatar Char")
    session.add_all([proj, char])
    session.commit()

    # 1. Existing reference asset on disk
    dest_dir = storage_root / "projects" / "p-up-1" / "references"
    dest_dir.mkdir(parents=True, exist_ok=True)
    old_asset = dest_dir / "old_avatar.jpg"
    old_asset.write_bytes(create_test_image(color="yellow"))
    char.reference_image_path = "projects/p-up-1/references/old_avatar.jpg"
    session.commit()

    # 2. Stage new upload in temp
    temp_dir = storage_root / "temp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    temp_file = temp_dir / "new_avatar.jpg.tmp"
    temp_file.write_bytes(create_test_image(color="green"))

    final_dest = dest_dir / "new_avatar.jpg"

    op = um.start_staged_upload(
        db=session,
        project_id="p-up-1",
        entity_type="character_reference",
        entity_id="c-up-1",
        temp_path=temp_file,
        destination_path=final_dest,
        old_asset_path=old_asset,
        checksum="hash123",
        file_size_bytes=len(temp_file.read_bytes())
    )
    assert op.status == "STAGED"
    assert old_asset.exists()  # Old asset MUST remain intact!

    # 3. Simulate DB commit
    char.reference_image_path = "projects/p-up-1/references/new_avatar.jpg"
    um.mark_db_committed(session, op.id)
    session.commit()

    session.refresh(op)
    assert op.status == "DB_COMMITTED"
    assert old_asset.exists()  # Still untouched!

    # 4. Finalize upload
    finalized_path = um.finalize_upload(session, op.id)
    assert finalized_path.exists()
    assert finalized_path == final_dest
    assert not temp_file.exists()
    assert not old_asset.exists()  # Old asset safely removed ONLY after finalization

    session.refresh(op)
    assert op.status == "FINALIZED"
    assert op.completed_at is not None

    # 5. Idempotent repeat finalization: does not corrupt or raise
    second_finalize = um.finalize_upload(session, op.id)
    assert second_finalize == final_dest
    assert second_finalize.exists()

    session.close()
    engine.dispose()


def test_task2_crash_recovery_after_db_commit(tmp_path):
    """
    TASK 2:
    - Crash after DB commit (status=DB_COMMITTED) with file still in temp.
    - Application restart recovery moves file to destination and marks FINALIZED.
    """
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    um = UploadManager(storage_root=storage_root, temp_root=storage_root / "temp")

    db_file = tmp_path / "crash_recovery.db"
    db_url = f"sqlite:///{db_file}"
    run_migrations(db_url)

    engine = create_engine(db_url)
    SessionMaker = sessionmaker(bind=engine)
    session = SessionMaker()

    proj = Project(id="p-crash-1", name="Crash Project", budget_total=100.0, budget_spent=0.0)
    loc = Location(id="l-crash-1", project_id="p-crash-1", name="Cyber Alley")
    session.add_all([proj, loc])
    session.commit()

    temp_dir = storage_root / "temp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    temp_file = temp_dir / "crash_loc.jpg.tmp"
    temp_file.write_bytes(create_test_image(color="magenta"))

    dest_dir = storage_root / "projects" / "p-crash-1" / "references"
    dest_file = dest_dir / "cyber_alley.jpg"

    op = um.start_staged_upload(
        db=session,
        project_id="p-crash-1",
        entity_type="location_reference",
        entity_id="l-crash-1",
        temp_path=temp_file,
        destination_path=dest_file
    )

    # Commit DB reference but simulate process crash BEFORE finalization
    loc.reference_image_path = "projects/p-crash-1/references/cyber_alley.jpg"
    um.mark_db_committed(session, op.id)
    session.commit()

    assert temp_file.exists()
    assert not dest_file.exists()

    # Restart / recovery runs
    stats = um.recover_staged_uploads(session)
    assert stats["finalized"] == 1

    # Verify destination file now exists, temp is cleaned, and op is FINALIZED
    assert dest_file.exists()
    assert not temp_file.exists()
    session.refresh(op)
    assert op.status == "FINALIZED"

    session.close()
    engine.dispose()


def test_task2_missing_file_recovery_reverts_entity(tmp_path):
    """
    TASK 2:
    - If a staged file is completely lost before finalization:
      Recovery detects missing file, reverts entity in DB to old reference (or empty),
      and marks upload FAILED. No database record references a missing file!
    """
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    um = UploadManager(storage_root=storage_root, temp_root=storage_root / "temp")

    db_file = tmp_path / "missing_recovery.db"
    db_url = f"sqlite:///{db_file}"
    run_migrations(db_url)

    engine = create_engine(db_url)
    SessionMaker = sessionmaker(bind=engine)
    session = SessionMaker()

    proj = Project(id="p-miss-1", name="Missing File Project", budget_total=100.0, budget_spent=0.0)
    char = Character(id="c-miss-1", project_id="p-miss-1", name="Missing File Operative")
    session.add_all([proj, char])
    session.commit()

    dest_dir = storage_root / "projects" / "p-miss-1" / "references"
    dest_dir.mkdir(parents=True, exist_ok=True)
    old_asset = dest_dir / "safe_original.jpg"
    old_asset.write_bytes(create_test_image(color="cyan"))
    char.reference_image_path = "projects/p-miss-1/references/safe_original.jpg"
    session.commit()

    temp_file = storage_root / "temp" / "lost_upload.jpg.tmp"
    dest_file = dest_dir / "never_arrived.jpg"

    op = um.start_staged_upload(
        db=session,
        project_id="p-miss-1",
        entity_type="character_reference",
        entity_id="c-miss-1",
        temp_path=temp_file,
        destination_path=dest_file,
        old_asset_path=old_asset
    )

    # DB committed to the new path, but temp file was lost/deleted before finalization
    char.reference_image_path = "projects/p-miss-1/references/never_arrived.jpg"
    um.mark_db_committed(session, op.id)
    session.commit()

    assert not temp_file.exists()
    assert not dest_file.exists()

    # Recovery detects missing file
    stats = um.recover_staged_uploads(session)
    assert stats["reverted_missing"] == 1

    session.refresh(char)
    # Character's reference image path MUST be safely reverted to old_asset!
    assert "safe_original.jpg" in char.reference_image_path
    assert old_asset.exists()

    session.refresh(op)
    assert op.status == "FAILED"
    assert "reverted" in op.error_message.lower()

    session.close()
    engine.dispose()


# ==============================================================================
# TASK 3: Gemini Usage Persistence Verification Tests
# ==============================================================================

def test_task3_gemini_usage_propagation_to_cost_ledger(tmp_path):
    """
    TASK 3:
    - Mocked Gemini generate_content response with usage_metadata propagates
      tokens, provider='gemini', and actual model name into CostRecord.
    """
    projects = client.get("/api/projects").json()
    assert len(projects) > 0
    project_id = projects[0]["id"]

    mock_gemini_response = MagicMock()
    mock_gemini_response.text = '{"title": "Neural Breach", "slugline": "INT. CORE - NIGHT", "summary": "Core breach in progress", "script_content": "REN\\nAccess granted.", "estimated_duration": 15}'
    
    mock_usage = MagicMock()
    mock_usage.prompt_token_count = 320
    mock_usage.candidates_token_count = 650
    mock_usage.total_token_count = 970
    mock_gemini_response.usage_metadata = mock_usage

    orig_client = gemini_service._client
    orig_key = gemini_service.api_key

    mock_client = MagicMock()
    mock_client.models.generate_content.return_value = mock_gemini_response

    gemini_service._client = mock_client
    gemini_service.api_key = "mock-gemini-key"

    try:
        unique_prompt = f"Unique prompt for ledger propagation {uuid.uuid4().hex[:6]}"
        res = client.post(
            "/api/scenes/generate",
            json={
                "project_id": project_id,
                "prompt": unique_prompt
            }
        )
        assert res.status_code == 200

        # Inspect CostRecord in database
        db = SessionLocal()
        try:
            record = db.query(CostRecord).filter(
                CostRecord.project_id == project_id,
                CostRecord.provider == "gemini",
                CostRecord.input_tokens == 320,
                CostRecord.output_tokens == 650
            ).first()

            assert record is not None
            assert record.model == settings.gemini_model
            assert record.input_tokens == 320
            assert record.output_tokens == 650
            assert "Google Gemini" in record.details
        finally:
            db.close()
    finally:
        gemini_service._client = orig_client
        gemini_service.api_key = orig_key


def test_task3_gemini_missing_usage_metadata_marked_unknown():
    """
    TASK 3:
    - When Gemini response lacks usage_metadata, tokens are stored as None/unknown,
      NOT treated as confirmed zero.
    """
    mock_response = MagicMock()
    mock_response.usage_metadata = None  # Provider did not return token metrics

    meta = extract_gemini_usage(mock_response, model_name="gemini-2.5-flash")
    assert meta["provider"] == "gemini"
    assert meta["model"] == "gemini-2.5-flash"
    assert meta["input_tokens"] is None   # Explicitly unknown, not 0!
    assert meta["output_tokens"] is None  # Explicitly unknown, not 0!
    assert meta["usage_known"] is False


def test_task3_offline_fallback_confirmed_zero_local_tokens():
    """
    TASK 3:
    - Offline fallback records provider='offline_engine', model='offline-studio-engine',
      and confirmed 0 local tokens (not attributed to Gemini).
    """
    meta = get_offline_usage()
    assert meta["provider"] == "offline_engine"
    assert meta["model"] == "offline-studio-engine"
    assert meta["input_tokens"] == 0
    assert meta["output_tokens"] == 0
    assert meta["usage_known"] is True
    assert meta["is_fallback"] is True
    assert "Offline Studio Engine" in meta["engine_label"]
