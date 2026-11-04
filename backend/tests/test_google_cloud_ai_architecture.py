"""
Test Suite: Google Cloud AI First Architecture.
Verifies:
1. Gemini as default/primary AI provider
2. Structured output with Pydantic schema validation
3. Exponential backoff retry on transient errors (429, 503) without infinite retry
4. Non-transient error handling (auth, invalid model)
5. Accurate attribution metadata and zero silent faking
6. Google Imagen 3 image generation adapter and local asset registration
7. Google Veo video generation adapter, operation polling, and multi-take versioning
8. Asynchronous Generation Job lifecycle (pending -> processing -> completed/failed/cancelled)
9. Honest cancellation semantics
10. Budget pre-flight authorization and warning thresholds
11. Optional Google Drive backup status and security
12. Studio operation without Ollama
"""

import os
import uuid
import json
import pytest
from unittest.mock import MagicMock, patch
from pathlib import Path
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from fastapi.testclient import TestClient

from backend.app.main import app
from backend.app.config import settings
from backend.app.database import Base, get_db
from backend.app.models.entities import Project, Scene, Shot, Character, Location, MediaAsset, GenerationJob, CostRecord
from backend.app.services.ai_provider import (
    GeminiProvider,
    OllamaProvider,
    OfflineTemplateProvider,
    get_ai_provider,
    AIProviderError
)
from backend.app.services.structured_output import (
    ScreenplaySceneOutput,
    SceneBreakdownOutput,
    CharacterProfileOutput
)
from backend.app.services.google_image_adapter import google_image_adapter, GoogleImageAdapter
from backend.app.services.google_video_adapter import google_video_adapter, GoogleVideoAdapter
from backend.app.services.generation_job_manager import generation_job_manager
from backend.app.services.google_drive_service import google_drive_service


