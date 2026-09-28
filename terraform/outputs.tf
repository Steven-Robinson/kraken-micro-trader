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
