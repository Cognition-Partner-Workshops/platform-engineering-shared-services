################################################################################
# Demo Banking Database
#
# Provisions an RDS PostgreSQL instance seeded with banking-themed data.
# Intended for short-lived demos showcasing JDBC/MCP connector integration,
# DDL introspection, and materialized view queries.
#
# The database is private by default. To reach it from outside the VPC, set
# publicly_accessible = true AND allowed_cidr_blocks to specific trusted
# operator CIDRs (VPN/office egress). Wildcard CIDRs are rejected.
#
# Teardown:
#   cd terraform/environments/demo-banking-db
#   terraform destroy -auto-approve
################################################################################

terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }

  backend "s3" {
    bucket         = "workshop-terraform-state-599083837640"
    key            = "platform/demo-banking-db/terraform.tfstate"
    region         = "us-east-1"
    dynamodb_table = "workshop-terraform-lock"
    encrypt        = true
  }
}

provider "aws" {
  region = var.region
}

locals {
  tags = {
    Environment = "demo"
    ManagedBy   = "terraform"
    Project     = "workshop-platform"
    Component   = "banking-demo-db"
  }
}

################################################################################
# RDS PostgreSQL
################################################################################

module "rds" {
  source = "../../modules/rds-postgres"

  identifier    = "workshop-banking-demo"
  region        = var.region
  database_name = "banking"

  master_username = var.db_username
  master_password = var.db_password

  instance_class        = "db.t4g.micro"
  engine_version        = "16.4"
  allocated_storage     = 20
  max_allocated_storage = 50
  publicly_accessible   = var.publicly_accessible

  vpc_cidr            = "10.100.0.0/16"
  allowed_cidr_blocks = var.allowed_cidr_blocks

  tags = local.tags
}

################################################################################
# Variables
################################################################################

variable "region" {
  description = "AWS region"
  type        = string
  default     = "us-east-1"
}

variable "db_username" {
  description = "Database master username"
  type        = string
  default     = "bankadmin"
}

variable "db_password" {
  description = "Database master password"
  type        = string
  sensitive   = true
}

variable "publicly_accessible" {
  description = "Assign a public IP to the RDS instance. Requires allowed_cidr_blocks to list specific trusted CIDRs."
  type        = bool
  default     = false
}

variable "allowed_cidr_blocks" {
  description = "Trusted CIDRs allowed to reach PostgreSQL (5432/tcp), e.g. [\"203.0.113.10/32\"]. Empty restricts access to the database VPC. Wildcards (0.0.0.0/0) are rejected."
  type        = list(string)
  default     = []
}

################################################################################
# Outputs
################################################################################

output "db_endpoint" {
  description = "RDS endpoint (host:port)"
  value       = module.rds.endpoint
}

output "db_hostname" {
  description = "RDS hostname"
  value       = module.rds.hostname
}

output "db_port" {
  description = "RDS port"
  value       = module.rds.port
}

output "db_name" {
  description = "Database name"
  value       = module.rds.database_name
}

output "db_username" {
  description = "Database master username"
  value       = module.rds.master_username
}

output "jdbc_url" {
  description = "JDBC connection URL"
  value       = module.rds.jdbc_url
}
