variable "region" {
  description = "AWS region to provision the Mac worker in."
  type        = string
  default     = "us-east-2"
}

variable "availability_zone" {
  description = "AZ that offers the chosen Mac instance type. mac2-m2.metal is available in us-east-2b/c."
  type        = string
  default     = "us-east-2b"
}

variable "instance_type" {
  description = "Mac EC2 instance type. mac2-m2.metal (Apple M2, 8 vCPU / 24 GiB) is the smallest Apple-silicon Mac with capacity in us-east-2; mac2.metal (M1) is cheaper but was capacity-constrained at provisioning time."
  type        = string
  default     = "mac2-m2.metal"
}

variable "macos_ami_prefix" {
  description = "Name prefix for the Amazon-owned macOS AMI. Filter also pins arm64_mac for Apple-silicon (mac2*) hosts."
  type        = string
  # 15.x = macOS Sequoia. Use "amzn-ec2-macos-14" for Sonoma, etc.
  default = "amzn-ec2-macos-15"
}

variable "root_volume_size_gb" {
  description = "Size of the root EBS volume in GiB."
  type        = number
  default     = 200
}

variable "ssh_ingress_cidrs" {
  description = "CIDR blocks allowed to SSH (port 22) to the worker. Lock this down to your IP(s)."
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

variable "name" {
  description = "Base name applied to all resources."
  type        = string
  default     = "devin-outpost-mac"
}

variable "project" {
  description = "Project tag value."
  type        = string
  default     = "devin-outposts"
}

variable "private_key_path" {
  description = "Local path where the generated SSH private key (PEM) is written."
  type        = string
  default     = "devin-outpost-mac.pem"
}
