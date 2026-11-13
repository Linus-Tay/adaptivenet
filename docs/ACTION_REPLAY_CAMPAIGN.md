# Action replay on the real 20-trial RIPE campaign

**Question.** The campaign showed that AWS+RIPE *detects* origin-path packet loss (13/20 vs 0/20
for AWS-only). Would the controller have *acted*, that is, moved traffic away from the impaired
region? The test uses the rules exactly as committed, on the real recorded data.

**Answer: almost never. 1 of 20 trials would have failed over, and there were 0 false actions.**
The false-action count covers the 20 same-time healthy control windows (4 h) and 1.6 h of fully
healthy gaps between slots. These numbers use the controller's committed HTTP deadline of 500 ms
(`controller/recovery.py` `DEADLINE_SEC = 0.5`).

* The one failover is slot 0 (Virginia, 35% loss), 210 s after the fault request, on "RIPE + HTTP bad for 5 windows".
* The detector flagged 13/20 trials, the same 13 as the campaign result, and the safety reviewer
  passed every proposal. The confidence rule (2 of the 3 signals AWS / RIPE / HTTP bad for 3 windows
  in a row) held 10 of them. The SafetyController's 3-fresh-checks streak held 2.
* For comparison, a 2 s HTTP deadline gives 0/20 and 0 false actions. An earlier version of this doc
  used 2 s by mistake: 2 s is only the HTTP transport timeout.

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
   origin: good means HTTP 2xx, served by that region, within 500 ms. This is the same test as
   `HttpMeasurement.met_deadline` in the lab controller.
3. A proposal, from two detectors run separately:
   * **frozen campaign detector**: `ml/team_starter/ripe_value.py`, the AWS-only fair rule OR the
     primary RIPE rule, mapped to a 25-point shift away from the single flagged region;
   * **lab rule detector**: `demo/lab/detectors.py` (`aws_ripe_http`) plus
     `demo/lab/controller.build_proposal`, exactly what the lab controller uses.
4. `controller.end_to_end.decide`: the safety reviewer as it is today.
5. `ConfidenceTracker.failover`, then `SafetyController(mode="local").step`. The witness sample is
   the controller-owned observation (3 fresh checks).

An **action** is the first tick where the SafetyController applies a 25-point step. The route
starts at 50/50, so a move is possible away from either region. The same harness also replays
every gap of at least 2 min in which no slot was running in either region (5 min after each slot
is excluded); any action there is a false action. Run it with:

```bash
python3 scripts/action_replay_campaign.py --campaign-dir /Users/my/Project/adaptivenet-ripe-campaign \
  --http-deadline-ms 500 --http-deadline-ms 2000 --out docs/ml/action-replay-campaign.json
```

The full per-slot output is in `docs/ml/action-replay-campaign.json`. `tests/test_action_replay_campaign.py`
checks the harness on synthetic fixtures. When RIPE and HTTP agree, it does act: 50→25 in 75 s.
When healthy, it never acts. Unconfirmed slots are excluded.

## Results

| | Frozen detector, 500 ms | Lab detector, 500 ms | Frozen detector, 2 s | Lab detector, 2 s |
| --- | --- | --- | --- | --- |
| Trials with a shift proposal during the fault | 13/20 | 20/20 | 13/20 | 20/20 |
| Proposals blocked by the safety reviewer | 0 | 0 | 0 | 0 |
| **Would fail over (25-point step applied)** | **1/20** (slot 0, 210 s) | **1/20** (slot 0, 210 s) | 0/20 | 0/20 |
| Held by the confidence rule | 10 | 16 | 13 | 20 |
| Held by the SafetyController (3 fresh checks) | 2 | 3 | 0 | 0 |
| No proposal | 7 | 0 | 7 | 0 |
| False actions, 20 control windows | 0 | 0 | 0 | 0 |
| False actions, 1.6 h healthy gaps | 0 | 0 | 0 | 0 |

Per trial. "Bad" counts are that signal's votes for the impaired region over the 15 s ticks in the
960 s fault window. Slot 0 at 500 ms stops at the action, so it has fewer ticks.

