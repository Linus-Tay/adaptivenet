# Existing Terraform-managed AMP workspaces confirmed by the user's AWS CLI.
# Override if the workspaces are ever intentionally replaced.
variable "sg_amp_workspace_id" {
  type        = string
  description = "Existing Singapore AMP workspace ID"
  default     = "ws-12cfc829-fd81-440a-8c13-38cd28123fef"
}

variable "us_amp_workspace_id" {
  type        = string
  description = "Existing Virginia AMP workspace ID"
  default     = "ws-cc3d9d44-3a8e-49db-b275-0e8989ab47de"
}
