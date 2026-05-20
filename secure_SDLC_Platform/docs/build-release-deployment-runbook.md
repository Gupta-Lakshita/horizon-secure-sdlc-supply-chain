# Build, Release, and Deployment Runbook

**Client Engineering Guide for Horizon Relevance AI DevSecOps Platform**  
Evidence date: May 20, 2026  
Example client/application: Acme Fintech / `acme-fintech-angular-main`

## Table of Contents

1. Purpose and Audience
2. End-to-End Process Summary
3. Architecture and Artifact Flow
4. Prerequisites
5. Build & Deploy Pipeline
6. Validate Pipeline
7. Release Promotion Pipeline
8. Evidence and Audit Artifacts
9. Validation Checklist
10. Troubleshooting
11. Appendix: Captured Evidence

## 1. Purpose and Audience

This document explains how a client engineer uses the Horizon Relevance AI DevSecOps platform to build an application, publish an immutable container image, store release metadata, promote the same image across environments, and verify the final deployment.

The guide is written for client developers, QA engineers, release managers, platform administrators, and auditors who need to understand what happens during build, release, and deployment.

## 2. End-to-End Process Summary

The process uses three product capabilities:

- **Build & Deploy Pipeline**: builds source code, creates a Docker image, pushes the image to ECR, writes release metadata to S3, and deploys to the selected non-production environment.
- **Release Promotion Pipeline**: promotes the same immutable image digest from DEV to QA, QA to STAGE, and later STAGE to PROD. It reads the original build metadata from S3 and writes environment-specific evidence.
- **Validate Pipeline**: runs quality, functional, performance, API, and security validation against the deployed application.

Important release principle: QA, STAGE, and PROD should not rebuild the source code. They should promote and deploy the same image digest that was created by the original build.

Recommended release lifecycle:

1. Developer pushes application code to GitHub.
2. Developer runs **Build & Deploy Pipeline** for DEV. This creates one image tag and immutable digest.
3. Developer or QA runs **Validate Pipeline** against the DEV application URL.
4. Release manager runs **Release Promotion Pipeline** from DEV to QA. This deploys the same image digest to QA.
5. QA runs **Validate Pipeline** against the QA application URL.
6. Release manager runs **Release Promotion Pipeline** from QA to STAGE. This deploys the same digest to STAGE.
7. Release manager runs **Release Promotion Pipeline** from STAGE to PROD after required approval. PROD receives the same digest that passed QA and STAGE.

## 3. Architecture and Artifact Flow

```text
Developer GitHub repository
        |
        v
Horizon Build & Deploy Pipeline
        |
        +--> Jenkins builds Docker image
        +--> Amazon ECR stores image tag and digest
        +--> Amazon S3 stores image.json and templateconfiguration.json
        +--> EKS DEV receives application deployment

Release Promotion Pipeline
        |
        +--> Reads image.json/templateconfiguration.json from the base S3 prefix
        +--> Promotes image tag/digest to target environment release tag
        +--> Deploys promoted digest to target EKS namespace
        +--> Writes approval.json and deployment.json under environment evidence folder
```

## 4. Prerequisites

The client platform administrator must configure the Environment Catalog before engineers create pipeline requests. The catalog should contain environment mappings for AWS account, region, ECR repository, artifact bucket, deploy role, EKS cluster, namespace strategy, and notification settings.

For this evidence run:

| Setting | Value |
|---|---|
| AWS Region | `us-east-1` |
| Artifact Bucket | `acme-fintech-devsecops` |
| ECR Repository | `426946630837.dkr.ecr.us-east-1.amazonaws.com/acme-fintech` |
| Project | `acme-fintech-angular-main` |
| Source Repo | `https://github.com/ankur1825/horizon-demo-angular.git` |
| Branch | `main` |
| Build Image Tag | `5153d121c12` |
| Base Artifact Prefix | `devops-pipeline/acme-fintech-angular-main/5153d121c12` |

## 5. Build & Deploy Pipeline

The engineer opens the Horizon Relevance frontend and selects **Build & Deploy Pipeline**.

Minimum required fields:

| Field | Example Value |
|---|---|
| Project Name | `acme-fintech-angular-main` |
| Project Type | `Angular` |
| Repository Type | `GitHub` |
| Repository URL | `https://github.com/ankur1825/horizon-demo-angular.git` |
| Branch | `main` |
| Target Environment | `DEV` |
| Requester Notification Email | user email |

The pipeline performs these actions:

1. Checks out the repository and selected branch.
2. Builds the Docker image using the application Dockerfile.
3. Pushes the image to ECR using a short commit-based tag and `latest`.
4. Generates `image.json` and `templateconfiguration.json`.
5. Uploads both metadata files to S3 under the base artifact prefix.
6. Deploys the image to EKS using the Environment Catalog target namespace.

## 6. Validate Pipeline

