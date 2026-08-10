import base64
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import requests
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field


app = FastAPI(title="Horizon Thin Runner", version="0.1.2")


class RunnerRequest(BaseModel):
    pipelineType: Optional[str] = None
    pipelineKind: Optional[str] = None
    serviceName: Optional[str] = None
    requestId: str = Field(default_factory=lambda: str(uuid.uuid4()))
    clientId: Optional[str] = None
    installationId: Optional[str] = None
    requestedBy: Optional[str] = None
    jobName: Optional[str] = None
    buildNumber: Optional[str] = None
    buildUrl: Optional[str] = None
    executionMode: Optional[str] = None
    executionStage: Optional[str] = None
    stageName: Optional[str] = None
    parameters: Dict[str, Any] = Field(default_factory=dict)
    payload: Dict[str, Any] = Field(default_factory=dict)


class RunnerResponse(BaseModel):
    status: str
    requestId: str
    planId: Optional[str] = None
    executionStage: Optional[str] = None
    message: str
    executedActions: List[str] = Field(default_factory=list)
    availableStages: List[str] = Field(default_factory=list)
    reportSummary: Optional[Dict[str, Any]] = None


class RunnerConfig:
    client_id = os.getenv("HORIZON_CLIENT_ID", "")
    installation_id = os.getenv("HORIZON_INSTALLATION_ID", "")
    activation_token = os.getenv("HORIZON_ACTIVATION_TOKEN", "")
    plan_endpoint = os.getenv("HORIZON_EXECUTION_PLAN_ENDPOINT", "")
    event_endpoint = os.getenv("HORIZON_EXECUTION_EVENT_ENDPOINT", "")
    public_key_path = os.getenv("HORIZON_PUBLIC_KEY_PATH", "")
    allow_shell = os.getenv("HORIZON_RUNNER_ALLOW_SHELL", "false").lower() == "true"
    execute_actions = os.getenv("HORIZON_RUNNER_EXECUTE", "true").lower() == "true"
    work_dir = Path(os.getenv("HORIZON_RUNNER_WORK_DIR", os.getenv("HORIZON_RUNNER_WORKDIR", "/var/lib/horizon-runner")))
    timeout_seconds = int(os.getenv("HORIZON_RUNNER_ACTION_TIMEOUT_SECONDS", "1800"))
    kaniko_image = os.getenv("HORIZON_KANIKO_IMAGE", "gcr.io/kaniko-project/executor:v1.23.2")
    keep_build_jobs = os.getenv("HORIZON_RUNNER_KEEP_BUILD_JOBS", "false").lower() == "true"
    namespace = os.getenv("HORIZON_RUNNER_NAMESPACE", os.getenv("POD_NAMESPACE", "default"))
    ui_test_isolated = os.getenv("HORIZON_UI_TEST_ISOLATED", "false").lower() == "true"
    ui_test_image = os.getenv("HORIZON_UI_TEST_IMAGE", "mcr.microsoft.com/playwright:v1.44.1-jammy")
    findings_upload_url = os.getenv("HORIZON_FINDINGS_UPLOAD_URL", "http://horizon-backend:8000/upload_vulnerabilities")
    findings_upload_token = os.getenv("HORIZON_FINDINGS_UPLOAD_TOKEN", "")
    security_fail_on_severity = os.getenv("HORIZON_SECURITY_FAIL_ON_SEVERITY", "CRITICAL,HIGH")
    security_fail_on_dashboard_upload = os.getenv("HORIZON_SECURITY_FAIL_ON_DASHBOARD_UPLOAD", "false").lower() == "true"
    policy_bundle_dir = Path(os.getenv("HORIZON_POLICY_BUNDLE_DIR", str(Path(__file__).resolve().parent / "policy_bundles")))
    policy_bundle_mode = os.getenv("HORIZON_POLICY_BUNDLE_MODE", "managed")


config = RunnerConfig()
config.work_dir.mkdir(parents=True, exist_ok=True)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def safe_file_token(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in ("-", "_", ".") else "-" for ch in value)[:160] or str(uuid.uuid4())


def current_namespace() -> str:
    path = Path("/var/run/secrets/kubernetes.io/serviceaccount/namespace")
    if path.exists():
        return path.read_text().strip()
    return os.getenv("POD_NAMESPACE", "default")


def action_workspace(request_id: str) -> Path:
    path = config.work_dir / "runs" / safe_file_token(request_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def runner_context_path(request_id: str) -> Path:
    return action_workspace(request_id) / "runner-context.json"


def load_runner_context(request: RunnerRequest) -> Dict[str, Any]:
    run_dir = action_workspace(request.requestId)
    payload = request.payload or request.parameters or {}
    context: Dict[str, Any] = {
        "requestId": request.requestId,
        "runDir": run_dir,
        "project": {"name": str(payload.get("PROJECT_NAME") or request.jobName or request.requestId)},
        "requestPayload": payload,
        "runner": {
            "clientId": request.clientId or payload.get("CLIENT_ID") or config.client_id,
            "installationId": request.installationId or payload.get("INSTALLATION_ID") or config.installation_id,
            "jobName": request.jobName,
            "buildNumber": request.buildNumber,
            "buildUrl": request.buildUrl,
        },
    }
    path = runner_context_path(request.requestId)
    if path.exists():
        try:
            saved = json.loads(path.read_text())
        except json.JSONDecodeError:
            saved = {}
        context.update(saved)
        context["runDir"] = Path(context.get("runDir") or run_dir)
        if context.get("sourceDir"):
            context["sourceDir"] = Path(context["sourceDir"])
        context["requestPayload"] = payload
        context["project"] = {"name": str(payload.get("PROJECT_NAME") or context.get("project", {}).get("name") or request.jobName or request.requestId)}
        context["runner"] = {
            **(context.get("runner") or {}),
            "clientId": request.clientId or payload.get("CLIENT_ID") or config.client_id,
            "installationId": request.installationId or payload.get("INSTALLATION_ID") or config.installation_id,
            "jobName": request.jobName,
            "buildNumber": request.buildNumber,
            "buildUrl": request.buildUrl,
        }
    return context


def save_runner_context(context: Dict[str, Any]) -> None:
    path = runner_context_path(context["requestId"])
    serializable: Dict[str, Any] = {}
    for key in ("requestId", "project", "requestPayload", "runner", "git", "image", "artifact"):
        if key in context:
            serializable[key] = context[key]
    serializable["runDir"] = str(context["runDir"])
    if context.get("sourceDir"):
        serializable["sourceDir"] = str(context["sourceDir"])
    path.write_text(json.dumps(serializable, indent=2, sort_keys=True))


def command_text(args: List[str]) -> str:
    return " ".join(args)


def run_command(
    args: List[str],
    *,
    cwd: Optional[Path] = None,
    env: Optional[Dict[str, str]] = None,
    input_text: Optional[str] = None,
    timeout: Optional[int] = None,
    check: bool = True,
    log_output: bool = True,
) -> subprocess.CompletedProcess:
    print(f"$ {command_text(args)}", flush=True)
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    result = subprocess.run(
        args,
        cwd=str(cwd) if cwd else None,
        env=merged_env,
        input=input_text,
        text=True,
        capture_output=True,
        timeout=timeout or config.timeout_seconds,
    )
    if log_output and result.stdout:
        print(result.stdout, flush=True)
    if log_output and result.stderr:
        print(result.stderr, flush=True)
    if check and result.returncode != 0:
        raise HTTPException(status_code=500, detail=f"Action command failed ({result.returncode}): {command_text(args)}")
    return result


def command_output_tail(result: subprocess.CompletedProcess, max_chars: int = 4000) -> str:
    output = "\n".join(part for part in [result.stdout, result.stderr] if part)
    return output[-max_chars:] if output else ""


def render_value(value: Any, context: Dict[str, Any]) -> Any:
    if isinstance(value, dict):
        return {key: render_value(item, context) for key, item in value.items()}
    if isinstance(value, list):
        return [render_value(item, context) for item in value]
    if not isinstance(value, str):
        return value

    replacements = {
        "{{git.shortSha}}": context.get("git", {}).get("shortSha", ""),
        "{{git.commitSha}}": context.get("git", {}).get("commitSha", ""),
        "{{project.name}}": context.get("project", {}).get("name", ""),
        "{{image.tag}}": context.get("image", {}).get("tag", ""),
        "{{image.uri}}": context.get("image", {}).get("uri", ""),
        "{{image.uriWithDigest}}": context.get("image", {}).get("uriWithDigest", ""),
        "{{artifact.prefix}}": context.get("artifact", {}).get("prefix", ""),
    }
    for token, replacement in replacements.items():
        value = value.replace(token, replacement)
    return value


def safe_k8s_name(value: str) -> str:
    cleaned = "".join(ch.lower() if ch.isalnum() else "-" for ch in value)
    cleaned = "-".join(part for part in cleaned.split("-") if part)
    return cleaned[:63] or f"horizon-{uuid.uuid4().hex[:8]}"


def as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def csv_values(value: Any) -> List[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [item.strip() for item in str(value or "").split(",") if item.strip()]


def get_source_dir(context: Dict[str, Any]) -> Path:
    source_dir = context.get("sourceDir")
    if source_dir:
        return Path(source_dir)
    fallback = context["runDir"] / "source"
    if fallback.exists():
        return fallback
    raise HTTPException(status_code=422, detail="Source checkout is required before this action")


def action_report_dir(action: Dict[str, Any], context: Dict[str, Any], default_name: str) -> Path:
    path = context["runDir"] / (action.get("reportDir") or f"reports/{default_name}")
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True))


def relative_path(path: Path, base: Path) -> str:
    try:
        return str(path.resolve().relative_to(base.resolve()))
    except ValueError:
        return str(path)


def xml_local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def parse_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def junit_case_status(case: ET.Element) -> str:
    for child in list(case):
        name = xml_local_name(child.tag)
        if name == "failure":
            return "FAILED"
        if name == "error":
            return "ERROR"
        if name == "skipped":
            return "SKIPPED"
    return "PASSED"


def parse_junit_reports(report_dir: Path, context: Dict[str, Any], max_cases: int = 100) -> Dict[str, Any]:
    reports: List[Dict[str, Any]] = []
    test_cases: List[Dict[str, Any]] = []
    total = failed = errored = skipped = passed = 0
    duration_seconds = 0.0

    for xml_path in sorted(report_dir.rglob("*.xml")):
        try:
            root = ET.parse(xml_path).getroot()
        except (ET.ParseError, OSError):
            continue

        suites = [item for item in root.iter() if xml_local_name(item.tag) == "testsuite"]
        cases = [item for item in root.iter() if xml_local_name(item.tag) == "testcase"]
        report_total = len(cases) or sum(parse_int(suite.attrib.get("tests")) for suite in suites)
        report_failed = 0
        report_errored = 0
        report_skipped = 0
        report_duration = 0.0

        for suite in suites:
            if not cases:
                report_failed += parse_int(suite.attrib.get("failures"))
                report_errored += parse_int(suite.attrib.get("errors"))
                report_skipped += parse_int(suite.attrib.get("skipped"))
            report_duration += parse_float(suite.attrib.get("time"))

        for case in cases:
            status = junit_case_status(case)
            case_duration = parse_float(case.attrib.get("time"))
            report_duration += case_duration if not suites else 0.0
            if status == "FAILED":
                report_failed += 1
            elif status == "ERROR":
                report_errored += 1
            elif status == "SKIPPED":
                report_skipped += 1
            if len(test_cases) < max_cases:
                failure_text = ""
                for child in list(case):
                    if xml_local_name(child.tag) in {"failure", "error"}:
                        failure_text = (child.attrib.get("message") or child.text or "").strip()
                        break
                test_cases.append({
                    "name": case.attrib.get("name") or "unnamed test",
                    "className": case.attrib.get("classname") or "",
                    "status": status,
                    "durationSeconds": round(case_duration, 3),
                    "failure": failure_text[:1000],
                })

        report_passed = max(report_total - report_failed - report_errored - report_skipped, 0)
        reports.append({
            "path": relative_path(xml_path, context["runDir"]),
            "tests": report_total,
            "passed": report_passed,
            "failed": report_failed,
            "errors": report_errored,
            "skipped": report_skipped,
            "durationSeconds": round(report_duration, 3),
        })
        total += report_total
        failed += report_failed
        errored += report_errored
        skipped += report_skipped
        passed += report_passed
        duration_seconds += report_duration

    return {
        "junitReports": reports,
        "total": total,
        "passed": passed,
        "failed": failed,
        "errors": errored,
        "skipped": skipped,
        "durationSeconds": round(duration_seconds, 3),
        "testCases": test_cases,
        "testCaseLimit": max_cases,
    }


def report_artifacts(report_dir: Path, context: Dict[str, Any]) -> Dict[str, Any]:
    artifacts: List[Dict[str, str]] = []
    typed_patterns = [
        ("junit", "*.xml"),
        ("json", "*.json"),
        ("html", "html-report/index.html"),
        ("html", "**/index.html"),
        ("screenshot", "**/*.png"),
        ("video", "**/*.webm"),
        ("trace", "**/*.zip"),
        ("sarif", "**/*.sarif"),
    ]
    seen = set()
    for artifact_type, pattern in typed_patterns:
        for path in sorted(report_dir.glob(pattern)):
            if not path.is_file():
                continue
            key = str(path.resolve())
            if key in seen:
                continue
            seen.add(key)
            artifacts.append({
                "type": artifact_type,
                "path": relative_path(path, context["runDir"]),
            })
            if len(artifacts) >= 100:
                break
    counts: Dict[str, int] = {}
    for artifact in artifacts:
        counts[artifact["type"]] = counts.get(artifact["type"], 0) + 1
    return {"items": artifacts, "counts": counts}


def sync_relative_report_dir(source_dir: Path, report_dir: Path, action: Dict[str, Any]) -> None:
    configured = Path(str(action.get("reportDir") or "reports/selenium"))
    if configured.is_absolute():
        return
    source_report_dir = source_dir / configured
    if not source_report_dir.exists():
        return
    if source_report_dir.resolve() == report_dir.resolve():
        return
    shutil.copytree(source_report_dir, report_dir, dirs_exist_ok=True)


def extract_report_archive_from_logs(logs: str, report_dir: Path) -> bool:
    match = re.search(r"__HORIZON_REPORT_TGZ_BEGIN__\s*(.*?)\s*__HORIZON_REPORT_TGZ_END__", logs, re.S)
    if not match:
        return False
    archive_text = re.sub(r"\s+", "", match.group(1))
    if not archive_text:
        return False
    archive_path = report_dir / "report-archive.tgz"
    archive_path.write_bytes(base64.b64decode(archive_text))
    run_command(["tar", "-xzf", str(archive_path), "-C", str(report_dir)], check=False)
    archive_path.unlink(missing_ok=True)
    return True


def execute_isolated_node_ui_test(source_dir: Path, report_dir: Path, target_url: str, script: str, context: Dict[str, Any]) -> subprocess.CompletedProcess:
    git_context = context.get("git") or {}
    repo_url = git_context.get("repoUrl")
    branch = git_context.get("branch") or "main"
    if not repo_url:
        raise HTTPException(status_code=422, detail="Isolated UI test execution requires a checked-out Git repository URL.")

    job_name = safe_file_token(f"horizon-ui-{context['requestId']}")[:55].strip("-")
    report_subdir = relative_path(report_dir, context["runDir"])
    script_body = f"""
set -eu
work=/workspace/source
report_dir=/workspace/{shlex.quote(report_subdir)}
rm -rf "$work"
mkdir -p "$work" "$report_dir"
git clone --depth 1 --branch {shlex.quote(branch)} {shlex.quote(repo_url)} "$work"
cd "$work"
export TARGET_APP_URL={shlex.quote(target_url)}
export APPLICATION_URL={shlex.quote(target_url)}
export APP_URL={shlex.quote(target_url)}
export SELENIUM_REPORT_DIR="$report_dir"
export CI=true
if [ -f package-lock.json ]; then npm ci; else npm install; fi
set +e
npm run {shlex.quote(script)}
status=$?
set -e
mkdir -p "$report_dir"
echo __HORIZON_REPORT_TGZ_BEGIN__
tar -C "$report_dir" -czf - . 2>/dev/null | base64 | tr -d '\n'
echo
echo __HORIZON_REPORT_TGZ_END__
exit "$status"
""".strip()
    manifest = {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": job_name, "namespace": config.namespace, "labels": {"app": "horizon-ui-test", "managed-by": "horizon-runner"}},
        "spec": {
            "backoffLimit": 0,
            "ttlSecondsAfterFinished": 600,
            "template": {
                "metadata": {"labels": {"job-name": job_name, "app": "horizon-ui-test"}},
                "spec": {
                    "restartPolicy": "Never",
                    "serviceAccountName": os.getenv("HORIZON_RUNNER_SERVICE_ACCOUNT", "jenkins"),
                    "containers": [{
                        "name": "ui-test",
                        "image": config.ui_test_image,
                        "imagePullPolicy": "IfNotPresent",
                        "command": ["/bin/sh", "-lc", script_body],
                    }],
                },
            },
        },
    }
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
        json.dump(manifest, handle)
        manifest_path = handle.name
    try:
        run_command(["kubectl", "delete", "job", job_name, "-n", config.namespace, "--ignore-not-found=true"], check=False)
        run_command(["kubectl", "apply", "-f", manifest_path])
        status = run_command(["kubectl", "wait", f"job/{job_name}", "-n", config.namespace, "--for=condition=complete", "--timeout=1800s"], check=False, timeout=1860)
        if status.returncode != 0:
            failed = run_command(["kubectl", "wait", f"job/{job_name}", "-n", config.namespace, "--for=condition=failed", "--timeout=5s"], check=False, timeout=10)
            if failed.returncode != 0:
                print(f"UI test job {job_name} did not complete cleanly before timeout.", flush=True)
        logs = run_command(["kubectl", "logs", f"job/{job_name}", "-n", config.namespace], check=False)
        extract_report_archive_from_logs((logs.stdout or "") + "\n" + (logs.stderr or ""), report_dir)
        return subprocess.CompletedProcess(args=["kubectl", "job", job_name], returncode=status.returncode, stdout=logs.stdout, stderr=logs.stderr)
    finally:
        Path(manifest_path).unlink(missing_ok=True)
        if not config.keep_build_jobs:
            run_command(["kubectl", "delete", "job", job_name, "-n", config.namespace, "--ignore-not-found=true"], check=False)


