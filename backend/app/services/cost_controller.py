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
import hashlib
import threading
from typing import Optional, Any, Dict, Tuple
from sqlalchemy import update, or_
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from fastapi import HTTPException
from backend.app.config import settings
from backend.app.models.entities import Project, Scene, Shot, CostRecord

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
        currency: str = "USD"
    ) -> CostRecord:
        """
        Database-level atomic budget enforcement and provider-aware ledger recording.
        - Uses conditional atomic SQL UPDATE to enforce project and scene budget limits.
        - Composite operation identity deduplication prevents distinct operations with identical payloads from colliding.
        - Checks duplicate operation identity; returns existing record idempotently without charging again.
        - Separates subscription Flow credits from currency costs.
        - Rolls back transactions completely on limit violation.
        """
        with _budget_lock:
            # 1. Determine provider and operation mapping
            op_name = operation or action_type
            op_meta = ACCOUNTING_OPERATIONS.get(op_name, {})

            resolved_provider = provider or op_meta.get("provider", "offline_engine")
            resolved_model = model or op_meta.get("default_model")

            # 2. Composite deduplication check (Idempotency)
            p_hash = CostController.compute_hash(payload) if payload else ""
            op_id = CostController.compute_operation_identity(
                project_id=project_id,
                action_type=action_type,
                provider=resolved_provider,
                operation=op_name,
                payload=payload
            ) if payload else ""

            if payload and not allow_duplicate and op_id:
                existing = db.query(CostRecord).filter(
                    CostRecord.project_id == project_id,
                    CostRecord.operation_identity == op_id
                ).first()
                if existing:
                    return existing

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
                provider=resolved_provider,
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
            db.commit()
            db.refresh(record)
            return record

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
        - Validates flow_credits > 0, finite, reasonable upper bound <= 100.0.
        - Validates shot belongs to project.
        - If generation_attempt_id already recorded, returns existing ledger record without charging or incrementing.
        - Updates shot.flow_credits_spent and increments shot.generation_attempts only for new attempts.
        - Returns (record, shot, is_new).
        """
        if not math.isfinite(flow_credits) or flow_credits <= 0.0 or flow_credits > 100.0:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid flow_credits '{flow_credits}': must be a finite positive number <= 100.0"
            )

        attempt_id = generation_attempt_id or str(uuid.uuid4())

        # Check for idempotent duplicate by generation_attempt_id
        existing = db.query(CostRecord).filter(
            CostRecord.project_id == project_id,
            CostRecord.generation_attempt_id == attempt_id
        ).first()

        shot = db.query(Shot).filter(Shot.id == shot_id, Shot.project_id == project_id).first()
        if not shot:
            raise HTTPException(
                status_code=404,
                detail=f"Shot {shot_id} not found or does not belong to project {project_id}"
            )

        if existing:
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
            "attempt_id": attempt_id,
            "credits": flow_credits
        })
        op_id = CostController.compute_operation_identity(
            project_id=project_id,
            action_type="flow_video_generation",
            provider="google_flow",
            operation="flow_video_generation",
            payload={"attempt_id": attempt_id}
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
            generation_attempt_id=attempt_id,
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
                CostRecord.project_id == project_id,
                CostRecord.generation_attempt_id == attempt_id
            ).first()
            if existing:
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
