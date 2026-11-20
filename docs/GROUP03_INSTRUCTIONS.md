---
title: "AdaptiveNet: How to Test Our System"
---

# AdaptiveNet: how to test our system

**CSC3104 Group 03** · Repository: https://github.com/Linus-Tay/adaptivenet (branch `main`) · Time: about 40 minutes, most of it the first Docker build

AdaptiveNet watches a web service in two AWS regions, Singapore and Virginia. It combines AWS's own metrics with measurements from outside AWS (RIPE Atlas internet probes and real HTTP requests), decides whether a region is degraded, and moves traffic only after independent safety checks pass. The claim to test: **some network failures cannot be seen from inside AWS, and outside measurements can see them.**

| Part | What you do | Time | What it shows |
|---|---|---|---|
| **A. Tests** | run 502 automated tests and a smoke test | 5 min | each component behaves as designed, including the failure cases |
| **B. Docker demo** | start the demo in Docker and inject three faults from a web page | 20 min | a fault that AWS-only monitoring misses is caught from outside, traffic moves away in safe steps and comes back |
| **C. Our analysis** | rerun three analyses on the data we recorded on the real AWS system | 5 min | the article's main numbers come from the saved data and the committed code |

**No AWS account, RIPE account or key is needed. Nothing here contacts AWS.**

**How to use this guide.** Every command is in a grey box. Each step has one box for **macOS / Linux** and one box for **Windows**. Copy the whole box for your computer, paste it into the terminal and press Enter. Nothing in a box needs to be changed.

## 1. Download and install

**Step 1. Install these two programs.**

| You need | Where |
|---|---|
| **Python 3.11 or newer** | https://www.python.org/downloads/ (on Windows, tick **"Add python.exe to PATH"** on the first screen of the installer) |
| **Docker Desktop** | https://www.docker.com/products/docker-desktop/ (after installing, start Docker Desktop and wait until it says it is running) |

**Step 2. Download the code.** Download https://github.com/Linus-Tay/adaptivenet/archive/refs/heads/main.zip and unzip it. You get a folder named `adaptivenet-main`. (With Git you can use `git clone https://github.com/Linus-Tay/adaptivenet.git` instead; the folder is then named `adaptivenet`.)

**Step 3. Open a terminal inside that folder.**

- **Windows:** open the `adaptivenet-main` folder in File Explorer until you see the file `run.py`. Click the address bar at the top of the window, type `powershell` and press Enter.
- **macOS:** open the Terminal app. Type `cd` followed by a space, drag the `adaptivenet-main` folder onto the Terminal window, and press Enter.

**Step 4. Check that you are in the right folder.** Type `ls` and press Enter. The list must include `run.py`. Keep this terminal window open and use it for every command in this guide.

## 2. Two ways to run every step

**Way 1 (use this one): our script `run.py`.** Sections 3 and 4 use it. It works the same on macOS, Linux and Windows.

**Way 2 (only if a `run.py` command fails): the same steps by hand.** Run each box one line at a time.

macOS / Linux, setup:

```
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.lock -r ml/team_starter/requirements-ml-inference.lock
pip install --no-deps -e .
export PYTHONPATH=services:.
```

macOS / Linux, Part A (the tests, then the smoke test):

```
python -m pytest -q tests demo/tests ml/team_starter/tests step7_finish step8_finish
python -m demo.smoke_local
```

macOS / Linux, Part B (start the demo, reset it, stop it). On Linux only, not macOS, first run `mkdir -p data/lab-live data/demo-replay data/demo-models && chmod -R a+rwX data`.

```
demo/run.sh lab-up
demo/run.sh lab-reset
demo/run.sh lab-down
```

Windows (PowerShell), setup:

```
python -m venv .venv
Set-ExecutionPolicy -Scope Process Bypass
.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.lock -r ml\team_starter\requirements-ml-inference.lock
python -m pip install --no-deps -e .
$env:PYTHONPATH = "services;."
```

Windows (PowerShell), Part A (the tests, then the smoke test):

```
python -m pytest -q tests demo/tests ml/team_starter/tests step7_finish step8_finish
python -m demo.smoke_local
```

Windows (PowerShell), Part B (start the demo, reset it, stop it):

```
powershell -ExecutionPolicy Bypass -File demo\run.ps1 lab-up
powershell -ExecutionPolicy Bypass -File demo\run.ps1 lab-reset
powershell -ExecutionPolicy Bypass -File demo\run.ps1 lab-down
```

