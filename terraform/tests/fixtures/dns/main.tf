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

module "dns" {
  source      = "../../../modules/dns"
  domain_name = "example.com"
  tags        = { Environment = "test" }
}
