#!/usr/bin/env ruby
# frozen_string_literal: true

require "json"
require "yaml"

def usage!
  warn <<~USAGE
    Usage:
      values-helper.rb validate --file <values.yaml> [--environment QA]
      values-helper.rb plan --file <values.yaml> [--environment QA]
      values-helper.rb checks --file <values.yaml> [--environment QA]
      values-helper.rb provision-items --file <values.yaml> [--environment QA]
      values-helper.rb destroy-items --file <values.yaml> --environment QA
      values-helper.rb tfvars --file <values.yaml> --environment QA
      values-helper.rb state-tfvars --file <values.yaml>
      values-helper.rb backend-config --file <values.yaml> --scope <platform|environment> [--environment QA]
      values-helper.rb get --file <values.yaml> --path installer.namespace
  USAGE
  exit 1
end

def parse_args(argv)
  args = { command: argv.shift || usage! }
  until argv.empty?
    case (key = argv.shift)
    when "--file", "-f" then args[:file] = argv.shift
    when "--environment", "-e" then args[:environment] = argv.shift&.upcase
    when "--path" then args[:path] = argv.shift
    when "--scope" then args[:scope] = argv.shift
    else
      warn "Unknown argument: #{key}"
      usage!
    end
  end
  args
end

def load_values(path)
  usage! if path.to_s.empty?
  YAML.load_file(path)
rescue Psych::SyntaxError => e
  warn "YAML parse failed: #{e.message}"
  exit 1
end

def dig_path(obj, path)
  path.to_s.split(".").reduce(obj) do |cursor, key|
    return nil if cursor.nil?
    if cursor.is_a?(Hash)
      cursor[key]
    else
      nil
    end
  end
end

def environments(values)
  envs = values["environments"]
  return envs if envs.is_a?(Array) && envs.any?
  dig_path(values, "environmentCatalog.environments") || []
end

def environment(values, name)
  environments(values).find { |env| env["name"].to_s.upcase == name.to_s.upcase }
end

def env_or_exit(values, name)
  env = environment(values, name)
  return env if env
  warn "Environment not found in values file: #{name}"
  exit 1
end

def state_of(hash)
  hash.is_a?(Hash) ? hash.fetch("state", "disabled").to_s : "disabled"
end

def deletion_policy_of(hash, values)
  default_policy = dig_path(values, "lifecycle.defaultDeletionPolicy") || "retain"
  hash.is_a?(Hash) ? hash.fetch("deletionPolicy", default_policy).to_s : default_policy
end

def role_name(role_arn)
  role_arn.to_s.split("/").last
end

def walk_resources(object, prefix = [], &block)
  return unless object.is_a?(Hash)
  yield prefix.join("."), object if object.key?("state")
  object.each do |key, value|
    walk_resources(value, prefix + [key], &block) if value.is_a?(Hash)
    value.each_with_index { |item, i| walk_resources(item, prefix + ["#{key}[#{i}]"], &block) } if value.is_a?(Array)
  end
end

def validate_values(values, env_name = nil)
  errors = []
  %w[installer client license accessModel].each { |key| errors << "Missing top-level key: #{key}" unless values[key].is_a?(Hash) }
  errors << "Missing top-level environments list" unless environments(values).any?
  names = environments(values).map { |env| env["name"].to_s.upcase }
  errors << "Requested environment is not defined: #{env_name}" if env_name && !names.include?(env_name.upcase)

  counts = Hash.new(0)
  names.each { |name| counts[name] += 1 }
  duplicates = counts.select { |_name, count| count > 1 }.keys
  errors << "Duplicate environment names: #{duplicates.join(", ")}" if duplicates.any?

  unless dig_path(values, "lifecycle.allowDestroyExistingResources") == true
    environments(values).each do |env|
      walk_resources(env) do |path, resource|
        if state_of(resource) == "existing" && deletion_policy_of(resource, values) == "delete"
          errors << "#{env["name"]}.#{path} is existing but has deletionPolicy=delete"
        end
      end
    end
  end

  if errors.any?
    warn errors.join("\n")
    exit 1
  end
end

