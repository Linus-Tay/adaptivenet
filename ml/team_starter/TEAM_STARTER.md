# AdaptiveNet — ML Team Starter (shadow-only)

Separate, additive and runnable handoff for the ML teammate. Everything under ml/team_starter/; no edits to the existing collector, SQS worker, UI, SQLite schema, Terraform, deployed AWS resources or ml/predictor.py.

## Historical note (4 November 2026)

Written when the ML work started: no trained model or automatic mitigation existed yet. The current status is in the top-level README.

## Quick start (offline, no AWS credentials)

Run at repository root in an environment with the repo installed:

~~~bash
python -m pip install -e '.[dev]'
python -m unittest discover -s ml/team_starter/tests -v
~~~

With your own populated local database, export a model input and run the working shadow baseline:

~~~bash
python scripts/model_handoff.py input --mode combined --out data/model-input.json
python -m ml.team_starter.cli --input data/model-input.json --group aws_only
python -m ml.team_starter.cli --input data/model-input.json --group aws_ripe
~~~

For HTTP-enhanced projection, save a real GET /v1/detection/input response to data/detection-input.json, then:

~~~bash
python -m ml.team_starter.cli --input data/detection-input.json --group aws_ripe_http
~~~

The CLI is a **working rule-baseline smoke test**, not a fitted classifier or comparative evaluation. The existing baseline uses AWS internal features only even when inspecting a combined feature group. A nonzero feature count or matching proposals across arms is NOT a claim of model accuracy.

## Preserve source data BEFORE rotation/pruning

~~~bash
python -m ml.team_starter.archive \
  --db data/adaptivenet-ingestion.db \
  --witness data/probe-events.jsonl \
  --out data/ml-archive
~~~

The helper reads the live SQLite database read-only, preserves all currently available snapshots and HTTP witness rows, and deduplicates on repeat runs. It writes only to ignored data/ml-archive; labels remain unset. Run frequently (for example every ten minutes during testing), before the 512-KiB HTTP journal rotates or SQLite prunes after seven days. It is **manual**, not a continuously running lossless recorder: events already rotated away cannot be recovered. A durable continuous archive needs separate implementation and validation.

## Six-class coverage and false-alarm audit

See [COVERAGE_AND_HEALTHY_CONTROLS.md](COVERAGE_AND_HEALTHY_CONTROLS.md): `python -m ml.team_starter.coverage_audit --labels <verified.json>` reports which classes have enough verified runs per split and whether false alarms per day can be estimated, and lists the evidence still missing.

## Ownership and next milestones

Telemetry owner: keep AWS sources, SQS/outbox, feature contracts, origin monitoring and dashboards stable.

ML teammate: independently verified episode labels, source-time joining, missing-value masks, temporal splits, reproducible training, group-disjoint evaluation, shadow predict(), and evidence/reporting.

Independent safety boundary: services/adaptivenet/ingestion/decisions.py always has executed=false. No model-driven AWS changes are authorized.

Read ARCHITECTURE.md, EPISODE_LABELS.md, docs/ml/PREDICTION_GUIDE.md and docs/ARCHITECTURE_AND_ML_2026.md.

Acceptance: tests green; no fake training samples, no RIPE leakage into AWS-only, no future information in features, no automatic routing/scale actions; compare AWS-only / AWS+RIPE / AWS+RIPE+HTTP on identical held-out episode groups and report false alarms/day, F1, delay, misses and abstentions.


## Completed offline foundation (7 November 2026)

See [ML_FOUNDATION.md](ML_FOUNDATION.md) for continuous archiving, reviewed labels, as-of joining, train/evaluation commands and shadow-only model integration. The starter README above describes the original 4 November handoff; it is retained unchanged for historical context.
