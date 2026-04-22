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
  description = "Whether the RDS instance is publicly accessible"
  type        = bool
  default     = true
}

variable "vpc_cidr" {
  description = "CIDR block for the dedicated VPC"
  type        = string
  default     = "10.100.0.0/16"
}

variable "allowed_cidr_blocks" {
  description = "CIDR blocks allowed to connect to the database"
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

variable "tags" {
  description = "Tags to apply to all resources"
  type        = map(string)
  default     = {}
}
