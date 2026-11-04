"""
Production PostgreSQL Integration & Concurrency Test Suite (TASK 5).

Verifies multi-session, parallel-transaction correctness under true concurrent PostgreSQL connections:
1. Cost ledger idempotency & speculative transaction rollback (no leftover budget increments).
2. Concurrent budget enforcement with database-enforced row locks / conditional updates.
3. Concurrent worker lease reclaim on expired jobs: exactly one winner, losers fenced off.
4. Concurrent upload operation claim on DB_COMMITTED operations: exactly one owner.
5. Flow generation attempt idempotency: parallel identical UUID attempts record credits exactly once.

Environment Configuration:
- TEST_POSTGRES_URL / POSTGRES_URL / DATABASE_URL.
- Default fallback to postgresql+psycopg://postgres:postgres@127.0.0.1:5432/aifilmstudio_test.
- When CI environment variable is detected, tests will fail explicitly rather than being silently skipped.
"""

import os
import time
import uuid
import hashlib
import concurrent.futures
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text, pool
from sqlalchemy.orm import sessionmaker
from fastapi import HTTPException

from backend.app.models.entities import (
    Base, Project, Scene, Shot, CostRecord, StudioJob, UploadOperation
)
from backend.app.services.cost_controller import CostController
from backend.app.services.job_manager import JobManager, JobOwnershipLostError
from backend.app.services.upload_manager import UploadManager, UploadOwnershipLostError
from backend.app.database import run_migrations


def get_postgres_engine():
    """Builds and verifies PostgreSQL engine. Fails explicitly in CI if unavailable."""
    pg_url = (
        os.environ.get("TEST_POSTGRES_URL")
        or os.environ.get("POSTGRES_URL")
        or os.environ.get("DATABASE_URL")
        or "postgresql+psycopg://postgres:postgres@127.0.0.1:5432/aifilmstudio_test"
    )

    is_ci = bool(os.environ.get("CI") or os.environ.get("GITHUB_ACTIONS"))

    try:
        engine = create_engine(pg_url, poolclass=pool.NullPool)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return engine
    except Exception as e:
        if is_ci:
            pytest.fail(f"PostgreSQL service is expected in CI but failed to connect ({pg_url}): {e}")
        else:
            pytest.skip(f"PostgreSQL is not available in local environment: {e}")


@pytest.fixture(scope="module")
def pg_engine():
    engine = get_postgres_engine()
    # Ensure migrations are applied to head
    from alembic.config import Config
    from alembic import command
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", engine.url.render_as_string(hide_password=False))
    command.upgrade(cfg, "head")
    yield engine
    engine.dispose()


@pytest.fixture
def pg_session_factory(pg_engine):
    return sessionmaker(bind=pg_engine)


def test_pg_cost_ledger_idempotency_parallel_transactions(pg_session_factory):
    """
    TASK 5: Cost ledger idempotency under parallel PostgreSQL transactions.
    - 10 threads concurrently charge identical operation.
    - Exactly one winner commits initial charge.
    - 9 concurrent threads encounter IntegrityError and roll back speculative budget increment.
    - Budget spent increments by cost EXACTLY ONCE.
    - Zero phantom budget increments remain.
    """
    project_id = f"pg-proj-ledger-{uuid.uuid4().hex[:8]}"
    initial_budget = 500.0
    cost_per_op = 10.0

    with pg_session_factory() as session:
        proj = Project(id=project_id, name="PG Ledger Proj", budget_total=initial_budget, budget_spent=0.0)
        session.add(proj)
        session.commit()

    controller = CostController()
    payload = {"scene_num": 1, "prompt": "cyberpunk skyline", "tokens": 1500}

    def charge_attempt(worker_idx: int):
        with pg_session_factory() as s:
            rec = controller.authorize_and_charge(
                db=s,
                project_id=project_id,
                action_type="screenplay_generation",
                payload=payload,
                details=f"Concurrent charge attempt from worker {worker_idx}",
                estimated_cost=cost_per_op,
                allow_duplicate=False
            )
            return worker_idx, rec.id

    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(charge_attempt, i) for i in range(10)]
        for f in concurrent.futures.as_completed(futures):
            results.append(f.result())

    # All 10 got the exact same CostRecord ID
    record_ids = {rec_id for _, rec_id in results}
    assert len(record_ids) == 1

    # Verify database state in PostgreSQL
    with pg_session_factory() as session:
        proj = session.query(Project).filter(Project.id == project_id).first()
        assert proj.budget_spent == cost_per_op, (
            f"Expected budget_spent to be {cost_per_op}, but found {proj.budget_spent} (speculative leak!)"
        )
        ledger_count = session.query(CostRecord).filter(CostRecord.project_id == project_id).count()
        assert ledger_count == 1, f"Expected exactly 1 cost record in ledger, found {ledger_count}"


