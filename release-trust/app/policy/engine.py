"""
Policy evaluation engine for the Release Trust service.

Rules are implemented as Python functions (not OPA) so the backend can
evaluate them against DB-resident evidence and make authoritative gate decisions.
"""
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import RuleResult

POLICY_VERSION = "2026.06.1"

# Non-exception-eligible rule IDs — digest / identity / tenant violations
NON_EXCEPTED_RULES = {"rule_requires_digest", "rule_requires_same_digest"}

_PROD_ENVS = {"prod", "production"}
_NONPROD_ENVS = {"qa", "stage", "staging", "uat"}


def _env_tier(target_environment: str) -> str:
    e = target_environment.lower()
    if e in _PROD_ENVS:
        return "prod"
    if e in _NONPROD_ENVS:
        return "nonprod"
    return "dev"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Individual rule evaluators
# Each returns (result, message, remediation) where result is
# "pass" | "warn" | "fail"
# ---------------------------------------------------------------------------

def rule_requires_digest(subject: Dict[str, Any], env_tier: str) -> Tuple[str, str, str]:
    """All environments: block if no immutable image digest is recorded."""
    if not subject.get("imageDigest"):
        return (
            "fail",
            "HR-POL-RT-001 no immutable image digest recorded for this release",
            "Run release.trust.resolve_digest to record the immutable sha256 digest before promoting",
        )
    return "pass", "", ""


def rule_requires_sbom(evidence: Dict[str, str], env_tier: str) -> Tuple[str, str, str]:
    """Warn DEV; block QA/STAGE/PROD if SBOM is not present."""
    sbom_status = evidence.get("sbom", "missing")
    if sbom_status == "present":
        return "pass", "", ""
    if env_tier == "dev":
        return (
            "warn",
            "HR-POL-RT-002 SBOM evidence is missing (warn in dev)",
            "Run release.trust.generate_sbom to generate CycloneDX SBOM evidence",
        )
    return (
        "fail",
        f"HR-POL-RT-002 SBOM evidence is required for {env_tier} promotion but is missing",
        "Run release.trust.generate_sbom before promoting to this environment",
    )


def rule_blocks_critical_cve(evidence: Dict[str, Any], env_tier: str) -> Tuple[str, str, str]:
    """Warn DEV; block all other envs if critical runtime CVEs exist."""
    critical_count = evidence.get("criticalRuntimeCves", 0)
    if not isinstance(critical_count, int):
        critical_count = 0
    if critical_count == 0:
        return "pass", "", ""
    if env_tier == "dev":
        return (
            "warn",
            f"HR-POL-RT-CVE {critical_count} critical CVE(s) detected (warn in dev)",
            "Review and remediate critical vulnerabilities before promoting beyond dev",
        )
    return (
        "fail",
        f"HR-POL-RT-CVE {critical_count} critical CVE(s) block promotion to {env_tier}",
        "Remediate all critical CVEs or file an approved exception before promoting",
    )


def rule_requires_signature(evidence: Dict[str, str], env_tier: str) -> Tuple[str, str, str]:
    """Observe DEV/QA; warn STAGE; block PROD if signature is not valid."""
    sig_status = evidence.get("signature", "missing")
    if sig_status == "valid":
        return "pass", "", ""
    if env_tier == "dev":
        return "pass", "", ""
    if env_tier == "nonprod":
        return (
            "warn",
            "HR-POL-RT-003 image signature is not present (warn in non-prod)",
            "Run release.trust.sign_image to sign the image before production promotion",
        )
    return (
        "fail",
        "HR-POL-RT-003 production deployment requires a valid image signature",
        "Run release.trust.sign_image with a valid KMS key before promoting to production",
    )


def rule_requires_same_digest(
    subject_digest: Optional[str],
    requested_digest: Optional[str],
    env_tier: str,
) -> Tuple[str, str, str]:
    """Block QA/STAGE/PROD if the requested digest differs from the approved digest."""
    if env_tier == "dev":
        return "pass", "", ""
    if not subject_digest or not requested_digest:
        return "pass", "", ""
    if subject_digest == requested_digest:
        return "pass", "", ""
    return (
        "fail",
        f"HR-POL-RT-006 requested digest {requested_digest} does not match approved digest {subject_digest}",
        "Ensure the deployed image digest matches the digest recorded during evidence collection",
    )


# ---------------------------------------------------------------------------
# Main evaluation entry point
# ---------------------------------------------------------------------------

