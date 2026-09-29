terraform {
  required_version = ">= 1.5.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.4"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

# -----------------------------------------------------------------------------
# 1. DynamoDB State Table
# -----------------------------------------------------------------------------
resource "aws_dynamodb_table" "bot_state" {
  name         = "kraken_bot_state"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "bot_id"

  attribute {
    name = "bot_id"
    type = "S"
  }

  tags = {
    Project = "KrakenMicroTrader"
    Managed = "Terraform"
  }
}

# -----------------------------------------------------------------------------
# 2. Package Lambda Code
# -----------------------------------------------------------------------------
data "archive_file" "lambda_zip" {
  type        = "zip"
  source_dir  = "${path.module}/../src"
  output_path = "${path.module}/lambda.zip"
  excludes    = ["local_state.json", "__pycache__", "local_runner.py"]
}

# -----------------------------------------------------------------------------
# 3. IAM Role & Permissions
# -----------------------------------------------------------------------------
resource "aws_iam_role" "lambda_exec" {
  name = "kraken-trader-lambda-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "lambda.amazonaws.com"
        }
      }
    ]
  })
}

resource "aws_iam_policy" "lambda_policy" {
  name        = "kraken-trader-lambda-policy"
  description = "Allows Lambda to read/write DynamoDB, read SSM params, and write CloudWatch logs"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "DynamoDBAccess"
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:PutItem",
          "dynamodb:UpdateItem"
        ]
        Resource = aws_dynamodb_table.bot_state.arn
      },
      {
        Sid    = "SSMParameterAccess"
        Effect = "Allow"
        Action = [
          "ssm:GetParameter",
          "ssm:GetParameters"
        ]
        Resource = "arn:aws:ssm:${var.aws_region}:*:parameter/crypto-bot/*"
      },
      {
        Sid    = "CloudWatchLogging"
        Effect = "Allow"
        Action = [
          "logs:CreateLogStream",
          "logs:PutLogEvents"
        ]
        Resource = "${aws_cloudwatch_log_group.lambda_logs.arn}:*"
      },
      {
        Sid    = "SNSPublishAccess"
        Effect = "Allow"
        Action = [
          "sns:Publish"
        ]
        Resource = aws_sns_topic.trade_alerts.arn
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "lambda_policy_attach" {
  role       = aws_iam_role.lambda_exec.name
  policy_arn = aws_iam_policy.lambda_policy.arn
}

resource "aws_cloudwatch_log_group" "lambda_logs" {
  name              = "/aws/lambda/kraken-micro-trader"
  retention_in_days = 14
}

# -----------------------------------------------------------------------------
# 4. Amazon SNS Trade Alert Topic
# -----------------------------------------------------------------------------
resource "aws_sns_topic" "trade_alerts" {
  name = "kraken-trade-alerts"

  tags = {
    Project = "KrakenMicroTrader"
    Managed = "Terraform"
  }
}

resource "aws_sns_topic_subscription" "email_sub" {
  count     = var.alert_email != "" ? 1 : 0
  topic_arn = aws_sns_topic.trade_alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

# -----------------------------------------------------------------------------
# 5. Lambda Function
# -----------------------------------------------------------------------------
resource "aws_lambda_function" "crypto_bot" {
  function_name    = "kraken-micro-trader"
  role             = aws_iam_role.lambda_exec.arn
  handler          = "handler.lambda_handler"
  runtime          = "python3.12"
  filename         = data.archive_file.lambda_zip.output_path
  source_code_hash = data.archive_file.lambda_zip.output_base64sha256
  timeout          = 30
  memory_size      = 256

  environment {
    variables = {
      DRY_RUN               = tostring(var.dry_run)
      TRADING_PAIR          = var.trading_pair
      BOT_ID                = var.bot_id
      ALLOCATED_CAPITAL_GBP = var.allocated_capital_gbp
      PROFIT_TARGET_PERCENT = var.profit_target_percent
      BUY_DISCOUNT_PERCENT  = var.buy_discount_percent
      DYNAMODB_TABLE        = aws_dynamodb_table.bot_state.name
      SSM_PARAM_API_KEY         = "/crypto-bot/kraken-api-key"
      SSM_PARAM_API_SECRET      = "/crypto-bot/kraken-api-secret"
      SNS_TOPIC_ARN             = aws_sns_topic.trade_alerts.arn
      DYNAMIC_TARGET_ENABLED    = tostring(var.dynamic_target_enabled)
      MIN_PROFIT_TARGET_PERCENT = var.min_profit_target_percent
      MAX_PROFIT_TARGET_PERCENT = var.max_profit_target_percent
    }
  }

  depends_on = [
    aws_iam_role_policy_attachment.lambda_policy_attach,
    aws_cloudwatch_log_group.lambda_logs
  ]

  tags = {
    Project = "KrakenMicroTrader"
    Managed = "Terraform"
  }
}

# -----------------------------------------------------------------------------
# 5. EventBridge Cron Schedule (Every 5 Minutes)
# -----------------------------------------------------------------------------
resource "aws_cloudwatch_event_rule" "cron_schedule" {
  name                = "kraken-trader-schedule"
  description         = "Triggers Kraken Micro-Trader bot periodically"
  schedule_expression = var.cron_schedule
}

resource "aws_cloudwatch_event_target" "lambda_target" {
  rule      = aws_cloudwatch_event_rule.cron_schedule.name
  target_id = "KrakenMicroTraderLambda"
  arn       = aws_lambda_function.crypto_bot.arn
}

resource "aws_lambda_permission" "allow_eventbridge" {
  statement_id  = "AllowExecutionFromEventBridge"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.crypto_bot.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.cron_schedule.arn
}
