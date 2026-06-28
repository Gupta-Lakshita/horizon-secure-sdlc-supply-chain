from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.main import get_client_id
from app.models import CreateRunRequest, RunDetailResponse, RunResponse

router = APIRouter(tags=["runs"])


@router.post("/runs", status_code=201, response_model=RunResponse)
async def create_run(
    request: CreateRunRequest,
    client_id: str = Depends(get_client_id),
    db: AsyncSession = Depends(get_db),
) -> RunResponse:
    try:
        row = (
            await db.execute(
                text(
                    "INSERT INTO release_trust_runs "
                    "(client_id, application, release_id, commit_sha, image_digest, evidence_s3_prefix, created_by) "
                    "VALUES (:client_id, :application, :release_id, :commit_sha, :image_digest, :s3_prefix, :created_by) "
                    "RETURNING id, application, release_id, commit_sha, image_digest, status, created_at"
                ),
                {
                    "client_id": client_id,
                    "application": request.application,
                    "release_id": request.releaseId,
                    "commit_sha": request.commitSha,
                    "image_digest": request.imageDigest,
                    "s3_prefix": request.evidenceS3Prefix,
                    "created_by": request.createdBy,
                },
            )
        ).mappings().first()

        await db.commit()

    except Exception as exc:
        await db.rollback()

        if "uq_release_runs" in str(exc):
            raise HTTPException(
                status_code=409,
                detail=f"Release run for ({client_id}, {request.application}, {request.releaseId}) already exists",
            )

        raise HTTPException(status_code=500, detail=str(exc))

    return RunResponse(
        id=str(row["id"]),
        application=row["application"],
        releaseId=row["release_id"],
        commitSha=row["commit_sha"],
        imageDigest=row["image_digest"],
        status=row["status"],
        createdAt=str(row["created_at"]) if row["created_at"] else "",
    )


@router.get("/runs/{run_id}", response_model=RunDetailResponse)
async def get_run(
    run_id: str,
    client_id: str = Depends(get_client_id),
    db: AsyncSession = Depends(get_db),
) -> RunDetailResponse:
    row = (
        await db.execute(
            text(
                "SELECT id, application, release_id, commit_sha, image_digest, "
                "evidence_s3_prefix, status, created_at, updated_at "
                "FROM release_trust_runs "
                "WHERE id = :run_id AND client_id = :client_id"
            ),
            {"run_id": run_id, "client_id": client_id},
        )
    ).mappings().first()

    if not row:
        raise HTTPException(
            status_code=404,
            detail=f"Release run {run_id} not found",
        )

    ev_rows = (
        await db.execute(
            text(
                "SELECT evidence_type, status, object_key, sha256, schema_version, created_at "
                "FROM release_trust_evidence "
                "WHERE release_run_id = :run_id AND client_id = :client_id "
                "ORDER BY created_at"
            ),
            {"run_id": run_id, "client_id": client_id},
        )
    ).mappings().all()

    evidence = [
        {
            "evidenceType": e["evidence_type"],
            "status": e["status"],
            "objectKey": e["object_key"],
            "sha256": e["sha256"],
            "schemaVersion": e["schema_version"],
            "createdAt": str(e["created_at"]) if e["created_at"] else "",
        }
        for e in ev_rows
    ]

    return RunDetailResponse(
        id=str(row["id"]),
        application=row["application"],
        releaseId=row["release_id"],
        commitSha=row["commit_sha"],
        imageDigest=row["image_digest"],
        evidenceS3Prefix=row["evidence_s3_prefix"],
        status=row["status"],
        createdAt=str(row["created_at"]) if row["created_at"] else "",
        updatedAt=str(row["updated_at"]) if row["updated_at"] else "",
        evidence=evidence,
    )


@router.get("/runs", response_model=list[RunResponse])
async def list_runs(
    application: Optional[str] = None,
    client_id: str = Depends(get_client_id),
    db: AsyncSession = Depends(get_db),
) -> list[RunResponse]:
    if application:
        rows = (
            await db.execute(
                text(
                    "SELECT id, application, release_id, commit_sha, image_digest, status, created_at "
                    "FROM release_trust_runs "
                    "WHERE client_id = :client_id "
                    "AND application = :application "
                    "ORDER BY created_at DESC "
                    "LIMIT 100"
                ),
                {
                    "client_id": client_id,
                    "application": application,
                },
            )
        ).mappings().all()
    else:
        rows = (
            await db.execute(
                text(
                    "SELECT id, application, release_id, commit_sha, image_digest, status, created_at "
                    "FROM release_trust_runs "
                    "WHERE client_id = :client_id "
                    "ORDER BY created_at DESC "
                    "LIMIT 100"
                ),
                {
                    "client_id": client_id,
                },
            )
        ).mappings().all()

    return [
        RunResponse(
            id=str(r["id"]),
            application=r["application"],
            releaseId=r["release_id"],
            commitSha=r["commit_sha"],
            imageDigest=r["image_digest"],
            status=r["status"],
            createdAt=str(r["created_at"]) if r["created_at"] else "",
        )
        for r in rows
    ]