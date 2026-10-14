terraform {
  required_version = ">= 1.6.0, < 2.0.0"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 6.0" }
  }
  # Separate from EC2, EKS, Route 53 and existing stacks. Supply remote S3 backend at init.
  backend "s3" {}
}

variable "aws_account_id" { type = string }
variable "region_a_instance_id" {
  type = string
  default = "i-0039feaedbd2b3266"
}
variable "region_b_instance_id" {
  type = string
  default = "i-0ea5125bcb2d201ec"
}

provider "aws" {
  alias = "sg"
  region = "ap-southeast-1"
  allowed_account_ids = [var.aws_account_id]
}
provider "aws" {
  alias = "us"
  region = "us-east-1"
  allowed_account_ids = [var.aws_account_id]
}

data "aws_instance" "sg" {
  provider = aws.sg
  instance_id = var.region_a_instance_id
}
data "aws_instance" "us" {
  provider = aws.us
  instance_id = var.region_b_instance_id
}

# CloudFront is global. AWS default metrics are accessible via CloudWatch us-east-1.
resource "aws_cloudfront_distribution" "adaptive_demo" {
  provider = aws.us
  enabled = true
  comment = "AdaptiveNet disposable research demo: SG primary, Virginia backup"
  is_ipv6_enabled = true
  price_class = "PriceClass_200"
  http_version = "http2"
  wait_for_deployment = true
  retain_on_delete = true

  origin {
    domain_name = data.aws_instance.sg.public_dns
    origin_id = "ec2-sg"
    connection_attempts = 2
    connection_timeout = 5
    custom_origin_config {
      http_port = 80
      https_port = 443
      origin_protocol_policy = "http-only" # Demo EC2 HTTP origins: NOT end-to-end TLS.
      origin_ssl_protocols = ["TLSv1.2"]
      origin_read_timeout = 15
      origin_keepalive_timeout = 5
    }
  }
  origin {
    domain_name = data.aws_instance.us.public_dns
    origin_id = "ec2-us"
    connection_attempts = 2
    connection_timeout = 5
    custom_origin_config {
      http_port = 80
      https_port = 443
      origin_protocol_policy = "http-only" # Upgrade origin to HTTPS before production.
      origin_ssl_protocols = ["TLSv1.2"]
      origin_read_timeout = 15
      origin_keepalive_timeout = 5
    }
  }
  origin_group {
    origin_id = "regional-failover"
    failover_criteria { status_codes = [500, 502, 503, 504] }
    member { origin_id = "ec2-sg" }
    member { origin_id = "ec2-us" }
  }
  default_cache_behavior {
    target_origin_id = "regional-failover"
    allowed_methods = ["GET", "HEAD", "OPTIONS"]
    cached_methods = ["GET", "HEAD"]
    viewer_protocol_policy = "redirect-to-https"
    cache_policy_id = "4135ea2d-6df8-44a3-9df3-4b5a84be39ad" # AWS managed CachingDisabled
    compress = false
  }
  restrictions {
    geo_restriction { restriction_type = "none" }
  }
  viewer_certificate {
    cloudfront_default_certificate = true
    minimum_protocol_version = "TLSv1.2_2021"
  }
  tags = {
    Project = "AdaptiveNet"
    Environment = "research-demo"
    ManagedBy = "Terraform"
  }
  lifecycle {
    prevent_destroy = true
  }
}

output "cloudfront_distribution_id" { value = aws_cloudfront_distribution.adaptive_demo.id }
output "cloudfront_domain_name" { value = aws_cloudfront_distribution.adaptive_demo.domain_name }
output "cloudfront_health_url" { value = "https://${aws_cloudfront_distribution.adaptive_demo.domain_name}/healthz" }
