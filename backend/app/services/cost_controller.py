"""
Cost Controller Module.
Implements:
- Manual credit and budget tracking
- Per-scene and per-project generation budget enforcement
- API usage / generation limit enforcement
- Duplicate job prevention via cryptographic hashing
"""

import hashlib
import json
from typing import Optional, Tuple, Any
from sqlalchemy.orm import Session
from fastapi import HTTPException
from backend.app.models.entities import Project, Scene, CostRecord

COST_RATES = {
    "script_generation": {"credits": 1.0, "cost_usd": 0.005},
    "storyboard_generation": {"credits": 2.0, "cost_usd": 0.01},
    "flow_prompt_pack": {"credits": 1.0, "cost_usd": 0.002},
    "continuity_check": {"credits": 0.5, "cost_usd": 0.001},
    "audio_synthesis": {"credits": 1.5, "cost_usd": 0.015},
    "video_render": {"credits": 3.0, "cost_usd": 0.02},
}

class CostController:
    @staticmethod
    def compute_hash(payload: Any) -> str:
        """Computes a SHA-256 hash for deduplication."""
        raw = json.dumps(payload, sort_keys=True, default=str)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @staticmethod
    def check_and_deduplicate(db: Session, project_id: str, action_type: str, payload: Any) -> Optional[CostRecord]:
        """Checks if an identical job was already recorded to prevent duplicate expenditure."""
        p_hash = CostController.compute_hash(payload)
        existing = db.query(CostRecord).filter(
            CostRecord.project_id == project_id,
            CostRecord.action_type == action_type,
            CostRecord.prompt_hash == p_hash
        ).first()
        return existing

    @staticmethod
    def authorize_and_charge(
        db: Session,
        project_id: str,
        action_type: str,
        scene_id: Optional[str] = None,
        payload: Optional[Any] = None,
        details: str = "",
        allow_duplicate: bool = False
    ) -> CostRecord:
        """Enforces budget limits, verifies duplicate jobs, and deducts budget."""
        project = db.query(Project).filter(Project.id == project_id).first()
        if not project:
            raise HTTPException(status_code=404, detail="Project not found")

        rate = COST_RATES.get(action_type, {"credits": 1.0, "cost_usd": 0.01})
        req_cost = rate["cost_usd"]

        # 1. Duplicate check
        p_hash = CostController.compute_hash(payload) if payload else ""
        if payload and not allow_duplicate:
            existing = db.query(CostRecord).filter(
                CostRecord.project_id == project_id,
                CostRecord.action_type == action_type,
                CostRecord.prompt_hash == p_hash
            ).first()
            if existing:
                # Return existing record without charging twice
                return existing

        # 2. Check scene budget if applicable
        if scene_id:
            scene = db.query(Scene).filter(Scene.id == scene_id).first()
            if scene and scene.budget_limit:
                scene_spent = sum([
                    r.cost_usd for r in db.query(CostRecord).filter(
                        CostRecord.project_id == project_id,
                        CostRecord.scene_id == scene_id
                    ).all()
                ])
                if scene_spent + req_cost > scene.budget_limit:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Scene budget limit exceeded! (${scene_spent:.2f} + ${req_cost:.3f} > limit ${scene.budget_limit:.2f})"
                    )

        # 3. Check project budget limit
        if project.budget_spent + req_cost > project.budget_total:
            raise HTTPException(
                status_code=400,
                detail=f"Project budget limit reached! Spent: ${project.budget_spent:.2f}, Limit: ${project.budget_total:.2f}. Increase budget to continue."
            )

        # 4. Charge and record
        record = CostRecord(
            project_id=project_id,
            scene_id=scene_id,
            action_type=action_type,
            credits_used=rate["credits"],
            cost_usd=req_cost,
            prompt_hash=p_hash,
            details=details or f"Action: {action_type}"
        )
        project.budget_spent += req_cost

        db.add(record)
        db.commit()
        db.refresh(record)
        return record

cost_controller = CostController()
