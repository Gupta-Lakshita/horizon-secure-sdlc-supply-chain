from fastapi import APIRouter, Depends, HTTPException
from app.main import get_client_id

router = APIRouter(tags=["evidence"])


@router.post("/runs/{run_id}/evidence")
def record_evidence(run_id: str, client_id: str = Depends(get_client_id)):
    raise HTTPException(status_code=501, detail="not implemented")


@router.get("/runs/{run_id}/evidence")
def list_evidence(run_id: str, client_id: str = Depends(get_client_id)):
    raise HTTPException(status_code=501, detail="not implemented")
