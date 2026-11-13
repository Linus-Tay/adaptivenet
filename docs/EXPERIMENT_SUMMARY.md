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
| 7 | ML same-day retrain (shadow) | 12–13 Nov | Best model **16–17/20** at **~14 false alarms per region per day** (95% CI 4.6–33); AWS-only ML without host features 0/20 (anomaly) or 2/20 (supervised) | Done, shadow-only |
| 8 | Confidence rules, lab runs | 13 Nov | Latency fault: detected in ~11–15 s, 100→75 at ~65–93 s (two runs), failback to SG. Application-error fault: first blocked by the safety reviewer (no RIPE RTT); after the reviewer change (option B), 100→75 at ~45 s, 75→50 at ~85 s | Done |
| 9 | Local Docker demo | 12 Nov | 700 ms SG latency: AWS-only healthy, AWS+RIPE flags in ~20 s, 4 steps to Virginia | Demonstration, not evidence |
| 10 | Test suite | 13 Nov | **486/486** pass on `main` | Done |
| 11 | Live EKS scale test (SG) | 13 Nov | CPU 2% → 97% peak; HPA scaled pods **2→3 about 40 s after load start, →4 about 30 s later**; controller held; 0 of 10,762 requests failed | Done, single run |
| 12 | Offline action replay of the 20 campaign trials | 13 Nov | At the controller's 500 ms HTTP deadline: **1/20** trials would fail over (slot 0, Virginia 35%, 210 s); **0 false actions** in 20 controls and 1.6 h healthy gaps; held by the confidence rule (10) and 3-fresh-checks (2) | Done, offline |

---

## 1. RIPE pilot (10 Nov)

- Six 25% packet-loss trials in Virginia. AWS-only detected 0/6, AWS+RIPE 6/6.
- No false alarms in 4 healthy controls. Exact McNemar p = 0.031 (6 discordant pairs).
- RIPE alerted 1 to 9 min after each fault trigger.
- Details: `docs/ml/RIPE_VALUE_STUDY.md` §1.

