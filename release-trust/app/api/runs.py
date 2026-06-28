from fastapi import APIRouter, Depends, HTTPException
from app.main import get_client_id

router = APIRouter(tags=["runs"])


@router.post("/runs")
def create_run(client_id: str = Depends(get_client_id)):
    raise HTTPException(status_code=501, detail="not implemented")


@router.get("/runs/{run_id}")
def get_run(run_id: str, client_id: str = Depends(get_client_id)):
    raise HTTPException(status_code=501, detail="not implemented")


@router.get("/runs")
def list_runs(client_id: str = Depends(get_client_id)):
    raise HTTPException(status_code=501, detail="not implemented")
