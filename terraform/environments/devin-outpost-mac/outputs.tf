output "host_id" {
  description = "Dedicated Host ID backing the Mac instance."
  value       = aws_ec2_host.mac.id
}

output "instance_id" {
  description = "EC2 instance ID of the Mac worker."
  value       = aws_instance.worker.id
}

output "public_ip" {
  description = "Public IP of the Mac worker."
  value       = aws_instance.worker.public_ip
}

output "public_dns" {
  description = "Public DNS of the Mac worker."
  value       = aws_instance.worker.public_dns
}

output "ami_id" {
  description = "macOS AMI the worker booted from."
  value       = data.aws_ami.macos.id
}

output "ami_name" {
  description = "macOS AMI name the worker booted from."
  value       = data.aws_ami.macos.name
}

output "ssh_command" {
  description = "Convenience SSH command (key is written to private_key_path)."
  value       = "ssh -i ${var.private_key_path} ec2-user@${aws_instance.worker.public_ip}"
}
