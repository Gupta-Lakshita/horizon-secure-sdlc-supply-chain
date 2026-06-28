from fastapi import APIRouter, Depends, HTTPException
from app.main import get_client_id

router = APIRouter(tags=["exceptions"])


@router.post("/runs/{run_id}/exceptions")
def request_exception(run_id: str, client_id: str = Depends(get_client_id)):
    raise HTTPException(status_code=501, detail="not implemented")


@router.post("/runs/{run_id}/exceptions/{exception_id}/approve")
def approve_exception(run_id: str, exception_id: str, client_id: str = Depends(get_client_id)):
    raise HTTPException(status_code=501, detail="not implemented")


@router.post("/runs/{run_id}/exceptions/{exception_id}/revoke")
def revoke_exception(run_id: str, exception_id: str, client_id: str = Depends(get_client_id)):
    raise HTTPException(status_code=501, detail="not implemented")


@router.get("/runs/{run_id}/exceptions")
def list_exceptions(run_id: str, client_id: str = Depends(get_client_id)):
    raise HTTPException(status_code=501, detail="not implemented")