def print_quality_summary(summary: Dict[str, Any]) -> None:
    print("== Horizon validation evidence ==", flush=True)
    print(f"Tool: {summary.get('toolName') or summary.get('tool')}", flush=True)
    print(f"Status: {summary.get('status')}", flush=True)
    if summary.get("kind") == "security":
        counts = summary.get("severityCounts") or {}
        upload = summary.get("dashboardUpload") or {}
        print(
            "Findings: total={total} blocking={blocking} critical={critical} high={high} medium={medium} low={low} unknown={unknown}".format(
                total=summary.get("findingCount", 0),
                blocking=summary.get("blockingFindingCount", 0),
                critical=counts.get("CRITICAL", 0),
                high=counts.get("HIGH", 0),
                medium=counts.get("MEDIUM", 0),
                low=counts.get("LOW", 0),
                unknown=counts.get("UNKNOWN", 0),
            ),
            flush=True,
        )
        if summary.get("tools"):
            print(f"Tools: {', '.join(summary['tools'])}", flush=True)
        print(
            "Dashboard upload: {status} endpoint={endpoint} uploaded={uploaded}".format(
                status=upload.get("status", "NOT_CONFIGURED"),
                endpoint=upload.get("endpoint", ""),
                uploaded=upload.get("uploadedCount", 0),
            ),
            flush=True,
        )
        for failure in summary.get("thresholdFailures") or []:
            print(f"Threshold: {failure}", flush=True)
        for finding in (summary.get("sampleFindings") or [])[:10]:
            print(
                " - [{severity}] {category} {target} {rule}: {title}".format(
                    severity=finding.get("severity", "UNKNOWN"),
                    category=finding.get("source", finding.get("category", "Security")),
                    target=finding.get("target", ""),
                    rule=finding.get("vulnerability_id") or finding.get("rule") or finding.get("ruleId") or "",
                    title=finding.get("description", "")[:180],
                ),
                flush=True,
            )
        artifacts = summary.get("artifacts", {}).get("counts", {})
        if artifacts:
            print(f"Artifacts: {artifacts}", flush=True)
        return
    if summary.get("targetAppUrl"):
        print(f"Target: {summary['targetAppUrl']}", flush=True)
    if summary.get("baseUrl"):
        print(f"API Base URL: {summary['baseUrl']}", flush=True)
    if summary.get("collection"):
        print(f"Collection: {summary['collection']}", flush=True)
    if summary.get("environment"):
        print(f"Environment: {summary['environment']}", flush=True)
    print(
        "Tests: total={total} passed={passed} failed={failed} errors={errors} skipped={skipped} duration={duration}s".format(
            total=summary.get("totalTests", 0),
            passed=summary.get("passedTests", 0),
            failed=summary.get("failedTests", 0),
            errors=summary.get("errorTests", 0),
            skipped=summary.get("skippedTests", 0),
            duration=summary.get("durationSeconds", 0),
        ),
        flush=True,
    )
    for case in (summary.get("testCases") or [])[:20]:
        label = f"{case.get('className') + ' - ' if case.get('className') else ''}{case.get('name')}"
        print(f" - [{case.get('status')}] {label}", flush=True)
    for request in (summary.get("requests") or [])[:20]:
        method = request.get("method") or "HTTP"
        status_code = request.get("statusCode") or "n/a"
        latency = request.get("responseTimeMs")
        assertions = request.get("assertions") or {}
        print(
            " - [{status}] {method} {name} -> {code} ({latency}ms, assertions {passed}/{total})".format(
                status=request.get("status", "UNKNOWN"),
                method=method,
                name=request.get("name") or request.get("url") or "request",
                code=status_code,
                latency=latency if latency is not None else "n/a",
                passed=assertions.get("passed", 0),
                total=assertions.get("total", 0),
            ),
            flush=True,
        )
    artifacts = summary.get("artifacts", {}).get("counts", {})
    if artifacts:
        print(f"Artifacts: {artifacts}", flush=True)


def collect_report_summaries(context: Dict[str, Any], execution_stage: str = "") -> List[Dict[str, Any]]:
    report_root = context["runDir"] / "reports"
    if not report_root.exists():
        return []
    summaries: List[Dict[str, Any]] = []
    for path in sorted(report_root.rglob("summary.json")):
        try:
            summary = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        summary.setdefault("reportDir", relative_path(path.parent, context["runDir"]))
        if execution_stage and normalize_execution_stage(str(summary.get("stage") or "")) != execution_stage:
            continue
        artifact = context.get("artifact", {})
        if artifact.get("bucket") and artifact.get("prefix"):
            summary["s3Uri"] = f"s3://{artifact['bucket']}/{artifact['prefix'].strip('/')}/test-results/{relative_path(path.parent, report_root)}"
        summaries.append(summary)
    return summaries


def write_evidence_index(report_root: Path, context: Dict[str, Any], bucket: str, prefix: str) -> None:
    summaries = collect_report_summaries(context)
    index = {
        "status": "COMPLETED",
        "requestId": context["requestId"],
        "project": context.get("project", {}),
        "git": context.get("git", {}),
        "generatedAt": utc_now().isoformat(),
        "s3Prefix": f"s3://{bucket}/{prefix}/test-results/",
        "reports": summaries,
    }
    write_json(report_root / "evidence-index.json", index)


def normalize_http_url(value: Any) -> str:
    url = str(value or "").strip()
    if url and not re.match(r"(?i)^https?://", url):
        return f"http://{url}"
    return url


def ecr_host(registry: str, region: str) -> str:
    registry = registry.strip()
    if ".dkr.ecr." in registry:
        return registry
    return f"{registry}.dkr.ecr.{region}.amazonaws.com"


def assume_role_env(role_arn: str, region: str, session_name: str) -> Dict[str, str]:
    if not role_arn:
        return {"AWS_REGION": region, "AWS_DEFAULT_REGION": region}
    result = run_command(
        [
            "aws",
            "sts",
            "assume-role",
            "--region",
            region,
            "--role-arn",
            role_arn,
            "--role-session-name",
            safe_file_token(session_name)[:64],
            "--query",
            "Credentials",
            "--output",
            "json",
        ],
        timeout=60,
        log_output=False,
    )
    creds = json.loads(result.stdout)
    return {
        "AWS_REGION": region,
        "AWS_DEFAULT_REGION": region,
        "AWS_ACCESS_KEY_ID": creds["AccessKeyId"],
        "AWS_SECRET_ACCESS_KEY": creds["SecretAccessKey"],
        "AWS_SESSION_TOKEN": creds["SessionToken"],
    }


def detect_npm_script(source_dir: Path, candidates: List[str]) -> Optional[str]:
    package_json = source_dir / "package.json"
    if not package_json.exists():
        return None
    scripts = json.loads(package_json.read_text()).get("scripts", {})
    for script in candidates:
        if script in scripts:
            return script
    return None


def load_public_key():
    if not config.public_key_path:
        return None
    path = Path(config.public_key_path)
    if not path.exists():
        raise HTTPException(status_code=500, detail="Configured public key path does not exist")
    return serialization.load_pem_public_key(path.read_bytes())


def verify_plan_signature(plan: Dict[str, Any]) -> None:
    signature = plan.get("signature")
    signed_payload = plan.get("signedPayload") or plan.get("plan")
    if not signature or not signed_payload:
        raise HTTPException(status_code=502, detail="Execution plan is missing signature or signed payload")

    public_key = load_public_key()
    if public_key is None:
        raise HTTPException(status_code=500, detail="Execution plan public key is not configured")

    signature_bytes = base64.b64decode(signature)
    payload_bytes = canonical_json(signed_payload)
    try:
        public_key.verify(signature_bytes, payload_bytes, padding.PKCS1v15(), hashes.SHA256())
    except InvalidSignature:
        try:
            public_key.verify(
                signature_bytes,
                payload_bytes,
                padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH),
                hashes.SHA256(),
            )
        except InvalidSignature as exc:
            raise HTTPException(status_code=502, detail="Execution plan signature verification failed") from exc
    except ValueError as exc:
        raise HTTPException(status_code=502, detail="Execution plan signature could not be decoded") from exc


def normalize_request(request: RunnerRequest) -> Dict[str, Any]:
    body = request.model_dump(exclude_none=True)
    body["pipelineType"] = body.get("pipelineType") or body.get("pipelineKind") or "BUILD_DEPLOY"
    body["clientId"] = body.get("clientId") or config.client_id
    body["installationId"] = body.get("installationId") or config.installation_id
    body["payload"] = body.get("payload") or body.get("parameters") or {}
    return body


def validate_plan(plan: Dict[str, Any]) -> Dict[str, Any]:
    signed_payload = plan.get("signedPayload") or plan.get("plan") or plan
    expires_at = signed_payload.get("expiresAt") or signed_payload.get("expires_at")
    if expires_at and parse_time(expires_at) < utc_now():
        raise HTTPException(status_code=410, detail="Execution plan has expired")
    verify_plan_signature(plan)
    return signed_payload


def request_execution_plan(request: RunnerRequest) -> Dict[str, Any]:
    if not config.plan_endpoint:
        raise HTTPException(status_code=503, detail="Horizon execution plan endpoint is not configured")

    headers = {"Content-Type": "application/json"}
    if config.activation_token:
        headers["Authorization"] = f"Bearer {config.activation_token}"

    try:
        response = requests.post(config.plan_endpoint, json=normalize_request(request), headers=headers, timeout=30)
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"Unable to reach Horizon execution service: {exc}") from exc

    if response.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail=f"Horizon execution service rejected request: {response.status_code} {response.text}",
        )
    plan = response.json()
    (config.work_dir / f"plan-{safe_file_token(request.requestId)}.json").write_text(json.dumps(plan, indent=2))
    return plan


def emit_event(event_type: str, request: RunnerRequest, plan_id: Optional[str], detail: Dict[str, Any]) -> None:
    event = {
        "eventType": event_type,
        "requestId": request.requestId,
        "planId": plan_id,
        "clientId": request.clientId or config.client_id,
        "installationId": request.installationId or config.installation_id,
        "pipelineType": request.pipelineType or request.pipelineKind,
        "detail": detail,
        "timestamp": utc_now().isoformat(),
    }
    (config.work_dir / f"event-{safe_file_token(request.requestId)}-{event_type}.json").write_text(json.dumps(event, indent=2))
    if not config.event_endpoint:
        return
    headers = {"Content-Type": "application/json"}
    if config.activation_token:
        headers["Authorization"] = f"Bearer {config.activation_token}"
    try:
        requests.post(config.event_endpoint, json=event, headers=headers, timeout=10)
    except requests.RequestException:
        pass


def execute_git_checkout(action: Dict[str, Any], context: Dict[str, Any]) -> None:
    run_dir = context["runDir"]
    target_dir = run_dir / safe_file_token(action.get("directory") or "source")
    if target_dir.exists():
        shutil.rmtree(target_dir)
    repo_url = action["repoUrl"]
    branch = action.get("branch") or "main"
    run_command(["git", "clone", "--depth", "1", "--branch", branch, repo_url, str(target_dir)])
    commit_sha = run_command(["git", "rev-parse", "HEAD"], cwd=target_dir).stdout.strip()
    short_sha = run_command(["git", "rev-parse", "--short=11", "HEAD"], cwd=target_dir).stdout.strip()
    context["sourceDir"] = target_dir
    context["git"] = {"commitSha": commit_sha, "shortSha": short_sha, "branch": branch, "repoUrl": repo_url}


def execute_project_build(action: Dict[str, Any], context: Dict[str, Any]) -> None:
    source_dir = context.get("sourceDir") or context["runDir"] / safe_file_token(action.get("directory") or "source")
    project_type = (action.get("projectType") or "").lower()
    if not source_dir.exists():
        raise HTTPException(status_code=422, detail=f"Source directory does not exist: {source_dir}")

    if project_type == "docker":
        if not (source_dir / "Dockerfile").exists():
            raise HTTPException(status_code=422, detail="Dockerfile is required for Docker projects")
        return

    if project_type in {"angular", "nodejs", "webcomponent"}:
        if (source_dir / "package-lock.json").exists():
            run_command(["npm", "ci"], cwd=source_dir)
        else:
            run_command(["npm", "install"], cwd=source_dir)
        script = detect_npm_script(source_dir, ["prodbuild", "build:prod", "build"])
        if not script:
            raise HTTPException(status_code=422, detail="No npm build script found. Expected prodbuild, build:prod, or build.")
        run_command(["npm", "run", script], cwd=source_dir)
        return

    if project_type in {"springboot", "springboot-java11"}:
        if (source_dir / "mvnw").exists():
            run_command(["chmod", "+x", "mvnw"], cwd=source_dir)
            run_command(["./mvnw", "-B", "-DskipTests", "clean", "package"], cwd=source_dir)
            return
        if (source_dir / "pom.xml").exists():
            run_command(["mvn", "-B", "-DskipTests", "clean", "package"], cwd=source_dir)
            return
        if (source_dir / "gradlew").exists():
            run_command(["chmod", "+x", "gradlew"], cwd=source_dir)
            run_command(["./gradlew", "clean", "build", "-x", "test"], cwd=source_dir)
            return
        raise HTTPException(status_code=422, detail="Spring Boot project requires Maven or Gradle build files")

    raise HTTPException(status_code=422, detail=f"Unsupported project type for build: {action.get('projectType')}")


def kubectl_apply_object(obj: Dict[str, Any], *, env: Optional[Dict[str, str]] = None) -> None:
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
        json.dump(obj, handle)
        path = handle.name
    try:
        run_command(["kubectl", "apply", "-f", path], env=env)
    finally:
        Path(path).unlink(missing_ok=True)


