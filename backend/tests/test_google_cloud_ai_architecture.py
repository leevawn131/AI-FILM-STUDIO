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
    """Verifies that Gemini 4 Hydrogen is primary default with Carbon deep-reasoning and 3.8 Flash fallback."""
    assert settings.ai_provider == "gemini"
    assert settings.gemini_model == "gemini-4-hydrogen"
    assert settings.gemini_reasoning_model == "gemini-4-carbon"
    assert settings.gemini_fallback_model == "gemini-3.8-flash"
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


def _make_valid_mp3_bytes(tmp_path: Path, duration: float = 1.0) -> bytes:
    import subprocess
    out = tmp_path / f"sample_{uuid.uuid4().hex[:6]}.mp3"
    subprocess.run(
        [
            "ffmpeg", "-y", "-f", "lavfi",
            "-i", "anullsrc=r=44100:cl=stereo",
            "-t", str(duration),
            "-c:a", "libmp3lame", "-b:a", "96k",
            str(out),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=True,
    )
    return out.read_bytes()


def _make_mock_image_response(img_bytes: bytes, resp_id: str = "interactions/nano-img-1", status: str = "completed"):
    inter = MagicMock()
    inter.id = resp_id
    inter.status = status
    inter.output_image = MagicMock()
    inter.output_image.data = base64.b64encode(img_bytes).decode("ascii")
    inter.output_image.uri = None
    inter.outputs = None
    inter.steps = None
    return inter


def test_google_image_adapter_nano_banana_with_mock_client(test_db, sample_project):
    """Verifies Nano Banana 2.1 uses client.interactions.create(), validates with Pillow, and records true dimensions."""
    import io
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (640, 480), color="blue").save(buf, format="JPEG")
    valid_jpeg = buf.getvalue()

    adapter = GoogleImageAdapter(api_key="mock-api-key")
    mock_client = MagicMock()
    mock_client.interactions.create.return_value = _make_mock_image_response(valid_jpeg, "interactions/nano-456")
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
    assert res["model"] == "gemini-nano-banana-2.1"
    assert res["width"] == 640
    assert res["height"] == 480
    assert res["filename"].endswith(".jpg")
    assert res["is_fallback"] is False
    assert Path(res["file_path"]).exists()

    # Verify primary Nano Banana 2.1 interface is Interactions API (never legacy generate_content/generate_images)
    assert mock_client.interactions.create.call_count == 1
    assert mock_client.models.generate_content.call_count == 0
    assert mock_client.models.generate_images.call_count == 0
    ic_kwargs = mock_client.interactions.create.call_args[1]
    assert ic_kwargs["model"] == "gemini-nano-banana-2.1"
    assert ic_kwargs["response_format"] == {
        "type": "image",
        "aspect_ratio": "16:9",
        "image_size": "1K",
    }


def test_google_video_adapter_interactions_api_lifecycle(test_db, sample_project, tmp_path):
    """Verifies Gemini Omni Flash 1.1 video generation via Interactions API and FFprobe inspection."""
    from backend.app.services.ffmpeg_service import ffmpeg_service

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
    assert sub["model"] == "gemini-omni-1.2-flash"
    assert sub["provider_operation_id"] == "interactions/omni-video-98765"
    assert sub["supports_cancellation"] is True
    assert sub["target_duration_seconds"] == 5
    assert "duration_seconds" not in sub

    # 2. Poll completed status with genuine FFmpeg-generated MP4 bytes
    synth_clip = tmp_path / "omni_out.mp4"
    ffmpeg_service.generate_synthetic_test_clip(synth_clip, duration=1.0, width=320, height=240)
    real_mp4_bytes = synth_clip.read_bytes()

    mock_completed = MagicMock()
    mock_completed.status = "completed"
    mock_completed.output_video.data = base64.b64encode(real_mp4_bytes).decode("utf-8")
    mock_completed.output_video.mime_type = "video/mp4"
    mock_completed.output_video.uri = None
    mock_completed.output_audio = None
    mock_client.interactions.get.return_value = mock_completed

    poll_res = adapter.poll_operation_status("interactions/omni-video-98765")
    assert poll_res["status"] == "completed"
    assert poll_res["video_bytes"] == real_mp4_bytes

    # 3. Import completed video locally and update Shot take selection with real FFprobe metadata
    imported_asset = adapter.import_completed_video(
        project_id=sample_project.id,
        shot_id=shot.id,
        video_bytes=poll_res["video_bytes"],
        db=test_db
    )

    assert imported_asset.version == 1
    assert imported_asset.shot_id == shot.id
    assert imported_asset.width == 320
    assert imported_asset.height == 240
    assert abs(imported_asset.duration_seconds - 1.0) <= 0.2
    assert imported_asset.codec == "h264"
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


def test_google_music_adapter_score_generation(test_db, sample_project, tmp_path):
    """Verifies Google Lyria 3.5 generates real MP3 music score via Interactions API and validates via FFprobe."""
    real_mp3_bytes = _make_valid_mp3_bytes(tmp_path, duration=1.0)

    adapter = GoogleMusicAdapter(api_key="mock-key")
    mock_client = MagicMock()
    mock_interaction = MagicMock()
    mock_interaction.id = "interactions/lyria-score-11223"
    mock_interaction.status = "completed"
    mock_interaction.output_audio.data = base64.b64encode(real_mp3_bytes).decode("utf-8")
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
    assert res["audio_codec"] == "mp3"
    assert abs(res["duration_seconds"] - 1.0) <= 0.2
    assert res["interaction_id"] == "interactions/lyria-score-11223"
    assert Path(res["file_path"]).exists()

    # Verify AudioTrack
    track = test_db.query(AudioTrack).filter(AudioTrack.id == res["track_id"]).first()
    assert track is not None
    assert track.track_type == "music"
    assert track.ducking_enabled is True

    # Verify Interactions API arguments: single submission, no unsupported audio_config dictionary
    assert mock_client.interactions.create.call_count == 1
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
    """Verifies the unified system AI status endpoint exposes verified model catalog, honesty flags, and masks secrets."""
    res = client.get("/api/system/ai-status")
    assert res.status_code == 200
    data = res.json()
    assert data["primary_provider"] == "gemini"
    assert "gemini" in data
    assert "ollama" in data
    assert "offline_template" in data
    assert "google_models" in data
    assert data["google_models"]["text"] == "gemini-4-hydrogen"
    assert data["google_models"]["text_reasoning"] == "gemini-4-carbon"
    assert data["google_models"]["text_fallback"] == "gemini-3.8-flash"
    assert data["google_models"]["image"] == "gemini-nano-banana-2.1"
    assert data["google_models"]["video"] == "gemini-omni-1.2-flash"
    assert data["google_models"]["tts"] == "gemini-3.8-flash-tts"
    assert data["google_models"]["music"] == "lyria-3.5"
    assert "model_catalog" in data
    assert data["model_catalog"]["gemini-3.1-flash-image"]["status"] == "deprecated"
    assert data["model_catalog"]["gemini-nano-banana-2.1"]["status"] == "stable"
    assert data["model_catalog"]["gemini-omni-1.2-flash"]["status"] == "stable"
    assert data["live_api_tested"] is False
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
    assert call_kwargs["response_format"] == {
        "type": "video",
        "aspect_ratio": "16:9",
        "resolution": "720p",
        "duration": "5s",
    }


