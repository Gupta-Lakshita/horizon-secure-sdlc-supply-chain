import base64
import json
import os
import re
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


def print_quality_summary(summary: Dict[str, Any]) -> None:
    print("== Horizon validation evidence ==", flush=True)
    print(f"Tool: {summary.get('toolName') or summary.get('tool')}", flush=True)
    print(f"Status: {summary.get('status')}", flush=True)
    if summary.get("targetAppUrl"):
        print(f"Target: {summary['targetAppUrl']}", flush=True)
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
    artifacts = summary.get("artifacts", {}).get("counts", {})
    if artifacts:
        print(f"Artifacts: {artifacts}", flush=True)


def collect_report_summaries(context: Dict[str, Any]) -> List[Dict[str, Any]]:
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
            if (source_dir / "package-lock.json").exists():
                run_command(["npm", "ci"], cwd=source_dir)
            else:
                run_command(["npm", "install"], cwd=source_dir)
            script = detect_npm_script(source_dir, ["test:e2e", "e2e", "test:ui"])
            if not script:
                raise HTTPException(status_code=422, detail="No UI end-to-end npm script found. Expected test:e2e, e2e, or test:ui.")
            package = json.loads((source_dir / "package.json").read_text())
            deps = {**package.get("dependencies", {}), **package.get("devDependencies", {})}
            if "@playwright/test" in deps or "playwright" in deps:
                run_command(["npx", "playwright", "install", "chromium"], cwd=source_dir, check=False)
            command = f"npm run {script}"
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


def execute_quality_api(action: Dict[str, Any], context: Dict[str, Any]) -> None:
    source_dir = get_source_dir(context)
    report_dir = action_report_dir(action, context, "newman")
    collection = action.get("collectionPath") or _find_first(source_dir, [
        "tests/postman/horizon-demo-angular.postman_collection.json",
        "tests/postman/collection.json",
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
    cmd = [
        "newman", "run", collection,
        "--timeout-request", str(action.get("timeoutMs") or "30000"),
        "--reporters", "cli,junit,json",
        "--reporter-junit-export", str(report_dir / "results.xml"),
        "--reporter-json-export", str(report_dir / "results.json"),
    ]
    if env_path:
        cmd.extend(["--environment", env_path])
    if data_file:
        cmd.extend(["--iteration-data", data_file])
    if base_url:
        cmd.extend(["--env-var", f"baseUrl={base_url}", "--env-var", f"apiBaseUrl={base_url}"])
    result = run_command(cmd, cwd=source_dir, check=False)
    write_json(report_dir / "summary.json", {
        "status": "PASSED" if result.returncode == 0 else "FAILED",
        "collection": collection,
        "environment": env_path,
        "iterationDataFile": data_file,
        "baseUrl": base_url,
    })
    if result.returncode != 0 and as_bool(action.get("failOnError"), True):
        raise HTTPException(status_code=500, detail="API regression test failed")


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
    patterns = [
        ("hardcoded_secret", re.compile(r"(?i)(password|secret|token|apikey|api_key)\s*[:=]\s*['\"][^'\"]{8,}")),
        ("dangerous_eval", re.compile(r"\beval\s*\(")),
        ("insecure_http", re.compile(r"(?i)['\"]http://")),
        ("shell_exec", re.compile(r"(?i)(exec|spawn|Runtime\.getRuntime\(\)\.exec)")),
    ]
    findings = []
    for path in source_dir.rglob("*"):
        if not path.is_file() or any(part in {".git", "node_modules", "target", "dist", "build"} for part in path.parts):
            continue
        try:
            text = path.read_text(errors="ignore")
        except OSError:
            continue
        for idx, line in enumerate(text.splitlines(), start=1):
            for rule_id, pattern in patterns:
                if pattern.search(line):
                    findings.append({
                        "ruleId": rule_id,
                        "file": str(path.relative_to(source_dir)),
                        "line": idx,
                        "severity": "HIGH" if rule_id in {"hardcoded_secret", "dangerous_eval"} else "MEDIUM",
                    })
    write_json(report_dir / "findings.json", findings)
    write_json(report_dir / "summary.json", {"status": "PASSED", "findingCount": len(findings), "reviewTeam": action.get("reviewTeam") or ""})


def execute_security_container_iac(action: Dict[str, Any], context: Dict[str, Any]) -> None:
    source_dir = get_source_dir(context)
    report_dir = action_report_dir(action, context, "container-iac")
    image_uri = render_value(action.get("imageUri") or "", context)
    region = action.get("awsRegion") or os.getenv("AWS_REGION", "us-east-1")
    role_env = assume_role_env(action.get("roleArn", ""), region, f"horizon-scan-{context['requestId']}")
    results = {"filesystem": "NOT_RUN", "image": "NOT_RUN", "status": "PASSED"}
    if shutil.which("trivy"):
        fs_json = report_dir / "filesystem-security.json"
        fs = run_command(["trivy", "fs", "--format", "json", "--scanners", "vuln,secret,config", "--severity", "CRITICAL,HIGH,MEDIUM", "--output", str(fs_json), "."], cwd=source_dir, check=False)
        results["filesystem"] = "PASSED" if fs.returncode == 0 else "FINDINGS"
        if image_uri:
            image_json = report_dir / "image-security.json"
            image = run_command(["trivy", "image", "--format", "json", "--severity", "CRITICAL,HIGH,MEDIUM", "--output", str(image_json), image_uri], env=role_env, check=False)
            results["image"] = "PASSED" if image.returncode == 0 else "FINDINGS"
    else:
        results["status"] = "COMPLETED_WITHOUT_EXTERNAL_SCANNER"
    write_json(report_dir / "summary.json", results)


def execute_security_policy(action: Dict[str, Any], context: Dict[str, Any]) -> None:
    source_dir = get_source_dir(context)
    report_dir = action_report_dir(action, context, "policy")
    findings = []
    for path in list(source_dir.rglob("*.yaml")) + list(source_dir.rglob("*.yml")):
        if any(part in {".git", "node_modules", "target", "dist", "build"} for part in path.parts):
            continue
        text = path.read_text(errors="ignore")
        if "privileged: true" in text:
            findings.append({"ruleId": "no-privileged-workloads", "file": str(path.relative_to(source_dir)), "severity": "HIGH"})
        if "hostNetwork: true" in text:
            findings.append({"ruleId": "no-host-network", "file": str(path.relative_to(source_dir)), "severity": "HIGH"})
        if re.search(r"image:\s+[^:\s]+(?:\s|$)", text):
            findings.append({"ruleId": "image-tag-required", "file": str(path.relative_to(source_dir)), "severity": "MEDIUM"})
    if (source_dir / "Dockerfile").exists():
        dockerfile = (source_dir / "Dockerfile").read_text(errors="ignore")
        if re.search(r"(?im)^USER\s+root\s*$", dockerfile) or not re.search(r"(?im)^USER\s+\S+", dockerfile):
            findings.append({"ruleId": "container-non-root-user", "file": "Dockerfile", "severity": "MEDIUM"})
    write_json(report_dir / "findings.json", findings)
    write_json(report_dir / "summary.json", {"status": "PASSED", "findingCount": len(findings)})


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
    report_summary = {"reports": collect_report_summaries(load_runner_context(request))}
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
