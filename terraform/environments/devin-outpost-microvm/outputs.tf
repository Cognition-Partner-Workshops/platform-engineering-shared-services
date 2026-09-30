output "microvm_image_arn" {
  description = "MicroVM image workers are launched from."
  value       = local.microvm_image_arn
}

output "reconciler_function_name" {
  description = "Lambda that claims queued sessions and runs a MicroVM for each."
  value       = aws_lambda_function.reconciler.function_name
}

output "state_table_name" {
  description = "DynamoDB table mapping session IDs to MicroVM IDs."
  value       = aws_dynamodb_table.sessions.name
}

output "artifact_bucket" {
  description = "Bucket holding the worker image build artifact."
  value       = aws_s3_bucket.artifacts.id
}

output "worker_log_group" {
  description = "CloudWatch log group receiving worker output, one stream per session."
  value       = aws_cloudwatch_log_group.workers.name
}

output "acceptor_id" {
  description = "Identity this deployment claims sessions as."
  value       = aws_lambda_function.reconciler.environment[0].variables["ACCEPTOR_ID"]
}

output "reconcile_command" {
  description = "Force a reconcile without waiting for the schedule."
  value       = "aws lambda invoke --region ${var.region} --function-name ${aws_lambda_function.reconciler.function_name} /dev/stdout"
}