def test_pg_concurrent_budget_limit_enforcement(pg_session_factory):
    """
    TASK 5: Concurrent budget limit enforcement under parallel transactions.
    - Project budget is $10.00.
    - 10 threads attempt to spend $2.00 each concurrently (distinct operations).
    - Exactly 5 can succeed ($10.00 / $2.00 = 5).
    - Remaining 5 fail with 400 'budget exceeded'.
    - Final budget_spent in PostgreSQL is exactly $10.00, never exceeding limit.
    """
    project_id = f"pg-proj-budget-{uuid.uuid4().hex[:8]}"
    total_budget = 10.0
    cost_per_charge = 2.0

    with pg_session_factory() as session:
        proj = Project(id=project_id, name="PG Budget Proj", budget_total=total_budget, budget_spent=0.0)
        session.add(proj)
        session.commit()

    controller = CostController()

    def budget_spend_attempt(idx: int):
        with pg_session_factory() as s:
            try:
                rec = controller.authorize_and_charge(
                    db=s,
                    project_id=project_id,
                    action_type="unique_vfx_task",
                    payload={"unique_task_id": f"task-{idx}-{uuid.uuid4().hex[:6]}"},
                    details=f"Parallel charge {idx}",
                    estimated_cost=cost_per_charge,
                    allow_duplicate=True
                )
                return idx, True, None
            except HTTPException as e:
                return idx, False, e.detail

    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(budget_spend_attempt, i) for i in range(10)]
        for f in concurrent.futures.as_completed(futures):
            results.append(f.result())

    successes = [idx for idx, ok, _ in results if ok]
    failures = [idx for idx, ok, err in results if not ok]

    assert len(successes) == 5, f"Expected exactly 5 successful charges, got {len(successes)}"
    assert len(failures) == 5, f"Expected exactly 5 budget rejections, got {len(failures)}"

    with pg_session_factory() as session:
        proj = session.query(Project).filter(Project.id == project_id).first()
        assert proj.budget_spent == total_budget
        assert session.query(CostRecord).filter(CostRecord.project_id == project_id).count() == 5


def test_pg_worker_lease_reclaim_concurrency(pg_session_factory):
    """
    TASK 5: Multi-session worker lease reclaim concurrency in PostgreSQL.
    - Expired job is targeted by 10 concurrent worker sessions.
    - Exactly one worker claims the expired job (EXECUTE).
    - Other 9 get IN_PROGRESS.
    - Winning worker completes successfully; losing worker rejected.
    """
    project_id = f"pg-proj-reclaim-{uuid.uuid4().hex[:8]}"

    with pg_session_factory() as session:
        proj = Project(id=project_id, name="PG Reclaim Proj", budget_total=100.0, budget_spent=0.0)
        session.add(proj)
        session.commit()

        # Initial dead worker claims job
        dead_worker = JobManager(worker_id="dead-pg-worker")
        payload = {"render_task": "batch_42"}
        job, action = dead_worker.prepare_or_claim_job(session, project_id, "pg_render", payload, lease_seconds=1)
        assert action == "EXECUTE"
        assert job.fencing_token == 1

        # Expire lease
        job.lease_expires_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=60)
        session.commit()
        job_id = job.id

    def worker_reclaim(wid: int):
        with pg_session_factory() as s:
            mgr = JobManager(worker_id=f"pg-worker-{wid}")
            claimed_job, act = mgr.prepare_or_claim_job(s, project_id, "pg_render", payload, lease_seconds=300)
            token = claimed_job.fencing_token
            return wid, act, token

    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(worker_reclaim, i) for i in range(10)]
        for f in concurrent.futures.as_completed(futures):
            results.append(f.result())

    winners = [(wid, act, tok) for wid, act, tok in results if act == "EXECUTE"]
    in_progress = [(wid, act, tok) for wid, act, tok in results if act == "IN_PROGRESS"]

    assert len(winners) == 1, f"Expected exactly 1 reclaim winner, got {len(winners)}: {winners}"
    assert len(in_progress) == 9

    winning_wid, _, winning_token = winners[0]
    assert winning_token == 2

    # Winning worker completes
    with pg_session_factory() as session:
        winner_mgr = JobManager(worker_id=f"pg-worker-{winning_wid}")
        completed = winner_mgr.complete_job(
            session, job_id, {"result": "pg_success"},
            worker_id=f"pg-worker-{winning_wid}", fencing_token=winning_token
        )
        assert completed.status == "COMPLETED"

        # Stale dead worker or loser trying to complete with token 1 is rejected
        dead_mgr = JobManager(worker_id="dead-pg-worker")
        with pytest.raises(JobOwnershipLostError):
            dead_mgr.complete_job(
                session, job_id, {"result": "stale"},
                worker_id="dead-pg-worker", fencing_token=1
            )


