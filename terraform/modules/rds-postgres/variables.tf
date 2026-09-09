variable "identifier" {
  description = "RDS instance identifier"
  type        = string
}

variable "region" {
  description = "AWS region for subnet AZs"
  type        = string
  default     = "us-east-1"
}

variable "engine_version" {
  description = "PostgreSQL engine version"
  type        = string
  default     = "16.4"
}

variable "instance_class" {
  description = "RDS instance class"
  type        = string
  default     = "db.t4g.micro"
}

variable "allocated_storage" {
  description = "Allocated storage in GB"
  type        = number
  default     = 20
}

variable "max_allocated_storage" {
  description = "Max allocated storage in GB for autoscaling"
  type        = number
  default     = 50
}

variable "database_name" {
  description = "Name of the default database to create"
  type        = string
}

variable "master_username" {
  description = "Master username for the database"
  type        = string
}

variable "master_password" {
  description = "Master password for the database"
  type        = string
  sensitive   = true
}

variable "publicly_accessible" {
  description = "Whether the RDS instance gets a public IP. Defaults to false; when enabled, allowed_cidr_blocks must list specific trusted CIDRs (VPN/office egress) and must not contain 0.0.0.0/0 or ::/0."
  type        = bool
  default     = false
}

variable "vpc_cidr" {
  description = "CIDR block for the dedicated VPC"
  type        = string
  default     = "10.100.0.0/16"
}

variable "allowed_cidr_blocks" {
  description = "CIDR blocks allowed to connect to the database on 5432/tcp. Empty (default) restricts ingress to the module's own VPC CIDR. Wildcard CIDRs (0.0.0.0/0, ::/0) are rejected."
  type        = list(string)
  default     = []

  validation {
    condition = alltrue([
      for cidr in var.allowed_cidr_blocks :
      !contains(["0.0.0.0/0", "::/0"], cidr) && !can(regex("/0$", cidr))
    ])
    error_message = "allowed_cidr_blocks must not contain wildcard CIDRs (0.0.0.0/0, ::/0); list specific trusted CIDRs instead."
  }
}

variable "tags" {
  description = "Tags to apply to all resources"
  type        = map(string)
  default     = {}
}
