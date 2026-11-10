# Paired native incident comparison (ML-only)

This optional tool complements `evaluation.py` without changing AWS ingestion,
cloud infrastructure, feature contracts or the independent decision reviewer.
Its purpose is **the same test episode and snapshot IDs in all three arms**:
`aws_only`, `aws_ripe`, and `aws_ripe_http`.

## Prerequisites

A dataset generated from **genuine, independently reviewed** incident episodes,
with explicit `experiment_run_id` for each run and enough time-disjoint blocks
for an embargoed train/validation/test split. A saved demonstration or synthetic
unit test fixture is not native detection performance. Each classifier must
have `status=trained`. No label or incident is inferred from metrics.

## Commands (from repository root)

```powershell
python -m ml.team_starter.offline_cli train --dataset data/ml-archive/verified-dataset.json --group all --out data/ml-results/native
python -m ml.team_starter.offline_cli compare --dataset data/ml-archive/verified-dataset.json --predictions-dir data/ml-results/native --out data/ml-results/native/paired-incident-report.json
```

Training now saves `test-predictions-aws_only.json`,
`test-predictions-aws_ripe.json`, and `test-predictions-aws_ripe_http.json`
**only if each corresponding native classifier actually trained**. These
artifacts contain held-out IDs, labels, timestamps and predictions, never raw
measurements or credentials. Both commands fail closed with exit code `2` when
there are no eligible native labels or predictions. `compare` still writes an
explicit `not_evaluable` JSON report when an arm is absent.

## Metrics and interpretation

- A fault detection is the first nonhealthy *alert class* in a reviewed positive
  episode (application failure, network degradation, partial outage, transient).
  Exact-class detection is counted separately.
- The detection delay for a detected episode is first alert timestamp minus
  the reviewed episode onset; misses are kept as misses, not imputed as delays.
- Pairing requires **identical** held-out episode, region, snapshot, timestamp,
  truth-label and experiment-run keys across all three arms. Any mismatch
  raises an error, rather than silently comparing different cohorts.
- Paired delay improvement is AWS-only delay minus the external-arm delay
  **among episodes detected by both models**. External-only and AWS-only-only
  detections are reported separately; the median on jointly detected episodes
  alone does not establish one model is better on all incidents.
- False alert rates use only the measured span of verified healthy episodes.
  A single short healthy interval does not establish a reliable 24-hour rate.
- The current task is condition detection with horizon `0`, **not forecasting**.
  These outputs do not establish Internet-wide behavior: the local HTTP witness
  offers only one network vantage, and RIPE data can be much slower.

## Historical availability limitation

The current offline builder already masks per-column future-dated AWS source
observations and future-dated RIPE provenance. The original upstream snapshots
do **not** export each metric's exact result-receipt time. They are constructed
after the synchronous live poll, but timestamp verification of individual
metric receipts is not possible for arbitrary raw-history replay using only
`observed_at` and poll-start `collected_at`. Preserve this limitation in the
report; do not claim strict per-metric receipt-time verification or silently
manufacture missing availability timestamps.

## Validation

```powershell
python -m pytest -q ml/team_starter/tests
python -m pytest -q
```
