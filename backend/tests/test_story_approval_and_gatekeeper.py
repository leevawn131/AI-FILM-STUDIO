"""
Comprehensive Test Suite for S4 M2.2–M2.4:
- Story Approval Lifecycle (Review, Submit, Decide)
- Screenplay Mutations & Approval Invalidation (Atomic Transactions)
- Server-Side Gatekeeper Enforcement (Fail-closed, Exact Hash Matching)
- Optimistic Concurrency & TOCTOU Protection
- State Transition Engine & Audit History
"""

import os
import json
import uuid
from datetime import datetime, timezone, timedelta
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session

from unittest.mock import patch, MagicMock
from backend.app.main import app
from backend.app.database import get_db, run_migrations
from backend.app.services.gemini_service import gemini_service
from backend.app.models.entities import (
    Project, Scene, Shot, PipelineStageState, PipelineCheckpoint, PipelineCheckpointAudit,
    StudioJob, CostRecord, Character, Location
)
from backend.app.services.pipeline_orchestrator import (
    pipeline_orchestrator, PipelineOrchestrator, acquire_project_write_lock
)
from backend.app.services.gatekeeper import gatekeeper
from backend.app.services.job_manager import JobManager, JobOwnershipLostError
from backend.app.services.cost_controller import CostController
from backend.app.agents.screenwriter_agent import screenwriter_agent
from backend.tests.test_postgres_concurrency import get_postgres_engine

client = TestClient(app)


@pytest.fixture
def test_db_session(tmp_path):
    """Provides an isolated SQLite database and test DB session."""
    db_file = tmp_path / "approval_gatekeeper_test.db"
    db_url = f"sqlite:///{db_file}"
    engine = create_engine(db_url, connect_args={"timeout": 15.0})
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


def test_story_approval_lifecycle_submit_and_decide(test_db_session):
    """
    Acceptance Criteria 1-12:
    - Initial checkpoint is LOCKED.
    - Submit fails on empty screenplay.
    - Valid submit transitions checkpoint and screenplay stage to WAITING_FOR_APPROVAL.
    - Audit SUBMITTED_FOR_REVIEW is logged.
    - Idempotent submit with same hash creates no duplicate audit.
    - APPROVED transitions checkpoint to APPROVED, screenplay stage to COMPLETED, project to character_design.
    - Idempotent approve with same hash creates no duplicate audit.
    - REJECTED transitions checkpoint to REJECTED, screenplay stage to WAITING_FOR_USER_INPUT.
    - REQUEST_CHANGES records distinct audit decision.
    - Decisions outside WAITING_FOR_APPROVAL are rejected.
    """
    session, engine = test_db_session

    # 1. Create project (initialized automatically)
    create_res = client.post("/api/projects", json={"name": "Lifecycle Film"})
    assert create_res.status_code == 201
    project_id = create_res.json()["id"]

    # Criteria 1: Checkpoint is initially LOCKED
    cp_res = client.get(f"/api/pipeline/{project_id}/checkpoints/story_approval")
    assert cp_res.status_code == 200
    assert cp_res.json()["status"] == "LOCKED"

    # Criteria 2: Submit with empty screenplay must fail (400)
    sub_empty = client.post(f"/api/pipeline/{project_id}/checkpoints/story_approval/submit")
    assert sub_empty.status_code == 400
    assert "empty screenplay" in sub_empty.json()["detail"].lower()

    # Criteria 12: Decision outside WAITING_FOR_APPROVAL is rejected (400)
    dec_locked = client.post(f"/api/pipeline/{project_id}/checkpoints/story_approval/decision", json={
        "decision": "APPROVED",
        "expected_artifact_version": 1,
        "expected_artifact_hash": "a" * 64,
        "reviewer_name": "Director"
    })
    assert dec_locked.status_code == 400
    assert "waiting_for_approval" in dec_locked.json()["detail"].lower()

    # 2. Add valid scene
    scene = Scene(
        id=str(uuid.uuid4()),
        project_id=project_id,
        scene_number=1,
        slugline="INT. CONTROL ROOM - NIGHT",
        title="Opening Sequence",
        summary="A technician spots a glitch.",
        script_content="INT. CONTROL ROOM - NIGHT\n\nMonitors flicker violently.",
        estimated_duration=25,
        order_index=1
    )
    session.add(scene)
    session.commit()

    # Criteria 3 & 4: Submit valid screenplay -> WAITING_FOR_APPROVAL, audit SUBMITTED_FOR_REVIEW
    sub_res = client.post(f"/api/pipeline/{project_id}/checkpoints/story_approval/submit")
    assert sub_res.status_code == 200
    sub_data = sub_res.json()
    assert sub_data["status"] == "WAITING_FOR_APPROVAL"
    assert len(sub_data["artifact_content_hash"]) == 64
    submitted_hash = sub_data["artifact_content_hash"]
    submitted_version = sub_data["artifact_version"]

    # Check stage updated to WAITING_FOR_APPROVAL
    stage_res = client.get(f"/api/pipeline/{project_id}/stages/screenplay")
    assert stage_res.status_code == 200
    assert stage_res.json()["status"] == "WAITING_FOR_APPROVAL"

    # Verify audit SUBMITTED_FOR_REVIEW
    audit_res = client.get(f"/api/pipeline/{project_id}/checkpoints/story_approval/audit")
    assert audit_res.status_code == 200
    audit_items = audit_res.json()["items"]
    assert len(audit_items) == 1
    assert audit_items[0]["decision"] == "SUBMITTED_FOR_REVIEW"
    assert audit_items[0]["artifact_content_hash"] == submitted_hash

    # Criteria 10: Idempotent re-submit with same content does not duplicate audit
    sub_idem = client.post(f"/api/pipeline/{project_id}/checkpoints/story_approval/submit")
    assert sub_idem.status_code == 200
    audit_res2 = client.get(f"/api/pipeline/{project_id}/checkpoints/story_approval/audit")
    assert len(audit_res2.json()["items"]) == 1

    # Criteria 5, 6, 7: APPROVED transitions checkpoint, stage, and project current stage
    dec_app = client.post(f"/api/pipeline/{project_id}/checkpoints/story_approval/decision", json={
        "decision": "APPROVED",
        "expected_artifact_version": submitted_version,
        "expected_artifact_hash": submitted_hash,
        "reviewer_name": "Executive Producer",
        "feedback_notes": "Masterpiece draft."
    })
    assert dec_app.status_code == 200
    app_data = dec_app.json()
    assert app_data["status"] == "APPROVED"
    assert app_data["decision"] == "APPROVED"

    # Screenplay stage is COMPLETED
    stage_comp = client.get(f"/api/pipeline/{project_id}/stages/screenplay")
    assert stage_comp.json()["status"] == "COMPLETED"
    assert stage_comp.json()["content_hash"] == submitted_hash

    # Project current stage advanced to character_design
    proj_res = client.get(f"/api/pipeline/{project_id}")
    assert proj_res.json()["current_pipeline_stage"] == "character_design"
    assert proj_res.json()["pipeline_overall_status"] == "in_progress"

    # Criteria 11: Idempotent re-approve does not create duplicate audit
    dec_idem = client.post(f"/api/pipeline/{project_id}/checkpoints/story_approval/decision", json={
        "decision": "APPROVED",
        "expected_artifact_version": submitted_version,
        "expected_artifact_hash": submitted_hash,
        "reviewer_name": "Executive Producer"
    })
    assert dec_idem.status_code == 200
    audit_res3 = client.get(f"/api/pipeline/{project_id}/checkpoints/story_approval/audit")
    # Total audits: 1 SUBMITTED_FOR_REVIEW + 1 APPROVED = 2
    assert len(audit_res3.json()["items"]) == 2
    assert audit_res3.json()["items"][0]["decision"] == "APPROVED"

    # Test REQUEST_CHANGES lifecycle on another project
    create_res_b = client.post("/api/projects", json={"name": "Revision Film"})
    proj_b_id = create_res_b.json()["id"]
    scene_b = Scene(
        id=str(uuid.uuid4()),
        project_id=proj_b_id,
        scene_number=1,
        slugline="INT. CAFE - DAY",
        script_content="INT. CAFE - DAY\nCoffee spills.",
        order_index=1
    )
    session.add(scene_b)
    session.commit()

    client.post(f"/api/pipeline/{proj_b_id}/checkpoints/story_approval/submit")
    review_b = client.get(f"/api/pipeline/{proj_b_id}/checkpoints/story_approval/review").json()

    # Criteria 8 & 9: REQUEST_CHANGES transitions checkpoint to REJECTED, stage to WAITING_FOR_USER_INPUT, audit has REQUEST_CHANGES
    req_changes_res = client.post(f"/api/pipeline/{proj_b_id}/checkpoints/story_approval/decision", json={
        "decision": "REQUEST_CHANGES",
        "expected_artifact_version": review_b["artifact_version"],
        "expected_artifact_hash": review_b["current_artifact_hash"],
        "reviewer_name": "Lead Writer",
        "feedback_notes": "Needs more dialogue in Scene 1."
    })
    assert req_changes_res.status_code == 200
    assert req_changes_res.json()["decision"] == "REQUEST_CHANGES"
    assert req_changes_res.json()["status"] == "REJECTED"

    stage_b = client.get(f"/api/pipeline/{proj_b_id}/stages/screenplay")
    assert stage_b.json()["status"] == "WAITING_FOR_USER_INPUT"

    proj_b_state = client.get(f"/api/pipeline/{proj_b_id}").json()
    assert proj_b_state["current_pipeline_stage"] == "screenplay"

    audit_b = client.get(f"/api/pipeline/{proj_b_id}/checkpoints/story_approval/audit").json()
    assert audit_b["items"][0]["decision"] == "REQUEST_CHANGES"
    assert audit_b["items"][0]["feedback_notes"] == "Needs more dialogue in Scene 1."


def test_review_endpoint_read_only_and_safe_projections(test_db_session):
    """
    Verifies Requirement 2.1:
    - Review artifact API returns ordered scenes and canonical hash.
    - GET request is strictly read-only and leaks no private keys or internal paths.
    """
    session, engine = test_db_session
    create_res = client.post("/api/projects", json={"name": "Review Data Film"})
    project_id = create_res.json()["id"]

    for i in range(1, 4):
        s = Scene(
            id=str(uuid.uuid4()),
            project_id=project_id,
            scene_number=i,
            order_index=i,
            slugline=f"INT. SET {i} - DAY",
            title=f"Scene {i}",
            summary=f"Summary for scene {i}",
            script_content=f"INT. SET {i} - DAY\nAction line {i}.",
            estimated_duration=15 * i,
            budget_limit=20.0
        )
        session.add(s)
    session.commit()

    res = client.get(f"/api/pipeline/{project_id}/checkpoints/story_approval/review")
    assert res.status_code == 200
    data = res.json()

    assert data["project_id"] == project_id
    assert data["project_name"] == "Review Data Film"
    assert data["checkpoint_status"] == "LOCKED"
    assert data["current_artifact_hash"] is not None
    assert len(data["current_artifact_hash"]) == 64
    assert len(data["scenes"]) == 3
    assert data["scenes"][0]["scene_number"] == 1
    assert data["scenes"][1]["scene_number"] == 2
    assert data["scenes"][2]["scene_number"] == 3

    # Ensure no internal or dangerous fields leaked in scene projections
    for sc in data["scenes"]:
        assert "budget_limit" not in sc
        assert "budget_spent" not in sc
        assert "file_path" not in sc
        assert "worker_id" not in sc


