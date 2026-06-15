import base64
import csv
import hashlib
import json
import math
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
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
    display_args: Optional[List[str]] = None,
) -> subprocess.CompletedProcess:
    visible_args = display_args or args
    print(f"$ {command_text(visible_args)}", flush=True)
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
        raise HTTPException(status_code=500, detail=f"Action command failed ({result.returncode}): {command_text(visible_args)}")
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
        ("jtl", "*.jtl"),
        ("log", "*.log"),
        ("text", "*.txt"),
        ("properties", "*.properties"),
        ("sarif", "*.sarif"),
        ("screenshot", "**/*.png"),
        ("video", "**/*.webm"),
        ("trace", "**/*.zip"),
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
    if summary.get("targetAppUrl"):
        print(f"Target: {summary['targetAppUrl']}", flush=True)
    if summary.get("baseUrl"):
        print(f"API Base URL: {summary['baseUrl']}", flush=True)
    if summary.get("collection"):
        print(f"Collection: {summary['collection']}", flush=True)
    if summary.get("environment"):
        print(f"Environment: {summary['environment']}", flush=True)
    if summary.get("qualityGateStatus") is not None or summary.get("sonarStatus") is not None:
        print(
            "Code Quality: mode={mode} sonar={sonar} gate={gate} project={project} issues={issues} bugs={bugs} vulnerabilities={vulnerabilities} smells={smells} coverage={coverage}".format(
                mode=summary.get("mode", "n/a"),
                sonar=summary.get("sonarStatus", "n/a"),
                gate=summary.get("qualityGateStatus", "n/a"),
                project=summary.get("projectKey", "n/a"),
                issues=summary.get("issueCount", "n/a"),
                bugs=summary.get("measures", {}).get("bugs", "n/a"),
                vulnerabilities=summary.get("measures", {}).get("vulnerabilities", "n/a"),
                smells=summary.get("measures", {}).get("code_smells", "n/a"),
                coverage=summary.get("measures", {}).get("coverage", "n/a"),
            ),
            flush=True,
        )
        for missing in summary.get("missingConfiguration") or []:
            print(f" - [MISSING] {missing}", flush=True)
        for condition in summary.get("qualityGateConditions") or []:
            print(
                " - [{status}] {metric}: actual={actual} threshold={threshold}".format(
                    status=condition.get("status", "UNKNOWN"),
                    metric=condition.get("metricKey", "metric"),
                    actual=condition.get("actualValue", "n/a"),
                    threshold=condition.get("errorThreshold", "n/a"),
                ),
                flush=True,
            )
        for issue in summary.get("issues") or []:
            print(
                " - [{severity}] {type} {component}:{line} {message}".format(
                    severity=issue.get("severity", "UNKNOWN"),
                    type=issue.get("type", "ISSUE"),
                    component=issue.get("component", ""),
                    line=issue.get("line", ""),
                    message=issue.get("message", ""),
                ),
                flush=True,
            )
    elif summary.get("totalSamples") is not None:
        print(
            "Performance: samples={samples} failed={failed} error={error}% avg={avg}ms p90={p90}ms p95={p95}ms p99={p99}ms max={max_ms}ms throughput={throughput}/s duration={duration}s".format(
                samples=summary.get("totalSamples", 0),
                failed=summary.get("failedSamples", 0),
                error=summary.get("errorPercent", 0),
                avg=summary.get("averageResponseMs", summary.get("averageMs", 0)),
                p90=summary.get("p90ResponseMs", 0),
                p95=summary.get("p95ResponseMs", summary.get("p95Ms", 0)),
                p99=summary.get("p99ResponseMs", 0),
                max_ms=summary.get("maxResponseMs", 0),
                throughput=summary.get("throughputPerSecond", 0),
                duration=summary.get("durationSeconds", 0),
            ),
            flush=True,
        )
        if summary.get("generatedSmokePlan"):
            print("Plan: generated smoke/baseline JMX; provide a repository JMX plan for enterprise load coverage.", flush=True)
        thresholds = summary.get("thresholds") or {}
        if thresholds:
            print(f"Thresholds: {thresholds}", flush=True)
        for failure in summary.get("failedThresholds") or []:
            print(f" - [FAILED] {failure}", flush=True)
        for sampler in (summary.get("samplers") or [])[:20]:
            print(
                " - [{status}] {name}: samples={samples} failed={failed} avg={avg}ms p95={p95}ms p99={p99}ms throughput={throughput}/s codes={codes}".format(
                    status=sampler.get("status", "UNKNOWN"),
                    name=sampler.get("name") or "sampler",
                    samples=sampler.get("samples", 0),
                    failed=sampler.get("failures", 0),
                    avg=sampler.get("averageMs", 0),
                    p95=sampler.get("p95Ms", 0),
                    p99=sampler.get("p99Ms", 0),
                    throughput=sampler.get("throughputPerSecond", 0),
                    codes=sampler.get("responseCodes", {}),
                ),
                flush=True,
            )
    elif summary.get("findingCounts") is not None or summary.get("dashboardUpload") is not None:
        counts = summary.get("findingCounts") or {}
        blocking = summary.get("blockingFindings", 0)
        print(
            "Security Findings: total={total} blocking={blocking} critical={critical} high={high} medium={medium} low={low} unknown={unknown}".format(
                total=summary.get("findingCount", 0),
                blocking=blocking,
                critical=counts.get("CRITICAL", 0),
                high=counts.get("HIGH", 0),
                medium=counts.get("MEDIUM", 0),
                low=counts.get("LOW", 0),
                unknown=counts.get("UNKNOWN", 0),
            ),
            flush=True,
        )
        if summary.get("tools"):
            print(f"Security tools: {', '.join(summary.get('tools') or [])}", flush=True)
        upload = summary.get("dashboardUpload") or {}
        if upload:
            print(
                "Dashboard upload: status={status} endpoint={endpoint} uploaded={count}".format(
                    status=upload.get("status", "UNKNOWN"),
                    endpoint=upload.get("endpoint", "not configured"),
                    count=upload.get("count", 0),
                ),
                flush=True,
            )
        for failure in summary.get("failedThresholds") or []:
            print(f" - [FAILED] {failure}", flush=True)
        for finding in (summary.get("sampleFindings") or [])[:20]:
            print(
                " - [{severity}] {source} {component} {target} {rule}: {description}".format(
                    severity=finding.get("severity", "UNKNOWN"),
                    source=finding.get("source", "Security Finding"),
                    component=finding.get("package_name") or "component",
                    target=finding.get("target") or "",
                    rule=finding.get("vulnerability_id") or finding.get("rule") or "",
                    description=(finding.get("description") or "")[:180],
                ),
                flush=True,
            )
    else:
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


def _jmeter_plan_candidates(action: Dict[str, Any]) -> List[str]:
    candidates = [
        str(action.get("testPlan") or "").strip(),
        "tests/jmeter/test.jmx",
        "tests/jmeter/performance.jmx",
        "tests/jmeter/load-test.jmx",
        "tests/performance/test.jmx",
        "tests/performance/performance.jmx",
        "qe/performance/E2E_performance.jmx",
    ]
    seen = set()
    ordered = []
    for candidate in candidates:
        if candidate and candidate not in seen:
            ordered.append(candidate)
            seen.add(candidate)
    return ordered


def _jmeter_percentile(values: List[int], percentile: float) -> int:
    if not values:
        return 0
    sorted_values = sorted(values)
    index = max(0, min(len(sorted_values) - 1, math.ceil(len(sorted_values) * percentile / 100) - 1))
    return sorted_values[index]


