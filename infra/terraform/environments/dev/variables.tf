variable "aws_account_id" {
  type = string
  validation {
    condition     = can(regex("^[0-9]{12}$", var.aws_account_id))
    error_message = "Expected a 12-digit AWS account ID."
  }
}
variable "aws_region" {
  type    = string
  default = "ap-southeast-1"
}
