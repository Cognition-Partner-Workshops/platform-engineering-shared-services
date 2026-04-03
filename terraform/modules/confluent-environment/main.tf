terraform {
  required_providers {
    confluent = {
      source  = "confluentinc/confluent"
      version = ">= 2.0"
    }
  }
}

################################################################################
# Confluent Cloud Environment & Kafka Cluster
################################################################################

resource "confluent_environment" "this" {
  display_name = var.environment_name

  stream_governance {
    package = var.stream_governance_package
  }
}

resource "confluent_kafka_cluster" "this" {
  display_name = var.cluster_name
  availability = var.availability
  cloud        = var.cloud_provider
  region       = var.cloud_region

  dynamic "basic" {
    for_each = var.cluster_type == "basic" ? [1] : []
    content {}
  }

  dynamic "standard" {
    for_each = var.cluster_type == "standard" ? [1] : []
    content {}
  }

  dynamic "dedicated" {
    for_each = var.cluster_type == "dedicated" ? [1] : []
    content {
      cku = var.dedicated_cku
    }
  }

  environment {
    id = confluent_environment.this.id
  }
}

################################################################################
# Service Account & API Key for Kafka Management
################################################################################

resource "confluent_service_account" "kafka_manager" {
  display_name = "${var.cluster_name}-manager"
  description  = "Service account for managing Kafka resources in ${var.cluster_name}"
}

resource "confluent_role_binding" "kafka_manager_admin" {
  principal   = "User:${confluent_service_account.kafka_manager.id}"
  role_name   = "CloudClusterAdmin"
  crn_pattern = confluent_kafka_cluster.this.rbac_crn
}

resource "confluent_api_key" "kafka_manager" {
  display_name = "${var.cluster_name}-manager-api-key"
  description  = "API key for the ${var.cluster_name} Kafka manager service account"

  owner {
    id          = confluent_service_account.kafka_manager.id
    api_version = confluent_service_account.kafka_manager.api_version
    kind        = confluent_service_account.kafka_manager.kind
  }

  managed_resource {
    id          = confluent_kafka_cluster.this.id
    api_version = confluent_kafka_cluster.this.api_version
    kind        = confluent_kafka_cluster.this.kind

    environment {
      id = confluent_environment.this.id
    }
  }

  depends_on = [confluent_role_binding.kafka_manager_admin]
}
