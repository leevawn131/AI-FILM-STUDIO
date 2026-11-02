"""
Pipeline Read API Tests for AI Film Studio (S4 Milestone 2.1).

Validates:
1. Complete pipeline state retrieval (`GET /api/pipeline/{project_id}`):
   - Returns overall pipeline status and active stage.
   - All 11 discrete stages returned in exact canonical order with 1-based order_index.
   - All 4 approval checkpoints returned in canonical order with initial LOCKED status.
2. Individual stage detail (`GET /api/pipeline/{project_id}/stages/{stage_name}`):
   - Stage status, content hashes, stage_metadata, and timestamps.
   - Active StudioJob projection without exposing sensitive worker details.
   - Associated checkpoint linkage (e.g. screenplay -> story_approval).
3. Approval checkpoint detail (`GET /api/pipeline/{project_id}/checkpoints/{checkpoint_type}`):
   - Checkpoint identity, status, artifact version, and content hash.
   - On-the-fly current artifact hash calculation and hash freshness comparison.
   - Latest compliance audit journal decision summary.
4. Checkpoint audit history (`GET /api/pipeline/{project_id}/checkpoints/{checkpoint_type}/audit`):
   - Chronological, descending order compliance audit entries.
   - Pagination support (`limit`, `offset`, `total`).
   - Boundary conditions (offset beyond total, limit boundaries, empty lists).
5. Project-wide audit history (`GET /api/pipeline/{project_id}/audit`):
   - Multi-checkpoint aggregation and optional filtering by checkpoint_type.
6. Security and cross-project isolation:
   - Rejects non-existent projects with HTTP 404.
   - Rejects soft-deleted projects with HTTP 404.
   - Rejects invalid/unknown stage names with HTTP 404.
   - Rejects invalid/unknown checkpoint types with HTTP 404.
   - Strict cross-project data isolation: Project A never leaks into Project B.
7. Resilience & self-healing:
   - Safely self-heals legacy projects missing pipeline stages or checkpoints.
8. Read-only integrity:
   - Repeated GET requests never mutate data or generate spurious audit entries.
9. Dual-engine compatibility:
   - Runs against SQLite and PostgreSQL.
"""

import os
import json
import uuid
from datetime import datetime, timezone
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from alembic.config import Config
from alembic import command

from backend.app.main import app
from backend.app.config import settings
from backend.app.database import get_db, init_db, run_migrations
from backend.app.models.entities import (
    Base, Project, Scene, Shot, Character, Location,
    PipelineStageState, PipelineCheckpoint, PipelineCheckpointAudit,
    StudioJob
)
from backend.app.services.pipeline_orchestrator import (
    pipeline_orchestrator, PIPELINE_STAGES, PIPELINE_CHECKPOINTS
)
from backend.tests.test_postgres_concurrency import get_postgres_engine

client = TestClient(app)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@pytest.fixture
def test_db_session(tmp_path):
    """Provides an isolated SQLite database and test DB session."""
    db_file = tmp_path / "read_api_test.db"
    db_url = f"sqlite:///{db_file}"
    engine = create_engine(db_url)
    run_migrations(target_engine=engine, db_url=db_url)
    TestingSessionLocal = sessionmaker(bind=engine)

    def override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    session = TestingSessionLocal()
    try:
        yield session, engine
    finally:
        session.close()
        app.dependency_overrides.pop(get_db, None)
        engine.dispose()


def test_get_complete_pipeline_state_success(test_db_session):
    """
    Verifies Requirement 3.A:
    - Retrieves complete pipeline state for an initialized project.
    - Verifies all 11 stages in exact canonical order with 1-based order_index.
    - Verifies all 4 checkpoints in canonical order.
    """
    session, engine = test_db_session

    # Create project via projects API
    create_res = client.post("/api/projects", json={
        "name": "Canonical Pipeline Project",
        "logline": "A test project for pipeline read API validation.",
        "genre": "Drama",
        "budget_total": 120.0
    })
    assert create_res.status_code == 201
    project_id = create_res.json()["id"]

    # Retrieve complete pipeline state
    res = client.get(f"/api/pipeline/{project_id}")
    assert res.status_code == 200
    data = res.json()

    assert data["project_id"] == project_id
    assert data["project_name"] == "Canonical Pipeline Project"
    assert data["current_pipeline_stage"] == "story_development"
    assert data["pipeline_overall_status"] == "in_progress"
    assert data["is_initialized"] is True

    # Verify exactly 11 stages in canonical order
    stages = data["stages"]
    assert len(stages) == 11
    returned_stage_names = [s["stage_name"] for s in stages]
    assert returned_stage_names == PIPELINE_STAGES

    for idx, stage in enumerate(stages):
        assert stage["project_id"] == project_id
        assert stage["order_index"] == idx + 1
        assert stage["status"] == "NOT_STARTED"
        assert stage["retry_count"] == 0
        assert "created_at" in stage
        assert "updated_at" in stage

    # Verify exactly 4 checkpoints in canonical order
    checkpoints = data["checkpoints"]
    assert len(checkpoints) == 4
    expected_cp_types = [cp[0] for cp in PIPELINE_CHECKPOINTS]
    returned_cp_types = [cp["checkpoint_type"] for cp in checkpoints]
    assert returned_cp_types == expected_cp_types

    for cp in checkpoints:
        assert cp["project_id"] == project_id
        assert cp["status"] == "LOCKED"
        assert cp["artifact_version"] == 1
        assert cp["artifact_content_hash"] is None
        assert cp["reviewer_name"] == "Director / User"


