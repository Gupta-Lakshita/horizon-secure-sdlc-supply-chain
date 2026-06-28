from fastapi import APIRouter, Depends, HTTPException
from app.main import get_client_id

router = APIRouter(tags=["bundle"])


@router.get("/runs/{run_id}/bundle")
def get_bundle(run_id: str, client_id: str = Depends(get_client_id)):
    raise HTTPException(status_code=501, detail="not implemented")
