"""
Tests for AI Film Studio – Reliability Fixes 3:
1. Strict upload worker fencing & preservation of old assets until DB finalization.
2. In-place replacement backup & crash recovery between file placement and DB commit.
3. Strict fencing tokens required for renew_upload_lease, finalize_upload, fail_upload.
4. Safe operation identity deduplication with SHA-256 suffixes for long identities (>128 chars),
   identical prefixes, multiple duplicates, and repeated migration idempotency.
5. Safe staged upload expiration checking lease_expires_at, active upload protection,
   multi-instance atomic claim, and interrupted cleanup recovery.
"""

import os
import time
import sqlite3
import hashlib
import concurrent.futures
from pathlib import Path
from datetime import datetime, timezone, timedelta

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from backend.app.database import run_migrations
from backend.app.models.entities import Project, Character, Location, UploadOperation, CostRecord
from backend.app.services.upload_manager import (
    UploadManager,
    UploadOwnershipLostError,
    _verify_file_integrity
)
from backend.app.services.security import compute_file_sha256


def create_sample_image(content: bytes = b"test_img") -> bytes:
    # 1x1 GIF
    return b"GIF89a\x01\x00\x01\x00\x80\x00\x00\xff\xff\xff\x00\x00\x00!\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;" + content


# ==============================================================================
# TASK 1: PREVENT STALE UPLOAD WORKERS FROM DELETING OLD ASSETS
# ==============================================================================

def test_task1_stale_worker_cannot_delete_old_asset_on_reclaim(tmp_path):
    """
    Simulates:
    - Worker A begins finalization.
    - Worker A's lease expires / Worker B reclaims the operation.
    - Worker A attempts to finalize: rejected with UploadOwnershipLostError.
    - Old asset remains intact on disk!
    - Worker B finalizes: succeeds, new asset verified, old asset safely cleaned.
    """
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    um_a = UploadManager(storage_root=storage_root, worker_id="worker-A")
    um_b = UploadManager(storage_root=storage_root, worker_id="worker-B")

    db_file = tmp_path / "stale_upload.db"
    db_url = f"sqlite:///{db_file}"
    run_migrations(db_url)

    engine = create_engine(db_url)
    SessionMaker = sessionmaker(bind=engine)
    session = SessionMaker()

    proj = Project(id="p-t1", name="Fencing Project")
    char = Character(id="c-t1", project_id=proj.id, name="Hero")
    session.add_all([proj, char])
    session.commit()

    ref_dir = storage_root / "projects" / "p-t1" / "references"
    ref_dir.mkdir(parents=True, exist_ok=True)
    old_asset = ref_dir / "hero_v1.png"
    old_asset.write_bytes(create_sample_image(b"original_hero_v1"))
    char.reference_image_path = "projects/p-t1/references/hero_v1.png"
    session.commit()

    temp_dir = storage_root / "temp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    temp_file = temp_dir / "hero_v2.tmp"
    new_data = create_sample_image(b"new_hero_v2")
    temp_file.write_bytes(new_data)
    new_sha = hashlib.sha256(new_data).hexdigest()
    final_dest = ref_dir / "hero_v2.png"

    # Worker A starts staged upload
    op = um_a.start_staged_upload(
        db=session,
        project_id=proj.id,
        entity_type="character_reference",
        entity_id=char.id,
        temp_path=temp_file,
        destination_path=final_dest,
        old_asset_path=old_asset,
        checksum=new_sha,
        file_size_bytes=len(new_data)
    )
    assert op.worker_id == "worker-A"
    assert op.fencing_token == 1
    token_a = op.fencing_token

    char.reference_image_path = "projects/p-t1/references/hero_v2.png"
    op = um_a.mark_db_committed(session, op.id)
    session.commit()

    # Worker A's lease expires
    past = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=10)
    session.execute(
        text("UPDATE upload_operations SET lease_expires_at = :past WHERE id = :id"),
        {"past": past, "id": op.id}
    )
    session.commit()

    # Worker B reclaims the operation
    claimed_b = um_b.claim_upload_operation(session, op.id, worker_id="worker-B")
    assert claimed_b is not None
    assert claimed_b.worker_id == "worker-B"
    assert claimed_b.fencing_token == 2
    token_b = claimed_b.fencing_token

    # Stale Worker A attempts to continue and finalize
    with pytest.raises(UploadOwnershipLostError):
        um_a.finalize_upload(session, op.id, worker_id="worker-A", fencing_token=token_a)

    # CRITICAL INVARIANT: Old asset MUST still exist on disk!
    assert old_asset.exists()
    assert old_asset.read_bytes() == create_sample_image(b"original_hero_v1")

    # Winning Worker B finalizes
    res = um_b.finalize_upload(session, op.id, worker_id="worker-B", fencing_token=token_b)
    assert res == final_dest
    assert final_dest.exists()
    assert compute_file_sha256(final_dest) == new_sha

    # Now that winning DB transition committed, old asset is safely cleaned up
    assert not old_asset.exists()

    session.refresh(op)
    assert op.status == "FINALIZED"
    session.close()


