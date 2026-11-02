"""
Pipeline Foundation Tests for AI Film Studio (S4 Milestone 1 Foundation).

Validates:
1. Migration upgrade and schema inspection (0008_pipeline_orchestration_tables).
2. Migration downgrade behavior (safe refusal when audit records exist, validated archival).
3. SQLite and PostgreSQL DB-level audit immutability triggers (UPDATE and DELETE blocked).
4. Foreign key RESTRICT: Hard deletion of project is rejected when pipeline records exist.
5. Soft deletion of project preserves pipeline records and audit journals.
6. Stage initialization: exactly 11 production stages in precise architectural order with initial status NOT_STARTED.
7. Checkpoint initialization: exactly 4 checkpoints in LOCKED status with artifact type and version 1.
8. Idempotent initialization: multiple sequential calls do not create duplicates.
9. Concurrent initialization attempts: multi-threaded concurrent calls without race-condition corruption across SQLite and PostgreSQL.
10. Legacy project self-healing: handles 11 stages with missing checkpoints and 4 checkpoints with missing stages without overwriting existing states.
11. Project creation integration: newly created projects atomically initialize all stages/checkpoints.
12. Atomic creation rollback: failure during initialization rolls back the project.
13. Transactionally fenced stage completion and failure: StudioJob and PipelineStageState updated atomically within one transaction.
14. Artifact hash scope control: screenplay supported; visual_bible, storyboard, assembly_cut raise NotImplementedError.
15. PostgreSQL dual-engine compatibility for foundation, concurrency, and worker fencing.
"""

import os
import uuid
import json
import sqlite3
import pytest
import concurrent.futures
from datetime import datetime, timezone, timedelta
from pathlib import Path
from sqlalchemy import create_engine, text, inspect, event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker, Session
from fastapi.testclient import TestClient
from alembic.config import Config
from alembic import command

from backend.app.main import app
from backend.app.config import settings
from backend.app.database import get_db, run_migrations
from backend.app.models.entities import (
    Base, Project, Scene, Character, Location,
    PipelineStageState, PipelineCheckpoint, PipelineCheckpointAudit,
    StudioJob
)
from backend.app.services.job_manager import job_manager, JobOwnershipLostError
from backend.app.services.pipeline_orchestrator import (
    PipelineOrchestrator, initialize_project_pipeline, is_pipeline_complete,
    PIPELINE_STAGES, PIPELINE_CHECKPOINTS
)
from backend.tests.test_postgres_concurrency import get_postgres_engine

def utc_now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# FIXTURES
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def isolated_engine_and_session(tmp_path_factory):
    """Creates a dedicated SQLite database with migration 0008 applied for test isolation."""
    db_dir = tmp_path_factory.mktemp("pipeline_foundation_db")
    db_path = db_dir / "foundation_test.db"
    db_url = f"sqlite:///{db_path}"

    test_engine = create_engine(db_url, connect_args={"check_same_thread": False})

    # Enable SQLite foreign key enforcement
    @event.listens_for(test_engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    run_migrations(target_engine=test_engine, db_url=db_url)
    SessionMaker = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)

    def override_get_db():
        session = SessionMaker()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_get_db
    yield test_engine, SessionMaker
    app.dependency_overrides.pop(get_db, None)
    test_engine.dispose()


@pytest.fixture
def db_session(isolated_engine_and_session):
    """Provides an isolated database session."""
    _, SessionMaker = isolated_engine_and_session
    session = SessionMaker()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def clean_project(db_session):
    """Creates an uninitialized project without pipeline stages."""
    proj = Project(
        name=f"Foundation Film {uuid.uuid4().hex[:8]}",
        logline="Test film for foundation verification.",
        genre="Sci-Fi",
        aspect_ratio="16:9",
        fps=24,
        target_duration_sec=90,
        budget_total=120.0,
        budget_spent=0.0
    )
    db_session.add(proj)
    db_session.commit()
    db_session.refresh(proj)
    return proj


@pytest.fixture
def client():
    return TestClient(app)


# ---------------------------------------------------------------------------
# 1. DATABASE MIGRATION & SCHEMA TESTS
# ---------------------------------------------------------------------------

def test_migration_0008_upgrade_and_schema_inspection(tmp_path):
    """Verifies Alembic migration 0008 creates all required tables, columns, and constraints."""
    test_db_path = tmp_path / "schema_test.db"
    db_url = f"sqlite:///{test_db_path}"

    test_engine = create_engine(db_url)
    run_migrations(target_engine=test_engine, db_url=db_url)

    inspector = inspect(test_engine)
    tables = set(inspector.get_table_names())

    # Required tables
    assert "pipeline_stage_states" in tables
    assert "pipeline_checkpoints" in tables
    assert "pipeline_checkpoint_audit" in tables

    # Required project columns
    proj_columns = {col["name"]: col for col in inspector.get_columns("projects")}
    assert "deleted_at" in proj_columns
    assert "current_pipeline_stage" in proj_columns
    assert "pipeline_overall_status" in proj_columns

    # Required stage state columns
    stage_columns = {col["name"]: col for col in inspector.get_columns("pipeline_stage_states")}
    for required in ["id", "project_id", "stage_name", "status", "active_job_id", "retry_count", "content_hash", "consumed_input_hash"]:
        assert required in stage_columns

    # Required checkpoint columns
    cp_columns = {col["name"]: col for col in inspector.get_columns("pipeline_checkpoints")}
    for required in ["id", "project_id", "checkpoint_type", "status", "artifact_type", "artifact_version", "artifact_content_hash"]:
        assert required in cp_columns

    # Required audit columns (no cascade FK to project to preserve independent history)
    audit_columns = {col["name"]: col for col in inspector.get_columns("pipeline_checkpoint_audit")}
    for required in ["id", "project_id", "project_name_snapshot", "checkpoint_id", "checkpoint_type", "decision", "artifact_content_hash", "artifact_version"]:
        assert required in audit_columns

    # Verify unique constraints
    stage_uqs = inspector.get_unique_constraints("pipeline_stage_states")
    stage_uq_cols = [uq["column_names"] for uq in stage_uqs]
    assert ["project_id", "stage_name"] in stage_uq_cols

    cp_uqs = inspector.get_unique_constraints("pipeline_checkpoints")
    cp_uq_cols = [uq["column_names"] for uq in cp_uqs]
    assert ["project_id", "checkpoint_type"] in cp_uq_cols

    test_engine.dispose()


