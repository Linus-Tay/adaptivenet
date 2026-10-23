# Restrict producer/consumer to the existing Singapore dev telemetry queue.
# Attach the output policies to the specific EC2/EKS role or SSO permission set
# actually running each process; do NOT attach broad access to all identities.
resource "aws_iam_policy" "telemetry_producer" {
  name        = "adaptivenet-dev-telemetry-publish"
  description = "Publish normalized AdaptiveNet telemetry polls only"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["sqs:GetQueueUrl", "sqs:GetQueueAttributes", "sqs:SendMessage"]
      Resource = aws_sqs_queue.telemetry.arn
    }]
  })
}

resource "aws_iam_policy" "telemetry_consumer" {
  name        = "adaptivenet-dev-telemetry-consume"
  description = "Process and acknowledge normalized AdaptiveNet telemetry polls only"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["sqs:GetQueueUrl", "sqs:GetQueueAttributes", "sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:ChangeMessageVisibility"]
      Resource = aws_sqs_queue.telemetry.arn
    }]
  })
}

# Alarms are visible in CloudWatch even before notification actions are configured.
# Alarm routing to SNS/email is a separate, explicit deployment decision.
resource "aws_cloudwatch_metric_alarm" "telemetry_backlog_age" {
  alarm_name          = "adaptivenet-dev-telemetry-oldest-over-5min"
  alarm_description   = "Consumer stalled or queue delayed; inspect ingestion and SQS"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  threshold           = 300
  metric_name         = "ApproximateAgeOfOldestMessage"
  namespace           = "AWS/SQS"
  period              = 60
  statistic           = "Maximum"
  treat_missing_data  = "notBreaching"
  dimensions = {
    QueueName = aws_sqs_queue.telemetry.name
  }
}

resource "aws_cloudwatch_metric_alarm" "telemetry_dlq_nonempty" {
  alarm_name          = "adaptivenet-dev-telemetry-dlq-not-empty"
  alarm_description   = "Failed telemetry poll exceeded redrive attempts; inspect DLQ before replay"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  threshold           = 0
  metric_name         = "ApproximateNumberOfMessagesVisible"
  namespace           = "AWS/SQS"
  period              = 60
  statistic           = "Maximum"
  treat_missing_data  = "notBreaching"
  dimensions = {
    QueueName = aws_sqs_queue.dead_letter.name
  }
}

output "telemetry_producer_policy_arn" {
  value       = aws_iam_policy.telemetry_producer.arn
  description = "Attach to the publisher identity; not automatically attached"
}

output "telemetry_consumer_policy_arn" {
  value       = aws_iam_policy.telemetry_consumer.arn
  description = "Attach to the detector/SQLite consumer identity; not automatically attached"
}