def _jmeter_generate_smoke_plan(plan_path: Path) -> None:
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text("""<?xml version="1.0" encoding="UTF-8"?>
<jmeterTestPlan version="1.2" properties="5.0" jmeter="5.6.3">
  <hashTree>
    <TestPlan guiclass="TestPlanGui" testclass="TestPlan" testname="Horizon Generated Smoke Performance Test" enabled="true">
      <stringProp name="TestPlan.comments">Generated by Horizon runner when no repository JMX plan is supplied. Use a repository-owned JMX plan for enterprise load coverage.</stringProp>
      <boolProp name="TestPlan.functional_mode">false</boolProp>
      <boolProp name="TestPlan.tearDown_on_shutdown">true</boolProp>
      <boolProp name="TestPlan.serialize_threadgroups">false</boolProp>
      <elementProp name="TestPlan.user_defined_variables" elementType="Arguments" guiclass="ArgumentsPanel" testclass="Arguments" testname="User Defined Variables" enabled="true">
        <collectionProp name="Arguments.arguments"/>
      </elementProp>
    </TestPlan>
    <hashTree>
      <ThreadGroup guiclass="ThreadGroupGui" testclass="ThreadGroup" testname="Baseline Availability Load" enabled="true">
        <stringProp name="ThreadGroup.on_sample_error">continue</stringProp>
        <elementProp name="ThreadGroup.main_controller" elementType="LoopController" guiclass="LoopControlPanel" testclass="LoopController" testname="Loop Controller" enabled="true">
          <boolProp name="LoopController.continue_forever">false</boolProp>
          <stringProp name="LoopController.loops">${__P(jmeterLoops,5)}</stringProp>
        </elementProp>
        <stringProp name="ThreadGroup.num_threads">${__P(jmeterThreads,10)}</stringProp>
        <stringProp name="ThreadGroup.ramp_time">${__P(jmeterRampSeconds,30)}</stringProp>
        <boolProp name="ThreadGroup.scheduler">false</boolProp>
      </ThreadGroup>
      <hashTree>
        <ConfigTestElement guiclass="HttpDefaultsGui" testclass="ConfigTestElement" testname="HTTP Request Defaults" enabled="true">
          <stringProp name="HTTPSampler.domain">${__P(jmeterHost,localhost)}</stringProp>
          <stringProp name="HTTPSampler.port">${__P(jmeterPort,80)}</stringProp>
          <stringProp name="HTTPSampler.protocol">${__P(jmeterProtocol,http)}</stringProp>
          <elementProp name="HTTPsampler.Arguments" elementType="Arguments" guiclass="HTTPArgumentsPanel" testclass="Arguments" enabled="true">
            <collectionProp name="Arguments.arguments"/>
          </elementProp>
        </ConfigTestElement>
        <hashTree/>
        <HTTPSamplerProxy guiclass="HttpTestSampleGui" testclass="HTTPSamplerProxy" testname="Application availability" enabled="true">
          <stringProp name="HTTPSampler.path">${__P(jmeterPath,/)}</stringProp>
          <stringProp name="HTTPSampler.method">GET</stringProp>
          <boolProp name="HTTPSampler.follow_redirects">true</boolProp>
          <boolProp name="HTTPSampler.auto_redirects">false</boolProp>
          <boolProp name="HTTPSampler.use_keepalive">true</boolProp>
          <boolProp name="HTTPSampler.DO_MULTIPART_POST">false</boolProp>
        </HTTPSamplerProxy>
        <hashTree>
          <ResponseAssertion guiclass="AssertionGui" testclass="ResponseAssertion" testname="HTTP success response" enabled="true">
            <collectionProp name="Asserion.test_strings">
              <stringProp name="49586">200</stringProp>
            </collectionProp>
            <stringProp name="Assertion.custom_message">Expected HTTP 200 from application endpoint.</stringProp>
            <stringProp name="Assertion.test_field">Assertion.response_code</stringProp>
            <boolProp name="Assertion.assume_success">false</boolProp>
            <intProp name="Assertion.test_type">8</intProp>
          </ResponseAssertion>
          <hashTree/>
        </hashTree>
      </hashTree>
    </hashTree>
  </hashTree>
</jmeterTestPlan>
""", encoding="utf-8")


def _jmeter_row_value(row: Dict[str, Any], *keys: str) -> str:
    lowered = {str(key).lower(): value for key, value in row.items()}
    for key in keys:
        value = lowered.get(key.lower())
        if value is not None:
            return str(value)
    return ""


def _jmeter_sampler_stats(label: str, rows: List[Dict[str, Any]], duration_seconds: float) -> Dict[str, Any]:
    elapsed_values: List[int] = []
    failures = 0
    response_codes: Dict[str, int] = {}
    bytes_received = 0
    for row in rows:
        elapsed = parse_int(_jmeter_row_value(row, "elapsed", "Elapsed"))
        elapsed_values.append(elapsed)
        success = _jmeter_row_value(row, "success", "Success").strip().lower() == "true"
        code = _jmeter_row_value(row, "responseCode", "response_code", "ResponseCode") or "unknown"
        response_codes[code] = response_codes.get(code, 0) + 1
        bytes_received += parse_int(_jmeter_row_value(row, "bytes", "Bytes", "receivedBytes"))
        if not success:
            failures += 1
    total = len(rows)
    error_percent = round((failures / total) * 100, 2) if total else 0.0
    average_ms = round(sum(elapsed_values) / total, 2) if total else 0.0
    return {
        "name": label,
        "status": "PASSED" if failures == 0 else "FAILED",
        "samples": total,
        "failures": failures,
        "errorPercent": error_percent,
        "averageMs": average_ms,
        "minMs": min(elapsed_values) if elapsed_values else 0,
        "p90Ms": _jmeter_percentile(elapsed_values, 90),
        "p95Ms": _jmeter_percentile(elapsed_values, 95),
        "p99Ms": _jmeter_percentile(elapsed_values, 99),
        "maxMs": max(elapsed_values) if elapsed_values else 0,
        "throughputPerSecond": round(total / duration_seconds, 2) if duration_seconds > 0 else 0.0,
        "responseCodes": response_codes,
        "bytesReceived": bytes_received,
    }


def _jmeter_parse_results(jtl: Path) -> Dict[str, Any]:
    if not jtl.exists():
        return {"samples": 0, "samplers": [], "responseCodes": {}, "message": "JMeter results.jtl was not produced."}
    try:
        rows = list(csv.DictReader(jtl.open(encoding="utf-8")))
    except OSError:
        rows = []
    rows = [row for row in rows if any(str(value or "").strip() for value in row.values())]
    if not rows:
        return {"samples": 0, "samplers": [], "responseCodes": {}, "message": "JMeter produced no samples."}

    elapsed_values: List[int] = []
    timestamps: List[int] = []
    failures = 0
    response_codes: Dict[str, int] = {}
    sampler_rows: Dict[str, List[Dict[str, Any]]] = {}
    bytes_received = 0
    for row in rows:
        elapsed = parse_int(_jmeter_row_value(row, "elapsed", "Elapsed"))
        elapsed_values.append(elapsed)
        timestamp = parse_int(_jmeter_row_value(row, "timeStamp", "timestamp", "Timestamp"))
        if timestamp:
            timestamps.append(timestamp)
            timestamps.append(timestamp + elapsed)
        success = _jmeter_row_value(row, "success", "Success").strip().lower() == "true"
        if not success:
            failures += 1
        code = _jmeter_row_value(row, "responseCode", "response_code", "ResponseCode") or "unknown"
        response_codes[code] = response_codes.get(code, 0) + 1
        label = _jmeter_row_value(row, "label", "Label") or "JMeter sampler"
        sampler_rows.setdefault(label, []).append(row)
        bytes_received += parse_int(_jmeter_row_value(row, "bytes", "Bytes", "receivedBytes"))

    total = len(rows)
    duration_seconds = round((max(timestamps) - min(timestamps)) / 1000, 3) if len(timestamps) >= 2 else 0.0
    if duration_seconds <= 0 and elapsed_values:
        duration_seconds = round(sum(elapsed_values) / 1000, 3)
    samplers = [
        _jmeter_sampler_stats(label, grouped_rows, duration_seconds)
        for label, grouped_rows in sorted(sampler_rows.items())
    ]
    error_percent = round((failures / total) * 100, 2) if total else 0.0
    average_ms = round(sum(elapsed_values) / total, 2) if total else 0.0
    return {
        "samples": total,
        "failures": failures,
        "errorPercent": error_percent,
        "averageMs": average_ms,
        "minMs": min(elapsed_values) if elapsed_values else 0,
        "p90Ms": _jmeter_percentile(elapsed_values, 90),
        "p95Ms": _jmeter_percentile(elapsed_values, 95),
        "p99Ms": _jmeter_percentile(elapsed_values, 99),
        "maxMs": max(elapsed_values) if elapsed_values else 0,
        "durationSeconds": duration_seconds,
        "throughputPerSecond": round(total / duration_seconds, 2) if duration_seconds > 0 else 0.0,
        "responseCodes": response_codes,
        "samplers": samplers,
        "bytesReceived": bytes_received,
    }


