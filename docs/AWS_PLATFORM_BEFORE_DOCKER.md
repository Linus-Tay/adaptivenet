# AdaptiveNet AWS platform setup BEFORE Docker

This package **does not require Docker** to plan or provision the AWS foundation. It adds an OPTIONAL AWS observability / EKS stack alongside the existing foundation and EC2 demo. Existing Terraform directories are untouched.

## AWS architecture choices

- **Starter demo**: two minimal EC2 HTTP targets using existing `infra/terraform/environments/demo-cloud`, local dashboard/controller, local SQLite/registry. It does NOT require EKS, RDS, or paid managed Prometheus.
- **Cloud monitoring foundation**: new `infra/terraform/environments/platform` creates an AWS Managed Prometheus workspace and CloudWatch Logs group in Singapore; optional second copy in Virginia. Creation of AMP workspaces alone does **not** collect metrics.
- **Full Kubernetes experiment**: `--eks --two-regions` also creates VPC/networking, two EKS clusters, and two EC2 worker nodes per cluster. This incurs material hourly charges even when idle and this topology uses public-subnet demo nodes. The cluster has IAM auth and Kubernetes control-plane audit logs but its API endpoint is publicly reachable; not production-grade. It does **not** install a Prometheus scraper, K8s exporter, autoscaler, app, HTTPS ingress, or controller in the cluster. Don't claim end-to-end integration until those exist.
- **SQL**: RDS PostgreSQL is **not required** for the current in-memory/file-backed prototype and is deliberately not created. If a durable multi-user application registry becomes necessary, add RDS in private subnets with credentials in Secrets Manager later.
- **RIPE Atlas** is external to AWS. AWS CLI cannot create a RIPE account, supply credits, or authorize measurements. Existing `scripts/atlas_setup.py` handles RIPE when endpoints exist.
- **CloudWatch** log groups exist; actual application logs require app workload log configuration, not yet completed.

## 1. Local tools (macOS)

Install Homebrew from https://brew.sh if needed, then:

```bash
brew install awscli terraform kubectl
aws configure sso --profile adaptivenet-admin
aws sso login --profile adaptivenet-admin
aws sts get-caller-identity --profile adaptivenet-admin
```

SSO region is where **IAM Identity Center** lives; your resource region can still be Singapore. Check the account ID against the actual project account. Do not use root credentials or long-lived GitHub access keys.

## 2. Safe preview (no AWS writes)

```bash
cd adaptivenet
bash scripts/aws-platform-setup.sh --profile adaptivenet-admin
```

This checks STS, lists existing resources, runs `terraform init` and `terraform plan`; neither `init` nor `plan` deploy the planned resources. Provider downloads need internet. The default single Singapore workspace + log group is the smallest starting option.

## 3. Optional expanded plans

```bash
# CloudWatch + AMP in both regions, no EKS:
bash scripts/aws-platform-setup.sh --profile adaptivenet-admin --two-regions

# Two EKS clusters and monitoring (costly), supply the *IAM role ARN* of a persistent cluster administrator:
bash scripts/aws-platform-setup.sh --profile adaptivenet-admin --eks --two-regions --cluster-admin-arn arn:aws:iam::123456789012:role/YourAccessRole
```

`--k8s-version` overrides the initial default (1.34): verify that this release is currently supported in both target regions before requesting it.

## 4. Apply only with explicit approval

Repeat the desired command with `--apply`. Script requires the live AWS account ID and `APPLY ADAPTIVENET` typed manually. Terraform saved plans are ephemeral; no opaque plan persists to disk after the script exits. Real AWS charges, IAM roles, public networking, logs and possible data-retention consequences apply. Use AWS Budgets billing alerts separately in the console; never treat a budget as a hard spending cap.

## 5. Foundation is a separate stack

The preexisting `infra/terraform/bootstrap` controls project-specific S3 state storage, ECR registries, and GitHub OIDC roles. The preexisting `infra/terraform/environments/dev` manages SQS, DLQ and artifact bucket; it **requires a configured S3 backend**. Read their README and verify whether these stacks already exist before applying to avoid duplicate names or destroying existing state. This new script intentionally does not touch them.

## 6. After EKS becomes available

```bash
aws eks update-kubeconfig --region ap-southeast-1 --name adaptivenet-sg --profile adaptivenet-admin
kubectl get nodes
```

Using an IAM Identity Center assumed role may require an EKS access entry for the underlying SSO role. For managed Prometheus ingestion, explicitly configure an EKS managed scraper and target `ServiceMonitor`/scrape endpoints after workloads are installed. See https://docs.aws.amazon.com/prometheus/latest/userguide/AMP-collector-how-to.html . That extra wiring is NOT performed by this script.

## 7. Important state and rollback rules

Platform stack uses **local Terraform state** for the first setup to avoid assuming ownership of existing S3 state buckets. Keep `infra/terraform/environments/platform/terraform.tfstate` secure and backed up; never check it into Git. Before using this in CI or with teammates, migrate to a dedicated remote S3 backend and locking, carefully and with a backup. Never create duplicate Terraform workspaces that manage the same AWS resources. To decommission, inspect `terraform plan -destroy` in the same state directory. Do not delete EKS/RDS/other resources directly if Terraform manages them.

This is an infrastructure preparation checkpoint, not a complete production deployment. The full project still needs exported internal telemetry, access-controlled visualization, AWS Route 53 control, external probe results, instrumented K8s workloads and reproducible experiments.
