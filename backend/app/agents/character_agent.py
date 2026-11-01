"""
Character Designer Agent.
Specializes in generating consistent character design sheets,
facial characteristics, master clothing anchors, personality profiles,
and Google Flow visual consistency prompt tokens.
"""

from typing import Dict, Any, Optional
import random

ARCHETYPES = [
    {
        "role": "Protagonist",
        "name": "KAIEN CROSS",
        "age": "32",
        "face": "Sharp jawline, deep-set slate-grey eyes, faint linear cybernetic suture along left temple",
        "clothing": "Weathered ballistic nylon duster coat, slate tactical turtleneck, fingerless magnetic grip gloves",
        "voice": "Low gravelly baritone, calm measured rhythm, subtle electronic comm distortion",
        "personality": "Relentlessly analytical, hyper-vigilant, carrying quiet grief",
        "prompt_tags": "32yo male operative, slate-grey eyes, temple scar, black ballistic duster coat, high detail cinematic 8k"
    },
    {
        "role": "Antagonist / Rival",
        "name": "VALERIA VOSS",
        "age": "38",
        "face": "Striking high cheekbones, piercing icy blue eyes, sleek asymmetrical platinum-blonde bob cut",
        "clothing": "Tailored high-collar monochromatic graphite corporate suit with polished obsidian lapels",
        "voice": "Silky, aristocratic, chillingly composed with razor-sharp articulation",
        "personality": "Ruthlessly pragmatic, vision of synthetic evolution at any human cost",
        "prompt_tags": "38yo female corporate executive, icy blue eyes, asymmetrical platinum bob, sharp tailored graphite suit, cinematic lighting"
    },
    {
        "role": "Supporting Specialist",
        "name": "DR. ARUN CHEN",
        "age": "45",
        "face": "Warm observant dark brown eyes, wireframe round spectacles, silver streaks in dark wavy hair",
        "clothing": "Wrinkled utility vest over rolled-up sleeve linen shirt, multi-tool holsters",
        "voice": "Soft-spoken, rapid animated speech when explaining quantum mechanics",
        "personality": "Curious, compassionate, morally torn by weaponized technologies",
        "prompt_tags": "45yo scientist, round wire glasses, wavy hair with silver streaks, utility vest, warm expression, realistic 35mm photograph"
    }
]

class CharacterDesignerAgent:
    def design_character(
        self,
        genre: str,
        role_hint: Optional[str] = None,
        name_hint: Optional[str] = None,
        concept_prompt: Optional[str] = None
    ) -> Dict[str, Any]:
        """Creates a detailed consistency-locked character profile."""
        choice = random.choice(ARCHETYPES)
        name = name_hint or choice["name"]
        role = role_hint or choice["role"]

        if concept_prompt:
            face = f"Features tailored for '{concept_prompt}': {choice['face']}"
            clothing = f"Costume adapted for narrative context: {choice['clothing']}"
            prompt_tags = f"{name}, {choice['age']}yo {role.lower()}, {choice['prompt_tags']}"
        else:
            face = choice["face"]
            clothing = choice["clothing"]
            prompt_tags = f"{name}, {choice['prompt_tags']}"

        return {
            "name": name,
            "role": role,
            "age": choice["age"],
            "facial_characteristics": face,
            "clothing": clothing,
            "voice_profile": choice["voice"],
            "personality": choice["personality"],
            "visual_prompt_tags": prompt_tags
        }

character_designer_agent = CharacterDesignerAgent()
