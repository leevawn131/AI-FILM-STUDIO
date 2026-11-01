"""
Cinematographer Agent.
Specializes in optical camera specs, anamorphic lenses, lighting schemes,
color grading anchors, and Google Flow video prompt formulation.
"""

from typing import Dict, Any, Optional

LENS_PROFILES = {
    "Wide Establishing Shot": "24mm wide angle cinema lens, deep depth of field, sharp architectural geometry",
    "Medium Two-Shot": "35mm anamorphic prime lens, subtle oval bokeh, natural human eye perspective",
    "Over-The-Shoulder Close Up": "50mm f/1.8 lens, creamy background separation, intimate focal plane",
    "Extreme Close-Up Reaction": "85mm macro portrait lens, shallow depth of field f/1.4, intense eye reflections",
    "Low Angle Heroic Dutch Tilt": "28mm wide anamorphic lens, dynamic vertical perspective, dramatic lens flare"
}

class CinematographerAgent:
    def optimize_flow_prompt(
        self,
        camera_angle: str,
        camera_movement: str,
        visual_description: str,
        lighting: str,
        visual_style: Optional[str] = None,
        character_anchors: Optional[str] = None
    ) -> Dict[str, str]:
        """Crafts a cinematic prompt optimized specifically for Google Flow."""
        lens = LENS_PROFILES.get(camera_angle, "35mm anamorphic lens, cinema prime")
        style = visual_style or "35mm film grain, 8k cinematic masterpiece, photorealistic, natural lighting, high dynamic range"

        chars_str = f" Subjects: {character_anchors}." if character_anchors else ""

        flow_prompt = (
            f"{camera_angle}, {camera_movement}. "
            f"{visual_description}.{chars_str} "
            f"Lens: {lens}. "
            f"Lighting: {lighting}. "
            f"Style: {style}. Film still, masterpiece quality, color graded."
        )

        negative_prompt = (
            "blurry, cartoon, 3d render, low resolution, oversaturated, deformed face, "
            "extra limbs, watermark, text overlay, floating artifacts, jerky movement"
        )

        return {
            "video_prompt": flow_prompt,
            "negative_prompt": negative_prompt
        }

cinematographer_agent = CinematographerAgent()