def test_screenplay_mutations_invalidate_approvals_transactionally(test_db_session):
    """
    Acceptance Criteria 13-17 & 21:
    - Modifying script_content invalidates approval to STALE.
    - Modifying slugline invalidates approval to STALE.
    - Adding a scene invalidates approval to STALE.
    - Deleting a scene invalidates approval to STALE.
    - Refining a scene invalidates approval to STALE.
    - Audit INVALIDATED retains the old hash and old version.
    - Non-canonical changes (e.g. budget limit) do NOT cause spurious invalidation.
    """
    session, engine = test_db_session

    create_res = client.post("/api/projects", json={"name": "Mutation Invalidation Film"})
    project_id = create_res.json()["id"]

    # 1. Add scene and approve
    scene_res = client.post("/api/scenes", json={
        "project_id": project_id,
        "scene_number": 1,
        "slugline": "INT. ARCHIVE - NIGHT",
        "title": "Scene 1",
        "script_content": "INT. ARCHIVE - NIGHT\nDust settles on files.",
        "estimated_duration": 20
    })
    assert scene_res.status_code == 201
    scene_id = scene_res.json()["id"]

    client.post(f"/api/pipeline/{project_id}/checkpoints/story_approval/submit")
    rev = client.get(f"/api/pipeline/{project_id}/checkpoints/story_approval/review").json()
    hash_v1 = rev["current_artifact_hash"]

    client.post(f"/api/pipeline/{project_id}/checkpoints/story_approval/decision", json={
        "decision": "APPROVED",
        "expected_artifact_version": 1,
        "expected_artifact_hash": hash_v1,
        "reviewer_name": "Director"
    })

    cp_app = client.get(f"/api/pipeline/{project_id}/checkpoints/story_approval").json()
    assert cp_app["status"] == "APPROVED"
    assert cp_app["artifact_version"] == 1

    # Criteria: Non-canonical change (budget_limit) does NOT invalidate approval
    client.patch(f"/api/scenes/{scene_id}", json={"budget_limit": 99.0})
    cp_still_app = client.get(f"/api/pipeline/{project_id}/checkpoints/story_approval").json()
    assert cp_still_app["status"] == "APPROVED"
    assert cp_still_app["artifact_version"] == 1

    # Criteria 13 & 21: Updating script_content invalidates approval to STALE and preserves old hash in audit
    client.patch(f"/api/scenes/{scene_id}", json={"script_content": "INT. ARCHIVE - NIGHT\nAlarm blares!"})
    cp_stale = client.get(f"/api/pipeline/{project_id}/checkpoints/story_approval").json()
    assert cp_stale["status"] == "STALE"
    assert cp_stale["artifact_version"] == 2

    audits = client.get(f"/api/pipeline/{project_id}/checkpoints/story_approval/audit").json()["items"]
    inv_audit = audits[0]
    assert inv_audit["decision"] == "INVALIDATED"
    assert inv_audit["artifact_content_hash"] == hash_v1
    assert inv_audit["artifact_version"] == 1

    # Re-submit and approve version 2
    client.post(f"/api/pipeline/{project_id}/checkpoints/story_approval/submit")
    rev2 = client.get(f"/api/pipeline/{project_id}/checkpoints/story_approval/review").json()
    hash_v2 = rev2["current_artifact_hash"]
    client.post(f"/api/pipeline/{project_id}/checkpoints/story_approval/decision", json={
        "decision": "APPROVED",
        "expected_artifact_version": 2,
        "expected_artifact_hash": hash_v2,
        "reviewer_name": "Director"
    })
    assert client.get(f"/api/pipeline/{project_id}/checkpoints/story_approval").json()["status"] == "APPROVED"

    # Criteria 14: Updating slugline invalidates approval
    client.patch(f"/api/scenes/{scene_id}", json={"slugline": "EXT. ARCHIVE COURTYARD - NIGHT"})
    assert client.get(f"/api/pipeline/{project_id}/checkpoints/story_approval").json()["status"] == "STALE"

    # Re-submit and approve version 3
    client.post(f"/api/pipeline/{project_id}/checkpoints/story_approval/submit")
    rev3 = client.get(f"/api/pipeline/{project_id}/checkpoints/story_approval/review").json()
    client.post(f"/api/pipeline/{project_id}/checkpoints/story_approval/decision", json={
        "decision": "APPROVED",
        "expected_artifact_version": 3,
        "expected_artifact_hash": rev3["current_artifact_hash"],
        "reviewer_name": "Director"
    })

    # Criteria 15: Creating a new scene invalidates approval
    s2_res = client.post("/api/scenes", json={
        "project_id": project_id,
        "scene_number": 2,
        "slugline": "INT. CORRIDOR - NIGHT",
        "script_content": "Running footsteps."
    })
    assert s2_res.status_code == 201
    s2_id = s2_res.json()["id"]
    assert client.get(f"/api/pipeline/{project_id}/checkpoints/story_approval").json()["status"] == "STALE"

    # Re-submit and approve version 4
    client.post(f"/api/pipeline/{project_id}/checkpoints/story_approval/submit")
    rev4 = client.get(f"/api/pipeline/{project_id}/checkpoints/story_approval/review").json()
    client.post(f"/api/pipeline/{project_id}/checkpoints/story_approval/decision", json={
        "decision": "APPROVED",
        "expected_artifact_version": 4,
        "expected_artifact_hash": rev4["current_artifact_hash"],
        "reviewer_name": "Director"
    })

    # Criteria 16: Deleting a scene invalidates approval
    client.delete(f"/api/scenes/{s2_id}")
    assert client.get(f"/api/pipeline/{project_id}/checkpoints/story_approval").json()["status"] == "STALE"

    # Re-submit and approve version 5
    client.post(f"/api/pipeline/{project_id}/checkpoints/story_approval/submit")
    rev5 = client.get(f"/api/pipeline/{project_id}/checkpoints/story_approval/review").json()
    client.post(f"/api/pipeline/{project_id}/checkpoints/story_approval/decision", json={
        "decision": "APPROVED",
        "expected_artifact_version": 5,
        "expected_artifact_hash": rev5["current_artifact_hash"],
        "reviewer_name": "Director"
    })

    # Criteria 17: Refining scene dialogue invalidates approval
    client.post(f"/api/scenes/{scene_id}/refine", json={"direction": "Add gritty realism"})
    assert client.get(f"/api/pipeline/{project_id}/checkpoints/story_approval").json()["status"] == "STALE"


def test_optimistic_concurrency_and_toctou_prevention(test_db_session):
    """
    Acceptance Criteria 18, 19, 20:
    - Modifying artifact while review is open causes 409 Conflict upon decision.
    - Fake hash from frontend causes 409 Conflict.
    - Outdated artifact version causes 409 Conflict.
    - TOCTOU conflict invalidates checkpoint to STALE and logs INVALIDATED audit.
    """
    session, engine = test_db_session

    create_res = client.post("/api/projects", json={"name": "TOCTOU Defense Film"})
    project_id = create_res.json()["id"]

    scene = Scene(
        id=str(uuid.uuid4()),
        project_id=project_id,
        scene_number=1,
        slugline="INT. BUNKER - NIGHT",
        script_content="INT. BUNKER - NIGHT\nA radio crackles.",
        order_index=1
    )
    session.add(scene)
    session.commit()

    # Submit for review
    client.post(f"/api/pipeline/{project_id}/checkpoints/story_approval/submit")

    # Reviewer opens review modal: receives version 1 and current hash
    rev_data = client.get(f"/api/pipeline/{project_id}/checkpoints/story_approval/review").json()
    exp_version = rev_data["artifact_version"]
    exp_hash = rev_data["current_artifact_hash"]

    # Criteria 19: Fake hash from frontend rejected with 409
    fake_hash = "f" * 64
    res_fake = client.post(f"/api/pipeline/{project_id}/checkpoints/story_approval/decision", json={
        "decision": "APPROVED",
        "expected_artifact_version": exp_version,
        "expected_artifact_hash": fake_hash,
        "reviewer_name": "Director"
    })
    assert res_fake.status_code == 409

    # Re-submit since TOCTOU guard marked it STALE
    client.post(f"/api/pipeline/{project_id}/checkpoints/story_approval/submit")
    rev_data2 = client.get(f"/api/pipeline/{project_id}/checkpoints/story_approval/review").json()
    exp_version2 = rev_data2["artifact_version"]
    exp_hash2 = rev_data2["current_artifact_hash"]

    # Criteria 20: Outdated version rejected with 409
    res_wrong_ver = client.post(f"/api/pipeline/{project_id}/checkpoints/story_approval/decision", json={
        "decision": "APPROVED",
        "expected_artifact_version": exp_version2 + 99,
        "expected_artifact_hash": exp_hash2,
        "reviewer_name": "Director"
    })
    assert res_wrong_ver.status_code == 409
    assert "version mismatch" in res_wrong_ver.json()["detail"].lower()

    # Criteria 18: Concurrently modifying screenplay in DB while review token is held
    scene_db = session.query(Scene).filter(Scene.project_id == project_id).first()
    scene_db.script_content = "INT. BUNKER - NIGHT\nConcurrently injected line!"
    session.commit()

    # Reviewer now submits decision with the previously read token (TOCTOU race)
    res_race = client.post(f"/api/pipeline/{project_id}/checkpoints/story_approval/decision", json={
        "decision": "APPROVED",
        "expected_artifact_version": exp_version2,
        "expected_artifact_hash": exp_hash2,
        "reviewer_name": "Director"
    })
    assert res_race.status_code == 409
    assert "modified since review was opened" in res_race.json()["detail"].lower()

    # Verify checkpoint became STALE and INVALIDATED audit was logged
    cp = client.get(f"/api/pipeline/{project_id}/checkpoints/story_approval").json()
    assert cp["status"] == "STALE"

    audits = client.get(f"/api/pipeline/{project_id}/checkpoints/story_approval/audit").json()["items"]
    assert audits[0]["decision"] == "INVALIDATED"


def test_server_side_gatekeeper_enforcement(test_db_session):
    """
    Acceptance Criteria 22-32:
    - Blocks storyboard generation when checkpoint LOCKED (403).
    - Blocks when WAITING_FOR_APPROVAL (403).
    - Blocks when REJECTED (403).
    - Blocks when STALE (409).
    - Allows when APPROVED and hash matches (200).
    - Blocks when screenplay edited after approval (409).
    - Blocks cross-project scene references (400).
    - Blocks manual shot creation when unapproved (403).
    - Gatekeeper does NOT mutate DB state.
    - Gatekeeper does NOT claim jobs or charge budget when blocking.
    - Unsupported artifact hash fails closed (403).
    """
    session, engine = test_db_session

    create_res = client.post("/api/projects", json={"name": "Gatekeeper Enforced Film"})
    project_id = create_res.json()["id"]

    scene = Scene(
        id=str(uuid.uuid4()),
        project_id=project_id,
        scene_number=1,
        slugline="INT. CYBERPUNK LAB - NIGHT",
        script_content="INT. CYBERPUNK LAB - NIGHT\nBlue holos float.",
        order_index=1
    )
    session.add(scene)
    session.commit()

    # Criteria 22: Blocks storyboard generation when checkpoint is LOCKED (403)
    gen_locked = client.post(f"/api/storyboard/generate/{scene.id}", json={"shot_count": 2})
    assert gen_locked.status_code == 403
    assert "approval is required" in gen_locked.json()["detail"].lower()

    # Criteria 29: Blocks manual shot creation when unapproved (403)
    man_locked = client.post("/api/storyboard", json={
        "project_id": project_id,
        "scene_id": scene.id,
        "camera_angle": "Wide Shot",
        "duration_seconds": 4.0
    })
    assert man_locked.status_code == 403

    # Criteria 30 & 31: Gatekeeper causes zero side effects when blocking
    jobs_count = session.query(StudioJob).count()
    costs_count = session.query(CostRecord).count()
    shots_count = session.query(Shot).count()
    assert jobs_count == 0
    assert costs_count == 0
    assert shots_count == 0

    # Criteria 23: Blocks when WAITING_FOR_APPROVAL (403)
    client.post(f"/api/pipeline/{project_id}/checkpoints/story_approval/submit")
    gen_waiting = client.post(f"/api/storyboard/generate/{scene.id}", json={"shot_count": 2})
    assert gen_waiting.status_code == 403
    assert "waiting_for_approval" in gen_waiting.json()["detail"].lower()

    # Criteria 24: Blocks when REJECTED (403)
    rev = client.get(f"/api/pipeline/{project_id}/checkpoints/story_approval/review").json()
    client.post(f"/api/pipeline/{project_id}/checkpoints/story_approval/decision", json={
        "decision": "REJECTED",
        "expected_artifact_version": rev["artifact_version"],
        "expected_artifact_hash": rev["current_artifact_hash"],
        "reviewer_name": "Director"
    })
    gen_rej = client.post(f"/api/storyboard/generate/{scene.id}", json={"shot_count": 2})
    assert gen_rej.status_code == 403
    assert "rejected" in gen_rej.json()["detail"].lower()

    # Re-submit and approve
    client.post(f"/api/pipeline/{project_id}/checkpoints/story_approval/submit")
    rev2 = client.get(f"/api/pipeline/{project_id}/checkpoints/story_approval/review").json()
    client.post(f"/api/pipeline/{project_id}/checkpoints/story_approval/decision", json={
        "decision": "APPROVED",
        "expected_artifact_version": rev2["artifact_version"],
        "expected_artifact_hash": rev2["current_artifact_hash"],
        "reviewer_name": "Director"
    })

    # Criteria 26: Allows generation when APPROVED and hash matches exactly (200)
    gen_approved = client.post(f"/api/storyboard/generate/{scene.id}", json={"shot_count": 2})
    assert gen_approved.status_code == 200
    assert len(gen_approved.json()) >= 1

    # Manual shot creation allowed when APPROVED
    man_app = client.post("/api/storyboard", json={
        "project_id": project_id,
        "scene_id": scene.id,
        "camera_angle": "Medium Close Up",
        "duration_seconds": 3.5
    })
    assert man_app.status_code == 201

    # Criteria 28: Blocks cross-project scene reference (400)
    create_other = client.post("/api/projects", json={"name": "Other Film"})
    other_proj_id = create_other.json()["id"]
    cross_res = client.post("/api/storyboard", json={
        "project_id": other_proj_id,
        "scene_id": scene.id, # Belongs to project_id, not other_proj_id
        "camera_angle": "Over The Shoulder"
    })
    assert cross_res.status_code == 400
    assert "belongs to project" in cross_res.json()["detail"].lower()

    # Criteria 25 & 27: Blocks when screenplay modified after approval (hash mismatch / STALE -> 409)
    client.patch(f"/api/scenes/{scene.id}", json={"script_content": "Modified after approval."})
    gen_stale = client.post(f"/api/storyboard/generate/{scene.id}", json={"shot_count": 2})
    assert gen_stale.status_code == 409
    assert "stale" in gen_stale.json()["detail"].lower() or "hash mismatch" in gen_stale.json()["detail"].lower()

    # Criteria 32: Unsupported checkpoint fails closed (403)
    with pytest.raises(Exception) as excinfo:
        gatekeeper.verify_checkpoint(db=session, project_id=project_id, checkpoint_type="visual_approval")
    assert "unavailable" in str(excinfo.value.detail).lower() or "fail-closed" in str(excinfo.value.detail).lower()