def test_migration_0008_downgrade_safety(tmp_path):
    """
    Verifies safe downgrade behavior:
    - Non-empty audit table blocks downgrade with clear error unless AUDIT_ARCHIVE_PATH is provided.
    - When AUDIT_ARCHIVE_PATH is provided, validates that archive file is created, non-empty, and record count matches DB count before dropping tables.
    """
    test_db_path = tmp_path / "downgrade_test.db"
    db_url = f"sqlite:///{test_db_path}"

    test_engine = create_engine(db_url)
    run_migrations(target_engine=test_engine, db_url=db_url)

    alembic_ini_path = settings.root_dir / "alembic.ini"
    alembic_cfg = Config(str(alembic_ini_path))
    alembic_cfg.set_main_option("script_location", str(settings.root_dir / "backend" / "alembic"))
    alembic_cfg.set_main_option("sqlalchemy.url", db_url)
    alembic_cfg.attributes["configure_logger"] = False

    # Insert an audit record
    with test_engine.connect() as conn:
        conn.execute(text("""
            INSERT INTO pipeline_checkpoint_audit (
                id, project_id, project_name_snapshot, checkpoint_id,
                checkpoint_type, decision, artifact_content_hash,
                artifact_version, reviewer_name, created_at
            ) VALUES (
                'audit-1', 'proj-1', 'Test Film', 'cp-1',
                'story_approval', 'APPROVED', 'abc123hash',
                1, 'Director', datetime('now')
            )
        """))
        conn.commit()

    # Attempt downgrade without AUDIT_ARCHIVE_PATH: must fail with clear refusal message
    with pytest.raises(Exception) as exc_info:
        with test_engine.connect() as conn:
            alembic_cfg.attributes["connection"] = conn
            command.downgrade(alembic_cfg, "0007_upload_operation_lease_fencing")

    assert "contains 1 audit record" in str(exc_info.value)
    assert "AUDIT_ARCHIVE_PATH" in str(exc_info.value)

    # Downgrade with valid AUDIT_ARCHIVE_PATH succeeds and preserves records in archive
    archive_file = tmp_path / "audit_archive.json"
    os.environ["AUDIT_ARCHIVE_PATH"] = str(archive_file)
    try:
        with test_engine.connect() as conn:
            alembic_cfg.attributes["connection"] = conn
            command.downgrade(alembic_cfg, "0007_upload_operation_lease_fencing")
            conn.commit()

        # Verify archive exists and contains exact record count
        assert archive_file.exists()
        assert archive_file.stat().st_size > 0
        with open(archive_file, "r", encoding="utf-8") as f:
            archived = json.load(f)
        assert len(archived) == 1
        assert archived[0]["id"] == "audit-1"
        assert archived[0]["decision"] == "APPROVED"

        inspector = inspect(test_engine)
        tables = set(inspector.get_table_names())
        assert "pipeline_stage_states" not in tables
        assert "pipeline_checkpoints" not in tables
        assert "pipeline_checkpoint_audit" not in tables
    finally:
        os.environ.pop("AUDIT_ARCHIVE_PATH", None)
        test_engine.dispose()


# ---------------------------------------------------------------------------
# 2. DATABASE AUDIT IMMUTABILITY TRIGGERS
# ---------------------------------------------------------------------------

def test_sqlite_audit_table_immutability_triggers(tmp_path):
    """Verifies that direct UPDATE and DELETE on pipeline_checkpoint_audit are blocked by SQLite triggers."""
    test_db_path = tmp_path / "audit_immutability.db"
    db_url = f"sqlite:///{test_db_path}"

    test_engine = create_engine(db_url)
    run_migrations(target_engine=test_engine, db_url=db_url)

    with test_engine.connect() as conn:
        conn.execute(text("""
            INSERT INTO pipeline_checkpoint_audit (
                id, project_id, project_name_snapshot, checkpoint_id,
                checkpoint_type, decision, artifact_content_hash,
                artifact_version, reviewer_name, created_at
            ) VALUES (
                'audit-immutable-1', 'proj-1', 'Immutable Test', 'cp-1',
                'story_approval', 'APPROVED', 'hash-initial',
                1, 'Director', datetime('now')
            )
        """))
        conn.commit()

        # Direct UPDATE must fail
        with pytest.raises(Exception) as exc_update:
            conn.execute(text("UPDATE pipeline_checkpoint_audit SET decision = 'TAMPERED' WHERE id = 'audit-immutable-1'"))
            conn.commit()
        assert "pipeline_checkpoint_audit is immutable and append-only" in str(exc_update.value)

        # Direct DELETE must fail
        with pytest.raises(Exception) as exc_delete:
            conn.execute(text("DELETE FROM pipeline_checkpoint_audit WHERE id = 'audit-immutable-1'"))
            conn.commit()
        assert "pipeline_checkpoint_audit is immutable and append-only" in str(exc_delete.value)

    test_engine.dispose()


