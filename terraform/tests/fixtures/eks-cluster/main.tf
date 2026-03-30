terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region                      = "us-east-1"
  skip_credentials_validation = true
  skip_metadata_api_check     = true
  skip_requesting_account_id  = true

  access_key = "mock-access-key"
  secret_key = "mock-secret-key"
}

module "eks_cluster" {
  source             = "../../../modules/eks-cluster"
  cluster_name       = "test-cluster"
  cluster_version    = "1.31"
  vpc_id             = "vpc-12345"
  private_subnet_ids = ["subnet-111", "subnet-222"]
  environment        = "test"
  tags               = { Environment = "test" }
}