def execute_image_build_push(action: Dict[str, Any], context: Dict[str, Any]) -> None:
    rendered = render_value(action, context)
    region = rendered["awsRegion"]
    registry_host = ecr_host(rendered["ecrRegistry"], region)
    repository = rendered["ecrRepository"]
    image_tag = rendered.get("imageTag") or context.get("git", {}).get("shortSha")
    repo_uri = f"{registry_host}/{repository}".lower()
    image_uri = f"{repo_uri}:{image_tag}".lower()
    role_env = assume_role_env(rendered.get("roleArn", ""), region, f"horizon-build-{context['requestId']}")
    source_dir = context.get("sourceDir")

    describe = run_command(
        ["aws", "ecr", "describe-repositories", "--region", region, "--repository-names", repository],
        env=role_env,
        check=False,
    )
    if describe.returncode != 0:
        run_command(
            [
                "aws",
                "ecr",
                "create-repository",
                "--region",
                region,
                "--repository-name",
                repository,
                "--image-scanning-configuration",
                "scanOnPush=true",
                "--encryption-configuration",
                "encryptionType=AES256",
            ],
            env=role_env,
        )
    password = run_command(["aws", "ecr", "get-login-password", "--region", region], env=role_env, timeout=60, log_output=False).stdout.strip()
    docker_auth = base64.b64encode(f"AWS:{password}".encode("utf-8")).decode("utf-8")
    docker_config = {"auths": {registry_host: {"auth": docker_auth}}}
    docker_config_b64 = base64.b64encode(json.dumps(docker_config).encode("utf-8")).decode("utf-8")
    build_context_uri = ""
    build_context_key = ""
    if source_dir and Path(source_dir).exists() and rendered.get("artifactBucket"):
        node_modules = Path(source_dir) / "node_modules"
        if node_modules.exists():
            shutil.rmtree(node_modules)
        build_context_key = (rendered.get("buildContextKey") or f"horizon-runner-contexts/{safe_file_token(context['requestId'])}/context.tar.gz").strip("/")
        context_tar = context["runDir"] / "context.tar.gz"
        run_command(["tar", "-czf", str(context_tar), "-C", str(source_dir), "."])
        run_command(
            [
                "aws",
                "s3",
                "cp",
                str(context_tar),
                f"s3://{rendered['artifactBucket']}/{build_context_key}",
                "--region",
                region,
            ],
            env=role_env,
        )
        build_context_uri = f"s3://{rendered['artifactBucket']}/{build_context_key}"

    namespace = current_namespace()
    secret_name = safe_k8s_name(f"horizon-kaniko-{context['requestId']}-{uuid.uuid4().hex[:6]}")
    job_name = safe_k8s_name(f"horizon-build-{context['requestId']}-{uuid.uuid4().hex[:6]}")
    secret_data = {
        "config.json": docker_config_b64,
        "AWS_ACCESS_KEY_ID": base64.b64encode(role_env.get("AWS_ACCESS_KEY_ID", "").encode("utf-8")).decode("utf-8"),
        "AWS_SECRET_ACCESS_KEY": base64.b64encode(role_env.get("AWS_SECRET_ACCESS_KEY", "").encode("utf-8")).decode("utf-8"),
        "AWS_SESSION_TOKEN": base64.b64encode(role_env.get("AWS_SESSION_TOKEN", "").encode("utf-8")).decode("utf-8"),
        "AWS_REGION": base64.b64encode(region.encode("utf-8")).decode("utf-8"),
        "AWS_DEFAULT_REGION": base64.b64encode(region.encode("utf-8")).decode("utf-8"),
    }
    kubectl_apply_object(
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": secret_name, "namespace": namespace},
            "type": "Opaque",
            "data": secret_data,
        }
    )

    destinations = [image_uri]
    for tag in rendered.get("additionalTags") or []:
        if tag:
            destinations.append(f"{repo_uri}:{tag}".lower())
    args = [
        f"--context={build_context_uri or ('git://' + rendered['repoUrl'].replace('https://', '').replace('http://', '') + '#refs/heads/' + (rendered.get('branch') or 'main'))}",
        f"--dockerfile={rendered.get('dockerfile') or 'Dockerfile'}",
        "--cleanup",
    ]
    for destination in destinations:
        args.append(f"--destination={destination}")

    job = {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": job_name, "namespace": namespace},
        "spec": {
            "backoffLimit": 0,
            "template": {
                "spec": {
                    "restartPolicy": "Never",
                    "containers": [
                        {
                            "name": "kaniko",
                            "image": config.kaniko_image,
                            "args": args,
                            "env": [
                                {"name": "AWS_ACCESS_KEY_ID", "valueFrom": {"secretKeyRef": {"name": secret_name, "key": "AWS_ACCESS_KEY_ID"}}},
                                {"name": "AWS_SECRET_ACCESS_KEY", "valueFrom": {"secretKeyRef": {"name": secret_name, "key": "AWS_SECRET_ACCESS_KEY"}}},
                                {"name": "AWS_SESSION_TOKEN", "valueFrom": {"secretKeyRef": {"name": secret_name, "key": "AWS_SESSION_TOKEN"}}},
                                {"name": "AWS_REGION", "valueFrom": {"secretKeyRef": {"name": secret_name, "key": "AWS_REGION"}}},
                                {"name": "AWS_DEFAULT_REGION", "valueFrom": {"secretKeyRef": {"name": secret_name, "key": "AWS_DEFAULT_REGION"}}},
                            ],
                            "volumeMounts": [{"name": "docker-config", "mountPath": "/kaniko/.docker", "readOnly": True}],
                        }
                    ],
                    "volumes": [{"name": "docker-config", "secret": {"secretName": secret_name}}],
                }
            },
        },
    }
    kubectl_apply_object(job)
    try:
        run_command(["kubectl", "wait", f"job/{job_name}", "-n", namespace, "--for=condition=complete", f"--timeout={config.timeout_seconds}s"], timeout=config.timeout_seconds + 30)
    except HTTPException:
        run_command(["kubectl", "logs", f"job/{job_name}", "-n", namespace], timeout=120)
        raise
    finally:
        run_command(["kubectl", "logs", f"job/{job_name}", "-n", namespace], timeout=120)
        if not config.keep_build_jobs:
            run_command(["kubectl", "delete", "job", job_name, "-n", namespace, "--ignore-not-found=true"], timeout=120)
            run_command(["kubectl", "delete", "secret", secret_name, "-n", namespace, "--ignore-not-found=true"], timeout=120)
            if build_context_uri:
                run_command(["aws", "s3", "rm", build_context_uri, "--region", region], env=role_env, timeout=120)

    digest = run_command(
        [
            "aws",
            "ecr",
            "describe-images",
            "--region",
            region,
            "--repository-name",
            repository,
            "--image-ids",
            f"imageTag={image_tag}",
            "--query",
            "imageDetails[0].imageDigest",
            "--output",
            "text",
        ],
        env=role_env,
    ).stdout.strip()
    context["image"] = {
        "tag": image_tag,
        "uri": image_uri,
        "repoUri": repo_uri,
        "digest": digest,
        "uriWithDigest": f"{repo_uri}@{digest}",
        "repository": repository,
        "registry": registry_host,
    }


def execute_artifact_publish(action: Dict[str, Any], context: Dict[str, Any]) -> None:
    rendered = render_value(action, context)
    region = rendered["awsRegion"]
    bucket = rendered["artifactBucket"]
    prefix = rendered["artifactPrefix"].strip("/")
    role_env = assume_role_env(rendered.get("roleArn", ""), region, f"horizon-artifact-{context['requestId']}")
    context["artifact"] = {"bucket": bucket, "prefix": prefix}

    artifact_dir = context["runDir"] / "artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    image = context.get("image", {})
    project = context.get("project", {})
    metadata = rendered.get("metadata") or {}
    (artifact_dir / "image.json").write_text(json.dumps({
        "ImageURI": image.get("uriWithDigest"),
        "ImageSHA": image.get("digest"),
        "ImageRepo": image.get("repoUri"),
        "ImageTag": image.get("tag"),
    }, indent=2))
    (artifact_dir / "templateconfiguration.json").write_text(json.dumps({
        "Parameters": {
            "ProjectType": metadata.get("projectType"),
            "ImageName": image.get("repository"),
            "ImageURI": image.get("uriWithDigest"),
            "ImageRepo": image.get("repoUri"),
            "ImageTag": image.get("tag"),
            "TargetEnv": metadata.get("targetEnv"),
            "ProjectName": project.get("name"),
        }
    }, indent=2))
    for filename in ("image.json", "templateconfiguration.json"):
        run_command(["aws", "s3", "cp", str(artifact_dir / filename), f"s3://{bucket}/{prefix}/{filename}", "--region", region], env=role_env)


def execute_artifact_context(action: Dict[str, Any], context: Dict[str, Any]) -> None:
    rendered = render_value(action, context)
    context["artifact"] = {
        "bucket": rendered["artifactBucket"],
        "prefix": rendered["artifactPrefix"].strip("/"),
        "region": rendered["awsRegion"],
        "roleArn": rendered.get("roleArn", ""),
    }


def execute_publish_reports(action: Dict[str, Any], context: Dict[str, Any]) -> None:
    rendered = render_value(action, context)
    region = rendered.get("awsRegion") or context.get("artifact", {}).get("region")
    bucket = rendered.get("artifactBucket") or context.get("artifact", {}).get("bucket")
    prefix = (rendered.get("artifactPrefix") or context.get("artifact", {}).get("prefix") or "").strip("/")
    role_env = assume_role_env(rendered.get("roleArn") or context.get("artifact", {}).get("roleArn", ""), region, f"horizon-reports-{context['requestId']}")
    report_root = context["runDir"] / (rendered.get("reportRoot") or "reports")
    if not report_root.exists():
        report_root.mkdir(parents=True, exist_ok=True)
        write_json(report_root / "summary.json", {"status": "NO_REPORTS", "message": "No validation reports were produced."})
    write_evidence_index(report_root, context, bucket, prefix)
    run_command(["aws", "s3", "sync", str(report_root), f"s3://{bucket}/{prefix}/test-results/", "--region", region], env=role_env)


def execute_quality_ui(action: Dict[str, Any], context: Dict[str, Any]) -> None:
    source_dir = get_source_dir(context)
    report_dir = action_report_dir(action, context, "selenium")
    target_url = normalize_http_url(action.get("targetAppUrl"))
    env = {
        "TARGET_APP_URL": target_url,
        "APPLICATION_URL": target_url,
        "APP_URL": target_url,
        "SELENIUM_REPORT_DIR": str(report_dir),
        "CI": "true",
    }
    status_code: Optional[int] = None
    command = ""
    output_tail = ""
    framework = "unknown"
    try:
        if (source_dir / "package.json").exists():
            framework = "node"
            script = detect_npm_script(source_dir, ["test:e2e", "e2e", "test:ui"])
            if not script:
                raise HTTPException(status_code=422, detail="No UI end-to-end npm script found. Expected test:e2e, e2e, or test:ui.")
            package = json.loads((source_dir / "package.json").read_text())
            deps = {**package.get("dependencies", {}), **package.get("devDependencies", {})}
            command = f"npm run {script}"
            if config.ui_test_isolated:
                command = f"kubernetes job {config.ui_test_image}: npm run {script}"
                result = execute_isolated_node_ui_test(source_dir, report_dir, target_url, script, context)
            else:
                if (source_dir / "package-lock.json").exists():
                    run_command(["npm", "ci"], cwd=source_dir)
                else:
                    run_command(["npm", "install"], cwd=source_dir)
                if "@playwright/test" in deps or "playwright" in deps:
                    run_command(["npx", "playwright", "install", "chromium"], cwd=source_dir, check=False)
                result = run_command(["npm", "run", script], cwd=source_dir, env=env, check=False)
            status_code = result.returncode
            output_tail = command_output_tail(result)
        elif (source_dir / "pom.xml").exists():
            framework = "maven"
            command = "mvn -B -Dtest=*UITest* test"
            result = run_command(["mvn", "-B", "-Dtest=*UITest*", f"-Dsurefire.reportsDirectory={report_dir}", "test"], cwd=source_dir, env=env, check=False)
            status_code = result.returncode
            output_tail = command_output_tail(result)
        else:
            raise HTTPException(status_code=422, detail="No supported UI test framework found.")
    finally:
        sync_relative_report_dir(source_dir, report_dir, action)
        junit = parse_junit_reports(report_dir, context)
        artifacts = report_artifacts(report_dir, context)
        summary = {
            "tool": "selenium",
            "toolName": "UI End-to-End Test",
            "framework": framework,
            "stage": "ui-test",
            "status": "PASSED" if status_code == 0 else "FAILED",
            "targetAppUrl": target_url,
            "command": command,
            "exitCode": status_code,
            "requestId": context["requestId"],
            "project": context.get("project", {}),
            "git": context.get("git", {}),
            "runner": context.get("runner", {}),
            "reportDir": relative_path(report_dir, context["runDir"]),
            "totalTests": junit["total"],
            "passedTests": junit["passed"],
            "failedTests": junit["failed"],
            "errorTests": junit["errors"],
            "skippedTests": junit["skipped"],
            "durationSeconds": junit["durationSeconds"],
            "junitReports": junit["junitReports"],
            "testCases": junit["testCases"],
            "artifacts": artifacts,
        }
        if status_code not in {0, None}:
            summary["outputTail"] = output_tail
        write_json(report_dir / "summary.json", summary)
        write_json(report_dir / "evidence.json", {
            **summary,
            "generatedAt": utc_now().isoformat(),
            "evidenceType": "validation.ui",
        })
        print_quality_summary(summary)
    if status_code != 0 and as_bool(action.get("required"), True):
        raise HTTPException(status_code=500, detail={
            "message": "UI end-to-end test failed",
            "targetAppUrl": target_url,
            "command": command,
            "exitCode": status_code,
            "reportDir": str(report_dir.relative_to(context["runDir"])),
            "outputTail": output_tail,
        })


def _find_first(source_dir: Path, candidates: List[str]) -> str:
    for candidate in candidates:
        path = source_dir / candidate
        if path.exists():
            return candidate
    return ""


def _newman_url(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        raw = value.get("raw")
        if raw:
            return str(raw)
        protocol = value.get("protocol") or ""
        host = value.get("host") or []
        path = value.get("path") or []
        host_text = ".".join(str(part) for part in host) if isinstance(host, list) else str(host)
        path_text = "/".join(str(part) for part in path) if isinstance(path, list) else str(path)
        if host_text:
            return f"{protocol + '://' if protocol else ''}{host_text}{'/' + path_text if path_text else ''}"
    return ""


def _newman_stat(stats: Dict[str, Any], key: str) -> Dict[str, int]:
    value = stats.get(key) or {}
    return {
        "total": parse_int(value.get("total")),
        "failed": parse_int(value.get("failed")),
        "pending": parse_int(value.get("pending")),
    }


def parse_newman_results(report_dir: Path, context: Dict[str, Any], max_cases: int = 100) -> Dict[str, Any]:
    results_path = report_dir / "results.json"
    empty = {
        "stats": {},
        "requests": [],
        "testCases": [],
        "failedAssertions": [],
        "durationSeconds": 0,
    }
    if not results_path.exists():
        return empty
    try:
        data = json.loads(results_path.read_text())
    except (OSError, json.JSONDecodeError):
        return empty

    run = data.get("run") or {}
    stats = run.get("stats") or {}
    timings = run.get("timings") or {}
    duration_seconds = 0.0
    try:
        if timings.get("started") and timings.get("completed"):
            duration_seconds = (parse_time(str(timings["completed"])) - parse_time(str(timings["started"]))).total_seconds()
    except ValueError:
        duration_seconds = 0.0

    requests: List[Dict[str, Any]] = []
    test_cases: List[Dict[str, Any]] = []
    failed_assertions: List[Dict[str, Any]] = []
    for execution in run.get("executions") or []:
        item = execution.get("item") or {}
        request = execution.get("request") or {}
        response = execution.get("response") or {}
        assertions = execution.get("assertions") or []
        request_name = str(item.get("name") or request.get("name") or "API request")
        request_assertions = []
        failed_count = 0
        skipped_count = 0
        for assertion in assertions:
            assertion_name = str(assertion.get("assertion") or assertion.get("name") or "assertion")
            error = assertion.get("error") or {}
            skipped = bool(assertion.get("skipped"))
            status = "SKIPPED" if skipped else ("FAILED" if error else "PASSED")
            if status == "FAILED":
                failed_count += 1
            if status == "SKIPPED":
                skipped_count += 1
            case = {
                "name": assertion_name,
                "className": request_name,
                "status": status,
            }
            if error:
                case["message"] = str(error.get("message") or error)
                failed_assertions.append({
                    "request": request_name,
                    "assertion": assertion_name,
                    "message": case["message"],
                })
            if len(test_cases) < max_cases:
                test_cases.append(case)
            request_assertions.append(case)

        status_code = response.get("code") if isinstance(response, dict) else None
        response_time = response.get("responseTime") if isinstance(response, dict) else None
        total_assertions = len(request_assertions)
        requests.append({
            "name": request_name,
            "method": str(request.get("method") or "").upper(),
            "url": _newman_url(request.get("url")),
            "status": "FAILED" if failed_count else "PASSED",
            "statusCode": status_code,
            "statusText": response.get("status") if isinstance(response, dict) else "",
            "responseTimeMs": response_time,
            "assertions": {
                "total": total_assertions,
                "passed": max(total_assertions - failed_count - skipped_count, 0),
                "failed": failed_count,
                "skipped": skipped_count,
            },
        })

    assertion_stat = _newman_stat(stats, "assertions")
    request_stat = _newman_stat(stats, "requests")
    return {
        "stats": {
            "iterations": _newman_stat(stats, "iterations"),
            "requests": request_stat,
            "testScripts": _newman_stat(stats, "testScripts"),
            "prerequestScripts": _newman_stat(stats, "prerequestScripts"),
            "assertions": assertion_stat,
        },
        "requests": requests,
        "testCases": test_cases,
        "failedAssertions": failed_assertions,
        "durationSeconds": round(duration_seconds, 3),
    }


def execute_quality_api(action: Dict[str, Any], context: Dict[str, Any]) -> None:
    source_dir = get_source_dir(context)
    report_dir = action_report_dir(action, context, "newman")
    collection = action.get("collectionPath") or _find_first(source_dir, [
        "tests/postman/horizon-demo-angular.postman_collection.json",
        "tests/postman/collection.json",
        "tests/api/horizon-demo-api.collection.json",
        "tests/api/collection.json",
    ])
    if not collection:
        matches = sorted((source_dir / "tests/postman").glob("*postman_collection.json")) if (source_dir / "tests/postman").exists() else []
        collection = str(matches[0].relative_to(source_dir)) if matches else ""
    if not collection or not (source_dir / collection).exists():
        raise HTTPException(status_code=422, detail="API collection not found. Provide a collection path or add tests/postman/*postman_collection.json.")

    env_path = action.get("environmentPath") or ""
    if env_path and not (source_dir / env_path).exists():
        env_path = ""
    if not env_path:
        target_env = str((context.get("requestPayload") or {}).get("TARGET_ENV") or "qa").lower()
        env_path = _find_first(source_dir, [
            f"tests/postman/{target_env}.postman_environment.json",
            f"tests/postman/{target_env}.environment.json",
            "tests/postman/environment.json",
        ])
    data_file = action.get("iterationDataFile") or ""
    if data_file and not (source_dir / data_file).exists():
        raise HTTPException(status_code=422, detail=f"Iteration data file not found: {data_file}")

    base_url = normalize_http_url(action.get("baseUrl"))
    html_dir = report_dir / "html"
    html_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        "newman", "run", collection,
        "--timeout-request", str(action.get("timeoutMs") or "30000"),
        "--reporters", "cli,junit,json,htmlextra",
        "--reporter-junit-export", str(report_dir / "results.xml"),
        "--reporter-json-export", str(report_dir / "results.json"),
        "--reporter-htmlextra-export", str(html_dir / "index.html"),
        "--reporter-htmlextra-title", f"{context.get('project', {}).get('name') or context.get('jobName') or 'Horizon'} API Regression",
    ]
    if env_path:
        cmd.extend(["--environment", env_path])
    if data_file:
        cmd.extend(["--iteration-data", data_file])
    if base_url:
        cmd.extend(["--env-var", f"baseUrl={base_url}", "--env-var", f"apiBaseUrl={base_url}"])
    (report_dir / "newman-command.txt").write_text(shlex.join(cmd) + "\n")
    result = run_command(cmd, cwd=source_dir, check=False)
    output_tail = command_output_tail(result)
    junit = parse_junit_reports(report_dir, context)
    newman = parse_newman_results(report_dir, context)
    artifacts = report_artifacts(report_dir, context)
    assertion_stats = newman.get("stats", {}).get("assertions", {})
    total_tests = junit["total"] or assertion_stats.get("total", 0)
    failed_tests = junit["failed"] or assertion_stats.get("failed", 0)
    skipped_tests = junit["skipped"] or assertion_stats.get("pending", 0)
    passed_tests = junit["passed"] or max(total_tests - failed_tests - skipped_tests, 0)
    summary = {
        "tool": "newman",
        "toolName": "API Regression Test",
        "framework": "newman",
        "stage": "api-test",
        "status": "PASSED" if result.returncode == 0 else "FAILED",
        "collection": collection,
        "environment": env_path,
        "iterationDataFile": data_file,
        "baseUrl": base_url,
        "command": shlex.join(cmd),
        "exitCode": result.returncode,
        "requestId": context["requestId"],
        "project": context.get("project", {}),
        "git": context.get("git", {}),
        "runner": context.get("runner", {}),
        "reportDir": relative_path(report_dir, context["runDir"]),
        "totalTests": total_tests,
        "passedTests": passed_tests,
        "failedTests": failed_tests,
        "errorTests": junit["errors"],
        "skippedTests": skipped_tests,
        "durationSeconds": junit["durationSeconds"] or newman.get("durationSeconds", 0),
        "junitReports": junit["junitReports"],
        "testCases": junit["testCases"] or newman.get("testCases", []),
        "newmanStats": newman.get("stats", {}),
        "requests": newman.get("requests", []),
        "failedAssertions": newman.get("failedAssertions", []),
        "artifacts": artifacts,
    }
    if result.returncode != 0:
        summary["outputTail"] = output_tail
    write_json(report_dir / "summary.json", summary)
    write_json(report_dir / "evidence.json", {
        **summary,
        "generatedAt": utc_now().isoformat(),
        "evidenceType": "validation.api",
    })
    print_quality_summary(summary)
    if result.returncode != 0 and as_bool(action.get("failOnError"), True):
        raise HTTPException(status_code=500, detail={
            "message": "API regression test failed",
            "collection": collection,
            "baseUrl": base_url,
            "exitCode": result.returncode,
            "reportDir": str(report_dir.relative_to(context["runDir"])),
            "outputTail": output_tail,
        })


