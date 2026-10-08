output "state_bucket" { value = aws_s3_bucket.state.id }
output "aws_account_id" { value = var.aws_account_id }
output "aws_region" { value = var.aws_region }
output "role_arns" { value = { for key, role in aws_iam_role.github : key => role.arn } }
output "image_repositories" { value = { for key, repo in aws_ecr_repository.images : key => repo.repository_url } }

output "github_repository" { value = var.github_repository }
