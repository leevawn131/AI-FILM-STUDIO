"""
Offline Cinematic Studio Engine.
Generates screenplays, shot lists, Flow video prompts, character designs,
and continuity analyses without requiring any external paid API.
"""

import hashlib
import random
from typing import List, Dict, Any, Optional

GENRE_STYLES = {
    "Sci-Fi": {
        "lighting": ["neon cyan and magenta edge rim", "harsh fluorescent corridor flicker", "holographic ambient glow", "volumetric starship engine flare"],
        "angles": ["Dutch angle surveillance", "Low angle heroic wide", "Medium over-the-shoulder HUD view", "Macro close-up on biometric sensor"],
        "movements": ["Slow mechanical push-in", "Weightless zero-g orbital pan", "Fast rack focus from terminal to face", "Smooth tracking shot down cybernetic corridor"],
        "mood": "dystopian, atmospheric, hyper-detailed, synthwave reflections, chromatic aberration, 8k cinematic masterpiece"
    },
    "Cyberpunk": {
        "lighting": ["rain-slicked neon street reflections", "flickering sodium vapor and holographic signage", "deep high-contrast noir shadows"],
        "angles": ["Low angle looking up at towering skyscrapers", "Extreme close-up on cybernetic ocular lens", "Medium two-shot across rain-streaked window"],
        "movements": ["Dynamic gimbal orbit", "Low tracking shot along wet asphalt", "Whip pan to dark alleyway"],
        "mood": "gritty neon noir, rain droplets on anamorphic lens, blade runner aesthetic, dense volumetric haze"
    },
    "Noir / Mystery": {
        "lighting": ["Venetian blind shadow slats across face", "Single overhead tungsten bulb with smoke swirls", "Silhouetted streetlight in heavy fog"],
        "angles": ["High angle voyeuristic view", "Low angle silhouette", "Chiaroscuro profile close-up"],
        "movements": ["Slow creeping dolly backward", "Static locked-off frame with smoke drift", "Gentle diagonal pan"],
        "mood": "black and white rich tonality, 35mm film grain, 1940s classic noir lighting, mysterious atmosphere"
    },
    "Action / Thriller": {
        "lighting": ["High dynamic range sun flare", "Muzzle flash strobe illumination", "Emergency strobe warning lights"],
        "angles": ["Dynamic tilted low angle", "Handheld shaky cam point-of-view", "Overhead tactical bird's eye view"],
        "movements": ["Fast whip pan", "High-speed tracking shot following subject", "Snap zoom to tense eyes"],
        "mood": "intense adrenaline, motion blur on fast action, hyper-crisp shutter angle, blockbuster IMAX aesthetic"
    },
    "Drama / Indie": {
        "lighting": ["Soft natural golden hour window light", "Subtle practical bedside lamp", "Overcast twilight blue hour tone"],
        "angles": ["Intimate eye-level medium close-up", "Unobtrusive wide master shot", "Profile reflection in coffee shop glass"],
        "movements": ["Subtle breathing handheld movement", "Gentle slow dolly forward", "Static long take letting performance breathe"],
        "mood": "A24 cinematic realism, delicate depth of field f/1.4, rich analog color timing, emotional resonance"
    }
}

