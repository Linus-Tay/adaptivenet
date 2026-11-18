---
title: "AdaptiveNet: How to Test Our System"
subtitle: "CSC3104 Group 03 · GROUP03_INSTRUCTIONS · version 2"
---

# AdaptiveNet: how to test our system

**Course:** CSC3104, Group 03\
**Repository:** https://github.com/Linus-Tay/adaptivenet, branch `main` (download as a ZIP: https://github.com/Linus-Tay/adaptivenet/archive/refs/heads/main.zip)\
**Time needed:** about 30 minutes for Parts A and B (most of it is the first Docker build), and about 10 more minutes for Part C

## What AdaptiveNet does

AdaptiveNet watches a web service that runs in two AWS regions, Singapore (`region-a`) and Virginia (`region-b`). It combines AWS's own metrics with measurements taken from outside AWS (RIPE Atlas internet probes and real HTTP requests), decides whether a region is degraded, and moves traffic only after independent safety checks pass.

The claim we ask you to test is simple: **some network failures cannot be seen from inside AWS, and outside measurements can see them.** In our 20-trial packet-loss campaign on the real system, AWS metrics alone detected 0 of 20 faults, while AWS plus RIPE Atlas detected 13 of 20, with no false alarm in 20 healthy controls.

## What we expect you to test

The tests are in four parts. **Parts A, B and C need no AWS account, no RIPE Atlas account and no keys. Nothing in them contacts AWS.**

| Part | What you do | Time | What it shows |
|---|---|---|---|
| **A. Automated tests** | run 500 unit and integration tests and two smoke tests, with one script | 5 min | the collector, queue, feature snapshots, safety reviewer, confidence rules, safety controller and ML code behave as designed |
| **B. Docker demo** | start 11 containers and inject three kinds of fault from a web page | 15–20 min | the whole loop working: a fault that AWS-only monitoring misses is detected by the external views, traffic is moved in bounded steps, and moved back when the region recovers |
| **C. Rerun our analysis** | rerun three analyses on the data we recorded on the real system | 5–10 min | the main numbers in our article come from the saved data and the committed code, not from hand editing |
| **D. Real AWS and RIPE (optional)** | rebuild the cloud system in your own AWS account | hours, costs money | the real deployment. **We have not tested a rebuild in a fresh account**, so we describe it and offer a live demonstration instead |

Each step below shows the command to type, **what you should see**, and what the step proves. Run every command from the repository root folder (`adaptivenet/`) unless the step says otherwise.

---

## 1. What to download

You need two programs. Everything else is installed by our script.

| Download | Version | Where | Needed for |
|---|---|---|---|
| **Python** | 3.11 or newer (we tested 3.12, 3.13 and 3.14) | https://www.python.org/downloads/ (on Windows, tick **"Add python.exe to PATH"** in the installer) | Parts A and C, and the run script |
| **Docker Desktop** | any recent version (it includes Docker Compose v2) | https://www.docker.com/products/docker-desktop/ | Part B (on Linux, Docker Engine with the Compose v2 plugin also works) |
| Git (optional) | any recent version | https://git-scm.com/downloads | only if you clone instead of downloading the ZIP |
| AWS CLI v2 (optional) | any recent version | https://aws.amazon.com/cli/ | only Part D |

Node.js, Terraform, Helm and kubectl are **not** needed for Parts A to C. Start Docker Desktop after installing it and wait until it says it is running.

## 2. Get the code and set it up with one script

Download https://github.com/Linus-Tay/adaptivenet/archive/refs/heads/main.zip and unzip it, or clone it:

```bash
git clone https://github.com/Linus-Tay/adaptivenet.git
```

Open a terminal (macOS: Terminal; Windows: PowerShell) in the repository folder (`adaptivenet`, or `adaptivenet-main` if you used the ZIP). Please test the `main` branch.

Everything in this guide goes through one script, `run.py`, at the repository root. It works the same way on macOS, Linux and Windows PowerShell and needs only Python to start. **On macOS and Linux type `python3` where this guide says `python`** if `python` is not found.

