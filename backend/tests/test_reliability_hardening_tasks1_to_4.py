"""
Comprehensive Test Suite for Reliability Hardening Tasks 1 through 4:
- TASK 1: Strict Worker Fencing (missing, incorrect, expired tokens; heartbeat & completion rejections)
- TASK 2: Atomic Upload Operation Claim and Recovery (multi-session concurrency, expired reclaim, stale worker rejection)
- TASK 3: Destination Integrity Verification (SHA-256 checks, corrupt destination, atomic replacement, crash recovery)
- TASK 4: Auditable Migration of Duplicate Operation Identities (canonical record selection, provenance preservation, upgrade/downgrade)
"""

import os
import uuid
import sqlite3
import hashlib
import concurrent.futures
from pathlib import Path
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from backend.app.models.entities import (
    Base, Project, Character, CostRecord, StudioJob, UploadOperation
)
from backend.app.services.job_manager import JobManager, JobOwnershipLostError
from backend.app.services.upload_manager import (
    UploadManager, UploadOwnershipLostError, _verify_file_integrity
)
from backend.app.services.security import compute_file_sha256
from backend.app.database import run_migrations


def create_sample_image(content: bytes) -> bytes:
    # 1x1 GIF or PNG-like header for valid bytes
    header = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15c4"
    return header + content


# ==============================================================================
# TASK 1: STRICT WORKER FENCING TESTS
# ==============================================================================

def test_task1_strict_fencing_missing_token_raises_error(tmp_path):
    """Missing fencing_token/lease_token must immediately raise JobOwnershipLostError."""
    db_file = tmp_path / "fencing_missing.db"
    db_url = f"sqlite:///{db_file}"
    run_migrations(db_url)

    engine = create_engine(db_url)
    SessionMaker = sessionmaker(bind=engine)
    session = SessionMaker()

    proj = Project(id="p-fence-miss", name="Fencing Missing", budget_total=100.0, budget_spent=0.0)
    session.add(proj)
    session.commit()

    jm = JobManager(worker_id="worker-test")
    job, action = jm.prepare_or_claim_job(session, proj.id, "render", {"frame": 1})
    assert action == "EXECUTE"
    assert job.fencing_token == 1

    # 1. Missing token on renew_lease raises JobOwnershipLostError
    with pytest.raises(JobOwnershipLostError):
        jm.renew_lease(session, job.id, worker_id=job.worker_id, fencing_token=None, lease_token=None)

    # 2. Missing token on complete_job raises JobOwnershipLostError
    with pytest.raises(JobOwnershipLostError):
        jm.complete_job(session, job.id, {"done": True}, worker_id=job.worker_id, fencing_token=None, lease_token=None)

    # 3. Missing token on fail_job raises JobOwnershipLostError
    with pytest.raises(JobOwnershipLostError):
        jm.fail_job(session, job.id, "Some error", worker_id=job.worker_id, fencing_token=None, lease_token=None)

    # 4. Missing token on assert_ownership raises JobOwnershipLostError
    with pytest.raises(JobOwnershipLostError):
        jm.assert_ownership(session, job.id, worker_id=job.worker_id, fencing_token=None, lease_token=None)

    session.close()


def test_task1_strict_fencing_incorrect_token_rejected(tmp_path):
    """Incorrect fencing_token must reject completion, failure, and renew operations."""
    db_file = tmp_path / "fencing_incorrect.db"
    db_url = f"sqlite:///{db_file}"
    run_migrations(db_url)

    engine = create_engine(db_url)
    SessionMaker = sessionmaker(bind=engine)
    session = SessionMaker()

    proj = Project(id="p-fence-inc", name="Fencing Inc", budget_total=100.0, budget_spent=0.0)
    session.add(proj)
    session.commit()

    jm = JobManager(worker_id="worker-active")
    job, _ = jm.prepare_or_claim_job(session, proj.id, "audio", {"track": 2})

    # Incorrect token on renew returns False
    assert jm.renew_lease(session, job.id, worker_id=job.worker_id, fencing_token=999) is False

    # Incorrect token on complete raises JobOwnershipLostError
    with pytest.raises(JobOwnershipLostError):
        jm.complete_job(session, job.id, {"result": "ok"}, worker_id=job.worker_id, fencing_token=999)

    # Incorrect token on fail raises JobOwnershipLostError
    with pytest.raises(JobOwnershipLostError):
        jm.fail_job(session, job.id, "error", worker_id=job.worker_id, fencing_token=999)

    # Incorrect token on assert raises JobOwnershipLostError
    with pytest.raises(JobOwnershipLostError):
        jm.assert_ownership(session, job.id, worker_id=job.worker_id, fencing_token=999)

    session.close()


