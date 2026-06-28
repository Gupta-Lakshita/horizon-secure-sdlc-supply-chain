from fastapi import FastAPI, Header, HTTPException
from typing import Optional
import os

app = FastAPI(title="Horizon Release Trust Service", version="1.0.0")


def get_client_id(x_client_id: str = Header(...)) -> str:
    """Client ID always from header (set by runner from config.client_id). Never from body."""
    if not x_client_id:
        raise HTTPException(status_code=401, detail="X-Client-Id header required")
    return x_client_id


@app.get("/healthz")
def healthz():
    return {"status": "ok", "service": "release-trust"}


# All routes in separate modules — see api/ directory
from app.api import runs, evidence, evaluate, approvals, exceptions, bundle, preflight
app.include_router(runs.router, prefix="/pipeline/api/release-trust")
app.include_router(evidence.router, prefix="/pipeline/api/release-trust")
app.include_router(evaluate.router, prefix="/pipeline/api/release-trust")
app.include_router(approvals.router, prefix="/pipeline/api/release-trust")
app.include_router(exceptions.router, prefix="/pipeline/api/release-trust")
app.include_router(bundle.router, prefix="/pipeline/api/release-trust")
app.include_router(preflight.router, prefix="/pipeline/api/release-promotion")
