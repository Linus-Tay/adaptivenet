# AdaptiveNet — Multi-Region Resilience Laboratory

**Real AWS telemetry + bounded experiments + ML-ready features.** AdaptiveNet monitors two regional EC2 catalogue APIs through AWS Managed Prometheus (AMP), RIPE Atlas, and CloudFront CloudWatch. A collector can publish timestamped telemetry polls through SQS to a separate SQLite feature worker (with direct-SQLite fallback); three *separate* interfaces make it possible to see the workload, observe metrics, and control authorized experiments.

> **Current stage (October 2026):** CloudFront is deployed, both regional API origins and AMP/RIPE data have been observed live, and six CloudFront metrics have been collected. SQS producer/consumer code now exists, but live SQS deployment/access still needs verification. EKS/HPA, trained ML and automatic mitigation are not running. See the status table.

## 1. What runs and how it works

~~~text
Clients / local witness and workload test (:3001)
          │ HTTP GET /api/catalog
          ▼
CloudFront CDN (HTTPS, SG primary → Virginia origin failover)
          │                                  │
     SG EC2 /api/catalog               VA EC2 /api/catalog
          │                                  │
     AMP Prometheus/node metrics       AMP Prometheus/node metrics
          └────────────────┬─────────────────┘
        RIPE Atlas ICMP probes + CloudFront global CloudWatch
                           │
     Existing source collector (every 120s)
                           │
    SQS mode: local outbox → SQS → detector
                           │
          SQLite WAL: deduplicated events + snapshots
        (direct SQLite is still a fallback)
                           │
       GET-only observability UI :8766 / JSON APIs
                           │
         model_input.v1 → future trained model
                           │
       proposal.v1 → independent shadow reviewer
                           │
            live mitigator NOT IMPLEMENTED

SEPARATE OPERATOR :3000 (localhost only)
    └─ manually confirmed EC2 service-stop fault via
       AWS SSM, with restoration timer scheduled first
PUBLIC WORKLOAD TEST :3001
    └─ real CloudFront/SG/VA API reads; no fault controls
~~~

| Interface | URL | Purpose |
|---|---|---|
| **Network Observatory** | http://127.0.0.1:8766 | Read-only CloudFront, regional Prometheus, RIPE, history, data-quality gates, HTTP witness |
| **Experiment Operator** | http://127.0.0.1:3000 | Real AWS fault button for the fixed demo instances; local simulator controls in local mode |
| **Workload Test** | http://127.0.0.1:3001 | CloudFront-served catalogue and direct SG/VA responses, latency, origin and request timeline |
| **Deployed backend** | https://d2zsecrmlm6eiu.cloudfront.net/api/catalog | CloudFront HTTPS API with SG primary and VA failover |

The CloudFront domain is **not** hosting the Next.js pages. Those are local research UI processes. The observer does not inject or mitigate faults. Closing the Mac stops local ingestion, not AWS or RIPE.

## 2. What is verified, defined, and unfinished

| Component | Current status | Gap |
|---|---|---|
| CloudFront | Distribution `E3BJ0HVVT3JBMT` deployed and previously returned HTTP 200 from Singapore | Real Virginia failover/recovery still needs a verified experiment |
| Prometheus | SG/US AMP CPU, memory, app signals previously fresh | Optional catalogue p95 histogram can return `no_series` |
| RIPE Atlas | Previously 3 SG + 5 VA live ping probes | Confirm current IDs, target and freshness before experiments |
| CloudFront metrics | Six standard CloudWatch metrics previously observed in `us-east-1` | Quiet traffic means no new per-minute points; live reading expires after 15 min |
| Ingestion worker | Direct SQLite or SQS-backed source polling → detector → SQLite | Cloud SQS runtime and IAM not verified |
| Continuous HTTP witness | Implemented, **off unless `--traffic`**; logs real per-endpoint status/latency | Witness data is not the same as RIPE or AMP data |
| AWS fault injection | Fixed tagged-EC2 SSM service-stop command, 10–60s, restore-timer-first; button is available in local AWS mode | Live SSM execution and automatic recovery still require verification |
| SQS + DLQ | SQS/DLQ Terraform, producer, consumer, outbox, tests, IAM policies and alarms | Verify queue and permissions live; EKS hosting later |
| EKS / Helm | Terraform/Helm scaffolding exists; `enable_eks=false` default | Cluster deployment, HPA, node scaling, ingress and evaluation unfinished |
| Route 53 | Weighted DNS/health-check Terraform exists, off by default | No model-controlled route-weight adapter |
| ML / controller | Feature and proposal contracts + rule baseline + shadow safety reviewer | Train/evaluate predictive model and later build audited actuator |

**CloudFront, RIPE and AMP are independent.** CloudFront viewer metrics are *global*; AMP is per-region; RIPE measures ICMP network reachability (an HTTP service failure can leave RIPE ping healthy). This difference should be recorded as experimental evidence, not treated as a broken sensor.

## 3. Installation: choose the right Git checkout

