#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ ! -f .env ]]; then
  cp .env.example .env
  python3 - <<'PY'
from pathlib import Path
import secrets
p=Path('.env'); s=p.read_text().replace('replace-with-a-long-random-value',secrets.token_urlsafe(36)); p.write_text(s)
PY
  chmod 600 .env
fi
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
pytest -q
printf '\nLocal test setup complete. Run: docker compose up --build -d\n'
