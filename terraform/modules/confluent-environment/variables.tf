variable "environment_name" {
  description = "Display name for the Confluent Cloud environment"
  type        = string
}

variable "cluster_name" {
  description = "Display name for the Kafka cluster"
  type        = string
}

variable "cloud_provider" {
  description = "Cloud provider for the Kafka cluster (AWS, GCP, AZURE)"
  type        = string
  default     = "AWS"

  validation {
    condition     = contains(["AWS", "GCP", "AZURE"], var.cloud_provider)
    error_message = "cloud_provider must be one of: AWS, GCP, AZURE."
  }
}

variable "cloud_region" {
  description = "Cloud region for the Kafka cluster (e.g. us-east-1)"
  type        = string
  default     = "us-east-1"
}

variable "availability" {
  description = "Availability type for the Kafka cluster (SINGLE_ZONE or MULTI_ZONE)"
  type        = string
  default     = "SINGLE_ZONE"

  validation {
    condition     = contains(["SINGLE_ZONE", "MULTI_ZONE"], var.availability)
    error_message = "availability must be SINGLE_ZONE or MULTI_ZONE."
  }
}

variable "cluster_type" {
  description = "Kafka cluster type: basic, standard, or dedicated"
  type        = string
  default     = "basic"

  validation {
    condition     = contains(["basic", "standard", "dedicated"], var.cluster_type)
    error_message = "cluster_type must be one of: basic, standard, dedicated."
  }
}

variable "dedicated_cku" {
  description = "Number of CKUs for dedicated clusters (ignored for basic/standard)"
  type        = number
  default     = 1
}

variable "stream_governance_package" {
  description = "Stream Governance package: ESSENTIALS or ADVANCED"
  type        = string
  default     = "ESSENTIALS"

  validation {
    condition     = contains(["ESSENTIALS", "ADVANCED"], var.stream_governance_package)
    error_message = "stream_governance_package must be ESSENTIALS or ADVANCED."
  }
}
