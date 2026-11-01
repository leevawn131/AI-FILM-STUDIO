import pytest
import concurrent.futures
from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend.app.main import app
from backend.app.database import SessionLocal
from backend.app.models.entities import Project, Scene, CostRecord
from backend.app.services.cost_controller import cost_controller

client = TestClient(app)

def test_provider_aware_ledger_and_credit_separation():
    """Verifies that flow_credits, providers, and unknown costs are correctly recorded."""
    db = SessionLocal()
    try:
        # Create a test project
        project = Project(
            name="Ledger Test Project",
            genre="Sci-Fi",
            budget_total=50.0,
            budget_spent=0.0
        )
        db.add(project)
        db.commit()
        db.refresh(project)

        # 1. Flow Video Prompt Pack (Uses Flow Credits, zero dollar charge)
        flow_record = cost_controller.authorize_and_charge(
            db=db,
            project_id=project.id,
            action_type="flow_prompt_pack",
            provider="google_flow",
            model="google-flow-v1",
            operation="flow_video_prompt",
            flow_credits=1.0,
            actual_cost=None,
            actual_cost_known=False,
            payload={"prompt": "neon city flyover"},
            details="Google Flow prompt pack generation"
        )
        assert flow_record.provider == "google_flow"
        assert flow_record.model == "google-flow-v1"
        assert flow_record.flow_credits == 1.0
        assert flow_record.actual_cost is None
        assert flow_record.actual_cost_known is False
        assert flow_record.cost_usd == 0.0
        # Flow credits did not consume currency budget
        assert project.budget_spent == 0.0

        # 2. Gemini Screenplay Generation (Tracks tokens, estimated cost)
        gemini_record = cost_controller.authorize_and_charge(
            db=db,
            project_id=project.id,
            action_type="script_generation",
            provider="gemini",
            model="gemini-2.5-flash",
            operation="screenplay_draft",
            input_tokens=1500,
            output_tokens=800,
            estimated_cost=0.015,
            actual_cost=None,
            actual_cost_known=False,
            payload={"scene": "INT. LAB - NIGHT"},
            details="Gemini screenplay generation"
        )
        assert gemini_record.provider == "gemini"
        assert gemini_record.input_tokens == 1500
        assert gemini_record.output_tokens == 800
        assert gemini_record.estimated_cost == 0.015
        assert gemini_record.actual_cost is None
        assert gemini_record.actual_cost_known is False
        assert project.budget_spent == 0.015

        # 3. Known Actual Cost
        render_record = cost_controller.authorize_and_charge(
            db=db,
            project_id=project.id,
            action_type="video_render",
            provider="local_ffmpeg",
            operation="final_render",
            actual_cost=0.05,
            actual_cost_known=True,
            payload={"export": "master_1080p"},
            details="Local FFmpeg master render"
        )
        assert render_record.actual_cost == 0.05
        assert render_record.actual_cost_known is True
        assert round(project.budget_spent, 3) == 0.065

        # Cleanup
        db.delete(project)
        db.commit()
    finally:
        db.close()

def test_idempotent_duplicate_job_prevention():
    """Verifies that submitting the same payload returns the existing record without duplicate charging."""
    db = SessionLocal()
    try:
        project = Project(
            name="Idempotency Test Project",
            genre="Thriller",
            budget_total=20.0,
            budget_spent=0.0
        )
        db.add(project)
        db.commit()
        db.refresh(project)

        payload = {"shot_id": "shot-123", "prompt": "detective enters dark alley"}

        # First execution
        rec1 = cost_controller.authorize_and_charge(
            db=db,
            project_id=project.id,
            action_type="storyboard_generation",
            estimated_cost=0.10,
            payload=payload,
            allow_duplicate=False
        )
        initial_spent = project.budget_spent
        assert initial_spent == 0.10

        # Duplicate execution with exact same payload
        rec2 = cost_controller.authorize_and_charge(
            db=db,
            project_id=project.id,
            action_type="storyboard_generation",
            estimated_cost=0.10,
            payload=payload,
            allow_duplicate=False
        )

        # Same record returned, no duplicate charge
        assert rec1.id == rec2.id
        assert project.budget_spent == initial_spent

        # Verify only 1 record exists in DB
        records = db.query(CostRecord).filter(CostRecord.project_id == project.id).all()
        assert len(records) == 1

        db.delete(project)
        db.commit()
    finally:
        db.close()

