import base64
import json
import os
import shutil
import subprocess
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

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
    parameters: Dict[str, Any] = Field(default_factory=dict)
    payload: Dict[str, Any] = Field(default_factory=dict)


class RunnerResponse(BaseModel):
    status: str
    requestId: str
    planId: Optional[str] = None
    message: str
    executedActions: List[str] = Field(default_factory=list)


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


def execute_actions(actions: List[Dict[str, Any]], request: RunnerRequest) -> List[str]:
    context: Dict[str, Any] = {
        "requestId": request.requestId,
        "runDir": action_workspace(request.requestId),
        "project": {"name": str((request.payload or request.parameters or {}).get("PROJECT_NAME") or request.jobName or request.requestId)},
    }
    executed = []
    for idx, action in enumerate(actions):
        action_type = action.get("type") or action.get("action")
        name = action.get("name") or f"action-{idx + 1}"

        if action_type == "log":
            print(action.get("message", name), flush=True)
            executed.append(name)
            continue

        if not config.execute_actions:
            executed.append(f"{name}:planned")
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
            continue

        if action_type == "git.checkout":
            execute_git_checkout(action, context)
            executed.append(name)
            continue

        if action_type == "project.build":
            execute_project_build(render_value(action, context), context)
            executed.append(name)
            continue

        if action_type == "image.build_push":
            execute_image_build_push(action, context)
            executed.append(name)
            continue

        if action_type == "artifact.publish":
            execute_artifact_publish(action, context)
            executed.append(name)
            continue

        if action_type == "eks.deploy":
            execute_eks_deploy(action, context)
            executed.append(name)
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
    emit_event("requested", request, None, {"payloadKeys": sorted((request.payload or request.parameters).keys())})
    plan = request_execution_plan(request)
    signed_plan = validate_plan(plan)
    plan_id = signed_plan.get("planId") or signed_plan.get("plan_id") or plan.get("planId")
    actions = signed_plan.get("actions") or signed_plan.get("steps") or []
    executed = execute_actions(actions, request)
    emit_event("completed", request, plan_id, {"executedActions": executed})
    return RunnerResponse(
        status="completed",
        requestId=request.requestId,
        planId=plan_id,
        message="Execution plan completed",
        executedActions=executed,
    )


@app.post("/v1/validate")
async def validate(request: Request) -> Dict[str, Any]:
    plan = await request.json()
    signed_plan = validate_plan(plan)
    return {"status": "valid", "planId": signed_plan.get("planId") or signed_plan.get("plan_id")}
