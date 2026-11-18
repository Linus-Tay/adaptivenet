# AdaptiveNet local demo

One command brings up the whole AdaptiveNet loop on your laptop, with no AWS credentials and no AWS calls:

```
collector -> durable outbox -> file queue -> feature worker -> SQLite snapshots
   -> detector (rules; ML shadow beside it) -> proposal.v1 -> independent safety reviewer
   -> confidence rules -> SafetyController on the LOCAL gateway only (bounded, verified, journaled)
   -> dashboard (:3000)
```

There are two modes. **Live lab** (`demo/run.sh lab-up`) runs containers you can break from the dashboard or a fault CLI. **Replay** (`demo/run.sh replay`) feeds recorded real AdaptiveNet telemetry through the same detector, reviewer and decision steps.

## 1. Start the live lab

Requirements: Docker Desktop running, and Python 3.11+ on the host for the fault CLI (the repo `.venv` works). Port 8080 must be free.

```bash
export PATH=/Applications/Docker.app/Contents/Resources/bin:$PATH
```

```bash
demo/run.sh lab-up
```

`run.sh` creates `demo/.env.demo` with a random local `LAB_TOKEN` (git ignores it). It layers `demo/compose.lab-overlay.yaml` on the group's `compose.yaml` and starts 11 containers.

| Open | What it is |
| --- | --- |
| http://127.0.0.1:3000 | **Main demo page**: fault buttons (latency, HTTP 503, packet loss; Singapore or Virginia; 30 to 120 s), traffic split, AWS-only vs AWS+RIPE vs AWS+RIPE+HTTP with detection times, the telemetry → detector → proposal → safety review → controller pipeline, signals by source and the decision log |
| http://127.0.0.1:3000 → **Real AWS telemetry** tab | real data per region in plain sentences. Live when `scripts/live_monitor.py --sqs` runs on the host (:8766); otherwise a recorded real moment from `REAL_DATA_DIR`, always labelled with its own time |
| http://127.0.0.1:3001 | the shop site, served through the gateway |
| http://127.0.0.1:8192 | debug only: the raw decision state (`/api/state`) and assessment (`/v1/assessment`) that the dashboard reads |

| Container | Why it is there | Host port |
| --- | --- | --- |
| `demo-a`, `demo-b` | Singapore and Virginia origins: the catalogue plus `/lab/metrics` (measured CPU, memory, load, request counters) and app faults. Separate so each fails on its own | 8101, 8102 |
| `gateway` | stands in for CloudFront. Users and observers reach the regions through it, path faults sit on it, and its traffic split is the only thing the controller changes | 8080 |
| `control-api` | the group's original lab API: manual routes and bounded faults (`/experiments`) used by the dashboard buttons and the routing experiment | 8000 |
| `lab-collector` | internal scrape every 10 s (AMP stand-in), 3 lab probes × 3 pings every 10 s (RIPE stand-in), HTTP witness every 5 s; unchanged `encode_poll` and `Outbox` | none |
| `lab-feature-worker` | unchanged `receive_one` → `make_snapshot` → SQLite. A directory queue (`demo/lab/file_queue.py`) stands in for SQS with the same at-least-once, delete-after-persist behaviour | none |
| `lab-ml` | the detector, suggest only: the rule detector (`demo/lab/detectors.py`) per feature group, the `ml/predictor.py` proposal with its safety review, optional trained models side by side. Also serves the decision state the dashboard reads | 8192 |
| `lab-controller` | proposal → `end_to_end.decide` → confidence rules → `SafetyController(mode="local")`. Shares the gateway's network namespace because the route adapter only accepts loopback | none |
| `lab-users` | steady user traffic (2 requests/s) through the gateway | none |
| `dashboard`, `demo-web` | the demo page and the shop site | 3000, 3001 |

Data goes to `data/lab-live/`, which git ignores. Stop with `demo/run.sh lab-down`.

## 2. Inject faults

From the dashboard: pick a region and duration, then press **Inject +1.5 s latency**, **Inject HTTP 503** or **Inject 80% packet loss**. **Reset demo** clears faults, routes 100% to Singapore and starts a fresh controller journal.

The same from the command line (repo root, `PYTHONPATH=services:.`):

```bash
python -m demo.faultctl lab-latency --region sg --ms 1500 --duration 120
```

```bash
python -m demo.faultctl lab-errors --region sg --rate 1.0
```

```bash
python -m demo.faultctl lab-loss --region sg --pct 80
```

```bash
python -m demo.faultctl app-5xx --region us --rate 0.5
```

```bash
python -m demo.faultctl cpu --region sg
```

```bash
python -m demo.faultctl region-down --region sg
```

```bash
python -m demo.faultctl status
```

```bash
demo/run.sh lab-reset
```

`lab-clear` removes faults; `lab-reset` also restarts the gateway (100% Singapore) and clears the controller journal. Faults expire on their own after at most 120 s. Each command is appended to `fault-log.jsonl` as an operator action, not a label: a fault command is not proof that a failure happened.

### What to expect