def test_pg_upload_operation_claim_concurrency(pg_session_factory, tmp_path):
    """
    TASK 5: Multi-session upload operation claim concurrency in PostgreSQL.
    - 10 parallel worker sessions attempt to claim an eligible DB_COMMITTED upload.
    - Exactly ONE worker acquires ownership.
    - Remaining 9 workers are skipped.
    """
    storage_root = tmp_path / "pg_storage"
    storage_root.mkdir()
    project_id = f"pg-proj-up-{uuid.uuid4().hex[:8]}"

    with pg_session_factory() as session:
        proj = Project(id=project_id, name="PG Upload Proj")
        session.add(proj)
        session.commit()

        um_init = UploadManager(storage_root=storage_root, worker_id="init-pg-worker")
        temp_f = storage_root / "test_pg.tmp"
        temp_f.write_bytes(b"content")
        dest_f = storage_root / "final_pg.jpg"

        op = um_init.start_staged_upload(
            db=session,
            project_id=project_id,
            entity_type="character_reference",
            entity_id="c-pg-1",
            temp_path=temp_f,
            destination_path=dest_f
        )
        um_init.mark_db_committed(session, op.id)
        # Clear claim to make it eligible
        op.worker_id = None
        op.lease_expires_at = None
        session.commit()
        op_id = op.id

    def worker_claim(wid: int):
        with pg_session_factory() as s:
            mgr = UploadManager(storage_root=storage_root, worker_id=f"pg-uploader-{wid}")
            claimed = mgr.claim_upload_operation(s, op_id, worker_id=f"pg-uploader-{wid}")
            return wid, (claimed is not None)

    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(worker_claim, i) for i in range(10)]
        for f in concurrent.futures.as_completed(futures):
            results.append(f.result())

    winners = [wid for wid, won in results if won]
    assert len(winners) == 1, f"Expected exactly 1 winner, got: {winners}"


def test_pg_flow_generation_attempt_idempotency_concurrency(pg_session_factory):
    """
    TASK 5: Flow generation attempt idempotency in PostgreSQL.
    - 10 parallel threads attempt to record the SAME generation_attempt_id.
    - Exactly 1 record created in cost_records.
    - Shot flow_credits_spent increments by exactly 2.5 (never 25.0).
    - Shot generation_attempts increments by exactly 1 (never 10).
    - Conflicting payload with same attempt_id raises 409 Conflict.
    """
    project_id = f"pg-proj-flow-{uuid.uuid4().hex[:8]}"
    attempt_id = str(uuid.uuid4())
    credits = 2.5

    with pg_session_factory() as session:
        proj = Project(id=project_id, name="PG Flow Proj")
        scene = Scene(id=f"pg-scene-{uuid.uuid4().hex[:8]}", project_id=project_id, scene_number=1, slugline="INT. LAB")
        shot = Shot(
            id=f"pg-shot-{uuid.uuid4().hex[:8]}",
            scene_id=scene.id,
            project_id=project_id,
            shot_number=1,
            visual_description="Close up of device",
            flow_credits_spent=0.0,
            generation_attempts=0
        )
        session.add_all([proj, scene, shot])
        session.commit()
        shot_id = shot.id

    controller = CostController()

    def record_attempt(idx: int):
        with pg_session_factory() as s:
            rec, s_rec, is_new = controller.record_flow_credit_usage(
                db=s,
                project_id=project_id,
                shot_id=shot_id,
                flow_credits=credits,
                generation_attempt_id=attempt_id,
                notes=f"Concurrent attempt from worker {idx}"
            )
            return idx, rec.id, is_new

    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(record_attempt, i) for i in range(10)]
        for f in concurrent.futures.as_completed(futures):
            results.append(f.result())

    # All 10 returned the exact same cost record id
    record_ids = {rec_id for _, rec_id, _ in results}
    assert len(record_ids) == 1

    # Exactly one was is_new=True, 9 were is_new=False
    new_records = [idx for idx, _, is_new in results if is_new]
    assert len(new_records) == 1, f"Expected 1 new record, found: {len(new_records)}"

    with pg_session_factory() as session:
        shot = session.query(Shot).filter(Shot.id == shot_id).first()
        assert shot.flow_credits_spent == credits, f"Expected {credits}, found {shot.flow_credits_spent}"
        assert shot.generation_attempts == 1, f"Expected 1 attempt, found {shot.generation_attempts}"
        records = session.query(CostRecord).filter(CostRecord.generation_attempt_id == attempt_id).all()
        assert len(records) == 1

        # Conflicting payload with same attempt ID raises 409 Conflict
        with pytest.raises(HTTPException) as exc:
            controller.record_flow_credit_usage(
                db=session,
                project_id=project_id,
                shot_id=shot_id,
                flow_credits=5.0,  # conflicting credits! (different from 2.5)
                generation_attempt_id=attempt_id
            )
        assert exc.value.status_code == 409


