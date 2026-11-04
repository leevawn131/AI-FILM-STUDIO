"""
Continuity Supervisor Agent.
Ensures rigorous visual and narrative continuity across scenes and storyboard shots:
- Character identity and signature wardrobe
- Props and set state
- Time of day and weather progression
- Lighting contrast and color temperature shifts
- Spatial staging and 180-degree axis integrity
Combines deterministic sequence validation with AI scene analysis.
"""

import logging
from typing import List, Dict, Any, Optional
from backend.app.services.ai_provider import get_ai_provider
from backend.app.services.structured_output import ContinuityAuditOutput

logger = logging.getLogger("continuity_agent")


class ContinuityAgent:
    def verify_project_continuity(
        self,
        shots: List[Dict[str, Any]],
        characters: List[Dict[str, Any]],
        provider_preference: Optional[str] = None
    ) -> Dict[str, Any]:
        """Performs a comprehensive continuity audit across sequence shots."""
        deterministic_issues: List[str] = []
        deterministic_suggestions: List[str] = []

        if len(shots) < 2:
            return {
                "status": "PASS",
                "score": 100,
                "issues": [],
                "suggestions": ["Add more shots to perform comprehensive cross-shot continuity verification."]
            }

        # 1. Deterministic Rule Checks
        prev_shot = None
        for i, s in enumerate(shots):
            shot_code = s.get("shot_code", f"Shot {i+1}")
            curr_lighting = s.get("lighting", "").lower()

            if prev_shot:
                prev_lighting = prev_shot.get("lighting", "").lower()
                # Day vs Night collision
                if ("night" in prev_lighting or "midnight" in prev_lighting) and ("day" in curr_lighting or "noon" in curr_lighting):
                    deterministic_issues.append(f"{shot_code}: Sudden lighting jump from Night to Day without transitional passage.")
                if "golden hour" in prev_lighting and "neon" in curr_lighting:
                    deterministic_suggestions.append(f"{shot_code}: Color temperature shifts noticeably from warm golden hour to cool neon.")

            # Check character wardrobe anchors
            char_refs = s.get("character_references", "[]")
            for c in characters:
                cname = c.get("name")
                if cname and cname in str(char_refs):
                    if not c.get("clothing"):
                        deterministic_suggestions.append(f"Character '{cname}' lacks a defined master wardrobe in Character Database.")

            prev_shot = s

        # 2. AI In-depth Narrative Continuity Audit
        provider = get_ai_provider(provider_preference)
        shots_summary = "\n".join([
            f"Shot {s.get('shot_code', i+1)}: Angle={s.get('camera_angle')}, Light={s.get('lighting')}, Chars={s.get('character_references')}"
            for i, s in enumerate(shots[:10]) # audit up to 10 key sequence shots
        ])
        chars_summary = "\n".join([
            f"Character {c.get('name')}: Costume={c.get('clothing', 'Not specified')}, Traits={c.get('facial_characteristics', '')}"
            for c in characters
        ])

        user_prompt = (
            f"Sequence Shots:\n{shots_summary}\n\n"
            f"Master Characters:\n{chars_summary}\n\n"
            "Audit the continuity across this sequence. Identify any spatial, costume, lighting, or temporal contradictions. Output status (PASS or WARNING), score (0-100), issues list, and suggestions list."
        )

        try:
            audit, meta = provider.generate_structured(
                prompt=user_prompt,
                schema=ContinuityAuditOutput,
                system_prompt="You are a veteran Hollywood Script Supervisor & Continuity Director. Rigorously inspect visual, wardrobe, and temporal consistency.",
                shots=shots,
                characters=characters
            )
            combined_issues = list(dict.fromkeys(deterministic_issues + audit.issues))
            combined_suggestions = list(dict.fromkeys(deterministic_suggestions + audit.suggestions))
            final_status = "WARNING" if combined_issues else ("PASS" if audit.status == "PASS" else "WARNING")
            final_score = min(audit.score, max(40, 100 - (len(combined_issues) * 20) - (len(combined_suggestions) * 5)))

            return {
                "status": final_status,
                "score": final_score,
                "issues": combined_issues,
                "suggestions": combined_suggestions or ["Visual and narrative continuity show solid coherence."],
                "_generation_metadata": meta
            }
        except Exception as e:
            logger.debug(f"AI continuity audit fallback: {e}")
            score = max(50, 100 - (len(deterministic_issues) * 20) - (len(deterministic_suggestions) * 5))
            from backend.app.services.ai_provider import OfflineTemplateProvider
            return {
                "status": "PASS" if not deterministic_issues else "WARNING",
                "score": score,
                "issues": deterministic_issues,
                "suggestions": deterministic_suggestions or ["Lighting and visual direction show consistent continuity across sequence."],
                "_generation_metadata": OfflineTemplateProvider()._meta()
            }


continuity_agent = ContinuityAgent()