| Command | What it does |
|---|---|
| `python run.py check` | checks Python, Docker, Docker Compose, the virtual environment, the demo ports and (optionally) the AWS CLI, and says what to fix |
| `python run.py setup` | creates `.venv` and installs every Python package from our lock files |
| `python run.py test` | runs all test suites, then the end-to-end smoke test (Part A) |
| `python run.py demo-up`, `demo-reset`, `demo-down`, `status` | starts, resets and stops the Docker demo, and shows active faults (Part B) |
| `python run.py fault lab-latency --region sg --ms 1500` | injects a demo fault from the command line (B6) |
| `python run.py live` | the real AWS pipeline (Part D only; needs AWS sign-in) |

### 2.1 Check the prerequisites

```bash
python run.py check
```

**Expected** (on a fresh copy with Docker running; your Python version will differ):

```
Prerequisites
  OK  Python 3.13.18
  OK  Docker is running
  OK  Docker Compose v2
  --  .venv not created yet  -> python run.py setup
  OK  demo ports free
  --  AWS CLI not found (only needed for live AWS: https://aws.amazon.com/cli/)
All required prerequisites are present.
```

Any line marked `FIX` says what to do. Lines marked `--` are fine at this point. If a demo port is in use, close the program using it (a local LM Studio server often uses 8080).

### 2.2 Install

```bash
python run.py setup
```

**Expected** (about 1 minute): `pip` installs the packages and the script ends with

```
Successfully installed adaptivenet-0.1.0
Setup done. Next:  python run.py test
```

The packages go into the `.venv` folder inside the repository, not into your system Python.

> **macOS:** if `run.py check` cannot find Docker although Docker Desktop is installed, start Docker Desktop once and run the check again. The script also looks in `/Applications/Docker.app`.
>
> **Windows:** `run.py` runs in plain PowerShell; you do not need WSL. We verified `run.py` on macOS and Linux. On a real Windows machine we confirmed the tests, the smoke test and the Docker demo through the older PowerShell route (`demo\run.ps1`, see B9), which `run.py` replaces; `run.py` itself has not yet been run on Windows. If it fails there, use the route in B9.

---

## 3. Part A: automated tests

### A1. All test suites and the smoke test

```bash
python run.py test
```

**Expected** (about 1 to 2 minutes). First the test suites:

```
500 passed, 1 warning, 4 subtests passed in 10.77s
```

The time will differ. The one warning is a library deprecation notice. **No test should fail.** None of these tests call AWS or the internet; AWS, RIPE and Kubernetes responses are replaced by fixtures.

| Folder | What the tests check |
|---|---|
| `tests/` | data collection (AWS Prometheus, RIPE Atlas, CloudFront); the SQS queue with outbox, duplicate removal and dead-letter queue; feature snapshots; the safety reviewer; the confidence rules; the safety controller (failover, failback, scaling); read-only EKS HPA access; the campaign action replay |
| `demo/tests/` | demo services, the fault command line, failback |
| `ml/team_starter/tests/` | datasets, training, evaluation, the RIPE value study, shadow prediction |
| `step7_finish/`, `step8_finish/` | the ML shadow inference service and final ML checks |

**What this shows:** each part of the pipeline does what the article describes, including the failure cases (duplicate messages, stale or future-dated data, a reviewer that blocks an unsafe proposal, a controller that refuses to move traffic to an unhealthy region).

Then the script runs the end-to-end smoke test, which starts every demo service as a local process (no Docker). **Expected** (the numbers can differ slightly):

```
baseline healthy: {'aws_only': 'healthy', 'aws_ripe': 'healthy', 'aws_ripe_http': 'healthy'}
injected 700 ms latency on the gateway path to region-a (the origin itself is untouched)
during fault: {'aws_only': 'healthy', 'aws_ripe': 'network_degradation', 'aws_ripe_http': 'network_degradation'}
decisions: 12, failover decisions: 7, gateway region-a weight: 75%
...
PASS: gray failure seen only with external signals; bounded failover applied on the local gateway
```

**What this shows:** the main story in one run. AWS-only stays `healthy` during a network fault, the two views with external signals report `network_degradation`, and the gateway moves to 75% Singapore. The last line must start with `PASS`.

### A2. Control-plane smoke test (optional, also run in our CI)

