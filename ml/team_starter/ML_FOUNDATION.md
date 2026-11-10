# AdaptiveNet — offline ML foundation (shadow only)

## What changed and why

All files below stay in `ml/`. No AWS, Terraform, Kubernetes, SQS, ingestion,
SQLite schema, dashboard, or working telemetry files were changed.

| File | Purpose |
|---|---|
| `ml/team_starter/archive_watch.py` | Continuously schedules existing, atomic, idempotent read-only archive snapshots (10s default). Must run during experiments. Cannot restore data already rotated/pruned. |
| `ml/team_starter/verified_labels.py` | Strict reviewed evidence schema; unknown/unverified periods remain unlabeled. |
| `ml/team_starter/dataset.py` | Rebuilds historical `model_input.v1` from archived real snapshots, as-of joins actual HTTP witness completions, masks future-dated sources, projects three leak-resistant arms. |
| `ml/team_starter/evaluation.py` | Chronological split of complete experiment blocks with embargo, fixed rule baseline and incident metrics. |
| `ml/team_starter/training.py` | Regularized logistic regression with train-only imputation/scaling and explicit missingness masks, only if enough independently reviewed episodes exist; writes local model + report. |
| `ml/team_starter/shadow.py` | Opt-in local model inference, safe hold/investigate proposal.v1, calls existing independent reviewer. |
| `ml/team_starter/offline_cli.py` | Minimal reproducible workflow. |
| `ml/predictor.py` | Same `predict(model_input: dict) -> dict`; unchanged default rule path; opt-in `ADAPTIVENET_SHADOW_MODEL` local trusted artifact. |
| `ml/team_starter/tests/test_offline_foundation.py` | TEST-ONLY fabricated fixtures for safety and contracts; not training evidence. |

**Do not copy archived JSONL, raw datasets, local SQLite files, `.env`, secrets,
or saved local pickles into Git.** Keep outputs under ignored `data/`.

## 1. Install and test (Windows PowerShell from repository root)

```powershell
python -m pip install -e ".[dev,ml]"
python -m pip install -r ml/team_starter/requirements-ml-inference.lock
python -m pytest -q
python -m unittest discover -s ml/team_starter/tests -v
```

## 2. Preserve real measurements continuously

Start the *existing* telemetry/witness services as your team normally does.
The ML process runs separately and performs READ-ONLY polling:

```powershell
python -m ml.team_starter.archive_watch --db data/adaptivenet-ingestion.db --witness data/probe-events.jsonl --out data/ml-archive --interval 10
```

Run this in a dedicated terminal for the duration of your experiments. Archive
writes use atomic replacement and fsync. It is not guaranteed lossless across
process outages, abrupt journal rotation, or the upstream database's retention
window; log collection gaps and exclude unsupported intervals from evaluation.
This command does not launch probes or execute fault injections.
Only one watcher may use a given output folder: an exclusive lock prevents
accidental concurrent overwrite. On an unclean crash, verify no watcher is
running before removing the `.ml_archive_watch.lock` file.

## 3. Verify and label real experiment episodes

Copy `ml/team_starter/examples/verified_episodes.template.json` to
`data/verified_episodes.json` and replace placeholders using actual, documented
evidence. For each event record:

- Unique episode ID and **explicit `experiment_run_id`** shared across every phase of one run; `region-a`, `region-b`, or `both`.
- `label` = `healthy`, `application_failure`, `network_degradation`,
  `partial_outage`, `transient`, or `recovery`.
- **Verified**, time-zoned start and end; a request for injection is NOT onset.
- Reviewer and review timestamp; evidence record references; `reviewed: true`
  only after someone checked the underlying experiment/witness/service proof.
- `partial_outage` needs at least two *independent* networks/vantage points;
  today's single-network HTTP witness is insufficient to verify this class.
- Split transitions into separate time intervals rather than assigning multiple
  contradictory labels to the same region/time. Do not label missing intervals
  as healthy.

The template intentionally has `reviewed: false` and invalid timestamps. It
CANNOT accidentally be trained as if it were a genuine incident.

## 4. Build the offline dataset and three ablations

```powershell
python -m ml.team_starter.offline_cli dataset --snapshots data/ml-archive/feature_snapshots.jsonl --witness data/ml-archive/http_witness.jsonl --labels data/verified_episodes.json --out data/ml-archive/verified-dataset.json
python -m ml.team_starter.offline_cli train --dataset data/ml-archive/verified-dataset.json --group all --out data/ml-results
```

