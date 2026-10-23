# Model-driven mitigation: prediction -> decision -> action -> verified recovery

**Status (23 Oct 2026):** built and verified in the local lab (five uvicorn processes, real HTTP
probes, real route changes on the local gateway, emulated scale-out). Not verified: the
Kubernetes scale actuator against a live cluster, and any AWS route change (no AWS actuator exists;
the AWS/RIPE path runs the same controller in shadow mode only).

## What was missing in the repo before this change

- `ml/predictor.py` was never imported by any service; its rule baseline could only answer
  `hold`/`investigate`, so no prediction could ever lead to an action.
- `review_proposal()` always returned `executed=false` and rejected `scale_out` outright.
- `POST /controller/tick` crashed with HTTP 500 on every call (`forward()` returns a
  `JSONResponse`; the code indexed it like a dict), its probes bypassed the gateway so injected
  faults were invisible, and its "p95" of five samples was the maximum.
- There was no scale actuator, no failover action, no action journal and no recovery check.

## The loop now

```
features.v1 snapshot            AWS path: ingestion/features.py (AMP + RIPE + CloudFront)
                                local lab: controller/local_snapshot.py from three HTTP probe views
   |  build_model_input()        ingestion/handoff.py (unchanged contract)
   v
ml/predictor.predict()          cause per region -> proposal.v1
                                  trained model (ADAPTIVENET_MODEL_PATH, from ml/train.py) or
                                  deterministic dual-view rules (ml/rules.py)
   |  proposal: hold | investigate | shift_traffic | scale_out | failover (+affected_region, assessment)
   v
ingestion/decisions.review_proposal()   independent validation, unchanged defaults
                                  new knobs: mode, max_route_delta, scale_adapter_available
   |  policy_valid / rejection_reasons
   v
controller/mitigation.MitigationController.decide()
   - hysteresis: 3 identical proposals (2 for failover), cooldown 30 s
   - bounded route steps of 25 pp; failover = full switch in one step
   - never moves traffic into a region the model does not call healthy
   - substitution: scale_out with no scale adapter, or scale_out that did not recover
     within the verification window -> shift traffic away instead
   - verification: after each action, checks the recovery signal every tick until
     `recovered` (2 consecutive healthy checks) or `not_recovered` (deadline, default 90 s);
     a shift step whose users are still degraded is `superseded` by the next step at once
   - optional failback (CONTROLLER_AUTO_FAILBACK=true): restore the pre-incident weight in
     25 pp steps after 6 healthy ticks; off by default (the original "no automatic failback" rule)
   v
controller/actuators.py          GatewayRouteActuator (local route weights)
                                 EmulatedScaleActuator (local: PUT /admin/capacity on the workload)
                                 KubernetesScaleActuator (in-cluster: Deployment scale subresource,
                                   RBAC in infra/k8s/control-api-rbac.yaml)
                                 ShadowActuator (nothing changes)
   v
journal entry {action, region, from/to weight, model_id, proposal_action, actuation, verification}
```

### Recovery signal

| action | signal | source |
|---|---|---|
| shift_traffic / failover / failback | users' path healthy: 5xx < 5 % and worst probe latency < 800 ms | local: unpinned gateway probes; AWS: CloudFront `cloudfront_5xx_pct` (no latency) |
| scale_out | the region's own assessment is `healthy` again | internal + pinned external views |

### Cause rules (ml/rules.py), per region

| external (user path) | internal (in-region) | cause | action |
|---|---|---|---|
| degraded | healthy | network_degradation | shift_traffic |
| any | degraded | compute_overload | scale_out |
| any | critical (app down / errors >= 0.9) | critical_failure | failover |
| critical (total loss) | healthy | network_degradation | shift_traffic |
| unknown / stale | any | unknown | investigate (never acts) |

Thresholds: RTT p50 >= 800 ms or loss >= 20 % (external); HTTP p95 >= 800 ms, error ratio >= 0.20,
CPU >= 85 %, memory < 15 % (internal). Env overrides: `ADAPTIVENET_RTT_DEGRADED_MS`,
`ADAPTIVENET_LOSS_DEGRADED_PCT`, `ADAPTIVENET_P95_DEGRADED_MS`, `ADAPTIVENET_ERROR_DEGRADED`,
`ADAPTIVENET_CPU_DEGRADED_PCT`. **Calibrate the RTT threshold against each region's healthy RIPE
baseline before using it on real Atlas data; 800 ms is a local-lab HTTP number.**

## Endpoints (control API, port 8000, header `X-Lab-Token`)

