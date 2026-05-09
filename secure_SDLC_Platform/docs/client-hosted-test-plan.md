# Client-Hosted Test Plan

Use this flow to simulate a real regulated client while Horizon Relevance acts as the vendor.

## Simulated Client

- Client: Acme Fintech
- Namespace: `acme-devsecops`
- Domain: `devsecops.acme-demo.com`
- AWS account: client-owned or isolated account/prefix
- ECR: client-owned
- S3: client-owned artifact bucket
- IAM: client-owned execution role

## Validation Flow

1. Provision or isolate the client AWS environment.
2. Deploy OpenLDAP, Keycloak, Jenkins, backend, frontend, and ingress into the client namespace.
3. Configure backend enterprise license values.
4. Confirm `GET /pipeline/api/license/status` returns active trial details.
5. Submit `POST /pipeline/api/devops/pipeline` with client cloud fields.
6. Confirm backend denies invalid/expired license payloads.
7. Confirm Jenkins starts with `Validate License`.
8. Confirm Jenkins does not print license keys or signatures.
9. Run Angular, Spring Boot, Node.js, and WebComponent demo apps.
10. Confirm code is cloned only inside the client Jenkins pod.
11. Confirm image is pushed to client ECR.
12. Confirm metadata, test results, and evidence are written to client S3.
13. Confirm the security dashboard shows findings without exposing scanner vendor names.
14. Confirm prod deployment is blocked when `prod_deploy` is not licensed.
15. Confirm trial limits and usage enforcement after database-backed counters are added.

## Data Boundary Checks

- No source code leaves the client environment.
- No client secrets are stored in ConfigMaps.
- No raw scanner reports leave the client environment unless explicitly configured.
- Horizon Relevance license validation receives only license metadata.