The trainer requires an explicit experiment run ID on every reviewed episode,
counts distinct **runs** rather than phases when checking per-class support,
and groups whole run windows before chronological splitting. An older v1
manifest without run IDs can be inspected, but is **not training eligible**.
The trainer always writes `data/ml-results/evaluation-report.json`. If verified
training evidence is inadequate, it outputs `not_trained` and precise blockers;
exit code 2 means no trained model. **Do not claim accuracy on insufficient data.**

AWS-only = 13 columns per region (26 numeric features before mask columns).
AWS+RIPE = 17 per region (34). AWS+RIPE+HTTP = 34 plus 30 fast HTTP
columns across the three monitored scopes. All arms use the **same held-out
episode split**. Internal `mode=internal_only` does NOT remove RIPE values:
these projections do. No RIPE provenance, region comparison or global CloudFront
values enter AWS-only features.

Source timestamps are checked as of the immutable snapshot timestamp; a
future-dated source measurement is masked rather than used. Witness outcomes
are included only when their request **completed** by decision time. Derived
EWMA features are attested by the existing snapshot creator, but archived
snapshots do not carry per-metric `collected_at` timestamps. Thus full
independent proof of every upstream receipt time requires the underlying
observation log; the pipeline reports this limitation instead of inventing
receipt times. It never interprets repeated snapshots of one cached RIPE
measurement as independent probe observations. Because the fixed 17-column
contract is a point-in-time summary, this foundation **does not yet implement
a new temporal RIPE sequence model** or claim to prove sustained outages from
cached RIPE values; that requires additional distinct timestamped measurements
and a versioned sequence-feature/inference contract.

**Evaluation:** detection (forecast horizon = 0), macro/per-class F1,
precision, recall, coverage, abstentions, false alert transitions per 24h of
observed healthy control, missed incident episodes, median/p95 detection delay
from verified onset and multiclass Brier score. Small test sets mean results
will be uncertain: also report episode counts, probe cadence, class balance,
incident diversity, confidence distributions and evaluation limitations.

## 5. Demonstrate shadow-only inference and independent review

`model_id` is returned in `data/ml-results/evaluation-report.json`. Choose a
model artifact created locally by this trainer, **not a downloaded pickle**.
Use a *current* real `model_input.v1` from the existing handoff; historical
export rows must abstain due to live age checks:

```powershell
python scripts/model_handoff.py input --mode combined --out data/model-input.json
python -m ml.team_starter.offline_cli shadow --input data/model-input.json --model data/ml-results/<MODEL_NAME>.pkl --snapshot data/current-feature-snapshot.json --out data/shadow-proposal.json
```

The `--snapshot` argument is mandatory: provide the **matching original**
feature snapshot (same `snapshot_id`) to invoke the independent reviewer.
The reviewer MUST return `policy_valid: true`, `approved_for_execution: false`, `executed: false`,
`effective_action: hold`. No AWS or Kubernetes write permission is used.

The model-produced proposal is untrusted until the independent reviewer
accepts its schema and snapshot ID; standalone `predict()` never grants
execution authority.

The standard runtime interface is still `ml.predictor.predict(model_input)`.
To opt in **only for shadow proposals**, set:

```powershell
$env:ADAPTIVENET_SHADOW_MODEL = "data/ml-results/<MODEL_NAME>.pkl"
```

Unset it to use the original conservative baseline. A future EKS inference
service, ECR image and API/ClusterIP/Helm deployment can wrap this read-only
entry point after security and integration approval; those are **not deployed**
by this offline deliverable.

## 6. Research interpretation and pending evidence

- The conventional HPA baseline reportedly reached 6/6 pods from 2/2 in both
  regions with a 60% CPU target and 0 restarts; this is **not** ML evidence.
- Need verified healthy windows, short transients, sustained delay/loss,
  service-down episodes, partial-path failures and recovery **with real data**.
- Need independent outcome records rather than injection requests, confirmed
  timestamps and enough distinct episodes **per class in training and test**.
- RIPE's remote measurement cadence can be around 900s, slower than 5s HTTP;
  re-fetching the same RIPE observation cannot prove a 5-minute outage.
- The present HTTP witness is **one network vantage**; even severe direct HTTP
  failures there do not prove a widespread regional outage.
- CloudFront can stay HTTP 200 by failing over; check each origin separately.
- Do not use test fixture data for any report. Model metrics must come from
  independently verified actual episodes and untouched held-out windows.
- The offline pipeline measures *detection*. Advance forecasting can only be
  claimed after a separate pre-declared future horizon, adequate precursor
  evidence, and prospective holdout validation.