macOS and Linux:

```bash
.venv/bin/python scripts/smoke.py
```

Windows PowerShell:

```
.venv\Scripts\python scripts\smoke.py
```

**Expected:** some server log lines, then

```
PASS: auth, baseline, fault, healthy control plane, B routing, expiry, bounds, latency, loss, cleanup
```

**What this shows:** the lab control plane rejects unauthenticated requests, keeps traffic changes within bounds, and expires faults on time.

---

## 4. Part B: the Docker demo

The demo runs the full loop in 11 local containers:

```
local probes (stand-ins for AWS metrics, RIPE Atlas and the HTTP witness)
  -> collector -> outbox -> queue -> feature worker -> snapshots
  -> rule detector -> proposal -> confidence rules
  -> independent safety reviewer -> decision
  -> safety controller moves traffic on the LOCAL gateway only
```

It makes no AWS calls and uses no credentials. The values are real measurements of local containers, labelled as lab data.

| Container | Role |
|---|---|
| `demo-a`, `demo-b` | the Singapore and Virginia origins, kept separate so each is its own failure domain |
| `gateway` | stands in for CloudFront: network faults are applied on it, and its traffic split is the only thing the controller changes |
| `control-api` | the lab API behind the dashboard's fault buttons and manual routes (port 8000) |
| `lab-collector`, `lab-feature-worker` | mirror the real collector and feature worker, with a file queue in place of SQS |
| `lab-ml` | the detector, which only suggests; it also serves the decision state for the dashboard |
| `lab-controller` | acts only after the confidence rules, the safety reviewer and its own probes agree |
| `lab-users` | steady user traffic, so there is something to measure and move |
| `dashboard`, `demo-web` | the control room (:3000) and the Harbour Lane Coffee café website (:3001) |

Decisions in the demo come from transparent threshold rules (`demo/lab/detectors.py`). The trained ML models run only in shadow mode and never drive the controller; Part C explains why.

### B1. Start the demo

Make sure Docker Desktop is running and `python run.py check` shows no `FIX` line.

```bash
python run.py demo-up
```

**Expected:** the first run builds the images, which takes several minutes. It ends with

```
Demo dashboard: http://127.0.0.1:3000   cafe site: http://127.0.0.1:3001   debug: http://127.0.0.1:8192
```

The script also creates the lab's random local token (`demo/.env.demo`, not an AWS credential) and the `data/` folders, with the permissions Linux needs.

```bash
docker ps --format '{{.Names}}  {{.Status}}'
```

**Expected:** 11 containers whose names start with `adaptivenet-lab-` (for example `adaptivenet-lab-dashboard-1`, `adaptivenet-lab-gateway-1`, `adaptivenet-lab-lab-controller-1`), all `Up`, none `Restarting`.

### B2. Open the pages

Wait about 30 seconds, then open:

| Address | What you should see |
|---|---|
| http://127.0.0.1:3000 | **Control room for the local lab (main demo page).** Title "Can AWS-only monitoring see this failure?", a "Where customers are sent" bar at **100% Singapore · 0% Virginia**, three fault buttons (**Inject +1.5 s latency**, **Inject HTTP 503**, **Inject 80% packet loss**), three cards (AWS only, AWS + RIPE Atlas, AWS + RIPE + HTTP), all **Healthy**, and a **Shadow ML, side by side (never acts)** panel |
| http://127.0.0.1:3001 | **Harbour Lane Coffee**, the demo café that the lab's users visit through the gateway. The *Customer website* tab is what a customer sees; the *Behind the scenes* tab shows the load time and the serving region of each request, so you can watch customers move to Virginia during a fault |
| http://127.0.0.1:8192 | debug only: the raw decision state that the control room reads |
| http://127.0.0.1:8000/experiments | the lab API's experiment view |

The page on :3000 shows the local lab only. Live telemetry from our real AWS system is on a separate page, port 8766, served by the real pipeline that only group members can run (Part D). Lab faults never touch it, and you do not need it.

The "What AdaptiveNet is doing about it" row should read: Data in "a few s old", Detector sees **Healthy**, Detector suggests **Do nothing**, Safety review **No action**, Controller **Idle**.