def test_task1_strict_fencing_expired_lease_rejected(tmp_path):
    """Worker whose lease expired cannot complete, fail, or renew even with valid token."""
    db_file = tmp_path / "fencing_expired.db"
    db_url = f"sqlite:///{db_file}"
    run_migrations(db_url)

    engine = create_engine(db_url)
    SessionMaker = sessionmaker(bind=engine)
    session = SessionMaker()

    proj = Project(id="p-fence-exp", name="Fencing Exp", budget_total=100.0, budget_spent=0.0)
    session.add(proj)
    session.commit()

    jm = JobManager(worker_id="worker-slow")
    job, _ = jm.prepare_or_claim_job(session, proj.id, "render", {"scene": 1}, lease_seconds=1)

    # Simulate expired lease
    past_time = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=60)
    job.lease_expires_at = past_time
    session.commit()

    # Expired lease on renew returns False
    assert jm.renew_lease(session, job.id, worker_id=job.worker_id, fencing_token=job.fencing_token) is False

    # Expired lease on complete raises JobOwnershipLostError
    with pytest.raises(JobOwnershipLostError):
        jm.complete_job(session, job.id, {"rendered": True}, worker_id=job.worker_id, fencing_token=job.fencing_token)

    # Expired lease on fail raises JobOwnershipLostError
    with pytest.raises(JobOwnershipLostError):
        jm.fail_job(session, job.id, "error", worker_id=job.worker_id, fencing_token=job.fencing_token)

    session.close()


def test_task1_strict_fencing_old_worker_heartbeat_and_mutation_after_reclaim(tmp_path):
    """Stale worker cannot heartbeat, complete, or fail after job was reclaimed by new worker."""
    db_file = tmp_path / "fencing_reclaim.db"
    db_url = f"sqlite:///{db_file}"
    run_migrations(db_url)

    engine = create_engine(db_url)
    SessionMaker = sessionmaker(bind=engine)
    session = SessionMaker()

    proj = Project(id="p-fence-rec", name="Fencing Rec", budget_total=100.0, budget_spent=0.0)
    session.add(proj)
    session.commit()

    worker_a = JobManager(worker_id="worker-A")
    worker_b = JobManager(worker_id="worker-B")

    payload = {"task": "heavy_compute"}
    job_a, _ = worker_a.prepare_or_claim_job(session, proj.id, "compute", payload, lease_seconds=1)
    token_a = job_a.fencing_token
    assert token_a == 1

    # Lease expires
    job_a.lease_expires_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=10)
    session.commit()

    # Worker B reclaims
    job_b, action_b = worker_b.prepare_or_claim_job(session, proj.id, "compute", payload, lease_seconds=300)
    assert action_b == "EXECUTE"
    assert job_b.worker_id == "worker-B"
    token_b = job_b.fencing_token
    assert token_b == 2

    # Old Worker A attempts heartbeat renewal -> returns False
    assert worker_a.renew_lease(session, job_a.id, worker_id="worker-A", fencing_token=token_a) is False

    # Old Worker A attempts completion -> rejected
    with pytest.raises(JobOwnershipLostError):
        worker_a.complete_job(session, job_a.id, {"result": "from A"}, worker_id="worker-A", fencing_token=token_a)

    # Old Worker A attempts failure -> rejected
    with pytest.raises(JobOwnershipLostError):
        worker_a.fail_job(session, job_a.id, "crash in A", worker_id="worker-A", fencing_token=token_a)

    # Worker B successfully completes
    completed = worker_b.complete_job(session, job_b.id, {"result": "from B"}, worker_id="worker-B", fencing_token=token_b)
    assert completed.status == "COMPLETED"

    session.close()


# ==============================================================================
# TASK 2: ATOMIC UPLOAD CLAIM AND RECOVERY TESTS
# ==============================================================================