After a deployment is running, the engineer opens the frontend and selects **Validate Pipeline**. This pipeline does not build or promote a new image. It validates the already deployed application by using the deployed app URL, image URI, source repository metadata, and selected validation gates.

Typical validation gates:

| Gate | What It Validates | Typical Environment |
|---|---|---|
| UI End-to-End Test | Browser-level user workflows using Selenium-style checks | DEV, QA, STAGE |
| API Regression Test | API endpoint behavior using Postman/Newman collections | DEV, QA, STAGE |
| Performance Test | Response time, error rate, and load behavior | QA, STAGE |
| Code Quality Scan | Maintainability, bugs, code smells, and coverage signals | DEV, QA |
| Static Security Scan | Source-level security weaknesses and risky code patterns | DEV, QA |
| Container / IaC Vulnerability Scan | Image package vulnerabilities and infrastructure misconfiguration | DEV, QA, STAGE |
| Policy Validation | Enterprise deployment policy and Kubernetes/IaC guardrails | DEV, QA, STAGE |

For a real client run, validate DEV first to catch basic runtime issues, then validate QA after release promotion to prove the exact promoted digest behaves correctly in QA.

## 7. Release Promotion Pipeline

The engineer opens the frontend and selects **Release Promotion Pipeline**.

For DEV to QA:

| Field | Example Value |
|---|---|
| Project Name | `acme-fintech-angular-qa` |
| Artifact Prefix / Build ID | `devops-pipeline/acme-fintech-angular-main/5153d121c12` |
| Source Image Tag | `5153d121c12` |
| Target Release Tag(s) | `qa-5153d121c12` |
| Source Environment | `DEV` |
| Target Environment | `QA` |

For QA to STAGE:

| Field | Example Value |
|---|---|
| Project Name | `acme-fintech-angular` |
| Artifact Prefix / Build ID | `devops-pipeline/acme-fintech-angular-main/5153d121c12` |
| Source Image Tag | `5153d121c12` |
| Target Release Tag(s) | `stage-5153d121c12` |
| Source Environment | `QA` |
| Target Environment | `STAGE` |

Do not use `devops-pipeline/acme-fintech-angular-main/5153d121c12/qa` as the artifact prefix. The `/qa` folder contains QA evidence. The release pipeline must read the base prefix where `image.json` and `templateconfiguration.json` live.

For STAGE to PROD:

| Field | Example Value |
|---|---|
| Project Name | `acme-fintech-angular` |
| Artifact Prefix / Build ID | `devops-pipeline/acme-fintech-angular-main/5153d121c12` |
| Source Image Tag | `5153d121c12` |
| Target Release Tag(s) | `prod-5153d121c12`, optionally `prod` |
| Source Environment | `STAGE` |
| Target Environment | `PROD` |

For production, use an immutable production tag such as `prod-5153d121c12` for auditability. If the client also wants a mutable `prod` alias, keep it as a convenience pointer, not the primary audit identifier.

## 8. Evidence and Audit Artifacts

The Build & Deploy pipeline generated:

```text
s3://acme-fintech-devsecops/devops-pipeline/acme-fintech-angular-main/5153d121c12/image.json
s3://acme-fintech-devsecops/devops-pipeline/acme-fintech-angular-main/5153d121c12/templateconfiguration.json
```

The QA promotion generated:

```text
s3://acme-fintech-devsecops/devops-pipeline/acme-fintech-angular-main/5153d121c12/qa/approval.json
s3://acme-fintech-devsecops/devops-pipeline/acme-fintech-angular-main/5153d121c12/qa/deployment.json
```

The STAGE promotion generated:

```text
s3://acme-fintech-devsecops/devops-pipeline/acme-fintech-angular-main/5153d121c12/stage/approval.json
s3://acme-fintech-devsecops/devops-pipeline/acme-fintech-angular-main/5153d121c12/stage/deployment.json
```

### image.json

```json
{
  "ImageURI": "426946630837.dkr.ecr.us-east-1.amazonaws.com/acme-fintech@sha256:ef76917d6020f8ce39c1db1f667d0716496136bd7a4ea30dca5789284db315c1",
  "ImageSHA": "sha256:ef76917d6020f8ce39c1db1f667d0716496136bd7a4ea30dca5789284db315c1",
  "ImageRepo": "426946630837.dkr.ecr.us-east-1.amazonaws.com/acme-fintech",
  "ImageTag": "5153d121c12"
}
```

### templateconfiguration.json

```json
{
  "Parameters": {
    "ProjectType": "Angular",
    "ImageName": "acme-fintech",
    "ImageURI": "426946630837.dkr.ecr.us-east-1.amazonaws.com/acme-fintech@sha256:ef76917d6020f8ce39c1db1f667d0716496136bd7a4ea30dca5789284db315c1",
    "ImageRepo": "426946630837.dkr.ecr.us-east-1.amazonaws.com/acme-fintech",
    "ImageTag": "5153d121c12",
    "TargetEnv": "DEV"
  }
}
```

