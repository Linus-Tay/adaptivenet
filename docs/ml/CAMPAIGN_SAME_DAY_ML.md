# Same-day ML retrain on the RIPE campaign (shadow result, not the main claim)

**Date:** 2026-11-13. **Mode:** offline, shadow only. No AWS calls, no change to any
detector in production, and the frozen RIPE rule was not tuned. The main claim stays in
`RIPE_CAMPAIGN_RESULTS.md` (frozen rule, AWS-only 0/20 vs AWS+RIPE 13/20).

## Why

Models trained on Nov 9 healthy data raised 58–150 false alarms a day on the reviewed
Nov 10 healthy window, because host memory drifted between days
(`SIX_CLASS_AND_FALSE_ALARM_STUDY.md`). MY approved retraining on healthy data from the
same evening as the 12–13 Nov campaign trials, then testing on the 20 trials and on
held-out healthy time.

## What was done

Code: `ml/team_starter/campaign_ml.py`. The protocol (constants, splits, thresholds) was
committed in 2cac3fd **before** the first scoring run.

* **Rows:** one row every 30 s per region (AMP export), 2026-11-12 15:14 to 2026-11-13
  00:04 UTC. Features are as-of: the AMP values at t, plus the per-probe RIPE path
  features from `ripe_value.path_features` using results with timestamp ≤ t.
* **Labels:** *impaired* is the SSM request to request + 960 s of the 20 confirmed slots.
  *Healthy* is everything outside every submitted slot (confirmed or not), with 60 s
  before and 600 s after each slot dropped, never labelled. Slot 1 (no rule added) is
  excluded entirely. That gives 1,012 healthy rows (8.43 h) and 640 impaired rows.
* **Dead probe:** 1010367 is removed for Singapore only. It answered 0 of 408 pings to
  Singapore and 379 of 392 to Virginia.
* **Models, one per region:** a *supervised* logistic regression (healthy vs impaired,
  p ≥ 0.55, the same setup as the earlier native arms), and an *anomaly* model
  (IsolationForest trained on healthy rows only; it alerts above the 99.5th percentile of
  its own training scores). Each model is run with AWS-only features and with AWS+RIPE,
  with and without host columns (memory, CPU, load).
* **Splits (no leakage):**
  * *Blocked:* 5 contiguous time folds, cut only between slots (4 slots per fold). Each
    fold is scored by a model that never saw its rows, with a 900 s embargo either side,
    so all 20 trials and all 8.43 healthy hours are scored out-of-sample.
  * *Forward:* train on the campaign before 19:52 UTC and test after it (last 10 trials,
    3.94 healthy h). This is the stricter split, since the model never sees later data.
* **False alarms** = alert onsets on held-out healthy rows. A run of consecutive alert
  rows counts once. Exact Poisson 95% CI.

## Results (blocked split, all 20 trials, 8.43 h held-out healthy)

| Detector | Trials detected | False alarms/day (95% CI) | Healthy rows in alert | Same-time controls alarmed |
|---|---:|---:|---:|---:|
| **Frozen rule AWS+RIPE (main claim, unchanged)** | 13/20 | 0 (0–10.5) | 0% | 0/20 |
| Frozen rule AWS-only | 0/20 | 0 (0–10.5) | 0% | 0/20 |
| **Anomaly ML, AWS+RIPE, no host** | 17/20 | 14 (4.6–33) | 0.7% | 1/20 |
| Anomaly ML, AWS+RIPE | 16/20 | 14 (4.6–33) | 1.2% | 1/20 |
| Anomaly ML, AWS-only | 3/20 | 23 (9.8–45) | 1.4% | 2/20 |
| Anomaly ML, AWS-only, no host | 0/20 | 0 (0–10.5) | 0% | 0/20 |
| Supervised ML, AWS+RIPE, no host | 19/20 | 23 (9.8–45) | 8.4% | 4/20 |
| Supervised ML, AWS+RIPE | 20/20 | 94 (65–132) | 12% | 8/20 |
| Supervised ML, AWS-only | 19/20 | 236 (188–293) | 23% | 14/20 |
| Supervised ML, AWS-only, no host | 2/20 | 2.8 (0.1–16) | 0.1% | 1/20 |

The forward split agrees: anomaly AWS+RIPE caught 9/10 trials at 18/day (3.8–53), and the
frozen rule caught 5/10 at 0/day. Every row is in `campaign-same-day-ml.json`
(private, not committed).

Anomaly AWS+RIPE without host columns missed slots 12 (Virginia 25%), 13 (Singapore 10%)
and 16 (Virginia 15%). It caught 6/6 at 35% loss. Its median delay from the SSM request
was 296 s, against 471 s for the frozen rule on the same rows.

## What it means (plain)

1. **Same-day training cuts false alarms, but doesn't remove them.** The best model
   (anomaly, AWS+RIPE) goes from 58–150 a day on Nov 10 to about 14 a day (95% CI
   5–33). This isn't a like-for-like comparison: it's a different healthy window, and
   8.4 h is short.
2. **It's still worse than the frozen rule on false alarms.** The rule raised 0 in the
   same 8.4 h. The ML catches 3–4 more trials (mostly at 10–25% loss) and alerts about
   3 min sooner, but at roughly 14 false alarms a day per region (the 8.43 healthy hours are summed over both regions, about 4.2 h of clock time). Not a good trade for a pager.
3. **The AWS-only "detections" are not real.** Supervised AWS-only scores 19/20, but it
   drops to 2/20 once memory/CPU/load are removed, and it raises 236 false alarms a day.
   The app metrics are flat during trials, so the model is picking up small host
   changes, not the network. The same confound as Nov 10, now inside one day. AWS-only
   ML without host columns detects 0/20, matching the frozen AWS-only rule.
4. **RIPE carries the signal.** Every ML arm that detects trials honestly has RIPE
   features in it.

## Caveats

* One evening, one kind of fault (random drop), 4 probes (3 of which reached Singapore), 8.4 region-hours of healthy
  time. The false-alarm CIs are wide.
* Healthy rows sit between trials, so they're close in time to impairments. That
  suits same-day training, but it's not a quiet day.
* The probe-exclusion fix (Singapore only) was made after the first run and before
  write-up. The first run removed 1010367 from both regions and scored the frozen rule at
  12/20. With the fix, the frozen rule gives 13/20, which reproduces the main result
  and confirms the rows are built correctly. AWS+RIPE ML arms gained 1–2 detected trials with the fix; false alarms for the anomaly arms did not change.
* ML stays shadow-only. It must not replace or gate the frozen rule.

## Reproduce

```bash
C=/path/to/adaptivenet-ripe-campaign
python -m ml.team_starter.campaign_ml --campaign-dir $C --campaign-plan $C/campaign-plan.json \
  --ripe region-a=$C/ripe/236862553.json --ripe region-b=$C/ripe/236862555.json --out $C/results/same-day-ml
```
