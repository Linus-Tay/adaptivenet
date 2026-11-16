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

**Amendment 3 (2026-11-14 05:30 UTC, before any snapshot or scoring): candidate search.**
MY asked to look for training changes that cut false alarms or improve detection. The
search space and selection rule (`CANDIDATE_SEARCH` in `live_shadow.py`) are fixed now:
model (IsolationForest, EllipticEnvelope, robust max-z), features (AWS+RIPE without host,
the same without the RTT-inflation columns, RIPE loss columns only), threshold quantile
(0.995, 0.999), persistence (1, 2, 3 rows) and training data (campaign+live, live only).
Candidates are fit on live data to 15 Nov 08:00 SGT, scored on a validation block from
15 Nov 08:15 to 16 Nov 03:00 SGT and on the 20 campaign trials. Pick: the most trials
detected among candidates with validation FA <= 2/day (ties: lower FA, then simpler).
The pick is frozen in a commit by 16 Nov 20:45 SGT, before the held-out window. On
Wednesday every candidate is scored once on held-out and all are reported, not only the pick.

**Amendment 4 (2026-11-14 10:05 UTC, approved by MY in the thread): Tuesday trials.**
10 packet-loss trials on Tue 17 Nov from 19:00 SGT (seed 1711, 5 cycles), with the same guarded runner,
automatic rollback and RIPE measurements as the campaign. The plan is frozen in
`docs/ml/live/tuesday-trials-plan.json`. The trials fall inside the held-out window, and their SSM commands
exclude them from the false-alarm count automatically. Detection is scored separately for every model
(current, planned retrain, every candidate including the frozen pick, frozen rules). Each model is trained
only on data before the held-out start, so these trials are fully out-of-sample. Labels follow campaign_ml.

**Amendment 5 (2026-11-14 10:10 UTC, before any scoring): earlier stop.** MY leaves with the Mac
on Wed 18 Nov between 07:00 and 08:00 SGT, and the team review is at 15:00 SGT. The run therefore ends at
Wed 07:00 SGT (17 Nov 23:00 UTC), and scoring starts automatically at 07:02 SGT. Each stage is committed
and pushed as soon as it finishes. The held-out start is unchanged, so the held-out window is 34 h. The
candidate pick is frozen automatically at Mon 16 Nov 19:30 SGT (retries 20:00 and 20:30), before the
held-out window.

**Amendment 6 (2026-11-16 03:00 UTC, before the candidate search and before any held-out data):
witness evidence.** At boot on Monday, a restart truncated the witness stdout log, and the journal had
already rotated. That lost the witness log from Sat 14 Nov 08:44 to Mon 16 Nov 01:11 SGT. The witness
itself ran throughout: a check on Sunday 17:00 SGT found no gap over 60 s, and the 30-minute health check
never saw it stale. The AMP origin request rate is an independent record of it. While the witness was on, `http_rps`
was always >= 0.126 in region-a (CloudFront plus direct requests) and >= 0.063 in region-b (direct only).
While the Mac was off, both were exactly 0. A row now counts as witnessed if the log has a record in the
previous 60 s, or if that region's `http_rps` is >= 0.03 (about half the lower witness-only rate). The threshold was chosen from on/off periods only. The witness now runs as its own
launchd agent with KeepAlive, and its log is appended, never truncated.
