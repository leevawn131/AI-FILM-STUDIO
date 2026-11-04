"""
Film Editor Agent.
Specializes in dramatic cut timings, montage assembly, cross-shot pacing,
transitions (hard cut, dissolve, fade, J-cut/L-cut cues), and audio sync cues.
"""

import logging
from typing import List, Dict, Any, Optional
from backend.app.services.ai_provider import get_ai_provider
from backend.app.services.structured_output import AssemblyCutOutput

logger = logging.getLogger("editor_agent")


class EditorAgent:
    def create_assembly_cut(
        self,
        shots: List[Dict[str, Any]],
        pacing_preference: Optional[str] = None,
        provider_preference: Optional[str] = None
    ) -> Dict[str, Any]:
        """Arranges video shots into an optimized assembly sequence with cut timings and transitions."""
        if not shots:
            return {
                "total_timeline_duration_sec": 0.0,
                "clip_count": 0,
                "rhythm_assessment": "No shots provided for assembly.",
                "clips": []
            }

        provider = get_ai_provider(provider_preference)
        shots_summary = "\n".join([
            f"Shot {s.get('shot_code', i+1)}: Angle={s.get('camera_angle')}, Dur={s.get('duration_seconds')}s"
            for i, s in enumerate(shots)
        ])

        user_prompt = (
            f"Pacing Preference: {pacing_preference or 'Balanced Narrative'}\n"
            f"Shot Sequence:\n{shots_summary}\n\n"
            "Analyze and arrange these shots into an optimal assembly cut. Determine transition for each shot (hard_cut, dissolve, fade_in, fade_out), start and end timestamps, and audio cues."
        )

        try:
            cut, meta = provider.generate_structured(
                prompt=user_prompt,
                schema=AssemblyCutOutput,
                system_prompt="You are an ACE award-winning Film Editor. Structure rhythmic assembly cuts with precise transition and cut timing recommendations.",
                shots=shots
            )
            res = cut.model_dump()
            res["_generation_metadata"] = meta
            return res
        except Exception as e:
            logger.debug(f"AI editor assembly cut fallback: {e}")
            assembled_shots = []
            accumulated_time = 0.0

            for idx, shot in enumerate(shots):
                duration = float(shot.get("duration_seconds", 4.0))
                angle = shot.get("camera_angle", "")
                if "Wide Establishing" in angle:
                    transition = "fade_in" if idx == 0 else "dissolve"
                elif "Extreme Close-Up" in angle:
                    transition = "hard_cut"
                else:
                    transition = "hard_cut"

                assembled_shots.append({
                    "shot_id": shot.get("id"),
                    "shot_code": shot.get("shot_code", f"SH{idx+1:02d}"),
                    "order_index": idx + 1,
                    "start_time_seconds": round(accumulated_time, 2),
                    "duration_seconds": round(duration, 2),
                    "end_time_seconds": round(accumulated_time + duration, 2),
                    "transition": transition,
                    "camera_angle": angle,
                    "video_asset_id": shot.get("video_asset_id")
                })
                accumulated_time += duration

            from backend.app.services.ai_provider import OfflineTemplateProvider
            return {
                "total_timeline_duration_sec": round(accumulated_time, 2),
                "clip_count": len(assembled_shots),
                "rhythm_assessment": "Cinematic narrative assembly cut ready for preview rendering.",
                "clips": assembled_shots,
                "_generation_metadata": OfflineTemplateProvider()._meta()
            }


editor_agent = EditorAgent()
