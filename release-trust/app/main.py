from fastapi import FastAPI
from app.deps import get_client_id  # re-export for backwards compat

app = FastAPI(title="Horizon Release Trust Service", version="1.0.0")


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
