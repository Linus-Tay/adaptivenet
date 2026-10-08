# Human-only bootstrap

Creates the state bucket, three ECR repositories and three GitHub OIDC roles. Run as your human AWS SSO setup identity. No EKS, NAT, ALB, Route53 or workload is deployed here. Read docs/AWS_GITHUB_SETUP.md first.

CI cannot change these IAM trust/policy resources or the bootstrap state. Reuse an existing GitHub OIDC provider via the variable rather than creating a duplicate. The initial local state must be migrated to the newly created bucket; see the guide. Keep the bootstrap key separate from dev/terraform.tfstate.