### B3. Test 1: a network fault that AWS cannot see (the main test)

On http://127.0.0.1:3000:

1. Press **Reset demo** and wait about 30 seconds.
2. Leave **Region** on **Singapore** and **Duration** on **120 s**.
3. Press **Inject +1.5 s latency**.

This adds 1.5 seconds of delay on the network path to Singapore. The servers stay healthy, which is the kind of "gray failure" that monitoring inside AWS misses.

**Expected sequence** (the detection and first-move times are from our run on 18 Nov 2026, the rest from 13 Nov; yours may differ by some seconds):

| Time after pressing | What the page shows |
|---|---|
| 0 s | a red **FAULT ACTIVE** badge: "Singapore · +1500 ms latency" |
| about 15 s | the **AWS + RIPE + HTTP** card shows **Network degradation**, with "Detected *N* s after the fault started" |
| about 20 s | the **AWS + RIPE Atlas** card also shows **Network degradation** |
| the whole time | **AWS only** stays **Healthy** ("Not detected during this fault"), and a yellow banner says "AWS-only says healthy, but the external signals see a problem." |
| about 20–60 s | Detector suggests **Move 25% of traffic**, Safety review **Approved: move traffic**, Controller **Confirming** (it needs 2 of 3 signals to agree for 3 windows, then 3 fresh checks of its own) |
| about 75 s | the traffic bar moves to **75% Singapore · 25% Virginia** (76 s in our run) |
| about 2 minutes | if the fault is still active, a further step to **50% · 50%** |
| after the fault ends | the cards return to **Healthy**; Controller shows "Waiting: Singapore must stay healthy" |
| about 2 minutes after the fault ends | traffic steps back to **75%**, then **100% Singapore** (failback); Controller returns to **Idle** |

![Main demo page during Test 1 (18 Nov 2026), with the latency fault still active: AWS only stays Healthy, AWS + RIPE + HTTP detected the fault 2 s after it started and AWS + RIPE Atlas after 12 s, the safety review approved moving traffic, the traffic bar has moved to 75% Singapore · 25% Virginia, and the Shadow ML panel shows the trained models beside the rule detector without acting.](demo_fault_screenshot_20261118.png)

The "What happened, step by step" log, lower on the page, records each decision with a time stamp.

**What this shows:** with the same data, the view that uses only AWS-internal metrics cannot see a network-path fault, while the views that add outside measurements detect it. The system then moves traffic in bounded 25-point steps, only after the confidence rules, the safety reviewer and its own fresh checks agree, and moves it back once the region stays healthy.

### B4. Test 2: an HTTP error fault

1. Press **Reset demo** and wait about 30 seconds.
2. Press **Inject HTTP 503** (Singapore, 120 s).

