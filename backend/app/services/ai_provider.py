"""
Unified AI Provider Abstraction Module.
Supports:
1. Ollama (Default local AI, http://127.0.0.1:11434)
2. Google Gemini (Optional cloud provider)
3. Offline Template Engine (Deterministic creative fallback)

Enforces:
- Schema validation via Pydantic
- Automatic repair & limited single-step self-correction retry
- Explicit attribution (Ollama vs Gemini vs Offline Template)
- Zero silent faking of AI output
"""

import os
import json
import time
import random
import logging
import urllib.request
import urllib.error
from abc import ABC, abstractmethod
from typing import Dict, Any, Optional, Type, TypeVar, List, Tuple
from pydantic import BaseModel

from backend.app.config import settings
from backend.app.services.structured_output import (
    parse_and_validate_structured_json,
    ScreenplaySceneOutput,
    SceneBreakdownOutput,
    CharacterProfileOutput,
    CinematographyOutput,
    ContinuityAuditOutput,
    AssemblyCutOutput
)
from backend.app.services.offline_ai_engine import offline_ai

logger = logging.getLogger("ai_provider")

T = TypeVar("T", bound=BaseModel)


class AIProviderError(Exception):
    """Base exception for AI provider operations."""
    pass


class AIConnectionError(AIProviderError):
    """Raised when an AI service endpoint is unreachable."""
    pass


class AIModelNotFoundError(AIProviderError):
    """Raised when the specified model is not installed or available."""
    pass


class AIStructuredOutputError(AIProviderError):
    """Raised when model output fails schema validation after repair and retries."""
    pass


class BaseAIProvider(ABC):
    @property
    @abstractmethod
    def provider_name(self) -> str:
        pass

    @abstractmethod
    def generate_text(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        **kwargs
    ) -> Tuple[str, Dict[str, Any]]:
        """Generates plain text and returns (response_text, usage_metadata)."""
        pass

    @abstractmethod
    def generate_structured(
        self,
        prompt: str,
        schema: Type[T],
        system_prompt: Optional[str] = None,
        **kwargs
    ) -> Tuple[T, Dict[str, Any]]:
        """Generates structured data validated against schema and returns (model_instance, usage_metadata)."""
        pass

    @abstractmethod
    def health_check(self) -> Dict[str, Any]:
        """Performs a health check and returns provider availability status."""
        pass

    @abstractmethod
    def get_available_models(self) -> List[str]:
        """Returns list of models available from this provider."""
        pass


