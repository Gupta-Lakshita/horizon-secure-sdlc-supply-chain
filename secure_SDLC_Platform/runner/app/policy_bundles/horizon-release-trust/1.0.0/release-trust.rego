package horizon.release_trust

# HR-POL-RT-001: All environments require immutable image digest
deny[msg] {
  input.kind == "Deployment"
  container := input.spec.template.spec.containers[_]
  not contains(container.image, "@sha256:")
  msg := sprintf("HR-POL-RT-001 container %s must use immutable image digest", [container.name])
}

# HR-POL-RT-002: QA/STAGE/PROD require SBOM evidence annotation
deny[msg] {
  input.kind == "Deployment"
  env := input.metadata.labels["horizonrelevance.com/environment"]
  env != "dev"
  not input.metadata.annotations["horizonrelevance.com/sbom-status"] == "present"
  msg := sprintf("HR-POL-RT-002 environment %s requires SBOM evidence before promotion", [env])
}

# HR-POL-RT-003: PROD requires valid signature annotation
deny[msg] {
  input.kind == "Deployment"
  input.metadata.labels["horizonrelevance.com/environment"] == "prod"
  not input.metadata.annotations["horizonrelevance.com/signature-status"] == "valid"
  msg := "HR-POL-RT-003 production deployment requires a valid image signature"
}

# HR-POL-RT-004: PROD requires valid provenance annotation
deny[msg] {
  input.kind == "Deployment"
  input.metadata.labels["horizonrelevance.com/environment"] == "prod"
  not input.metadata.annotations["horizonrelevance.com/provenance-status"] == "valid"
  msg := "HR-POL-RT-004 production deployment requires valid SLSA provenance"
}

# HR-POL-RT-005: PROD requires release-manager approval annotation
deny[msg] {
  input.kind == "Deployment"
  input.metadata.labels["horizonrelevance.com/environment"] == "prod"
  not input.metadata.annotations["horizonrelevance.com/approved-by"]
  msg := "HR-POL-RT-005 production deployment requires release-manager approval"
}

# HR-POL-RT-006: Warn if deployed digest doesn't match approved digest
warn[msg] {
  input.kind == "Deployment"
  env := input.metadata.labels["horizonrelevance.com/environment"]
  env != "dev"
  approved := input.metadata.annotations["horizonrelevance.com/approved-digest"]
  container := input.spec.template.spec.containers[_]
  not endswith(container.image, approved)
  msg := sprintf("HR-POL-RT-006 container %s digest does not match approved digest", [container.name])
}