# Three-class native study, false-alarm rate and the date confound

**Date:** 2026-11-12. **Mode:** offline, shadow only, no AWS calls.
**Inputs (private, not committed):** the team's archive `data/ml-archive/feature_snapshots.jsonl`
(3,249 snapshots, 2026-11-08 05:52 to 2026-11-10 19:39 UTC) and `http_witness.jsonl`, plus two
reviewed manifests: `data/verified_episodes.json` (6 application failures and 3 healthy sessions,
Nov 9) and `data/ml-results/native/step2-network-approved/verified_episodes.json` (6 network
degradation trials on Nov 10, plus 4 healthy controls).

## 1. What was combined, and how

The two manifests were merged without creating or relabeling any episode. The network study's 4
healthy controls are region-b sub-windows of the app study's healthy `both` sessions, so they were
dropped as duplicates and the wider windows were kept. Result: **15 reviewed episodes, all with an
explicit `experiment_run_id`**: 3 healthy, 6 application failure and 6 network degradation. That
gives 227 labeled snapshot rows.

| Class | Reviewed runs | Status |
|---|---:|---|
| healthy | 3 | trainable |
| application_failure | 6 | trainable |
| network_degradation | 6 | trainable |
| partial_outage | 0 | needs ≥2 independent vantage points per episode |
| transient | 0 | no reviewed phase |
| recovery | 0 | no reviewed phase (Step 6.7 measured alert clearance only, not labels) |

## 2. Why a new split was needed

The experiments were run in class blocks: every app failure and every healthy session on Nov 9,
every network trial on Nov 10. The existing global chronological split therefore puts **all** app
failures and healthy runs in train and **only** network trials in validation and test, so no
multiclass test is possible. `coverage_audit` reports this directly.

`--split class_stratified` (new, opt-in; the default is unchanged) holds out the latest run of each
class as test and the one before as validation when a class has ≥4 runs. Whole runs stay together,
and a 900 s embargo still applies. It is **weaker** than a forward-in-time split, because training
contains Nov 10 network runs that were recorded after the Nov 9 test runs. Rows are still built as-of,
so no future observation enters any row.

## 3. Held-out results (1 test run per class, 31 rows)

| Arm | Macro F1 | App-failure P / R | Healthy recall | Network P / R | Missed incidents | False alarms on held-out healthy |
|---|---:|---:|---:|---:|---:|---:|
| Fixed rule, AWS only | 0.19 | 0 / 0 | 1.00 | 0 / 0 | 2 of 2 | 0 |
| AWS only | 0.52 | 0.17 / 1.00 | 0.17 | 1.00 / 1.00 | 0 of 2 | 2 |
| AWS + RIPE | 0.61 | 0.20 / 1.00 | 0.33 | 1.00 / 1.00 | 0 of 2 | 3 |
| AWS + RIPE + HTTP | 1.00 | 1.00 / 1.00 | 1.00 | 1.00 / 1.00 | 0 of 2 | 0 |

Median detection delay from the verified onset was 18.0 s for every trained arm. There were no
abstentions in test. The multiclass Brier scores were 0.50 (AWS only), 0.40 (AWS + RIPE) and 0.008
(AWS + RIPE + HTTP).

**False alarms per day** (`coverage_audit`, exposure = verified healthy test duration of 0.20 h):
AWS only 240/day (95% CI 29–867), AWS + RIPE 360/day (74–1,052), AWS + RIPE + HTTP 0/day (0–443).
These are **indicative only**. With 12 minutes of held-out healthy time, the intervals are too wide
to support a claim.

With one test run per class, these are case studies, not reliable estimates.

## 4. The date confound (most important finding)

The trained models were replayed (`offline_cli replay`) over **2026-11-10 13:45–17:55 UTC**, an
*unreviewed* window that starts 20 min after the last network trial ended. In that window, every
HTTP witness check returned 200, `app_up` = 1 in both regions, internal AWS data was fresh, and
region-b RIPE loss was 0 apart from one 20-minute bucket (max 25%).

| Arm | network_degradation | application_failure | healthy | abstain |
|---|---:|---:|---:|---:|
| AWS only | 190 | 33 | 6 | 20 |
| AWS + RIPE | 188 | 17 | 19 | 25 |
| AWS + RIPE + HTTP | 120 | 1 | 107 | 21 |

The AWS-only arm calls a quiet afternoon "network degradation" because region-b
`memory_available_pct` drifted from about 59.5% (Nov 9, when every healthy control was recorded) to
about 57.6% (all of Nov 10). The model learned the **date**, not the network condition. That
probably also inflates the earlier binary study's AWS-only network results, whose healthy controls
are also from Nov 9.

These counts are **not** errors or false alarms until a human reviews the window. If it is reviewed
as healthy, it:
1. adds about 4.2 h of held-out healthy exposure, the first real false-alarm denominator for the network test block;
2. gives the latest healthy run, so the chronological and class-stratified splits both test on it; and
3. lets the paired comparison show that AWS-only network "detection" is spurious, while the HTTP arm is far less affected.

The RIPE value study (PR #23) independently confirms the confound: AWS-only retrained without host columns raises 0 alerts here. It also finds a Virginia RIPE rule alert on 8 of 236 rounds (one onset at 16:27 UTC) and Singapore RTT inflation on probe 13106. The review must therefore rest on "no fault injected", not on external quietness. If the window is approved, those alerts count as false alarms. If the 16:20–16:40 external path is judged genuinely degraded, that sub-window is excluded rather than labelled healthy.

The review request, with the checks the reviewer must make, is kept with the private evidence at
`adaptivenet-evidence/reviews/healthy-control-nov10-pm-REVIEW-REQUEST.json`.

## 5. Reproduce

```bash
python -m pip install -e '.[ml]'
python -m ml.team_starter.coverage_audit --labels unified_verified_episodes.json --split class_stratified
python -m ml.team_starter.offline_cli dataset --snapshots data/ml-archive/feature_snapshots.jsonl \
  --witness data/ml-archive/http_witness.jsonl --labels unified_verified_episodes.json --out dataset.json
python -m ml.team_starter.offline_cli train --dataset dataset.json --split class_stratified --out models/
python -m ml.team_starter.coverage_audit --labels unified_verified_episodes.json --dataset dataset.json \
  --predictions models/test-predictions-aws_only.json --split class_stratified
python -m ml.team_starter.offline_cli replay --models models/*.pkl \
  --snapshots data/ml-archive/feature_snapshots.jsonl --witness data/ml-archive/http_witness.jsonl \
  --start-utc 2026-11-10T13:45:00Z --end-utc 2026-11-10T17:55:00Z --out replay.json
```

Model IDs from this run: `logreg-aws_only-e053dc49d57c4e3f`, `logreg-aws_ripe-7841c8bd039d87ba`,
`logreg-aws_ripe_http-4c89b96a51612734` (scikit-learn 1.9.1).

## 6. Still required

- A human review of the Nov 10 afternoon window, plus new passive healthy controls on the **same days** as fault runs, so that date cannot separate the classes.
- Fault runs interleaved with healthy controls in time (not class blocks), so the stronger chronological split can be used.
- `transient` / `recovery` phases labeled inside their incident runs, and `partial_outage` from multi-probe RIPE evidence.
