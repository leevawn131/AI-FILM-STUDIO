"""
Google Flow Handoff Module.
Generates:
- Formatted Google Flow video prompts
- Character consistency anchor guides
- Scene-by-scene camera instructions
- Downloadable production packages (.zip with JSON, Markdown, and TXT)
"""

import json
import zipfile
from pathlib import Path
from typing import Dict, Any
from sqlalchemy.orm import Session
from backend.app.config import settings
from backend.app.models.entities import Project, Scene, Shot, Character, Location
from backend.app.services.offline_ai_engine import offline_ai

class FlowHandoffService:
    @staticmethod
    def generate_package(db: Session, project_id: str) -> Dict[str, Any]:
        project = db.query(Project).filter(Project.id == project_id).first()
        if not project:
            raise ValueError("Project not found")

        scenes = db.query(Scene).filter(Scene.project_id == project_id).order_by(Scene.order_index).all()
        shots = db.query(Shot).filter(Shot.project_id == project_id).order_by(Shot.order_index).all()
        characters = db.query(Character).filter(Character.project_id == project_id).all()
        locations = db.query(Location).filter(Location.project_id == project_id).all()

        scenes_data = [{"slugline": s.slugline, "scene_number": s.scene_number, "summary": s.summary} for s in scenes]
        shots_data = [
            {
                "shot_number": s.shot_number,
                "shot_code": s.shot_code,
                "duration_seconds": s.duration_seconds,
                "camera_angle": s.camera_angle,
                "camera_movement": s.camera_movement,
                "lighting": s.lighting,
                "video_prompt": s.video_prompt,
                "negative_prompt": s.negative_prompt,
                "character_references": s.character_references
            }
            for s in shots
        ]
        chars_data = [
            {
                "name": c.name,
                "role": c.role,
                "age": c.age,
                "facial_characteristics": c.facial_characteristics,
                "clothing": c.clothing,
                "voice_profile": c.voice_profile,
                "visual_prompt_tags": c.visual_prompt_tags
            }
            for c in characters
        ]
        locs_data = [{"name": l.name, "time_of_day": l.time_of_day, "lighting_setup": l.lighting_setup} for l in locations]

        # Generate markdown instructions
        handoff_pkg = offline_ai.generate_flow_handoff_package(
            project_name=project.name,
            scenes=scenes_data,
            shots=shots_data,
            characters=chars_data,
            locations=locs_data
        )

        instructions_md = handoff_pkg["instructions_markdown"]

        # Quick copy text format
        quick_copy_lines = [f"# GOOGLE FLOW PROMPT SHEET: {project.name}\n"]
        for s in shots:
            quick_copy_lines.append(f"--- [{s.shot_code}] ({s.duration_seconds}s) ---")
            quick_copy_lines.append(f"PROMPT:\n{s.video_prompt}\n")
            quick_copy_lines.append(f"NEGATIVE:\n{s.negative_prompt}\n")
            quick_copy_lines.append(f"DOWNLOAD AS: {s.shot_code}.mp4\n\n")

        quick_copy_txt = "\n".join(quick_copy_lines)

        # JSON metadata
        json_meta = {
            "project_name": project.name,
            "project_id": project.id,
            "genre": project.genre,
            "aspect_ratio": project.aspect_ratio,
            "fps": project.fps,
            "visual_style": project.visual_style,
            "characters": chars_data,
            "shots": shots_data
        }

        # Build ZIP archive on disk
        pkg_dir = settings.projects_dir / project_id / "flow_packages"
        pkg_dir.mkdir(parents=True, exist_ok=True)
        zip_path = pkg_dir / f"flow_production_package_{project_id}.zip"

        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("INSTRUCTIONS_GOOGLE_FLOW.md", instructions_md)
            zf.writestr("flow_prompts_quick_copy.txt", quick_copy_txt)
            zf.writestr("flow_metadata.json", json.dumps(json_meta, indent=2))

        return {
            "zip_path": str(zip_path),
            "zip_filename": f"flow_production_package_{project.name.replace(' ', '_')}.zip",
            "instructions_markdown": instructions_md,
            "quick_copy_txt": quick_copy_txt,
            "total_shots": len(shots),
            "total_characters": len(characters),
            "estimated_minutes": handoff_pkg["estimated_generation_minutes"]
        }

flow_handoff_service = FlowHandoffService()
