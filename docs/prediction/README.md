# AdaptiveNet — Prediction & Machine Learning Handoff

**Audience:** teammates building the predictive/fault-detection model, offline evaluation, and shadow-mode inference.  
**Canonical handoff:** this file; **do not use the repository root README as an ML specification**.  
**Source branch:** `feature/ingestion-ml-handoff`. **Owner of the infrastructure and collector:** AdaptiveNet telemetry team.  
**Status:** feature extraction, persistence, export, proposal contract and shadow review exist. **No trained predictive model or live ML-based mitigation has been delivered.**

> **The handoff is ready for model development; the training data are not yet a complete, independently labeled incident dataset.** Do not claim fault prediction accuracy until enough real, verified experiments have been collected.

## 1. Your assignment

Build and evaluate a model that consumes timestamped AWS/RIPE features to **detect or predict regional degradation**. Train offline first, then supply a predictor that returns a **versioned proposal**. An ML proposal does **not** modify CloudFront, Route 53, EKS, EC2 or any real infrastructure.

**Deliver these artifacts:**

1. **Dataset and label specification:** experiment-run IDs; observed fault start/end/recovery; affected region; cause; target state; prediction horizon; clear healthy intervals; verified source of each label. Do not infer labels from a fault button click.
2. **Repeatable data preparation:** time alignment, masks for null features, feature-selection lists, chronological windowing, quality gates and train/validation/test split by **entire experiment run and time**.
3. **Baseline and predictive models:** simple threshold/rule baseline, one or more trained alternatives, and **AWS-only vs AWS+RIPE** ablation. CloudFront global telemetry can be evaluated as another feature group, not treated as a regional measurement.
4. **Evaluation report:** precision, recall, F1, false-positive rate, detection delay/lead time, missed faults, calibration, data coverage and experiment conditions. Include a healthy/no-action baseline.
5. **Implementation:** `ml/predictor.py` exposes `predict(model_input: dict) -> dict`; returns an `adaptivenet.proposal.v1` proposal. Demonstrate a shadow reviewer response without any AWS action.

**Agreed research question:** Can external RIPE Atlas measurements combined with AWS telemetry improve early identification of unhealthy regional service/network conditions compared with AWS telemetry alone? Define *early* operationally (e.g. 1-minute or 5-minute forecast horizon) and collect labeled examples before claiming a specific horizon is achievable.

## 2. The existing data pipeline (do not reimplement this)

```text
SG and VA EC2 application / host exporters
        └── AWS Managed Prometheus (AMP): app status, RPS, errors, CPU, memory, HTTP p95
External RIPE Atlas probes: ICMP RTT and packet loss (regional measurement IDs)
AWS CloudFront / CloudWatch: global CDN requests, errors and transfer metrics
        ↓
Python ingestion watch — polls AWS/RIPE/CloudWatch; preserves source UTC timestamps
        ↓
SQLite WAL observations (SHA256 IDs, repeat-poll deduplication)
        ↓
Versioned feature snapshots: adaptivenet.features.v1
        ↓
build_model_input(): adaptivenet.model_input.v1
        ↓
YOUR model: predict(input) -> adaptivenet.proposal.v1
        ↓
Independent review: adaptivenet.decision_review.v1
        ↓
Shadow only; no live actuator
```

Existing separate user interfaces:

| Port | Surface | Meaning for the prediction team |
|---|---|---|
| `8766` | Read-only Network Observatory and JSON endpoints | Review real features, missingness, timestamps and data quality |
| `3000` | Experiment controller | Manual faults; **not** ground-truth evidence by itself |
| `3001` | Actual workload test UI | Observe CloudFront and both direct origins, HTTP status/latency and response origin |
| CloudFront domain | Deployed CDN API | SG primary + VA fallback; **global** viewer metrics |

The `/v1/experiments/probes` endpoint contains **independent, opt-in HTTP witness journal events**. These are useful *evidence* for incident verification; they are **not merged into the current regional model feature columns**.

## 3. Setup and obtain data

**If another teammate supplies the exported JSONL, you need no AWS account or RIPE API key to train on it.** Keep generated datasets in ignored `data/`, share deliberately, and do not commit secrets, AWS credentials, SQLite files or unreviewed raw experiment data.

For a local checkout with an existing populated SQLite database:

```bash
git clone https://github.com/Linus-Tay/adaptivenet.git
cd adaptivenet
git fetch origin
git switch --track origin/feature/ingestion-ml-handoff

python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
python -m pytest -q

# Run only if this checkout has the populated local data/adaptivenet-ingestion.db
python scripts/export_ml_dataset.py --mode combined --limit 500 --out data/ml-snapshots.jsonl
python scripts/export_ml_dataset.py --mode internal_only --limit 500 --out data/ml-internal-only-snapshots.jsonl

# Current live input (requires a recent snapshot; useful for schema inspection)
python scripts/model_handoff.py input --mode combined --out data/model-input.json

# Rule baseline + shadow review; no AWS calls
python scripts/model_handoff.py demo --mode combined
```