**Expected:** within about 5 seconds, **AWS + RIPE Atlas** and **AWS + RIPE + HTTP** show **Network degradation** for Singapore while **AWS only** stays **Healthy**. (In this lab the 503 errors are injected on the gateway path that outside users take, so the servers' own metrics stay clean.) Safety review shows **Approved: move traffic**, and the traffic bar moves to **75% · 25%** (101 s after the fault started in our 18 Nov run). If the fault is still active, further 25-point steps follow. After the fault ends, traffic returns to **100% Singapore** in 25-point steps.

**What this shows:** a second fault type, detected only from outside, handled by the same safety chain.

### B5. Test 3: packet loss

1. Press **Reset demo** and wait about 30 seconds.
2. Press **Inject 80% packet loss** (Singapore, 120 s).

This drops 80% of requests on the gateway path to Singapore.

**Expected** (our run on 18 Nov 2026): both external views show **Network degradation** within about 5 s, **AWS only** stays **Healthy**, the traffic bar moves to **75% · 25%** at about 66 s and to **50% · 50%** at about 106 s, and traffic returns to **100% Singapore** after the fault ends.

**What this shows:** the same kind of fault as our real campaign (packet loss), which AWS metrics cannot see. A further check: with `--pct 50` (B6) the fault is detected but traffic does not move: the safety chain holds rather than move traffic on weaker evidence.

### B6. The same faults from the command line (optional)

```bash
python run.py fault lab-latency --region sg --ms 1500 --duration 120
python run.py fault lab-errors --region sg --rate 1.0
python run.py fault lab-loss --region sg --pct 80
python run.py status
```

**Expected:** JSON output containing `"status": "active"` and `"latency_ms": 1500`, `"error_rate": 1.0` or `"loss_pct": 80`; `status` shows the active faults. The main page then behaves as in B3, B4 or B5. Faults inside an origin (`app-5xx --region us --rate 0.5`, `cpu --region sg`, `region-down --region sg`) are described in `demo/DEMO.md`.

### B7. Reset and stop

```bash
python run.py demo-reset
python run.py demo-down
```

**Expected:** `demo-reset` clears the faults and restarts the gateway and lab controller (traffic back at 100% Singapore, faults cleared). `demo-down` prints `Removed` for each container and the network; `docker ps` then shows no `adaptivenet-lab-` containers.

### B8. The Shadow ML panel stays partly empty on your machine

The **Shadow ML** panel shows the rule detector (which drives decisions) and a baseline predictor. The trained models are not shipped with the repository, so their rows say "none loaded". Please skip `scripts/export_demo_models.py`, which needs those model files. This does not affect Tests 1 to 3. We can show the trained models live on our own machine, and the recorded results behind them are reproduced in Part C.

---

### B9. Alternative without run.py

The same demo commands exist as shell scripts, if you prefer them or `run.py` fails:

| `run.py` | macOS / Linux | Windows PowerShell |
|---|---|---|
| `python run.py demo-up` | `demo/run.sh lab-up` | `powershell -ExecutionPolicy Bypass -File demo\run.ps1 lab-up` |
| `python run.py demo-reset` | `demo/run.sh lab-reset` | `... demo\run.ps1 lab-reset` |
| `python run.py demo-down` | `demo/run.sh lab-down` | `... demo\run.ps1 lab-down` |
| `python run.py status` | `demo/run.sh status` | `... demo\run.ps1 status` |

On Linux, `demo/run.sh` needs this once first: `mkdir -p data/lab-live data/demo-replay data/demo-models && chmod -R a+rwX data`. The PowerShell route (`demo\run.ps1`) is the one we confirmed on a real Windows machine.

---

## 5. Part C: rerun our analysis from the saved data

Parts A and B use local stand-ins. The numbers in our article come from the **real** system: two EC2 regions, Amazon Managed Prometheus, four RIPE Atlas probes and an HTTP witness, recorded on 12 November (the 20-trial campaign) and 14 to 18 November (the live run and 10 more trials). Part C reruns the analysis on that recorded data with the committed code. No account is needed, because the data is already saved.

### C0. Get the saved data

Part C uses the Python environment that `python run.py setup` created. Activate it first and set the module path:

macOS / Linux:

```bash
source .venv/bin/activate
export PYTHONPATH=services:.
```

Windows PowerShell (then write `$env:CAMP` and `$env:LIVE` where the commands below write `$CAMP` and `$LIVE`, and type each command on one line without the `\` line breaks):

```
Set-ExecutionPolicy -Scope Process Bypass
.venv\Scripts\Activate.ps1
$env:PYTHONPATH = "services;."
$env:CAMP = "evidence/ripe-campaign-20261112"
$env:LIVE = "evidence/live-shadow-20261114"
```

On Windows, C2's `cmp` becomes `fc.exe /b <file1> <file2>`, and C4's `cp -r $LIVE /tmp/live-copy` becomes `Copy-Item -Recurse $env:LIVE $env:TEMP\live-copy` (then use `$env:TEMP\live-copy` as the live folder).

The recorded data is in two folders of the repository: `evidence/ripe-campaign-20261112/` (the 12 November campaign) and `evidence/live-shadow-20261114/` (the 14–18 November live run). Set two variables:

```bash
export CAMP=evidence/ripe-campaign-20261112
export LIVE=evidence/live-shadow-20261114
ls $CAMP $LIVE
```

**Expected:** the first folder holds `campaign-plan.json`, `campaign-log.jsonl`, `amp-region-a.jsonl`, `amp-region-b.jsonl`, `ripe/`, `trials/` and `http_witness.jsonl`; the second holds `live.json`, `amp-region-*.jsonl`, `ripe-region-*.json`, `http_witness.jsonl`, `exclusions-ssm.json`, `retrain-report.json` and `trials/`. Both also hold `witness.stdout`, the raw HTTP witness output.

| File or folder | What it is |
|---|---|
| `campaign-plan.json`, `campaign-log.jsonl`, `trials/` | the 20 pre-registered fault slots, when each fault ran, and the AWS SSM confirmation of each fault (the ground truth) |
| `amp-region-*.jsonl` | AWS-internal metrics exported from Amazon Managed Prometheus |
| `ripe/236862553.json`, `ripe/236862555.json` | the RIPE Atlas ping results for Singapore and Virginia |
| `http_witness.jsonl`, `witness.stdout` | the HTTP witness (one request every 15 s) |
| `$LIVE` folder | the 14–18 November live run: metrics, RIPE results, witness, the 10 extra fault trials, the SSM exclusion list and the saved scoring report |

The AWS account number and EC2 instance IDs in the SSM records are identifiers, not credentials.

### C1. The campaign result (AWS-only 0/20, AWS+RIPE 13/20)

```bash
python -m ml.team_starter.ripe_value study \
  --ripe region-a=$CAMP/ripe/236862553.json --ripe region-b=$CAMP/ripe/236862555.json \
  --campaign-dir $CAMP --campaign-plan $CAMP/campaign-plan.json
```

**Expected** (under a second): a line that starts with `campaign` and contains

```
"evaluable_episodes": 20, "both_detected": 0, "aws_only_only": 0, "aws_ripe_only": 13, "neither": 7,
"aws_only_recall": 0.0, "aws_ripe_recall": 0.65, ... "mcnemar_exact_p": 0.000244140625
... {"evaluable": 20, "aws_only": 0, "aws_ripe": 0}
```

The last part is the 20 same-time healthy controls: neither rule alarmed on any of them. The full report is written to `data/ml-results/native/ripe-value/ripe-value-report.json`.

**What this shows:** the article's main result. Each fault is a real firewall rule on one EC2 instance, confirmed by AWS SSM, so the labels do not depend on RIPE. The rules were frozen before the campaign. AWS metrics saw none of the 20 faults; adding RIPE found 13, with no false alarm, and the difference is significant (exact McNemar p = 0.00024).

### C2. What the controller would have done (action replay)

```bash
python scripts/action_replay_campaign.py --campaign-dir $CAMP \
  --http-deadline-ms 500 --http-deadline-ms 2000 --out /tmp/action-replay.json
cmp /tmp/action-replay.json docs/ml/action-replay-campaign.json && echo SAME
```

**Expected** (about 3 minutes): four summary lines, the first of which is

```
campaign {"trials": 20, "would_fail_over_away_from_impaired_region": 1, "failover_delays_s": [210],
 "false_actions_in_control_windows": 0, ... } healthy gaps: 1.58 h, 0 actions
```

then `SAME`: your output is byte-for-byte identical to the committed result.

**What this shows:** the real campaign data fed through the real decision path (detector, safety reviewer, confidence rules, safety controller). The safety chain is cautious: at our 500 ms HTTP deadline it would have moved traffic in 1 of 20 trials, after 210 s, and it never took a false action in the 20 control windows or 1.6 hours of healthy time. Most trials were held back because the HTTP witness did not also see the fault, and the controller needs 2 of 3 views to agree. We report this as a limit, not a success.

### C3. The ML results on unseen faults (amendment 10)

```bash
python -m ml.team_starter.amendment10 $CAMP $LIVE
```

**Expected** (about 30 seconds): a table headed `Amendment 10 (POST-HOC, ...)` that is identical to the section of the same name at the end of `docs/ml/live/WEDNESDAY_RESULTS.md`. Some rows to check:

| Detector | False alarms per region per day, as pre-registered | corrected |
|---|---|---|
| 13 Nov model (current/anomaly_aws_ripe_no_host) | 28.14, 77 onsets, 65.667 h | 25.68, 68 onsets, 63.542 h |
| frozen loss_only rule (comparator) | 5.12, 14 onsets | 2.64, 7 onsets |
| supervised S1 | 6.94, 19 onsets | 4.91, 13 onsets |
| supervised S2 | 21.93, 60 onsets | 17.75, 47 onsets |

and the last line: `AWS-only view of the frozen rule on the Tuesday trials: 0/10 trials detected, controls alarmed 0/10.`

**What this shows:** how we tested the ML honestly. The protocol (`docs/ml/LIVE_SHADOW_PROTOCOL.md`) was committed before the live data existed. Each model and its pass mark were frozen before 10 new faults on 17 November and before 65.7 hours of held-out healthy time were scored. **No ML model passed its pre-registered mark.** The simple loss rule again beat AWS-only (8 of 10 against 0 of 10, no control alarm, p = 0.008), which repeats the campaign result on new faults. After the run we found that the original count charged detectors for alarms during the real faults. We corrected this openly (amendment 10): both counts are shown and no verdict changed. This is why the ML stays in shadow mode and never moves traffic.

### C4. Full rescoring of the live run (optional, slow)

```bash
cp -r $LIVE /tmp/live-copy
python -m ml.team_starter.live_shadow retrain --campaign-dir $CAMP --live-dir /tmp/live-copy
```

**Expected** (about 3 minutes): this retrains and rescores every model on the live data and writes `/tmp/live-copy/RETRAIN.md` and `/tmp/live-copy/retrain-report.json`. It runs on a copy because it overwrites `retrain-report.json`, which C3 reads. The new `RETRAIN.md` matches the part of `docs/ml/live/WEDNESDAY_RESULTS.md` above the amendment 10 section, for example `current/anomaly_aws_ripe_no_host | 28.14 (22.21-35.17) | 77 | 65.667 | 17/20`. It has one extra context row, `context/frozen_aws_rule | 2.19 ...`, which was added with amendment 10; no other number changes. The command does not touch git (only the group's own run used `--commit`). C3 does not need C4.

---

## 6. Part D: the real AWS and RIPE Atlas system (optional)

The real system ran on the group's AWS account: EC2 web servers in Singapore and Virginia behind one CloudFront distribution, Amazon Managed Prometheus in each region, an SQS queue with a dead-letter queue, CloudWatch alarms, and an EKS cluster in Singapore with a Kubernetes HPA. It used RIPE Atlas ping measurements from four probes. **The repository contains no credentials.**

### D1. What we can offer instead of a rebuild

The group's AWS environment is still in place. **Please contact CSC3104 Group 03 if you would like a live demonstration or temporary access.** The two live tests we ran on 13 November are documented, with their raw evidence, in `docs/LIVE_TESTS_20261113.md`:

- *CloudFront failover:* we stopped the Singapore service for 60 s. All 108 requests through CloudFront returned HTTP 200; the 27 sent during the outage were served by Virginia.
- *EKS scale-up:* a bounded load job made the Kubernetes HPA scale from 2 to 4 pods; 10,762 requests, 0 failed.

In both tests AWS's own mechanisms acted (CloudFront origin failover, the HPA), not the AdaptiveNet controller, which only changes the local lab gateway. Each test ran once.

### D2. Rebuilding it in your own account

**We have never rebuilt the system in a fresh AWS account, so these steps are untested and may need fixes.** They need your own AWS account (it costs money while it runs), Terraform 1.13.5, the AWS CLI and, for EKS, Helm and kubectl. The RIPE part needs a RIPE Atlas account with an API key and enough credits.

| Step | Folder or script | What it creates |
|---|---|---|
| 1 | `infra/terraform/bootstrap` (copy `terraform.tfvars.example`) | the Terraform state bucket, an artifacts bucket and a GitHub OIDC role |
| 2 | `infra/terraform/environments/dev` (copy `backend.hcl.example`) | the SQS queue and dead-letter queue, IAM policies, CloudWatch alarms |
| 3 | `infra/terraform/environments/demo-cloud` | the two EC2 web servers (Singapore, Virginia) with their VPCs; Route 53 stays off (`enable_route53 = false`, see the README) |
| 4 | `infra/terraform/environments/platform` | Amazon Managed Prometheus per region; the EKS cluster only with `enable_eks = true` |
| 5 | `infra/terraform/environments/cloudfront` | the CloudFront distribution with Singapore as primary origin and Virginia as failover |
| 6 | `infra/helm/demo` | the EKS app with its HPA |
| 7 | `scripts/ripe_campaign_measurements.py` (reads `ATLAS_API_KEY`) | the RIPE Atlas ping measurements to both origins; this spends RIPE credits |

Several values are tied to our account and must be changed: the AMP workspace IDs and queue settings in `.env` (copy `.env.example`), the RIPE target addresses and measurement IDs, and the EC2 instance IDs in `scripts/aws_fault.py`. After that, install the AWS tools with `python run.py setup --cloud`, sign in to AWS (see `docs/ENV_SETUP.txt`), and start the real pipeline with `python run.py live --profile <your AWS profile>` (dashboard on port 8766; it polls AWS directly and does not use the SQS queue). Guarded faults run through `scripts/aws_fault.py`, which schedules its own recovery before it starts a fault. Always review `terraform plan` before `terraform apply`.

---

## 7. Troubleshooting

| Problem | Fix |
|---|---|
| `docker: command not found` on macOS | `export PATH=/Applications/Docker.app/Contents/Resources/bin:$PATH` |
| `Cannot connect to the Docker daemon` | start Docker Desktop (or `sudo systemctl start docker` on Linux) and wait until it is running |
| `port is already allocated` | another program uses a demo port (`python run.py check` lists them). Stop it, then run `python run.py demo-down` and `python run.py demo-up` |
| containers keep `Restarting`, logs show `Permission denied: '/app/data/...'` (Linux) | run `chmod -R a+rwX data`, then `python run.py demo-down` and `python run.py demo-up` |
| `ModuleNotFoundError` in Part C | activate the environment and set `PYTHONPATH` as in C0, and run from the repository root |
| cards say "unknown" or the data is old right after start | wait 30–60 seconds for the first probe rounds |
| demo seems stuck after many faults | `python run.py demo-reset`, then wait 30 seconds |
| `python` not found (macOS/Linux) | use `python3`; on Windows, reinstall Python with "Add python.exe to PATH" ticked, or use `py` |
| `No .venv yet. Run: python run.py setup` | run `python run.py setup` first |
| Part C: `FileNotFoundError` for a trial file | check that `$CAMP` and `$LIVE` point at the saved data folder, and that you run from the repository root |

---

## Appendix: how we checked these instructions

- **Parts A and C** (18 Nov 2026): on a clean Linux machine (Python 3.13) with a fresh copy of `main` at `d4e457a`: `python run.py setup` succeeded, `python run.py test` gave 500 passed, 0 failed, and the smoke test printed `PASS`; A2 printed `PASS`; C1 printed 0/20 against 13/20 and p = 0.000244; C3's output was identical to the amendment 10 section of `docs/ml/live/WEDNESDAY_RESULTS.md`. On earlier commits with the same analysis code, C2's output was byte-for-byte identical to `docs/ml/action-replay-campaign.json` (167 s), and C4 (158 s) reproduced every row of the official report and made no git commit. The group also ran `run.py` on macOS.
- **Part B** (18 Nov 2026, current 11-container demo): a fresh ZIP of the code installed, passed all tests and started the demo with `demo/run.sh lab-up`, which `python run.py demo-up` now wraps. Latency: detected in 15 s, 100→75% at 76 s. HTTP 503: detected in 5 s, 100→75% at 101 s. 80% packet loss: detected in 5 s, 100→75% at 66 s, 75→50% at 106 s. At 50% loss the fault was detected but traffic did not move. The longer sequence in B3 (second step, failback) is from our 13 Nov runs of the earlier 13-container version on a clean Linux machine.
- **Windows** (18 Nov 2026, plain PowerShell, before `run.py` existed): the tests and the smoke test passed after three fixes now in `main`, and the Docker demo ran through `demo\run.ps1`. `run.py` itself has not yet been run on Windows.
- **Not tested by us:** a rebuild of Part D in a fresh AWS account.