def test_state_transitions_and_concurrency(test_db_session):
    """
    Acceptance Criteria 33-38:
    - Valid and invalid state transitions.
    - Concurrent approval attempts with conflicting decisions.
    - PostgreSQL behavior tested when database available.
    """
    session, engine = test_db_session

    # Unit transition rules
    from backend.app.services.pipeline_orchestrator import (
        validate_checkpoint_transition, validate_stage_transition
    )

    # Criteria 33: Valid transitions
    assert validate_checkpoint_transition("LOCKED", "WAITING_FOR_APPROVAL") is True
    assert validate_checkpoint_transition("WAITING_FOR_APPROVAL", "APPROVED") is True
    assert validate_checkpoint_transition("WAITING_FOR_APPROVAL", "REJECTED") is True
    assert validate_checkpoint_transition("WAITING_FOR_APPROVAL", "STALE") is True
    assert validate_checkpoint_transition("APPROVED", "STALE") is True
    assert validate_checkpoint_transition("REJECTED", "STALE") is True
    assert validate_checkpoint_transition("STALE", "WAITING_FOR_APPROVAL") is True

    # Criteria 34: Invalid transitions
    assert validate_checkpoint_transition("LOCKED", "APPROVED") is False
    assert validate_checkpoint_transition("LOCKED", "REJECTED") is False
    assert validate_checkpoint_transition("APPROVED", "WAITING_FOR_APPROVAL") is False
    assert validate_checkpoint_transition("APPROVED", "REJECTED") is False
    assert validate_checkpoint_transition("STALE", "APPROVED") is False

    # Stage transitions
    assert validate_stage_transition("NOT_STARTED", "IN_PROGRESS") is True
    assert validate_stage_transition("NOT_STARTED", "WAITING_FOR_APPROVAL") is True
    assert validate_stage_transition("IN_PROGRESS", "COMPLETED") is True
    assert validate_stage_transition("COMPLETED", "STALE") is True
    assert validate_stage_transition("COMPLETED", "FAILED") is False

    # Criteria 35 & 36: Two concurrent reviewers cannot both win
    from concurrent.futures import ThreadPoolExecutor

    create_res = client.post("/api/projects", json={"name": "Concurrent Reviewers Film"})
    c_proj_id = create_res.json()["id"]
    c_scene = client.post("/api/scenes", json={
        "project_id": c_proj_id,
        "scene_number": 1,
        "slugline": "INT. WAR ROOM - NIGHT",
        "script_content": "INT. WAR ROOM - NIGHT\nDebate erupts."
    })
    client.post(f"/api/pipeline/{c_proj_id}/checkpoints/story_approval/submit")
    c_rev = client.get(f"/api/pipeline/{c_proj_id}/checkpoints/story_approval/review").json()
    token_v = c_rev["artifact_version"]
    token_h = c_rev["current_artifact_hash"]

    def submit_decision(decision: str, reviewer: str):
        return client.post(f"/api/pipeline/{c_proj_id}/checkpoints/story_approval/decision", json={
            "decision": decision,
            "expected_artifact_version": token_v,
            "expected_artifact_hash": token_h,
            "reviewer_name": reviewer
        })

    with ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(submit_decision, "APPROVED", "Director")
        f2 = executor.submit(submit_decision, "REJECTED", "Producer")
        r1 = f1.result()
        r2 = f2.result()

    statuses = {r1.status_code, r2.status_code}
    # Exactly one must succeed (200) and the other must be rejected (400 or 409)
    assert 200 in statuses
    assert (400 in statuses or 409 in statuses)
    assert r1.status_code != r2.status_code or (r1.status_code == 200 and r2.status_code != 200)

    # Audits must reflect only the winning decision
    c_audits = client.get(f"/api/pipeline/{c_proj_id}/checkpoints/story_approval/audit").json()["items"]
    dec_audits = [a for a in c_audits if a["decision"] in ("APPROVED", "REJECTED")]
    assert len(dec_audits) == 1

    # Criteria 38: PostgreSQL test if available

    pg_engine = get_postgres_engine()
    if pg_engine:
        TestingPgSession = sessionmaker(bind=pg_engine)
        pg_session = TestingPgSession()
        try:
            proj_id = str(uuid.uuid4())
            p = Project(id=proj_id, name="PostgreSQL State Transition Film")
            pg_session.add(p)
            pg_session.commit()
            PipelineOrchestrator.initialize_project_pipeline(proj_id, db=pg_session)

            s = Scene(
                id=str(uuid.uuid4()),
                project_id=proj_id,
                scene_number=1,
                slugline="EXT. POSTGRES - DAY",
                script_content="PostgreSQL high-concurrency test.",
                order_index=1
            )
            pg_session.add(s)
            pg_session.commit()

            # Submit & Approve on PostgreSQL with row locking
            sub_res = PipelineOrchestrator.submit_story_for_review(pg_session, proj_id)
            assert sub_res["status"] == "WAITING_FOR_APPROVAL"

            from backend.app.schemas.pipeline import CheckpointDecisionRequest
            dec_req = CheckpointDecisionRequest(
                decision="APPROVED",
                expected_artifact_version=sub_res["artifact_version"],
                expected_artifact_hash=sub_res["artifact_content_hash"],
                reviewer_name="Postgres Reviewer"
            )
            app_res = PipelineOrchestrator.record_story_decision(pg_session, proj_id, dec_req)
            assert app_res["status"] == "APPROVED"
        finally:
            pg_session.rollback()
            pg_session.close()
            pg_engine.dispose()


def test_legacy_approval_bypass_blocked(test_db_session):
    """
    Requirement 1:
    - Calling record_checkpoint_decision on story_approval raises 400.
    - Calling record_checkpoint_decision on non-story checkpoints raises 400 (fail-closed).
    """
    session, engine = test_db_session
    create_res = client.post("/api/projects", json={"name": "Bypass Defense Film"})
    project_id = create_res.json()["id"]

    # Attempting to call legacy record_checkpoint_decision on story_approval
    with pytest.raises(Exception) as exc_story:
        PipelineOrchestrator.record_checkpoint_decision(
            session, project_id, "story_approval", "APPROVED", reviewer_name="Hacker"
        )
    assert exc_story.value.status_code == 400
    assert "bypass" in str(exc_story.value.detail).lower() or "story approval" in str(exc_story.value.detail).lower()

    # Attempting to call on non-story checkpoint (e.g. visual_approval)
    with pytest.raises(Exception) as exc_non_story:
        PipelineOrchestrator.record_checkpoint_decision(
            session, project_id, "visual_approval", "APPROVED", reviewer_name="Hacker"
        )
    assert exc_non_story.value.status_code == 400
    assert "not implemented" in str(exc_non_story.value.detail).lower() or "fail-closed" in str(exc_non_story.value.detail).lower()


def test_concurrent_submissions_stable_snapshot(test_db_session):
    """
    Requirement 2:
    - Multiple threads submit story for review concurrently.
    - Serialized under project write lock.
    - Produces a consistent state (WAITING_FOR_APPROVAL) and valid version without race conditions.
    """
    from concurrent.futures import ThreadPoolExecutor

    session, engine = test_db_session
    create_res = client.post("/api/projects", json={"name": "Concurrent Submit Film"})
    project_id = create_res.json()["id"]

    scene = client.post("/api/scenes", json={
        "project_id": project_id,
        "scene_number": 1,
        "slugline": "INT. LAB - DAY",
        "script_content": "Scientists observe stable reactions."
    })
    assert scene.status_code == 201

    def do_submit():
        return client.post(f"/api/pipeline/{project_id}/checkpoints/story_approval/submit")

    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = [executor.submit(do_submit) for _ in range(3)]
        results = [f.result() for f in futures]

    for r in results:
        assert r.status_code == 200

    # Verify checkpoint state
    cp = client.get(f"/api/pipeline/{project_id}/checkpoints/story_approval").json()
    assert cp["status"] == "WAITING_FOR_APPROVAL"
    assert cp["artifact_version"] == 1

    # Audit history should have exactly 1 SUBMITTED_FOR_REVIEW due to idempotency
    audits = client.get(f"/api/pipeline/{project_id}/checkpoints/story_approval/audit").json()["items"]
    submit_audits = [a for a in audits if a["decision"] == "SUBMITTED_FOR_REVIEW"]
    assert len(submit_audits) == 1


def test_worker_invalidation_and_late_completion_rejection(test_db_session):
    """
    Requirements 3 & 4:
    - Worker running on H1 while screenplay changes to H2 causes stage to become STALE and clears active_job_id.
    - Late completion attempt by worker is rejected when stage is STALE or consumed_input_hash is mismatched.
    """
    session, engine = test_db_session
    create_res = client.post("/api/projects", json={"name": "Worker Invalidation Film"})
    project_id = create_res.json()["id"]

    # 1. Create scene with content H1
    sc_res = client.post("/api/scenes", json={
        "project_id": project_id,
        "scene_number": 1,
        "slugline": "INT. BUNKER - NIGHT",
        "script_content": "H1 initial draft content."
    })
    scene_id = sc_res.json()["id"]

    # Compute H1 hash
    h1_hash = PipelineOrchestrator.compute_artifact_hash(session, project_id, "screenplay")
    assert h1_hash is not None

    # Submit and approve H1
    client.post(f"/api/pipeline/{project_id}/checkpoints/story_approval/submit")
    client.post(f"/api/pipeline/{project_id}/checkpoints/story_approval/decision", json={
        "decision": "APPROVED",
        "expected_artifact_version": 1,
        "expected_artifact_hash": h1_hash,
        "reviewer_name": "Director"
    })

    # Start a downstream worker job (e.g. character_design) consuming H1
    from backend.app.services.job_manager import job_manager, JobOwnershipLostError
    job, action = job_manager.prepare_or_claim_job(
        db=session,
        project_id=project_id,
        job_type="character_generation",
        payload={"screenplay_hash": h1_hash}
    )
    worker_id = job_manager.worker_id
    fencing_token = job.fencing_token

    # Associate stage with job
    stage = session.query(PipelineStageState).filter(
        PipelineStageState.project_id == project_id,
        PipelineStageState.stage_name == "character_design"
    ).first()
    stage.status = "IN_PROGRESS"
    stage.active_job_id = job.id
    stage.consumed_input_hash = h1_hash
    session.commit()

    # 2. User mutates screenplay to H2
    client.patch(f"/api/scenes/{scene_id}", json={"script_content": "H2 radically modified content."})

    # Check stage was invalidated to STALE and active_job_id cleared
    session.expire_all()
    stage_refreshed = session.query(PipelineStageState).filter(
        PipelineStageState.project_id == project_id,
        PipelineStageState.stage_name == "character_design"
    ).first()
    assert stage_refreshed.status == "STALE"
    assert stage_refreshed.active_job_id is None

    # 3. Worker tries to complete stage with consumed_input_hash=h1_hash
    with pytest.raises(JobOwnershipLostError) as exc_stale:
        PipelineOrchestrator.complete_pipeline_stage(
            db=session,
            project_id=project_id,
            stage_name="character_design",
            active_job_id=job.id,
            worker_id=worker_id,
            fencing_token=fencing_token,
            result_hash="res-char-hash",
            consumed_input_hash=h1_hash
        )
    assert "obsolete input snapshot" in str(exc_stale.value.detail).lower() or "stale" in str(exc_stale.value.detail).lower()


def test_invalidation_audit_no_duplicate_when_already_stale(test_db_session):
    """
    Requirement 5:
    - Updating a scene when checkpoint is already STALE does not create redundant INVALIDATED audit rows.
    """
    session, engine = test_db_session
    create_res = client.post("/api/projects", json={"name": "Audit Deduplication Film"})
    project_id = create_res.json()["id"]

    sc_res = client.post("/api/scenes", json={
        "project_id": project_id,
        "scene_number": 1,
        "slugline": "INT. OFFICE - DAY",
        "script_content": "First draft."
    })
    scene_id = sc_res.json()["id"]

    # Submit and approve
    client.post(f"/api/pipeline/{project_id}/checkpoints/story_approval/submit")
    rev = client.get(f"/api/pipeline/{project_id}/checkpoints/story_approval/review").json()
    client.post(f"/api/pipeline/{project_id}/checkpoints/story_approval/decision", json={
        "decision": "APPROVED",
        "expected_artifact_version": 1,
        "expected_artifact_hash": rev["current_artifact_hash"],
        "reviewer_name": "Director"
    })

    # First modification -> STALE, exactly 1 INVALIDATED row
    client.patch(f"/api/scenes/{scene_id}", json={"script_content": "Second draft modification."})
    audits_1 = client.get(f"/api/pipeline/{project_id}/checkpoints/story_approval/audit").json()["items"]
    inval_audits_1 = [a for a in audits_1 if a["decision"] == "INVALIDATED"]
    assert len(inval_audits_1) == 1

    # Second modification while already STALE -> remains STALE, no duplicate INVALIDATED row
    client.patch(f"/api/scenes/{scene_id}", json={"script_content": "Third draft modification."})
    audits_2 = client.get(f"/api/pipeline/{project_id}/checkpoints/story_approval/audit").json()["items"]
    inval_audits_2 = [a for a in audits_2 if a["decision"] == "INVALIDATED"]
    assert len(inval_audits_2) == 1