def test_task2_concurrent_upload_claim_exactly_one_winner(tmp_path):
    """Multiple concurrent workers attempting to claim the same DB_COMMITTED upload: exactly one succeeds."""
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    db_file = tmp_path / "upload_concurrency.db"
    db_url = f"sqlite:///{db_file}"
    run_migrations(db_url)

    engine = create_engine(db_url)
    SessionMaker = sessionmaker(bind=engine)

    with SessionMaker() as session:
        proj = Project(id="p-up-conc", name="Upload Conc", budget_total=100.0, budget_spent=0.0)
        session.add(proj)
        session.commit()

        um_init = UploadManager(storage_root=storage_root, worker_id="init-worker")
        temp_f = storage_root / "test.tmp"
        temp_f.write_bytes(b"content")
        dest_f = storage_root / "final.jpg"

        op = um_init.start_staged_upload(
            db=session,
            project_id=proj.id,
            entity_type="character_reference",
            entity_id="c-test",
            temp_path=temp_f,
            destination_path=dest_f
        )
        um_init.mark_db_committed(session, op.id)
        # Simulate unowned or expired claim
        op.worker_id = None
        op.lease_expires_at = None
        session.commit()
        op_id = op.id

    def worker_attempt_claim(wid: str):
        with SessionMaker() as s:
            mgr = UploadManager(storage_root=storage_root, worker_id=wid)
            claimed = mgr.claim_upload_operation(s, op_id, worker_id=wid)
            return wid, (claimed is not None)

    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(worker_attempt_claim, f"contestant-{i}") for i in range(5)]
        for f in concurrent.futures.as_completed(futures):
            results.append(f.result())

    winners = [wid for wid, won in results if won]
    assert len(winners) == 1, f"Expected exactly 1 winner, got: {winners}"


def test_task2_expired_upload_claim_recovery_and_stale_rejection(tmp_path):
    """Expired upload operation can be claimed by worker 2; stale worker 1 cannot finalize or fail."""
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    db_file = tmp_path / "upload_lease_fencing.db"
    db_url = f"sqlite:///{db_file}"
    run_migrations(db_url)

    engine = create_engine(db_url)
    SessionMaker = sessionmaker(bind=engine)
    session = SessionMaker()

    proj = Project(id="p-up-fence", name="Upload Fence", budget_total=100.0, budget_spent=0.0)
    session.add(proj)
    session.commit()

    mgr1 = UploadManager(storage_root=storage_root, worker_id="worker-up-1")
    mgr2 = UploadManager(storage_root=storage_root, worker_id="worker-up-2")

    temp_f = storage_root / "test_file.tmp"
    img_data = create_sample_image(b"valid_payload")
    temp_f.write_bytes(img_data)
    dest_f = storage_root / "dest_file.png"

    sha = hashlib.sha256(img_data).hexdigest()
    op = mgr1.start_staged_upload(
        db=session,
        project_id=proj.id,
        entity_type="character_reference",
        entity_id="c-up-test",
        temp_path=temp_f,
        destination_path=dest_f,
        checksum=sha,
        file_size_bytes=len(img_data)
    )
    mgr1.mark_db_committed(session, op.id)
    session.commit()

    token1 = op.fencing_token

    # Simulate Worker 1 lease expiration
    op.lease_expires_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=10)
    session.commit()

    # Worker 2 reclaims the upload operation
    claimed2 = mgr2.claim_upload_operation(session, op.id, worker_id="worker-up-2")
    assert claimed2 is not None
    assert claimed2.worker_id == "worker-up-2"
    token2 = claimed2.fencing_token
    assert token2 > token1

    # Stale Worker 1 attempts to finalize with token1 -> rejected
    with pytest.raises(UploadOwnershipLostError):
        mgr1.finalize_upload(session, op.id, worker_id="worker-up-1", fencing_token=token1)

    # Stale Worker 1 attempts to fail with token1 -> rejected
    with pytest.raises(UploadOwnershipLostError):
        mgr1.fail_upload(session, op.id, "stale fail", worker_id="worker-up-1", fencing_token=token1)

    # Worker 2 finalizes with token2 -> succeeds
    final_p = mgr2.finalize_upload(session, op.id, worker_id="worker-up-2", fencing_token=token2)
    assert final_p.exists()
    session.refresh(op)
    assert op.status == "FINALIZED"

    session.close()


# ==============================================================================
# TASK 3: DESTINATION INTEGRITY VERIFICATION TESTS
# ==============================================================================

