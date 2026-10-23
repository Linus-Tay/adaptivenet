# ML → Controller handoff

**Read this first:** the telemetry pipeline is working in a local dev environment. It is not a trained predictive model or a production-grade failover controller.

## October 2026: live experiment and dataset handoff

The data pipeline now fuses **regional AWS Managed Prometheus + external RIPE Atlas + global CloudFront CloudWatch** observations. The latter is **global viewer/CDN data**, not independent SG or VA application measurements. CloudFront`.last_known` is historical **UI-only**; it is **not passed into the versioned model input**.

Three surfaces are intentionally different:
- `:3001` *workload viewer*: real CloudFront + SG/VA API requests, coffee catalogue, HTTP outcomes and origin evidence; no fault controls.
- `:8766` *observer*: local read-only feature snapshots, CloudFront/AMP/RIPE and independent HTTP witness journal.
- `:3000` *operator*: manually authorized fault button targeting allowlisted EC2, available in local AWS mode; does not call a trained model.

**Modelling export commands** (from activated Python virtualenv and project root):

~~~bash
python scripts/model_handoff.py input --mode combined --out data/model-input.json
python scripts/model_handoff.py input --mode internal_only --out data/internal-model-input.json
python scripts/export_ml_dataset.py --mode combined --limit 500 --out data/ml-snapshots.jsonl
python scripts/model_handoff.py demo --mode combined
~~~

The JSONL dataset records **real snapshot timestamps, the exact source features, missing masks and provenance**. Its `fault_label` and `label_source` fields are **intentionally null**. You must generate explicit run labels after independently verifying that SSM actually stopped the application, when the impairment started, when the service recovered, and whether CloudFront failed over. A submitted SSM command or a browser click **does not establish ground truth**. Pair fault and nonfault intervals, and time-split by experiment/run, not random snapshot rows.

**Data limitations:** RIPE Atlas ICMP samples are relatively sparse and can stay healthy while an application service is stopped; CDN request metrics can stop publishing during quiet traffic; Prometheus `http_p95_ms` may be null if histogram buckets are unavailable. Missingness, delayed publication and source timestamps are scientifically relevant. Do not treat stale as zero or use a future recovery outcome as a predictive input.

**SQS and Kubernetes scope:** the optional `live_monitor.py --sqs` path now publishes existing source polls through a durable outbox and standard SQS into an independent SQLite feature consumer. Direct SQLite remains a fallback. **This is not yet an EKS-hosted ingestion service** and live SQS permissions/deployment are unverified. Both paths retain the same model-input contract. EKS/HPA and the automatic mitigation adapter remain for the controller/infrastructure team; establish an HPA baseline before comparing ML scaling. See [SQS operations](SQS_INGESTION.md).

## One-minute diagram

```text
AWS AMP / RIPE Atlas
        ↓ sources.py          ← provider fetch + normalize
Observation (frozen dataclass)
        ↓ store.py            ← SHA256 event id + SQLite WAL
Raw events with source timestamps
        ↓ features.py         ← freshness, probe quorum, EWMA, deltas
FeatureSnapshotV1
        ↓ handoff.py          ← mask nulls, cap snapshot age, select mode
ModelInputV1
        ↓ YOUR predict(input)  ← trained model / rule baseline
ModelProposalV1
        ↓ decisions.py        ← independent safety review
DecisionReviewV1 (ALWAYS shadow / hold)
        ↓ future actuator     ← NOT BUILT, NOT ENABLED
```

## Existing data structures (why they matter)

| Location | Structure | Reason |
|---|---|---|
| `models.py` | `@dataclass(frozen=True) Observation` | Validated, immutable source event with value, unit and timestamps |
| `models.py` | SHA-256 `event_id` | The same fetched RIPE result hashes identically; prevents duplicate inserts |
| `features.py` | `defaultdict(list)` keyed by `(region, metric)` | Fast average-case O(1) partitioning; independent regions/series |
| `features.py` | `deque(maxlen=120)` | Memory-bounded rolling EWMA, no ever-growing list |
| `features.py` | dict keyed by Atlas `probe_id` | Take newest result per probe; repeated polling doesn't bias RTT |
| `store.py` | SQLite `PRIMARY KEY(event_id)` + B-tree indexes | Atomic deduplicated journal and bounded time-window retrieval |
| `handoff.py` | ordered dict + missingness map | Reproducible column order, explicit `null` vs `0`, no training leakage |

