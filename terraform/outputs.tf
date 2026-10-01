output "lambda_function_name" {
  description = "Name of the deployed Lambda trading bot"
  value       = aws_lambda_function.crypto_bot.function_name
}

output "lambda_function_arn" {
  description = "ARN of the deployed Lambda trading bot"
  value       = aws_lambda_function.crypto_bot.arn
}

output "dynamodb_table_name" {
  description = "Name of the DynamoDB state table"
  value       = aws_dynamodb_table.bot_state.name
}

output "eventbridge_schedule" {
  description = "Execution schedule expression"
  value       = aws_cloudwatch_event_rule.cron_schedule.schedule_expression
}

output "dry_run_mode" {
  description = "Whether the bot is currently in simulation mode"
  value       = var.dry_run
}

# -----------------------------------------------------------------------------
# EC2 Triangular Arbitrage Daemon Outputs
# -----------------------------------------------------------------------------
output "ec2_instance_id" {
  description = "Instance ID of the EC2 Arbitrage Daemon"
  value       = try(aws_instance.arb_daemon[0].id, null)
}

output "ec2_public_ip" {
  description = "Public IP of the EC2 Arbitrage Daemon"
  value       = try(aws_instance.arb_daemon[0].public_ip, null)
}

output "ec2_security_group_id" {
  description = "Security Group ID of the EC2 Arbitrage Daemon"
  value       = try(aws_security_group.arb_sg[0].id, null)
}