def test_ai_scene_generation_failure_atomicity_and_clean_job_failure(test_db_session, monkeypatch):
    """
    Requirement 6:
    - Fault injection in generate_ai_scene: simulate exception before db.commit().
    - Verifies that scene addition is rolled back and fail_job marks the job as FAILED cleanly.
    """
    session, engine = test_db_session
    create_res = client.post("/api/projects", json={"name": "Atomic Failure Film"})
    project_id = create_res.json()["id"]

    # Mock PipelineOrchestrator.invalidate_downstream_stages to raise RuntimeError
    def mock_invalidate_failure(*args, **kwargs):
        raise RuntimeError("Simulated DB crash during scene generation pipeline invalidation")

    monkeypatch.setattr(
        PipelineOrchestrator,
        "invalidate_downstream_stages",
        mock_invalidate_failure
    )

    gen_res = client.post("/api/scenes/generate", json={"project_id": project_id, "prompt": "Epic sci-fi battle"})
    assert gen_res.status_code == 500

    # Ensure no orphan scene was committed
    scenes = client.get(f"/api/scenes/{project_id}").json()
    assert len(scenes) == 0

    # Ensure job was recorded as FAILED
    failed_job = session.query(StudioJob).filter(
        StudioJob.project_id == project_id,
        StudioJob.job_type == "screenplay_generate"
    ).first()
    assert failed_job is not None
    assert failed_job.status == "FAILED"
    assert "Simulated DB crash" in (failed_job.error_message or "")


def test_sqlite_lock_acquisition_and_serialization(tmp_path):
    """
    Requirement 6C: SQLite lock acquisition and transaction serialization.
    - Demonstrates that acquire_project_write_lock acquires BEGIN IMMEDIATE.
    - Concurrent connection attempting to acquire write lock while held receives 409 database is locked.
    - After the first transaction commits, the second connection successfully acquires write lock.
    - Calling acquire_project_write_lock when a DBAPI transaction is already active without write lock raises 409.
    """
    from sqlalchemy.orm import sessionmaker
    from backend.app.services.pipeline_orchestrator import acquire_project_write_lock

    db_file = tmp_path / "sqlite_lock_serialization.db"
    db_url = f"sqlite:///{db_file}"
    engine = create_engine(db_url, connect_args={"timeout": 15.0})
    run_migrations(target_engine=engine, db_url=db_url)
    SessionFactory = sessionmaker(bind=engine)

    proj_id = str(uuid.uuid4())
    with SessionFactory() as s:
        p = Project(id=proj_id, name="SQLite Serialization Film")
        s.add(p)
        s.commit()

    s1 = SessionFactory()
    s2 = SessionFactory()

    try:
        # 1. s1 acquires project write lock via BEGIN IMMEDIATE
        p1 = acquire_project_write_lock(s1, proj_id)
        assert p1 is not None

        # 2. s2 attempts to acquire write lock with a low busy timeout -> 409 Database write lock contention
        c2 = s2.connection()
        c2.exec_driver_sql("PRAGMA busy_timeout = 100")
        with pytest.raises(HTTPException) as excinfo:
            acquire_project_write_lock(s2, proj_id)
        assert excinfo.value.status_code == 409
        assert "locked" in str(excinfo.value.detail).lower()

        # 3. s1 commits and releases the lock
        s1.commit()

        # 4. s2 can now acquire the project write lock successfully
        p2 = acquire_project_write_lock(s2, proj_id)
        assert p2 is not None
        s2.commit()

        # 5. Calling acquire_project_write_lock when connection is already in an active un-locked transaction
        s3 = SessionFactory()
        pre_id = str(uuid.uuid4())
        s3.add(Project(id=pre_id, name="Pre-existing Transaction"))
        s3.flush()
        with pytest.raises(HTTPException) as exc_unlocked:
            acquire_project_write_lock(s3, proj_id)
        assert exc_unlocked.value.status_code == 409
        assert "caller transaction is already in progress" in str(exc_unlocked.value.detail).lower()
        # Caller pending write was NOT lost -> caller can commit it successfully
        s3.commit()
        s3.close()
        with SessionFactory() as verify_s:
            saved_p = verify_s.query(Project).filter(Project.id == pre_id).first()
            assert saved_p is not None
            assert saved_p.name == "Pre-existing Transaction"
    finally:
        s1.close()
        s2.close()
        engine.dispose()


def test_worker_failure_rejected_when_stage_stale(test_db_session):
    """
    Requirement 6D: Worker failure rejected when stage is STALE.
    - Worker begins executing on H1.
    - Screenplay mutates to H2 -> stage is invalidated to STALE and active_job_id cleared.
    - Stale worker calls fail_pipeline_stage -> rejected with JobOwnershipLostError.
    - Stage remains STALE, neither marked FAILED nor overwritten.
    """
    from backend.app.services.job_manager import JobManager, JobOwnershipLostError
    session, engine = test_db_session

    create_res = client.post("/api/projects", json={"name": "Worker Failure Defense Film"})
    project_id = create_res.json()["id"]

    sc_res = client.post("/api/scenes", json={
        "project_id": project_id,
        "scene_number": 1,
        "slugline": "INT. BASE - NIGHT",
        "script_content": "H1 initial draft."
    })
    scene_id = sc_res.json()["id"]

    h1 = PipelineOrchestrator.compute_artifact_hash(session, project_id, "screenplay")
    client.post(f"/api/pipeline/{project_id}/checkpoints/story_approval/submit")
    client.post(f"/api/pipeline/{project_id}/checkpoints/story_approval/decision", json={
        "decision": "APPROVED",
        "expected_artifact_version": 1,
        "expected_artifact_hash": h1,
        "reviewer_name": "Director"
    })

    # Start character_design worker job on H1
    jm = JobManager()
    worker_id = "worker-fail-char-01"
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

    # Screenplay is modified to H2 -> invalidates character_design stage to STALE
    client.patch(f"/api/scenes/{scene_id}", json={"script_content": "H2 modified content."})

    session.expire_all()
    stage_stale = session.query(PipelineStageState).filter(
        PipelineStageState.project_id == project_id,
        PipelineStageState.stage_name == "character_design"
    ).first()
    assert stage_stale.status == "STALE"
    assert stage_stale.active_job_id is None

    # Stale worker attempts to fail the stage
    with pytest.raises(JobOwnershipLostError) as excinfo:
        PipelineOrchestrator.fail_pipeline_stage(
            db=session,
            project_id=project_id,
            stage_name="character_design",
            active_job_id=job.id,
            error=RuntimeError("Worker GPU Out of Memory"),
            worker_id=worker_id,
            fencing_token=fencing_token
        )
    assert "stale" in str(excinfo.value.detail).lower()

    # Stage remains STALE, not overwritten by FAILED
    session.expire_all()
    stage_after = session.query(PipelineStageState).filter(
        PipelineStageState.project_id == project_id,
        PipelineStageState.stage_name == "character_design"
    ).first()
    assert stage_after.status == "STALE"


def test_final_commit_fault_injection_in_generate_ai_scene(test_db_session, monkeypatch):
    """
    Requirement 6E: Commit fault injection in generate_ai_scene.
    - Simulates an OperationalError directly during db.commit() in generate_ai_scene.
    - Verifies that scene addition is rolled back and fail_job executes in a clean transaction.
    """
    session, engine = test_db_session
    create_res = client.post("/api/projects", json={"name": "Commit Fault Film"})
    project_id = create_res.json()["id"]

    orig_commit = Session.commit
    commit_count = 0

    def mock_commit(self):
        nonlocal commit_count
        commit_count += 1
        # Commit 1 is prepare_or_claim_job. Commit 2 is authorize_and_charge. Commit 3 is final commit in generate_ai_scene.
        if commit_count == 3:
            from sqlalchemy.exc import OperationalError
            raise OperationalError("Simulated disk I/O failure during final commit", {}, Exception())
        return orig_commit(self)

    monkeypatch.setattr(Session, "commit", mock_commit)

    gen_res = client.post("/api/scenes/generate", json={"project_id": project_id, "prompt": "Epic final scene"})
    assert gen_res.status_code == 500

    # Ensure no scene was committed
    scenes = client.get(f"/api/scenes/{project_id}").json()
    assert len(scenes) == 0

    # Ensure job was recorded as FAILED in a clean transaction
    failed_job = session.query(StudioJob).filter(
        StudioJob.project_id == project_id,
        StudioJob.job_type == "screenplay_generate"
    ).first()
    assert failed_job is not None
    assert failed_job.status == "FAILED"


def test_sqlite_lock_caller_pending_write_preserved_on_rejection(tmp_path):
    """
    Requirement 1 Test 1: Caller with pending write calls acquire_project_write_lock.
    - Helper rejects with 409 because transaction is already active without write lock.
    - Helper does NOT call db.rollback().
    - Caller's pending write is preserved and can be committed.
    """
    from sqlalchemy.orm import sessionmaker
    from backend.app.services.pipeline_orchestrator import acquire_project_write_lock

    db_file = tmp_path / "sqlite_lock_pending_write.db"
    db_url = f"sqlite:///{db_file}"
    engine = create_engine(db_url, connect_args={"timeout": 15.0})
    run_migrations(target_engine=engine, db_url=db_url)
    SessionFactory = sessionmaker(bind=engine)

    proj_id = str(uuid.uuid4())
    with SessionFactory() as s:
        p = Project(id=proj_id, name="Main Project")
        s.add(p)
        s.commit()

    caller_session = SessionFactory()
    pending_proj_id = str(uuid.uuid4())
    caller_session.add(Project(id=pending_proj_id, name="Pending Work"))
    caller_session.flush()  # DBAPI transaction is now active

    # Helper must reject WITHOUT rolling back caller_session
    with pytest.raises(HTTPException) as exc:
        acquire_project_write_lock(caller_session, proj_id)
    assert exc.value.status_code == 409
    assert "caller transaction is already in progress" in str(exc.value.detail).lower()

    # Caller commits its own transaction
    caller_session.commit()
    caller_session.close()

    # Verify pending work was committed and NOT lost
    with SessionFactory() as verify_s:
        saved = verify_s.query(Project).filter(Project.id == pending_proj_id).first()
        assert saved is not None
        assert saved.name == "Pending Work"

    engine.dispose()


def test_sqlite_lock_reentrancy_and_metadata_cleanup(tmp_path):
    """
    Requirement 1 Tests 3 & 5: Lock reentrancy and metadata cleanup.
    - Demonstrates reentrant locking within the same active transaction.
    - Demonstrates that lock metadata is cleared upon commit and rollback.
    """
    from sqlalchemy.orm import sessionmaker
    from backend.app.services.pipeline_orchestrator import acquire_project_write_lock

    db_file = tmp_path / "sqlite_lock_reentrancy.db"
    db_url = f"sqlite:///{db_file}"
    engine = create_engine(db_url, connect_args={"timeout": 15.0})
    run_migrations(target_engine=engine, db_url=db_url)
    SessionFactory = sessionmaker(bind=engine)

    p1_id = str(uuid.uuid4())
    p2_id = str(uuid.uuid4())
    with SessionFactory() as s:
        s.add(Project(id=p1_id, name="Project 1"))
        s.add(Project(id=p2_id, name="Project 2"))
        s.commit()

    s = SessionFactory()

    # Tx 1: Acquire lock for Project 1
    p1 = acquire_project_write_lock(s, p1_id)
    assert p1 is not None
    assert s.info.get("_active_sqlite_write_lock_tx") is not None
    assert p1_id in s.info.get("_locked_projects", set())

    # Reentrant call for Project 1 within same transaction succeeds
    p1_again = acquire_project_write_lock(s, p1_id)
    assert p1_again.id == p1_id

    # Reentrant call for Project 2 within same transaction succeeds (DB is write-locked)
    p2 = acquire_project_write_lock(s, p2_id)
    assert p2.id == p2_id
    assert p2_id in s.info.get("_locked_projects", set())

    # Commit Tx 1 -> metadata must be cleared
    s.commit()
    assert s.info.get("_active_sqlite_write_lock_tx") is None
    assert not s.info.get("_locked_projects")

    # Tx 2: Acquire fresh lock in new transaction
    p1_fresh = acquire_project_write_lock(s, p1_id)
    assert p1_fresh is not None
    assert s.info.get("_active_sqlite_write_lock_tx") is not None

    # Rollback Tx 2 -> metadata must be cleared
    s.rollback()
    assert s.info.get("_active_sqlite_write_lock_tx") is None
    assert not s.info.get("_locked_projects")

    s.close()
    engine.dispose()


