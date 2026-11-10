# Step 4.2.11 — Historical as-of availability audit

Read-only, ML-only diagnostic for **persisted native feature snapshots**. This
module does not modify `ingestion.py`, SQLite tables, AWS, Terraform, Docker,
RIPE measurement configuration, or the native training data.

Run from the AdaptiveNet repository root:

```powershell
python -m ml.team_starter.availability_audit --snapshots data/ml-archive/feature_snapshots.jsonl --db data/adaptivenet-ingestion.db --out data/ml-results/native/availability-audit.json
Get-Content data/ml-results/native/availability-audit.json
python -m pytest -q ml/team_starter/tests/test_availability_audit.py
python -m pytest -q
```

Checks: snapshot IDs and contract, duplicates, cutoff consistency with any
corresponding current SQLite snapshot, ingestion-run timestamps, regional AMP,
RIPE and CloudFront provenance times, and missing (`null`) feature values.
The ongoing ingestion watcher can add rows concurrently; SQLite read is a
single consistent read-only transaction.

Important: Some archived snapshots may no longer be present in the live SQLite
file due to the application's seven-day retention policy. Report those as
`not_in_current_sqlite`, not as falsification. Do not add missing values as
zeros. `limited_availability_evidence` is **not** proof of strict per-metric
historical replay safety. The live poll start timestamp is not a per-metric
result receipt time; exact receipt cannot be reconstructed from this schema.

Accept original persisted feature snapshots as the primary historical artifacts;
for arbitrary raw-event replay, use only values whose availability by the
cutoff is independently established, otherwise abstain. No synthetic tests
are native performance evaluation or verified incident labels.
