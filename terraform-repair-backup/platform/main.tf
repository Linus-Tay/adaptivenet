terraform {
  required_version = ">= 1.6, < 2.0"
  required_providers { aws = { source = "hashicorp/aws"
      version = "~> 6.0" } }
}
variable "aws_account_id" { type = string }
variable "enable_eks" { type = bool
  default = false }
variable "enable_second_region" { type = bool
  default = false }
variable "eks_kubernetes_version" { type = string
  default = "1.34" }
variable "node_instance_type" { type = string
  default = "t3.medium" }
variable "cluster_admin_arn" { type = string
  default = "" }
provider "aws" {
  alias = "sg"
  region = "ap-southeast-1"
  allowed_account_ids = [var.aws_account_id]
  default_tags { tags = { Project = "AdaptiveNet"
    ManagedBy = "Terraform"
    Stack = "platform" } }
}
provider "aws" {
  alias = "us"
  region = "us-east-1"
  allowed_account_ids = [var.aws_account_id]
  default_tags { tags = { Project = "AdaptiveNet"
    ManagedBy = "Terraform"
    Stack = "platform" } }
}
module "sg" {
  source = "../../modules/observability-region"
  providers = { aws = aws.sg }
  region = "ap-southeast-1"
  enable_eks = var.enable_eks
  kubernetes_version = var.eks_kubernetes_version
  node_instance_type = var.node_instance_type
  cluster_admin_arn = var.cluster_admin_arn
}
module "us" {
  count = var.enable_second_region ? 1 : 0
  source = "../../modules/observability-region"
  providers = { aws = aws.us }
  region = "us-east-1"
  enable_eks = var.enable_eks
  kubernetes_version = var.eks_kubernetes_version
  node_instance_type = var.node_instance_type
  cluster_admin_arn = var.cluster_admin_arn
}
output "prometheus_singapore_workspace_id" { value = module.sg.prometheus_workspace_id }
output "prometheus_virginia_workspace_id" { value = var.enable_second_region ? module.us[0].prometheus_workspace_id : null }
output "eks_singapore_cluster" { value = module.sg.eks_cluster_name }
output "eks_virginia_cluster" { value = var.enable_second_region ? module.us[0].eks_cluster_name : null }
