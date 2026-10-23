#!/usr/bin/env bash
# One-time guarded SQS-only bootstrap using AdaptiveNet's shared remote dev state.
set -euo pipefail
ACTION="--plan"
if [[ "$#" -gt 0 ]]; then ACTION="$1"; fi
if [[ "$ACTION" != "--plan" && "$ACTION" != "--apply" ]]; then
  echo "Usage: bash scripts/deploy_sqs_terraform.sh [--plan|--apply]" >&2; exit 2
fi
PROFILE="$(printenv AWS_PROFILE || true)"
if [[ -z "$PROFILE" ]]; then PROFILE="adaptivenet-admin"; fi
ACCOUNT="468354413374"
REGION="ap-southeast-1"
BUCKET="adaptivenet-$ACCOUNT-$REGION-tfstate"
STATE_KEY="dev/terraform.tfstate"
QUEUE="adaptivenet-dev-telemetry"
DLQ="adaptivenet-dev-telemetry-dlq"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TF_DIR="$ROOT/infra/terraform/environments/dev"
for cmd in aws terraform python3; do command -v "$cmd" >/dev/null || { echo "Missing: $cmd" >&2; exit 2; }; done
export AWS_PROFILE="$PROFILE" AWS_REGION="$REGION" AWS_DEFAULT_REGION="$REGION" TF_IN_AUTOMATION=1

REAL_ACCOUNT="$(aws sts get-caller-identity --profile "$PROFILE" --query Account --output text)"
[[ "$REAL_ACCOUNT" == "$ACCOUNT" ]] || { echo "WRONG ACCOUNT: $REAL_ACCOUNT" >&2; exit 3; }
echo "Account $REAL_ACCOUNT | AWS region $REGION | profile $PROFILE"
if [[ -f "$TF_DIR/terraform.tfstate" || -f "$TF_DIR/terraform.tfstate.backup" ]]; then
  echo "Existing local Terraform dev state detected; do not overwrite or delete it." >&2; exit 3
fi
[[ -f "$TF_DIR/backend.tf" ]] || { echo "Missing backend.tf: unsafe local state" >&2; exit 3; }
python3 - "$TF_DIR/.terraform/terraform.tfstate" "$BUCKET" "$STATE_KEY" <<'PY'
import json, pathlib, sys
p=pathlib.Path(sys.argv[1])
if p.exists():
    try:
        backend=(json.loads(p.read_text()).get("backend") or {}).get("config") or {}
    except (OSError, ValueError) as exc:
        raise SystemExit(f"Invalid existing .terraform metadata: {exc}")
    if backend and any(backend.get(k) not in (None,v)
                       for k,v in (("bucket",sys.argv[2]),("key",sys.argv[3]))):
        raise SystemExit("Existing backend points to another state! Stop.")
PY
aws s3api head-bucket --bucket "$BUCKET" --profile "$PROFILE" >/dev/null 2>&1 || {
  echo "Remote state bucket $BUCKET is absent or inaccessible. No changes made." >&2; exit 3;
}

# The checked-in lock has an upstream-signed zip hash set but may lack an
# h1 checksum for a developer's platform (e.g. Apple Silicon). Refresh only
# the pinned AWS provider's official hashes for Mac and CI Linux first.
# Keep init readonly so it cannot silently select another provider version.
echo "Verifying official hashicorp/aws provider checksums for Mac + CI Linux"
terraform -chdir="$TF_DIR" providers lock \
  -platform=darwin_arm64 \
  -platform=linux_amd64 \
  registry.terraform.io/hashicorp/aws
python3 - "$TF_DIR/.terraform.lock.hcl" <<'PY'
import pathlib, re, sys
lock = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8")
if not re.search(r'provider "registry\.terraform\.io/hashicorp/aws"\s*\{[^}]*?version\s*=\s*"6\.12\.0"',lock,re.S):
    raise SystemExit("Unexpected provider selection: expected hashicorp/aws v6.12.0")
