# AdaptiveNet: summary of experiment results

CSC3104 Group 03, 13 November 2026.

This page brings together every experiment run for AdaptiveNet, with the main numbers and the limitations of each. Detailed write-ups are in the documents named under each result.

## At a glance

| # | Experiment | Date | Main result | Status |
|---|---|---|---|---|
| 1 | RIPE pilot (Virginia, 25% loss) | 10 Nov | AWS-only **0/6**, AWS+RIPE **6/6**, McNemar p = 0.031 | Superseded by #2 |
| 2 | 20-trial pre-registered RIPE campaign | 12 Nov | AWS-only **0/20**, AWS+RIPE **13/20** (VA 7/10, SG 6/10), p = 0.00024, 0 false alarms in 20 controls, median delay 459 s | Headline result |
| 3 | Routing: static route vs safety controller (900 ms delay) | local | Requests ≤500 ms: static **0%**, controller **65.1%** (125/192); final window 48/48 | Done |
| 4 | Live SSM service stops | 23 Oct, 9 Nov | 3 stops, detected and auto-recovered; 36/36 CloudFront HTTP 200 | Done, serving origin not identifiable |
| 5 | Live CloudFront failover check | 13 Nov | **108/108 HTTP 200**; all 27 requests during the stop served by Virginia | Done, single run |
| 6 | Earlier ML studies | Oct–12 Nov | 85.7% accuracy on 118 rows; 50% recall tie from a memory confound; **58–150 false alarms/day** | Superseded by #7 |
| 7 | ML same-day retrain (shadow) | 12–13 Nov | Best model **16–17/20** at **~14 false alarms/day** (5–33); AWS-only ML 0/20 | Done, shadow-only |
| 8 | Confidence rules, lab runs | 13 Nov | Latency fault: detected in ~15 s, 100→75 at ~93 s, failback to SG. Application-error fault: confident in ~30 s, **blocked by the safety reviewer** | Done, design question open |
| 9 | Local Docker demo | 12 Nov | 700 ms SG latency: AWS-only healthy, AWS+RIPE flags in ~20 s, 4 steps to Virginia | Demonstration, not evidence |
| 10 | Test suite | 13 Nov | **472/472** pass on the submission branch | Done |
| 11 | Live EKS scale test (SG) | 13 Nov | CPU 2% → 97% peak; HPA scaled pods **2→3→4 in ~1 min**; controller held; 0 of 10,762 requests failed | Done, single run |

---

## 1. RIPE pilot (10 Nov)

- Six 25% packet-loss trials in Virginia. AWS-only detected 0/6, AWS+RIPE 6/6.
- No false alarms in 4 healthy controls. Exact McNemar p = 0.031 (6 discordant pairs).
- RIPE alerted 1 to 9 min after each fault trigger.
- Details: `docs/ml/RIPE_VALUE_STUDY.md` §1.

