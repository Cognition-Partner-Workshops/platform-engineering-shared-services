################################################################################
# Cluster Connection
################################################################################

variable "kafka_cluster_id" {
  description = "Confluent Cloud Kafka cluster ID"
  type        = string
}

variable "kafka_rest_endpoint" {
  description = "REST endpoint for the Kafka cluster"
  type        = string
}

variable "kafka_api_key_id" {
  description = "API key ID for Kafka cluster authentication"
  type        = string
}

variable "kafka_api_key_secret" {
  description = "API key secret for Kafka cluster authentication"
  type        = string
  sensitive   = true
}

################################################################################
# Topic Definitions
################################################################################

variable "topics" {
  description = <<-EOT
    List of Kafka topics to create. Each topic object supports:
      - name              (required) Topic name
      - partitions        (optional) Number of partitions (default: var.default_partitions)
      - cleanup_policy    (optional) "delete", "compact", or "compact,delete"
      - retention_ms      (optional) Message retention in milliseconds
      - retention_bytes   (optional) Max bytes retained per partition (-1 = unlimited)
      - max_message_bytes (optional) Max message size in bytes
      - delete_retention_ms (optional) Time to retain delete markers for compacted topics
      - extra_config      (optional) Map of additional topic-level config overrides
      - acls              (optional) List of ACL objects: { service_account_id, permission }
                          permission is one of: "read", "write", "readwrite"
  EOT
  type = list(object({
    name                = string
    partitions          = optional(number)
    cleanup_policy      = optional(string)
    retention_ms        = optional(number)
    retention_bytes     = optional(number)
    max_message_bytes   = optional(number)
    delete_retention_ms = optional(number)
    extra_config        = optional(map(string))
    acls = optional(list(object({
      service_account_id = string
      permission         = string
    })), [])
  }))

  validation {
    condition = alltrue([
      for topic in var.topics : can(regex("^[a-zA-Z0-9._-]+$", topic.name))
    ])
    error_message = "Topic names must only contain alphanumeric characters, dots, underscores, or hyphens."
  }
}

################################################################################
# Default Topic Configuration
################################################################################

variable "default_partitions" {
  description = "Default number of partitions for topics"
  type        = number
  default     = 6
}

variable "default_cleanup_policy" {
  description = "Default cleanup policy: delete, compact, or compact,delete"
  type        = string
  default     = "delete"
}

variable "default_retention_ms" {
  description = "Default message retention period in milliseconds (7 days)"
  type        = number
  default     = 604800000
}

variable "default_retention_bytes" {
  description = "Default max bytes per partition (-1 = unlimited)"
  type        = number
  default     = -1
}

variable "default_max_message_bytes" {
  description = "Default maximum message size in bytes (1 MB)"
  type        = number
  default     = 1048576
}

variable "default_delete_retention_ms" {
  description = "Default delete retention for compacted topics in milliseconds (24 hours)"
  type        = number
  default     = 86400000
}
