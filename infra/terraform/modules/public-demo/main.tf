variable "name" { type = string }
variable "region_label" { type = string }
variable "instance_type" { type = string }

data "aws_vpc" "default" { default = true }
data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
}
data "aws_ssm_parameter" "al2023" { name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64" }

resource "aws_security_group" "demo" {
  name_prefix = "${var.name}-"
  description = "Public HTTP and ICMP for the AdaptiveNet disposable demo; no SSH"
  vpc_id      = data.aws_vpc.default.id
  ingress {
    description = "HTTP demo endpoint"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
  ingress {
    description = "RIPE Atlas ping probes"
    from_port   = -1
    to_port     = -1
    protocol    = "icmp"
    cidr_blocks = ["0.0.0.0/0"]
  }
  egress {
    description = "Updates"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
  lifecycle { create_before_destroy = true }
}

resource "aws_instance" "demo" {
  iam_instance_profile        = aws_iam_instance_profile.telemetry.name
  ami                         = data.aws_ssm_parameter.al2023.value
  instance_type               = var.instance_type
  subnet_id                   = sort(data.aws_subnets.default.ids)[0]
  vpc_security_group_ids      = [aws_security_group.demo.id]
  associate_public_ip_address = true
  metadata_options { http_tokens = "required" }
  root_block_device {
    encrypted   = true
    volume_size = 10
    volume_type = "gp3"
  }
  user_data = templatefile("${path.module}/user_data.sh.tftpl", { region_label = var.region_label })
  tags      = { Name = var.name, Project = "AdaptiveNet", Environment = "demo" }
}
output "public_ip" { value = aws_instance.demo.public_ip }
output "public_dns" { value = aws_instance.demo.public_dns }
