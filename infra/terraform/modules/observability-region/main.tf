terraform {
  required_providers { aws = { source = "hashicorp/aws"
  version = "~> 6.0" } }
}
variable "region" { type = string }
variable "enable_eks" { type = bool }
variable "kubernetes_version" { type = string }
variable "node_instance_type" { type = string }
variable "cluster_admin_arn" { type = string }
locals { slug = var.region == "ap-southeast-1" ? "sg" : "us" }
resource "aws_prometheus_workspace" "main" { alias = "adaptivenet-${local.slug}" }
resource "aws_cloudwatch_log_group" "app" {
  name              = "/adaptivenet/${local.slug}/application"
  retention_in_days = 7
}
resource "aws_cloudwatch_log_group" "eks" {
  count             = var.enable_eks ? 1 : 0
  name              = "/aws/eks/adaptivenet-${local.slug}/cluster"
  retention_in_days = 7
}
resource "aws_vpc" "eks" {
  count                = var.enable_eks ? 1 : 0
  cidr_block           = "10.${local.slug == "sg" ? 40 : 50}.0.0/16"
  enable_dns_hostnames = true
  enable_dns_support   = true
  tags                 = { Name = "adaptivenet-${local.slug}", "kubernetes.io/cluster/adaptivenet-${local.slug}" = "shared" }
}
data "aws_availability_zones" "available" { state = "available" }
resource "aws_internet_gateway" "eks" {
  count  = var.enable_eks ? 1 : 0
  vpc_id = aws_vpc.eks[0].id
}
resource "aws_subnet" "public" {
  count                   = var.enable_eks ? 2 : 0
  vpc_id                  = aws_vpc.eks[0].id
  cidr_block              = "10.${local.slug == "sg" ? 40 : 50}.${count.index * 16}.0/20"
  availability_zone       = data.aws_availability_zones.available.names[count.index]
  map_public_ip_on_launch = true
  tags                    = { Name = "adaptivenet-${local.slug}-public-${count.index}", "kubernetes.io/role/elb" = "1" }
}
resource "aws_route_table" "public" {
  count  = var.enable_eks ? 1 : 0
  vpc_id = aws_vpc.eks[0].id
}
resource "aws_route" "internet" {
  count                  = var.enable_eks ? 1 : 0
  route_table_id         = aws_route_table.public[0].id
  destination_cidr_block = "0.0.0.0/0"
  gateway_id             = aws_internet_gateway.eks[0].id
}
resource "aws_route_table_association" "public" {
  count          = var.enable_eks ? 2 : 0
  route_table_id = aws_route_table.public[0].id
  subnet_id      = aws_subnet.public[count.index].id
}
resource "aws_iam_role" "control" {
  count              = var.enable_eks ? 1 : 0
  name               = "adaptivenet-${local.slug}-eks-control"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{ Effect = "Allow", Principal = { Service = "eks.amazonaws.com" }, Action = "sts:AssumeRole" }] })
}
resource "aws_iam_role_policy_attachment" "control" {
  count      = var.enable_eks ? 1 : 0
  role       = aws_iam_role.control[0].name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSClusterPolicy"
}
resource "aws_iam_role" "nodes" {
  count              = var.enable_eks ? 1 : 0
  name               = "adaptivenet-${local.slug}-eks-nodes"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{ Effect = "Allow", Principal = { Service = "ec2.amazonaws.com" }, Action = "sts:AssumeRole" }] })
}
resource "aws_iam_role_policy_attachment" "node_policies" {
  for_each   = var.enable_eks ? toset(["AmazonEKSWorkerNodePolicy", "AmazonEC2ContainerRegistryPullOnly", "AmazonEKS_CNI_Policy"]) : toset([])
  role       = aws_iam_role.nodes[0].name
  policy_arn = "arn:aws:iam::aws:policy/${each.key}"
}
resource "aws_eks_cluster" "main" {
  count    = var.enable_eks ? 1 : 0
  name     = "adaptivenet-${local.slug}"
  role_arn = aws_iam_role.control[0].arn
  version  = var.kubernetes_version
  vpc_config {
    subnet_ids              = aws_subnet.public[*].id
    endpoint_private_access = true
    endpoint_public_access  = true
    public_access_cidrs     = ["219.74.62.94/32"]
  }
  access_config {
    authentication_mode                         = "API"
    bootstrap_cluster_creator_admin_permissions = false
  }
  enabled_cluster_log_types = ["api", "audit", "authenticator"]
  depends_on                = [aws_iam_role_policy_attachment.control, aws_cloudwatch_log_group.eks]
}
resource "aws_eks_access_entry" "admin" {
  count         = var.enable_eks && var.cluster_admin_arn != "" ? 1 : 0
  cluster_name  = aws_eks_cluster.main[0].name
  principal_arn = var.cluster_admin_arn
  type          = "STANDARD"
}
resource "aws_eks_access_policy_association" "admin" {
  count         = var.enable_eks && var.cluster_admin_arn != "" ? 1 : 0
  cluster_name  = aws_eks_cluster.main[0].name
  principal_arn = aws_eks_access_entry.admin[0].principal_arn
  policy_arn    = "arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy"
  access_scope { type = "cluster" }
}
resource "aws_eks_node_group" "nodes" {
  count           = var.enable_eks ? 1 : 0
  cluster_name    = aws_eks_cluster.main[0].name
  node_group_name = "general"
  node_role_arn   = aws_iam_role.nodes[0].arn
  subnet_ids      = aws_subnet.public[*].id
  instance_types  = [var.node_instance_type]
  capacity_type   = "ON_DEMAND"
  scaling_config {
    desired_size = 2
    min_size     = 1
    max_size     = 4
  }
  update_config { max_unavailable = 1 }
  depends_on = [aws_iam_role_policy_attachment.node_policies]
}
output "prometheus_workspace_id" { value = aws_prometheus_workspace.main.id }
output "eks_cluster_name" { value = var.enable_eks ? aws_eks_cluster.main[0].name : null }
