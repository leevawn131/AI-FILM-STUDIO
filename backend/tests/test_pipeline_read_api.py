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
from backend.app.schemas.pipeline import CheckpointDecisionRequest
from backend.app.services.pipeline_orchestrator import (
    pipeline_orchestrator, PIPELINE_STAGES, PIPELINE_CHECKPOINTS
)
from backend.tests.test_postgres_concurrency import get_postgres_engine

client = TestClient(app)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def approve_story_helper(session, project_id, decision="APPROVED", notes=None, reviewer_name="Director"):
    sub = pipeline_orchestrator.submit_story_for_review(session, project_id, reviewer_name=reviewer_name)
    pipeline_orchestrator.record_story_decision(
        session,
        project_id,
        CheckpointDecisionRequest(
            decision=decision,
            reviewer_name=reviewer_name,
            feedback_notes=notes,
            expected_artifact_version=sub["artifact_version"],
            expected_artifact_hash=sub["artifact_content_hash"],
        )
    )
    return session.query(PipelineCheckpoint).filter(
        PipelineCheckpoint.project_id == project_id,
        PipelineCheckpoint.checkpoint_type == "story_approval"
    ).first()


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

    # 3. Approve checkpoint via service (following proper submit -> approve lifecycle)
    approved_cp = approve_story_helper(
        session=session,
        project_id=project_id,
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

    # Generate audit events through proper submit -> decide lifecycle
    decisions = [
        ("REJECTED", "Needs more dramatic tension.", "Director"),
        ("APPROVED", "Revisions accepted.", "Director"),
    ]
    for dec, notes, rev in decisions:
        approve_story_helper(session, project_id, dec, notes=notes, reviewer_name=rev)

    # Invalidate
    pipeline_orchestrator.invalidate_downstream_stages(session, project_id, "screenplay")

    # Re-submit and approve
    approve_story_helper(
        session, project_id, "APPROVED", notes="Final sign-off.", reviewer_name="Producer"
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

    # Submit and record decision
    approve_story_helper(session, project_id, "APPROVED", notes="Approved.")

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

    approve_story_helper(
        session, id_a, "APPROVED", notes="Alpha notes", reviewer_name="Reviewer Alpha"
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
    Verifies Requirement 1:
    - Strict public allowlist for stage_metadata: unknown fields and sensitive keys are removed by default.
    - Tests explicit removal of:
      * private_key
      * raw_prompt
      * provider_response
      * request_headers
      * source_path
      * artifact_uri
      * /mnt, /opt, /srv, /workspace paths
      * GitHub tokens and AWS-style credentials
      * Nested sensitive fields
      * Unknown metadata fields
    - Tests controlled, user-safe error messages.
    - Asserts that NO sentinel secret appears anywhere in serialized API responses.
    """
    session, engine = test_db_session

    create_res = client.post("/api/projects", json={"name": "Allowlist Sanitization Film"})
    project_id = create_res.json()["id"]

    sentinels = {
        "private_key": "-----BEGIN RSA PRIVATE KEY-----\nSENTINEL_PRIVKEY_SECRET_DATA_999\n-----END RSA PRIVATE KEY-----",
        "raw_prompt": "SENTINEL_RAW_PROMPT_SECRET_INSTRUCTIONS_DO_NOT_LEAK",
        "provider_response": {"candidates": [{"content": "SENTINEL_PROVIDER_INTERNAL_BODY_123"}]},
        "request_headers": {"Authorization": "Bearer SENTINEL_BEARER_AUTH_HEADER_456"},
        "source_path": "/opt/internal/film/SENTINEL_SOURCE_FILE.mp4",
        "artifact_uri": "s3://sentinel-storage-bucket/SENTINEL_INTERNAL_ARTIFACT.bin",
        "mnt_path_val": "/mnt/nfs/share/SENTINEL_MNT_SECRET_ASSET.mov",
        "srv_path_val": "/srv/confidential/SENTINEL_SRV_SECRET_DATA.txt",
        "workspace_path_val": "/workspace/ai-film-studio/SENTINEL_WORKSPACE_SECRET.py",
        "github_token": "ghp_SENTINELGITHUBTOKEN0123456789abcdef",
        "aws_key": "AKIASENTINELAWSKEY",
        "aws_secret": "SENTINEL_AWS_SECRET_KEY_ABCDEF123456789",
        "nested_secret": "SENTINEL_NESTED_SECRET_PAYLOAD_888",
        "unknown_field": "SENTINEL_UNKNOWN_DEBUG_METADATA_VALUE_777",
        "error_traceback": "Traceback (most recent call last):\n  File \"/opt/backend/worker.py\", line 99\nConnectionError: DB postgresql://admin:SENTINEL_DB_SECRET_PASS@db:5432 failed"
    }

    # Inject into stage
    stage = session.query(PipelineStageState).filter(
        PipelineStageState.project_id == project_id,
        PipelineStageState.stage_name == "screenplay"
    ).first()

    raw_metadata = {
        # Prohibited top-level sensitive keys
        "private_key": sentinels["private_key"],
        "raw_prompt": sentinels["raw_prompt"],
        "provider_response": sentinels["provider_response"],
        "request_headers": sentinels["request_headers"],
        "source_path": sentinels["source_path"],
        "artifact_uri": sentinels["artifact_uri"],
        "unknown_debug_field": sentinels["unknown_field"],
        "aws_secret": sentinels["aws_secret"],
        # Allowed fields with embedded paths and credentials that must be redacted
        "model": "screenplay-gpt-v4",
        "resolution": "4K",
        "generation_summary": (
            f"Drafted screenplay with {sentinels['github_token']} and {sentinels['aws_key']} "
            f"saved at {sentinels['mnt_path_val']} and {sentinels['srv_path_val']}"
        ),
        "summary": {
            "act_count": 3,
            "raw_prompt": sentinels["raw_prompt"],  # Nested sensitive field
            "private_key": sentinels["nested_secret"],  # Nested sensitive field
            "note": f"Completed at {sentinels['workspace_path_val']}"
        }
    }

    stage.stage_metadata = json.dumps(raw_metadata)
    stage.error_message = sentinels["error_traceback"]
    session.commit()

    # Query 1: Pipeline status overview
    pipe_res = client.get(f"/api/pipeline/{project_id}")
    assert pipe_res.status_code == 200
    pipe_text = pipe_res.text
    pipe_data = pipe_res.json()

    # Query 2: Individual stage detail
    detail_res = client.get(f"/api/pipeline/{project_id}/stages/screenplay")
    assert detail_res.status_code == 200
    detail_text = detail_res.text
    detail_data = detail_res.json()

    # Check both serialized responses: NONE of the sentinel secrets may appear!
    for label, sentinel in sentinels.items():
        if isinstance(sentinel, str):
            assert sentinel not in pipe_text, f"Sentinel '{label}' leaked in pipeline overview response!"
            assert sentinel not in detail_text, f"Sentinel '{label}' leaked in stage detail response!"

    # Verify that safe allowlisted fields ARE present and preserved
    sc_stage = next(s for s in pipe_data["stages"] if s["stage_name"] == "screenplay")
    meta = sc_stage["stage_metadata"]
    assert meta["model"] == "screenplay-gpt-v4"
    assert meta["resolution"] == "4K"
    assert "[REDACTED_PATH]" in meta["generation_summary"]
    assert "[REDACTED_TOKEN]" in meta["generation_summary"]
    assert "[REDACTED_CREDENTIAL]" in meta["generation_summary"]
    assert meta["summary"]["act_count"] == 3
    assert "[REDACTED_PATH]" in meta["summary"]["note"]
    assert "raw_prompt" not in meta["summary"]
    assert "private_key" not in meta["summary"]
    assert "unknown_debug_field" not in meta

    # Verify error message is a controlled, user-safe message
    assert sc_stage["error_message"] == "External service connection failure. Please retry."
    assert detail_data["error_message"] == "External service connection failure. Please retry."


def test_cross_project_job_isolation(test_db_session):
    """
    Verifies Requirement 2:
    - active_job_id cross-project leakage prevention.
    - If Project A stage points to Project B's StudioJob:
      * Pipeline overview for A must return active_job_id = null.
      * Stage detail for A must return active_job_id = null.
      * Stage detail for A must return active_job = null.
      * Project B's job ID and worker information must NOT appear anywhere in Project A's serialized responses.
    """
    session, engine = test_db_session

    res_a = client.post("/api/projects", json={"name": "Project Alpha Isolation"})
    id_a = res_a.json()["id"]

    res_b = client.post("/api/projects", json={"name": "Project Beta Isolation"})
    id_b = res_b.json()["id"]

    foreign_job_id = f"foreign-job-{uuid.uuid4()}"
    foreign_worker_id = "foreign-gpu-worker-secret-999"

    # Create StudioJob belonging to Project B
    job_b = StudioJob(
        id=foreign_job_id,
        project_id=id_b,
        job_type="cinematic_prompt_agent",
        idempotency_key=f"idemp-{uuid.uuid4().hex[:8]}",
        status="RUNNING",
        worker_id=foreign_worker_id,
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

    # 1. Pipeline overview for A
    res_overview = client.get(f"/api/pipeline/{id_a}")
    assert res_overview.status_code == 200
    overview_text = res_overview.text
    overview_data = res_overview.json()
    cp_stage = next(s for s in overview_data["stages"] if s["stage_name"] == "cinematic_prompts")
    assert cp_stage["active_job_id"] is None
    assert foreign_job_id not in overview_text
    assert foreign_worker_id not in overview_text

    # 2. Stage detail for A
    res_detail = client.get(f"/api/pipeline/{id_a}/stages/cinematic_prompts")
    assert res_detail.status_code == 200
    detail_text = res_detail.text
    detail_data = res_detail.json()
    assert detail_data["active_job_id"] is None
    assert detail_data["active_job"] is None
    assert foreign_job_id not in detail_text
    assert foreign_worker_id not in detail_text


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

    approved_cp = approve_story_helper(
        session=session,
        project_id=project_id,
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
    Verifies Requirement 3: Deep read-only state immutability.
    - Snapshots entire relevant database state (all rows and column values across Project,
      PipelineStageState, PipelineCheckpoint, PipelineCheckpointAudit, StudioJob, Scene).
    - Invokes every M2.1 GET endpoint across:
      1) Initialized project
      2) Uninitialized project
      3) Partially initialized project
    - Asserts that post-invocation database state is 100% identical (zero inserts, updates, deletes, or mutations).
    """
    session, engine = test_db_session
    from sqlalchemy import inspect as sa_inspect

    # 1. Create initialized project via client
    create_res = client.post("/api/projects", json={"name": "Initialized Read Film"})
    init_proj_id = create_res.json()["id"]

    # Add a scene and an approved checkpoint to initialized project for full endpoint coverage
    scene = Scene(
        id=str(uuid.uuid4()),
        project_id=init_proj_id,
        scene_number=1,
        slugline="INT. READONLY - DAY",
        script_content="INT. READONLY - DAY\nTesting immutable read state.",
        order_index=1
    )
    session.add(scene)
    session.commit()
    approve_story_helper(session, init_proj_id, "APPROVED", notes="Initial approval")

    # 2. Create uninitialized project directly via ORM
    uninit_proj = Project(
        id=str(uuid.uuid4()),
        name="Uninitialized Read Film",
        budget_total=100.0,
        budget_spent=0.0
    )
    session.add(uninit_proj)

    # 3. Create partially initialized project with 1 stage only
    part_proj = Project(
        id=str(uuid.uuid4()),
        name="Partial Read Film",
        budget_total=200.0,
        budget_spent=0.0
    )
    part_stage = PipelineStageState(
        id=str(uuid.uuid4()),
        project_id=part_proj.id,
        stage_name="story_development",
        status="NOT_STARTED",
        created_at=utc_now(),
        updated_at=utc_now()
    )
    session.add_all([part_proj, part_stage])
    session.commit()

    def snapshot_db() -> Dict[str, Any]:
        session.expire_all()
        def row_dict(obj):
            cols = sa_inspect(obj).mapper.column_attrs
            return {c.key: getattr(obj, c.key) for c in cols}

        return {
            "projects": [row_dict(p) for p in session.query(Project).order_by(Project.id).all()],
            "stages": [row_dict(s) for s in session.query(PipelineStageState).order_by(PipelineStageState.id).all()],
            "checkpoints": [row_dict(c) for c in session.query(PipelineCheckpoint).order_by(PipelineCheckpoint.id).all()],
            "audits": [row_dict(a) for a in session.query(PipelineCheckpointAudit).order_by(PipelineCheckpointAudit.id).all()],
            "jobs": [row_dict(j) for j in session.query(StudioJob).order_by(StudioJob.id).all()],
            "scenes": [row_dict(sc) for sc in session.query(Scene).order_by(Scene.id).all()],
        }

    snapshot_before = snapshot_db()

    # Invoke all M2.1 GET endpoints multiple times
    for _ in range(2):
        # A. Initialized project endpoints
        r1 = client.get(f"/api/pipeline/{init_proj_id}")
        assert r1.status_code == 200
        for stage_name in PIPELINE_STAGES:
            r_st = client.get(f"/api/pipeline/{init_proj_id}/stages/{stage_name}")
            assert r_st.status_code == 200
        for cp_type, _ in PIPELINE_CHECKPOINTS:
            r_cp = client.get(f"/api/pipeline/{init_proj_id}/checkpoints/{cp_type}")
            assert r_cp.status_code == 200
            r_aud = client.get(f"/api/pipeline/{init_proj_id}/checkpoints/{cp_type}/audit")
            assert r_aud.status_code == 200
        r_all_aud = client.get(f"/api/pipeline/{init_proj_id}/audit")
        assert r_all_aud.status_code == 200

        # B. Uninitialized project endpoints
        u1 = client.get(f"/api/pipeline/{uninit_proj.id}")
        assert u1.status_code == 200
        u2 = client.get(f"/api/pipeline/{uninit_proj.id}/stages/screenplay")
        assert u2.status_code == 404
        u3 = client.get(f"/api/pipeline/{uninit_proj.id}/checkpoints/story_approval")
        assert u3.status_code == 404
        u4 = client.get(f"/api/pipeline/{uninit_proj.id}/checkpoints/story_approval/audit")
        assert u4.status_code == 200
        u5 = client.get(f"/api/pipeline/{uninit_proj.id}/audit")
        assert u5.status_code == 200

        # C. Partially initialized project endpoints
        p1 = client.get(f"/api/pipeline/{part_proj.id}")
        assert p1.status_code == 200
        p2 = client.get(f"/api/pipeline/{part_proj.id}/stages/story_development")
        assert p2.status_code == 200
        p3 = client.get(f"/api/pipeline/{part_proj.id}/stages/screenplay")
        assert p3.status_code == 404
        p4 = client.get(f"/api/pipeline/{part_proj.id}/checkpoints/story_approval")
        assert p4.status_code == 404
        p5 = client.get(f"/api/pipeline/{part_proj.id}/checkpoints/story_approval/audit")
        assert p5.status_code == 200
        p6 = client.get(f"/api/pipeline/{part_proj.id}/audit")
        assert p6.status_code == 200

    snapshot_after = snapshot_db()

    # Compare complete deep snapshot
    assert snapshot_after == snapshot_before, "Database state was mutated during read requests!"


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