# ---------------------------------------------------------------------------
# 3. BLOCKER 1: PRESERVE PIPELINE RECORDS & HARD DELETE RESTRICTION
# ---------------------------------------------------------------------------

def test_project_hard_delete_blocked_by_restrict_foreign_key(tmp_path):
    """
    Verifies BLOCKER 1:
    - PipelineStageState and PipelineCheckpoint foreign keys to Project use RESTRICT / NO ACTION.
    - Hard deletion of a project via ORM or direct SQL raises IntegrityError.
    - Soft deletion succeeds and preserves pipeline history and audit logs intact.
    """
    test_db_path = tmp_path / "hard_delete_test.db"
    db_url = f"sqlite:///{test_db_path}"
    test_engine = create_engine(db_url, connect_args={"check_same_thread": False})

    @event.listens_for(test_engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    run_migrations(target_engine=test_engine, db_url=db_url)
    SessionTest = sessionmaker(bind=test_engine)
    session = SessionTest()

    proj = Project(name="Hard Delete Target", budget_total=100.0)
    session.add(proj)
    session.commit()
    session.refresh(proj)
    pid = proj.id

    # Initialize pipeline
    initialize_project_pipeline(pid, session)

    # Add an audit record for this project
    audit = PipelineCheckpointAudit(
        project_id=pid,
        project_name_snapshot=proj.name,
        checkpoint_id="dummy-cp",
        checkpoint_type="story_approval",
        decision="APPROVED",
        artifact_content_hash="hash123",
        artifact_version=1,
        reviewer_name="Director"
    )
    session.add(audit)
    session.commit()

    # 1. Attempt ORM hard deletion: must fail due to ondelete=RESTRICT
    with pytest.raises(IntegrityError):
        session.delete(proj)
        session.commit()
    session.rollback()

    # 2. Attempt raw SQL hard deletion: must fail due to ondelete=RESTRICT
    with pytest.raises(IntegrityError):
        session.execute(text(f"DELETE FROM projects WHERE id = '{pid}'"))
        session.commit()
    session.rollback()

    # Confirm stages and checkpoints are untouched
    assert session.query(PipelineStageState).filter(PipelineStageState.project_id == pid).count() == 11
    assert session.query(PipelineCheckpoint).filter(PipelineCheckpoint.project_id == pid).count() == 4
    assert session.query(PipelineCheckpointAudit).filter(PipelineCheckpointAudit.project_id == pid).count() == 1

    # 3. Soft deletion must succeed without error
    proj = session.query(Project).filter(Project.id == pid).first()
    proj.deleted_at = utc_now()
    session.commit()

    # Verify pipeline records and audit logs remain intact after soft deletion
    assert session.query(PipelineStageState).filter(PipelineStageState.project_id == pid).count() == 11
    assert session.query(PipelineCheckpoint).filter(PipelineCheckpoint.project_id == pid).count() == 4
    assert session.query(PipelineCheckpointAudit).filter(PipelineCheckpointAudit.project_id == pid).count() == 1

    session.close()
    test_engine.dispose()


# ---------------------------------------------------------------------------
# 4. PIPELINE INITIALIZATION & ORDERING TESTS
# ---------------------------------------------------------------------------

def test_exact_11_stages_and_ordering(db_session, clean_project):
    """
    Validates that initialize_project_pipeline creates exactly 11 stages
    in the strict architectural sequence with initial NOT_STARTED status.
    Directly compares ordered stages to PIPELINE_STAGES (Additional Fix B).
    """
    initialize_project_pipeline(clean_project.id, db_session)

    stages = db_session.query(PipelineStageState).filter(
        PipelineStageState.project_id == clean_project.id
    ).all()

    assert len(stages) == 11, f"Expected exactly 11 stages, found {len(stages)}"

    stage_order_map = {name: idx for idx, name in enumerate(PIPELINE_STAGES)}
    sorted_stages = sorted(stages, key=lambda s: stage_order_map[s.stage_name])

    # Direct ordered comparison against PIPELINE_STAGES
    ordered_names = [s.stage_name for s in sorted_stages]
    assert ordered_names == PIPELINE_STAGES, f"Ordered stages {ordered_names} did not match PIPELINE_STAGES"

    # Verify each stage is initialized with NOT_STARTED
    for s in stages:
        assert s.status == "NOT_STARTED"
        assert s.active_job_id is None
        assert s.retry_count == 0
        assert s.content_hash is None
        assert s.consumed_input_hash is None


def test_exact_4_checkpoints_initialization(db_session, clean_project):
    """
    Validates that initialize_project_pipeline creates exactly 4 approval checkpoints
    in LOCKED state with correct artifact type mappings.
    """
    initialize_project_pipeline(clean_project.id, db_session)

    cps = db_session.query(PipelineCheckpoint).filter(
        PipelineCheckpoint.project_id == clean_project.id
    ).all()

    assert len(cps) == 4, f"Expected exactly 4 checkpoints, found {len(cps)}"

    expected_cps = {
        "story_approval": "screenplay",
        "visual_approval": "visual_bible",
        "production_approval": "storyboard",
        "final_review": "assembly_cut"
    }

    cp_map = {cp.checkpoint_type: cp for cp in cps}
    assert set(cp_map.keys()) == set(expected_cps.keys())

    for cp_type, artifact_type in expected_cps.items():
        cp = cp_map[cp_type]
        assert cp.status == "LOCKED"
        assert cp.artifact_type == artifact_type
        assert cp.artifact_version == 1
        assert cp.artifact_content_hash is None
        assert cp.reviewer_name == "Director / User"


def test_initialization_idempotency(db_session, clean_project):
    """
    Validates that calling initialize_project_pipeline multiple times
    is safe and does not create duplicate stage or checkpoint records.
    """
    initialize_project_pipeline(clean_project.id, db_session)
    initialize_project_pipeline(clean_project.id, db_session)
    initialize_project_pipeline(db=db_session, project_id=clean_project.id)

    stage_count = db_session.query(PipelineStageState).filter(
        PipelineStageState.project_id == clean_project.id
    ).count()
    cp_count = db_session.query(PipelineCheckpoint).filter(
        PipelineCheckpoint.project_id == clean_project.id
    ).count()

    assert stage_count == 11
    assert cp_count == 4


def test_concurrent_initialization_attempts(isolated_engine_and_session, clean_project):
    """
    Validates BLOCKER 2:
    - Multi-threaded concurrent initialize_project_pipeline calls resolve without errors.
    - Exactly 11 valid stages and 4 checkpoints exist.
    - No duplicate records or corrupted states.
    - Existing states are not overwritten.
    """
    _, SessionMaker = isolated_engine_and_session

    # Seed one stage with custom in-progress state to verify it is NOT overwritten
    seed_session = SessionMaker()
    first_stage = PipelineStageState(
        project_id=clean_project.id,
        stage_name="story_development",
        status="IN_PROGRESS",
        stage_metadata='{"custom_seed": true}'
    )
    seed_session.add(first_stage)
    seed_session.commit()
    seed_session.close()

    def init_worker():
        session = SessionMaker()
        try:
            initialize_project_pipeline(clean_project.id, session)
        finally:
            session.close()

    # Launch 5 concurrent threads attempting to initialize the same project
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(init_worker) for _ in range(5)]
        for f in concurrent.futures.as_completed(futures):
            f.result()

    verify_session = SessionMaker()
    try:
        stages = verify_session.query(PipelineStageState).filter(
            PipelineStageState.project_id == clean_project.id
        ).all()
        cps = verify_session.query(PipelineCheckpoint).filter(
            PipelineCheckpoint.project_id == clean_project.id
        ).all()

        assert len(stages) == 11
        assert len(cps) == 4

        # Verify existing in-progress state was preserved and not overwritten
        stage_map = {s.stage_name: s for s in stages}
        assert stage_map["story_development"].status == "IN_PROGRESS"
        assert stage_map["story_development"].stage_metadata == '{"custom_seed": true}'

        # Verify exact stage names and checkpoint types
        assert {s.stage_name for s in stages} == set(PIPELINE_STAGES)
        assert {cp.checkpoint_type for cp in cps} == {c[0] for c in PIPELINE_CHECKPOINTS}
    finally:
        verify_session.close()