def test_ai_scene_generation_cost_idempotency_and_retry_after_final_failure(test_db_session, monkeypatch):
    """
    Requirement 3: AI succeeds, final commit fails -> retry with same identity does not double-charge.
    - Phase A claims job.
    - Phase B executes AI and records cost in ledger (committed independently).
    - Phase C final commit is aborted by fault injection.
    - Job is marked FAILED, but CostRecord remains intact (audit preserved).
    - Client retries with exact same payload:
      - Job retry is claimed (RUNNING).
      - CostController detects operation_identity, returns existing record without double-charging!
      - Final commit succeeds: scene created, job COMPLETED.
      - Exactly 1 charge exists in cost ledger!
    """
    session, engine = test_db_session
    create_res = client.post("/api/projects", json={"name": "Cost Idempotency Film", "budget_total": 100.0})
    project_id = create_res.json()["id"]

    orig_commit = Session.commit
    commit_count = 0

    def mock_commit(self):
        nonlocal commit_count
        commit_count += 1
        # Commit 1: prepare_or_claim_job
        # Commit 2: cost_controller.authorize_and_charge
        # Commit 3: generate_ai_scene final commit -> FAIL
        if commit_count == 3:
            from sqlalchemy.exc import OperationalError
            raise OperationalError("Simulated disk failure during final scene commit", {}, Exception())
        return orig_commit(self)

    monkeypatch.setattr(Session, "commit", mock_commit)

    # Attempt 1: Fails at final commit
    res1 = client.post("/api/scenes/generate", json={"project_id": project_id, "prompt": "Unique Scene Alpha"})
    assert res1.status_code == 500

    # Verify no scene was committed
    scenes = client.get(f"/api/scenes/{project_id}").json()
    assert len(scenes) == 0

    # Verify job is FAILED
    job = session.query(StudioJob).filter(StudioJob.project_id == project_id).first()
    assert job is not None
    assert job.status == "FAILED"

    # Verify CostRecord exists (audit history preserved) and refund was recorded under Policy B
    cost_records = session.query(CostRecord).filter(CostRecord.project_id == project_id).all()
    # Under Policy B: initial charge + compensation refund record
    assert len(cost_records) == 2
    charge_rec = [r for r in cost_records if r.action_type != "refund"][0]
    refund_rec = [r for r in cost_records if r.action_type == "refund"][0]
    assert refund_rec.operation_identity == f"refund_{charge_rec.id}"

    # Remove fault injection for Attempt 2 (Retry)
    monkeypatch.setattr(Session, "commit", orig_commit)

    # Attempt 2: Retry with exact same request
    res2 = client.post("/api/scenes/generate", json={"project_id": project_id, "prompt": "Unique Scene Alpha"})
    assert res2.status_code == 200

    # Verify scene was committed
    scenes_after = client.get(f"/api/scenes/{project_id}").json()
    assert len(scenes_after) == 1
    assert scenes_after[0]["id"] == res2.json()["id"]

    # Verify job is now COMPLETED
    session.expire_all()
    job_after = session.query(StudioJob).filter(StudioJob.id == job.id).first()
    assert job_after.status == "COMPLETED"

    # Under Policy B: cost ledger records sequenced retry charge
    cost_records_after = session.query(CostRecord).filter(CostRecord.project_id == project_id).all()
    assert len(cost_records_after) == 3
    retry_rec = [r for r in cost_records_after if ":retry:" in (r.operation_identity or "")][0]
    assert retry_rec is not None


def test_ai_scene_generation_snapshot_drift_rejected(test_db_session, monkeypatch):
    """
    Requirement 2: Snapshot drift defense in generate_ai_scene.
    - AI generation begins based on input screenplay snapshot H1.
    - Before finalization, screenplay is mutated to H2.
    - Finalize phase detects snapshot drift and rejects stale generation with HTTP 409.
    - Stale scene is not saved, job is marked FAILED.
    """
    session, engine = test_db_session
    create_res = client.post("/api/projects", json={"name": "Drift Defense Film"})
    project_id = create_res.json()["id"]

    # Create Scene 1 (Screenplay hash H1)
    s1 = Scene(
        project_id=project_id,
        scene_number=1,
        slugline="INT. BASE - DAY",
        script_content="H1 initial screenplay content.",
        order_index=1
    )
    session.add(s1)
    session.commit()

    # Hook write_scene to simulate concurrent mutation during AI execution
    orig_write_scene = screenwriter_agent.write_scene
    def mutating_write_scene(*args, **kwargs):
        # Concurrently edit Scene 1 to mutate screenplay to H2 while AI is thinking
        with sessionmaker(bind=engine)() as s_mut:
            sc = s_mut.query(Scene).filter(Scene.project_id == project_id).first()
            sc.script_content = "H2 concurrently edited content."
            s_mut.commit()
        return orig_write_scene(*args, **kwargs)

    monkeypatch.setattr(screenwriter_agent, "write_scene", mutating_write_scene)

    gen_res = client.post("/api/scenes/generate", json={"project_id": project_id, "prompt": "Scene 2 attempted"})
    assert gen_res.status_code == 409
    assert "screenplay modified" in gen_res.json()["detail"].lower() or "drifted" in gen_res.json()["detail"].lower()

    # Verify only Scene 1 exists (stale generated scene was discarded)
    scenes = client.get(f"/api/scenes/{project_id}").json()
    assert len(scenes) == 1
    assert scenes[0]["scene_number"] == 1


def test_complete_pipeline_stage_mandatory_consumed_input_hash(test_db_session):
    """
    Requirement 4: Mandatory consumed_input_hash for stages dependent on screenplay.
    - character_design, location_design, storyboard fail closed if consumed_input_hash is None.
    - Fail closed if consumed_input_hash != current_screenplay_hash.
    - Fail closed if stage.consumed_input_hash != consumed_input_hash.
    - Succeeds when consumed_input_hash matches.
    """
    session, engine = test_db_session
    create_res = client.post("/api/projects", json={"name": "Input Hash Validation Film"})
    project_id = create_res.json()["id"]

    # Add scene so screenplay has a canonical hash
    sc = Scene(
        project_id=project_id,
        scene_number=1,
        slugline="INT. LAB - DAY",
        script_content="Canonical screenplay content.",
        order_index=1
    )
    session.add(sc)
    session.commit()

    current_hash = PipelineOrchestrator.compute_artifact_hash(session, project_id, "screenplay")
    assert current_hash is not None

    for stage_name in ("character_design", "location_design", "storyboard"):
        # Setup job and stage
        jm = JobManager(worker_id=f"worker-{stage_name}")
        job, action = jm.prepare_or_claim_job(
            db=session, project_id=project_id, job_type=f"{stage_name}_gen", payload={"h": current_hash}
        )

        stage = session.query(PipelineStageState).filter(
            PipelineStageState.project_id == project_id,
            PipelineStageState.stage_name == stage_name
        ).first()
        stage.status = "IN_PROGRESS"
        stage.active_job_id = job.id
        stage.consumed_input_hash = current_hash
        session.commit()

        # 1. consumed_input_hash is None -> FAIL CLOSED
        with pytest.raises(JobOwnershipLostError) as exc_none:
            PipelineOrchestrator.complete_pipeline_stage(
                db=session,
                project_id=project_id,
                stage_name=stage_name,
                active_job_id=job.id,
                worker_id=f"worker-{stage_name}",
                fencing_token=job.fencing_token,
                consumed_input_hash=None
            )
        assert "mandatory" in str(exc_none.value.detail).lower()

        # 2. consumed_input_hash is obsolete/mismatched -> FAIL CLOSED
        with pytest.raises(JobOwnershipLostError) as exc_mismatch:
            PipelineOrchestrator.complete_pipeline_stage(
                db=session,
                project_id=project_id,
                stage_name=stage_name,
                active_job_id=job.id,
                worker_id=f"worker-{stage_name}",
                fencing_token=job.fencing_token,
                consumed_input_hash="deadbeef" * 8
            )
        assert "obsolete" in str(exc_mismatch.value.detail).lower()

        # 3. Matching consumed_input_hash -> SUCCEEDS
        completed = PipelineOrchestrator.complete_pipeline_stage(
            db=session,
            project_id=project_id,
            stage_name=stage_name,
            active_job_id=job.id,
            worker_id=f"worker-{stage_name}",
            fencing_token=job.fencing_token,
            result_hash=f"res-{stage_name}",
            consumed_input_hash=current_hash
        )
        assert completed.status == "COMPLETED"


def test_job_manager_worker_fencing_prevents_stale_worker_mutations(test_db_session):
    """
    Requirement 6: JobManager worker fencing test.
    - Worker 1 claims job (fencing_token=1).
    - Worker 1 lease expires.
    - Worker 2 reclaims job (fencing_token=2).
    - Worker 1 attempts complete_job with token 1 -> rejected with JobOwnershipLostError.
    - Worker 1 attempts fail_job with token 1 -> rejected with JobOwnershipLostError.
    - Worker 2 completes job with token 2 -> succeeds!
    """
    session, engine = test_db_session
    create_res = client.post("/api/projects", json={"name": "Fencing Defense Film"})
    project_id = create_res.json()["id"]

    jm1 = JobManager(worker_id="worker-01")
    job1, action1 = jm1.prepare_or_claim_job(
        db=session, project_id=project_id, job_type="fenced_task", payload={"task": "process"}
    )
    assert job1.worker_id == "worker-01"
    assert job1.fencing_token == 1

    # Simulate Worker 1 lease expiry
    job1.lease_expires_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=10)
    session.commit()

    # Worker 2 reclaims job
    jm2 = JobManager(worker_id="worker-02")
    job2, action2 = jm2.prepare_or_claim_job(
        db=session, project_id=project_id, job_type="fenced_task", payload={"task": "process"}
    )
    assert action2 == "EXECUTE"
    assert job2.worker_id == "worker-02"
    assert job2.fencing_token == 2

    # Stale Worker 1 attempts complete_job with token 1 -> REJECTED
    with pytest.raises(JobOwnershipLostError):
        jm1.complete_job(
            db=session,
            job_id=job1.id,
            result={"output": "stale-worker-1"},
            worker_id="worker-01",
            fencing_token=1
        )

    # Stale Worker 1 attempts fail_job with token 1 -> REJECTED
    with pytest.raises(JobOwnershipLostError):
        jm1.fail_job(
            db=session,
            job_id=job1.id,
            error=RuntimeError("Worker 1 crashed"),
            worker_id="worker-01",
            fencing_token=1
        )

    # Valid Worker 2 completes job with token 2 -> SUCCEEDS
    completed_job = jm2.complete_job(
        db=session,
        job_id=job2.id,
        result={"output": "valid-worker-2"},
        worker_id="worker-02",
        fencing_token=2
    )
    assert completed_job.status == "COMPLETED"


# =========================================================================
# HARDENING: Complete Generation Snapshot, Cost Policy B, SQLite Locks & Leases
# =========================================================================

def test_screenplay_generate_foreign_character_or_location_rejected(test_db_session):
    """
    Requirement 1: Input validation upfront:
    Characters and location must belong to data.project_id.
    Foreign or invalid characters/locations reject with HTTP 400 before AI execution.
    """
    session, engine = test_db_session

    p1 = Project(id="proj-a", name="Project A")
    p2 = Project(id="proj-b", name="Project B")
    session.add_all([p1, p2])
    session.commit()
    PipelineOrchestrator.initialize_project_pipeline("proj-a", db=session)
    PipelineOrchestrator.initialize_project_pipeline("proj-b", db=session)

    c_b = Character(id="char-b", project_id="proj-b", name="Foreign Character")
    l_b = Location(id="loc-b", project_id="proj-b", name="Foreign Location")
    session.add_all([c_b, l_b])
    session.commit()

    # 1. Foreign character ID -> 400
    res_char = client.post("/api/scenes/generate", json={
        "project_id": "proj-a",
        "prompt": "Scene with foreign char",
        "character_ids": ["char-b"]
    })
    assert res_char.status_code == 400
    assert "character ids are invalid or do not belong to this project" in res_char.json()["detail"].lower()

    # 2. Foreign location ID -> 400
    res_loc = client.post("/api/scenes/generate", json={
        "project_id": "proj-a",
        "prompt": "Scene with foreign loc",
        "location_id": "loc-b"
    })
    assert res_loc.status_code == 400
    assert "location id is invalid or does not belong to this project" in res_loc.json()["detail"].lower()


