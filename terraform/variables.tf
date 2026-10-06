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

variable "trading_pairs" {
  description = "Comma-separated list of Kraken asset pairs to trade (e.g. SOLGBP or SOLGBP,ETHGBP)"
  type        = string
  default     = "SOLGBP"
}

variable "allocated_capital_gbp" {
  description = "Total GBP capital to allocate per trade cycle"
  type        = string
  default     = "48.00"
}

variable "profit_target_percent" {
  description = "Gross target percentage profit per cycle (e.g. 0.4)"
  type        = string
  default     = "0.4"
}

variable "buy_discount_percent" {
  description = "Discount below best bid for limit buys to guarantee Maker status"
  type        = string
  default     = "0.2"
}

variable "post_fill_cooldown_minutes" {
  description = "Minutes to wait after a sell fill before placing the next buy order"
  type        = string
  default     = "6"
}

variable "dry_run" {
  description = "Set to true to simulate orders without risking real capital"
  type        = bool
  default     = true
}

variable "cron_schedule" {
  description = "EventBridge cron expression or rate"
  type        = string
  default     = "rate(2 minutes)"
}

variable "alert_email" {
  description = "Email address to receive order execution and profit notifications"
  type        = string
  default     = ""
}

variable "dynamic_target_enabled" {
  description = "Whether to dynamically compute profit target using 24h market volatility (ATR)"
  type        = bool
  default     = true
}

variable "min_profit_target_percent" {
  description = "Minimum allowed profit target floor (protects fee efficiency)"
  type        = string
  default     = "0.35"
}

variable "max_profit_target_percent" {
  description = "Maximum allowed profit target ceiling (avoids stranded orders)"
  type        = string
  default     = "0.8"
}

variable "dynamic_capital_enabled" {
  description = "Automatically detect deposits and trade size dynamically"
  type        = bool
  default     = true
}

variable "min_fee_buffer_gbp" {
  description = "Minimum uncommitted GBP cash reserved for trading fees"
  type        = string
  default     = "2.00"
}

variable "fee_buffer_percent" {
  description = "Percentage of portfolio equity reserved for fees"
  type        = string
  default     = "3.0"
}