| Fault | Where it acts | AWS-only | AWS+RIPE / +HTTP | Decision path |
| --- | --- | --- | --- | --- |
| `lab-latency` (gray failure) | gateway path in front of the region | **healthy**: the origin's own metrics stay clean | `network_degradation` | `shift_traffic` → reviewer → confidence rules (3 windows) → 3 fresh controller probes → gateway 100→75 → client check → further steps while the fault lasts → failback |
| `lab-loss` (gray failure) | gateway path: a share of requests is dropped (HTTP 504) | healthy | `path_loss` | same as above at 80 %. At 50 % the fault is detected but traffic usually does not move: the HTTP and probe votes flicker, so 2 of 3 bad for 3 windows in a row rarely holds. That matches the real campaign replay, where partial loss seldom led to an action |
| `lab-errors` (HTTP 503) | gateway path | healthy | HTTP failures; RIPE stand-in sees loss | failover after the confidence rules agree |
| `app-5xx` | inside the origin | `application_failure` | `application_failure` | failover away from that region |
| `cpu` | inside the origin | `compute_saturation` | same | **scale** plan (HPA `minReplicas` 2→3) is shown and **not executed** |
| `region-down` | origin returns 503 and the path drops everything | `application_failure` | `region_down` | 100% loss leaves no RTT, so RIPE quality is `loss_only`; the reviewer accepts that only for a shift away from the lossy region (`reviewer_source_loss_only_v1`) |

The first two rows are the point of the demo: the same telemetry, seen only through the AWS-only projection, cannot see a path-only fault. Latency is 1500 ms because the controller's own client deadline is 500 ms; below that the safety controller (correctly) does not treat the source as degraded.

## 3. Replay recorded real data

Use an archive folder made by `python -m ml.team_starter.archive` (with `feature_snapshots.jsonl` and `http_witness.jsonl`), or an ingestion `.db`. These files are private, so keep them under `data/` and never commit them.

```bash
REPLAY_SOURCE=/absolute/path/to/data/ml-archive \
REPLAY_START_AT=2026-11-09T14:27:00Z REPLAY_SPEED=10 demo/run.sh replay
```

Open http://127.0.0.1:8192. The header shows `replay_recorded_data`. Stop with `demo/run.sh replay-down`.

Replay writes snapshots in their recorded order and spacing (divided by `REPLAY_SPEED`, idle gaps capped by `REPLAY_MAX_GAP`, default 120 s) into `data/demo-replay/`. Freshness gates compare against the wall clock, so each record is shifted by one constant offset; the original timestamps are kept under `replay`, and values, nulls and quality flags are never changed. The controller runs in `review` mode, assumes a 50/50 split (`DEMO_REVIEW_REGION_A_PERCENT`) and actuates nothing.

**Replay of the verified episode `fault-run-20261109T142558Z-3fef5580`** (a region-b service stop, 14:29:35 to 14:31:27 UTC), checked on 12 Nov 2026 with the lab rule detectors:

* AWS-only stays **healthy**, because the stored AMP features still read `app_up = 1`.
* AWS+RIPE also stays healthy: it was a service stop, not a network fault.
* AWS+RIPE+HTTP flags it from the witness `network_error` responses, first as `multi_region_degradation` (no action), then region-b at 14:31:25, which leads to a **failover** plan.
* Detection lands near the end of the episode. Treat it as a demo of the pipeline on real data, not an evaluation result.

## 4. Showing the trained models beside the rules

The dashboard's **Shadow ML** panel shows what the trained models say next to the rule detector that drives decisions. To fill it, copy the three frozen network models (logistic regression, healthy vs network degradation, trained on recorded 9 to 10 November AWS + RIPE + witness data: healthy controls and packet-loss trials) into `data/demo-models/`. The script checks each model's SHA-256 against the frozen Step 4 report first:

```bash
python scripts/export_demo_models.py --root /path/to/folder/holding/data
```

`--root` is the folder whose `data/ml-results/native/` holds the models (on the group Mac: the evidence copy of the repo). The models are not in the repository, so on a fresh clone the panel shows "none loaded" for them; the rule detector and the baseline predictor still appear. Then restart with `demo/run.sh lab-up`. Never download pickle files; use only artifacts the group made. The models were trained on real AWS data, so on lab data they can disagree with the rules (for example the AWS+RIPE+HTTP model flagging a healthy lab), which is exactly why they stay shadow-only. `ADAPTIVENET_SHADOW_MODEL=/app/models/<file>.pkl` makes `ml/predictor.py` use one. Decisions still come from the rule detector for `DEMO_DECISION_GROUP` (default `aws_ripe_http`), and every proposal still goes through the reviewer.

## 5. Without Docker, and tests

```bash
demo/run.sh test
```

```bash
PYTHONPATH=services:. python -m demo.smoke_local
```

The smoke test starts every lab service as a local process on random loopback ports, injects 700 ms of gateway latency on region-a and checks that AWS-only stays healthy, AWS+RIPE+HTTP flags region-a and the gateway moves 100→75.

## Honesty notes and limits

* Lab values are real measurements of local containers. They reuse the `aws_amp` / `ripe_atlas` source names because the feature contract only accepts those, but every observation carries a `lab` label and lab data never leaves `data/lab-live/`. Never mix it into training data.
* The RIPE stand-in measures gateway-path reachability: an HTTP error counts as a lost probe, which real ICMP loss would not. Packet loss is emulated by dropping requests, not packets.
* The lab detectors are transparent thresholds (`demo/lab/detectors.py`, overridable by `DEMO_*` env vars), not trained ML. Each sees only its group's projection, so AWS-only cannot read RIPE or HTTP values. Missing inputs give `unknown`, never `healthy`.
* The lab probes every 10 s. Real RIPE Atlas probes measure every 240 s, so the real system reacts far more slowly to path faults than the demo.
* There is one HTTP witness vantage, as in the real system.
* **Scale is never executed by the demo.** Live HPA writes stay behind `scripts/end_to_end.py execute --approve-live-write <site>`. **Failover** only runs on the local lab gateway. There is no EKS or CloudFront traffic actuator.
* The emulated HPA observation in the controller is a fixture (2 replicas, bounds 2..6, 60% target) carrying the measured origin CPU, not a read of EKS.