| Slot | Region | Loss | Frozen detector first flag | RIPE bad | HTTP bad (500 ms) | HTTP bad (2 s) | Frozen detector → | Lab detector → |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 0 | Virginia | 35% | 120 s | 31/65 | 13/15 | 7/65 | **fails over at 210 s** | **fails over at 210 s** |
| 2 | Virginia | 30% | 285 s | 0/65 | 51/65 | 4/65 | confidence | confidence |
| 3 | Singapore | 35% | 360 s | 26/65 | 12/65 | 0/65 | confidence | confidence |
| 4 | Singapore | 20% | 870 s | 23/65 | 19/65 | 0/65 | confidence | safety controller (3 fresh checks) |
| 5 | Virginia | 15% | 885 s | 0/65 | 38/65 | 1/65 | confidence | confidence |
| 6 | Singapore | 15% | never | 0/65 | 7/65 | 0/65 | no proposal | confidence |
| 7 | Virginia | 35% | 405 s | 18/65 | 45/65 | 9/65 | safety controller (3 fresh checks) | safety controller (3 fresh checks) |
| 8 | Singapore | 25% | never | 0/65 | 17/65 | 2/65 | no proposal | confidence |
| 9 | Virginia | 30% | 600 s | 16/65 | 45/65 | 4/65 | confidence | confidence |
| 10 | Virginia | 10% | 465 s | 0/65 | 41/65 | 0/65 | confidence | confidence |
| 11 | Singapore | 35% | 30 s | 50/65 | 25/65 | 2/65 | safety controller (3 fresh checks) | safety controller (3 fresh checks) |
| 12 | Virginia | 25% | never | 0/65 | 36/65 | 2/65 | no proposal | confidence |
| 13 | Singapore | 10% | never | 0/65 | 5/65 | 0/65 | no proposal | confidence |
| 14 | Singapore | 10% | never | 16/65 | 6/65 | 0/65 | no proposal | confidence |
| 15 | Virginia | 25% | 435 s | 0/65 | 46/65 | 0/65 | confidence | confidence |
| 16 | Virginia | 15% | never | 0/65 | 43/65 | 1/65 | no proposal | confidence |
| 17 | Singapore | 35% | 750 s | 15/65 | 26/65 | 1/65 | confidence | confidence |
| 18 | Singapore | 35% | 900 s | 37/65 | 18/65 | 5/65 | confidence | confidence |
| 19 | Virginia | 20% | never | 0/65 | 44/65 | 0/65 | no proposal | confidence |
| 20 | Singapore | 25% | 885 s | 21/65 | 12/65 | 0/65 | confidence | confidence |

## Why it rarely acted

* **AWS never voted bad.** The fault drops ICMP replies and TCP port-80 responses on the
  instance's way out. The app's own metrics (up, 5xx, p95) do not see it. This is the campaign's
  AWS-only 0/20 again.
* **RIPE voted bad in 10/20 trials**, for 15-50 of 65 ticks. The confidence rule uses the per-probe
  *median* loss of at least 20% (`end_to_end.network_health`), which is stricter than the frozen
  detector's pooled 10% quorum. In slot 14 (10% loss), the RIPE-bad ticks come from one fully
  unreachable probe plus one lossy probe. With only 4 probes, the median still counts the
  unreachable one; the frozen detector excludes unreachable probes.
* **HTTP at 500 ms is a real signal for Singapore but noise for Virginia.** From the witness Mac,
  healthy Singapore requests have a median of 45 ms and only 0.5% exceed 500 ms. Faulted Singapore
  windows reach 5-26 of 65 bad ticks. Healthy Virginia requests have a median of **500 ms**, and
  **50%** exceed 500 ms, so Virginia's HTTP vote is close to a coin flip whether or not there is a
  fault (36-51 of 65 in faulted Virginia windows). The slot 0 failover is real RIPE loss (31/65)
  coinciding with that HTTP vote. It was not a clean detection of the fault by HTTP.
* **RIPE and HTTP rarely agreed for 3 windows in a row.** RIPE updates every 240 s while HTTP flips
  every 15 s, so one good HTTP sample resets the streak. In slots 7 and 11 confidence passed, but the
  SafetyController's own 3-fresh-checks streak broke before a step.
* **No false actions**, even with Virginia's noisy HTTP vote. A move away from Virginia also needs
  a second bad signal, and in healthy times RIPE and AWS stayed good. Note one side effect: when
  Singapore is faulted, a Virginia HTTP "bad" vote also fails the "other region clean" check, which
  makes moving away from Singapore harder.
* **The reviewer was never the blocker.** No snapshot in these windows had RIPE quality `loss_only`,
  so reviewer option 2 (accept `loss_only` for the source region) would change nothing here.
* **The lab detector is noisy on real data.** It proposed shifts in every trial and before faults
  (`shift_proposals_by_blamed_region.warmup_before_fault` in the JSON). Its 800 ms HTTP p95 threshold
  is a lab default. The confidence rule kept those proposals from becoming actions.

**Reading.** On this data the action path is safe, with no false actions, but it acts on partial
(10-35%) packet loss in 1/20 trials. That one action relied partly on a deadline that the healthy
Virginia path misses half the time from this witness location. Acting reliably would need a rule
change, for example letting RIPE count alone when its loss is sustained, judging HTTP on a
failure-or-slow *rate* over 60 s, or a per-region HTTP deadline set from that path's own baseline.
Any such change would be a new, versioned rule scored on these same trials and controls. None was made here.

## Limits

* One campaign, one day, 20 trials, one HTTP witness location (MY's Mac). The Virginia HTTP result
  depends strongly on that location.
* Traffic never actually moved, so the replay stops each slot at the first action. Client recovery
  after a move cannot be replayed.
* The route starts at 50/50. With Singapore as the 100% primary, a Virginia fault would have had no
  Virginia traffic to move.
* No HPA observation is passed to `decide`, so the optional destination-capacity check is skipped,
  which `decide` allows.
* The controller's own probes are replaced by the recorded witness requests (15 s cadence).
  Confidence windows count each 15 s tick, while RIPE rounds arrive every 240 s.
* The healthy gaps are short (21 gaps, 1.6 h), on top of the 20 pre-registered 12-minute controls.
