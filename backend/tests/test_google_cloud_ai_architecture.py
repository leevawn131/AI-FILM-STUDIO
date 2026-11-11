"""
Test Suite: Google Cloud AI Ecosystem Architecture.
Verified baseline: November 5, 2026.

Verifies:
1. Gemini 3.8 Flash as default/primary AI reasoning engine (`gemini-3.8-flash`)
2. Structured output with Pydantic schema validation & self-correction retry
3. Exponential backoff retry on transient errors (429, 503) without infinite retry
4. Non-transient error handling (auth failure, invalid model)
5. Accurate attribution metadata and zero silent faking
6. Google Nano Banana 2 image generation adapter (`gemini-3.1-flash-image`) and local persistence
7. Google Gemini Omni Flash 1.1 video generation via Interactions API (`client.interactions.create()`)
8. Google Gemini TTS audio generation (`gemini-3.8-flash-tts`) with prebuilt voices & database registration
9. Google Lyria 3.5 music generation (`lyria-3.5`) via Interactions API with database registration
10. Asynchronous Generation Job lifecycle (pending -> processing -> completed/failed/cancelled)
11. Honest cancellation semantics (never fake remote cancellation)
12. Budget pre-flight authorization and warning thresholds
13. Optional Google Drive backup status and credential security
14. Masked API keys in all system endpoints (never leak raw secrets)
15. Zero API quota consumed during offline test runs
"""

import os
import uuid
import json
import base64
import pytest
from unittest.mock import MagicMock, patch
from pathlib import Path
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from fastapi.testclient import TestClient

from backend.app.main import app
from backend.app.config import settings
from backend.app.database import Base, get_db
from backend.app.models.entities import (
    Project, Scene, Shot, Character, Location, MediaAsset,
    GenerationJob, CostRecord, AudioTrack
)
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
from backend.app.services.google_tts_adapter import google_tts_adapter, GoogleTTSAdapter, SUPPORTED_VOICES
from backend.app.services.google_music_adapter import google_music_adapter, GoogleMusicAdapter
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
    """Verifies that Gemini 3.8 Flash is the primary AI provider and Ollama is disabled by default."""
    assert settings.ai_provider == "gemini"
    assert settings.gemini_model == "gemini-3.8-flash"
    assert getattr(settings, "ollama_enabled", False) is False

    provider = get_ai_provider()
    assert provider is not None


def test_gemini_structured_output_with_mock_client():
    """Verifies GeminiProvider validates JSON schema into Pydantic models."""
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.text = json.dumps({
        "title": "NEO-TOKYO SHADOWS",
        "slugline": "INT. DETECTIVE OFFICE - NIGHT",
        "summary": "Ren inspects a decrypted memory cube while rain streaks the window.",
        "script_content": "REN sits at his desk. Rain taps against the reinforced glass.",
        "estimated_duration": 45.0
    })
    mock_response.usage_metadata.prompt_token_count = 120
    mock_response.usage_metadata.candidates_token_count = 80

    mock_client.models.generate_content.return_value = mock_response

    provider = GeminiProvider(api_key="mock-key-12345", model="gemini-3.8-flash")
    provider._client = mock_client

    result, meta = provider.generate_structured(
        prompt="Write scene 1 for Nexus Horizon",
        schema=ScreenplaySceneOutput,
        system_prompt="You are a master Hollywood Screenwriter."
    )

    assert isinstance(result, ScreenplaySceneOutput)
    assert result.title == "NEO-TOKYO SHADOWS"
    assert result.slugline == "INT. DETECTIVE OFFICE - NIGHT"
    assert meta["provider"] == "gemini"
    assert meta["model"] == "gemini-3.8-flash"
    assert meta["input_tokens"] == 120
    assert meta["output_tokens"] == 80
    assert meta["is_fallback"] is False


def test_gemini_retry_with_exponential_backoff_on_transient_error():
    """Verifies that transient 429/503 errors trigger backoff retry up to max_retries."""
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.text = "Cinematic dialogue sequence"
    mock_response.usage_metadata.prompt_token_count = 50
    mock_response.usage_metadata.candidates_token_count = 20

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
        assert not mock_sleep.called