async def evaluate_release(
    run_id: str,
    client_id: str,
    target_environment: str,
    db: AsyncSession,
    requested_digest: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Evaluate all policy rules for a release run.

    1. Load release record (403 if client_id mismatch)
    2. Load evidence
    3. Load active exceptions
    4. Run rules
    5. Apply exceptions to eligible failures
    6. Persist evaluation + rule results
    7. Return decision dict
    """
    # 1. Load release record
    row = (await db.execute(
        text(
            "SELECT id, client_id, application, release_id, image_digest, status "
            "FROM release_trust_runs WHERE id = :run_id"
        ),
        {"run_id": run_id},
    )).mappings().first()

    if not row:
        raise HTTPException(status_code=404, detail=f"Release run {run_id} not found")
    if row["client_id"] != client_id:
        raise HTTPException(status_code=403, detail="client_id mismatch")

    subject = {
        "clientId": client_id,
        "application": row["application"],
        "releaseId": row["release_id"],
        "imageDigest": row["image_digest"],
    }

    # 2. Load evidence summary
    ev_rows = (await db.execute(
        text(
            "SELECT evidence_type, status, summary_json FROM release_trust_evidence "
            "WHERE release_run_id = :run_id AND client_id = :client_id"
        ),
        {"run_id": run_id, "client_id": client_id},
    )).mappings().all()

    evidence_map: Dict[str, Any] = {}
    for ev in ev_rows:
        evidence_map[ev["evidence_type"]] = ev["status"]
        if ev["summary_json"] and isinstance(ev["summary_json"], dict):
            evidence_map.update(ev["summary_json"])

    # 3. Load active exceptions
    now_ts = _now_iso()
    exc_rows = (await db.execute(
        text(
            "SELECT rule_id, environment_scope, expires_at FROM release_trust_exceptions "
            "WHERE release_run_id = :run_id AND client_id = :client_id "
            "AND status = 'approved' AND (expires_at IS NULL OR expires_at > :now)"
        ),
        {"run_id": run_id, "client_id": client_id, "now": now_ts},
    )).mappings().all()

    env_tier = _env_tier(target_environment)

    def _exception_covers(rule_id: str) -> bool:
        if rule_id in NON_EXCEPTED_RULES:
            return False
        for exc in exc_rows:
            scope = (exc["environment_scope"] or "").lower()
            if exc["rule_id"] == rule_id and (not scope or scope == target_environment.lower()):
                return True
        return False

    # 4. Evaluate rules
    evaluated_at = _now_iso()
    raw_results: List[Tuple[str, str, str, str, bool]] = []  # (rule_id, raw_result, message, remediation, exception_eligible)

    for rule_id, (raw, msg, rem) in [
        ("rule_requires_digest", rule_requires_digest(subject, env_tier)),
        ("rule_requires_sbom", rule_requires_sbom(evidence_map, env_tier)),
        ("rule_blocks_critical_cve", rule_blocks_critical_cve(evidence_map, env_tier)),
        ("rule_requires_signature", rule_requires_signature(evidence_map, env_tier)),
        (
            "rule_requires_same_digest",
            rule_requires_same_digest(subject.get("imageDigest"), requested_digest, env_tier),
        ),
    ]:
        eligible = rule_id not in NON_EXCEPTED_RULES
        raw_results.append((rule_id, raw, msg, rem, eligible))

    # 5. Apply exceptions
    rule_results: List[RuleResult] = []
    for rule_id, raw, msg, rem, eligible in raw_results:
        if raw == "fail" and eligible and _exception_covers(rule_id):
            result = "excepted"
        else:
            result = raw
        rule_results.append(RuleResult(
            ruleId=rule_id,
            result=result,
            severity="error" if result == "fail" else ("warning" if result == "warn" else None),
            message=msg or None,
            remediation=rem or None,
            exceptionEligible=eligible,
        ))

    # 6. Overall decision
    results = {r.result for r in rule_results}
    if "fail" in results:
        decision = "block"
    elif "warn" in results:
        decision = "warn"
    else:
        decision = "pass"

    blockers = [r.message for r in rule_results if r.result == "fail" and r.message]

    # 7. Persist evaluation
    eval_row = (await db.execute(
        text(
            "INSERT INTO release_trust_evaluations "
            "(release_run_id, client_id, environment, policy_version, decision, evaluated_at) "
            "VALUES (:run_id, :client_id, :env, :pv, :decision, :at) RETURNING id"
        ),
        {
            "run_id": run_id,
            "client_id": client_id,
            "env": target_environment,
            "pv": POLICY_VERSION,
            "decision": decision,
            "at": evaluated_at,
        },
    )).mappings().first()
    eval_id = str(eval_row["id"])

    for r in rule_results:
        await db.execute(
            text(
                "INSERT INTO release_trust_rule_results "
                "(evaluation_id, client_id, rule_id, result, severity, message, remediation, exception_eligible) "
                "VALUES (:eval_id, :client_id, :rule_id, :result, :severity, :message, :remediation, :eligible)"
            ),
            {
                "eval_id": eval_id,
                "client_id": client_id,
                "rule_id": r.ruleId,
                "result": r.result,
                "severity": r.severity,
                "message": r.message,
                "remediation": r.remediation,
                "eligible": r.exceptionEligible,
            },
        )

    await db.commit()

    return {
        "decision": decision,
        "rules": [r.model_dump() for r in rule_results],
        "blockers": blockers,
        "policyVersion": POLICY_VERSION,
        "evaluatedAt": evaluated_at,
        "evaluationId": eval_id,
    }