def test_pg_staged_upload_cleanup_concurrency(pg_session_factory, tmp_path):
    """
    TASK 4 on PostgreSQL: Concurrent cleanup of expired staged upload.
    - 5 parallel PostgreSQL worker sessions attempt to claim and clean the same expired STAGED upload.
    - Exactly ONE worker succeeds in claiming and unlinking.
    - 4 remaining workers return False without errors.
    """
    storage_root = tmp_path / "pg_cleanup_storage"
    storage_root.mkdir()
    project_id = f"pg-proj-clean-{uuid.uuid4().hex[:8]}"

    with pg_session_factory() as session:
        proj = Project(id=project_id, name="PG Cleanup Proj")
        session.add(proj)
        session.commit()

        um_init = UploadManager(storage_root=storage_root, worker_id="init-clean-worker")
        temp_f = storage_root / "pg_expired.tmp"
        temp_f.write_bytes(b"temp_cleanup_data")
        dest_f = storage_root / "pg_expired.png"

        op = um_init.start_staged_upload(
            db=session,
            project_id=project_id,
            entity_type="character_reference",
            entity_id="c-pg-clean",
            temp_path=temp_f,
            destination_path=dest_f
        )
        # Expire lease
        past = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=15)
        session.execute(
            text("UPDATE upload_operations SET lease_expires_at = :past WHERE id = :id"),
            {"past": past, "id": op.id}
        )
        session.commit()
        op_id = op.id

    def worker_cleanup(wid: int):
        with pg_session_factory() as s:
            mgr = UploadManager(storage_root=storage_root, worker_id=f"pg-cleaner-{wid}")
            return wid, mgr.claim_and_clean_staged_upload(s, op_id, worker_id=f"pg-cleaner-{wid}")

    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(worker_cleanup, i) for i in range(5)]
        for f in concurrent.futures.as_completed(futures):
            results.append(f.result())

    successes = [wid for wid, res in results if res is True]
    assert len(successes) == 1, f"Expected exactly 1 cleaner to succeed, got: {successes}"
    assert not temp_f.exists()

    with pg_session_factory() as session:
        op_check = session.query(UploadOperation).filter(UploadOperation.id == op_id).first()
        assert op_check.status == "FAILED"


def test_pg_stale_upload_worker_fenced_off(pg_session_factory, tmp_path):
    """
    TASK 1 & 2 on PostgreSQL: Stale upload worker is strictly fenced off.
    - Worker A creates upload and DB commits.
    - Worker A lease expires.
    - Worker B reclaims upload (fencing token incremented).
    - Stale Worker A attempts to finalize: strictly rejected with UploadOwnershipLostError (HTTP 409).
    - Old asset is preserved until Worker B finalizes.
    """
    storage_root = tmp_path / "pg_fence_storage"
    storage_root.mkdir()
    project_id = f"pg-proj-fence-{uuid.uuid4().hex[:8]}"

    old_asset = storage_root / "pg_old_asset.jpg"
    old_asset.write_bytes(b"pg_old_asset_bytes")

    temp_f = storage_root / "pg_new_asset.tmp"
    temp_f.write_bytes(b"pg_new_asset_bytes")
    sha = hashlib.sha256(b"pg_new_asset_bytes").hexdigest()
    dest_f = storage_root / "pg_new_asset.jpg"

    mgr_a = UploadManager(storage_root=storage_root, worker_id="pg-worker-A")
    mgr_b = UploadManager(storage_root=storage_root, worker_id="pg-worker-B")

    with pg_session_factory() as session:
        proj = Project(id=project_id, name="PG Fence Proj")
        session.add(proj)
        session.commit()

        op = mgr_a.start_staged_upload(
            db=session,
            project_id=project_id,
            entity_type="character_reference",
            entity_id="c-pg-fence",
            temp_path=temp_f,
            destination_path=dest_f,
            old_asset_path=old_asset,
            checksum=sha,
            file_size_bytes=len(b"pg_new_asset_bytes")
        )
        mgr_a.mark_db_committed(session, op.id)
        session.commit()

        op_id = op.id
        token_a = op.fencing_token

        # Expire Worker A lease
        past = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=5)
        session.execute(
            text("UPDATE upload_operations SET lease_expires_at = :past WHERE id = :id"),
            {"past": past, "id": op_id}
        )
        session.commit()

    # Worker B reclaims
    with pg_session_factory() as session:
        claimed_b = mgr_b.claim_upload_operation(session, op_id, worker_id="pg-worker-B")
        assert claimed_b is not None
        assert claimed_b.fencing_token == token_a + 1
        token_b = claimed_b.fencing_token

    # Stale Worker A attempts to finalize
    with pg_session_factory() as session:
        with pytest.raises(UploadOwnershipLostError):
            mgr_a.finalize_upload(session, op_id, worker_id="pg-worker-A", fencing_token=token_a)

    # Old asset MUST still exist!
    assert old_asset.exists()

    # Winning Worker B finalizes
    with pg_session_factory() as session:
        final_p = mgr_b.finalize_upload(session, op_id, worker_id="pg-worker-B", fencing_token=token_b)
        assert final_p == dest_f
        assert dest_f.exists()

    # Now old asset is cleanly removed
    assert not old_asset.exists()