def test_google_image_adapter_nano_banana_fallback(test_db, sample_project):
    """Verifies that Nano Banana adapter generates deterministic offline SVG fallback when key is not configured."""
    adapter = GoogleImageAdapter(api_key=None)

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
    assert res["is_fallback"] is True

    # Verify registered in SQLite
    asset = test_db.query(MediaAsset).filter(MediaAsset.id == res["asset_id"]).first()
    assert asset is not None
    assert asset.media_type == "image"
    assert asset.project_id == sample_project.id


def test_google_image_adapter_nano_banana_with_mock_client(test_db, sample_project):
    """Verifies Nano Banana 2 image generation with mock client saving real bytes and inspecting true PIL dimensions."""
    import io
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (640, 480), color="blue").save(buf, format="JPEG")
    valid_jpeg = buf.getvalue()

    adapter = GoogleImageAdapter(api_key="mock-api-key")
    mock_client = MagicMock()
    mock_interaction = MagicMock()
    mock_interaction.id = "interactions/img-456"
    mock_interaction.output_image.data = base64.b64encode(valid_jpeg).decode("utf-8")
    mock_client.interactions.create.return_value = mock_interaction
    adapter._client = mock_client

    res = adapter.generate_image(
        prompt="Neo-Tokyo detective standing under holograms",
        project_id=sample_project.id,
        db=test_db,
        entity_type="character",
        aspect_ratio="16:9",
        image_size="1K",
        tier="default"
    )

    assert res["status"] == "completed"
    assert res["provider"] == "google_nano_banana"
    assert res["model"] == "gemini-3.1-flash-image"
    assert res["width"] == 640
    assert res["height"] == 480
    assert res["filename"].endswith(".jpg")
    assert res["is_fallback"] is False
    assert Path(res["file_path"]).exists()


def test_google_video_adapter_interactions_api_lifecycle(test_db, sample_project):
    """Verifies Gemini Omni Flash 1.1 video generation via Interactions API."""
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
    mock_interaction = MagicMock()
    mock_interaction.id = "interactions/omni-video-98765"
    mock_interaction.status = "processing"
    mock_client.interactions.create.return_value = mock_interaction
    adapter._client = mock_client

    # 1. Submit video generation via Interactions API
    sub = adapter.submit_video_generation(
        prompt=shot.video_prompt,
        project_id=sample_project.id,
        shot_id=shot.id,
        db=test_db,
        task="text_to_video"
    )

    assert sub["status"] == "processing"
    assert sub["provider"] == "google_omni"
    assert sub["model"] == "gemini-omni-1.1-flash"
    assert sub["provider_operation_id"] == "interactions/omni-video-98765"
    assert sub["supports_cancellation"] is True

    # 2. Poll completed status
    mock_completed = MagicMock()
    mock_completed.status = "completed"
    mock_completed.output_video.data = base64.b64encode(b"FAKE_OMNI_FLASH_MP4_BYTES").decode("utf-8")
    mock_completed.output_video.mime_type = "video/mp4"
    mock_completed.output_video.uri = None
    mock_completed.output_audio = None
    mock_client.interactions.get.return_value = mock_completed

    poll_res = adapter.poll_operation_status("interactions/omni-video-98765")
    assert poll_res["status"] == "completed"
    assert poll_res["video_bytes"] == b"FAKE_OMNI_FLASH_MP4_BYTES"

    # 3. Import completed video locally and update Shot take selection
    imported_asset = adapter.import_completed_video(
        project_id=sample_project.id,
        shot_id=shot.id,
        video_bytes=poll_res["video_bytes"],
        db=test_db
    )

    assert imported_asset.version == 1
    assert imported_asset.shot_id == shot.id
    test_db.refresh(shot)
    assert shot.video_asset_id == imported_asset.id
    assert shot.generation_status == "ready"

    # 4. Cancellation via Interactions API
    mock_client.interactions.cancel.return_value = {"status": "cancelled"}
    cancel_res = adapter.cancel_operation("interactions/omni-video-98765")
    assert cancel_res["status"] == "cancelled"