class OllamaProvider(BaseAIProvider):
    def __init__(self, host: Optional[str] = None, model: Optional[str] = None, timeout: Optional[int] = None):
        self.host = (host or settings.ollama_host).rstrip("/")
        self.model = model or settings.ollama_model
        self.timeout = timeout or settings.ollama_timeout_seconds

    @property
    def provider_name(self) -> str:
        return "ollama"

    def health_check(self) -> Dict[str, Any]:
        url = f"{self.host}/api/tags"
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                models = [m.get("name", "") for m in data.get("models", [])]
                model_present = any(self.model in m for m in models)
                return {
                    "provider": "ollama",
                    "status": "online",
                    "host": self.host,
                    "configured_model": self.model,
                    "model_available": model_present,
                    "available_models": models
                }
        except urllib.error.URLError as e:
            return {
                "provider": "ollama",
                "status": "offline",
                "host": self.host,
                "configured_model": self.model,
                "model_available": False,
                "error": f"Cannot connect to Ollama at {self.host}: {e.reason if hasattr(e, 'reason') else e}"
            }
        except Exception as e:
            return {
                "provider": "ollama",
                "status": "offline",
                "host": self.host,
                "configured_model": self.model,
                "model_available": False,
                "error": str(e)
            }

    def get_available_models(self) -> List[str]:
        status = self.health_check()
        return status.get("available_models", [])

    def _call_ollama_chat(
        self,
        messages: List[Dict[str, str]],
        json_format: bool = False
    ) -> Tuple[str, Dict[str, Any]]:
        url = f"{self.host}/api/chat"
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "options": {
                "temperature": 0.7,
                "num_ctx": 4096
            }
        }
        if json_format:
            payload["format"] = json_format if isinstance(json_format, (dict, list)) else "json"

        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST"
        )

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                res_data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.URLError as e:
            err_reason = str(e.reason if hasattr(e, "reason") else e)
            if "Connection refused" in err_reason or "Cannot assign requested address" in err_reason:
                raise AIConnectionError(
                    f"Local Ollama server is not running at {self.host}. Please start it with 'ollama serve'."
                ) from e
            raise AIConnectionError(f"Ollama request failed: {err_reason}") from e
        except TimeoutError as e:
            raise AIConnectionError(
                f"Ollama request timed out after {self.timeout}s for model '{self.model}'."
            ) from e

        if "error" in res_data:
            err = res_data["error"]
            if "not found" in err.lower():
                raise AIModelNotFoundError(
                    f"Ollama model '{self.model}' was not found. Please run 'ollama pull {self.model}'."
                )
            raise AIProviderError(f"Ollama returned error: {err}")

        content = res_data.get("message", {}).get("content", "")
        prompt_tokens = res_data.get("prompt_eval_count", 0)
        eval_tokens = res_data.get("eval_count", 0)

        meta = {
            "provider": "ollama",
            "model": self.model,
            "input_tokens": prompt_tokens,
            "output_tokens": eval_tokens,
            "total_tokens": prompt_tokens + eval_tokens,
            "usage_known": True,
            "is_fallback": False,
            "engine_label": f"Ollama ({self.model})"
        }
        return content, meta

    def generate_text(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        **kwargs
    ) -> Tuple[str, Dict[str, Any]]:
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        return self._call_ollama_chat(messages, json_format=False)

    def generate_structured(
        self,
        prompt: str,
        schema: Type[T],
        system_prompt: Optional[str] = None,
        **kwargs
    ) -> Tuple[T, Dict[str, Any]]:
        # Instruct model on JSON schema
        schema_json = json.dumps(schema.model_json_schema(), indent=2)
        full_system = (
            (system_prompt or "You are an expert Hollywood cinematic AI assistant.")
            + f"\nCRITICAL: You must return ONLY a single valid JSON object strictly complying with this JSON Schema:\n{schema_json}"
        )
        messages = [
            {"role": "system", "content": full_system},
            {"role": "user", "content": prompt}
        ]

        schema_def = schema.model_json_schema()
        # First attempt
        content, meta = self._call_ollama_chat(messages, json_format=schema_def)
        validated, err = parse_and_validate_structured_json(content, schema)
        if validated:
            return validated, meta

        # 1-step self-correction retry
        logger.warning(f"Ollama structured validation failed: {err}. Retrying with correction prompt...")
        correction_messages = list(messages)
        correction_messages.append({"role": "assistant", "content": content})
        correction_messages.append({
            "role": "user",
            "content": f"Your previous response had JSON/schema validation errors:\n{err}\nPlease output ONLY the corrected, valid JSON conforming to the requested schema."
        })

        retry_content, retry_meta = self._call_ollama_chat(correction_messages, json_format=schema_def)
        retry_meta["input_tokens"] += meta["input_tokens"]
        retry_meta["output_tokens"] += meta["output_tokens"]
        retry_meta["total_tokens"] = retry_meta["input_tokens"] + retry_meta["output_tokens"]

        validated_retry, retry_err = parse_and_validate_structured_json(retry_content, schema)
        if validated_retry:
            return validated_retry, retry_meta

        raise AIStructuredOutputError(
            f"Ollama failed to produce valid structured output for {schema.__name__}: {retry_err}"
        )