def test_screenplay_generate_input_drift_detection_and_compensation(test_db_session):
    """
    Requirement 1 & 2: Canonical Generation Snapshot Drift Detection:
    - Input drift during AI execution (project title, genre, characters, location, screenplay)
      is detected under project write lock in Phase C.
    - AI result is discarded with HTTP 409.
    - Policy B compensation is recorded (Project.budget_spent is restored, refund record created).
    """
    session, engine = test_db_session

    # Case 1: Project title drifts during AI execution
    p = Project(id="proj-drift-title", name="Original Title", genre="Action", budget_total=100.0, budget_spent=0.0)
    session.add(p)
    session.commit()
    PipelineOrchestrator.initialize_project_pipeline("proj-drift-title", db=session)

    orig_write = screenwriter_agent.write_scene

    def drift_title_during_ai(**kwargs):
        with sessionmaker(bind=engine)() as mut_sess:
            proj = mut_sess.query(Project).filter(Project.id == "proj-drift-title").first()
            proj.name = "Mutated Title"
            mut_sess.commit()
        return orig_write(**kwargs)

    with patch.object(screenwriter_agent, "write_scene", side_effect=drift_title_during_ai):
        res = client.post("/api/scenes/generate", json={
            "project_id": "proj-drift-title",
            "prompt": "Explosion in alleyway"
        })
        assert res.status_code == 409
        assert "drifted" in res.json()["detail"].lower()

    # Verify no scene was created
    scenes = session.query(Scene).filter(Scene.project_id == "proj-drift-title").all()
    assert len(scenes) == 0

    # Verify budget_spent is 0.0 and refund record exists if charge occurred
    session.refresh(p)
    assert p.budget_spent == 0.0

    # Case 2: Project genre drifts during AI execution
    p2 = Project(id="proj-drift-genre", name="Genre Drift Proj", genre="Comedy", budget_total=100.0, budget_spent=0.0)
    session.add(p2)
    session.commit()
    PipelineOrchestrator.initialize_project_pipeline("proj-drift-genre", db=session)

    def drift_genre_during_ai(**kwargs):
        with sessionmaker(bind=engine)() as mut_sess:
            proj = mut_sess.query(Project).filter(Project.id == "proj-drift-genre").first()
            proj.genre = "Horror"
            mut_sess.commit()
        return orig_write(**kwargs)

    with patch.object(screenwriter_agent, "write_scene", side_effect=drift_genre_during_ai):
        res = client.post("/api/scenes/generate", json={
            "project_id": "proj-drift-genre",
            "prompt": "Funny joke"
        })
        assert res.status_code == 409
        assert "drifted" in res.json()["detail"].lower()

    # Case 3: Character content drifts during AI execution
    p3 = Project(id="proj-drift-char", name="Char Drift Proj", genre="Sci-Fi", budget_total=100.0, budget_spent=0.0)
    session.add(p3)
    c3 = Character(id="char-c3", project_id="proj-drift-char", name="Drifter", clothing="Leather jacket")
    session.add(c3)
    session.commit()
    PipelineOrchestrator.initialize_project_pipeline("proj-drift-char", db=session)

    def drift_char_during_ai(**kwargs):
        with sessionmaker(bind=engine)() as mut_sess:
            ch = mut_sess.query(Character).filter(Character.id == "char-c3").first()
            ch.clothing = "Neon battle armor"
            mut_sess.commit()
        return orig_write(**kwargs)

    with patch.object(screenwriter_agent, "write_scene", side_effect=drift_char_during_ai):
        res = client.post("/api/scenes/generate", json={
            "project_id": "proj-drift-char",
            "prompt": "Sci-fi duel",
            "character_ids": ["char-c3"]
        })
        assert res.status_code == 409
        assert "drifted" in res.json()["detail"].lower()

    # Case 4: Location content drifts during AI execution
    p4 = Project(id="proj-drift-loc", name="Loc Drift Proj", genre="Mystery", budget_total=100.0, budget_spent=0.0)
    session.add(p4)
    l4 = Location(id="loc-l4", project_id="proj-drift-loc", name="Old Mansion", lighting_setup="Candlelight")
    session.add(l4)
    session.commit()
    PipelineOrchestrator.initialize_project_pipeline("proj-drift-loc", db=session)

    def drift_loc_during_ai(**kwargs):
        with sessionmaker(bind=engine)() as mut_sess:
            lc = mut_sess.query(Location).filter(Location.id == "loc-l4").first()
            lc.lighting_setup = "Harsh fluorescent flickering lights"
            mut_sess.commit()
        return orig_write(**kwargs)

    with patch.object(screenwriter_agent, "write_scene", side_effect=drift_loc_during_ai):
        res = client.post("/api/scenes/generate", json={
            "project_id": "proj-drift-loc",
            "prompt": "Detective entrance",
            "location_id": "loc-l4"
        })
        assert res.status_code == 409
        assert "drifted" in res.json()["detail"].lower()

    # Case 5: Screenplay content drifts during AI execution
    p5 = Project(id="proj-drift-sc", name="Screenplay Drift Proj", genre="Thriller", budget_total=100.0, budget_spent=0.0)
    session.add(p5)
    session.commit()
    PipelineOrchestrator.initialize_project_pipeline("proj-drift-sc", db=session)

    def drift_screenplay_during_ai(**kwargs):
        with sessionmaker(bind=engine)() as mut_sess:
            mut_sc = Scene(
                id=str(uuid.uuid4()), project_id="proj-drift-sc", scene_number=1,
                slugline="INT. VAULT - NIGHT", script_content="Interfering scene.", order_index=1
            )
            mut_sess.add(mut_sc)
            mut_sess.commit()
        return orig_write(**kwargs)

    with patch.object(screenwriter_agent, "write_scene", side_effect=drift_screenplay_during_ai):
        res = client.post("/api/scenes/generate", json={
            "project_id": "proj-drift-sc",
            "prompt": "Initial scene generation"
        })
        assert res.status_code == 409
        assert "drifted" in res.json()["detail"].lower()


def test_cost_controller_policy_b_compensation_and_retry(test_db_session):
    """
    Requirement 2: Cost ledger Policy B:
    - Costs incurred only on successful finalization.
    - Finalization failure triggers record_compensation().
    - Compensation is idempotent (no duplicate refunds).
    - Concurrent compensation race creates exactly 1 refund record.
    - Sequenced retry generation after compensation succeeds with :retry:N attempt identity.
    - Budget limits and spent stay consistent across the lifecycle.
    """
    import concurrent.futures
    session, engine = test_db_session

    project_id = "proj-cost-policy-b"
    proj = Project(id=project_id, name="Cost Policy B Proj", budget_total=100.0, budget_spent=0.0)
    session.add(proj)
    session.commit()

    cc = CostController()
    payload = {"scene_num": 1, "prompt": "cyberpunk rooftop chase"}

    # 1. Authorize & charge
    rec = cc.authorize_and_charge(
        db=session,
        project_id=project_id,
        action_type="script_generation",
        provider="gemini",
        model="gemini-1.5-pro",
        actual_cost=25.0,
        actual_cost_known=True,
        payload=payload
    )
    session.commit()
    session.refresh(proj)
    assert proj.budget_spent == 25.0
    rec_id = rec.id

    # 2. Record compensation
    refund = cc.record_compensation(session, project_id, rec, reason="input_drift")
    session.commit()
    session.refresh(proj)
    assert proj.budget_spent == 0.0
    assert refund.cost_usd == -25.0
    assert refund.operation_identity == f"refund_{rec_id}"

    # 3. Idempotent repeated compensation call
    refund_dup = cc.record_compensation(session, project_id, rec, reason="input_drift")
    session.commit()
    session.refresh(proj)
    assert proj.budget_spent == 0.0
    assert refund_dup.id == refund.id

    # 4. Concurrent compensation race (5 threads)
    payload2 = {"scene_num": 2, "prompt": "underground bunker"}
    rec2 = cc.authorize_and_charge(
        db=session,
        project_id=project_id,
        action_type="script_generation",
        provider="gemini",
        model="gemini-1.5-pro",
        actual_cost=30.0,
        actual_cost_known=True,
        payload=payload2
    )
    session.commit()
    session.refresh(proj)
    assert proj.budget_spent == 30.0
    rec2_id = rec2.id

    SessionLocal = sessionmaker(bind=engine)
    def concurrent_refunder():
        s = SessionLocal()
        try:
            r = s.query(CostRecord).filter(CostRecord.id == rec2_id).first()
            res = cc.record_compensation(s, project_id, r, reason="concurrent_race")
            return res.id
        finally:
            s.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as ex:
        futures = [ex.submit(concurrent_refunder) for _ in range(5)]
        returned_ids = [f.result() for f in futures]

    assert len(set(returned_ids)) == 1
    session.refresh(proj)
    assert proj.budget_spent == 0.0

    refund_records_in_db = session.query(CostRecord).filter(
        CostRecord.operation_identity == f"refund_{rec2_id}"
    ).all()
    assert len(refund_records_in_db) == 1

    # 5. Retry generation with same payload after compensation charges under sequenced attempt identity
    rec_retry = cc.authorize_and_charge(
        db=session,
        project_id=project_id,
        action_type="script_generation",
        provider="gemini",
        model="gemini-1.5-pro",
        actual_cost=25.0,
        actual_cost_known=True,
        payload=payload
    )
    session.commit()
    session.refresh(proj)
    assert proj.budget_spent == 25.0
    assert ":retry:1" in rec_retry.operation_identity


def test_sqlite_lock_lifecycle_and_transaction_ownership(test_db_session):
    """
    Requirement 3 & 6: SQLite lock lifecycle and transaction boundary verification:
    - Caller modifies row in SAVEPOINT.
    - Lock helper rejects with 409 because transaction/savepoint is already active without lock.
    - Caller rolls back SAVEPOINT.
    - Real DB reload confirms row reverted to pre-savepoint state.
    - Pending outer transaction work is intact and committable.
    - Outer commit/rollback releases lock.
    - Session.close releases lock so another Session can immediately acquire it.
    - Metadata is cleared across all exit paths (commit, rollback, close).
    """
    session, engine = test_db_session
    SessionLocal = sessionmaker(bind=engine)

    p1 = Project(id="proj-sq-1", name="SQLite Proj 1", genre="Original Genre")
    p2 = Project(id="proj-sq-2", name="SQLite Proj 2", genre="Original Genre")
    session.add_all([p1, p2])
    session.commit()

    # 1. Caller begins outer transaction with pending work, then opens a SAVEPOINT
    p1.genre = "Outer Modified Genre"
    sp = session.begin_nested()
    p1.name = "Savepoint Modification Name"

    # Lock acquisition inside savepoint must be rejected with 409 without rolling back caller work
    with pytest.raises(HTTPException) as exc_info:
        acquire_project_write_lock(session, "proj-sq-1")
    assert exc_info.value.status_code == 409

    # Caller rolls back the SAVEPOINT
    sp.rollback()

    # Real DB verification: reload from separate connection to confirm savepoint change was never committed
    with SessionLocal() as check_sess:
        p1_check = check_sess.query(Project).filter(Project.id == "proj-sq-1").first()
        assert p1_check.name == "SQLite Proj 1"
        assert p1_check.genre == "Original Genre"

    # Verify pending outer work remains intact in caller session
    assert p1.name == "SQLite Proj 1"
    assert p1.genre == "Outer Modified Genre"
    session.commit()

    # Confirm outer work was committed to DB
    with SessionLocal() as check_sess2:
        p1_check2 = check_sess2.query(Project).filter(Project.id == "proj-sq-1").first()
        assert p1_check2.genre == "Outer Modified Genre"

    # 2. Acquire lock first -> begin_nested() -> nested rollback preserves outer write lock
    acquire_project_write_lock(session, "proj-sq-1")
    assert session.info.get("_active_sqlite_write_lock_tx") is not None

    sp2 = session.begin_nested()
    # Reentrant lock inside savepoint succeeds
    acquire_project_write_lock(session, "proj-sq-1")
    sp2.rollback()

    # Outer lock is still active and valid after nested rollback
    assert session.info.get("_active_sqlite_write_lock_tx") is not None
    # Reentrant lock after nested rollback succeeds
    acquire_project_write_lock(session, "proj-sq-1")

    # 3. Outer transaction rollback clears lock metadata
    session.rollback()
    assert session.info.get("_active_sqlite_write_lock_tx") is None
    assert session.info.get("_locked_projects") is None

    # 4. Session close while holding lock cleans up metadata and releases lock for other sessions
    sess_temp = SessionLocal()
    acquire_project_write_lock(sess_temp, "proj-sq-1")
    assert sess_temp.info.get("_active_sqlite_write_lock_tx") is not None
    sess_temp.close()
    assert sess_temp.info.get("_active_sqlite_write_lock_tx") is None

    # Another session can immediately acquire lock
    sess_next = SessionLocal()
    acquire_project_write_lock(sess_next, "proj-sq-1")
    assert sess_next.info.get("_active_sqlite_write_lock_tx") is not None
    sess_next.rollback()
    sess_next.close()

    # 5. Query Project failure after lock acquired rolls back helper transaction
    sess_fail = SessionLocal()
    with patch.object(sess_fail, "query", side_effect=RuntimeError("Simulated query failure")):
        with pytest.raises(RuntimeError):
            acquire_project_write_lock(sess_fail, "proj-sq-1")
    assert sess_fail.get_transaction() is None
    assert sess_fail.info.get("_active_sqlite_write_lock_tx") is None
    sess_fail.close()

    # 6. Multiple projects in same write transaction
    acquire_project_write_lock(session, "proj-sq-1")
    acquire_project_write_lock(session, "proj-sq-2")
    assert session.info.get("_locked_projects") == {"proj-sq-1", "proj-sq-2"}
    session.commit()
    assert session.info.get("_locked_projects") is None