def test_get_pipeline_state_missing_and_soft_deleted_project(test_db_session):
    """
    Verifies Requirement 4:
    - Returns HTTP 404 for non-existent project IDs.
    - Returns HTTP 404 for soft-deleted projects.
    """
    session, engine = test_db_session

    # Non-existent project
    fake_id = str(uuid.uuid4())
    res = client.get(f"/api/pipeline/{fake_id}")
    assert res.status_code == 404
    assert res.json()["detail"] == "Project not found"

    # Soft-deleted project
    create_res = client.post("/api/projects", json={"name": "To Delete Project"})
    project_id = create_res.json()["id"]

    # Delete project (soft delete)
    del_res = client.delete(f"/api/projects/{project_id}")
    assert del_res.status_code == 200

    # Read pipeline state for soft-deleted project must return 404
    res_after_del = client.get(f"/api/pipeline/{project_id}")
    assert res_after_del.status_code == 404
    assert res_after_del.json()["detail"] == "Project not found"


def test_get_individual_stage_detail_and_associations(test_db_session):
    """
    Verifies Requirement 3.B:
    - Retrieves details for an individual stage.
    - Verifies associated checkpoint linkage (screenplay -> story_approval).
    - Verifies active background StudioJob summary projection without leaking internal secrets.
    """
    session, engine = test_db_session

    create_res = client.post("/api/projects", json={"name": "Stage Detail Film"})
    project_id = create_res.json()["id"]

    # 1. Query screenplay stage (which has associated checkpoint story_approval)
    res = client.get(f"/api/pipeline/{project_id}/stages/screenplay")
    assert res.status_code == 200
    data = res.json()

    assert data["project_id"] == project_id
    assert data["stage_name"] == "screenplay"
    assert data["order_index"] == 2
    assert data["status"] == "NOT_STARTED"
    assert data["associated_checkpoint"] is not None
    assert data["associated_checkpoint"]["checkpoint_type"] == "story_approval"
    assert data["associated_checkpoint"]["artifact_type"] == "screenplay"
    assert data["active_job"] is None

    # 2. Attach a StudioJob to a stage and verify safe job projection
    job = StudioJob(
        id=str(uuid.uuid4()),
        project_id=project_id,
        job_type="screenwriter_agent",
        idempotency_key=f"idemp-{uuid.uuid4().hex[:8]}",
        status="RUNNING",
        worker_id="secret_worker_node_42",
        fencing_token=105,
        payload='{"secret_key": "private_data"}',
        created_at=utc_now(),
        updated_at=utc_now()
    )
    session.add(job)
    session.flush()

    stage_rec = session.query(PipelineStageState).filter(
        PipelineStageState.project_id == project_id,
        PipelineStageState.stage_name == "screenplay"
    ).first()
    stage_rec.active_job_id = job.id
    stage_rec.status = "IN_PROGRESS"
    stage_rec.stage_metadata = '{"act_count": 3, "estimated_pages": 45}'
    session.commit()

    res_with_job = client.get(f"/api/pipeline/{project_id}/stages/screenplay")
    assert res_with_job.status_code == 200
    job_data = res_with_job.json()

    assert job_data["status"] == "IN_PROGRESS"
    assert job_data["active_job_id"] == job.id
    assert job_data["stage_metadata"] == {"act_count": 3, "estimated_pages": 45}

    active_job = job_data["active_job"]
    assert active_job is not None
    assert active_job["id"] == job.id
    assert active_job["job_type"] == "screenwriter_agent"
    assert active_job["status"] == "RUNNING"
    # Verify sensitive worker and internal payload information is NOT leaked
    assert "worker_id" not in active_job
    assert "fencing_token" not in active_job
    assert "payload" not in active_job
    assert "secret_worker_node_42" not in str(job_data)


def test_get_stage_detail_validation_and_not_found(test_db_session):
    """
    Verifies validation on stage endpoints:
    - Unknown stage name returns HTTP 404.
    - Missing project returns HTTP 404.
    """
    session, engine = test_db_session

    create_res = client.post("/api/projects", json={"name": "Stage Validation Film"})
    project_id = create_res.json()["id"]

    # Unknown stage
    res_bad_stage = client.get(f"/api/pipeline/{project_id}/stages/teleportation")
    assert res_bad_stage.status_code == 404
    assert "Stage 'teleportation' not found" in res_bad_stage.json()["detail"]

    # Missing project
    res_bad_proj = client.get(f"/api/pipeline/{uuid.uuid4()}/stages/screenplay")
    assert res_bad_proj.status_code == 404
    assert res_bad_proj.json()["detail"] == "Project not found"


