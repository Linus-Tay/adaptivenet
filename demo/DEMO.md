# AdaptiveNet local demo

One command brings up the whole AdaptiveNet loop on your laptop, with no AWS credentials and no AWS calls:

```
collector -> durable outbox -> queue -> feature worker -> SQLite snapshots
   -> ML (shadow) -> proposal.v1 -> independent safety reviewer
   -> EKS decision (scale / failover / no-op, dry run)
   -> SafetyController on the LOCAL gateway only (bounded, verified, journaled)
   -> decision view
```

There are two modes. **Live lab** runs real containers that you can break with a fault CLI. **Replay** feeds recorded real AdaptiveNet telemetry through the same ML, reviewer and decision steps.

Nothing in `compose.yaml`, `compose.telemetry.yaml`, `Dockerfile`, `Dockerfile.telemetry`, Terraform, SQS or the ingestion code was changed for this. The demo lives entirely in `demo/` and reuses the existing modules.

## 0. Recommended: your group's existing lab plus ML and the safety controller

This keeps the three existing sites and adds only the missing pieces next to them. `compose.yaml` is not edited; `demo/compose.lab-overlay.yaml` is layered on top of it.

```bash
export PATH=/Applications/Docker.app/Contents/Resources/bin:$PATH
```

```bash
demo/run.sh lab-up
```

| Open | What it is |
| --- | --- |
| http://127.0.0.1:3000 | **Main demo page.** The group dashboard, rebuilt: fault controls (latency / HTTP 503, Singapore or Virginia, 30–120 s), traffic split, AWS-only vs AWS+RIPE vs AWS+RIPE+HTTP side by side with detection times, the telemetry → ML → proposal → safety review → controller pipeline, signals by source, and the decision log. The real-AWS SSM panel is unchanged when the dashboard runs in AWS mode. |
| http://127.0.0.1:3000 → **Real AWS telemetry** tab | The real data in plain sentences per region: inside AWS (Prometheus), from the internet (RIPE Atlas), real user requests (HTTP witness), CloudFront, each with a freshness badge, plus the same AWS-only / +RIPE / +RIPE+HTTP verdicts. Live when `scripts/live_monitor.py --sqs` runs on the host (:8766). Otherwise it shows a recorded real moment from `REAL_DATA_DIR` (set it in `demo/.env.demo`), with a time slider and one-click jumps to reviewed episodes. Recorded data is always labelled with its own time. |
| http://127.0.0.1:3001 | Group demo site (catalogue through the gateway) |
| http://127.0.0.1:8190 | Raw decision-service view (debugging; the dashboard reads the same data) |
| http://127.0.0.1:8766 | Real AWS telemetry dashboard (only when the real ingestion API is running; not part of this lab) |

The overlay adds `lab-collector`, `lab-feature-worker`, `lab-ml`, `lab-controller`, `lab-view` and `lab-users`. It also runs `demo-a` / `demo-b` from the demo image, which serves the same catalogue plus `/lab/metrics` so the internal view has measured CPU and memory values. External observers go through the group's gateway (`PATH_MODE=gateway`), so the dashboard's fault buttons are exactly what they see. In this mode, the RIPE stand-in measures gateway-path reachability: an HTTP error counts as a lost probe, which real ICMP loss would not.

Press **Reset demo**, then **Inject +1.5 s latency** (Singapore, 120 s) on the dashboard. Within about 15 s, AWS+RIPE+HTTP reports `network_degradation` in region-a while AWS-only stays healthy. After 3 confidence windows plus three fresh controller checks (about 90 s in the 13 Nov run), the controller moves the gateway from 100% to 75% region-a, then 50% and 25% if the fault lasts long enough. When the fault ends, failback returns traffic to 100% region-a. The same faults are also available from the command line:

```bash
PYTHONPATH=services:. python3 -m demo.faultctl lab-latency --region sg --ms 1500 --duration 120
```

```bash
PYTHONPATH=services:. python3 -m demo.faultctl lab-errors --region sg --rate 1.0
```

```bash
demo/run.sh lab-reset
```

`lab-reset` restarts the gateway (100% region-a, no faults) and clears the controller journal. Stop with `demo/run.sh lab-down`. Data goes to `data/lab-live/`, which git ignores. Port 8080 is used by the group's gateway; quit LM Studio's server first if it holds that port.