The expected results are the same as in sections 3 and 4. Part C (section 5) does not use `run.py`, so its boxes are the same for both ways.

### Step 5. Check and install

macOS / Linux:

```
python3 run.py check
python3 run.py setup
```

Windows (PowerShell):

```
python run.py check
python run.py setup
```

**Expected:** `check` prints one line per item. Each line is marked `OK`, or `FIX` followed by what to do. It ends with `All required prerequisites are present.` The lines `.venv not created yet` and `AWS CLI not found` are fine. `setup` takes about a minute and ends with `Setup done. Next:  python run.py test`.

---

## 3. Part A: automated tests

macOS / Linux:

```
python3 run.py test
```

Windows (PowerShell):

```
python run.py test
```

**Expected** (1 to 2 minutes). Many lines are printed. Look for these two results.

First, the test run ends with this line (the time at the end of the line will differ):

```
502 passed, 1 warning, 4 subtests passed
```

The warning is a library deprecation notice. **No test should fail.**

Second, the smoke test, which runs every demo service as a local process without Docker, ends with lines like these:

```
during fault: {'aws_only': 'healthy', 'aws_ripe': 'network_degradation', 'aws_ripe_http': 'network_degradation'}
decisions: 12, failover decisions: 7, gateway region-a weight: 75%
...
PASS: gray failure seen only with external signals; bounded failover applied on the local gateway
```

The two counts of decisions can differ slightly. The last line must start with `PASS`.

**What this shows.** The tests cover data collection (AWS Prometheus, RIPE Atlas, CloudFront), the queue with outbox, duplicate removal and dead-letter queue, feature snapshots, the safety reviewer, the confidence rules, the safety controller (failover, failback, scaling) and the ML code, all with fixtures in place of AWS. The smoke test is the main story in one run: during a network fault the AWS-only view stays `healthy`, the views with outside signals report `network_degradation`, and traffic moves to 75% Singapore.

---

## 4. Part B: the Docker demo

The demo runs the whole loop in 11 local containers: two origins (Singapore, Virginia), a gateway that stands in for CloudFront, a collector and feature worker, the detector, the safety controller, simulated users, the control-room page and a demo café website. The controller can only change the split on the local gateway.

### Step 1. Start the demo

Docker Desktop must be running.

macOS / Linux:

```
python3 run.py demo-up
```

Windows (PowerShell):

```
python run.py demo-up
```

**Expected:** the first run builds the images, which takes several minutes. It ends with this line:

```
Demo dashboard: http://127.0.0.1:3000   cafe site: http://127.0.0.1:3001   debug: http://127.0.0.1:8192
```

### Step 2. Open the two pages

Wait about 30 seconds, then open these two addresses in a web browser:

- **http://127.0.0.1:3000** is the control room. You should see the title "Can AWS-only monitoring see this failure?", a traffic bar at **100% Singapore · 0% Virginia**, three fault buttons, and three cards named **AWS only**, **AWS + RIPE Atlas** and **AWS + RIPE + HTTP**, all showing **Healthy**. The pipeline row (Detector, Safety review, Controller) shows **Do nothing / No action / Idle**.
- **http://127.0.0.1:3001** is Harbour Lane Coffee, the café that the simulated users visit. Its *Behind the scenes* tab shows each request's load time and which region served it.

