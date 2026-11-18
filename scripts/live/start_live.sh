#!/usr/bin/env bash
# Start the real AWS telemetry pipeline (SQS + HTTP witness) and its dashboard on :8766.
#   scripts/live/start_live.sh                      # profile adaptivenet-admin
#   AWS_PROFILE_NAME=adaptivenet-ml FORCE_IPV4=1 scripts/live/start_live.sh
# Sign in first (aws sso login). Run on one machine only: two pipelines share one SQS queue.
# FORCE_IPV4=1 is for networks whose IPv6 route to AWS hangs (it was needed on the group Mac).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
PY="$ROOT/.venv/bin/python"; [[ -x "$PY" ]] || PY="$(command -v python3)"
# The venv comes first; a bundled AWS CLI folder goes last because it ships its own python.
export PATH="$ROOT/.venv/bin:$PATH${AWS_CLI_DIR:+:$AWS_CLI_DIR}"
[[ -d "$HOME/aws-cli" ]] && export PATH="$PATH:$HOME/aws-cli"
export PYTHONPATH="$ROOT/services:$ROOT"
[[ "${FORCE_IPV4:-0}" == "1" ]] && export PYTHONPATH="$ROOT/scripts/live/ipv4:$PYTHONPATH"
export SSL_CERT_FILE="${SSL_CERT_FILE:-$("$PY" -c 'import certifi;print(certifi.where())')}"
exec "$PY" scripts/live_monitor.py --sqs --traffic --profile "${AWS_PROFILE_NAME:-adaptivenet-admin}"
