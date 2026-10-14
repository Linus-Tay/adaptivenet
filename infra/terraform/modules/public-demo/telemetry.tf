# AdaptiveNet demo telemetry IAM. Additive changes to existing EC2 instances.
variable "aws_account_id" {
  type = string
}
variable "aws_region" {
  type = string
}
variable "amp_workspace_id" {
  type = string
}

locals {
  telemetry_log_group = "/adaptivenet/${var.region_label == "region-a" ? "sg" : "us"}/application"
}

resource "aws_iam_role" "telemetry" {
  name = "${var.name}-telemetry"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
  tags = { Project = "AdaptiveNet", Environment = "demo" }
}

resource "aws_iam_role_policy_attachment" "ssm" {
  role       = aws_iam_role.telemetry.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_role_policy" "observability" {
  name = "${var.name}-observability"
  role = aws_iam_role.telemetry.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "WriteToOwnPrometheusWorkspace"
        Effect   = "Allow"
        Action   = ["aps:RemoteWrite"]
        Resource = "arn:aws:aps:${var.aws_region}:${var.aws_account_id}:workspace/${var.amp_workspace_id}"
      },
      {
        Sid      = "CreateOwnLogStreamAndWriteEvents"
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents", "logs:DescribeLogStreams"]
        Resource = "arn:aws:logs:${var.aws_region}:${var.aws_account_id}:log-group:${local.telemetry_log_group}:*"
      },
      {
        Sid      = "ListLogGroupsForAgent"
        Effect   = "Allow"
        Action   = ["logs:DescribeLogGroups"]
        Resource = "*"
      }
    ]
  })
}

resource "aws_iam_instance_profile" "telemetry" {
  name = "${var.name}-telemetry"
  role = aws_iam_role.telemetry.name
}