def test_google_tts_adapter_dialogue_generation(test_db, sample_project):
    """Verifies Google Gemini TTS generates audio via Interactions API with speech_metadata."""
    # Verify voices catalog
    voices = GoogleTTSAdapter.get_available_voices()
    assert "Puck" in voices
    assert "Charon" in voices
    assert "Kore" in voices
    assert "Fenrir" in voices
    assert "Aoede" in voices

    adapter = GoogleTTSAdapter(api_key="mock-key")
    mock_client = MagicMock()
    mock_interaction = MagicMock()
    mock_interaction.id = "interactions/tts-turn-987"
    # Valid RIFF WAV header (mono, 44.1kHz, 16-bit)
    wav_bytes = b"RIFF$\x00\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00D\xac\x00\x00\x88X\x01\x00\x02\x00\x10\x00data\x00\x00\x00\x00"
    mock_interaction.output_audio.data = base64.b64encode(wav_bytes).decode("utf-8")
    mock_client.interactions.create.return_value = mock_interaction
    adapter._client = mock_client

    res = adapter.generate_dialogue_speech(
        text="The core is destabilizing. We have thirty seconds.",
        project_id=sample_project.id,
        db=test_db,
        voice_name="Charon",
        character_name="REN",
        emotion="urgent"
    )

    assert res["status"] == "completed"
    assert res["provider"] == "google_tts"
    assert res["model"] == "gemini-3.8-flash-tts"
    assert res["voice_name"] == "Charon"
    assert res["interaction_id"] == "interactions/tts-turn-987"
    assert Path(res["file_path"]).exists()

    # Verify Interactions API call payload schema per documented contract
    call_kwargs = mock_client.interactions.create.call_args[1]
    assert call_kwargs["response_format"] == {"type": "audio"}
    assert call_kwargs["generation_config"]["speech_config"] == [{"voice": "Charon"}]
    assert isinstance(call_kwargs["input"], list)
    assert call_kwargs["input"][0]["type"] == "user_input"
    assert call_kwargs["input"][0]["content"][0]["type"] == "text"
    assert call_kwargs["input"][0]["content"][0]["text"] == "The core is destabilizing. We have thirty seconds."
    assert call_kwargs["input"][0]["content"][0]["annotations"][0]["type"] == "speech_metadata"
    assert call_kwargs["input"][0]["content"][0]["annotations"][0]["speaker"] == "REN"
    assert "urgent" in call_kwargs["input"][0]["content"][0]["annotations"][0]["style"]

    # Verify AudioTrack in DB
    track = test_db.query(AudioTrack).filter(AudioTrack.id == res["track_id"]).first()
    assert track is not None
    assert track.track_type == "dialogue"
    assert "REN" in track.title

    # Verify MediaAsset in DB
    asset = test_db.query(MediaAsset).filter(MediaAsset.id == res["asset_id"]).first()
    assert asset is not None
    assert asset.media_type == "audio"


def test_google_music_adapter_score_generation(test_db, sample_project):
    """Verifies Google Lyria 3.5 generates real MP3 music score via Interactions API."""
    adapter = GoogleMusicAdapter(api_key="mock-key")
    mock_client = MagicMock()
    mock_interaction = MagicMock()
    mock_interaction.id = "interactions/lyria-score-11223"
    mock_interaction.status = "completed"
    mock_interaction.output_audio.data = base64.b64encode(b"FAKE_LYRIA_SCORE_MP3_BYTES").decode("utf-8")
    mock_client.interactions.create.return_value = mock_interaction
    adapter._client = mock_client

    res = adapter.generate_music_score(
        project_id=sample_project.id,
        db=test_db,
        mood="dark cyberpunk",
        genre="synthwave",
        bpm=120,
        duration_seconds=30
    )

    assert res["status"] == "completed"
    assert res["provider"] == "google_lyria"
    assert res["model"] == "lyria-3.5"
    assert res["mood"] == "dark cyberpunk"
    assert res["filename"].endswith(".mp3")
    assert res["interaction_id"] == "interactions/lyria-score-11223"
    assert Path(res["file_path"]).exists()

    # Verify AudioTrack
    track = test_db.query(AudioTrack).filter(AudioTrack.id == res["track_id"]).first()
    assert track is not None
    assert track.track_type == "music"
    assert track.ducking_enabled is True

    # Verify Interactions API arguments: no unsupported audio_config dictionary
    call_kwargs = mock_client.interactions.create.call_args[1]
    assert call_kwargs["response_format"] == {"type": "audio", "mime_type": "audio/mp3"}
    assert "generation_config" not in call_kwargs or "audio_config" not in call_kwargs.get("generation_config", {})
    prompt_str = call_kwargs["input"]
    assert "dark cyberpunk" in prompt_str
    assert "synthwave" in prompt_str
    assert "120 BPM" in prompt_str


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
        provider="google_nano_banana",
        model="gemini-3.1-flash-image"
    )

    assert job.status == "pending"
    assert job.provider == "google_nano_banana"

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
    assert retried_job.status == "completed"

    # 5. Recovery upon local server restart: orphaned jobs safely marked as failed with clear message
    orphaned_job = generation_job_manager.create_job(
        db=test_db,
        project_id=sample_project.id,
        generation_type="image_generation",
        input_parameters={"prompt": "orphaned render"},
        provider="google_nano_banana",
        model="gemini-3.1-flash-image"
    )
    orphaned_job.status = "processing"
    test_db.commit()

    recovered_count = generation_job_manager.recover_interrupted_jobs(test_db)
    assert recovered_count >= 1
    test_db.refresh(orphaned_job)
    assert orphaned_job.status == "failed"
    assert "interrupted by local backend restart" in orphaned_job.error_details


