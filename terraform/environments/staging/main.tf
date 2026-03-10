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
    key            = "platform/staging/terraform.tfstate"
    region         = "us-east-1"
    dynamodb_table = "workshop-terraform-lock"
    encrypt        = true
  }
}

provider "aws" {
  region = var.region
}

locals {
  environment = "staging"
  tags = {
    Environment = local.environment
    ManagedBy   = "terraform"
    Project     = "workshop-platform"
  }
}

module "networking" {
  source = "../../modules/networking"

  vpc_name           = "workshop-${local.environment}"
  vpc_cidr           = "10.1.0.0/16"
  availability_zones = ["${var.region}a", "${var.region}b", "${var.region}c"]
  single_nat_gateway = false
  tags               = local.tags
}

module "eks" {
  source = "../../modules/eks-cluster"

  cluster_name        = "workshop-${local.environment}"
  cluster_version     = "1.31"
  vpc_id              = module.networking.vpc_id
  private_subnet_ids  = module.networking.private_subnet_ids
  node_instance_types = ["t3.large"]
  node_min_size       = 2
  node_max_size       = 5
  node_desired_size   = 3
  environment         = local.environment
  tags                = local.tags
}

variable "region" {
  description = "AWS region"
  type        = string
  default     = "us-east-1"
}
