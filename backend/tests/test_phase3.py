import io
import pytest
from fastapi.testclient import TestClient
from backend.app.main import app

client = TestClient(app)

def test_characters_and_locations_workflow():
    # Fetch demo project
    projects = client.get("/api/projects").json()
    assert len(projects) > 0
    project_id = projects[0]["id"]

    # 1. List characters (should have seeded REN SHINODA)
    chars = client.get(f"/api/characters/{project_id}").json()
    assert len(chars) >= 1
    assert any(c["name"] == "REN SHINODA" for c in chars)

    # 2. AI Character Designer generation
    char_gen_payload = {
        "project_id": project_id,
        "role": "Antagonist",
        "concept_prompt": "Ruthless cyber-syndicate executive"
    }
    char_res = client.post("/api/characters/ai-generate", json=char_gen_payload)
    assert char_res.status_code == 200
    new_char = char_res.json()
    assert "facial_characteristics" in new_char
    assert "clothing" in new_char
    assert "voice_profile" in new_char
    assert "personality" in new_char
    assert new_char["project_id"] == project_id
    new_char_id = new_char["id"]

    # 3. Upload reference image mock
    fake_img = io.BytesIO(b"fake image bytes")
    upload_res = client.post(
        f"/api/characters/{new_char_id}/upload-ref",
        files={"file": ("test_ref.jpg", fake_img, "image/jpeg")}
    )
    assert upload_res.status_code == 200
    assert "reference_url" in upload_res.json()

    # 4. List locations (should have seeded Spire 9)
    locs = client.get(f"/api/locations/{project_id}").json()
    assert len(locs) >= 1
    assert any("Spire 9" in l["name"] for l in locs)

    # 5. AI Location generation
    loc_gen_payload = {
        "project_id": project_id,
        "time_of_day": "Twilight",
        "atmosphere_prompt": "Heavy acid rain and neon reflections"
    }
    loc_res = client.post("/api/locations/ai-generate", json=loc_gen_payload)
    assert loc_res.status_code == 200
    new_loc = loc_res.json()
    assert "weather" in new_loc
    assert "lighting_setup" in new_loc
    assert "visual_prompt_tags" in new_loc
    assert new_loc["project_id"] == project_id

    # 6. Update location
    patch_loc = client.patch(f"/api/locations/{new_loc['id']}", json={"time_of_day": "Dawn"})
    assert patch_loc.status_code == 200
    assert patch_loc.json()["time_of_day"] == "Dawn"
