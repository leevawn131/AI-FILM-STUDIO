"""
Google Nano Banana 2 Image Generation Adapter.
Officially supported Google Cloud AI image generation via `google-genai` SDK.

Capabilities:
- Nano Banana 2 (`gemini-3.1-flash-image`) default fast cinematic image generation
- Nano Banana Pro (`gemini-3-pro-image`) high-fidelity artistic renders
- Nano Banana 2 Lite (`gemini-3.1-flash-lite-image`) lightweight rapid ideation
- Character reference portraits and costume sheets
- Location architectural and environment concept art
- Storyboard visual frames
- Local file persistence in studio data directory
- Automatic MediaAsset registration in SQLite database
"""

import os
import uuid
import base64
import logging
from pathlib import Path
from typing import Dict, Any, Optional, Tuple
from sqlalchemy.orm import Session

from backend.app.config import settings
from backend.app.models.entities import Project, MediaAsset, Character, Location, Shot
from backend.app.services.cost_controller import cost_controller

logger = logging.getLogger("google_image_adapter")


SUPPORTED_IMAGE_SIZES = {
    "gemini-3.1-flash-image": ["512", "1K", "2K", "4K"],
    "gemini-3.1-flash-lite-image": ["512", "1K"],
    "gemini-3-pro-image": ["1K", "2K", "4K"],
}


def get_supported_image_sizes_for_model(model_name: str) -> List[str]:
    """Returns the list of officially supported image sizes for a given model."""
    m = model_name.lower()
    if "lite" in m:
        return ["512", "1K"]
    elif "pro" in m:
        return ["1K", "2K", "4K"]
    else:
        return ["512", "1K", "2K", "4K"]


class GoogleImageAdapterError(Exception):
    """Raised when Google Nano Banana image generation fails."""
    pass


