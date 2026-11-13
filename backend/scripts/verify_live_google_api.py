#!/usr/bin/env python3
"""
AI FILM STUDIO — Official Google Cloud AI Live Smoke-Test Script.
Verified baseline: November 13, 2026.

This script executes live smoke tests against official Google Cloud AI endpoints:
- Gemini 4 Hydrogen (Reasoning): `gemini-4-hydrogen`
- Nano Banana 2.1 (Image Generation): `gemini-nano-banana-2.1` via Interactions API
- Gemini Omni 1.2 Flash (Video Generation): `gemini-omni-1.2-flash` via Interactions API
- Gemini TTS (Speech Generation): `gemini-3.8-flash-tts`
- Lyria 3.5 (Music Generation): `lyria-3.5` via Interactions API

SAFETY NOTICE:
- Running this script consumes real Google Cloud API quota.
- This script is NEVER run during normal automated test runs.
- It requires an explicit user confirmation prompt before calling live endpoints.
"""

import os
import sys
import time
import json
import argparse
from pathlib import Path
from datetime import datetime, timezone

# Add project root to sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.config import settings

ESTIMATED_COSTS = {
    "Gemini 4 Hydrogen (Text Reasoning)": 0.0005,
    "Nano Banana 2.1 (Image Generation)": 0.0336,
    "Gemini Omni 1.2 Flash (Video Generation)": 0.35,
    "Gemini TTS (Speech Generation)": 0.01,
    "Lyria 3.5 (Music Generation)": 0.08
}


def print_banner():
    print("=" * 72)
    print("  AI FILM STUDIO — GOOGLE CLOUD AI LIVE API SMOKE TEST (Nov 13, 2026)")
    print("=" * 72)


