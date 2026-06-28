from fastapi import APIRouter, Depends, HTTPException
from app.main import get_client_id

router = APIRouter(tags=["preflight"])


@router.post("/preflight")
def preflight_check(client_id: str = Depends(get_client_id)):
    raise HTTPException(status_code=501, detail="not implemented")