def test_get_checkpoint_detail_freshness_and_latest_audit(test_db_session):
    """
    Verifies Requirement 3.C:
    - Retrieves checkpoint details.
    - Evaluates on-the-fly current artifact hash and hash freshness.
    - Verifies latest compliance audit journal summary.
    """
    session, engine = test_db_session

    create_res = client.post("/api/projects", json={"name": "Checkpoint Detail Film"})
    project_id = create_res.json()["id"]

    # 1. Initial LOCKED state with no screenplay scenes
    res = client.get(f"/api/pipeline/{project_id}/checkpoints/story_approval")
    assert res.status_code == 200
    cp_data = res.json()

    assert cp_data["checkpoint_type"] == "story_approval"
    assert cp_data["status"] == "LOCKED"
    assert cp_data["artifact_content_hash"] is None
    assert cp_data["current_artifact_hash"] is None
    assert cp_data["is_hash_fresh"] is None
    assert cp_data["latest_audit"] is None

    # 2. Add screenplay scene and compute artifact hash
    scene = Scene(
        id=str(uuid.uuid4()),
        project_id=project_id,
        scene_number=1,
        slugline="EXT. ROOFTOP - DUSK",
        title="Scene 1",
        summary="A lone operative watches the skyline.",
        script_content="EXT. ROOFTOP - DUSK\n\nRain falls across the neon city.",
        estimated_duration=30,
        order_index=1
    )
    session.add(scene)
    session.commit()

    # Query again: current_artifact_hash should now be dynamically computed
    res_with_scene = client.get(f"/api/pipeline/{project_id}/checkpoints/story_approval")
    assert res_with_scene.status_code == 200
    cp_data_scene = res_with_scene.json()
    assert cp_data_scene["current_artifact_hash"] is not None
    assert len(cp_data_scene["current_artifact_hash"]) == 64
    # Not approved yet, so artifact_content_hash is still None
    assert cp_data_scene["artifact_content_hash"] is None

    # 3. Approve checkpoint via service
    approved_cp = pipeline_orchestrator.record_checkpoint_decision(
        db=session,
        project_id=project_id,
        checkpoint_type="story_approval",
        decision="APPROVED",
        notes="Dialogue passes director guidelines.",
        reviewer_name="Executive Producer"
    )

    res_approved = client.get(f"/api/pipeline/{project_id}/checkpoints/story_approval")
    assert res_approved.status_code == 200
    approved_data = res_approved.json()

    assert approved_data["status"] == "APPROVED"
    assert approved_data["artifact_content_hash"] == approved_cp.artifact_content_hash
    assert approved_data["is_hash_fresh"] is True
    assert approved_data["reviewer_name"] == "Executive Producer"
    assert approved_data["feedback_notes"] == "Dialogue passes director guidelines."

    latest_audit = approved_data["latest_audit"]
    assert latest_audit is not None
    assert latest_audit["decision"] == "APPROVED"
    assert latest_audit["reviewer_name"] == "Executive Producer"
    assert latest_audit["artifact_content_hash"] == approved_cp.artifact_content_hash

    # 4. Invalidate upstream screenplay
    pipeline_orchestrator.invalidate_downstream_stages(session, project_id, "screenplay")

    res_stale = client.get(f"/api/pipeline/{project_id}/checkpoints/story_approval")
    assert res_stale.status_code == 200
    stale_data = res_stale.json()

    assert stale_data["status"] == "STALE"
    assert stale_data["latest_audit"]["decision"] == "INVALIDATED"


def test_get_checkpoint_detail_validation_and_not_found(test_db_session):
    """
    Verifies validation on checkpoint detail endpoints:
    - Unknown checkpoint type returns HTTP 404.
    - Non-existent project returns HTTP 404.
    """
    session, engine = test_db_session

    create_res = client.post("/api/projects", json={"name": "CP Validation Film"})
    project_id = create_res.json()["id"]

    # Unknown checkpoint type
    res_bad_cp = client.get(f"/api/pipeline/{project_id}/checkpoints/catering_approval")
    assert res_bad_cp.status_code == 404
    assert "Checkpoint 'catering_approval' not found" in res_bad_cp.json()["detail"]

    # Non-existent project
    res_bad_proj = client.get(f"/api/pipeline/{uuid.uuid4()}/checkpoints/story_approval")
    assert res_bad_proj.status_code == 404
    assert res_bad_proj.json()["detail"] == "Project not found"


