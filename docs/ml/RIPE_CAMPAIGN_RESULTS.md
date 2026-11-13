# Campaign results: 20 packet-loss trials, AWS-only vs AWS+RIPE (2026-11-12/13)

This is the pre-registered campaign from `docs/ml/RIPE_TRIALS_PROTOCOL.md`. Its
plan, with deviations, is in `docs/ml/ripe-campaign-plan-20261112.json`. The run was
2026-11-12 15:22 UTC to 2026-11-12 23:57 UTC, with RIPE measurements 236862553
(Singapore) and 236862555 (Virginia), 240 s, using the same 4 probes as before.

## Headline

| | AWS-only (fair rule) | AWS+RIPE (frozen primary rule) |
|---|---:|---:|
| Impaired windows detected (n = 20) | **0** | **13** (65%, exact 95% CI 41–85%) |
| Same-time healthy control windows alarmed (n = 20) | 0 | **0** (exact 95% upper bound 17%) |

* **Paired, by episode:** 13 detected by RIPE only, 0 by AWS-only only, 7 by
  neither. Exact McNemar test: **p = 0.00024**. Episode-bootstrap 95% CI for the
  recall gain: **+45 to +85 points**.
* **Both regions:** Virginia 7/10 (exact 95% CI 35–93%), Singapore 6/10 (26–88%).
* **Delay** from the logged SSM request to the first RIPE alert, for detected
  trials: median **459 s**, range 22–892 s. That's about 2 RIPE rounds.
* **The secondary variants gave identical results.** `loss_only` and `loss_or_rtt2`
  also scored 13/20 detected with 0/20 control alarms. Every detection came from
  the loss quorum.

### Detection by configured loss level

| Loss | Trials | AWS-only | AWS+RIPE |
|---:|---:|---:|---:|
| 35% | 6 | 0 | **6** |
| 30% | 2 | 0 | **2** |
| 25% | 4 | 0 | 2 |
| 20% | 2 | 0 | 1 |
| 15% | 3 | 0 | 1 |
| 10% | 3 | 0 | 1 |

At 30–35% loss, RIPE detected 8 of 8. At 10–25% it detected 5 of 12. With 4 probes
× 3 pings per 4-minute round, light random loss often never reaches the frozen
quorum (≥2 probes losing packets and ≥10% pooled loss within 8 minutes). This is
the detection limit of a 4-probe, 240 s RIPE setup. It's not a tuning artefact,
because the rule was not changed after the data arrived.

## Why AWS-only scores 0

AMP was exported read-only with the collector's PromQL, at a 30 s step. Over the
whole campaign (1,053 points per region), app_up was 1, 5xx was 0, and the
server-side p95 stayed at 4.75–5.5 ms throughout, impaired or not. AWS-only had
complete data, so this isn't missing data or an abstention. The loss is on the
network path, after the handler has already finished, and the application's own
metrics have nothing that could react to it.

## Deviations (all recorded in the plan file, none changes an analysis rule)

1. **Attempt 1** (08:42 UTC): slot 0 never reached AWS. The CLI's SSO refresh hung
   on a black-holed IPv6 route. The run was paused before any server change and
   later re-started from the Desktop button with a fresh plan (same seed).
2. **Slot 1** (Singapore 25%) was refused on the instance: `iptables` was not
   installed on the Singapore EC2. No rule was added, so it's excluded. MY approved
   installing `iptables-nft`, the same command the team used on Virginia, and ran
   it. Make-up slot 20 (Singapore 25%) ran at the end.
3. MY's SSO session was renewed once mid-run (00:05 SGT) after the session length
   was changed. No slot was affected.
4. The per-probe RTT baseline needs 2 h of history from the new measurements, so the
   RTT trigger was effectively inactive for roughly the first 4 slots. That doesn't
   change the result: all detections came from the loss quorum, and the
   `loss_only` variant gives identical numbers.

## What this supports

On 20 controlled, independently confirmed packet-loss impairments across both
origins, AWS-internal monitoring detected none and AWS+RIPE detected 13, with no
false alarms in 20 same-time healthy windows (p = 0.00024). RIPE reliably sees
loss of ≥ 30% within about 2 rounds. Lighter loss is often missed with this probe
count.

Caveats to keep with the claim:
* Controlled impairment of one kind (random drop of ICMP echo-replies and port-80
  responses).
* 4 probes per region.
* Same-time controls cover 8-minute windows; the false-alarm rate per day comes
  from the approved Nov 10 healthy window (`RIPE_VALUE_STUDY.md`). There, the
  loss-only rule raised about 3 alarm onsets a day and the RTT trigger far more.
* Ground truth is the SSM-confirmed firewall rule, not RIPE, so these labels don't
  have the circularity of the Nov 10 pilot.

## Reproduce

```bash
python -m ml.team_starter.amp_export --region region-a --start <unix> --end <unix> --out <dir>/amp-region-a.jsonl
python -m ml.team_starter.amp_export --region region-b --start <unix> --end <unix> --out <dir>/amp-region-b.jsonl
python -m ml.team_starter.ripe_value study --ripe region-a=<dir>/ripe/236862553.json \
  --ripe region-b=<dir>/ripe/236862555.json --campaign-dir <dir> --campaign-plan <dir>/campaign-plan.json
```