## Contracts and units

- **Raw event** `Observation`: `region`, `source`, `metric`, `value`, `unit`, `observed_at` (source UTC seconds), `collected_at` (collector UTC seconds), `source_id`, `labels`. `event_id` derived from identity + timestamp + value, not retrieval time.
- **Features** `adaptivenet.features.v1`: `snapshot_id`, `computed_at_unix`, `regions`, `comparison`, `quality`, `provenance`, readiness flags. Source data is not re-timestamped.
- **Model input** `adaptivenet.model_input.v1`: `mode`, `snapshot_id`, `ready_for_inference`, `age_seconds`, `features`, `missing`, `quality`, `comparison`, `provenance`. `missing[region][feature]=true` when value is `null`.
- **Proposal** `adaptivenet.proposal.v1`: `snapshot_id`, `action`, `confidence`, optional `target_region_a_percent` and `evidence`.
- **Review** `adaptivenet.decision_review.v1`: `policy_valid`, `rejection_reasons`, `eligible_for_operator_review`, `approved_for_execution=false`, `executed=false`, `effective_action=hold`.

**Units:** `%` for CPU / memory / packet loss, **milliseconds** for p95 and RIPE RTT, requests/sec for RPS, fraction `[0,1]` for HTTP error ratio and model confidence. Never confuse RTT with HTTP duration.

## Working example for ML team

```python
from adaptivenet.ingestion.handoff import build_model_input
from adaptivenet.ingestion.decisions import review_proposal

# Load a REAL latest_snapshot() from Store; do not pass fabricated samples.
model_input = build_model_input(snapshot, mode="internal_only")
if model_input["ready_for_inference"]:
    proposal = predict(model_input)  # your own model, returns adaptivenet.proposal.v1
    decision = review_proposal(snapshot, proposal)
    # decision always says executed=False; never call AWS from a predictor.
```

A model callback can return:

```json
{
  "contract_version": "adaptivenet.proposal.v1",
  "snapshot_id": "<exact model_input.snapshot_id>",
  "action": "investigate",
  "confidence": 0.89,
  "evidence": ["region-a: sustained CPU pressure across observed windows"]
}
```

For the current **internal-only** mode, proposal validation still requires combined-data readiness before considering shifts, so proposals should stay in **shadow mode**. This intentionally prevents unvalidated AWS-only failover.

## ML evaluation rules

1. Treat one snapshot as an observation, **not** a label. Generate ground truth from experiment manifests with start/end time and injected fault cause.
2. Split train/validation/test by **time and experiment run**; overlapping 10-minute windows are not independent.
3. Keep nulls and binary masks: missing RIPE is not zero packet loss; missing p95 is not 0 ms.
4. Compare internal-only and combined only on appropriate matched windows; report sample counts and signal coverage.
5. Report precision, recall, false-positive rate, detection delay, intervention delay, recovery time, and mitigation side effects. Include a no-action baseline.
6. Ensure the feature cutoff precedes the ground-truth outcome; no post-fault future features in pre-fault forecasts.
7. Calibrate confidence; confidence is not independently verified health.

## Controller safety rules

- **Model proposes; controller independently verifies.** A prediction is untrusted input.
- Verify exact snapshot ID, contract version, finite 0–1 confidence, evidence and live snapshot age.
- Never select a target whose current internal health or capacity is unknown.
- Existing review limits single route-weight delta to 25 percentage points and rejects scale-out as unimplemented.
- Shadow decisions never call `route53`, `kubectl`, `terraform` or mutating AWS APIs.
- Before future live control: independently read routing state, enforce cooldown/consecutive breaches, canary steps, authz, idempotency, rollback and full audit trail.

## Work distribution

| Team | Modify | Don't modify |
|---|---|---|
| Data | `sources.py`, `features.py`, `store.py` with tests | AWS identities/state without review |
| ML | `ml/`, their new predictor module | Terraform or production Route 53 |
| Controller | `decisions.py`, new shadow journal/controller module | Training assumptions or unchecked model output |
| UI | `apps/dashboard/`, `packages/contracts/` | Secrets or direct AMP access in browser |

See [DATA_DICTIONARY.md](DATA_DICTIONARY.md) for names, types, freshness and null handling.