def main():
    parser = argparse.ArgumentParser(description="Live smoke test for Google Cloud AI endpoints.")
    parser.add_argument("--force", action="store_true", help="Bypass interactive confirmation prompt.")
    parser.add_argument("--skip-video", action="store_true", help=r"Skip video generation (saves ~$0.15).")
    parser.add_argument("--skip-music", action="store_true", help=r"Skip music generation (saves ~$0.05).")
    args = parser.parse_args()

    print_banner()

    api_key = settings.gemini_api_key or os.getenv("GEMINI_API_KEY", "")
    if not api_key:
        print("\n[BLOCKED] GEMINI_API_KEY is not configured.")
        print("Please set your API key in backend/.env or via environment variable:")
        print("  export GEMINI_API_KEY=\"AIzaSy...\"")
        print("\nNo live API calls were performed. Quota spent: $0.00.")
        sys.exit(1)

    masked_key = f"{api_key[:4]}...{api_key[-4:]}" if len(api_key) >= 8 else "(configured)"
    print(f"\nConfigured API Key: {masked_key}")
    print("\nPlanned operations & estimated maximum costs:")
    total_est = 0.0
    for op, cost in ESTIMATED_COSTS.items():
        skip = (args.skip_video and "Video" in op) or (args.skip_music and "Music" in op)
        flag = " [SKIPPED BY USER FLAG]" if skip else ""
        print(f"  * {op:<45}: ~${cost:.4f}{flag}")
        if not skip:
            total_est += cost
    print(f"  {'TOTAL ESTIMATED COST':<45}: ~${total_est:.4f}")

    if not args.force:
        print("\nWARNING: This will make real network calls to Google Cloud and bill your account.")
        confirm = input("\nDo you want to proceed with live Google API calls? (y/N): ").strip().lower()
        if confirm != "y":
            print("\nAborted by user. No live API calls performed. Quota spent: $0.00.")
            sys.exit(0)

    try:
        from google import genai
        from google.genai import types
    except ImportError:
        print("\n[ERROR] google-genai SDK not installed in environment. Run 'pip install google-genai'.")
        sys.exit(1)

    client = genai.Client(api_key=api_key)
    results = []

    # 1. Gemini 3.8 Flash (Text Reasoning)
    print("\n[1/5] Testing Gemini 3.8 Flash text reasoning...")
    t0 = time.time()
    try:
        resp = client.models.generate_content(
            model=settings.gemini_model,
            contents="State in one concise sentence the role of a Hollywood Screenwriter.",
            config=types.GenerateContentConfig(temperature=0.2, max_output_tokens=60)
        )
        latency = time.time() - t0
        text_out = resp.text.strip() if resp.text else "(no text returned)"
        tokens = getattr(resp.usage_metadata, "total_token_count", None) if hasattr(resp, "usage_metadata") else None
        print(f"  -> SUCCESS ({latency:.2f}s, tokens={tokens}): \"{text_out[:80]}...\"")
        results.append({
            "service": "Reasoning (Gemini 3.8 Flash)",
            "model": settings.gemini_model,
            "status": "PASS",
            "latency_seconds": round(latency, 2),
            "output_preview": text_out[:100],
            "tokens": tokens
        })
    except Exception as e:
        print(f"  -> FAILED: {e}")
        results.append({
            "service": "Reasoning (Gemini 3.8 Flash)",
            "model": settings.gemini_model,
            "status": "FAIL",
            "error": str(e)
        })

    # 2. Nano Banana 2.1 (Image Generation via Interactions API)
    print(f"\n[2/5] Testing Nano Banana 2.1 image generation ({settings.google_image_model})...")
    t0 = time.time()
    try:
        img_resp = client.interactions.create(
            model=settings.google_image_model,
            input="A minimalist cinematic film clapboard in volumetric blue light, 16:9 widescreen.",
            response_format={"type": "image", "aspect_ratio": "16:9", "image_size": "1K"}
        )
        latency = time.time() - t0
        out_img = getattr(img_resp, "output_image", None)
        img_data = getattr(out_img, "data", None) if out_img else None
        if img_data:
            raw_len = len(img_data)
            print(f"  -> SUCCESS ({latency:.2f}s, payload_len={raw_len})")
            results.append({
                "service": "Image Generation (Nano Banana 2.1)",
                "model": settings.google_image_model,
                "status": "PASS",
                "latency_seconds": round(latency, 2),
                "bytes_received": raw_len
            })
        else:
            print("  -> FAILED: No output_image.data in response")
            results.append({
                "service": "Image Generation (Nano Banana 2.1)",
                "model": settings.google_image_model,
                "status": "FAIL",
                "error": "No output image returned"
            })
    except Exception as e:
        print(f"  -> FAILED: {e}")
        results.append({
            "service": "Image Generation (Nano Banana 2.1)",
            "model": settings.google_image_model,
            "status": "FAIL",
            "error": str(e)
        })

    # 3. Gemini Omni 1.2 Flash (Video Generation via Interactions API)
    if args.skip_video:
        print("\n[3/5] Skipping Gemini Omni 1.2 Flash video generation as requested.")
        results.append({
            "service": "Video Generation (Gemini Omni 1.2 Flash)",
            "model": settings.google_video_model,
            "status": "SKIPPED",
            "note": "Skipped by user flag"
        })
    else:
        print(f"\n[3/5] Testing Gemini Omni 1.2 Flash video generation ({settings.google_video_model}) via Interactions API...")
        t0 = time.time()
        try:
            interaction = client.interactions.create(
                model=settings.google_video_model,
                input="Slow cinematic pan across a futuristic cityscape at dusk, 4k photoreal.",
                background=True,
                generation_config={"video_config": {"task": "text_to_video"}},
                response_format={"type": "video", "aspect_ratio": "16:9", "resolution": "720p", "duration": "5s"}
            )
            op_id = getattr(interaction, "id", None)
            latency = time.time() - t0
            print(f"  -> SUCCESS: Created interaction ID {op_id} ({latency:.2f}s)")
            results.append({
                "service": "Video Generation (Gemini Omni 1.2 Flash)",
                "model": settings.google_video_model,
                "status": "PASS",
                "interaction_id": op_id,
                "latency_seconds": round(latency, 2)
            })
        except Exception as e:
            print(f"  -> FAILED: {e}")
            results.append({
                "service": "Video Generation (Gemini Omni Flash 1.1)",
                "model": settings.google_video_model,
                "status": "FAIL",
                "error": str(e)
            })

    # 4. Gemini TTS (Speech Generation via Interactions API)
    print(f"\n[4/5] Testing Gemini TTS speech generation ({settings.google_tts_model}) via Interactions API...")
    t0 = time.time()
    try:
        tts_text = "AI Film Studio system operational. Ready for scene production."
        tts_interaction = client.interactions.create(
            model=settings.google_tts_model,
            input=[
                {
                    "type": "user_input",
                    "content": [
                        {
                            "type": "text",
                            "text": tts_text,
                            "annotations": [
                                {
                                    "type": "speech_metadata",
                                    "speaker": "Puck",
                                    "style": "natural dialogue cadence, articulate delivery"
                                }
                            ]
                        }
                    ]
                }
            ],
            generation_config={"speech_config": [{"voice": "Puck"}]},
            response_format={"type": "audio"}
        )
        latency = time.time() - t0
        tts_op_id = getattr(tts_interaction, "id", None)
        print(f"  -> SUCCESS: Created TTS interaction ID {tts_op_id} ({latency:.2f}s)")
        results.append({
            "service": "TTS (Gemini TTS)",
            "model": settings.google_tts_model,
            "status": "PASS",
            "interaction_id": tts_op_id,
            "latency_seconds": round(latency, 2)
        })
    except Exception as e:
        print(f"  -> FAILED: {e}")
        results.append({
            "service": "TTS (Gemini TTS)",
            "model": settings.google_tts_model,
            "status": "FAIL",
            "error": str(e)
        })

    # 5. Lyria 3.5 (Music Generation via Interactions API)
    if args.skip_music:
        print("\n[5/5] Skipping Lyria music generation as requested.")
        results.append({
            "service": "Music Generation (Lyria 3.5)",
            "model": settings.google_music_model,
            "status": "SKIPPED",
            "note": "Skipped by user flag"
        })
    else:
        print(f"\n[5/5] Testing Lyria 3.5 music generation ({settings.google_music_model}) via Interactions API...")
        t0 = time.time()
        try:
            interaction = client.interactions.create(
                model=settings.google_music_model,
                input="Cinematic ambient synthwave score, 110 BPM, instrumental.",
                background=True,
                response_format={"type": "audio", "mime_type": "audio/mp3"}
            )
            op_id = getattr(interaction, "id", None)
            latency = time.time() - t0
            print(f"  -> SUCCESS: Created interaction ID {op_id} ({latency:.2f}s)")
            results.append({
                "service": "Music Generation (Lyria 3.5)",
                "model": settings.google_music_model,
                "status": "PASS",
                "interaction_id": op_id,
                "latency_seconds": round(latency, 2)
            })
        except Exception as e:
            print(f"  -> FAILED: {e}")
            results.append({
                "service": "Music Generation (Lyria 3.5)",
                "model": settings.google_music_model,
                "status": "FAIL",
                "error": str(e)
            })

    # Save log report
    log_dir = Path("backend/data")
    log_dir.mkdir(parents=True, exist_ok=True)
    report_file = log_dir / "live_api_verification.json"
    report_data = {
        "verified_at": datetime.now(timezone.utc).isoformat(),
        "api_key_masked": masked_key,
        "results": results
    }
    report_file.write_text(json.dumps(report_data, indent=2))
    print(f"\nSaved live verification report to: {report_file}")
    print("=" * 72)


if __name__ == "__main__":
    main()