def execute_quality_performance(action: Dict[str, Any], context: Dict[str, Any]) -> None:
    source_dir = get_source_dir(context)
    report_dir = action_report_dir(action, context, "jmeter")
    test_plan = action.get("testPlan") or "tests/performance/test.jmx"
    if not (source_dir / test_plan).exists():
        raise HTTPException(status_code=422, detail=f"Performance test plan not found: {test_plan}")
    base_url = normalize_http_url(action.get("baseUrl"))
    parsed = urlparse(base_url)
    jtl = report_dir / "results.jtl"
    html_dir = report_dir / "html"
    cmd = [
        "jmeter", "-n", "-t", test_plan,
        f"-Jprotocol={parsed.scheme or 'http'}",
        f"-Jhost={parsed.hostname or base_url}",
        f"-Jport={parsed.port or (443 if parsed.scheme == 'https' else 80)}",
        f"-Jbase_path={parsed.path or '/'}",
        f"-Jthreads={action.get('threads') or 10}",
        f"-JrampSeconds={action.get('rampSeconds') or 30}",
        f"-Jloops={action.get('loops') or 5}",
        "-l", str(jtl), "-e", "-o", str(html_dir),
    ]
    result = run_command(cmd, cwd=source_dir, check=False)
    response_times: List[int] = []
    failures = 0
    total = 0
    if jtl.exists():
        for line in jtl.read_text(errors="ignore").splitlines()[1:]:
            parts = line.split(",")
            if len(parts) >= 8:
                total += 1
                try:
                    response_times.append(int(parts[1]))
                except ValueError:
                    pass
                if parts[7].lower() != "true":
                    failures += 1
    response_times_sorted = sorted(response_times)
    p95 = response_times_sorted[int(len(response_times_sorted) * 0.95) - 1] if response_times_sorted else 0
    avg_ms = int(mean(response_times)) if response_times else 0
    error_pct = (failures / total * 100) if total else 0
    max_error = float(action.get("maxErrorPercent") or 1)
    max_avg = int(action.get("maxAvgMs") or 2000)
    max_p95 = int(action.get("maxP95Ms") or 5000)
    passed = result.returncode == 0 and error_pct <= max_error and avg_ms <= max_avg and p95 <= max_p95
    write_json(report_dir / "summary.json", {
        "status": "PASSED" if passed else "FAILED",
        "baseUrl": base_url,
        "samples": total,
        "failures": failures,
        "errorPercent": round(error_pct, 2),
        "averageMs": avg_ms,
        "p95Ms": p95,
        "thresholds": {"maxErrorPercent": max_error, "maxAvgMs": max_avg, "maxP95Ms": max_p95},
    })
    if not passed:
        raise HTTPException(status_code=500, detail="Performance test failed")


def execute_quality_code(action: Dict[str, Any], context: Dict[str, Any]) -> None:
    source_dir = get_source_dir(context)
    report_dir = action_report_dir(action, context, "code-quality")
    source_files = [p for p in source_dir.rglob("*") if p.is_file() and ".git" not in p.parts and "node_modules" not in p.parts]
    lines = 0
    for path in source_files:
        if path.suffix.lower() in {".js", ".ts", ".java", ".py", ".html", ".css", ".scss"}:
            try:
                lines += len(path.read_text(errors="ignore").splitlines())
            except OSError:
                pass
    sonar_status = "NOT_CONFIGURED"
    if (source_dir / "sonar-project.properties").exists() and shutil.which("sonar-scanner") and os.getenv("SONAR_HOST_URL"):
        cmd = ["sonar-scanner", "-Dproject.settings=sonar-project.properties", f"-Dsonar.host.url={os.getenv('SONAR_HOST_URL')}"]
        if os.getenv("SONAR_TOKEN"):
            cmd.append(f"-Dsonar.token={os.getenv('SONAR_TOKEN')}")
        sonar = run_command(cmd, cwd=source_dir, check=False)
        sonar_status = "PASSED" if sonar.returncode == 0 else "FAILED"
    write_json(report_dir / "summary.json", {
        "status": "PASSED" if sonar_status != "FAILED" else "FAILED",
        "mode": "SONAR_SCANNER" if sonar_status != "NOT_CONFIGURED" else "LOCAL_CODE_QUALITY",
        "sourceFiles": len(source_files),
        "linesOfCode": lines,
        "sonarStatus": sonar_status,
    })
    if sonar_status == "FAILED" and as_bool(action.get("required"), False):
        raise HTTPException(status_code=500, detail="Code quality scan failed")


SEVERITY_RANK = {"UNKNOWN": 0, "LOW": 1, "MEDIUM": 2, "HIGH": 3, "CRITICAL": 4}


def normalize_security_severity(value: Any) -> str:
    severity = str(value or "UNKNOWN").strip().upper()
    aliases = {"ERROR": "HIGH", "WARNING": "MEDIUM", "WARN": "MEDIUM", "INFO": "LOW", "INFORMATIONAL": "LOW"}
    return aliases.get(severity, severity if severity in SEVERITY_RANK else "UNKNOWN")


def security_thresholds(action: Dict[str, Any]) -> List[str]:
    configured = action.get("failOnSeverity") or config.security_fail_on_severity
    return [normalize_security_severity(item) for item in csv_values(configured) if normalize_security_severity(item) in SEVERITY_RANK]


def security_risk_score(severity: str) -> int:
    return {"CRITICAL": 95, "HIGH": 80, "MEDIUM": 55, "LOW": 25}.get(normalize_security_severity(severity), 10)


