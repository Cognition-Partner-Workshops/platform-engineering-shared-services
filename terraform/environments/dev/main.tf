terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 2.0"
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

# Kubernetes provider configured after EKS is created
provider "kubernetes" {
  host                   = module.eks.cluster_endpoint
  cluster_ca_certificate = base64decode(module.eks.cluster_certificate_authority_data)

  exec {
    api_version = "client.authentication.k8s.io/v1beta1"
    command     = "aws"
    args        = ["eks", "get-token", "--cluster-name", module.eks.cluster_name]
  }
}

locals {
  environment = "dev"
  tags = {
    Environment = local.environment
    ManagedBy   = "terraform"
    Project     = "workshop-platform"
  }

  # ECR repositories for the dotnet-angular-monolith decomposition demo
  ecr_repositories = [
    "workshop/web-frontend",
    "workshop/api-gateway",
    "workshop/order-service",
    "workshop/inventory-service",
    "workshop/customer-service",
    "workshop/product-service",
  ]

  # App namespaces for the decomposition demo
  app_namespaces = [
    {
      name        = "decomposition-dev"
      environment = "dev"
      team        = "dotnet-angular-monolith"
    },
    {
      name        = "decomposition-staging"
      environment = "staging"
      team        = "dotnet-angular-monolith"
    },
  ]
}

################################################################################
# Core Infrastructure
################################################################################

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

################################################################################
# Container Registry
################################################################################

module "ecr" {
  source = "../../modules/ecr"

  repository_names = local.ecr_repositories
  tags             = local.tags
}

################################################################################
# App Namespaces
################################################################################

module "namespaces" {
  source = "../../modules/namespaces"

  namespaces = local.app_namespaces

  depends_on = [module.eks]
}

################################################################################
# Variables & Outputs
################################################################################

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

output "ecr_repository_urls" {
  value = module.ecr.repository_urls
}

output "app_namespaces" {
  value = module.namespaces.namespace_names
}