class GeminiProvider(BaseAIProvider):
    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None):
        self.api_key = api_key or settings.gemini_api_key or os.getenv("GEMINI_API_KEY", "")
        self.model = model or settings.gemini_model
        self.max_retries = getattr(settings, "gemini_max_retries", 3)
        self._client = None
        if self.api_key:
            try:
                from google import genai
                self._client = genai.Client(api_key=self.api_key)
            except Exception as e:
                logger.warning(f"Failed to initialize google.genai: {e}")

    @property
    def provider_name(self) -> str:
        return "gemini"

    def health_check(self) -> Dict[str, Any]:
        if not self.api_key:
            return {
                "provider": "gemini",
                "status": "not_configured",
                "configured_model": self.model,
                "note": "GEMINI_API_KEY is not set. Add your API key in settings to enable Google Cloud AI."
            }
        masked_key = f"{self.api_key[:4]}...{self.api_key[-4:]}" if len(self.api_key) >= 8 else "(configured)"
        return {
            "provider": "gemini",
            "status": "configured" if self._client else "init_failed",
            "configured_model": self.model,
            "has_client": bool(self._client),
            "masked_key": masked_key
        }

    def get_available_models(self) -> List[str]:
        return [
            "gemini-3.8-flash",
            "gemini-3.1-flash-image",
            "gemini-3-pro-image",
            "gemini-3.1-flash-lite-image",
            "gemini-omni-1.1-flash",
            "gemini-3.8-flash-tts",
            "gemini-3.8-flash-lite-tts",
            "lyria-3.5",
            "lyria-3-clip-preview"
        ]

    def _execute_with_retry(self, fn, *args, **kwargs):
        """
        Executes a call against Google GenAI with exponential backoff on transient errors.
        Handles:
        - Rate limits (HTTP 429)
        - Service Unavailable (HTTP 503)
        - Connection timeouts
        Never retries indefinitely (max_retries = 3).
        Non-transient errors (auth error, invalid argument, quota exhaustion) raise immediately.
        """
        attempt = 0
        while attempt <= self.max_retries:
            try:
                return fn(*args, **kwargs)
            except Exception as e:
                err_str = str(e).lower()
                is_transient = any(term in err_str for term in ["429", "rate limit", "503", "unavailable", "timeout", "resource exhausted", "overloaded"])
                
                # If non-transient or exhausted retries, raise immediately
                if not is_transient or attempt >= self.max_retries:
                    if "api_key" in err_str or "unauthenticated" in err_str or "403" in err_str:
                        raise AIProviderError(f"Google Gemini authentication failed: {e}")
                    if "404" in err_str or "not found" in err_str:
                        raise AIModelNotFoundError(f"Google Gemini model '{self.model}' not found: {e}")
                    raise AIProviderError(f"Google Gemini API error: {e}")

                attempt += 1
                backoff = (1.5 ** attempt) + (random.uniform(0.1, 0.4))
                logger.warning(f"Google Gemini transient error ({e}). Retrying attempt {attempt}/{self.max_retries} in {backoff:.2f}s...")
                time.sleep(backoff)

    def generate_text(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        **kwargs
    ) -> Tuple[str, Dict[str, Any]]:
        if not self._client:
            raise AIProviderError("Google Gemini API is not configured. Provide GEMINI_API_KEY.")

        from google.genai import types
        sys_inst = system_prompt or "You are an expert Hollywood cinematic AI assistant."
        config = types.GenerateContentConfig(
            system_instruction=sys_inst,
            temperature=kwargs.get("temperature", 0.7)
        )

        def _call():
            return self._client.models.generate_content(
                model=self.model,
                contents=prompt,
                config=config
            )

        response = self._execute_with_retry(_call)
        content = response.text or ""

        # Extract tokens
        prompt_tokens = getattr(response.usage_metadata, "prompt_token_count", None) if hasattr(response, "usage_metadata") else None
        cand_tokens = getattr(response.usage_metadata, "candidates_token_count", None) if hasattr(response, "usage_metadata") else None
        tot_tokens = (prompt_tokens or 0) + (cand_tokens or 0)

        meta = {
            "provider": "gemini",
            "model": self.model,
            "input_tokens": prompt_tokens,
            "output_tokens": cand_tokens,
            "total_tokens": tot_tokens,
            "usage_known": prompt_tokens is not None,
            "is_fallback": False,
            "engine_label": f"Google Gemini ({self.model})",
            "generation_status": "completed"
        }
        return content, meta

    def generate_structured(
        self,
        prompt: str,
        schema: Type[T],
        system_prompt: Optional[str] = None,
        **kwargs
    ) -> Tuple[T, Dict[str, Any]]:
        if not self._client:
            raise AIProviderError("Google Gemini API is not configured. Provide GEMINI_API_KEY.")

        from google.genai import types
        sys_inst = (system_prompt or "You are an expert Hollywood screenwriter and director AI.")
        config = types.GenerateContentConfig(
            system_instruction=sys_inst,
            response_mime_type="application/json",
            response_schema=schema,
            temperature=kwargs.get("temperature", 0.7)
        )

        def _call():
            return self._client.models.generate_content(
                model=self.model,
                contents=prompt,
                config=config
            )

        response = self._execute_with_retry(_call)
        raw_text = response.text or ""

        prompt_tokens = getattr(response.usage_metadata, "prompt_token_count", None) if hasattr(response, "usage_metadata") else None
        cand_tokens = getattr(response.usage_metadata, "candidates_token_count", None) if hasattr(response, "usage_metadata") else None
        tot_tokens = (prompt_tokens or 0) + (cand_tokens or 0)

        meta = {
            "provider": "gemini",
            "model": self.model,
            "input_tokens": prompt_tokens,
            "output_tokens": cand_tokens,
            "total_tokens": tot_tokens,
            "usage_known": prompt_tokens is not None,
            "is_fallback": False,
            "engine_label": f"Google Gemini ({self.model})",
            "generation_status": "completed"
        }

        validated, err = parse_and_validate_structured_json(raw_text, schema)
        if validated:
            return validated, meta

        # 1-step self-correction retry
        logger.warning(f"Gemini structured output validation failed: {err}. Retrying with correction prompt...")
        correction_prompt = (
            f"Original prompt:\n{prompt}\n\n"
            f"Your previous response had JSON/schema validation errors:\n{err}\n"
            "Please output ONLY the corrected, valid JSON conforming to the requested schema."
        )

        def _call_retry():
            return self._client.models.generate_content(
                model=self.model,
                contents=correction_prompt,
                config=config
            )

        retry_resp = self._execute_with_retry(_call_retry)
        retry_text = retry_resp.text or ""
        validated_retry, retry_err = parse_and_validate_structured_json(retry_text, schema)
        if validated_retry:
            return validated_retry, meta

        raise AIStructuredOutputError(f"Gemini output failed schema validation for {schema.__name__}: {retry_err}")


