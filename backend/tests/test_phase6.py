import pytest
from pathlib import Path
from fastapi.testclient import TestClient
from backend.app.main import app

client = TestClient(app)

def test_audio_management_and_export_workflow():
    # 1. Fetch demo project
    projects = client.get("/api/projects").json()
    assert len(projects) > 0
    project_id = projects[0]["id"]

    # 2. Synthesize Cinematic Ambient BGM Track
    bgm_res = client.post(
        f"/api/audio/generate-bgm/{project_id}",
        json={"title": "Test Cyber Ambient", "duration": 5.0, "mood": "cyberpunk"}
    )
    assert bgm_res.status_code == 200
    bgm_track = bgm_res.json()
    assert bgm_track["track_type"] == "music"
    assert bgm_track["duration_seconds"] > 0
    assert "url" in bgm_track

    # 3. Synthesize Scratch Voiceover Track
    vo_res = client.post(
        f"/api/audio/generate-voiceover/{project_id}",
        json={"character_name": "REN", "dialogue_text": "Neural link established.", "duration": 3.0}
    )
    assert vo_res.status_code == 200
    vo_track = vo_res.json()
    assert vo_track["track_type"] == "voiceover"
    assert vo_track["duration_seconds"] > 0

    # 4. List audio tracks
    tracks_res = client.get(f"/api/audio/{project_id}")
    assert tracks_res.status_code == 200
    tracks = tracks_res.json()
    assert len(tracks) >= 2

    # 5. List Export Presets
    presets_res = client.get("/api/export/presets")
    assert presets_res.status_code == 200
    presets = presets_res.json()
    assert "1080p_cinematic" in presets
    assert "720p_fast" in presets

    # 6. Launch Master Export Job
    export_payload = {
        "project_id": project_id,
        "preset_name": "720p_fast",
        "resolution": "640x360",
        "fps": 24,
        "include_subtitles": True
    }
    export_res = client.post(f"/api/export/render/{project_id}", json=export_payload)
    assert export_res.status_code == 200
    job = export_res.json()
    assert job["status"] == "completed"
    assert job["progress_pct"] == 100
    assert job["download_url"] is not None

    # 7. Download Master Export MP4
    down_res = client.get(job["download_url"])
    assert down_res.status_code == 200
    assert down_res.headers["content-type"] == "video/mp4"
    assert len(down_res.content) > 100
