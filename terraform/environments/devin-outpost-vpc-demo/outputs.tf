output "trusted_connector_arn" {
  description = "Egress connector whose security group may reach the internal server."
  value       = data.external.connector["trusted"].result.arn
}

output "restricted_connector_arn" {
  description = "Egress connector limited to outbound HTTPS, with no path to the internal server."
  value       = data.external.connector["restricted"].result.arn
}

output "server_private_ip" {
  description = "Address of the internal service, reachable only from inside the VPC."
  value       = aws_instance.server.private_ip
}

output "server_url" {
  description = "URL a worker on the trusted connector can fetch."
  value       = "http://${aws_instance.server.private_ip}:${var.server_port}/"
}