def test_honest_cancellation_semantics_for_remote_processing(test_db, sample_project):
    """Verifies that in-flight jobs without provider cancellation support reject fake cancellation."""
    job = generation_job_manager.create_job(
        db=test_db,
        project_id=sample_project.id,
        generation_type="video_generation",
        input_parameters={"prompt": "Action sequence"},
        provider="google_custom_cloud",
        model="custom-engine-001",
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

    # Expensive operation (video generation) requires confirmation
    res2 = client.get(f"/api/cost/check-authorization/{sample_project.id}?operation=video_generation&estimated_cost=0.15")
    assert res2.status_code == 200
    data2 = res2.json()
    assert data2["authorized"] is True
    assert data2["requires_confirmation"] is True

    # Exceeding budget limit rejects authorization
    sample_project.budget_spent = 99.95
    test_db.commit()

    res3 = client.get(f"/api/cost/check-authorization/{sample_project.id}?operation=video_generation&estimated_cost=0.15")
    assert res3.status_code == 200
    data3 = res3.json()
    assert data3["authorized"] is False
    assert data3["can_proceed"] is False
    assert "Insufficient budget" in data3["reason"]


def test_google_drive_status_endpoint(client):
    """Verifies that Google Drive status reports safely without leaking credentials."""
    res = client.get("/api/system/google-drive/status")
    assert res.status_code == 200
    data = res.json()
    assert "is_configured" in data
    assert "enabled" in data
    assert "message" in data


def test_studio_system_ai_status_endpoint(client):
    """Verifies the unified system AI status endpoint exposes Google Cloud AI details and masks secrets."""
    res = client.get("/api/system/ai-status")
    assert res.status_code == 200
    data = res.json()
    assert data["primary_provider"] == "gemini"
    assert "gemini" in data
    assert "ollama" in data
    assert "offline_template" in data
    assert "google_models" in data
    assert data["google_models"]["text"] == "gemini-3.8-flash"
    assert data["google_models"]["image"] == "gemini-3.1-flash-image"
    assert data["google_models"]["video"] == "gemini-omni-1.1-flash"
    assert data["google_models"]["tts"] == "gemini-3.8-flash-tts"
    assert data["google_models"]["music"] == "lyria-3.5"
    # Verify no raw API key leak
    assert data["gemini"].get("api_key") is None
    assert "masked_key" in data["gemini"]


def test_audio_endpoints_tts_and_music(client, sample_project):
    """Verifies /api/audio/voices, /api/audio/tts/generate, and /api/audio/music/generate."""
    # 1. Voices list
    res_voices = client.get("/api/audio/voices")
    assert res_voices.status_code == 200
    v_data = res_voices.json()
    assert "voices" in v_data
    assert "Puck" in v_data["voices"]

    # 2. TTS generate (offline fallback when key not configured)
    res_tts = client.post(
        f"/api/audio/tts/generate/{sample_project.id}",
        json={"text": "System operational.", "voice_name": "Puck", "character_name": "AI Voice"}
    )
    assert res_tts.status_code == 200
    tts_data = res_tts.json()
    assert tts_data["project_id"] == sample_project.id
    assert tts_data["track_type"] == "dialogue"

    # 3. Music generate (offline fallback when key not configured)
    res_music = client.post(
        f"/api/audio/music/generate/{sample_project.id}",
        json={"mood": "cyberpunk", "genre": "synthwave", "duration_seconds": 20}
    )
    assert res_music.status_code == 200
    music_data = res_music.json()
    assert music_data["project_id"] == sample_project.id
    assert music_data["track_type"] == "music"


def test_google_video_adapter_multimodal_and_task_filtering(test_db, sample_project, tmp_path):
    """Verifies Omni Flash multimodal attachments, removal of upscale task, and response format."""
    scene = Scene(
        id=str(uuid.uuid4()),
        project_id=sample_project.id,
        scene_number=1,
        slugline="INT. LAB - DAY",
        script_content="Scanning brain scan."
    )
    test_db.add(scene)
    test_db.commit()

    shot = Shot(
        id=str(uuid.uuid4()),
        project_id=sample_project.id,
        scene_id=scene.id,
        shot_code="SC01_SH05",
        video_prompt="Close up of cybernetic eye scanning"
    )
    test_db.add(shot)
    test_db.commit()

    # Create dummy reference image
    ref_img = tmp_path / "reference.png"
    ref_img.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDRFAKEPNG")

    adapter = GoogleVideoAdapter(api_key="mock-key")
    mock_client = MagicMock()
    mock_interaction = MagicMock()
    mock_interaction.id = "interactions/omni-multi-1122"
    mock_interaction.status = "processing"
    mock_client.interactions.create.return_value = mock_interaction
    adapter._client = mock_client

    # Submit with image attachment and upscale task (which must map to text_to_video or image_to_video)
    res = adapter.submit_video_generation(
        prompt="Scan neural pathway",
        project_id=sample_project.id,
        shot_id=shot.id,
        db=test_db,
        task="upscale", # should be sanitized away from upscale
        input_image_path=str(ref_img)
    )

    assert res["status"] == "processing"
    # upscale should be filtered out
    assert res["task"] != "upscale"
    assert res["task"] == "image_to_video"

    # Check call arguments to interactions.create
    call_kwargs = mock_client.interactions.create.call_args[1]
    assert isinstance(call_kwargs["input"], list)
    assert call_kwargs["input"][0]["type"] == "text"
    assert call_kwargs["input"][1]["type"] == "image"
    assert call_kwargs["input"][1]["mime_type"] == "image/png"
    assert call_kwargs["response_format"] == {"type": "video", "aspect_ratio": "16:9", "resolution": "720p"}


def test_google_music_adapter_clip_vs_full_model_and_fallback_honesty(test_db, sample_project):
    """Verifies duration handling for lyria-3-clip-preview and honest 2.0s duration in offline fallback."""
    adapter = GoogleMusicAdapter(api_key="mock-key")
    mock_client = MagicMock()
    mock_interaction = MagicMock()
    mock_interaction.id = "interactions/lyria-clip-30s"
    mock_interaction.output_audio.data = base64.b64encode(b"FAKE_LYRIA_MP3_CLIP").decode("utf-8")
    mock_client.interactions.create.return_value = mock_interaction
    adapter._client = mock_client

    # Model lyria-3-clip-preview enforces 30 seconds
    res = adapter.generate_music_score(
        project_id=sample_project.id,
        db=test_db,
        mood="tense",
        genre="synth",
        duration_seconds=60, # Requesting 60s
        model="lyria-3-clip-preview"
    )
    assert res["duration_seconds"] == 30.0
    assert res["filename"].endswith(".mp3")

    # Offline fallback honesty check
    fallback_adapter = GoogleMusicAdapter(api_key="")
    fb_res = fallback_adapter.generate_music_score(
        project_id=sample_project.id,
        db=test_db,
        mood="ambient",
        genre="drone",
        duration_seconds=60
    )
    assert fb_res["is_fallback"] is True
    # Never fake 60 seconds for a 2-second silent reference header
    assert fb_res["duration_seconds"] == 2.0


def test_cost_controller_reservation_and_compensation(test_db, sample_project):
    """Verifies pre-flight budget reservation and compensation refund via CostController."""
    from backend.app.services.cost_controller import CostController

    initial_spent = sample_project.budget_spent

    # 1. Pre-flight reservation
    res_rec = CostController.reserve_budget(
        db=test_db,
        project_id=sample_project.id,
        estimated_cost=0.15,
        operation="video_generation",
        details="Pre-flight Omni Flash reservation"
    )
    assert res_rec is not None
    assert res_rec.action_type == "budget_reservation"
    assert res_rec.actual_cost_known is False

    test_db.refresh(sample_project)
    assert round(sample_project.budget_spent - initial_spent, 2) == 0.15

    # 2. Release reservation upon operation failure
    refund_rec = CostController.release_reservation(
        db=test_db,
        project_id=sample_project.id,
        reservation_record=res_rec,
        reason="remote_provider_timeout"
    )
    assert refund_rec is not None
    assert refund_rec.action_type == "refund"
    assert refund_rec.cost_usd == -0.15

    test_db.refresh(sample_project)
    assert round(sample_project.budget_spent, 2) == round(initial_spent, 2)


def test_async_jobs_tts_and_music_endpoints(client, sample_project):
    """Verifies /api/jobs/generation/tts and /api/jobs/generation/music asynchronous endpoints."""
    # 1. Submit TTS job
    res_tts = client.post(
        "/api/jobs/generation/tts",
        json={
            "project_id": sample_project.id,
            "text": "Initiating override sequence.",
            "voice_name": "Puck",
            "character_name": "OPERATOR"
        }
    )
    assert res_tts.status_code == 200
    tts_job = res_tts.json()
    assert tts_job["generation_type"] == "tts_generation"
    assert tts_job["status"] in ("pending", "processing", "completed")

    # 2. Submit Music job
    res_music = client.post(
        "/api/jobs/generation/music",
        json={
            "project_id": sample_project.id,
            "mood": "dramatic",
            "genre": "orchestral",
            "duration_seconds": 30
        }
    )
    assert res_music.status_code == 200
    music_job = res_music.json()
    assert music_job["generation_type"] == "music_generation"
    assert music_job["status"] in ("pending", "processing", "completed")


def test_dynamic_ai_config_runtime_updates(client):
    """Verifies runtime AI config update affects adapters immediately and masks secrets."""
    res = client.post(
        "/api/system/ai-config",
        json={
            "gemini_api_key": "AIzaSyNEWKEYFORTESTING1234567890",
            "google_video_model": "gemini-omni-1.1-flash-custom",
            "google_tts_model": "gemini-3.8-flash-lite-tts",
            "google_music_model": "lyria-3-clip-preview",
            "google_image_model": "gemini-3-pro-image"
        }
    )
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "success"
    assert data["masked_key"] == "AIza...7890"

    # Verify adapters reflect runtime updates without server restart
    assert google_video_adapter.model == "gemini-omni-1.1-flash-custom"
    assert google_tts_adapter.model == "gemini-3.8-flash-lite-tts"
    assert google_music_adapter.model == "lyria-3-clip-preview"
    assert google_image_adapter.model == "gemini-3-pro-image"
    assert google_video_adapter.api_key == "AIzaSyNEWKEYFORTESTING1234567890"


def test_google_tts_adapter_multispeaker_contract(test_db, sample_project):
    """Verifies documented multi-speaker dialogue configuration with speaker mappings."""
    adapter = GoogleTTSAdapter(api_key="mock-key")
    mock_client = MagicMock()
    mock_interaction = MagicMock()
    mock_interaction.id = "interactions/tts-multi-555"
    wav_bytes = b"RIFF$\x00\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00D\xac\x00\x00\x88X\x01\x00\x02\x00\x10\x00data\x00\x00\x00\x00"
    mock_interaction.output_audio.data = base64.b64encode(wav_bytes).decode("utf-8")
    mock_client.interactions.create.return_value = mock_interaction
    adapter._client = mock_client

    turns = [
        {"speaker": "REN", "voice": "Puck", "text": "We need to evacuate now.", "emotion": "urgent"},
        {"speaker": "ELENA", "voice": "Kore", "text": "The blast doors are already locked.", "emotion": "tense"}
    ]

    res = adapter.generate_multi_speaker_dialogue(
        dialogue_turns=turns,
        project_id=sample_project.id,
        db=test_db,
        scene_id="scene-multi-01"
    )

    assert res["status"] == "completed"
    assert res["is_multi_speaker"] is True
    assert set(res["speakers"]) == {"REN", "ELENA"}
    assert Path(res["file_path"]).exists()

    # Inspect call arguments
    call_kwargs = mock_client.interactions.create.call_args[1]
    assert call_kwargs["response_format"] == {"type": "audio"}
    speech_cfg = call_kwargs["generation_config"]["speech_config"]
    assert len(speech_cfg) == 2
    assert {"voice": "Puck", "speaker": "REN"} in speech_cfg
    assert {"voice": "Kore", "speaker": "ELENA"} in speech_cfg

    # Multi-turn user_input verification
    input_items = call_kwargs["input"]
    assert len(input_items) == 2
    assert input_items[0]["type"] == "user_input"
    assert input_items[0]["content"][0]["annotations"][0]["speaker"] == "REN"
    assert "urgent" in input_items[0]["content"][0]["annotations"][0]["style"]
    assert input_items[1]["type"] == "user_input"
    assert input_items[1]["content"][0]["annotations"][0]["speaker"] == "ELENA"
    assert "tense" in input_items[1]["content"][0]["annotations"][0]["style"]


def test_google_image_adapter_model_size_limitations(test_db, sample_project):
    """Verifies model-specific image-size limitations for Flash, Flash Lite, and Pro."""
    import pytest
    adapter = GoogleImageAdapter(api_key="mock-key")
    mock_client = MagicMock()
    mock_interaction = MagicMock()
    mock_interaction.id = "interactions/img-limit-test"
    # Small valid JPEG byte buffer
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (320, 240), color="red").save(buf, format="JPEG")
    mock_interaction.output_image.data = base64.b64encode(buf.getvalue()).decode("utf-8")
    mock_client.interactions.create.return_value = mock_interaction
    adapter._client = mock_client

    # 1. Flash Lite Image accepts 512 and 1K, but rejects 2K and 4K
    res_lite_1k = adapter.generate_image(
        prompt="Cyberpunk drone",
        project_id=sample_project.id,
        db=test_db,
        model="gemini-3.1-flash-lite-image",
        image_size="1K"
    )
    assert res_lite_1k["status"] == "completed"

    with pytest.raises(ValueError, match="not supported"):
        adapter.generate_image(
            prompt="Cyberpunk drone",
            project_id=sample_project.id,
            db=test_db,
            model="gemini-3.1-flash-lite-image",
            image_size="2K"
        )

    with pytest.raises(ValueError, match="not supported"):
        adapter.generate_image(
            prompt="Cyberpunk drone",
            project_id=sample_project.id,
            db=test_db,
            model="gemini-3.1-flash-lite-image",
            image_size="4K"
        )

    # 2. Pro Image accepts 1K, 2K, 4K, but rejects 512
    res_pro_2k = adapter.generate_image(
        prompt="Masterpiece painting",
        project_id=sample_project.id,
        db=test_db,
        model="gemini-3-pro-image",
        image_size="2K"
    )
    assert res_pro_2k["status"] == "completed"

    with pytest.raises(ValueError, match="not supported"):
        adapter.generate_image(
            prompt="Masterpiece painting",
            project_id=sample_project.id,
            db=test_db,
            model="gemini-3-pro-image",
            image_size="512"
        )


