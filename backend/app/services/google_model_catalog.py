"""
Centralized Google Cloud AI model catalog (verified against Google Gemini docs as of Nov 13, 2026).

Provides authoritative lifecycle status (`stable`, `fallback`, `deprecated`, `unknown`),
capabilities, supported sizes/ratios/resolutions, pricing estimates (explicitly marked as
estimates rather than invoiced billing), and honest `mock_contract_tested` vs `live_api_tested`
verification reporting.
"""
from pathlib import Path
from typing import Any, Dict, List, Optional
import json

from ..config import settings


DEFAULT_LIVE_VERIFICATION_FILE = Path(settings.data_dir) / "live_api_verification.json"

NANO_BANANA_PANORAMIC_ASPECT_RATIOS: List[str] = [
    "1:1",
    "2:3",
    "3:2",
    "3:4",
    "4:3",
    "4:5",
    "5:4",
    "9:16",
    "16:9",
    "21:9",
    "1:4",
    "4:1",
    "1:8",
    "8:1",
]

STANDARD_IMAGE_ASPECT_RATIOS: List[str] = [
    "1:1",
    "2:3",
    "3:2",
    "3:4",
    "4:3",
    "4:5",
    "5:4",
    "9:16",
    "16:9",
    "21:9",
]

IMAGE_SUPPORTED_SIZES_BY_MODEL: Dict[str, List[str]] = {
    "gemini-nano-banana-2.1": ["1K", "2K", "4K"],
    "gemini-3.1-flash-lite-image": ["512", "1K"],
    "gemini-3-pro-image": ["1K", "2K", "4K"],
}

DEPRECATED_IMAGE_MODELS: Dict[str, str] = {
    "gemini-3.1-flash-image": "Deprecated as of Nov 2026; migrated to gemini-nano-banana-2.1.",
}

VIDEO_SUPPORTED_TASKS: List[str] = [
    "text_to_video",
    "image_to_video",
    "reference_to_video",
    "edit",
    "extend",
    "upscale",
]

