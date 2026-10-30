# AdaptiveNet canonical architecture — 30 October 2026

This document aligns the progress presentation with what the code actually executes.
It distinguishes existing operational paths from planned ML and mitigation work.
No part of this change authorizes AWS fault injection or routing changes.

## Three layers, one purpose

**Research objective:** Does combining internal AWS health, independently observed
Internet paths (RIPE Atlas) and external HTTP user-impact evidence improve early
detection of gray failures, sustained degradation and partial outages compared
with AWS-only monitoring? Evaluate false alarms and detection delay, not accuracy
alone.

1. **OBSERVE — measurements and trustworthy features**
   - Delivery: CloudFront -> Singapore EC2 (primary) / Virginia EC2 (fallback).
   - Internal: AWS Managed Prometheus (AMP) for per-region CPU, memory, app
     health, HTTP latency, errors and load; CloudWatch for *global* CDN metrics.
   - External: RIPE Atlas RTT/ICMP loss from separate probes; real HTTP witnesses.
   - Transport: one producer -> durable disk outbox -> SQS standard queue ->
     one consumer -> SQLite observation-id deduplication and feature snapshots.
     The consumer ACKs only after persistence. Source timestamps are preserved.
   - Fast path: one bounded HTTP GET per endpoint **every 15s**, concurrently,
     stored with real UTC timestamps in `data/probe-events.jsonl`.
     `/v1/fast-signals/latest` serves actual 60s, 300s and 900s windows.
   - Corroboration path: AMP/RIPE/CloudWatch source polling every **60s** by
     default. This is only the *fetch cadence*, not the underlying RIPE ping
     measurement cadence or CloudWatch publication interval.
   - Multi-rate read-only join: `/v1/detection/input` combines the existing
     `adaptivenet.model_input.v1` snapshot and
     `adaptivenet.fast_signals.v1` without changing either stable contract.
     This `adaptivenet.detection_input.v1` is for **shadow ML research**.
     It never triggers model execution or infrastructure changes.
2. **PREDICT — data-quality gates, labeling, training and classification**
   - Independent verified healthy and fault labels; no button-click-only labels.
   - Candidate classes: healthy, transient, sustained degradation, partial
     outage, widespread *among monitored vantage points*, internal/service
     failure and insufficient evidence.
   - Rule baseline, AWS-only model, AWS+RIPE model; separate experiment for
     adding HTTP witnesses, with matched experiment-group test sets.
   - Potential rolling features: per-path baseline RTT ratios, 1/5/15-minute
     HTTP failure fraction, success p95, consecutive failed requests, distinct
     RIPE measurements, probe diversity, data age and missingness.
   - Distinct source timestamps define duration; do not infer persistence
     from repeatedly retrieving the same 15-minute RIPE result.
   - Model produces an evidence-backed, timestamped shadow **proposal**.
     Trained inference, episode labels and action policies are **not built** in
     this branch.
3. **ACT — independent safety review and outcome verification**
   - Readiness, severity, confidence, cooldown, target health and a separate
     independent safety controller gate before any mitigation.
   - Possible controlled actions: hold/investigate, HPA scale, traffic shift,
     Route 53 weight change, region failover and recovery.
   - Independent client traffic verifies whether the action improved HTTP
     success and latency; no success claim from SQS or a route-change API alone.
   - Current production automatic actions remain DISABLED.

## Why two speeds?

| Path | Sampling | Detects / corroborates | Status |
| --- | --- | --- | --- |
| HTTP witness | 15s target | first signs of degraded application service | Implemented; collector network only |
| AMP/CloudWatch source fetch | 60s default | cloud-side fault vs healthy infrastructure | Implemented; CloudWatch itself can lag |
| RIPE Atlas REST fetch | 60s default | independent ICMP path observations | Implemented; results can repeat |
| RIPE underlying measurement | configured at RIPE, often ~900s currently | real new RTT/loss per remote probe | Must inspect with `ripe_measurements.py status` |
| Model inference | on every relevant new signal, eventual target 15–30s | classify and predict | NOT yet implemented |
| Safety controller and live actuation | after validated proposal | safe recovery | NOT integrated |

**Do not claim that 15s HTTP polling automatically enables 15s gray-failure
detection.** Probe HTTP timeouts and concurrent polling impose latency. One
collector network cannot establish partial/widespread faults across ASNs.
Use ≥2 independent external witness networks for that evaluation.
Avoid running all witnesses inside the very cloud region they monitor.
RIPE REST latest may be cached for up to five minutes. RIPE streaming could
lower delivery delay but cannot increase how often each probe measures.
Increasing measurement frequency incurs RIPE credits and requires explicit
measurement changes, which this PR does not make.

## Current Docker truth

The existing `compose.yaml` and root `Dockerfile` implement a **local lab**;
previous `python scripts/live_monitor.py --sqs` ran native Python producer
and worker subprocesses. Merely having Dockerfiles and EKS/Helm manifests
does not mean SQS ingestion was deployed in containers.

**New optional Docker runtime:** `Dockerfile.telemetry` and
`compose.telemetry.yaml` containerize the *actual* producer and feature
consumer separately. They use boto3, awscurl and the AWS CLI, share the
SQLite and outbox directory with the Mac, and mount the host AWS SSO config
and cache read-only. Docker Compose loads nonsecret infra settings from the
local `.env` into selected environment variables; it does not mount the
potentially sensitive whole `.env` into either container.

