# Wednesday retrain: current vs retrained (shadow only)

Held-out live: 2026-11-16T13:00:00Z to 2026-11-17T22:59:30Z (7880 rows). Live training rows: 13050.

| model | held-out FA/day (95% CI) | FA onsets | held-out h | campaign trials detected | missed slots |
|---|---:|---:|---:|---:|---|
| current/anomaly_aws_ripe_no_host | 28.14 (22.21-35.17) | 77 | 65.667 | 17/20 | [12, 13, 16] |
| retrained/anomaly_aws_ripe_no_host | 16.08 (11.68-21.59) | 44 | 65.667 | 12/20 | [5, 6, 8, 12, 13, 14, 16, 19] |
| current/anomaly_aws_ripe | 46.05 (38.36-54.83) | 126 | 65.667 | 16/20 | [5, 12, 13, 16] |
| retrained/anomaly_aws_ripe | 18.27 (13.56-24.09) | 50 | 65.667 | 13/20 | [5, 6, 8, 12, 13, 16, 19] |
| current/anomaly_aws_ripe_no_host_persist2 | 20.47 (15.46-26.58) | 56 | 65.667 | 14/20 | [5, 10, 12, 13, 16, 19] |
| retrained/anomaly_aws_ripe_no_host_persist2 | 13.89 (9.83-19.06) | 38 | 65.667 | 10/20 | [5, 6, 8, 12, 13, 14, 16, 17, 19, 20] |
| current/anomaly_aws_ripe_persist2 | 28.87 (22.86-35.98) | 79 | 65.667 | 15/20 | [5, 12, 13, 16, 19] |
| retrained/anomaly_aws_ripe_persist2 | 12.06 (8.3-16.94) | 33 | 65.667 | 12/20 | [5, 6, 8, 12, 13, 14, 16, 19] |
| context/frozen_aws_ripe_rule | 30.7 (24.49-38.01) | 84 | 65.667 | n/a |  |
| context/frozen_ripe_rule | 28.87 (22.86-35.98) | 79 | 65.667 | n/a |  |
| context/frozen_aws_ripe_rule_loss_only | 5.12 (2.8-8.59) | 14 | 65.667 | n/a |  |
| context/frozen_ripe_rule_loss_only | 2.92 (1.26-5.76) | 8 | 65.667 | n/a |  |
| context/frozen_aws_ripe_rule_loss_or_rtt2 | 7.31 (4.46-11.29) | 20 | 65.667 | n/a |  |
| context/frozen_ripe_rule_loss_or_rtt2 | 5.12 (2.8-8.59) | 14 | 65.667 | n/a |  |

## Team summary (generated from the numbers above; shadow only)

* Held-out: 2026-11-16T13:00:00Z to 2026-11-17T22:59:30Z, 65.667 healthy hours after exclusions, never used for training or choosing.
* Current model (13 Nov anomaly, AWS+RIPE, no host): 28.14/day (95% CI 22.21-35.17) false alarms; 17/20 campaign trials.
* Planned retrain (+ live healthy data): 16.08/day (95% CI 11.68-21.59) false alarms; 12/20 campaign trials.
* Frozen RIPE rule (unchanged, context): 30.7/day.
* Candidate picked on Monday before held-out (robust_z_max|aws_ripe_no_host_no_rtt|q0.999|p1|live_only): 2.92/day (95% CI 1.26-5.76); all 108 candidates are in retrain-report.json.
  (Monday pick failed review before any held-out scoring: trained on live data only it watches one column, RIPE result age, and alarmed in 20/20 campaign controls. Reported, not claimed.)
* Pick 2 (amendment 7, frozen before the Tuesday trials and held-out, isolation_forest|ripe_loss_only|q0.995|p2|campaign+live): 6.58/day (95% CI 3.9-10.4).
* Tuesday trials (unseen faults): current 10/10 (controls alarmed 2/10), retrained 8/10 (controls alarmed 1/10), Monday pick 0/10 (controls alarmed 1/10), pick 2 9/10 (controls alarmed 0/10), frozen rule 9/10 (controls alarmed 3/10).
* Early-warning test (amendment 8, isolation_forest|aws_ripe_no_host_no_rtt|q0.995|p2|live_only vs frozen loss_only rule): pick 8/10, rule 8/10; only pick [], only rule []; pick earlier on 2/8 shared trials (median pick minus rule 30 s); held-out FA pick 8.04/day, rule 5.12/day; Tuesday controls alarmed pick 0/10, rule 0/10. Verdict: did not pass. Early warning only: an alert asks for a check and never moves traffic.
* Supervised S1 (amendment 9, S1|gboost|ripe_loss+rtt|campaign+live|p0.8|k1): Tuesday 9/10 (controls 0/10); only model [11], only rule []; earlier on 3/8 shared; held-out 6.94/day (95% CI 4.18-10.84, 65.667 h). Verdict: did not pass.
* Supervised S2 (amendment 9, S2|logreg|http800+ripe_loss|campaign+live|p0.9|k3): Tuesday 10/10 (controls 1/10); only model [7, 11], only rule []; earlier on 6/8 shared; held-out 21.93/day (95% CI 16.73-28.23, 65.667 h). Verdict: did not pass.
* Lab HTTP rule (context): Tuesday 10/10 (controls 5/10); held-out 128.28/day on 7880 rows with HTTP features (0 without).
* The ML stays shadow-only; it never triggers an action.

