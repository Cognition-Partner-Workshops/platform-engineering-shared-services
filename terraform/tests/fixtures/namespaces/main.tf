terraform {
  required_providers {
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 2.0"
    }
  }
}

provider "kubernetes" {
  host = "https://localhost"

  client_certificate     = ""
  client_key             = ""
  cluster_ca_certificate = ""
}

module "namespaces" {
  source = "../../../modules/namespaces"
  namespaces = [
    {
      name        = "test-ns"
      environment = "test"
      team        = "platform"
    }
  ]
}
