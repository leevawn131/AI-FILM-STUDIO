"""
Cost Controller & Usage Ledger Module.
Implements:
- Provider-aware usage ledger (Gemini, Google Flow, Local FFmpeg, Offline Engine)
- Separation of Flow subscription credits from monetary API costs
- Support for unknown costs without inventing provider pricing
- Database-level atomic budget enforcement using conditional UPDATEs
- Per-scene and per-project budget limit enforcement with atomic rollbacks
- Idempotent duplicate job prevention via cryptographic hashing
- Concurrency protection across processes and independent sessions
- Explicit tracking of external Google Flow video generations and regenerations
"""

import math
import uuid
import json
import logging
import hashlib
import threading
from typing import Optional, Any, Dict, Tuple
from sqlalchemy import update, or_, case
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from fastapi import HTTPException
from backend.app.config import settings
from backend.app.models.entities import Project, Scene, Shot, CostRecord

logger = logging.getLogger("cost_controller")

# Global re-entrant lock to ensure thread safety
_budget_lock = threading.RLock()

# Accounting operation definitions (Transparent local bookkeeping, not vendor price guarantees)
ACCOUNTING_OPERATIONS = {
    "screenplay_generation": {
        "provider": "gemini",
        "default_model": settings.gemini_model,
        "description": "Interactive screenplay drafting and dialogue generation",
        "consumes_flow_credits": False,
        "default_flow_credits": 0.0,
        "default_estimated_cost": 0.0
    },
    "script_generation": {
        "provider": "gemini",
        "default_model": settings.gemini_model,
        "description": "Interactive screenplay drafting and dialogue generation",
        "consumes_flow_credits": False,
        "default_flow_credits": 0.0,
        "default_estimated_cost": 0.0
    },
    "storyboard_generation": {
        "provider": "gemini",
        "default_model": settings.gemini_model,
        "description": "Shot prompt expansion and visual direction planning",
        "consumes_flow_credits": False,
        "default_flow_credits": 0.0,
        "default_estimated_cost": 0.0
    },
    "flow_prompt_pack": {
        "provider": "google_flow",
        "default_model": "google-flow-v1",
        "description": "Google Flow video prompt package and camera parameters (Local Preparation)",
        "consumes_flow_credits": False, # Local preparation does NOT deduct Flow credits
        "default_flow_credits": 0.0,
        "default_estimated_cost": 0.0
    },
    "flow_video_generation": {
        "provider": "google_flow",
        "default_model": "google-flow-v1",
        "description": "External Google Flow video generation",
        "consumes_flow_credits": True,
        "default_flow_credits": 1.0,
        "default_estimated_cost": 0.0
    },
    "audio_synthesis": {
        "provider": "offline_engine",
        "default_model": "local-synth",
        "description": "Cinematic ambient tone and scratch voice synthesis",
        "consumes_flow_credits": False,
        "default_flow_credits": 0.0,
        "default_estimated_cost": 0.0
    },
    "video_render": {
        "provider": "local_ffmpeg",
        "default_model": "ffmpeg-local",
        "description": "Local FFmpeg video concatenation, transitions, and audio sync",
        "consumes_flow_credits": False,
        "default_flow_credits": 0.0,
        "default_estimated_cost": 0.0
    },
    "image_generation": {
        "provider": "google_nano_banana",
        "default_model": getattr(settings, "google_image_model", "gemini-3.1-flash-image"),
        "description": "Google Nano Banana 2 image generation",
        "consumes_flow_credits": False,
        "default_flow_credits": 0.0,
        "default_estimated_cost": 0.02
    },
    "video_generation": {
        "provider": "google_omni",
        "default_model": getattr(settings, "google_video_model", "gemini-omni-1.1-flash"),
        "description": "Google Gemini Omni Flash 1.1 video generation via Interactions API",
        "consumes_flow_credits": False,
        "default_flow_credits": 0.0,
        "default_estimated_cost": 0.15
    },
    "tts_generation": {
        "provider": "google_tts",
        "default_model": getattr(settings, "google_tts_model", "gemini-3.8-flash-tts"),
        "description": "Google Gemini TTS dialogue speech synthesis",
        "consumes_flow_credits": False,
        "default_flow_credits": 0.0,
        "default_estimated_cost": 0.01
    },
    "music_generation": {
        "provider": "google_lyria",
        "default_model": getattr(settings, "google_music_model", "lyria-3.5"),
        "description": "Google Lyria 3.5 score music generation via Interactions API",
        "consumes_flow_credits": False,
        "default_flow_credits": 0.0,
        "default_estimated_cost": 0.05
    }
}