def security_finding_id(*parts: Any) -> str:
    raw = "|".join(str(part or "") for part in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def context_application(context: Dict[str, Any]) -> str:
    payload = context.get("requestPayload") or {}
    return str(
        payload.get("PROJECT_NAME")
        or payload.get("projectName")
        or context.get("project", {}).get("name")
        or context.get("runner", {}).get("jobName")
        or context["requestId"]
    )


def canonical_release_id(rendered: Dict[str, Any], context: Dict[str, Any]) -> str:
    """Use the backend-created ID; never derive an independent runner ID."""
    payload = context.get("requestPayload") or {}
    release_id = (
        rendered.get("releaseId")
        or payload.get("RELEASE_TRUST_RELEASE_ID")
        or payload.get("releaseTrustReleaseId")
        or context.get("release_trust", {}).get("releaseId")
    )
    if not release_id:
        raise HTTPException(status_code=422, detail="Release Trust action requires RELEASE_TRUST_RELEASE_ID")
    return str(release_id)


def context_requested_by(context: Dict[str, Any]) -> str:
    payload = context.get("requestPayload") or {}
    return str(payload.get("REQUESTED_BY") or payload.get("requestedBy") or payload.get("requesterEmail") or "")


def make_security_finding(
    *,
    category: str,
    target: str,
    severity: Any,
    rule: str,
    description: str,
    component: str = "",
    installed_version: str = "",
    fixed_version: str = "",
    line: Optional[int] = None,
    status: str = "OPEN",
    reference: str = "",
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    normalized = normalize_security_severity(severity)
    finding = {
        "target": target or "source",
        "package_name": component or target or "source",
        "installed_version": installed_version or "",
        "vulnerability_id": rule or security_finding_id(category, target, description),
        "severity": normalized,
        "fixed_version": fixed_version or "",
        "risk_score": security_risk_score(normalized),
        "description": description or rule or category,
        "source": category,
        "timestamp": utc_now().isoformat(),
        "line": line,
        "rule": rule or "",
        "status": status,
        "predictedSeverity": normalized,
        "reference": reference or "",
    }
    if extra:
        finding.update({key: value for key, value in extra.items() if value not in (None, "")})
    return finding


def count_by_severity(findings: List[Dict[str, Any]]) -> Dict[str, int]:
    counts = {key: 0 for key in ("CRITICAL", "HIGH", "MEDIUM", "LOW", "UNKNOWN")}
    for finding in findings:
        counts[normalize_security_severity(finding.get("severity"))] += 1
    return counts


def blocking_findings(findings: List[Dict[str, Any]], thresholds: List[str]) -> List[Dict[str, Any]]:
    if not thresholds:
        return []
    minimum = min(SEVERITY_RANK[item] for item in thresholds)
    return [
        finding
        for finding in findings
        if str(finding.get("status") or "").upper() != "WAIVED"
        and SEVERITY_RANK[normalize_security_severity(finding.get("severity"))] >= minimum
    ]


def write_command_audit(path: Path, command: List[str], result: subprocess.CompletedProcess) -> None:
    path.write_text(
        "\n".join(
            [
                f"command={shlex.join(command)}",
                f"exitCode={result.returncode}",
                "stdout:",
                result.stdout or "",
                "stderr:",
                result.stderr or "",
            ]
        )
    )


def upload_security_findings(findings: List[Dict[str, Any]], context: Dict[str, Any]) -> Dict[str, Any]:
    endpoint = config.findings_upload_url.strip()
    if not endpoint:
        return {"status": "NOT_CONFIGURED", "uploadedCount": 0}
    payload = context.get("requestPayload") or {}
    try:
        build_number = int(str(context.get("runner", {}).get("buildNumber") or payload.get("BUILD_NUMBER") or 0) or 0)
    except ValueError:
        build_number = 0
    jenkins_url = context.get("runner", {}).get("buildUrl") or payload.get("BUILD_URL") or ""
    jenkins_job = context.get("runner", {}).get("jobName") or payload.get("JOB_NAME") or ""
    enriched_findings = []
    for finding in findings:
        enriched = dict(finding)
        enriched["jenkins_job"] = str(enriched.get("jenkins_job") or jenkins_job)
        enriched["build_number"] = int(enriched.get("build_number") or build_number or 0)
        enriched["jenkins_url"] = str(enriched.get("jenkins_url") or jenkins_url)
        enriched_findings.append(enriched)
    body = {
        "application": context_application(context),
        "requestedBy": context_requested_by(context),
        "repo_url": context.get("git", {}).get("repoUrl") or payload.get("GIT_REPO_URL") or payload.get("repositoryUrl") or "",
        "jenkins_url": jenkins_url,
        "jenkins_job": jenkins_job,
        "build_number": build_number,
        "vulnerabilities": enriched_findings,
    }
    headers = {"Content-Type": "application/json"}
    if config.findings_upload_token:
        headers["Authorization"] = f"Bearer {config.findings_upload_token}"
    try:
        response = requests.post(endpoint, json=body, headers=headers, timeout=30)
    except requests.RequestException as exc:
        return {"status": "FAILED", "endpoint": endpoint, "error": str(exc), "uploadedCount": 0}
    status = "UPLOADED" if response.ok else "FAILED"
    return {"status": status, "endpoint": endpoint, "httpStatus": response.status_code, "uploadedCount": len(enriched_findings), "response": response.text[:1000]}


def finalize_security_report(
    *,
    action: Dict[str, Any],
    context: Dict[str, Any],
    report_dir: Path,
    stage: str,
    tool_names: List[str],
    findings: List[Dict[str, Any]],
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    thresholds = security_thresholds(action)
    evidence_path = relative_path(report_dir / "evidence.json", context["runDir"])
    for finding in findings:
        finding.setdefault("evidence_uri", evidence_path)
    blockers = blocking_findings(findings, thresholds)
    upload = upload_security_findings(findings, context)
    write_json(report_dir / "findings.json", findings)
    write_json(report_dir / "evidence.json", {
        "stage": stage,
        "application": context_application(context),
        "generatedAt": utc_now().isoformat(),
        "tools": tool_names,
        "thresholds": thresholds,
        "findingCount": len(findings),
        "dashboardUpload": upload,
    })
    summary = {
        "kind": "security",
        "stage": stage,
        "status": "FAILED" if blockers else ("COMPLETED_WITH_FINDINGS" if findings else "PASSED"),
        "tool": ",".join(tool_names),
        "toolName": ",".join(tool_names),
        "tools": tool_names,
        "findingCount": len(findings),
        "blockingFindingCount": len(blockers),
        "severityCounts": count_by_severity(findings),
        "thresholds": thresholds,
        "thresholdFailures": [f"{item.get('severity')} {item.get('source')} {item.get('target')} {item.get('vulnerability_id')}" for item in blockers[:25]],
        "sampleFindings": findings[:10],
        "dashboardUpload": upload,
        "artifacts": report_artifacts(report_dir, context),
    }
    if extra:
        summary.update(extra)
    write_json(report_dir / "summary.json", summary)
    print_quality_summary(summary)
    if upload.get("status") == "FAILED" and config.security_fail_on_dashboard_upload:
        raise HTTPException(status_code=500, detail=f"Security findings dashboard upload failed: {upload.get('error') or upload.get('response')}")
    if blockers and as_bool(action.get("failOnFindings"), True):
        raise HTTPException(status_code=500, detail=f"{stage} found {len(blockers)} blocking findings at threshold {','.join(thresholds)}")


def parse_trivy_report(path: Path, default_category: str = "Dependency Vulnerability") -> List[Dict[str, Any]]:
    findings: List[Dict[str, Any]] = []
    if not path.exists():
        return findings
    try:
        doc = json.loads(path.read_text())
    except json.JSONDecodeError:
        return findings
    for result in doc.get("Results", []) or []:
        target = result.get("Target") or path.name
        result_class = str(result.get("Class") or "")
        result_type = str(result.get("Type") or "")
        category = "Container Vulnerability" if "os-pkgs" in result_type or "image" in path.name else default_category
        for vuln in result.get("Vulnerabilities") or []:
            findings.append(make_security_finding(
                category=category,
                target=target,
                severity=vuln.get("Severity"),
                rule=vuln.get("VulnerabilityID") or vuln.get("PkgID") or "",
                component=vuln.get("PkgName") or "",
                installed_version=vuln.get("InstalledVersion") or "",
                fixed_version=vuln.get("FixedVersion") or "",
                description=vuln.get("Title") or vuln.get("Description") or "",
                reference=(vuln.get("PrimaryURL") or ""),
                extra={"scanner": "trivy"},
            ))
        for misconfig in result.get("Misconfigurations") or []:
            findings.append(make_security_finding(
                category="IaC Misconfiguration",
                target=target,
                severity=misconfig.get("Severity"),
                rule=misconfig.get("ID") or misconfig.get("AVDID") or "",
                description=misconfig.get("Title") or misconfig.get("Description") or "",
                reference=misconfig.get("PrimaryURL") or "",
                extra={"scanner": "trivy", "resultClass": result_class},
            ))
        for secret in result.get("Secrets") or []:
            findings.append(make_security_finding(
                category="Secret Exposure",
                target=target,
                severity=secret.get("Severity") or "HIGH",
                rule=secret.get("RuleID") or secret.get("Category") or "secret",
                description=secret.get("Title") or "Potential secret exposure",
                line=secret.get("StartLine"),
                extra={"scanner": "trivy"},
            ))
    return findings


def semgrep_default_rules(path: Path) -> None:
    path.write_text(
        """
rules:
  - id: horizon.javascript.eval
    message: Avoid dynamic eval-style execution.
    severity: ERROR
    languages: [javascript, typescript]
    pattern-either:
      - pattern: eval(...)
      - pattern: new Function(...)
  - id: horizon.insecure.http-url
    message: Plain HTTP endpoint detected.
    severity: WARNING
    languages: [generic]
    pattern-regex: "http://[^\\s'\\\"]+"
  - id: horizon.hardcoded.secret
    message: Potential hardcoded secret detected.
    severity: ERROR
    languages: [generic]
    pattern-regex: "(?i)(password|passwd|secret|token|api[_-]?key|client_secret)\\s*[:=]\\s*['\\\"][^'\\\"]{8,}"
""".strip()
    )


def parse_semgrep_report(path: Path) -> List[Dict[str, Any]]:
    findings: List[Dict[str, Any]] = []
    if not path.exists():
        return findings
    try:
        doc = json.loads(path.read_text())
    except json.JSONDecodeError:
        return findings
    for result in doc.get("results") or []:
        extra = result.get("extra") or {}
        findings.append(make_security_finding(
            category="Static Code Security Finding",
            target=result.get("path") or "source",
            severity=extra.get("severity"),
            rule=result.get("check_id") or "",
            description=extra.get("message") or result.get("check_id") or "Static code finding",
            line=(result.get("start") or {}).get("line"),
            extra={"scanner": "semgrep"},
        ))
    return findings


def parse_gitleaks_report(path: Path) -> List[Dict[str, Any]]:
    findings: List[Dict[str, Any]] = []
    if not path.exists():
        return findings
    try:
        doc = json.loads(path.read_text() or "[]")
    except json.JSONDecodeError:
        return findings
    for item in doc if isinstance(doc, list) else []:
        findings.append(make_security_finding(
            category="Secret Exposure",
            target=item.get("File") or "source",
            severity="HIGH",
            rule=item.get("RuleID") or item.get("Description") or "secret",
            description=item.get("Description") or "Secret detected by Gitleaks",
            line=item.get("StartLine"),
            extra={"scanner": "gitleaks", "commit": item.get("Commit")},
        ))
    return findings


def discover_manifest_inputs(source_dir: Path) -> List[Path]:
    candidates: List[Path] = []
    for name in ("k8s", "kubernetes", "manifests", "deploy", "deployment", "helm", "charts"):
        path = source_dir / name
        if path.exists():
            candidates.append(path)
    for path in list(source_dir.glob("*.yaml")) + list(source_dir.glob("*.yml")):
        candidates.append(path)
    return candidates


def built_in_conftest_policy(policy_dir: Path) -> Path:
    policy_dir.mkdir(parents=True, exist_ok=True)
    path = policy_dir / "horizon-kubernetes.rego"
    path.write_text(
        r'''
package main

deny[msg] {
  input.kind == "Pod"
  container := input.spec.containers[_]
  container.securityContext.privileged == true
  msg := sprintf("privileged container %s is not allowed", [container.name])
}

deny[msg] {
  input.kind == "Pod"
  input.spec.hostNetwork == true
  msg := "hostNetwork is not allowed"
}

deny[msg] {
  input.kind == "Pod"
  container := input.spec.containers[_]
  not container.securityContext.runAsNonRoot
  msg := sprintf("container %s should set securityContext.runAsNonRoot=true", [container.name])
}

deny[msg] {
  input.kind == "Pod"
  container := input.spec.containers[_]
  not container.resources.limits.cpu
  msg := sprintf("container %s is missing CPU limit", [container.name])
}

deny[msg] {
  input.kind == "Pod"
  container := input.spec.containers[_]
  not container.resources.limits.memory
  msg := sprintf("container %s is missing memory limit", [container.name])
}

warn[msg] {
  input.kind == "Pod"
  container := input.spec.containers[_]
  endswith(container.image, ":latest")
  msg := sprintf("container %s uses latest image tag", [container.name])
}
'''.strip()
    )
    return path


def policy_bundle_slug(bundle: Dict[str, Any]) -> str:
    name = str(bundle.get("name") or bundle.get("bundle") or bundle.get("ref") or "horizon-baseline")
    name = name.split("@", 1)[0]
    version = str(bundle.get("version") or "1.0.0")
    return f"{safe_file_token(name)}-{safe_file_token(version)}"


def default_policy_bundles() -> List[Dict[str, Any]]:
    return [
        {"name": "horizon-baseline", "version": "1.0.0", "ref": "horizon-baseline@1.0.0", "source": "runner-fallback"},
        {"name": "horizon-kubernetes-restricted-lite", "version": "1.0.0", "ref": "horizon-kubernetes-restricted-lite@1.0.0", "source": "runner-fallback"},
        {"name": "horizon-release-trust", "version": "1.0.0", "ref": "horizon-release-trust@1.0.0", "source": "runner-fallback"},
    ]


def resolve_policy_bundles(action: Dict[str, Any], report_dir: Path, source_dir: Path) -> List[Dict[str, Any]]:
    configured = action.get("policyBundles") or (action.get("policy") or {}).get("bundles") or default_policy_bundles()
    resolved: List[Dict[str, Any]] = []
    for item in configured:
        if isinstance(item, str):
            name, _, version = item.partition("@")
            bundle = {"name": name, "version": version or "1.0.0", "ref": f"{name}@{version or '1.0.0'}", "source": "license-control-plane"}
        elif isinstance(item, dict):
            name = str(item.get("name") or item.get("bundle") or "").strip()
            if not name and item.get("ref"):
                name = str(item["ref"]).split("@", 1)[0]
            version = str(item.get("version") or (str(item.get("ref") or "").split("@", 1)[1] if "@" in str(item.get("ref") or "") else "1.0.0"))
            bundle = {
                "name": name,
                "version": version,
                "ref": item.get("ref") or f"{name}@{version}",
                "source": item.get("source") or action.get("policyBundleSource") or "license-control-plane",
                "description": item.get("description") or "",
            }
        else:
            continue
        if not bundle.get("name"):
            continue

        fallback_dir = config.policy_bundle_dir / str(bundle["name"]) / str(bundle["version"])
        if not fallback_dir.exists():
            fallback_dir = config.policy_bundle_dir / str(bundle["name"])
        target_dir = report_dir / "policies" / policy_bundle_slug(bundle)
        target_dir.mkdir(parents=True, exist_ok=True)
        if fallback_dir.exists():
            shutil.copytree(fallback_dir, target_dir, dirs_exist_ok=True)
            bundle["path"] = str(target_dir)
            bundle["resolved"] = True
            bundle["resolvedSource"] = str(fallback_dir)
        else:
            built_in_conftest_policy(target_dir)
            bundle["path"] = str(target_dir)
            bundle["resolved"] = False
            bundle["resolvedSource"] = "generated-baseline"
        resolved.append(bundle)

    if as_bool(action.get("clientOverridesEnabled"), False):
        for name in ("policy", "policies", ".horizon/policy", ".horizon/policies"):
            path = source_dir / name
            if path.exists():
                bundle = {
                    "name": "client-overrides",
                    "version": "repo",
                    "ref": "client-overrides@repo",
                    "source": "client-repository",
                    "path": str(path),
                    "resolved": True,
                    "resolvedSource": str(path),
                }
                resolved.append(bundle)

    return resolved


def parse_conftest_report(path: Path, bundle: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    findings: List[Dict[str, Any]] = []
    if not path.exists():
        return findings
    try:
        doc = json.loads(path.read_text() or "[]")
    except json.JSONDecodeError:
        return findings
    for result in doc if isinstance(doc, list) else []:
        target = result.get("filename") or result.get("namespace") or "manifest"
        for failure in result.get("failures") or []:
            description = str(failure.get("msg") if isinstance(failure, dict) else failure)
            metadata = (failure.get("metadata") or {}) if isinstance(failure, dict) else {}
            rule_id = str(metadata.get("rule_id") or metadata.get("id") or "").strip()
            if not rule_id:
                match = re.search(r"\b(HR-POL-[A-Z0-9-]+)\b", description)
                rule_id = match.group(1) if match else security_finding_id(bundle.get("ref") if bundle else "", target, description)
            findings.append(make_security_finding(
                category="Policy Violation",
                target=target,
                severity="HIGH",
                rule=rule_id,
                description=description,
                fixed_version="Review policy bundle guidance, adjust the manifest, or request a time-bound waiver.",
                extra={
                    "scanner": "conftest",
                    "policy_bundle": (bundle or {}).get("name"),
                    "policy_version": (bundle or {}).get("version"),
                    "policy_ref": (bundle or {}).get("ref"),
                    "policy_decision": "deny",
                    "waiver_status": "none",
                },
            ))
        for warning in result.get("warnings") or []:
            description = str(warning.get("msg") if isinstance(warning, dict) else warning)
            metadata = (warning.get("metadata") or {}) if isinstance(warning, dict) else {}
            rule_id = str(metadata.get("rule_id") or metadata.get("id") or "").strip()
            if not rule_id:
                match = re.search(r"\b(HR-POL-[A-Z0-9-]+)\b", description)
                rule_id = match.group(1) if match else security_finding_id(bundle.get("ref") if bundle else "", target, description)
            findings.append(make_security_finding(
                category="Policy Violation",
                target=target,
                severity="MEDIUM",
                rule=rule_id,
                description=description,
                fixed_version="Review policy bundle guidance and adjust the manifest when practical.",
                extra={
                    "scanner": "conftest",
                    "policy_bundle": (bundle or {}).get("name"),
                    "policy_version": (bundle or {}).get("version"),
                    "policy_ref": (bundle or {}).get("ref"),
                    "policy_decision": "warn",
                    "waiver_status": "none",
                },
            ))
    return findings


def load_policy_waivers(source_dir: Path) -> List[Dict[str, Any]]:
    for rel in (".horizon/policy-waivers.json", ".horizon/waivers.json", "policy-waivers.json"):
        path = source_dir / rel
        if path.exists():
            try:
                data = json.loads(path.read_text() or "[]")
            except json.JSONDecodeError:
                return []
            return data if isinstance(data, list) else data.get("waivers", [])
    return []


def waiver_matches_finding(waiver: Dict[str, Any], finding: Dict[str, Any]) -> bool:
    rule = str(waiver.get("rule") or waiver.get("rule_id") or waiver.get("policy_rule") or "")
    bundle = str(waiver.get("bundle") or waiver.get("policy_bundle") or "")
    target = str(waiver.get("target") or "")
    if rule and rule not in {str(finding.get("rule") or ""), str(finding.get("vulnerability_id") or "")}:
        return False
    if bundle and bundle != str(finding.get("policy_bundle") or ""):
        return False
    if target and target not in str(finding.get("target") or ""):
        return False
    expires = waiver.get("expires_at") or waiver.get("expiresAt")
    if expires:
        try:
            if parse_time(str(expires)) <= utc_now():
                return False
        except ValueError:
            return False
    return bool(rule or bundle or target)


def apply_policy_waivers(findings: List[Dict[str, Any]], source_dir: Path, enabled: bool) -> Dict[str, Any]:
    waivers = load_policy_waivers(source_dir) if enabled else []
    applied = 0
    for finding in findings:
        for waiver in waivers:
            if waiver_matches_finding(waiver, finding):
                finding["status"] = "WAIVED"
                finding["waiver_status"] = "active"
                finding["waiver_expiry"] = waiver.get("expires_at") or waiver.get("expiresAt") or ""
                finding["waiver_reason"] = waiver.get("reason") or ""
                finding["waiver_approved_by"] = waiver.get("approved_by") or waiver.get("approvedBy") or ""
                applied += 1
                break
    return {"enabled": enabled, "loaded": len(waivers), "applied": applied}


def execute_security_preflight(action: Dict[str, Any], context: Dict[str, Any]) -> None:
    source_dir = get_source_dir(context)
    report_dir = action_report_dir(action, context, "security-preflight")
    excluded = {".git", "node_modules", "target", "dist", "build", ".angular", ".mvn"}
    secret_patterns = [
        ("aws_access_key", re.compile(r"AKIA[0-9A-Z]{16}")),
        ("private_key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----")),
        ("hardcoded_secret", re.compile(r"(?i)\b(password|passwd|secret|token|apikey|api_key|client_secret)\b\s*[:=]\s*['\"][^'\"]{8,}")),
        ("connection_string", re.compile(r"(?i)(jdbc:|mongodb://|postgres(?:ql)?://|mysql://)[^\s'\"]+")),
    ]
    text_suffixes = {".cfg", ".conf", ".env", ".groovy", ".ini", ".java", ".js", ".json", ".properties", ".py", ".sh", ".tf", ".ts", ".txt", ".yaml", ".yml", ".xml"}
    findings = []
    for path in source_dir.rglob("*"):
        if not path.is_file() or any(part in excluded for part in path.parts):
            continue
        if path.suffix.lower() not in text_suffixes and path.name not in {"Dockerfile", "Jenkinsfile"}:
            continue
        try:
            text = path.read_text(errors="ignore")
        except OSError:
            continue
        for line_no, line in enumerate(text.splitlines(), start=1):
            for rule_id, pattern in secret_patterns:
                if pattern.search(line):
                    findings.append({
                        "ruleId": rule_id,
                        "file": str(path.relative_to(source_dir)),
                        "line": line_no,
                        "severity": "HIGH",
                        "category": "Secret Exposure" if rule_id != "connection_string" else "Configuration Exposure",
                    })

    trivy_summary = {"status": "NOT_RUN", "findingCount": 0}
    if shutil.which("trivy"):
        trivy_json = report_dir / "filesystem-security.json"
        trivy = run_command(
            [
                "trivy", "fs",
                "--format", "json",
                "--scanners", "secret,config",
                "--severity", "CRITICAL,HIGH,MEDIUM",
                "--output", str(trivy_json),
                ".",
            ],
            cwd=source_dir,
            check=False,
        )
        trivy_count = 0
        if trivy_json.exists():
            try:
                doc = json.loads(trivy_json.read_text())
                for result in doc.get("Results", []) or []:
                    trivy_count += len(result.get("Secrets") or [])
                    trivy_count += len(result.get("Misconfigurations") or [])
            except json.JSONDecodeError:
                trivy_count = 0
        trivy_summary = {
            "status": "COMPLETED" if trivy.returncode in {0, 1} else "ERROR",
            "findingCount": trivy_count,
            "exitCode": trivy.returncode,
        }

    terraform_summary = {"status": "NOT_RUN"}
    if shutil.which("terraform") and any(source_dir.rglob("*.tf")):
        tf = run_command(["terraform", "fmt", "-check", "-recursive"], cwd=source_dir, check=False)
        terraform_summary = {"status": "PASSED" if tf.returncode == 0 else "FAILED", "exitCode": tf.returncode}

    (report_dir / "secrets.txt").write_text(
        "\n".join(f"{item['severity']} {item['ruleId']} {item['file']}:{item['line']}" for item in findings)
    )
    (report_dir / "preflight-failures.txt").write_text(
        "\n".join(f"{item['ruleId']} {item['file']}:{item['line']}" for item in findings if item["severity"] in {"CRITICAL", "HIGH"})
    )
    write_json(report_dir / "findings.json", findings)
    total_findings = len(findings) + int(trivy_summary.get("findingCount") or 0)
    write_json(report_dir / "summary.json", {
        "status": "PASSED" if total_findings == 0 else "COMPLETED_WITH_FINDINGS",
        "findingCount": total_findings,
        "patternFindingCount": len(findings),
        "externalScan": trivy_summary,
        "terraformFormat": terraform_summary,
        "failOnFindings": as_bool(action.get("failOnFindings"), False),
    })
    if total_findings and as_bool(action.get("failOnFindings"), False):
        raise HTTPException(status_code=500, detail="Source security preflight found blocking findings")


def execute_security_static_code(action: Dict[str, Any], context: Dict[str, Any]) -> None:
    source_dir = get_source_dir(context)
    report_dir = action_report_dir(action, context, "static-security")
    findings: List[Dict[str, Any]] = []
    tools: List[str] = []

    if shutil.which("semgrep"):
        rules_path = report_dir / "horizon-semgrep-rules.yml"
        semgrep_default_rules(rules_path)
        config_args = ["--config", str(rules_path)]
        for repo_rules in (".semgrep.yml", ".semgrep.yaml"):
            if (source_dir / repo_rules).exists():
                config_args.extend(["--config", repo_rules])
        semgrep_json = report_dir / "semgrep.json"
        semgrep_cmd = ["semgrep", "scan", *config_args, "--json", "--output", str(semgrep_json), "--metrics=off", "."]
        semgrep = run_command(semgrep_cmd, cwd=source_dir, check=False)
        write_command_audit(report_dir / "semgrep-command.txt", semgrep_cmd, semgrep)
        semgrep_sarif = report_dir / "semgrep.sarif"
        semgrep_sarif_cmd = ["semgrep", "scan", *config_args, "--sarif", "--output", str(semgrep_sarif), "--metrics=off", "."]
        run_command(semgrep_sarif_cmd, cwd=source_dir, check=False)
        findings.extend(parse_semgrep_report(semgrep_json))
        tools.append("semgrep")

    if shutil.which("gitleaks"):
        gitleaks_json = report_dir / "gitleaks.json"
        gitleaks_cmd = [
            "gitleaks", "detect",
            "--source", ".",
            "--no-git",
            "--redact",
            "--report-format", "json",
            "--report-path", str(gitleaks_json),
        ]
        gitleaks = run_command(gitleaks_cmd, cwd=source_dir, check=False)
        write_command_audit(report_dir / "gitleaks-command.txt", gitleaks_cmd, gitleaks)
        gitleaks_sarif = report_dir / "gitleaks.sarif"
        gitleaks_sarif_cmd = [
            "gitleaks", "detect",
            "--source", ".",
            "--no-git",
            "--redact",
            "--report-format", "sarif",
            "--report-path", str(gitleaks_sarif),
        ]
        run_command(gitleaks_sarif_cmd, cwd=source_dir, check=False)
        findings.extend(parse_gitleaks_report(gitleaks_json))
        tools.append("gitleaks")

    if not tools:
        findings.append(make_security_finding(
            category="Static Code Security Finding",
            target="runner",
            severity="MEDIUM",
            rule="scanner-not-configured",
            description="No static security scanner is installed in the runner image.",
        ))

    finalize_security_report(
        action=action,
        context=context,
        report_dir=report_dir,
        stage="static-security",
        tool_names=tools or ["local-static-security"],
        findings=findings,
        extra={"reviewTeam": action.get("reviewTeam") or ""},
    )


def execute_security_container_iac(action: Dict[str, Any], context: Dict[str, Any]) -> None:
    source_dir = get_source_dir(context)
    report_dir = action_report_dir(action, context, "container-iac")
    payload = context.get("requestPayload") or {}
    image_uri = render_value(action.get("imageUri") or payload.get("IMAGE_URI") or payload.get("imageUri") or "{{image.uriWithDigest}}", context)
    region = action.get("awsRegion") or os.getenv("AWS_REGION", "us-east-1")
    role_env = assume_role_env(action.get("roleArn", ""), region, f"horizon-scan-{context['requestId']}")
    findings: List[Dict[str, Any]] = []
    tools: List[str] = []
    scan_status: Dict[str, Any] = {"filesystem": "NOT_RUN", "image": "NOT_RUN"}
    if shutil.which("trivy"):
        fs_json = report_dir / "filesystem-security.json"
        fs_cmd = [
            "trivy", "fs",
            "--format", "json",
            "--scanners", "vuln,secret,config",
            "--severity", "CRITICAL,HIGH,MEDIUM,LOW,UNKNOWN",
            "--output", str(fs_json),
            ".",
        ]
        fs = run_command(fs_cmd, cwd=source_dir, check=False)
        write_command_audit(report_dir / "trivy-filesystem-command.txt", fs_cmd, fs)
        fs_sarif = report_dir / "filesystem-security.sarif"
        run_command(["trivy", "fs", "--format", "sarif", "--scanners", "vuln,secret,config", "--output", str(fs_sarif), "."], cwd=source_dir, check=False)
        fs_table = report_dir / "filesystem-security.txt"
        table = run_command(["trivy", "fs", "--format", "table", "--scanners", "vuln,secret,config", "."], cwd=source_dir, check=False)
        fs_table.write_text((table.stdout or "") + "\n" + (table.stderr or ""))
        findings.extend(parse_trivy_report(fs_json, default_category="Dependency Vulnerability"))
        scan_status["filesystem"] = "COMPLETED" if fs.returncode in {0, 1} else "ERROR"
        tools.append("trivy-fs")
        if image_uri:
            image_json = report_dir / "image-security.json"
            image_cmd = ["trivy", "image", "--format", "json", "--severity", "CRITICAL,HIGH,MEDIUM,LOW,UNKNOWN", "--output", str(image_json), image_uri]
            image = run_command(image_cmd, env=role_env, check=False)
            write_command_audit(report_dir / "trivy-image-command.txt", image_cmd, image)
            image_sarif = report_dir / "image-security.sarif"
            run_command(["trivy", "image", "--format", "sarif", "--output", str(image_sarif), image_uri], env=role_env, check=False)
            image_table = report_dir / "image-security.txt"
            image_text = run_command(["trivy", "image", "--format", "table", image_uri], env=role_env, check=False)
            image_table.write_text((image_text.stdout or "") + "\n" + (image_text.stderr or ""))
            findings.extend(parse_trivy_report(image_json, default_category="Container Vulnerability"))
            scan_status["image"] = "COMPLETED" if image.returncode in {0, 1} else "ERROR"
            tools.append("trivy-image")
    else:
        findings.append(make_security_finding(
            category="Container Vulnerability",
            target="runner",
            severity="MEDIUM",
            rule="trivy-not-configured",
            description="Trivy is not installed in the runner image.",
        ))

    finalize_security_report(
        action=action,
        context=context,
        report_dir=report_dir,
        stage="container-iac",
        tool_names=tools or ["local-container-iac"],
        findings=findings,
        extra={"scanStatus": scan_status, "imageUri": image_uri},
    )


def execute_security_policy(action: Dict[str, Any], context: Dict[str, Any]) -> None:
    source_dir = get_source_dir(context)
    report_dir = action_report_dir(action, context, "policy")
    findings: List[Dict[str, Any]] = []
    tools: List[str] = []
    manifest_inputs = discover_manifest_inputs(source_dir)
    rendered_dir = report_dir / "rendered"
    rendered_dir.mkdir(parents=True, exist_ok=True)
    conftest_inputs: List[Path] = []

    for item in manifest_inputs:
        if item.is_dir() and (item / "Chart.yaml").exists() and shutil.which("helm"):
            rendered = rendered_dir / f"{safe_file_token(item.name)}.yaml"
            helm_cmd = ["helm", "template", safe_k8s_name(context_application(context)), str(item)]
            helm = run_command(helm_cmd, cwd=source_dir, check=False)
            write_command_audit(report_dir / f"helm-template-{safe_file_token(item.name)}.txt", helm_cmd, helm)
            if helm.stdout:
                rendered.write_text(helm.stdout)
                conftest_inputs.append(rendered)
        elif item.is_dir() and (item / "kustomization.yaml").exists() and shutil.which("kubectl"):
            rendered = rendered_dir / f"{safe_file_token(item.name)}-kustomize.yaml"
            kustomize_cmd = ["kubectl", "kustomize", str(item)]
            kustomize = run_command(kustomize_cmd, cwd=source_dir, check=False)
            write_command_audit(report_dir / f"kustomize-{safe_file_token(item.name)}.txt", kustomize_cmd, kustomize)
            if kustomize.stdout:
                rendered.write_text(kustomize.stdout)
                conftest_inputs.append(rendered)
        else:
            conftest_inputs.append(item)

    policy_bundles = resolve_policy_bundles(action, report_dir, source_dir)

    if shutil.which("conftest") and conftest_inputs:
        for bundle in policy_bundles:
            policy_path = bundle.get("path")
            if not policy_path:
                continue
            bundle_token = policy_bundle_slug(bundle)
            conftest_json = report_dir / f"conftest-{bundle_token}.json"
            conftest_cmd = ["conftest", "test", "--output", "json", "--policy", str(policy_path)]
            conftest_cmd.extend(str(item) for item in conftest_inputs)
            conftest = run_command(conftest_cmd, cwd=source_dir, check=False)
            write_command_audit(report_dir / f"conftest-command-{bundle_token}.txt", conftest_cmd, conftest)
            conftest_json.write_text(conftest.stdout or "[]")
            findings.extend(parse_conftest_report(conftest_json, bundle=bundle))
        tools.append("conftest")

    write_json(report_dir / "manifest-inputs.json", [relative_path(path, source_dir) for path in conftest_inputs])
    write_json(report_dir / "policy-bundle-manifest.json", [
        {key: bundle.get(key) for key in ("name", "version", "ref", "source", "resolved", "resolvedSource", "description")}
        for bundle in policy_bundles
    ])

    if (source_dir / "Dockerfile").exists():
        dockerfile = (source_dir / "Dockerfile").read_text(errors="ignore")
        if re.search(r"(?im)^USER\s+root\s*$", dockerfile) or not re.search(r"(?im)^USER\s+\S+", dockerfile):
            findings.append(make_security_finding(
                category="Policy Violation",
                target="Dockerfile",
                severity="MEDIUM",
                rule="container-non-root-user",
                description="Dockerfile should run as a non-root user for enterprise workloads.",
                fixed_version="Add a non-root USER directive and ensure file permissions support it.",
                extra={
                    "scanner": "horizon-policy",
                    "policy_bundle": "horizon-baseline",
                    "policy_version": "1.0.0",
                    "policy_ref": "horizon-baseline@1.0.0",
                    "policy_decision": "warn",
                    "waiver_status": "none",
                },
            ))
    waiver_summary = apply_policy_waivers(findings, source_dir, as_bool(action.get("waiversEnabled"), False))

    finalize_security_report(
        action=action,
        context=context,
        report_dir=report_dir,
        stage="policy-validation",
        tool_names=tools or ["horizon-policy"],
        findings=findings,
        extra={
            "manifestInputCount": len(conftest_inputs),
            "policyBundleCount": len(policy_bundles),
            "policyBundles": [bundle.get("ref") for bundle in policy_bundles],
            "policyBundleMode": action.get("policyBundleMode") or config.policy_bundle_mode,
            "waivers": waiver_summary,
        },
    )


def execute_release_load_metadata(action: Dict[str, Any], context: Dict[str, Any]) -> None:
    rendered = render_value(action, context)
    region = rendered["awsRegion"]
    bucket = rendered["artifactBucket"]
    prefix = rendered["artifactPrefix"].strip("/")
    role_env = assume_role_env(rendered.get("roleArn", ""), region, f"horizon-release-load-{context['requestId']}")
    artifact_dir = context["runDir"] / "release-artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    image_path = artifact_dir / "image.json"
    template_path = artifact_dir / "templateconfiguration.json"
    run_command(["aws", "s3", "cp", f"s3://{bucket}/{rendered['imageJsonPath'].lstrip('/')}", str(image_path), "--region", region], env=role_env)
    run_command(["aws", "s3", "cp", f"s3://{bucket}/{rendered['templateConfigPath'].lstrip('/')}", str(template_path), "--region", region], env=role_env)
    image_doc = json.loads(image_path.read_text())
    digest = image_doc.get("ImageSHA") or image_doc.get("imageDigest") or image_doc.get("digest")
    tag = image_doc.get("ImageTag") or image_doc.get("imageTag")
    repo = image_doc.get("ImageRepo") or image_doc.get("imageRepo")
    if not digest or not str(digest).startswith("sha256:"):
        raise HTTPException(status_code=422, detail="image.json does not contain a valid image digest")
    context["artifact"] = {"bucket": bucket, "prefix": prefix, "region": region, "roleArn": rendered.get("roleArn", "")}
    context["release"] = {"sourceImageDigest": digest, "sourceImageTag": tag, "sourceImageRepo": repo, "template": json.loads(template_path.read_text())}


def execute_release_promote_image(action: Dict[str, Any], context: Dict[str, Any]) -> None:
    rendered = render_value(action, context)
    region = rendered["awsRegion"]
    source_role_env = assume_role_env(rendered.get("sourceRoleArn", ""), region, f"horizon-release-src-{context['requestId']}")
    target_role_env = assume_role_env(rendered.get("targetRoleArn", ""), region, f"horizon-release-tgt-{context['requestId']}")
    source_registry = ecr_host(rendered["sourceRegistry"], region)
    target_registry = ecr_host(rendered["targetRegistry"], region)
    source_repo = rendered["sourceRepository"]
    target_repo = rendered["targetRepository"]
    digest = context.get("release", {}).get("sourceImageDigest")
    if not digest:
        source_tag = rendered.get("sourceImageTag")
        if not source_tag:
            raise HTTPException(status_code=422, detail="Release promotion requires image digest or source image tag")
        digest = run_command(
            ["aws", "ecr", "describe-images", "--region", region, "--repository-name", source_repo, "--image-ids", f"imageTag={source_tag}", "--query", "imageDetails[0].imageDigest", "--output", "text"],
            env=source_role_env,
        ).stdout.strip()
    manifest = run_command(
        ["aws", "ecr", "batch-get-image", "--region", region, "--repository-name", source_repo, "--image-ids", f"imageDigest={digest}", "--query", "images[0].imageManifest", "--output", "text"],
        env=source_role_env,
        log_output=False,
    ).stdout.strip()
    describe = run_command(["aws", "ecr", "describe-repositories", "--region", region, "--repository-names", target_repo], env=target_role_env, check=False)
    if describe.returncode != 0:
        run_command(["aws", "ecr", "create-repository", "--region", region, "--repository-name", target_repo, "--image-scanning-configuration", "scanOnPush=true"], env=target_role_env)
    target_tags = csv_values(rendered.get("targetTags")) or ["promoted"]
    for tag in target_tags:
        run_command(["aws", "ecr", "put-image", "--region", region, "--repository-name", target_repo, "--image-tag", tag, "--image-manifest", manifest], env=target_role_env)
    repo_uri = f"{target_registry}/{target_repo}".lower()
    context["image"] = {
        "tag": target_tags[0],
        "uri": f"{repo_uri}:{target_tags[0]}",
        "repoUri": repo_uri,
        "digest": digest,
        "uriWithDigest": f"{repo_uri}@{digest}",
        "repository": target_repo,
        "registry": target_registry,
    }


def execute_release_publish_approval(action: Dict[str, Any], context: Dict[str, Any]) -> None:
    rendered = render_value(action, context)
    region = rendered["awsRegion"]
    bucket = rendered["artifactBucket"]
    prefix = rendered["artifactPrefix"].strip("/")
    target_env = rendered.get("targetEnv") or "release"
    role_env = assume_role_env(rendered.get("roleArn", ""), region, f"horizon-release-approval-{context['requestId']}")
    approval = {
        "status": "APPROVED",
        "approvedBy": rendered.get("approvedBy") or "horizon-release-manager",
        "targetEnv": target_env,
        "imageDigest": context.get("image", {}).get("digest"),
        "recordedAt": utc_now().isoformat(),
    }
    path = context["runDir"] / "artifacts" / "approval.json"
    write_json(path, approval)
    run_command(["aws", "s3", "cp", str(path), f"s3://{bucket}/{prefix}/{target_env.lower()}/approval.json", "--region", region], env=role_env)


def execute_eks_deploy(action: Dict[str, Any], context: Dict[str, Any]) -> None:
    rendered = render_value(action, context)
    region = rendered["awsRegion"]
    role_env = assume_role_env(rendered.get("roleArn", ""), region, f"horizon-deploy-{context['requestId']}")
    kubeconfig = context["runDir"] / "kubeconfig"
    deploy_name = safe_k8s_name(rendered["deploymentName"])
    namespace = rendered["namespace"]
    image_uri = rendered["imageUri"]
    container_port = int(rendered.get("containerPort") or 80)
    service_name = safe_k8s_name(rendered.get("serviceName") or deploy_name)

    env = {**role_env, "KUBECONFIG": str(kubeconfig)}
    run_command(["aws", "eks", "update-kubeconfig", "--region", region, "--name", rendered["clusterName"], "--kubeconfig", str(kubeconfig)], env=role_env)
    manifest = {
        "apiVersion": "v1",
        "kind": "List",
        "items": [
            {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {"name": deploy_name, "namespace": namespace, "labels": {"app": deploy_name, "managed-by": "horizon-runner"}},
                "spec": {
                    "replicas": 1,
                    "selector": {"matchLabels": {"app": deploy_name}},
                    "template": {
                        "metadata": {"labels": {"app": deploy_name, "managed-by": "horizon-runner"}},
                        "spec": {
                            "containers": [
                                {
                                    "name": safe_k8s_name(rendered.get("containerName") or deploy_name),
                                    "image": image_uri,
                                    "ports": [{"containerPort": container_port}],
                                }
                            ]
                        },
                    },
                },
            },
            {
                "apiVersion": "v1",
                "kind": "Service",
                "metadata": {"name": service_name, "namespace": namespace, "labels": {"app": deploy_name, "managed-by": "horizon-runner"}},
                "spec": {"type": "ClusterIP", "selector": {"app": deploy_name}, "ports": [{"port": container_port, "targetPort": container_port}]},
            },
        ],
    }
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
        json.dump(manifest, handle)
        manifest_path = handle.name
    try:
        run_command(["kubectl", "apply", "-f", manifest_path], env=env)
        run_command(["kubectl", "rollout", "status", f"deployment/{deploy_name}", "-n", namespace, "--timeout=300s"], env=env, timeout=360)
    finally:
        Path(manifest_path).unlink(missing_ok=True)

    target_env = rendered.get("targetEnv") or rendered.get("environment") or ""
    deployment = {
        "application": context.get("project", {}).get("name"),
        "targetEnv": target_env,
        "namespace": namespace,
        "deploymentName": deploy_name,
        "serviceName": service_name,
        "imageUri": image_uri,
        "deployedAt": utc_now().isoformat(),
    }
    artifact = context.get("artifact") or {}
    if artifact.get("bucket") and artifact.get("prefix"):
        path = context["runDir"] / "artifacts" / "deployment.json"
        path.write_text(json.dumps(deployment, indent=2))
        run_command(["aws", "s3", "cp", str(path), f"s3://{artifact['bucket']}/{artifact['prefix']}/{(target_env or 'deploy').lower()}/deployment.json", "--region", region], env=role_env)


def execute_release_trust_attest(action: Dict[str, Any], context: Dict[str, Any]) -> None:
    rendered = render_value(action, context)
    trust_url = rendered.get("trustServiceUrl") or os.getenv("HORIZON_TRUST_SERVICE_URL", "")
    if not trust_url:
        raise HTTPException(status_code=503, detail="Release trust service URL is not configured (HORIZON_TRUST_SERVICE_URL)")
    image_digest = context.get("image", {}).get("digest") or context.get("release", {}).get("sourceImageDigest")
    if not image_digest:
        raise HTTPException(status_code=422, detail="Image digest is required for trust attestation")
    artifact = context.get("artifact", {})
    payload = {
        "imageDigest": image_digest,
        "imageRepo": context.get("image", {}).get("repository") or rendered.get("imageRepo", ""),
        "targetEnv": rendered.get("targetEnv", ""),
        "stage": rendered.get("stage", "pipeline"),
        "attestedBy": rendered.get("attestedBy") or "horizon-runner",
        "changeTicket": rendered.get("changeTicket") or rendered.get("changeTicketRef", ""),
        "evidenceBucket": rendered.get("evidenceBucket") or artifact.get("bucket", ""),
        "evidencePrefix": rendered.get("evidencePrefix") or artifact.get("prefix", ""),
        "evidenceRegion": rendered.get("evidenceRegion") or artifact.get("region", ""),
        "roleArn": rendered.get("roleArn") or artifact.get("roleArn", ""),
    }
    headers = {"Content-Type": "application/json"}
    client_id = context.get("runner", {}).get("clientId") or config.client_id
    if client_id:
        headers["X-Client-ID"] = client_id
    try:
        response = requests.post(f"{trust_url.rstrip('/')}/v1/attest", json=payload, headers=headers, timeout=30)
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"Unable to reach release trust service: {exc}") from exc
    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"Release trust attestation failed: {response.status_code} {response.text}")
    context["trust"] = response.json()


def execute_release_trust_gate(action: Dict[str, Any], context: Dict[str, Any]) -> None:
    rendered = render_value(action, context)
    trust_url = rendered.get("trustServiceUrl") or os.getenv("HORIZON_TRUST_SERVICE_URL", "")
    if not trust_url:
        raise HTTPException(status_code=503, detail="Release trust service URL is not configured (HORIZON_TRUST_SERVICE_URL)")
    image_digest = context.get("image", {}).get("digest") or context.get("release", {}).get("sourceImageDigest")
    if not image_digest:
        raise HTTPException(status_code=422, detail="Image digest is required for release gate check")
    artifact = context.get("artifact", {})
    payload = {
        "imageDigest": image_digest,
        "imageRepo": context.get("image", {}).get("repository") or rendered.get("imageRepo", ""),
        "targetEnv": rendered.get("targetEnv", ""),
        "changeTicket": rendered.get("changeTicket") or rendered.get("changeTicketRef", ""),
        "requiredStages": rendered.get("requiredStages") or [],
        "evidenceBucket": rendered.get("evidenceBucket") or artifact.get("bucket", ""),
        "evidencePrefix": rendered.get("evidencePrefix") or artifact.get("prefix", ""),
        "evidenceRegion": rendered.get("evidenceRegion") or artifact.get("region", ""),
        "roleArn": rendered.get("roleArn") or artifact.get("roleArn", ""),
    }
    headers = {"Content-Type": "application/json"}
    client_id = context.get("runner", {}).get("clientId") or config.client_id
    if client_id:
        headers["X-Client-ID"] = client_id
    try:
        response = requests.post(f"{trust_url.rstrip('/')}/v1/gate", json=payload, headers=headers, timeout=30)
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"Unable to reach release trust service: {exc}") from exc
    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"Release gate check failed: {response.status_code} {response.text}")
    gate = response.json()
    context["trustGate"] = gate
    if gate.get("status") == "DENIED":
        violations = [v.get("message", v.get("policyId", "")) for v in gate.get("violations", [])]
        raise HTTPException(status_code=422, detail=f"Release gate denied: {'; '.join(violations)}")