def test_pg_concurrent_recover_staged_uploads_multi_instance(pg_session_factory, tmp_path):
    """
    TASK 1 & 3 on PostgreSQL: Multiple concurrent worker instances running recover_staged_uploads().
    - 1 DB_COMMITTED upload (must be claimed and finalized by exactly 1 worker).
    - 1 expired STAGED upload (must be cleaned by exactly 1 worker).
    - 1 active STAGED upload (must remain active and preserved).
    - 1 recent untracked temp file (must remain intact, protected by safe age threshold).
    - 1 abandoned untracked temp file (older than threshold, must be cleaned).
    """
    storage_root = tmp_path / f"pg_multi_rec_{uuid.uuid4().hex[:8]}"
    storage_root.mkdir()
    temp_dir = storage_root / "temp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    dest_dir = storage_root / "dest"
    dest_dir.mkdir(parents=True, exist_ok=True)
    project_id = f"pg-proj-mrec-{uuid.uuid4().hex[:8]}"

    with pg_session_factory() as session:
        proj = Project(id=project_id, name="PG Multi Rec Proj")
        session.add(proj)
        session.commit()

        # 1. DB_COMMITTED upload
        content_comm = b"pg_comm_file_bytes"
        sha_comm = hashlib.sha256(content_comm).hexdigest()
        temp_comm = temp_dir / "pg_comm.tmp"
        temp_comm.write_bytes(content_comm)
        dest_comm = dest_dir / "pg_comm.png"

        init_mgr = UploadManager(storage_root=storage_root, temp_root=temp_dir, worker_id="pg-init")
        op_comm = init_mgr.start_staged_upload(
            db=session,
            project_id=project_id,
            entity_type="character_reference",
            entity_id="c-pg-comm",
            temp_path=temp_comm,
            destination_path=dest_comm,
            checksum=sha_comm,
            file_size_bytes=len(content_comm)
        )
        init_mgr.mark_db_committed(session, op_comm.id)
        # Clear claim to make it eligible
        op_comm.worker_id = None
        op_comm.lease_expires_at = None
        session.commit()

        # 2. Expired STAGED upload
        temp_exp = temp_dir / "pg_exp.tmp"
        temp_exp.write_bytes(b"pg_exp_bytes")
        op_exp = init_mgr.start_staged_upload(
            db=session,
            project_id=project_id,
            entity_type="character_reference",
            entity_id="c-pg-exp",
            temp_path=temp_exp,
            destination_path=dest_dir / "pg_exp.png"
        )
        past = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=20)
        session.execute(
            text("UPDATE upload_operations SET lease_expires_at = :past WHERE id = :id"),
            {"past": past, "id": op_exp.id}
        )
        session.commit()

        # 3. Active STAGED upload
        temp_act = temp_dir / "pg_act.tmp"
        temp_act.write_bytes(b"pg_act_bytes")
        init_mgr.start_staged_upload(
            db=session,
            project_id=project_id,
            entity_type="character_reference",
            entity_id="c-pg-act",
            temp_path=temp_act,
            destination_path=dest_dir / "pg_act.png"
        )

        # 4. Recent untracked temp file (0 seconds old)
        recent_untracked = temp_dir / "pg_recent_untracked.tmp"
        recent_untracked.write_bytes(b"pg_recent_untracked_bytes")

        # 5. Abandoned untracked temp file (2 hours old)
        abandoned_untracked = temp_dir / "pg_abandoned_untracked.tmp"
        abandoned_untracked.write_bytes(b"pg_abandoned_untracked_bytes")
        old_ts = time.time() - 7200
        os.utime(abandoned_untracked, (old_ts, old_ts))
        session.commit()


    def pg_worker_recovery(w_idx: int):
        with pg_session_factory() as s:
            mgr = UploadManager(
                storage_root=storage_root,
                temp_root=temp_dir,
                worker_id=f"pg-worker-rec-{w_idx}",
                orphan_min_age_seconds=1800
            )
            return mgr.recover_staged_uploads(s)

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        futs = [executor.submit(pg_worker_recovery, i) for i in range(3)]
        results = [f.result() for f in concurrent.futures.as_completed(futs)]

    # Exactly 1 worker finalized the committed upload
    assert sum(r["finalized"] for r in results) == 1
    assert dest_comm.exists()

    # Expired staged upload temp was cleaned
    assert not temp_exp.exists()

    # Active staged temp remains
    assert temp_act.exists()

    # Recent untracked temp file MUST remain intact
    assert recent_untracked.exists()

    # Abandoned untracked temp file MUST be cleaned
    assert not abandoned_untracked.exists()


