package main

deny[msg] {
  input.kind == "Deployment"
  input.metadata.labels["horizonrelevance.com/environment"] == "prod"
  not input.metadata.annotations["horizonrelevance.com/change-ticket"]
  msg := "HR-POL-PROD-001 production deployments require horizonrelevance.com/change-ticket annotation"
}

deny[msg] {
  input.kind == "Deployment"
  input.metadata.labels["horizonrelevance.com/environment"] == "prod"
  container := input.spec.template.spec.containers[_]
  not contains(container.image, "@sha256:")
  msg := sprintf("HR-POL-PROD-002 production container %s must use immutable image digest", [container.name])
}