**Limitations.** Only 6 trials, in one region, on one day, with one fault type. The controls were recorded on a different day. The RIPE thresholds were written with knowledge of the trial setup, and alarm onsets were partly defined by RIPE data. The pre-registered campaign (#2) was run to remove these weaknesses, so the pilot is motivation only.

## 2. 20-trial pre-registered RIPE campaign (12 Nov, 15:22–23:57 UTC)

**Design.** The protocol and both detection rules were committed before the first trial. There were 20 slots (10 per region), drawn from a seeded plan that randomised region order, start time and loss level from {10, 15, 20, 25, 30, 35}%. Each trial used SSM to install iptables rules that dropped ICMP echo replies and HTTP responses at the origin for 960 s, with an automatic rollback armed first. Ground truth came only from the SSM confirmation, not from RIPE. The untouched region served as a same-time healthy control. There were four RIPE probes per region, measuring every 240 s.

**Results.**

| Measure | Value |
|---|---|
| AWS-only detected | 0/20 |
| AWS+RIPE detected | 13/20 = 65% (exact 95% CI 41–85%) |
| By region | Virginia 7/10, Singapore 6/10 |
| False alarms in 20 same-time controls | 0 (exact 95% upper bound 17%) |
| Exact McNemar, 13 discordant pairs, all favouring AWS+RIPE | p = 0.00024 |
| Bootstrap 95% CI for the gain in recall | 45–85 points |
| Detection delay after the SSM request | median 459 s, range 22–892 s (1 to 4 RIPE rounds) |

**By loss level.**

| Loss | 10% | 15% | 20% | 25% | 30% | 35% |
|---|---|---|---|---|---|---|
| AWS+RIPE detected | 1/3 | 1/3 | 1/2 | 2/4 | 2/2 | 6/6 |

AWS+RIPE detected all 8 trials at 30–35% loss, but only 5 of 12 at 10–25%.

**Why AWS-only missed every trial.** AWS data was complete (1,053 AMP samples per region). The application stayed up, there were no 5xx responses, and server-side p95 latency stayed between 4.75 and 5.5 ms. That p95 comes from the server's own request handler, so it cannot see loss on the path after the handler finishes. This is a direct measurement of differential observability.

Details: `docs/ml/RIPE_CAMPAIGN_RESULTS.md`.

**Limitations.**
- 20 trials in nine hours, on one day, with one fault type (packet loss) and four probes per region. The 65% confidence interval is wide (41–85%).
- Light loss (10–25%) is often missed, because each probe loses few packets in one 240 s round.
- Detection is slow (median 459 s, almost 8 minutes), so it is not suited to fast failover on its own.
- On the 10 November healthy window, the RTT branch of the primary rule produced about 14 alarm onsets per day from single-path RTT inflation, although none occurred in the campaign controls. A loss-only variant gave 1 onset. RTT alerts need a multi-path quorum before operational use.
- An upper bound of 17% means that 0 of 20 controls does not by itself prove a low false-alarm rate.
- The rules were frozen before the campaign and were not tuned afterwards.

## 3. Routing: static route vs safety controller (local)

- A 900 ms delay was injected on the path to region A for 120 s, with a client deadline of 500 ms. Each mode served 192 requests in 4 windows of 48. The static run replayed the request timing of the adaptive run.

| Mode | Final A/B | Shifts | HTTP 200 | ≤500 ms | Final window ≤500 ms |
|---|---|---|---|---|---|
| Static route | 100/0 | 0 | 192/192 | 0/192 (0%) | 0/48 |
| Safety controller | 0/100 | 4 | 192/192 | 125/192 (65.1%) | 48/48 |

- The first shift came 1.93 s after the first slowdown, and recovery was confirmed after 60.18 s. Region B served every final-window request in 40.3–74.6 ms.
- Both modes returned HTTP 200 for every request, so an availability-only check would have judged the static route acceptable.
- Details: `docs/SAFETY_CONTROLLER_TASK4.md`.

**Limitations.** One local run per mode, with a simulated delay, not on AWS. The 65.1% figure includes the time before the controller reacted, so it depends on how long the fault lasts.

## 4. Live SSM service stops (23 Oct, 9 Nov)

- The application service was stopped through SSM 3 times. Prometheus recorded failed scrapes within seconds, while the node exporter and the other region stayed healthy, and the service recovered automatically.
- During one Singapore stop, all 36 monitored CloudFront requests returned HTTP 200.
- When replayed through the decision path, both outage profiles are mapped to failover, not scaling.
- Details: `docs/END_TO_END.md` (section "Fit with the recorded SSM outage experiments") and `tests/test_end_to_end.py::test_ssm_service_stop_shape_maps_to_failover_not_scale`.

**Limitations.** Cached responses meant the serving origin could not be identified, so these runs do not show which region answered. Three runs only. Experiment #5 was run to close this gap.

## 5. Live CloudFront failover check (13 Nov)

- An SSM command stopped the Singapore service at 04:02:01Z for about 60 s.
- All 108 monitored requests returned HTTP 200.

| Phase | Requests | Served by | Median latency |
|---|---|---|---|
| Before the stop | 12 | Singapore | 56.6 ms |
| During the stop (04:02:06–04:03:27Z) | 27 | Virginia (all 27) | 531.8 ms |
| After recovery | 69 | Singapore | 59.7 ms |

- The first request served by Virginia (04:02:06Z) came 5 s after the stop. Traffic was back on Singapore about 90 s after the stop.
- The switch was made by CloudFront's own 5xx origin failover, **not by the AdaptiveNet controller**.
- Details: `docs/LIVE_TESTS_20261113.md`.

**Limitations.** Single run. It shows that the CloudFront failover path works. It does not show the AdaptiveNet controller driving a live failover; by design, the controller refuses live failover.

## 6. Earlier ML studies

- **Application failures:** on 118 rows across 9 episodes, the AWS-only and AWS+RIPE models caught the single held-out failure after 8.08 s. The AWS+RIPE+HTTP model reached 85.7% accuracy but missed it. External signals add nothing when the service is simply stopped.
- **Confound:** in a network-degradation study, all feature groups tied at 50% recall. The held-out set had no healthy rows, and the AWS-only model relied on the free memory of the Virginia host, which dropped when the trial tooling was installed and never recovered. Without memory, CPU and load features, the AWS-only model raised no alerts. Details: `docs/ml/RIPE_VALUE_STUDY.md` §3.
- **False alarms:** on a 4 h healthy window (10 Nov 13:45–17:55 UTC), the models raised false alarms at 58–150 per day (AWS-only 92, AWS+RIPE 150, AWS+RIPE+HTTP 58). The causes were host-memory drift between days and one probe that is unreachable from Singapore only. Details: `docs/ml/SIX_CLASS_AND_FALSE_ALARM_STUDY.md` §5.

**Limitations.** The datasets are very small (one held-out failure). The 4 h window is shorter than the 6 h minimum, so the rates are indicative only. The earlier perfect three-class scores do not survive these checks.

## 7. ML same-day retrain (shadow mode)

- The models were retrained on campaign-day data. The train/test split was committed before scoring, and the healthy hours used to count false alarms were held out of training.
- The best model (an anomaly detector on AWS+RIPE features) detected 16–17 of the 20 trials at about 14 false alarms per day (range 5–33 per day over 8.4 h of unseen healthy time), down from 58–150.
- With the remaining memory and CPU drift features removed, the AWS-only model detected 0 of 20, which confirms that its earlier detections were artefacts.
- **Decision:** the frozen rule (13/20, with no false alarms in the same hours) remains the alerting mechanism, and the ML models remain in shadow mode. Further gains would need fresh, pre-registered data.
- Details: `docs/ml/CAMPAIGN_SAME_DAY_ML.md`.

**Limitations.** Training and testing used data from the same day, so the result is optimistic for other days. About 14 alarms per day is still far too many for operations. The 16–17/20 was measured on the same 20 trials as the rule, not on new trials.

## 8. Confidence rules and lab runs

**Rules.** Failover needs 2 of 3 views (AWS, RIPE, HTTP) for 3 consecutive checks, with the other region clean and no reversal within 2 min. Failback happens after 2 min clean and 2 min since the last step. Scale-out needs CPU ≥80% for 3 consecutive checks with the application running. Scale-in is a recommendation only, at ≤30% for 5 min, and never goes below the initial size. Traffic moves in 25-point steps. ML is not used.

**Lab results.**
- **Latency fault on Singapore:** the AWS-only view stayed healthy, AWS+RIPE+HTTP detected the fault in about 15 s, traffic moved from 100% to 75% at about 93 s, and it returned to Singapore after sustained recovery.
- **Application-error fault (100% gateway errors):** the rules reached a confident decision in about 30 s, but the safety reviewer blocked the move because no RIPE RTT was available.

Details: `docs/END_TO_END.md`.

**Limitations.** Lab only, with simulated faults. The latency run shows one 25-point step, not a full move. The reviewer's block on the error fault is conservative, and whether it should allow such moves without RIPE RTT is still an open design question.

## 9. Local Docker demo

- 10 containers on a laptop, reusing the production ingestion, review and controller code, with a directory queue in place of SQS.
- With no fault, all views were healthy and the controller held. With 700 ms on the Singapore external path, AWS-only stayed healthy, AWS+RIPE flagged Singapore in about 20 s, and all traffic moved to Virginia in 4 verified steps.

**Limitations.** This is a demonstration of function with simulated faults, not evaluation evidence.

## 10. Test suite

- All 472 automated tests pass on the submission branch, including replays of the recorded SSM outages.

**Limitations.** The tests cover the scaling rules as unit tests. Live EKS behaviour is covered separately in #11.

## 11. Live EKS scale test (Singapore, 13 Nov 04:20Z)

- A load test sent 10,762 requests over 90 s, and none failed. CPU went from 2% to 81–97% (peak 97%).
- The Kubernetes HPA scaled the pods 2 → 3 at about 04:20:57Z and 3 → 4 at about 04:21:25Z.
- The AdaptiveNet controller held. It saw high CPU in only one check, not the 3 consecutive checks its scale-out rule needs, because the HPA had already brought the load down.
- Cleanup passed: by 04:27:23Z the HPA was back at 2/6, with 2 pods at 2% CPU.
- Details: `docs/LIVE_TESTS_20261113.md`.

**Limitations.** Single run. It shows that the HPA scales on EKS and that the controller correctly does not add a second action on top. It does not show the controller's own scale-out acting live.