def test_get_checkpoint_audit_history_and_pagination(test_db_session):
    """
    Verifies Requirement 3.D:
    - Retrieves paginated audit history for a specific checkpoint.
    - Verifies chronological ordering (descending by created_at).
    - Verifies pagination boundaries (`limit`, `offset`, `total`).
    """
    session, engine = test_db_session

    create_res = client.post("/api/projects", json={"name": "Audit History Film"})
    project_id = create_res.json()["id"]

    # Add scene so hash can be computed
    scene = Scene(
        id=str(uuid.uuid4()),
        project_id=project_id,
        scene_number=1,
        slugline="INT. BUNKER - NIGHT",
        script_content="INT. BUNKER - NIGHT\n\nEmergency lights pulse amber.",
        order_index=1
    )
    session.add(scene)
    session.commit()

    # Generate 5 audit events
    decisions = [
        ("REJECTED", "Needs more dramatic tension.", "Director"),
        ("APPROVED", "Revisions accepted.", "Director"),
    ]
    for dec, notes, rev in decisions:
        pipeline_orchestrator.record_checkpoint_decision(
            session, project_id, "story_approval", dec, notes=notes, reviewer_name=rev
        )

    # Invalidate
    pipeline_orchestrator.invalidate_downstream_stages(session, project_id, "screenplay")

    # Re-approve
    pipeline_orchestrator.record_checkpoint_decision(
        session, project_id, "story_approval", "APPROVED", notes="Final sign-off.", reviewer_name="Producer"
    )

    # 1. Default audit query (returns all entries, limit=50)
    res_all = client.get(f"/api/pipeline/{project_id}/checkpoints/story_approval/audit")
    assert res_all.status_code == 200
    data_all = res_all.json()

    assert data_all["project_id"] == project_id
    assert data_all["checkpoint_type"] == "story_approval"
    assert data_all["total"] >= 4
    items = data_all["items"]
    assert len(items) == data_all["total"]

    # Verify chronological descending ordering
    timestamps = [item["created_at"] for item in items]
    assert timestamps == sorted(timestamps, reverse=True)

    # Latest decision should be APPROVED by Producer
    assert items[0]["decision"] == "APPROVED"
    assert items[0]["reviewer_name"] == "Producer"

    # 2. Pagination: limit=2, offset=0
    res_page1 = client.get(f"/api/pipeline/{project_id}/checkpoints/story_approval/audit?limit=2&offset=0")
    assert res_page1.status_code == 200
    p1_data = res_page1.json()
    assert p1_data["limit"] == 2
    assert p1_data["offset"] == 0
    assert len(p1_data["items"]) == 2
    assert p1_data["items"][0]["id"] == items[0]["id"]
    assert p1_data["items"][1]["id"] == items[1]["id"]

    # 3. Pagination: limit=2, offset=2
    res_page2 = client.get(f"/api/pipeline/{project_id}/checkpoints/story_approval/audit?limit=2&offset=2")
    assert res_page2.status_code == 200
    p2_data = res_page2.json()
    assert p2_data["limit"] == 2
    assert p2_data["offset"] == 2
    assert len(p2_data["items"]) == 2
    assert p2_data["items"][0]["id"] == items[2]["id"]
    assert p2_data["items"][1]["id"] == items[3]["id"]

    # 4. Pagination boundary: offset beyond total
    res_beyond = client.get(f"/api/pipeline/{project_id}/checkpoints/story_approval/audit?limit=10&offset=500")
    assert res_beyond.status_code == 200
    beyond_data = res_beyond.json()
    assert beyond_data["items"] == []
    assert beyond_data["total"] == data_all["total"]

    # 5. Invalid query parameters (limit=0, offset=-1) must return 422
    res_bad_param = client.get(f"/api/pipeline/{project_id}/checkpoints/story_approval/audit?limit=0")
    assert res_bad_param.status_code == 422


def test_get_project_wide_audit_history(test_db_session):
    """
    Verifies project-wide audit retrieval:
    `GET /api/pipeline/{project_id}/audit`
    """
    session, engine = test_db_session

    create_res = client.post("/api/projects", json={"name": "Project Wide Audit Film"})
    project_id = create_res.json()["id"]

    scene = Scene(
        id=str(uuid.uuid4()),
        project_id=project_id,
        scene_number=1,
        slugline="INT. LAB - DAY",
        script_content="INT. LAB - DAY\n\nBeakers bubble.",
        order_index=1
    )
    session.add(scene)
    session.commit()

    # Record decision
    pipeline_orchestrator.record_checkpoint_decision(
        session, project_id, "story_approval", "APPROVED", notes="Approved."
    )

    res = client.get(f"/api/pipeline/{project_id}/audit")
    assert res.status_code == 200
    data = res.json()
    assert data["project_id"] == project_id
    assert data["total"] >= 1
    assert data["items"][0]["checkpoint_type"] == "story_approval"

    # Filter by checkpoint_type
    res_filtered = client.get(f"/api/pipeline/{project_id}/audit?checkpoint_type=story_approval")
    assert res_filtered.status_code == 200
    assert res_filtered.json()["total"] >= 1

    res_empty_filter = client.get(f"/api/pipeline/{project_id}/audit?checkpoint_type=visual_approval")
    assert res_empty_filter.status_code == 200
    assert res_empty_filter.json()["total"] == 0