def test_pg_concurrent_story_approval_with_barrier(pg_session_factory):
    """
    Requirement 6A: PostgreSQL concurrent Story Approval.
    - Two independent sessions attempt conflicting decisions (APPROVED vs REJECTED).
    - Synchronized via threading.Barrier to hit row lock simultaneously.
    - Exactly one decision succeeds (200), loser receives 409 Conflict.
    - Exactly one decision audit row exists; no state or audit overwritten.
    """
    import threading
    from backend.app.models.entities import PipelineStageState, PipelineCheckpoint, PipelineCheckpointAudit
    from backend.app.services.pipeline_orchestrator import PipelineOrchestrator
    from backend.app.schemas.pipeline import CheckpointDecisionRequest

    project_id = f"pg-appr-{uuid.uuid4().hex[:8]}"

    # Setup project with screenplay and submit for review
    with pg_session_factory() as session:
        proj = Project(id=project_id, name="PG Concurrency Film")
        session.add(proj)
        session.commit()
        PipelineOrchestrator.initialize_project_pipeline(project_id, db=session)

        sc = Scene(
            id=str(uuid.uuid4()),
            project_id=project_id,
            scene_number=1,
            slugline="INT. LAB - NIGHT",
            script_content="Blue chemicals glow in the dark.",
            order_index=1
        )
        session.add(sc)
        session.commit()

        sub_res = PipelineOrchestrator.submit_story_for_review(session, project_id)
        assert sub_res["status"] == "WAITING_FOR_APPROVAL"
        h1 = sub_res["artifact_content_hash"]
        v1 = sub_res["artifact_version"]

    barrier = threading.Barrier(2)
    results = {}

    def reviewer_worker(reviewer_id: str, decision: str):
        with pg_session_factory() as sess:
            barrier.wait(timeout=5.0)
            req = CheckpointDecisionRequest(
                decision=decision,
                expected_artifact_version=v1,
                expected_artifact_hash=h1,
                reviewer_name=f"Reviewer-{reviewer_id}"
            )
            try:
                dec_res = PipelineOrchestrator.record_story_decision(sess, project_id, req)
                results[reviewer_id] = {"status": 200, "decision": dec_res["decision"]}
            except HTTPException as e:
                results[reviewer_id] = {"status": e.status_code, "error": str(e.detail)}
            except Exception as e:
                results[reviewer_id] = {"status": 500, "error": str(e)}

    t1 = threading.Thread(target=reviewer_worker, args=("A", "APPROVED"))
    t2 = threading.Thread(target=reviewer_worker, args=("B", "REJECTED"))

    t1.start()
    t2.start()
    t1.join(timeout=10.0)
    t2.join(timeout=10.0)

    # Exactly one winner (200) and one loser (409)
    statuses = [r["status"] for r in results.values()]
    assert 200 in statuses
    assert 409 in statuses
    assert len(statuses) == 2

    # Verify database state consistency
    with pg_session_factory() as session:
        cp = session.query(PipelineCheckpoint).filter(
            PipelineCheckpoint.project_id == project_id,
            PipelineCheckpoint.checkpoint_type == "story_approval"
        ).first()
        winner_decision = [r["decision"] for r in results.values() if r["status"] == 200][0]
        expected_status = "APPROVED" if winner_decision == "APPROVED" else "REJECTED"
        assert cp.status == expected_status

        # Audits must have exactly 1 SUBMITTED_FOR_REVIEW and 1 decision audit
        audits = session.query(PipelineCheckpointAudit).filter(
            PipelineCheckpointAudit.project_id == project_id
        ).order_by(PipelineCheckpointAudit.created_at.asc()).all()

        assert len(audits) == 2
        assert audits[0].decision == "SUBMITTED_FOR_REVIEW"
        assert audits[1].decision == winner_decision


def wait_for_row_lock_waiter(engine, waiter_pid: int, timeout: float = 5.0) -> bool:
    """
    Deterministically verifies that the PostgreSQL backend session (waiter_pid)
    is genuinely blocked waiting for a lock (granted = false in pg_locks).
    Eliminates race conditions and avoids arbitrary sleep-based waits.
    """
    start = time.time()
    while time.time() - start < timeout:
        with engine.connect() as conn:
            blocked = conn.execute(
                text("SELECT 1 FROM pg_locks WHERE pid = :pid AND granted = false"),
                {"pid": waiter_pid}
            ).scalar()
            if blocked:
                return True
        time.sleep(0.01)
    raise TimeoutError(f"PID {waiter_pid} was not observed waiting for row lock within {timeout}s")


