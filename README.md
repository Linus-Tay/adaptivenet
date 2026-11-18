# AdaptiveNet

AdaptiveNet watches a web service that runs in two AWS regions, Singapore (`region-a`) and Virginia (`region-b`). It combines AWS's own metrics with measurements taken from outside AWS, decides whether a region is degraded, and moves traffic or scales only after independent safety checks pass.

The question behind it: **do outside network signals catch failures that AWS's own metrics miss?** In a pre-registered campaign of 20 packet-loss trials, AWS metrics alone caught 0 of 20 and AWS + RIPE Atlas caught 13 of 20, with no false alarms in 20 healthy controls ([Results](#6-results)).

Everything in sections 3 and 4 runs on a laptop with no AWS or RIPE account. Step-by-step marking instructions with expected output are in [docs/GROUP03_INSTRUCTIONS.md](docs/GROUP03_INSTRUCTIONS.md) ([PDF](docs/GROUP03_INSTRUCTIONS.pdf)).

**Branches.** `main` is the current, handoff version (the same as `v2`). The original course submission is kept at tag `v1.0-submission`.

---

## 1. Architecture

```
                users / HTTP witness (every 15 s)
                               |
                       CloudFront distribution
                  origin group: Singapore primary,
                  Virginia on 500/502/503/504
                     /                       \
        EC2 Singapore (region-a)       EC2 Virginia (region-b)
        app + node exporter            app + node exporter
                     \                       /
            Amazon Managed Prometheus (AMP), per region

  AMP metrics + RIPE Atlas pings + HTTP witness + CloudFront metrics
    -> collector -> outbox -> SQS standard queue (+ dead-letter queue)
    -> feature worker -> SQLite (events + adaptivenet.features.v1 snapshots)
    -> rule-based detection + confidence rules (2 of 3 signals, 3 windows)
    -> independent safety reviewer
    -> safety controller: bounded, verified, journaled, reversible steps
       (ML detectors run beside this in shadow mode and never act)

  EKS (Singapore): the same app under a Kubernetes HPA (2 to 6 pods, 60 % CPU)
```

**Serving path.** CloudFront sits in front of both EC2 origins. Its origin group sends requests to Singapore and fails over to Virginia when Singapore returns a 5xx ([infra/terraform/environments/cloudfront/main.tf](infra/terraform/environments/cloudfront/main.tf)). This failover is CloudFront's own; the AdaptiveNet controller has no AWS traffic actuator.

**Three views of each region.**

| View | Source | What it can see |
|---|---|---|
| AWS-internal | AMP: app up, HTTP 5xx ratio, handler p95 latency, host CPU and memory | the app and host from inside the region |
| Network path | RIPE Atlas: the same 4 probes ping both origins every 240 s; the collector polls the REST "latest results" endpoint every 60 s (not the Streaming API) | packet loss and RTT on real internet paths, independent of AWS |
| Client | HTTP witness: one GET every 15 s from one machine in Singapore, recording status, latency and which region answered | what a user actually gets |

CloudWatch CloudFront metrics (requests, error rates) are also collected, for display only. They are global aggregates, not per-region signals.

**Pipeline.** The collector ([services/adaptivenet/ingestion/](services/adaptivenet/ingestion/)) writes each poll to a local outbox, then publishes it to a standard SQS queue. Standard SQS can deliver a message twice, so the feature worker deduplicates by a SHA-256 event ID, writes to SQLite, builds a `features.v1` snapshot and only then deletes the message. After five failed receives a message goes to the dead-letter queue. Source timestamps are kept as recorded, and future-dated data is dropped. Details: [docs/SQS_INGESTION.md](docs/SQS_INGESTION.md).

**Decisions.** Detection is rule-based. The confidence rules ([controller/confidence.py](services/adaptivenet/controller/confidence.py)) only ever make the controller wait longer:

- **Failover** (25 points away from a region): at least 2 of the 3 views say bad in the same window, for 3 windows in a row, the other region is clean, and traffic has not moved the other way in the last 120 s.
- **Failback** (25 points back to Singapore): Singapore clean for 120 s, and 120 s since the last failover step.
- **Scale up** (HPA minimum + 1): CPU at or above 80 % with the app up, for 3 windows. **Scale down**: CPU at or below 30 % for 300 s, never below the baseline.

