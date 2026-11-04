# AdaptiveNet ML — architecture & contract boundaries

## End-to-end flow

~~~mermaid
flowchart TD
  U[Client] --> CF[CloudFront global CDN]
  CF --> SG[Singapore origin]
  CF --> VA[Virginia origin]
  SG --> AMP[AWS Managed Prometheus]
  VA --> AMP
  AMP --> C[Docker source collector]
  RIPE[RIPE Atlas ICMP RTT/loss] --> C
  CW[CloudWatch global CDN metrics] --> C
  C --> OUT[Durable local outbox]
  OUT --> SQS[AWS SQS]
  SQS --> F[Docker feature worker]
  F --> DB[(SQLite WAL feature snapshots)]
  W[Native 5s HTTP witness one network] --> J[(Local bounded witness journal)]
  DB --> INPUT[Read-only detection_input.v1]
  J --> INPUT
  INPUT -. future offline archive .-> LAB[Verified episodes and as-of joins]
  LAB --> A[AWS-only]
  LAB --> B[AWS plus RIPE]
  LAB --> H[AWS plus RIPE plus HTTP]
  A --> E[Same held-out episode evaluation]
  B --> E
  H --> E
  E -. trained shadow inference .-> P[proposal.v1]
  P --> REVIEW[Independent review: executed false]
  REVIEW -. not connected .-> ACT[Future authorized mitigation controller]
~~~

## Source rates and data meaning

- HTTP witness target ≈5s in the local Mac kit (not guaranteed five-second detection). The remote PR #19 was written for a slower cadence.
- AMP/CloudWatch and RIPE source fetch ≈60s. RIPE's underlying remote measurement can be ≈900s, with cached results. Repeated REST fetches are not fresh independent observations.
- CloudFront CloudWatch is global, delayed and optional to regional readiness. Never treat it as per-region latency or promote stale last_known UI values into live ML features.
- The default Mac witness is **one network vantage**, not proof of a widespread outage.

## Existing stable contracts

- services/adaptivenet/ingestion/features.py: adaptivenet.features.v1 snapshots.
- services/adaptivenet/ingestion/handoff.py: adaptivenet.model_input.v1; 17 ordered columns for both regions; null masks, source provenance, freshness.
- services/adaptivenet/ingestion/fast_signals.py: adaptivenet.fast_signals.v1, 60/300/900-second HTTP windows.
- GET /v1/detection/input: adaptivenet.detection_input.v1 slow/fast join (NO trained model execution).
- ml/team_starter/feature_views.py: strict AWS-only (13 internal columns ×2), AWS+RIPE (+4 columns ×2), AWS+RIPE+HTTP (add versioned fast-signal windows).
- ml/predictor.py: safe existing baseline, teammate should keep predict(model_input)->adaptivenet.proposal.v1.
- services/adaptivenet/ingestion/decisions.py: independent policy review always approved_for_execution=false and executed=false.

**Critical:** model_input.mode=internal_only only changes the readiness gate; it does NOT remove RIPE features. The training pipeline must use explicit feature selection and prevent RIPE/comparison/provenance leakage into AWS-only.

## Evaluation and safety

Build actual, independently verified healthy and incident manifests. Evaluate failures, short transients, service-stop, latency/packet-loss, partial outages and recovery with the same test episodes across models. Split by entire episodes and chronologically, with an embargo; never mix adjacent windows across train/test. Enforce an as-of cutoff at inference time: observations collected later cannot be training features for earlier decisions.

Metrics: per-class precision/recall/F1, false alarms per day, misses, median/p95 detection delay, coverage/abstentions, calibration, and explicitly defined forecasting horizon. Don't report accuracy before real labels. A fault command/request is NOT a ground-truth outage.

Keep prediction shadow-only. Any future EKS/Helm/HPA deployment, Route 53/CloudFront traffic shift, cooldowns, live destination checks, rollback and independent outcome verification belongs to a separate audited controller.
