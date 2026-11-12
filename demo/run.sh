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
mkdir -p "$ROOT/data/demo-live" "$ROOT/data/demo-replay" "$ROOT/data/demo-models"
live() { docker compose -f "$HERE/compose.yaml" --env-file "$ENV_FILE" "$@"; }
replay() { docker compose -f "$HERE/compose.replay.yaml" --env-file "$ENV_FILE" "$@"; }
case "${1:-up}" in
  up)     live up --build -d; echo "Decision view: http://127.0.0.1:8190";;
  down)   live down; replay down 2>/dev/null || true;;
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
  *) echo "usage: demo/run.sh up|down|logs|status|reset|replay|test"; exit 2;;
esac