class OfflineAIEngine:
    def __init__(self):
        pass

    def generate_screenplay_scene(
        self,
        project_title: str,
        genre: str,
        scene_number: int,
        scene_prompt: Optional[str] = None,
        characters: Optional[List[Dict[str, str]]] = None,
        location: Optional[Dict[str, str]] = None
    ) -> Dict[str, Any]:
        """Procedurally crafts a rich screenplay scene with slugline, action, and dialogue."""
        genre_key = genre if genre in GENRE_STYLES else "Sci-Fi"
        char_names = [c.get("name", "Protagonist") for c in (characters or [])]
        if not char_names:
            char_names = ["KAI", "DR. ELENA VANCE"]

        primary_char = char_names[0]
        secondary_char = char_names[1] if len(char_names) > 1 else "OBSERVER"

        loc_name = location.get("name", "SUB-LEVEL RECOVERY LAB") if location else "SUB-LEVEL RESEARCH FACILITY"
        time_of_day = location.get("time_of_day", "NIGHT") if location else "NIGHT"

        slugline = f"INT. {loc_name.upper()} - {time_of_day.upper()}"
        
        prompt_hint = f" ({scene_prompt})" if scene_prompt else ""
        title = f"Scene {scene_number}: Breakthrough{prompt_hint}"

        script_content = f"""{slugline}

A dense silence hangs in the air, broken only by the low rhythmic hum of pressurized cooling conduits. Condensation drips onto cold brushed steel.

{primary_char} stands rigid before the central optical array. Blue phosphorescent light reflects off their focused eyes. Their breath forms shallow vapor in the refrigerated air.

{primary_char}
(whispering, voice tight)
If this sequence fails, the entire sector goes dark. We won't get a second window.

{secondary_char} steps out from the perimeter shadows, holding a portable telemetry scanner. A cautious nod.

{secondary_char}
The harmonics are already fluctuating beyond safe thresholds. We trigger the pulse now, or we abort.

{primary_char} touches the manual release lever. Tension spikes in their posture. A high-frequency whine builds from beneath the floorplates.

{primary_char}
Initializing sequence in three... two... one.

A blinding surge of cerulean radiance floods the chamber. The telemetry monitors scream with data cascades.

FADE OUT.
"""
        summary = f"{primary_char} and {secondary_char} initiate a critical high-stakes sequence in the {loc_name}, risking total sector failure as energy harmonics surge."
        return {
            "title": title,
            "slugline": slugline,
            "summary": summary,
            "script_content": script_content,
            "estimated_duration": 20
        }

    def generate_storyboard_shots(
        self,
        scene_slugline: str,
        scene_summary: str,
        genre: str,
        characters: Optional[List[Dict[str, str]]] = None,
        visual_style: Optional[str] = None,
        count: int = 4
    ) -> List[Dict[str, Any]]:
        """Generates a professional cinematographic shot list broken down with Flow-ready prompts."""
        style_pack = GENRE_STYLES.get(genre, GENRE_STYLES["Sci-Fi"])
        char_names = [c.get("name", "Subject") for c in (characters or [])]
        char_refs_map = {c.get("name"): c for c in (characters or []) if "name" in c}
        lead = char_names[0] if char_names else "Protagonist"

        shots_templates = [
            {
                "angle": "Wide Establishing Shot",
                "movement": "Slow Cinematic Crane Down",
                "duration": 5.0,
                "desc": f"Atmospheric wide view of {scene_slugline}. Volumetric haze drifts across industrial architecture while environmental lighting sets a tense mood.",
                "light": random.choice(style_pack["lighting"]),
                "actors": [lead] if lead else []
            },
            {
                "angle": "Medium Two-Shot",
                "movement": "Slow Dolly Push In",
                "duration": 4.5,
                "desc": f"{lead} locked in intense confrontation with surroundings, checking control monitors as shadows cut across their silhouette.",
                "light": "High contrast chiaroscuro with sharp edge rim lighting",
                "actors": char_names[:2]
            },
            {
                "angle": "Over-The-Shoulder Close Up",
                "movement": "Gentle Handheld Drift",
                "duration": 3.5,
                "desc": f"Framed past {lead}'s shoulder looking toward the fluctuating energy terminal. Sparks and soft optical flares dance in the shallow depth of field.",
                "light": "Dynamic practical light spill from console display",
                "actors": [lead]
            },
            {
                "angle": "Extreme Close-Up Reaction",
                "movement": "Static Locked-Off Frame",
                "duration": 3.0,
                "desc": f"Macro shot on {lead}'s eyes. The pulsing reflection of warning signals glows in their iris, pupils dilating under sheer pressure.",
                "light": "Subtle warm fill contrasting against cold neon eye reflections",
                "actors": [lead]
            },
            {
                "angle": "Low Angle Heroic Dutch Tilt",
                "movement": "Rapid Tracking Push",
                "duration": 4.0,
                "desc": f"Powerful low angle as the environmental climax unleashes. Dust particles suspended in beam of light as character takes decisive action.",
                "light": "Blinding backlight flare slicing through atmospheric mist",
                "actors": [lead]
            }
        ]

        shots = []
        for i in range(min(count, len(shots_templates))):
            tmpl = shots_templates[i]
            
            # Formulate Google Flow video prompt with character anchors and cinematic tokens
            char_visual_tokens = []
            for act in tmpl["actors"]:
                if act in char_refs_map:
                    cdata = char_refs_map[act]
                    traits = f"{act} ({cdata.get('facial_characteristics', '')}, wearing {cdata.get('clothing', '')})"
                    char_visual_tokens.append(traits)
            
            char_desc_str = f" Featuring {', '.join(char_visual_tokens)}." if char_visual_tokens else ""
            
            style_str = visual_style or "35mm anamorphic lens, 8k resolution, cinematic color grade, Photorealistic masterpiece, photoreal textures, Raytraced lighting"
            flow_prompt = (
                f"{tmpl['angle']}, {tmpl['movement']}. "
                f"{tmpl['desc']}{char_desc_str} "
                f"Lighting: {tmpl['light']}. "
                f"Camera style: {style_str}. High cinematic quality, steady professional cinematography, award winning movie frame."
            )

            shot_number = i + 1
            shots.append({
                "shot_number": shot_number,
                "camera_angle": tmpl["angle"],
                "camera_movement": tmpl["movement"],
                "duration_seconds": tmpl["duration"],
                "visual_description": tmpl["desc"],
                "lighting": tmpl["light"],
                "character_references": str(tmpl["actors"]),
                "video_prompt": flow_prompt,
                "negative_prompt": "blurry, low quality, cartoon, 3d render, watermark, extra limbs, oversaturated, deformed face, jittery motion",
                "seed": random.randint(100000, 999999)
            })

        return shots

    def analyze_continuity(self, shots: List[Dict[str, Any]], characters: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Continuity Supervisor Agent checks consistency across shots."""
        issues = []
        suggestions = []

        if len(shots) < 2:
            return {
                "status": "PASS",
                "score": 100,
                "issues": [],
                "suggestions": ["Add more shots to perform comprehensive cross-shot continuity verification."]
            }

        prev_shot = None
        for i, s in enumerate(shots):
            shot_code = s.get("shot_code", f"Shot {i+1}")
            # Check lighting consistency between adjacent shots
            curr_lighting = s.get("lighting", "").lower()
            if prev_shot:
                prev_lighting = prev_shot.get("lighting", "").lower()
                if "night" in prev_lighting and "day" in curr_lighting:
                    issues.append(f"{shot_code}: Sudden lighting jump from Night to Day without transitional passage.")
                if "golden hour" in prev_lighting and "neon" in curr_lighting:
                    suggestions.append(f"{shot_code}: Color temperature shifts noticeably from warm golden hour to cool neon.")

            # Check character tags
            char_refs = s.get("character_references", "[]")
            for c in characters:
                cname = c.get("name")
                if cname and cname in char_refs:
                    if not c.get("clothing"):
                        suggestions.append(f"Character '{cname}' lacks a defined master wardrobe in Character Database.")
            prev_shot = s

        score = max(50, 100 - (len(issues) * 20) - (len(suggestions) * 5))
        return {
            "status": "PASS" if not issues else "WARNING",
            "score": score,
            "issues": issues,
            "suggestions": suggestions or ["Lighting and visual direction show consistent continuity across sequence."]
        }

    def generate_flow_handoff_package(
        self,
        project_name: str,
        scenes: List[Dict[str, Any]],
        shots: List[Dict[str, Any]],
        characters: List[Dict[str, Any]],
        locations: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Generates comprehensive instructions, prompt sheets, and metadata for Google Flow."""
        instructions_md = f"""# GOOGLE FLOW PRODUCTION HANDOFF PACKAGE
## Project: {project_name}
### Generation Instructions for Filmmakers & Video AI Prompters

This production package organizes all camera angles, subject anchors, and video prompts
specifically optimized for **Google Flow** video & image generation.

---

### MASTER CHARACTER CONSISTENCY ANCHORS
"""
        for char in characters:
            instructions_md += f"""
#### Character: {char.get('name', 'Unknown')}
- **Role:** {char.get('role', 'N/A')} | **Age:** {char.get('age', 'N/A')}
- **Facial Characteristics:** {char.get('facial_characteristics', 'None recorded')}
- **Wardrobe Anchor:** {char.get('clothing', 'Standard costume')}
- **Voice Profile:** {char.get('voice_profile', 'Standard dialogue')}
- **Visual Prompt Tag:** `{char.get('visual_prompt_tags', '')}`
"""

        instructions_md += "\n---\n### SHOT LIST & FLOW VIDEO PROMPTS\n"
        for shot in shots:
            scode = shot.get("shot_code", f"SH_{shot.get('shot_number')}")
            instructions_md += f"""
#### [{scode}] - Duration: {shot.get('duration_seconds')}s
- **Camera Angle:** {shot.get('camera_angle')}
- **Camera Movement:** {shot.get('camera_movement')}
- **Lighting:** {shot.get('lighting')}
- **Video Prompt (Paste into Google Flow):**
```text
{shot.get('video_prompt')}
```
- **Negative Prompt:**
```text
{shot.get('negative_prompt')}
```
- **Target Filename When Downloaded:** `{scode}.mp4`
"""

        instructions_md += """
---
### AUTOMATIC MEDIA IMPORT INSTRUCTIONS
1. Generate videos in Google Flow using the prompts above.
2. Download the resulting MP4 clips.
3. Name your clips to include the shot code (e.g., `SC01_SH01.mp4` or `SC01_SH02_v1.mp4`).
4. Drop them into the Studio watched folder or upload via the Media Library.
5. AI Film Studio will automatically validate with FFprobe, match them to storyboard slots, and link them to your timeline.
"""
        return {
            "instructions_markdown": instructions_md,
            "total_shots": len(shots),
            "total_characters": len(characters),
            "estimated_generation_minutes": round(sum(s.get("duration_seconds", 4.0) for s in shots) / 60.0, 2)
        }

offline_ai = OfflineAIEngine()
