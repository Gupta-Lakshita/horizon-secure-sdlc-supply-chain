package main

deny[msg] {
  input.resource_type == "aws_s3_bucket_public_access_block"
  input.change.after.block_public_policy == false
  msg := "HR-POL-AWS-001 S3 buckets must block public bucket policies"
}

deny[msg] {
  input.resource_type == "aws_security_group_rule"
  input.change.after.cidr_blocks[_] == "0.0.0.0/0"
  input.change.after.from_port < 1024
  msg := "HR-POL-AWS-002 privileged ports must not be open to the internet"
}

deny[msg] {
  input.kind == "Ingress"
  not input.spec.tls
  msg := "HR-POL-AWS-003 ingress must define TLS"
}