def check_lines(values, env_name = nil)
  envs = env_name ? [env_or_exit(values, env_name)] : environments(values)
  lines = []

  if state_of(values["terraformState"]) == "existing"
    region = dig_path(values, "terraformState.region") || dig_path(values, "platform.region")
    lines << ["s3_bucket", dig_path(values, "terraformState.bucket"), region, "terraformState.bucket"]
    lines << ["dynamodb_table", dig_path(values, "terraformState.lockTable"), region, "terraformState.lockTable"]
  end

  if state_of(dig_path(values, "platform.cluster")) == "existing"
    lines << ["eks_cluster", dig_path(values, "platform.cluster.name"), dig_path(values, "platform.region"), "platform.cluster"]
  end

  %w[accessModel.jenkins.runtimeRole accessModel.backend.validationRole].each do |path|
    role = dig_path(values, path)
    lines << ["iam_role", role_name(role["roleArn"]), dig_path(values, "platform.region"), path] if state_of(role) == "existing" && role["roleArn"].to_s != ""
  end

  envs.each do |env|
    region = dig_path(env, "aws.region") || dig_path(values, "platform.region")
    lines << ["s3_bucket", dig_path(env, "foundation.artifactBucket.name"), region, "#{env["name"]}.artifactBucket"] if state_of(dig_path(env, "foundation.artifactBucket")) == "existing"
    lines << ["ecr_repository", dig_path(env, "foundation.applicationEcr.repositoryName"), region, "#{env["name"]}.applicationEcr"] if state_of(dig_path(env, "foundation.applicationEcr")) == "existing"
    lines << ["eks_cluster", dig_path(env, "eks.clusterName"), region, "#{env["name"]}.eks"] if state_of(dig_path(env, "eks")) == "existing"
    %w[deployRole sourceRole targetRole].each do |role_key|
      role = dig_path(env, "iam.#{role_key}")
      lines << ["iam_role", role_name(role["roleArn"]), region, "#{env["name"]}.iam.#{role_key}"] if state_of(role) == "existing" && role["roleArn"].to_s != ""
    end
  end

  lines.compact.each { |line| puts line.join("\t") }
end

def provision_items(values, env_name = nil, destroy: false)
  envs = env_name ? [env_or_exit(values, env_name)] : environments(values)
  envs.each do |env|
    walk_resources(env) do |path, resource|
      next unless state_of(resource) == "provision"
      next if destroy && deletion_policy_of(resource, values) != "delete"
      label = resource["clusterName"] || resource["name"] || resource["repositoryName"] || resource["template"] || resource["roleArn"] || path
      puts [env["name"], path, "provision", deletion_policy_of(resource, values), label].join("\t")
    end
  end
end

def plan(values, env_name = nil)
  validate_values(values, env_name)
  envs = env_name ? [env_or_exit(values, env_name)] : environments(values)
  puts "== Horizon installer desired-state plan =="
  puts "Installer mode: #{dig_path(values, "installer.mode")}"
  puts "Terraform state bucket: #{dig_path(values, "terraformState.bucket")}"
  puts "Terraform lock table: #{dig_path(values, "terraformState.lockTable")}"
  puts
  envs.each do |env|
    puts "Environment: #{env["name"]} (#{env["displayName"]})"
    puts "  Account: #{dig_path(env, "aws.accountId")}"
    puts "  Region: #{dig_path(env, "aws.region")}"
    puts "  Terraform state: #{dig_path(env, "terraform.stateKey")}"
    walk_resources(env) do |path, resource|
      next if state_of(resource) == "disabled"
      label = resource["clusterName"] || resource["name"] || resource["repositoryName"] || resource["roleArn"] || resource["template"] || ""
      puts "  - #{path}: #{state_of(resource)}, deletionPolicy=#{deletion_policy_of(resource, values)} #{label}"
    end
    puts
  end
end

def backend_config(values, scope, env_name = nil)
  tfstate = values["terraformState"] || {}
  key = if scope == "platform"
          dig_path(values, "platform.terraform.stateKey") || "#{tfstate["keyPrefix"]}/platform/terraform.tfstate"
        elsif scope == "environment"
          env = env_or_exit(values, env_name)
          dig_path(env, "terraform.stateKey") || "#{tfstate["keyPrefix"]}/#{env_name.downcase}/terraform.tfstate"
        else
          warn "--scope must be platform or environment"
          exit 1
        end
  puts "bucket         = #{tfstate["bucket"].to_s.inspect}"
  puts "key            = #{key.to_s.inspect}"
  puts "region         = #{(tfstate["region"] || dig_path(values, "platform.region") || "us-east-1").inspect}"
  puts "dynamodb_table = #{tfstate["lockTable"].to_s.inspect}" if tfstate["lockTable"].to_s != ""
  puts "encrypt        = true"
  puts "kms_key_id     = #{tfstate["kmsKeyArn"].inspect}" if tfstate["kmsKeyArn"].to_s != ""
end

