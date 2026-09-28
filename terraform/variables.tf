variable "aws_region" {
  description = "AWS Region to deploy resources into"
  type        = string
  default     = "eu-west-1"
}

variable "bot_id" {
  description = "Unique identifier for this trading bot instance"
  type        = string
  default     = "SOLGBP-PINGPONG"
}

variable "trading_pair" {
  description = "Kraken asset pair to trade (e.g. SOLGBP, ETHGBP, XBTGBP)"
  type        = string
  default     = "SOLGBP"
}

variable "allocated_capital_gbp" {
  description = "Total GBP capital to allocate per trade cycle"
  type        = string
  default     = "48.00"
}

variable "profit_target_percent" {
  description = "Gross target percentage profit per cycle (e.g. 2.0)"
  type        = string
  default     = "2.0"
}

variable "buy_discount_percent" {
  description = "Discount below best bid for limit buys to guarantee Maker status"
  type        = string
  default     = "0.2"
}

variable "dry_run" {
  description = "Set to true to simulate orders without risking real capital"
  type        = bool
  default     = true
}

variable "cron_schedule" {
  description = "EventBridge cron expression or rate"
  type        = string
  default     = "rate(5 minutes)"
}
