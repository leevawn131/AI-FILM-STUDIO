"""
Google Imagen 3 Image Generation Adapter.
Officially supported Google Generative AI image generation via `google-genai` SDK.

Capabilities:
- Character reference portraits and costume sheets
- Location architectural and environment concept art
- Storyboard visual frames
- Concept art rendering
- Local file persistence in studio data directory
- Automatic MediaAsset registration in SQLite database
"""

import os
import uuid
import logging
from pathlib import Path
from typing import Dict, Any, Optional, Tuple
from sqlalchemy.orm import Session

from backend.app.config import settings
from backend.app.models.entities import Project, MediaAsset, Character, Location, Shot
from backend.app.services.cost_controller import cost_controller

logger = logging.getLogger("google_image_adapter")


class GoogleImageAdapterError(Exception):
    """Raised when Google Imagen generation fails."""
    pass


class GoogleImageAdapter:
    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None):
        self.api_key = api_key or settings.gemini_api_key or os.getenv("GEMINI_API_KEY", "")
        self.model = model or settings.google_image_model
        self._client = None
        if self.api_key:
            try:
                from google import genai
                self._client = genai.Client(api_key=self.api_key)
            except Exception as e:
                logger.warning(f"Could not initialize google-genai client for Imagen: {e}")

    @property
    def is_available(self) -> bool:
        return bool(self._client and self.api_key)

    def generate_image(
        self,
        prompt: str,
        project_id: str,
        db: Session,
        aspect_ratio: str = "16:9", # "1:1", "3:4", "4:3", "9:16", "16:9"
        entity_type: Optional[str] = None, # "character", "location", "storyboard"
        entity_id: Optional[str] = None,
        negative_prompt: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Generates an image via official Google Imagen 3 API,
        saves the output locally in the project assets folder,
        and registers a local MediaAsset record in SQLite.
        """
        project = db.query(Project).filter(Project.id == project_id).first()
        if not project:
            raise ValueError(f"Project '{project_id}' not found.")

        # Ensure project references directory exists
        dest_dir = settings.projects_dir / project_id / "references"
        dest_dir.mkdir(parents=True, exist_ok=True)

        if not self.is_available:
            # Deterministic fallback when Google API key is not configured
            # Generates a clean concept placeholder with explicit fallback attribution
            logger.info("Google Imagen 3 API key not configured. Generating deterministic reference visual.")
            return self._generate_fallback_reference(
                prompt=prompt,
                project=project,
                dest_dir=dest_dir,
                entity_type=entity_type,
                entity_id=entity_id,
                db=db
            )

        from google.genai import types

        # Normalize aspect ratio for Imagen 3
        valid_ratios = ["1:1", "3:4", "4:3", "9:16", "16:9"]
        clean_ratio = aspect_ratio if aspect_ratio in valid_ratios else "16:9"

        config_kwargs: Dict[str, Any] = {
            "number_of_images": 1,
            "output_mime_type": "image/jpeg",
            "aspect_ratio": clean_ratio,
            "person_generation": "allow_adult",
        }
        if negative_prompt:
            config_kwargs["negative_prompt"] = negative_prompt

        config = types.GenerateImagesConfig(**config_kwargs)

        try:
            logger.info(f"Submitting image generation to Google Imagen 3 ({self.model})...")
            response = self._client.models.generate_images(
                model=self.model,
                prompt=prompt,
                config=config
            )
        except Exception as e:
            err_msg = str(e)
            logger.error(f"Google Imagen 3 generation failed: {err_msg}")
            raise GoogleImageAdapterError(f"Google Imagen 3 API error: {err_msg}")

        if not response.generated_images:
            raise GoogleImageAdapterError("Google Imagen 3 returned no images in response.")

        img_obj = response.generated_images[0]
        img_bytes = img_obj.image.image_bytes

        # Save to local project directory
        asset_id = str(uuid.uuid4())
        filename = f"{entity_type or 'concept'}_{entity_id or asset_id[:8]}_{asset_id[:6]}.jpg"
        file_path = dest_dir / filename
        file_path.write_bytes(img_bytes)

        # Register local MediaAsset in SQLite
        media_asset = MediaAsset(
            id=asset_id,
            project_id=project_id,
            filename=filename,
            file_path=str(file_path),
            media_type="image",
            status="ready",
            file_size_bytes=len(img_bytes),
            width=1920 if clean_ratio == "16:9" else (1080 if clean_ratio == "1:1" else 1280),
            height=1080 if clean_ratio == "16:9" else (1080 if clean_ratio == "1:1" else 720)
        )
        db.add(media_asset)

        # Link to target entity
        if entity_type == "character" and entity_id:
            char = db.query(Character).filter(Character.id == entity_id).first()
            if char:
                char.reference_image_path = str(file_path)
        elif entity_type == "location" and entity_id:
            loc = db.query(Location).filter(Location.id == entity_id).first()
            if loc:
                loc.reference_image_path = str(file_path)
        elif entity_type == "storyboard" and entity_id:
            shot = db.query(Shot).filter(Shot.id == entity_id).first()
            if shot:
                shot.thumbnail_path = str(file_path)

        # Record accounting entry
        cost_controller.authorize_and_charge(
            db=db,
            project_id=project_id,
            action_type="image_generation",
            provider="google_imagen",
            model=self.model,
            operation="image_generation",
            details=f"Google Imagen 3 generation for {entity_type or 'concept'} ({entity_id or 'general'})",
            estimated_cost=0.03,
            actual_cost=0.03,
            actual_cost_known=True,
            allow_duplicate=True
        )
        db.commit()

        return {
            "status": "completed",
            "provider": "google_imagen",
            "model": self.model,
            "asset_id": media_asset.id,
            "file_path": str(file_path),
            "filename": filename,
            "file_size_bytes": len(img_bytes),
            "is_fallback": False,
            "aspect_ratio": clean_ratio
        }

    def _generate_fallback_reference(
        self,
        prompt: str,
        project: Project,
        dest_dir: Path,
        entity_type: Optional[str],
        entity_id: Optional[str],
        db: Session
    ) -> Dict[str, Any]:
        """Generates a clean local concept placeholder with explicit fallback attribution."""
        asset_id = str(uuid.uuid4())
        filename = f"{entity_type or 'concept'}_{entity_id or asset_id[:8]}_fallback.svg"
        file_path = dest_dir / filename

        # Create clean SVG artwork placeholder
        title = (entity_type or "CONCEPT REFERENCE").upper()
        svg_content = f"""<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="720" viewBox="0 0 1280 720">
  <defs>
    <linearGradient id="bg" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#0b0f19" />
      <stop offset="100%" stop-color="#1e293b" />
    </linearGradient>
  </defs>
  <rect width="1280" height="720" fill="url(#bg)"/>
  <rect x="40" y="40" width="1200" height="640" fill="none" stroke="#38bdf8" stroke-width="2" stroke-dasharray="8 8" opacity="0.4"/>
  <text x="640" y="280" font-family="monospace" font-size="28" font-weight="bold" fill="#38bdf8" text-anchor="middle" letter-spacing="4">{title}</text>
  <text x="640" y="340" font-family="sans-serif" font-size="18" fill="#94a3b8" text-anchor="middle">Google Imagen 3 API Key Not Set (Local Concept Reference)</text>
  <text x="640" y="400" font-family="sans-serif" font-size="14" fill="#cbd5e1" text-anchor="middle" max-width="800">{prompt[:90]}...</text>
  <text x="640" y="460" font-family="monospace" font-size="12" fill="#64748b" text-anchor="middle">Project: {project.name} | Genre: {project.genre}</text>
</svg>"""
        file_path.write_text(svg_content, encoding="utf-8")

        media_asset = MediaAsset(
            id=asset_id,
            project_id=project.id,
            filename=filename,
            file_path=str(file_path),
            media_type="image",
            status="ready",
            file_size_bytes=len(svg_content.encode("utf-8")),
            width=1280,
            height=720
        )
        db.add(media_asset)

        if entity_type == "character" and entity_id:
            char = db.query(Character).filter(Character.id == entity_id).first()
            if char:
                char.reference_image_path = str(file_path)
        elif entity_type == "location" and entity_id:
            loc = db.query(Location).filter(Location.id == entity_id).first()
            if loc:
                loc.reference_image_path = str(file_path)
        elif entity_type == "storyboard" and entity_id:
            shot = db.query(Shot).filter(Shot.id == entity_id).first()
            if shot:
                shot.thumbnail_path = str(file_path)

        db.commit()

        return {
            "status": "completed",
            "provider": "offline_template",
            "model": "offline-concept-visualizer",
            "asset_id": media_asset.id,
            "file_path": str(file_path),
            "filename": filename,
            "file_size_bytes": len(svg_content.encode("utf-8")),
            "is_fallback": True,
            "aspect_ratio": "16:9"
        }


google_image_adapter = GoogleImageAdapter()
