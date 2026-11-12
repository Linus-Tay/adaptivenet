# Does RIPE Atlas add detection value beyond AWS-only monitoring?

**Status (2026-11-12):** partly established. The diagnosis and the external-path
events below come from real public RIPE Atlas data. The paired AWS-only versus
AWS+RIPE result still needs the archived AWS feature snapshots, so it is **not yet
computed**. No numbers on this page are invented, and none come from a fixture.

Code: `ml/team_starter/ripe_value.py`. Tests: `ml/team_starter/tests/test_ripe_value.py`
(synthetic fixtures only).

## 1. Why the three existing arms tied (50% recall, 17/34 abstentions in every arm)

| Cause | Evidence |
|---|---|
| Every arm abstains on the same rows. `ready_for_shadow_assessment` requires fresh AWS internal data in every arm, and the RIPE and HTTP arms only add extra conditions. External data can never rescue an AWS-side abstention, so the 17/34 is shared by construction. | `dataset.py` `make_dataset` sets readiness to `internal_ok and (aws_only or external_ok) ...` |
| The snapshot RIPE columns are regional aggregates. Probe **1010367 never reached the Singapore origin**: 100% loss in every one of its 1,373 results across msm 234316056, 234705187 and 235485629, yet the same probe reaches Virginia normally. The mean `packet_loss_pct` for Singapore is therefore pinned at about 25%, whatever the real path state. | Public results, section 3 |
| Real path degradations hit **one probe at a time**. In 29 of the 30 Singapore candidates below, the other Singapore probes stayed normal. A regional p50 barely moves, and loss doesn't move at all. | `other_probes_same_region_inflated_fraction` in the report |
| RIPE rounds are 240 s (Nov 8 onward) or 900 s (Oct). A test episode of a few minutes contains zero or one new RIPE result. | Measurement metadata |
| `http_p95_ms` (AWS-only) is the application's own server-side handler histogram (`adaptivenet_http_request_duration_seconds`, 5 m rate). It **cannot contain client-to-origin transit time**, so on a pure path degradation AWS-only has nothing to react to. | `services/adaptivenet/ingestion/sources.py:26` |

So the tie doesn't mean RIPE is useless. The test episodes were faults that AWS
internals could already see, the RIPE representation erased path-level signal, and
the readiness gate made the arms identical wherever AWS data was unusable.

## 2. What was added

* `fetch`: read-only download of **public** RIPE results (no key, nothing written to RIPE or AWS).
* Per-probe, **past-only** path features: RTT inflation against each probe's own
  trailing 6 h median, the number of inflated paths, loss among *reachable* probes
  only, a separate count of unreachable probes, and result age. Stale or absent
  values stay `None`, never 0.
* A candidate external-path event finder (≥50 ms **and** ≥1.3× over baseline,
  sustained ≥2 rounds). Each event has a cross-region control (the same probe
  toward the other origin) and a peer check (other probes toward the same origin).
  **Candidates are not labels**, so `status=candidate_unreviewed`.
* A fair AWS-only detector: app down, 5xx ratio ≥5%, robust-z ≥3, or p95 ≥1.5×
  its own pre-event median. This is deliberately more sensitive than the trained
  model, so AWS-only isn't handicapped.
* A paired, episode-level comparison: exact McNemar on discordant episodes, an
  episode-bootstrap 95% CI for the recall difference, and the paired delay gain on
  jointly detected episodes.
* A native fourth arm, `aws_ripe_path`: the row's exact `aws_only` values plus the
  path features, retrained on the **identical** reviewed chronological split, with
  the same logistic pipeline and threshold. The two arms differ only by external
  path information.

The thresholds were fixed before looking at any AWS data or labels.

## 3. Real external-path events in the public RIPE data (Nov 8–12 2026, 240 s rounds)

Measurements: Singapore 234316056, 234705187, 235485629 and Virginia 234317384,
234705665, 235482125. This covers 10,983 results through 2026-11-12 07:23 UTC.
There are 32 candidates in all (30 Singapore, 2 Virginia). The candidates lasting
≥30 minutes are:

| Region | Probe | Start (UTC) | End (UTC) | Rounds | Baseline ms | Peak ms | Same probe to other region | Other probes same region |
|---|---|---|---|---:|---:|---:|---|---|
| SG | 13106 | 11-09 12:39 | 11-09 15:31 | 44 | 170 | 311 | normal | 9% / 0% |
| SG | 13106 | 11-10 00:27 | 11-10 02:59 | 39 | 172 | 267 | **also flagged** | 0% / 0% |
| SG | 1008208 | 11-10 06:04 | 11-10 06:36 | 9 | 149 | 273 | normal | 0% / 0% |
| SG | 1008208 | 11-10 07:20 | 11-10 08:39 | 21 | 149 | 287 | normal | 0% / 0% |
| SG | 13106 | 11-10 14:16 | 11-10 16:11 | 30 | 185 | 291 | normal | 0% / 0% |
| SG | **1015838** | **11-10 21:29** | **11-11 00:13** | **42** | **96** | **211** | normal (≈230 ms flat) | 0% / 0% |
| SG | 1008208 | 11-11 08:13 | 11-11 10:37 | 37 | 149 | 284 | normal | 0% / 0% |
| SG | 13106 | 11-12 01:23 | 11-12 02:28 | 17 | 173 | 258 | normal | 0% / 0% |

The clearest case is probe 1015838. Its RTT to the Singapore origin roughly doubled
(96 to about 204 ms median) for 2 h 45 m. Over the same hours, the same probe's RTT
to Virginia stayed flat at about 230 ms, the other Singapore probes stayed at
baseline, and the TTL didn't change (116). So the probe's own access link isn't the
cause, the origin isn't globally slow, and there was no hop-count route change. It
was on-path congestion affecting users behind that network on their way to
Singapore. Probe 1008208 also shows recurring daily inflation (about 149 to 280 ms
around 06:00–10:00 UTC), which is a repeatable congestion pattern.

**Claim supported now:** RIPE Atlas observed real, sustained, path-specific
degradation toward the Singapore origin, and none of the AWS-only features can
measure it, because they are host and application metrics plus server-side handler
latency.

**Claim NOT yet supported:** "AWS-only did not alert during these events." That
needs the archived AMP feature snapshots for these hours (section 4). Until then,
this is a structural argument, not an empirical paired result.

## 4. What is still required

1. **AWS-side data for 2026-11-08 → 11-12.** Either option works:
   * `data/ml-archive/feature_snapshots.jsonl`, plus `verified-dataset.json`,
     `verified_episodes.json` and network reviews from the machine that produced the
     Step 8 study, or
   * read-only AMP query access (AMP keeps 150 days by default) to export the same
     `model_input.v1` features for those hours.
2. **Independent review** of each candidate before it's used as a label. Suitable
   evidence includes a second RIPE measurement type (traceroute) from the same
   probe, CloudFront per-edge latency, or another HTTP vantage. The RIPE detector
   must not grade itself, and without that review its precision is unknown.
3. **Healthy windows** with AWS and RIPE both normal, to estimate the RIPE-path
   false-alarm rate per day.
4. The 240 s measurements end at 2026-11-12 08:47 UTC. More events require
   re-cloning them, which spends RIPE credits and needs the team's API key.

## 5. Commands

```bash
# public results (repeat per measurement); written under ignored data/
python -m ml.team_starter.ripe_value fetch --msm 235485629 --start <unix> --stop <unix> --out data/ripe-public/235485629.json

# candidates only
python -m ml.team_starter.ripe_value study \
  --ripe region-a=data/ripe-public/234316056.json --ripe region-a=data/ripe-public/234705187.json \
  --ripe region-a=data/ripe-public/235485629.json --ripe region-b=data/ripe-public/234317384.json \
  --ripe region-b=data/ripe-public/234705665.json --ripe region-b=data/ripe-public/235482125.json

# + AWS join (paired AWS-only vs AWS+RIPE on each event)
...  --snapshots data/ml-archive/feature_snapshots.jsonl

# + native retrain on the reviewed split (aws_only vs aws_ripe_path)
...  --dataset data/ml-archive/verified-dataset.json --labels data/verified_episodes.json \
     --reviews data/network_binary_reviews.json

python -m pytest -q ml/team_starter/tests/test_ripe_value.py
```

Everything here is shadow-only and makes no AWS calls. Raw RIPE results stay under
the ignored `data/` directory.

Integration note: `integration/end-to-end` now adds `packet_loss_median_pct` and
`ripe_probes_full_loss` to `features.py`, so the safety controller is no longer
pinned by the dead probe. That fixes loss. It doesn't capture single-path RTT
inflation, which is what this study's per-probe baseline features are for.
