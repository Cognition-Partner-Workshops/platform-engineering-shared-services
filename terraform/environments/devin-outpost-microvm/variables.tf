variable "region" {
  description = "AWS region. Lambda MicroVMs are available in us-east-1, us-east-2 and us-west-2."
  type        = string
  default     = "us-east-2"
}

variable "name" {
  description = "Base name applied to all resources."
  type        = string
  default     = "devin-outpost-microvm"
}

variable "project" {
  description = "Project tag value."
  type        = string
  default     = "devin-outposts"
}

variable "outpost_id" {
  description = "Outpost to serve, e.g. outpost_env-xxxxxxxx. Create it under Settings > Environment > Outposts."
  type        = string
}

variable "outpost_token" {
  description = <<-EOT
    Service account token (cog_...) with the outposts machine scope, shown once at
    outpost creation. Only the reconciler reads it; workers never see it. Pass it via
    TF_VAR_outpost_token rather than a tfvars file so it stays out of the repo.
  EOT
  type        = string
  sensitive   = true
}

variable "devin_api_url" {
  description = "Devin API base URL."
  type        = string
  default     = "https://api.devin.ai"
}

variable "max_concurrent_sessions" {
  description = "Sessions served at once. Each claimed session runs in its own MicroVM."
  type        = number
  default     = 2
}

variable "reconcile_interval_minutes" {
  description = <<-EOT
    How often the reconciler polls the queue. Claims last about 5 minutes, so this must
    stay well below claim_renew_margin_seconds. It also bounds session pickup latency.
  EOT
  type        = number
  default     = 1
}

variable "claim_renew_margin_seconds" {
  description = "Renew a claim when its deadline is within this many seconds."
  type        = number
  default     = 60
}

variable "microvm_memory_mib" {
  description = <<-EOT
    Baseline memory per worker. vCPU scales with it at 2 GiB per vCPU, and a MicroVM
    bursts to 4x baseline. Valid: 512, 1024, 2048, 4096, 8192.
  EOT
  type        = number
  default     = 4096

  validation {
    condition     = contains([512, 1024, 2048, 4096, 8192], var.microvm_memory_mib)
    error_message = "microvm_memory_mib must be one of 512, 1024, 2048, 4096, 8192."
  }
}

variable "microvm_max_duration_seconds" {
  description = "Hard lifetime cap per worker, which is also the cap on a single session. AWS maximum is 28800 (8h)."
  type        = number
  default     = 28800

  validation {
    condition     = var.microvm_max_duration_seconds > 0 && var.microvm_max_duration_seconds <= 28800
    error_message = "microvm_max_duration_seconds must be between 1 and 28800."
  }
}

variable "enable_ingress" {
  description = <<-EOT
    Attach the ALL_INGRESS connector so the worker's HTTPS endpoint is reachable for
    debugging. Lifecycle hooks are delivered inside the MicroVM and do not need it, so
    the default is the NO_INGRESS connector.
  EOT
  type        = bool
  default     = false
}

variable "log_retention_days" {
  description = "CloudWatch log retention for the reconciler, image builds and workers."
  type        = number
  default     = 14
}

variable "python_bin" {
  description = <<-EOT
    Python used to build the reconciler package and drive the MicroVM image API. Needs
    boto3 >= 1.41 / botocore >= 1.43, the first versions carrying the lambda-microvms model.
  EOT
  type        = string
  default     = "python3"
}