def test_google_video_adapter_files_api_upload_workflow(test_db, sample_project, tmp_path):
    """Verifies official Google Files API upload workflow for video input with active state polling."""
    vid_file = tmp_path / "input_clip.mp4"
    vid_file.write_bytes(b"FAKE_INPUT_MP4_VIDEO_HEADER_DATA")

    adapter = GoogleVideoAdapter(api_key="mock-key")
    mock_client = MagicMock()
    mock_file = MagicMock()
    mock_file.name = "files/test-upload-name-99"
    mock_file.uri = "https://generativelanguage.googleapis.com/v1beta/files/test-upload-name-99"
    # State transition: PROCESSING -> ACTIVE
    mock_file.state.name = "ACTIVE"
    mock_client.files.upload.return_value = mock_file
    mock_client.files.get.return_value = mock_file

    mock_interaction = MagicMock()
    mock_interaction.id = "interactions/omni-files-7788"
    mock_client.interactions.create.return_value = mock_interaction
    adapter._client = mock_client

    scene = Scene(
        id=str(uuid.uuid4()),
        project_id=sample_project.id,
        scene_number=2,
        slugline="INT. LAB - EXTENSION",
        script_content="Continued lab sequence."
    )
    test_db.add(scene)
    test_db.commit()

    shot = Shot(
        id=str(uuid.uuid4()),
        project_id=sample_project.id,
        scene_id=scene.id,
        shot_code="SC02_SH01",
        video_prompt="Extend this scene"
    )
    test_db.add(shot)
    test_db.commit()

    sub = adapter.submit_video_generation(
        prompt="Extend camera movement",
        project_id=sample_project.id,
        shot_id=shot.id,
        db=test_db,
        task="extend",
        aspect_ratio="9:16",
        resolution="1080p",
        input_video_path=str(vid_file)
    )

    assert sub["status"] == "processing"
    assert sub["aspect_ratio"] == "9:16"
    assert mock_client.files.upload.called

    # Check that interactions.create received the file URI (not base64 payload)
    call_kwargs = mock_client.interactions.create.call_args[1]
    video_input = [item for item in call_kwargs["input"] if item.get("type") == "video"][0]
    assert video_input["uri"] == mock_file.uri
    assert video_input["mime_type"] == "video/mp4"
    assert call_kwargs["response_format"] == {"type": "video", "aspect_ratio": "9:16", "resolution": "1080p"}