**Limitations.** Only 6 trials, in one region, on one day, with one fault type. The controls were recorded on a different day. The RIPE thresholds were written with knowledge of the trial setup, and alarm onsets were partly defined by RIPE data. The pre-registered campaign (#2) was run to remove these weaknesses, so the pilot is motivation only.

## 2. 20-trial pre-registered RIPE campaign (12 Nov, 15:22–23:57 UTC)

**Design.** The protocol and both detection rules were committed before the first trial. There were 20 slots (10 per region), drawn from a seeded plan that randomised region order, start time and loss level from {10, 15, 20, 25, 30, 35}%. Each trial used SSM to install iptables rules that dropped ICMP echo replies and HTTP responses at the origin for 960 s, with an automatic rollback armed first. Ground truth came only from the SSM confirmation, not from RIPE. The untouched region served as a same-time healthy control. The same four RIPE probes measured both regions every 240 s; one of them (probe 1010367) received no replies from Singapore during the campaign, so only three reached Singapore. Slot 1 (Singapore 25%) was refused on the instance and never ran; a make-up slot 20 with the same settings ran at the end, so 20 trials were scored.

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

**User impact (exploratory, not pre-registered).** The HTTP witness also requested the CloudFront URL every 15 s from MY's Mac in Singapore. All 2,181 requests returned HTTP 200 and all were served by Singapore, so CloudFront never failed over. During the 10 Singapore trials, 179 of 639 requests (28.0%) took longer than 500 ms (p95 1,313 ms). During the 10 Virginia trials it was 3 of 640 (0.5%, p95 124 ms), because Virginia is the standby origin, and in healthy time 1 of 783 (0.1%, p95 126 ms; healthy time excludes 30 s before and 60 s after each trial).

**What the AWS-only baseline covers.** It uses three application signals: app up, 5xx ratio and handler p95. Host network counters (for example TCP retransmissions) and CloudFront origin metrics were not collected, so 0/20 is a statement about these signals, not about all internal telemetry.

Details: `docs/ml/RIPE_CAMPAIGN_RESULTS.md`.

**Limitations.**
- 20 trials in nine hours, on one day, with one fault type (packet loss) and four probes, three of which reached Singapore. The 65% confidence interval is wide (41–85%).
- Light loss (10–25%) is often missed, because each probe loses few packets in one 240 s round.
- Detection is slow (median 459 s, almost 8 minutes), so it is not suited to fast failover on its own.
- On the 10 November healthy window, the RTT branch of the primary rule produced about 14 alarm onsets per region per day, mostly from single-path RTT inflation, although none occurred in the campaign controls. A loss-only variant gave 1 onset. RTT alerts need a multi-path quorum before operational use.
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
- The best model (an anomaly detector on AWS+RIPE features) detected 16–17 of the 20 trials at about 14 false alarms per region per day (95% CI 4.6–33, over 8.4 region-hours of unseen healthy time, about 4.2 h of clock time; about 28 a day for both regions together), down from 58–150.
- With the host memory and CPU features removed, the AWS-only models detected 0 of 20 (anomaly) and 2 of 20 (supervised), which confirms that their earlier detections came mostly from host drift.
- **Decision:** the frozen rule (13/20, with no false alarms in the same hours) remains the alerting mechanism, and the ML models remain in shadow mode. Further gains would need fresh, pre-registered data.
- Details: `docs/ml/CAMPAIGN_SAME_DAY_ML.md`.

**Limitations.** Training and testing used data from the same day, so the result is optimistic for other days. About 14 alarms per day is still far too many for operations. The 16–17/20 was measured on the same 20 trials as the rule, not on new trials.

## 8. Confidence rules and lab runs

**Rules.** Failover needs 2 of 3 views (AWS, RIPE, HTTP) for 3 consecutive checks, with the other region clean and no reversal within 2 min. Failback happens after 2 min clean and 2 min since the last step. Scale-out needs CPU ≥80% for 3 consecutive checks with the application running. Scale-in is a recommendation only, at ≤30% for 5 min, and never goes below the initial size. Traffic moves in 25-point steps. ML is not used.

**Lab results.**
- **Latency fault on Singapore:** the AWS-only view stayed healthy, AWS+RIPE+HTTP detected the fault in about 15 s, traffic moved from 100% to 75% at about 65–93 s across two runs, and it returned to Singapore after sustained recovery.
- **Application-error fault (100% gateway errors):** the rules reached a confident decision in about 30 s, but the safety reviewer first blocked the move because no RIPE RTT was available (every probe lost every packet, so RIPE quality was `loss_only`). The team then chose option B: the reviewer accepts `loss_only` RIPE data for the region traffic moves away from, and only there (`reviewer_source_loss_only_v1`). In a lab run with that rule, traffic moved 100→75 at about 45 s and 75→50 at about 85 s, and returned to 100% at about 250 s after the fault ended. The offline campaign replay is unchanged (1/20, 0 false actions).

Details: `docs/END_TO_END.md`.

**Limitations.** Lab only, with simulated faults. The latency run shows one 25-point step, not a full move. The reviewer's block on the error fault is conservative, and whether it should allow such moves without RIPE RTT is still an open design question.

## 9. Local Docker demo

- 13 containers on a laptop (the stack `demo/run.sh lab-up` starts), reusing the production ingestion, review and controller code, with a directory queue in place of SQS.
- With no fault, all views were healthy and the controller held. With 700 ms on the Singapore external path, AWS-only stayed healthy, AWS+RIPE flagged Singapore in about 20 s, and all traffic moved to Virginia in 4 verified steps.

**Limitations.** This is a demonstration of function with simulated faults, not evaluation evidence.

## 10. Test suite

- All 486 automated tests pass on `main`, including replays of the recorded SSM outages.

**Limitations.** The tests cover the scaling rules as unit tests. Live EKS behaviour is covered separately in #11.

## 11. Live EKS scale test (Singapore, 13 Nov 04:20Z)

- A load test sent 10,762 requests over 90 s, and none failed. CPU went from 2% to 81–97% (peak 97%).
- The Kubernetes HPA scaled the pods 2 → 3 at about 04:20:57Z (about 40 s after the load started at 04:20:16Z) and 3 → 4 at about 04:21:25Z (about 30 s later).
- The AdaptiveNet controller held. Its SafetyController needs 3 consecutive fresh readings with CPU at or above the 60% HPA target; CPU was at or above 60% in five readings in a row, but the new pods had no metrics timestamp yet in four of them, so only one reading had complete, fresh pod metrics before the HPA brought CPU back under 60%. (The 80% confidence rule from #8 runs in the lab controller only and was not part of this test.)
- Cleanup passed: by 04:27:23Z the HPA was back at 2/6, with 2 pods at 2% CPU.
- Details: `docs/LIVE_TESTS_20261113.md`.

**Limitations.** Single run. It shows that the HPA scales on EKS and that the controller correctly does not add a second action on top. It does not show the controller's own scale-out acting live.

## 12. Offline action replay of the 20 campaign trials (13 Nov)

**Question.** The campaign shows detection. Would the controller have moved traffic, using the committed rules on the recorded data?

**Method.** The recorded AMP, RIPE and HTTP witness data for each slot was replayed on a 15 s clock through the unchanged code: snapshot builder, confidence rules, the frozen campaign detector (and, separately, the lab rule detector), the safety reviewer and the SafetyController. The HTTP vote uses the controller's 500 ms deadline (`recovery.DEADLINE_SEC`). Nothing was tuned and no AWS call was made.

**Result (frozen detector, 500 ms).**
- 1 of 20 trials would fail over: slot 0 (Virginia, 35% loss), 210 s after the fault request, with RIPE and HTTP both bad.
- 0 false actions in the 20 same-time control windows and in 1.6 h of healthy gaps between slots.
- The detector proposed a shift in 13 trials and the safety reviewer passed all 13. The 2-of-3 confidence rule held 10 and the SafetyController's 3-fresh-checks streak held 2. 7 trials never produced a proposal.
- With a 2 s HTTP deadline, the result is 0 of 20.

**Why so few actions.** AWS never voted bad. RIPE did in 10 of 20 trials. Under partial loss most HTTP requests still finished with HTTP 200, so a second bad view rarely lasted three windows. From the witness Mac, healthy Virginia requests have a median of 500 ms, so Virginia's HTTP vote is close to a coin flip and the slot 0 failover is partly noise. Singapore's HTTP vote is a real signal (healthy median 45 ms).

**Reading.** The action path is safe on this data (no false actions) but rarely acts on 10–35% partial loss. The limit is the confidence rule, not the safety reviewer. Rule changes that would let it act are listed in the replay document as options; none was applied.

Details: `docs/ACTION_REPLAY_CAMPAIGN.md` and `docs/ml/action-replay-campaign.json` on branch `test/action-replay-campaign` (aaee8bf).

**Limitations.** One campaign, one witness location, 15 s replay steps instead of the controller's own probes. Traffic never moved, so recovery after a move is not replayed.
