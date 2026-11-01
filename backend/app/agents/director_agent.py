"""
Director Agent.
Breaks down screenplay scenes into visual beats, blocks camera choreography,
determines pacing, and manages shot lists.
"""

from typing import List, Dict, Any, Optional
from backend.app.services.gemini_service import gemini_service

class DirectorAgent:
    def breakdown_scene_to_shots(
        self,
        scene_slugline: str,
        scene_summary: str,
        genre: str,
        characters: Optional[List[Dict[str, str]]] = None,
        visual_style: Optional[str] = None,
        shot_count: int = 4
    ) -> List[Dict[str, Any]]:
        """Deconstructs dramatic text into sequential camera shots."""
        return gemini_service.generate_shots(
            scene_slugline=scene_slugline,
            scene_summary=scene_summary,
            genre=genre,
            characters=characters,
            visual_style=visual_style,
            count=shot_count
        )

    def analyze_pacing(self, shots: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Evaluates scene tempo and cut rhythm."""
        total_duration = sum(s.get("duration_seconds", 4.0) for s in shots)
        avg_shot_len = total_duration / len(shots) if shots else 0

        pacing_type = "Slow Atmospheric" if avg_shot_len > 5.0 else ("Fast Kinetic" if avg_shot_len < 3.0 else "Balanced Narrative")

        return {
            "total_scene_duration_sec": round(total_duration, 1),
            "shot_count": len(shots),
            "average_shot_duration_sec": round(avg_shot_len, 1),
            "pacing_style": pacing_type,
            "director_recommendation": f"Scene moves at a {pacing_type.lower()} pace. Ensure audio soundscape matches the rhythm."
        }

director_agent = DirectorAgent()