class OfflineTemplateProvider(BaseAIProvider):
    """
    Deterministic cinematic template generator.
    Always explicitly marked with is_fallback: True. Never fakes real AI generation.
    """
    @property
    def provider_name(self) -> str:
        return "offline_template"

    def health_check(self) -> Dict[str, Any]:
        return {
            "provider": "offline_template",
            "status": "online",
            "configured_model": "offline-studio-engine",
            "note": "Built-in procedural cinematic engine is always available."
        }

    def get_available_models(self) -> List[str]:
        return ["offline-studio-engine"]

    def _meta(self) -> Dict[str, Any]:
        return {
            "provider": "offline_template",
            "model": "offline-studio-engine",
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "usage_known": True,
            "is_fallback": True,
            "engine_label": "Offline Studio Engine (Template)"
        }

    def generate_text(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        **kwargs
    ) -> Tuple[str, Dict[str, Any]]:
        return f"[OFFLINE TEMPLATE OUTPUT]\nPrompt: {prompt}", self._meta()

    def generate_structured(
        self,
        prompt: str,
        schema: Type[T],
        system_prompt: Optional[str] = None,
        **kwargs
    ) -> Tuple[T, Dict[str, Any]]:
        # Route to appropriate offline template generator based on schema
        meta = self._meta()

        if schema == ScreenplaySceneOutput:
            res = offline_ai.generate_screenplay_scene(
                project_title=kwargs.get("project_title", "Untitled Film"),
                genre=kwargs.get("genre", "Sci-Fi"),
                scene_number=kwargs.get("scene_number", 1),
                scene_prompt=prompt,
                characters=kwargs.get("characters"),
                location=kwargs.get("location")
            )
            item = ScreenplaySceneOutput(
                title=res["title"],
                slugline=res["slugline"],
                summary=res["summary"],
                script_content=res["script_content"],
                estimated_duration=res.get("estimated_duration", 15)
            )
            return item, meta  # type: ignore

        elif schema == SceneBreakdownOutput:
            raw_shots = offline_ai.generate_shot_list(
                scene_slugline=kwargs.get("scene_slugline", "INT. STUDIO - DAY"),
                scene_summary=kwargs.get("scene_summary", prompt),
                genre=kwargs.get("genre", "Sci-Fi"),
                characters=kwargs.get("characters"),
                visual_style=kwargs.get("visual_style"),
                count=kwargs.get("count", 4)
            )
            shots = []
            for s in raw_shots:
                shots.append(ShotBreakdownItem(
                    shot_number=s["shot_number"],
                    camera_angle=s["camera_angle"],
                    camera_movement=s["camera_movement"],
                    duration_seconds=float(s["duration_seconds"]),
                    visual_prompt=s["video_prompt"],
                    negative_prompt=s.get("negative_prompt", "blurry, low quality"),
                    lighting=s.get("lighting", "Cinematic lighting"),
                    composition=s.get("composition", "Rule of thirds"),
                    subject="Protagonist",
                    action=s.get("visual_description", "")
                ))
            item = SceneBreakdownOutput(
                pacing_style="Balanced Narrative",
                director_recommendation="Generated via built-in Offline Studio Engine template.",
                shots=shots
            )
            return item, meta  # type: ignore

        elif schema == CharacterProfileOutput:
            # Procedural character generation based on prompt/genre
            genre = kwargs.get("genre", "Sci-Fi")
            name = kwargs.get("name_hint", "KAIEN CROSS")
            role = kwargs.get("role_hint", "Protagonist")
            item = CharacterProfileOutput(
                name=name,
                role=role,
                age="32",
                facial_characteristics=f"Sharp cinematic jawline, intense focused gaze, styled for {genre}",
                clothing=f"Signature wardrobe suited for {genre} cinematic world, high-detail textures",
                personality="Determined, introspective, driven by internal conflict",
                voice_profile="Calm, measured, resonant cinematic tone",
                visual_prompt_tags=f"{name}, {role}, {genre} visual style, cinematic 35mm film still"
            )
            return item, meta  # type: ignore

        elif schema == CinematographyOutput:
            item = CinematographyOutput(
                camera="Arri Alexa LF",
                lens="35mm anamorphic prime lens",
                lighting="Volumetric chiaroscuro lighting",
                framing="2.39:1 widescreen",
                composition="Rule of thirds with deep perspective",
                color_palette="High dynamic range cinematic palette",
                visual_style="35mm film grain, 8k resolution, photoreal",
                video_prompt=prompt,
                negative_prompt="blurry, distorted, low quality, watermark"
            )
            return item, meta  # type: ignore

        elif schema == ContinuityAuditOutput:
            shots_list = kwargs.get("shots", [])
            chars_list = kwargs.get("characters", [])
            raw = offline_ai.analyze_continuity(shots=shots_list, characters=chars_list)
            item = ContinuityAuditOutput(
                status=raw.get("status", "PASS"),
                score=raw.get("score", 100),
                issues=raw.get("issues", []),
                suggestions=raw.get("suggestions", [])
            )
            return item, meta  # type: ignore

        elif schema == AssemblyCutOutput:
            shots_list = kwargs.get("shots", [])
            raw = offline_ai.generate_assembly_cut(shots=shots_list) if hasattr(offline_ai, "generate_assembly_cut") else None
            # Standard assembly calculation
            clips = []
            acc = 0.0
            for idx, s in enumerate(shots_list):
                d = float(s.get("duration_seconds", 4.0))
                clips.append(AssemblyClipItem(
                    shot_id=s.get("id"),
                    shot_code=s.get("shot_code", f"SH{idx+1:02d}"),
                    order_index=idx + 1,
                    start_time_seconds=round(acc, 2),
                    duration_seconds=round(d, 2),
                    end_time_seconds=round(acc + d, 2),
                    transition="fade_in" if idx == 0 else "hard_cut",
                    camera_angle=s.get("camera_angle"),
                    video_asset_id=s.get("video_asset_id")
                ))
                acc += d

            item = AssemblyCutOutput(
                total_timeline_duration_sec=round(acc, 2),
                clip_count=len(clips),
                rhythm_assessment="Balanced narrative assembly rhythm.",
                clips=clips
            )
            return item, meta  # type: ignore

        raise AIProviderError(f"Offline template provider does not support schema {schema.__name__}")


def get_ai_provider(provider_override: Optional[str] = None) -> BaseAIProvider:
    """
    Factory function returning the configured AI provider.
    Priority:
    1. Explicit provider_override if given.
    2. settings.ai_provider ("gemini", "ollama", "offline_template").
    Default is "gemini" (Google Cloud AI First).
    If "gemini" is selected but no GEMINI_API_KEY is configured:
    - If settings.ollama_enabled is True, check if Ollama is online.
    - Otherwise, falls back to OfflineTemplateProvider with clear is_fallback attribution.
    """
    target = (provider_override or settings.ai_provider or "gemini").lower().strip()

    if target == "gemini":
        gemini = GeminiProvider()
        if gemini.api_key:
            return gemini
        if getattr(settings, "ollama_enabled", False):
            ollama = OllamaProvider()
            if ollama.health_check().get("status") == "online":
                return ollama
        return OfflineTemplateProvider()
    elif target in ("offline", "offline_template", "template"):
        return OfflineTemplateProvider()
    elif target == "ollama":
        return OllamaProvider()
    else:
        # Default fallback to Gemini
        return GeminiProvider()