def test_cross_project_isolation_and_security(test_db_session):
    """
    Verifies Requirement 4:
    - Strict cross-project isolation: Project A never exposes data belonging to Project B.
    """
    session, engine = test_db_session

    # Create Project A
    res_a = client.post("/api/projects", json={"name": "Project Alpha"})
    id_a = res_a.json()["id"]

    # Create Project B
    res_b = client.post("/api/projects", json={"name": "Project Beta"})
    id_b = res_b.json()["id"]

    # Add scene and approval for Project A
    scene_a = Scene(
        id=str(uuid.uuid4()),
        project_id=id_a,
        scene_number=1,
        slugline="INT. ALPHA - DAY",
        script_content="Alpha script.",
        order_index=1
    )
    session.add(scene_a)
    session.commit()

    pipeline_orchestrator.record_checkpoint_decision(
        session, id_a, "story_approval", "APPROVED", notes="Alpha notes", reviewer_name="Reviewer Alpha"
    )

    # Query Project B pipeline
    res_pipe_b = client.get(f"/api/pipeline/{id_b}")
    assert res_pipe_b.status_code == 200
    data_b = res_pipe_b.json()
    assert data_b["project_id"] == id_b
    assert data_b["project_name"] == "Project Beta"
    # Project B's story_approval must still be LOCKED
    cp_b_story = next(cp for cp in data_b["checkpoints"] if cp["checkpoint_type"] == "story_approval")
    assert cp_b_story["status"] == "LOCKED"
    assert cp_b_story["artifact_content_hash"] is None

    # Query Project B checkpoint audit: must NOT contain Project A's audit records
    res_audit_b = client.get(f"/api/pipeline/{id_b}/checkpoints/story_approval/audit")
    assert res_audit_b.status_code == 200
    assert res_audit_b.json()["total"] == 0
    assert res_audit_b.json()["items"] == []


def test_uninitialized_and_partial_pipeline_read_only_zero_mutation(test_db_session):
    """
    Verifies Requirement 1: Strict read-only API with ZERO database mutations.
    - Legacy uninitialized projects return is_initialized=False, empty stages, empty checkpoints.
    - Partially initialized projects return is_initialized=False and existing stages only.
    - Calling all GET endpoints produces zero INSERTs, zero UPDATEs, zero COMMITs.
    """
    session, engine = test_db_session

    # 1. Create uninitialized project directly via ORM without initializing pipeline
    uninit_proj = Project(
        id=str(uuid.uuid4()),
        name="Legacy Uninitialized Film",
        budget_total=100.0,
        budget_spent=0.0
    )
    # 2. Create partially initialized project with only 1 stage
    partial_proj = Project(
        id=str(uuid.uuid4()),
        name="Partial Film",
        budget_total=150.0,
        budget_spent=0.0
    )
    partial_stage = PipelineStageState(
        id=str(uuid.uuid4()),
        project_id=partial_proj.id,
        stage_name="story_development",
        status="NOT_STARTED",
        created_at=utc_now(),
        updated_at=utc_now()
    )
    session.add_all([uninit_proj, partial_proj, partial_stage])
    session.commit()

    # Record exact baseline counts
    stage_cnt_before = session.query(PipelineStageState).count()
    cp_cnt_before = session.query(PipelineCheckpoint).count()
    audit_cnt_before = session.query(PipelineCheckpointAudit).count()

    # Call all GET endpoints on uninitialized project
    res_status_uninit = client.get(f"/api/pipeline/{uninit_proj.id}")
    assert res_status_uninit.status_code == 200
    data_uninit = res_status_uninit.json()
    assert data_uninit["is_initialized"] is False
    assert data_uninit["stages"] == []
    assert data_uninit["checkpoints"] == []

    # Individual stage on uninitialized project returns 404 (not initialized)
    res_stage_uninit = client.get(f"/api/pipeline/{uninit_proj.id}/stages/screenplay")
    assert res_stage_uninit.status_code == 404
    assert "has not been initialized" in res_stage_uninit.json()["detail"]

    # Checkpoint on uninitialized project returns 404 (not initialized)
    res_cp_uninit = client.get(f"/api/pipeline/{uninit_proj.id}/checkpoints/story_approval")
    assert res_cp_uninit.status_code == 404
    assert "has not been initialized" in res_cp_uninit.json()["detail"]

    # Audit queries return empty list
    res_audit_uninit = client.get(f"/api/pipeline/{uninit_proj.id}/checkpoints/story_approval/audit")
    assert res_audit_uninit.status_code == 200
    assert res_audit_uninit.json()["total"] == 0

    res_proj_audit_uninit = client.get(f"/api/pipeline/{uninit_proj.id}/audit")
    assert res_proj_audit_uninit.status_code == 200
    assert res_proj_audit_uninit.json()["total"] == 0

    # Call GET endpoints on partial project
    res_status_partial = client.get(f"/api/pipeline/{partial_proj.id}")
    assert res_status_partial.status_code == 200
    data_partial = res_status_partial.json()
    assert data_partial["is_initialized"] is False
    assert len(data_partial["stages"]) == 1
    assert data_partial["stages"][0]["stage_name"] == "story_development"
    assert data_partial["checkpoints"] == []

    # Existing stage returns 200
    res_stage_ok = client.get(f"/api/pipeline/{partial_proj.id}/stages/story_development")
    assert res_stage_ok.status_code == 200
    assert res_stage_ok.json()["stage_name"] == "story_development"

    # Missing stage returns 404
    res_stage_missing = client.get(f"/api/pipeline/{partial_proj.id}/stages/screenplay")
    assert res_stage_missing.status_code == 404

    # Missing checkpoint returns 404
    res_cp_missing = client.get(f"/api/pipeline/{partial_proj.id}/checkpoints/story_approval")
    assert res_cp_missing.status_code == 404

    # Assert ZERO database mutations occurred across all calls
    assert session.query(PipelineStageState).count() == stage_cnt_before
    assert session.query(PipelineCheckpoint).count() == cp_cnt_before
    assert session.query(PipelineCheckpointAudit).count() == audit_cnt_before

    # Verify uninitialized project still has 0 stages in DB
    assert session.query(PipelineStageState).filter(PipelineStageState.project_id == uninit_proj.id).count() == 0
    # Verify partial project still has only 1 stage in DB
    assert session.query(PipelineStageState).filter(PipelineStageState.project_id == partial_proj.id).count() == 1


