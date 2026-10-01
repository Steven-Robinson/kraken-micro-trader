# -----------------------------------------------------------------------------
# EC2 Triangular Arbitrage Daemon Infrastructure (eu-west-1)
# -----------------------------------------------------------------------------

# AMI Lookup for Amazon Linux 2023 ARM64
data "aws_ami" "al2023_arm64" {
  most_recent = true
  owners      = ["amazon"]

  filter {
    name   = "name"
    values = ["al2023-ami-2023.*-kernel-6.1-arm64"]
  }

  filter {
    name   = "state"
    values = ["available"]
  }
}

# 1. Dedicated Minimalist VPC
resource "aws_vpc" "arb_vpc" {
  count                = var.enable_ec2_arb_daemon ? 1 : 0
  cidr_block           = "10.0.0.0/16"
  enable_dns_hostnames = true
  enable_dns_support   = true

  tags = {
    Name    = "kraken-arb-vpc"
    Project = "KrakenMicroTrader"
    Managed = "Terraform"
  }
}

resource "aws_subnet" "arb_subnet" {
  count                   = var.enable_ec2_arb_daemon ? 1 : 0
  vpc_id                  = aws_vpc.arb_vpc[0].id
  cidr_block              = "10.0.1.0/24"
  availability_zone       = "${var.aws_region}a"
  map_public_ip_on_launch = true

  tags = {
    Name    = "kraken-arb-subnet"
    Project = "KrakenMicroTrader"
  }
}

resource "aws_internet_gateway" "arb_igw" {
  count  = var.enable_ec2_arb_daemon ? 1 : 0
  vpc_id = aws_vpc.arb_vpc[0].id

  tags = {
    Name    = "kraken-arb-igw"
    Project = "KrakenMicroTrader"
  }
}

resource "aws_route_table" "arb_rt" {
  count  = var.enable_ec2_arb_daemon ? 1 : 0
  vpc_id = aws_vpc.arb_vpc[0].id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.arb_igw[0].id
  }

  tags = {
    Name    = "kraken-arb-rt"
    Project = "KrakenMicroTrader"
  }
}

resource "aws_route_table_association" "arb_rta" {
  count          = var.enable_ec2_arb_daemon ? 1 : 0
  subnet_id      = aws_subnet.arb_subnet[0].id
  route_table_id = aws_route_table.arb_rt[0].id
}

# 2. Security Group (Outbound only, zero inbound ports needed thanks to SSM)
resource "aws_security_group" "arb_sg" {
  count       = var.enable_ec2_arb_daemon ? 1 : 0
  name        = "kraken-arb-sg"
  description = "Security group for Kraken Arbitrage Daemon"
  vpc_id      = aws_vpc.arb_vpc[0].id

  egress {
    description = "Allow all outbound traffic for WebSockets, API calls, and OS updates"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name    = "kraken-arb-sg"
    Project = "KrakenMicroTrader"
  }
}

# 3. Dedicated Deployment Artifacts S3 Bucket
resource "aws_s3_bucket" "arb_deploy" {
  count         = var.enable_ec2_arb_daemon ? 1 : 0
  bucket_prefix = "kraken-arb-deploy-"
  force_destroy = true

  tags = {
    Name    = "kraken-arb-deploy-bucket"
    Project = "KrakenMicroTrader"
    Managed = "Terraform"
  }
}

resource "aws_s3_bucket_public_access_block" "arb_deploy" {
  count                   = var.enable_ec2_arb_daemon ? 1 : 0
  bucket                  = aws_s3_bucket.arb_deploy[0].id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "arb_deploy" {
  count  = var.enable_ec2_arb_daemon ? 1 : 0
  bucket = aws_s3_bucket.arb_deploy[0].id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_object" "arb_code" {
  count  = var.enable_ec2_arb_daemon ? 1 : 0
  bucket = aws_s3_bucket.arb_deploy[0].id
  key    = "code.zip"
  source = data.archive_file.lambda_zip.output_path
  etag   = data.archive_file.lambda_zip.output_md5
}

# 4. IAM Role & Instance Profile
resource "aws_iam_role" "ec2_arb_role" {
  count = var.enable_ec2_arb_daemon ? 1 : 0
  name  = "kraken-arb-ec2-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "ec2.amazonaws.com"
        }
      }
    ]
  })
}

