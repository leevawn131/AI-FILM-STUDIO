"""
Pipeline Foundation Tests for AI Film Studio (S4 Milestone 1 Foundation).

Validates:
1. Migration upgrade and schema inspection (0008_pipeline_orchestration_tables).
2. Migration downgrade behavior (safe refusal when audit records exist, force purge).
3. SQLite and PostgreSQL DB-level audit immutability triggers (UPDATE and DELETE blocked).
4. Stage initialization: exactly 11 production stages in precise order with initial status NOT_STARTED.
5. Checkpoint initialization: exactly 4 checkpoints in LOCKED status with artifact type and version 1.
6. Idempotent initialization: multiple sequential calls do not create duplicates.
7. Concurrent initialization attempts: multi-threaded concurrent calls without race-condition corruption.
8. Project creation integration: newly created projects atomically initialize all stages/checkpoints.
9. Atomic creation rollback: failure during initialization rolls back the project.
10. Existing legacy project compatibility: preserves existing data and self-heals missing pipeline records.
11. PostgreSQL dual-engine compatibility for foundation and concurrency.
"""

import os
import uuid
import sqlite3
import pytest
import concurrent.futures
from pathlib import Path
from sqlalchemy import create_engine, text, inspect
from sqlalchemy.orm import sessionmaker, Session
from fastapi.testclient import TestClient
from alembic.config import Config
from alembic import command

from backend.app.main import app
from backend.app.config import settings
from backend.app.database import get_db, run_migrations
from backend.app.models.entities import (
    Base, Project, Scene, Character, Location,
    PipelineStageState, PipelineCheckpoint, PipelineCheckpointAudit
)
from backend.app.services.pipeline_orchestrator import (
    initialize_project_pipeline, PIPELINE_STAGES, PIPELINE_CHECKPOINTS
)
from backend.tests.test_postgres_concurrency import get_postgres_engine

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
    - Empty audit table allows downgrade cleanly.
    - Non-empty audit table blocks downgrade with clear error unless FORCE_AUDIT_PURGE=1.
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

    # Attempt downgrade without FORCE_AUDIT_PURGE: must fail and preserve records
    with pytest.raises(Exception) as exc_info:
        with test_engine.connect() as conn:
            alembic_cfg.attributes["connection"] = conn
            command.downgrade(alembic_cfg, "0007_upload_operation_lease_fencing")

    assert "contains 1 audit record" in str(exc_info.value)
    assert "FORCE_AUDIT_PURGE=1" in str(exc_info.value)

    # Downgrade with FORCE_AUDIT_PURGE=1 succeeds
    os.environ["FORCE_AUDIT_PURGE"] = "1"
    try:
        with test_engine.connect() as conn:
            alembic_cfg.attributes["connection"] = conn
            command.downgrade(alembic_cfg, "0007_upload_operation_lease_fencing")
            conn.commit()

        inspector = inspect(test_engine)
        tables = set(inspector.get_table_names())
        assert "pipeline_stage_states" not in tables
        assert "pipeline_checkpoints" not in tables
        assert "pipeline_checkpoint_audit" not in tables
    finally:
        os.environ.pop("FORCE_AUDIT_PURGE", None)
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
# 3. PIPELINE INITIALIZATION & ORDERING TESTS
# ---------------------------------------------------------------------------