def test_project_budget_limit_enforcement():
    """Verifies that operations exceeding project budget are rejected with HTTP 400."""
    db = SessionLocal()
    try:
        project = Project(
            name="Strict Budget Project",
            genre="Drama",
            budget_total=1.0, # $1.00 limit
            budget_spent=0.90
        )
        db.add(project)
        db.commit()
        db.refresh(project)

        # Attempting operation costing $0.15 exceeds remaining $0.10
        with pytest.raises(HTTPException) as exc:
            cost_controller.authorize_and_charge(
                db=db,
                project_id=project.id,
                action_type="heavy_render",
                estimated_cost=0.15,
                payload={"render": "heavy"}
            )
        assert exc.value.status_code == 400
        assert "Project budget limit reached" in exc.value.detail
        assert project.budget_spent == 0.90  # Unchanged

        db.delete(project)
        db.commit()
    finally:
        db.close()

def test_scene_budget_limit_enforcement():
    """Verifies that operations exceeding a specific scene's budget are rejected with HTTP 400."""
    db = SessionLocal()
    try:
        project = Project(
            name="Scene Budget Project",
            genre="Action",
            budget_total=100.0,
            budget_spent=0.0
        )
        db.add(project)
        db.commit()
        db.refresh(project)

        scene = Scene(
            project_id=project.id,
            scene_number=1,
            slugline="EXT. MOUNTAIN - DAY",
            budget_limit=0.50 # $0.50 scene limit
        )
        db.add(scene)
        db.commit()
        db.refresh(scene)

        # First operation on scene: $0.40 (within $0.50)
        cost_controller.authorize_and_charge(
            db=db,
            project_id=project.id,
            scene_id=scene.id,
            action_type="scene_shot_1",
            estimated_cost=0.40,
            payload={"shot": 1}
        )

        # Second operation on scene: $0.20 ($0.40 + $0.20 = $0.60 > $0.50 limit)
        with pytest.raises(HTTPException) as exc:
            cost_controller.authorize_and_charge(
                db=db,
                project_id=project.id,
                scene_id=scene.id,
                action_type="scene_shot_2",
                estimated_cost=0.20,
                payload={"shot": 2}
            )
        assert exc.value.status_code == 400
        assert "Scene budget limit exceeded" in exc.value.detail

        db.delete(project)
        db.commit()
    finally:
        db.close()

def test_concurrent_budget_requests_thread_safety():
    """
    Verifies that concurrent requests deducting from a budget cannot overspend
    due to race conditions, protected by thread lock.
    """
    db = SessionLocal()
    try:
        project = Project(
            name="Concurrent Safety Project",
            genre="Action",
            budget_total=5.0,  # $5.00 total budget
            budget_spent=0.0
        )
        db.add(project)
        db.commit()
        project_id = project.id
    finally:
        db.close()

    charge_amount = 0.50
    # 20 workers trying to charge $0.50 each ($10.00 total requested on a $5.00 budget)
    # Exactly 10 should succeed, and 10 should be rejected with HTTP 400.

    def charge_worker(worker_id: int):
        worker_db = SessionLocal()
        try:
            cost_controller.authorize_and_charge(
                db=worker_db,
                project_id=project_id,
                action_type=f"concurrent_op_{worker_id}",
                estimated_cost=charge_amount,
                payload={"worker": worker_id},
                allow_duplicate=True
            )
            return True
        except HTTPException as e:
            if e.status_code == 400:
                return False
            raise
        finally:
            worker_db.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(charge_worker, i) for i in range(20)]
        results = [f.result() for f in futures]

    successes = results.count(True)
    failures = results.count(False)

    verify_db = SessionLocal()
    try:
        p = verify_db.query(Project).filter(Project.id == project_id).first()
        assert successes == 10
        assert failures == 10
        assert round(p.budget_spent, 2) == 5.00
        assert p.budget_spent <= p.budget_total

        verify_db.delete(p)
        verify_db.commit()
    finally:
        verify_db.close()
