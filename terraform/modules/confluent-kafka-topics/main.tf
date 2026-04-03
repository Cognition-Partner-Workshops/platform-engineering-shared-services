terraform {
  required_providers {
    confluent = {
      source  = "confluentinc/confluent"
      version = ">= 2.0"
    }
  }
}

################################################################################
# Confluent Cloud Kafka Topics
################################################################################

resource "confluent_kafka_topic" "topics" {
  for_each = { for topic in var.topics : topic.name => topic }

  kafka_cluster {
    id = var.kafka_cluster_id
  }

  topic_name       = each.value.name
  partitions_count = try(each.value.partitions, var.default_partitions)
  rest_endpoint    = var.kafka_rest_endpoint

  config = merge(
    {
      "cleanup.policy"      = try(each.value.cleanup_policy, var.default_cleanup_policy)
      "retention.ms"        = tostring(try(each.value.retention_ms, var.default_retention_ms))
      "retention.bytes"     = tostring(try(each.value.retention_bytes, var.default_retention_bytes))
      "max.message.bytes"   = tostring(try(each.value.max_message_bytes, var.default_max_message_bytes))
      "delete.retention.ms" = tostring(try(each.value.delete_retention_ms, var.default_delete_retention_ms))
    },
    try(each.value.extra_config, {})
  )

  credentials {
    key    = var.kafka_api_key_id
    secret = var.kafka_api_key_secret
  }

  lifecycle {
    prevent_destroy = false
  }
}

################################################################################
# Optional: Per-Topic ACLs (Service Account access)
################################################################################

resource "confluent_kafka_acl" "topic_read" {
  for_each = {
    for entry in local.topic_acl_entries : "${entry.topic_name}-${entry.service_account_id}-read" => entry
    if entry.permission == "read" || entry.permission == "readwrite"
  }

  kafka_cluster {
    id = var.kafka_cluster_id
  }

  resource_type = "TOPIC"
  resource_name = each.value.topic_name
  pattern_type  = "LITERAL"
  principal     = "User:${each.value.service_account_id}"
  host          = "*"
  operation     = "READ"
  permission    = "ALLOW"
  rest_endpoint = var.kafka_rest_endpoint

  credentials {
    key    = var.kafka_api_key_id
    secret = var.kafka_api_key_secret
  }

  depends_on = [confluent_kafka_topic.topics]
}

resource "confluent_kafka_acl" "topic_write" {
  for_each = {
    for entry in local.topic_acl_entries : "${entry.topic_name}-${entry.service_account_id}-write" => entry
    if entry.permission == "write" || entry.permission == "readwrite"
  }

  kafka_cluster {
    id = var.kafka_cluster_id
  }

  resource_type = "TOPIC"
  resource_name = each.value.topic_name
  pattern_type  = "LITERAL"
  principal     = "User:${each.value.service_account_id}"
  host          = "*"
  operation     = "WRITE"
  permission    = "ALLOW"
  rest_endpoint = var.kafka_rest_endpoint

  credentials {
    key    = var.kafka_api_key_id
    secret = var.kafka_api_key_secret
  }

  depends_on = [confluent_kafka_topic.topics]
}

resource "confluent_kafka_acl" "consumer_group_read" {
  for_each = {
    for entry in local.topic_acl_entries : "${entry.topic_name}-${entry.service_account_id}-group" => entry
    if entry.permission == "read" || entry.permission == "readwrite"
  }

  kafka_cluster {
    id = var.kafka_cluster_id
  }

  resource_type = "GROUP"
  resource_name = "*"
  pattern_type  = "LITERAL"
  principal     = "User:${each.value.service_account_id}"
  host          = "*"
  operation     = "READ"
  permission    = "ALLOW"
  rest_endpoint = var.kafka_rest_endpoint

  credentials {
    key    = var.kafka_api_key_id
    secret = var.kafka_api_key_secret
  }

  depends_on = [confluent_kafka_topic.topics]
}

################################################################################
# Locals
################################################################################

locals {
  # Flatten topic → ACL entries for iteration
  topic_acl_entries = flatten([
    for topic in var.topics : [
      for acl in try(topic.acls, []) : {
        topic_name         = topic.name
        service_account_id = acl.service_account_id
        permission         = acl.permission
      }
    ]
  ])
}