if len(re.findall(r'"h1:[A-Za-z0-9+/=]+"',lock)) < 2:
    raise SystemExit("Cross-platform provider checksums missing; stopping before init")
PY

echo "Initializing locked S3 backend s3://$BUCKET/$STATE_KEY"
terraform -chdir="$TF_DIR" init -reconfigure -input=false -lockfile=readonly \
  -backend-config="bucket=$BUCKET" -backend-config="key=$STATE_KEY" \
  -backend-config="region=$REGION" -backend-config="encrypt=true" \
  -backend-config="use_lockfile=true"

STATE_LIST="$(terraform -chdir="$TF_DIR" state list -no-color 2>/dev/null || true)"
# Never create/import over a queue that another state/account already owns.
if aws sqs get-queue-url --queue-name "$QUEUE" --profile "$PROFILE" --region "$REGION" >/dev/null 2>&1; then
  grep -Fxq 'aws_sqs_queue.telemetry' <<< "$STATE_LIST" || {
    echo "Main queue exists outside this state. Import it first; refusing apply." >&2; exit 3;
  }
fi
if aws sqs get-queue-url --queue-name "$DLQ" --profile "$PROFILE" --region "$REGION" >/dev/null 2>&1; then
  grep -Fxq 'aws_sqs_queue.dead_letter' <<< "$STATE_LIST" || {
    echo "DLQ exists outside this state. Import it first; refusing apply." >&2; exit 3;
  }
fi

WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT
terraform -chdir="$TF_DIR" plan -input=false -lock-timeout=60s \
  -var="aws_account_id=$ACCOUNT" -var="aws_region=$REGION" \
  -target=aws_sqs_queue.dead_letter -target=aws_sqs_queue.telemetry \
  -target=aws_sqs_queue_redrive_allow_policy.dead_letter \
  -out="$WORKDIR/queue.tfplan"
terraform -chdir="$TF_DIR" show -json "$WORKDIR/queue.tfplan" > "$WORKDIR/queue.json"
python3 - "$WORKDIR/queue.json" <<'PY'
import json,sys
with open(sys.argv[1],encoding="utf-8") as f: plan=json.load(f)
allowed={"aws_sqs_queue.dead_letter","aws_sqs_queue.telemetry",
         "aws_sqs_queue_redrive_allow_policy.dead_letter"}
create=[]
for row in plan.get("resource_changes",[]):
    address=row["address"]
    actions=row.get("change",{}).get("actions",[])
    if address not in allowed or actions not in (["create"],["no-op"]):
        raise SystemExit(f"REFUSING unexpected Terraform change: {address} {actions}")
    if actions==["create"]: create.append(address)
print("Verified queue-only create plan:", ", ".join(create) or "(already managed)")
PY

if [[ "$ACTION" == "--plan" ]]; then
  echo "PLAN ONLY: nothing has been created. Run with --apply to deploy."; exit 0
fi
echo "Applying checked, saved queue-only plan..."
terraform -chdir="$TF_DIR" apply -input=false -lock-timeout=60s "$WORKDIR/queue.tfplan"
MAIN_URL="$(aws sqs get-queue-url --queue-name "$QUEUE" --profile "$PROFILE" --region "$REGION" --query QueueUrl --output text)"
DLQ_URL="$(aws sqs get-queue-url --queue-name "$DLQ" --profile "$PROFILE" --region "$REGION" --query QueueUrl --output text)"
echo "Created/verified SQS queue: $MAIN_URL"
echo "Created/verified DLQ: $DLQ_URL"
aws sqs get-queue-attributes --queue-url "$MAIN_URL" \
  --attribute-names RedrivePolicy VisibilityTimeout ReceiveMessageWaitTimeSeconds \
  --profile "$PROFILE" --region "$REGION"
echo "Next: python scripts/sqs_ingestion.py publish-once && python scripts/sqs_ingestion.py consume-once"