def test_google_music_adapter_clip_vs_full_model_and_fallback_honesty(test_db, sample_project, tmp_path):
    """Verifies duration handling for lyria-3-clip-preview and honest 2.0s duration in offline fallback."""
    real_mp3_bytes = _make_valid_mp3_bytes(tmp_path, duration=1.5)

    adapter = GoogleMusicAdapter(api_key="mock-key")
    mock_client = MagicMock()
    mock_interaction = MagicMock()
    mock_interaction.id = "interactions/lyria-clip-30s"
    mock_interaction.status = "completed"
    mock_interaction.output_audio.data = base64.b64encode(real_mp3_bytes).decode("utf-8")
    mock_client.interactions.create.return_value = mock_interaction
    adapter._client = mock_client

    # Model lyria-3-clip-preview caps target duration to 30 seconds in prompt, while FFprobe inspects true output duration
    res = adapter.generate_music_score(
        project_id=sample_project.id,
        db=test_db,
        mood="tense",
        genre="synth",
        duration_seconds=60,  # Requesting 60s
        model="lyria-3-clip-preview"
    )
    assert res["target_duration_seconds"] == 30
    assert abs(res["duration_seconds"] - 1.5) <= 0.25
    assert res["audio_codec"] == "mp3"
    assert res["filename"].endswith(".mp3")
    assert "30-second musical clip preview" in mock_client.interactions.create.call_args[1]["input"]

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
    """Verifies runtime AI config update switches verified models cleanly, rejects deprecated image models, and masks secrets."""
    from backend.app.config import settings
    saved_settings = {
        "gemini_api_key": settings.gemini_api_key,
        "gemini_model": settings.gemini_model,
        "google_video_model": settings.google_video_model,
        "google_tts_model": settings.google_tts_model,
        "google_music_model": settings.google_music_model,
        "google_image_model": settings.google_image_model,
    }
    saved_env_key = os.environ.get("GEMINI_API_KEY")
    try:
        bad_res = client.post(
            "/api/system/ai-config",
            json={"google_image_model": "gemini-3.1-flash-image"}
        )
        assert bad_res.status_code == 400

        res = client.post(
            "/api/system/ai-config",
            json={
                "gemini_api_key": "AIzaSyNEWKEYFORTESTING1234567890",
                "gemini_model": "gemini-4-carbon",
                "google_video_model": "gemini-omni-1.1-flash",
                "google_tts_model": "gemini-3.8-flash-lite-tts",
                "google_music_model": "lyria-3-clip-preview",
                "google_image_model": "gemini-3-pro-image"
            }
        )
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "success"
        assert data["masked_key"] == "AIza...7890"
        assert data["gemini_model"] == "gemini-4-carbon"

        # Verify adapters reflect runtime updates without server restart
        assert google_video_adapter.model == "gemini-omni-1.1-flash"
        assert google_tts_adapter.model == "gemini-3.8-flash-lite-tts"
        assert google_music_adapter.model == "lyria-3-clip-preview"
        assert google_image_adapter.model == "gemini-3-pro-image"
        assert google_video_adapter.api_key == "AIzaSyNEWKEYFORTESTING1234567890"
    finally:
        for k, v in saved_settings.items():
            setattr(settings, k, v)
        if saved_env_key is None:
            os.environ.pop("GEMINI_API_KEY", None)
        else:
            os.environ["GEMINI_API_KEY"] = saved_env_key