def test_task3_correct_destination_checksum(tmp_path):
    """Destination verified against SHA-256 and size before FINALIZED."""
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    um = UploadManager(storage_root=storage_root)

    db_file = tmp_path / "integ_correct.db"
    db_url = f"sqlite:///{db_file}"
    run_migrations(db_url)

    engine = create_engine(db_url)
    SessionMaker = sessionmaker(bind=engine)
    session = SessionMaker()

    proj = Project(id="p-chk-1", name="Checksum Project")
    session.add(proj)
    session.commit()

    temp_f = storage_root / "good.tmp"
    data = create_sample_image(b"good_image_data")
    temp_f.write_bytes(data)
    dest_f = storage_root / "final_good.png"
    sha = hashlib.sha256(data).hexdigest()

    op = um.start_staged_upload(
        db=session,
        project_id=proj.id,
        entity_type="character_reference",
        entity_id="c-chk-1",
        temp_path=temp_f,
        destination_path=dest_f,
        checksum=sha,
        file_size_bytes=len(data)
    )
    um.mark_db_committed(session, op.id)
    session.commit()

    res = um.finalize_upload(session, op.id, worker_id=op.worker_id, fencing_token=op.fencing_token)
    assert res == dest_f
    assert dest_f.exists()
    assert compute_file_sha256(dest_f) == sha
    session.refresh(op)
    assert op.status == "FINALIZED"

    session.close()


def test_task3_wrong_destination_checksum_and_missing_temp(tmp_path):
    """Destination exists but wrong checksum; temp is missing: upload must fail and preserve old asset."""
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    um = UploadManager(storage_root=storage_root)

    db_file = tmp_path / "integ_wrong.db"
    db_url = f"sqlite:///{db_file}"
    run_migrations(db_url)

    engine = create_engine(db_url)
    SessionMaker = sessionmaker(bind=engine)
    session = SessionMaker()

    proj = Project(id="p-chk-2", name="Checksum Project 2")
    char = Character(id="c-chk-2", project_id=proj.id, name="Hero")
    session.add_all([proj, char])
    session.commit()

    old_asset = storage_root / "old_hero.png"
    old_asset.write_bytes(create_sample_image(b"old_hero"))
    char.reference_image_path = "old_hero.png"
    session.commit()

    temp_f = storage_root / "temp_missing.tmp"
    dest_f = storage_root / "hero_new.png"
    # Destination exists on disk with corrupt/wrong content!
    dest_f.write_bytes(create_sample_image(b"completely_different_corrupt_content"))

    expected_sha = hashlib.sha256(b"original_intended_content_that_was_lost").hexdigest()

    op = um.start_staged_upload(
        db=session,
        project_id=proj.id,
        entity_type="character_reference",
        entity_id=char.id,
        temp_path=temp_f,
        destination_path=dest_f,
        old_asset_path=old_asset,
        checksum=expected_sha,
        file_size_bytes=500
    )
    um.mark_db_committed(session, op.id)
    session.commit()

    assert not temp_f.exists()
    assert dest_f.exists()

    # Finalize should raise ValueError due to checksum mismatch
    with pytest.raises(ValueError) as exc:
        um.finalize_upload(session, op.id, worker_id=op.worker_id, fencing_token=op.fencing_token)

    assert "does not match expected upload checksum" in str(exc.value)

    session.refresh(op)
    assert op.status == "FAILED"
    # Old asset MUST be preserved on disk
    assert old_asset.exists()
    # Entity pointer reverted to old asset
    session.refresh(char)
    assert "old_hero.png" in char.reference_image_path

    session.close()


def test_task3_destination_corrupt_with_valid_temp_performs_atomic_replacement(tmp_path):
    """Destination exists but is corrupt; valid temp exists: atomic replacement restores integrity."""
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    um = UploadManager(storage_root=storage_root)

    db_file = tmp_path / "integ_replace.db"
    db_url = f"sqlite:///{db_file}"
    run_migrations(db_url)

    engine = create_engine(db_url)
    SessionMaker = sessionmaker(bind=engine)
    session = SessionMaker()

    proj = Project(id="p-chk-3", name="Replace Project")
    session.add(proj)
    session.commit()

    temp_f = storage_root / "valid_temp.tmp"
    good_data = create_sample_image(b"good_data_for_recovery")
    temp_f.write_bytes(good_data)
    good_sha = hashlib.sha256(good_data).hexdigest()

    dest_f = storage_root / "final_dest.png"
    dest_f.write_bytes(b"corrupt_earlier_attempt_junk")

    op = um.start_staged_upload(
        db=session,
        project_id=proj.id,
        entity_type="character_reference",
        entity_id="c-replace",
        temp_path=temp_f,
        destination_path=dest_f,
        checksum=good_sha,
        file_size_bytes=len(good_data)
    )
    um.mark_db_committed(session, op.id)
    session.commit()

    # Finalize detects corrupt destination, verifies valid temp, and safely replaces destination!
    res = um.finalize_upload(session, op.id, worker_id=op.worker_id, fencing_token=op.fencing_token)
    assert res == dest_f
    assert compute_file_sha256(dest_f) == good_sha
    session.refresh(op)
    assert op.status == "FINALIZED"

    session.close()