Then the independent safety reviewer ([ingestion/decisions.py](services/adaptivenet/ingestion/decisions.py)) checks the proposal, and the safety controller takes 3 fresh probes of the destination, applies at most one 25-point step, verifies client recovery and journals the action. In the demo it moves only the local gateway. Live HPA writes need an explicit operator flag. Details: [docs/END_TO_END.md](docs/END_TO_END.md), [docs/SAFETY_CONTROLLER_TASK4.md](docs/SAFETY_CONTROLLER_TASK4.md).

**ML is shadow-only.** The models in [ml/team_starter/](ml/team_starter/) score the same snapshots and log what they would have proposed. Their output is never read by the confidence rules, and every result below supports keeping it that way.

### Why Route 53 stays off

Weighted Route 53 records with health checks were written early (`infra/terraform/environments/demo-cloud/main.tf`) but set to `enable_route53 = false` from the start (commit d32db70). Two reasons came up as the design settled:

1. DNS answers are cached for their TTL by resolvers and clients, so changing a weight does not give a prompt or exact per-request split (34b8b3c).
2. Traffic enters through one CloudFront distribution. Editing Route 53 records does not change which origin CloudFront uses, so the two mechanisms would fight rather than combine (34b8b3c).

The team therefore chose CloudFront origin failover as the serving path and left Route 53 weighted routing as future work (b3a3b80). It would need its own write adapter, reviewed permissions and a live test before it could be enabled.

---

## 2. Datasets

The recorded data behind the two headline evaluations is committed under `evidence/`: `evidence/ripe-campaign-20261112/` (the 12 Nov campaign) and `evidence/live-shadow-20261114/` (the 14 to 18 Nov live run). Part C of the testing guide reruns the analysis from them. The SSM records there contain the AWS account number, EC2 instance IDs and public IPs; these are identifiers, not credentials. Other evidence (earlier archives, trained model files) stays in the git-ignored `data/` folder.

| Dataset | What it is | Why it was used |
|---|---|---|
| Synthetic fixtures | small generated snapshots inside the tests | to test code paths; never scored as a result |
| Recorded telemetry, Oct to 11 Nov | real AMP + RIPE + witness snapshots from both regions, SSM service stops on 23 Oct and 9 Nov, the 10 Nov RIPE pilot (6 trials) and a 4 h healthy window on 10 Nov | first ML studies, and to find what breaks them (the host-memory confound and the false-alarm rate) |
| RIPE campaign, 12 Nov | 20 pre-registered packet-loss trials (10 per region, 10 to 35 % loss, 960 s each), AMP export, RIPE measurements 236862553 / 236862555, witness log, SSM records | the headline evaluation: ground truth comes from SSM, not from RIPE, and the untouched region is a same-time control |
| Live shadow run, 14 to 18 Nov | 4 days of live AMP (30 s), new RIPE measurements 237672407 / 237672408, the witness, and 10 further packet-loss trials on 17 Nov | to retrain on more healthy days and score every detector on unseen time and unseen faults, under a protocol committed before the data existed |
| Public benchmarks (Edge Fault, SOFI) | published fault datasets | to sanity-check the training code on outside labels; not evidence about AdaptiveNet |
| Local lab | traffic and faults generated by the Docker demo | demonstration only |

---

## 3. Install and run the tests

You need Git and Python 3.11 or newer (tested on 3.12 in CI and 3.14 locally). From the repository root:

```bash
git clone https://github.com/Linus-Tay/adaptivenet.git && cd adaptivenet
```

```bash
python3 -m venv .venv && source .venv/bin/activate
```

```bash
pip install -r requirements-dev.lock -r ml/team_starter/requirements-ml-inference.lock && pip install --no-deps -e .
```

Both lock files are needed. The second holds scikit-learn, numpy and scipy; without it the ML tests fail on import.

Run every Python suite (none of them call AWS):

```bash
PYTHONPATH=services:. python -m pytest -q tests demo/tests ml/team_starter/tests step7_finish step8_finish
```

On `main` (18 Nov 2026, clean install from a GitHub ZIP, Python 3.14) this gives **496 passed**. A no-Docker smoke test of the demo loop:

```bash
PYTHONPATH=services:. python -m demo.smoke_local
```

