"""
Google Nano Banana Image Generation Adapter.
Officially supported Google Cloud AI image generation via `google-genai` Interactions API
(`client.interactions.create(...)`).

Capabilities:
- Nano Banana 2.1 (`gemini-nano-banana-2.1`) primary image model (1K, 2K, 4K; 14 aspect ratios incl. panoramic 1:4, 4:1, 1:8, 8:1)
- Nano Banana Pro (`gemini-3-pro-image`) high-fidelity renders (1K, 2K, 4K)
- Nano Banana 2 Lite (`gemini-3.1-flash-lite-image`) lightweight rapid ideation (512, 1K)
- Strict rejection of deprecated (`gemini-3.1-flash-image`) and unknown model IDs (no silent fallback)
- Local file persistence in studio data directory and SQLite MediaAsset registration
"""

import os
import io
import uuid
import logging
from pathlib import Path
from typing import Dict, Any, Optional, Tuple, List
from sqlalchemy.orm import Session

from backend.app.config import settings
from backend.app.models.entities import Project, MediaAsset, Character, Location, Shot
from backend.app.services.cost_controller import cost_controller
from backend.app.services.google_model_catalog import (
    DEPRECATED_IMAGE_MODELS,
    IMAGE_SUPPORTED_SIZES_BY_MODEL,
    NANO_BANANA_PANORAMIC_ASPECT_RATIOS,
    STANDARD_IMAGE_ASPECT_RATIOS,
    estimate_image_cost_usd,
)
from backend.app.services.provider_error_classifier import (
    classify_provider_exception,
    decode_media_bytes,
    safe_str,
)

logger = logging.getLogger("google_image_adapter")


SUPPORTED_IMAGE_SIZES: Dict[str, List[str]] = dict(IMAGE_SUPPORTED_SIZES_BY_MODEL)


def get_supported_image_sizes_for_model(model_name: str) -> List[str]:
    """
    Returns the officially supported image sizes for a verified Nano Banana model.
    Explicitly rejects deprecated (`gemini-3.1-flash-image`) and unknown models instead of
    guessing or silently falling back.
    """
    if not model_name or not isinstance(model_name, str):
        raise ValueError("Image model name must be a non-empty string.")
    if model_name in DEPRECATED_IMAGE_MODELS:
        raise ValueError(
            f"Image model '{model_name}' is deprecated ({DEPRECATED_IMAGE_MODELS[model_name]}). "
            "Use 'gemini-nano-banana-2.1' instead."
        )
    if model_name not in SUPPORTED_IMAGE_SIZES:
        raise ValueError(
            f"Unknown or unsupported image model '{model_name}'. "
            f"Supported models: {list(SUPPORTED_IMAGE_SIZES.keys())}"
        )
    return list(SUPPORTED_IMAGE_SIZES[model_name])


def get_supported_aspect_ratios_for_model(model_name: str) -> List[str]:
    """Returns supported aspect ratios for a verified Nano Banana model."""
    if model_name == "gemini-3-pro-image":
        return list(STANDARD_IMAGE_ASPECT_RATIOS)
    return list(NANO_BANANA_PANORAMIC_ASPECT_RATIOS)


class GoogleImageAdapterError(Exception):
    """Raised when Google Nano Banana image generation fails."""
    pass