def test_task3_crash_after_rename_and_repeated_recovery(tmp_path):
    """Move already finished on disk before server crashed; recovery detects valid destination and finalizes."""
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    um = UploadManager(storage_root=storage_root)

    db_file = tmp_path / "integ_crash_rec.db"
    db_url = f"sqlite:///{db_file}"
    run_migrations(db_url)

    engine = create_engine(db_url)
    SessionMaker = sessionmaker(bind=engine)
    session = SessionMaker()

    proj = Project(id="p-chk-4", name="Crash Recovery Project")
    session.add(proj)
    session.commit()

    temp_f = storage_root / "moved.tmp"
    data = create_sample_image(b"already_moved_file")
    dest_f = storage_root / "already_at_dest.png"
    # Destination already written
    dest_f.write_bytes(data)
    sha = hashlib.sha256(data).hexdigest()

    op = um.start_staged_upload(
        db=session,
        project_id=proj.id,
        entity_type="character_reference",
        entity_id="c-crash-1",
        temp_path=temp_f,
        destination_path=dest_f,
        checksum=sha,
        file_size_bytes=len(data)
    )
    um.mark_db_committed(session, op.id)
    # Simulate unowned op after crash
    op.worker_id = None
    op.lease_expires_at = None
    session.commit()

    # First recovery run
    stats1 = um.recover_staged_uploads(session)
    assert stats1["finalized"] == 1

    session.refresh(op)
    assert op.status == "FINALIZED"

    # Repeated recovery is idempotent
    stats2 = um.recover_staged_uploads(session)
    assert stats2["finalized"] == 0

    session.close()


# ==============================================================================
# TASK 4: SAFE MIGRATION OF DUPLICATE OPERATION IDENTITIES
# ==============================================================================