def test_google_tts_adapter_multispeaker_contract(test_db, sample_project):
    """
    Verifies Google's documented multi-speaker configuration:
    - One user_input object with multiple ordered content items
    - speech_metadata annotations carrying speaker + style (no undocumented fields)
    - generation_config.speech_config == {"speakers": [{"speaker": ..., "voice": ...}, ...]}
    - Max 2 speakers enforced
    - Single unique speaker routes cleanly to documented single-speaker list structure
    """
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
        {"speaker": "ELENA", "voice": "Kore", "text": "The blast doors are already locked.", "emotion": "tense"},
        {"speaker": "REN", "voice": "Puck", "text": "Try the service conduit.", "emotion": "determined"}
    ]

    res = adapter.generate_multi_speaker_dialogue(
        dialogue_turns=turns,
        project_id=sample_project.id,
        db=test_db,
        scene_id="scene-multi-01"
    )

    assert res["status"] == "completed"
    assert res["is_multi_speaker"] is True
    assert res["speakers"] == ["REN", "ELENA"]
    assert Path(res["file_path"]).exists()

    # Inspect exact Interactions API call arguments
    call_kwargs = mock_client.interactions.create.call_args[1]
    assert call_kwargs["response_format"] == {"type": "audio"}
    gen_cfg = call_kwargs["generation_config"]
    assert "mode" not in gen_cfg
    assert gen_cfg["speech_config"] == {
        "speakers": [
            {"speaker": "REN", "voice": "Puck"},
            {"speaker": "ELENA", "voice": "Kore"}
        ]
    }

    # Verify ONE user_input object with ordered text content turns
    input_items = call_kwargs["input"]
    assert isinstance(input_items, list)
    assert len(input_items) == 1
    assert input_items[0]["type"] == "user_input"
    content_turns = input_items[0]["content"]
    assert len(content_turns) == 3

    expected_speakers = ["REN", "ELENA", "REN"]
    expected_emotions = ["urgent", "tense", "determined"]
    for idx, c in enumerate(content_turns):
        assert c["type"] == "text"
        assert c["text"] == turns[idx]["text"]
        assert len(c["annotations"]) == 1
        ann = c["annotations"][0]
        assert set(ann.keys()) == {"type", "speaker", "style"}
        assert ann["type"] == "speech_metadata"
        assert ann["speaker"] == expected_speakers[idx]
        assert expected_emotions[idx] in ann["style"]

    # Enforce max 2 speakers without calling Google API or charging budget
    mock_client.interactions.create.reset_mock()
    spent_before_invalid = sample_project.budget_spent
    three_speaker_turns = [
        {"speaker": "REN", "voice": "Puck", "text": "Go."},
        {"speaker": "ELENA", "voice": "Kore", "text": "Locked."},
        {"speaker": "MARCUS", "voice": "Fenrir", "text": "Follow me."}
    ]
    with pytest.raises(ValueError, match="at most 2 speakers"):
        adapter.generate_multi_speaker_dialogue(
            dialogue_turns=three_speaker_turns,
            project_id=sample_project.id,
            db=test_db
        )
    assert mock_client.interactions.create.call_count == 0
    test_db.refresh(sample_project)
    assert sample_project.budget_spent == spent_before_invalid

    # Single speaker across multiple turns routes to single-speaker speech_config list structure
    single_turns = [
        {"speaker": "REN", "voice": "Charon", "text": "Log entry one."},
        {"speaker": "REN", "voice": "Charon", "text": "End transmission."}
    ]
    res_single = adapter.generate_multi_speaker_dialogue(
        dialogue_turns=single_turns,
        project_id=sample_project.id,
        db=test_db
    )
    assert res_single["is_multi_speaker"] is False
    single_kwargs = mock_client.interactions.create.call_args[1]
    assert single_kwargs["generation_config"] == {"speech_config": [{"voice": "Charon"}]}
    assert len(single_kwargs["input"]) == 1
    assert len(single_kwargs["input"][0]["content"]) == 2


