from fastapi import APIRouter, Depends, HTTPException
from app.main import get_client_id

router = APIRouter(tags=["evaluate"])


@router.post("/runs/{run_id}/evaluate")
def evaluate_run(run_id: str, client_id: str = Depends(get_client_id)):
    raise HTTPException(status_code=501, detail="not implemented")


@router.get("/runs/{run_id}/evaluations")
def list_evaluations(run_id: str, client_id: str = Depends(get_client_id)):
    raise HTTPException(status_code=501, detail="not implemented")