def test_task4_migration_duplicate_operation_identities_audit_trail(tmp_path):
    """
    TASK 4:
    - Sets up database at revision 0005.
    - Inserts duplicate operation_identity rows, empty identities, NULL identities, unique identities.
    - Upgrades to revision 0006.
    - Verifies canonical record retains identity.
    - Verifies historical duplicates are re-keyed with full audit provenance.
    - Verifies no financial ledger rows are deleted.
    - Verifies unique constraint prevents future duplicates.
    - Verifies downgrade and re-upgrade safety.
    """
    db_file = tmp_path / "migration_audit.db"
    db_url = f"sqlite:///{db_file}"

    # Upgrade to 0005
    from alembic.config import Config
    from alembic import command
    alembic_cfg = Config("alembic.ini")
    alembic_cfg.set_main_option("sqlalchemy.url", db_url)
    command.upgrade(alembic_cfg, "0005_lease_fencing_and_upload_recovery")

    engine = create_engine(db_url)
    with engine.connect() as conn:
        # Create a test project
        conn.execute(text("INSERT INTO projects (id, name, budget_total, budget_spent) VALUES ('p-mig', 'Mig Proj', 1000.0, 0.0)"))

        # Insert 3 duplicates with identical operation_identity 'dup-flow-tx-1'
        t1 = datetime(2026, 11, 1, 10, 0, 0)
        t2 = datetime(2026, 11, 1, 10, 5, 0)
        t3 = datetime(2026, 11, 1, 10, 10, 0)

        conn.execute(text("""
            INSERT INTO cost_records (id, project_id, action_type, operation_identity, actual_cost, provider, operation, details, created_at)
            VALUES ('rec-canonical', 'p-mig', 'test_act', 'dup-flow-tx-1', 1.5, 'google_flow', 'compute', 'Initial generation charge', :t1)
        """), {"t1": t1})
        conn.execute(text("""
            INSERT INTO cost_records (id, project_id, action_type, operation_identity, actual_cost, provider, operation, details, created_at)
            VALUES ('rec-dup-2', 'p-mig', 'test_act', 'dup-flow-tx-1', 1.5, 'google_flow', 'compute', 'Duplicate retry charge', :t2)
        """), {"t2": t2})
        conn.execute(text("""
            INSERT INTO cost_records (id, project_id, action_type, operation_identity, actual_cost, provider, operation, details, created_at)
            VALUES ('rec-dup-3', 'p-mig', 'test_act', 'dup-flow-tx-1', 1.5, 'google_flow', 'compute', 'Third duplicate charge', :t3)
        """), {"t3": t3})

        # Insert unique record
        conn.execute(text("""
            INSERT INTO cost_records (id, project_id, action_type, operation_identity, actual_cost, provider, operation, details, created_at)
            VALUES ('rec-uniq', 'p-mig', 'test_act', 'unique-tx-99', 3.0, 'google_gemini', 'api', 'Unique call', :t1)
        """), {"t1": t1})

        # Insert empty string identity
        conn.execute(text("""
            INSERT INTO cost_records (id, project_id, action_type, operation_identity, actual_cost, provider, operation, details, created_at)
            VALUES ('rec-empty', 'p-mig', 'test_act', '', 0.5, 'local_ffmpeg', 'export', 'Empty identity record', :t1)
        """), {"t1": t1})

        # Insert NULL identity
        conn.execute(text("""
            INSERT INTO cost_records (id, project_id, action_type, operation_identity, actual_cost, provider, operation, details, created_at)
            VALUES ('rec-null', 'p-mig', 'test_act', NULL, 0.5, 'local_ffmpeg', 'export', 'Null identity record', :t1)
        """), {"t1": t1})

        conn.commit()
    engine.dispose()

    # Now run upgrade to 0006 (and head)
    command.upgrade(alembic_cfg, "head")

    engine2 = create_engine(db_url)
    with engine2.connect() as conn:
        # 1. Verify row count: Exactly 6 rows, NO financial rows deleted!
        count = conn.execute(text("SELECT count(*) FROM cost_records")).scalar()
        assert count == 6

        # 2. Canonical record retained its exact operation_identity
        can = conn.execute(text("SELECT operation_identity, details FROM cost_records WHERE id = 'rec-canonical'")).fetchone()
        assert can[0] == "dup-flow-tx-1"

        # 3. Historical duplicate #2 was re-keyed with deterministic SHA-256 suffix and has audit note
        dup2 = conn.execute(text("SELECT operation_identity, details FROM cost_records WHERE id = 'rec-dup-2'")).fetchone()
        assert dup2[0].startswith("dup-flow-tx-1#dup-1-")
        assert len(dup2[0]) == len("dup-flow-tx-1#dup-1-") + 16
        assert "MIGRATION_AUDIT: Historical duplicate #1" in dup2[1]
        assert "Canonical record ID: 'rec-canonical'" in dup2[1]

        # 4. Historical duplicate #3 was re-keyed with deterministic SHA-256 suffix and has audit note
        dup3 = conn.execute(text("SELECT operation_identity, details FROM cost_records WHERE id = 'rec-dup-3'")).fetchone()
        assert dup3[0].startswith("dup-flow-tx-1#dup-2-")
        assert len(dup3[0]) == len("dup-flow-tx-1#dup-2-") + 16
        assert "MIGRATION_AUDIT: Historical duplicate #2" in dup3[1]
        assert "Canonical record ID: 'rec-canonical'" in dup3[1]

        # 5. Unique record unchanged
        uniq = conn.execute(text("SELECT operation_identity FROM cost_records WHERE id = 'rec-uniq'")).fetchone()
        assert uniq[0] == "unique-tx-99"

        # 6. Empty identity converted to NULL
        empty_rec = conn.execute(text("SELECT operation_identity FROM cost_records WHERE id = 'rec-empty'")).fetchone()
        assert empty_rec[0] is None

        # 7. NULL identity remained NULL
        null_rec = conn.execute(text("SELECT operation_identity FROM cost_records WHERE id = 'rec-null'")).fetchone()
        assert null_rec[0] is None

        # 8. Uniqueness constraint is enforced: inserting existing canonical identity raises IntegrityError
        from sqlalchemy.exc import IntegrityError
        with pytest.raises(IntegrityError):
            conn.execute(text("""
                INSERT INTO cost_records (id, project_id, action_type, operation_identity, actual_cost, provider, operation, details)
                VALUES ('rec-violate', 'p-mig', 'test_act', 'dup-flow-tx-1', 1.0, 'google_flow', 'compute', 'Violation')
            """))

    engine2.dispose()

    # Test downgrade to 0005
    command.downgrade(alembic_cfg, "0005_lease_fencing_and_upload_recovery")
    # And re-upgrade to head
    command.upgrade(alembic_cfg, "head")