CI ([.github/workflows/ci.yml](.github/workflows/ci.yml)) also runs `scripts/smoke.py`, the dashboard build and type-check, `terraform fmt -check` and `helm lint`.

## 4. Run the Docker demo

The demo runs the full loop in local containers with no AWS calls. You need Docker Desktop (or Docker Engine with Compose v2) running. On a Mac where `docker` is not on your PATH, run `export PATH=/Applications/Docker.app/Contents/Resources/bin:$PATH` first. Windows is untested. In PowerShell use `demo\run.ps1` (`lab-up`, `lab-down`, `lab-reset`, `lab-logs`, `status`) and set `$env:PYTHONPATH="services;."` instead of the `PYTHONPATH=services:.` prefix; WSL 2 with the bash commands also works.

On Linux only, the containers run as uid 10001, so prepare `data/` once:

```bash
mkdir -p data/lab-live data/demo-replay data/demo-models && chmod -R a+rwX data
```

Start the demo (11 containers: `compose.yaml` plus `demo/compose.lab-overlay.yaml`). Port 8080 must be free.

```bash
demo/run.sh lab-up
```

Open **http://127.0.0.1:3000** once the first build finishes (a few minutes). It has the fault buttons, the traffic split, the three views side by side, the decision pipeline and a **Real AWS telemetry** tab. The shop site served through the gateway is on :3001. Port 8192 is a debug view of the raw decision state that the dashboard reads.

Why each container is there:

| Container | Role |
|---|---|
| `demo-a`, `demo-b` | the Singapore and Virginia origins, kept separate so each is its own failure domain |
| `gateway` | stands in for CloudFront: path faults sit on it, and its traffic split is the only thing the controller changes |
| `control-api` | the group's original lab API: manual routes and bounded faults for the dashboard buttons and the routing experiment |
| `lab-collector`, `lab-feature-worker` | mirror the real collector and feature worker, with a file queue in place of SQS, so the decoupling is the same |
| `lab-ml` | the detector, suggest only. Demo decisions come from the rule detector (`demo/lab/detectors.py`) turned into a `proposal.v1`; trained models can be shown beside it. It also serves the decision state for the dashboard |
| `lab-controller` | acts only after the confidence rules, the safety reviewer and its own probes agree, keeping suggest and act separate |
| `lab-users` | steady user traffic, so there is something to measure and move |
| `dashboard`, `demo-web` | the demo page (:3000) and the shop site (:3001) |

1. Press **Reset demo**, then **Inject +1.5 s latency** (Singapore, 120 s).
2. In about 15 s, AWS + RIPE + HTTP flags `region-a` while AWS-only stays healthy. Internal metrics cannot see a path fault.
3. After 3 confidence windows and 3 controller probes, Singapore's share drops from 100 % to 75 % (76 s in our 18 Nov run). A longer fault takes further 25-point steps.
4. When Singapore stays clean, traffic returns to 100 % (about 110 s after the fault ended).

**Inject HTTP 503** and **Inject 80% packet loss** work the same way. The same faults from the command line, then reset and stop:

```bash
PYTHONPATH=services:. python -m demo.faultctl lab-latency --region sg --ms 1500 --duration 120
```

```bash
PYTHONPATH=services:. python -m demo.faultctl lab-errors --region sg --rate 1.0
```

```bash
PYTHONPATH=services:. python -m demo.faultctl lab-loss --region sg --pct 80
```

```bash
demo/run.sh lab-reset
```

```bash
demo/run.sh lab-down
```

**Shadow ML beside the rules.** The dashboard's Shadow ML panel shows the three frozen trained models next to the rule detector. They never act. To load them, copy them into `data/demo-models/` (each model's hash is checked first) and restart the demo:

```bash
python scripts/export_demo_models.py --root /path/to/folder/holding/data
```

