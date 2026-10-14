# AWS disposable two-region demo (NOT production)

This optional, billable Terraform root deploys two HTTP demo instances: Singapore
and N. Virginia. It requires a default VPC with public subnets in each region.
It supports public RIPE Atlas ICMP ping observations and HTTP endpoint checks.
It is not EKS, internal Prometheus, an AWS autoscaling deployment, or a complete
AdaptiveNet cloud controller. No customer data should be hosted here.

It does not reuse the existing dev Terraform state: manage as a separate root.
For a first run its state is local; do not store a state file in Git. For team
use, configure a protected remote backend before applying.

Commands (with AWS CLI SSO logged in):

    export AWS_PROFILE=adaptivenet-admin
    aws sts get-caller-identity
    terraform init
    terraform plan -var="aws_account_id=$(aws sts get-caller-identity --query Account --output text)" -out=demo.tfplan
    terraform apply demo.tfplan
    terraform output

Run those commands from this directory. Terraform apply creates billable EC2,
EBS, public IPv4 and optionally Route 53 health checks; delete them with
`terraform destroy` when finished. HTTP (not HTTPS) is for synthetic testing
only. Do not attach user data or confidential services. Cloud routes, if enabled,
are operated by Route 53 health checks; controller /tick controls ONLY the
local Compose gateway. Existing DNS and hosted zones are not created.

For a domain you own, add:
`-var=enable_route53=true -var=route53_zone_id=Z... -var=demo_domain=test.example.com`
to plan. Do not enable without checking your zone and costs.