def execute_quality_performance(action: Dict[str, Any], context: Dict[str, Any]) -> None:
    source_dir = get_source_dir(context)
    report_dir = action_report_dir(action, context, "jmeter")
    base_url = normalize_http_url(action.get("baseUrl"))
    test_plan = ""
    for candidate in _jmeter_plan_candidates(action):
        if (source_dir / candidate).exists():
            test_plan = candidate
            break
    generated_smoke_plan = False
    if test_plan:
        test_plan_arg = test_plan
        test_plan_path = source_dir / test_plan
    else:
        if not base_url:
            raise HTTPException(status_code=422, detail="JMeter requires JMETER_BASE_URL, TARGET_APP_URL, API_BASE_URL, or a repository JMX plan.")
        generated_smoke_plan = True
        test_plan_path = report_dir / "generated-performance-smoke.jmx"
        _jmeter_generate_smoke_plan(test_plan_path)
        test_plan_arg = str(test_plan_path)

    parsed = urlparse(base_url) if base_url else urlparse("http://localhost/")
    protocol = parsed.scheme or "http"
    host = parsed.hostname or "localhost"
    port = str(parsed.port or (443 if protocol == "https" else 80))
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    jtl = report_dir / "results.jtl"
    jmeter_log = report_dir / "jmeter.log"
    html_dir = report_dir / "html"
    runtime_props = report_dir / "jmeter-runtime.properties"
    threads = parse_int(action.get("threads"), 10)
    ramp_seconds = parse_int(action.get("rampSeconds"), 30)
    loops = parse_int(action.get("loops"), 5)
    max_error = parse_float(action.get("maxErrorPercent"), 1.0)
    max_avg = parse_float(action.get("maxAvgMs"), 2000.0)
    max_p95 = parse_float(action.get("maxP95Ms"), 5000.0)
    runtime_props.write_text(
        "\n".join([
            f"protocol={protocol}",
            f"host={host}",
            f"port={port}",
            f"base_path={path}",
            f"jmeterProtocol={protocol}",
            f"jmeterHost={host}",
            f"jmeterPort={port}",
            f"jmeterPath={path}",
            f"jmeterThreads={threads}",
            f"jmeterRampSeconds={ramp_seconds}",
            f"jmeterLoops={loops}",
            "",
        ]),
        encoding="utf-8",
    )
    if html_dir.exists():
        shutil.rmtree(html_dir)
    cmd = [
        "jmeter", "-n", "-t", test_plan_arg,
        "-l", str(jtl),
        "-j", str(jmeter_log),
        "-e", "-o", str(html_dir),
        "-q", str(runtime_props),
        f"-Jprotocol={protocol}",
        f"-Jhost={host}",
        f"-Jport={port}",
        f"-Jbase_path={path}",
        f"-JjmeterProtocol={protocol}",
        f"-JjmeterHost={host}",
        f"-JjmeterPort={port}",
        f"-JjmeterPath={path}",
        f"-Jthreads={threads}",
        f"-JrampSeconds={ramp_seconds}",
        f"-Jloops={loops}",
        f"-JjmeterThreads={threads}",
        f"-JjmeterRampSeconds={ramp_seconds}",
        f"-JjmeterLoops={loops}",
        "-Jjmeter.save.saveservice.output_format=csv",
        "-Jjmeter.save.saveservice.print_field_names=true",
        "-Jjmeter.save.saveservice.timestamp_format=ms",
        "-Jjmeter.save.saveservice.successful=true",
        "-Jjmeter.save.saveservice.elapsed=true",
        "-Jjmeter.save.saveservice.label=true",
        "-Jjmeter.save.saveservice.response_code=true",
        "-Jjmeter.save.saveservice.response_message=true",
        "-Jjmeter.save.saveservice.bytes=true",
        "-Jjmeter.save.saveservice.thread_counts=true",
    ]
    (report_dir / "jmeter-command.txt").write_text(shlex.join(cmd) + "\n", encoding="utf-8")
    result = run_command(cmd, cwd=source_dir, check=False)
    output_tail = command_output_tail(result)
    parsed_results = _jmeter_parse_results(jtl)
    failed_thresholds: List[str] = []
    if result.returncode != 0:
        failed_thresholds.append(f"JMeter exited with code {result.returncode}.")
    if parsed_results.get("samples", 0) == 0:
        failed_thresholds.append(str(parsed_results.get("message") or "JMeter produced no samples."))
    if parsed_results.get("errorPercent", 0.0) > max_error:
        failed_thresholds.append(f"Error percent {parsed_results.get('errorPercent')} exceeded threshold {max_error}.")
    if parsed_results.get("averageMs", 0.0) > max_avg:
        failed_thresholds.append(f"Average response {parsed_results.get('averageMs')}ms exceeded threshold {max_avg}ms.")
    if parsed_results.get("p95Ms", 0) > max_p95:
        failed_thresholds.append(f"P95 response {parsed_results.get('p95Ms')}ms exceeded threshold {max_p95}ms.")
    passed = not failed_thresholds
    artifacts = report_artifacts(report_dir, context)
    summary = {
        "tool": "jmeter",
        "toolName": "Performance Test",
        "framework": "jmeter",
        "stage": "performance-test",
        "status": "PASSED" if passed else "FAILED",
        "baseUrl": base_url,
        "testPlan": test_plan_arg,
        "testPlanSource": "generated-smoke" if generated_smoke_plan else "repository",
        "generatedSmokePlan": generated_smoke_plan,
        "command": shlex.join(cmd),
        "exitCode": result.returncode,
        "requestId": context["requestId"],
        "project": context.get("project", {}),
        "git": context.get("git", {}),
        "runner": context.get("runner", {}),
        "reportDir": relative_path(report_dir, context["runDir"]),
        "threads": threads,
        "rampSeconds": ramp_seconds,
        "loops": loops,
        "samples": parsed_results.get("samples", 0),
        "totalSamples": parsed_results.get("samples", 0),
        "failures": parsed_results.get("failures", 0),
        "failedSamples": parsed_results.get("failures", 0),
        "totalTests": parsed_results.get("samples", 0),
        "passedTests": max(parsed_results.get("samples", 0) - parsed_results.get("failures", 0), 0),
        "failedTests": parsed_results.get("failures", 0),
        "errorTests": 0,
        "skippedTests": 0,
        "errorPercent": parsed_results.get("errorPercent", 0.0),
        "averageMs": parsed_results.get("averageMs", 0.0),
        "averageResponseMs": parsed_results.get("averageMs", 0.0),
        "minResponseMs": parsed_results.get("minMs", 0),
        "p90ResponseMs": parsed_results.get("p90Ms", 0),
        "p95Ms": parsed_results.get("p95Ms", 0),
        "p95ResponseMs": parsed_results.get("p95Ms", 0),
        "p99ResponseMs": parsed_results.get("p99Ms", 0),
        "maxResponseMs": parsed_results.get("maxMs", 0),
        "durationSeconds": parsed_results.get("durationSeconds", 0),
        "throughputPerSecond": parsed_results.get("throughputPerSecond", 0.0),
        "responseCodes": parsed_results.get("responseCodes", {}),
        "samplers": parsed_results.get("samplers", []),
        "bytesReceived": parsed_results.get("bytesReceived", 0),
        "thresholds": {"maxErrorPercent": max_error, "maxAvgMs": max_avg, "maxP95Ms": max_p95},
        "failedThresholds": failed_thresholds,
        "artifacts": artifacts,
    }
    if output_tail:
        summary["outputTail"] = output_tail
    write_json(report_dir / "summary.json", summary)
    write_json(report_dir / "evidence.json", {
        **summary,
        "generatedAt": utc_now().isoformat(),
        "evidenceType": "validation.performance",
    })
    print_quality_summary(summary)
    if not passed:
        raise HTTPException(status_code=500, detail={
            "message": "Performance test failed",
            "failedThresholds": failed_thresholds,
            "testPlan": test_plan_arg,
            "baseUrl": base_url,
            "exitCode": result.returncode,
            "reportDir": str(report_dir.relative_to(context["runDir"])),
            "outputTail": output_tail,
        })


def _sonar_sanitize_key(value: Any) -> str:
    key = re.sub(r"[^A-Za-z0-9_.:-]+", "-", str(value or "application").strip().lower()).strip("-")
    return key or "application"


def _sonar_project_type(action: Dict[str, Any], context: Dict[str, Any]) -> str:
    payload = context.get("requestPayload") or {}
    return str(action.get("projectType") or payload.get("PROJECT_TYPE") or "").strip().lower()


def _sonar_existing_paths(source_dir: Path, candidates: List[str]) -> str:
    return ",".join(candidate for candidate in candidates if (source_dir / candidate).exists())


def _sonar_read_project_key(properties_path: Path) -> str:
    if not properties_path.exists():
        return ""
    for line in properties_path.read_text(errors="ignore").splitlines():
        if line.strip().startswith("sonar.projectKey="):
            return line.split("=", 1)[1].strip()
    return ""