MODEL_CATALOG: Dict[str, Dict[str, Any]] = {
    # --- Text Reasoning ---
    "gemini-4-hydrogen": {
        "model_id": "gemini-4-hydrogen",
        "display_name": "Gemini 4 Hydrogen",
        "category": "text",
        "status": "stable",
        "role": "default",
        "api_surface": "models.generate_content",
        "capabilities": [
            "reasoning",
            "scriptwriting",
            "structured_json",
            "scene_breakdown",
            "multimodal_understanding",
            "long_context_1m",
        ],
        "pricing_estimate": {
            "currency": "USD",
            "is_estimate": True,
            "input_per_1m_tokens": 1.50,
            "output_per_1m_tokens": 7.50,
            "note": "Estimated Google published pricing (Nov 12, 2026 GA); free tier supported.",
        },
        "mock_contract_tested": True,
        "notes": "Default general-purpose reasoning and screenplay model (1M context window).",
    },
    "gemini-4-carbon": {
        "model_id": "gemini-4-carbon",
        "display_name": "Gemini 4 Carbon",
        "category": "text",
        "status": "stable",
        "role": "deep_reasoning",
        "api_surface": "models.generate_content",
        "capabilities": [
            "deep_reasoning",
            "complex_planning",
            "creative_director",
            "structured_json",
        ],
        "pricing_estimate": {
            "currency": "USD",
            "is_estimate": True,
            "input_per_1m_tokens": 2.00,
            "output_per_1m_tokens": 10.00,
            "post_dec_1_input_per_1m_tokens": 4.00,
            "post_dec_1_output_per_1m_tokens": 20.00,
            "note": "Estimated promo $2/$10 through Nov 30, 2026; $4/$20 starting Dec 1, 2026. Higher cost than Hydrogen.",
        },
        "mock_contract_tested": True,
        "notes": "Flagship deep-reasoning model selectable at runtime for complex creative tasks.",
    },
    "gemini-3.8-flash": {
        "model_id": "gemini-3.8-flash",
        "display_name": "Gemini 3.8 Flash",
        "category": "text",
        "status": "fallback",
        "role": "fallback",
        "api_surface": "models.generate_content",
        "capabilities": ["reasoning", "structured_json", "fast_fallback"],
        "pricing_estimate": {
            "currency": "USD",
            "is_estimate": True,
            "input_per_1m_tokens": 0.30,
            "output_per_1m_tokens": 1.50,
            "note": "Estimated legacy/fallback text model pricing.",
        },
        "mock_contract_tested": True,
        "notes": "Existing fallback text model retained for compatibility.",
    },

    # --- Image Generation (Nano Banana Family via Interactions API) ---
    "gemini-nano-banana-2.1": {
        "model_id": "gemini-nano-banana-2.1",
        "display_name": "Nano Banana 2.1",
        "category": "image",
        "status": "stable",
        "role": "default",
        "api_surface": "interactions.create",
        "supported_image_sizes": ["1K", "2K", "4K"],
        "supported_aspect_ratios": list(NANO_BANANA_PANORAMIC_ASPECT_RATIOS),
        "capabilities": [
            "image_generation",
            "reference_image_blending",
            "panoramic_aspect_ratios",
            "1K",
            "2K",
            "4K",
        ],
        "pricing_estimate": {
            "currency": "USD",
            "is_estimate": True,
            "1K_per_image": 0.0336,
            "2K_per_image": 0.0504,
            "4K_per_image": 0.1134,
            "note": "Estimated per-image rate via Interactions API; 512 is NOT supported on Nano Banana 2.1.",
        },
        "mock_contract_tested": True,
        "notes": "Primary Nano Banana 2.1 model supporting 1K/2K/4K and 14 aspect ratios (including 1:4, 4:1, 1:8, 8:1).",
    },
    "gemini-3.1-flash-lite-image": {
        "model_id": "gemini-3.1-flash-lite-image",
        "display_name": "Nano Banana 2 Lite",
        "category": "image",
        "status": "stable",
        "role": "lite",
        "api_surface": "interactions.create",
        "supported_image_sizes": ["512", "1K"],
        "supported_aspect_ratios": list(NANO_BANANA_PANORAMIC_ASPECT_RATIOS),
        "capabilities": ["image_generation", "fast_draft", "512", "1K"],
        "pricing_estimate": {
            "currency": "USD",
            "is_estimate": True,
            "512_per_image": 0.015,
            "1K_per_image": 0.025,
            "note": "Estimated lite/draft image pricing.",
        },
        "mock_contract_tested": True,
        "notes": "Low-cost draft image generation supporting 512 and 1K output sizes.",
    },
    "gemini-3-pro-image": {
        "model_id": "gemini-3-pro-image",
        "display_name": "Nano Banana Pro",
        "category": "image",
        "status": "stable",
        "role": "pro",
        "api_surface": "interactions.create",
        "supported_image_sizes": ["1K", "2K", "4K"],
        "supported_aspect_ratios": list(STANDARD_IMAGE_ASPECT_RATIOS),
        "capabilities": ["image_generation", "high_fidelity", "1K", "2K", "4K"],
        "pricing_estimate": {
            "currency": "USD",
            "is_estimate": True,
            "1K_per_image": 0.134,
            "2K_per_image": 0.134,
            "4K_per_image": 0.240,
            "note": "Estimated Pro image pricing.",
        },
        "mock_contract_tested": True,
        "notes": "High-fidelity image model supporting 1K, 2K, and 4K.",
    },
    "gemini-3.1-flash-image": {
        "model_id": "gemini-3.1-flash-image",
        "display_name": "Gemini 3.1 Flash Image (Deprecated)",
        "category": "image",
        "status": "deprecated",
        "role": "deprecated",
        "replacement_model": "gemini-nano-banana-2.1",
        "api_surface": "models.generate_content",
        "supported_image_sizes": [],
        "supported_aspect_ratios": [],
        "capabilities": [],
        "pricing_estimate": {
            "currency": "USD",
            "is_estimate": True,
            "1K_per_image": 0.039,
            "note": "Deprecated; blocked from dispatch to prevent unexpected fallback billing.",
        },
        "mock_contract_tested": True,
        "notes": "Deprecated image model replaced by gemini-nano-banana-2.1.",
    },

    # --- Video Generation (Gemini Omni Flash via Interactions API) ---
    "gemini-omni-1.2-flash": {
        "model_id": "gemini-omni-1.2-flash",
        "display_name": "Gemini Omni 1.2 Flash",
        "category": "video",
        "status": "stable",
        "role": "default",
        "api_surface": "interactions.create",
        "supported_aspect_ratios": ["16:9", "9:16"],
        "supported_resolutions": ["360p", "720p", "1080p", "4k"],
        "supported_tasks": list(VIDEO_SUPPORTED_TASKS),
        "capabilities": [
            "text_to_video",
            "image_to_video",
            "reference_to_video_up_to_6",
            "edit",
            "extend",
            "upscale",
            "360p",
            "720p",
            "1080p",
            "4k",
        ],
        "pricing_estimate": {
            "currency": "USD",
            "is_estimate": True,
            "360p_per_second": 0.04,
            "720p_per_second": 0.07,
            "1080p_per_second": 0.12,
            "4k_per_second": 0.24,
            "note": "Estimated Omni 1.2 Flash per-second rate (~$0.07/s at 720p; ~30% cheaper than Omni 1.1).",
        },
        "mock_contract_tested": True,
        "notes": "Default Omni 1.2 Flash video model: 3-15s generation/edit, 3-10s extend, up to 6 reference images, 360p/720p/1080p/4k.",
    },
    "gemini-omni-1.1-flash": {
        "model_id": "gemini-omni-1.1-flash",
        "display_name": "Gemini Omni 1.1 Flash",
        "category": "video",
        "status": "fallback",
        "role": "fallback",
        "api_surface": "interactions.create",
        "supported_aspect_ratios": ["16:9", "9:16"],
        "supported_resolutions": ["360p", "720p", "1080p"],
        "supported_tasks": ["text_to_video", "image_to_video", "reference_to_video", "edit", "extend"],
        "capabilities": ["text_to_video", "image_to_video", "reference_to_video", "edit", "extend"],
        "pricing_estimate": {
            "currency": "USD",
            "is_estimate": True,
            "720p_per_second": 0.10,
            "1080p_per_second": 0.15,
            "note": "Estimated Omni 1.1 Flash rate; auto-routes to Omni 1.2 on Dec 3, 2026.",
        },
        "mock_contract_tested": True,
        "notes": "Explicit video fallback; scheduled to auto-route to Omni 1.2 on Dec 3, 2026.",
    },

    # --- Text-to-Speech ---
    "gemini-3.8-flash-tts": {
        "model_id": "gemini-3.8-flash-tts",
        "display_name": "Gemini 3.8 Flash TTS",
        "category": "tts",
        "status": "stable",
        "role": "default",
        "api_surface": "interactions.create",
        "capabilities": ["single_speaker_tts", "multi_speaker_tts", "speech_style_annotations"],
        "pricing_estimate": {
            "currency": "USD",
            "is_estimate": True,
            "per_clip_estimate": 0.01,
            "note": "Estimated per-clip voiceover cost.",
        },
        "mock_contract_tested": True,
        "notes": "Default character voiceover and multi-speaker dialogue model.",
    },
    "gemini-3.8-flash-lite-tts": {
        "model_id": "gemini-3.8-flash-lite-tts",
        "display_name": "Gemini 3.8 Flash Lite TTS",
        "category": "tts",
        "status": "stable",
        "role": "lite",
        "api_surface": "interactions.create",
        "capabilities": ["single_speaker_tts", "multi_speaker_tts", "fast_tts"],
        "pricing_estimate": {
            "currency": "USD",
            "is_estimate": True,
            "per_clip_estimate": 0.005,
            "note": "Estimated lite TTS clip cost.",
        },
        "mock_contract_tested": True,
        "notes": "Lightweight fast TTS model for draft lines.",
    },

    # --- Music Generation ---
    "lyria-3.5": {
        "model_id": "lyria-3.5",
        "display_name": "Lyria 3.5",
        "category": "music",
        "status": "stable",
        "role": "default",
        "api_surface": "interactions.create",
        "capabilities": ["full_track_music", "bgm", "multimodal_reference"],
        "pricing_estimate": {
            "currency": "USD",
            "is_estimate": True,
            "per_track_estimate": 0.08,
            "note": "Estimated full-track BGM generation cost.",
        },
        "mock_contract_tested": True,
        "notes": "Default BGM & soundtrack generation model.",
    },
    "lyria-3-clip-preview": {
        "model_id": "lyria-3-clip-preview",
        "display_name": "Lyria 3 Clip Preview",
        "category": "music",
        "status": "stable",
        "role": "lite",
        "api_surface": "interactions.create",
        "capabilities": ["short_clip_music", "stinger", "30s_preview"],
        "pricing_estimate": {
            "currency": "USD",
            "is_estimate": True,
            "per_clip_estimate": 0.05,
            "note": "Estimated 30s music clip cost.",
        },
        "mock_contract_tested": True,
        "notes": "30-second music clip preview and stinger model.",
    },
}