def test_task1_recovery_after_crash_between_file_placement_and_db_commit(tmp_path):
    """
    Simulates crash right after moving file to destination before DB commit:
    - Destination already exists and is valid.
    - Status is still DB_COMMITTED.
    - Old asset is still preserved.
    - Recovery reclaims operation, detects valid destination, commits FINALIZED, and cleans old asset.
    """
    storage_root = tmp_path / "storage_crash"
    storage_root.mkdir()
    um = UploadManager(storage_root=storage_root, worker_id="worker-crash")

    db_file = tmp_path / "crash_placement.db"
    db_url = f"sqlite:///{db_file}"
    run_migrations(db_url)

    engine = create_engine(db_url)
    SessionMaker = sessionmaker(bind=engine)
    session = SessionMaker()

    proj = Project(id="p-crash", name="Crash Test Project")
    char = Character(id="c-crash", project_id=proj.id, name="Hero")
    session.add_all([proj, char])
    session.commit()

    ref_dir = storage_root / "projects" / "p-crash" / "references"
    ref_dir.mkdir(parents=True, exist_ok=True)
    old_asset = ref_dir / "old_hero.png"
    old_asset.write_bytes(create_sample_image(b"old_data"))

    temp_dir = storage_root / "temp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    temp_file = temp_dir / "new_hero.tmp"
    new_data = create_sample_image(b"new_data")
    new_sha = hashlib.sha256(new_data).hexdigest()
    final_dest = ref_dir / "new_hero.png"

    op = um.start_staged_upload(
        db=session,
        project_id=proj.id,
        entity_type="character_reference",
        entity_id=char.id,
        temp_path=temp_file,
        destination_path=final_dest,
        old_asset_path=old_asset,
        checksum=new_sha,
        file_size_bytes=len(new_data)
    )
    um.mark_db_committed(session, op.id)
    session.commit()

    # Simulate placement finished, but crash happened before DB status updated to FINALIZED
    final_dest.write_bytes(new_data)
    if temp_file.exists():
        temp_file.unlink()

    # Old asset is still present
    assert old_asset.exists()

    # Expire lease to simulate dead worker
    past = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=10)
    session.execute(
        text("UPDATE upload_operations SET lease_expires_at = :past WHERE id = :id"),
        {"past": past, "id": op.id}
    )
    session.commit()

    # Startup recovery runs
    rec_mgr = UploadManager(storage_root=storage_root, worker_id="recovery-worker")
    stats = rec_mgr.recover_staged_uploads(session)
    assert stats["finalized"] == 1

    session.refresh(op)
    assert op.status == "FINALIZED"
    assert final_dest.exists()
    assert not old_asset.exists()

    session.close()


def test_task1_in_place_replacement_preserves_old_asset_on_rejection(tmp_path):
    """
    Tests dest_p == old_p scenario (replacing asset in-place).
    If worker loses ownership, backup of old_p must be safely restored.
    """
    storage_root = tmp_path / "storage_inplace"
    storage_root.mkdir()
    um = UploadManager(storage_root=storage_root, worker_id="worker-1")

    db_file = tmp_path / "inplace.db"
    db_url = f"sqlite:///{db_file}"
    run_migrations(db_url)

    engine = create_engine(db_url)
    SessionMaker = sessionmaker(bind=engine)
    session = SessionMaker()

    proj = Project(id="p-inp", name="In-place Project")
    session.add(proj)
    session.commit()

    dest_file = storage_root / "same_file.png"
    old_data = create_sample_image(b"original_inplace_content")
    dest_file.write_bytes(old_data)

    temp_file = storage_root / "new_content.tmp"
    new_data = create_sample_image(b"new_replacement_content")
    temp_file.write_bytes(new_data)
    new_sha = hashlib.sha256(new_data).hexdigest()

    op = um.start_staged_upload(
        db=session,
        project_id=proj.id,
        entity_type="character_reference",
        entity_id="c-inp",
        temp_path=temp_file,
        destination_path=dest_file,
        old_asset_path=dest_file,  # same path!
        checksum=new_sha,
        file_size_bytes=len(new_data)
    )
    um.mark_db_committed(session, op.id)
    session.commit()

    # Expire lease before finalize so conditional DB update fails
    past = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=5)
    session.execute(
        text("UPDATE upload_operations SET lease_expires_at = :past WHERE id = :id"),
        {"past": past, "id": op.id}
    )
    session.commit()

    with pytest.raises(UploadOwnershipLostError):
        um.finalize_upload(session, op.id, worker_id="worker-1", fencing_token=op.fencing_token)

    # In-place file MUST be restored to original content!
    assert dest_file.exists()
    assert dest_file.read_bytes() == old_data

    session.close()


