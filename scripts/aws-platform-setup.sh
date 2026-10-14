#!/usr/bin/env bash
# AdaptiveNet AWS platform setup. NO AWS writes unless --apply is passed.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROFILE="${AWS_PROFILE:-adaptivenet-admin}"
APPLY=0; EKS=0; TWO=0; ADMIN_ARN=""; VERSION="1.34"
usage() { cat <<'HELP'
Usage: bash scripts/aws-platform-setup.sh [--profile NAME] [--eks] [--two-regions] [--cluster-admin-arn IAM_ROLE_ARN] [--k8s-version VERSION] [--apply]
Default: verify AWS auth, inspect existing resources, prepare Terraform plan for Singapore Prometheus + CloudWatch only. No changes.
--eks: include costly EKS cluster and two EC2 worker nodes per chosen region (demo-only public-subnet architecture).
--two-regions: provision also in N. Virginia; default Singapore only.
--apply: AFTER generating plan, require typing exact AWS account ID and APPLY ADAPTIVENET to actually provision.
Does NOT deploy applications, provision SQL, configure RIPE account, create global DNS, or install a managed Prometheus scraper.
HELP
}
while (($#)); do case "$1" in
  --profile) PROFILE="${2:?}"; shift 2;;
  --eks) EKS=1; shift;;
  --two-regions) TWO=1; shift;;
  --cluster-admin-arn) ADMIN_ARN="${2:?}"; shift 2;;
  --k8s-version) VERSION="${2:?}"; shift 2;;
  --apply) APPLY=1; shift;;
  -h|--help) usage; exit 0;;
  *) echo "Unknown option: $1" >&2; usage; exit 2;;
esac; done
for c in aws terraform python3; do command -v "$c" >/dev/null || { echo "Missing $c. Install it before continuing." >&2; exit 1; }; done
export AWS_PROFILE="$PROFILE" AWS_PAGER=""
if ! aws sts get-caller-identity >/dev/null 2>&1; then
  echo "AWS profile $PROFILE is not authenticated. Run: aws sso login --profile $PROFILE" >&2
  exit 1
fi
ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
ARN="$(aws sts get-caller-identity --query Arn --output text)"
[[ "$ACCOUNT" =~ ^[0-9]{12}$ ]] || { echo 'Invalid AWS account ID' >&2; exit 1; }
echo "AWS account: $ACCOUNT"; echo "AWS identity: $ARN"
echo 'Existing resources (read-only, errors may indicate restricted permissions):'
for region in ap-southeast-1 us-east-1; do
  echo "== $region =="
  aws eks list-clusters --region "$region" --query 'clusters' --output json 2>/dev/null || true
  aws amp list-workspaces --region "$region" --query 'workspaces[].{alias:alias,id:workspaceId}' --output json 2>/dev/null || true
done
if ((EKS)) && [[ -z "$ADMIN_ARN" ]]; then
  echo 'For EKS, supply --cluster-admin-arn arn:aws:iam::ACCOUNT:role/ROLE (use the persistent SSO IAM role ARN, not STS assumed-role ARN).' >&2
  exit 1
fi
if [[ -n "$ADMIN_ARN" ]] && [[ "$ADMIN_ARN" != arn:aws:iam::"$ACCOUNT":role/* ]]; then
  echo 'cluster admin must be an IAM role in the selected account' >&2; exit 1
fi
WORK="$ROOT/infra/terraform/environments/platform"
cd "$WORK"
[[ ! -e terraform.tfstate && ! -e terraform.tfstate.backup ]] || echo 'NOTICE: existing local Terraform state detected; do not erase or relocate it.'
echo 'Checking Terraform formatting and configuration before AWS planning...'
terraform fmt -recursive "$ROOT/infra/terraform/environments/platform" "$ROOT/infra/terraform/modules/observability-region"
terraform init -input=false
terraform validate -no-color
PLAN="$(mktemp "${TMPDIR:-/tmp}/adaptivenet-platform.XXXXXX.tfplan")"
trap 'rm -f "$PLAN"' EXIT
terraform plan -input=false -lock-timeout=30s \
  -var="aws_account_id=$ACCOUNT" \
  -var="enable_eks=$([[ "$EKS" == 1 ]] && echo true || echo false)" \
  -var="enable_second_region=$([[ "$TWO" == 1 ]] && echo true || echo false)" \
  -var="cluster_admin_arn=$ADMIN_ARN" \
  -var="eks_kubernetes_version=$VERSION" \
  -out="$PLAN"
terraform show -no-color "$PLAN" | tail -35
if (( !APPLY )); then
  echo 'PLAN ONLY — no AWS resources created. Rerun with --apply after reviewing costs and resources.'
  exit 0
fi
echo 'WARNING: Real AWS billing begins after apply. The EKS option uses publicly addressed demo nodes and a public Kubernetes API endpoint; NOT production hardened.'
read -r -p "To authorize provisioning, type the AWS account ID ($ACCOUNT): " CONFIRM
[[ "$CONFIRM" == "$ACCOUNT" ]] || { echo 'Cancelled'; exit 1; }
read -r -p 'Type APPLY ADAPTIVENET: ' CONFIRM
[[ "$CONFIRM" == 'APPLY ADAPTIVENET' ]] || { echo 'Cancelled'; exit 1; }
terraform apply -input=false "$PLAN"
echo 'Provisioning completed. Keep this directory and its terraform.tfstate secure; do not delete it. Review outputs above.'
