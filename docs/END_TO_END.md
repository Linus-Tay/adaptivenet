# End-to-end decision path

`integration/end-to-end` combines `feature/ml-team-starter-20261104`, `safety-controller`
and `eksdeployment`, and adds one path from a model proposal to an EKS decision:

```
model proposal (shadow_predict or proposal.v1 JSON)
  -> map_model_output   (condition -> candidate shift_traffic / scale_out, still untrusted)
  -> safety reviewer    (review_proposal / scale_proposal_rejection_reasons)
  -> EKS decision       scale | failover | no-op   (services/adaptivenet/controller/end_to_end.py)
```

## Dry run (default)

```bash
python3 scripts/end_to_end.py decide --snapshot features.json --proposal proposal.json \
  --hpa region-a=hpa-sg.json --current-region-a-percent 50
```

Use `--model model.pkl --model-input model_input.json` instead of `--proposal` to run the
trained shadow model first. `--kubeconfig-sg` / `--kubeconfig-us` read HPA state through the
existing read-only allowlisted kubectl paths. `decide` never writes.

## Mapping (v1, for team review)

| Model condition | Candidate |
| --- | --- |
| `network_degradation`, `partial_outage` | shift 25 points away from the single degraded region |
| `application_failure`, CPU >= 80% in that region | `scale_out` in that region |
| `application_failure` otherwise | shift away from the degraded region |
| `healthy`, `normal`, `recovery`, `transient`, abstain | no-op |

A region counts as degraded when internal quality is fresh and `app_up == 0`, the error ratio
is >= 0.20 or p95 is >= 800 ms. It also counts as degraded with healthy internals (a gray,
network-only failure) when external RIPE quality is fresh, at least 3 probes report, and the
per-probe **median** loss is >= 20%, p50 RTT is >= 400 ms, or every probe is unreachable.
The mean `packet_loss_pct` is not used: one permanently unreachable probe out of four pins it
at 25%. Snapshots now carry `packet_loss_median_pct` and `ripe_probes_full_loss`; a partly
unreachable probe set is flagged as `ignored_unreachable_ripe_probes:<n>` in
`mapping.signals`. If both or neither region is degraded, or the other region is unknown,
the decision is no-op.

## Decisions

* **scale**: reviewer passes, HPA observation present, CPU at or above the HPA target and
  `recommend_capacity` recommends one more replica (the existing `ScalingLimited` block stays).
* **failover**: reviewer marks the shift eligible; if a destination HPA observation is
  supplied it must be fresh with ready replicas at or above its minimum. There is no EKS traffic
  actuator, so the plan names the local gateway as the only executor.
* **no-op**: everything else, with the reviewer's reasons.

## Fit with the recorded SSM outage experiments