# ==============================================================================
# TASK 2: STRICT UPLOAD FENCING
# ==============================================================================

def test_task2_strict_upload_fencing_missing_tokens_rejected(tmp_path):
    """
    Verifies that renew_upload_lease, finalize_upload, and fail_upload
    strictly reject missing fencing_token with UploadOwnershipLostError.
    """
    storage_root = tmp_path / "storage_strict"
    storage_root.mkdir()
    um = UploadManager(storage_root=storage_root, worker_id="worker-strict")

    db_file = tmp_path / "strict_fencing.db"
    db_url = f"sqlite:///{db_file}"
    run_migrations(db_url)

    engine = create_engine(db_url)
    SessionMaker = sessionmaker(bind=engine)
    session = SessionMaker()

    proj = Project(id="p-strict", name="Strict Project")
    session.add(proj)
    session.commit()

    temp_f = storage_root / "strict.tmp"
    data = create_sample_image(b"strict_data")
    temp_f.write_bytes(data)
    dest_f = storage_root / "strict.png"
    sha = hashlib.sha256(data).hexdigest()

    op = um.start_staged_upload(
        db=session,
        project_id=proj.id,
        entity_type="character_reference",
        entity_id="c-strict",
        temp_path=temp_f,
        destination_path=dest_f,
        checksum=sha,
        file_size_bytes=len(data)
    )
    um.mark_db_committed(session, op.id)
    session.commit()

    # 1. renew_upload_lease without fencing_token MUST raise
    with pytest.raises(UploadOwnershipLostError) as exc1:
        um.renew_upload_lease(session, op.id, worker_id="worker-strict", fencing_token=None)
    assert "Explicit fencing_token is required" in str(exc1.value)

    # 2. finalize_upload without fencing_token MUST raise
    with pytest.raises(UploadOwnershipLostError) as exc2:
        um.finalize_upload(session, op.id, worker_id="worker-strict", fencing_token=None)
    assert "Explicit fencing_token is required" in str(exc2.value)

    # 3. fail_upload without fencing_token MUST raise
    with pytest.raises(UploadOwnershipLostError) as exc3:
        um.fail_upload(session, op.id, "error", worker_id="worker-strict", fencing_token=None)
    assert "Explicit fencing_token is required" in str(exc3.value)

    session.close()


def test_task2_mismatched_worker_or_expired_lease_rejected(tmp_path):
    """Verifies that operations are rejected when worker_id or fencing_token mismatch, or lease is expired."""
    storage_root = tmp_path / "storage_mismatch"
    storage_root.mkdir()
    um = UploadManager(storage_root=storage_root, worker_id="worker-orig")

    db_file = tmp_path / "mismatch.db"
    db_url = f"sqlite:///{db_file}"
    run_migrations(db_url)

    engine = create_engine(db_url)
    SessionMaker = sessionmaker(bind=engine)
    session = SessionMaker()

    proj = Project(id="p-mis", name="Mismatch Project")
    session.add(proj)
    session.commit()

    temp_f = storage_root / "mis.tmp"
    data = create_sample_image(b"mis_data")
    temp_f.write_bytes(data)
    dest_f = storage_root / "mis.png"

    op = um.start_staged_upload(
        db=session,
        project_id=proj.id,
        entity_type="character_reference",
        entity_id="c-mis",
        temp_path=temp_f,
        destination_path=dest_f,
        checksum=hashlib.sha256(data).hexdigest(),
        file_size_bytes=len(data)
    )
    um.mark_db_committed(session, op.id)
    session.commit()

    # Wrong worker
    with pytest.raises(UploadOwnershipLostError):
        um.finalize_upload(session, op.id, worker_id="impostor-worker", fencing_token=op.fencing_token)

    # Wrong token
    with pytest.raises(UploadOwnershipLostError):
        um.finalize_upload(session, op.id, worker_id="worker-orig", fencing_token=999)

    # Expired lease
    past = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=1)
    session.execute(
        text("UPDATE upload_operations SET lease_expires_at = :past WHERE id = :id"),
        {"past": past, "id": op.id}
    )
    session.commit()

    with pytest.raises(UploadOwnershipLostError):
        um.finalize_upload(session, op.id, worker_id="worker-orig", fencing_token=op.fencing_token)

    session.close()


# ==============================================================================
# TASK 3: SAFE OPERATION IDENTITY MIGRATION
# ==============================================================================

