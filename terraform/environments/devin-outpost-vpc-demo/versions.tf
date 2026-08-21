terraform {
  required_version = ">= 1.5"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.60"
    }
    external = {
      source  = "hashicorp/external"
      version = "~> 2.3"
    }
  }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project   = "devin-outposts"
      ManagedBy = "terraform"
      Component = "devin-outpost-vpc-demo"
    }
  }
}