# Attach AWS Systems Manager agent policy for secure shell access without SSH ports
resource "aws_iam_role_policy_attachment" "ssm_attach" {
  count      = var.enable_ec2_arb_daemon ? 1 : 0
  role       = aws_iam_role.ec2_arb_role[0].name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

# Attach policy to read Kraken credentials from SSM Parameter Store, publish to SNS, and pull code from S3
resource "aws_iam_policy" "ec2_arb_policy" {
  count = var.enable_ec2_arb_daemon ? 1 : 0
  name  = "kraken-arb-ec2-policy"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "SSMParameterAccess"
        Effect = "Allow"
        Action = [
          "ssm:GetParameter",
          "ssm:GetParameters"
        ]
        Resource = [
          "arn:aws:ssm:${var.aws_region}:*:parameter/crypto-bot/kraken-api-key",
          "arn:aws:ssm:${var.aws_region}:*:parameter/crypto-bot/kraken-api-secret"
        ]
      },
      {
        Sid    = "SNSPublishAccess"
        Effect = "Allow"
        Action = [
          "sns:Publish"
        ]
        Resource = aws_sns_topic.trade_alerts.arn
      },
      {
        Sid    = "S3DeployAccess"
        Effect = "Allow"
        Action = [
          "s3:GetObject"
        ]
        Resource = "${aws_s3_bucket.arb_deploy[0].arn}/*"
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "ec2_policy_attach" {
  count      = var.enable_ec2_arb_daemon ? 1 : 0
  role       = aws_iam_role.ec2_arb_role[0].name
  policy_arn = aws_iam_policy.ec2_arb_policy[0].arn
}

resource "aws_iam_instance_profile" "ec2_arb_profile" {
  count = var.enable_ec2_arb_daemon ? 1 : 0
  name  = "kraken-arb-ec2-profile"
  role  = aws_iam_role.ec2_arb_role[0].name
}

# 5. EC2 Nano Instance running WebSocket Daemon
resource "aws_instance" "arb_daemon" {
  count                  = var.enable_ec2_arb_daemon ? 1 : 0
  ami                    = data.aws_ami.al2023_arm64.id
  instance_type          = var.ec2_instance_type
  subnet_id              = aws_subnet.arb_subnet[0].id
  vpc_security_group_ids = [aws_security_group.arb_sg[0].id]
  iam_instance_profile   = aws_iam_instance_profile.ec2_arb_profile[0].name

  root_block_device {
    volume_size           = 8
    volume_type           = "gp3"
    encrypted             = true
    delete_on_termination = true
  }

  user_data = <<-EOF
              #!/bin/bash
              set -e
              echo "=== Starting Kraken Arbitrage Setup ==="

              # Configure swap space to avoid dnf OOM on nano instances
              if [ ! -f /swapfile ]; then
                fallocate -l 1G /swapfile || dd if=/dev/zero of=/swapfile bs=1M count=1024
                chmod 600 /swapfile
                mkswap /swapfile
                swapon /swapfile
                echo "/swapfile swap swap defaults 0 0" >> /etc/fstab
              fi

              dnf install -y python3-pip unzip awscli

              # Create application directory
              mkdir -p /opt/kraken-micro-trader/src
              cd /opt/kraken-micro-trader

              # Download and extract application code from S3
              aws s3 cp s3://${aws_s3_bucket.arb_deploy[0].id}/code.zip /opt/kraken-micro-trader/code.zip --region ${var.aws_region}
              unzip -o /opt/kraken-micro-trader/code.zip -d /opt/kraken-micro-trader/src/

              # Set up Python virtual environment
              python3 -m venv /opt/kraken-micro-trader/venv
              /opt/kraken-micro-trader/venv/bin/pip install --upgrade pip
              /opt/kraken-micro-trader/venv/bin/pip install websockets boto3

              # Create systemd service
              cat << 'SERVICE' > /etc/systemd/system/kraken-arb.service
              [Unit]
              Description=Kraken WebSocket Triangular Arbitrage Daemon
              After=network.target

              [Service]
              Type=simple
              User=root
              WorkingDirectory=/opt/kraken-micro-trader
              Environment="AWS_REGION=${var.aws_region}"
              Environment="AWS_DEFAULT_REGION=${var.aws_region}"
              Environment="DRY_RUN=${tostring(var.arb_dry_run)}"
              Environment="ARB_CAPITAL_GBP=${var.arb_capital_gbp}"
              Environment="SNS_TOPIC_ARN=${aws_sns_topic.trade_alerts.arn}"
              Environment="PYTHONPATH=/opt/kraken-micro-trader/src"
              ExecStart=/opt/kraken-micro-trader/venv/bin/python /opt/kraken-micro-trader/src/arbitrage/ws_daemon.py
              Restart=always
              RestartSec=5

              [Install]
              WantedBy=multi-user.target
              SERVICE

              systemctl daemon-reload
              systemctl enable --now kraken-arb.service
              echo "=== Kraken Arbitrage Setup Complete ==="
              EOF

  tags = {
    Name    = "kraken-arb-daemon"
    Project = "KrakenMicroTrader"
    Managed = "Terraform"
  }
}