def test_task3_long_identities_and_prefix_collision_deduplication(tmp_path):
    """
    Tests Task 3 requirements:
    - Original identities longer than 128 characters.
    - Similar original identities with identical prefixes.
    - Multiple historical duplicates.
    - Deterministic SHA-256 unique suffixes.
    - Preserving complete original identity in migration audit details.
    - Unique constraint enforced.
    - Repeated migration / downgrade and re-upgrade verification.
    """
    from alembic.config import Config
    from alembic import command

    db_file = tmp_path / "long_identities_migration.db"
    db_url = f"sqlite:///{db_file}"

    alembic_cfg = Config("alembic.ini")
    alembic_cfg.set_main_option("sqlalchemy.url", db_url)
    command.upgrade(alembic_cfg, "0005_lease_fencing_and_upload_recovery")

    engine = create_engine(db_url)
    with engine.connect() as conn:
        conn.execute(text("INSERT INTO projects (id, name, budget_total, budget_spent) VALUES ('p-t3', 'Task 3 Proj', 5000.0, 0.0)"))

        # 1. Long identity (>140 chars) with duplicates
        long_prefix = "workflow-flow-video-generation-attempt-composite-shot-sequence-very-long-prefix-" * 2
        long_ident_a = f"{long_prefix}TAIL_A"
        long_ident_b = f"{long_prefix}TAIL_B"  # Same prefix, different tail

        t1 = datetime(2026, 11, 1, 8, 0, 0)
        t2 = datetime(2026, 11, 1, 8, 1, 0)
        t3 = datetime(2026, 11, 1, 8, 2, 0)
        t4 = datetime(2026, 11, 1, 8, 3, 0)

        # 3 copies of long_ident_a
        conn.execute(text("""
            INSERT INTO cost_records (id, project_id, action_type, operation_identity, actual_cost, provider, operation, details, created_at)
            VALUES ('rec-a1', 'p-t3', 'test_act', :ident, 2.0, 'google_flow', 'gen', 'Canonical A', :t1)
        """), {"ident": long_ident_a, "t1": t1})
        conn.execute(text("""
            INSERT INTO cost_records (id, project_id, action_type, operation_identity, actual_cost, provider, operation, details, created_at)
            VALUES ('rec-a2', 'p-t3', 'test_act', :ident, 2.0, 'google_flow', 'gen', 'Duplicate A2', :t2)
        """), {"ident": long_ident_a, "t2": t2})
        conn.execute(text("""
            INSERT INTO cost_records (id, project_id, action_type, operation_identity, actual_cost, provider, operation, details, created_at)
            VALUES ('rec-a3', 'p-t3', 'test_act', :ident, 2.0, 'google_flow', 'gen', 'Duplicate A3', :t3)
        """), {"ident": long_ident_a, "t3": t3})

        # 2 copies of long_ident_b (identical prefix, different tail)
        conn.execute(text("""
            INSERT INTO cost_records (id, project_id, action_type, operation_identity, actual_cost, provider, operation, details, created_at)
            VALUES ('rec-b1', 'p-t3', 'test_act', :ident, 3.0, 'google_flow', 'gen', 'Canonical B', :t1)
        """), {"ident": long_ident_b, "t1": t1})
        conn.execute(text("""
            INSERT INTO cost_records (id, project_id, action_type, operation_identity, actual_cost, provider, operation, details, created_at)
            VALUES ('rec-b2', 'p-t3', 'test_act', :ident, 3.0, 'google_flow', 'gen', 'Duplicate B2', :t4)
        """), {"ident": long_ident_b, "t4": t4})

        conn.commit()
    engine.dispose()

    # Upgrade to head (runs 0006)
    command.upgrade(alembic_cfg, "head")

    engine2 = create_engine(db_url)
    with engine2.connect() as conn:
        # Check all 5 records preserved
        all_recs = conn.execute(text("SELECT id, operation_identity, details FROM cost_records ORDER BY id ASC")).fetchall()
        assert len(all_recs) == 5

        rec_dict = {r[0]: (r[1], r[2]) for r in all_recs}

        # 1. Canonical records retained exact original identity
        assert rec_dict['rec-a1'][0] == long_ident_a
        assert rec_dict['rec-b1'][0] == long_ident_b

        # 2. Duplicate records re-keyed and never collide
        ident_a2 = rec_dict['rec-a2'][0]
        ident_a3 = rec_dict['rec-a3'][0]
        ident_b2 = rec_dict['rec-b2'][0]

        all_identities = {rec_dict[k][0] for k in rec_dict}
        assert len(all_identities) == 5  # Completely unique!

        # Check deterministic SHA-256 suffix
        assert "#dup-1-" in ident_a2
        assert "#dup-2-" in ident_a3
        assert "#dup-1-" in ident_b2
        # Verify A2 and B2 did not collide even though their prefixes are identical!
        assert ident_a2 != ident_b2

        # Check full provenance preserved in details
        assert f"operation_identity '{long_ident_a}'" in rec_dict['rec-a2'][1]
        assert f"Canonical record ID: 'rec-a1'" in rec_dict['rec-a2'][1]
        assert f"operation_identity '{long_ident_b}'" in rec_dict['rec-b2'][1]
        assert f"Canonical record ID: 'rec-b1'" in rec_dict['rec-b2'][1]

        # 3. Unique constraint enforcement: attempting to insert duplicate identity raises IntegrityError
        with pytest.raises(Exception):
            conn.execute(text("""
                INSERT INTO cost_records (id, project_id, action_type, operation_identity, actual_cost, provider, operation, details, created_at)
                VALUES ('rec-violator', 'p-t3', 'test_act', :ident, 1.0, 'local_ffmpeg', 'transcode', 'duplicate attempt', :now)
            """), {"ident": long_ident_a, "now": datetime.now(timezone.utc).replace(tzinfo=None)})
            conn.commit()

    engine2.dispose()

    # 4. Repeated migration idempotency: Downgrade to 0005 and Re-upgrade to head
    command.downgrade(alembic_cfg, "0005_lease_fencing_and_upload_recovery")
    command.upgrade(alembic_cfg, "head")

    engine3 = create_engine(db_url)
    with engine3.connect() as conn:
        count = conn.execute(text("SELECT count(*) FROM cost_records")).scalar()
        assert count == 5
    engine3.dispose()


