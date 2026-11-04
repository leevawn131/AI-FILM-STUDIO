"""
Structured Output Validation & Repair Module.
Ensures AI agents produce valid, schema-compliant JSON data with robust recovery,
sanitization, and graceful fallback handling.
"""

import json
import re
import logging
from typing import Type, TypeVar, Optional, Any, Tuple, List
from pydantic import BaseModel, Field, ValidationError

logger = logging.getLogger("structured_output")

T = TypeVar("T", bound=BaseModel)


# --- Common Agent Output Schemas ---

class ScreenplaySceneOutput(BaseModel):
    title: str = Field(..., description="Scene title")
    slugline: str = Field(..., description="Standard Fountain slugline, e.g. INT. LAB - NIGHT")
    summary: str = Field(..., description="Brief dramatic summary of the scene")
    script_content: str = Field(..., description="Full screenplay text with slugline, action, character names, and dialogue")
    estimated_duration: int = Field(15, description="Estimated duration in seconds")


class ShotBreakdownItem(BaseModel):
    shot_number: int = Field(1, description="Shot order number within the scene")
    shot_type: str = Field("Medium Shot", description="Wide Establishing, Medium, Close-Up, Extreme Close-Up, etc.")
    camera_angle: str = Field("Eye Level", description="Eye Level, Low Angle, High Angle, Dutch Angle, Overhead")
    camera_movement: str = Field("Static", description="Static, Slow Push In, Pan Left, Tracking, Dolly")
    subject: str = Field("", description="Main character or object in focus")
    action: str = Field("", description="Dramatic action occurring during the shot")
    environment: str = Field("", description="Background or location details")
    lighting: str = Field("Cinematic volumetric lighting", description="Lighting style and direction")
    mood: str = Field("Dramatic", description="Emotional atmosphere")
    composition: str = Field("Rule of thirds", description="Framing and visual arrangement")
    duration_seconds: float = Field(4.0, description="Shot duration in seconds")
    visual_prompt: str = Field(..., description="Complete cinematic image/video prompt for Google Flow or image generator")
    negative_prompt: str = Field("blurry, distorted, low quality, cartoon, watermark, extra limbs", description="Negative prompt")


class SceneBreakdownOutput(BaseModel):
    pacing_style: str = Field("Balanced Narrative", description="Slow Atmospheric, Balanced Narrative, Fast Kinetic")
    director_recommendation: Optional[str] = Field("", description="Director note on visual staging and pacing")
    shots: List[ShotBreakdownItem] = Field(default_factory=list, description="Ordered list of camera shots")


class CharacterProfileOutput(BaseModel):
    name: str = Field(..., description="Character full name")
    role: str = Field("Protagonist", description="Narrative role (Protagonist, Antagonist, Mentor, Supporting)")
    age: str = Field("30", description="Age or age range")
    facial_characteristics: str = Field(..., description="Distinctive facial features, eyes, scars, hairstyle")
    clothing: str = Field(..., description="Signature wardrobe, textures, colors, and accessories")
    personality: str = Field(..., description="Core personality traits, emotional demeanor, motivations")
    voice_profile: str = Field(..., description="Pitch, tone, accent, rhythm, speech idiosyncrasies")
    visual_prompt_tags: str = Field(..., description="Comma-separated consistency prompt tags for AI video/image generators")


class LocationOutput(BaseModel):
    name: str = Field(..., description="Location name")
    description: str = Field(..., description="Visual and environmental description of the location")
    time_of_day: str = Field("Night", description="Time of day, e.g. Dawn, Golden Hour, Noon, Midnight")
    weather: str = Field("Clear", description="Atmospheric weather conditions")
    lighting_setup: str = Field(..., description="Detailed lighting setup and color temperature")
    visual_prompt_tags: str = Field(..., description="Visual prompt tags for image and video generation")


class CinematographyOutput(BaseModel):
    camera: str = Field("Arri Alexa LF", description="Camera body specification")
    lens: str = Field("35mm anamorphic prime", description="Lens model, focal length, aperture")
    lighting: str = Field("High-contrast chiaroscuro", description="Lighting arrangement, color temperature, key/fill ratio")
    framing: str = Field("Wide aspect 2.39:1", description="Framing and aspect ratio")
    composition: str = Field("Rule of thirds with deep depth of field", description="Spatial visual arrangement")
    color_palette: str = Field("Cool cyan shadows and warm amber highlights", description="Color grading scheme")
    visual_style: str = Field("35mm film grain, 8k cinematic masterpiece, photorealistic", description="Overall aesthetic anchor")
    video_prompt: str = Field(..., description="Detailed Flow video generation prompt")
    negative_prompt: str = Field("blurry, low quality, oversaturated, deformed face, text overlay", description="Negative prompt")


