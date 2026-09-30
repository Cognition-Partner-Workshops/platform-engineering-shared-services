variable "region" {
  description = "AWS region. Must match the region of the worker stack."
  type        = string
  default     = "us-east-2"
}

variable "name" {
  description = "Base name applied to all resources."
  type        = string
  default     = "devin-outpost-vpc-demo"
}

variable "vpc_cidr" {
  description = "CIDR for the demo VPC."
  type        = string
  default     = "10.42.0.0/16"
}

variable "server_port" {
  description = "Port the internal service listens on."
  type        = number
  default     = 8080
}

variable "server_banner" {
  description = "Body the internal service returns, used as proof a worker reached it."
  type        = string
  default     = "internal-service-reachable"
}

variable "server_instance_type" {
  description = "Instance type for the internal service. Needs to be arm64 to match the AMI filter."
  type        = string
  default     = "t4g.nano"
}

variable "python_bin" {
  description = <<-EOT
    Python used to drive the network connector API. Needs boto3 >= 1.41 /
    botocore >= 1.43, the first versions carrying the lambda-core connector model.
  EOT
  type        = string
  default     = "python3"
}