def _assert_no_secrets(doc: Dict[str, Any]) -> None:
    secret_pattern = re.compile(
        r"(?i)\b(password|passwd|secret|token|apikey|api_key|client_secret)\b\s*[:=]\s*['\"][^'\"]{8,}"
    )
    for value in doc.values():
        if isinstance(value, str) and secret_pattern.search(value):
            raise HTTPException(status_code=422, detail="release.trust.collect_source: potential secret detected in source document")


def execute_release_trust_collect_source(action: Dict[str, Any], context: Dict[str, Any]) -> None:
    """Capture source metadata and write source.json to run dir and S3."""
    rendered = render_value(action, context)
    region = rendered["awsRegion"]
    bucket = rendered["artifactBucket"]
    application = context_application(context)
    release_id = canonical_release_id(rendered, context)

    git = context.get("git") or {}
    commit_sha = git.get("commitSha") or rendered.get("commitSha")
    if not commit_sha:
        raise HTTPException(status_code=422, detail="release.trust.collect_source: commitSha is required")

    source_doc = {
        "schemaVersion": "2026-06-source-v1",
        "clientId": config.client_id,
        "application": application,
        "releaseId": release_id,
        "provider": "github",
        "repositoryUrl": git.get("repoUrl") or rendered.get("repositoryUrl", ""),
        "branch": git.get("branch") or rendered.get("branch", ""),
        "commitSha": commit_sha,
        "tag": rendered.get("tag", ""),
        "prNumber": rendered.get("prNumber"),
        "requester": context.get("requestPayload", {}).get("requester", ""),
        "checkoutAt": utc_now().isoformat(),
    }

    _assert_no_secrets(source_doc)

    local_path = context["runDir"] / "artifacts" / "source.json"
    local_path.parent.mkdir(parents=True, exist_ok=True)
    write_json(local_path, source_doc)

    role_env = assume_role_env(rendered.get("roleArn", ""), region, f"horizon-rt-source-{context['requestId']}")
    s3_key = f"release-trust/{application}/{release_id}/source.json"
    run_command(["aws", "s3", "cp", str(local_path), f"s3://{bucket}/{s3_key}", "--region", region], env=role_env)

    context.setdefault("release_trust", {})["source"] = source_doc
    context["release_trust"]["evidencePrefix"] = f"release-trust/{application}/{release_id}"