class ContinuityAuditOutput(BaseModel):
    status: str = Field("PASS", description="PASS or WARNING")
    score: int = Field(100, description="Continuity score between 0 and 100")
    issues: List[str] = Field(default_factory=list, description="List of continuity discrepancies detected")
    suggestions: List[str] = Field(default_factory=list, description="Constructive suggestions for visual coherence")


class AssemblyClipItem(BaseModel):
    shot_id: Optional[str] = None
    shot_code: str
    order_index: int
    start_time_seconds: float
    duration_seconds: float
    end_time_seconds: float
    transition: str = "hard_cut" # hard_cut, dissolve, fade_in, fade_out
    camera_angle: Optional[str] = None
    video_asset_id: Optional[str] = None
    audio_cue: Optional[str] = None


class AssemblyCutOutput(BaseModel):
    total_timeline_duration_sec: float
    clip_count: int
    rhythm_assessment: str
    pacing_recommendation: Optional[str] = None
    clips: List[AssemblyClipItem]


# --- JSON Extraction & Repair Functions ---

def extract_json_block(text: str) -> str:
    """Extracts raw JSON content from markdown code fences or conversational text."""
    if not text:
        return ""

    # 1. Look for ```json ... ``` or ``` ... ```
    fence_match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text, re.IGNORECASE)
    if fence_match:
        return fence_match.group(1).strip()

    # 2. Look for outermost curly braces { ... }
    first_brace = text.find("{")
    last_brace = text.rfind("}")
    if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
        return text[first_brace:last_brace + 1].strip()

    # 3. Look for outermost brackets [ ... ]
    first_bracket = text.find("[")
    last_bracket = text.rfind("]")
    if first_bracket != -1 and last_bracket != -1 and last_bracket > first_bracket:
        return text[first_bracket:last_bracket + 1].strip()

    return text.strip()


def repair_json_string(raw_json: str) -> str:
    """Repairs common AI JSON formatting defects."""
    if not raw_json:
        return "{}"

    s = raw_json.strip()

    # Replace smart quotes with standard quotes
    s = s.replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'")

    # Replace single-quoted dictionary keys and simple string values with double quotes
    s = re.sub(r"'(?=(?:[^\"\\]|\\.)*$)", '"', s)
    # Also replace Python True/False/None with JSON true/false/null
    s = re.sub(r"\bTrue\b", "true", s)
    s = re.sub(r"\bFalse\b", "false", s)
    s = re.sub(r"\bNone\b", "null", s)

    # Remove trailing commas before closing braces/brackets
    s = re.sub(r",\s*([\}\]])", r"\1", s)

    # Balance unclosed brackets or braces if model was cut off
    open_curly = s.count("{")
    close_curly = s.count("}")
    if open_curly > close_curly:
        s += "}" * (open_curly - close_curly)

    open_square = s.count("[")
    close_square = s.count("]")
    if open_square > close_square:
        s += "]" * (open_square - close_square)

    return s


def parse_and_validate_structured_json(
    raw_text: str,
    schema: Type[T]
) -> Tuple[Optional[T], Optional[str]]:
    """
    Attempts to extract, repair, parse, and validate JSON text against a Pydantic schema.
    Returns: (validated_object, error_message)
    """
    json_block = extract_json_block(raw_text)
    if not json_block:
        return None, "No JSON content found in model response."

    # First attempt: direct parse
    try:
        parsed_data = json.loads(json_block)
        validated = schema.model_validate(parsed_data)
        return validated, None
    except (json.JSONDecodeError, ValidationError) as first_err:
        logger.debug(f"Direct JSON parse failed: {first_err}. Attempting heuristic repair...")

    # Second attempt: repaired JSON
    repaired = repair_json_string(json_block)
    try:
        parsed_data = json.loads(repaired)
        validated = schema.model_validate(parsed_data)
        return validated, None
    except (json.JSONDecodeError, ValidationError) as second_err:
        return None, f"JSON validation failed after repair: {second_err}"