def test_pg_concurrency_case_a_worker_wins(pg_engine, pg_session_factory):
    """
    Requirement 5: Case A — Worker wins lock:
    - Worker acquires project lock first.
    - Mutation starts and must wait on project row lock.
    - Worker completes stage on H1 successfully and commits.
    - Mutation unblocks, mutates screenplay to H2, and invalidates stage.
    - Final state: stage is STALE.
    - Worker job maintains COMPLETED status of valid completed worker.
    - Audit and hashes remain consistent.
    """
    import threading
    from backend.app.models.entities import PipelineStageState, PipelineCheckpoint, PipelineCheckpointAudit
    from backend.app.services.pipeline_orchestrator import PipelineOrchestrator, acquire_project_write_lock
    from backend.app.services.job_manager import JobManager
    from backend.app.schemas.pipeline import CheckpointDecisionRequest

    project_id = f"pg-case-a-{uuid.uuid4().hex[:8]}"

    with pg_session_factory() as session:
        proj = Project(id=project_id, name="PG Case A Worker Wins")
        session.add(proj)
        session.commit()
        PipelineOrchestrator.initialize_project_pipeline(project_id, db=session)

        sc = Scene(
            id=str(uuid.uuid4()),
            project_id=project_id,
            scene_number=1,
            slugline="INT. LAB - DAY",
            script_content="H1 initial screenplay content.",
            order_index=1
        )
        session.add(sc)
        session.commit()

        sub = PipelineOrchestrator.submit_story_for_review(session, project_id)
        h1 = sub["artifact_content_hash"]
        v1 = sub["artifact_version"]

        PipelineOrchestrator.record_story_decision(
            session, project_id,
            CheckpointDecisionRequest(
                decision="APPROVED",
                expected_artifact_version=v1,
                expected_artifact_hash=h1,
                reviewer_name="Director"
            )
        )

        worker_id = "pg-worker-case-a"
        jm = JobManager(worker_id=worker_id)
        job, action = jm.prepare_or_claim_job(
            db=session,
            project_id=project_id,
            job_type="character_generation",
            payload={"screenplay_hash": h1}
        )
        fencing_token = job.fencing_token

        stage = session.query(PipelineStageState).filter(
            PipelineStageState.project_id == project_id,
            PipelineStageState.stage_name == "character_design"
        ).first()
        stage.status = "IN_PROGRESS"
        stage.active_job_id = job.id
        stage.consumed_input_hash = h1
        session.commit()
        job_id = job.id

    worker_locked = threading.Event()
    mutation_attempting_lock = threading.Event()
    worker_committed = threading.Event()
    mutation_pid_holder = {}
    thread_exceptions = []
    worker_result = {}
    mutation_result = {}

    def worker_thread():
        try:
            with pg_session_factory() as sess_w:
                # 1. Acquire project write lock first (SELECT ... FOR UPDATE)
                acquire_project_write_lock(sess_w, project_id)
                worker_locked.set()

                # Wait until mutation thread obtains PID and attempts to acquire the same lock
                assert mutation_attempting_lock.wait(timeout=5.0)
                mutation_pid = mutation_pid_holder["pid"]
                wait_for_row_lock_waiter(pg_engine, mutation_pid)

                # Complete stage on H1
                PipelineOrchestrator.complete_pipeline_stage(
                    db=sess_w,
                    project_id=project_id,
                    stage_name="character_design",
                    active_job_id=job_id,
                    worker_id=worker_id,
                    fencing_token=fencing_token,
                    result_hash="res-char-case-a",
                    consumed_input_hash=h1
                )
                worker_committed.set()
                worker_result["success"] = True
        except Exception as exc:
            thread_exceptions.append(exc)
            raise

    def mutation_thread():
        try:
            assert worker_locked.wait(timeout=5.0)
            with pg_session_factory() as sess_m:
                pid = sess_m.execute(text("SELECT pg_backend_pid()")).scalar()
                mutation_pid_holder["pid"] = pid
                mutation_attempting_lock.set()
                # 2. This will BLOCK until worker_thread commits sess_w!
                acquire_project_write_lock(sess_m, project_id)
                assert worker_committed.wait(timeout=2.0), "Mutation unblocked before worker committed!"

                # Mutate screenplay to H2 and invalidate downstream
                sc_to_edit = sess_m.query(Scene).filter(Scene.project_id == project_id).first()
                sc_to_edit.script_content = "H2 mutated screenplay content."
                PipelineOrchestrator.invalidate_downstream_stages(
                    db=sess_m,
                    project_id=project_id,
                    changed_stage="screenplay",
                    commit=False
                )
                sess_m.commit()
                mutation_result["success"] = True
        except Exception as exc:
            thread_exceptions.append(exc)
            raise

    t_w = threading.Thread(target=worker_thread)
    t_m = threading.Thread(target=mutation_thread)
    t_w.start()
    t_m.start()
    t_w.join(timeout=10.0)
    t_m.join(timeout=10.0)

    if thread_exceptions:
        raise thread_exceptions[0]

    assert worker_result.get("success") is True
    assert mutation_result.get("success") is True

    with pg_session_factory() as session:
        st = session.query(PipelineStageState).filter(
            PipelineStageState.project_id == project_id,
            PipelineStageState.stage_name == "character_design"
        ).first()
        # Stage is STALE because mutation invalidated it
        assert st.status == "STALE"
        assert st.active_job_id is None

        # StudioJob preserves COMPLETED status from the valid worker
        jb = session.query(StudioJob).filter(StudioJob.id == job_id).first()
        assert jb.status == "COMPLETED"

        # Checkpoint is STALE
        cp = session.query(PipelineCheckpoint).filter(
            PipelineCheckpoint.project_id == project_id,
            PipelineCheckpoint.checkpoint_type == "story_approval"
        ).first()
        assert cp.status == "STALE"

        # Invalidation audit recorded with H1 and V1
        inv_audit = session.query(PipelineCheckpointAudit).filter(
            PipelineCheckpointAudit.project_id == project_id,
            PipelineCheckpointAudit.decision == "INVALIDATED"
        ).first()
        assert inv_audit is not None
        assert inv_audit.artifact_content_hash == h1
        assert inv_audit.artifact_version == v1