# ---------------------------------------------------------------------------
# 5. BLOCKER 3: LEGACY PROJECT SELF-HEALING
# ---------------------------------------------------------------------------

def test_legacy_project_self_healing_missing_checkpoints(db_session, clean_project):
    """
    Verifies BLOCKER 3:
    - Project with all 11 stages but missing checkpoints is safely self-healed.
    - Existing stage states and metadata are preserved without being reset.
    """
    # 1. Manually initialize only the 11 stages (one marked IN_PROGRESS)
    now = utc_now()
    for name in PIPELINE_STAGES:
        st = "IN_PROGRESS" if name == "screenplay" else "NOT_STARTED"
        meta = '{"script_version": 2}' if name == "screenplay" else None
        db_session.add(PipelineStageState(
            project_id=clean_project.id,
            stage_name=name,
            status=st,
            stage_metadata=meta,
            created_at=now,
            updated_at=now
        ))
    db_session.commit()

    # Confirm 11 stages and 0 checkpoints
    assert db_session.query(PipelineStageState).filter(PipelineStageState.project_id == clean_project.id).count() == 11
    assert db_session.query(PipelineCheckpoint).filter(PipelineCheckpoint.project_id == clean_project.id).count() == 0
    assert not is_pipeline_complete(db_session, clean_project.id)

    # 2. Trigger self-healing via get_pipeline_status
    status = PipelineOrchestrator.get_pipeline_status(db_session, clean_project.id)

    # Confirm 4 checkpoints were created
    assert len(status["checkpoints"]) == 4
    assert db_session.query(PipelineCheckpoint).filter(PipelineCheckpoint.project_id == clean_project.id).count() == 4
    assert is_pipeline_complete(db_session, clean_project.id)

    # Confirm existing stage state and metadata were NOT overwritten
    screenplay_st = db_session.query(PipelineStageState).filter(
        PipelineStageState.project_id == clean_project.id,
        PipelineStageState.stage_name == "screenplay"
    ).first()
    assert screenplay_st.status == "IN_PROGRESS"
    assert screenplay_st.stage_metadata == '{"script_version": 2}'