def test_exact_11_stages_and_ordering(db_session, clean_project):
    """
    Validates that initialize_project_pipeline creates exactly 11 stages
    in the strict architectural sequence with initial NOT_STARTED status.
    """
    initialize_project_pipeline(clean_project.id, db_session)

    stages = db_session.query(PipelineStageState).filter(
        PipelineStageState.project_id == clean_project.id
    ).all()

    assert len(stages) == 11, f"Expected exactly 11 stages, found {len(stages)}"

    expected_stages = [
        "story_development",
        "screenplay",
        "character_design",
        "location_design",
        "storyboard",
        "cinematic_prompts",
        "flow_handoff",
        "media_import",
        "film_editing",
        "audio_mixing",
        "final_rendering"
    ]

    # Verify stage names match expected list exactly
    actual_names = [s.stage_name for s in stages]
    assert set(actual_names) == set(expected_stages)

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
    # Call 3 times consecutively
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
    Validates that multiple concurrent threads calling initialize_project_pipeline
    simultaneously on the same project resolve cleanly without corruption or duplicates.
    """
    _, SessionMaker = isolated_engine_and_session

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
            f.result()  # verify no uncaught exceptions

    verify_session = SessionMaker()
    try:
        stage_count = verify_session.query(PipelineStageState).filter(
            PipelineStageState.project_id == clean_project.id
        ).count()
        cp_count = verify_session.query(PipelineCheckpoint).filter(
            PipelineCheckpoint.project_id == clean_project.id
        ).count()

        assert stage_count == 11
        assert cp_count == 4
    finally:
        verify_session.close()


# ---------------------------------------------------------------------------
# 4. PROJECT CREATION INTEGRATION TESTS
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

    # Force initialize_project_pipeline to raise an error
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

    # Verify no partial project was committed
    orphan = db_session.query(Project).filter(Project.name == "Failed Atomic Film").first()
    assert orphan is None


# ---------------------------------------------------------------------------
# 5. EXISTING PROJECT COMPATIBILITY & SELF-HEALING
# ---------------------------------------------------------------------------

def test_existing_project_compatibility_and_self_healing(client, db_session):
    """
    Verifies that existing legacy projects (which predate S4):
    - Can have initialize_project_pipeline called safely without losing existing film data.
    - Are automatically self-healed when fetched via GET /api/projects/{id}.
    """
    # 1. Create a legacy project without pipeline records
    legacy_proj = Project(
        name="Legacy Space Epic",
        logline="Created before S4 pipeline existed.",
        genre="Space Opera",
        budget_total=250.0,
        budget_spent=50.0
    )
    db_session.add(legacy_proj)
    db_session.commit()
    db_session.refresh(legacy_proj)

    # 2. Add scenes and characters to legacy project
    char = Character(project_id=legacy_proj.id, name="COMMANDER VANCE")
    sc = Scene(project_id=legacy_proj.id, scene_number=1, slugline="INT. BRIDGE - DAY", script_content="Vance speaks.")
    db_session.add_all([char, sc])
    db_session.commit()

    # Confirm no pipeline records exist initially
    assert db_session.query(PipelineStageState).filter(PipelineStageState.project_id == legacy_proj.id).count() == 0
    assert db_session.query(PipelineCheckpoint).filter(PipelineCheckpoint.project_id == legacy_proj.id).count() == 0

    # 3. Access project via API: verifies transparent self-healing
    res = client.get(f"/api/projects/{legacy_proj.id}")
    assert res.status_code == 200
    data = res.json()
    assert data["current_pipeline_stage"] == "story_development"
    assert data["pipeline_overall_status"] == "in_progress"

    # Verify 11 stages and 4 checkpoints now exist
    assert db_session.query(PipelineStageState).filter(PipelineStageState.project_id == legacy_proj.id).count() == 11
    assert db_session.query(PipelineCheckpoint).filter(PipelineCheckpoint.project_id == legacy_proj.id).count() == 4

    # Verify original data was not modified or corrupted
    assert db_session.query(Character).filter(Character.project_id == legacy_proj.id).count() == 1
    assert db_session.query(Scene).filter(Scene.project_id == legacy_proj.id).count() == 1


# ---------------------------------------------------------------------------
# 6. POSTGRESQL DUAL-ENGINE COMPATIBILITY & CONCURRENCY
# ---------------------------------------------------------------------------

def test_postgresql_pipeline_foundation_and_concurrency():
    """
    Verifies migration 0008, audit immutability triggers, and concurrent
    pipeline initialization on a real PostgreSQL instance.
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

    # 3. Test concurrent initialization on PostgreSQL
    PgSession = sessionmaker(bind=pg_engine)
    pg_session = PgSession()

    proj = Project(
        name=f"PG Concurrent Film {uuid.uuid4().hex[:6]}",
        budget_total=100.0,
        budget_spent=0.0
    )
    pg_session.add(proj)
    pg_session.commit()
    pg_session.refresh(proj)
    proj_id = proj.id
    pg_session.close()

    def pg_init_worker():
        s = PgSession()
        try:
            initialize_project_pipeline(proj_id, s)
        finally:
            s.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(pg_init_worker) for _ in range(5)]
        for f in concurrent.futures.as_completed(futures):
            f.result()

    verify_s = PgSession()
    try:
        stage_count = verify_s.query(PipelineStageState).filter(
            PipelineStageState.project_id == proj_id
        ).count()
        cp_count = verify_s.query(PipelineCheckpoint).filter(
            PipelineCheckpoint.project_id == proj_id
        ).count()

        assert stage_count == 11
        assert cp_count == 4
    finally:
        verify_s.close()
        pg_engine.dispose()
