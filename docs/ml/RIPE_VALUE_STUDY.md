# Does RIPE Atlas add detection value beyond AWS-only monitoring?

**Answer (2026-11-12): yes, for network packet loss on the origin path, with a small
sample.** In the 6 reviewed Virginia packet-loss trials, AWS-only detected **0 of 6**
and AWS+RIPE detected **6 of 6**. Neither arm raised an alarm in the 4 reviewed
healthy controls. The exact McNemar test gives p = 0.031 (6 discordant episodes, all
in RIPE's favour). Every number here comes from the team's archived AWS feature
snapshots, the reviewed episode manifest and public RIPE Atlas results. Nothing is
fabricated. The limitations in section 6 are part of the result.

Code: `ml/team_starter/ripe_value.py` and `ml/team_starter/ripe_value_confound.py`.
Tests: `ml/team_starter/tests/test_ripe_value.py` (synthetic fixtures only).
Evidence used (read-only): `/Users/my/Project/adaptivenet-evidence/adaptivenet/data/ml-results/native/step2-network-approved/`
(`verified_network_dataset.json`, `verified_episodes.json`, `approved_chronological_split.json`),
`.../native/authorized-network-trials/*.json`, and public RIPE measurements 234317384,
234705665 and 235482125 (Virginia) plus 234316056, 234705187 and 235485629 (Singapore).

## 1. Headline result

The unit is one reviewed episode. Detectors are fixed rules, declared before they
were run against the outcomes, with no training and so no train/test leakage.

| Episode | Truth | AWS-only (fair rule) | AWS+RIPE | RIPE rounds alerting |
|---|---|---|---|---:|
| planned-control 15:45 Nov 9 | healthy | no alarm | no alarm | 0/12 |
| planned-control 16:01 Nov 9 | healthy | no alarm | no alarm | 0/11 |
| planned-control 16:26 Nov 9 | healthy | no alarm | no alarm | 0/12 |
| planned-control 18:05 Nov 9 | healthy | no alarm | no alarm | 0/11 |
| network-trial-1c97d1b18433 | 25% loss, VA | **miss** | detected | 35/37 |
| network-trial-9a546977616b | 25% loss, VA | **miss** | detected | 2/23 |
| network-trial-a65a252ed200 | 25% loss, VA | **miss** | detected | 11/16 |
| network-trial-ff63a063c834 | 25% loss, VA | **miss** | detected | 11/19 |
| network-trial-a7e5356532f8 | 25% loss, VA | **miss** (AWS data missing, 17/17 abstain) | detected | 5/19 |
| network-trial-31b8c3c6ebf6 | 25% loss, VA | **miss** | detected | 13/17 |

* The recall difference is +1.0. The episode-bootstrap 95% CI is degenerate at
  [1.0, 1.0] because all six pairs agree. With n = 6, read this as "every case went
  one way", not as a precise effect size.
* **Timing, independent of RIPE.** RIPE first alerted **61–521 s after the logged SSM
  impairment request** in all six trials (median 417 s). No alert was active at any
  request time. RIPE rounds are 240 s apart, so 1–2 rounds is the expected delay.
* The seventh request (`814b352e651a`, 08:52 Nov 10) shows only 1/119 RIPE packets
  lost and was never reviewed as effective. It isn't counted either way.

The "fair" AWS-only rule alerts on app down, a 5xx ratio ≥5%, robust-z ≥3, or p95
≥1.5× its own pre-event median. It's more sensitive than any threshold the project
used. The AWS+RIPE rule is AWS-only OR (in the trailing 480 s, ≥2 distinct
reachable probes lost packets and pooled loss ≥10%) OR (≥1 path's RTT ≥50 ms
**and** ≥1.3× its own trailing 6 h median). Persistently unreachable probes are
excluded from loss.

## 2. Why AWS-only can't see this fault

During **every** degraded row, the archived AWS application signals for Virginia are
**bit-identical** to the healthy controls. All 49 healthy rows and 82 of 109
degraded rows have exactly `app_up=1`, `5xx=0`, `error_ratio=0`, `p95=4.75 ms`,
`robust_z=0` and `rps=0.0667`. The remaining 27 degraded rows are AMP gaps (`None`)
or small variants with a missing robust-z or `rps=0.0632`. `http_p95_ms` is the application's own server-side handler
histogram (`services/adaptivenet/ingestion/sources.py:26`), so it can't include
client-to-origin transit time. The response loss happens on the network path, after
the handler has finished.

Over the same windows, RIPE saw 8–24% loss across 4 independent probes. The same
probes saw 0.0% loss during the healthy controls. The single-vantage HTTP witness
also saw the trials (300 s p95 of about 800–3,500 ms against about 500–770 ms when
healthy), but it can't say whether the problem is the origin path or the Mac's own
network. RIPE's four probes in different networks can.

## 3. Why the original three arms tied at 50% recall

1. **The held-out test had no healthy rows.** An "always degraded" model therefore
   scores the same as a real detector. All three saved models labeled all 17 rows
   of `31b8c3c6ebf6` as degraded and abstained on all 17 rows of `a7e5356532f8`.
2. **The trained AWS-only model used a confound.** Its largest coefficients are
   Virginia `memory_available_bytes` and `memory_available_pct` (−1.75 toward
   degradation). Virginia free memory fell from about 567–571 MB to about 546–561 MB
   at roughly 09:00 Nov 10, when the trial tooling was set up. It **stayed low after
   the trials ended** (14:00–16:00 median about 548–554 MB), so it tracks the
   experiment setup, not network state. If you retrain the identical pipeline without
   host memory, CPU and load columns, AWS-only alerts on **0** held-out rows. With
   the same columns removed, `aws_ripe_path` alerts on 19/19, 16/17 and 19/19 rows of
   the three trials where AWS was ready (`ripe_value_confound.py`).
3. **A shared readiness gate.** Every arm abstains whenever AWS internal data is
   stale (`dataset.py` `make_dataset`). External evidence can never rescue an AWS
   gap, which is why `a7e5356532f8` was 17/17 abstentions in every arm even though
   RIPE saw the loss. A future gate should let the external arms decide on
   external-only evidence and record `aws_internal_unavailable` as a reason.
4. **The RIPE representation erased the signal.** Probe 1010367 never reached the
   Singapore origin (100% loss in all 1,373 of its results), which pins Singapore
   mean loss at about 25%. Real path events also tend to hit one probe at a time,
   so regional aggregates hide them. The new per-probe features fix both.

## 4. Unlabeled time (false-alarm upper bound)

Outside all reviewed episodes (±30 min), over about 82 h per region from Nov 8 to Nov 12:

| Region | Loss-quorum onsets | RTT-inflation onsets | Rate |
|---|---:|---:|---|
| Virginia | 3 | 5 | 2.3/day (loss only: 0.9/day) |
| Singapore | 0 | 55 | 16/day |

This time is **unlabeled**, so these are upper bounds, not verified false alarms.
Most Singapore onsets are real, recurring external path congestion. Probe 1008208's
path to Singapore rises from about 149 to about 280 ms almost every day around
06:00–10:00 UTC. Probe 1015838's path went from 96 to about 204 ms for 2 h 45 m
(Nov 10 21:29 to Nov 11 00:13 UTC), while the same probe's Virginia path and the
other Singapore probes stayed flat. These are gray failures for users behind those
networks, but nobody has reviewed them. For an alerting deployment, the RTT rule
needs a quorum of ≥2 paths, or time-of-day baselines, before it should page anyone.

## 5. Claims

**Supported:** On the team's reviewed packet-loss experiments, RIPE Atlas detected
network degradation that AWS-only monitoring could not detect at all (0/6 vs 6/6,
no false alarms in 4 healthy controls, McNemar p = 0.031). It did so within 1–2 RIPE
rounds of the impairment request. The AWS-only failure is structural: its signals
didn't change.

**Not supported:**
* Earlier detection *when both detect*. AWS-only never detected, so there is no
  paired delay.
* Generalization to other fault types. For service-stop failures, AWS-only already
  works.
* Forecasting. The horizon is 0.
* A verified false-alarm rate per day.

## 6. Limitations (read with the result)

* **Six positive and four healthy episodes, one region, one fault type, one day**
  (healthy controls Nov 9, trials Nov 10). Small n, so p = 0.031 is the smallest
  p-value possible with six discordant pairs.
* **Partly circular labels.** The reviewed onset of each trial was defined as "first
  observed RIPE loss", and RIPE was one of the label evidence sources. Detection *at
  the reviewed onset* is therefore partly by construction. The RIPE-independent
  checks are the firewall injection log (SSM request and configured duration), the
  HTTP witness, and the request-to-alert delays above. The AWS-only result doesn't
  depend on the labels at all, because its inputs were identical in both classes.
* The RIPE rule thresholds were written after the trial configuration (25% loss) was
  known, but before running the rule against the outcomes. They weren't fitted to
  these episodes. A truly prospective test needs new trials.
* Review was an AI technical review with reported (not signed) human approval. See
  `verified_episodes.json` notes.
* The Singapore candidate events are unreviewed and are not labels.

## 7. What would strengthen it

1. Further impairment trials in **Singapore**, plus healthy controls on the **same
   days** as the trials, to remove the day confound. Review the onset from the
   injection log, not from RIPE.
2. Keep 240 s RIPE measurements running. The current ones end 2026-11-12 08:47 UTC,
   and re-cloning spends RIPE credits.
3. Change the readiness gate so external arms can act on external-only evidence (section 3.3).
4. Drop host memory, CPU and load from network-degradation features, or treat them as
   confounders and stratify by day.

## 8. Reproduce

```bash
# public RIPE results (read-only, no key); repeat per measurement, <=7-day chunks handled
python -m ml.team_starter.ripe_value fetch --msm 235482125 --start <unix> --stop <unix> --out data/ripe-public/235482125.json

N=data/ml-results/native
python -m ml.team_starter.ripe_value study \
  --ripe region-a=data/ripe-public/234316056.json --ripe region-a=data/ripe-public/234705187.json \
  --ripe region-a=data/ripe-public/235485629.json --ripe region-b=data/ripe-public/234317384.json \
  --ripe region-b=data/ripe-public/234705665.json --ripe region-b=data/ripe-public/235482125.json \
  --dataset $N/step2-network-approved/verified_network_dataset.json \
  --trials-dir $N/authorized-network-trials --out $N/ripe-value

python -m ml.team_starter.ripe_value_confound \
  --dataset $N/step2-network-approved/verified_network_dataset.json \
  --split $N/step2-network-approved/approved_chronological_split.json \
  --ripe region-b=data/ripe-public/235482125.json --ripe region-b=data/ripe-public/234705665.json \
  --ripe region-b=data/ripe-public/234317384.json --ripe region-a=data/ripe-public/235485629.json \
  --ripe region-a=data/ripe-public/234705187.json --ripe region-a=data/ripe-public/234316056.json

python -m pytest -q ml/team_starter/tests/test_ripe_value.py
```

Everything is shadow-only and makes no AWS calls. Raw RIPE results and native
evidence stay under the ignored `data/` directory.

Integration note: `integration/end-to-end` adds `packet_loss_median_pct` and
`ripe_probes_full_loss` to `features.py`, so the controller is no longer pinned by
the dead Singapore probe. The per-probe loss quorum and RTT-inflation features here
are what actually separated the trials from the controls. They're the ones to carry
into the live feature contract.