def tfvars(values, env_name)
  env = env_or_exit(values, env_name)
  result = {
    environment_name: env["name"],
    client_id: dig_path(values, "client.id"),
    aws_region: dig_path(env, "aws.region"),
    aws_account_id: dig_path(env, "aws.accountId"),
    tags: { Client: dig_path(values, "client.id"), Environment: env["name"], ManagedBy: "horizon-enterprise-installer" },
    create_vpc: state_of(dig_path(env, "foundation.vpc")) == "provision",
    existing_vpc_id: dig_path(env, "foundation.vpc.vpcId"),
    existing_subnet_ids: dig_path(env, "foundation.vpc.subnetIds") || [],
    create_kms_key: state_of(dig_path(env, "foundation.kms")) == "provision",
    existing_kms_key_arn: dig_path(env, "foundation.kms.keyArn"),
    create_artifact_bucket: state_of(dig_path(env, "foundation.artifactBucket")) == "provision",
    artifact_bucket_name: dig_path(env, "foundation.artifactBucket.name"),
    create_ecr_repository: state_of(dig_path(env, "foundation.applicationEcr")) == "provision",
    ecr_repository_name: dig_path(env, "foundation.applicationEcr.repositoryName"),
    create_secret_prefix: state_of(dig_path(env, "foundation.secretsManager")) == "provision",
    secret_prefix: dig_path(env, "foundation.secretsManager.secretPrefix"),
    create_acm_certificate: state_of(dig_path(env, "foundation.acm")) == "provision",
    existing_acm_certificate_arn: dig_path(env, "foundation.acm.certificateArn"),
    create_route53_records: state_of(dig_path(env, "foundation.dns")) == "provision",
    base_domain: dig_path(env, "foundation.dns.baseDomain"),
    create_eks_cluster: state_of(dig_path(env, "eks")) == "provision",
    eks_cluster_name: dig_path(env, "eks.clusterName"),
    kubernetes_version: dig_path(env, "eks.kubernetesVersion") || "1.30",
    eks_endpoint_public_access: dig_path(env, "eks.endpointPublicAccess") != false,
    create_ebs_csi_driver: state_of(dig_path(env, "eks.ebsCsiDriver")) == "provision",
    create_ingress_controller: state_of(dig_path(env, "eks.ingressController")) == "provision",
    create_node_group: state_of(dig_path(env, "eks.nodeGroup")) == "provision",
    node_instance_types: dig_path(env, "eks.nodeGroup.instanceTypes") || ["t3.small"],
    node_desired_size: dig_path(env, "eks.nodeGroup.desiredSize") || 1,
    node_min_size: dig_path(env, "eks.nodeGroup.minSize") || 1,
    node_max_size: dig_path(env, "eks.nodeGroup.maxSize") || 2,
    create_namespace: state_of(dig_path(env, "eks.namespace")) == "provision",
    namespace_name: dig_path(env, "eks.namespace.template"),
    create_eks_access_entry: state_of(dig_path(env, "eks.accessEntry")) == "provision",
    eks_access_policy_arn: dig_path(env, "eks.accessEntry.policyArn"),
    eks_access_scope_type: dig_path(env, "eks.accessEntry.scopeType") || "namespace",
    deploy_role_arn: dig_path(env, "iam.deployRole.roleArn"),
    jenkins_runtime_role_arn: dig_path(values, "accessModel.jenkins.runtimeRole.roleArn"),
    create_deploy_role: state_of(dig_path(env, "iam.deployRole")) == "provision",
    deletion_protection: dig_path(values, "lifecycle.deletionProtection") != false
  }
  puts JSON.pretty_generate(result)
end

def state_tfvars(values)
  tfstate = values["terraformState"] || {}
  puts JSON.pretty_generate({
    aws_region: tfstate["region"] || dig_path(values, "platform.region") || "us-east-1",
    create_state_bucket: state_of(tfstate) == "provision",
    state_bucket_name: tfstate["bucket"],
    state_lock_table_name: tfstate["lockTable"],
    state_kms_key_arn: tfstate["kmsKeyArn"],
    state_key_prefix: tfstate["keyPrefix"],
    tags: { Client: dig_path(values, "client.id"), ManagedBy: "horizon-enterprise-installer", Purpose: "terraform-state" }
  })
end

args = parse_args(ARGV)
values = load_values(args[:file])

case args[:command]
when "validate" then validate_values(values, args[:environment]); puts "Values OK"
when "plan" then plan(values, args[:environment])
when "checks" then validate_values(values, args[:environment]); check_lines(values, args[:environment])
when "provision-items" then validate_values(values, args[:environment]); provision_items(values, args[:environment])
when "destroy-items" then validate_values(values, args[:environment]); provision_items(values, args[:environment], destroy: true)
when "tfvars" then validate_values(values, args[:environment]); tfvars(values, args[:environment] || (warn("--environment is required for tfvars") && exit(1)))
when "state-tfvars" then validate_values(values, args[:environment]); state_tfvars(values)
when "backend-config" then validate_values(values, args[:environment]); backend_config(values, args[:scope], args[:environment])
when "get"
  value = dig_path(values, args[:path])
  puts(value.is_a?(Hash) || value.is_a?(Array) ? JSON.pretty_generate(value) : value) unless value.nil?
else
  usage!
end