# ==============================================================================
# TASK 4: SAFE STAGED UPLOAD EXPIRATION
# ==============================================================================

def test_task4_active_staged_upload_never_cleaned(tmp_path):
    """Active staged upload (lease_expires_at in the future) is NEVER deleted or failed."""
    storage_root = tmp_path / "storage_active"
    storage_root.mkdir()
    um = UploadManager(storage_root=storage_root, worker_id="worker-active")

    db_file = tmp_path / "active_staged.db"
    db_url = f"sqlite:///{db_file}"
    run_migrations(db_url)

    engine = create_engine(db_url)
    SessionMaker = sessionmaker(bind=engine)
    session = SessionMaker()

    proj = Project(id="p-act", name="Active Proj")
    session.add(proj)
    session.commit()

    temp_f = storage_root / "active.tmp"
    temp_f.write_bytes(create_sample_image(b"active_file"))
    dest_f = storage_root / "active.png"

    op = um.start_staged_upload(
        db=session,
        project_id=proj.id,
        entity_type="character_reference",
        entity_id="c-act",
        temp_path=temp_f,
        destination_path=dest_f
    )
    # Active lease (5 mins in the future)
    assert op.status == "STAGED"
    assert op.lease_expires_at > datetime.now(timezone.utc).replace(tzinfo=None)

    # Attempt to clean active upload
    cleaned = um.claim_and_clean_staged_upload(session, op.id, worker_id="worker-cleaner")
    assert cleaned is False

    # Temp file remains untouched
    assert temp_f.exists()

    session.refresh(op)
    assert op.status == "STAGED"

    # Full recovery coordinator also skips it
    stats = um.recover_staged_uploads(session)
    assert stats["staged_cleaned"] == 0
    assert temp_f.exists()

    session.close()


def test_task4_expired_staged_upload_cleaned(tmp_path):
    """Expired staged upload (lease_expires_at < now) is claimed and cleaned."""
    storage_root = tmp_path / "storage_exp"
    storage_root.mkdir()
    um = UploadManager(storage_root=storage_root, worker_id="worker-exp")

    db_file = tmp_path / "exp_staged.db"
    db_url = f"sqlite:///{db_file}"
    run_migrations(db_url)

    engine = create_engine(db_url)
    SessionMaker = sessionmaker(bind=engine)
    session = SessionMaker()

    proj = Project(id="p-exp", name="Expired Proj")
    session.add(proj)
    session.commit()

    temp_f = storage_root / "expired.tmp"
    temp_f.write_bytes(create_sample_image(b"expired_file"))
    dest_f = storage_root / "expired.png"

    op = um.start_staged_upload(
        db=session,
        project_id=proj.id,
        entity_type="character_reference",
        entity_id="c-exp",
        temp_path=temp_f,
        destination_path=dest_f
    )

    # Set lease to past
    past = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=10)
    session.execute(
        text("UPDATE upload_operations SET lease_expires_at = :past WHERE id = :id"),
        {"past": past, "id": op.id}
    )
    session.commit()

    cleaned = um.claim_and_clean_staged_upload(session, op.id, worker_id="worker-cleaner")
    assert cleaned is True
    assert not temp_f.exists()

    session.refresh(op)
    assert op.status == "FAILED"
    assert "expired" in op.error_message.lower()

    # Second cleanup attempt is a no-op (idempotent)
    second_cleaned = um.claim_and_clean_staged_upload(session, op.id, worker_id="worker-cleaner-2")
    assert second_cleaned is False

    session.close()