**Important:** `data/adaptivenet-ingestion.db` belongs to the machine that collected observations and is **not on GitHub**. If your checkout has no database, the exporter cannot create historical data. Ask the telemetry team for a real, reviewed `ml-snapshots.jsonl` export or record your own authorized run; do **not** fill the gap with fabricated data.

If the collector's local HTTP API is running, inspect (from that machine):

```bash
curl -fsS http://127.0.0.1:8766/v1/features/latest
curl -fsS 'http://127.0.0.1:8766/v1/features/history?limit=120'
curl -fsS http://127.0.0.1:8766/v1/ingestion/status
curl -fsS http://127.0.0.1:8766/v1/experiments/probes
```

**API ≠ queue.** The active collector polls real sources and persists to local SQLite. SQS/DLQ resources are only Terraform definitions at this stage; there is **no** working SQS ingestion consumer/publisher in this research path.

## 4. Exact model input contract

Implementation: `services/adaptivenet/ingestion/handoff.py`. Type declarations: `packages/contracts/src/model_handoff.ts`. Feature descriptions: `docs/DATA_DICTIONARY.md`.

`adaptivenet.model_input.v1` contains:

| Field | Description |
|---|---|
| `contract_version` | Always `adaptivenet.model_input.v1` |
| `snapshot_id` | Stable ID identifying one normalized feature snapshot |
| `mode` | `internal_only` or `combined` |
| `computed_at_unix`, `age_seconds` | UTC source snapshot time and wall-clock age |
| `ready_for_inference` | Data-quality gate AND current snapshot age (live max 180s) |
| `quality_reasons`, `quality` | Missing/stale/insufficient upstream observations and region gates |
| `feature_columns` | Canonical ordered 17 regional columns, **same order for both regions** |
| `features.region-a`, `features.region-b` | Numeric feature values or `null`, never invented zeros |
| `missing.region-a`, `missing.region-b` | Explicit Boolean null masks for every canonical feature |
| `provenance` | Original source timestamps/ages and source identifiers |
| `comparison` | Precomputed region A minus region B differences; `null` where unavailable |
| `cloudfront` | Global CDN status, only live `features` and provenance; no `last_known` |
| `actions_enabled` | Always `false` |

The canonical ordered `feature_columns` (from `handoff.py`) are:

```text
app_up
cpu_busy_pct
cpu_busy_ewma_pct
memory_available_bytes
memory_total_bytes
memory_available_pct
node_load1
http_rps
http_5xx_rps
http_error_ratio
http_p95_ms
http_p95_ewma_ms
http_p95_robust_z
network_rtt_p50_ms
network_rtt_p95_ms
packet_loss_pct
ripe_probes_reporting
```

**Regions:** `region-a` = Singapore (`ap-southeast-1`); `region-b` = N. Virginia (`us-east-1`). CloudFront is `global`, not `region-a` or `region-b`. Percentages are 0–100 except `http_error_ratio` and confidence, which are in 0–1. RIPE RTT and regional HTTP duration are different physical measurements, although both are expressed in milliseconds.

**Null handling:** `null` means missing/unavailable/stale, not healthy and not zero. Preserve masks when imputing/scaling. `app_up=0` can mean a measured application-down signal; `app_up=null` means no valid signal. `cloudfront.features` are live-only; the observatory's `cloudfront.last_known` is **not** model input.

**Timing:** feature snapshot freshness for live inference: 180s; AMP observation freshness: 180s, with bounded +30s source clock skew; RIPE Atlas: 2100s and ≥3 reporting probes; CloudFront metrics: 900s. Historic dataset exports intentionally reconstruct `ready_for_inference` at each row's **collection timestamp**; that does NOT authorize real-time inference using historic rows.

### Critical ablation detail: internal-only does not automatically filter RIPE values

In the *current implementation*, the `mode` flag changes **which quality gate controls `ready_for_inference`**, but **both modes include all 17 regional feature columns**. Therefore, an AWS-only training run **MUST explicitly drop** `network_rtt_p50_ms`, `network_rtt_p95_ms`, `packet_loss_pct`, and `ripe_probes_reporting`. Record a fixed feature selection in the run configuration. Otherwise, an "internal-only" result may accidentally still use RIPE measurements and invalidate the comparison.

For the AWS+RIPE baseline, include those four RIPE-derived columns, preserving missingness. Decide separately whether to include the five precomputed `comparison` deltas; do not let combined-only deltas leak RIPE into AWS-only runs.