def _sonar_generate_project_properties(source_dir: Path, action: Dict[str, Any], context: Dict[str, Any]) -> str:
    props = source_dir / "sonar-project.properties"
    existing_key = _sonar_read_project_key(props)
    if existing_key:
        return existing_key

    payload = context.get("requestPayload") or {}
    project_name = str(action.get("projectName") or payload.get("PROJECT_NAME") or context.get("project", {}).get("name") or context["requestId"]).strip()
    project_key = _sonar_sanitize_key(action.get("projectKey") or payload.get("SONAR_PROJECT_KEY") or project_name)
    project_type = _sonar_project_type(action, context)
    lines = [
        f"sonar.projectKey={project_key}",
        f"sonar.projectName={project_name or project_key}",
        "sonar.sourceEncoding=UTF-8",
        "sonar.scm.provider=git",
    ]

    if project_type in {"angular", "nodejs", "webcomponent"}:
        sources = _sonar_existing_paths(source_dir, ["src", "server"]) or "."
        tests = _sonar_existing_paths(source_dir, ["src", "tests"])
        lines.extend([
            f"sonar.sources={sources}",
            "sonar.exclusions=**/node_modules/**,**/dist/**,**/build/**,**/coverage/**,**/*.spec.ts,**/*.spec.js,**/*.test.ts,**/*.test.js",
            "sonar.test.inclusions=**/*.spec.ts,**/*.spec.js,**/*.test.ts,**/*.test.js,tests/**/*.js,tests/**/*.ts",
            "sonar.javascript.lcov.reportPaths=coverage/lcov.info,coverage/**/lcov.info",
        ])
        if tests:
            lines.append(f"sonar.tests={tests}")
    elif project_type in {"springboot", "springboot-java11", "java"}:
        java_sources = _sonar_existing_paths(source_dir, ["src/main/java", "src/main/kotlin"]) or "src/main/java"
        java_tests = _sonar_existing_paths(source_dir, ["src/test/java", "src/test/kotlin"])
        java_binaries = _sonar_existing_paths(source_dir, ["target/classes", "build/classes/java/main", "build/classes/kotlin/main"]) or "target/classes"
        junit_reports = _sonar_existing_paths(source_dir, ["target/surefire-reports", "build/test-results/test"])
        jacoco_reports = _sonar_existing_paths(source_dir, ["target/site/jacoco/jacoco.xml", "build/reports/jacoco/test/jacocoTestReport.xml"])
        lines.extend([
            f"sonar.sources={java_sources}",
            f"sonar.java.binaries={java_binaries}",
            f"sonar.coverage.jacoco.xmlReportPaths={jacoco_reports or 'target/site/jacoco/jacoco.xml'}",
        ])
        if java_tests:
            lines.append(f"sonar.tests={java_tests}")
        if junit_reports:
            lines.append(f"sonar.junit.reportPaths={junit_reports}")
    else:
        lines.extend([
            "sonar.sources=.",
            "sonar.exclusions=**/.git/**,**/node_modules/**,**/dist/**,**/build/**,**/target/**,**/coverage/**",
        ])

    props.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return project_key


def _sonar_run_coverage(source_dir: Path, action: Dict[str, Any], context: Dict[str, Any]) -> List[Dict[str, Any]]:
    project_type = _sonar_project_type(action, context)
    coverage_steps: List[Dict[str, Any]] = []
    if as_bool(action.get("skipCoverage"), False):
        return [{"name": "coverage", "status": "SKIPPED", "message": "Coverage preparation was disabled for this scan."}]
    if project_type in {"angular", "nodejs", "webcomponent"}:
        package_json = source_dir / "package.json"
        if not package_json.exists():
            return [{"name": "javascript-coverage", "status": "SKIPPED", "message": "package.json not found."}]
        install_cmd = ["npm", "ci"] if (source_dir / "package-lock.json").exists() else ["npm", "install"]
        install = run_command(install_cmd, cwd=source_dir, check=False)
        coverage_steps.append({"name": "npm-install", "status": "PASSED" if install.returncode == 0 else "FAILED", "exitCode": install.returncode})
        if install.returncode != 0:
            coverage_steps[-1]["outputTail"] = command_output_tail(install)
            return coverage_steps
        try:
            package = json.loads(package_json.read_text())
        except json.JSONDecodeError:
            package = {}
        scripts = package.get("scripts") or {}
        if "test:coverage" in scripts:
            test_cmd = ["npm", "run", "test:coverage"]
        elif "test" in scripts:
            test_cmd = ["npm", "test", "--", "--watch=false", "--browsers=ChromeHeadless", "--code-coverage"]
        else:
            coverage_steps.append({"name": "javascript-coverage", "status": "SKIPPED", "message": "No npm test script found."})
            return coverage_steps
        test = run_command(test_cmd, cwd=source_dir, check=False)
        coverage_steps.append({"name": "javascript-coverage", "status": "PASSED" if test.returncode == 0 else "FAILED", "exitCode": test.returncode})
        if test.returncode != 0:
            coverage_steps[-1]["outputTail"] = command_output_tail(test)
    elif project_type in {"springboot", "springboot-java11", "java"}:
        if (source_dir / "mvnw").exists():
            run_command(["chmod", "+x", "mvnw"], cwd=source_dir, check=False)
            cmd = ["./mvnw", "-B", "clean", "verify"]
        elif (source_dir / "pom.xml").exists():
            cmd = ["mvn", "-B", "clean", "verify"]
        elif (source_dir / "gradlew").exists():
            run_command(["chmod", "+x", "gradlew"], cwd=source_dir, check=False)
            cmd = ["./gradlew", "clean", "test", "jacocoTestReport"]
        elif (source_dir / "build.gradle").exists() or (source_dir / "build.gradle.kts").exists():
            cmd = ["gradle", "clean", "test", "jacocoTestReport"]
        else:
            return [{"name": "java-coverage", "status": "SKIPPED", "message": "No Maven or Gradle build file found."}]
        result = run_command(cmd, cwd=source_dir, check=False)
        coverage_steps.append({"name": "java-coverage", "status": "PASSED" if result.returncode == 0 else "FAILED", "exitCode": result.returncode})
        if result.returncode != 0:
            coverage_steps[-1]["outputTail"] = command_output_tail(result)
    else:
        coverage_steps.append({"name": "coverage", "status": "SKIPPED", "message": f"No coverage preparation rule for project type '{project_type}'."})
    return coverage_steps


def _sonar_parse_report_task(source_dir: Path) -> Dict[str, str]:
    task_path = source_dir / ".scannerwork" / "report-task.txt"
    values: Dict[str, str] = {}
    if not task_path.exists():
        return values
    for line in task_path.read_text(errors="ignore").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    return values


