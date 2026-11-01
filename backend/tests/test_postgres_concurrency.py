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
import uuid
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
