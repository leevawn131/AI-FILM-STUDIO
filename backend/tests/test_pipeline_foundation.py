"""
Pipeline Foundation Tests for AI Film Studio (S4 Milestone 1 Foundation).

Validates:
1. Migration upgrade and schema inspection for 0008 and 0009.
2. Migration 0008 -> 0009 upgrade and data preservation (CASCADE -> RESTRICT conversion).
3. Migration 0009 downgrade safety (converts RESTRICT back to CASCADE).
4. Fresh database migration to latest head.
5. SQLite production-engine foreign-key enforcement (enabled via backend.app.database connect listener).
6. ORM hard deletion and direct SQL hard deletion blocked by RESTRICT foreign keys.
7. Soft deletion of project preserves pipeline records and audit journals.
8. Migration downgrade safety with exact deep record-level archival validation.
9. SQLite and PostgreSQL DB-level audit immutability triggers (UPDATE and DELETE blocked).
10. Stage initialization: exactly 11 production stages in precise architectural order with initial status NOT_STARTED.
11. Checkpoint initialization: exactly 4 checkpoints in LOCKED status with artifact type and version 1.
12. Idempotent initialization: multiple sequential calls do not create duplicates.
13. Concurrent initialization attempts: multi-threaded concurrent calls without race-condition corruption across SQLite and PostgreSQL.
14. Legacy project self-healing: handles 11 stages with missing checkpoints and 4 checkpoints with missing stages without overwriting existing states.
15. Exact pipeline completeness: rejects unexpected stages/checkpoints or missing stages/checkpoints.
16. Project creation integration: newly created projects atomically initialize all stages/checkpoints.
17. Atomic project creation rollback: failure during initialization rolls back the project.
18. Atomic demo project seeding rollback: intermediate flushes instead of commits; failure during pipeline initialization rolls back all demo entities.
19. Transactionally fenced stage completion and failure: StudioJob and PipelineStageState updated atomically within one transaction.
20. Artifact hash scope control: screenplay supported; visual_bible, storyboard, assembly_cut raise NotImplementedError.
21. PostgreSQL dual-engine compatibility for foundation, migration 0009, concurrency, and worker fencing.
"""

import os
import uuid
import json
import sqlite3
import threading
import pytest
import concurrent.futures
from datetime import datetime, timezone, timedelta
from pathlib import Path
from sqlalchemy import create_engine, text, inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker, Session
from fastapi.testclient import TestClient
from alembic.config import Config
from alembic import command