The rest of this document describes the standalone stack (`demo/run.sh up`), which uses its own emulated internet path for packet-loss faults. Run one stack at a time; `lab-up` and `up` each stop the other.

## 1. Start the standalone live lab

Requirements: Docker Desktop running, and Python 3.11+ on the host for the fault CLI (the repo `.venv` works).

```bash
demo/run.sh up
```

Then open the decision view at http://127.0.0.1:8190.

`run.sh` creates `demo/.env.demo` with a random local `LAB_TOKEN`. Git ignores this file. After about 30–60 seconds, all three feature-group rows should read **healthy** and the decision table should show `hold` / `no-op`.

| Service | What it is | Host port (loopback) |
| --- | --- | --- |
| `origin-sg`, `origin-us` | The existing demo catalogue, plus `/lab/metrics` measured inside the container (CPU, memory, load, request counters) and app faults | 8181, 8182 |
| `gateway` | The existing local route simulator (`adaptivenet.gateway`), the only thing the controller may change | 8180 |
| `edge` | An emulated internet path between external observers and the origins. Latency and loss faults are injected here | 8191 |
| `users` | Steady background user traffic on the user path (edge → gateway) | none |
| `collector` | Internal scrape every 15 s (AMP stand-in), 5 lab probes × 3 pings every 30 s (RIPE stand-in), and an HTTP witness every 5 s. It uses the unchanged `encode_poll` and `Outbox`. | none |
| `feature-worker` | The unchanged `receive_one` → `make_snapshot` → SQLite | none |
| `ml` | Shadow ML: `ml/predictor.py` plus the safety review, optional trained artifacts, and per-group lab detectors | 8192 |
| `controller` | Proposal → `end_to_end.decide` → `SafetyController(mode="local")`. It shares the gateway's network namespace because the route adapter only accepts loopback | none |
| `view` | Read-only HTML view at `/` and JSON at `/api/state` | 8190 |

Data lands in `data/demo-live/`, which git ignores. The SQS stand-in is a directory queue (`demo/lab/file_queue.py`) that implements the three calls the existing transport makes. It keeps the same at-least-once and delete-after-persist behaviour.

## 2. Inject faults

```bash
python -m demo.faultctl latency --region sg --ms 700
```

```bash
python -m demo.faultctl loss --region sg --pct 50
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
demo/run.sh reset
```

`reset` clears all faults, routes 100% back to region-a and clears the controller journal. Run the CLI from the repo root with `PYTHONPATH=services:.`, or use `demo/run.sh status`. Faults expire on their own (default 180 s, max 900 s). Each command is appended to `fault-log.jsonl` as an operator action. The log is not a label: a fault command is not proof that a failure happened.

### What to expect

| Fault | Where it acts | AWS-only | AWS+RIPE / +HTTP | Decision path |
| --- | --- | --- | --- | --- |
| `latency --ms 700` (gray failure) | external path only | **healthy**, because internal metrics stay clean | `network_degradation` in region-a (`path_latency` once the next probe round lands, about 30 s) | `shift_traffic` → reviewer → **failover** → local controller qualifies 3 fresh probes → gateway 100→75 → client verification → `continue_candidate` → 75→50 … |
| `loss --pct 50` | external path only | healthy | `path_loss` | same as above |
| `app-5xx --rate 0.5` | inside the origin | `application_failure` | `application_failure` | failover away from that region |
| `cpu` | inside the origin | `compute_saturation` | same | **scale** plan (HPA `minReplicas` 2→3) is shown and **not executed** |
| `region-down` | origin returns 503 and the path drops all pings | `application_failure` | `region_down` | 100% loss leaves no RTT and RIPE quality falls to `loss_only`. Since `reviewer_source_loss_only_v1` (13 Nov) the reviewer accepts that for a shift away from the lossy region; before, it refused with `telemetry_quality_gate_failed`. |

The first row is the point of the demo: the same telemetry, viewed only through the AWS-only feature projection, cannot see a path-only degradation.

`latency` uses 700 ms because the controller's own client deadline is 500 ms. Below that, the safety controller (correctly) does not treat the source as degraded.

## 3. Replay recorded real data

