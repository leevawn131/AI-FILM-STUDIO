"""
Gemini Service with seamless local Offline Studio Engine fallback.
Provides access to Google Gemini when configured, and falls back to
local offline creative filmmaking generation when offline or no API key is set.
"""

import logging
import json
import os
from typing import List, Dict, Any, Optional
from backend.app.config import settings
from backend.app.services.offline_ai_engine import offline_ai

logger = logging.getLogger("gemini_service")

class GeminiService:
    def __init__(self):
        self.api_key = settings.gemini_api_key or os.getenv("GEMINI_API_KEY", "")
        self.model = settings.gemini_model
        self._client = None
        
        if self.api_key:
            try:
                from google import genai
                self._client = genai.Client(api_key=self.api_key)
                logger.info(f"Gemini client initialized with model {self.model}")
            except Exception as e:
                logger.warning(f"Could not initialize google-genai client: {e}. Falling back to Offline Studio Engine.")

    @property
    def has_gemini(self) -> bool:
        return bool(self._client and self.api_key)

    def generate_screenplay(
        self,
        project_title: str,
        genre: str,
        scene_number: int,
        prompt: Optional[str] = None,
        characters: Optional[List[Dict[str, str]]] = None,
        location: Optional[Dict[str, str]] = None
    ) -> Dict[str, Any]:
        """Generates a screenplay scene using Gemini if available, or Offline Studio Engine."""
        if self.has_gemini:
            try:
                char_summary = ", ".join([f"{c.get('name')} ({c.get('role', '')})" for c in (characters or [])])
                loc_summary = location.get("name", "Unknown Set") if location else "Interior Studio"
                
                sys_prompt = (
                    "You are a master Hollywood Screenwriter Agent. Write a formatted screenplay scene in Fountain/standard screenplay format. "
                    "Return a JSON object with keys: title, slugline, summary, script_content, estimated_duration."
                )
                user_msg = (
                    f"Project: {project_title}\nGenre: {genre}\nScene #{scene_number}\n"
                    f"Characters: {char_summary}\nLocation: {loc_summary}\nPrompt: {prompt or 'An intense turning point'}"
                )
                
                response = self._client.models.generate_content(
                    model=self.model,
                    contents=f"{sys_prompt}\n\n{user_msg}",
                    config={"response_mime_type": "application/json"}
                )
                data = json.loads(response.text)
                return data
            except Exception as e:
                logger.warning(f"Gemini API call failed ({e}). Using Offline Studio Engine.")

        # Fallback to local offline engine
        return offline_ai.generate_screenplay_scene(
            project_title=project_title,
            genre=genre,
            scene_number=scene_number,
            scene_prompt=prompt,
            characters=characters,
            location=location
        )

    def generate_shots(
        self,
        scene_slugline: str,
        scene_summary: str,
        genre: str,
        characters: Optional[List[Dict[str, str]]] = None,
        visual_style: Optional[str] = None,
        count: int = 4
    ) -> List[Dict[str, Any]]:
        """Generates storyboard shots and Flow video prompts."""
        if self.has_gemini:
            try:
                sys_prompt = (
                    "You are a master Cinematographer and Director Agent. Break this scene down into cinematic shots for Google Flow video generation. "
                    "Return a JSON array of shot objects with keys: shot_number, camera_angle, camera_movement, duration_seconds, visual_description, lighting, character_references, video_prompt, negative_prompt."
                )
                user_msg = f"Scene: {scene_slugline}\nSummary: {scene_summary}\nGenre: {genre}\nVisual Style: {visual_style}\nShot count: {count}"
                response = self._client.models.generate_content(
                    model=self.model,
                    contents=f"{sys_prompt}\n\n{user_msg}",
                    config={"response_mime_type": "application/json"}
                )
                shots = json.loads(response.text)
                if isinstance(shots, list) and len(shots) > 0:
                    return shots
            except Exception as e:
                logger.warning(f"Gemini shot generation failed ({e}). Using Offline Studio Engine.")

        return offline_ai.generate_storyboard_shots(
            scene_slugline=scene_slugline,
            scene_summary=scene_summary,
            genre=genre,
            characters=characters,
            visual_style=visual_style,
            count=count
        )

    def check_continuity(self, shots: List[Dict[str, Any]], characters: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Checks continuity across storyboard shots."""
        return offline_ai.analyze_continuity(shots=shots, characters=characters)

gemini_service = GeminiService()
