"""
Cinematographer Agent.
Specializes in optical camera specifications, anamorphic lenses, lighting schemes,
framing, composition, color grading anchors, and Google Flow video prompt formulation.
"""

import logging
from typing import Dict, Any, Optional
from backend.app.services.ai_provider import get_ai_provider
from backend.app.services.structured_output import CinematographyOutput

logger = logging.getLogger("cinematographer_agent")

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
        character_anchors: Optional[str] = None,
        provider_preference: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Crafts a professional cinematography prompt optimized specifically for Google Flow
        using the active AI provider or deterministic optical lens formulas.
        """
        provider = get_ai_provider(provider_preference)
        style = visual_style or "35mm film grain, 8k cinematic masterpiece, photorealistic, natural lighting, high dynamic range"
        chars_str = f" Subjects: {character_anchors}." if character_anchors else ""

        user_prompt = (
            f"Camera Angle: {camera_angle}\n"
            f"Camera Movement: {camera_movement}\n"
            f"Action/Description: {visual_description}\n"
            f"Lighting: {lighting}\n"
            f"Visual Style: {style}\n"
            f"{chars_str}\n"
            "Formulate the ultimate cinematic Google Flow video prompt including camera body, lens, framing, lighting, and negative prompt."
        )

        try:
            profile, meta = provider.generate_structured(
                prompt=user_prompt,
                schema=CinematographyOutput,
                system_prompt="You are a master Hollywood Director of Photography (DP). Formulate precise optical camera specs and prompts."
            )
            return {
                "video_prompt": profile.video_prompt,
                "negative_prompt": profile.negative_prompt,
                "camera": profile.camera,
                "lens": profile.lens,
                "lighting": profile.lighting,
                "composition": profile.composition,
                "color_palette": profile.color_palette,
                "_generation_metadata": meta
            }
        except Exception as e:
            logger.debug(f"AI cinematography optimization fallback: {e}")
            lens = LENS_PROFILES.get(camera_angle, "35mm anamorphic cinema prime lens")
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
            from backend.app.services.ai_provider import OfflineTemplateProvider
            offline_meta = OfflineTemplateProvider()._meta()
            return {
                "video_prompt": flow_prompt,
                "negative_prompt": negative_prompt,
                "lens": lens,
                "_generation_metadata": offline_meta
            }


cinematographer_agent = CinematographerAgent()
