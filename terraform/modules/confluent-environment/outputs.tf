output "environment_id" {
  description = "Confluent Cloud environment ID"
  value       = confluent_environment.this.id
}

output "cluster_id" {
  description = "Kafka cluster ID"
  value       = confluent_kafka_cluster.this.id
}

output "cluster_bootstrap_endpoint" {
  description = "Bootstrap endpoint for the Kafka cluster"
  value       = confluent_kafka_cluster.this.bootstrap_endpoint
}

output "cluster_rest_endpoint" {
  description = "REST endpoint for the Kafka cluster"
  value       = confluent_kafka_cluster.this.rest_endpoint
}

output "cluster_rbac_crn" {
  description = "RBAC CRN for the Kafka cluster"
  value       = confluent_kafka_cluster.this.rbac_crn
}

output "kafka_manager_service_account_id" {
  description = "Service account ID for the Kafka manager"
  value       = confluent_service_account.kafka_manager.id
}

output "kafka_manager_api_key_id" {
  description = "API key ID for the Kafka manager service account"
  value       = confluent_api_key.kafka_manager.id
}

output "kafka_manager_api_key_secret" {
  description = "API key secret for the Kafka manager service account"
  value       = confluent_api_key.kafka_manager.secret
  sensitive   = true
}