For an existing Git working tree, first run `pwd`, `git rev-parse --show-toplevel` and `git status --short`. Don't assume any particular Downloads folder still exists. If local modifications block checkout, stash or commit **only your own changes**; do not use `git reset --hard`.

Fresh clone:

~~~bash
git clone https://github.com/Linus-Tay/adaptivenet.git
cd adaptivenet
git fetch origin
git switch --track origin/feature/sqs-telemetry-ingestion
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]' awscurl certifi
npm ci
aws sso login --profile adaptivenet-admin
export AWS_PROFILE=adaptivenet-admin
export SSL_CERT_FILE="$(python -c 'import certifi; print(certifi.where())')"
python -m pytest -q
npm run build
npm run typecheck
~~~

On an existing checkout, use `git switch feature/sqs-telemetry-ingestion` then `git pull --ff-only` (or main once the PR is merged). In each new terminal, activate the virtualenv before invoking `python`. The Python.org macOS CA store can require a certifi bundle: **never disable TLS verification**.

### Local config (ignored by Git)

Keep an existing `.env`. Only create it when absent: `test -f .env || cp .env.example .env`, then `chmod 600 .env`. The values below are the **previously working** configuration, not a guarantee that RIPE measurements are still active:

~~~dotenv
ATLAS_MEASUREMENTS=region-a=222874954,region-b=222874912
CLOUDFRONT_DISTRIBUTION_ID=E3BJ0HVVT3JBMT
~~~

Use `python scripts/ripe_measurements.py status` to verify targets and current results. When cloning measurements, `python scripts/ripe_measurements.py set --region-a NEW_ID --region-b NEW_ID` updates just the IDs with a backup. No RIPE API secret is needed to read public results; `ATLAS_API_KEY` is only for creating new measurements. Shell-exported `ATLAS_MEASUREMENTS` overrides `.env`; `unset ATLAS_MEASUREMENTS` if necessary. AWS SSO profiles and RIPE API keys are unrelated.

**Do not redeploy CloudFront** merely to monitor it; the distribution already exists and its Terraform state is isolated under `infra/terraform/environments/cloudfront`. Any Terraform change requires review.

## 4. Run the real continuous experiment environment

From the **same checkout**, after stopping other servers on ports 8766/3000/3001:

~~~bash
source .venv/bin/activate
export AWS_PROFILE=adaptivenet-admin
export SSL_CERT_FILE="$(python -c 'import certifi; print(certifi.where())')"
python scripts/ripe_measurements.py status
python scripts/cloudfront_doctor.py --lookback-minutes 180
python scripts/live_monitor.py --sqs --traffic --with-ui
~~~

`live_monitor.py --sqs` supervises: (1) real AMP/RIPE/CloudWatch polling every 120s, a durable SQS producer and an independent SQLite detector consumer; (2) read-only observer on 8766; (3) **explicitly opted-in** one GET/minute to CloudFront + each EC2 origin, saved in `data/probe-events.jsonl`; (4) separate workload page on 3001; and (5) separate operator page on 3000. Ctrl+C stops these children. The `--traffic` flag generates actual chargeable HTTP requests but **never faults**, and may help maintain CloudFront request samples. Without `--traffic`, there is no witness generator; idle CloudFront live samples eventually expire and their last real values are shown amber as **STALE**.

Run `python scripts/live_monitor.py` for **telemetry + observer only**; omit `--with-ui` and `--traffic` if those components are not wanted. If SSO expires, run `aws sso login --profile adaptivenet-admin` again.

### Real fault injection via the operator button

The existing AWS-mode operator is independent of SQS. No separate UI arming flag is needed. After confirming the origins are responding, open localhost:3000 to choose the fixed demo region and bounded duration; the service-restoration timer is scheduled first. A submitted SSM request is **not proof** of execution or recovery. Real latency/packet-loss injection, EKS scaling and model-driven mitigation remain future team work.

## 5. Data, freshness and troubleshooting

Observer API endpoints (GET only): `/v1/features/latest`, `/v1/features/history?limit=120`, `/v1/ingestion/status`, `/v1/experiments/probes`.

| Signal | Meaning | Boundaries |
|---|---|---|
| AWS AMP | Region-level CPU/memory/app health, HTTP RPS, 5xx and p95 histogram | Approx. 180s live freshness; optional HTTP p95 can be missing |
| RIPE Atlas | Independent ICMP RTT, packet loss, 3+ probe quorum | 2100s max age; not HTTP availability |
| CloudFront CloudWatch | Global requests/4xx/5xx/total error/bytes | 900s live freshness; `last_known` retained **display-only** and excluded from ML |
| HTTP witness | Bounded real client GETs: HTTP status, origin and duration | Local journal, not Prometheus, not fake RIPE events |

**`null` is unavailable, not zero**. Source timestamps are never refreshed just because data is re-polled. An old snapshot marked READY historically is not live inference-ready: the adapter rechecks wall-clock age. The viewer's 30-second auto-refresh reads SQLite; only `watch` polls cloud sources.

