variable "aws_account_id" {
  type = string
  validation {
    condition     = can(regex("^[0-9]{12}$", var.aws_account_id))
    error_message = "Use your actual 12-digit AWS account ID."
  }
}
variable "github_repository" {
  type        = string
  description = "Exact OWNER/REPO; trust is limited to this repository."
  validation {
    condition     = can(regex("^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", var.github_repository))
    error_message = "Use OWNER/REPO without wildcards."
  }
}
variable "aws_region" {
  type    = string
  default = "ap-southeast-1"
}
variable "existing_github_oidc_provider_arn" {
  type        = string
  default     = null
  description = "If GitHub OIDC already exists in this account, supply its ARN; do not create a duplicate."
}
