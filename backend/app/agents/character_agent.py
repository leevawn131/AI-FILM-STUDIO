"""
Character Designer Agent.
Specializes in generating consistent, visually locked character design profiles,
including facial features, master costume anchors, personality, voice profiles,
and Google Flow / image generation consistency prompt tokens.
"""

import logging
from typing import Dict, Any, Optional
from backend.app.services.ai_provider import get_ai_provider
from backend.app.services.structured_output import CharacterProfileOutput

logger = logging.getLogger("character_agent")


class CharacterDesignerAgent:
    def design_character(
        self,
        genre: str,
        role_hint: Optional[str] = None,
        name_hint: Optional[str] = None,
        concept_prompt: Optional[str] = None,
        provider_preference: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Creates a detailed consistency-locked character profile using the active AI provider
        (Ollama / Gemini / Offline Studio Engine).
        """
        provider = get_ai_provider(provider_preference)
        role = role_hint or "Protagonist"
        name = name_hint or ("REN SHINODA" if "cyber" in genre.lower() else "KAIEN CROSS")
        
        user_prompt = (
            f"Film Genre: {genre}\n"
            f"Character Name: {name}\n"
            f"Narrative Role: {role}\n"
            f"Concept Direction: {concept_prompt or 'Complex, memorable character with distinctive visual traits.'}\n"
            "Create a complete, detailed character design profile with facial characteristics, costume, personality, voice, and visual prompt tags."
        )

        try:
            profile, meta = provider.generate_structured(
                prompt=user_prompt,
                schema=CharacterProfileOutput,
                system_prompt="You are a master character designer for major motion pictures. Produce a unique, three-dimensional character profile.",
                genre=genre,
                name_hint=name,
                role_hint=role
            )
            result = profile.model_dump()
            result["_generation_metadata"] = meta
            return result
        except Exception as e:
            logger.warning(f"AI character generation failed with {provider.provider_name} ({e}). Using offline engine.")
            from backend.app.services.ai_provider import OfflineTemplateProvider
            offline_p = OfflineTemplateProvider()
            profile, meta = offline_p.generate_structured(
                prompt=user_prompt,
                schema=CharacterProfileOutput,
                genre=genre,
                name_hint=name,
                role_hint=role
            )
            result = profile.model_dump()
            result["_generation_metadata"] = meta
            return result


character_designer_agent = CharacterDesignerAgent()
