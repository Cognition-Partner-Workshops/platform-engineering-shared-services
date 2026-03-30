################################################################################
# RDS PostgreSQL
################################################################################

resource "aws_db_subnet_group" "this" {
  name       = "rds-${var.environment}-subnet-group"
  subnet_ids = var.private_subnet_ids

  tags = merge(var.tags, {
    Name = "rds-${var.environment}-subnet-group"
  })
}

resource "aws_security_group" "rds" {
  name_prefix = "rds-${var.environment}-"
  description = "Security group for RDS PostgreSQL instance"
  vpc_id      = var.vpc_id

  ingress {
    description     = "PostgreSQL from EKS nodes"
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [var.eks_node_security_group_id]
  }

  egress {
    description = "Allow all outbound"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(var.tags, {
    Name = "rds-${var.environment}-sg"
  })
}

resource "aws_db_instance" "this" {
  identifier = "workshop-${var.environment}-postgres"

  engine         = "postgres"
  engine_version = var.engine_version
  instance_class = var.instance_class

  allocated_storage = var.allocated_storage
  storage_type      = "gp3"
  storage_encrypted = true

  db_name  = var.db_name
  username = var.db_username
  password = var.db_password

  multi_az = var.multi_az

  db_subnet_group_name   = aws_db_subnet_group.this.name
  vpc_security_group_ids = [aws_security_group.rds.id]

  backup_retention_period = var.backup_retention_period
  backup_window           = var.backup_window

  skip_final_snapshot       = var.skip_final_snapshot
  final_snapshot_identifier = "workshop-${var.environment}-postgres-final"

  tags = merge(var.tags, {
    Name = "workshop-${var.environment}-postgres"
  })
}