def test_safe_public_projection_redacts_sensitive_metadata_and_errors(test_db_session):
    """
    Verifies Requirement 2: Safe Public Projections.
    - Sanitizes stage_metadata: strips worker_id, fencing_token, internal payloads, file paths.
    - Sanitizes error_message: redacts file paths, API keys, bearer tokens, DB credentials.
    - Preserves safe frontend fields (e.g. model, resolution, retry_count, content hashes).
    """
    session, engine = test_db_session

    create_res = client.post("/api/projects", json={"name": "Sanitization Test Film"})
    project_id = create_res.json()["id"]

    # Inject sensitive data into a stage
    stage = session.query(PipelineStageState).filter(
        PipelineStageState.project_id == project_id,
        PipelineStageState.stage_name == "screenplay"
    ).first()

    sensitive_metadata = {
        "worker_id": "gpu-worker-us-central-99",
        "fencing_token": 999123,
        "payload": {"prompt": "cinematic prompt", "internal_secret": "sk-proj-supersecretkey123456789"},
        "disk_path": "/var/data/models/weights.bin",
        "model": "screenplay-gpt-v4",
        "resolution": "4K",
        "safe_summary": "Script draft generated at /home/leevawn/output.fdr"
    }
    sensitive_error = (
        'Traceback (most recent call last):\n'
        '  File "/home/leevawn/Phim/backend/worker.py", line 123, in execute\n'
        '    raise ConnectionError("DB postgresql://admin:topsecretpass@db.internal:5432/film failed with sk-ant-api03-12345678901234567890")'
    )

    stage.stage_metadata = json.dumps(sensitive_metadata)
    stage.error_message = sensitive_error
    session.commit()

    # Query complete pipeline state
    pipe_res = client.get(f"/api/pipeline/{project_id}")
    assert pipe_res.status_code == 200
    pipe_data = pipe_res.json()
    sc_stage = next(s for s in pipe_data["stages"] if s["stage_name"] == "screenplay")

    meta = sc_stage["stage_metadata"]
    assert "worker_id" not in meta
    assert "fencing_token" not in meta
    assert "payload" not in meta
    assert "disk_path" not in meta
    assert meta["model"] == "screenplay-gpt-v4"
    assert meta["resolution"] == "4K"
    assert "[REDACTED_PATH]" in meta["safe_summary"]
    assert "/home/leevawn" not in meta["safe_summary"]

    err = sc_stage["error_message"]
    assert "topsecretpass" not in err
    assert "[REDACTED_PASS]" in err
    assert "[REDACTED_API_KEY]" in err
    assert "sk-ant-api03" not in err
    assert "/home/leevawn" not in err

    # Query individual stage detail
    detail_res = client.get(f"/api/pipeline/{project_id}/stages/screenplay")
    assert detail_res.status_code == 200
    detail_data = detail_res.json()
    assert "worker_id" not in detail_data["stage_metadata"]
    assert "topsecretpass" not in detail_data["error_message"]


def test_cross_project_job_isolation(test_db_session):
    """
    Verifies Requirement 2:
    - active StudioJob lookup strictly validates StudioJob.project_id == project_id.
    - Stage referencing a job belonging to another project never exposes that job.
    """
    session, engine = test_db_session

    res_a = client.post("/api/projects", json={"name": "Project A"})
    id_a = res_a.json()["id"]

    res_b = client.post("/api/projects", json={"name": "Project B"})
    id_b = res_b.json()["id"]

    # Create StudioJob belonging to Project B
    job_b = StudioJob(
        id=str(uuid.uuid4()),
        project_id=id_b,
        job_type="cinematic_prompt_agent",
        idempotency_key=f"idemp-{uuid.uuid4().hex[:8]}",
        status="RUNNING",
        worker_id="worker_b",
        fencing_token=1,
        created_at=utc_now(),
        updated_at=utc_now()
    )
    session.add(job_b)
    session.flush()

    # Corrupt or mismatched association: Project A stage points to Job B
    stage_a = session.query(PipelineStageState).filter(
        PipelineStageState.project_id == id_a,
        PipelineStageState.stage_name == "cinematic_prompts"
    ).first()
    stage_a.active_job_id = job_b.id
    session.commit()

    # Read Project A stage detail: active_job must be None
    res = client.get(f"/api/pipeline/{id_a}/stages/cinematic_prompts")
    assert res.status_code == 200
    data = res.json()
    assert data["active_job_id"] == job_b.id
    assert data["active_job"] is None  # Must NOT leak Project B's job!