The `mode` flag also does **not** by itself switch off global CloudFront values. For a clean AWS-AMP-only vs AWS-AMP-plus-RIPE experiment, either exclude global CloudFront from **both** groups or evaluate it as a separately controlled feature group.

## 5. Exported JSONL format and feature preparation

`scripts/export_ml_dataset.py` emits **one JSON object per stored snapshot**, sorted chronologically from the last 500 saved snapshots. Key fields:

```json
{
  "snapshot_id": "features-<source-derived-id>",
  "computed_at_unix": 0,
  "mode": "combined",
  "source_ready_at_collection": false,
  "feature_columns": ["app_up", "... other canonical columns ..."],
  "features": {"region-a": {"app_up": null}, "region-b": {"app_up": null}},
  "missing": {"region-a": {"app_up": true}, "region-b": {"app_up": true}},
  "quality": {},
  "provenance": {},
  "comparison": {},
  "cloudfront_live_at_collection": null,
  "fault_label": null,
  "label_source": null
}
```

**The above is a shape illustration, not a real measurement or a complete valid row.** Never substitute its values for collected observations. Dataset `fault_label` and `label_source` are deliberately unfilled. The exporter does not automatically join to fault/SSM events, traffic logs or incident results. The 500-row limit is an exporter limit, **not** a claim you already have 500 observations. Runs lasting many hours/days should be exported/archived periodically to preserve experiment-run boundaries and coverage.

Minimal read-only inspection without external packages:

```python
import json
from pathlib import Path

rows = [json.loads(line) for line in Path("data/ml-snapshots.jsonl").read_text().splitlines() if line.strip()]
assert all(r["mode"] == "combined" for r in rows)
assert all(r["fault_label"] is None for r in rows)  # Before verified labeling
for r in rows[:3]:
    print(r["snapshot_id"], r["computed_at_unix"],
          r["features"]["region-a"]["cpu_busy_pct"],
          r["missing"]["region-a"]["http_p95_ms"])
```

Suggested training preprocessing: sort/deduplicate by `snapshot_id` and timestamp; verify monotonic time and region identity; separate train-fitted transformations from validation/test; preserve unit types and null masks; construct causal rolling windows using only measurements observed at or before the feature cutoff. Global CloudFront timestamps may lag traffic; evaluate whether a CloudWatch sample was actually **available by decision time**, not merely older than a future fault label.

## 6. Ground truth: experiment manifests and labels

**Do not train on manually guessed labels.** An operator request or SSM `send-command` acceptance is an intent record, not proof the service stopped. A CloudFront HTTP 200 during a regional outage can be legitimate failover, not a "no fault" label.

For each run, record a separate reviewed manifest (suggested field names; not yet enforced by repository code):

```json
{
  "experiment_run_id": "run-unique-id",
  "region": "region-a",
  "fault_type": "http_service_stop",
  "intended_duration_seconds": 30,
  "ssm_command_id": "record-if-available",
  "ssm_execution_status": "verified-or-failed",
  "verified_fault_start_utc": null,
  "verified_fault_end_utc": null,
  "verified_recovery_utc": null,
  "observation_evidence": [],
  "label_reviewer": null
}
```

For *healthy* control runs, explicitly log their start/end and baseline behavior using both regional direct HTTP witnesses and relevant AMP health. For *fault* runs, independently verify (a) target region directly failed, (b) CloudFront response/origin if failover is part of the study, and (c) direct service recovered after the timer. RIPE ICMP can remain healthy during an HTTP-only stop; that is expected.

Define labels in two distinct ways: **detection** (`is_region_unhealthy_at_t`) and **prediction** (`region_will_become_unhealthy_within_H_seconds`). An observation taken *after* a fault starts can be a detection positive, but must not count as a successful pre-fault prediction. Choose and document horizon `H`, grace periods, acceptable event times, and neutral/ambiguous periods near transition boundaries.

**Experimental limitation:** if faults are random/unannounced and all pre-fault measurements are identical to healthy baseline, the model cannot scientifically predict their start before any precursors. Evaluating **early detection**, fault-cause classification and risk under measurable degradation may be more defensible than claiming advance prediction of arbitrary manual shutdowns.

Use the independent witness journal `data/probe-events.jsonl` to support incident verification, but do not silently include the post-outage witness HTTP status as a *pre-outage* feature. Ensure no post-fault outcome leaks into your prediction window.

## 7. Model interface and shadow validation

The teammate-owned Python entry point already exists:

```python
# ml/predictor.py (currently a rule baseline, NOT a trained model)
from adaptivenet.ingestion.handoff import conservative_rule_baseline

def predict(model_input: dict) -> dict:
    return conservative_rule_baseline(model_input)
```

