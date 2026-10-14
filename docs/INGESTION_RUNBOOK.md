# Runbook — use on the Mac, not EC2

## Prerequisites

- Existing `adaptivenet` project and AWS SSO profile `adaptivenet-admin` authorized to **query** two AMP workspaces. `aps:QueryMetrics` may need granting if AWS rejects the queries.
- `python3` 3.11+, `aws` CLI and `awscurl`. `brew install awscurl` on Mac.
- RIPE measurement IDs already created and publicly queryable. **Polling results costs no additional RIPE probe-measurement credits, but creating/continuing recurring measurements does.**
- Correct CA bundle for macOS Python, if needed: `export SSL_CERT_FILE="$(brew --prefix ca-certificates)/share/ca-certificates/cacert.pem"`.

## Install and verify (no Terraform)

Unzip the overlay in Downloads. In your Mac Terminal:

```bash
python3 ~/Downloads/AdaptiveNet-Ingestion-Overlay/scripts/install_overlay.py \
  --project "$HOME/Downloads/adaptivenet 5"
# Review preview. If it lists only newly added files:
python3 ~/Downloads/AdaptiveNet-Ingestion-Overlay/scripts/install_overlay.py \
  --project "$HOME/Downloads/adaptivenet 5" --apply

cd "$HOME/Downloads/adaptivenet 5"
aws sso login --profile adaptivenet-admin
python3 -m pytest -q tests/test_ingestion_pipeline.py
python3 scripts/ingestion.py once --profile adaptivenet-admin
```

If the current RIPE run has ended (auto-stop at 2026-10-14T13:21:22Z), quality will become stale after 35 minutes. This is expected; `ready_for_model` will stay false until fresh live probe observations are available. Use archived raw event observations for retrospective modelling.

## Run two terminals

**Mac Terminal 1 (polling + SQLite persistence):**

```bash
cd "$HOME/Downloads/adaptivenet 5"
python3 scripts/ingestion.py watch --profile adaptivenet-admin --interval 120
```

**Mac Terminal 2 (read-only API):**

```bash
cd "$HOME/Downloads/adaptivenet 5"
python3 scripts/ingestion.py serve --port 8766
```

Visit `http://127.0.0.1:8766/`. Open `http://127.0.0.1:8766/v1/features/latest` to see the exact model contract. Watch is optional and stops when Terminal 1 is stopped; **it does not stop RIPE's scheduled measurements**. The API only serves persisted snapshots and performs no AWS calls.

## Export datasets to classmates

```bash
python3 scripts/ingestion.py export --format jsonl --limit 200 --out data/features-v1.jsonl
python3 scripts/ingestion.py export --format csv --limit 200 --out data/features-v1.csv
```

Share exported features and handoff docs. Never share `.env`, SSO tokens, AWS state, secrets or the private SQLite journal with unrelated sensitive data. Time-based experiments should include an explicit experiment manifest and immutable configuration reference.

## Model-to-controller dry run

```bash
python3 scripts/ingestion.py review --proposal examples/model_proposal.example.json
```

This example intentionally uses a placeholder snapshot ID and should be rejected. Classmates replace `snapshot_id` with the latest snapshot ID when testing. The tool never changes traffic.

## Diagnostics

- `sqlite3 data/adaptivenet-ingestion.db 'select source_status, inserted, duplicates, errors_json from ingestion_runs order by run_id desc limit 5;'`
- `curl -sS http://127.0.0.1:8766/v1/ingestion/status`
- `curl -sS http://127.0.0.1:8766/v1/features/latest`
- `aws sso login --profile adaptivenet-admin` if session expired.
- `python3 scripts/ingestion.py once` after changes; inspect named `source_errors`. Source errors are class names or `no_series` identifiers, intentionally not leaked subprocess credentials.

## Known limitations

- The dashboard is a functional handoff UI, not the final Next.js production dashboard.
- Running `once` without traffic on `/api/catalog` may show missing rate/latency metrics; this is better than making up a zero. Generate only **controlled, approved test traffic** for experiments.
- Metrics queries can incur AWS charges; default watch interval in examples is 120 seconds to reduce query load.
- No Route53, EKS, SQS or AWS writes are performed.
