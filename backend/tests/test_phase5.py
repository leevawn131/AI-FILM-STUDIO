import pytest
from pathlib import Path
from fastapi.testclient import TestClient
from backend.app.main import app

client = TestClient(app)

def test_editor_and_ffmpeg_render_workflow():
    # 1. Fetch demo project
    projects = client.get("/api/projects").json()
    assert len(projects) > 0
    project_id = projects[0]["id"]

    # 2. Get Timeline
    tl_res = client.get(f"/api/editor/timeline/{project_id}")
    assert tl_res.status_code == 200
    timeline = tl_res.json()
    assert "timeline_items" in timeline
    assert len(timeline["timeline_items"]) >= 1
    assert timeline["total_duration_seconds"] > 0

    # 3. AI Assembly Cut by Film Editor Agent
    assembly_res = client.post(f"/api/editor/ai-assembly-cut/{project_id}")
    assert assembly_res.status_code == 200
    assembly = assembly_res.json()
    assert "clips" in assembly
    assert len(assembly["clips"]) >= 1
    assert "total_timeline_duration_sec" in assembly

    # 4. Render timeline preview with FFmpeg
    render_res = client.post(
        f"/api/editor/render-preview/{project_id}",
        json={"resolution": "640x360", "fps": 24, "burn_subtitles": True}
    )
    assert render_res.status_code == 200
    render_data = render_res.json()
    assert "video_url" in render_data
    assert "duration_seconds" in render_data
    assert render_data["duration_seconds"] > 0
    assert render_data["file_size_bytes"] > 0