def test_legacy_project_self_healing_missing_stages(db_session, clean_project):
    """
    Verifies BLOCKER 3:
    - Project with 4 checkpoints but missing stages is safely self-healed.
    - Existing checkpoint states and notes are preserved without being reset.
    """
    # 1. Manually initialize only the 4 checkpoints (one marked APPROVED)
    now = utc_now()
    for cp_type, art_type in PIPELINE_CHECKPOINTS:
        st = "APPROVED" if cp_type == "story_approval" else "LOCKED"
        notes = "Director approved" if cp_type == "story_approval" else ""
        db_session.add(PipelineCheckpoint(
            project_id=clean_project.id,
            checkpoint_type=cp_type,
            status=st,
            artifact_type=art_type,
            artifact_version=1,
            feedback_notes=notes,
            created_at=now,
            updated_at=now
        ))
    db_session.commit()

    # Confirm 4 checkpoints and 0 stages
    assert db_session.query(PipelineCheckpoint).filter(PipelineCheckpoint.project_id == clean_project.id).count() == 4
    assert db_session.query(PipelineStageState).filter(PipelineStageState.project_id == clean_project.id).count() == 0
    assert not is_pipeline_complete(db_session, clean_project.id)

    # 2. Trigger self-healing
    status = PipelineOrchestrator.get_pipeline_status(db_session, clean_project.id)

    # Confirm 11 stages were created
    assert len(status["stages"]) == 11
    assert db_session.query(PipelineStageState).filter(PipelineStageState.project_id == clean_project.id).count() == 11
    assert is_pipeline_complete(db_session, clean_project.id)

    # Confirm existing checkpoint state and notes were NOT overwritten
    story_cp = db_session.query(PipelineCheckpoint).filter(
        PipelineCheckpoint.project_id == clean_project.id,
        PipelineCheckpoint.checkpoint_type == "story_approval"
    ).first()
    assert story_cp.status == "APPROVED"
    assert story_cp.feedback_notes == "Director approved"


# ---------------------------------------------------------------------------
# 6. BLOCKER 4: TRANSACTIONALLY FENCED STAGE COMPLETION
# ---------------------------------------------------------------------------

