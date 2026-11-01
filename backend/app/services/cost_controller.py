"""
Cost Controller & Usage Ledger Module.
Implements:
- Provider-aware usage ledger (Gemini, Google Flow, Local FFmpeg, Offline Engine)
- Separation of Flow subscription credits from monetary API costs
- Support for unknown costs without inventing provider pricing
- Project and per-scene budget limit enforcement
- Idempotent duplicate job prevention via cryptographic hashing
- Concurrency protection against race conditions
"""

import json
import hashlib
import threading
from typing import Optional, Any, Dict
from sqlalchemy.orm import Session
from fastapi import HTTPException
from backend.app.config import settings
from backend.app.models.entities import Project, Scene, CostRecord

# Global re-entrant lock to ensure atomic budget deduction under concurrent requests
_budget_lock = threading.RLock()

# Accounting operation definitions (Transparent local bookkeeping, not vendor price guarantees)
ACCOUNTING_OPERATIONS = {
    "screenplay_generation": {
        "provider": "gemini",
        "default_model": settings.gemini_model,
        "description": "Interactive screenplay drafting and dialogue generation",
        "consumes_flow_credits": False,
        "default_estimated_cost": 0.0
    },
    "storyboard_generation": {
        "provider": "gemini",
        "default_model": settings.gemini_model,
        "description": "Shot prompt expansion and visual direction planning",
        "consumes_flow_credits": False,
        "default_estimated_cost": 0.0
    },
    "flow_prompt_pack": {
        "provider": "google_flow",
        "default_model": "google-flow-v1",
        "description": "Google Flow video prompt package and camera parameters",
        "consumes_flow_credits": True,
        "default_flow_credits": 1.0,
        "default_estimated_cost": 0.0
    },
    "audio_synthesis": {
        "provider": "offline_engine",
        "default_model": "local-synth",
        "description": "Cinematic ambient tone and scratch voice synthesis",
        "consumes_flow_credits": False,
        "default_estimated_cost": 0.0
    },
    "video_render": {
        "provider": "local_ffmpeg",
        "default_model": "ffmpeg-local",
        "description": "Local FFmpeg video concatenation, transitions, and audio sync",
        "consumes_flow_credits": False,
        "default_estimated_cost": 0.0
    }
}

# Legacy rate dict for backwards compatibility with clients/tests reading COST_RATES
COST_RATES = {
    "script_generation": {"credits": 1.0, "cost_usd": 0.0},
    "storyboard_generation": {"credits": 2.0, "cost_usd": 0.0},
    "flow_prompt_pack": {"credits": 1.0, "cost_usd": 0.0},
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
    def check_and_deduplicate(db: Session, project_id: str, action_type: str, payload: Any) -> Optional[CostRecord]:
        """Checks if an identical job was already recorded to prevent duplicate expenditure."""
        p_hash = CostController.compute_hash(payload)
        if not p_hash:
            return None
        return db.query(CostRecord).filter(
            CostRecord.project_id == project_id,
            CostRecord.prompt_hash == p_hash
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
        input_tokens: int = 0,
        output_tokens: int = 0,
        flow_credits: Optional[float] = None,
        actual_cost: Optional[float] = None,
        estimated_cost: Optional[float] = None,
        actual_cost_known: bool = False,
        currency: str = "USD"
    ) -> CostRecord:
        """
        Provider-aware ledger recording and budget enforcement.
        - Checks duplicate hash; returns existing record idempotently without charging again.
        - Enforces project and scene budget limits atomically under thread lock.
        - Separates subscription Flow credits from currency costs.
        - Correctly marks unknown costs (actual_cost=None, actual_cost_known=False).
        """
        with _budget_lock:
            project = db.query(Project).filter(Project.id == project_id).first()
            if not project:
                raise HTTPException(status_code=404, detail="Project not found")

            # 1. Deduplication check (Idempotency)
            p_hash = CostController.compute_hash(payload) if payload else ""
            if payload and not allow_duplicate and p_hash:
                existing = db.query(CostRecord).filter(
                    CostRecord.project_id == project_id,
                    CostRecord.prompt_hash == p_hash
                ).first()
                if existing:
                    # Return existing record immediately without deducting budget or duplicating ledger
                    return existing

            # 2. Determine provider and operation mapping
            op_name = operation or action_type
            op_meta = ACCOUNTING_OPERATIONS.get(op_name, {})
            
            resolved_provider = provider or op_meta.get("provider", "offline_engine")
            resolved_model = model or op_meta.get("default_model")
            
            # Flow credits separation
            resolved_flow_credits = flow_credits if flow_credits is not None else (
                op_meta.get("default_flow_credits", 0.0) if op_meta.get("consumes_flow_credits") else 0.0
            )

            # Determine cost to deduct from monetary budget
            # If actual cost is explicitly known, use it; otherwise use estimated_cost
            if actual_cost is not None and actual_cost_known:
                monetary_charge = max(0.0, float(actual_cost))
            elif estimated_cost is not None:
                monetary_charge = max(0.0, float(estimated_cost))
            else:
                monetary_charge = op_meta.get("default_estimated_cost", 0.0)

            # 3. Check scene budget if applicable
            if scene_id and monetary_charge > 0:
                scene = db.query(Scene).filter(Scene.id == scene_id).first()
                if scene and scene.budget_limit and scene.budget_limit > 0:
                    scene_records = db.query(CostRecord).filter(
                        CostRecord.project_id == project_id,
                        CostRecord.scene_id == scene_id
                    ).all()
                    scene_spent = sum([
                        (r.actual_cost if (r.actual_cost is not None and r.actual_cost_known) else (r.estimated_cost or r.cost_usd or 0.0))
                        for r in scene_records
                    ])
                    if scene_spent + monetary_charge > scene.budget_limit:
                        raise HTTPException(
                            status_code=400,
                            detail=f"Scene budget limit exceeded! (${scene_spent:.2f} + ${monetary_charge:.3f} > limit ${scene.budget_limit:.2f})"
                        )

            # 4. Check project budget
            if monetary_charge > 0 and (project.budget_spent + monetary_charge > project.budget_total):
                raise HTTPException(
                    status_code=400,
                    detail=f"Project budget limit reached! Spent: ${project.budget_spent:.2f}, Limit: ${project.budget_total:.2f}. Increase budget to continue."
                )

            # 5. Create ledger record
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
                details=details or f"Provider: {resolved_provider} | Op: {op_name}"
            )

            # 6. Update project spent
            project.budget_spent += monetary_charge

            db.add(record)
            db.commit()
            db.refresh(record)
            return record

    @staticmethod
    def get_project_usage_summary(db: Session, project_id: str) -> Dict[str, Any]:
        """Returns aggregated breakdown by provider, credits, tokens, and costs."""
        records = db.query(CostRecord).filter(CostRecord.project_id == project_id).all()
        
        flow_credits_total = sum([r.flow_credits or 0.0 for r in records])
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
