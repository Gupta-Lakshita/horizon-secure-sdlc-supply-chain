from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.main import get_client_id
from app.models import EvaluateRequest, EvaluateResponse, RuleResult
from app.policy.engine import evaluate_release

router = APIRouter(tags=["evaluate"])


@router.post("/runs/{run_id}/evaluate", response_model=EvaluateResponse)
async def evaluate_run(
    run_id: str,
    request: EvaluateRequest,
    client_id: str = Depends(get_client_id),
    db: AsyncSession = Depends(get_db),
) -> EvaluateResponse:
    result = await evaluate_release(
        run_id=run_id,
        client_id=client_id,
        target_environment=request.targetEnvironment,
        db=db,
    )
    return EvaluateResponse(
        decision=result["decision"],
        rules=[RuleResult(**r) for r in result["rules"]],
        blockers=result["blockers"],
        policyVersion=result["policyVersion"],
        evaluatedAt=result["evaluatedAt"],
    )


@router.get("/runs/{run_id}/evaluations")
async def list_evaluations(
    run_id: str,
    client_id: str = Depends(get_client_id),
    db: AsyncSession = Depends(get_db),
):
    from sqlalchemy import text

    row = (await db.execute(
        text("SELECT client_id FROM release_trust_runs WHERE id = :run_id"),
        {"run_id": run_id},
    )).mappings().first()

    if not row:
        raise HTTPException(status_code=404, detail=f"Release run {run_id} not found")
    if row["client_id"] != client_id:
        raise HTTPException(status_code=403, detail="client_id mismatch")

    rows = (await db.execute(
        text(
            "SELECT id, environment, policy_version, decision, evaluated_at "
            "FROM release_trust_evaluations WHERE release_run_id = :run_id AND client_id = :client_id "
            "ORDER BY evaluated_at DESC"
        ),
        {"run_id": run_id, "client_id": client_id},
    )).mappings().all()

    return [
        {
            "id": str(r["id"]),
            "environment": r["environment"],
            "policyVersion": r["policy_version"],
            "decision": r["decision"],
            "evaluatedAt": r["evaluated_at"].isoformat(),
        }
        for r in rows
    ]
