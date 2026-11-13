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

## Live execution (needs explicit approval)

`execute` runs only for a `scale` decision and only with `--approve-live-write <site>` matching
the plan's site. It then uses the existing `SafetyController(mode="eks")` loop (three fresh
observations, journal, atomic HPA minimum patch). Failover is always refused.
No live write has been run from this branch.
