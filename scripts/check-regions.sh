#!/usr/bin/env bash
set -euo pipefail
PROFILE="${1:-adaptivenet-admin}"
checks=(
 'Singapore|ap-southeast-1|i-0039feaedbd2b3266|54.179.156.53|ws-12cfc829-fd81-440a-8c13-38cd28123fef|sg'
 'Virginia|us-east-1|i-0ea5125bcb2d201ec|100.58.157.137|ws-cc3d9d44-3a8e-49db-b275-0e8989ab47de|us'
)
for line in "${checks[@]}"; do
  IFS='|' read -r name region instance ip workspace slug <<< "$line"
  echo "===== $name ($region) ====="
  curl -fsS --connect-timeout 3 --max-time 8 "http://$ip/healthz" || echo 'Public health check FAILED'
  echo
  aws ssm describe-instance-information --profile "$PROFILE" --region "$region" \
    --filters "Key=InstanceIds,Values=$instance" \
    --query 'InstanceInformationList[0].[InstanceId,PingStatus]' --output text --no-cli-pager || true
  echo 'Prometheus:'
  if command -v awscurl >/dev/null; then
    AWS_PROFILE="$PROFILE" awscurl --region "$region" --service aps \
      "https://aps-workspaces.$region.amazonaws.com/workspaces/$workspace/api/v1/query?query=adaptivenet_demo_up" || true
  else
    echo 'Install awscurl on your Mac for signed Prometheus queries.'
  fi
  echo
  echo 'CloudWatch recent events (up to 3):'
  aws logs filter-log-events --profile "$PROFILE" --region "$region" \
    --log-group-name "/adaptivenet/$slug/application" --limit 3 \
    --query 'events[].[timestamp,message]' --output text --no-cli-pager || true
  echo
done