The Docker setup is **not claimed verified on your Mac or AWS** until you
run the container smoke test below. EKS, Helm deployment, multi-replica DB
and managed worker IAM remain later platform tasks.

## One authoritative runtime at a time

Before switching, stop the previous `live_monitor.py --sqs` with Ctrl+C.
**Do not** run native SQS producer/consumer concurrently with Docker worker
processes or with the old `ingestion.py watch`; those represent alternative
runtimes for the same source collection.

### Option A: native collector/consumer, simplest and already validated

~~~bash
source .venv/bin/activate
export AWS_PROFILE=adaptivenet-admin
python scripts/live_monitor.py --sqs --traffic --with-ui --interval 60 --probe-interval 15
~~~

### Option B: Dockerized collector and consumer (opt-in validation)

From repository root:

~~~bash
aws sso login --profile adaptivenet-admin
mkdir -p data
export AWS_PROFILE=adaptivenet-admin
export ADAPTIVENET_UID="$(id -u)"
export ADAPTIVENET_GID="$(id -g)"
docker compose -f compose.telemetry.yaml config --quiet
docker compose -f compose.telemetry.yaml up -d --build
docker compose -f compose.telemetry.yaml ps
docker compose -f compose.telemetry.yaml logs -f --tail=40
~~~

In a separate terminal, while Docker workers remain running:

~~~bash
source .venv/bin/activate
export AWS_PROFILE=adaptivenet-admin
python scripts/live_monitor.py --external-workers --traffic --with-ui
~~~

This launches **only** the native read-only API, bounded witnesses and two
web interfaces. It intentionally does not spawn more SQS workers. If port
8766 or 3000/3001 is already occupied, stop the original process first.

Docker caveats: host SSO credentials must already be cached and may expire;
renew on the host, not inside the container. The read-only AWS cache must
be accessible to `ADAPTIVENET_UID`. The Dockerized producer still only
polls sources; it cannot mint new RIPE measurements. AWS API/SQS usage incurs
costs. Stop containers with:

~~~bash
docker compose -f compose.telemetry.yaml down
~~~

### Verify *continuous* processing, not a single successful poll

~~~bash
python scripts/sqs_ingestion.py status
curl -fsS http://127.0.0.1:8766/v1/ingestion/status
curl -fsS http://127.0.0.1:8766/v1/features/history?limit=5
curl -fsS http://127.0.0.1:8766/v1/fast-signals/latest
curl -fsS http://127.0.0.1:8766/v1/detection/input
python scripts/ripe_measurements.py status
python scripts/amp_metric_doctor.py --profile adaptivenet-admin
~~~

Expect advancing feature snapshot times (nominally around 60s), a drained
queue/outbox when caught up, and 15s HTTP witness observations. If sources
are slow, actual snapshots may arrive later; timestamps and backlog tell
the truth. Check the DLQ separately if failures persist. The read-only
`/v1/detection/input` must have `automatic_actions_enabled: false`.

## What to eliminate, retain or defer

- **Retain:** one SQS transport path, existing normalized observations,
  SHA256 deduplication, SQLite experiment store, stable model_input.v1,
  explicit missingness, independent witness journal, two regional sources
  and CloudFront global context.
- **Remove architectural ambiguity:** native vs Docker runtime must be
  explicit. Lab Compose is *not* production AWS ingestion. Direct
  `ingestion.py watch` is a *fallback*, not a parallel producer.
- **Don't duplicate:** avoid second RIPE collectors, duplicate model adapters,
  a second queue for the same slow source or having both Python and Docker
  read SQS concurrently without understanding that consumers compete.
- **Defer:** RIPE streaming, extra external ASNs/vantage hosts, PostgreSQL for
  EKS, HPA, route-changing control, trained models, cost-optimized production
  worker scheduling. These are required before promising cloud-native
  always-on predictive auto-recovery.

## Minimum ML experiment protocol

1. Record verified healthy intervals across multiple days and paths.
2. Create an **episode manifest**: experiment_id, UTC start/end, fault
   source/target, actual execution confirmation, HTTP results, region and
   recovery timestamp. Prevent leakage from fault command metadata into
   inference-time features.
3. Run matched control, transient, sustained delay/loss, partial outage,
   internal compute and service-stop, recovery experiments. Label using
   independently verified evidence, not the same RIPE thresholds fed to ML.
4. Aggregate fast HTTP and slow features using source timestamps, masks,
   age, observed probe identity and a strict information cutoff at each
   inference timestamp. Never use future observations or CloudFront
   `last_known` UI values as a current feature.
5. Compare rule baseline, AWS-only, AWS+RIPE, and optionally
   AWS+RIPE+HTTP models. Split **by entire experiment**, not random adjacent
   snapshots. Keep final test episodes untouched.
6. Report per-class precision/recall/F1, false alarms per day, median and
   p95 detection delay, missed episodes, coverage/abstentions, calibration
   and recovery timing. Validate whether faster observations truly improve
   delay without unacceptable false alarms.