def test_cost_accounting_edge_case_provider_succeeded_local_processing_failed(test_db, sample_project):
    """
    Verifies that when Google successfully generates a video but local FFmpeg processing fails:
    - Incurred provider expense is NOT erased or refunded.
    - Outstanding reservation is finalized with local_processing_status='failed'.
    - Project.budget_spent remains charged.
    """
    from backend.app.services.cost_controller import CostController
    from backend.app.services.google_video_adapter import LocalMediaProcessingError

    initial_spent = sample_project.budget_spent or 0.0

    # 1. Pre-flight reservation
    reservation = CostController.reserve_budget(
        db=test_db,
        project_id=sample_project.id,
        estimated_cost=0.15,
        operation="video_generation",
        provider="google_omni",
        model="gemini-omni-1.1-flash",
        details="Pre-flight video reservation for edge case test"
    )
    test_db.commit()
    test_db.refresh(sample_project)
    assert round(sample_project.budget_spent - initial_spent, 2) == 0.15

    # 2. Simulate local media processing failure after provider completion
    mux_error = LocalMediaProcessingError("FFmpeg: Audio stream corrupted during muxing")
    finalized = CostController.finalize_reservation(
        db=test_db,
        project_id=sample_project.id,
        reservation_record=reservation,
        actual_cost=None,
        actual_cost_known=False,
        local_processing_status="failed",
        error_details=str(mux_error)
    )
    test_db.commit()

    # 3. Verify that the provider expense remains charged against the project budget
    test_db.refresh(sample_project)
    assert round(sample_project.budget_spent - initial_spent, 2) == 0.15
    assert finalized.action_type == "video_generation"
    assert "local media processing failed" in finalized.details

    # 4. Verify no refund was recorded
    refund = test_db.query(CostRecord).filter(
        CostRecord.operation_identity == f"refund_{reservation.id}"
    ).first()
    assert refund is None


