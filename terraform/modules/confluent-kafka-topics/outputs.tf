output "topic_ids" {
  description = "Map of topic name to Confluent topic resource ID"
  value       = { for name, topic in confluent_kafka_topic.topics : name => topic.id }
}

output "topic_names" {
  description = "List of all created topic names"
  value       = [for name, topic in confluent_kafka_topic.topics : topic.topic_name]
}

output "topic_configs" {
  description = "Map of topic name to its effective configuration"
  value = {
    for name, topic in confluent_kafka_topic.topics : name => {
      partitions = topic.partitions_count
      config     = topic.config
    }
  }
}