def test_google_image_adapter_model_size_limitations(test_db, sample_project):
    """Verifies model-specific image-size limitations and rejection of deprecated/unknown models via Interactions API."""
    import io
    from PIL import Image

    adapter = GoogleImageAdapter(api_key="mock-key")
    mock_client = MagicMock()
    buf = io.BytesIO()
    Image.new("RGB", (320, 240), color="red").save(buf, format="JPEG")
    jpeg_bytes = buf.getvalue()
    mock_client.interactions.create.side_effect = lambda **kwargs: _make_mock_image_response(jpeg_bytes)
    adapter._client = mock_client

    # 1. Nano Banana 2.1 accepts 1K, 2K, 4K and rejects 512
    res_nb21_2k = adapter.generate_image(
        prompt="Wide cityscape",
        project_id=sample_project.id,
        db=test_db,
        model="gemini-nano-banana-2.1",
        image_size="2K",
        aspect_ratio="4:1",
    )
    assert res_nb21_2k["status"] == "completed"
    with pytest.raises(ValueError, match="not supported"):
        adapter.generate_image(
            prompt="Wide cityscape",
            project_id=sample_project.id,
            db=test_db,
            model="gemini-nano-banana-2.1",
            image_size="512",
        )

    # 2. Flash Lite Image accepts 512 and 1K, but rejects 2K and 4K
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

    # 3. Pro Image accepts 1K, 2K, 4K, but rejects 512
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

    # 4. Deprecated and unknown models are explicitly rejected (never silently falling back)
    with pytest.raises(ValueError, match="deprecated"):
        adapter.generate_image(
            prompt="Deprecated test",
            project_id=sample_project.id,
            db=test_db,
            model="gemini-3.1-flash-image",
            image_size="1K",
        )
    with pytest.raises(ValueError, match="Unknown or unsupported"):
        adapter.generate_image(
            prompt="Unknown model test",
            project_id=sample_project.id,
            db=test_db,
            model="gemini-nonexistent-image",
            image_size="1K",
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
    assert call_kwargs["response_format"] == {
        "type": "video",
        "aspect_ratio": "9:16",
        "resolution": "1080p",
        "duration": "5s",
        "delivery": "uri",
    }


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


def test_google_image_adapter_invalid_bytes_and_api_rejection_accounting(test_db, sample_project):
    """
    Verifies:
    - Corrupted/non-image bytes returned by provider fail clearly without fabricated dimensions,
      while preserving provider charge with local_processing_status='failed'.
    - Confirmed API 400 rejection releases the pre-flight budget reservation cleanly.
    """
    from backend.app.services.google_image_adapter import GoogleImageAdapterError

    adapter = GoogleImageAdapter(api_key="mock-key")
    mock_client = MagicMock()
    adapter._client = mock_client

    # 1. Invalid bytes returned -> fail clearly without faking dimensions, keep charge (~$0.0336)
    mock_client.interactions.create.return_value = _make_mock_image_response(b"NOT_A_VALID_IMAGE_PAYLOAD")
    spent_before = sample_project.budget_spent or 0.0

    with pytest.raises(GoogleImageAdapterError, match="invalid image bytes"):
        adapter.generate_image(
            prompt="Broken image",
            project_id=sample_project.id,
            db=test_db,
            model="gemini-nano-banana-2.1",
            image_size="1K"
        )
    test_db.refresh(sample_project)
    assert round((sample_project.budget_spent or 0.0) - spent_before, 4) == 0.0336
    assert test_db.query(MediaAsset).filter(MediaAsset.project_id == sample_project.id, MediaAsset.media_type == "image").count() == 0

    # 2. Confirmed API 400 rejection -> release reservation and restore budget
    mock_client.interactions.create.side_effect = RuntimeError("400 INVALID_ARGUMENT: bad prompt")
    spent_before_reject = sample_project.budget_spent
    with pytest.raises(GoogleImageAdapterError, match="Nano Banana generation failed"):
        adapter.generate_image(
            prompt="Rejected prompt",
            project_id=sample_project.id,
            db=test_db,
            image_size="1K"
        )
    test_db.refresh(sample_project)
    assert round(sample_project.budget_spent - spent_before_reject, 4) == 0.0


def test_google_video_adapter_files_api_timeout_aborts_before_submit(test_db, sample_project, tmp_path):
    """Verifies submit_video_generation fails cleanly without calling interactions.create if Files API stays PROCESSING."""
    from backend.app.services.google_video_adapter import GoogleVideoAdapterError

    vid_file = tmp_path / "slow_upload.mp4"
    vid_file.write_bytes(b"FAKE_VIDEO_DATA")

    scene = Scene(
        id=str(uuid.uuid4()),
        project_id=sample_project.id,
        scene_number=1,
        slugline="INT. HANGAR",
        script_content="Shuttle prepares."
    )
    test_db.add(scene)
    test_db.flush()
    shot = Shot(
        id=str(uuid.uuid4()),
        project_id=sample_project.id,
        scene_id=scene.id,
        shot_code="SC_STUCK_01",
        video_prompt="Extend clip"
    )
    test_db.add(shot)
    test_db.commit()

    adapter = GoogleVideoAdapter(api_key="mock-key")
    mock_client = MagicMock()
    mock_file = MagicMock()
    mock_file.name = "files/stuck-processing-1"
    mock_file.uri = "https://generativelanguage.googleapis.com/v1beta/files/stuck-processing-1"
    mock_file.state.name = "PROCESSING"
    mock_client.files.upload.return_value = mock_file
    mock_client.files.get.return_value = mock_file
    adapter._client = mock_client

    with patch("backend.app.services.google_video_adapter.time.sleep", return_value=None):
        with pytest.raises(GoogleVideoAdapterError, match="PROCESSING"):
            adapter.submit_video_generation(
                prompt="Extend",
                project_id=sample_project.id,
                shot_id=shot.id,
                db=test_db,
                input_video_path=str(vid_file)
            )
    assert mock_client.interactions.create.call_count == 0


def test_google_video_adapter_files_download_and_ffprobe_failure_honesty(test_db, sample_project, tmp_path):
    """
    Verifies:
    - Authenticated video_uri/audio_uri downloads prefer client.files.download()
    - Corrupted video bytes fail FFprobe inspection clearly with LocalMediaProcessingError
      rather than inventing 1920x1080 / 24fps / 4.0s / h264 metadata.
    """
    from backend.app.services.ffmpeg_service import ffmpeg_service
    from backend.app.services.google_video_adapter import LocalMediaProcessingError

    scene = Scene(
        id=str(uuid.uuid4()),
        project_id=sample_project.id,
        scene_number=1,
        slugline="EXT. BRIDGE",
        script_content="Rain on steel."
    )
    test_db.add(scene)
    test_db.flush()
    shot = Shot(
        id=str(uuid.uuid4()),
        project_id=sample_project.id,
        scene_id=scene.id,
        shot_code="SC_URI_01",
        video_prompt="Bridge in storm"
    )
    test_db.add(shot)
    test_db.commit()

    synth_clip = tmp_path / "dl_clip.mp4"
    ffmpeg_service.generate_synthetic_test_clip(synth_clip, duration=1.0, width=320, height=240)
    real_mp4 = synth_clip.read_bytes()

    adapter = GoogleVideoAdapter(api_key="mock-key")
    mock_client = MagicMock()
    mock_client.files.download.return_value = real_mp4
    adapter._client = mock_client

    asset = adapter.import_completed_video(
        project_id=sample_project.id,
        shot_id=shot.id,
        video_uri="https://generativelanguage.googleapis.com/v1beta/files/video-dl-99",
        db=test_db
    )
    assert mock_client.files.download.call_count == 1
    assert asset.width == 320
    assert asset.height == 240
    assert asset.codec == "h264"

    # Now fail clearly on corrupt bytes (do NOT invent 1920x1080 / 24fps / 4s / h264)
    with pytest.raises(LocalMediaProcessingError, match="FFprobe"):
        adapter.import_completed_video(
            project_id=sample_project.id,
            shot_id=shot.id,
            video_bytes=b"NOT_A_VALID_MP4_CONTAINER",
            db=test_db
        )


def test_google_music_adapter_no_blind_retry_and_ffprobe_validation(test_db, sample_project):
    """
    Verifies Lyria:
    - Never issues a second interactions.create call on failure or ambiguous timeout
    - Distinguishes clear 400 rejection (releases reservation) from ambiguous TimeoutError (preserves reservation)
    - Rejects corrupted MP3 bytes cleanly via FFprobe instead of inventing duration/codec
    """
    from backend.app.services.google_music_adapter import GoogleMusicAdapterError

    adapter = GoogleMusicAdapter(api_key="mock-key")
    mock_client = MagicMock()
    adapter._client = mock_client

    # 1. Ambiguous timeout -> 1 call only, reservation preserved as provider_outcome_unknown
    spent_0 = sample_project.budget_spent or 0.0
    mock_client.interactions.create.side_effect = TimeoutError("Read timed out waiting for Lyria")
    with pytest.raises(GoogleMusicAdapterError, match="ambiguous"):
        adapter.generate_music_score(project_id=sample_project.id, db=test_db, mood="noir", genre="jazz")
    assert mock_client.interactions.create.call_count == 1
    test_db.refresh(sample_project)
    assert round((sample_project.budget_spent or 0.0) - spent_0, 2) == 0.08

    # 2. Clear 400 rejection -> 1 call only, reservation released
    mock_client.interactions.create.reset_mock()
    spent_1 = sample_project.budget_spent
    mock_client.interactions.create.side_effect = RuntimeError("400 INVALID_ARGUMENT: unsupported parameter")
    with pytest.raises(GoogleMusicAdapterError, match="rejected"):
        adapter.generate_music_score(project_id=sample_project.id, db=test_db, mood="epic", genre="orchestral")
    assert mock_client.interactions.create.call_count == 1
    test_db.refresh(sample_project)
    assert round(sample_project.budget_spent - spent_1, 2) == 0.0

    # 3. Corrupted MP3 output -> FFprobe fails clearly without fabricating duration, keeps charge
    mock_client.interactions.create.side_effect = None
    bad_inter = MagicMock()
    bad_inter.id = "interactions/lyria-bad-mp3"
    bad_inter.status = "completed"
    bad_inter.output_audio.data = base64.b64encode(b"GARBAGE_NOT_MP3_BYTES").decode("utf-8")
    mock_client.interactions.create.return_value = bad_inter

    spent_2 = sample_project.budget_spent
    with pytest.raises(GoogleMusicAdapterError, match="FFprobe"):
        adapter.generate_music_score(project_id=sample_project.id, db=test_db, mood="tense", genre="ambient")
    test_db.refresh(sample_project)
    assert round(sample_project.budget_spent - spent_2, 2) == 0.08


def test_video_job_recovery_timeout_and_idempotent_reservations(test_db, sample_project, tmp_path):
    """
    Verifies GenerationJobManager & CostController lifecycle end-to-end:
    - Polling timeout preserves budget reservation (`provider_outcome_unknown`) and keeps `provider_operation_id`
    - Retrying / recovering a job with existing `provider_operation_id` resumes polling ONLY,
      never submitting a second paid provider generation and never duplicating budget reservations
    - Finalize & Release operations are strictly idempotent (never double-charge or double-refund)
    """
    from backend.app.services.cost_controller import CostController
    from backend.app.services.ffmpeg_service import ffmpeg_service

    scene = Scene(
        id=str(uuid.uuid4()),
        project_id=sample_project.id,
        scene_number=1,
        slugline="EXT. ORBIT",
        script_content="Station turns."
    )
    test_db.add(scene)
    test_db.flush()
    shot = Shot(
        id=str(uuid.uuid4()),
        project_id=sample_project.id,
        scene_id=scene.id,
        shot_code="SC_REC_01",
        video_prompt="Orbiting station"
    )
    test_db.add(shot)
    test_db.commit()

    # 1. CostController finalize/release strict idempotency checks
    base_spent = sample_project.budget_spent or 0.0
    r1 = CostController.reserve_budget(
        db=test_db, project_id=sample_project.id, estimated_cost=0.15,
        operation="video_generation", job_id="idem-job-1"
    )
    r1_dup = CostController.reserve_budget(
        db=test_db, project_id=sample_project.id, estimated_cost=0.15,
        operation="video_generation", job_id="idem-job-1"
    )
    test_db.commit()
    assert r1.id == r1_dup.id
    test_db.refresh(sample_project)
    assert round(sample_project.budget_spent - base_spent, 2) == 0.15

    CostController.finalize_reservation(
        db=test_db, project_id=sample_project.id, reservation_record=r1,
        actual_cost=0.25, actual_cost_known=True, local_processing_status="completed"
    )
    # Second finalize with actual cost must be a no-op (never double-add +0.10 diff)
    CostController.finalize_reservation(
        db=test_db, project_id=sample_project.id, reservation_record=r1,
        actual_cost=0.25, actual_cost_known=True, local_processing_status="completed"
    )
    # Release after finalization must refuse to refund
    assert CostController.release_reservation(test_db, sample_project.id, r1, reason="should_not_refund") is None
    test_db.commit()
    test_db.refresh(sample_project)
    assert round(sample_project.budget_spent - base_spent, 2) == 0.25

    # Double release on unfinalized reservation never double-refunds
    r2 = CostController.reserve_budget(
        db=test_db, project_id=sample_project.id, estimated_cost=0.10, operation="image_generation"
    )
    test_db.commit()
    ref1 = CostController.release_reservation(test_db, sample_project.id, r2, reason="err")
    ref2 = CostController.release_reservation(test_db, sample_project.id, r2, reason="err")
    assert ref1.id == ref2.id
    test_db.refresh(sample_project)
    assert round(sample_project.budget_spent - base_spent, 2) == 0.25

    # 2. Video job polling timeout -> preserves reservation & provider_operation_id -> recovery completes cleanly
    job = generation_job_manager.create_job(
        db=test_db,
        project_id=sample_project.id,
        shot_id=shot.id,
        generation_type="video_generation",
        input_parameters={"prompt": shot.video_prompt},
        provider="google_omni",
        model="gemini-omni-1.1-flash"
    )

    synth_clip = tmp_path / "rec_clip.mp4"
    ffmpeg_service.generate_synthetic_test_clip(synth_clip, duration=1.0, width=320, height=240)
    valid_mp4 = synth_clip.read_bytes()

    spent_before_video = sample_project.budget_spent
    with patch.object(google_video_adapter, "submit_video_generation", return_value={
        "status": "processing",
        "provider": "google_omni",
        "model": "gemini-omni-1.1-flash",
        "provider_operation_id": "interactions/omni-timeout-op-1",
    }) as mock_submit, patch.object(google_video_adapter, "poll_operation_status", return_value={
        "status": "processing",
        "provider_operation_id": "interactions/omni-timeout-op-1",
    }) as mock_poll, patch("backend.app.services.generation_job_manager.time.sleep", return_value=None):
        generation_job_manager._execute_video_job(job, {"prompt": shot.video_prompt}, test_db)

    assert mock_submit.call_count == 1
    assert mock_poll.call_count == 60
    test_db.refresh(job)
    test_db.refresh(sample_project)
    assert job.status == "provider_outcome_unknown"
    assert job.provider_operation_id == "interactions/omni-timeout-op-1"
    # Budget reservation was NOT released on polling timeout (Omni 1.1 5s rate = $0.50 est)
    assert round(sample_project.budget_spent - spent_before_video, 2) == 0.50
    res_rec = CostController.get_active_job_reservation(test_db, job.id, sample_project.id)
    assert res_rec is not None
    assert res_rec.action_type == "provider_outcome_unknown"
    # Releasing provider_outcome_unknown accidentally without confirmed failure must refuse
    assert CostController.release_reservation(test_db, sample_project.id, res_rec, reason="oops") is None

    # Recovery run when provider completes -> NO second submit_video_generation, NO duplicate reservation
    with patch.object(google_video_adapter, "submit_video_generation") as mock_submit_rec, \
         patch.object(google_video_adapter, "poll_operation_status", return_value={
             "status": "completed",
             "provider_operation_id": "interactions/omni-timeout-op-1",
             "video_bytes": valid_mp4,
         }), patch("backend.app.services.generation_job_manager.time.sleep", return_value=None):
        job.status = "processing"
        test_db.commit()
        generation_job_manager._execute_video_job(job, {"prompt": shot.video_prompt}, test_db)

    assert mock_submit_rec.call_count == 0
    test_db.refresh(job)
    test_db.refresh(sample_project)
    assert job.status == "completed"
    assert job.result_asset_id is not None
    # Still only charged once ($0.50)
    assert round(sample_project.budget_spent - spent_before_video, 2) == 0.50
    assert test_db.query(CostRecord).filter(
        CostRecord.project_id == sample_project.id,
        CostRecord.details.like(f"%job:{job.id}%"),
        CostRecord.action_type != "refund"
    ).count() == 1


def test_preflight_budget_enforcement_all_media_types(test_db, sample_project):
    """Verifies Image, Video, TTS, and Music all enforce pre-flight budget BEFORE calling Google APIs."""
    from fastapi import HTTPException

    sample_project.budget_total = 0.004
    sample_project.budget_spent = 0.0
    test_db.commit()

    # 1. Image (~$0.0336)
    img_adapter = GoogleImageAdapter(api_key="mock-key")
    img_adapter._client = MagicMock()
    with pytest.raises(HTTPException) as exc_img:
        img_adapter.generate_image(prompt="Test", project_id=sample_project.id, db=test_db)
    assert exc_img.value.status_code == 400
    assert img_adapter._client.interactions.create.call_count == 0
    assert img_adapter._client.models.generate_content.call_count == 0

    # 2. TTS ($0.01)
    tts_adapter = GoogleTTSAdapter(api_key="mock-key")
    tts_adapter._client = MagicMock()
    with pytest.raises(HTTPException) as exc_tts:
        tts_adapter.generate_dialogue_speech(text="Hello", project_id=sample_project.id, db=test_db)
    assert exc_tts.value.status_code == 400
    assert tts_adapter._client.interactions.create.call_count == 0

    # 3. Music ($0.08)
    music_adapter = GoogleMusicAdapter(api_key="mock-key")
    music_adapter._client = MagicMock()
    with pytest.raises(HTTPException) as exc_mus:
        music_adapter.generate_music_score(project_id=sample_project.id, db=test_db)
    assert exc_mus.value.status_code == 400
    assert music_adapter._client.interactions.create.call_count == 0

    # 4. Video job ($0.35 for 5s 720p Omni 1.2)
    vid_job = generation_job_manager.create_job(
        db=test_db,
        project_id=sample_project.id,
        generation_type="video_generation",
        input_parameters={"prompt": "Scene"},
        provider="google_omni",
        model="gemini-omni-1.2-flash"
    )
    with patch.object(google_video_adapter, "submit_video_generation") as mock_vsub:
        with pytest.raises(HTTPException) as exc_vid:
            generation_job_manager._execute_video_job(vid_job, {"prompt": "Scene"}, test_db)
        assert exc_vid.value.status_code == 400
        assert mock_vsub.call_count == 0


# ==============================================================================
# Nov 13, 2026 Upgrade & Critical Billing-Safety Regression Tests
# ==============================================================================


def test_video_dispatch_started_before_provider_and_missing_id_anomaly(test_db, sample_project):
    """
    CRITICAL BILLING SAFETY:
    1. `dispatch_state='dispatch_started'` is persisted durably BEFORE calling Google.
    2. When `interactions.create()` returns WITHOUT `.id`, NO synthetic `uuid4()` is invented;
       `SubmissionResponseAnomalyError` transitions job to `provider_outcome_unknown`, keeps
       `provider_operation_id=None`, preserves budget reservation, and blocks auto-retry!
    """
    from backend.app.services.generation_job_manager import DuplicateGenerationRiskError
    from backend.app.services.provider_error_classifier import SubmissionResponseAnomalyError

    scene = Scene(
        id=str(uuid.uuid4()),
        project_id=sample_project.id,
        scene_number=1,
        slugline="EXT. BRIDGE",
        script_content="Rain on steel."
    )
    test_db.add(scene)
    test_db.flush()
    shot = Shot(
        id=str(uuid.uuid4()),
        project_id=sample_project.id,
        scene_id=scene.id,
        shot_code="SC_ANOM_01",
        video_prompt="Wide bridge shot"
    )
    test_db.add(shot)
    test_db.commit()

    seen_dispatch_states = []

    adapter = GoogleVideoAdapter(api_key="mock-key")
    mock_client = MagicMock()
    # Anomaly: interaction object lacks a valid string `id`!
    malformed_inter = MagicMock()
    malformed_inter.id = None
    malformed_inter.status = "processing"

    def _capture_state_on_create(**kwargs):
        # Verify DB already committed dispatch_started BEFORE Google call runs!
        test_db.expire_all()
        cur_job = test_db.query(GenerationJob).filter(GenerationJob.shot_id == shot.id).first()
        seen_dispatch_states.append(cur_job.dispatch_state if cur_job else None)
        return malformed_inter

    mock_client.interactions.create.side_effect = _capture_state_on_create
    adapter._client = mock_client

    # Direct adapter test also confirms SubmissionResponseAnomalyError (never uuid4)
    with pytest.raises(SubmissionResponseAnomalyError):
        adapter.submit_video_generation(
            prompt=shot.video_prompt,
            project_id=sample_project.id,
            shot_id=shot.id,
            db=test_db,
        )

    # Full GenerationJobManager test with missing interaction.id
    job = generation_job_manager.create_job(
        db=test_db,
        project_id=sample_project.id,
        shot_id=shot.id,
        generation_type="video_generation",
        input_parameters={"prompt": shot.video_prompt},
        provider="google_omni",
        model="gemini-omni-1.2-flash",
    )
    assert job.dispatch_state == "not_dispatched"

    spent_before = sample_project.budget_spent or 0.0
    seen_dispatch_states.clear()
    with patch.object(google_video_adapter, "_api_key", "mock-key"), \
         patch.object(google_video_adapter, "_client", mock_client), \
         patch.object(google_video_adapter, "_injected_client", True):
        generation_job_manager._execute_video_job(job, {"prompt": shot.video_prompt}, test_db)

    assert seen_dispatch_states == ["dispatch_started"]
    test_db.refresh(job)
    test_db.refresh(sample_project)
    assert job.status == "provider_outcome_unknown"
    assert job.dispatch_state == "provider_outcome_unknown"
    assert job.provider_operation_id is None
    # Reservation preserved ($0.35 for 5s 720p Omni 1.2)
    assert round(sample_project.budget_spent - spent_before, 4) == 0.35

    # Automatic retry MUST be blocked without explicit acknowledgement
    with pytest.raises(DuplicateGenerationRiskError):
        generation_job_manager.retry_job(test_db, job.id, acknowledge_new_generation=False)


def test_crash_after_dispatch_started_recovery_preserves_reservation_and_blocks_retry(client, test_db, sample_project):
    """
    Simulates crash right after `dispatch_started` committed (before `provider_operation_id` saved):
    - `recover_interrupted_jobs` + `recover_interrupted_reservations` NEVER refund/release reservation
    - `job.status == 'provider_outcome_unknown'`, `job.dispatch_state == 'provider_outcome_unknown'`
    - `POST /api/jobs/generation/{id}/retry` returns HTTP 409 unless `acknowledge_new_generation=true`
    - `POST /api/jobs/generation/{id}/reconcile` with `confirmed_not_billed` releases cleanly!
    """
    from backend.app.services.cost_controller import CostController

    job = generation_job_manager.create_job(
        db=test_db,
        project_id=sample_project.id,
        generation_type="video_generation",
        input_parameters={"prompt": "Crash mid dispatch"},
        provider="google_omni",
        model="gemini-omni-1.2-flash",
    )
    reservation = CostController.reserve_budget(
        db=test_db,
        project_id=sample_project.id,
        estimated_cost=0.35,
        operation="video_generation",
        provider="google_omni",
        model="gemini-omni-1.2-flash",
        job_id=job.id,
    )
    job.status = "processing"
    job.dispatch_state = "dispatch_started"
    job.provider_operation_id = None
    test_db.commit()

    spent_before_recovery = sample_project.budget_spent
    generation_job_manager.recover_interrupted_jobs(test_db)

    test_db.refresh(job)
    test_db.refresh(sample_project)
    test_db.refresh(reservation)
    assert job.status == "provider_outcome_unknown"
    assert job.dispatch_state == "provider_outcome_unknown"
    assert job.provider_operation_id is None
    assert reservation.action_type == "provider_outcome_unknown"
    # Budget must NOT be refunded!
    assert round(sample_project.budget_spent - spent_before_recovery, 4) == 0.0

    # HTTP retry without explicit ack -> 409 Conflict
    r_block = client.post(f"/api/jobs/generation/{job.id}/retry")
    assert r_block.status_code == 409

    # Explicit reconciliation confirming not billed -> cleanly refunds reservation
    r_rec = client.post(
        f"/api/jobs/generation/{job.id}/reconcile",
        json={"remote_status": "confirmed_not_billed", "notes": "Checked Google AI Studio usage log: zero runs."},
    )
    assert r_rec.status_code == 200
    test_db.refresh(job)
    test_db.refresh(sample_project)
    assert job.dispatch_state == "provider_failed"
    assert job.status == "failed"
    assert round(sample_project.budget_spent - (spent_before_recovery - 0.35), 4) == 0.0

    # After reconciliation settled it as failed, retry IS allowed when desired (new job attempt)
    with patch.object(generation_job_manager, "launch_job_async"):
        r_retry = client.post(f"/api/jobs/generation/{job.id}/retry", json={"acknowledge_new_generation": True})
        assert r_retry.status_code == 200


def test_missing_output_never_releases_reservation_across_all_media(test_db, sample_project):
    """
    Missing output bytes when provider returned completed/accepted must NEVER auto-refund
    across Image, Video, TTS, or Music. Only confirmed failure/rejection releases.
    """
    from backend.app.services.google_image_adapter import GoogleImageAdapterError
    from backend.app.services.google_tts_adapter import GoogleTTSAdapterError
    from backend.app.services.google_music_adapter import GoogleMusicAdapterError

    # 1. Image completed with output_image=None -> error + charge preserved
    img_adapter = GoogleImageAdapter(api_key="mock-key")
    mock_img_client = MagicMock()
    empty_img_inter = MagicMock()
    empty_img_inter.id = "interactions/empty-img"
    empty_img_inter.status = "completed"
    empty_img_inter.output_image = None
    empty_img_inter.outputs = None
    empty_img_inter.steps = None
    mock_img_client.interactions.create.return_value = empty_img_inter
    img_adapter._client = mock_img_client

    spent0 = sample_project.budget_spent or 0.0
    with pytest.raises(GoogleImageAdapterError, match="no retrievable image bytes"):
        img_adapter.generate_image(prompt="Missing img", project_id=sample_project.id, db=test_db)
    test_db.refresh(sample_project)
    assert round(sample_project.budget_spent - spent0, 4) == 0.0336

    # 2. TTS completed with empty output -> error + charge preserved
    tts_adapter = GoogleTTSAdapter(api_key="mock-key")
    mock_tts_client = MagicMock()
    empty_tts_inter = MagicMock()
    empty_tts_inter.id = "interactions/empty-tts"
    empty_tts_inter.status = "completed"
    empty_tts_inter.output_audio = None
    empty_tts_inter.outputs = None
    mock_tts_client.interactions.create.return_value = empty_tts_inter
    tts_adapter._client = mock_tts_client

    spent1 = sample_project.budget_spent
    with pytest.raises(GoogleTTSAdapterError, match="no audio candidates"):
        tts_adapter.generate_dialogue_speech(text="Hi", project_id=sample_project.id, db=test_db)
    test_db.refresh(sample_project)
    assert round(sample_project.budget_spent - spent1, 4) == 0.01

    # 3. Music completed with empty output -> error + charge preserved
    mus_adapter = GoogleMusicAdapter(api_key="mock-key")
    mock_mus_client = MagicMock()
    empty_mus_inter = MagicMock()
    empty_mus_inter.id = "interactions/empty-mus"
    empty_mus_inter.status = "completed"
    empty_mus_inter.output_audio = None
    empty_mus_inter.outputs = None
    mock_mus_client.interactions.create.return_value = empty_mus_inter
    mus_adapter._client = mock_mus_client

    spent2 = sample_project.budget_spent
    with pytest.raises(GoogleMusicAdapterError, match="no audio content"):
        mus_adapter.generate_music_score(project_id=sample_project.id, db=test_db)
    test_db.refresh(sample_project)
    assert round(sample_project.budget_spent - spent2, 4) == 0.08

    # 4. Video poll completed with no video bytes/uri -> status='output_missing' + charge preserved
    vid_adapter = GoogleVideoAdapter(api_key="mock-key")
    mock_vid_client = MagicMock()
    empty_vid_inter = MagicMock()
    empty_vid_inter.status = "completed"
    empty_vid_inter.output_video = None
    empty_vid_inter.output_audio = None
    empty_vid_inter.outputs = None
    mock_vid_client.interactions.get.return_value = empty_vid_inter
    vid_adapter._client = mock_vid_client

    poll_res = vid_adapter.poll_operation_status("interactions/empty-vid")
    assert poll_res["status"] == "output_missing"


def test_structured_provider_error_classification_and_omni12_capabilities(test_db, sample_project, tmp_path):
    """
    Verifies structured HTTP code extraction (not just string matching) and Omni 1.2 Flash features:
    - Structured 400 error without '400' in message still releases reservation cleanly.
    - Structured 504/503 error or TimeoutError preserves reservation.
    - Omni 1.2 Flash supports 4k, 12s duration, delivery='uri', upscale with video input, and 6 ref images.
    """
    from backend.app.services.provider_error_classifier import classify_provider_exception

    class CustomSDKClientError(Exception):
        def __init__(self, msg, status_code):
            super().__init__(msg)
            self.status_code = status_code

    err_400 = CustomSDKClientError("Bad prompt parameter rejected by API", status_code=400)
    cls_400 = classify_provider_exception(err_400)
    assert cls_400.category == "rejected_before_generation"
    assert cls_400.http_status == 400

    err_504 = CustomSDKClientError("Upstream gateway error", status_code=504)
    cls_504 = classify_provider_exception(err_504)
    assert cls_504.category == "provider_outcome_unknown"
    assert cls_504.http_status == 504

    # Omni 1.2 Flash 4k + 6 reference images
    scene = Scene(
        id=str(uuid.uuid4()),
        project_id=sample_project.id,
        scene_number=1,
        slugline="EXT. SKYLINE",
        script_content="Wide skyline."
    )
    test_db.add(scene)
    test_db.flush()
    shot = Shot(
        id=str(uuid.uuid4()),
        project_id=sample_project.id,
        scene_id=scene.id,
        shot_code="SC_OMNI12_01",
        video_prompt="Epic 4k skyline sweep"
    )
    test_db.add(shot)
    test_db.commit()

    ref_imgs = []
    for i in range(6):
        p = tmp_path / f"ref_{i}.png"
        p.write_bytes(b"\x89PNG\r\n\x1a\nFAKEPNG")
        ref_imgs.append(str(p))

    adapter = GoogleVideoAdapter(api_key="mock-key", model="gemini-omni-1.2-flash")
    mock_client = MagicMock()
    mock_inter = MagicMock()
    mock_inter.id = "interactions/omni12-4k-refs"
    mock_client.interactions.create.return_value = mock_inter
    adapter._client = mock_client

    res = adapter.submit_video_generation(
        prompt="Epic sweep",
        project_id=sample_project.id,
        shot_id=shot.id,
        db=test_db,
        duration_seconds=12,
        resolution="4K",
        reference_image_paths=ref_imgs,
    )
    assert res["model"] == "gemini-omni-1.2-flash"
    assert res["task"] == "reference_to_video"
    assert res["resolution"] == "4k"
    assert res["delivery"] == "uri"
    assert res["target_duration_seconds"] == 12
    kwargs = mock_client.interactions.create.call_args[1]
    assert kwargs["response_format"] == {
        "type": "video",
        "aspect_ratio": "16:9",
        "resolution": "4k",
        "duration": "12s",
        "delivery": "uri",
    }

    # 7 reference images rejected
    p7 = tmp_path / "ref_7.png"
    p7.write_bytes(b"PNG")
    with pytest.raises(ValueError, match="at most 6"):
        adapter.submit_video_generation(
            prompt="Too many refs",
            project_id=sample_project.id,
            shot_id=shot.id,
            db=test_db,
            reference_image_paths=ref_imgs + [str(p7)],
        )


def test_ai_status_live_api_tested_honesty_with_and_without_key(client, tmp_path):
    """Verifies live_api_tested is NEVER inferred from API key presence alone, only real verification logs."""
    from backend.app.config import settings
    saved_key = settings.gemini_api_key
    saved_env_key = os.environ.get("GEMINI_API_KEY")
    try:
        # Even when an API key is configured, live_api_tested stays False unless verification file passes
        client.post("/api/system/ai-config", json={"gemini_api_key": "AIzaSyMOCKKEY1234567890"})
        res = client.get("/api/system/ai-status")
        assert res.status_code == 200
        data = res.json()
        assert data["gemini"]["api_key_configured"] is True
        assert data["live_api_tested"] is False
        assert data["model_catalog"]["gemini-4-hydrogen"]["live_api_tested"] is False
        assert data["model_catalog"]["gemini-4-hydrogen"]["mock_contract_tested"] is True

        # Verify reading an explicit PASS record activates ONLY that exact model
        from backend.app.services.google_model_catalog import build_model_catalog_payload
        ver_file = tmp_path / "live_api_verification.json"
        ver_file.write_text(json.dumps({
            "results": {
                "gemini-4-hydrogen": {"status": "PASS", "tested_at": "2026-11-13T10:00:00Z"}
            }
        }), encoding="utf-8")
        payload = build_model_catalog_payload(verification_file=ver_file)
        assert payload["any_live_api_tested"] is True
        assert payload["catalog"]["gemini-4-hydrogen"]["live_api_tested"] is True
        assert payload["catalog"]["gemini-omni-1.2-flash"]["live_api_tested"] is False
    finally:
        settings.gemini_api_key = saved_key
        if saved_env_key is None:
            os.environ.pop("GEMINI_API_KEY", None)
        else:
            os.environ["GEMINI_API_KEY"] = saved_env_key