The SSM experiments (23 Oct and 9 Nov 2026) stopped the application service, not the host:
application scrapes failed (`app_up = 0`), node exporter, CPU and memory stayed normal, and the
other region stayed healthy. That shape maps to `application_failure` -> **failover** away from
the stopped region, never to **scale** (CPU stays low, and more replicas of a stopped service
would not help). `tests/test_end_to_end.py::test_ssm_service_stop_shape_maps_to_failover_not_scale`
covers both the Singapore and Virginia runs. With Singapore as the primary origin, pass
`--current-region-a-percent 100`; the first step is then 100 -> 75 (the reviewer's 25-point limit).

The report did not establish that CloudFront served from Virginia, or the switching time, so the
failover plan records CloudFront origin failover as unverified. These were application-level
interruptions, so they do not exercise the network-only (RIPE) path.

## Confidence rules (before any action)

`services/adaptivenet/controller/confidence.py` adds one small rule set in front of the reviewer and
`SafetyController`. The rules can only make the controller wait; they never approve anything on their
own. They read raw measurements only, never ML or detector output, so ML stays shadow-only.

A **window** is one new feature snapshot (every 10–15 s in the lab, where the controller ticks every 5 s; a repeated snapshot counts once).
In each window three independent signals vote on each region: **AWS** (app down, error ratio >= 20% or
p95 >= 800 ms), **RIPE** (median probe loss >= 20%, p50 RTT >= 400 ms or every probe unreachable; needs
3+ fresh probes) and **HTTP** (the controller's own client request failed, was served by the wrong region
or missed the 500 ms deadline; the request itself times out at 2 s). Stale or missing data votes *unknown*, never good.

**Post-campaign rule (`ripe_loss_only_v1`, added 13 Nov 2026).** When every RIPE probe is lost
there is no RTT, ingestion marks RIPE quality `loss_only`, and `network_health` above returns unknown.
The confidence rules judge that case on loss alone (all probes lost, or median loss >= 20%, counts as a
bad RIPE vote). This is controller-side only: `network_health`, `decide()` and the frozen campaign
detector (`ml/team_starter/ripe_value.py` on `ripe-detection-evidence`) are unchanged. It does not alter
any of the 20 campaign trial outcomes: those were scored by the frozen detector, which never runs this
code. Even if it had, 10-35% injected loss almost always leaves some RTT replies, so RIPE quality would
stay `fresh` and this rule would not apply.

| Action | Rule (defaults) |
| --- | --- |
| Failover (25 points away) | 2 of 3 signals bad, for 3 windows in a row; the other region has no bad vote and AWS says it is up; no reversal within 120 s of a failback step. Each further step needs 3 fresh bad windows. |
| Failback (25 points back to Singapore) | Singapore has no bad vote and at least 2 good votes for 120 s in a row (any bad window restarts the clock); 120 s since the last failover step. A rolled-back step restarts the clock. |
| Scale up (HPA min +1) | CPU >= 80% with the app up, for 3 windows in a row; 300 s since the last scale change. A stopped app is a failover case, not a scale case. |
| Scale down (HPA min -1) | CPU <= 30%, error ratio < 5% and the app up, for 300 s in a row; 300 s since the last scale change; never below the baseline minimum. Recommendation only. |

After these pass, every existing gate still applies unchanged: reviewer, three fresh controller probes,
25-point step limit, journal, client recovery check and rollback. So in the lab a failover takes at least
3 confidence windows plus the controller's 3 probes (about 60 s in the lab run below).

Examples: the SSM service-stop shape (AWS `app_up = 0` and client HTTP failing, RIPE fine) passes as
AWS + HTTP. A gray network fault (RIPE loss and slow client requests, AWS fine) passes as RIPE + HTTP.
RIPE alone, or a single slow client probe, never moves traffic.

Where they run: the demo lab controller (`demo/lab/controller.py`) applies them every tick and logs the
votes, streaks and verdicts under `confidence` in `decisions.jsonl`. `scripts/end_to_end.py decide`
still judges one snapshot and is unchanged; it has no window history and no HTTP probe. Thresholds can be
overridden for the lab with `CONFIDENCE_<FIELD>` environment variables (for example
`CONFIDENCE_FAILOVER_BAD_WINDOWS=4`); `FAILBACK_HEALTHY_SEC` still works. Tests:
`tests/test_confidence_rules.py`, `demo/tests/test_failback.py`.

### Local lab check (13 Nov 2026, `demo/run.sh lab-up`, `FAILBACK_HEALTHY_SEC=60`)

* **1.5 s latency on Singapore for 120 s.** For the first four ticks the detector already said
  `network_degradation` but no signal had turned bad, so the controller held. RIPE + HTTP then turned bad,
  and after 3 bad windows the controller's own 3 probes ran and moved the gateway from 100% to 75% about
  70 s after the fault started (this run; a later run on `submission/final`, the one quoted in the README,
  took about 93 s, because the timing depends on when the snapshot windows and probes line up). The next step began collecting fresh windows, but the fault ended first,
  so no further step was taken. Singapore stayed clean for 60 s, the failback then waited for the 120 s
  no-reversal rule, made its 3 probes and went from 75% back to 100%.
* **100% gateway errors on Singapore for 120 s.** With `ripe_loss_only_v1`, RIPE + HTTP agreed for 3 windows
  within about 30 s. The existing reviewer still refused with `telemetry_quality_gate_failed` (RIPE quality
  is `loss_only`), so traffic did not move. This is the existing gate's documented behaviour and was not
  changed. After the fault ended the detector kept saying `network_degradation` for about 45 s, while
  every signal was already clean, so the confidence rules held that stale failover.
* Scale up and scale down were covered by unit tests only, because the lab never executes scaling and the
  lab HPA always sits at its baseline minimum.

## Live execution (needs explicit approval)

`execute` runs only for a `scale` decision and only with `--approve-live-write <site>` matching
the plan's site. It then uses the existing `SafetyController(mode="eks")` loop (three fresh
observations, journal, atomic HPA minimum patch). Failover is always refused.
No live write has been run from this branch.