### QA deployment evidence

```json
{
  "application": "acme-fintech-angular-qa",
  "sourceEnv": "DEV",
  "targetEnv": "QA",
  "imageUri": "426946630837.dkr.ecr.us-east-1.amazonaws.com/acme-fintech@sha256:1ef7dcb6c14ccf26b00b2c206c088c7fcf6642687d83aba8b20464bdf07c343d",
  "imageDigest": "sha256:1ef7dcb6c14ccf26b00b2c206c088c7fcf6642687d83aba8b20464bdf07c343d",
  "sourceImageDigest": "sha256:ef76917d6020f8ce39c1db1f667d0716496136bd7a4ea30dca5789284db315c1",
  "targetTags": "qa-5153d121c12",
  "jenkinsJob": "acme-fintech-angular-qa-qa-release",
  "buildNumber": "5",
  "buildUrl": "https://horizonrelevance.com/jenkins/job/acme-fintech-angular-qa-qa-release/5/",
  "deployedAt": "2026-05-19T19:33:13Z"
}
```

### STAGE deployment evidence

```json
{
  "application": "acme-fintech-angular",
  "sourceEnv": "QA",
  "targetEnv": "STAGE",
  "imageUri": "426946630837.dkr.ecr.us-east-1.amazonaws.com/acme-fintech@sha256:1ef7dcb6c14ccf26b00b2c206c088c7fcf6642687d83aba8b20464bdf07c343d",
  "imageDigest": "sha256:1ef7dcb6c14ccf26b00b2c206c088c7fcf6642687d83aba8b20464bdf07c343d",
  "sourceImageDigest": "sha256:ef76917d6020f8ce39c1db1f667d0716496136bd7a4ea30dca5789284db315c1",
  "targetTags": "stage-5153d121c12",
  "jenkinsJob": "acme-fintech-angular-stage-release",
  "buildNumber": "1",
  "buildUrl": "https://horizonrelevance.com/jenkins/job/acme-fintech-angular-stage-release/1/",
  "deployedAt": "2026-05-20T00:35:51Z"
}
```

## 9. Validation Checklist

| Check | Expected Result |
|---|---|
| Build pipeline status | Jenkins build is `SUCCESS` |
| S3 base artifacts | `image.json` and `templateconfiguration.json` exist |
| ECR image | image tag `5153d121c12` and digest exist |
| DEV deployment | application pod runs in DEV namespace |
| DEV validation | selected validation gates pass against the DEV URL |
| QA promotion | QA release job is `SUCCESS` and writes QA evidence |
| QA deployment | QA pod is `Running` |
| QA validation | selected validation gates pass against the QA URL and promoted digest |
| STAGE promotion | STAGE release job is `SUCCESS` and writes STAGE evidence |
| STAGE deployment | STAGE pod is `Running` |
| PROD promotion | approval is captured and the same digest is deployed to PROD |
| Digest control | promoted environments use the immutable release image, not a source rebuild |

## 10. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Release job cannot find `image.json` | Artifact prefix includes `/qa` or `/stage` | Use base prefix `devops-pipeline/acme-fintech-angular-main/5153d121c12` |
| QA or STAGE rebuilds image | Build & Deploy pipeline used instead of Release Promotion | Use Release Promotion Pipeline after DEV build |
| EKS access error | Deploy role lacks EKS access entry or namespace RBAC | Run installer preflight and fix deploy role mapping |
| S3 access denied | Jenkins runtime role cannot read/write artifact bucket | Update client IAM role policy |
| ECR image not found | Wrong repository, tag, or account mapping | Verify Environment Catalog and ECR repository |
| Notification failure | SMTP not configured or requester email invalid | Configure client SMTP provider and notification settings |

## 11. Appendix: Captured Evidence

Evidence files were captured under local workspace `captures/` and embedded in the DOCX version.

### Frontend workflow snapshots

![Build and Deploy Pipeline](assets/build-release/frontend-build-deploy-dev.png)

![Validation Pipeline](assets/build-release/frontend-validation-qa.png)

![Release Promotion Pipeline](assets/build-release/frontend-release-promotion-qa-to-stage.png)

### Platform evidence snapshots

![S3 Artifact Tree](assets/build-release/s3-artifact-tree.png)

![image.json](assets/build-release/image-json.png)

![templateconfiguration.json](assets/build-release/templateconfiguration-json.png)

![ECR Metadata](assets/build-release/ecr-metadata.png)

![Jenkins Build Log](assets/build-release/jenkins-build-excerpt.png)

![Jenkins QA Release Log](assets/build-release/jenkins-qa-excerpt.png)

![Jenkins STAGE Release Log](assets/build-release/jenkins-stage-excerpt.png)

![Kubernetes Runtime Evidence](assets/build-release/kubernetes-pods.png)