from backend.app.main import app, seed_initial_demo_if_empty
from backend.app.config import settings
from backend.app.database import get_db, run_migrations
from backend.app.models.entities import (
    Base, Project, Scene, Character, Location, Shot,
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
    """
    Creates a dedicated SQLite database with all migrations applied to head for test isolation.
    Relies on production engine connect listener in backend.app.database for PRAGMA foreign_keys=ON.
    """
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
# 1. DATABASE MIGRATION & SCHEMA TESTS (0008, 0009, UPGRADE & DOWNGRADE)
# ---------------------------------------------------------------------------

def test_migration_0008_upgrade_and_schema_inspection(tmp_path):
    """
    Verifies Alembic migration 0008 creates all required tables, columns,
    and constraints matching its originally published contents.
    """
    test_db_path = tmp_path / "schema_0008_test.db"
    db_url = f"sqlite:///{test_db_path}"

    test_engine = create_engine(db_url)
    alembic_ini_path = settings.root_dir / "alembic.ini"
    alembic_cfg = Config(str(alembic_ini_path))
    alembic_cfg.set_main_option("script_location", str(settings.root_dir / "backend" / "alembic"))
    alembic_cfg.set_main_option("sqlalchemy.url", db_url)
    alembic_cfg.attributes["configure_logger"] = False

    with test_engine.connect() as conn:
        alembic_cfg.attributes["connection"] = conn
        command.upgrade(alembic_cfg, "0008_pipeline_orchestration_tables")

    inspector = inspect(test_engine)
    tables = set(inspector.get_table_names())

    assert "pipeline_stage_states" in tables
    assert "pipeline_checkpoints" in tables
    assert "pipeline_checkpoint_audit" in tables

    proj_columns = {col["name"]: col for col in inspector.get_columns("projects")}
    assert "deleted_at" in proj_columns
    assert "current_pipeline_stage" in proj_columns
    assert "pipeline_overall_status" in proj_columns

    test_engine.dispose()


def test_migration_0008_to_0009_upgrade_and_data_preservation(tmp_path):
    """
    Verifies BLOCKER 1:
    - Upgrade an existing database migrated to original 0008 schema up to 0009.
    - Foreign keys are converted from CASCADE to RESTRICT.
    - Existing pipeline stage, checkpoint, and audit data are preserved without loss.
    - Safe downgrade converts RESTRICT back to CASCADE.
    """
    test_db_path = tmp_path / "migrate_0008_to_0009.db"
    db_url = f"sqlite:///{test_db_path}"
    test_engine = create_engine(db_url, connect_args={"check_same_thread": False})

    alembic_ini_path = settings.root_dir / "alembic.ini"
    alembic_cfg = Config(str(alembic_ini_path))
    alembic_cfg.set_main_option("script_location", str(settings.root_dir / "backend" / "alembic"))
    alembic_cfg.set_main_option("sqlalchemy.url", db_url)
    alembic_cfg.attributes["configure_logger"] = False

    # 1. Upgrade to original 0008
    with test_engine.connect() as conn:
        alembic_cfg.attributes["connection"] = conn
        command.upgrade(alembic_cfg, "0008_pipeline_orchestration_tables")
        conn.commit()

    # 2. Insert existing film and pipeline data under 0008
    with test_engine.connect() as conn:
        conn.execute(text("""
            INSERT INTO projects (id, name, budget_total, budget_spent)
            VALUES ('proj-compat-1', 'Legacy 0008 Film', 100.0, 0.0);
        """))
        conn.execute(text("""
            INSERT INTO pipeline_stage_states (id, project_id, stage_name, status, stage_metadata)
            VALUES ('stage-compat-1', 'proj-compat-1', 'screenplay', 'IN_PROGRESS', '{\"legacy_note\": \"preserved\"}');
        """))
        conn.execute(text("""
            INSERT INTO pipeline_checkpoints (id, project_id, checkpoint_type, status, artifact_type, feedback_notes)
            VALUES ('cp-compat-1', 'proj-compat-1', 'story_approval', 'APPROVED', 'screenplay', 'Looks great');
        """))
        conn.execute(text("""
            INSERT INTO pipeline_checkpoint_audit (id, project_id, project_name_snapshot, checkpoint_id, checkpoint_type, decision, artifact_content_hash, artifact_version, reviewer_name)
            VALUES ('audit-compat-1', 'proj-compat-1', 'Legacy 0008 Film', 'cp-compat-1', 'story_approval', 'APPROVED', 'hash-comp', 1, 'Director');
        """))
        conn.commit()

    # 3. Upgrade to 0009_pipeline_fk_restrict (HEAD)
    with test_engine.connect() as conn:
        alembic_cfg.attributes["connection"] = conn
        command.upgrade(alembic_cfg, "0009_pipeline_fk_restrict")
        conn.commit()

    # 4. Verify all records survived with zero data loss
    with test_engine.connect() as conn:
        stage_row = conn.execute(text("SELECT id, status, stage_metadata FROM pipeline_stage_states WHERE id = 'stage-compat-1'")).fetchone()
        assert stage_row is not None
        assert stage_row[1] == "IN_PROGRESS"
        assert "preserved" in stage_row[2]

        cp_row = conn.execute(text("SELECT id, status, feedback_notes FROM pipeline_checkpoints WHERE id = 'cp-compat-1'")).fetchone()
        assert cp_row is not None
        assert cp_row[1] == "APPROVED"
        assert cp_row[2] == "Looks great"

        audit_row = conn.execute(text("SELECT id, decision FROM pipeline_checkpoint_audit WHERE id = 'audit-compat-1'")).fetchone()
        assert audit_row is not None
        assert audit_row[1] == "APPROVED"

        # 5. Verify RESTRICT enforcement on hard delete
        with pytest.raises(IntegrityError):
            conn.execute(text("DELETE FROM projects WHERE id = 'proj-compat-1'"))
            conn.commit()
        conn.rollback()

    # 6. Test downgrade back to 0008
    with test_engine.connect() as conn:
        alembic_cfg.attributes["connection"] = conn
        command.downgrade(alembic_cfg, "0008_pipeline_orchestration_tables")
        conn.commit()

        # Data still exists after downgrade
        count = conn.execute(text("SELECT count(*) FROM pipeline_stage_states WHERE id = 'stage-compat-1'")).scalar()
        assert count == 1

    test_engine.dispose()


def test_fresh_database_migration_to_latest_head(tmp_path):
    """
    Verifies that a fresh database successfully upgrades from initial baseline to latest head (0009).
    """
    test_db_path = tmp_path / "fresh_head.db"
    db_url = f"sqlite:///{test_db_path}"
    test_engine = create_engine(db_url)

    run_migrations(target_engine=test_engine, db_url=db_url)

    inspector = inspect(test_engine)
    tables = set(inspector.get_table_names())
    assert "pipeline_stage_states" in tables
    assert "pipeline_checkpoints" in tables
    assert "pipeline_checkpoint_audit" in tables

    test_engine.dispose()


def test_sqlite_production_engine_foreign_key_enforcement(tmp_path):
    """
    Verifies BLOCKER 2:
    - Production engine configuration automatically enables foreign key enforcement on SQLite.
    - No test-specific pragma listeners are needed.
    - Hard delete of project with pipeline records is rejected.
    - Direct SQL hard delete is rejected.
    - Soft delete succeeds.
    - Pipeline records and audit history remain intact.
    """
    test_db_path = tmp_path / "prod_fk_test.db"
    db_url = f"sqlite:///{test_db_path}"
    # Create engine using standard production pattern (backend.app.database listener handles foreign_keys)
    engine = create_engine(db_url, connect_args={"check_same_thread": False})
    run_migrations(target_engine=engine, db_url=db_url)

    SessionTest = sessionmaker(bind=engine)
    session = SessionTest()

    # 1. Verify PRAGMA foreign_keys is ON
    fk_status = session.execute(text("PRAGMA foreign_keys;")).scalar()
    assert fk_status == 1, "PRAGMA foreign_keys must be 1 on production SQLite connections"

    # 2. Create project and initialize pipeline
    proj = Project(name="Production FK Test Film", budget_total=100.0)
    session.add(proj)
    session.commit()
    pid = proj.id

    initialize_project_pipeline(pid, session)

    audit = PipelineCheckpointAudit(
        project_id=pid,
        project_name_snapshot=proj.name,
        checkpoint_id="cp-test",
        checkpoint_type="story_approval",
        decision="APPROVED",
        artifact_content_hash="audit_hash",
        artifact_version=1,
        reviewer_name="Director"
    )
    session.add(audit)
    session.commit()

    # 3. ORM hard delete rejected
    with pytest.raises(IntegrityError):
        session.delete(proj)
        session.commit()
    session.rollback()

    # 4. Direct SQL hard delete rejected
    with pytest.raises(IntegrityError):
        session.execute(text(f"DELETE FROM projects WHERE id = '{pid}'"))
        session.commit()
    session.rollback()

    # 5. Soft delete succeeds
    p = session.query(Project).filter(Project.id == pid).first()
    p.deleted_at = utc_now()
    session.commit()

    # 6. Verify pipeline records and audit trail intact
    assert session.query(PipelineStageState).filter(PipelineStageState.project_id == pid).count() == 11
    assert session.query(PipelineCheckpoint).filter(PipelineCheckpoint.project_id == pid).count() == 4
    assert session.query(PipelineCheckpointAudit).filter(PipelineCheckpointAudit.project_id == pid).count() == 1

    session.close()
    engine.dispose()


def test_migration_0008_downgrade_safety_and_record_verification(tmp_path):
    """
    Verifies published 0008 downgrade behavior:
    - Non-empty audit table blocks downgrade without FORCE_AUDIT_PURGE=1.
    - Downgrade with FORCE_AUDIT_PURGE=1 drops tables.
    """
    test_db_path = tmp_path / "downgrade_verify_test.db"
    db_url = f"sqlite:///{test_db_path}"

    test_engine = create_engine(db_url)
    run_migrations(target_engine=test_engine, db_url=db_url)

    alembic_ini_path = settings.root_dir / "alembic.ini"
    alembic_cfg = Config(str(alembic_ini_path))
    alembic_cfg.set_main_option("script_location", str(settings.root_dir / "backend" / "alembic"))
    alembic_cfg.set_main_option("sqlalchemy.url", db_url)
    alembic_cfg.attributes["configure_logger"] = False

    # Insert audit record
    with test_engine.connect() as conn:
        conn.execute(text("""
            INSERT INTO pipeline_checkpoint_audit (
                id, project_id, project_name_snapshot, checkpoint_id,
                checkpoint_type, decision, artifact_content_hash,
                artifact_version, reviewer_name, created_at
            ) VALUES (
                'audit-verify-1', 'proj-1', 'Verification Film', 'cp-1',
                'story_approval', 'APPROVED', 'abc123hash',
                1, 'Director', datetime('now')
            )
        """))
        conn.commit()

    # 1. Downgrade from 0009 to 0008 succeeds (only alters FK)
    with test_engine.connect() as conn:
        alembic_cfg.attributes["connection"] = conn
        command.downgrade(alembic_cfg, "0008_pipeline_orchestration_tables")
        conn.commit()

    # 2. Attempt downgrade to 0007 without FORCE_AUDIT_PURGE=1: must fail
    with pytest.raises(RuntimeError) as exc_info:
        with test_engine.connect() as conn:
            alembic_cfg.attributes["connection"] = conn
            command.downgrade(alembic_cfg, "0007_upload_operation_lease_fencing")

    assert "contains 1 audit record" in str(exc_info.value)
    assert "FORCE_AUDIT_PURGE=1" in str(exc_info.value)

    # 3. Downgrade with FORCE_AUDIT_PURGE=1 succeeds
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
# 1B. ADMINISTRATIVE AUDIT ARCHIVAL & FULL-RECORD VERIFICATION
# ---------------------------------------------------------------------------

def test_administrative_audit_archival_and_roundtrip_verification(tmp_path):
    """
    Verifies administrative audit archival utility:
    - Exports all records with consistent timestamp serialization.
    - Prevents silent overwrite of an existing archive.
    - Verifies all 11 columns in round-trip verification.
    """
    from backend.app.services.audit_archiver import (
        archive_audit_records,
        verify_audit_archive,
        AuditArchiveExistsError,
        AUDIT_COLUMNS,
    )

    test_db_path = tmp_path / "audit_archival_test.db"
    db_url = f"sqlite:///{test_db_path}"
    engine = create_engine(db_url)
    run_migrations(target_engine=engine, db_url=db_url)

    now_iso = datetime(2026, 11, 2, 8, 30, 0, tzinfo=timezone.utc).isoformat()
    with engine.connect() as conn:
        conn.execute(text("""
            INSERT INTO pipeline_checkpoint_audit (
                id, project_id, project_name_snapshot, checkpoint_id,
                checkpoint_type, decision, artifact_content_hash,
                artifact_version, reviewer_name, feedback_notes, created_at
            ) VALUES (
                'audit-rec-1', 'proj-10', 'Archival Film', 'cp-10',
                'story_approval', 'APPROVED', 'hash-rec-1',
                2, 'Lead Director', 'Excellent work', :ts
            ), (
                'audit-rec-2', 'proj-10', 'Archival Film', 'cp-11',
                'visual_approval', 'REVISE', 'hash-rec-2',
                1, 'Art Director', 'Adjust colors', :ts
            )
        """), {"ts": now_iso})
        conn.commit()

    archive_file = tmp_path / "archives" / "audit_backup.json"

    # 1. Export records
    result = archive_audit_records(engine, archive_file)
    assert result["status"] == "success"
    assert result["records_exported"] == 2
    assert archive_file.exists()

    with open(archive_file, "r", encoding="utf-8") as f:
        archived_json = json.load(f)
    assert len(archived_json) == 2
    assert archived_json[0]["id"] == "audit-rec-1"
    assert archived_json[0]["feedback_notes"] == "Excellent work"
    assert archived_json[1]["id"] == "audit-rec-2"
    assert archived_json[1]["decision"] == "REVISE"

    # 2. Refuses silent overwrite
    with pytest.raises(AuditArchiveExistsError) as exc_exists:
        archive_audit_records(engine, archive_file, overwrite=False)
    assert "already exists" in str(exc_exists.value)

    # 3. Explicit overwrite succeeds
    overwrite_res = archive_audit_records(engine, archive_file, overwrite=True)
    assert overwrite_res["status"] == "success"

    # 4. Direct verification succeeds
    verify_res = verify_audit_archive(engine, archive_file)
    assert verify_res["status"] == "verified"
    assert verify_res["verified_record_count"] == 2
    assert set(verify_res["verified_columns"]) == set(AUDIT_COLUMNS)
    engine.dispose()


def test_audit_verification_strict_json_types(tmp_path):
    """
    Verifies strict JSON value typing enforcement in verify_audit_archive():
    - artifact_version 1 changed to 1.0 (float) is rejected.
    - artifact_version 1 changed to "1" (string) is rejected.
    - artifact_version 1 changed to True (bool) is rejected.
    - string values changed to numbers (int/float) are rejected.
    - invalid timestamp string is rejected.
    - null changed to empty string "" is rejected.
    """
    from backend.app.services.audit_archiver import (
        archive_audit_records,
        verify_audit_archive,
        AuditVerificationError,
    )

    test_db_path = tmp_path / "strict_types_test.db"
    db_url = f"sqlite:///{test_db_path}"
    engine = create_engine(db_url)
    run_migrations(target_engine=engine, db_url=db_url)

    now_iso = datetime(2026, 11, 2, 9, 30, 0, tzinfo=timezone.utc).isoformat()
    with engine.connect() as conn:
        conn.execute(text("""
            INSERT INTO pipeline_checkpoint_audit (
                id, project_id, project_name_snapshot, checkpoint_id,
                checkpoint_type, decision, artifact_content_hash,
                artifact_version, reviewer_name, feedback_notes, created_at
            ) VALUES (
                'audit-type-1', 'proj-type', 'Types Film', 'cp-type-1',
                'story_approval', 'APPROVED', 'validhash123',
                1, 'Director', NULL, :ts
            )
        """), {"ts": now_iso})
        conn.commit()

    archive_file = tmp_path / "strict_base.json"
    archive_audit_records(engine, archive_file)

    with open(archive_file, "r", encoding="utf-8") as f:
        base_records = json.load(f)

    # 1. artifact_version 1 -> 1.0 (float)
    f_float = tmp_path / "tamper_float.json"
    r_float = [dict(base_records[0])]
    r_float[0]["artifact_version"] = 1.0
    with open(f_float, "w", encoding="utf-8") as f:
        json.dump(r_float, f)
    with pytest.raises(AuditVerificationError) as exc_float:
        verify_audit_archive(engine, f_float)
    assert "expected JSON integer, got float" in str(exc_float.value)

    # 2. artifact_version 1 -> "1" (str)
    f_str = tmp_path / "tamper_version_str.json"
    r_str = [dict(base_records[0])]
    r_str[0]["artifact_version"] = "1"
    with open(f_str, "w", encoding="utf-8") as f:
        json.dump(r_str, f)
    with pytest.raises(AuditVerificationError) as exc_str:
        verify_audit_archive(engine, f_str)
    assert "expected JSON integer, got str" in str(exc_str.value)

    # 3. artifact_version 1 -> True (bool)
    f_bool = tmp_path / "tamper_version_bool.json"
    r_bool = [dict(base_records[0])]
    r_bool[0]["artifact_version"] = True
    with open(f_bool, "w", encoding="utf-8") as f:
        json.dump(r_bool, f)
    with pytest.raises(AuditVerificationError) as exc_bool:
        verify_audit_archive(engine, f_bool)
    assert "expected JSON integer, got bool" in str(exc_bool.value)

    # 4. string values changed to numbers (e.g. artifact_content_hash = 12345)
    f_num = tmp_path / "tamper_str_num.json"
    r_num = [dict(base_records[0])]
    r_num[0]["artifact_content_hash"] = 12345
    with open(f_num, "w", encoding="utf-8") as f:
        json.dump(r_num, f)
    with pytest.raises(AuditVerificationError) as exc_num:
        verify_audit_archive(engine, f_num)
    assert "expected JSON string, got int" in str(exc_num.value)

    # 5. invalid timestamp
    f_ts = tmp_path / "tamper_invalid_ts.json"
    r_ts = [dict(base_records[0])]
    r_ts[0]["created_at"] = "invalid-not-a-timestamp"
    with open(f_ts, "w", encoding="utf-8") as f:
        json.dump(r_ts, f)
    with pytest.raises(AuditVerificationError) as exc_ts:
        verify_audit_archive(engine, f_ts)
    assert "not a valid ISO 8601 timestamp" in str(exc_ts.value)

    # 6. null changed to empty string ""
    f_null = tmp_path / "tamper_null_to_empty.json"
    r_null = [dict(base_records[0])]
    r_null[0]["feedback_notes"] = ""
    with open(f_null, "w", encoding="utf-8") as f:
        json.dump(r_null, f)
    with pytest.raises(AuditVerificationError) as exc_null:
        verify_audit_archive(engine, f_null)
    assert "source is None, but archive is ''" in str(exc_null.value)

    engine.dispose()


def test_safe_archive_replacement_concurrency_and_failure_resilience(tmp_path):
    """
    Verifies Requirement 4:
    - Avoids concurrent writers using the same temporary filename (uses unique uuid suffix).
    - Ensure overwrite=False cannot overwrite an archive created concurrently.
    - When overwrite=True, preserves previous valid archive until new archive passes verification.
    - Never deletes a previous valid archive because a new export failed.
    """
    from backend.app.services.audit_archiver import (
        archive_audit_records,
        AuditArchiveExistsError,
        AuditVerificationError,
    )

    test_db_path = tmp_path / "replacement_test.db"
    db_url = f"sqlite:///{test_db_path}"
    engine = create_engine(db_url)
    run_migrations(target_engine=engine, db_url=db_url)

    now_iso = datetime(2026, 11, 2, 9, 35, 0, tzinfo=timezone.utc).isoformat()
    with engine.connect() as conn:
        conn.execute(text("""
            INSERT INTO pipeline_checkpoint_audit (
                id, project_id, project_name_snapshot, checkpoint_id,
                checkpoint_type, decision, artifact_content_hash,
                artifact_version, reviewer_name, created_at
            ) VALUES (
                'rec-orig-1', 'proj-r', 'Orig Film', 'cp-r1',
                'story_approval', 'APPROVED', 'hashorig',
                1, 'Director', :ts
            )
        """), {"ts": now_iso})
        conn.commit()

    archive_file = tmp_path / "production_archive.json"

    # 1. First valid export
    archive_audit_records(engine, archive_file, overwrite=False)
    original_mtime = archive_file.stat().st_mtime
    original_content = archive_file.read_text(encoding="utf-8")

    # 2. Overwrite=False strictly blocks overwrite
    with pytest.raises(AuditArchiveExistsError):
        archive_audit_records(engine, archive_file, overwrite=False)
    assert archive_file.read_text(encoding="utf-8") == original_content

    # 3. Simulate failure during overwrite=True export (e.g. corrupting temp verification)
    # Monkey-patch verify_audit_archive to simulate verification rejection of the new data
    import backend.app.services.audit_archiver as archiver_module
    real_verify = archiver_module.verify_audit_archive

    def failing_verify(source_records_or_target, archive_path):
        raise AuditVerificationError("Simulated verification failure for new export")

    archiver_module.verify_audit_archive = failing_verify
    try:
        with pytest.raises(AuditVerificationError):
            archive_audit_records(engine, archive_file, overwrite=True)

        # Previous valid archive MUST STILL EXIST and remain completely untouched!
        assert archive_file.exists()
        assert archive_file.read_text(encoding="utf-8") == original_content
    finally:
        archiver_module.verify_audit_archive = real_verify

    # 4. Legitimate overwrite=True succeeds and atomically replaces
    archive_audit_records(engine, archive_file, overwrite=True)
    assert archive_file.exists()

    engine.dispose()


def test_safe_administrative_purge_target_types_and_unrelated_session_isolation(tmp_path):
    """
    Verifies Requirements 1 & 2:
    - Explicit transaction ownership for Engine, Connection, and Session targets.
    - Test 1: Session purge followed by rollback (audit records remain).
    - Test 2: Session purge followed by commit (audit records purged).
    - Test 3: Connection purge with caller transaction followed by rollback (audit records remain).
    - Test 4: Engine purge (durable purge committed explicitly).
    - Test 5: Session containing unrelated pending work (unrelated work is not committed prematurely,
              and commits/rolls back along with session).
    """
    from backend.app.services.audit_archiver import (
        archive_audit_records,
        safe_administrative_purge,
        AuditArchivalError,
    )

    test_db_path = tmp_path / "targets_test.db"
    db_url = f"sqlite:///{test_db_path}"
    engine = create_engine(db_url)
    run_migrations(target_engine=engine, db_url=db_url)

    SessionLocalTest = sessionmaker(bind=engine)

    # -----------------------------------------------------------------------
    # 1. Engine Purge: Dedicated transaction, durable commit
    # -----------------------------------------------------------------------
    with engine.connect() as conn:
        conn.execute(text("""
            INSERT INTO pipeline_checkpoint_audit (
                id, project_id, project_name_snapshot, checkpoint_id,
                checkpoint_type, decision, artifact_content_hash,
                artifact_version, reviewer_name, created_at
            ) VALUES (
                'rec-eng-1', 'proj-1', 'Eng Film', 'cp-1',
                'story_approval', 'APPROVED', 'hasheng',
                1, 'Director', datetime('now')
            )
        """))
        conn.commit()

    archive_eng = tmp_path / "archive_eng.json"
    archive_audit_records(engine, archive_eng)
    res_eng = safe_administrative_purge(engine, archive_eng, force=True)
    assert res_eng["status"] == "purged"
    assert res_eng["is_durable"] is True
    assert res_eng["purged_record_count"] == 1

    with engine.connect() as check_conn:
        cnt = check_conn.execute(text("SELECT count(*) FROM pipeline_checkpoint_audit")).scalar()
        assert cnt == 0, "Engine purge must be immediately durable in database"

    # -----------------------------------------------------------------------
    # 2. Session Purge followed by Rollback
    # -----------------------------------------------------------------------
    with engine.connect() as conn:
        conn.execute(text("""
            INSERT INTO pipeline_checkpoint_audit (
                id, project_id, project_name_snapshot, checkpoint_id,
                checkpoint_type, decision, artifact_content_hash,
                artifact_version, reviewer_name, created_at
            ) VALUES (
                'rec-sess-rb-1', 'proj-1', 'Sess RB Film', 'cp-1',
                'story_approval', 'APPROVED', 'hashsessrb',
                1, 'Director', datetime('now')
            )
        """))
        conn.commit()

    archive_sess_rb = tmp_path / "archive_sess_rb.json"
    archive_audit_records(engine, archive_sess_rb)

    session_rb = SessionLocalTest()
    res_sess_rb = safe_administrative_purge(session_rb, archive_sess_rb, force=True)
    assert res_sess_rb["status"] == "pending_commit"
    assert res_sess_rb["is_durable"] is False

    # Caller explicitly rolls back session
    session_rb.rollback()
    session_rb.close()

    with engine.connect() as check_conn:
        cnt = check_conn.execute(text("SELECT count(*) FROM pipeline_checkpoint_audit")).scalar()
        assert cnt == 1, "Session purge followed by rollback must restore the audit records in DB"

    # -----------------------------------------------------------------------
    # 3. Session Purge followed by Commit
    # -----------------------------------------------------------------------
    session_commit = SessionLocalTest()
    res_sess_commit = safe_administrative_purge(session_commit, archive_sess_rb, force=True)
    assert res_sess_commit["status"] == "pending_commit"
    assert res_sess_commit["is_durable"] is False

    # Caller explicitly commits session
    session_commit.commit()
    session_commit.close()

    with engine.connect() as check_conn:
        cnt = check_conn.execute(text("SELECT count(*) FROM pipeline_checkpoint_audit")).scalar()
        assert cnt == 0, "Session purge followed by commit must permanently purge records from DB"

    # -----------------------------------------------------------------------
    # 4. Connection Purge with Caller Transaction followed by Rollback
    # -----------------------------------------------------------------------
    with engine.connect() as conn:
        conn.execute(text("""
            INSERT INTO pipeline_checkpoint_audit (
                id, project_id, project_name_snapshot, checkpoint_id,
                checkpoint_type, decision, artifact_content_hash,
                artifact_version, reviewer_name, created_at
            ) VALUES (
                'rec-conn-rb-1', 'proj-1', 'Conn RB Film', 'cp-1',
                'story_approval', 'APPROVED', 'hashconnrb',
                1, 'Director', datetime('now')
            )
        """))
        conn.commit()

    archive_conn_rb = tmp_path / "archive_conn_rb.json"
    archive_audit_records(engine, archive_conn_rb)

    conn_caller = engine.connect()
    # Caller starts connection transaction without executing DML before purge:
    tx_caller = conn_caller.begin()
    res_conn_rb = safe_administrative_purge(conn_caller, archive_conn_rb, force=True)
    assert res_conn_rb["status"] == "pending_commit"
    assert res_conn_rb["is_durable"] is False

    # Caller rolls back
    tx_caller.rollback()
    conn_caller.close()

    with engine.connect() as check_conn:
        cnt = check_conn.execute(text("SELECT count(*) FROM pipeline_checkpoint_audit")).scalar()
        assert cnt == 1, "Connection purge followed by rollback must preserve audit records"

    # Also verify: if caller has an active DBAPI transaction in progress (e.g. DML),
    # safe_administrative_purge rejects with AuditArchivalError:
    conn_active = engine.connect()
    conn_active.execute(text("""
        INSERT INTO pipeline_checkpoint_audit (
            id, project_id, project_name_snapshot, checkpoint_id,
            checkpoint_type, decision, artifact_content_hash,
            artifact_version, reviewer_name, created_at
        ) VALUES (
            'rec-conn-active', 'proj-1', 'Active Film', 'cp-1',
            'story_approval', 'APPROVED', 'hashactive',
            1, 'Reviewer', datetime('now')
        )
    """))
    with pytest.raises(AuditArchivalError, match="Cannot safely acquire SQLite write lock"):
        safe_administrative_purge(conn_active, archive_conn_rb, force=True)
    conn_active.rollback()
    conn_active.close()

    # Clean up record using Engine purge
    archive_clean = tmp_path / "archive_clean.json"
    archive_audit_records(engine, archive_clean)
    safe_administrative_purge(engine, archive_clean, force=True)

    # -----------------------------------------------------------------------
    # 5. Session Containing Unrelated Pending Work
    # -----------------------------------------------------------------------
    with engine.connect() as conn:
        conn.execute(text("""
            INSERT INTO pipeline_checkpoint_audit (
                id, project_id, project_name_snapshot, checkpoint_id,
                checkpoint_type, decision, artifact_content_hash,
                artifact_version, reviewer_name, created_at
            ) VALUES (
                'rec-unrelated-1', 'proj-1', 'Unrelated Film', 'cp-1',
                'story_approval', 'APPROVED', 'hashunrelated',
                1, 'Director', datetime('now')
            )
        """))
        conn.commit()

    archive_unrelated = tmp_path / "archive_unrelated.json"
    archive_audit_records(engine, archive_unrelated)

    session_work = SessionLocalTest()
    unrelated_proj = Project(
        id="proj-unrelated-pending-work",
        name="Unrelated Pending Film",
        budget_total=1000.0,
        budget_spent=0.0
    )
    session_work.add(unrelated_proj)

    res_unrelated = safe_administrative_purge(session_work, archive_unrelated, force=True)
    assert res_unrelated["status"] == "pending_commit"
    assert res_unrelated["is_durable"] is False

    # Unrelated project must NOT be visible to other connections before session.commit()
    with engine.connect() as check_conn:
        db_proj = check_conn.execute(text("SELECT id FROM projects WHERE id = 'proj-unrelated-pending-work'")).fetchone()
        assert db_proj is None, "safe_administrative_purge must NOT commit unrelated caller objects!"

    # Commit session: both unrelated project and audit purge commit together
    session_work.commit()
    session_work.close()

    with engine.connect() as check_conn:
        db_proj_after = check_conn.execute(text("SELECT id FROM projects WHERE id = 'proj-unrelated-pending-work'")).fetchone()
        assert db_proj_after is not None, "Unrelated project must be committed upon session.commit()"
        cnt = check_conn.execute(text("SELECT count(*) FROM pipeline_checkpoint_audit")).scalar()
        assert cnt == 0, "Audit records must be purged upon session.commit()"

    engine.dispose()


def test_purge_concurrency_regression_blocks_unarchived_deletions(tmp_path):
    """
    Verifies Requirements 1 & 3 (Genuinely Deterministic Two-Connection Concurrency):
    1. Worker A starts safe_administrative_purge().
    2. A acquires BEGIN IMMEDIATE write lock.
    3. A signals lock_acquired and waits for worker B to prove lock contention.
    4. Worker B opens a separate connection.
    5. B explicitly attempts BEGIN IMMEDIATE while A still owns the write lock.
    6. Configures a bounded SQLite busy_timeout (100ms).
    7. B must receive the expected database-is-locked result while A still holds its lock.
    8. B signals lock_contention_proven.
    9. A continues, verifies the archive, purges the archived records, and commits.
    10. B retries BEGIN IMMEDIATE after A completes, then inserts the new record successfully.
    11. Assert that archived records were removed and the concurrent unarchived record survived.
    - All waits and joins use bounded timeouts to prevent hanging indefinitely.
    - Exceptions are captured from both workers and re-raised if unexpected.
    - Connections and transactions are cleaned up in finally blocks.
    """
    from backend.app.services.audit_archiver import (
        archive_audit_records,
        safe_administrative_purge,
    )

    test_db_path = tmp_path / "purge_concurrency_race.db"
    db_url = f"sqlite:///{test_db_path}"
    engine = create_engine(db_url)
    run_migrations(target_engine=engine, db_url=db_url)

    # 1. Insert initial record 1
    with engine.connect() as conn:
        conn.execute(text("""
            INSERT INTO pipeline_checkpoint_audit (
                id, project_id, project_name_snapshot, checkpoint_id,
                checkpoint_type, decision, artifact_content_hash,
                artifact_version, reviewer_name, created_at
            ) VALUES (
                'rec-race-1', 'proj-1', 'Race Film', 'cp-1',
                'story_approval', 'APPROVED', 'hashrace1',
                1, 'Director', datetime('now')
            )
        """))
        conn.commit()

    archive_file = tmp_path / "race_archive.json"
    archive_audit_records(engine, archive_file)

    # Synchronization events
    lock_acquired = threading.Event()
    lock_contention_proven = threading.Event()
    worker_a_result = {}
    worker_b_result = {}

    def purge_worker_a():
        try:
            def on_lock():
                lock_acquired.set()
                # Bounded wait until worker B has proven lock contention on the database
                contention_ok = lock_contention_proven.wait(timeout=5.0)
                if not contention_ok:
                    raise TimeoutError("Worker A timed out waiting for Worker B to prove lock contention")

            safe_administrative_purge(
                engine,
                archive_file,
                force=True,
                _on_lock_acquired=on_lock
            )
            worker_a_result["success"] = True
        except Exception as e:
            worker_a_result["error"] = e

    def insert_worker_b():
        try:
            # 1. Bounded wait until Worker A has acquired BEGIN IMMEDIATE
            acquired_ok = lock_acquired.wait(timeout=5.0)
            if not acquired_ok:
                raise TimeoutError("Worker B timed out waiting for Worker A to acquire write lock")

            # 2. Worker B connects and verifies lock contention with bounded busy_timeout
            with engine.connect() as conn_b:
                conn_b.exec_driver_sql("PRAGMA busy_timeout = 100;")
                try:
                    conn_b.exec_driver_sql("BEGIN IMMEDIATE")
                    worker_b_result["locked"] = False
                    raise AssertionError("Worker B unexpectedly acquired BEGIN IMMEDIATE while Worker A held the write lock!")
                except Exception as e:
                    # Verify database is locked
                    if "locked" in str(e).lower():
                        worker_b_result["locked"] = True
                    else:
                        raise e
                    try:
                        conn_b.rollback()
                    except Exception:
                        pass
                finally:
                    # Signal to Worker A that contention has been verified at the database level
                    lock_contention_proven.set()

        except Exception as e:
            worker_b_result["error"] = e
            # Ensure Worker A does not hang if Worker B encountered an error
            lock_contention_proven.set()

    t_a = threading.Thread(target=purge_worker_a, name="worker_a")
    t_b = threading.Thread(target=insert_worker_b, name="worker_b")

    t_a.start()
    t_b.start()

    # Bounded joins with explicit timeout
    t_b.join(timeout=10.0)
    assert not t_b.is_alive(), "Worker B thread timed out and failed to terminate"

    t_a.join(timeout=10.0)
    assert not t_a.is_alive(), "Worker A thread timed out and failed to terminate"

    # Re-raise any captured worker exceptions
    if "error" in worker_a_result:
        raise worker_a_result["error"]
    if "error" in worker_b_result:
        raise worker_b_result["error"]

    assert worker_a_result.get("success") is True, f"Worker A failed: {worker_a_result}"
    assert worker_b_result.get("locked") is True, "Worker B must have proven database lock contention"

    # 3. Now Worker B retries BEGIN IMMEDIATE after Worker A completed and inserts record 2
    with engine.connect() as conn_retry:
        conn_retry.exec_driver_sql("BEGIN IMMEDIATE")
        conn_retry.execute(text("""
            INSERT INTO pipeline_checkpoint_audit (
                id, project_id, project_name_snapshot, checkpoint_id,
                checkpoint_type, decision, artifact_content_hash,
                artifact_version, reviewer_name, created_at
            ) VALUES (
                'rec-race-2-concurrent', 'proj-1', 'Race Film', 'cp-2',
                'visual_approval', 'APPROVED', 'hashrace2',
                1, 'Director', datetime('now')
            )
        """))
        conn_retry.commit()

    # 4. Assertions:
    with engine.connect() as check_conn:
        rows = check_conn.execute(text("SELECT id FROM pipeline_checkpoint_audit")).fetchall()
        row_ids = [r[0] for r in rows]
        # rec-race-1 was archived and MUST be purged
        assert "rec-race-1" not in row_ids, "Archived record 1 must be purged"
        # rec-race-2-concurrent was unarchived and MUST NOT be deleted
        assert "rec-race-2-concurrent" in row_ids, "Unarchived concurrent record 2 must NEVER be deleted!"
        assert len(row_ids) == 1

    engine.dispose()


def test_audit_verification_detects_every_field_modification(tmp_path):
    """
    Verifies that tampering with ANY of the 11 audit fields:
    id, project_id, project_name_snapshot, checkpoint_id, checkpoint_type,
    decision, artifact_content_hash, artifact_version, reviewer_name,
    feedback_notes, created_at
    is strictly detected by verify_audit_archive() and raises AuditVerificationError.
    """
    from backend.app.services.audit_archiver import (
        archive_audit_records,
        verify_audit_archive,
        AuditVerificationError,
        AUDIT_COLUMNS,
    )

    test_db_path = tmp_path / "tamper_detect_test.db"
    db_url = f"sqlite:///{test_db_path}"
    engine = create_engine(db_url)
    run_migrations(target_engine=engine, db_url=db_url)

    now_iso = datetime(2026, 11, 2, 9, 0, 0, tzinfo=timezone.utc).isoformat()
    with engine.connect() as conn:
        conn.execute(text("""
            INSERT INTO pipeline_checkpoint_audit (
                id, project_id, project_name_snapshot, checkpoint_id,
                checkpoint_type, decision, artifact_content_hash,
                artifact_version, reviewer_name, feedback_notes, created_at
            ) VALUES (
                'audit-base-1', 'proj-tamper', 'Tamper Film', 'cp-tamper',
                'story_approval', 'APPROVED', 'basehash123',
                1, 'Auditor User', 'Original feedback', :ts
            )
        """), {"ts": now_iso})
        conn.commit()

    archive_file = tmp_path / "tamper_audit.json"
    archive_audit_records(engine, archive_file)

    # Read base archive
    with open(archive_file, "r", encoding="utf-8") as f:
        original_records = json.load(f)

    # Modifications to test for every single column
    tamper_values = {
        "id": "audit-base-TAMPERED",
        "project_id": "proj-tampered-id",
        "project_name_snapshot": "Tampered Film Name",
        "checkpoint_id": "cp-tampered-id",
        "checkpoint_type": "production_approval",
        "decision": "REJECTED",
        "artifact_content_hash": "tamperedhash999",
        "artifact_version": 99,
        "reviewer_name": "Malicious Imposter",
        "feedback_notes": "Tampered notes",
        "created_at": "2026-12-31T23:59:59+00:00",
    }

    assert len(tamper_values) == len(AUDIT_COLUMNS) == 11

    for col in AUDIT_COLUMNS:
        tampered_archive_file = tmp_path / f"tampered_{col}.json"
        tampered_data = [dict(original_records[0])]
        tampered_data[0][col] = tamper_values[col]

        with open(tampered_archive_file, "w", encoding="utf-8") as f:
            json.dump(tampered_data, f, indent=2)

        with pytest.raises(AuditVerificationError) as exc_tamper:
            verify_audit_archive(engine, tampered_archive_file)

        error_msg = str(exc_tamper.value)
        # Either the column mismatch is reported or identity mismatch for 'id'
        if col == "id":
            assert "missing" in error_msg or "id" in error_msg
        else:
            assert col in error_msg, f"Expected column '{col}' to be cited in error: {error_msg}"

    engine.dispose()


def test_audit_verification_rejects_missing_extra_and_duplicate_records(tmp_path):
    """
    Verifies that verify_audit_archive():
    - Rejects duplicate record IDs in archive.
    - Rejects missing source records.
    - Rejects unexpected extra records.
    - Rejects record count mismatch.
    - Rejects unexpected extra columns.
    """
    from backend.app.services.audit_archiver import (
        archive_audit_records,
        verify_audit_archive,
        AuditVerificationError,
    )

    test_db_path = tmp_path / "records_mismatch_test.db"
    db_url = f"sqlite:///{test_db_path}"
    engine = create_engine(db_url)
    run_migrations(target_engine=engine, db_url=db_url)

    now_iso = datetime(2026, 11, 2, 9, 15, 0, tzinfo=timezone.utc).isoformat()
    with engine.connect() as conn:
        conn.execute(text("""
            INSERT INTO pipeline_checkpoint_audit (
                id, project_id, project_name_snapshot, checkpoint_id,
                checkpoint_type, decision, artifact_content_hash,
                artifact_version, reviewer_name, feedback_notes, created_at
            ) VALUES (
                'rec-1', 'proj-1', 'Film 1', 'cp-1', 'story_approval',
                'APPROVED', 'hash1', 1, 'Reviewer', 'notes 1', :ts
            ), (
                'rec-2', 'proj-1', 'Film 1', 'cp-2', 'visual_approval',
                'APPROVED', 'hash2', 1, 'Reviewer', 'notes 2', :ts
            )
        """), {"ts": now_iso})
        conn.commit()

    archive_file = tmp_path / "valid_archive.json"
    archive_audit_records(engine, archive_file)

    with open(archive_file, "r", encoding="utf-8") as f:
        valid_records = json.load(f)

    # 1. Missing a record (1 record instead of 2)
    missing_file = tmp_path / "missing.json"
    with open(missing_file, "w", encoding="utf-8") as f:
        json.dump([valid_records[0]], f)
    with pytest.raises(AuditVerificationError) as exc_missing:
        verify_audit_archive(engine, missing_file)
    assert "count mismatch" in str(exc_missing.value)

    # 2. Extra record
    extra_file = tmp_path / "extra.json"
    extra_rec = dict(valid_records[0])
    extra_rec["id"] = "rec-3-extra"
    with open(extra_file, "w", encoding="utf-8") as f:
        json.dump(valid_records + [extra_rec], f)
    with pytest.raises(AuditVerificationError) as exc_extra:
        verify_audit_archive(engine, extra_file)
    assert "count mismatch" in str(exc_extra.value)

    # 3. Duplicate ID (2 records, but same ID duplicated)
    dup_file = tmp_path / "dup.json"
    dup_rec = dict(valid_records[0])
    with open(dup_file, "w", encoding="utf-8") as f:
        json.dump([valid_records[0], dup_rec], f)
    with pytest.raises(AuditVerificationError) as exc_dup:
        verify_audit_archive(engine, dup_file)
    assert "Duplicate record id" in str(exc_dup.value)

    # 4. Unexpected extra column in record
    extra_col_file = tmp_path / "extra_col.json"
    rec_with_extra = dict(valid_records[0])
    rec_with_extra["unauthorized_injected_column"] = "malicious_data"
    with open(extra_col_file, "w", encoding="utf-8") as f:
        json.dump([rec_with_extra, valid_records[1]], f)
    with pytest.raises(AuditVerificationError) as exc_col:
        verify_audit_archive(engine, extra_col_file)
    assert "unexpected extra column" in str(exc_col.value)

    engine.dispose()


def test_destructive_purge_blocked_on_verification_failure(tmp_path):
    """
    Verifies that safe_administrative_purge() strictly refuses destructive purge
    when archive verification fails, preserving database audit history.
    """
    from backend.app.services.audit_archiver import (
        archive_audit_records,
        safe_administrative_purge,
        AuditVerificationError,
        AuditArchivalError,
    )

    test_db_path = tmp_path / "purge_safety_test.db"
    db_url = f"sqlite:///{test_db_path}"
    engine = create_engine(db_url)
    run_migrations(target_engine=engine, db_url=db_url)

    with engine.connect() as conn:
        conn.execute(text("""
            INSERT INTO pipeline_checkpoint_audit (
                id, project_id, project_name_snapshot, checkpoint_id,
                checkpoint_type, decision, artifact_content_hash,
                artifact_version, reviewer_name, feedback_notes, created_at
            ) VALUES (
                'rec-p1', 'proj-1', 'Film 1', 'cp-1', 'story_approval',
                'APPROVED', 'hash1', 1, 'Reviewer', 'safe notes', datetime('now')
            )
        """))
        conn.commit()

    archive_file = tmp_path / "valid_before_tamper.json"
    archive_audit_records(engine, archive_file)

    # Tamper with archive
    with open(archive_file, "w", encoding="utf-8") as f:
        json.dump([{"id": "rec-p1", "decision": "TAMPERED"}], f)

    # Attempt purge with tampered archive: must fail
    with pytest.raises(AuditVerificationError):
        safe_administrative_purge(engine, archive_file, force=True)

    # Verify database record still exists (not purged)
    with engine.connect() as conn:
        cnt = conn.execute(text("SELECT count(*) FROM pipeline_checkpoint_audit")).scalar()
        assert cnt == 1, "Audit record must not be purged when archive verification fails"

    # Attempt purge without force=True: must fail
    with pytest.raises(AuditArchivalError) as exc_force:
        safe_administrative_purge(engine, archive_file, force=False)
    assert "requires explicit force=True" in str(exc_force.value)

    # Re-export valid archive and verify safe purge succeeds
    archive_audit_records(engine, archive_file, overwrite=True)
    purge_res = safe_administrative_purge(engine, archive_file, force=True)
    assert purge_res["status"] == "purged"
    assert purge_res["purged_record_count"] == 1

    with engine.connect() as conn:
        cnt_after = conn.execute(text("SELECT count(*) FROM pipeline_checkpoint_audit")).scalar()
        assert cnt_after == 0

    engine.dispose()


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

        with pytest.raises(Exception) as exc_update:
            conn.execute(text("UPDATE pipeline_checkpoint_audit SET decision = 'TAMPERED' WHERE id = 'audit-immutable-1'"))
            conn.commit()
        assert "pipeline_checkpoint_audit is immutable and append-only" in str(exc_update.value)

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
    Directly compares ordered stages to PIPELINE_STAGES (Additional Fix B).
    """
    initialize_project_pipeline(clean_project.id, db_session)

    stages = db_session.query(PipelineStageState).filter(
        PipelineStageState.project_id == clean_project.id
    ).all()

    assert len(stages) == 11, f"Expected exactly 11 stages, found {len(stages)}"

    stage_order_map = {name: idx for idx, name in enumerate(PIPELINE_STAGES)}
    sorted_stages = sorted(stages, key=lambda s: stage_order_map[s.stage_name])

    ordered_names = [s.stage_name for s in sorted_stages]
    assert ordered_names == PIPELINE_STAGES

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

        stage_map = {s.stage_name: s for s in stages}
        assert stage_map["story_development"].status == "IN_PROGRESS"
        assert stage_map["story_development"].stage_metadata == '{"custom_seed": true}'
        assert {s.stage_name for s in stages} == set(PIPELINE_STAGES)
        assert {cp.checkpoint_type for cp in cps} == {c[0] for c in PIPELINE_CHECKPOINTS}
    finally:
        verify_session.close()


# ---------------------------------------------------------------------------
# 4. LEGACY SELF-HEALING & EXACT PIPELINE COMPLETENESS
# ---------------------------------------------------------------------------

def test_legacy_project_self_healing_missing_checkpoints(db_session, clean_project):
    """
    Verifies that a project with all 11 stages but missing checkpoints is safely self-healed.
    Existing stage states and metadata are preserved without being reset.
    """
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

    assert not is_pipeline_complete(db_session, clean_project.id)

    # 1. Strict read-only GET must NOT self-heal or mutate DB
    status_before = PipelineOrchestrator.get_pipeline_status(db_session, clean_project.id)
    assert status_before["is_initialized"] is False
    assert len(status_before["checkpoints"]) == 0
    assert not is_pipeline_complete(db_session, clean_project.id)

    # 2. Explicit initialization / backfill flow
    initialize_project_pipeline(clean_project.id, db_session)
    assert is_pipeline_complete(db_session, clean_project.id)

    status = PipelineOrchestrator.get_pipeline_status(db_session, clean_project.id)
    assert len(status["checkpoints"]) == 4
    assert status["is_initialized"] is True
    assert is_pipeline_complete(db_session, clean_project.id)

    screenplay_st = db_session.query(PipelineStageState).filter(
        PipelineStageState.project_id == clean_project.id,
        PipelineStageState.stage_name == "screenplay"
    ).first()
    assert screenplay_st.status == "IN_PROGRESS"
    assert screenplay_st.stage_metadata == '{"script_version": 2}'


def test_legacy_project_self_healing_missing_stages(db_session, clean_project):
    """
    Verifies that a project with 4 checkpoints but missing stages is safely self-healed.
    Existing checkpoint states and notes are preserved without being reset.
    """
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

    assert not is_pipeline_complete(db_session, clean_project.id)

    # 1. Strict read-only GET must NOT self-heal or mutate DB
    status_before = PipelineOrchestrator.get_pipeline_status(db_session, clean_project.id)
    assert status_before["is_initialized"] is False
    assert len(status_before["stages"]) == 0
    assert not is_pipeline_complete(db_session, clean_project.id)

    # 2. Explicit initialization / backfill flow
    initialize_project_pipeline(clean_project.id, db_session)
    assert is_pipeline_complete(db_session, clean_project.id)

    status = PipelineOrchestrator.get_pipeline_status(db_session, clean_project.id)
    assert len(status["stages"]) == 11
    assert status["is_initialized"] is True
    assert is_pipeline_complete(db_session, clean_project.id)

    story_cp = db_session.query(PipelineCheckpoint).filter(
        PipelineCheckpoint.project_id == clean_project.id,
        PipelineCheckpoint.checkpoint_type == "story_approval"
    ).first()
    assert story_cp.status == "APPROVED"
    assert story_cp.feedback_notes == "Director approved"


def test_exact_pipeline_completeness_and_unexpected_records(db_session, clean_project):
    """
    Verifies additional correction: exact equality in is_pipeline_complete().
    - Unexpected stages or checkpoints cause is_pipeline_complete to return False.
    - Missing stages or checkpoints cause is_pipeline_complete to return False.
    - Self-healing does not delete or overwrite existing records.
    """
    initialize_project_pipeline(clean_project.id, db_session)
    assert is_pipeline_complete(db_session, clean_project.id) is True

    # 1. Add unexpected rogue stage
    rogue_stage = PipelineStageState(
        project_id=clean_project.id,
        stage_name="rogue_bonus_stage",
        status="NOT_STARTED"
    )
    db_session.add(rogue_stage)
    db_session.commit()

    # Exact equality must reject unexpected stage
    assert is_pipeline_complete(db_session, clean_project.id) is False

    db_session.delete(rogue_stage)
    db_session.commit()
    assert is_pipeline_complete(db_session, clean_project.id) is True

    # 2. Add unexpected rogue checkpoint
    rogue_cp = PipelineCheckpoint(
        project_id=clean_project.id,
        checkpoint_type="rogue_approval",
        status="LOCKED",
        artifact_type="rogue"
    )
    db_session.add(rogue_cp)
    db_session.commit()

    # Exact equality must reject unexpected checkpoint
    assert is_pipeline_complete(db_session, clean_project.id) is False

    db_session.delete(rogue_cp)
    db_session.commit()
    assert is_pipeline_complete(db_session, clean_project.id) is True


# ---------------------------------------------------------------------------
# 5. ATOMIC DEMO SEEDING ROLLBACK TEST
# ---------------------------------------------------------------------------

def test_demo_project_seeding_atomic_rollback(tmp_path, monkeypatch):
    """
    Verifies BLOCKER 3:
    - seed_initial_demo_if_empty() creates all entities in one transaction without intermediate commits.
    - If pipeline initialization fails, the entire transaction is rolled back.
    - Zero partial demo records (projects, characters, locations, scenes, shots) remain.
    """
    import backend.app.main as main_module

    test_db_path = tmp_path / "demo_seed_test.db"
    db_url = f"sqlite:///{test_db_path}"
    test_engine = create_engine(db_url, connect_args={"check_same_thread": False})
    run_migrations(target_engine=test_engine, db_url=db_url)

    TestSession = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)
    monkeypatch.setattr(main_module, "SessionLocal", TestSession)

    # Force initialize_project_pipeline to fail
    def mock_fail_init(*args, **kwargs):
        raise RuntimeError("Simulated failure during demo pipeline initialization")

    monkeypatch.setattr(main_module, "initialize_project_pipeline", mock_fail_init)

    with pytest.raises(RuntimeError):
        seed_initial_demo_if_empty(force=True)

    # Verify no partial demo records committed
    db = TestSession()
    try:
        demo = db.query(Project).filter(Project.name == "Cyber Horizon: Echoes of 2088").first()
        assert demo is None, "Demo project must not exist after rollback"

        char = db.query(Character).filter(Character.name == "REN SHINODA").first()
        assert char is None, "Character must not exist after rollback"

        loc = db.query(Location).filter(Location.name == "Spire 9 Submerged Data Core").first()
        assert loc is None, "Location must not exist after rollback"

        scene = db.query(Scene).filter(Scene.title == "Scene 1: The Breach").first()
        assert scene is None, "Scene must not exist after rollback"

        shot = db.query(Shot).filter(Shot.shot_code == "SC01_SH01").first()
        assert shot is None, "Shot must not exist after rollback"
    finally:
        db.close()
        test_engine.dispose()


# ---------------------------------------------------------------------------
# 6. TRANSACTIONALLY FENCED STAGE COMPLETION TESTS
# ---------------------------------------------------------------------------

def test_transactionally_fenced_stage_completion(db_session, clean_project):
    """
    Verifies transactional worker fencing:
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
            fencing_token=19
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
            worker_id="rogue-worker",
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
    loc_stage.active_job_id = other_job.id
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

    with pytest.raises(JobOwnershipLostError):
        PipelineOrchestrator.fail_pipeline_stage(
            db=db_session,
            project_id=clean_project.id,
            stage_name="storyboard",
            active_job_id=job4.id,
            error="CUDA out of memory",
            worker_id="worker-4",
            fencing_token=39
        )
    db_session.refresh(job4)
    db_session.refresh(sb_stage)
    assert job4.status == "RUNNING"
    assert sb_stage.status == "IN_PROGRESS"

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
# 7. ARTIFACT HASH SCOPE CONTROL
# ---------------------------------------------------------------------------

def test_compute_artifact_hash_scope_control(db_session, clean_project):
    """
    Verifies Additional Fix D:
    - compute_artifact_hash correctly computes canonical SHA-256 for 'screenplay'.
    - compute_artifact_hash raises NotImplementedError for visual_bible, storyboard, assembly_cut.
    """
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

    for unsupported in ["visual_bible", "storyboard", "assembly_cut"]:
        with pytest.raises(NotImplementedError) as exc_info:
            PipelineOrchestrator.compute_artifact_hash(db_session, clean_project.id, unsupported)
        assert f"'{unsupported}' is not implemented" in str(exc_info.value)


# ---------------------------------------------------------------------------
# 8. PROJECT CREATION ROUTER INTEGRATION
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
    Verifies migration 0008, migration 0009, audit immutability triggers, RESTRICT foreign keys,
    and concurrent pipeline initialization on a real PostgreSQL instance.
    """
    pg_engine = get_postgres_engine()
    if not pg_engine:
        pytest.skip("PostgreSQL test database not available.")

    # 1. Run migrations to head (0009) on PostgreSQL
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

        with pytest.raises(Exception) as exc_update:
            conn.execute(text(f"UPDATE pipeline_checkpoint_audit SET decision = 'TAMPERED' WHERE id = '{audit_id}'"))
            conn.commit()
        assert "pipeline_checkpoint_audit is immutable and append-only" in str(exc_update.value)
        conn.rollback()

        with pytest.raises(Exception) as exc_delete:
            conn.execute(text(f"DELETE FROM pipeline_checkpoint_audit WHERE id = '{audit_id}'"))
            conn.commit()
        assert "pipeline_checkpoint_audit is immutable and append-only" in str(exc_delete.value)
        conn.rollback()

    # 3. Test RESTRICT foreign key constraint on PostgreSQL (established via 0009)
    PgSession = sessionmaker(bind=pg_engine)
    pg_session = PgSession()

    pg_proj = Project(name=f"PG Hard Delete Film {uuid.uuid4().hex[:6]}", budget_total=100.0)
    pg_session.add(pg_proj)
    pg_session.commit()
    pg_session.refresh(pg_proj)
    pg_pid = pg_proj.id

    initialize_project_pipeline(pg_pid, pg_session)

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
    Verifies transactional worker fencing specifically on PostgreSQL:
    - Valid worker completes stage and job atomically.
    - Stale fencing token is rejected and rolled back.
    - Expired lease is rejected and rolled back.
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
                fencing_token=199
            )

        session.refresh(job2)
        session.refresh(char_stage)
        assert job2.status == "RUNNING"
        assert char_stage.status == "IN_PROGRESS"

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


def test_postgresql_audit_purge_transaction_and_locking(tmp_path):
    """
    Verifies Requirement 2 on real PostgreSQL:
    - Audit archival and verification under PostgreSQL table locking.
    - safe_administrative_purge() properly manages PostgreSQL triggers and transactions.
    - Tampered archive aborts purge and rolls back without deleting PostgreSQL records.
    - Safe purge cleanly purges records under table lock.
    """
    from backend.app.services.audit_archiver import (
        archive_audit_records,
        safe_administrative_purge,
        AuditVerificationError,
    )

    pg_engine = get_postgres_engine()
    if not pg_engine:
        pytest.skip("PostgreSQL test database not available.")

    # 1. Ensure migrations applied to head on PostgreSQL
    alembic_cfg = Config(str(settings.root_dir / "alembic.ini"))
    alembic_cfg.set_main_option("script_location", str(settings.root_dir / "backend" / "alembic"))
    alembic_cfg.set_main_option("sqlalchemy.url", pg_engine.url.render_as_string(hide_password=False))
    alembic_cfg.attributes["configure_logger"] = False
    command.upgrade(alembic_cfg, "head")

    audit_id = f"pg-purge-{uuid.uuid4().hex[:8]}"
    with pg_engine.connect() as conn:
        conn.execute(text("""
            INSERT INTO pipeline_checkpoint_audit (
                id, project_id, project_name_snapshot, checkpoint_id,
                checkpoint_type, decision, artifact_content_hash,
                artifact_version, reviewer_name, feedback_notes, created_at
            ) VALUES (
                :id, 'pg-proj-p', 'PG Purge Film', 'cp-pg-1',
                'story_approval', 'APPROVED', 'pghash123',
                1, 'PG Director', 'pg notes', NOW()
            );
        """), {"id": audit_id})
        conn.commit()

    archive_file = tmp_path / "pg_audit_archive.json"

    # 2. Archive records from PostgreSQL
    archive_res = archive_audit_records(pg_engine, archive_file)
    assert archive_res["status"] == "success"

    # 3. Verification failure rolls back on PostgreSQL
    with open(archive_file, "r", encoding="utf-8") as f:
        tampered_data = json.load(f)
    tampered_data[0]["decision"] = "TAMPERED"
    tampered_file = tmp_path / "pg_tampered.json"
    with open(tampered_file, "w", encoding="utf-8") as f:
        json.dump(tampered_data, f)

    with pytest.raises(AuditVerificationError):
        safe_administrative_purge(pg_engine, tampered_file, force=True)

    # Verify record still exists in PostgreSQL
    with pg_engine.connect() as conn:
        cnt = conn.execute(text(f"SELECT count(*) FROM pipeline_checkpoint_audit WHERE id = '{audit_id}'")).scalar()
        assert cnt == 1, "Record must not be purged when archive verification fails on PostgreSQL"

    # 4. Safe purge succeeds on PostgreSQL using Engine
    purge_res = safe_administrative_purge(pg_engine, archive_file, force=True)
    assert purge_res["status"] == "purged"
    assert purge_res["is_durable"] is True

    with pg_engine.connect() as conn:
        cnt_after = conn.execute(text(f"SELECT count(*) FROM pipeline_checkpoint_audit WHERE id = '{audit_id}'")).scalar()
        assert cnt_after == 0, "Record must be cleanly purged after successful verification on PostgreSQL"

    # 5. Genuine Deterministic Concurrency Test on PostgreSQL:
    # ACCESS EXCLUSIVE lock blocks concurrent INSERT during purge window.
    # Worker B sets bounded lock_timeout, proves lock contention at DB level,
    # rolls back, and retries successfully after Worker A finishes.
    # Record inserted by concurrent worker must NOT be deleted.
    audit_id_1 = f"pg-race-1-{uuid.uuid4().hex[:8]}"
    audit_id_2 = f"pg-race-2-{uuid.uuid4().hex[:8]}"

    with pg_engine.connect() as conn:
        conn.execute(text("""
            INSERT INTO pipeline_checkpoint_audit (
                id, project_id, project_name_snapshot, checkpoint_id,
                checkpoint_type, decision, artifact_content_hash,
                artifact_version, reviewer_name, feedback_notes, created_at
            ) VALUES (
                :id, 'pg-proj-p', 'PG Race Film', 'cp-pg-1',
                'story_approval', 'APPROVED', 'pghashrace1',
                1, 'PG Director', 'pg notes', NOW()
            );
        """), {"id": audit_id_1})
        conn.commit()

    archive_race_file = tmp_path / "pg_race_archive.json"
    archive_audit_records(pg_engine, archive_race_file)

    pg_lock_acquired = threading.Event()
    pg_contention_proven = threading.Event()
    worker_a_pg_result = {}
    worker_b_pg_result = {}

    def purge_worker_pg():
        try:
            def on_lock():
                pg_lock_acquired.set()
                contention_ok = pg_contention_proven.wait(timeout=5.0)
                if not contention_ok:
                    raise TimeoutError("Worker A (PG) timed out waiting for Worker B to prove lock contention")

            safe_administrative_purge(
                pg_engine,
                archive_race_file,
                force=True,
                _on_lock_acquired=on_lock
            )
            worker_a_pg_result["success"] = True
        except Exception as e:
            worker_a_pg_result["error"] = e

    def insert_worker_pg():
        try:
            acquired_ok = pg_lock_acquired.wait(timeout=5.0)
            if not acquired_ok:
                raise TimeoutError("Worker B (PG) timed out waiting for Worker A to acquire lock")

            with pg_engine.connect() as conn_b:
                tx_b = conn_b.begin()
                conn_b.execute(text("SET lock_timeout = '200ms';"))
                try:
                    conn_b.execute(text("""
                        INSERT INTO pipeline_checkpoint_audit (
                            id, project_id, project_name_snapshot, checkpoint_id,
                            checkpoint_type, decision, artifact_content_hash,
                            artifact_version, reviewer_name, feedback_notes, created_at
                        ) VALUES (
                            :id, 'pg-proj-p', 'PG Race Film', 'cp-pg-2',
                            'visual_approval', 'APPROVED', 'pghashrace2',
                            1, 'PG Director', 'pg notes 2', NOW()
                        );
                    """), {"id": audit_id_2})
                    worker_b_pg_result["locked"] = False
                    raise AssertionError("Worker B unexpectedly inserted while Worker A held ACCESS EXCLUSIVE lock!")
                except Exception as e:
                    err_msg = str(e).lower()
                    if "lock timeout" in err_msg or "55p03" in err_msg or "locknotavailable" in err_msg:
                        worker_b_pg_result["locked"] = True
                    else:
                        raise e
                    try:
                        tx_b.rollback()
                    except Exception:
                        pass
                finally:
                    pg_contention_proven.set()
        except Exception as e:
            worker_b_pg_result["error"] = e
            pg_contention_proven.set()

    t_pg_a = threading.Thread(target=purge_worker_pg, name="worker_pg_a")
    t_pg_b = threading.Thread(target=insert_worker_pg, name="worker_pg_b")

    t_pg_a.start()
    t_pg_b.start()

    t_pg_b.join(timeout=10.0)
    assert not t_pg_b.is_alive(), "Worker B (PG) thread timed out"

    t_pg_a.join(timeout=10.0)
    assert not t_pg_a.is_alive(), "Worker A (PG) thread timed out"

    if "error" in worker_a_pg_result:
        raise worker_a_pg_result["error"]
    if "error" in worker_b_pg_result:
        raise worker_b_pg_result["error"]

    assert worker_a_pg_result.get("success") is True, f"PG Worker A failed: {worker_a_pg_result}"
    assert worker_b_pg_result.get("locked") is True, "PG Worker B must have proven lock timeout contention"

    # Worker B retries insert after Worker A completed and released the lock
    with pg_engine.connect() as conn_retry:
        conn_retry.execute(text("""
            INSERT INTO pipeline_checkpoint_audit (
                id, project_id, project_name_snapshot, checkpoint_id,
                checkpoint_type, decision, artifact_content_hash,
                artifact_version, reviewer_name, feedback_notes, created_at
            ) VALUES (
                :id, 'pg-proj-p', 'PG Race Film', 'cp-pg-2',
                'visual_approval', 'APPROVED', 'pghashrace2',
                1, 'PG Director', 'pg notes 2', NOW()
            );
        """), {"id": audit_id_2})
        conn_retry.commit()

    with pg_engine.connect() as conn:
        rows = conn.execute(text("SELECT id FROM pipeline_checkpoint_audit")).fetchall()
        row_ids = [r[0] for r in rows]
        assert audit_id_1 not in row_ids, f"Archived record {audit_id_1} must be purged on PostgreSQL"
        assert audit_id_2 in row_ids, f"Unarchived concurrent record {audit_id_2} must survive on PostgreSQL!"

    pg_engine.dispose()


def test_caller_owned_failure_transaction_semantics_sqlite(tmp_path):
    """
    Verifies Requirement 4 (SQLite Caller-Owned Failure Transaction Contract):
    - When safe_administrative_purge() fails on a Session/caller-Connection target:
      1. SAVEPOINT rollback restores changes made inside that savepoint.
      2. The caller-owned outer transaction remains active with write lock held.
      3. Unrelated caller work on the session is not rolled back.
      4. A second connection attempting BEGIN IMMEDIATE receives 'database is locked'.
      5. After the caller explicitly calls session.rollback(), the lock is released.
      6. The second connection can now acquire BEGIN IMMEDIATE and insert normally.
      7. Failed verification never deletes audit records or leaves triggers disabled.
    """
    from backend.app.services.audit_archiver import (
        safe_administrative_purge,
        AuditVerificationError,
    )

    test_db_path = tmp_path / "caller_failure_contract.db"
    db_url = f"sqlite:///{test_db_path}"
    engine = create_engine(db_url)
    run_migrations(target_engine=engine, db_url=db_url)

    # 1. Insert initial audit record
    with engine.connect() as conn:
        conn.execute(text("""
            INSERT INTO pipeline_checkpoint_audit (
                id, project_id, project_name_snapshot, checkpoint_id,
                checkpoint_type, decision, artifact_content_hash,
                artifact_version, reviewer_name, created_at
            ) VALUES (
                'rec-caller-fail-1', 'proj-1', 'Fail Contract Film', 'cp-1',
                'story_approval', 'APPROVED', 'hash1',
                1, 'Director', datetime('now')
            )
        """))
        conn.commit()

    # 2. Create invalid archive (missing record)
    bad_archive = tmp_path / "bad_archive.json"
    with open(bad_archive, "w") as f:
        f.write("[]")

    SessionLocalTest = sessionmaker(bind=engine)
    session = SessionLocalTest()

    # Add unrelated work to session before purge
    unrelated_proj = Project(
        id="proj-caller-unrelated-fail",
        name="Unrelated Work Preserved",
        budget_total=500.0,
        budget_spent=0.0
    )
    session.add(unrelated_proj)
    assert unrelated_proj in session.new, "Unrelated project should be pending in session"

    # 3. Call safe_administrative_purge with invalid archive
    with pytest.raises(AuditVerificationError):
        safe_administrative_purge(session, bad_archive, force=True)

    # Assert unrelated work is NOT automatically rolled back
    assert unrelated_proj in session.new, "Unrelated caller work must NOT be automatically discarded by purge failure"

    # 4. While caller session transaction is still active, connection 2 tries BEGIN IMMEDIATE
    with engine.connect() as conn2:
        conn2.exec_driver_sql("PRAGMA busy_timeout = 100;")
        conn2_locked = False
        try:
            conn2.exec_driver_sql("BEGIN IMMEDIATE")
        except Exception as e:
            if "locked" in str(e).lower():
                conn2_locked = True
            try:
                conn2.rollback()
            except Exception:
                pass
        assert conn2_locked is True, "Connection 2 must be locked while caller session holds write lock"

    # 5. Caller promptly rolls back outer transaction
    session.rollback()
    session.close()

    # 6. Connection 2 can now acquire BEGIN IMMEDIATE and insert normally
    with engine.connect() as conn2:
        conn2.exec_driver_sql("BEGIN IMMEDIATE")
        conn2.execute(text("""
            INSERT INTO pipeline_checkpoint_audit (
                id, project_id, project_name_snapshot, checkpoint_id,
                checkpoint_type, decision, artifact_content_hash,
                artifact_version, reviewer_name, created_at
            ) VALUES (
                'rec-caller-fail-2', 'proj-1', 'Fail Contract Film', 'cp-2',
                'visual_approval', 'APPROVED', 'hash2',
                1, 'Director', datetime('now')
            )
        """))
        conn2.commit()

    # 7. Assertions on data preservation and trigger integrity
    with engine.connect() as check_conn:
        rows = check_conn.execute(text("SELECT id FROM pipeline_checkpoint_audit")).fetchall()
        row_ids = [r[0] for r in rows]
        assert "rec-caller-fail-1" in row_ids, "Existing audit record 1 must NEVER be deleted by failed verification"
        assert "rec-caller-fail-2" in row_ids, "Record 2 inserted after lock release must exist"
        assert len(row_ids) == 2

        # Verify immutability triggers remain active
        trigger_blocked = False
        try:
            check_conn.execute(text("DELETE FROM pipeline_checkpoint_audit WHERE id = 'rec-caller-fail-1'"))
        except Exception as e:
            if "immutable and append-only" in str(e):
                trigger_blocked = True
        assert trigger_blocked is True, "Immutability trigger must remain active and block DELETE after failed verification"

    engine.dispose()


def test_caller_owned_failure_transaction_semantics_postgresql(tmp_path):
    """
    Verifies Requirement 4 (PostgreSQL Caller-Owned Failure Transaction Contract):
    - When safe_administrative_purge() fails on PostgreSQL Session target:
      1. SAVEPOINT rollback restores savepoint state.
      2. The outer transaction remains active holding ACCESS EXCLUSIVE table lock.
      3. A second connection attempting INSERT with bounded lock_timeout experiences lock timeout.
      4. After the caller explicitly rolls back the session, the table lock is released.
      5. The second connection can now insert records normally.
      6. Audit records are preserved and table triggers remain active.
    """
    from backend.tests.test_postgres_concurrency import get_postgres_engine
    from backend.app.services.audit_archiver import (
        safe_administrative_purge,
        AuditVerificationError,
    )

    pg_engine = get_postgres_engine()
    if pg_engine is None:
        pytest.skip("PostgreSQL test instance not reachable; skipping PG caller failure contract test.")

    audit_id_1 = f"pg-cfail-1-{uuid.uuid4().hex[:8]}"
    audit_id_2 = f"pg-cfail-2-{uuid.uuid4().hex[:8]}"

    # 1. Insert initial audit record
    with pg_engine.connect() as conn:
        conn.execute(text("""
            INSERT INTO pipeline_checkpoint_audit (
                id, project_id, project_name_snapshot, checkpoint_id,
                checkpoint_type, decision, artifact_content_hash,
                artifact_version, reviewer_name, feedback_notes, created_at
            ) VALUES (
                :id, 'pg-cfail-proj', 'PG CFail Film', 'cp-1',
                'story_approval', 'APPROVED', 'pghash1',
                1, 'PG Director', 'notes', NOW()
            );
        """), {"id": audit_id_1})
        conn.commit()

    bad_archive = tmp_path / "pg_bad_archive.json"
    with open(bad_archive, "w") as f:
        f.write("[]")

    SessionLocalTest = sessionmaker(bind=pg_engine)
    session = SessionLocalTest()

    # 2. Call safe_administrative_purge with bad archive
    with pytest.raises(AuditVerificationError):
        safe_administrative_purge(session, bad_archive, force=True)

    # 3. Connection 2 attempts INSERT with lock_timeout while session holds ACCESS EXCLUSIVE lock
    with pg_engine.connect() as conn2:
        tx2 = conn2.begin()
        conn2.execute(text("SET lock_timeout = '200ms';"))
        conn2_locked = False
        try:
            conn2.execute(text("""
                INSERT INTO pipeline_checkpoint_audit (
                    id, project_id, project_name_snapshot, checkpoint_id,
                    checkpoint_type, decision, artifact_content_hash,
                    artifact_version, reviewer_name, feedback_notes, created_at
                ) VALUES (
                    :id, 'pg-cfail-proj', 'PG CFail Film', 'cp-2',
                    'visual_approval', 'APPROVED', 'pghash2',
                    1, 'PG Director', 'notes 2', NOW()
                );
            """), {"id": audit_id_2})
        except Exception as e:
            err_msg = str(e).lower()
            if "lock timeout" in err_msg or "55p03" in err_msg or "locknotavailable" in err_msg:
                conn2_locked = True
            try:
                tx2.rollback()
            except Exception:
                pass
        assert conn2_locked is True, "PG Connection 2 must timeout while caller session holds ACCESS EXCLUSIVE lock"

    # 4. Caller promptly rolls back outer transaction
    session.rollback()
    session.close()

    # 5. Connection 2 retries INSERT and succeeds
    with pg_engine.connect() as conn2:
        conn2.execute(text("""
            INSERT INTO pipeline_checkpoint_audit (
                id, project_id, project_name_snapshot, checkpoint_id,
                checkpoint_type, decision, artifact_content_hash,
                artifact_version, reviewer_name, feedback_notes, created_at
            ) VALUES (
                :id, 'pg-cfail-proj', 'PG CFail Film', 'cp-2',
                'visual_approval', 'APPROVED', 'pghash2',
                1, 'PG Director', 'notes 2', NOW()
            );
        """), {"id": audit_id_2})
        conn2.commit()

    # 6. Verify audit records and trigger protection
    with pg_engine.connect() as check_conn:
        rows = check_conn.execute(text(f"SELECT id FROM pipeline_checkpoint_audit WHERE id IN ('{audit_id_1}', '{audit_id_2}')")).fetchall()
        row_ids = [r[0] for r in rows]
        assert audit_id_1 in row_ids, "Existing PostgreSQL audit record must NOT be deleted by failed verification"
        assert audit_id_2 in row_ids, "PostgreSQL record 2 inserted after lock release must survive"
        assert len(row_ids) == 2

        # Verify trigger blocks DELETE
        trigger_blocked = False
        try:
            check_conn.execute(text(f"DELETE FROM pipeline_checkpoint_audit WHERE id = '{audit_id_1}'"))
        except Exception as e:
            if "immutable and append-only" in str(e):
                trigger_blocked = True
        assert trigger_blocked is True, "PostgreSQL trigger must block DELETE after failed verification"

    pg_engine.dispose()