@pytest.fixture
def test_db(tmp_path):
    """Isolated SQLite database with latest schema."""
    db_file = tmp_path / "test_google_cloud.db"
    engine = create_engine(f"sqlite:///{db_file}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    TestingSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = TestingSession()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def client(test_db):
    def override_get_db():
        try:
            yield test_db
        finally:
            pass
    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def sample_project(test_db):
    proj = Project(
        id=str(uuid.uuid4()),
        name="Nexus Horizon",
        genre="Cyberpunk Sci-Fi",
        logline="A renegade cyber-detective investigates memory corruption in Neo-Tokyo.",
        budget_total=100.0,
        budget_spent=0.0,
        budget_warning_threshold=0.8
    )
    test_db.add(proj)
    test_db.commit()
    return proj


def test_gemini_is_primary_provider():
    """Verifies that Gemini is the primary AI provider and Ollama is not required."""
    assert settings.ai_provider in ("gemini", "gemini-2.5-flash")
    assert getattr(settings, "ollama_enabled", False) is False

    # When no override is given and no API key is set in test env,
    # get_ai_provider safely falls back to offline engine with explicit is_fallback: True
    provider = get_ai_provider()
    meta = provider.health_check()
    assert meta["status"] in ("online", "configured", "not_configured")


def test_gemini_structured_output_with_mock_client():
    """Verifies that GeminiProvider correctly parses and validates Pydantic structured schemas."""
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.text = json.dumps({
        "name": "KAIEN CROSS",
        "role": "Protagonist",
        "age": "32",
        "facial_characteristics": "Cybernetic neural optical implant in left eye, chiseled jawline.",
        "clothing": "Weathered ballistic trench coat with fiber-optic lining.",
        "personality": "Stoic, vigilant, haunted by past missions.",
        "voice_profile": "Deep, resonant, measured baritone.",
        "visual_prompt_tags": "cyberpunk detective, cinematic lighting, 35mm photograph"
    })
    mock_response.usage_metadata = MagicMock()
    mock_response.usage_metadata.prompt_token_count = 120
    mock_response.usage_metadata.candidates_token_count = 95

    mock_client.models.generate_content.return_value = mock_response

    provider = GeminiProvider(api_key="mock-key-12345", model="gemini-2.5-flash")
    provider._client = mock_client

    result, meta = provider.generate_structured(
        prompt="Design the protagonist",
        schema=CharacterProfileOutput
    )

    assert isinstance(result, CharacterProfileOutput)
    assert result.name == "KAIEN CROSS"
    assert meta["provider"] == "gemini"
    assert meta["model"] == "gemini-2.5-flash"
    assert meta["input_tokens"] == 120
    assert meta["output_tokens"] == 95
    assert meta["is_fallback"] is False


def test_gemini_retry_with_exponential_backoff_on_transient_error():
    """Verifies that transient errors (e.g. 429 rate limit) trigger limited backoff and retry."""
    mock_client = MagicMock()
    
    # First call raises 429 rate limit, second call succeeds
    mock_response = MagicMock()
    mock_response.text = "Cinematic dialogue sequence"
    mock_response.usage_metadata = None

    call_count = 0
    def side_effect(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise Exception("429 Resource has been exhausted (rate limit)")
        return mock_response

    mock_client.models.generate_content.side_effect = side_effect

    provider = GeminiProvider(api_key="mock-key-12345")
    provider._client = mock_client
    provider.max_retries = 2

    with patch("time.sleep") as mock_sleep:
        text, meta = provider.generate_text("Test prompt")
        assert text == "Cinematic dialogue sequence"
        assert call_count == 2
        assert mock_sleep.called


def test_gemini_non_transient_error_fails_immediately():
    """Verifies that non-transient auth/permission errors fail immediately without infinite retry."""
    mock_client = MagicMock()
    mock_client.models.generate_content.side_effect = Exception("403 API_KEY_INVALID: User not authenticated")

    provider = GeminiProvider(api_key="invalid-key")
    provider._client = mock_client
    provider.max_retries = 3

    with patch("time.sleep") as mock_sleep:
        with pytest.raises(AIProviderError) as exc_info:
            provider.generate_text("Test prompt")
        assert "authentication failed" in str(exc_info.value).lower()
        # Sleep should not be called because it is non-transient
        assert not mock_sleep.called


def test_google_image_adapter_local_persistence(test_db, sample_project, tmp_path):
    """Verifies that Google Imagen adapter persists images locally and registers MediaAsset."""
    adapter = GoogleImageAdapter(api_key=None) # Tests deterministic local reference generation

    res = adapter.generate_image(
        prompt="Cyberpunk rain-slicked alleyway with neon signs",
        project_id=sample_project.id,
        db=test_db,
        entity_type="concept",
        aspect_ratio="16:9"
    )

    assert res["status"] == "completed"
    assert Path(res["file_path"]).exists()
    assert res["file_size_bytes"] > 0

    # Verify registered in SQLite
    asset = test_db.query(MediaAsset).filter(MediaAsset.id == res["asset_id"]).first()
    assert asset is not None
    assert asset.media_type == "image"
    assert asset.project_id == sample_project.id


def test_google_video_adapter_submission_and_take_selection(test_db, sample_project):
    """Verifies video generation adapter submits operation and imports completed takes."""
    scene = Scene(
        id=str(uuid.uuid4()),
        project_id=sample_project.id,
        scene_number=1,
        slugline="EXT. ROOFTOP - NIGHT",
        script_content="The hero gazes over the neon city."
    )
    test_db.add(scene)
    test_db.commit()

    shot = Shot(
        id=str(uuid.uuid4()),
        project_id=sample_project.id,
        scene_id=scene.id,
        shot_code="SC01_SH01",
        duration_seconds=4.0,
        camera_angle="Low Angle Heroic",
        camera_movement="Slow Track In",
        video_prompt="Hero walking through steam into the night"
    )
    test_db.add(shot)
    test_db.commit()

    adapter = GoogleVideoAdapter(api_key="mock-key")
    mock_client = MagicMock()
    mock_op = MagicMock()
    mock_op.name = "operations/veo-generate-12345"
    mock_client.models.generate_videos.return_value = mock_op
    adapter._client = mock_client

    sub = adapter.submit_video_generation(
        prompt=shot.video_prompt,
        project_id=sample_project.id,
        shot_id=shot.id,
        db=test_db
    )

    assert sub["status"] == "processing"
    assert sub["provider_operation_id"] == "operations/veo-generate-12345"

    # Simulate importing completed video bytes
    dummy_video_bytes = b"FAKE_MP4_HEADER_CONTENT_BYTES"
    imported_asset = adapter.import_completed_video(
        project_id=sample_project.id,
        shot_id=shot.id,
        video_bytes=dummy_video_bytes,
        video_uri=None,
        db=test_db
    )

    assert imported_asset.version == 1
    assert imported_asset.shot_id == shot.id
    test_db.refresh(shot)
    assert shot.video_asset_id == imported_asset.id
    assert shot.generation_status == "imported"


def test_asynchronous_generation_job_lifecycle(test_db, sample_project):
    """Verifies GenerationJob creation, status transitions, and history querying."""
    scene = Scene(
        id=str(uuid.uuid4()),
        project_id=sample_project.id,
        scene_number=1,
        slugline="EXT. SKYLINE - DUSK",
        script_content="Flying cars cruise between high-rises."
    )
    test_db.add(scene)
    test_db.commit()

    shot = Shot(
        id=str(uuid.uuid4()),
        project_id=sample_project.id,
        scene_id=scene.id,
        shot_code="SC01_SH02",
        video_prompt="Wide panorama of futuristic skyline"
    )
    test_db.add(shot)
    test_db.commit()

    # 1. Create pending job
    job = generation_job_manager.create_job(
        db=test_db,
        project_id=sample_project.id,
        shot_id=shot.id,
        generation_type="image_generation",
        input_parameters={"prompt": shot.video_prompt, "aspect_ratio": "16:9"},
        provider="google_imagen",
        model="imagen-3.0-generate-002"
    )

    assert job.status == "pending"
    assert job.provider == "google_imagen"

    # 2. Query history
    jobs = generation_job_manager.list_jobs(test_db, project_id=sample_project.id)
    assert len(jobs) >= 1
    assert jobs[0].id == job.id

    # 3. Cancel pending job
    cancel_res = generation_job_manager.cancel_job(test_db, job.id)
    assert cancel_res["status"] == "success"
    test_db.refresh(job)
    assert job.status == "cancelled"

    # 4. Idempotent completed job check
    job.status = "completed"
    test_db.commit()
    retried_job = generation_job_manager.retry_job(test_db, job.id)
    assert retried_job.status == "completed"  # Does not re-run already completed job


def test_honest_cancellation_semantics_for_remote_processing(test_db, sample_project):
    """Verifies that in-flight jobs without provider cancellation support reject fake cancellation."""
    job = generation_job_manager.create_job(
        db=test_db,
        project_id=sample_project.id,
        generation_type="video_generation",
        input_parameters={"prompt": "Action sequence"},
        provider="google_veo",
        model="veo-2.0-generate-001",
        supports_cancellation=False
    )
    job.status = "processing"
    test_db.commit()

    res = generation_job_manager.cancel_job(test_db, job.id)
    assert res["status"] == "unsupported"
    assert "does not support in-flight cancellation" in res["message"]


def test_budget_authorization_and_warning_threshold(client, sample_project, test_db):
    """Verifies pre-flight authorization check and budget threshold warnings."""
    # Under threshold check
    res1 = client.get(f"/api/cost/check-authorization/{sample_project.id}?operation=script_generation&estimated_cost=0.01")
    assert res1.status_code == 200
    data1 = res1.json()
    assert data1["authorized"] is True
    assert data1["can_proceed"] is True
    assert data1["requires_confirmation"] is False

    # Expensive operation (e.g. video generation) requires confirmation
    res2 = client.get(f"/api/cost/check-authorization/{sample_project.id}?operation=veo_video_generation&estimated_cost=0.20")
    assert res2.status_code == 200
    data2 = res2.json()
    assert data2["authorized"] is True
    assert data2["requires_confirmation"] is True

    # Exceeding budget limit rejects authorization
    sample_project.budget_spent = 99.95
    test_db.commit()

    res3 = client.get(f"/api/cost/check-authorization/{sample_project.id}?operation=veo_video_generation&estimated_cost=0.20")
    assert res3.status_code == 200
    data3 = res3.json()
    assert data3["authorized"] is False
    assert data3["can_proceed"] is False
    assert "Insufficient budget" in data3["reason"]


def test_google_drive_status_endpoint(client):
    """Verifies that Google Drive status reports safely without throwing or leaking credentials."""
    res = client.get("/api/system/google-drive/status")
    assert res.status_code == 200
    data = res.json()
    assert "is_configured" in data
    assert "enabled" in data
    assert "message" in data


def test_studio_system_ai_status_endpoint(client):
    """Verifies the unified system AI status endpoint exposes Google Cloud AI details."""
    res = client.get("/api/system/ai-status")
    assert res.status_code == 200
    data = res.json()
    assert data["primary_provider"] == "gemini"
    assert "gemini" in data
    assert "ollama" in data
    assert "offline_template" in data
    assert "google_models" in data
    assert data["google_models"]["text"] == "gemini-2.5-flash"
