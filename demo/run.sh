#!/usr/bin/env bash
# Local demo helper. No AWS access is used by any command here.
#   demo/run.sh lab-up | lab-down | lab-logs | lab-reset | status | real-down | test
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
ENV_FILE="$HERE/.env.demo"   # ignored by git (.env.*), holds only a random local lab token
if [[ ! -f "$ENV_FILE" ]]; then
  umask 077
  printf 'LAB_TOKEN=%s\n' "$(python3 -c 'import secrets;print(secrets.token_hex(16))')" > "$ENV_FILE"
fi
set -a; source "$ENV_FILE"; set +a
mkdir -p "$ROOT/data/lab-live" "$ROOT/data/demo-models"
lab() { docker compose -f "$ROOT/compose.yaml" -f "$HERE/compose.lab-overlay.yaml" --env-file "$ENV_FILE" "$@"; }
fault() { (cd "$ROOT" && PYTHONPATH="$ROOT/services:$ROOT" python3 -m demo.faultctl "$@"); }
# Stop a real-AWS live_monitor.py started from this checkout (SIGINT lets it stop its children).
stop_real() {
  pkill -f "adaptivenet-real-live.sh" 2>/dev/null || true   # optional keep-alive loop around live_monitor
  pkill -INT -f "$ROOT/scripts/live_monitor.py" 2>/dev/null && echo "Stopped the real AWS telemetry pipeline" || true
}
case "${1:-lab-up}" in
  lab-up)   lab up --build -d
            echo "Demo dashboard: http://127.0.0.1:3000   shop site: http://127.0.0.1:3001"
            echo "Debug view of the decision pipeline: http://127.0.0.1:8192";;
  lab-down) lab down; stop_real;;
  real-down) stop_real;;
  lab-logs) lab logs -f --tail 50 "${@:2}";;
  lab-reset) fault lab-clear >/dev/null || true; lab stop lab-controller
            rm -f "$ROOT/data/lab-live/controller-journal.db"
            lab restart gateway; lab start lab-controller;;
  status)   fault status;;
  test)     (cd "$ROOT" && PYTHONPATH="$ROOT/services:$ROOT" python3 -m pytest -q demo/tests);;
  *) echo "usage: demo/run.sh lab-up|lab-down|lab-logs|lab-reset|status|real-down|test"; exit 2;;
esac