def test_task4_concurrent_workers_cleaning_same_expired_staged(tmp_path):
    """
    Two workers concurrently attempt to clean the same expired staged upload.
    Exactly ONE worker succeeds and cleans the file; the other gets False.
    """
    storage_root = tmp_path / "storage_race"
    storage_root.mkdir()

    db_file = tmp_path / "race_staged.db"
    db_url = f"sqlite:///{db_file}"
    run_migrations(db_url)

    engine = create_engine(db_url, pool_size=5, max_overflow=5)
    SessionMaker = sessionmaker(bind=engine)
    init_session = SessionMaker()

    proj = Project(id="p-race", name="Race Proj")
    init_session.add(proj)
    init_session.commit()

    temp_f = storage_root / "race.tmp"
    temp_f.write_bytes(create_sample_image(b"race_file"))
    dest_f = storage_root / "race.png"

    um = UploadManager(storage_root=storage_root, worker_id="init-worker")
    op = um.start_staged_upload(
        db=init_session,
        project_id=proj.id,
        entity_type="character_reference",
        entity_id="c-race",
        temp_path=temp_f,
        destination_path=dest_f
    )
    # Expire lease
    past = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=15)
    init_session.execute(
        text("UPDATE upload_operations SET lease_expires_at = :past WHERE id = :id"),
        {"past": past, "id": op.id}
    )
    init_session.commit()
    op_id = op.id
    init_session.close()

    def attempt_clean(worker_name: str) -> bool:
        mgr = UploadManager(storage_root=storage_root, worker_id=worker_name)
        with SessionMaker() as sess:
            return mgr.claim_and_clean_staged_upload(sess, op_id, worker_id=worker_name)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(attempt_clean, "cleaner-1")
        f2 = executor.submit(attempt_clean, "cleaner-2")
        results = [f1.result(), f2.result()]

    # Exactly one succeeded!
    assert results.count(True) == 1
    assert results.count(False) == 1
    assert not temp_f.exists()

    engine.dispose()


def test_task4_recovery_after_cleanup_interruption(tmp_path):
    """
    Simulates crash right after DB update marked status='FAILED' before unlinking temp file.
    Recovery pass detects orphaned temp file and unlinks it cleanly.
    """
    storage_root = tmp_path / "storage_orphaned"
    storage_root.mkdir()
    temp_dir = storage_root / "temp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    um = UploadManager(storage_root=storage_root, temp_root=temp_dir, worker_id="worker-orphan")

    db_file = tmp_path / "orphan_staged.db"
    db_url = f"sqlite:///{db_file}"
    run_migrations(db_url)

    engine = create_engine(db_url)
    SessionMaker = sessionmaker(bind=engine)
    session = SessionMaker()

    proj = Project(id="p-orph", name="Orphan Proj")
    session.add(proj)
    session.commit()

    temp_f = temp_dir / "interrupted.tmp"
    temp_f.write_bytes(create_sample_image(b"interrupted_content"))
    dest_f = storage_root / "interrupted.png"

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    # DB status is already FAILED (interrupted during unlinking)
    op = UploadOperation(
        project_id=proj.id,
        entity_type="character_reference",
        entity_id="c-orph",
        temp_path=str(temp_f),
        destination_path=str(dest_f),
        status="FAILED",
        error_message="Cleanup interrupted",
        fencing_token=1,
        created_at=now,
        updated_at=now
    )
    session.add(op)
    session.commit()

    assert temp_f.exists()

    # Next recovery coordinator run cleans orphaned .tmp file
    stats = um.recover_staged_uploads(session)
    assert not temp_f.exists()

    session.close()


# ==============================================================================
# TASK 1 & 2 REGRESSION TESTS: UNTRACKED SAFE AGE & CHECKSUM VERIFICATION
# ==============================================================================

def test_untracked_active_temp_file_not_deleted_by_recovery(tmp_path):
    """
    TASK 1: An active in-progress upload writes a .tmp file before DB commit.
    Recovery MUST NOT delete recently created untracked temporary files.
    """
    storage_root = tmp_path / "storage_untracked_active"
    storage_root.mkdir()
    temp_dir = storage_root / "temp"
    temp_dir.mkdir(parents=True, exist_ok=True)

    db_file = tmp_path / "untracked_active.db"
    db_url = f"sqlite:///{db_file}"
    run_migrations(db_url)

    engine = create_engine(db_url)
    SessionMaker = sessionmaker(bind=engine)
    session = SessionMaker()

    # Create an in-flight temporary file written seconds ago (no DB record yet!)
    active_temp = temp_dir / "streaming_upload_123.tmp"
    active_temp.write_bytes(b"actively_streaming_bytes_not_yet_in_db")

    um = UploadManager(storage_root=storage_root, temp_root=temp_dir, orphan_min_age_seconds=1800)

    # Run recovery coordinator
    stats = um.recover_staged_uploads(session)

    # In-flight temporary file MUST NOT be deleted!
    assert active_temp.exists()
    assert active_temp.read_bytes() == b"actively_streaming_bytes_not_yet_in_db"
    session.close()