def get_model_info(model_id: str) -> Dict[str, Any]:
    """Returns catalog entry for `model_id`, or an explicit unknown descriptor."""
    entry = MODEL_CATALOG.get(model_id)
    if entry:
        return dict(entry)
    return {
        "model_id": model_id,
        "display_name": model_id,
        "category": "unknown",
        "status": "unknown",
        "role": "unknown",
        "api_surface": "unknown",
        "capabilities": [],
        "pricing_estimate": {"currency": "USD", "is_estimate": True, "note": "Unknown model"},
        "mock_contract_tested": False,
        "notes": "Unrecognized model ID; not present in verified catalog.",
    }


def read_live_verification_records(verification_file: Optional[Path] = None) -> Dict[str, Any]:
    """
    Loads manual live-API smoke-test verification results from disk if present.
    Never infers live verification merely from an API key being set.
    """
    target = verification_file or DEFAULT_LIVE_VERIFICATION_FILE
    if not target.exists():
        return {}
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {}
        results = data.get("results") or data.get("models") or {}
        if isinstance(results, dict):
            return results
        if isinstance(results, list):
            mapped: Dict[str, Any] = {}
            for item in results:
                if isinstance(item, dict) and item.get("model_id"):
                    mapped[str(item["model_id"])] = item
            return mapped
        return {}
    except Exception:
        return {}