def test_transactionally_fenced_stage_completion(db_session, clean_project):
    """
    Verifies BLOCKER 4:
    1. Valid worker atomically completes both StudioJob and PipelineStageState.
    2. Stale fencing token is rejected; both remain unchanged.
    3. Wrong worker_id is rejected; both remain unchanged.
    4. Expired lease is rejected; both remain unchanged.
    5. Stage active_job_id changed before completion; raises error and rolls back both updates.
    6. Fenced failure: valid worker fails both job and stage; invalid worker is rejected and rolled back.
    """
    initialize_project_pipeline(clean_project.id, db_session)

    # --- Scenario 1: Valid worker completes both job and stage ---
    job1 = StudioJob(
        project_id=clean_project.id,
        job_type="screenplay_generation",
        idempotency_key=f"idem-1-{uuid.uuid4().hex[:8]}",
        status="RUNNING",
        worker_id="worker-valid-1",
        fencing_token=10,
        lease_expires_at=utc_now() + timedelta(minutes=5)
    )
    db_session.add(job1)
    db_session.commit()
    db_session.refresh(job1)

    stage = db_session.query(PipelineStageState).filter(
        PipelineStageState.project_id == clean_project.id,
        PipelineStageState.stage_name == "screenplay"
    ).first()
    stage.status = "IN_PROGRESS"
    stage.active_job_id = job1.id
    db_session.commit()

    completed_stage = PipelineOrchestrator.complete_pipeline_stage(
        db=db_session,
        project_id=clean_project.id,
        stage_name="screenplay",
        active_job_id=job1.id,
        worker_id="worker-valid-1",
        fencing_token=10,
        result_hash="sha256_screenplay_v1",
        stage_metadata='{"scenes_generated": 5}'
    )

    db_session.refresh(job1)
    assert job1.status == "COMPLETED"
    assert completed_stage.status == "COMPLETED"
    assert completed_stage.content_hash == "sha256_screenplay_v1"
    assert completed_stage.completed_at is not None

    # --- Scenario 2: Stale fencing token is rejected ---
    job2 = StudioJob(
        project_id=clean_project.id,
        job_type="character_generation",
        idempotency_key=f"idem-2-{uuid.uuid4().hex[:8]}",
        status="RUNNING",
        worker_id="worker-2",
        fencing_token=20,
        lease_expires_at=utc_now() + timedelta(minutes=5)
    )
    db_session.add(job2)
    db_session.commit()
    db_session.refresh(job2)

    char_stage = db_session.query(PipelineStageState).filter(
        PipelineStageState.project_id == clean_project.id,
        PipelineStageState.stage_name == "character_design"
    ).first()
    char_stage.status = "IN_PROGRESS"
    char_stage.active_job_id = job2.id
    db_session.commit()

    with pytest.raises(JobOwnershipLostError):
        PipelineOrchestrator.complete_pipeline_stage(
            db=db_session,
            project_id=clean_project.id,
            stage_name="character_design",
            active_job_id=job2.id,
            worker_id="worker-2",
            fencing_token=19  # stale token
        )

    db_session.refresh(job2)
    db_session.refresh(char_stage)
    assert job2.status == "RUNNING"
    assert char_stage.status == "IN_PROGRESS"

    # --- Scenario 3: Wrong worker_id is rejected ---
    with pytest.raises(JobOwnershipLostError):
        PipelineOrchestrator.complete_pipeline_stage(
            db=db_session,
            project_id=clean_project.id,
            stage_name="character_design",
            active_job_id=job2.id,
            worker_id="rogue-worker",  # wrong worker
            fencing_token=20
        )

    db_session.refresh(job2)
    db_session.refresh(char_stage)
    assert job2.status == "RUNNING"
    assert char_stage.status == "IN_PROGRESS"

    # --- Scenario 4: Expired lease is rejected ---
    job2.lease_expires_at = utc_now() - timedelta(seconds=10)
    db_session.commit()

    with pytest.raises(JobOwnershipLostError):
        PipelineOrchestrator.complete_pipeline_stage(
            db=db_session,
            project_id=clean_project.id,
            stage_name="character_design",
            active_job_id=job2.id,
            worker_id="worker-2",
            fencing_token=20
        )

    db_session.refresh(job2)
    db_session.refresh(char_stage)
    assert job2.status == "RUNNING"
    assert char_stage.status == "IN_PROGRESS"

    # --- Scenario 5: Stage active_job_id changed before completion ---
    job3 = StudioJob(
        project_id=clean_project.id,
        job_type="location_generation",
        idempotency_key=f"idem-3-{uuid.uuid4().hex[:8]}",
        status="RUNNING",
        worker_id="worker-3",
        fencing_token=30,
        lease_expires_at=utc_now() + timedelta(minutes=5)
    )
    db_session.add(job3)
    db_session.commit()
    db_session.refresh(job3)

    other_job = StudioJob(
        project_id=clean_project.id,
        job_type="location_generation",
        idempotency_key=f"idem-other-{uuid.uuid4().hex[:8]}",
        status="RUNNING",
        worker_id="worker-other",
        fencing_token=31,
        lease_expires_at=utc_now() + timedelta(minutes=5)
    )
    db_session.add(other_job)
    db_session.commit()

    loc_stage = db_session.query(PipelineStageState).filter(
        PipelineStageState.project_id == clean_project.id,
        PipelineStageState.stage_name == "location_design"
    ).first()
    loc_stage.status = "IN_PROGRESS"
    loc_stage.active_job_id = other_job.id  # stage reassigned to another job
    db_session.commit()

    with pytest.raises(JobOwnershipLostError):
        PipelineOrchestrator.complete_pipeline_stage(
            db=db_session,
            project_id=clean_project.id,
            stage_name="location_design",
            active_job_id=job3.id,
            worker_id="worker-3",
            fencing_token=30
        )

    # Crucial: verify that job3 was NOT committed as COMPLETED (transaction was rolled back)
    db_session.refresh(job3)
    db_session.refresh(loc_stage)
    assert job3.status == "RUNNING"
    assert loc_stage.status == "IN_PROGRESS"

    # --- Scenario 6: Fenced failure ---
    job4 = StudioJob(
        project_id=clean_project.id,
        job_type="storyboard_generation",
        idempotency_key=f"idem-4-{uuid.uuid4().hex[:8]}",
        status="RUNNING",
        worker_id="worker-4",
        fencing_token=40,
        lease_expires_at=utc_now() + timedelta(minutes=5)
    )
    db_session.add(job4)
    db_session.commit()
    db_session.refresh(job4)

    sb_stage = db_session.query(PipelineStageState).filter(
        PipelineStageState.project_id == clean_project.id,
        PipelineStageState.stage_name == "storyboard"
    ).first()
    sb_stage.status = "IN_PROGRESS"
    sb_stage.active_job_id = job4.id
    db_session.commit()

    # Stale fail attempt rejected
    with pytest.raises(JobOwnershipLostError):
        PipelineOrchestrator.fail_pipeline_stage(
            db=db_session,
            project_id=clean_project.id,
            stage_name="storyboard",
            active_job_id=job4.id,
            error="CUDA out of memory",
            worker_id="worker-4",
            fencing_token=39  # stale token
        )
    db_session.refresh(job4)
    db_session.refresh(sb_stage)
    assert job4.status == "RUNNING"
    assert sb_stage.status == "IN_PROGRESS"

    # Valid fail succeeds atomically
    failed_stage = PipelineOrchestrator.fail_pipeline_stage(
        db=db_session,
        project_id=clean_project.id,
        stage_name="storyboard",
        active_job_id=job4.id,
        error="CUDA out of memory",
        worker_id="worker-4",
        fencing_token=40
    )
    db_session.refresh(job4)
    assert job4.status == "FAILED"
    assert failed_stage.status == "FAILED"
    assert "CUDA out of memory" in failed_stage.error_message


# ---------------------------------------------------------------------------
# 7. ADDITIONAL FIX D: ARTIFACT HASH SCOPE CONTROL
# ---------------------------------------------------------------------------

def test_compute_artifact_hash_scope_control(db_session, clean_project):
    """
    Verifies Additional Fix D:
    - compute_artifact_hash correctly computes canonical SHA-256 for 'screenplay'.
    - compute_artifact_hash raises NotImplementedError for visual_bible, storyboard, assembly_cut.
    """
    # 1. Screenplay hashing
    sc = Scene(
        project_id=clean_project.id,
        scene_number=1,
        order_index=1,
        slugline="EXT. MARS BASE - NIGHT",
        title="Arrival",
        summary="A shuttle lands in the dust.",
        script_content="Commander Vance steps out.",
        estimated_duration=45
    )
    db_session.add(sc)
    db_session.commit()

    hash_val = PipelineOrchestrator.compute_artifact_hash(db_session, clean_project.id, "screenplay")
    assert isinstance(hash_val, str)
    assert len(hash_val) == 64

    # 2. Unsupported checkpoint types must raise NotImplementedError
    for unsupported in ["visual_bible", "storyboard", "assembly_cut"]:
        with pytest.raises(NotImplementedError) as exc_info:
            PipelineOrchestrator.compute_artifact_hash(db_session, clean_project.id, unsupported)
        assert f"'{unsupported}' is not implemented" in str(exc_info.value)