def _is_ambiguous_provider_error(err: Exception) -> bool:
    """Compatibility helper delegating to structured `classify_provider_exception`."""
    return classify_provider_exception(err).is_outcome_unknown


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

    def _resolve_model(
        self,
        model: Optional[str] = None,
        tier: str = "default",
        image_size: str = "1K",
    ) -> Tuple[str, float]:
        """Resolves validated Nano Banana model and corresponding estimated cost."""
        if model:
            target_model = model
        elif tier == "pro":
            target_model = getattr(settings, "google_image_pro_model", "gemini-3-pro-image")
        elif tier == "lite":
            target_model = getattr(settings, "google_image_lite_model", "gemini-3.1-flash-lite-image")
        else:
            target_model = self.model or getattr(settings, "google_image_model", "gemini-nano-banana-2.1")

        # Validate known & non-deprecated model explicitly
        get_supported_image_sizes_for_model(target_model)
        est_cost = estimate_image_cost_usd(target_model, image_size=image_size)
        return target_model, est_cost

    @staticmethod
    def get_supported_image_sizes(model_name: Optional[str] = None) -> Any:
        """Returns supported image sizes by model or all models."""
        if model_name:
            return get_supported_image_sizes_for_model(model_name)
        return SUPPORTED_IMAGE_SIZES

    def _download_uri_bytes(self, uri: str) -> Optional[bytes]:
        files_api = getattr(self.client, "files", None)
        download_fn = getattr(files_api, "download", None) if files_api else None
        if callable(download_fn):
            try:
                downloaded = download_fn(file=uri)
                if isinstance(downloaded, (bytes, bytearray)):
                    return bytes(downloaded)
                if hasattr(downloaded, "read") and callable(downloaded.read):
                    buf = downloaded.read()
                    if isinstance(buf, (bytes, bytearray)):
                        return bytes(buf)
            except Exception as dl_err:
                logger.warning(f"Failed downloading Nano Banana output URI '{uri}': {dl_err}")
        return None

    def _extract_interaction_image_bytes(self, interaction: Any) -> Optional[bytes]:
        """
        Extracts image bytes from a Google Interactions API response.
        Primary: `interaction.output_image.data` (or `.uri` download).
        Fallback: type-guarded `outputs` / `steps` or legacy `candidates`/`parts` inline data.
        Strictly checks real `bytes`/`str` types to avoid MagicMock truthiness in tests.
        """
        output_image = getattr(interaction, "output_image", None)
        if output_image is not None:
            raw = decode_media_bytes(getattr(output_image, "data", None))
            if raw:
                return raw
            uri = safe_str(getattr(output_image, "uri", None))
            if uri:
                dl = self._download_uri_bytes(uri)
                if dl:
                    return dl

        outputs = getattr(interaction, "outputs", None)
        if isinstance(outputs, (list, tuple)):
            for item in outputs:
                raw = decode_media_bytes(getattr(item, "data", None))
                if raw:
                    return raw
                inline = getattr(item, "inline_data", None)
                if inline is not None:
                    raw = decode_media_bytes(getattr(inline, "data", None))
                    if raw:
                        return raw
                uri = safe_str(getattr(item, "uri", None))
                if uri:
                    dl = self._download_uri_bytes(uri)
                    if dl:
                        return dl

        steps = getattr(interaction, "steps", None)
        if isinstance(steps, (list, tuple)):
            for step in steps:
                step_outputs = getattr(step, "outputs", None)
                if isinstance(step_outputs, (list, tuple)):
                    for out in step_outputs:
                        raw = decode_media_bytes(getattr(out, "data", None))
                        if raw:
                            return raw
                        uri = safe_str(getattr(out, "uri", None))
                        if uri:
                            dl = self._download_uri_bytes(uri)
                            if dl:
                                return dl

        # Legacy candidate/parts fallback only if explicitly real lists
        candidates = getattr(interaction, "candidates", None)
        if isinstance(candidates, (list, tuple)) and candidates:
            content = getattr(candidates[0], "content", None)
            cand_parts = getattr(content, "parts", None) if content else None
            if isinstance(cand_parts, (list, tuple)):
                for part in cand_parts:
                    inline = getattr(part, "inline_data", None)
                    raw = decode_media_bytes(getattr(inline, "data", None)) if inline else None
                    if raw:
                        return raw

        return None

    def generate_image(
        self,
        prompt: str,
        project_id: str,
        db: Session,
        aspect_ratio: str = "16:9", # "1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9", "21:9", "1:4", "4:1", "1:8", "8:1"
        image_size: str = "1K",     # "1K", "2K", "4K" on 2.1/Pro; "512", "1K" on Lite
        entity_type: Optional[str] = None, # "character", "location", "storyboard"
        entity_id: Optional[str] = None,
        negative_prompt: Optional[str] = None,
        model: Optional[str] = None,
        tier: str = "default",
        reservation_record: Optional[Any] = None,
        job_id: Optional[str] = None,
        reference_image_paths: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """
        Generates an image via Google Nano Banana using `client.interactions.create()`,
        reserves budget pre-flight before the external API call, validates output bytes with Pillow
        (failing clearly on corrupted/invalid payloads without fabricating dimensions),
        and never releases a reservation when a provider operation returns without image bytes.
        """
        project = db.query(Project).filter(Project.id == project_id).first()
        if not project:
            raise ValueError(f"Project '{project_id}' not found.")

        clean_size = str(image_size or "1K").strip()
        target_model, est_cost = self._resolve_model(model=model, tier=tier, image_size=clean_size)

        supported_sizes = get_supported_image_sizes_for_model(target_model)
        if clean_size not in supported_sizes:
            raise ValueError(
                f"Image size '{clean_size}' is not supported by model '{target_model}'. "
                f"Supported sizes: {supported_sizes}"
            )

        supported_ratios = get_supported_aspect_ratios_for_model(target_model)
        req_ratio = (aspect_ratio or "16:9").strip()
        if req_ratio == "2.39:1":
            req_ratio = "21:9"
        if req_ratio not in supported_ratios:
            raise ValueError(
                f"Aspect ratio '{aspect_ratio}' is not supported by model '{target_model}'. "
                f"Supported aspect ratios: {supported_ratios}"
            )
        clean_ratio = req_ratio

        # Ensure project references directory exists
        dest_dir = settings.projects_dir / project_id / "references"
        dest_dir.mkdir(parents=True, exist_ok=True)

        if not self.is_available:
            # Deterministic fallback when Google API key is not configured
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

        full_prompt = f"{prompt}\n[Avoid: {negative_prompt}]" if negative_prompt else prompt
        interaction_input: Any = full_prompt
        if reference_image_paths:
            import base64
            content_items: List[Dict[str, Any]] = [{"type": "text", "text": full_prompt}]
            for ref_path in reference_image_paths:
                p = Path(ref_path)
                if p.exists() and p.is_file():
                    suffix = p.suffix.lower()
                    ref_mime = "image/png" if suffix == ".png" else ("image/webp" if suffix == ".webp" else "image/jpeg")
                    content_items.append({
                        "type": "image",
                        "data": base64.b64encode(p.read_bytes()).decode("ascii"),
                        "mime_type": ref_mime,
                    })
            if len(content_items) > 1:
                interaction_input = [{"type": "user_input", "content": content_items}]

        response_format = {
            "type": "image",
            "aspect_ratio": clean_ratio,
            "image_size": clean_size,
        }

        interaction = None
        try:
            logger.info(
                f"Submitting Nano Banana image generation ({target_model}, {clean_ratio}, {clean_size}) "
                f"via client.interactions.create()..."
            )
            interaction = self.client.interactions.create(
                model=target_model,
                input=interaction_input,
                response_format=response_format,
            )
        except Exception as api_err:
            err_msg = str(api_err)
            logger.error(f"Nano Banana interactions.create failed: {err_msg}")
            classified = classify_provider_exception(api_err)
            if classified.is_confirmed_pre_generation_rejection:
                cost_controller.release_reservation(
                    db=db,
                    project_id=project_id,
                    reservation_record=reservation_record,
                    reason=f"image_provider_rejected: {classified.reason}",
                )
            else:
                cost_controller.finalize_reservation(
                    db=db,
                    project_id=project_id,
                    reservation_record=reservation_record,
                    local_processing_status="provider_outcome_unknown",
                    error_details=classified.reason,
                )
            db.commit()
            raise GoogleImageAdapterError(f"Nano Banana generation failed: {err_msg}") from api_err

        provider_op_id = safe_str(getattr(interaction, "id", None)) or safe_str(getattr(interaction, "response_id", None))
        remote_status = (safe_str(getattr(interaction, "status", None)) or "").lower()

        if remote_status in ("failed", "cancelled", "budget_exceeded"):
            cost_controller.release_reservation(
                db=db,
                project_id=project_id,
                reservation_record=reservation_record,
                reason=f"image_provider_status_{remote_status}",
            )
            db.commit()
            raise GoogleImageAdapterError(
                f"Google Nano Banana ({target_model}) returned status '{remote_status}'."
            )

        img_bytes = self._extract_interaction_image_bytes(interaction)
        if not img_bytes:
            # Provider returned a response (e.g. completed or accepted) without retrievable image bytes.
            # NEVER auto-refund/release this reservation — keep billing accountability intact!
            cost_controller.finalize_reservation(
                db=db,
                project_id=project_id,
                reservation_record=reservation_record,
                actual_cost=None,
                actual_cost_known=False,
                local_processing_status="output_unretrieved",
                error_details=f"No retrievable image bytes returned (remote_status={remote_status or 'unknown'}, id={provider_op_id or 'none'})",
            )
            db.commit()
            raise GoogleImageAdapterError(
                f"Google Nano Banana ({target_model}) returned no retrievable image bytes in response."
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
                elif fmt == "gif":
                    ext = ".gif"
                    mime_type = "image/gif"
                elif fmt in ("jpeg", "jpg"):
                    ext = ".jpg"
                    mime_type = "image/jpeg"
                else:
                    raise ValueError(f"Unsupported decoded image format: '{fmt}'")
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