def test_abandoned_untracked_temp_file_cleaned_after_age_threshold(tmp_path):
    """
    TASK 1: An abandoned untracked temporary file older than safe age threshold IS cleaned.
    """
    storage_root = tmp_path / "storage_abandoned"
    storage_root.mkdir()
    temp_dir = storage_root / "temp"
    temp_dir.mkdir(parents=True, exist_ok=True)

    db_file = tmp_path / "abandoned.db"
    db_url = f"sqlite:///{db_file}"
    run_migrations(db_url)

    engine = create_engine(db_url)
    SessionMaker = sessionmaker(bind=engine)
    session = SessionMaker()

    # Create an abandoned temporary file from 2 hours ago
    abandoned_temp = temp_dir / "abandoned_upload_old.tmp"
    abandoned_temp.write_bytes(b"abandoned_stale_content")
    old_time = time.time() - 7200
    os.utime(abandoned_temp, (old_time, old_time))

    um = UploadManager(storage_root=storage_root, temp_root=temp_dir, orphan_min_age_seconds=1800)

    # Run recovery coordinator
    stats = um.recover_staged_uploads(session)

    # Abandoned file MUST be cleaned up
    assert not abandoned_temp.exists()
    assert stats["staged_cleaned"] >= 1
    session.close()


def test_concurrent_recovery_attempts_from_multiple_workers(tmp_path):
    """
    TASK 1 & 4: Concurrent recovery from multiple workers:
    - 1 DB_COMMITTED upload
    - 1 expired STAGED upload
    - 1 active STAGED upload
    - 1 recent untracked in-flight temp file
    - 1 old abandoned untracked temp file
    Runs recovery across 3 concurrent threads; verifies idempotency and correctness.
    """
    storage_root = tmp_path / "storage_concurrent_rec"
    storage_root.mkdir()
    temp_dir = storage_root / "temp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    dest_dir = storage_root / "dest"
    dest_dir.mkdir(parents=True, exist_ok=True)

    db_file = tmp_path / "concurrent_rec.db"
    db_url = f"sqlite:///{db_file}"
    run_migrations(db_url)

    engine = create_engine(db_url, pool_size=5, max_overflow=5)
    SessionMaker = sessionmaker(bind=engine)
    init_session = SessionMaker()

    proj = Project(id="p-con-rec", name="Concurrent Rec Proj")
    init_session.add(proj)
    init_session.commit()

    # 1. DB_COMMITTED upload ready for finalization
    data_comm = create_sample_image(b"committed_file")
    sha_comm = hashlib.sha256(data_comm).hexdigest()
    temp_comm = temp_dir / "comm.tmp"
    temp_comm.write_bytes(data_comm)
    dest_comm = dest_dir / "comm.png"

    um_init = UploadManager(storage_root=storage_root, temp_root=temp_dir, worker_id="init")
    op_comm = um_init.start_staged_upload(
        db=init_session,
        project_id="p-con-rec",
        entity_type="character_reference",
        entity_id="c-comm",
        temp_path=temp_comm,
        destination_path=dest_comm,
        checksum=sha_comm,
        file_size_bytes=len(data_comm)
    )
    um_init.mark_db_committed(init_session, op_comm.id)
    # Clear worker claim to make it eligible for recovery
    op_comm.worker_id = None
    op_comm.lease_expires_at = None
    init_session.commit()

    # 2. Expired STAGED upload
    temp_exp = temp_dir / "expired_staged.tmp"
    temp_exp.write_bytes(b"expired_data")
    op_exp = um_init.start_staged_upload(
        db=init_session,
        project_id="p-con-rec",
        entity_type="character_reference",
        entity_id="c-exp",
        temp_path=temp_exp,
        destination_path=dest_dir / "expired_staged.png"
    )
    past = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=20)
    init_session.execute(
        text("UPDATE upload_operations SET lease_expires_at = :past WHERE id = :id"),
        {"past": past, "id": op_exp.id}
    )
    init_session.commit()

    # 3. Active STAGED upload (future lease)
    temp_act = temp_dir / "active_staged.tmp"
    temp_act.write_bytes(b"active_data")
    op_act = um_init.start_staged_upload(
        db=init_session,
        project_id="p-con-rec",
        entity_type="character_reference",
        entity_id="c-act",
        temp_path=temp_act,
        destination_path=dest_dir / "active_staged.png"
    )

    # 4. Recent untracked temporary file (written right now)
    recent_untracked = temp_dir / "recent_untracked.tmp"
    recent_untracked.write_bytes(b"recent_bytes")

    # 5. Old abandoned untracked temporary file (2 hours old)
    old_abandoned = temp_dir / "old_abandoned.tmp"
    old_abandoned.write_bytes(b"abandoned_bytes")
    old_t = time.time() - 7200
    os.utime(old_abandoned, (old_t, old_t))

    init_session.close()

    def run_worker_recovery(w_id: str):
        mgr = UploadManager(storage_root=storage_root, temp_root=temp_dir, worker_id=w_id, orphan_min_age_seconds=1800)
        with SessionMaker() as sess:
            return mgr.recover_staged_uploads(sess)

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        futs = [executor.submit(run_worker_recovery, f"worker-rec-{i}") for i in range(3)]
        results = [f.result() for f in concurrent.futures.as_completed(futs)]

    # Exactly 1 recovery finalized the committed upload
    total_finalized = sum(r["finalized"] for r in results)
    assert total_finalized == 1
    assert dest_comm.exists()

    # Expired staged upload was cleaned
    assert not temp_exp.exists()

    # Active staged upload remains intact
    assert temp_act.exists()

    # Recent untracked temp file remains intact!
    assert recent_untracked.exists()

    # Old abandoned temp file was cleaned!
    assert not old_abandoned.exists()

    engine.dispose()


