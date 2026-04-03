################################################################################
# Confluent Cloud Provider
################################################################################

terraform {
  required_providers {
    confluent = {
      source  = "confluentinc/confluent"
      version = "~> 2.0"
    }
  }
}

# The Confluent provider authenticates via environment variables:
#   CONFLUENT_CLOUD_API_KEY
#   CONFLUENT_CLOUD_API_SECRET
provider "confluent" {}

################################################################################
# Confluent Cloud Environment & Kafka Cluster
################################################################################

module "confluent_environment" {
  source = "../../modules/confluent-environment"

  environment_name = "workshop-${local.environment}"
  cluster_name     = "workshop-${local.environment}"
  cloud_provider   = "AWS"
  cloud_region     = var.region
  cluster_type     = "basic"
  availability     = "SINGLE_ZONE"
}

################################################################################
# Kafka Topics
################################################################################

module "confluent_kafka_topics" {
  source = "../../modules/confluent-kafka-topics"

  kafka_cluster_id     = module.confluent_environment.cluster_id
  kafka_rest_endpoint  = module.confluent_environment.cluster_rest_endpoint
  kafka_api_key_id     = module.confluent_environment.kafka_manager_api_key_id
  kafka_api_key_secret = module.confluent_environment.kafka_manager_api_key_secret

  # Default topic settings for this environment
  default_partitions   = 6
  default_retention_ms = 604800000 # 7 days

  topics = local.confluent_topics
}

################################################################################
# Topic Definitions
#
# Add new topics to this list — they will be created on the next terraform apply.
# See modules/confluent-kafka-topics/variables.tf for all available options.
################################################################################

locals {
  confluent_topics = [
    {
      name           = "orders.created"
      partitions     = 6
      cleanup_policy = "delete"
      retention_ms   = 604800000 # 7 days
    },
    {
      name           = "orders.updated"
      partitions     = 6
      cleanup_policy = "delete"
      retention_ms   = 604800000
    },
    {
      name           = "orders.cancelled"
      partitions     = 3
      cleanup_policy = "delete"
      retention_ms   = 604800000
    },
    {
      name           = "inventory.stock-updated"
      partitions     = 6
      cleanup_policy = "delete"
      retention_ms   = 604800000
    },
    {
      name           = "customers.profile-updated"
      partitions     = 3
      cleanup_policy = "compact"
      retention_ms   = -1 # retain indefinitely (compacted)
    },
    {
      name           = "products.catalog-updated"
      partitions     = 3
      cleanup_policy = "compact"
      retention_ms   = -1
    },
  ]
}

################################################################################
# Confluent Outputs
################################################################################

output "confluent_environment_id" {
  description = "Confluent Cloud environment ID"
  value       = module.confluent_environment.environment_id
}

output "confluent_cluster_id" {
  description = "Confluent Kafka cluster ID"
  value       = module.confluent_environment.cluster_id
}

output "confluent_cluster_bootstrap_endpoint" {
  description = "Kafka cluster bootstrap endpoint"
  value       = module.confluent_environment.cluster_bootstrap_endpoint
}

output "confluent_topic_names" {
  description = "List of created Kafka topic names"
  value       = module.confluent_kafka_topics.topic_names
}
