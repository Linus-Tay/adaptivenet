# AdaptiveNet

AdaptiveNet watches a two-region web service (Singapore `region-a`, Virginia `region-b`) from inside AWS and from the internet, decides whether a region is degraded, and moves traffic or scales only after independent safety checks pass.

The main question it answers: **do outside network signals (RIPE Atlas probes, real HTTP requests) catch failures that AWS's own metrics miss?** In a pre-registered 20-trial packet-loss campaign, AWS metrics alone caught 0 of 20 faults and AWS+RIPE caught 13 of 20, with no false alarms in 20 healthy controls (see [Results](#7-results)).

```
AWS metrics (Prometheus) + RIPE Atlas probes + HTTP witness + CloudFront
  -> collector -> outbox -> SQS queue (+ dead-letter queue) -> feature worker -> SQLite snapshots
  -> ML detector (shadow only) -> proposal
  -> confidence rules (2 of 3 signals agree for 3 windows)
  -> independent safety reviewer
  -> decision: failover / failback / scale / no-op
  -> safety controller (bounded steps, verified, journaled, reversible)
```

Everything below can be run on a laptop **without any AWS or RIPE account**. The real cloud parts are described in [section 6](#6-real-aws-and-ripe-atlas-access).

---

## 1. What to download

| Tool | Version used | Needed for |
|---|---|---|
| Git | any recent | getting the code |
| Docker Desktop (Engine + Compose v2) | Engine 29.9, Compose 5.7 (any Compose v2 works) | the demo (required) |
| Python | 3.11 or newer (CI uses 3.12; also tested on 3.14) | tests and the fault command line |
| Node.js + npm | Node 24 | only to build or type-check the dashboards outside Docker |
| Terraform | 1.13.5 (`.terraform-version`) | only for the AWS infrastructure (group members) |
| Helm, kubectl, AWS CLI v2 | Helm 3, any recent kubectl / AWS CLI | only for EKS and AWS work (group members) |

Get the code:

```bash
git clone https://github.com/Linus-Tay/adaptivenet.git
```

```bash
cd adaptivenet && git checkout submission/final
```

## 2. Install

From the repository root:

```bash
python3 -m venv .venv
```

```bash
source .venv/bin/activate
```

```bash
pip install -r requirements-dev.lock -r ml/team_starter/requirements-ml-inference.lock && pip install --no-deps -e .
```

The first lock file holds the runtime and test packages (FastAPI, Uvicorn, httpx, pytest). The second holds the ML packages (scikit-learn 1.8.0, numpy 2.3.5, scipy 1.17.0) with pinned hashes. Docker Desktop must be running before you start the demo. If `docker` is not on your PATH on a Mac, run `export PATH=/Applications/Docker.app/Contents/Resources/bin:$PATH` first.

## 3. Run the demo

The demo runs the full loop in local containers. It makes no AWS calls and needs no credentials.

```bash
demo/run.sh lab-up
```

The first build takes a few minutes. Then open **http://127.0.0.1:3000**.

| Address | What it is |
|---|---|
| http://127.0.0.1:3000 | **Main demo page**: fault buttons, traffic split, AWS-only vs AWS+RIPE vs AWS+RIPE+HTTP verdicts with detection times, the pipeline stages and the decision log |
| http://127.0.0.1:3001 | The demo web site (product catalogue served through the gateway) |
| http://127.0.0.1:8190 | Raw decision-service view, for debugging |

What to try:

1. Press **Reset demo**.
2. Press **Inject +1.5 s latency** (Singapore, 120 s).
3. Within about 15 s, AWS+RIPE+HTTP reports `network_degradation` in region-a while AWS-only stays healthy. This is the core finding: internal metrics cannot see a network-path fault.
4. After 3 confidence windows plus 3 controller probes (about 90 s in our run), the gateway moves Singapore's traffic share from 100% to 75%. A longer fault takes further 25-point steps.
5. When the fault ends and Singapore stays clean, traffic returns to 100% Singapore (failback; about 100 s after the fault ended in our run).

What we saw on 13 Nov 2026 with a 120 s, +1.5 s latency fault: AWS-only stayed healthy the whole time; AWS+RIPE+HTTP flagged region-a after about 15 s; the gateway went 100→75 at about 93 s; and failback brought it back to 100 about 100 s after the fault ended.

The same faults from the command line:

```bash
PYTHONPATH=services:. python -m demo.faultctl lab-latency --region sg --ms 1500 --duration 120
```

```bash
PYTHONPATH=services:. python -m demo.faultctl lab-errors --region sg --rate 1.0
```

Reset and stop:

```bash
demo/run.sh lab-reset
```

```bash
demo/run.sh lab-down
```

Port 8080 must be free (the gateway uses it). Demo data goes to `data/`, which git ignores.

Other modes are in [demo/DEMO.md](demo/DEMO.md): a standalone stack with an emulated internet path for packet-loss faults (`demo/run.sh up`), replay of recorded real telemetry (`demo/run.sh replay`), and plugging in trained models.

## 4. Run the tests

With the virtual environment active, from the repository root:

```bash
PYTHONPATH=services:. python -m pytest -q tests demo/tests ml/team_starter/tests step7_finish step8_finish
```

This runs all Python test suites: services and controller (`tests/`), demo (`demo/tests/`), ML (`ml/team_starter/tests/`) and the ML shadow service (`step7_finish/`, `step8_finish/`). None of them call AWS. On this branch (13 Nov 2026, Python 3.14) the result was **471 passed, 1 failed**; `tests/` alone is 304 passed. The one failure is the ML test `test_shadow_masks_future_ripe_even_when_old_quality_flag_is_fresh`, a known failure that came over from the ML branch.

A no-Docker end-to-end smoke of the demo:

```bash
PYTHONPATH=services:. python -m demo.smoke_local
```

CI (`.github/workflows/ci.yml`) also runs `python scripts/smoke.py`, `npm ci && npm run build && npm run typecheck`, `terraform fmt -check` and `helm lint infra/helm/demo`.

## 5. Repository layout

| Path | What is there |
|---|---|
| `services/adaptivenet/ingestion/` | Collectors for AWS Prometheus (AMP), RIPE Atlas, CloudFront; SQS producer/consumer with outbox, dedup and DLQ; feature snapshots; safety reviewer (`decisions.py`) |
| `services/adaptivenet/controller/` | Confidence rules (`confidence.py`), safety controller, end-to-end decision (`end_to_end.py`), failback/recovery, read-only EKS HPA access, action journal |
| `services/adaptivenet/gateway.py` | Local traffic-split gateway (the only thing the demo controller may change) |
| `ml/team_starter/` | ML datasets, training, evaluation, RIPE value study, campaign analysis, shadow prediction |
| `ml/predictor.py` | Shadow predictor used by the demo |
| `step5_finish/`, `step7_finish/`, `step8_finish/` | Frozen-model shadow inference service, its container and EKS manifest, and the final ML checks |
| `demo/` | One-command Docker demo, fault CLI, replay, smoke test |
| `apps/dashboard/`, `apps/demo-web/` | Next.js demo page (:3000) and demo web site (:3001) |
| `packages/contracts/` | Shared TypeScript data contracts |
| `scripts/` | Operator tools: telemetry pipeline (`live_monitor.py`, `ingestion.py`, `sqs_ingestion.py`), probes, guarded AWS fault injection, RIPE campaign, EKS checks, push safety check |
| `infra/terraform/`, `infra/helm/` | AWS infrastructure (EC2, CloudFront, AMP, SQS, IAM, alarms, EKS) and the Helm chart with HPA |
| `docs/` | Design and results documents (list below) |
| `tests/` | Python tests for services, controller, ingestion, scripts |

## 6. Real AWS and RIPE Atlas access

The repository holds **no credentials, keys or tokens**. The real system runs on the group's AWS account (EC2 in Singapore and Virginia, CloudFront, Amazon Managed Prometheus, SQS, EKS) and uses the group's RIPE Atlas measurements.

**AWS and RIPE Atlas access is held by the group. To run anything against the real cloud, contact the group (CSC3104 Group 03) to request access.** You do not need it for the demo or the tests.

For group members with access:

- Copy `.env.example` to `.env` and fill in values. `.env` is git-ignored; never commit it.
- Real telemetry pipeline: `python scripts/live_monitor.py --sqs` (dashboard at http://127.0.0.1:8766). Setup and checks: [docs/SQS_INGESTION.md](docs/SQS_INGESTION.md).
- Guarded real fault injection (SSM, restore timer set first): `scripts/aws_fault.py`.
- RIPE campaign tools: `scripts/campaign/` (written for the Mac that ran the campaign; edit the folder paths at the top before reuse) and [docs/ml/RIPE_TRIALS_PROTOCOL.md](docs/ml/RIPE_TRIALS_PROTOCOL.md).
- EKS checks: [docs/SAFETY_CONTROLLER_TASK4.md](docs/SAFETY_CONTROLLER_TASK4.md). Live HPA writes need `scripts/end_to_end.py execute --approve-live-write <site>`.
- Before pushing: `python scripts/check_push_safety.py --staged`.

## 7. Results

All numbers come from the linked documents; each states its data, method and limits.

| Result | Number | Source |
|---|---|---|
| 20-trial packet-loss campaign, faults detected | AWS-only **0/20**, AWS+RIPE **13/20** (65%, 95% CI 41–85%) | [RIPE_CAMPAIGN_RESULTS.md](docs/ml/RIPE_CAMPAIGN_RESULTS.md) |
| Same campaign, healthy controls alarmed | AWS+RIPE **0/20** | same |
| Paired test (exact McNemar) | **p = 0.00024** | same |
| Detection delay, RIPE (detected trials) | median **459 s** (22–892 s), about 2 RIPE rounds | same |
| Same-day ML retrain (shadow only) | anomaly model AWS+RIPE: 17/20 detected, about 14 false alarms/day; frozen rule: 13/20, 0 false alarms | [CAMPAIGN_SAME_DAY_ML.md](docs/ml/CAMPAIGN_SAME_DAY_ML.md) |
| Earlier reviewed loss trials | AWS-only 0/6, AWS+RIPE 6/6 | [RIPE_VALUE_STUDY.md](docs/ml/RIPE_VALUE_STUDY.md) |
| Models trained on a different day | 58–150 false alarms/day (host memory drift) | [SIX_CLASS_AND_FALSE_ALARM_STUDY.md](docs/ml/SIX_CLASS_AND_FALSE_ALARM_STUDY.md) |
| Decision path and confidence rules | failover, failback and scale rules with lab checks | [END_TO_END.md](docs/END_TO_END.md) |

Limits stated in those documents still apply: one HTTP witness vantage, RIPE results arrive every few minutes, ML stays shadow-only, and the demo never executes a scale action.

## 8. More documents

| Document | Topic |
|---|---|
| [docs/ARCHITECTURE_AND_ML_2026.md](docs/ARCHITECTURE_AND_ML_2026.md) | Architecture, data rates, Docker vs native runtime |
| [docs/SQS_INGESTION.md](docs/SQS_INGESTION.md) | Queue, outbox, dedup, DLQ, alarms, Terraform setup |
| [docs/END_TO_END.md](docs/END_TO_END.md) | Proposal → reviewer → EKS decision, confidence rules |
| [docs/SAFETY_CONTROLLER_TASK4.md](docs/SAFETY_CONTROLLER_TASK4.md) | Safety controller and EKS integration tests |
| [docs/DATA_DICTIONARY.md](docs/DATA_DICTIONARY.md) | Feature fields |
| [docs/TEAM_HANDOFF.md](docs/TEAM_HANDOFF.md) | Model input/output contracts |
| [docs/ml/PREDICTION_GUIDE.md](docs/ml/PREDICTION_GUIDE.md) | Prediction handoff and feature rules |
| [ml/team_starter/TEAM_STARTER.md](ml/team_starter/TEAM_STARTER.md), [ML_FOUNDATION.md](ml/team_starter/ML_FOUNDATION.md) | ML commands: archive, labels, training, evaluation |
| [docs/ml/STEP7_SHADOW_SERVICE.md](docs/ml/STEP7_SHADOW_SERVICE.md), [STEP8_FINALIZER.md](docs/ml/STEP8_FINALIZER.md) | Shadow inference service, container, EKS handoff, final checks |
| [docs/ml/](docs/ml/) | All ML and RIPE study reports |
| [demo/DEMO.md](demo/DEMO.md) | Full demo guide |

`ml/team_starter/examples/` holds label templates only. The repository ships no training data: real evidence is private and stays under the git-ignored `data/` folder.

## 9. Safety rules built into the code

- ML output is untrusted. Every proposal goes through the confidence rules and the independent reviewer; the reviewer never executes anything itself.
- Traffic moves in steps of at most 25 points, only after fresh probes confirm the destination is healthy, with cooldowns and a journal of every action.
- Failback waits for 120 s of clean signals in Singapore (the lab shortens this to 60 s; set `FAILBACK_HEALTHY_SEC` to change it).
- Scaling never goes below baseline replicas, and live EKS writes need an explicit operator flag.
- Real AWS fault injection schedules its own recovery before it starts the fault.