def test_job_lease_expiration_and_fencing_during_ai_generation(test_db_session):
    """
    Requirement 5: Job ownership & lease expiration in generate_ai_scene:
    - Worker 1 prepares/claims job (fencing_token = 1).
    - Worker 1 lease expires during Phase B AI execution.
    - Worker 2 reclaims the job (fencing_token = 2).
    - Worker 1 finishes AI execution and asserts ownership:
      rejected with HTTP 409 conflict.
    - Worker 1 must NOT charge cost ledger.
    - Worker 1 must NOT finalize scene.
    - Worker 2 retains ownership of the job.
    """
    import json
    session, engine = test_db_session

    project_id = "proj-lease-expire"
    p = Project(id=project_id, name="Lease Expire Proj", budget_total=100.0, budget_spent=0.0)
    session.add(p)
    session.commit()
    PipelineOrchestrator.initialize_project_pipeline(project_id, db=session)

    orig_write = screenwriter_agent.write_scene

    def simulate_lease_expiry_and_reclaim(**kwargs):
        # In this hook (running in Phase B AI execution), simulate Worker 1 lease expiry
        # and Worker 2 reclaiming the job!
        with sessionmaker(bind=engine)() as hijack_sess:
            job = hijack_sess.query(StudioJob).filter(
                StudioJob.project_id == project_id,
                StudioJob.job_type == "screenplay_generate"
            ).first()
            assert job is not None
            # Expire Worker 1 lease
            job.lease_expires_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=10)
            hijack_sess.commit()

            # Worker 2 reclaims job
            jm2 = JobManager(worker_id="worker-02")
            job2, action2 = jm2.prepare_or_claim_job(
                db=hijack_sess,
                project_id=project_id,
                job_type="screenplay_generate",
                payload=json.loads(job.payload) if isinstance(job.payload, str) else job.payload
            )
            assert action2 == "EXECUTE"
            assert job2.worker_id == "worker-02"
            assert job2.fencing_token == 2

        return orig_write(**kwargs)

    with patch.object(screenwriter_agent, "write_scene", side_effect=simulate_lease_expiry_and_reclaim):
        res = client.post("/api/scenes/generate", json={
            "project_id": project_id,
            "prompt": "Worker 1 will lose lease during this generation"
        })
        assert res.status_code == 409
        assert "lease expired" in res.json()["detail"].lower() or "ownership" in res.json()["detail"].lower()

    # Verify Worker 1 did not charge cost
    session.refresh(p)
    assert p.budget_spent == 0.0

    # Verify no scene was created
    scenes = session.query(Scene).filter(Scene.project_id == project_id).all()
    assert len(scenes) == 0

    # Verify Worker 2 still owns the job
    job_in_db = session.query(StudioJob).filter(
        StudioJob.project_id == project_id,
        StudioJob.job_type == "screenplay_generate"
    ).first()
    assert job_in_db.worker_id == "worker-02"
    assert job_in_db.fencing_token == 2


def test_stable_character_selection_and_no_false_drift(test_db_session):
    """
    Requirement 4: Stable character and location resolution:
    - Multiple characters (>2) sorted deterministically by (created_at, id).
    - Stable character and location IDs included in canonical representation.
    - Repeated resolution without changes produces identical data and hash (no false drift).
    - Scene generation automatically selects the first 2 characters in stable order.
    """
    session, engine = test_db_session
    from backend.app.routers.screenplay import (
        resolve_canonical_characters, resolve_canonical_location,
        build_canonical_generation_input, compute_generation_input_hash
    )

    project_id = "proj-stable-chars"
    p = Project(id=project_id, name="Stable Film", genre="Thriller")
    session.add(p)
    session.commit()
    PipelineOrchestrator.initialize_project_pipeline(project_id, db=session)

    # Create 4 characters with same name but distinct IDs
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    c1 = Character(id="c-01", project_id=project_id, name="Agent Smith", role="Enforcer", created_at=now - timedelta(seconds=40))
    c2 = Character(id="c-02", project_id=project_id, name="Agent Smith", role="Enforcer", created_at=now - timedelta(seconds=30))
    c3 = Character(id="c-03", project_id=project_id, name="Trinity", role="Hacker", created_at=now - timedelta(seconds=20))
    c4 = Character(id="c-04", project_id=project_id, name="Neo", role="Protagonist", created_at=now - timedelta(seconds=10))
    l1 = Location(id="loc-01", project_id=project_id, name="Rooftop", time_of_day="Rainy Night")
    session.add_all([c1, c2, c3, c4, l1])
    session.commit()

    # Default resolution selects exactly first 2 in stable created_at order
    resolved = resolve_canonical_characters(session, project_id, character_ids=None)
    assert len(resolved) == 2
    assert resolved[0]["id"] == "c-01"
    assert resolved[1]["id"] == "c-02"

    resolved_loc = resolve_canonical_location(session, project_id, location_id="loc-01")
    assert resolved_loc["id"] == "loc-01"

    # Build canonical input twice: hashes must be identical
    input1 = build_canonical_generation_input(
        project_title="Stable Film", project_genre="Thriller", scene_number=1, prompt="Chase",
        characters=resolved, location=resolved_loc, screenplay_hash=None
    )
    input2 = build_canonical_generation_input(
        project_title="Stable Film", project_genre="Thriller", scene_number=1, prompt="Chase",
        characters=resolved, location=resolved_loc, screenplay_hash=None
    )
    hash1 = compute_generation_input_hash(input1)
    hash2 = compute_generation_input_hash(input2)
    assert hash1 == hash2

    # Scene generation without character_ids succeeds without false drift
    res = client.post("/api/scenes/generate", json={
        "project_id": project_id,
        "prompt": "Chase across the rooftops"
    })
    assert res.status_code == 200
    assert res.json()["scene_number"] == 1


def test_concurrent_scene_generation_policy_a_reservation(test_db_session):
    """
    Requirement 5: Concurrency policy for scene_number (Policy A):
    - Two concurrent requests generate scenes on the same project.
    - Each receives a distinct reserved scene number.
    - Both succeed without colliding or false drift.
    - Resulting project has two scenes with distinct scene numbers and sequential order indices.
    """
    import concurrent.futures
    session, engine = test_db_session

    project_id = f"proj-concurrent-gen-{uuid.uuid4().hex[:8]}"
    p = Project(id=project_id, name="Concurrent Scenes Film", genre="Action", budget_total=200.0, budget_spent=0.0)
    session.add(p)
    session.commit()
    PipelineOrchestrator.initialize_project_pipeline(project_id, db=session)

    def worker_generate(prompt_text):
        return client.post("/api/scenes/generate", json={
            "project_id": project_id,
            "prompt": prompt_text
        })

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(worker_generate, "Opening scene: bank heist")
        f2 = executor.submit(worker_generate, "Second scene: getaway chase")
        res1 = f1.result()
        res2 = f2.result()

    print("DEBUG res1:", res1.status_code, res1.json())
    print("DEBUG res2:", res2.status_code, res2.json())

    # Either worker may finish first depending on thread scheduling.
    # The worker finishing second will strictly detect the snapshot drift from the first insertion (Requirement 3).
    # Retrying the second worker reuses its original reserved scene number cleanly.
    if res1.status_code == 409:
        res1 = worker_generate("Opening scene: bank heist")
    if res2.status_code == 409:
        res2 = worker_generate("Second scene: getaway chase")

    assert res1.status_code == 200, f"res1 error: {res1.json()}"
    assert res2.status_code == 200, f"res2 error: {res2.json()}"

    scene1_num = res1.json()["scene_number"]
    scene2_num = res2.json()["scene_number"]

    # Both must have distinct scene numbers
    assert scene1_num != scene2_num
    assert {scene1_num, scene2_num} == {1, 2}

    # Verify database has both scenes with sequential order
    with sessionmaker(bind=engine)() as s:
        scenes = s.query(Scene).filter(Scene.project_id == project_id).order_by(Scene.order_index).all()
        assert len(scenes) == 2
        assert scenes[0].order_index == 1
        assert scenes[1].order_index == 2


def test_scene_generation_idempotency_repeat_post_and_request_token(test_db_session):
    """
    Requirement 1: Scene-generation idempotency:
    - Repeat an identical POST after successful completion:
      same scene ID, one Scene row in DB, one generation execution, one charge.
    - Intentionally request a new generation using a new request token:
      a new job and second scene are created.
    """
    session, engine = test_db_session
    project_id = f"proj-idemp-{uuid.uuid4().hex[:8]}"
    p = Project(id=project_id, name="Idempotency Test Film", genre="Sci-Fi", budget_total=200.0, budget_spent=0.0)
    session.add(p)
    session.commit()
    PipelineOrchestrator.initialize_project_pipeline(project_id, db=session)

    # 1. First generation request
    res1 = client.post("/api/scenes/generate", json={
        "project_id": project_id,
        "prompt": "Opening hero scene in neon rain"
    })
    assert res1.status_code == 200, res1.json()
    scene1_data = res1.json()
    scene1_id = scene1_data["id"]
    assert scene1_data["scene_number"] == 1

    with sessionmaker(bind=engine)() as s:
        # Exactly 1 Scene created
        scenes = s.query(Scene).filter(Scene.project_id == project_id).all()
        assert len(scenes) == 1
        # Exactly 1 CostRecord
        costs = s.query(CostRecord).filter(CostRecord.project_id == project_id).all()
        assert len(costs) == 1
        # Exactly 1 StudioJob with status COMPLETED
        jobs = s.query(StudioJob).filter(StudioJob.project_id == project_id).all()
        assert len(jobs) == 1
        assert jobs[0].status == "COMPLETED"

    # 2. Repeat the identical POST request: should return cached scene without executing again
    res2 = client.post("/api/scenes/generate", json={
        "project_id": project_id,
        "prompt": "Opening hero scene in neon rain"
    })
    assert res2.status_code == 200
    assert res2.json()["id"] == scene1_id
    assert res2.json()["scene_number"] == 1

    with sessionmaker(bind=engine)() as s:
        # Still exactly 1 Scene, 1 CostRecord, 1 StudioJob
        scenes = s.query(Scene).filter(Scene.project_id == project_id).all()
        assert len(scenes) == 1
        costs = s.query(CostRecord).filter(CostRecord.project_id == project_id).all()
        assert len(costs) == 1
        jobs = s.query(StudioJob).filter(StudioJob.project_id == project_id).all()
        assert len(jobs) == 1

    # 3. Intentionally request a second scene with identical prompt using an explicit request_token
    res3 = client.post("/api/scenes/generate", json={
        "project_id": project_id,
        "prompt": "Opening hero scene in neon rain",
        "request_token": "token-second-scene"
    })
    assert res3.status_code == 200
    scene2_data = res3.json()
    assert scene2_data["id"] != scene1_id
    assert scene2_data["scene_number"] == 2

    with sessionmaker(bind=engine)() as s:
        # Now 2 Scenes, 2 CostRecords, 2 StudioJobs
        scenes = s.query(Scene).filter(Scene.project_id == project_id).order_by(Scene.scene_number).all()
        assert len(scenes) == 2
        costs = s.query(CostRecord).filter(CostRecord.project_id == project_id).all()
        assert len(costs) == 2
        jobs = s.query(StudioJob).filter(StudioJob.project_id == project_id).all()
        assert len(jobs) == 2


def test_scene_generation_identical_request_while_running_returns_in_progress(test_db_session):
    """
    Requirement 1: Identical request while first job is running:
    - Same job ID returned as IN_PROGRESS (HTTP 409).
    - No second AI execution, no second reservation.
    """
    import threading
    session, engine = test_db_session
    project_id = f"proj-running-{uuid.uuid4().hex[:8]}"
    p = Project(id=project_id, name="Running Test Film", genre="Drama", budget_total=100.0, budget_spent=0.0)
    session.add(p)
    session.commit()
    PipelineOrchestrator.initialize_project_pipeline(project_id, db=session)

    ai_started = threading.Event()
    second_request_done = threading.Event()
    second_response = {}

    orig_write = screenwriter_agent.write_scene

    def paused_write_scene(**kwargs):
        ai_started.set()
        # Wait until second request has run and received its response
        second_request_done.wait(timeout=5.0)
        return orig_write(**kwargs)

    def worker_second():
        ai_started.wait(timeout=5.0)
        # Send identical request while first is in Phase B
        r = client.post("/api/scenes/generate", json={
            "project_id": project_id,
            "prompt": "High tension conference room standoff"
        })
        second_response["status"] = r.status_code
        second_response["data"] = r.json()
        second_request_done.set()

    t2 = threading.Thread(target=worker_second)
    t2.start()

    with patch.object(screenwriter_agent, "write_scene", side_effect=paused_write_scene):
        res1 = client.post("/api/scenes/generate", json={
            "project_id": project_id,
            "prompt": "High tension conference room standoff"
        })

    t2.join(timeout=5.0)

    # First request succeeded
    assert res1.status_code == 200, res1.json()

    # Second request received 409 IN_PROGRESS with matching job detail
    assert second_response["status"] == 409
    assert "already in progress" in second_response["data"]["detail"]

    with sessionmaker(bind=engine)() as s:
        # Exactly 1 Scene, 1 CostRecord, 1 StudioJob in total
        scenes = s.query(Scene).filter(Scene.project_id == project_id).all()
        assert len(scenes) == 1
        costs = s.query(CostRecord).filter(CostRecord.project_id == project_id).all()
        assert len(costs) == 1
        jobs = s.query(StudioJob).filter(StudioJob.project_id == project_id).all()
        assert len(jobs) == 1


