# Action replay on the real 20-trial RIPE campaign

**Question.** The campaign showed that AWS+RIPE *detects* origin-path packet loss (13/20 vs 0/20
for AWS-only). Would the controller have *acted*? That means moving traffic away from the
impaired region, using the rules exactly as committed, on the real recorded data.

**Answer: no. 0 of 20 trials would have failed over, and there were 0 false actions in the 20
same-time healthy control windows.** The detector flagged 13 of 20 trials (the same 13 as the
campaign result) and the safety reviewer passed those proposals. Every one was then held by the
confidence rule: 2 of the 3 signals (AWS, RIPE, HTTP) must be bad for 3 windows in a row. During
partial packet loss only RIPE was reliably bad. The HTTP check rarely went past its 2 s deadline,
and AWS-internal metrics never changed.

Nothing was tuned for this run. No AWS call was made.

## Inputs (all real, recorded 12-13 Nov 2026)

From `/Users/my/Project/adaptivenet-ripe-campaign/` on MY's Mac:

* AMP export `amp-region-a.jsonl`, `amp-region-b.jsonl` (30 s step, the collector's PromQL)
* RIPE ping results `ripe/236862553.json` (Singapore) and `ripe/236862555.json` (Virginia): 4 probes, 240 s rounds
* HTTP witness `witness.stdout` (`kind: http_witness`): one direct GET to each origin every 15 s, from MY's Mac
* `campaign-plan.json`, `campaign-log.jsonl`, `trials/`: SSM-confirmed ground truth. Slot 1 is
  unconfirmed and excluded, as in the campaign analysis, which leaves 20 trials.

## Method

`scripts/action_replay_campaign.py` replays each slot on a 15 s clock, from 10 min before the
fault request to the end of the 960 s fault. Each tick runs the committed code, unchanged:

1. `ingestion.features.make_snapshot`. RIPE goes through the real ingestion parser `parse_atlas_latest`.
2. `controller.confidence.ConfidenceTracker`. The HTTP vote is the latest witness request to that
   origin: good means HTTP 200, served by that region, within 2 s.
3. A proposal, from two detectors run separately:
   * **frozen campaign detector**: `ml/team_starter/ripe_value.py`, the AWS-only fair rule OR the
     primary RIPE rule, mapped to a 25-point shift away from the single flagged region;
   * **lab rule detector**: `demo/lab/detectors.py` (`aws_ripe_http`) plus
     `demo/lab/controller.build_proposal`, exactly what the lab controller uses.
4. `controller.end_to_end.decide`: the safety reviewer as it is today.
5. `ConfidenceTracker.failover`, then `SafetyController(mode="local").step`. The witness sample is
   the controller-owned observation (3 fresh checks).

An **action** is the first tick where the SafetyController applies a 25-point step. The route
starts at 50/50, so a move is possible away from either region. Run it with:

```bash
python3 scripts/action_replay_campaign.py --campaign-dir /Users/my/Project/adaptivenet-ripe-campaign \
  --out docs/ml/action-replay-campaign.json
```

The full per-slot output is in `docs/ml/action-replay-campaign.json`. `tests/test_action_replay_campaign.py`
checks the harness on synthetic fixtures. When RIPE and HTTP agree, it does act: 50→25 in 75 s, as
"RIPE+HTTP bad for 5 windows". When healthy, it never acts. Unconfirmed slots are excluded.

## Results

| | Frozen campaign detector | Lab rule detector |
| --- | --- | --- |
| Trials with a shift proposal during the fault | 13/20 | 20/20 |
| Proposals that passed the safety reviewer | all of them | all of them |
| **Would fail over (25-point step applied)** | **0/20** | **0/20** |
| False actions in the 20 control windows | 0/20 | 0/20 |
| Where it stopped | confidence 13, no proposal 7 | confidence 20 |

Per trial (65 ticks of 15 s in each 960 s fault window; a "bad tick" is that signal's vote for the impaired region):

| Slot | Region | Loss | Frozen detector first flag | Lab detector first flag | RIPE bad | HTTP bad | AWS bad | Stopped at (frozen) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 0 | Virginia | 35% | 120 s | 15 s | 31 | 7 | 0 | confidence |
| 2 | Virginia | 30% | 285 s | 135 s | 0 | 4 | 0 | confidence |
| 3 | Singapore | 35% | 360 s | 30 s | 26 | 0 | 0 | confidence |
| 4 | Singapore | 20% | 870 s | 60 s | 23 | 0 | 0 | confidence |
| 5 | Virginia | 15% | 885 s | 60 s | 0 | 1 | 0 | confidence |
| 6 | Singapore | 15% | never | 210 s | 0 | 0 | 0 | no proposal |
| 7 | Virginia | 35% | 405 s | 15 s | 18 | 9 | 0 | confidence |
| 8 | Singapore | 25% | never | 45 s | 0 | 2 | 0 | no proposal |
| 9 | Virginia | 30% | 600 s | 60 s | 16 | 4 | 0 | confidence |
| 10 | Virginia | 10% | 465 s | 75 s | 0 | 0 | 0 | confidence |
| 11 | Singapore | 35% | 30 s | 15 s | 50 | 2 | 0 | confidence |
| 12 | Virginia | 25% | never | 15 s | 0 | 2 | 0 | no proposal |
| 13 | Singapore | 10% | never | 45 s | 0 | 0 | 0 | no proposal |
| 14 | Singapore | 10% | never | 75 s | 16 | 0 | 0 | no proposal |
| 15 | Virginia | 25% | 435 s | 90 s | 0 | 0 | 0 | confidence |
| 16 | Virginia | 15% | never | 30 s | 0 | 1 | 0 | no proposal |
| 17 | Singapore | 35% | 750 s | 75 s | 15 | 1 | 0 | confidence |
| 18 | Singapore | 35% | 900 s | 45 s | 37 | 5 | 0 | confidence |
| 19 | Virginia | 20% | never | 75 s | 0 | 0 | 0 | no proposal |
| 20 | Singapore | 25% | 885 s | 0 s | 21 | 0 | 0 | confidence |

## Why nothing acted

* **AWS never voted bad.** The fault drops ICMP replies and TCP port-80 responses on the
  instance's way out. The app's own metrics (up, 5xx, p95) do not see it. This is the campaign's
  AWS-only 0/20 again.
* **RIPE voted bad in 10/20 trials**, for 15-50 of 65 ticks. The confidence rule uses the per-probe
  *median* loss of at least 20% (`end_to_end.network_health`), which is stricter than the frozen
  detector's pooled 10% quorum. That is why some trials the frozen detector flagged have no RIPE-bad tick here.
  It also works the other way. In slot 14 (10% loss), the RIPE-bad ticks come from one fully
  unreachable probe plus one lossy probe. With only 4 probes, the median still counts the
  unreachable one. The frozen detector excludes unreachable probes and did not flag that slot.
* **HTTP rarely voted bad, at most 9 of 65 ticks.** With 10-35% of response packets dropped, TCP
  retransmits slow requests down (often 1-1.9 s), but most still finish under the 2 s deadline with
  HTTP 200. Two signals bad for 3 windows in a row happened only for 1-6 ticks in slots 0, 7 and
  17, never for 3 windows.
* **The reviewer was never the blocker.** No snapshot in these windows had RIPE quality
  `loss_only`, so the reviewer change discussed for the lab error fault (option 2) would change
  nothing here.
* **The lab detector is noisy on real data.** It proposed shifts in every trial, including 10%
  loss, and also before faults started (see `shift_proposals_by_blamed_region.warmup_before_fault`
  in the JSON). Its 800 ms HTTP p95 threshold is a lab default, and Virginia's normal latency from
  the witness Mac is 480-1500 ms. The confidence rule is what kept those proposals from becoming actions.

**Reading.** The action path is safe on this data, with no false actions, but it does not act on
partial packet loss at 10-35%. The confidence rule was designed so that one signal can't move
traffic, and partial loss shows up clearly in only one signal. For it to act, the team would have
to change a rule, for example letting RIPE count alone when its loss is sustained, or judging HTTP
on a failure-or-slow *rate* over 60 s instead of one request. Any such change would be a new,
versioned rule that is then scored on these same trials and controls. It was not done here.

## Limits

* One campaign, one day, 20 trials, one HTTP witness location (MY's Mac).
* Traffic never actually moved, so the replay stops each slot at the first action. Client
  recovery after a move cannot be replayed.
* The route starts at 50/50. With Singapore as the 100% primary, a Virginia fault would have
  had no Virginia traffic to move.
* No HPA observation is passed to `decide`, so the optional destination-capacity check is skipped.
  `decide` allows this. It does not affect a result where nothing acted.
* The controller's own probes are replaced by the recorded witness requests (15 s cadence).
  Confidence windows count each 15 s tick, while RIPE rounds arrive every 240 s.