The model files and the recorded data behind the **Real AWS telemetry** tab are not in the repository (they stay in the group's git-ignored `data/`). On a fresh clone the Shadow ML panel shows the rule detector and the baseline predictor with "none loaded" for the trained models, and the Real AWS tab says no data folder is mounted. Everything else in the demo works as described. The group can show both live on its own machine; the recorded results themselves are reproduced from `evidence/` in Part C of the testing guide.

App faults inside an origin (`app-5xx`, `cpu`, `region-down`), replay of recorded real telemetry (`demo/run.sh replay`) and plugging in trained models are in [demo/DEMO.md](demo/DEMO.md). The real-AWS telemetry pair (collector and feature worker, 2 containers) is in `compose.telemetry.yaml`.

---

## 5. Real AWS and RIPE access

The repository holds no credentials. The real system runs on the group's AWS account and RIPE Atlas measurements; contact CSC3104 Group 03 for access. You do not need it for the tests or the demo.

For members with access: copy `.env.example` to `.env` (git-ignored) and fill it in; [docs/ENV_SETUP.txt](docs/ENV_SETUP.txt) walks through the AWS SSO profiles and the RIPE Atlas key. The real pipeline is `python scripts/live_monitor.py --sqs` (dashboard on :8766). Guarded fault injection through SSM, with its restore timer set before the fault, is `scripts/aws_fault.py`. Campaign tools are in `scripts/campaign/` and [docs/ml/RIPE_TRIALS_PROTOCOL.md](docs/ml/RIPE_TRIALS_PROTOCOL.md); they hold paths for the Mac that ran them, so edit those first. Run `python scripts/check_push_safety.py --staged` before pushing. Never run `terraform apply` without a reviewed plan and the real remote state.

---

## 6. Results

Every number comes from the linked document, which states the data, method and limits. A one-page summary of all experiments is [docs/EXPERIMENT_SUMMARY.md](docs/EXPERIMENT_SUMMARY.md).

### RIPE campaign, 12 Nov (pre-registered)

| Measure | Value |
|---|---|
| Faults detected, AWS-only | **0/20** |
| Faults detected, AWS + RIPE | **13/20** (65 %, 95 % CI 41 to 85 %; Virginia 7/10, Singapore 6/10) |
| Healthy controls alarmed | **0/20** |
| Exact McNemar | **p = 0.00024** |
| Detection delay | median 459 s (22 to 892 s), about 2 RIPE rounds |
| By loss level | 8/8 at 30 to 35 %, 5/12 at 10 to 25 % |

AWS-only missed every trial because the app stayed up with no 5xx and a handler p95 of about 5 ms: the server cannot see loss after its handler returns. Source: [docs/ml/RIPE_CAMPAIGN_RESULTS.md](docs/ml/RIPE_CAMPAIGN_RESULTS.md).

### Live shadow run, 14 to 18 Nov (pre-registered)

Held-out time was 16 Nov 13:00 to 17 Nov 23:00 UTC (65.7 healthy region-hours, never used for training or choosing). The 17 Nov trials were unseen faults. Counts below are as pre-registered.

| Detector | Held-out false alarms / day | 17 Nov trials detected | 17 Nov controls alarmed |
|---|---|---|---|
| 13 Nov anomaly model (current) | 28.1 | 10/10 | 2/10 |
| Same model retrained on live healthy days | 16.1 | 8/10 | 1/10 |
| Pick 2: isolation forest on RIPE loss | 6.6 | 9/10 | 0/10 |
| Supervised S1 (gradient boosting, RIPE loss + RTT) | 6.9 | 9/10 | 0/10 |
| Supervised S2 (logistic, HTTP + RIPE loss) | 21.9 | 10/10 | 1/10 |
| Frozen rule, loss only (AWS + RIPE) | 5.1 (RIPE only 2.9) | 8/10 | 0/10 |
| Frozen main rule (loss or RTT) | 30.7 | 9/10 | 3/10 |
| AWS-only view | 2.2 | 0/10 | 0/10 |

The candidate frozen on Monday failed review before scoring (it watched only RIPE result age and alarmed in 20/20 campaign controls) and is reported, not used. The early-warning test and S1 and S2 did not pass their pre-registered marks. A post hoc correction to the exclusion windows (amendment 10, approved by MY) lowers several false-alarm counts and is shown beside the originals; no verdict changes. The ML stays shadow-only. Source: [docs/ml/live/WEDNESDAY_RESULTS.md](docs/ml/live/WEDNESDAY_RESULTS.md), protocol [docs/ml/LIVE_SHADOW_PROTOCOL.md](docs/ml/LIVE_SHADOW_PROTOCOL.md).

### Other results

| Result | Number | Source |
|---|---|---|
| RIPE pilot, 10 Nov | AWS-only 0/6, AWS + RIPE 6/6 | [RIPE_VALUE_STUDY.md](docs/ml/RIPE_VALUE_STUDY.md) |
| Models trained on a different day | 58 to 150 false alarms/day (host-memory drift) | [SIX_CLASS_AND_FALSE_ALARM_STUDY.md](docs/ml/SIX_CLASS_AND_FALSE_ALARM_STUDY.md) |
| Same-day retrain, 13 Nov | anomaly AWS + RIPE 16 to 17/20 at about 14 false alarms per region per day | [CAMPAIGN_SAME_DAY_ML.md](docs/ml/CAMPAIGN_SAME_DAY_ML.md) |
| Static route vs safety controller, 900 ms delay (local) | requests within 500 ms: static 0 %, controller 65.1 % | [SAFETY_CONTROLLER_TASK4.md](docs/SAFETY_CONTROLLER_TASK4.md) |
| Live CloudFront failover, 13 Nov | 108/108 HTTP 200; all 27 requests during a 60 s Singapore stop served by Virginia, the first 5 s after the stop | [LIVE_TESTS_20261113.md](docs/LIVE_TESTS_20261113.md) |
| Live EKS scale-up, 13 Nov | HPA 2 to 4 pods at up to 97 % CPU; 10,762 requests, 0 failed; the controller correctly held | same |
| Offline action replay of the campaign | 1/20 trials would fail over at the 500 ms deadline; 0 false actions | [ACTION_REPLAY_CAMPAIGN.md](docs/ACTION_REPLAY_CAMPAIGN.md) |

**Limits.** One HTTP witness location, four RIPE probes (three reach Singapore), RIPE results every 240 s, one fault type in the trials, and each live AWS test ran once.

---

## 7. Repository layout

| Path | Contents |
|---|---|
| `services/adaptivenet/ingestion/` | collectors (AMP, RIPE, CloudFront), SQS producer and consumer, feature snapshots, safety reviewer |
| `services/adaptivenet/controller/` | confidence rules, safety controller, decision path, failback, read-only EKS HPA access, journal |
| `services/adaptivenet/gateway.py` | local traffic-split gateway, the only thing the demo controller changes |
| `ml/team_starter/` | ML datasets, training, evaluation, RIPE value study, campaign and live-run analysis |
| `step5_finish/`, `step7_finish/`, `step8_finish/` | frozen-model shadow inference service, its container and EKS manifest, final ML checks |
| `demo/` | Docker demo, fault CLI, replay, smoke test |
| `apps/dashboard/`, `apps/demo-web/` | Next.js demo page (:3000) and demo site (:3001) |
| `scripts/` | operator tools: live pipeline, probes, guarded AWS faults, RIPE campaign, live shadow job, EKS checks |
| `infra/terraform/`, `infra/helm/` | AWS infrastructure (EC2, CloudFront, AMP, SQS, IAM, alarms, EKS) and the Helm chart with the HPA |
| `docs/` | design notes and result reports |
| `tests/` | tests for services, controller, ingestion and scripts |

Further reading: [docs/ARCHITECTURE_AND_ML_2026.md](docs/ARCHITECTURE_AND_ML_2026.md) (data rates, runtime), [docs/DATA_DICTIONARY.md](docs/DATA_DICTIONARY.md) (feature fields), [docs/TEAM_HANDOFF.md](docs/TEAM_HANDOFF.md) (model contracts), [docs/ml/PREDICTION_GUIDE.md](docs/ml/PREDICTION_GUIDE.md), [ml/team_starter/TEAM_STARTER.md](ml/team_starter/TEAM_STARTER.md), [docs/ml/STEP7_SHADOW_SERVICE.md](docs/ml/STEP7_SHADOW_SERVICE.md).

## 8. Safety rules in the code

- ML output is untrusted and never triggers an action. The reviewer never executes anything itself.
- Traffic moves at most 25 points per step, only after fresh probes show the destination healthy, with cooldowns and a journal.
- Failback waits for 120 s of clean signals (60 s in the lab; set `FAILBACK_HEALTHY_SEC`).
- Scaling never goes below the baseline, and live EKS writes need `scripts/end_to_end.py execute --approve-live-write`.
- Real fault injection schedules its own recovery before it starts the fault.
