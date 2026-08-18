data "aws_caller_identity" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id

  # Derived rather than read back from the build so the reconciler's environment
  # does not depend on a data source that only resolves after the image exists.
  microvm_image_arn = "arn:aws:lambda:${var.region}:${local.account_id}:microvm-image:${var.name}"
  base_image_arn    = "arn:aws:lambda:${var.region}:aws:microvm-image:al2023-1"
  connector_prefix  = "arn:aws:lambda:${var.region}:aws:network-connector:aws-network-connector"

  ingress_connector = "${local.connector_prefix}:${var.enable_ingress ? "ALL_INGRESS" : "NO_INGRESS"}"
  egress_connector  = "${local.connector_prefix}:INTERNET_EGRESS"

  worker_hook_port = 8080
  worker_source    = "${path.module}/worker"
  worker_artifact  = "${path.module}/.build/worker.zip"
}

resource "random_id" "suffix" {
  byte_length = 4
}

# --- Worker artifact -------------------------------------------------------

resource "aws_s3_bucket" "artifacts" {
  bucket        = "${var.name}-${random_id.suffix.hex}"
  force_destroy = true
}

resource "aws_s3_bucket_public_access_block" "artifacts" {
  bucket                  = aws_s3_bucket.artifacts.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

data "archive_file" "worker" {
  type        = "zip"
  source_dir  = local.worker_source
  output_path = local.worker_artifact
  excludes    = ["__pycache__"]
}

resource "aws_s3_object" "worker" {
  bucket = aws_s3_bucket.artifacts.id
  key    = "worker/${data.archive_file.worker.output_md5}.zip"
  source = data.archive_file.worker.output_path
  etag   = data.archive_file.worker.output_md5
}

# --- MicroVM image ---------------------------------------------------------

resource "aws_cloudwatch_log_group" "image_build" {
  name              = "/aws/lambda/microvms/${var.name}"
  retention_in_days = var.log_retention_days
}

resource "aws_cloudwatch_log_group" "workers" {
  name              = "/devin/outpost/${var.name}/workers"
  retention_in_days = var.log_retention_days
}

data "aws_iam_policy_document" "lambda_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole", "sts:TagSession"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "image_build" {
  name               = "${var.name}-build"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

data "aws_iam_policy_document" "image_build" {
  statement {
    effect    = "Allow"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.artifacts.arn}/*"]
  }

  statement {
    effect    = "Allow"
    actions   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["arn:aws:logs:${var.region}:${local.account_id}:log-group:/aws/lambda/microvms/*"]
  }
}

resource "aws_iam_role_policy" "image_build" {
  role   = aws_iam_role.image_build.id
  policy = data.aws_iam_policy_document.image_build.json
}

# Workers run in direct-serve mode, so they need no Devin or AWS API access.
# The only grant is shipping their own output: a MicroVM writes its logs itself,
# and without this the configured log group silently stays empty.
resource "aws_iam_role" "worker" {
  name               = "${var.name}-worker"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

data "aws_iam_policy_document" "worker" {
  statement {
    effect    = "Allow"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.workers.arn}:*"]
  }
}

resource "aws_iam_role_policy" "worker" {
  role   = aws_iam_role.worker.id
  policy = data.aws_iam_policy_document.worker.json
}

resource "terraform_data" "microvm_image" {
  # Rebuilds on a worker source change; the script turns that into a new image
  # version rather than a replacement.
  triggers_replace = {
    artifact = aws_s3_object.worker.key
    memory   = var.microvm_memory_mib
    port     = local.worker_hook_port
  }

  input = {
    region     = var.region
    name       = var.name
    image_arn  = local.microvm_image_arn
    script     = "${path.module}/scripts/microvm_image.py"
    python_bin = var.python_bin
  }

  provisioner "local-exec" {
    command = join(" ", [
      var.python_bin, "${path.module}/scripts/microvm_image.py", "create",
      "--region", var.region,
      "--name", var.name,
      "--image-arn", local.microvm_image_arn,
      "--artifact-uri", "s3://${aws_s3_bucket.artifacts.id}/${aws_s3_object.worker.key}",
      "--base-image-arn", local.base_image_arn,
      "--build-role-arn", aws_iam_role.image_build.arn,
      "--log-group", aws_cloudwatch_log_group.image_build.name,
      "--memory-mib", tostring(var.microvm_memory_mib),
      "--hook-port", tostring(local.worker_hook_port),
    ])
  }

  provisioner "local-exec" {
    when = destroy
    command = join(" ", [
      self.input.python_bin, self.input.script, "delete",
      "--region", self.input.region,
      "--name", self.input.name,
      "--image-arn", self.input.image_arn,
    ])
  }

  depends_on = [aws_iam_role_policy.image_build]
}

# --- Reconciler ------------------------------------------------------------

resource "aws_secretsmanager_secret" "outpost_token" {
  name                    = "${var.name}-token-${random_id.suffix.hex}"
  description             = "Devin Outposts service account token used by the ${var.name} reconciler."
  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret_version" "outpost_token" {
  secret_id     = aws_secretsmanager_secret.outpost_token.id
  secret_string = var.outpost_token
}

resource "aws_dynamodb_table" "sessions" {
  name         = "${var.name}-sessions"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "session_id"

  attribute {
    name = "session_id"
    type = "S"
  }

  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }
}

# The Lambda runtime's bundled boto3 lags the lambda-microvms model, so the
# package carries its own copy.
resource "terraform_data" "reconciler_deps" {
  triggers_replace = {
    handler = filesha256("${path.module}/reconciler/handler.py")
  }

  provisioner "local-exec" {
    command = <<-EOT
      set -eu
      rm -rf "${path.module}/.build/reconciler"
      mkdir -p "${path.module}/.build/reconciler"
      cp "${path.module}/reconciler/handler.py" "${path.module}/.build/reconciler/"
      ${var.python_bin} -m pip install --quiet --upgrade --target "${path.module}/.build/reconciler" 'boto3>=1.41' 'botocore>=1.43'
    EOT
  }
}

data "archive_file" "reconciler" {
  type        = "zip"
  source_dir  = "${path.module}/.build/reconciler"
  output_path = "${path.module}/.build/reconciler.zip"
  depends_on  = [terraform_data.reconciler_deps]
}

resource "aws_iam_role" "reconciler" {
  name               = "${var.name}-reconciler"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

data "aws_iam_policy_document" "reconciler" {
  statement {
    effect    = "Allow"
    actions   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["arn:aws:logs:${var.region}:${local.account_id}:*"]
  }

  statement {
    effect    = "Allow"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [aws_secretsmanager_secret.outpost_token.arn]
  }

  statement {
    effect = "Allow"
    actions = [
      "dynamodb:DeleteItem",
      "dynamodb:GetItem",
      "dynamodb:PutItem",
      "dynamodb:Scan",
    ]
    resources = [aws_dynamodb_table.sessions.arn]
  }

  statement {
    effect = "Allow"
    actions = [
      "lambda:GetMicrovm",
      "lambda:ListMicrovms",
      "lambda:RunMicrovm",
      "lambda:TerminateMicrovm",
    ]
    resources = ["*"]
  }

  # run-microvm hands the worker role to the MicroVM, which requires the
  # reconciler to be allowed to pass it.
  statement {
    effect    = "Allow"
    actions   = ["iam:PassRole"]
    resources = [aws_iam_role.worker.arn]

    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role_policy" "reconciler" {
  role   = aws_iam_role.reconciler.id
  policy = data.aws_iam_policy_document.reconciler.json
}

resource "aws_cloudwatch_log_group" "reconciler" {
  name              = "/aws/lambda/${var.name}-reconciler"
  retention_in_days = var.log_retention_days
}

resource "aws_lambda_function" "reconciler" {
  function_name    = "${var.name}-reconciler"
  role             = aws_iam_role.reconciler.arn
  handler          = "handler.handler"
  runtime          = "python3.12"
  filename         = data.archive_file.reconciler.output_path
  source_code_hash = data.archive_file.reconciler.output_base64sha256
  timeout          = 120
  memory_size      = 512

  environment {
    variables = {
      OUTPOST_ID                   = var.outpost_id
      OUTPOST_TOKEN_SECRET_ARN     = aws_secretsmanager_secret.outpost_token.arn
      DEVIN_API_URL                = var.devin_api_url
      MICROVM_IMAGE_ARN            = local.microvm_image_arn
      MICROVM_EXECUTION_ROLE_ARN   = aws_iam_role.worker.arn
      MICROVM_LOG_GROUP            = aws_cloudwatch_log_group.workers.name
      MICROVM_MAX_DURATION_SECONDS = tostring(var.microvm_max_duration_seconds)
      STATE_TABLE                  = aws_dynamodb_table.sessions.name
      MAX_CONCURRENT_SESSIONS      = tostring(var.max_concurrent_sessions)
      CLAIM_RENEW_MARGIN_SECONDS   = tostring(var.claim_renew_margin_seconds)
      INGRESS_CONNECTORS           = local.ingress_connector
      EGRESS_CONNECTORS            = local.egress_connector
      # Stable across redeploys so claims held by a previous invocation are
      # recognised as ours rather than as another worker's.
      ACCEPTOR_ID = "${var.name}-${random_id.suffix.hex}"
    }
  }

  depends_on = [
    aws_cloudwatch_log_group.reconciler,
    aws_iam_role_policy.reconciler,
    terraform_data.microvm_image,
  ]
}

resource "aws_cloudwatch_event_rule" "reconcile" {
  name                = "${var.name}-reconcile"
  description         = "Polls the Devin Outposts queue and reconciles it onto MicroVM workers."
  schedule_expression = "rate(${var.reconcile_interval_minutes} ${var.reconcile_interval_minutes == 1 ? "minute" : "minutes"})"
}

resource "aws_cloudwatch_event_target" "reconcile" {
  rule = aws_cloudwatch_event_rule.reconcile.name
  arn  = aws_lambda_function.reconciler.arn
}

resource "aws_lambda_permission" "reconcile" {
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.reconciler.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.reconcile.arn
}