def execute_release_trust_resolve_digest(action: Dict[str, Any], context: Dict[str, Any]) -> None:
    """After ECR push, resolve and record the immutable image digest."""
    rendered = render_value(action, context)
    region = rendered["awsRegion"]
    role_env = assume_role_env(rendered.get("roleArn", ""), region, f"horizon-rt-digest-{context['requestId']}")

    digest = context.get("image", {}).get("digest")
    repo = rendered.get("imageRepository") or context.get("image", {}).get("repository", "")
    tag = rendered.get("imageTag") or context.get("image", {}).get("tag", "")

    if not digest:
        result = run_command(
            [
                "aws", "ecr", "describe-images",
                "--region", region,
                "--repository-name", repo,
                "--image-ids", f"imageTag={tag}",
                "--query", "imageDetails[0].imageDigest",
                "--output", "text",
            ],
            env=role_env,
        )
        digest = result.stdout.strip()

    if not digest or not digest.startswith("sha256:"):
        raise HTTPException(status_code=422, detail="release.trust.resolve_digest: could not resolve immutable image digest")

    registry = rendered.get("registry") or context.get("image", {}).get("registry", "")
    application = context_application(context)
    release_id = canonical_release_id(rendered, context)

    image_doc = {
        "schemaVersion": "2026-06-image-v1",
        "clientId": config.client_id,
        "application": application,
        "releaseId": release_id,
        "registry": registry,
        "repository": repo,
        "tag": tag,
        "digest": digest,
        "imageUriByTag": f"{registry}/{repo}:{tag}",
        "imageUriByDigest": f"{registry}/{repo}@{digest}",
        "pushedAt": utc_now().isoformat(),
        "ImageURI": f"{registry}/{repo}@{digest}",
        "ImageSHA": digest,
        "ImageRepo": f"{registry}/{repo}",
        "ImageTag": tag,
    }

    bucket = rendered["artifactBucket"]
    local_path = context["runDir"] / "artifacts" / "image-rt.json"
    write_json(local_path, image_doc)
    s3_key = f"release-trust/{application}/{release_id}/image.json"
    run_command(["aws", "s3", "cp", str(local_path), f"s3://{bucket}/{s3_key}", "--region", region], env=role_env)

    context.setdefault("release_trust", {})["imageDigest"] = digest
    context["release_trust"]["imageDoc"] = image_doc


def execute_release_trust_generate_sbom(action: Dict[str, Any], context: Dict[str, Any]) -> None:
    """Generate CycloneDX SBOM for the resolved image digest."""
    rendered = render_value(action, context)
    region = rendered["awsRegion"]
    bucket = rendered["artifactBucket"]
    application = context_application(context)
    release_id = canonical_release_id(rendered, context)
    role_env = assume_role_env(rendered.get("roleArn", ""), region, f"horizon-rt-sbom-{context['requestId']}")

    digest = context.get("release_trust", {}).get("imageDigest")
    if not digest:
        raise HTTPException(
            status_code=422,
            detail="release.trust.generate_sbom: imageDigest not in context — run resolve_digest first",
        )

    image_uri = context["release_trust"]["imageDoc"]["imageUriByDigest"]
    report_dir = context["runDir"] / "artifacts" / "sbom"
    report_dir.mkdir(parents=True, exist_ok=True)
    sbom_path = report_dir / "sbom.cyclonedx.json"

    status = "NOT_RUN"
    if shutil.which("trivy"):
        trivy_cmd = ["trivy", "image", "--format", "cyclonedx", "--output", str(sbom_path), image_uri]
        result = run_command(trivy_cmd, env=role_env, check=False)
        status = "COMPLETED" if result.returncode in {0, 1} else "ERROR"
    elif shutil.which("syft"):
        syft_cmd = ["syft", "packages", image_uri, "-o", f"cyclonedx-json={sbom_path}"]
        result = run_command(syft_cmd, env=role_env, check=False)
        status = "COMPLETED" if result.returncode == 0 else "ERROR"
    else:
        status = "TOOL_NOT_AVAILABLE"

    if status == "COMPLETED" and sbom_path.exists():
        s3_key = f"release-trust/{application}/{release_id}/sbom/sbom.cyclonedx.json"
        run_command(
            ["aws", "s3", "cp", str(sbom_path), f"s3://{bucket}/{s3_key}", "--region", region],
            env=role_env,
        )
        context.setdefault("release_trust", {})["sbom"] = {"status": "present", "s3Key": s3_key}
    else:
        context.setdefault("release_trust", {})["sbom"] = {
            "status": "missing" if status == "TOOL_NOT_AVAILABLE" else "error",
        }


def execute_release_trust_sign_image(action: Dict[str, Any], context: Dict[str, Any]) -> None:
    """Sign the release image by immutable digest using Cosign + KMS key."""
    rendered = render_value(action, context)
    region = rendered["awsRegion"]
    bucket = rendered["artifactBucket"]
    application = context_application(context)
    release_id = canonical_release_id(rendered, context)

    digest = context.get("release_trust", {}).get("imageDigest")
    if not digest:
        raise HTTPException(
            status_code=422,
            detail="release.trust.sign_image: imageDigest not in context — run resolve_digest first",
        )

    image_uri = context["release_trust"]["imageDoc"]["imageUriByDigest"]
    kms_key_id = rendered.get("kmsSigningKeyId") or os.getenv("HORIZON_KMS_SIGNING_KEY_ID", "")
    signing_role_arn = rendered.get("signingRoleArn") or os.getenv("HORIZON_SIGNING_ROLE_ARN", "")

    if not kms_key_id:
        context.setdefault("release_trust", {})["signature"] = {
            "status": "skipped",
            "reason": "no KMS signing key configured",
        }
        return

    # Use separate signing role (must not be the same as the deploy/runner role)
    sign_env = assume_role_env(signing_role_arn, region, f"horizon-rt-sign-{context['requestId']}")
    key_ref = kms_key_id if kms_key_id.startswith("awskms:") else f"awskms:///{kms_key_id}"

    result = run_command(
        ["cosign", "sign", "--yes", "--key", key_ref, image_uri],
        env=sign_env,
        check=False,
    )
    status = "valid" if result.returncode == 0 else "error"

    sig_doc = {
        "schemaVersion": "2026-06-signature-v1",
        "clientId": config.client_id,
        "application": application,
        "releaseId": release_id,
        "imageDigest": digest,
        "imageUri": image_uri,
        "keyRef": key_ref,
        "status": status,
        "signedAt": utc_now().isoformat(),
        "exitCode": result.returncode,
    }

    local_path = context["runDir"] / "artifacts" / "signature.json"
    write_json(local_path, sig_doc)

    role_env = assume_role_env(rendered.get("roleArn", ""), region, f"horizon-rt-sig-up-{context['requestId']}")
    s3_key = f"release-trust/{application}/{release_id}/signature.json"
    run_command(
        ["aws", "s3", "cp", str(local_path), f"s3://{bucket}/{s3_key}", "--region", region],
        env=role_env,
    )

    context.setdefault("release_trust", {})["signature"] = {"status": status, "s3Key": s3_key}
    if status == "error":
        raise HTTPException(status_code=500, detail="release.trust.sign_image: cosign signing failed")


def execute_release_trust_generate_provenance(action: Dict[str, Any], context: Dict[str, Any]) -> None:
    """Attach a SLSA provenance attestation to the release image using Cosign + KMS key."""
    rendered = render_value(action, context)
    region = rendered["awsRegion"]
    bucket = rendered["artifactBucket"]
    application = context_application(context)
    release_id = canonical_release_id(rendered, context)

    digest = context.get("release_trust", {}).get("imageDigest")
    if not digest:
        raise HTTPException(
            status_code=422,
            detail="release.trust.generate_provenance: imageDigest not in context — run resolve_digest first",
        )

    image_uri = context["release_trust"]["imageDoc"]["imageUriByDigest"]
    kms_key_id = rendered.get("kmsSigningKeyId") or os.getenv("HORIZON_KMS_SIGNING_KEY_ID", "")
    signing_role_arn = rendered.get("signingRoleArn") or os.getenv("HORIZON_SIGNING_ROLE_ARN", "")

    if not kms_key_id:
        context.setdefault("release_trust", {})["provenance"] = {
            "status": "skipped",
            "reason": "no KMS signing key configured",
        }
        return

    rt = context.get("release_trust", {})
    source = rt.get("source", {})

    predicate = {
        "builder": {"id": "https://horizonrelevance.com/horizon-runner/v1"},
        "buildType": "https://horizonrelevance.com/horizon-runner/build/v1",
        "invocation": {
            "configSource": {
                "uri": source.get("repositoryUrl", ""),
                "digest": {"sha1": source.get("commitSha", "")},
                "entryPoint": "horizon-runner",
            },
            "parameters": {
                "application": application,
                "releaseId": release_id,
                "targetEnv": rendered.get("targetEnv", ""),
            },
        },
        "buildConfig": {},
        "metadata": {
            "buildStartedOn": source.get("checkoutAt", utc_now().isoformat()),
            "buildFinishedOn": utc_now().isoformat(),
            "completeness": {"parameters": True, "environment": False, "materials": False},
            "reproducible": False,
        },
        "materials": [
            {
                "uri": source.get("repositoryUrl", ""),
                "digest": {"sha1": source.get("commitSha", "")},
            }
        ],
    }

    local_predicate = context["runDir"] / "artifacts" / "provenance-predicate.json"
    write_json(local_predicate, predicate)

    sign_env = assume_role_env(signing_role_arn, region, f"horizon-rt-prov-{context['requestId']}")
    key_ref = kms_key_id if kms_key_id.startswith("awskms:") else f"awskms:///{kms_key_id}"

    result = run_command(
        [
            "cosign", "attest", "--yes", "--key", key_ref,
            "--predicate", str(local_predicate), "--type", "slsaprovenance", image_uri,
        ],
        env=sign_env,
        check=False,
    )
    status = "valid" if result.returncode == 0 else "error"

    prov_doc = {
        "schemaVersion": "2026-06-provenance-v1",
        "clientId": config.client_id,
        "application": application,
        "releaseId": release_id,
        "imageDigest": digest,
        "imageUri": image_uri,
        "keyRef": key_ref,
        "status": status,
        "attestedAt": utc_now().isoformat(),
        "predicate": predicate,
    }

    local_prov = context["runDir"] / "artifacts" / "provenance.json"
    write_json(local_prov, prov_doc)

    role_env = assume_role_env(rendered.get("roleArn", ""), region, f"horizon-rt-prov-up-{context['requestId']}")
    s3_key = f"release-trust/{application}/{release_id}/provenance.json"
    run_command(
        ["aws", "s3", "cp", str(local_prov), f"s3://{bucket}/{s3_key}", "--region", region],
        env=role_env,
    )

    context.setdefault("release_trust", {})["provenance"] = {"status": status, "s3Key": s3_key}
    if status == "error":
        raise HTTPException(status_code=500, detail="release.trust.generate_provenance: cosign attest failed")