def is_model_live_verified(
    model_id: str,
    verification_records: Optional[Dict[str, Any]] = None,
) -> tuple[bool, Optional[str]]:
    """
    Returns `(True, tested_at)` ONLY if a real manual live-API verification record exists with PASS/verified=True
    for that exact `model_id`. Otherwise strictly returns `(False, None)`.
    """
    records = verification_records if verification_records is not None else read_live_verification_records()
    rec = records.get(model_id)
    if not isinstance(rec, dict):
        return False, None
    status = str(rec.get("status", "")).strip().upper()
    if status == "PASS" or rec.get("verified") is True or rec.get("live_api_tested") is True:
        tested_at = rec.get("tested_at") or rec.get("timestamp")
        return True, str(tested_at) if tested_at else None
    return False, None


def estimate_image_cost_usd(model_id: str, image_size: str = "1K") -> float:
    size = str(image_size or "1K").upper()
    if model_id == "gemini-3-pro-image":
        return 0.240 if size == "4K" else 0.134
    if model_id == "gemini-3.1-flash-lite-image":
        return 0.015 if size in ("512", "0.5K") else 0.025
    # Default: Nano Banana 2.1
    if size == "4K":
        return 0.1134
    if size == "2K":
        return 0.0504
    return 0.0336


def estimate_video_cost_usd(
    model_id: str,
    duration_seconds: int = 5,
    resolution: str = "720p",
) -> float:
    secs = max(1, int(duration_seconds or 5))
    res = str(resolution or "720p").lower()
    if model_id == "gemini-omni-1.1-flash":
        rate = 0.15 if res in ("1080p", "4k") else 0.10
    else:
        # gemini-omni-1.2-flash rates (~$0.07/s at 720p)
        if res == "4k":
            rate = 0.24
        elif res == "1080p":
            rate = 0.12
        elif res == "360p":
            rate = 0.04
        else:
            rate = 0.07
    return round(rate * secs, 4)


def estimate_tts_cost_usd(model_id: str) -> float:
    return 0.005 if "lite" in str(model_id).lower() else 0.01


def estimate_music_cost_usd(model_id: str) -> float:
    return 0.05 if "clip" in str(model_id).lower() else 0.08


def build_model_catalog_payload(
    verification_file: Optional[Path] = None,
) -> Dict[str, Any]:
    """
    Constructs the full `/api/system/ai-status` model catalog and currently selected runtime configuration.
    """
    records = read_live_verification_records(verification_file=verification_file)

    enriched_catalog: Dict[str, Dict[str, Any]] = {}
    for model_id, info in MODEL_CATALOG.items():
        live_ok, tested_at = is_model_live_verified(model_id, records)
        entry = dict(info)
        entry["live_api_tested"] = live_ok
        entry["live_verified_at"] = tested_at
        enriched_catalog[model_id] = entry

    selected_roles = {
        "text_default": settings.gemini_model,
        "text_reasoning": getattr(settings, "gemini_reasoning_model", "gemini-4-carbon"),
        "text_fallback": getattr(settings, "gemini_fallback_model", "gemini-3.8-flash"),
        "image_default": settings.google_image_model,
        "image_lite": settings.google_image_lite_model,
        "image_pro": settings.google_image_pro_model,
        "video_default": settings.google_video_model,
        "video_fallback": getattr(settings, "google_video_fallback_model", "gemini-omni-1.1-flash"),
        "tts_default": settings.google_tts_model,
        "tts_lite": settings.google_tts_lite_model,
        "music_default": settings.google_music_model,
        "music_clip": settings.google_music_clip_model,
    }

    selected_details: Dict[str, Dict[str, Any]] = {}
    for role_key, model_id in selected_roles.items():
        base = enriched_catalog.get(model_id)
        if base is None:
            live_ok, tested_at = is_model_live_verified(model_id, records)
            base = get_model_info(model_id)
            base["live_api_tested"] = live_ok
            base["live_verified_at"] = tested_at
        selected_details[role_key] = base

    return {
        "catalog": enriched_catalog,
        "selected": selected_roles,
        "selected_details": selected_details,
        "any_live_api_tested": any(v.get("live_api_tested") for v in enriched_catalog.values()),
        "pricing_note": (
            "All USD figures in pricing_estimate and local budgets are estimated local projections "
            "as of Nov 13, 2026, not authoritative provider invoices."
        ),
    }
