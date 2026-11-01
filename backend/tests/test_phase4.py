import pytest
from pathlib import Path
from fastapi.testclient import TestClient
from backend.app.main import app
from backend.app.config import settings

client = TestClient(app)

def test_flow_handoff_and_media_watcher_workflow():
    # 1. Fetch demo project
    projects = client.get("/api/projects").json()
    assert len(projects) > 0
    project_id = projects[0]["id"]

    # 2. Get Flow Handoff package info
    pkg_res = client.get(f"/api/flow-handoff/package-info/{project_id}")
    assert pkg_res.status_code == 200
    pkg_data = pkg_res.json()
    assert "instructions_markdown" in pkg_data
    assert "quick_copy_txt" in pkg_data
    assert pkg_data["total_shots"] >= 1

    # 3. Generate Flow package ZIP
    gen_zip_res = client.post(f"/api/flow-handoff/generate/{project_id}")
    assert gen_zip_res.status_code == 200
    assert "zip_filename" in gen_zip_res.json()

    # 4. Download package ZIP
    down_res = client.get(f"/api/flow-handoff/download/{project_id}")
    assert down_res.status_code == 200
    assert down_res.headers["content-type"] == "application/zip"
    assert len(down_res.content) > 100

    # 5. Check watcher status
    status_res = client.get("/api/media/watcher-status")
    assert status_res.status_code == 200
    assert "watch_directory_path" in status_res.json()

    # 6. Generate synthetic test clip for shot SC01_SH01
    clip_res = client.post(
        f"/api/media/generate-test-clip/{project_id}?shot_code=SC01_SH01&duration=2.0"
    )
    assert clip_res.status_code == 200
    clip_data = clip_res.json()
    assert clip_data["status"] == "success"
    assert "asset_id" in clip_data

    # 7. Verify shot SC01_SH01 is now auto-matched and has status 'imported'
    shots = client.get(f"/api/storyboard/{project_id}").json()
    sh1 = next((s for s in shots if s["shot_code"] == "SC01_SH01"), None)
    assert sh1 is not None
    assert sh1["generation_status"] == "imported"
    assert sh1["video_asset_id"] == clip_data["asset_id"]

    # 8. List project media assets
    media_res = client.get(f"/api/media/assets/{project_id}")
    assert media_res.status_code == 200
    assets = media_res.json()
    assert len(assets) >= 1
    assert assets[0]["width"] > 0
    assert assets[0]["duration_seconds"] > 0
    assert assets[0]["is_validated"] is True
