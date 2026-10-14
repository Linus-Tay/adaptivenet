terraform {
  required_version = ">= 1.6.0, < 2.0.0"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 6.0" }
  }
}

variable "aws_account_id" { type = string }
variable "route53_zone_id" { type = string, default = "" }
variable "demo_domain" { type = string, default = "" }
variable "instance_type" { type = string, default = "t3.micro" }
variable "enable_route53" { type = bool, default = false }

provider "aws" {
  alias = "singapore"
  region = "ap-southeast-1"
  allowed_account_ids = [var.aws_account_id]
}
provider "aws" {
  alias = "virginia"
  region = "us-east-1"
  allowed_account_ids = [var.aws_account_id]
}

module "sg" {
  source = "../../modules/public-demo"
  providers = { aws = aws.singapore }
  name = "adaptivenet-sg"
  region_label = "region-a"
  instance_type = var.instance_type
}
module "us" {
  source = "../../modules/public-demo"
  providers = { aws = aws.virginia }
  name = "adaptivenet-us"
  region_label = "region-b"
  instance_type = var.instance_type
}

resource "aws_route53_health_check" "sg" {
  count = var.enable_route53 ? 1 : 0
  fqdn = module.sg.public_dns
  port = 80
  type = "HTTP"
  resource_path = "/healthz"
  failure_threshold = 3
  request_interval = 30
}
resource "aws_route53_health_check" "us" {
  count = var.enable_route53 ? 1 : 0
  fqdn = module.us.public_dns
  port = 80
  type = "HTTP"
  resource_path = "/healthz"
  failure_threshold = 3
  request_interval = 30
}
resource "aws_route53_record" "sg" {
  count = var.enable_route53 ? 1 : 0
  zone_id = var.route53_zone_id
  name = var.demo_domain
  type = "A"
  ttl = 30
  records = [module.sg.public_ip]
  set_identifier = "region-a"
  weighted_routing_policy { weight = 50 }
  health_check_id = aws_route53_health_check.sg[0].id
}
resource "aws_route53_record" "us" {
  count = var.enable_route53 ? 1 : 0
  zone_id = var.route53_zone_id
  name = var.demo_domain
  type = "A"
  ttl = 30
  records = [module.us.public_ip]
  set_identifier = "region-b"
  weighted_routing_policy { weight = 50 }
  health_check_id = aws_route53_health_check.us[0].id
}
output "region_a_endpoint" { value = "http://${module.sg.public_ip}" }
output "region_b_endpoint" { value = "http://${module.us.public_ip}" }
output "region_a_dns" { value = module.sg.public_dns }
output "region_b_dns" { value = module.us.public_dns }
output "route53_enabled" { value = var.enable_route53 }