def test_interrupted_reservation_recovery_after_backend_shutdown(test_db, sample_project):
    """
    Verifies that an orphaned budget reservation from a local crash before cloud dispatch
    is safely released during backend startup recovery.
    """
    from backend.app.services.cost_controller import CostController
    from backend.app.services.generation_job_manager import generation_job_manager

    initial_spent = sample_project.budget_spent or 0.0
    dead_job_id = f"dead-job-{uuid.uuid4().hex[:8]}"

    # Simulate an orphaned reservation created by a job that died before remote dispatch
    reservation = CostController.reserve_budget(
        db=test_db,
        project_id=sample_project.id,
        estimated_cost=0.15,
        operation="video_generation",
        provider="google_omni",
        job_id=dead_job_id,
        details="Pre-flight video reservation before crash"
    )
    test_db.commit()
    test_db.refresh(sample_project)
    assert round(sample_project.budget_spent - initial_spent, 2) == 0.15

    # Run recovery
    released = CostController.recover_orphaned_job_reservation(
        db=test_db,
        job_id=dead_job_id,
        project_id=sample_project.id
    )
    test_db.commit()

    assert released is not None
    assert released.action_type == "refund"
    assert released.cost_usd == -0.15

    # Project budget should be fully restored
    test_db.refresh(sample_project)
    assert round(sample_project.budget_spent - initial_spent, 2) == 0.0
