variable "namespaces" {
  description = "List of namespaces to create with their configuration"
  type = list(object({
    name                  = string
    environment           = string
    team                  = string
    extra_labels          = optional(map(string), {})
    resource_quota_enabled = optional(bool, true)
    cpu_request_limit     = optional(string, "2")
    memory_request_limit  = optional(string, "4Gi")
    cpu_limit             = optional(string, "4")
    memory_limit          = optional(string, "8Gi")
    max_pods              = optional(string, "20")
  }))
}
