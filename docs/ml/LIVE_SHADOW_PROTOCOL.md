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

**Amendment 7 (2026-11-16, chosen by MY at 13:33 UTC; committed before the Tuesday trials and before any
held-out scoring): defective Monday pick, second pick.** An independent review found that the Monday pick
(`robust_z_max|aws_ripe_no_host_no_rtt|q0.999|p1|live_only`, commit 57cc91d) is an artefact. Trained on
live data only, 10 of its 11 columns have zero MAD in both regions and are muted, every loss column
included. Its only active column is `ripe_result_age_s`, which runs higher on the campaign day. So it
alarms in 20 of 20 campaign controls, at about 370 alarms a day on campaign healthy rows, and its 20/20
trials is not detection. The p2 runner-up has the same flaw. The cause is the selection rule: it never
looked at controls. The Monday pick stays frozen and is reported as it is, marked failed. A second pick
uses the same rule with one added gate: campaign controls alarmed in at most 1 of 20. It is chosen
mechanically from the same candidates and committed in `docs/ml/live/candidate-pick-2.json`. Campaign
controls and campaign healthy false alarms are now reported for every candidate, in SEARCH.md and on
Wednesday. The frozen rule's declared secondary variants (loss_only, loss_or_rtt2) are scored as context
on held-out and on the Tuesday trials. The frozen rule itself is unchanged.

**Amendment 8 (2026-11-16, chosen by MY at 16:23 UTC; committed before the Tuesday trials, no held-out data
read): early-warning test.** Origin: the reviewer took an exploratory look at the 12 Nov campaign day on
16 Nov around 16:20 UTC. It suggested an ML early warning could catch trials the frozen loss_only rule
misses (13 + 2 more) and alert earlier (on 8 of 13 shared trials, median 210 s). Because this idea came
from looking at campaign data, the Tuesday trials are its unseen test.
* *Pick (fixed rule over the existing 108 candidates):* campaign controls alarmed <= 1/20 and validation
  FA <= 5/day, then the most campaign trials; ties go to lower validation FA, then the simpler model.
  Result: `isolation_forest|aws_ripe_no_host_no_rtt|q0.995|p2|live_only` (15/20 trials, 1/20 controls,
  3.84 FA/day, CI 1.41-8.36). This matches the reviewer's scratch result.
* *Comparator:* the frozen rule's declared loss_only variant (AWS view OR RIPE loss quorum), unchanged.
* *Reported on the 10 Tuesday trials,* using the same labels and score: trials detected by each; trials
  only the pick catches and only the rule catches; on shared trials, seconds from the SSM request to the
  first alert (pick minus rule); held-out healthy FA/day with CI for both.
* *Pass mark:* the pick detects at least as many trials as the rule, AND it catches at least 1 trial the
  rule misses or is earlier on more than half of the shared trials, AND its held-out FA is <= 5/day.
  Otherwise the result is "did not pass".
* *Role:* early warning only. An alert asks for a confirming check and never moves traffic. Shadow only.
* The Monday pick, the second pick and the all-108 report are unchanged.
* *Addendum (2026-11-16 ~16:30 UTC, before the trials, no held-out data read):* each Tuesday slot has a
  same-time control window in the other region. Controls alarmed (out of 10) are reported for both the
  pick and the loss_only rule. The pass mark also requires the pick to alarm in at most 1 of the 10
  Tuesday controls.

**Amendment 9 (2026-11-17, chosen by the reviewer at MY's request; committed before the Tuesday trials, no
held-out data read): two supervised early-warning classifiers.** The full spec is in the reviewer's
`amendment9_supervised_spec.md`, implemented independently in `ml/team_starter/supervised_live.py`.
* *Data:* the 20 labelled 12 Nov campaign faults, plus live healthy rows and witness records from before
  2026-11-16T11:29Z (`LIVE_END`) only. Campaign scoring uses campaign_ml blocked folds.
* *S1, supervised RIPE:* 48 settings (logreg or gboost; RIPE loss columns with or without RTT columns;
  campaign or campaign+live training; p >= 0.5/0.8/0.9; persistence 1/2). Rule: campaign controls <= 1/20
  and validation FA <= 5/day, then the most trials; ties go to fewer validation onsets, lower campaign-day
  FA, fewer columns, logreg, fewer persistence rows, then higher threshold. **Pick:
  `gboost | ripe_loss+rtt | campaign+live | p>=0.8 | k1`**: 16/20 trials, 0/20 controls, campaign-day FA
  2.85/day, median delay 299 s, validation 4 onsets in 37.5 h = 2.56/day (0.70-6.55). 11 of 48 settings passed.
* *S2, supervised HTTP + RIPE:* window features from the 15 s witness (120 s window, 1800 s baseline,
  excess over 150/800 ms, own and other region). 54 settings. Rule: controls <= 1/20 and at most 1
  validation onset (6.9 region-hours on 16 Nov 08:02-11:29Z), then the same order. **Pick:
  `logreg | http800+ripe_loss | p>=0.9 | k3`**: 20/20 trials, 0/20 controls, campaign-day FA 0, median
  delay 151 s, validation 1 onset in 6.9 h = 3.49/day (0.09-19.4). 2 of 54 settings passed. Its frozen
  refit uses every live HTTP row before LIVE_END (2,060) and keeps 20/20, 0/20, 151 s.
* My independent implementation gives the same picks and numbers as the reviewer's run. The definitions
  are frozen in `docs/ml/live/supervised-picks.json`.
* *Wednesday report:* S1, S2, the frozen loss_only rule (comparator) and the unchanged lab HTTP rule
  (context; 60 s window, failures >= 20% or p95 >= 800 ms). Each reports Tuesday trials of 10, controls of
  10, trials only one side catches, model-minus-rule delays on shared trials, and held-out FA/day with CI
  and hours. For S2 and the lab rule, FA uses held-out rows with HTTP features, and the count without is given.
* *Pass marks:* S1 must detect >= the rule's trials, AND catch >= 1 trial the rule misses or be earlier
  on > half of the shared trials, AND have held-out FA <= 5/day, AND alarm in <= 1 of 10 Tuesday controls.
  S2 must meet the same, AND have fewer held-out onsets than the lab HTTP rule on the same rows. Otherwise
  the result is "did not pass". Role if passed: early warning only; an alert asks for a confirming check.
* *Honesty:* the reviewer explored these settings on the campaign day and pre-held-out live data on
  16 Nov, about 16:25-16:45 UTC, looking at roughly 150 settings, so the campaign numbers are optimistic.
  With amendment 8 there are three early-warning tests on 10 trials, and all are reported pass or fail.
  S2 uses one HTTP vantage point (MY's Mac); the Mac-to-Virginia path is noisy, which is the main FA risk.
  Models trained on the campaign day alone gave 50-115 FA/day on live data, which is why live negatives
  are included.