# Legacy rate dict for backwards compatibility with clients/tests reading COST_RATES
COST_RATES = {
    "script_generation": {"credits": 1.0, "cost_usd": 0.0},
    "storyboard_generation": {"credits": 2.0, "cost_usd": 0.0},
    "flow_prompt_pack": {"credits": 0.0, "cost_usd": 0.0}, # 0 credits for prompt package
    "flow_video_generation": {"credits": 1.0, "cost_usd": 0.0},
    "continuity_check": {"credits": 0.5, "cost_usd": 0.0},
    "audio_synthesis": {"credits": 1.5, "cost_usd": 0.0},
    "video_render": {"credits": 3.0, "cost_usd": 0.0},
    "image_generation": {"credits": 0.0, "cost_usd": 0.02},
    "video_generation": {"credits": 0.0, "cost_usd": 0.15},
    "tts_generation": {"credits": 0.0, "cost_usd": 0.01},
    "music_generation": {"credits": 0.0, "cost_usd": 0.05},
}

class CostController:
    @staticmethod
    def compute_hash(payload: Any) -> str:
        """Computes a SHA-256 hash of the payload for idempotent deduplication."""
        if payload is None:
            return ""
        raw = json.dumps(payload, sort_keys=True, default=str)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @staticmethod
    def compute_operation_identity(
        project_id: str,
        action_type: str,
        provider: str,
        operation: str,
        payload: Any
    ) -> str:
        """
        Builds a unique composite operation identity from:
        project_id + action_type + provider + operation + payload hash.
        Ensures two distinct operations with identical payloads do not share the same CostRecord.
        """
        p_hash = CostController.compute_hash(payload)
        raw = f"{project_id}:{action_type}:{provider}:{operation}:{p_hash}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @staticmethod
    def check_and_deduplicate(
        db: Session,
        project_id: str,
        action_type: str,
        payload: Any,
        provider: Optional[str] = None,
        operation: Optional[str] = None
    ) -> Optional[CostRecord]:
        """Checks if an identical operation was already recorded to prevent duplicate expenditure."""
        op_name = operation or action_type
        op_meta = ACCOUNTING_OPERATIONS.get(op_name, {})
        resolved_provider = provider or op_meta.get("provider", "offline_engine")
        op_id = CostController.compute_operation_identity(
            project_id=project_id,
            action_type=action_type,
            provider=resolved_provider,
            operation=op_name,
            payload=payload
        )
        return db.query(CostRecord).filter(
            CostRecord.project_id == project_id,
            CostRecord.operation_identity == op_id
        ).first()

    @staticmethod
    def authorize_and_charge(
        db: Session,
        project_id: str,
        action_type: str,
        scene_id: Optional[str] = None,
        shot_id: Optional[str] = None,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        operation: Optional[str] = None,
        payload: Optional[Any] = None,
        details: str = "",
        allow_duplicate: bool = False,
        input_tokens: Optional[int] = None,
        output_tokens: Optional[int] = None,
        flow_credits: Optional[float] = None,
        actual_cost: Optional[float] = None,
        estimated_cost: Optional[float] = None,
        actual_cost_known: bool = False,
        currency: str = "USD",
        effective_provider: Optional[str] = None
    ) -> CostRecord:
        """
        Database-level atomic budget enforcement and provider-aware ledger recording.
        - Uses conditional atomic SQL UPDATE to enforce project and scene budget limits.
        - Composite operation identity deduplication prevents distinct operations with identical payloads from colliding.
        - Checks duplicate operation identity; returns existing record idempotently without charging again.
        - Separates subscription Flow credits from currency costs.
        - Rolls back transactions completely on limit violation.
        """
        # 1. Determine provider and operation mapping
        op_name = operation or action_type
        op_meta = ACCOUNTING_OPERATIONS.get(op_name, {})

        resolved_provider = provider or op_meta.get("provider", "offline_engine")
        resolved_model = model or op_meta.get("default_model")

        # 2. Composite deduplication check (Database-enforced Idempotency under Policy B)
        # Policy B: Charges are incurred only for successful finalization.
        # If a previous generation was refunded, a retry re-charges with a sequenced attempt identity.
        p_hash = CostController.compute_hash(payload) if payload else ""
        if payload and not allow_duplicate:
            op_id = CostController.compute_operation_identity(
                project_id=project_id,
                action_type=action_type,
                provider=resolved_provider,
                operation=op_name,
                payload=payload
            )

            bind = db.get_bind()
            is_pg = bind.dialect.name == "postgresql" if bind else False

            with _budget_lock:
                # Fast-path query check
                existing = db.query(CostRecord).filter(
                    CostRecord.operation_identity == op_id
                ).first()
                if existing:
                    # In PostgreSQL, serialize concurrent retry sequences by locking the original CostRecord row
                    if is_pg:
                        db.query(CostRecord).filter(CostRecord.id == existing.id).with_for_update().first()

                    # Check if this charge was compensated/refunded due to past finalization failure
                    refund = db.query(CostRecord).filter(
                        CostRecord.operation_identity == f"refund_{existing.id}"
                    ).first()
                    if not refund:
                        # Original charge is active and unrefunded -> idempotent return
                        return existing

                    # The previous charge was refunded. Check if another concurrent retry already charged
                    # and is currently active (unrefunded).
                    # Bound retry identity prefix length to ensure it fits safely within 64-char legacy schema limits
                    retry_prefix = f"{op_id[:48]}:retry:"
                    active_retry = (
                        db.query(CostRecord)
                        .filter(
                            CostRecord.operation_identity.like(f"{retry_prefix}%"),
                            CostRecord.action_type != "refund"
                        )
                        .order_by(CostRecord.created_at.desc(), CostRecord.id.desc())
                        .first()
                    )
                    if active_retry:
                        retry_refund = db.query(CostRecord).filter(
                            CostRecord.operation_identity == f"refund_{active_retry.id}"
                        ).first()
                        if not retry_refund:
                            # Concurrent retry already charged and is currently active -> idempotent return
                            return active_retry

                    # Compute sequenced retry attempt identity
                    retry_count = db.query(CostRecord).filter(
                        CostRecord.operation_identity.like(f"{retry_prefix}%"),
                        CostRecord.action_type != "refund"
                    ).count()
                    op_id = f"{retry_prefix}{retry_count + 1}"

                    # Check if this specific retry attempt was already recorded and unrefunded
                    retry_existing = db.query(CostRecord).filter(
                        CostRecord.operation_identity == op_id
                    ).first()
                    if retry_existing:
                        retry_refund = db.query(CostRecord).filter(
                            CostRecord.operation_identity == f"refund_{retry_existing.id}"
                        ).first()
                        if not retry_refund:
                            return retry_existing
        else:
            op_id = None

        # Flow credits separation
        resolved_flow_credits = flow_credits if flow_credits is not None else (
            op_meta.get("default_flow_credits", 0.0) if op_meta.get("consumes_flow_credits") else 0.0
        )

        # Determine cost to deduct from monetary budget
        if actual_cost is not None and actual_cost_known:
            monetary_charge = max(0.0, float(actual_cost))
        elif estimated_cost is not None:
            monetary_charge = max(0.0, float(estimated_cost))
        else:
            monetary_charge = op_meta.get("default_estimated_cost", 0.0)

        # 3. Database-Level Atomic Budget Enforcement
        if monetary_charge > 0:
            # Conditional atomic UPDATE on Project budget
            stmt_proj = (
                update(Project)
                .where(Project.id == project_id)
                .where((Project.budget_spent + monetary_charge) <= Project.budget_total)
                .values(budget_spent=Project.budget_spent + monetary_charge)
            )
            res_proj = db.execute(stmt_proj)
            if res_proj.rowcount == 0:
                db.rollback()
                proj = db.query(Project.id, Project.budget_spent, Project.budget_total).filter(Project.id == project_id).first()
                if not proj:
                    raise HTTPException(status_code=404, detail="Project not found")
                raise HTTPException(
                    status_code=400,
                    detail=f"Project budget limit reached! Spent: ${proj.budget_spent:.2f}, Limit: ${proj.budget_total:.2f}. Increase budget to continue."
                )

            # Conditional atomic UPDATE on Scene budget if scene_id is provided
            if scene_id:
                scene = db.query(Scene.id, Scene.budget_limit, Scene.budget_spent).filter(Scene.id == scene_id).first()
                if scene and scene.budget_limit and scene.budget_limit > 0:
                    stmt_scene = (
                        update(Scene)
                        .where(Scene.id == scene_id)
                        .where((Scene.budget_spent + monetary_charge) <= Scene.budget_limit)
                        .values(budget_spent=Scene.budget_spent + monetary_charge)
                    )
                    res_scene = db.execute(stmt_scene)
                    if res_scene.rowcount == 0:
                        db.rollback()
                        raise HTTPException(
                            status_code=400,
                            detail=f"Scene budget limit exceeded! (${scene.budget_spent:.2f} + ${monetary_charge:.3f} > limit ${scene.budget_limit:.2f})"
                        )
        else:
            # Ensure project exists
            exists = db.query(Project.id).filter(Project.id == project_id).first()
            if not exists:
                raise HTTPException(status_code=404, detail="Project not found")

        # 4. Create ledger record in the same atomic transaction
        record = CostRecord(
            project_id=project_id,
            scene_id=scene_id,
            shot_id=shot_id,
            provider=effective_provider or resolved_provider,
            model=resolved_model,
            operation=op_name,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            flow_credits=resolved_flow_credits,
            actual_cost=actual_cost if actual_cost_known else None,
            currency=currency,
            estimated_cost=estimated_cost if estimated_cost is not None else monetary_charge,
            actual_cost_known=actual_cost_known,
            # Legacy compatibility fields
            action_type=action_type,
            credits_used=resolved_flow_credits if resolved_flow_credits > 0 else 1.0,
            cost_usd=monetary_charge,
            prompt_hash=p_hash,
            operation_identity=op_id,
            details=details or f"Provider: {resolved_provider} | Op: {op_name}"
        )

        db.add(record)
        try:
            db.commit()
            db.refresh(record)
            return record
        except IntegrityError:
            db.rollback()
            # If concurrent race condition occurred with identical operation_identity,
            # the speculative budget increment was cleanly rolled back. Return the committed winner.
            if op_id:
                existing = db.query(CostRecord).filter(
                    CostRecord.operation_identity == op_id
                ).first()
                if existing:
                    return existing
            raise

    @staticmethod
    def record_compensation(
        db: Session,
        project_id: str,
        original_record: CostRecord,
        reason: str = "generation_finalization_failed",
        commit: bool = True
    ) -> Optional[CostRecord]:
        """
        Policy B: Scene finalization failure compensation.
        Creates an immutable compensation/refund ledger entry and restores Project.budget_spent.
        - Uses locking/atomic idempotency tied to original CostRecord.
        - Only one transaction is allowed to create the refund and decrease Project.budget_spent.
        - Refund record and budget adjustment occur in the same atomic transaction.
        - Concurrent loser re-reads the committed refund and returns that exact record.
        - Does not decrease budget twice. Does not create duplicate refunds.
        - Deterministic behavior for refund amount 0: records 0-cost refund record for retry tracking
          without altering Project.budget_spent.
        """
        if not original_record:
            return None

        refund_op_id = f"refund_{original_record.id}"

        bind = db.get_bind()
        is_pg = bind.dialect.name == "postgresql" if bind else False

        with _budget_lock:
            # Row lock in PostgreSQL on the original CostRecord to serialize concurrent compensations
            if is_pg:
                db.query(CostRecord).filter(CostRecord.id == original_record.id).with_for_update().first()

            # 1. Fast-path check: already refunded?
            existing_refund = db.query(CostRecord).filter(
                CostRecord.operation_identity == refund_op_id
            ).first()
            if existing_refund:
                return existing_refund

            refund_amount = max(0.0, float(original_record.cost_usd or 0.0))
            if refund_amount > 0.0:
                stmt = (
                    update(Project)
                    .where(Project.id == project_id)
                    .values(
                        budget_spent=case(
                            (Project.budget_spent - refund_amount < 0.0, 0.0),
                            else_=Project.budget_spent - refund_amount
                        )
                    )
                )
                db.execute(stmt)

            refund_record = CostRecord(
                project_id=project_id,
                scene_id=original_record.scene_id,
                shot_id=original_record.shot_id,
                provider=original_record.provider,
                model=original_record.model,
                operation="compensation_refund",
                action_type="refund",
                cost_usd=-refund_amount,
                estimated_cost=-refund_amount,
                actual_cost=-refund_amount if original_record.actual_cost_known else None,
                actual_cost_known=original_record.actual_cost_known,
                currency=original_record.currency or "USD",
                operation_identity=refund_op_id,
                details=f"Compensation refund for cost record {original_record.id}: {reason}"
            )
            db.add(refund_record)
            if commit:
                try:
                    db.commit()
                    db.refresh(refund_record)
                    return refund_record
                except IntegrityError:
                    db.rollback()
                    # Concurrent race recovery
                    existing = db.query(CostRecord).filter(
                        CostRecord.operation_identity == refund_op_id
                    ).first()
                    if existing:
                        return existing
                    raise
            else:
                db.flush()
                return refund_record

    @staticmethod
    def get_active_job_reservation(
        db: Session,
        job_id: str,
        project_id: Optional[str] = None
    ) -> Optional[CostRecord]:
        """
        Finds the active (unrefunded) reservation or in-flight/unknown reservation
        associated with a GenerationJob so retries and recovery never double-reserve.
        """
        if not job_id:
            return None
        query = db.query(CostRecord).filter(
            CostRecord.action_type != "refund",
            CostRecord.details.like(f"%job:{job_id}%")
        )
        if project_id:
            query = query.filter(CostRecord.project_id == project_id)
        candidates = query.order_by(CostRecord.created_at.desc(), CostRecord.id.desc()).all()
        marker = f"job:{job_id}"
        for rec in candidates:
            details_str = rec.details or ""
            idx = details_str.find(marker)
            if idx == -1:
                continue
            after_idx = idx + len(marker)
            if after_idx < len(details_str) and not details_str[after_idx].isspace():
                continue
            refund = db.query(CostRecord).filter(
                CostRecord.operation_identity == f"refund_{rec.id}"
            ).first()
            if not refund:
                return rec
        return None

    @staticmethod
    def reserve_budget(
        db: Session,
        project_id: str,
        estimated_cost: float,
        operation: str,
        scene_id: Optional[str] = None,
        shot_id: Optional[str] = None,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        job_id: Optional[str] = None,
        details: str = "Pre-flight budget reservation"
    ) -> CostRecord:
        """
        Reserves estimated cost against project/scene budget prior to initiating an external cloud AI call.
        When `job_id` is provided, reservation creation is idempotent for active/unrefunded attempts
        so crash recovery or polling timeout retries cannot create duplicate reservations.
        """
        full_details = details
        if job_id and f"job:{job_id}" not in (details or ""):
            full_details = f"{details} job:{job_id}"

        idempotency_payload = (
            {"reservation_job_id": str(job_id), "operation": operation}
            if job_id
            else None
        )

        return CostController.authorize_and_charge(
            db=db,
            project_id=project_id,
            scene_id=scene_id,
            shot_id=shot_id,
            action_type="budget_reservation",
            provider=provider,
            model=model,
            operation=operation,
            payload=idempotency_payload,
            details=full_details,
            estimated_cost=estimated_cost,
            actual_cost=None,
            actual_cost_known=False,
            allow_duplicate=(job_id is None)
        )

    @staticmethod
    def finalize_reservation(
        db: Session,
        project_id: str,
        reservation_record: CostRecord,
        actual_cost: Optional[float] = None,
        actual_cost_known: bool = False,
        local_processing_status: str = "completed",
        error_details: Optional[str] = None
    ) -> CostRecord:
        """
        Idempotently finalizes an outstanding budget reservation when provider outcome is known or timed out.
        - When `local_processing_status` is 'provider_outcome_unknown' / 'outcome_unknown', transitions the
          record to 'provider_outcome_unknown' and keeps the reserved charge intact (never refunded on timeout).
        - When `local_processing_status` is 'completed' or 'failed' (provider succeeded, local work failed),
          idempotently transitions to operational `action_type` and adjusts Project.budget_spent ONLY ONCE if
          actual_cost differs from what has already been charged.
        - Never double-charges if called repeatedly, and never re-charges an already-refunded record.
        """
        if reservation_record is None:
            raise ValueError("reservation_record is required for finalize_reservation")

        bind = db.get_bind()
        is_pg = bind.dialect.name == "postgresql" if bind else False

        with _budget_lock:
            q = db.query(CostRecord).filter(CostRecord.id == reservation_record.id)
            if is_pg:
                q = q.with_for_update()
            rec = q.first() or reservation_record

            # Never finalize a reservation that has already been refunded
            existing_refund = db.query(CostRecord).filter(
                CostRecord.operation_identity == f"refund_{rec.id}"
            ).first()
            if existing_refund:
                logger.warning(f"Skipping finalize_reservation for already-refunded record {rec.id}")
                return rec

            op_name = rec.operation or "video_generation"
            existing_job_tags = [
                tok.lstrip("(").rstrip(".,;:)")
                for tok in (rec.details or "").split()
                if tok.lstrip("(").startswith("job:")
            ]
            job_suffix = f" {' '.join(existing_job_tags)}" if existing_job_tags else ""

            # Ambiguous / polling timeout state: preserve reserved budget without refunding
            if local_processing_status in ("provider_outcome_unknown", "outcome_unknown"):
                if rec.action_type == op_name:
                    return rec
                rec.action_type = "provider_outcome_unknown"
                rec.details = (
                    f"Provider outcome unknown ({rec.provider} {rec.model} {op_name}); "
                    f"reservation preserved: {error_details or 'timeout / interrupted response'}"
                    f"{job_suffix}"
                )
                db.flush()
                return rec

            # Idempotency guard: if already finalized with op_name and no new actual cost reconciliation needed
            if rec.action_type == op_name and (not actual_cost_known or rec.actual_cost_known):
                return rec

            if actual_cost_known and actual_cost is not None and not rec.actual_cost_known:
                new_cost = max(0.0, float(actual_cost))
                already_charged = float(rec.cost_usd or 0.0)
                diff = new_cost - already_charged
                if abs(diff) > 1e-12:
                    stmt = (
                        update(Project)
                        .where(Project.id == project_id)
                        .values(
                            budget_spent=case(
                                (Project.budget_spent + diff < 0.0, 0.0),
                                else_=Project.budget_spent + diff
                            )
                        )
                    )
                    db.execute(stmt)
                rec.actual_cost = new_cost
                rec.actual_cost_known = True
                rec.cost_usd = new_cost

            rec.action_type = op_name

            if local_processing_status == "failed":
                rec.details = (
                    f"Provider generation succeeded ({rec.provider} {rec.model}); "
                    f"local media processing failed: {error_details or 'Unknown error'}"
                    f"{job_suffix}"
                )
            else:
                rec.details = (
                    f"Completed {rec.provider} {rec.model} {op_name}{job_suffix}"
                )
            db.flush()
            return rec

    @staticmethod
    def release_reservation(
        db: Session,
        project_id: str,
        reservation_record: CostRecord,
        reason: str = "operation_failed_or_cancelled",
        allow_unknown_release: bool = False
    ) -> Optional[CostRecord]:
        """
        Idempotently releases reserved budget ONLY if the operation is confirmed failed/cancelled
        at the provider or failed pre-flight before provider generation.
        Never refunds already-finalized charges (e.g. local processing failures),
        and never refunds 'provider_outcome_unknown' reservations unless explicitly
        confirmed non-billable remotely (`allow_unknown_release=True`).
        """
        if not reservation_record:
            return None

        with _budget_lock:
            rec = db.query(CostRecord).filter(CostRecord.id == reservation_record.id).first() or reservation_record

            # If already refunded, return existing refund idempotently via record_compensation
            existing_refund = db.query(CostRecord).filter(
                CostRecord.operation_identity == f"refund_{rec.id}"
            ).first()
            if existing_refund:
                return existing_refund

            if rec.action_type == "provider_outcome_unknown" and not allow_unknown_release:
                logger.warning(
                    f"Refusing to release reservation {rec.id} with provider_outcome_unknown "
                    f"(reason='{reason}')"
                )
                return None

            if rec.action_type not in ("budget_reservation", "provider_outcome_unknown"):
                logger.warning(
                    f"Refusing to release already-finalized reservation {rec.id} "
                    f"(action_type={rec.action_type}, reason='{reason}')"
                )
                return None

            return CostController.record_compensation(
                db=db,
                project_id=project_id,
                original_record=rec,
                reason=reason
            )

    @staticmethod
    def recover_orphaned_job_reservation(
        db: Session,
        job_id: str,
        project_id: str
    ) -> Optional[CostRecord]:
        """
        Releases any unfinalized budget reservation for a job that terminated before reaching the cloud provider.
        """
        target_details = f"job:{job_id}"
        reservation = (
            db.query(CostRecord)
            .filter(
                CostRecord.project_id == project_id,
                CostRecord.action_type == "budget_reservation",
                CostRecord.details.like(f"%{target_details}%")
            )
            .first()
        )
        if reservation:
            refund = db.query(CostRecord).filter(
                CostRecord.operation_identity == f"refund_{reservation.id}"
            ).first()
            if not refund:
                logger.info(f"Releasing orphaned budget reservation {reservation.id} for job {job_id}")
                return CostController.release_reservation(
                    db=db,
                    project_id=project_id,
                    reservation_record=reservation,
                    reason="orphaned_job_local_backend_restart"
                )
        return None

    @staticmethod
    def recover_interrupted_reservations(db: Session) -> int:
        """
        Scans for all outstanding unrefunded 'budget_reservation' records across projects
        that are associated with dead jobs and safely releases them.
        """
        from backend.app.models.entities import GenerationJob
        reservations = (
            db.query(CostRecord)
            .filter(CostRecord.action_type == "budget_reservation")
            .all()
        )
        released_count = 0
        for res_rec in reservations:
            refund = db.query(CostRecord).filter(
                CostRecord.operation_identity == f"refund_{res_rec.id}"
            ).first()
            if refund:
                continue

            is_orphaned = True
            if "job:" in (res_rec.details or ""):
                try:
                    parts = res_rec.details.split("job:")[1].split()
                    job_id = parts[0].strip() if parts else ""
                    active_job = db.query(GenerationJob).filter(
                        GenerationJob.id == job_id,
                        GenerationJob.status.in_(["pending", "processing"])
                    ).first()
                    if active_job and active_job.provider_operation_id:
                        is_orphaned = False
                except Exception:
                    is_orphaned = True

            if is_orphaned:
                CostController.release_reservation(
                    db=db,
                    project_id=res_rec.project_id,
                    reservation_record=res_rec,
                    reason="interrupted_reservation_backend_recovery"
                )
                released_count += 1

        if released_count > 0:
            db.commit()
        return released_count

    @staticmethod
    def record_flow_credit_usage(
        db: Session,
        project_id: str,
        shot_id: str,
        flow_credits: float = 1.0,
        is_regeneration: bool = False,
        generation_attempt_id: Optional[str] = None,
        notes: str = ""
    ) -> Tuple[CostRecord, Shot, bool]:
        """
        Explicitly records actual external Google Flow video generation credits idempotently.
        - Validates generation_attempt_id is mandatory and a valid UUID. Never silently generates a random UUID.
        - Validates flow_credits > 0, finite, reasonable upper bound <= 100.0.
        - Validates shot belongs to project.
        - Same ID + identical payload returns existing ledger record without charging or incrementing.
        - Same ID + different shot_id or flow_credits returns HTTP 409 conflict.
        - Updates shot.flow_credits_spent and increments shot.generation_attempts only for new attempts.
        - Returns (record, shot, is_new).
        """
        if not generation_attempt_id:
            raise HTTPException(
                status_code=400,
                detail="generation_attempt_id is mandatory for recording Google Flow usage"
            )

        try:
            attempt_uuid = str(uuid.UUID(str(generation_attempt_id).strip()))
        except (ValueError, AttributeError):
            raise HTTPException(
                status_code=400,
                detail="generation_attempt_id must be a valid UUID string"
            )

        if not math.isfinite(flow_credits) or flow_credits <= 0.0 or flow_credits > 100.0:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid flow_credits '{flow_credits}': must be a finite positive number <= 100.0"
            )

        # Check for idempotent duplicate by generation_attempt_id
        existing = db.query(CostRecord).filter(
            CostRecord.generation_attempt_id == attempt_uuid
        ).first()

        shot = db.query(Shot).filter(Shot.id == shot_id, Shot.project_id == project_id).first()
        if not shot:
            raise HTTPException(
                status_code=404,
                detail=f"Shot {shot_id} not found or does not belong to project {project_id}"
            )

        if existing:
            # Check payload consistency
            if existing.shot_id != shot_id or not math.isclose(existing.flow_credits or 0.0, flow_credits, abs_tol=1e-5):
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"generation_attempt_id '{attempt_uuid}' conflict: existing attempt recorded for "
                        f"shot '{existing.shot_id}' with {existing.flow_credits} credits, but request specified "
                        f"shot '{shot_id}' with {flow_credits} credits."
                    )
                )
            return existing, shot, False

        # Atomically increment shot attempts and credits
        shot.flow_credits_spent = (shot.flow_credits_spent or 0.0) + flow_credits
        shot.generation_attempts = (shot.generation_attempts or 0) + 1

        attempt_label = f"Regeneration #{shot.generation_attempts}" if is_regeneration else f"Generation #{shot.generation_attempts}"
        detail_msg = f"Flow Video: {shot.shot_code} ({attempt_label})"
        if notes:
            detail_msg += f" - {notes}"

        p_hash = CostController.compute_hash({
            "shot_id": shot_id,
            "attempt_id": attempt_uuid,
            "credits": flow_credits
        })
        op_id = CostController.compute_operation_identity(
            project_id=project_id,
            action_type="flow_video_generation",
            provider="google_flow",
            operation="flow_video_generation",
            payload={"attempt_id": attempt_uuid}
        )

        record = CostRecord(
            project_id=project_id,
            scene_id=shot.scene_id,
            shot_id=shot.id,
            provider="google_flow",
            model="google-flow-v1",
            operation="flow_video_generation",
            input_tokens=0,
            output_tokens=0,
            flow_credits=flow_credits,
            actual_cost=None,
            currency="USD",
            estimated_cost=0.0,
            actual_cost_known=True,
            action_type="flow_video_generation",
            credits_used=flow_credits,
            cost_usd=0.0,
            prompt_hash=p_hash,
            operation_identity=op_id,
            generation_attempt_id=attempt_uuid,
            details=detail_msg
        )
        db.add(record)
        try:
            db.commit()
            db.refresh(record)
            db.refresh(shot)
            return record, shot, True
        except IntegrityError:
            db.rollback()
            # Handle concurrent race condition with identical generation_attempt_id
            existing = db.query(CostRecord).filter(
                CostRecord.generation_attempt_id == attempt_uuid
            ).first()
            if existing:
                if existing.shot_id != shot_id or not math.isclose(existing.flow_credits or 0.0, flow_credits, abs_tol=1e-5):
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            f"generation_attempt_id '{attempt_uuid}' conflict: concurrent attempt recorded with "
                            f"different shot_id or credits."
                        )
                    )
                db.refresh(shot)
                return existing, shot, False
            raise

    @staticmethod
    def get_project_usage_summary(db: Session, project_id: str) -> Dict[str, Any]:
        """Returns aggregated breakdown by provider, credits, tokens, and costs."""
        records = db.query(CostRecord).filter(CostRecord.project_id == project_id).all()

        flow_credits_total = sum([r.flow_credits or 0.0 for r in records if (r.flow_credits or 0.0) > 0 and r.actual_cost_known])
        input_tokens_total = sum([r.input_tokens or 0 for r in records])
        output_tokens_total = sum([r.output_tokens or 0 for r in records])

        known_actual_cost = sum([r.actual_cost for r in records if r.actual_cost is not None and r.actual_cost_known])
        estimated_cost_total = sum([r.estimated_cost or 0.0 for r in records if not r.actual_cost_known])
        unknown_cost_count = sum([1 for r in records if not r.actual_cost_known])

        by_provider = {}
        for r in records:
            p = r.provider or "offline_engine"
            by_provider[p] = by_provider.get(p, 0) + 1

        return {
            "total_flow_credits": round(flow_credits_total, 2),
            "total_input_tokens": input_tokens_total,
            "total_output_tokens": output_tokens_total,
            "known_actual_cost": round(known_actual_cost, 4),
            "estimated_cost_total": round(estimated_cost_total, 4),
            "unknown_cost_count": unknown_cost_count,
            "operations_by_provider": by_provider
        }

cost_controller = CostController()
