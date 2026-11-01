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

    return {
        "project_id": project_id,
        "budget_total": round(total, 2),
        "budget_spent": round(spent, 3),
        "budget_remaining": round(remaining, 3),
        "percent_used": pct,
        "total_actions": len(records),
        "records": records
    }

@router.get("/rates")
def get_cost_rates():
    """Returns the cost table and credit structure."""
    return {
        "rates": COST_RATES,
        "currency": "USD",
        "description": "Transparent local studio credit and budget estimation table."
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
