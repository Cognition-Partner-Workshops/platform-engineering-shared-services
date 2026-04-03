# Confluent Cloud Kafka Topic Management

Terraform modules for provisioning and managing Confluent Cloud environments, Kafka clusters, and topics as code.

## Architecture

```
terraform/modules/
├── confluent-environment/     # Confluent Cloud environment + Kafka cluster + service account
│   ├── main.tf
│   ├── variables.tf
│   └── outputs.tf
└── confluent-kafka-topics/    # Kafka topics + optional per-topic ACLs
    ├── main.tf
    ├── variables.tf
    └── outputs.tf
```

### Module Responsibilities

| Module | Creates |
|--------|---------|
| `confluent-environment` | Confluent environment, Kafka cluster, manager service account, API key |
| `confluent-kafka-topics` | Kafka topics with configurable partitions, retention, cleanup policies, and ACLs |

## Prerequisites

1. **Confluent Cloud account** with an active subscription
2. **Cloud API key** with `OrganizationAdmin` or `EnvironmentAdmin` role
3. Set the following environment variables before running Terraform:

```bash
export CONFLUENT_CLOUD_API_KEY="<your-cloud-api-key>"
export CONFLUENT_CLOUD_API_SECRET="<your-cloud-api-secret>"
```

## Usage

### Adding a New Topic

Edit the `confluent_topics` local in your environment file (e.g., `terraform/environments/dev/confluent.tf`):

```hcl
locals {
  confluent_topics = [
    # ... existing topics ...
    {
      name           = "payments.processed"
      partitions     = 6
      cleanup_policy = "delete"
      retention_ms   = 604800000  # 7 days
    },
  ]
}
```

Then apply:

```bash
cd terraform/environments/dev
terraform plan -target=module.confluent_kafka_topics
terraform apply -target=module.confluent_kafka_topics
```

### Topic Configuration Options

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `name` | string | *required* | Topic name (alphanumeric, dots, hyphens, underscores) |
| `partitions` | number | 6 | Number of partitions |
| `cleanup_policy` | string | `"delete"` | `"delete"`, `"compact"`, or `"compact,delete"` |
| `retention_ms` | number | 604800000 (7d) | Message retention in milliseconds (-1 = unlimited) |
| `retention_bytes` | number | -1 | Max bytes per partition (-1 = unlimited) |
| `max_message_bytes` | number | 1048576 (1MB) | Maximum message size |
| `delete_retention_ms` | number | 86400000 (24h) | Delete marker retention for compacted topics |
| `extra_config` | map(string) | `{}` | Additional topic-level config overrides |
| `acls` | list(object) | `[]` | Per-topic ACL rules (see below) |

### Adding ACLs for Service Accounts

To grant a service account access to a topic, add an `acls` block:

```hcl
{
  name       = "orders.created"
  partitions = 6
  acls = [
    {
      service_account_id = "sa-abc123"
      permission         = "readwrite"  # "read", "write", or "readwrite"
    },
  ]
}
```

### Compacted Topics (Changelog / State Store)

For topics that should retain the latest value per key indefinitely:

```hcl
{
  name             = "customers.profile-updated"
  partitions       = 3
  cleanup_policy   = "compact"
  retention_ms     = -1
}
```

## Cluster Types

The `confluent-environment` module supports three cluster types:

| Type | Use Case | Config |
|------|----------|--------|
| `basic` | Development, low throughput | `cluster_type = "basic"` |
| `standard` | Production workloads | `cluster_type = "standard"` |
| `dedicated` | High throughput, SLA-backed | `cluster_type = "dedicated"`, set `dedicated_cku` |

## Outputs

After applying, the following outputs are available:

| Output | Description |
|--------|-------------|
| `confluent_environment_id` | Confluent Cloud environment ID |
| `confluent_cluster_id` | Kafka cluster ID |
| `confluent_cluster_bootstrap_endpoint` | Bootstrap endpoint for Kafka clients |
| `confluent_topic_names` | List of all created topic names |

## Extending to Other Environments

To add Confluent Cloud to staging or prod:

1. Copy `terraform/environments/dev/confluent.tf` to the target environment
2. Adjust `cluster_type`, `availability`, and topic definitions as needed
3. For production, consider:
   - `cluster_type = "dedicated"` with appropriate CKU count
   - `availability = "MULTI_ZONE"` for high availability
   - Higher partition counts for throughput-heavy topics
   - Longer retention periods or compacted topics for critical data
