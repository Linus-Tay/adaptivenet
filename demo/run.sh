#!/usr/bin/env bash
# One-command local demo helper. No AWS access is used by any command here.
#   demo/run.sh up | down | logs | status | reset | replay | test
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
ENV_FILE="$HERE/.env.demo"   # ignored by git (.env.*), holds only a random local lab token
if [[ ! -f "$ENV_FILE" ]]; then
  umask 077
  printf 'LAB_TOKEN=%s\n' "$(python3 -c 'import secrets;print(secrets.token_hex(16))')" > "$ENV_FILE"
fi
set -a; source "$ENV_FILE"; set +a
mkdir -p "$ROOT/data/lab-live" "$ROOT/data/demo-live" "$ROOT/data/demo-replay" "$ROOT/data/demo-models"
live() { docker compose -f "$HERE/compose.yaml" --env-file "$ENV_FILE" "$@"; }
lab() { docker compose -f "$ROOT/compose.yaml" -f "$HERE/compose.lab-overlay.yaml" --env-file "$ENV_FILE" "$@"; }
replay() { docker compose -f "$HERE/compose.replay.yaml" --env-file "$ENV_FILE" "$@"; }
# Stop a real-AWS live_monitor.py started from this checkout (SIGINT lets it stop its children).
stop_real() {
  pkill -f "adaptivenet-real-live.sh" 2>/dev/null || true   # optional keep-alive loop around live_monitor
  pkill -INT -f "$ROOT/scripts/live_monitor.py" 2>/dev/null && echo "Stopped the real AWS telemetry pipeline" || true
}
case "${1:-up}" in
  lab-up)   live down 2>/dev/null || true; lab up --build -d
            echo "Group lab: dashboard http://127.0.0.1:3000  site http://127.0.0.1:3001"
            echo "ML + safety decisions: http://127.0.0.1:8190";;
  lab-down) lab down; stop_real;;
  real-down) stop_real;;
  lab-logs) lab logs -f --tail 50 "${@:2}";;
  lab-reset) lab stop lab-controller; rm -f "$ROOT/data/lab-live/controller-journal.db"
            lab restart gateway; lab start lab-controller;;
  up)     lab down 2>/dev/null || true; live up --build -d; echo "Decision view: http://127.0.0.1:8190";;
  down)   live down; replay down 2>/dev/null || true; stop_real;;
  logs)   live logs -f --tail 50 "${@:2}";;
  status) (cd "$ROOT" && PYTHONPATH="$ROOT/services:$ROOT" python3 -m demo.faultctl status);;
  reset)  (cd "$ROOT" && PYTHONPATH="$ROOT/services:$ROOT" python3 -m demo.faultctl clear && \
           PYTHONPATH="$ROOT/services:$ROOT" python3 -m demo.faultctl reset-route)
          live stop controller; rm -f "$ROOT/data/demo-live/controller-journal.db"; live start controller;;
  replay) live down 2>/dev/null || true
          : "${REPLAY_SOURCE:?set REPLAY_SOURCE to an ml archive folder or ingestion .db (absolute path)}"
          rm -rf "$ROOT/data/demo-replay"; mkdir -p "$ROOT/data/demo-replay"
          replay up --build -d; echo "Replay view: http://127.0.0.1:8190";;
  test)   (cd "$ROOT" && PYTHONPATH="$ROOT/services:$ROOT" python3 -m pytest -q demo/tests);;
  *) echo "usage: demo/run.sh lab-up|lab-down|real-down|lab-logs|lab-reset|up|down|logs|status|reset|replay|test"; exit 2;;
esac