def execute_release_trust_publish_evidence(action: Dict[str, Any], context: Dict[str, Any]) -> None:
    """Create manifest.json with SHA-256 of every evidence file, publish to S3, notify backend."""
    rendered = render_value(action, context)
    region = rendered["awsRegion"]
    bucket = rendered["artifactBucket"]
    application = context_application(context)
    release_id = canonical_release_id(rendered, context)
    role_env = assume_role_env(rendered.get("roleArn", ""), region, f"horizon-rt-manifest-{context['requestId']}")

    rt = context.get("release_trust", {})
    evidence_prefix = rt.get("evidencePrefix", f"release-trust/{application}/{release_id}")

    summary = {
        "schemaVersion": "2026-06-ssctp-v1",
        "clientId": config.client_id,
        "application": application,
        "releaseId": release_id,
        "imageDigest": rt.get("imageDigest"),
        "evidence": {
            "sbom": rt.get("sbom", {}).get("status", "missing"),
            "signature": rt.get("signature", {}).get("status", "missing"),
            "provenance": rt.get("provenance", {}).get("status", "missing"),
        },
        "timestamps": {"createdAt": utc_now().isoformat()},
    }

    local_artifacts = context["runDir"] / "artifacts"
    objects = []
    for file_path in sorted(local_artifacts.rglob("*")):
        if not file_path.is_file():
            continue
        sha = hashlib.sha256(file_path.read_bytes()).hexdigest()
        rel = str(file_path.relative_to(local_artifacts))
        objects.append({
            "path": rel,
            "sizeBytes": file_path.stat().st_size,
            "sha256": f"sha256:{sha}",
            "producer": "horizon-runner",
            "createdAt": utc_now().isoformat(),
        })

    manifest = {
        "schemaVersion": "2026-06-manifest-v1",
        "bundleRevision": 1,
        "clientId": config.client_id,
        "application": application,
        "releaseId": release_id,
        "imageDigest": rt.get("imageDigest"),
        "producer": "horizon-runner",
        "createdAt": utc_now().isoformat(),
        "objects": objects,
    }

    for filename, doc in [("release-trust-summary.json", summary), ("manifest.json", manifest)]:
        local_path = local_artifacts / filename
        write_json(local_path, doc)
        run_command(
            ["aws", "s3", "cp", str(local_path), f"s3://{bucket}/{evidence_prefix}/{filename}", "--region", region],
            env=role_env,
        )

    manifest_bytes = (local_artifacts / "manifest.json").read_bytes()
    manifest_sha = f"sha256:{hashlib.sha256(manifest_bytes).hexdigest()}"

    backend_url = rendered.get("releaseTrustBackendUrl") or os.getenv("HORIZON_RELEASE_TRUST_BACKEND_URL", "")
    if backend_url:
        def evidence_document(status: str, key: str, **extra: Any) -> Dict[str, Any]:
            return {"status": status, "reference": f"s3://{bucket}/{key}", "checksum": manifest_sha, **extra}
        scan_files = list(local_artifacts.rglob("*findings*.json"))
        critical = high = 0
        for scan_file in scan_files:
            try:
                for finding in json.loads(scan_file.read_text()):
                    severity = str(finding.get("severity", "")).upper()
                    critical += severity == "CRITICAL"
                    high += severity == "HIGH"
            except (OSError, ValueError, TypeError):
                continue
        scan_status = "generated" if scan_files else "missing"
        completion = {
            "commit_sha": (rt.get("source") or {}).get("commitSha", ""),
            "image_digest": rt.get("imageDigest", ""),
            "sbom": evidence_document("generated" if rt.get("sbom", {}).get("status") == "present" else "missing", rt.get("sbom", {}).get("s3Key", ""), format="cyclonedx-json"),
            "signature": evidence_document("verified" if rt.get("signature", {}).get("status") == "verified" else "generated" if rt.get("signature", {}).get("status") == "valid" else "missing", rt.get("signature", {}).get("s3Key", ""), provider="cosign"),
            "provenance": evidence_document("generated" if rt.get("provenance", {}).get("status") == "valid" else "missing", rt.get("provenance", {}).get("s3Key", ""), slsa_level="2"),
            "scan_evidence": evidence_document(scan_status, f"{evidence_prefix}/manifest.json", critical=critical, high=high),
            "runner_execution": {"request_id": context["requestId"], "job_name": context.get("runner", {}).get("jobName"), "build_number": context.get("runner", {}).get("buildNumber"), "finished_at": utc_now().isoformat(), "manifest_sha256": manifest_sha},
        }
        headers = {"X-Client-Id": config.client_id, "Content-Type": "application/json"}
        authorization = os.getenv("HORIZON_RELEASE_TRUST_AUTHORIZATION", "")
        if authorization:
            headers["Authorization"] = authorization
        try:
            response = requests.post(
                f"{backend_url.rstrip('/')}/pipeline/api/release-trust/runner/v1/releases/{release_id}/completion",
                json=completion, headers=headers, timeout=30,
            )
            if response.status_code < 200 or response.status_code >= 300:
                raise HTTPException(status_code=502, detail=f"Release Trust completion callback failed: HTTP {response.status_code}: {response.text[:500]}")
        except requests.RequestException as exc:
            raise HTTPException(status_code=502, detail=f"Release Trust completion callback failed: {exc}") from exc

    context["release_trust"]["manifestSha256"] = manifest_sha


def execute_release_trust_verify_promotion(action: Dict[str, Any], context: Dict[str, Any]) -> None:
    """Verify that the deployed image digest matches the approved digest and optionally verify cosign signature."""
    rendered = render_value(action, context)
    region = rendered["awsRegion"]

    rt = context.get("release_trust", {})
    approved_digest = rt.get("imageDigest")
    deployed_digest = (
        rendered.get("deployedDigest")
        or context.get("image", {}).get("digest")
        or context.get("release", {}).get("sourceImageDigest")
    )

    violations: List[str] = []

    if approved_digest and deployed_digest and approved_digest != deployed_digest:
        violations.append(
            f"HR-POL-RT-006 deployed digest {deployed_digest} does not match approved digest {approved_digest}"
        )

    # Optionally verify cosign signature
    image_doc = rt.get("imageDoc", {})
    image_uri = image_doc.get("imageUriByDigest") or deployed_digest
    kms_key_id = rendered.get("kmsSigningKeyId") or os.getenv("HORIZON_KMS_SIGNING_KEY_ID", "")
    if kms_key_id and image_uri and not violations:
        signing_role_arn = rendered.get("signingRoleArn") or os.getenv("HORIZON_SIGNING_ROLE_ARN", "")
        sign_env = assume_role_env(signing_role_arn, region, f"horizon-rt-verify-{context['requestId']}")
        key_ref = kms_key_id if kms_key_id.startswith("awskms:") else f"awskms:///{kms_key_id}"
        verify_result = run_command(
            ["cosign", "verify", "--key", key_ref, image_uri],
            env=sign_env,
            check=False,
        )
        if verify_result.returncode != 0:
            violations.append("HR-POL-RT-003 cosign signature verification failed for deployed image")

    if violations:
        raise HTTPException(status_code=422, detail="; ".join(violations))

    if kms_key_id and image_uri:
        context.setdefault("release_trust", {}).setdefault("signature", {})["status"] = "verified"

    context.setdefault("release_trust", {})["promotionVerification"] = {
        "status": "verified",
        "approvedDigest": approved_digest,
        "deployedDigest": deployed_digest,
        "verifiedAt": utc_now().isoformat(),
    }


STAGE_ALIASES = {
    "checkout": "checkout",
    "source": "checkout",
    "clone": "checkout",
    "scan": "scan",
    "validate": "scan",
    "validation": "scan",
    "quality": "scan",
    "security": "scan",
    "ui": "ui-test",
    "ui-test": "ui-test",
    "ui-e2e": "ui-test",
    "selenium": "ui-test",
    "api": "api-test",
    "api-test": "api-test",
    "api-regression": "api-test",
    "newman": "api-test",
    "performance": "performance-test",
    "performance-test": "performance-test",
    "jmeter": "performance-test",
    "code-quality": "code-quality",
    "sonarqube": "code-quality",
    "static-security": "static-security",
    "checkmarx": "static-security",
    "container-iac": "container-iac",
    "container-iac-vulnerability": "container-iac",
    "trivy": "container-iac",
    "policy": "policy-validation",
    "policy-validation": "policy-validation",
    "opa": "policy-validation",
    "validation-results": "validation-results",
    "publish-validation-results": "validation-results",
    "build": "build",
    "compile": "build",
    "package": "build",
    "publish": "publish",
    "push": "publish",
    "artifact": "publish",
    "deploy": "deploy",
    "release": "deploy",
    "release-trust": "release-trust",
    "trust": "release-trust",
    "evidence": "release-trust",
    "sbom": "release-trust",
    "provenance": "release-trust",
    "signing": "release-trust",
}


def normalize_execution_stage(value: Optional[str]) -> str:
    key = re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower()).strip("-")
    return STAGE_ALIASES.get(key, key)


def action_stage(action: Dict[str, Any]) -> str:
    action_type = action.get("type") or action.get("action") or ""
    action_name = str(action.get("name") or "")
    if action_type == "log":
        return "checkout"
    if action_type in {"git.checkout", "release.load_metadata"}:
        return "checkout"
    if action_type == "security.preflight":
        return "scan"
    if action_type == "quality.ui":
        return "ui-test"
    if action_type == "quality.api":
        return "api-test"
    if action_type == "quality.performance":
        return "performance-test"
    if action_type == "quality.code":
        return "code-quality"
    if action_type == "security.static_code":
        return "static-security"
    if action_type == "security.container_iac":
        return "container-iac"
    if action_type == "security.policy":
        return "policy-validation"
    if action_type == "project.build":
        return "build"
    if action_type == "artifact.publish_reports":
        return "validation-results"
    if action_type == "artifact.context" and "validation" in action_name:
        return "validation-results"
    if action_type in {"image.build_push", "artifact.context", "artifact.publish", "release.promote_image", "release.trust_attest"}:
        return "publish"
    if action_type in {"eks.deploy", "release.publish_approval", "release.trust_gate"}:
        return "deploy"
    if action_type in {
        "release.trust.collect_source",
        "release.trust.resolve_digest",
        "release.trust.generate_sbom",
        "release.trust.sign_image",
        "release.trust.generate_provenance",
        "release.trust.publish_evidence",
        "release.trust.verify_promotion",
    }:
        return "release-trust"
    return "build"


def available_action_stages(actions: List[Dict[str, Any]]) -> List[str]:
    order = [
        "checkout",
        "scan",
        "ui-test",
        "api-test",
        "performance-test",
        "code-quality",
        "static-security",
        "container-iac",
        "policy-validation",
        "build",
        "publish",
        "validation-results",
        "deploy",
        "release-trust",
    ]
    stages = {action_stage(action) for action in actions}
    return [stage for stage in order if stage in stages]


def actions_for_stage(actions: List[Dict[str, Any]], execution_stage: str) -> List[Dict[str, Any]]:
    if not execution_stage:
        return actions
    return [action for action in actions if action_stage(action) == execution_stage]


def execute_actions(actions: List[Dict[str, Any]], request: RunnerRequest) -> List[str]:
    context = load_runner_context(request)
    executed = []
    for idx, action in enumerate(actions):
        action_type = action.get("type") or action.get("action")
        name = action.get("name") or f"action-{idx + 1}"

        if action_type == "log":
            print(action.get("message", name), flush=True)
            executed.append(name)
            save_runner_context(context)
            continue

        if not config.execute_actions:
            executed.append(f"{name}:planned")
            save_runner_context(context)
            continue

        if action_type == "shell":
            if not config.allow_shell:
                raise HTTPException(status_code=403, detail="Shell actions are disabled for this runner")
            subprocess.run(
                action.get("command", ""),
                shell=True,
                check=True,
                cwd=str(config.work_dir),
                timeout=config.timeout_seconds,
            )
            executed.append(name)
            save_runner_context(context)
            continue

        if action_type == "git.checkout":
            execute_git_checkout(action, context)
            executed.append(name)
            save_runner_context(context)
            continue

        if action_type == "project.build":
            execute_project_build(render_value(action, context), context)
            executed.append(name)
            save_runner_context(context)
            continue

        if action_type == "image.build_push":
            execute_image_build_push(action, context)
            executed.append(name)
            save_runner_context(context)
            continue

        if action_type == "artifact.publish":
            execute_artifact_publish(action, context)
            executed.append(name)
            save_runner_context(context)
            continue

        if action_type == "artifact.context":
            execute_artifact_context(action, context)
            executed.append(name)
            save_runner_context(context)
            continue

        if action_type == "artifact.publish_reports":
            execute_publish_reports(action, context)
            executed.append(name)
            save_runner_context(context)
            continue

        if action_type == "quality.ui":
            execute_quality_ui(action, context)
            executed.append(name)
            save_runner_context(context)
            continue

        if action_type == "quality.api":
            execute_quality_api(action, context)
            executed.append(name)
            save_runner_context(context)
            continue

        if action_type == "quality.performance":
            execute_quality_performance(action, context)
            executed.append(name)
            save_runner_context(context)
            continue

        if action_type == "quality.code":
            execute_quality_code(action, context)
            executed.append(name)
            save_runner_context(context)
            continue

        if action_type == "security.preflight":
            execute_security_preflight(action, context)
            executed.append(name)
            save_runner_context(context)
            continue

        if action_type == "security.static_code":
            execute_security_static_code(action, context)
            executed.append(name)
            save_runner_context(context)
            continue

        if action_type == "security.container_iac":
            execute_security_container_iac(action, context)
            executed.append(name)
            save_runner_context(context)
            continue

        if action_type == "security.policy":
            execute_security_policy(action, context)
            executed.append(name)
            save_runner_context(context)
            continue

        if action_type == "release.load_metadata":
            execute_release_load_metadata(action, context)
            executed.append(name)
            save_runner_context(context)
            continue

        if action_type == "release.promote_image":
            execute_release_promote_image(action, context)
            executed.append(name)
            save_runner_context(context)
            continue

        if action_type == "release.publish_approval":
            execute_release_publish_approval(action, context)
            executed.append(name)
            save_runner_context(context)
            continue

        if action_type == "release.trust_attest":
            execute_release_trust_attest(action, context)
            executed.append(name)
            save_runner_context(context)
            continue

        if action_type == "release.trust_gate":
            execute_release_trust_gate(action, context)
            executed.append(name)
            save_runner_context(context)
            continue

        # === RELEASE TRUST ACTIONS ===

        if action_type == "release.trust.collect_source":
            execute_release_trust_collect_source(action, context)
            executed.append(name)
            save_runner_context(context)
            continue

        if action_type == "release.trust.resolve_digest":
            execute_release_trust_resolve_digest(action, context)
            executed.append(name)
            save_runner_context(context)
            continue

        if action_type == "release.trust.generate_sbom":
            execute_release_trust_generate_sbom(action, context)
            executed.append(name)
            save_runner_context(context)
            continue

        if action_type == "release.trust.sign_image":
            execute_release_trust_sign_image(action, context)
            executed.append(name)
            save_runner_context(context)
            continue

        if action_type == "release.trust.generate_provenance":
            execute_release_trust_generate_provenance(action, context)
            executed.append(name)
            save_runner_context(context)
            continue

        if action_type == "release.trust.publish_evidence":
            execute_release_trust_publish_evidence(action, context)
            executed.append(name)
            save_runner_context(context)
            continue

        if action_type == "release.trust.verify_promotion":
            execute_release_trust_verify_promotion(action, context)
            executed.append(name)
            save_runner_context(context)
            continue

        if action_type == "eks.deploy":
            execute_eks_deploy(action, context)
            executed.append(name)
            save_runner_context(context)
            continue
        

        raise HTTPException(status_code=422, detail=f"Unsupported runner action type: {action_type}")
    return executed


@app.get("/healthz")
def healthz() -> Dict[str, Any]:
    return {
        "status": "ok",
        "mode": os.getenv("HORIZON_RUNNER_MODE", "signed-plan"),
        "clientId": config.client_id,
        "installationId": config.installation_id,
        "executionPlanEndpointConfigured": bool(config.plan_endpoint),
        "publicKeyConfigured": bool(config.public_key_path),
        "executeActions": config.execute_actions,
        "kanikoImage": config.kaniko_image,
    }


@app.post("/v1/execute", response_model=RunnerResponse)
def execute(request: RunnerRequest) -> RunnerResponse:
    execution_stage = normalize_execution_stage(request.executionStage)
    emit_event("requested", request, None, {
        "payloadKeys": sorted((request.payload or request.parameters).keys()),
        "executionStage": execution_stage or "all",
    })
    plan = request_execution_plan(request)
    signed_plan = validate_plan(plan)
    plan_id = signed_plan.get("planId") or signed_plan.get("plan_id") or plan.get("planId")
    actions = signed_plan.get("actions") or signed_plan.get("steps") or []
    available_stages = available_action_stages(actions)
    selected_actions = actions_for_stage(actions, execution_stage)
    if execution_stage and not selected_actions:
        message = f"No execution actions mapped to stage '{execution_stage}'."
        executed: List[str] = []
    else:
        executed = execute_actions(selected_actions, request)
        message = f"Execution stage '{execution_stage}' completed" if execution_stage else "Execution plan completed"
    summary_stage = "" if execution_stage in {"", "validation-results"} else execution_stage
    report_summary = {"reports": collect_report_summaries(load_runner_context(request), summary_stage)}
    emit_event("completed", request, plan_id, {
        "executedActions": executed,
        "executionStage": execution_stage or "all",
        "availableStages": available_stages,
        "reportSummary": report_summary,
    })
    return RunnerResponse(
        status="completed",
        requestId=request.requestId,
        planId=plan_id,
        executionStage=execution_stage or None,
        message=message,
        executedActions=executed,
        availableStages=available_stages,
        reportSummary=report_summary,
    )


@app.post("/v1/validate")
async def validate(request: Request) -> Dict[str, Any]:
    plan = await request.json()
    signed_plan = validate_plan(plan)
    return {"status": "valid", "planId": signed_plan.get("planId") or signed_plan.get("plan_id")}
