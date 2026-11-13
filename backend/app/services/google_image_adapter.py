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
import io
import uuid
import socket
import base64
import logging
from pathlib import Path
from typing import Dict, Any, Optional, Tuple, List
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


def _is_ambiguous_provider_error(err: Exception) -> bool:
    """Returns True when an API exception leaves remote execution outcome ambiguous."""
    if isinstance(err, (TimeoutError, socket.timeout, ConnectionError)):
        return True
    msg = str(err).lower()
    rejection_markers = ("400", "401", "403", "404", "422", "429", "invalid_argument", "permission_denied", "unauthenticated", "api key")
    if any(m in msg for m in rejection_markers):
        return False
    ambiguous_markers = ("timeout", "timed out", "504", "gateway", "connection reset", "broken pipe", "remote end closed", "incomplete read")
    return any(m in msg for m in ambiguous_markers)


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

    @staticmethod
    def _extract_inline_image_bytes(gc_resp: Any) -> Optional[bytes]:
        """Extracts inline image bytes from GenerateContentResponse parts or candidates."""
        candidates = getattr(gc_resp, "candidates", None)
        if isinstance(candidates, (list, tuple)) and candidates:
            content = getattr(candidates[0], "content", None)
            cand_parts = getattr(content, "parts", None) if content else None
            if isinstance(cand_parts, (list, tuple)):
                for part in cand_parts:
                    inline = getattr(part, "inline_data", None)
                    raw_data = getattr(inline, "data", None) if inline else None
                    if raw_data:
                        return base64.b64decode(raw_data) if isinstance(raw_data, str) else bytes(raw_data)

        top_parts = getattr(gc_resp, "parts", None)
        if isinstance(top_parts, (list, tuple)):
            for part in top_parts:
                inline = getattr(part, "inline_data", None)
                raw_data = getattr(inline, "data", None) if inline else None
                if raw_data:
                    return base64.b64decode(raw_data) if isinstance(raw_data, str) else bytes(raw_data)
        return None

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
        tier: str = "default",
        reservation_record: Optional[Any] = None,
        job_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Generates an image via Google Nano Banana 2 using `client.models.generate_content()`,
        reserves budget pre-flight before the external API call, validates output bytes with Pillow
        (failing clearly on corrupted/invalid payloads without fabricating dimensions),
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

        # Pre-flight budget reservation BEFORE external Google API call
        if reservation_record is None:
            reservation_record = cost_controller.reserve_budget(
                db=db,
                project_id=project_id,
                estimated_cost=est_cost,
                operation="image_generation",
                provider="google_nano_banana",
                model=target_model,
                job_id=job_id,
                details=f"Pre-flight Nano Banana image reservation for {entity_type or 'concept'} ({entity_id or 'general'})"
            )
            db.commit()

        from google.genai import types
        gc_config = types.GenerateContentConfig(
            response_modalities=["IMAGE"],
            image_config=types.ImageConfig(
                aspect_ratio=clean_ratio,
                image_size=clean_size,
            ),
        )
        full_contents = f"{prompt}\n[Avoid: {negative_prompt}]" if negative_prompt else prompt

        gc_resp = None
        try:
            logger.info(
                f"Submitting Nano Banana image generation ({target_model}, {clean_ratio}, {clean_size}) "
                f"via models.generate_content()..."
            )
            gc_resp = self.client.models.generate_content(
                model=target_model,
                contents=full_contents,
                config=gc_config,
            )
        except Exception as api_err:
            err_msg = str(api_err)
            logger.error(f"Nano Banana models.generate_content failed: {err_msg}")
            if _is_ambiguous_provider_error(api_err):
                cost_controller.finalize_reservation(
                    db=db,
                    project_id=project_id,
                    reservation_record=reservation_record,
                    local_processing_status="provider_outcome_unknown",
                    error_details=err_msg,
                )
            else:
                cost_controller.release_reservation(
                    db=db,
                    project_id=project_id,
                    reservation_record=reservation_record,
                    reason=f"image_provider_error: {err_msg}",
                )
            db.commit()
            raise GoogleImageAdapterError(f"Nano Banana generation failed: {err_msg}") from api_err

        provider_op_id = getattr(gc_resp, "response_id", None) or getattr(gc_resp, "id", None)
        if not isinstance(provider_op_id, str):
            provider_op_id = None

        img_bytes = self._extract_inline_image_bytes(gc_resp)
        if not img_bytes:
            cost_controller.release_reservation(
                db=db,
                project_id=project_id,
                reservation_record=reservation_record,
                reason="image_no_inline_bytes_returned",
            )
            db.commit()
            raise GoogleImageAdapterError(
                f"Google Nano Banana ({target_model}) returned no inline image bytes in response."
            )

        # Provider outcome SUCCEEDED; validate bytes strictly with Pillow (no fake dimensions)
        from PIL import Image
        try:
            with Image.open(io.BytesIO(img_bytes)) as pil_img:
                pil_img.verify()
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
                if real_width <= 0 or real_height <= 0:
                    raise ValueError(f"Invalid image dimensions: {real_width}x{real_height}")
        except Exception as img_err:
            logger.error(f"Returned bytes failed Pillow validation: {img_err}")
            cost_controller.finalize_reservation(
                db=db,
                project_id=project_id,
                reservation_record=reservation_record,
                actual_cost=None,
                actual_cost_known=False,
                local_processing_status="failed",
                error_details=f"Invalid image bytes returned by provider: {img_err}",
            )
            db.commit()
            raise GoogleImageAdapterError(f"Nano Banana returned invalid image bytes: {img_err}") from img_err

        asset_id = str(uuid.uuid4())
        filename = f"{entity_type or 'concept'}_{entity_id or asset_id[:8]}_{asset_id[:6]}{ext}"
        file_path = dest_dir / filename
        try:
            file_path.write_bytes(img_bytes)

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

            cost_controller.finalize_reservation(
                db=db,
                project_id=project_id,
                reservation_record=reservation_record,
                actual_cost=None,
                actual_cost_known=False,
                local_processing_status="completed",
            )
            db.commit()
        except Exception as local_err:
            db.rollback()
            if file_path.exists():
                try:
                    file_path.unlink()
                except Exception:
                    pass
            cost_controller.finalize_reservation(
                db=db,
                project_id=project_id,
                reservation_record=reservation_record,
                actual_cost=None,
                actual_cost_known=False,
                local_processing_status="failed",
                error_details=str(local_err),
            )
            db.commit()
            raise GoogleImageAdapterError(f"Local image persistence failed after generation: {local_err}") from local_err

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
            "interaction_id": provider_op_id,
            "reservation_id": reservation_record.id if reservation_record else None,
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
