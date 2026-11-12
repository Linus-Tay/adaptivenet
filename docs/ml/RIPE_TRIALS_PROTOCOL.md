# Pre-registered protocol: 20 packet-loss trials (AWS-only vs AWS+RIPE)

This protocol is frozen in Git **before the first trial runs**. Any later change to
the analysis has to be reported as a deviation.

## Question
Can AWS+RIPE detect origin-path packet loss that AWS-only monitoring cannot, on
trials that weren't used to design the detectors?

## Design
* **20 impairment slots**, 10 on Singapore (`region-a`, i-0039feaedbd2b3266) and 10
  on Virginia (`region-b`, i-0ea5125bcb2d201ec). They come from a seeded plan
  (`scripts/ripe_trial_campaign.py plan`). The plan randomizes region order within
  each 50-minute cycle, start jitter (0–180 s) and loss level, drawn from
  {10, 15, 20, 25, 30, 35}%.
* **Impairment:** the existing guarded `scripts/native_network_fault_trial.py`. It
  adds iptables random DROP of ICMP echo-replies and TCP source-port-80 responses for
  960 s, and arms a systemd rollback before any rule is added.
* **Same-time healthy control:** during each slot, the other region is untouched.
  Its window [request + 240 s, request + 960 s] is the negative. That gives 20
  controls recorded on the same day as the trials, which removes the day confound
  found in the Nov 9/10 study.
* **Ground truth, independent of RIPE:** a slot counts as impaired only if the SSM
  output confirms the rollback was armed and the rules were added
  (`remote_rollback_armed_and_rules_added`). Slots that are unconfirmed, skipped or
  refused are listed and excluded, not counted as hits or misses. The window runs
  from the SSM request to request + duration.
* **RIPE:** fresh 240-second ping measurements to both origin IPs from the same
  probe set, with public results.
* **AWS-only data:** AMP history exported read-only, using exactly the collector's
  PromQL (`ml/team_starter/amp_export.py`, 30 s step).

## Frozen detectors (`ml/team_starter/ripe_value.py`, this commit)
* **AWS-only (fair):** app down, OR 5xx ratio ≥ 5%, OR p95 ≥ 1.5× the region's own
  median over the preceding 6 h (impaired windows excluded). Host memory, CPU and
  load are **not** used. They were shown to be a confound.
* **AWS+RIPE:** AWS-only, OR a RIPE loss quorum in the trailing 480 s (≥ 2 distinct
  reachable probes losing packets and pooled loss ≥ 10%), OR ≥ 1 path whose RTT is
  ≥ 50 ms and ≥ 1.3× its own trailing 6 h median. Persistently unreachable probes
  (12 consecutive rounds with no replies) are excluded.

### Secondary RIPE variants (added 2026-11-12 ~08:25 UTC, before trial 1 at 08:42)
The primary rule was scored on the user-approved healthy window
`passive-healthy-20261110T134500Z` (Nov 10 13:45–17:55 UTC, both regions). That
window was **not** used to change the primary rule, which stays frozen. Results:

| Detector | Singapore | Virginia |
|---|---|---|
| AWS-only (fair) | 0 alarm onsets | 0 alarm onsets |
| Primary | 3 onsets (all single-path RTT, probe 13106; alerting 130/247 rounds) | 2 onsets (1 loss quorum, 1 RTT) |

That's about 14 alarm onsets a day, so the primary rule has poor specificity on
paths with recurring congestion. The two variants below were declared **before
any campaign data existed** and are reported alongside the primary rule:
* `loss_only`: the loss quorum alone. On the same window it gives 0 onsets in
  Singapore and 1 in Virginia.
* `loss_or_rtt2`: the loss quorum, OR RTT inflation on ≥ 2 paths at once.

## Endpoints
1. **Primary:** episode-level detection in impaired windows, AWS-only vs AWS+RIPE,
   tested with an exact two-sided McNemar test on discordant slots.
2. **Specificity:** alarms in the 20 same-time control windows for each arm, with an
   exact binomial 95% CI.
3. **Secondary:** detection by loss level, and delay from the SSM request.
4. A trained model is **not** a primary endpoint. Any trained result will be
   reported as exploratory.

## Stopping and safety
* Creating the `STOP` file halts the campaign. Rollback timers already armed on the
  instances fire regardless.
* A slot is skipped if it can't start within 300 s of its planned time, so slots
  never overlap.
* No Terraform, SQS, Docker or security-group changes. No ML-triggered actions.
