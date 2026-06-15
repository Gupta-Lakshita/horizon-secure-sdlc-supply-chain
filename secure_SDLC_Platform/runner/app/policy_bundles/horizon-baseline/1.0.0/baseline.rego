package main

deny[msg] {
  input.kind == "Pod"
  input.spec.hostNetwork == true
  msg := "HR-POL-BASE-001 hostNetwork is not allowed for baseline workloads"
}

deny[msg] {
  input.kind == "Service"
  input.spec.type == "LoadBalancer"
  not input.metadata.annotations["horizonrelevance.com/allow-loadbalancer"]
  msg := "HR-POL-BASE-002 LoadBalancer services require horizonrelevance.com/allow-loadbalancer annotation"
}

warn[msg] {
  input.kind == "Deployment"
  not input.metadata.labels["app.kubernetes.io/name"]
  msg := "HR-POL-BASE-003 workload should include app.kubernetes.io/name label"
}