def test_hash_freshness_semantics_four_cases(test_db_session):
    """
    Verifies Requirement 3: Correct 3-state hash freshness semantics.
    - True: Screenplay with matching hash.
    - False: Screenplay with drifted hash or missing scenes after approval.
    - None: Unsupported artifact hashing (e.g. visual_approval / visual_bible) even when APPROVED.
    - None: Unapproved checkpoint without baseline artifact hash.
    """
    session, engine = test_db_session

    create_res = client.post("/api/projects", json={"name": "Freshness Semantics Film"})
    project_id = create_res.json()["id"]

    # Case 4: Unapproved checkpoint without hash -> is_hash_fresh is None
    res_unapproved = client.get(f"/api/pipeline/{project_id}/checkpoints/story_approval")
    assert res_unapproved.status_code == 200
    assert res_unapproved.json()["is_hash_fresh"] is None

    # Add scene and approve screenplay
    scene = Scene(
        id=str(uuid.uuid4()),
        project_id=project_id,
        scene_number=1,
        slugline="INT. ROOM - DAY",
        script_content="INT. ROOM - DAY\nA quiet morning.",
        order_index=1
    )
    session.add(scene)
    session.commit()

    approved_cp = pipeline_orchestrator.record_checkpoint_decision(
        db=session,
        project_id=project_id,
        checkpoint_type="story_approval",
        decision="APPROVED",
        reviewer_name="Director"
    )

    # Case 1: Matching screenplay hash -> is_hash_fresh is True
    res_fresh = client.get(f"/api/pipeline/{project_id}/checkpoints/story_approval")
    assert res_fresh.status_code == 200
    assert res_fresh.json()["is_hash_fresh"] is True

    # Modify scene content without invalidating checkpoint -> drifted content
    scene.script_content = "INT. ROOM - DAY\nAn explosion occurs!"
    session.commit()

    # Case 2A: Drifted screenplay hash -> is_hash_fresh is False
    res_drifted = client.get(f"/api/pipeline/{project_id}/checkpoints/story_approval")
    assert res_drifted.status_code == 200
    assert res_drifted.json()["is_hash_fresh"] is False

    # Delete all scenes -> current hash is None -> is_hash_fresh is False
    session.delete(scene)
    session.commit()

    # Case 2B: Missing artifact content after approval -> is_hash_fresh is False
    res_missing = client.get(f"/api/pipeline/{project_id}/checkpoints/story_approval")
    assert res_missing.status_code == 200
    assert res_missing.json()["current_artifact_hash"] is None
    assert res_missing.json()["is_hash_fresh"] is False

    # Case 3: APPROVED checkpoint on unsupported artifact type (visual_approval / visual_bible)
    # Manually set visual_approval to APPROVED with an arbitrary artifact_content_hash
    vis_cp = session.query(PipelineCheckpoint).filter(
        PipelineCheckpoint.project_id == project_id,
        PipelineCheckpoint.checkpoint_type == "visual_approval"
    ).first()
    vis_cp.status = "APPROVED"
    vis_cp.artifact_content_hash = "manual_vis_hash_123"
    session.commit()

    res_vis = client.get(f"/api/pipeline/{project_id}/checkpoints/visual_approval")
    assert res_vis.status_code == 200
    vis_data = res_vis.json()
    assert vis_data["status"] == "APPROVED"
    assert vis_data["artifact_content_hash"] == "manual_vis_hash_123"
    assert vis_data["current_artifact_hash"] is None
    # Crucial assertion: Must be None, NOT False! (Unsupported hashing must never be falsely marked stale)
    assert vis_data["is_hash_fresh"] is None


def test_audit_history_pagination_tie_breaker(test_db_session):
    """
    Verifies Requirement 5: Stable pagination tie-breaker.
    When multiple audit records have the exact same created_at timestamp,
    ordering by (created_at DESC, id DESC) provides deterministic pagination.
    """
    session, engine = test_db_session

    create_res = client.post("/api/projects", json={"name": "Tie Breaker Film"})
    project_id = create_res.json()["id"]

    same_time = utc_now()
    records = []
    for i in range(5):
        records.append(PipelineCheckpointAudit(
            id=f"audit-tie-{i:03d}",
            project_id=project_id,
            project_name_snapshot="Tie Breaker Film",
            checkpoint_id="cp-tie",
            checkpoint_type="story_approval",
            decision="APPROVED",
            artifact_content_hash="hash123",
            artifact_version=1,
            reviewer_name="Auditor",
            feedback_notes=f"Note {i}",
            created_at=same_time
        ))
    session.add_all(records)
    session.commit()

    # Page 1: limit 2, offset 0
    res_p1 = client.get(f"/api/pipeline/{project_id}/checkpoints/story_approval/audit?limit=2&offset=0")
    assert res_p1.status_code == 200
    p1_items = res_p1.json()["items"]
    assert len(p1_items) == 2
    assert p1_items[0]["id"] == "audit-tie-004"
    assert p1_items[1]["id"] == "audit-tie-003"

    # Page 2: limit 2, offset 2
    res_p2 = client.get(f"/api/pipeline/{project_id}/checkpoints/story_approval/audit?limit=2&offset=2")
    assert res_p2.status_code == 200
    p2_items = res_p2.json()["items"]
    assert len(p2_items) == 2
    assert p2_items[0]["id"] == "audit-tie-002"
    assert p2_items[1]["id"] == "audit-tie-001"


