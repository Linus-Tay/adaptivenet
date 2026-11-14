# Live shadow run, 14-18 Nov 2026 (pre-registered, shadow only)

Committed on 2026-11-14 **before** the first live RIPE result exists. The machine-readable
protocol is `LIVE_PROTOCOL` in `ml/team_starter/live_shadow.py`; nothing in it may change
once live data arrives. The ML never triggers an action, and the frozen RIPE rule is not tuned.

**Goal.** See whether retraining the 13 Nov anomaly model on several more days of healthy
data cuts its false alarms (about 14/day on 8.4 held-out hours, CI 4.6-33), and what it
costs in detection.

**Data.** Same 4 RIPE probes and 240 s pings as the campaign (new measurements, stop
Wed 18 Nov 01:00 UTC), the same 15 s HTTP witness, and a read-only AMP export at 30 s.
No fault trials. Probe 1010367 is dropped for Singapore only.

**Split.** Held-out = the last 36 h (16 Nov 13:00 to 18 Nov 01:00 UTC). Live training =
live start to held-out start minus a 900 s embargo.

**Models.** Same class and hyperparameters as `campaign_ml.PROTOCOL` (IsolationForest,
300 trees, seed 42, alert above the 99.5th percentile of training scores).
* *Current*: trained on every campaign healthy row.
* *Retrained*: campaign healthy rows + live training rows.
* Primary: AWS+RIPE without host columns. Secondary (declared now): with host columns,
  and a 2-consecutive-row persistence rule on each (proposed on 13 Nov, never scored on
  live data).

**Metrics.** False alarms = alert onsets on held-out live rows outside every exclusion
window (exact Poisson 95% CI). Detection = the 20 campaign trials, campaign_ml blocked
5-fold split; the retrained model adds the live training rows to every fold's training set.

**Exclusions.** Every SSM command in either region during the run (read-only
`ssm list-commands`) and any incident the operator reports, padded 60 s before and 600 s
after, fixed before the retrain is scored.

**Daily snapshot.** `scripts/live_shadow_daily.sh` (launchd, 09:15 SGT) writes alert
onsets per Singapore day for the current model and the frozen rules. Live time is
unreviewed until Wednesday, so these are alert onsets on presumed-healthy time, not
confirmed false alarms. Detection cannot be measured on the live run (no trials).

**Smoke check (not a result).** Running the pipeline on the campaign files reproduces the
published blocked detections for the current model (17/20 without host, 16/20 with host).

**Amendment 1 (2026-11-14, before any live data).** The Mac will be off from about
15 Nov 03:00 SGT to 16 Nov 14:00 SGT, so the witness traffic stops. Rows with no witness
record in the 60 s before them are dropped from both training and held-out, using the
witness log only. The held-out window (16 Nov 21:00 SGT onwards) falls after the Mac is
back. Live training is then about 18 h on 14 Nov plus about 7 h on 16 Nov.

**Amendment 2 (2026-11-14 04:40 UTC, before any snapshot or scoring).** MY corrected the
Mac's off time: Monday 16 Nov 03:00 to 14:00 SGT, not from Sunday. No rule changes: the
witness-gap rule works from the witness log alone, and the held-out window (16 Nov 21:00
SGT onwards) still begins after the Mac is back. Live training becomes about 42 h
(14 Nov 08:55 to 16 Nov 03:00 SGT) plus about 7 h on 16 Nov.