def test_valid_sha256_verification(tmp_path):
    """TASK 2: Valid SHA-256 and matching size pass integrity verification."""
    f = tmp_path / "valid.bin"
    content = b"exact_content_for_hash_verification"
    f.write_bytes(content)
    real_sha = hashlib.sha256(content).hexdigest()

    assert _verify_file_integrity(f, real_sha, len(content)) is True
    # Case insensitivity
    assert _verify_file_integrity(f, real_sha.upper(), len(content)) is True


def test_invalid_checksum_rejected_no_hash123_bypass(tmp_path):
    """
    TASK 2: The 'hash123' bypass is removed. Any checksum mismatch is strictly rejected.
    """
    f = tmp_path / "reject.bin"
    content = b"some_file_content"
    f.write_bytes(content)

    # 1. 'hash123' must be REJECTED unless the content actually hashes to 'hash123'
    assert _verify_file_integrity(f, "hash123", len(content)) is False

    # 2. Wrong hex SHA-256 must be REJECTED
    wrong_sha = hashlib.sha256(b"completely_different_content").hexdigest()
    assert _verify_file_integrity(f, wrong_sha, len(content)) is False


def test_mismatched_file_size_rejected(tmp_path):
    """TASK 2: Matching checksum with mismatched file size is rejected."""
    f = tmp_path / "size_mismatch.bin"
    content = b"content_for_size_test"
    f.write_bytes(content)
    real_sha = hashlib.sha256(content).hexdigest()

    # Correct checksum, wrong size
    assert _verify_file_integrity(f, real_sha, len(content) + 5) is False
    assert _verify_file_integrity(f, real_sha, 0) is True  # 0 or None skips size check
    assert _verify_file_integrity(f, real_sha, None) is True


def test_negative_finalize_upload_corrupt_checksum_rejection(tmp_path):
    """
    TASK 2: Staged file with corrupt checksum raises ValueError, reverts entity reference,
    preserves old asset, and marks operation FAILED.
    """
    storage_root = tmp_path / "storage_corrupt"
    storage_root.mkdir()
    um = UploadManager(storage_root=storage_root, worker_id="worker-corrupt")

    db_file = tmp_path / "corrupt_chk.db"
    db_url = f"sqlite:///{db_file}"
    run_migrations(db_url)

    engine = create_engine(db_url)
    SessionMaker = sessionmaker(bind=engine)
    session = SessionMaker()

    proj = Project(id="p-corrupt", name="Corrupt Proj")
    char = Character(id="c-corrupt", project_id="p-corrupt", name="Hero")
    session.add_all([proj, char])
    session.commit()

    ref_dir = storage_root / "projects" / "p-corrupt" / "references"
    ref_dir.mkdir(parents=True, exist_ok=True)
    old_asset = ref_dir / "old_hero.png"
    old_asset.write_bytes(create_sample_image(b"original_good_data"))
    char.reference_image_path = "projects/p-corrupt/references/old_hero.png"
    session.commit()

    temp_f = storage_root / "corrupted_file.tmp"
    temp_f.write_bytes(create_sample_image(b"tampered_data"))
    dest_f = ref_dir / "new_hero.png"

    # Expected checksum is different from actual tampered_data
    expected_sha = hashlib.sha256(b"legitimate_untampered_content").hexdigest()

    op = um.start_staged_upload(
        db=session,
        project_id="p-corrupt",
        entity_type="character_reference",
        entity_id="c-corrupt",
        temp_path=temp_f,
        destination_path=dest_f,
        old_asset_path=old_asset,
        checksum=expected_sha,
        file_size_bytes=len(temp_f.read_bytes())
    )
    char.reference_image_path = "projects/p-corrupt/references/new_hero.png"
    um.mark_db_committed(session, op.id)
    session.commit()

    # Finalization must fail because staged file fails SHA-256 integrity
    with pytest.raises(ValueError) as exc:
        um.finalize_upload(session, op.id, worker_id="worker-corrupt", fencing_token=op.fencing_token)

    assert "integrity verification" in str(exc.value)

    # Entity reference reverted
    session.refresh(char)
    assert "old_hero.png" in char.reference_image_path

    # Old asset preserved
    assert old_asset.exists()

    # Operation marked FAILED
    session.refresh(op)
    assert op.status == "FAILED"

    session.close()

