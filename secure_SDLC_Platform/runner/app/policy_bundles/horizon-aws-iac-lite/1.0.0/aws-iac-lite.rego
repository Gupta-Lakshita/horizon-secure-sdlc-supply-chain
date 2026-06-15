package main

deny[msg] {
  input.resource_type == "aws_s3_bucket_public_access_block"
  input.change.after.block_public_acls == false
  msg := "HR-POL-AWS-LITE-001 S3 buckets must block public ACLs"
}

deny[msg] {
  input.kind == "Ingress"
  input.spec.rules[_].host == "*"
  msg := "HR-POL-AWS-LITE-002 wildcard ingress host is not allowed"
}