| endpoint | purpose |
|---|---|
| `POST /controller/tick` | one observe -> predict -> review -> decide -> act -> verify cycle; returns `assessment`, `proposal`, `review`, `decision`, `applied`, `actuation`, `verification_closed`, `pending_verification`, `current_region_a_percent` |
| `GET /controller/observations` | the three probe views and the features.v1 snapshot built from them |
| `GET /controller/journal` | applied actions with verification state, pending ids, escalated regions |
| `PUT /controller/automation {"enabled": bool}` | toggle actuation at runtime (adaptive vs static runs) |
| `POST /controller/reset` | clear streaks, cooldown, journal, emulated replicas between runs |
| `GET /healthz` | now reports `automation_enabled` and `tick_seconds` |

Faults (`POST /experiments`, port 8001) gained `kind`: `network` (gateway, user path only) or
`compute` (inside the regional workload, visible to every observer).

## Configuration

| variable | default | meaning |
|---|---|---|
| `LOCAL_AUTO_MITIGATION` | false | actuate decisions (otherwise decide and journal only) |
| `CONTROLLER_TICK_SECONDS` | 0 | in-process control loop interval; 0 = manual ticks |
| `CONTROLLER_MODE` | combined | readiness gate: `combined` (both views) or `internal_only` |
| `CONTROLLER_COOLDOWN_SECONDS` | 30 | minimum gap between independent actions |
| `CONTROLLER_ROUTE_STEP_PERCENT` | 25 | route change per shift step |
| `CONTROLLER_VERIFY_AFTER_SECONDS` / `_DEADLINE_SECONDS` | 10 / 90 | verification window |
| `CONTROLLER_AUTO_FAILBACK` | false | restore baseline weight after recovery |
| `SCALE_ACTUATOR` | none | `emulated` (local lab), `kubernetes` (in-cluster, needs `SCALE_TARGETS=region-a=demo-a,region-b=demo-b`), `none` |
| `ADAPTIVENET_MODEL_PATH` | unset | trained bundle from `ml/train.py`; unset = rules |
| `ADAPTIVENET_PREDICTOR` | `ml.predictor:predict` | alternative predictor entry point |

## Run it

```bash
python -m venv .venv && source .venv/bin/activate && pip install -e '.[dev]'
python -m pytest -q                      # 142 tests (one skipped without scikit-learn)
python scripts/smoke.py                  # original smoke still passes
python scripts/mitigation_demo.py        # real processes: healthy, network, compute, critical
```

Manual: `python scripts/local.py` (sets the probe URLs and `SCALE_ACTUATOR=emulated`), then with
`LOCAL_AUTO_MITIGATION=true` inject a fault on 8001 and `POST /controller/tick` on 8000 every few
seconds, or set `CONTROLLER_TICK_SECONDS=5`. Docker: `compose.yaml` carries the same variables.

AWS/RIPE path, shadow only: `python scripts/model_handoff.py controller --mode combined` runs the
same model -> review -> decision on the latest stored snapshot and prints the decision.

Train a model once labeled runs exist:
`python ml/train.py --data data/ml-snapshots.labeled.jsonl --out data/cause_model.pkl` (labels
`healthy` or `<region>:<cause>`; needs `pip install -e '.[ml]'`), then set `ADAPTIVENET_MODEL_PATH`.

## Measured in the local lab (experiments/results/local-mitigation-2026-10-23/, tick 2 s, one run each)

| scenario | detection -> first action | action | steps | users recovered after |
|---|---|---|---|---|
| healthy | no action in 4 ticks | hold | 0 | n/a |
| network (A +1500 ms on user path) | 3 ticks, 9.6 s | shift_traffic 100 -> 75 -> 50 -> 25 -> 0 | 4 | 55.1 s |
| compute (A +1200 ms in region) | 3 ticks, 8.5 s | scale_out (emulated replicas 1 -> 2) | 1 | 17.6 s |
| critical (A 100 % errors) | 2 ticks, 2.7 s | failover 100 -> 0 | 1 | 9.5 s |

One run per scenario on one machine: these are a functional check, not an evaluation. The network
case recovers slowly by design (25 pp steps, the reviewer's original limit); set
`CONTROLLER_ROUTE_STEP_PERCENT=100` to compare against full immediate shifts.

## Limits, stated plainly

- Local scale-out is **emulated** (`emulated: true` in the journal): the workload divides the
  injected overload across "replicas". Real scaling is the Kubernetes actuator, unverified live.
- No AWS route actuator (Route 53 / CloudFront origin weights) exists; the AWS path is shadow.
- The rules predictor is a baseline, not the trained model the proposal promised; `ml/train.py`
  only has plumbing tests on synthetic rows, which say nothing about accuracy.
- Cause classes are per region and single-cause; two simultaneous faults pick the worst one.
- Verification trusts five probes per tick; the `recovered` verdict at a partial weight is possible
  when no probe happens to hit the slow region twice in a row (worst-of-5 reduces, not removes, this).
