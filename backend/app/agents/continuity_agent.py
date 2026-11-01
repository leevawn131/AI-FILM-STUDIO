"""
Continuity Supervisor Agent.
Ensures consistent visual continuity across storyboard shots and scenes,
flagging lighting jumps, wardrobe deviations, and timeline contradictions.
"""

from typing import List, Dict, Any
from backend.app.services.offline_ai_engine import offline_ai

class ContinuityAgent:
    def verify_project_continuity(
        self,
        shots: List[Dict[str, Any]],
        characters: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Performs a comprehensive continuity audit across all sequence shots."""
        return offline_ai.analyze_continuity(shots=shots, characters=characters)

continuity_agent = ContinuityAgent()