All three tests below are done with the buttons on the control room page (http://127.0.0.1:3000).

### Test 1: a network fault that AWS cannot see (the main test)

1. Press **Reset demo** and wait 30 seconds.
2. Leave the **Region** box on **Singapore** and the **Duration** box on **120 s**.
3. Press **Inject +1.5 s latency**.

The servers stay healthy; only the network path to Singapore becomes slow. Watch the page for about 5 minutes.

| After about | What you should see |
|---|---|
| 0 s | red **FAULT ACTIVE** badge |
| 5–15 s | **AWS + RIPE + HTTP** and then **AWS + RIPE Atlas** show **Network degradation**; **AWS only** stays **Healthy** the whole time, with a yellow banner "AWS-only says healthy, but the external signals see a problem." |
| 20–60 s | Detector suggests **Move 25% of traffic**, Safety review **Approved: move traffic**, Controller **Confirming** |
| 75 s | traffic bar **75% Singapore · 25% Virginia** |
| later | while the fault lasts, possibly a further step to 50 · 50 |
| about 2 min after the fault ends | traffic steps back to **100% Singapore**; Controller **Idle** |

![The control room during Test 1: AWS only is Healthy, both outside views show Network degradation, the safety review approved moving traffic, and traffic is at 75% Singapore · 25% Virginia.](demo_fault_screenshot_20261118.png)

**What this shows:** with the same data, the AWS-only view misses a network-path fault and the views with outside measurements catch it. Traffic then moves in bounded 25-point steps, only after the confidence rules, an independent safety reviewer and the controller's own checks agree, and moves back when the region is healthy.

### Test 2: HTTP errors

1. Press **Reset demo** and wait 30 seconds.
2. Press **Inject HTTP 503**.

**Expected:** within about 5 s both outside views show **Network degradation** while **AWS only** stays **Healthy**. Traffic moves to **75 · 25** (after about 100 to 120 s in our runs), and back to 100% Singapore after the fault ends.

### Test 3: packet loss

1. Press **Reset demo** and wait 30 seconds.
2. Press **Inject 80% packet loss**.

**Expected:** both outside views flag Singapore within about 10 s and **AWS only** stays **Healthy**. In most runs traffic then moves to **75 · 25** within about 2 minutes, sometimes on to **50 · 50**, and back to 100% after the fault ends. Occasionally the safety chain holds and traffic does not move, as in our campaign replay. This is the same kind of fault as our real campaign.

The **Shadow ML** panel on the control room shows the rule detector and a baseline. The trained models are not shipped, so their rows say "none loaded". That is expected and does not affect the tests.

### Step 3. Stop the demo

macOS / Linux:

```
python3 run.py demo-down
```

Windows (PowerShell):

```
python run.py demo-down
```

**Expected:** all the demo containers are removed.

---

## 5. Part C: rerun our analysis from the recorded data

The article's numbers come from the real system (two EC2 regions, Amazon Managed Prometheus, four RIPE Atlas probes, an HTTP witness), recorded on 12 November (20-trial campaign) and 14–18 November (live run and 10 more faults). The recordings are already in the code you downloaded, in the folder `evidence`. Nothing needs to be downloaded or edited.

Do Step 5 (Check and install, in section 2) first. Use the same terminal window, in the folder that contains `run.py`. For each check, copy the **whole** box, paste it and press Enter.

### C1. The campaign: AWS-only 0/20, AWS + RIPE 13/20

macOS / Linux:

```
PYTHONPATH=services:. .venv/bin/python -m ml.team_starter.ripe_value study \
  --ripe region-a=evidence/ripe-campaign-20261112/ripe/236862553.json \
  --ripe region-b=evidence/ripe-campaign-20261112/ripe/236862555.json \
  --campaign-dir evidence/ripe-campaign-20261112 \
  --campaign-plan evidence/ripe-campaign-20261112/campaign-plan.json
```

Windows (PowerShell):

```
$env:PYTHONPATH = "services;."
.venv\Scripts\python.exe -m ml.team_starter.ripe_value study `
  --ripe region-a=evidence/ripe-campaign-20261112/ripe/236862553.json `
  --ripe region-b=evidence/ripe-campaign-20261112/ripe/236862555.json `
  --campaign-dir evidence/ripe-campaign-20261112 `
  --campaign-plan evidence/ripe-campaign-20261112/campaign-plan.json
```

**Expected** (about one second): four lines are printed. The first line starts with the word `campaign`. Find these values in it:

| In the first line | Meaning |
|---|---|
| `"aws_only_only": 0` | faults that only the AWS-only view caught: 0 |
| `"aws_ripe_only": 13` | faults that only the AWS + RIPE view caught: 13 |
| `"neither": 7` | faults that neither view caught: 7 |
| `"aws_only_recall": 0.0, "aws_ripe_recall": 0.65` | AWS-only caught 0 of 20; AWS + RIPE caught 13 of 20 |
| `"mcnemar_exact_p": 0.000244140625` | the p-value of the comparison |
| `{"evaluable": 20, "aws_only": 0, "aws_ripe": 0}` at the end | 20 healthy control periods, no false alarm in either view |

**What this shows:** our main result. Each fault was a real firewall rule on one EC2 instance, confirmed by AWS; the rules were frozen beforehand. AWS metrics saw none of the 20 faults, adding RIPE found 13 with no false alarm (exact McNemar p = 0.00024).

### C2. What the safety controller would have done

This one takes about 3 minutes. Wait until the prompt comes back.

macOS / Linux:

```
PYTHONPATH=services:. .venv/bin/python scripts/action_replay_campaign.py \
  --campaign-dir evidence/ripe-campaign-20261112 \
  --http-deadline-ms 500 --http-deadline-ms 2000 --out replay.json
cmp replay.json docs/ml/action-replay-campaign.json && echo IDENTICAL
```

Windows (PowerShell):

```
$env:PYTHONPATH = "services;."
.venv\Scripts\python.exe scripts/action_replay_campaign.py `
  --campaign-dir evidence/ripe-campaign-20261112 `
  --http-deadline-ms 500 --http-deadline-ms 2000 --out replay.json
```

**Expected:** four long lines are printed. The first line starts with the word `campaign`. Find these values in it:

| In the first line | Meaning |
|---|---|
| `"trials": 20` | all 20 faults of the campaign were replayed |
| `"would_fail_over_away_from_impaired_region": 1` | the controller would have moved traffic in 1 of the 20 |
| `"failover_delays_s": [210]` | in that trial the delay before moving traffic was 210 s |
| `"false_actions_in_control_windows": 0` | no action during the healthy control periods |
| `healthy gaps: 1.58 h, 0 actions` at the end | no action in 1.58 hours of healthy time between trials |

On macOS / Linux the box then prints `IDENTICAL`. It means the result file you just produced, `replay.json`, is the same, byte for byte, as the file `docs/ml/action-replay-campaign.json` in our repository.

**What this shows:** the real data fed through the real decision chain. It is cautious: it would have moved traffic in 1 of 20 trials and never took a false action. We report this as a limit.

### C3. The ML models on unseen faults

macOS / Linux:

```
PYTHONPATH=services:. .venv/bin/python -m ml.team_starter.amendment10 \
  evidence/ripe-campaign-20261112 evidence/live-shadow-20261114
```

Windows (PowerShell):

```
$env:PYTHONPATH = "services;."
.venv\Scripts\python.exe -m ml.team_starter.amendment10 `
  evidence/ripe-campaign-20261112 evidence/live-shadow-20261114
```

**Expected** (about 30 seconds): two tables are printed under the heading `## Amendment 10 (POST-HOC, ...)`. Check three things:

1. In the first table, the row `frozen loss_only rule (comparator)` reads `5.12 (2.8-8.59), 14 onsets, 65.667 h | 2.64 (1.06-5.45), 7 onsets, 63.542 h`.
2. In the second table, all three rows say `did not pass` in the middle column.
3. The last line reads `**AWS-only view of the frozen rule on the Tuesday trials:** 0/10 trials detected, controls alarmed 0/10.`

The whole output is identical to the end of the file `docs/ml/live/WEDNESDAY_RESULTS.md`.

**What this shows:** each model and its pass mark were frozen before 10 new faults and 65.7 hours of held-out healthy time. **No ML model passed**, which is why ML stays in shadow mode and never moves traffic. The simple loss rule again beat AWS-only on the new faults (8 of 10 against 0 of 10). A counting error found afterwards is corrected openly, with both counts shown.

---

## 6. If something goes wrong

| Problem | Fix |
|---|---|
| `python` or `python3` is not found | Windows: reinstall Python with "Add python.exe to PATH" ticked, then open a new PowerShell window. macOS: install Python from python.org |
| `can't open file ... run.py` | the terminal is not in the right folder; repeat section 1, Steps 3 and 4 |
| `check` says Docker is not running | open Docker Desktop and wait until it is running |
| a demo port is in use (3000, 3001, 8000, 8080, 8101, 8102, 8192) | close the program using it (8080 is often LM Studio), then run the `demo-down` box and the `demo-up` box again |
| cards say "unknown" right after start | wait 30–60 s |
| demo seems stuck after many faults | press **Reset demo** on the control room page and wait 30 s |
| Part C says `No such file or directory` or `cannot find the path` for `.venv` | run the `setup` box in Step 5 (section 2) first |
| `ModuleNotFoundError` in Part C | copy the whole box again, including its first line |

The live AWS and RIPE Atlas parts are not needed for these tests; we can show them on request. Tested by us on macOS and Linux; on Windows we ran the tests, the smoke test and the demo through Way 2.