class GoogleImageAdapter:
    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None):
        self._api_key = api_key
        self._model = model
        self._client = None
        self._cached_api_key = None

    @property
    def api_key(self) -> str:
        return self._api_key or settings.gemini_api_key or os.getenv("GEMINI_API_KEY", "")

    @property
    def model(self) -> str:
        return self._model or settings.google_image_model

    @property
    def client(self):
        if self._client is not None and getattr(self, "_injected_client", False):
            return self._client
        curr_key = self.api_key
        if not curr_key:
            return None
        if self._client is not None and self._cached_api_key is None:
            self._cached_api_key = curr_key
            return self._client
        if self._client is None or self._cached_api_key != curr_key:
            try:
                from google import genai
                self._client = genai.Client(api_key=curr_key)
                self._cached_api_key = curr_key
                self._injected_client = False
            except Exception as e:
                logger.warning(f"Could not initialize google-genai client for Nano Banana: {e}")
                self._client = None
        return self._client

    @client.setter
    def client(self, value):
        self._client = value
        self._cached_api_key = self.api_key
        self._injected_client = True

    @property
    def is_available(self) -> bool:
        return bool(self.client and self.api_key)

    def _resolve_model(self, model: Optional[str] = None, tier: str = "default") -> Tuple[str, float]:
        """Resolves target Nano Banana model and corresponding estimated cost."""
        if model:
            target_model = model
        elif tier == "pro":
            target_model = getattr(settings, "google_image_pro_model", "gemini-3-pro-image")
        elif tier == "lite":
            target_model = getattr(settings, "google_image_lite_model", "gemini-3.1-flash-lite-image")
        else:
            target_model = self.model or getattr(settings, "google_image_model", "gemini-3.1-flash-image")

        if "pro" in target_model.lower():
            cost = 0.04
        elif "lite" in target_model.lower():
            cost = 0.01
        else:
            cost = 0.02
        return target_model, cost

    @staticmethod
    def get_supported_image_sizes(model_name: Optional[str] = None) -> Any:
        """Returns supported image sizes by model or all models."""
        if model_name:
            return get_supported_image_sizes_for_model(model_name)
        return SUPPORTED_IMAGE_SIZES

    def generate_image(
        self,
        prompt: str,
        project_id: str,
        db: Session,
        aspect_ratio: str = "16:9", # "1:1", "3:4", "4:3", "9:16", "16:9"
        image_size: str = "1K",     # "512", "1K", "2K", "4K"
        entity_type: Optional[str] = None, # "character", "location", "storyboard"
        entity_id: Optional[str] = None,
        negative_prompt: Optional[str] = None,
        model: Optional[str] = None,
        tier: str = "default"
    ) -> Dict[str, Any]:
        """
        Generates an image via official Google Nano Banana 2 API,
        saves the output locally in the project assets folder,
        inspects true image dimensions via Pillow,
        and registers a local MediaAsset record in SQLite.
        """
        project = db.query(Project).filter(Project.id == project_id).first()
        if not project:
            raise ValueError(f"Project '{project_id}' not found.")

        target_model, est_cost = self._resolve_model(model=model, tier=tier)

        # Enforce model-specific image-size limitations per documentation:
        # - Flash Image: 512, 1K, 2K, 4K
        # - Flash Lite Image: 512, 1K
        # - Pro Image: 1K, 2K, 4K
        supported_sizes = get_supported_image_sizes_for_model(target_model)
        if image_size not in supported_sizes:
            raise ValueError(
                f"Image size '{image_size}' is not supported by model '{target_model}'. "
                f"Supported sizes: {supported_sizes}"
            )
        clean_size = image_size

        # Normalize aspect ratio for Nano Banana
        valid_ratios = ["1:1", "3:4", "4:3", "9:16", "16:9"]
        clean_ratio = aspect_ratio if aspect_ratio in valid_ratios else "16:9"

        # Ensure project references directory exists
        dest_dir = settings.projects_dir / project_id / "references"
        dest_dir.mkdir(parents=True, exist_ok=True)

        if not self.is_available:
            # Deterministic fallback when Google API key is not configured
            # Generates a clean concept placeholder with explicit fallback attribution
            logger.info("Google API key not configured for Nano Banana. Generating deterministic reference visual.")
            return self._generate_fallback_reference(
                prompt=prompt,
                project=project,
                dest_dir=dest_dir,
                entity_type=entity_type,
                entity_id=entity_id,
                db=db
            )

        img_bytes = None
        provider_op_id = None

        # 1. Try official Interactions API first
        try:
            logger.info(f"Submitting image generation to Google Nano Banana ({target_model}) via Interactions API...")
            response_format = {
                "type": "image",
                "aspect_ratio": clean_ratio,
                "image_size": clean_size
            }
            interaction = self.client.interactions.create(
                model=target_model,
                input=prompt,
                response_format=response_format
            )
            provider_op_id = getattr(interaction, "id", None)
            output_image = getattr(interaction, "output_image", None)
            if output_image:
                raw_data = getattr(output_image, "data", None)
                if raw_data:
                    if isinstance(raw_data, str):
                        try:
                            img_bytes = base64.b64decode(raw_data)
                        except Exception:
                            img_bytes = raw_data.encode("utf-8")
                    else:
                        img_bytes = raw_data
                elif getattr(output_image, "uri", None):
                    import urllib.request
                    img_bytes = urllib.request.urlopen(output_image.uri).read()
        except Exception as e_inter:
            logger.info(f"Interactions API route ({e_inter}); attempting models.generate_images fallback...")

        # 2. Try generate_images fallback if needed
        if not img_bytes:
            from google.genai import types
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
                response = self.client.models.generate_images(
                    model=target_model,
                    prompt=prompt,
                    config=config
                )
                if hasattr(response, "generated_images") and response.generated_images:
                    img_obj = response.generated_images[0]
                    if hasattr(img_obj, "image") and hasattr(img_obj.image, "image_bytes"):
                        img_bytes = img_obj.image.image_bytes
                    elif hasattr(img_obj, "image_bytes"):
                        img_bytes = img_obj.image_bytes
            except Exception as e:
                # 3. Try generate_content multimodal route
                logger.info(f"generate_images attempt ({e}); trying generate_content multimodal route...")
                try:
                    gc_config = types.GenerateContentConfig(
                        response_modalities=["IMAGE"]
                    )
                    gc_resp = self.client.models.generate_content(
                        model=target_model,
                        contents=prompt,
                        config=gc_config
                    )
                    if hasattr(gc_resp, "candidates") and gc_resp.candidates:
                        parts = gc_resp.candidates[0].content.parts
                        for part in parts:
                            inline = getattr(part, "inline_data", None)
                            if inline and hasattr(inline, "data"):
                                raw_data = inline.data
                                if isinstance(raw_data, str):
                                    img_bytes = base64.b64decode(raw_data)
                                else:
                                    img_bytes = raw_data
                                break
                except Exception as e2:
                    err_msg = f"Nano Banana generation failed: {e}; multimodal route failed: {e2}"
                    logger.error(err_msg)
                    raise GoogleImageAdapterError(err_msg)

        if not img_bytes:
            raise GoogleImageAdapterError(f"Google Nano Banana ({target_model}) returned no image bytes in response.")

        # Inspect real dimensions and image format using Pillow
        import io
        from PIL import Image

        real_width = 1920 if clean_ratio == "16:9" else (1080 if clean_ratio == "1:1" else 1280)
        real_height = 1080 if clean_ratio == "16:9" else (1080 if clean_ratio == "1:1" else 720)
        ext = ".jpg"
        mime_type = "image/jpeg"
        try:
            with Image.open(io.BytesIO(img_bytes)) as pil_img:
                real_width, real_height = pil_img.size
                fmt = (pil_img.format or "").lower()
                if fmt == "png":
                    ext = ".png"
                    mime_type = "image/png"
                elif fmt == "webp":
                    ext = ".webp"
                    mime_type = "image/webp"
                else:
                    ext = ".jpg"
                    mime_type = "image/jpeg"
        except Exception as img_err:
            logger.warning(f"Could not inspect generated image with Pillow: {img_err}")

        # Save to local project directory
        asset_id = str(uuid.uuid4())
        filename = f"{entity_type or 'concept'}_{entity_id or asset_id[:8]}_{asset_id[:6]}{ext}"
        file_path = dest_dir / filename
        file_path.write_bytes(img_bytes)

        # Register local MediaAsset in SQLite with true dimensions
        media_asset = MediaAsset(
            id=asset_id,
            project_id=project_id,
            filename=filename,
            file_path=str(file_path),
            media_type="image",
            status="ready",
            file_size_bytes=len(img_bytes),
            width=real_width,
            height=real_height
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

        # Record accounting entry (estimated, not verified invoice)
        cost_controller.authorize_and_charge(
            db=db,
            project_id=project_id,
            action_type="image_generation",
            provider="google_nano_banana",
            model=target_model,
            operation="image_generation",
            details=f"Google Nano Banana generation for {entity_type or 'concept'} ({entity_id or 'general'})",
            estimated_cost=est_cost,
            actual_cost=None,
            actual_cost_known=False,
            allow_duplicate=True
        )
        db.commit()

        return {
            "status": "completed",
            "provider": "google_nano_banana",
            "model": target_model,
            "asset_id": media_asset.id,
            "file_path": str(file_path),
            "filename": filename,
            "file_size_bytes": len(img_bytes),
            "width": real_width,
            "height": real_height,
            "mime_type": mime_type,
            "is_fallback": False,
            "aspect_ratio": clean_ratio,
            "image_size": clean_size,
            "provider_operation_id": provider_op_id,
            "interaction_id": provider_op_id
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
  <text x="640" y="340" font-family="sans-serif" font-size="18" fill="#94a3b8" text-anchor="middle">Google Nano Banana 2 API Key Not Set (Local Concept Reference)</text>
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