## Amendment 10 (POST-HOC, 2026-11-18, approved by MY 04:06 UTC): corrected false-alarm counts

The pre-registered exclusion ended each Tuesday trial's window at the SSM command timeout (request + 180 s, padded to + 780 s). The fault lasts 960 s and the RIPE loss window adds 480 s, so held-out rows inside live faults were counted as healthy. The corrected count drops the impaired region from request - 60 s to request + duration + 600 s (the campaign_ml.build_rows rule). This change was made after seeing the data. **Both counts are shown, and every pre-registered verdict stays as it was.**

| detector | held-out FA/day, as pre-registered | held-out FA/day, corrected |
|---|---|---|
| 13 Nov model (current/anomaly_aws_ripe_no_host) | 28.14 (22.21-35.17), 77 onsets, 65.667 h | 25.68 (19.94-32.56), 68 onsets, 63.542 h |
| planned retrain (retrained/anomaly_aws_ripe_no_host) | 16.08 (11.68-21.59), 44 onsets, 65.667 h | 14.73 (10.47-20.14), 39 onsets, 63.542 h |
| Monday pick (failed): robust_z_max|aws_ripe_no_host_no_rtt|q0.999|p1|live_only | 2.92 (1.26-5.76), 8 onsets, 65.667 h | 2.64 (1.06-5.45), 7 onsets, 63.542 h |
| pick 2 (amendment 7): isolation_forest|ripe_loss_only|q0.995|p2|campaign+live | 6.58 (3.9-10.4), 18 onsets, 65.667 h | 4.53 (2.34-7.92), 12 onsets, 63.542 h |
| early-warning pick (amendment 8): isolation_forest|aws_ripe_no_host_no_rtt|q0.995|p2|live_only | 8.04 (5.04-12.17), 22 onsets, 65.667 h | 5.67 (3.17-9.34), 15 onsets, 63.542 h |
| frozen main rule (primary) | 30.7 (24.49-38.01), 84 onsets, 65.667 h | 29.08 (22.95-36.35), 77 onsets, 63.542 h |
| frozen main rule, RIPE-only | 28.87 (22.86-35.98), 79 onsets, 65.667 h | 27.19 (21.28-34.25), 72 onsets, 63.542 h |
| frozen loss_only rule (comparator) | 5.12 (2.8-8.59), 14 onsets, 65.667 h | 2.64 (1.06-5.45), 7 onsets, 63.542 h |
| frozen loss_only rule, RIPE-only | 2.92 (1.26-5.76), 8 onsets, 65.667 h | 0.38 (0.01-2.1), 1 onsets, 63.542 h |
| frozen loss_or_rtt2 rule | 7.31 (4.46-11.29), 20 onsets, 65.667 h | 4.53 (2.34-7.92), 12 onsets, 63.542 h |
| frozen loss_or_rtt2 rule, RIPE-only | 5.12 (2.8-8.59), 14 onsets, 65.667 h | 2.27 (0.83-4.93), 6 onsets, 63.542 h |
| AWS-only (aws_view_alert) | 2.19 (0.8-4.77), 6 onsets, 65.667 h | 2.27 (0.83-4.93), 6 onsets, 63.542 h |
| supervised S1: S1|gboost|ripe_loss+rtt|campaign+live|p0.8|k1 | 6.94 (4.18-10.84), 19 onsets, 65.667 h | 4.91 (2.61-8.4), 13 onsets, 63.542 h |
| supervised S2: S2|logreg|http800+ripe_loss|campaign+live|p0.9|k3 | 21.93 (16.73-28.23), 60 onsets, 65.667 h | 17.75 (13.04-23.61), 47 onsets, 63.542 h |
| lab HTTP rule (context) | 128.28 (115.21-142.43), 351 onsets, 65.667 h | 120.87 (107.98-134.86), 320 onsets, 63.542 h |

Held-out rows: 7880 as pre-registered, 7625 with amendment 10.

| test | pre-registered verdict (stands) | with amendment 10 (post hoc, for information) |
|---|---|---|
| early-warning pick (amendment 8) | did not pass | did not pass (no extra trial; earlier on 2 of 8; FA above 5/day) |
| S1 (amendment 9) | did not pass | did not pass as pre-registered; meets the limit after the post hoc correction by one alarm |
| S2 (amendment 9) | did not pass | did not pass (FA above 5/day) |

**AWS-only view of the frozen rule on the Tuesday trials:** 0/10 trials detected, controls alarmed 0/10.

