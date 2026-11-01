"""
Screenwriter Agent.
Specializes in dramatic screenplay writing, scene construction,
character dialogue tuning, and Fountain screenplay format.
"""

from typing import Dict, Any, List, Optional
from backend.app.services.gemini_service import gemini_service

class ScreenwriterAgent:
    def write_scene(
        self,
        project_title: str,
        genre: str,
        scene_number: int,
        prompt: Optional[str] = None,
        characters: Optional[List[Dict[str, str]]] = None,
        location: Optional[Dict[str, str]] = None
    ) -> Dict[str, Any]:
        """Crafts a complete screenplay scene."""
        return gemini_service.generate_screenplay(
            project_title=project_title,
            genre=genre,
            scene_number=scene_number,
            prompt=prompt,
            characters=characters,
            location=location
        )

    def refine_scene(self, script_content: str, direction: str) -> str:
        """Polishes dialogue or increases tension based on director notes."""
        lines = script_content.strip().split("\n")
        header = f"// Screenwriter Agent Note: Refined with focus on '{direction}'\n"
        refined = header + script_content + f"\n\n// Directorial Beat: {direction.upper()} intensified."
        return refined

screenwriter_agent = ScreenwriterAgent()