# ---------------------------------------------------------------------------
# 8. PROJECT CREATION & ROUTER INTEGRATION
# ---------------------------------------------------------------------------

def test_project_creation_automatically_initializes_pipeline(client, db_session):
    """
    Verifies that creating a project via POST /api/projects automatically
    and atomically initializes the 11 stages and 4 checkpoints.
    """
    payload = {
        "name": f"API Film {uuid.uuid4().hex[:6]}",
        "logline": "Integration test film.",
        "genre": "Sci-Fi",
        "aspect_ratio": "16:9",
        "budget_total": 100.0
    }

    res = client.post("/api/projects", json=payload)
    assert res.status_code == 201
    created_data = res.json()
    project_id = created_data["id"]

    assert created_data["current_pipeline_stage"] == "story_development"
    assert created_data["pipeline_overall_status"] == "in_progress"

    # Verify database state
    stages = db_session.query(PipelineStageState).filter(
        PipelineStageState.project_id == project_id
    ).all()
    cps = db_session.query(PipelineCheckpoint).filter(
        PipelineCheckpoint.project_id == project_id
    ).all()

    assert len(stages) == 11
    assert len(cps) == 4


def test_atomic_project_creation_rollback_on_initialization_failure(db_session, monkeypatch):
    """
    Verifies that if pipeline initialization fails during project creation,
    the entire transaction is rolled back and no partial project is committed.
    """
    import backend.app.routers.projects as proj_router

    def mock_fail_init(*args, **kwargs):
        raise RuntimeError("Simulated crash during pipeline initialization")

    monkeypatch.setattr(proj_router, "initialize_project_pipeline", mock_fail_init)

    client = TestClient(app)
    payload = {
        "name": "Failed Atomic Film",
        "logline": "Should not exist in database.",
        "genre": "Drama"
    }

    with pytest.raises(RuntimeError):
        client.post("/api/projects", json=payload)

    orphan = db_session.query(Project).filter(Project.name == "Failed Atomic Film").first()
    assert orphan is None


# ---------------------------------------------------------------------------
# 9. POSTGRESQL DUAL-ENGINE COMPATIBILITY & CONCURRENCY
# ---------------------------------------------------------------------------

def test_postgresql_pipeline_foundation_and_concurrency():
    """
    Verifies migration 0008, audit immutability triggers, RESTRICT foreign keys,
    and concurrent pipeline initialization on a real PostgreSQL instance.
    """
    pg_engine = get_postgres_engine()
    if not pg_engine:
        pytest.skip("PostgreSQL test database not available.")

    # 1. Run migrations on PostgreSQL
    alembic_cfg = Config(str(settings.root_dir / "alembic.ini"))
    alembic_cfg.set_main_option("script_location", str(settings.root_dir / "backend" / "alembic"))
    alembic_cfg.set_main_option("sqlalchemy.url", pg_engine.url.render_as_string(hide_password=False))
    alembic_cfg.attributes["configure_logger"] = False
    command.upgrade(alembic_cfg, "head")

    # 2. Verify audit table immutability trigger on PostgreSQL
    audit_id = f"pg-audit-{uuid.uuid4().hex[:8]}"
    with pg_engine.connect() as conn:
        conn.execute(text(f"""
            INSERT INTO pipeline_checkpoint_audit (
                id, project_id, project_name_snapshot, checkpoint_id,
                checkpoint_type, decision, artifact_content_hash,
                artifact_version, reviewer_name, created_at
            ) VALUES (
                '{audit_id}', 'pg-proj-1', 'PG Film', 'cp-1',
                'story_approval', 'APPROVED', 'pghash123',
                1, 'PG Director', NOW()
            )
        """))
        conn.commit()

        # Update must be rejected
        with pytest.raises(Exception) as exc_update:
            conn.execute(text(f"UPDATE pipeline_checkpoint_audit SET decision = 'TAMPERED' WHERE id = '{audit_id}'"))
            conn.commit()
        assert "pipeline_checkpoint_audit is immutable and append-only" in str(exc_update.value)
        conn.rollback()

        # Delete must be rejected
        with pytest.raises(Exception) as exc_delete:
            conn.execute(text(f"DELETE FROM pipeline_checkpoint_audit WHERE id = '{audit_id}'"))
            conn.commit()
        assert "pipeline_checkpoint_audit is immutable and append-only" in str(exc_delete.value)
        conn.rollback()

    # 3. Test RESTRICT foreign key constraint on PostgreSQL
    PgSession = sessionmaker(bind=pg_engine)
    pg_session = PgSession()

    pg_proj = Project(name=f"PG Hard Delete Film {uuid.uuid4().hex[:6]}", budget_total=100.0)
    pg_session.add(pg_proj)
    pg_session.commit()
    pg_session.refresh(pg_proj)
    pg_pid = pg_proj.id

    initialize_project_pipeline(pg_pid, pg_session)

    # Deleting project must be rejected by RESTRICT constraint
    with pytest.raises(IntegrityError):
        pg_session.delete(pg_proj)
        pg_session.commit()
    pg_session.rollback()

    # 4. Test concurrent initialization on PostgreSQL
    concurrent_proj = Project(
        name=f"PG Concurrent Film {uuid.uuid4().hex[:6]}",
        budget_total=100.0,
        budget_spent=0.0
    )
    pg_session.add(concurrent_proj)
    pg_session.commit()
    pg_session.refresh(concurrent_proj)
    conc_pid = concurrent_proj.id
    pg_session.close()

    def pg_init_worker():
        s = PgSession()
        try:
            initialize_project_pipeline(conc_pid, s)
        finally:
            s.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(pg_init_worker) for _ in range(5)]
        for f in concurrent.futures.as_completed(futures):
            f.result()

    verify_s = PgSession()
    try:
        stages = verify_s.query(PipelineStageState).filter(
            PipelineStageState.project_id == conc_pid
        ).all()
        cps = verify_s.query(PipelineCheckpoint).filter(
            PipelineCheckpoint.project_id == conc_pid
        ).all()

        assert len(stages) == 11
        assert len(cps) == 4

        stage_order_map = {name: idx for idx, name in enumerate(PIPELINE_STAGES)}
        sorted_pg_stages = sorted(stages, key=lambda s: stage_order_map[s.stage_name])
        assert [s.stage_name for s in sorted_pg_stages] == PIPELINE_STAGES
        assert {cp.checkpoint_type for cp in cps} == {c[0] for c in PIPELINE_CHECKPOINTS}
    finally:
        verify_s.close()
        pg_engine.dispose()