~~~bash
python scripts/ingestion_doctor.py --profile adaptivenet-admin
python scripts/amp_metric_doctor.py --profile adaptivenet-admin
python scripts/cloudfront_doctor.py --lookback-minutes 180
python scripts/ripe_measurements.py status
python scripts/ingestion.py once --profile adaptivenet-admin
~~~

If a CloudFront metric vanishes from the **live input**, that is the intentional freshness rule. The observatory still displays last-known real values (and their original timestamps) as **STALE**. Do not increase freshness thresholds or fabricate measurements to fill the dashboard.

## 6. Group handoff: predictive modelling

**Train and evaluate a model now, without waiting for EKS.** The existing `ml/predictor.py` is only a conservative rule example. What is needed next is a verified labeled dataset, temporal evaluation and a model proposal—not cloud permission.

~~~bash
# Versioned, wall-clock-validated inference input:
python scripts/model_handoff.py input --mode combined --out data/model-input.json
python scripts/model_handoff.py input --mode internal_only --out data/model-input-internal.json

# Up to 500 real historical feature snapshots; NO invented fault labels:
python scripts/export_ml_dataset.py --mode combined --limit 500 --out data/ml-snapshots.jsonl

# Demonstration baseline and independent shadow review:
python scripts/model_handoff.py demo --mode combined
~~~

Tasks for modelling teammates:
1. **Verified labels:** record fault *actual start*, *actual end*, affected region, category, whether SSM command executed, and confirmed recovery. An `aws_fault_requested` event is not a ground-truth failure. Include many healthy and varied experiment windows.
2. **Feature integrity:** preserve UTC observation timestamps, per-region provenance, missingness masks and freshness; `cloudfront.last_known` is UI-only and deliberately excluded from model input.
3. **Evaluation:** use time/experiment-run-disjoint splits; compare internal-only vs combined AMP+RIPE features; report coverage, precision, recall/F1, false-positive rate, detection delay, recovery time and model confidence calibration.
4. **Contract:** implement `predict(model_input)` in `ml/`; return `adaptivenet.proposal.v1` with exact snapshot ID, action (`hold`/`investigate`/`shift_traffic`/`scale_out`), confidence and evidence. **No direct AWS calls from ML.**
5. **Shadow first:** pass proposals to `services/adaptivenet/ingestion/decisions.py`. The reviewer always sets `executed=false`. Controllers need independent health/capacity gates and authorized, audited, reversible route/scale adapters later.

Read `docs/TEAM_HANDOFF.md`, `docs/DATA_DICTIONARY.md` and `packages/contracts/src/model_handoff.ts`. Coordinate with the team's separate ML branch instead of overwriting it. A handful of idle snapshots is not sufficient for reliable supervised prediction.

## 7. Infrastructure still to build

**SQS is now an optional implemented transport.** `--sqs` replaces the direct polling path with producer → SQS → consumer → the same SQLite feature contract. The old direct collector remains a fallback; do not run both simultaneously. SQS queue existence, IAM attachment, live end-to-end delivery and EKS deployment still require verification. See [SQS ingestion setup](docs/SQS_INGESTION.md).

**EKS/HPA** Terraform/Helm scaffolding exists but cluster deployment is unverified and `enable_eks=false` by default. First implement conventional Kubernetes resource requests, Metrics Server, HPA and node scaling as a **non-ML baseline**. You do not need a trained model to set that up.

**Route 53** weighted records and health-check Terraform exist but `enable_route53=false`; no safe live route-weight adapter is connected. CloudFront's SG-primary/VA-failover behavior is not a 50/50 weighted DNS experiment.

After model evaluation, design a separately authorized controller with idempotency, cooldown, max change per step, destination health/capacity verification, before/after snapshots, monitoring and rollback tests. Neither the observer nor the predictor changes AWS.

## 8. Safety, checks and code ownership

| Area | Main code |
|---|---|
| Telemetry | `services/adaptivenet/ingestion/`, `scripts/ingestion.py`, `scripts/live_monitor.py` |
| CloudFront/real witnesses | `scripts/cloudfront_doctor.py`, `scripts/continuous_probe.py` |
| Faults | `scripts/aws_fault.py`, `apps/dashboard/` (local, opt-in) |
| Workload | `apps/demo-web/` (read-only GET against real endpoints) |
| ML | `ml/`, `scripts/export_ml_dataset.py`, `scripts/model_handoff.py` |
| Safety/infra | `services/adaptivenet/ingestion/decisions.py`, `infra/terraform/` |

Run `python -m pytest -q`, `npm run build`, `npm run typecheck`, `npm audit --omit=dev`, `git diff --check` and `python scripts/check_push_safety.py --staged` before pushing. CI is enabled on the feature branch. Do not commit `.env`, SQLite databases, AWS credentials, Terraform state or unreviewed experiment data. Node dependency vulnerabilities require deliberate investigation before any public deployment; never blindly run a forced audit upgrade. The currently deployed CloudFront origins use **HTTP to EC2** (not end-to-end TLS), another reason this remains research-only.

**Validation boundary:** passing local tests and cloud diagnostics does not prove an SSM fault, successful origin failover, reliable service recovery, or an active SQS consumer. Capture and publish those as distinct experimental evidence.
