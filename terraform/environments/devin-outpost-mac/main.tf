########################################
# Lookups
########################################

# Latest Amazon-owned macOS AMI for Apple-silicon (arm64_mac) hosts.
data "aws_ami" "macos" {
  owners      = ["amazon"]
  most_recent = true

  filter {
    name   = "name"
    values = ["${var.macos_ami_prefix}*-arm64"]
  }

  filter {
    name   = "architecture"
    values = ["arm64_mac"]
  }

  filter {
    name   = "state"
    values = ["available"]
  }
}

# Use the account's default VPC and its default subnet in the target AZ.
data "aws_vpc" "default" {
  default = true
}

data "aws_subnet" "default" {
  vpc_id            = data.aws_vpc.default.id
  availability_zone = var.availability_zone
  default_for_az    = true
}

########################################
# SSH key pair (generated locally)
########################################

resource "tls_private_key" "worker" {
  algorithm = "RSA"
  rsa_bits  = 4096
}

resource "aws_key_pair" "worker" {
  key_name   = var.name
  public_key = tls_private_key.worker.public_key_openssh
}

resource "local_sensitive_file" "private_key" {
  content         = tls_private_key.worker.private_key_pem
  filename        = var.private_key_path
  file_permission = "0600"
}

########################################
# Security group: SSH in, everything out
########################################

resource "aws_security_group" "worker" {
  name        = "${var.name}-sg"
  description = "Devin Outpost Mac worker: SSH inbound, all outbound (worker only needs outbound)."
  vpc_id      = data.aws_vpc.default.id

  ingress {
    description = "SSH for provisioning/administration"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = var.ssh_ingress_cidrs
  }

  egress {
    description = "All outbound (worker opens an outbound-only connection to Devin Cloud)"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "${var.name}-sg"
  }
}

########################################
# Dedicated Host (required for Mac instances)
########################################

resource "aws_ec2_host" "mac" {
  instance_type     = var.instance_type
  availability_zone = var.availability_zone
  auto_placement    = "on"
  host_recovery     = "off"

  tags = {
    Name = "${var.name}-host"
  }
}

########################################
# Mac EC2 instance (the worker)
########################################

resource "aws_instance" "worker" {
  ami           = data.aws_ami.macos.id
  instance_type = var.instance_type

  # Mac instances must run on a Dedicated Host.
  tenancy   = "host"
  host_id   = aws_ec2_host.mac.id
  subnet_id = data.aws_subnet.default.id

  key_name                    = aws_key_pair.worker.key_name
  vpc_security_group_ids      = [aws_security_group.worker.id]
  associate_public_ip_address = true

  root_block_device {
    volume_size = var.root_volume_size_gb
    volume_type = "gp3"
    encrypted   = true
  }

  tags = {
    Name = var.name
  }
}