Replace the rule baseline with a loaded, versioned model and deterministic preprocessing. Keep the `predict(model_input)` signature. Verify `contract_version`, `mode`, columns, snapshot timestamp/age and readiness; return conservative `hold`/`investigate` if data are unavailable rather than manufacturing numeric samples.

A `adaptivenet.proposal.v1` example:

```json
{
  "contract_version": "adaptivenet.proposal.v1",
  "snapshot_id": "features-<exact-input-snapshot-id>",
  "action": "investigate",
  "confidence": 0.85,
  "evidence": ["region-a: model identified sustained degradation"],
  "model_id": "my-model-v1"
}
```

The example values are illustrative. Allowed actions: `hold`, `investigate`, `shift_traffic`, `scale_out`. For `shift_traffic`, include an integer `target_region_a_percent` in [0,100]. The current reviewer in `services/adaptivenet/ingestion/decisions.py` validates version, snapshot ID, confidence, evidence, health/capacity and bounded route-weight delta, but **never executes** any action. `scale_out` is explicitly unimplemented.

Test shadow validation against the **actual** stored snapshot:

```bash
python scripts/model_handoff.py input --mode combined --out data/model-input.json
python scripts/model_handoff.py demo --mode combined

# Once the ML team writes an actual proposal JSON (same snapshot_id):
python scripts/model_handoff.py review --proposal data/my-proposal.json
```

The `review` command returns `approved_for_execution=false`, `executed=false` and `effective_action=hold` regardless of whether the proposal passes validation. There is **no** current online inference scheduler wired into the monitor: a teammate must build or explicitly run model inference before model output appears in any observer UI.

## 8. Training and evaluation protocol

**Minimum reported comparisons:** (A) conservative rule baseline; (B) trained AMP-only model; (C) trained AMP+RIPE model; (D) optional controlled CloudFront/global-signal augmentation. Use matched, identical fault test sets and horizons.

- Partition by `experiment_run_id` and time; never randomly split overlapping snapshots/windows from the same fault run. Report the number of independent incidents, not only snapshot count.
- Fit scalers/imputers/class balancing and tune thresholds only on training/validation. Freeze all choices before final test; use only features actually available at the prediction time.
- Report precision, recall, F1, false alarms/hour, negative controls, detection delay, lead time if prediction is feasible, class imbalance, and missingness/RIPE quorum coverage.
- Measure under SG outage, VA outage, healthy baseline and recovery, including expected differences between direct origin, CloudFront and RIPE ICMP.
- Distinguish *model inference latency* from *observation publication delay*, *CloudFront failover time*, and *actual service recovery time*. Do not use global CDN error rates as a per-origin ground-truth label.
- Provide a reproducible experiment manifest and exact code/config/model version for each result. Where labels are ambiguous, report them explicitly instead of silently assigning healthy.

## 9. Suggested code ownership and PR deliverables

| Team | Own these | Coordinate before changing |
|---|---|---|
| Telemetry/data | `services/adaptivenet/ingestion/{sources,store,features,handoff}.py`, AWS/RIPE config | Any changes to the frozen v1 contract or units |
| Prediction/ML | `ml/`, model artifacts/config, preprocessing, training/evaluation scripts/tests, labeled-run loader | Proposal schema, run-manifest schema and dashboard overlays |
| Controller | `services/adaptivenet/ingestion/decisions.py` and later standalone actuator | Active AWS routing/scaling |
| Interface | `apps/dashboard`, `apps/demo-web` | Predictions overlay and experiment status presentation |

**PR 1 acceptance checklist:** can read supplied JSONL without AWS; outputs deterministic feature matrix/masks; explicitly separates AMP-only vs combined features; joins only verified fault labels; reports independent evaluation; emits schema-compliant `predict()` proposal; passes tests; does not call mutating AWS APIs. Add tests for stale/null data, wrong snapshot ID, missing RIPE quorum and service-down cases.

**Keep the root README focused on the overall project.** Put modelling design, evaluation and future changes in this handoff README and ML-specific docs/PRs. The project should never claim SQS queue processing, EKS HPA, automated mitigation or trained prediction until those have been separately built and verified.

## 10. Questions for the telemetry owner before final evaluation

Agree on: the target outcome and horizon; the list and timing of planned fault types; who will verify/review real incident manifests; minimum number of distinct runs; whether global CloudFront and witness features are allowable; whether the goal is classification, anomaly detection, early detection or true ahead-of-time forecasting; what constitutes a successful recommendation; and which artifact format the model must return. Until resolved, keep ML recommendations in **shadow mode** and document those choices in the evaluation report.