def test_scene_generation_retry_failed_job_reuses_reserved_scene_number(test_db_session):
    """
    Requirement 1: Retry a failed job:
    - Same job and same reserved scene number.
    - Do not silently create a different scene number.
    """
    session, engine = test_db_session
    project_id = f"proj-fail-retry-{uuid.uuid4().hex[:8]}"
    p = Project(id=project_id, name="Retry Film", genre="Thriller", budget_total=100.0, budget_spent=0.0)
    session.add(p)
    session.commit()
    PipelineOrchestrator.initialize_project_pipeline(project_id, db=session)

    # 1. First attempt fails during AI execution
    with patch.object(screenwriter_agent, "write_scene", side_effect=RuntimeError("Transient AI provider outage")):
        res1 = client.post("/api/scenes/generate", json={
            "project_id": project_id,
            "prompt": "Undercover agent discovers betrayal"
        })
        assert res1.status_code == 500

    with sessionmaker(bind=engine)() as s:
        # Job marked FAILED, reserved scene number was 1
        job = s.query(StudioJob).filter(StudioJob.project_id == project_id).first()
        assert job is not None
        assert job.status == "FAILED"
        payload = json.loads(job.payload)
        assert payload["reserved_scene_number"] == 1
        job_id = job.id

    # 2. Retry with identical request: succeeds and reuses scene number 1 on same job
    res2 = client.post("/api/scenes/generate", json={
        "project_id": project_id,
        "prompt": "Undercover agent discovers betrayal"
    })
    assert res2.status_code == 200, res2.json()
    assert res2.json()["scene_number"] == 1

    with sessionmaker(bind=engine)() as s:
        # Same job was reused, retry_count incremented to 1
        job = s.query(StudioJob).filter(StudioJob.project_id == project_id).first()
        assert job.id == job_id
        assert job.status == "COMPLETED"
        assert job.retry_count == 1

        # Exactly 1 Scene created
        scenes = s.query(Scene).filter(Scene.project_id == project_id).all()
        assert len(scenes) == 1
        assert scenes[0].scene_number == 1


def test_scene_number_validation_zero_negative_duplicate_and_explicit(test_db_session):
    """
    Requirement 6: Validate scene numbers:
    - Enforce scene_number >= 1.
    - Reject zero (422) and negative values (422).
    - Reject duplicate scene numbers (409).
    - Allow valid explicit numbers (e.g. 5).
    """
    session, engine = test_db_session
    project_id = f"proj-val-sc-{uuid.uuid4().hex[:8]}"
    p = Project(id=project_id, name="Val Film", genre="Mystery", budget_total=100.0, budget_spent=0.0)
    session.add(p)
    session.commit()
    PipelineOrchestrator.initialize_project_pipeline(project_id, db=session)

    # 1. Zero value rejected
    res_zero = client.post("/api/scenes/generate", json={
        "project_id": project_id,
        "prompt": "Invalid scene number zero",
        "scene_number": 0
    })
    assert res_zero.status_code == 422

    # 2. Negative value rejected
    res_neg = client.post("/api/scenes/generate", json={
        "project_id": project_id,
        "prompt": "Invalid scene number negative",
        "scene_number": -3
    })
    assert res_neg.status_code == 422

    # 3. Valid explicit number 5
    res_five = client.post("/api/scenes/generate", json={
        "project_id": project_id,
        "prompt": "Explicit scene number five",
        "scene_number": 5
    })
    assert res_five.status_code == 200, res_five.json()
    assert res_five.json()["scene_number"] == 5

    # 4. Duplicate number 5 rejected with 409
    res_dup = client.post("/api/scenes/generate", json={
        "project_id": project_id,
        "prompt": "Another scene requesting number five",
        "scene_number": 5
    })
    assert res_dup.status_code == 409
    assert "already taken or reserved" in res_dup.json()["detail"]


def test_screenplay_snapshot_drift_detects_insertion_during_ai(test_db_session):
    """
    Requirement 3: Validate complete screenplay snapshot:
    - Start generation with a known snapshot (e.g. initial scene 5).
    - Insert a different scene number (scene 6) during AI phase.
    - Verify generation is rejected with 409.
    - Compensation recorded exactly once.
    - Generated scene is NOT committed.
    """
    session, engine = test_db_session
    project_id = f"proj-snap-drift-{uuid.uuid4().hex[:8]}"
    p = Project(id=project_id, name="Snapshot Drift Film", genre="Noir", budget_total=100.0, budget_spent=0.0)
    session.add(p)
    # Existing scene 5
    sc5 = Scene(project_id=project_id, scene_number=5, slugline="INT. MOTEL - NIGHT", order_index=1)
    session.add(sc5)
    session.commit()
    PipelineOrchestrator.initialize_project_pipeline(project_id, db=session)

    orig_write = screenwriter_agent.write_scene

    def inject_scene_during_ai(**kwargs):
        # While AI is running, insert scene 6 into the project
        with sessionmaker(bind=engine)() as s:
            sc6 = Scene(project_id=project_id, scene_number=6, slugline="EXT. ALLEY - NIGHT", order_index=2)
            s.add(sc6)
            s.commit()
        return orig_write(**kwargs)

    with patch.object(screenwriter_agent, "write_scene", side_effect=inject_scene_during_ai):
        res = client.post("/api/scenes/generate", json={
            "project_id": project_id,
            "prompt": "Detective enters the diner"
        })
        assert res.status_code == 409
        assert "drifted" in res.json()["detail"].lower()

    with sessionmaker(bind=engine)() as s:
        # Only sc5 and sc6 exist in DB; the generated scene was NOT committed
        scenes = s.query(Scene).filter(Scene.project_id == project_id).all()
        assert len(scenes) == 2
        scene_nums = {s.scene_number for s in scenes}
        assert scene_nums == {5, 6}

        # Net ledger total is 0.0 (charge was compensated)
        records = s.query(CostRecord).filter(CostRecord.project_id == project_id).all()
        assert len(records) == 2  # 1 charge, 1 refund
        assert sum(r.cost_usd for r in records) == 0.0

        # Project budget_spent is 0.0
        proj = s.query(Project).filter(Project.id == project_id).first()
        assert proj.budget_spent == 0.0


def test_post_charge_failure_paths_compensation_and_job_safety(test_db_session):
    """
    Requirement 4: Cover every post-charge failure path:
    - Selected location deleted during AI execution.
    - Explicitly selected character deleted during AI execution.
    - For every case: exactly one compensation under Policy B, budget spent is 0,
      job marked FAILED, no scene created.
    """
    session, engine = test_db_session

    # Case A: Location deleted during AI execution
    proj_a = f"proj-del-loc-{uuid.uuid4().hex[:8]}"
    p_a = Project(id=proj_a, name="Loc Del Proj", budget_total=100.0, budget_spent=0.0)
    loc_a = Location(id=f"loc-{uuid.uuid4().hex[:8]}", project_id=proj_a, name="Old Warehouse")
    session.add_all([p_a, loc_a])
    session.commit()
    PipelineOrchestrator.initialize_project_pipeline(proj_a, db=session)

    orig_write = screenwriter_agent.write_scene

    def delete_location_during_ai(**kwargs):
        with sessionmaker(bind=engine)() as s:
            s.query(Location).filter(Location.id == loc_a.id).delete()
            s.commit()
        return orig_write(**kwargs)

    with patch.object(screenwriter_agent, "write_scene", side_effect=delete_location_during_ai):
        res_a = client.post("/api/scenes/generate", json={
            "project_id": proj_a,
            "prompt": "Meeting in the warehouse",
            "location_id": loc_a.id
        })
        assert res_a.status_code in (400, 404, 409)

    with sessionmaker(bind=engine)() as s:
        # Budget spent is 0.0
        p_check = s.query(Project).filter(Project.id == proj_a).first()
        assert p_check.budget_spent == 0.0
        # Exactly 1 charge and 1 refund
        recs = s.query(CostRecord).filter(CostRecord.project_id == proj_a).all()
        assert len(recs) == 2
        assert sum(r.cost_usd for r in recs) == 0.0
        # No scene created
        assert s.query(Scene).filter(Scene.project_id == proj_a).count() == 0
        # Job marked FAILED
        job = s.query(StudioJob).filter(StudioJob.project_id == proj_a).first()
        assert job.status == "FAILED"

    # Case B: Character deleted during AI execution
    proj_b = f"proj-del-char-{uuid.uuid4().hex[:8]}"
    p_b = Project(id=proj_b, name="Char Del Proj", budget_total=100.0, budget_spent=0.0)
    char_b = Character(id=f"char-{uuid.uuid4().hex[:8]}", project_id=proj_b, name="TARGET CHARACTER")
    session.add_all([p_b, char_b])
    session.commit()
    PipelineOrchestrator.initialize_project_pipeline(proj_b, db=session)

    def delete_char_during_ai(**kwargs):
        with sessionmaker(bind=engine)() as s:
            s.query(Character).filter(Character.id == char_b.id).delete()
            s.commit()
        return orig_write(**kwargs)

    with patch.object(screenwriter_agent, "write_scene", side_effect=delete_char_during_ai):
        res_b = client.post("/api/scenes/generate", json={
            "project_id": proj_b,
            "prompt": "Dialogue with character",
            "character_ids": [char_b.id]
        })
        assert res_b.status_code in (400, 404, 409)

    with sessionmaker(bind=engine)() as s:
        p_check = s.query(Project).filter(Project.id == proj_b).first()
        assert p_check.budget_spent == 0.0
        recs = s.query(CostRecord).filter(CostRecord.project_id == proj_b).all()
        assert len(recs) == 2
        assert sum(r.cost_usd for r in recs) == 0.0
        assert s.query(Scene).filter(Scene.project_id == proj_b).count() == 0
        job = s.query(StudioJob).filter(StudioJob.project_id == proj_b).first()
        assert job.status == "FAILED"


def test_effective_generation_backend_and_offline_fallback(test_db_session):
    """
    Requirement 5: Hash effective generation backend and runtime configuration:
    - Simulate has_gemini = True but force Gemini API failure and offline fallback.
    - Verify result metadata, cost record, and execution fingerprint identify Offline Studio Engine.
    - Verify fallback execution is not represented as Gemini execution.
    - Verify runtime provider/model change cannot silently reuse a result from a different provider.
    """
    session, engine = test_db_session
    project_id = f"proj-fallback-{uuid.uuid4().hex[:8]}"
    p = Project(id=project_id, name="Fallback Film", genre="Sci-Fi", budget_total=100.0, budget_spent=0.0)
    session.add(p)
    session.commit()
    PipelineOrchestrator.initialize_project_pipeline(project_id, db=session)

    orig_api_key = gemini_service.api_key
    orig_client = gemini_service._client

    try:
        # 1. Force Gemini client to fail and fall back to Offline Studio Engine
        gemini_service.api_key = "fake-key"
        mock_client = MagicMock()
        gemini_service._client = mock_client
        mock_client.models.generate_content.side_effect = RuntimeError("Gemini 503 Unavailable")

        res = client.post("/api/scenes/generate", json={
            "project_id": project_id,
            "prompt": "Space station docking sequence"
        })
        assert res.status_code == 200, res.json()

        with sessionmaker(bind=engine)() as s:
            job = s.query(StudioJob).filter(StudioJob.project_id == project_id).first()
            assert job is not None
            assert job.status == "COMPLETED"
            res_payload = json.loads(job.result)

            # Execution fingerprint identifies offline_engine with is_fallback=True
            assert res_payload["effective_provider"] == "offline_engine"
            assert res_payload["is_fallback"] is True
            assert res_payload["effective_fingerprint"] is not None

            # Cost record records offline_engine, not gemini
            cost = s.query(CostRecord).filter(CostRecord.project_id == project_id).first()
            assert cost.provider == "offline_engine"

        # 2. Runtime provider change: Gemini is now healthy. Repeating request must NOT silently reuse offline result.
        mock_response = MagicMock()
        mock_response.text = json.dumps({
            "title": "Scene 1: Docking Complete",
            "slugline": "EXT. STATION - NIGHT",
            "summary": "Gemini generated dramatic docking.",
            "script_content": "EXT. STATION - NIGHT\nAir hissed through the seals.",
            "estimated_duration": 15
        })
        mock_response.usage_metadata = MagicMock(prompt_token_count=100, candidates_token_count=50, total_token_count=150)
        mock_client.models.generate_content.side_effect = None
        mock_client.models.generate_content.return_value = mock_response

        res_gemini = client.post("/api/scenes/generate", json={
            "project_id": project_id,
            "prompt": "Space station docking sequence"
        })
        assert res_gemini.status_code == 200

        with sessionmaker(bind=engine)() as s:
            job_after = s.query(StudioJob).filter(StudioJob.project_id == project_id).first()
            res_after = json.loads(job_after.result)
            # Result was regenerated with Gemini, not silently reused from offline fallback!
            assert res_after["effective_provider"] == "gemini"
            assert res_after["is_fallback"] is False

    finally:
        gemini_service.api_key = orig_api_key
        gemini_service._client = orig_client



