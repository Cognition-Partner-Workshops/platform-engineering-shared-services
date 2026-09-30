data "aws_caller_identity" "current" {}

data "aws_availability_zones" "available" {
  state = "available"
}

data "aws_ami" "al2023" {
  most_recent = true
  owners      = ["amazon"]

  filter {
    name   = "name"
    values = ["al2023-ami-2023.*-arm64"]
  }
}

locals {
  private_cidrs = [cidrsubnet(var.vpc_cidr, 8, 1), cidrsubnet(var.vpc_cidr, 8, 2)]
  public_cidr   = cidrsubnet(var.vpc_cidr, 8, 0)
  azs           = slice(data.aws_availability_zones.available.names, 0, 2)
}

# --- Network ---------------------------------------------------------------

resource "aws_vpc" "this" {
  cidr_block           = var.vpc_cidr
  enable_dns_hostnames = true
  enable_dns_support   = true

  tags = { Name = var.name }
}

resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id
  tags   = { Name = var.name }
}

resource "aws_subnet" "public" {
  vpc_id                  = aws_vpc.this.id
  cidr_block              = local.public_cidr
  availability_zone       = local.azs[0]
  map_public_ip_on_launch = true

  tags = { Name = "${var.name}-public" }
}

# Workers and the internal server both live here. Nothing in this subnet has a
# public address, so the only way in is from inside the VPC.
resource "aws_subnet" "private" {
  count             = length(local.private_cidrs)
  vpc_id            = aws_vpc.this.id
  cidr_block        = local.private_cidrs[count.index]
  availability_zone = local.azs[count.index]

  tags = { Name = "${var.name}-private-${count.index}" }
}

resource "aws_eip" "nat" {
  domain = "vpc"
  tags   = { Name = var.name }
}

# Workers still have to reach the Devin gateway, so the private subnets get
# outbound-only internet. Egress here is the customer's to filter; the point of
# the VPC connector is that it replaces AWS's open INTERNET_EGRESS with a path
# the account controls.
resource "aws_nat_gateway" "this" {
  allocation_id = aws_eip.nat.id
  subnet_id     = aws_subnet.public.id
  depends_on    = [aws_internet_gateway.this]

  tags = { Name = var.name }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.this.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.this.id
  }

  tags = { Name = "${var.name}-public" }
}

resource "aws_route_table_association" "public" {
  subnet_id      = aws_subnet.public.id
  route_table_id = aws_route_table.public.id
}

resource "aws_route_table" "private" {
  vpc_id = aws_vpc.this.id

  route {
    cidr_block     = "0.0.0.0/0"
    nat_gateway_id = aws_nat_gateway.this.id
  }

  tags = { Name = "${var.name}-private" }
}

resource "aws_route_table_association" "private" {
  count          = length(aws_subnet.private)
  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private.id
}

# --- Security groups: the segmentation boundary ----------------------------

# Attached to the trusted connector. Members of this group are the only clients
# the internal server accepts.
resource "aws_security_group" "trusted_worker" {
  name        = "${var.name}-trusted-worker"
  description = "Devin workers allowed to reach the internal server"
  vpc_id      = aws_vpc.this.id

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${var.name}-trusted-worker" }
}

# Attached to the restricted connector: same image, same session, no path to
# the internal server. Only outbound HTTPS, which is all a worker needs to talk
# to the Devin gateway.
resource "aws_security_group" "restricted_worker" {
  name        = "${var.name}-restricted-worker"
  description = "Devin workers restricted to outbound HTTPS"
  vpc_id      = aws_vpc.this.id

  egress {
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    from_port   = 53
    to_port     = 53
    protocol    = "udp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${var.name}-restricted-worker" }
}

resource "aws_security_group" "server" {
  name        = "${var.name}-server"
  description = "Internal demo service"
  vpc_id      = aws_vpc.this.id

  ingress {
    from_port       = var.server_port
    to_port         = var.server_port
    protocol        = "tcp"
    security_groups = [aws_security_group.trusted_worker.id]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${var.name}-server" }
}

# --- Internal server -------------------------------------------------------

resource "aws_instance" "server" {
  ami                    = data.aws_ami.al2023.id
  instance_type          = var.server_instance_type
  subnet_id              = aws_subnet.private[0].id
  vpc_security_group_ids = [aws_security_group.server.id]

  metadata_options {
    http_tokens = "required"
  }

  root_block_device {
    volume_size = 8
    volume_type = "gp3"
    encrypted   = true
  }

  user_data = <<-EOT
    #!/bin/bash
    set -euo pipefail
    mkdir -p /srv/internal
    cat > /srv/internal/index.html <<'PAGE'
    ${var.server_banner}
    PAGE
    cat > /etc/systemd/system/internal-service.service <<'UNIT'
    [Unit]
    Description=Internal demo service
    [Service]
    ExecStart=/usr/bin/python3 -m http.server ${var.server_port} --directory /srv/internal
    Restart=always
    [Install]
    WantedBy=multi-user.target
    UNIT
    systemctl daemon-reload
    systemctl enable --now internal-service
  EOT

  tags = { Name = "${var.name}-server" }
}

# --- Network connectors ----------------------------------------------------

data "aws_iam_policy_document" "connector_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

# Lambda assumes this to put an ENI for each MicroVM into the private subnets.
resource "aws_iam_role" "connector" {
  name               = "${var.name}-connector"
  assume_role_policy = data.aws_iam_policy_document.connector_assume.json
}

resource "aws_iam_role_policy_attachment" "connector" {
  role       = aws_iam_role.connector.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
}

resource "terraform_data" "connector" {
  for_each = {
    trusted    = aws_security_group.trusted_worker.id
    restricted = aws_security_group.restricted_worker.id
  }

  triggers_replace = {
    name        = "${var.name}-${each.key}"
    subnets     = join(",", aws_subnet.private[*].id)
    group       = each.value
    operator    = aws_iam_role.connector.arn
    region      = var.region
    script_hash = filemd5("${path.module}/scripts/network_connector.py")
  }

  input = {
    region     = var.region
    name       = "${var.name}-${each.key}"
    script     = "${path.module}/scripts/network_connector.py"
    python_bin = var.python_bin
  }

  provisioner "local-exec" {
    command = join(" ", [
      var.python_bin, "${path.module}/scripts/network_connector.py", "create",
      "--region", var.region,
      "--name", "${var.name}-${each.key}",
      "--subnet-ids", join(" ", aws_subnet.private[*].id),
      "--security-group-ids", each.value,
      "--operator-role-arn", aws_iam_role.connector.arn,
    ])
  }

  provisioner "local-exec" {
    when = destroy
    command = join(" ", [
      self.input.python_bin, self.input.script, "delete",
      "--region", self.input.region,
      "--name", self.input.name,
    ])
  }

  depends_on = [aws_iam_role_policy_attachment.connector, aws_nat_gateway.this]
}

# CreateNetworkConnector returns the ARN to the provisioner's stdout, which
# Terraform cannot capture, so it is read back by name.
data "external" "connector" {
  for_each = terraform_data.connector

  program = [
    var.python_bin, "${path.module}/scripts/network_connector.py", "lookup",
    "--region", var.region,
    "--name", each.value.input.name,
  ]

  depends_on = [terraform_data.connector]
}
