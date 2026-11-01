import pytest
from fastapi.testclient import TestClient
from backend.app.main import app
from backend.app.database import init_db

client = TestClient(app)

@pytest.fixture(scope="module", autouse=True)
def setup_test_db():
    init_db()

def test_health_check():
    response = client.get("/api/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"
    assert data["ffmpeg_available"] is True

def test_list_projects():
    response = client.get("/api/projects")
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, list)
    assert len(data) >= 1
    assert data[0]["name"] == "Cyber Horizon: Echoes of 2088"

def test_create_and_delete_project():
    # Create
    payload = {
        "name": "The Obsidian Signal",
        "logline": "A deep space radio telescope picks up an ancient encrypted distress call.",
        "genre": "Sci-Fi",
        "aspect_ratio": "16:9",
        "fps": 24,
        "target_duration_sec": 180,
        "visual_style": "Kodak 5219 film stock, low key lighting, cold steel tones",
        "budget_total": 80.0
    }
    response = client.post("/api/projects", json=payload)
    assert response.status_code == 201
    created = response.json()
    project_id = created["id"]
    assert created["name"] == payload["name"]

    # Read
    get_res = client.get(f"/api/projects/{project_id}")
    assert get_res.status_code == 200
    assert get_res.json()["name"] == payload["name"]

    # Delete
    del_res = client.delete(f"/api/projects/{project_id}")
    assert del_res.status_code == 200

    # Verify 404 after delete
    get_res2 = client.get(f"/api/projects/{project_id}")
    assert get_res2.status_code == 404

def test_dashboard_stats():
    response = client.get("/api/dashboard/stats")
    assert response.status_code == 200
    stats = response.json()
    assert stats["total_projects"] >= 1
    assert "budget_allocated" in stats
    assert "total_scenes" in stats

def test_cost_summary():
    # Get initial demo project
    projects = client.get("/api/projects").json()
    project_id = projects[0]["id"]
    response = client.get(f"/api/cost/summary/{project_id}")
    assert response.status_code == 200
    summary = response.json()
    assert summary["project_id"] == project_id
    assert "budget_total" in summary
