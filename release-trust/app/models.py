from pydantic import BaseModel, Field
from typing import Any, Dict, List, Optional
import uuid


class CreateRunRequest(BaseModel):
    application: str
    releaseId: str
    commitSha: Optional[str] = None
    imageDigest: Optional[str] = None
    evidenceS3Prefix: Optional[str] = None
    createdBy: Optional[str] = None


class RunResponse(BaseModel):
    id: str
    application: str
    releaseId: str
    commitSha: Optional[str]
    imageDigest: Optional[str]
    status: str
    createdAt: str


class RunDetailResponse(RunResponse):
    evidenceS3Prefix: Optional[str]
    updatedAt: str
    evidence: List[Dict[str, Any]] = Field(default_factory=list)


class RecordEvidenceRequest(BaseModel):
    manifestS3Key: str
    manifestSha256: str
    imageDigest: Optional[str] = None


class RecordEvidenceResponse(BaseModel):
    releaseId: str
    evidenceCount: int
    status: str


class EvaluateRequest(BaseModel):
    targetEnvironment: str


class RuleResult(BaseModel):
    ruleId: str
    result: str  # "pass" | "warn" | "fail" | "excepted"
    severity: Optional[str] = None
    message: Optional[str] = None
    remediation: Optional[str] = None
    exceptionEligible: bool = True


class EvaluateResponse(BaseModel):
    decision: str  # "pass" | "warn" | "block"
    rules: List[RuleResult]
    blockers: List[str]
    policyVersion: str
    evaluatedAt: str


class ApprovalRequest(BaseModel):
    approvedBy: str
    targetEnvironment: str
    notes: Optional[str] = None


class ApprovalResponse(BaseModel):
    id: str
    releaseId: str
    approvedBy: str
    targetEnvironment: str
    createdAt: str


class ExceptionRequest(BaseModel):
    ruleId: str
    releaseScope: Optional[str] = None
    environmentScope: Optional[str] = None
    reason: str
    compensatingControl: Optional[str] = None
    requester: str
    issueReference: Optional[str] = None
    expiresAt: Optional[str] = None


class ExceptionApprovalRequest(BaseModel):
    approver: str
    notes: Optional[str] = None


class ExceptionRevocationRequest(BaseModel):
    revokedBy: str
    reason: str


class ExceptionResponse(BaseModel):
    id: str
    ruleId: str
    status: str
    requester: str
    approver: Optional[str]
    expiresAt: Optional[str]
    createdAt: str


class BundleResponse(BaseModel):
    releaseId: str
    imageDigest: Optional[str]
    manifestSha256: Optional[str]
    evidenceCount: int
    evidence: List[Dict[str, Any]]
    evaluations: List[Dict[str, Any]]


class PreflightRequest(BaseModel):
    targetEnvironment: str
    requestedDigest: str
    sourceEnvironment: Optional[str] = None


class PreflightResponse(BaseModel):
    allowed: bool
    decision: str
    expiresAt: str
    requestBinding: str
    policyVersion: str
    manifestDigest: str
    rules: List[RuleResult]
    blockers: List[str]