def _sonar_get_json(host_url: str, path: str, token: str = "", params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    url = host_url.rstrip("/") + path
    auth = (token, "") if token else None
    response = requests.get(url, params=params or {}, auth=auth, timeout=30)
    response.raise_for_status()
    return response.json()


def _sonar_wait_for_quality_gate(host_url: str, token: str, ce_task_id: str, timeout_seconds: int = 600) -> Dict[str, Any]:
    deadline = time.time() + timeout_seconds
    last_task: Dict[str, Any] = {}
    while time.time() < deadline:
        last_task = _sonar_get_json(host_url, "/api/ce/task", token, {"id": ce_task_id})
        task = last_task.get("task") or {}
        status = str(task.get("status") or "").upper()
        if status == "SUCCESS":
            analysis_id = task.get("analysisId") or ""
            if not analysis_id:
                raise RuntimeError("Sonar Compute Engine task succeeded but did not return analysisId.")
            quality_gate = _sonar_get_json(host_url, "/api/qualitygates/project_status", token, {"analysisId": analysis_id})
            return {"ceTask": last_task, "analysisId": analysis_id, "qualityGate": quality_gate}
        if status in {"FAILED", "CANCELED"}:
            raise RuntimeError(f"Sonar Compute Engine task ended with status {status}.")
        time.sleep(10)
    raise RuntimeError("Timed out waiting for Sonar Compute Engine task to finish.")


def _sonar_collect_measures(host_url: str, token: str, project_key: str) -> Dict[str, Any]:
    metric_keys = "ncloc,bugs,vulnerabilities,code_smells,coverage,duplicated_lines_density,security_hotspots,reliability_rating,security_rating,sqale_rating"
    try:
        data = _sonar_get_json(host_url, "/api/measures/component", token, {"component": project_key, "metricKeys": metric_keys})
    except requests.RequestException as exc:
        return {"collectionError": str(exc)}
    measures: Dict[str, Any] = {}
    for item in ((data.get("component") or {}).get("measures") or []):
        measures[str(item.get("metric"))] = item.get("value")
    return measures


def _sonar_collect_issues(host_url: str, token: str, project_key: str) -> Dict[str, Any]:
    try:
        data = _sonar_get_json(host_url, "/api/issues/search", token, {"componentKeys": project_key, "resolved": "false", "ps": 100})
    except requests.RequestException as exc:
        return {"total": 0, "issues": [], "collectionError": str(exc)}
    issues = []
    for issue in data.get("issues") or []:
        issues.append({
            "key": issue.get("key"),
            "rule": issue.get("rule"),
            "severity": issue.get("severity"),
            "type": issue.get("type"),
            "component": issue.get("component"),
            "line": issue.get("line"),
            "message": issue.get("message"),
        })
    return {"total": data.get("total", len(issues)), "issues": issues}


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
    required = as_bool(action.get("required"), True)
    host_url = normalize_http_url(action.get("hostUrl") or os.getenv("SONAR_HOST_URL") or os.getenv("HORIZON_SONAR_HOST_URL"))
    token = str(action.get("token") or os.getenv("SONAR_TOKEN") or "").strip()
    scanner = shutil.which("sonar-scanner")
    missing = []
    if not host_url:
        missing.append("SONAR_HOST_URL is not configured for the runner.")
    if not scanner:
        missing.append("sonar-scanner CLI is not installed in the runner image.")
    if not token:
        missing.append("SONAR_TOKEN is not configured. Use a project/service token, not a human password.")

    project_key = action.get("projectKey") or _sonar_sanitize_key((context.get("requestPayload") or {}).get("PROJECT_NAME") or context.get("project", {}).get("name"))
    summary: Dict[str, Any] = {
        "tool": "sonarqube",
        "toolName": "Code Quality Scan",
        "framework": "sonarqube",
        "stage": "code-quality",
        "mode": "SONAR_SCANNER" if not missing else "LOCAL_CODE_QUALITY",
        "status": "FAILED" if missing and required else "PASSED",
        "sonarStatus": "NOT_CONFIGURED" if missing else "PENDING",
        "qualityGateStatus": "NOT_RUN" if missing else "PENDING",
        "projectKey": project_key,
        "projectType": _sonar_project_type(action, context),
        "sonarUrl": host_url,
        "sourceFiles": len(source_files),
        "linesOfCode": lines,
        "missingConfiguration": missing,
        "requestId": context["requestId"],
        "project": context.get("project", {}),
        "git": context.get("git", {}),
        "runner": context.get("runner", {}),
        "reportDir": relative_path(report_dir, context["runDir"]),
        "totalTests": 0,
        "passedTests": 0,
        "failedTests": 0,
        "errorTests": 0,
        "skippedTests": 0,
    }

    if not missing:
        project_key = _sonar_generate_project_properties(source_dir, action, context)
        summary["projectKey"] = project_key
        coverage_steps = _sonar_run_coverage(source_dir, action, context)
        summary["coveragePreparation"] = coverage_steps
        cmd = [
            "sonar-scanner",
            "-Dproject.settings=sonar-project.properties",
            f"-Dsonar.host.url={host_url}",
            f"-Dsonar.token={token}",
        ]
        redacted_cmd = cmd[:-1] + ["-Dsonar.token=***"]
        (report_dir / "sonar-scanner-command.txt").write_text(shlex.join(redacted_cmd) + "\n", encoding="utf-8")
        result = run_command(cmd, cwd=source_dir, check=False, display_args=redacted_cmd)
        summary["command"] = shlex.join(redacted_cmd)
        summary["exitCode"] = result.returncode
        summary["sonarStatus"] = "PASSED" if result.returncode == 0 else "FAILED"
        if result.returncode != 0:
            summary["status"] = "FAILED"
            summary["qualityGateStatus"] = "NOT_RUN"
            summary["outputTail"] = command_output_tail(result)
        else:
            report_task = _sonar_parse_report_task(source_dir)
            summary["ceTaskId"] = report_task.get("ceTaskId")
            summary["dashboardUrl"] = report_task.get("dashboardUrl")
            summary["serverUrl"] = report_task.get("serverUrl")
            try:
                gate_result = _sonar_wait_for_quality_gate(host_url, token, str(report_task.get("ceTaskId") or ""))
                ce_task = gate_result.get("ceTask") or {}
                quality_gate = gate_result.get("qualityGate") or {}
                project_status = quality_gate.get("projectStatus") or {}
                summary["analysisId"] = gate_result.get("analysisId")
                summary["sonarStatus"] = "PASSED"
                summary["qualityGateStatus"] = project_status.get("status") or "UNKNOWN"
                summary["qualityGateConditions"] = project_status.get("conditions") or []
                summary["ceTaskStatus"] = (ce_task.get("task") or {}).get("status")
                summary["status"] = "PASSED" if summary["qualityGateStatus"] == "OK" else "FAILED"
                write_json(report_dir / "sonar-ce-task.json", ce_task)
                write_json(report_dir / "sonar-quality-gate.json", quality_gate)
            except (requests.RequestException, RuntimeError) as exc:
                summary["status"] = "FAILED"
                summary["sonarStatus"] = "FAILED"
                summary["qualityGateStatus"] = "UNKNOWN"
                summary["qualityGateError"] = str(exc)
            measures = _sonar_collect_measures(host_url, token, project_key)
            issues_result = _sonar_collect_issues(host_url, token, project_key)
            summary["measures"] = measures
            summary["issueCount"] = issues_result.get("total", 0)
            summary["issues"] = issues_result.get("issues", [])[:25]
            if issues_result.get("collectionError"):
                summary["issuesCollectionError"] = issues_result["collectionError"]
            if measures.get("collectionError"):
                summary["measuresCollectionError"] = measures["collectionError"]
            write_json(report_dir / "issues.json", issues_result)
            write_json(report_dir / "measures.json", measures)

    for source_name, target_name in [
        ("sonar-project.properties", "sonar-project.properties"),
        (".scannerwork/report-task.txt", "report-task.txt"),
    ]:
        source = source_dir / source_name
        if source.exists():
            shutil.copy2(source, report_dir / target_name)
    summary["artifacts"] = report_artifacts(report_dir, context)
    write_json(report_dir / "summary.json", summary)
    write_json(report_dir / "evidence.json", {
        **summary,
        "generatedAt": utc_now().isoformat(),
        "evidenceType": "validation.code-quality",
    })
    print_quality_summary(summary)
    if summary["status"] == "FAILED" and required:
        raise HTTPException(status_code=500, detail={
            "message": "Code quality scan failed",
            "sonarStatus": summary.get("sonarStatus"),
            "qualityGateStatus": summary.get("qualityGateStatus"),
            "missingConfiguration": missing,
            "projectKey": summary.get("projectKey"),
            "reportDir": str(report_dir.relative_to(context["runDir"])),
        })


SEVERITY_RANK = {"UNKNOWN": 0, "LOW": 1, "MEDIUM": 2, "HIGH": 3, "CRITICAL": 4}


def normalize_security_severity(value: Any) -> str:
    severity = str(value or "UNKNOWN").strip().upper()
    if severity in {"ERROR"}:
        return "HIGH"
    if severity in {"WARNING", "WARN"}:
        return "MEDIUM"
    if severity in {"INFO", "INFORMATIONAL"}:
        return "LOW"
    return severity if severity in SEVERITY_RANK else "UNKNOWN"


def security_risk_score(severity: Any) -> float:
    return {
        "CRITICAL": 10.0,
        "HIGH": 7.5,
        "MEDIUM": 5.0,
        "LOW": 3.0,
    }.get(normalize_security_severity(severity), 1.0)


def security_thresholds(action: Dict[str, Any]) -> List[str]:
    configured = (
        action.get("failOnSeverity")
        or action.get("failOnSeverities")
        or action.get("blockingSeverities")
        or config.security_fail_on_severity
    )
    return [normalize_security_severity(item) for item in csv_values(configured) if normalize_security_severity(item) in SEVERITY_RANK]


def security_finding_id(rule_id: Any, target: Any, component: Any, line: Any, description: Any) -> str:
    raw = "|".join(str(item or "") for item in [rule_id, target, component, line, description])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def context_application(context: Dict[str, Any]) -> str:
    payload = context.get("requestPayload") or {}
    return str(payload.get("PROJECT_NAME") or context.get("project", {}).get("name") or context["requestId"])


def context_requested_by(context: Dict[str, Any]) -> str:
    payload = context.get("requestPayload") or {}
    return str(payload.get("REQUESTED_BY") or payload.get("requestedBy") or "jenkins@horizonrelevance.com")


def context_build_number(context: Dict[str, Any]) -> int:
    payload = context.get("requestPayload") or {}
    runner = context.get("runner") or {}
    return parse_int(runner.get("buildNumber") or payload.get("BUILD_NUMBER") or payload.get("buildNumber") or 0)


def context_jenkins_job(context: Dict[str, Any]) -> str:
    payload = context.get("requestPayload") or {}
    runner = context.get("runner") or {}
    return str(runner.get("jobName") or payload.get("JOB_NAME") or payload.get("jobName") or context_application(context))


def context_jenkins_url(context: Dict[str, Any]) -> str:
    payload = context.get("requestPayload") or {}
    runner = context.get("runner") or {}
    return str(runner.get("buildUrl") or payload.get("BUILD_URL") or payload.get("buildUrl") or "")


def ensure_dashboard_finding_metadata(finding: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    enriched = dict(finding)
    enriched["jenkins_job"] = str(enriched.get("jenkins_job") or context_jenkins_job(context))
    enriched["build_number"] = parse_int(enriched.get("build_number") or context_build_number(context))
    enriched["jenkins_url"] = str(enriched.get("jenkins_url") or context_jenkins_url(context))
    return enriched


def make_security_finding(
    context: Dict[str, Any],
    *,
    target: Any,
    package_name: Any,
    installed_version: Any = "N/A",
    vulnerability_id: Any,
    severity: Any,
    fixed_version: Any = None,
    description: Any = "",
    source: Any = "Security Finding",
    line: Any = None,
    rule: Any = None,
    status: Any = "Open",
    predicted_severity: Any = None,
) -> Dict[str, Any]:
    normalized = normalize_security_severity(severity)
    vuln_id = str(vulnerability_id or rule or "SECURITY-FINDING")
    return {
        "target": str(target or "repository"),
        "package_name": str(package_name or "Application"),
        "installed_version": str(installed_version or "N/A"),
        "vulnerability_id": vuln_id,
        "severity": normalized,
        "fixed_version": str(fixed_version or "Review and remediate this finding."),
        "risk_score": security_risk_score(normalized),
        "description": str(description or vuln_id),
        "source": str(source or "Security Finding"),
        "timestamp": utc_now().isoformat(),
        "line": parse_int(line, 0) or None,
        "rule": str(rule or vuln_id),
        "status": str(status or "Open"),
        "predictedSeverity": normalize_security_severity(predicted_severity or normalized),
        "jenkins_job": context_jenkins_job(context),
        "build_number": context_build_number(context),
        "jenkins_url": context_jenkins_url(context),
    }


def count_by_severity(findings: List[Dict[str, Any]]) -> Dict[str, int]:
    counts = {severity: 0 for severity in ["CRITICAL", "HIGH", "MEDIUM", "LOW", "UNKNOWN"]}
    for finding in findings:
        severity = normalize_security_severity(finding.get("severity"))
        counts[severity] = counts.get(severity, 0) + 1
    return counts


def blocking_findings(findings: List[Dict[str, Any]], thresholds: List[str]) -> List[Dict[str, Any]]:
    ranks = [SEVERITY_RANK[item] for item in thresholds if item in SEVERITY_RANK]
    if not ranks:
        return []
    minimum = min(ranks)
    return [finding for finding in findings if SEVERITY_RANK.get(normalize_security_severity(finding.get("severity")), 0) >= minimum]


def write_command_audit(path: Path, args: List[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(shlex.join(args) + "\n", encoding="utf-8")


def upload_security_findings(report_dir: Path, action: Dict[str, Any], context: Dict[str, Any], findings: List[Dict[str, Any]]) -> Dict[str, Any]:
    endpoint = normalize_http_url(action.get("findingsUploadUrl") or action.get("dashboardUploadUrl") or config.findings_upload_url)
    enriched_findings = [ensure_dashboard_finding_metadata(finding, context) for finding in findings]
    payload = {
        "application": context_application(context),
        "requestedBy": context_requested_by(context),
        "repo_url": (context.get("git") or {}).get("repoUrl") or "",
        "jenkins_url": context_jenkins_url(context),
        "jenkins_job": context_jenkins_job(context),
        "build_number": context_build_number(context),
        "vulnerabilities": enriched_findings,
    }
    write_json(report_dir / "dashboard-upload.json", payload)
    if not endpoint:
        result = {"status": "SKIPPED", "endpoint": "", "count": len(enriched_findings), "reason": "HORIZON_FINDINGS_UPLOAD_URL is not configured"}
        write_json(report_dir / "dashboard-upload-summary.json", result)
        return result
    headers = {"Content-Type": "application/json"}
    token = str(action.get("findingsUploadToken") or config.findings_upload_token or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        response = requests.post(endpoint, json=payload, headers=headers, timeout=30)
        result = {
            "status": "UPLOADED" if response.status_code < 400 else "FAILED",
            "endpoint": endpoint,
            "count": len(enriched_findings),
            "httpStatus": response.status_code,
            "response": response.text[:2000],
        }
    except requests.RequestException as exc:
        result = {"status": "FAILED", "endpoint": endpoint, "count": len(enriched_findings), "error": str(exc)}
    write_json(report_dir / "dashboard-upload-summary.json", result)
    if result["status"] == "FAILED" and as_bool(action.get("failOnDashboardUpload"), config.security_fail_on_dashboard_upload):
        raise HTTPException(status_code=502, detail=f"Security findings dashboard upload failed: {result}")
    return result


def finalize_security_report(
    report_dir: Path,
    action: Dict[str, Any],
    context: Dict[str, Any],
    *,
    tool: str,
    tool_name: str,
    stage: str,
    tools: List[str],
    findings: List[Dict[str, Any]],
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    thresholds = security_thresholds(action)
    blocking = blocking_findings(findings, thresholds)
    counts = count_by_severity(findings)
    upload = upload_security_findings(report_dir, action, context, findings)
    failed_thresholds = []
    if blocking:
        failed_thresholds.append(
            f"{len(blocking)} findings met blocking severity threshold: {','.join(thresholds) or 'none'}"
        )
    summary = {
        "tool": tool,
        "toolName": tool_name,
        "stage": stage,
        "status": "FAILED" if blocking and as_bool(action.get("failOnFindings"), True) else ("COMPLETED_WITH_FINDINGS" if findings else "PASSED"),
        "findingCount": len(findings),
        "findingCounts": counts,
        "blockingFindings": len(blocking),
        "blockingSeverities": thresholds,
        "failedThresholds": failed_thresholds,
        "dashboardUpload": upload,
        "tools": sorted(set(tools)),
        "sampleFindings": findings[:25],
        "requestId": context["requestId"],
        "project": context.get("project", {}),
        "git": context.get("git", {}),
        "runner": context.get("runner", {}),
        "artifacts": report_artifacts(report_dir, context),
    }
    if extra:
        summary.update(extra)
    write_json(report_dir / "normalized-findings.json", findings)
    write_json(report_dir / "summary.json", summary)
    write_json(report_dir / "evidence.json", {
        **summary,
        "generatedAt": utc_now().isoformat(),
        "evidenceType": f"validation.{stage}",
    })
    print_quality_summary(summary)
    if blocking and as_bool(action.get("failOnFindings"), True):
        raise HTTPException(status_code=500, detail={
            "message": f"{tool_name} failed severity threshold",
            "blockingFindings": len(blocking),
            "blockingSeverities": thresholds,
            "reportDir": relative_path(report_dir, context["runDir"]),
        })
    return summary


def trivy_source_for_result(result: Dict[str, Any], default_source: str) -> str:
    target = str(result.get("Target") or "").lower()
    result_class = str(result.get("Class") or "").lower()
    result_type = str(result.get("Type") or "").lower()
    dependency_markers = (
        "package-lock.json", "package.json", "yarn.lock", "pnpm-lock.yaml", "pom.xml",
        "build.gradle", "requirements.txt", "poetry.lock", "pipfile.lock", "go.mod",
        "composer.lock", "gemfile.lock",
    )
    if result_class == "os-pkgs" or any(marker in target for marker in ("alpine", "debian", "ubuntu", "amazon", "redhat", "wolfi", "oracle")):
        return "Container Vulnerability"
    if result_class == "lang-pkgs" or result_type in {"npm", "node-pkg", "jar", "pom", "python-pkg", "gobinary"} or any(marker in target for marker in dependency_markers):
        return "Dependency Vulnerability"
    return default_source


def parse_trivy_report(path: Path, context: Dict[str, Any], default_source: str) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    try:
        doc = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    findings: List[Dict[str, Any]] = []
    for result in doc.get("Results", []) or []:
        target = result.get("Target") or "repository"
        source = trivy_source_for_result(result, default_source)
        for vuln in result.get("Vulnerabilities") or []:
            fixed = vuln.get("FixedVersion") or "Upgrade to a fixed version when available; otherwise apply vendor mitigation or accept risk formally."
            findings.append(make_security_finding(
                context,
                target=target,
                package_name=vuln.get("PkgName") or "Package",
                installed_version=vuln.get("InstalledVersion") or "N/A",
                vulnerability_id=vuln.get("VulnerabilityID") or "VULNERABILITY",
                severity=vuln.get("Severity"),
                fixed_version=fixed,
                description=" - ".join(item for item in [vuln.get("Title"), vuln.get("Description")] if item),
                source=source,
                rule=vuln.get("VulnerabilityID"),
            ))
        for misconfig in result.get("Misconfigurations") or []:
            finding_id = misconfig.get("ID") or misconfig.get("AVDID") or misconfig.get("Type") or "MISCONFIGURATION"
            metadata = misconfig.get("CauseMetadata") or {}
            findings.append(make_security_finding(
                context,
                target=target,
                package_name=misconfig.get("Type") or "Configuration",
                vulnerability_id=finding_id,
                severity=misconfig.get("Severity"),
                fixed_version=misconfig.get("Resolution") or "Review and harden this configuration.",
                description=" - ".join(item for item in [misconfig.get("Title"), misconfig.get("Message"), misconfig.get("Description")] if item),
                source="IaC Misconfiguration",
                line=metadata.get("StartLine") or metadata.get("EndLine"),
                rule=finding_id,
            ))
        for secret in result.get("Secrets") or []:
            finding_id = secret.get("RuleID") or secret.get("Category") or "SECRET"
            findings.append(make_security_finding(
                context,
                target=target,
                package_name=secret.get("Category") or "Secret",
                vulnerability_id=finding_id,
                severity=secret.get("Severity") or "HIGH",
                fixed_version="Remove the secret from source control, rotate it, and use an approved secret manager.",
                description=secret.get("Title") or "Potential secret detected.",
                source="Secret Exposure",
                line=secret.get("StartLine") or secret.get("EndLine"),
                rule=finding_id,
            ))
    return findings


def semgrep_default_rules(report_dir: Path) -> Path:
    rules = report_dir / "horizon-semgrep-rules.yml"
    rules.write_text(
        """
rules:
  - id: horizon-hardcoded-secret
    message: Potential hardcoded credential or secret.
    severity: ERROR
    languages: [generic]
    pattern-regex: (?i)\\b(password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key)\\b\\s*[:=]\\s*['\\\"][^'\\\"\\n]{8,}['\\\"]
  - id: horizon-private-key
    message: Private key material detected.
    severity: ERROR
    languages: [generic]
    pattern-regex: -----BEGIN (RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----
  - id: horizon-dynamic-code-execution
    message: Dynamic code execution detected.
    severity: ERROR
    languages: [javascript, typescript, python, java]
    pattern-either:
      - pattern: eval(...)
      - pattern: Function(...)
      - pattern: Runtime.getRuntime().exec(...)
  - id: horizon-unsafe-dom-update
    message: Unsafe DOM update pattern detected.
    severity: WARNING
    languages: [javascript, typescript]
    pattern-either:
      - pattern: $X.innerHTML = ...
      - pattern: document.write(...)
      - pattern: $S.bypassSecurityTrustHtml(...)
  - id: horizon-plain-http-endpoint
    message: Plain HTTP endpoint detected.
    severity: WARNING
    languages: [generic]
    pattern-regex: ['\\\"]http://(?!localhost|127\\.0\\.0\\.1)[^'\\\"\\s]+['\\\"]
""".strip() + "\n",
        encoding="utf-8",
    )
    return rules


def parse_semgrep_report(path: Path, context: Dict[str, Any]) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    try:
        doc = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    findings = []
    for item in doc.get("results", []) or []:
        extra = item.get("extra") or {}
        metadata = extra.get("metadata") or {}
        line = (item.get("start") or {}).get("line")
        rule_id = item.get("check_id") or "SEMGREP"
        findings.append(make_security_finding(
            context,
            target=item.get("path") or "repository",
            package_name=metadata.get("category") or metadata.get("technology") or "Source Code",
            vulnerability_id=rule_id,
            severity=extra.get("severity"),
            fixed_version=metadata.get("fix") or "Review the affected code path and apply secure coding remediation.",
            description=extra.get("message") or rule_id,
            source="Static Code Security Finding",
            line=line,
            rule=rule_id,
        ))
    return findings


def parse_gitleaks_report(path: Path, context: Dict[str, Any]) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    try:
        doc = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    findings = []
    if isinstance(doc, dict):
        items = doc.get("findings") or doc.get("Findings") or []
    else:
        items = doc
    for item in items or []:
        rule_id = item.get("RuleID") or item.get("ruleID") or item.get("Rule") or "SECRET"
        findings.append(make_security_finding(
            context,
            target=item.get("File") or item.get("file") or "repository",
            package_name=item.get("Description") or item.get("description") or "Secret",
            vulnerability_id=rule_id,
            severity="HIGH",
            fixed_version="Remove the secret, rotate it, and store future credentials in an approved secret manager.",
            description=item.get("Description") or item.get("Match") or "Potential secret detected by repository secret scan.",
            source="Secret Exposure",
            line=item.get("StartLine") or item.get("Line"),
            rule=rule_id,
        ))
    return findings


def discover_manifest_inputs(source_dir: Path, report_dir: Path) -> List[Path]:
    candidates: List[Path] = []
    for dirname in ["k8s", "kubernetes", "manifests", "deploy", "deployment", "helm"]:
        base = source_dir / dirname
        if base.exists():
            candidates.extend(path for path in base.rglob("*") if path.suffix.lower() in {".yaml", ".yml"} and path.is_file())
    candidates.extend(path for path in source_dir.glob("*.yaml") if path.is_file())
    candidates.extend(path for path in source_dir.glob("*.yml") if path.is_file())
    chart_dirs = sorted({path.parent for path in source_dir.rglob("Chart.yaml") if path.is_file()})
    rendered_dir = report_dir / "rendered"
    rendered_dir.mkdir(parents=True, exist_ok=True)
    for chart_dir in chart_dirs[:5]:
        output = rendered_dir / f"{safe_file_token(str(chart_dir.relative_to(source_dir)))}.yaml"
        result = run_command(["helm", "template", safe_k8s_name(chart_dir.name), str(chart_dir)], cwd=source_dir, check=False)
        if result.returncode == 0 and result.stdout.strip():
            output.write_text(result.stdout, encoding="utf-8")
            candidates.append(output)
        else:
            (rendered_dir / f"{safe_file_token(str(chart_dir.relative_to(source_dir)))}.log").write_text(command_output_tail(result), encoding="utf-8")
    for kustomization in source_dir.rglob("kustomization.yaml"):
        output = rendered_dir / f"{safe_file_token(str(kustomization.parent.relative_to(source_dir)))}-kustomize.yaml"
        result = run_command(["kubectl", "kustomize", str(kustomization.parent)], cwd=source_dir, check=False)
        if result.returncode == 0 and result.stdout.strip():
            output.write_text(result.stdout, encoding="utf-8")
            candidates.append(output)
    unique = []
    seen = set()
    for path in candidates:
        if any(part in {".git", "node_modules", "target", "dist", "build"} for part in path.parts):
            continue
        key = str(path.resolve())
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def built_in_conftest_policy(report_dir: Path) -> Path:
    policy_dir = report_dir / "policies"
    policy_dir.mkdir(parents=True, exist_ok=True)
    (policy_dir / "horizon-kubernetes.rego").write_text(
        """
package main

workload_containers[c] {
  input.kind == "Pod"
  c := input.spec.containers[_]
}

workload_containers[c] {
  input.kind != "Pod"
  spec := input.spec.template.spec
  c := spec.containers[_]
}

deny[msg] {
  c := workload_containers[_]
  c.securityContext.privileged == true
  msg := sprintf("Privileged container is not allowed: %v", [c.name])
}

deny[msg] {
  input.kind == "Pod"
  input.spec.hostNetwork == true
  msg := "hostNetwork is not allowed"
}

deny[msg] {
  c := workload_containers[_]
  not c.securityContext.runAsNonRoot
  msg := sprintf("Container should set securityContext.runAsNonRoot=true: %v", [c.name])
}

deny[msg] {
  c := workload_containers[_]
  not c.resources.limits.cpu
  msg := sprintf("Container is missing CPU limit: %v", [c.name])
}

deny[msg] {
  c := workload_containers[_]
  not c.resources.limits.memory
  msg := sprintf("Container is missing memory limit: %v", [c.name])
}

warn[msg] {
  c := workload_containers[_]
  endswith(c.image, ":latest")
  msg := sprintf("Image should not use latest tag: %v", [c.image])
}
""".strip() + "\n",
        encoding="utf-8",
    )
    return policy_dir


def parse_conftest_report(path: Path, context: Dict[str, Any]) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    try:
        doc = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    findings = []
    for result in doc if isinstance(doc, list) else [doc]:
        target = result.get("filename") or result.get("file") or "manifest"
        for item in result.get("failures") or []:
            message = item.get("msg") if isinstance(item, dict) else str(item)
            findings.append(make_security_finding(
                context,
                target=target,
                package_name="Kubernetes Policy",
                vulnerability_id=security_finding_id("OPA-DENY", target, "policy", "", message),
                severity="HIGH",
                fixed_version="Update the manifest or Helm values to satisfy the required platform policy.",
                description=message,
                source="Policy Violation",
                rule="OPA-DENY",
            ))
        for item in result.get("warnings") or []:
            message = item.get("msg") if isinstance(item, dict) else str(item)
            findings.append(make_security_finding(
                context,
                target=target,
                package_name="Kubernetes Policy",
                vulnerability_id=security_finding_id("OPA-WARN", target, "policy", "", message),
                severity="MEDIUM",
                fixed_version="Review and harden this manifest before production promotion.",
                description=message,
                source="Policy Violation",
                rule="OPA-WARN",
            ))
    return findings


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
    command_status: Dict[str, Any] = {}

    if shutil.which("semgrep"):
        tools.append("semgrep")
        rule_file = semgrep_default_rules(report_dir)
        repo_configs = [path for path in [source_dir / ".semgrep.yml", source_dir / ".semgrep.yaml"] if path.exists()]
        config_args = []
        for config_file in repo_configs + [rule_file]:
            config_args.extend(["--config", str(config_file)])
        semgrep_json = report_dir / "semgrep.json"
        semgrep_sarif = report_dir / "semgrep.sarif"
        json_cmd = ["semgrep", "scan", *config_args, "--json", "--output", str(semgrep_json), "."]
        sarif_cmd = ["semgrep", "scan", *config_args, "--sarif", "--output", str(semgrep_sarif), "."]
        write_command_audit(report_dir / "semgrep-command.txt", json_cmd)
        result = run_command(json_cmd, cwd=source_dir, check=False)
        command_status["semgrepJsonExitCode"] = result.returncode
        command_status["semgrepOutputTail"] = command_output_tail(result)
        sarif = run_command(sarif_cmd, cwd=source_dir, check=False)
        command_status["semgrepSarifExitCode"] = sarif.returncode
        findings.extend(parse_semgrep_report(semgrep_json, context))
    else:
        command_status["semgrep"] = "not_installed"

    if shutil.which("gitleaks"):
        tools.append("gitleaks")
        gitleaks_json = report_dir / "gitleaks.json"
        gitleaks_sarif = report_dir / "gitleaks.sarif"
        json_cmd = ["gitleaks", "detect", "--source", ".", "--no-git", "--redact", "--report-format", "json", "--report-path", str(gitleaks_json)]
        sarif_cmd = ["gitleaks", "detect", "--source", ".", "--no-git", "--redact", "--report-format", "sarif", "--report-path", str(gitleaks_sarif)]
        write_command_audit(report_dir / "gitleaks-command.txt", json_cmd)
        result = run_command(json_cmd, cwd=source_dir, check=False)
        command_status["gitleaksJsonExitCode"] = result.returncode
        command_status["gitleaksOutputTail"] = command_output_tail(result)
        sarif = run_command(sarif_cmd, cwd=source_dir, check=False)
        command_status["gitleaksSarifExitCode"] = sarif.returncode
        findings.extend(parse_gitleaks_report(gitleaks_json, context))
    else:
        command_status["gitleaks"] = "not_installed"

    if not tools:
        raise HTTPException(status_code=500, detail="Static security scanners are not installed in the runner image")

    finalize_security_report(
        report_dir,
        action,
        context,
        tool="static-security",
        tool_name="Static Security Scan",
        stage="static-security",
        tools=tools,
        findings=findings,
        extra={"commandStatus": command_status, "reviewTeam": action.get("reviewTeam") or ""},
    )


def execute_security_container_iac(action: Dict[str, Any], context: Dict[str, Any]) -> None:
    source_dir = get_source_dir(context)
    report_dir = action_report_dir(action, context, "container-iac")
    payload = context.get("requestPayload") or {}
    image_uri = render_value(action.get("imageUri") or payload.get("IMAGE_URI") or payload.get("imageUri") or "{{image.uriWithDigest}}" or "{{image.uri}}", context)
    if image_uri in {"{{image.uriWithDigest}}", "{{image.uri}}"}:
        image_uri = ""
    region = action.get("awsRegion") or os.getenv("AWS_REGION", "us-east-1")
    role_env = assume_role_env(action.get("roleArn", ""), region, f"horizon-scan-{context['requestId']}")
    if not shutil.which("trivy"):
        raise HTTPException(status_code=500, detail="Trivy is not installed in the runner image")

    tools = ["trivy"]
    command_status: Dict[str, Any] = {}
    findings: List[Dict[str, Any]] = []
    severities = "CRITICAL,HIGH,MEDIUM,LOW,UNKNOWN"

    fs_json = report_dir / "filesystem-security.json"
    fs_sarif = report_dir / "filesystem-security.sarif"
    fs_table = report_dir / "filesystem-security.txt"
    fs_cmd = ["trivy", "fs", "--format", "json", "--scanners", "vuln,secret,config", "--severity", severities, "--output", str(fs_json), "."]
    write_command_audit(report_dir / "trivy-filesystem-command.txt", fs_cmd)
    fs = run_command(fs_cmd, cwd=source_dir, check=False, timeout=1800)
    command_status["filesystemJsonExitCode"] = fs.returncode
    command_status["filesystemOutputTail"] = command_output_tail(fs)
    run_command(["trivy", "fs", "--format", "sarif", "--scanners", "vuln,secret,config", "--severity", severities, "--output", str(fs_sarif), "."], cwd=source_dir, check=False, timeout=1800)
    table = run_command(["trivy", "fs", "--format", "table", "--scanners", "vuln,secret,config", "--severity", severities, "."], cwd=source_dir, check=False, timeout=1800)
    fs_table.write_text((table.stdout or "") + (table.stderr or ""), encoding="utf-8")
    findings.extend(parse_trivy_report(fs_json, context, "Dependency Vulnerability"))

    if image_uri:
        image_json = report_dir / "image-security.json"
        image_sarif = report_dir / "image-security.sarif"
        image_table = report_dir / "image-security.txt"
        image_cmd = ["trivy", "image", "--format", "json", "--scanners", "vuln,secret,config", "--severity", severities, "--output", str(image_json), str(image_uri)]
        write_command_audit(report_dir / "trivy-image-command.txt", image_cmd)
        image = run_command(image_cmd, env=role_env, check=False, timeout=1800)
        command_status["imageJsonExitCode"] = image.returncode
        command_status["imageOutputTail"] = command_output_tail(image)
        run_command(["trivy", "image", "--format", "sarif", "--scanners", "vuln,secret,config", "--severity", severities, "--output", str(image_sarif), str(image_uri)], env=role_env, check=False, timeout=1800)
        table = run_command(["trivy", "image", "--format", "table", "--scanners", "vuln,secret,config", "--severity", severities, str(image_uri)], env=role_env, check=False, timeout=1800)
        image_table.write_text((table.stdout or "") + (table.stderr or ""), encoding="utf-8")
        findings.extend(parse_trivy_report(image_json, context, "Container Vulnerability"))
    else:
        command_status["image"] = "not_configured"

    finalize_security_report(
        report_dir,
        action,
        context,
        tool="container-iac",
        tool_name="Container/IaC Vulnerability Scan",
        stage="container-iac",
        tools=tools,
        findings=findings,
        extra={"commandStatus": command_status, "imageUri": image_uri or ""},
    )


def execute_security_policy(action: Dict[str, Any], context: Dict[str, Any]) -> None:
    source_dir = get_source_dir(context)
    report_dir = action_report_dir(action, context, "policy")
    if not shutil.which("conftest"):
        raise HTTPException(status_code=500, detail="Conftest is not installed in the runner image")

    tools = ["conftest", "opa"]
    manifest_inputs = discover_manifest_inputs(source_dir, report_dir)
    policy_dirs = [built_in_conftest_policy(report_dir)]
    for candidate in [
        source_dir / "policy",
        source_dir / "policies",
        source_dir / ".horizon" / "policy",
        source_dir / ".horizon" / "policies",
    ]:
        if candidate.exists():
            policy_dirs.append(candidate)

    findings: List[Dict[str, Any]] = []
    command_status: Dict[str, Any] = {"manifestCount": len(manifest_inputs), "policyDirectories": [str(path) for path in policy_dirs]}
    if manifest_inputs:
        conftest_json = report_dir / "conftest.json"
        cmd = ["conftest", "test", "--output", "json"]
        for policy_dir in policy_dirs:
            cmd.extend(["--policy", str(policy_dir)])
        cmd.extend(str(path) for path in manifest_inputs)
        write_command_audit(report_dir / "conftest-command.txt", cmd)
        result = run_command(cmd, cwd=source_dir, check=False)
        command_status["conftestExitCode"] = result.returncode
        command_status["conftestOutputTail"] = command_output_tail(result)
        conftest_json.write_text(result.stdout or "[]", encoding="utf-8")
        findings.extend(parse_conftest_report(conftest_json, context))
    else:
        command_status["conftest"] = "no_manifests_found"

    dockerfile = source_dir / "Dockerfile"
    if dockerfile.exists():
        text = dockerfile.read_text(errors="ignore")
        if re.search(r"(?im)^USER\s+root\s*$", text) or not re.search(r"(?im)^USER\s+\S+", text):
            findings.append(make_security_finding(
                context,
                target="Dockerfile",
                package_name="Container Policy",
                vulnerability_id="container-non-root-user",
                severity="MEDIUM",
                fixed_version="Set a non-root USER in the Dockerfile and run the application with least privilege.",
                description="Dockerfile should define a non-root runtime user.",
                source="Policy Violation",
                rule="container-non-root-user",
            ))

    write_json(report_dir / "manifest-inputs.json", [relative_path(path, source_dir) for path in manifest_inputs])
    finalize_security_report(
        report_dir,
        action,
        context,
        tool="policy-validation",
        tool_name="Policy Validation",
        stage="policy-validation",
        tools=tools,
        findings=findings,
        extra={"commandStatus": command_status},
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
    if action_type in {"image.build_push", "artifact.context", "artifact.publish", "release.promote_image"}:
        return "publish"
    if action_type in {"eks.deploy", "release.publish_approval"}:
        return "deploy"
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