Use an archive folder made by `python -m ml.team_starter.archive` (containing `feature_snapshots.jsonl` and `http_witness.jsonl`), or an ingestion `.db`. These files are private, so keep them under `data/` and never commit them.

```bash
REPLAY_SOURCE=/absolute/path/to/data/ml-archive \
REPLAY_START_AT=2026-11-09T14:27:00Z REPLAY_SPEED=10 demo/run.sh replay
```

Open http://127.0.0.1:8190 again. The header shows `replay_recorded_data`.

Replay writes snapshots in their recorded order and spacing (divided by `REPLAY_SPEED`, with idle gaps capped by `REPLAY_MAX_GAP`, default 120 s) into a separate `data/demo-replay/`. Live freshness gates compare against the wall clock, so each record is shifted by one constant offset. The original timestamps are kept under `replay` in every snapshot and witness event, and values, nulls and quality flags are never changed. The controller runs in `review` mode with no gateway, assumes an even 50/50 split (`DEMO_REVIEW_REGION_A_PERCENT`), and actuates nothing.

**What replay of the verified episode `fault-run-20261109T142558Z-3fef5580` shows** (a reviewed region-b service stop from 14:29:35 to 14:31:27 UTC). This was checked on 12 Nov 2026 with the lab rule detectors, not the trained models:

* AWS-only stays **healthy** for the whole episode, because the stored AMP-derived features still read `app_up = 1`.
* AWS+RIPE also stays healthy. It was a service stop, not a network fault.
* AWS+RIPE+HTTP flags it from the HTTP witness `network_error` responses: `multi_region_degradation` (so no action) at 14:29:37 and 14:30:12, then region-b at 14:31:25 and 14:32:11, which leads to `shift_traffic` → reviewer eligible → **failover** plan.
* The detection lands near the end of the episode, and the condition is named after the signal that fired (`network_degradation`) rather than the verified label (`application_failure`). Treat it as a demo of the pipeline on real data, not as an evaluation result. The scientific comparison belongs to the ML threads' reports.

## 4. Using trained models

Put trusted, locally generated artifacts in `data/demo-models/` (mounted read-only at `/app/models`). Never download pickle files.

```bash
DEMO_MODELS_DIR=/app/models demo/run.sh up
```

Name the files `aws_only.pkl`, `aws_ripe.pkl` and `aws_ripe_http.pkl`. Their shadow proposals then appear under the comparison table. You can also set `ADAPTIVENET_SHADOW_MODEL=/app/models/<file>.pkl` to make `ml/predictor.py` use one. Decisions still come from `DEMO_DECISION_GROUP` (default `aws_ripe_http`), and every proposal still goes through the reviewer.

## 5. Without Docker, and tests

```bash
demo/run.sh test
```

```bash
PYTHONPATH=services:. python -m demo.smoke_local
```

The smoke test starts every service as a local process on random loopback ports and checks the gray-failure story end to end. In the last run it passed: AWS-only stayed healthy, AWS+RIPE+HTTP flagged region-a, the gateway moved 100→75 after three qualifying observations, and client verification returned `continue_candidate` because 25% of traffic still went to the slow region.

## Honesty notes and limits

* Lab values are real measurements of local containers. They reuse the `aws_amp` / `ripe_atlas` source names because the feature contract only accepts those, but every observation carries a `lab` label and lab snapshots never leave `data/demo-live/`. Never mix them into training data.
* The lab detectors are transparent thresholds (`demo/lab/detectors.py`, overridable by `DEMO_*` env vars), not trained ML. They see only their group's projection from `feature_views.project`, so AWS-only cannot read RIPE or HTTP values. Missing inputs give `unknown`, never `healthy`.
* The lab RIPE stand-in probes every 30 s. Real RIPE Atlas results arrive every 15 minutes or more, so the real pipeline reacts far more slowly to path faults than the demo.
* There is one HTTP witness vantage, as in the real system. It cannot prove a widespread outage.
* **Scale is never executed by the demo.** Live HPA writes stay behind `scripts/end_to_end.py execute --approve-live-write <site>`. **Failover** only runs on the local lab gateway. There is no EKS or CloudFront traffic actuator.
* The emulated HPA observation in the controller is a fixture with 2 replicas, bounds 2..6 and a 60% target, carrying the measured origin CPU. It is not a read of EKS.
