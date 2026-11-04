from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from backend.app.database import get_db
from backend.app.models.entities import Project, CostRecord
from backend.app.schemas.entities import CostRecordResponse, BudgetSummary
from backend.app.services.cost_controller import COST_RATES

router = APIRouter(prefix="/api/cost", tags=["cost"])

@router.get("/summary/{project_id}", response_model=BudgetSummary)
def get_budget_summary(project_id: str, db: Session = Depends(get_db)):
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    records = db.query(CostRecord).filter(
        CostRecord.project_id == project_id
    ).order_by(CostRecord.created_at.desc()).all()

    spent = project.budget_spent
    total = project.budget_total
    remaining = max(0.0, total - spent)
    pct = round((spent / total * 100), 1) if total > 0 else 0.0
    thresh = getattr(project, "budget_warning_threshold", 0.8) or 0.8
    warning_triggered = (spent / total) >= thresh if total > 0 else False

    total_flow_credits = sum([r.flow_credits or 0.0 for r in records])
    provider_counts = {}
    for r in records:
        prov = r.provider or "offline_engine"
        provider_counts[prov] = provider_counts.get(prov, 0) + 1

    return {
        "project_id": project_id,
        "budget_total": round(total, 2),
        "budget_spent": round(spent, 3),
        "budget_remaining": round(remaining, 3),
        "percent_used": pct,
        "budget_warning_threshold": thresh,
        "warning_triggered": warning_triggered,
        "total_actions": len(records),
        "total_flow_credits": round(total_flow_credits, 2),
        "records": records,
        "provider_breakdown": provider_counts
    }


@router.get("/check-authorization/{project_id}")
def check_operation_authorization(
    project_id: str,
    operation: str = Query("general"),
    estimated_cost: float = Query(0.0, ge=0.0),
    db: Session = Depends(get_db)
):
    """
    Pre-flight check for cloud operations.
    Warns the user and indicates whether confirmation is recommended before consuming paid API quota.
    """
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    total = project.budget_total
    spent = project.budget_spent
    remaining = max(0.0, total - spent)
    thresh = getattr(project, "budget_warning_threshold", 0.8) or 0.8

    warning_triggered = (spent / total) >= thresh if total > 0 else False
    is_expensive = estimated_cost >= 0.10 or "video" in operation.lower()

    if estimated_cost > remaining:
        return {
            "authorized": False,
            "can_proceed": False,
            "requires_confirmation": False,
            "reason": f"Insufficient budget. Operation estimated at ${estimated_cost:.2f}, but only ${remaining:.2f} remaining.",
            "budget_total": round(total, 2),
            "budget_remaining": round(remaining, 2)
        }

    return {
        "authorized": True,
        "can_proceed": True,
        "requires_confirmation": is_expensive or warning_triggered,
        "warning_threshold_exceeded": warning_triggered,
        "estimated_cost": estimated_cost,
        "budget_total": round(total, 2),
        "budget_remaining": round(remaining, 2),
        "message": (
            f"Operation '{operation}' may consume paid API quota (${estimated_cost:.2f})."
            if is_expensive else "Operation authorized."
        )
    }


@router.post("/threshold/{project_id}")
def update_warning_threshold(
    project_id: str,
    warning_threshold: float = Query(..., ge=0.1, le=1.0),
    db: Session = Depends(get_db)
):
    """Configures project budget warning threshold (e.g. 0.8 for 80%)."""
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    project.budget_warning_threshold = warning_threshold
    db.commit()
    return {
        "project_id": project_id,
        "budget_warning_threshold": project.budget_warning_threshold,
        "message": f"Budget warning threshold set to {int(warning_threshold * 100)}%."
    }

@router.get("/rates")
def get_cost_rates():
    """Returns the accounting rates and credit ledger structure."""
    return {
        "rates": COST_RATES,
        "currency": "USD",
        "description": "Provider-aware accounting ledger. Distinguishes Flow subscription credits from API expenses and unknown costs."
    }

@router.post("/top-up/{project_id}")
def top_up_budget(project_id: str, amount: float = Query(..., gt=0), db: Session = Depends(get_db)):
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    project.budget_total += amount
    db.commit()
    return {
        "project_id": project_id,
        "new_budget_total": project.budget_total,
        "message": f"Successfully added ${amount:.2f} to budget."
    }
