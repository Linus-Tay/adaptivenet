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
  (Monday pick failed review before any held-out scoring: trained on live data only it watches one column, RIPE result age, and alarmed in 20/20 campaign controls. Reported, not claimed.)
* The ML stays shadow-only; it never triggers an action.
