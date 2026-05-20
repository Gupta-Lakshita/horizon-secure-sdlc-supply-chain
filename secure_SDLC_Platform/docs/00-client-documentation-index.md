# Client Documentation Reading Order

This index is the recommended sequence for client platform admins, developers, QA engineers, release managers, and security reviewers onboarding to the Horizon Relevance client-hosted AI DevSecOps platform.

## Fast Path By Role

| Role | Read These First |
| --- | --- |
| Product owner / buyer | 01, 02, 07 |
| Client platform admin | 01, 02, 03, 04, 08, 09, 10 |
| Security / cloud governance | 02, 03, 04, 05, 06, 07 |
| Developer | 01, 11, 12 |
| QA engineer | 11, 12 |
| Release manager | 11, 12 |

## Sequential Reading Path

1. [Client Onboarding, Trial, Paid, and Enterprise Playbook](client-onboarding-trial-paid-enterprise-playbook.md)  
   Start here to understand the business onboarding model, trial flow, paid conversion, client responsibilities, and Horizon Relevance responsibilities.

2. [Client Enterprise Architecture](client-enterprise-architecture.md)  
   Read this to understand the end-to-end client-hosted architecture, AWS services, product services, identity, CI/CD flow, and runtime model.

3. [AWS IAM and EKS Prerequisites](aws-iam-eks-prerequisites.md)  
   Use this before installation to prepare IAM roles, Jenkins IRSA, deploy-role assumptions, EKS access, and namespace-scoped permissions.

4. [Validation-Only Namespace-Scoped Access](validation-only-namespace-scoped-access.md)  
   Read this for the enterprise model where the client owns IAM and the installer validates access instead of creating broad roles.

5. [Sensitive Client Data Strategy](sensitive-client-data-strategy.md)  
   Review how client-owned AWS account IDs, domains, roles, secrets, and catalog values should be handled safely.

6. [Private ECR Image Distribution](private-ecr-image-distribution.md)  
   Review the product image delivery model, private registry access, and container extraction risk controls.

7. [License Contract](license-contract.md)  
   Understand license payloads, online sync, trial expiration, installation binding, and Jenkins/backend enforcement.

8. [Client Values Reference](client-values-reference.md)  
   Use this when preparing `client-values.yaml` for full-provision, BYO, and hybrid infrastructure modes.

9. [Generic Hybrid Installer Lifecycle](generic-hybrid-installer-lifecycle.md)  
   Read this before running installer phases for state, infrastructure, platform, catalog sync, validation, and destroy.

10. [Installer Runbook](installer-runbook.md)  
    Follow this as the operational step-by-step installation guide.

11. [Build, Release, and Deployment Runbook](build-release-deployment-runbook.md)  
    Use this after the platform is installed. It explains how engineers build once in DEV, validate the running app, and promote the same immutable image digest through QA, STAGE, and PROD.

12. [Client Hosted Test Plan](client-hosted-test-plan.md)  
    Use this to perform an end-to-end client simulation and capture test evidence.

## Downloadable Client Artifact

- [Build, Release, and Deployment Runbook DOCX](build-release-deployment-runbook.docx)

