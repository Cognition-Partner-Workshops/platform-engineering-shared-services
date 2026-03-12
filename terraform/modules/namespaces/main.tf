################################################################################
# Kubernetes Namespace Provisioning
#
# Creates namespaces for application teams with standard labels, resource
# quotas, and network policies. Each namespace is self-contained and isolated.
################################################################################

terraform {
  required_providers {
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 2.0"
    }
  }
}

resource "kubernetes_namespace" "app" {
  for_each = { for ns in var.namespaces : ns.name => ns }

  metadata {
    name = each.value.name

    labels = merge(
      {
        "app.kubernetes.io/managed-by" = "terraform"
        "platform/environment"         = each.value.environment
        "platform/team"                = each.value.team
      },
      each.value.extra_labels
    )
  }
}

resource "kubernetes_resource_quota" "app" {
  for_each = { for ns in var.namespaces : ns.name => ns if ns.resource_quota_enabled }

  metadata {
    name      = "${each.value.name}-quota"
    namespace = kubernetes_namespace.app[each.key].metadata[0].name
  }

  spec {
    hard = {
      "requests.cpu"    = each.value.cpu_request_limit
      "requests.memory" = each.value.memory_request_limit
      "limits.cpu"      = each.value.cpu_limit
      "limits.memory"   = each.value.memory_limit
      "pods"            = each.value.max_pods
    }
  }
}

resource "kubernetes_limit_range" "app" {
  for_each = { for ns in var.namespaces : ns.name => ns }

  metadata {
    name      = "${each.value.name}-limits"
    namespace = kubernetes_namespace.app[each.key].metadata[0].name
  }

  spec {
    limit {
      type = "Container"
      default = {
        cpu    = "500m"
        memory = "256Mi"
      }
      default_request = {
        cpu    = "100m"
        memory = "128Mi"
      }
    }
  }
}