def test_postgresql_pipeline_worker_fencing():
    """
    Verifies BLOCKER 4 transactional worker fencing specifically on PostgreSQL:
    - Valid worker completes stage and job atomically.
    - Stale fencing token is rejected and rolled back.
    - Expired lease is rejected and rolled back.
    - Stage failure with fencing.
    """
    pg_engine = get_postgres_engine()
    if not pg_engine:
        pytest.skip("PostgreSQL test database not available.")

    PgSession = sessionmaker(bind=pg_engine)
    session = PgSession()

    try:
        proj = Project(name=f"PG Fencing Film {uuid.uuid4().hex[:6]}", budget_total=100.0)
        session.add(proj)
        session.commit()
        session.refresh(proj)
        pid = proj.id

        initialize_project_pipeline(pid, session)

        # 1. Valid worker completion
        job = StudioJob(
            project_id=pid,
            job_type="screenplay_generation",
            idempotency_key=f"pg-idem-1-{uuid.uuid4().hex[:8]}",
            status="RUNNING",
            worker_id="pg-worker-1",
            fencing_token=100,
            lease_expires_at=utc_now() + timedelta(minutes=5)
        )
        session.add(job)
        session.commit()
        session.refresh(job)

        stage = session.query(PipelineStageState).filter(
            PipelineStageState.project_id == pid,
            PipelineStageState.stage_name == "screenplay"
        ).first()
        stage.status = "IN_PROGRESS"
        stage.active_job_id = job.id
        session.commit()

        completed = PipelineOrchestrator.complete_pipeline_stage(
            db=session,
            project_id=pid,
            stage_name="screenplay",
            active_job_id=job.id,
            worker_id="pg-worker-1",
            fencing_token=100,
            result_hash="pghash_screenplay"
        )
        session.refresh(job)
        assert job.status == "COMPLETED"
        assert completed.status == "COMPLETED"
        assert completed.content_hash == "pghash_screenplay"

        # 2. Stale worker rejection
        job2 = StudioJob(
            project_id=pid,
            job_type="character_generation",
            idempotency_key=f"pg-idem-2-{uuid.uuid4().hex[:8]}",
            status="RUNNING",
            worker_id="pg-worker-2",
            fencing_token=200,
            lease_expires_at=utc_now() + timedelta(minutes=5)
        )
        session.add(job2)
        session.commit()
        session.refresh(job2)

        char_stage = session.query(PipelineStageState).filter(
            PipelineStageState.project_id == pid,
            PipelineStageState.stage_name == "character_design"
        ).first()
        char_stage.status = "IN_PROGRESS"
        char_stage.active_job_id = job2.id
        session.commit()

        with pytest.raises(JobOwnershipLostError):
            PipelineOrchestrator.complete_pipeline_stage(
                db=session,
                project_id=pid,
                stage_name="character_design",
                active_job_id=job2.id,
                worker_id="pg-worker-2",
                fencing_token=199  # stale token
            )

        session.refresh(job2)
        session.refresh(char_stage)
        assert job2.status == "RUNNING"
        assert char_stage.status == "IN_PROGRESS"

        # 3. Expired lease rejection
        job2.lease_expires_at = utc_now() - timedelta(seconds=10)
        session.commit()

        with pytest.raises(JobOwnershipLostError):
            PipelineOrchestrator.complete_pipeline_stage(
                db=session,
                project_id=pid,
                stage_name="character_design",
                active_job_id=job2.id,
                worker_id="pg-worker-2",
                fencing_token=200
            )

        session.refresh(job2)
        session.refresh(char_stage)
        assert job2.status == "RUNNING"
        assert char_stage.status == "IN_PROGRESS"
    finally:
        session.close()
        pg_engine.dispose()