def test_pg_concurrency_case_b_mutation_wins(pg_engine, pg_session_factory):
    """
    Requirement 5: Case B — Mutation wins lock:
    - Mutation acquires project lock first.
    - Worker starts and must wait on project row lock.
    - Mutation commits H2 and invalidates stage.
    - Worker unblocks, attempts to complete on obsolete H1, and is rejected.
    - Final state: stage is STALE.
    - Old job is NOT COMPLETED.
    """
    import threading
    from backend.app.models.entities import PipelineStageState, PipelineCheckpoint, PipelineCheckpointAudit
    from backend.app.services.pipeline_orchestrator import PipelineOrchestrator, acquire_project_write_lock
    from backend.app.services.job_manager import JobManager, JobOwnershipLostError
    from backend.app.schemas.pipeline import CheckpointDecisionRequest

    project_id = f"pg-case-b-{uuid.uuid4().hex[:8]}"

    with pg_session_factory() as session:
        proj = Project(id=project_id, name="PG Case B Mutation Wins")
        session.add(proj)
        session.commit()
        PipelineOrchestrator.initialize_project_pipeline(project_id, db=session)

        sc = Scene(
            id=str(uuid.uuid4()),
            project_id=project_id,
            scene_number=1,
            slugline="INT. BUNKER - NIGHT",
            script_content="H1 initial screenplay content.",
            order_index=1
        )
        session.add(sc)
        session.commit()

        sub = PipelineOrchestrator.submit_story_for_review(session, project_id)
        h1 = sub["artifact_content_hash"]
        v1 = sub["artifact_version"]

        PipelineOrchestrator.record_story_decision(
            session, project_id,
            CheckpointDecisionRequest(
                decision="APPROVED",
                expected_artifact_version=v1,
                expected_artifact_hash=h1,
                reviewer_name="Director"
            )
        )

        worker_id = "pg-worker-case-b"
        jm = JobManager(worker_id=worker_id)
        job, action = jm.prepare_or_claim_job(
            db=session,
            project_id=project_id,
            job_type="character_generation",
            payload={"screenplay_hash": h1}
        )
        fencing_token = job.fencing_token

        stage = session.query(PipelineStageState).filter(
            PipelineStageState.project_id == project_id,
            PipelineStageState.stage_name == "character_design"
        ).first()
        stage.status = "IN_PROGRESS"
        stage.active_job_id = job.id
        stage.consumed_input_hash = h1
        session.commit()
        job_id = job.id

    mutation_locked = threading.Event()
    worker_attempting_lock = threading.Event()
    mutation_committed = threading.Event()
    worker_pid_holder = {}
    thread_exceptions = []
    worker_result = {}
    mutation_result = {}

    def mutation_thread():
        try:
            with pg_session_factory() as sess_m:
                # 1. Acquire project write lock first (SELECT ... FOR UPDATE)
                acquire_project_write_lock(sess_m, project_id)
                mutation_locked.set()

                # Wait until worker begins attempting to complete stage
                assert worker_attempting_lock.wait(timeout=5.0)
                worker_pid = worker_pid_holder["pid"]
                wait_for_row_lock_waiter(pg_engine, worker_pid)

                # Mutate screenplay to H2 and invalidate downstream stages
                sc_to_edit = sess_m.query(Scene).filter(Scene.project_id == project_id).first()
                sc_to_edit.script_content = "H2 radically modified screenplay."
                PipelineOrchestrator.invalidate_downstream_stages(
                    db=sess_m,
                    project_id=project_id,
                    changed_stage="screenplay",
                    commit=False
                )
                sess_m.commit()
                mutation_committed.set()
                mutation_result["success"] = True
        except Exception as exc:
            thread_exceptions.append(exc)
            raise

    def worker_thread():
        try:
            assert mutation_locked.wait(timeout=5.0)
            with pg_session_factory() as sess_w:
                pid = sess_w.execute(text("SELECT pg_backend_pid()")).scalar()
                worker_pid_holder["pid"] = pid
                worker_attempting_lock.set()
                try:
                    # 2. This will BLOCK until mutation_thread commits sess_m!
                    PipelineOrchestrator.complete_pipeline_stage(
                        db=sess_w,
                        project_id=project_id,
                        stage_name="character_design",
                        active_job_id=job_id,
                        worker_id=worker_id,
                        fencing_token=fencing_token,
                        result_hash="res-char-case-b",
                        consumed_input_hash=h1
                    )
                    worker_result["completed"] = True
                except JobOwnershipLostError as e:
                    worker_result["completed"] = False
                    worker_result["error"] = str(e.detail)
                assert mutation_committed.is_set(), "Worker unblocked before mutation committed!"
        except Exception as exc:
            thread_exceptions.append(exc)
            raise

    t_m = threading.Thread(target=mutation_thread)
    t_w = threading.Thread(target=worker_thread)
    t_m.start()
    t_w.start()
    t_m.join(timeout=10.0)
    t_w.join(timeout=10.0)

    if thread_exceptions:
        raise thread_exceptions[0]

    assert mutation_result.get("success") is True
    assert worker_result.get("completed") is False
    assert "stale" in worker_result.get("error", "").lower() or "obsolete" in worker_result.get("error", "").lower()

    with pg_session_factory() as session:
        st = session.query(PipelineStageState).filter(
            PipelineStageState.project_id == project_id,
            PipelineStageState.stage_name == "character_design"
        ).first()
        assert st.status == "STALE"
        assert st.active_job_id is None

        # StudioJob was NOT completed
        jb = session.query(StudioJob).filter(StudioJob.id == job_id).first()
        assert jb.status != "COMPLETED"

        # Checkpoint is STALE
        cp = session.query(PipelineCheckpoint).filter(
            PipelineCheckpoint.project_id == project_id,
            PipelineCheckpoint.checkpoint_type == "story_approval"
        ).first()
        assert cp.status == "STALE"

        # Invalidation audit recorded with H1 and V1
        inv_audit = session.query(PipelineCheckpointAudit).filter(
            PipelineCheckpointAudit.project_id == project_id,
            PipelineCheckpointAudit.decision == "INVALIDATED"
        ).first()
        assert inv_audit is not None
        assert inv_audit.artifact_content_hash == h1
        assert inv_audit.artifact_version == v1