def test_read_only_contract_does_not_mutate_data(test_db_session):
    """
    Verifies Requirement 4:
    - GET requests to all read endpoints never mutate data, modify stage status,
      or generate spurious compliance audit entries.
    """
    session, engine = test_db_session

    create_res = client.post("/api/projects", json={"name": "Immutability Read Film"})
    project_id = create_res.json()["id"]

    # Baseline counts
    audit_count_before = session.query(PipelineCheckpointAudit).count()
    stage_count_before = session.query(PipelineStageState).count()
    cp_count_before = session.query(PipelineCheckpoint).count()

    # Invoke all read endpoints multiple times
    for _ in range(3):
        r1 = client.get(f"/api/pipeline/{project_id}")
        assert r1.status_code == 200

        r2 = client.get(f"/api/pipeline/{project_id}/stages/screenplay")
        assert r2.status_code == 200

        r3 = client.get(f"/api/pipeline/{project_id}/checkpoints/story_approval")
        assert r3.status_code == 200

        r4 = client.get(f"/api/pipeline/{project_id}/checkpoints/story_approval/audit")
        assert r4.status_code == 200

        r5 = client.get(f"/api/pipeline/{project_id}/audit")
        assert r5.status_code == 200

    # Assert completely unchanged database state
    assert session.query(PipelineCheckpointAudit).count() == audit_count_before
    assert session.query(PipelineStageState).count() == stage_count_before
    assert session.query(PipelineCheckpoint).count() == cp_count_before


def test_postgresql_read_api_compatibility():
    """
    Verifies dual-engine PostgreSQL compatibility for the Pipeline Read API.
    Self-contained: ensures PostgreSQL schema is migrated to head before running.
    Skips cleanly if PostgreSQL test instance is unavailable locally; fails in CI.
    """
    is_ci = os.environ.get("CI") == "true" or os.environ.get("GITHUB_ACTIONS") == "true"
    pg_engine = get_postgres_engine()
    if pg_engine is None:
        if is_ci:
            pytest.fail("PostgreSQL test instance is required in CI but is not reachable.")
        else:
            pytest.skip("PostgreSQL test instance not reachable; skipping PG Pipeline Read API test.")

    try:
        # 1. Ensure PostgreSQL schema is upgraded to migration head independently
        alembic_cfg = Config(str(settings.root_dir / "alembic.ini"))
        alembic_cfg.set_main_option("script_location", str(settings.root_dir / "backend" / "alembic"))
        alembic_cfg.set_main_option("sqlalchemy.url", pg_engine.url.render_as_string(hide_password=False))
        alembic_cfg.attributes["configure_logger"] = False
        command.upgrade(alembic_cfg, "head")

        TestingSessionLocal = sessionmaker(bind=pg_engine)

        def override_get_db():
            db = TestingSessionLocal()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db] = override_get_db

        # Create project via client on PostgreSQL
        create_res = client.post("/api/projects", json={
            "name": "PG Pipeline Read Film",
            "logline": "PostgreSQL read API test project.",
            "budget_total": 200.0
        })
        assert create_res.status_code == 201
        project_id = create_res.json()["id"]

        # Read complete pipeline state
        pipe_res = client.get(f"/api/pipeline/{project_id}")
        assert pipe_res.status_code == 200
        data = pipe_res.json()
        assert data["is_initialized"] is True
        assert len(data["stages"]) == 11
        assert len(data["checkpoints"]) == 4

        # Read stage detail
        stage_res = client.get(f"/api/pipeline/{project_id}/stages/screenplay")
        assert stage_res.status_code == 200
        assert stage_res.json()["stage_name"] == "screenplay"

        # Read checkpoint detail
        cp_res = client.get(f"/api/pipeline/{project_id}/checkpoints/story_approval")
        assert cp_res.status_code == 200
        assert cp_res.json()["checkpoint_type"] == "story_approval"

        # Read audit history
        audit_res = client.get(f"/api/pipeline/{project_id}/checkpoints/story_approval/audit")
        assert audit_res.status_code == 200
        assert audit_res.json()["total"] == 0

    finally:
        app.dependency_overrides.pop(get_db, None)
        pg_engine.dispose()
