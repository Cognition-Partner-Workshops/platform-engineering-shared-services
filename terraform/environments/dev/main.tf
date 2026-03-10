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
    key            = "platform/dev/terraform.tfstate"
    region         = "us-east-1"
    dynamodb_table = "workshop-terraform-lock"
    encrypt        = true
  }
}

provider "aws" {
  region = var.region
}

locals {
  environment = "dev"
  tags = {
    Environment = local.environment
    ManagedBy   = "terraform"
    Project     = "workshop-platform"
  }
}

module "networking" {
  source = "../../modules/networking"

  vpc_name           = "workshop-${local.environment}"
  vpc_cidr           = "10.0.0.0/16"
  availability_zones = ["${var.region}a", "${var.region}b"]
  single_nat_gateway = true
  tags               = local.tags
}

module "eks" {
  source = "../../modules/eks-cluster"

  cluster_name        = "workshop-${local.environment}"
  cluster_version     = "1.31"
  vpc_id              = module.networking.vpc_id
  private_subnet_ids  = module.networking.private_subnet_ids
  node_instance_types = ["t3.medium"]
  node_min_size       = 1
  node_max_size       = 3
  node_desired_size   = 2
  environment         = local.environment
  tags                = local.tags
}

variable "region" {
  description = "AWS region"
  type        = string
  default     = "us-east-1"
}

output "cluster_endpoint" {
  value = module.eks.cluster_endpoint
}

output "cluster_name" {
  value = module.eks.cluster_name
}
