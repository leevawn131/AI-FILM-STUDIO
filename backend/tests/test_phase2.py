import pytest
from fastapi.testclient import TestClient
from backend.app.main import app

client = TestClient(app)

def test_screenplay_and_storyboard_workflow():
    # 1. Fetch demo project
    projects = client.get("/api/projects").json()
    assert len(projects) > 0
    project_id = projects[0]["id"]

    # 2. List scenes
    scenes = client.get(f"/api/scenes/{project_id}").json()
    assert len(scenes) >= 1
    scene1_id = scenes[0]["id"]

    # 3. AI Screenwriter scene generation
    gen_payload = {
        "project_id": project_id,
        "prompt": "The protagonist uncovers a sabotaged neural core in the sub-aquatic wing."
    }
    gen_res = client.post("/api/scenes/generate", json=gen_payload)
    assert gen_res.status_code == 200
    new_scene = gen_res.json()
    assert "slugline" in new_scene
    assert "script_content" in new_scene
    assert new_scene["project_id"] == project_id
    new_scene_id = new_scene["id"]

    # 4. Director Agent breakdown for the new scene
    breakdown_res = client.post(f"/api/storyboard/generate/{new_scene_id}", json={"shot_count": 3})
    assert breakdown_res.status_code == 200
    shots = breakdown_res.json()
    assert len(shots) == 3
    shot1 = shots[0]
    assert "video_prompt" in shot1
    assert "camera_angle" in shot1
    assert shot1["generation_status"] in ["ready_for_flow", "approved"]

    # 5. Cinematographer prompt optimization
    opt_res = client.post(
        f"/api/storyboard/{shot1['id']}/optimize-prompt",
        json={"character_anchors": "Ren Shinoda in dark trenchcoat"}
    )
    assert opt_res.status_code == 200
    opt_shot = opt_res.json()
    assert "Ren Shinoda" in opt_shot["video_prompt"]

    # 6. Continuity Supervisor verification
    cont_res = client.post(f"/api/storyboard/continuity-check/{project_id}")
    assert cont_res.status_code == 200
    cont_data = cont_res.json()
    assert "status" in cont_data
    assert "score" in cont_data
    assert isinstance(cont_data["score"], (int, float))

    # 7. Update shot status
    patch_res = client.patch(f"/api/storyboard/{shot1['id']}", json={"generation_status": "approved"})
    assert patch_res.status_code == 200
    assert patch_res.json()["generation_status"] == "approved"
